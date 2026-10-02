"""Tests for performance_cockpit.load_warehouse."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from performance_cockpit import load_warehouse as lw
from performance_cockpit.storage import LocalStorage

DATASET = "test-project.cockpit"


class TestRunLoaders:
    """Running the per-table loaders."""

    def test_runs_every_loader_in_order(self) -> None:
        calls: list[str] = []
        loaders = {name: (lambda n=name: calls.append(n) or 0) for name in lw.TABLES}

        assert lw.run_loaders(loaders) is True
        assert calls == list(lw.TABLES)

    def test_failure_does_not_stop_the_others(self) -> None:
        records = MagicMock(return_value=0)

        def broken() -> int:
            raise RuntimeError("laps exploded")

        ok = lw.run_loaders({"activities": lambda: 0, "garmin_laps": broken, "records": records})

        assert ok is False
        records.assert_called_once()

    def test_only_runs_selected_tables(self) -> None:
        activities, records = MagicMock(return_value=0), MagicMock(return_value=0)

        lw.run_loaders({"activities": activities, "records": records}, only=["records"])

        activities.assert_not_called()
        records.assert_called_once()


class TestTableLoaders:
    """Each table is wired to the right source files and parser."""

    def test_wiring(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        load_activities, load_per_activity = MagicMock(), MagicMock()
        monkeypatch.setattr(lw, "load_activities", load_activities)
        monkeypatch.setattr(lw, "load_per_activity", load_per_activity)
        storage, client = LocalStorage(tmp_path), MagicMock()

        loaders = lw.table_loaders(storage, client, DATASET, "test_user", reload_activities=True)
        for load in loaders.values():
            load()

        load_activities.assert_called_once_with(
            storage, client, f"{DATASET}.activities", "test_user", reload=True
        )
        laps_call, records_call = load_per_activity.call_args_list
        assert laps_call.args[2:7] == (
            f"{DATASET}.garmin_laps",
            "test_user",
            "splits",
            ".json",
            lw.parse_splits,
        )
        assert records_call.args[2:7] == (
            f"{DATASET}.records",
            "test_user",
            "fit",
            ".fit",
            lw.parse_fit,
        )


class TestMain:
    """Command line wiring."""

    def run_main(self, monkeypatch: pytest.MonkeyPatch, *args: str, ok: bool = True) -> MagicMock:
        """Run main() with fake BigQuery and loaders; return the run_loaders mock."""
        monkeypatch.setattr(lw.bigquery, "Client", MagicMock(name="Client"))
        run_loaders = MagicMock(return_value=ok)
        monkeypatch.setattr(lw, "run_loaders", run_loaders)
        monkeypatch.setenv("COCKPIT_USER_ID", "test_user")
        monkeypatch.setattr(sys, "argv", ["load-warehouse", *args])
        lw.main()
        return run_loaders

    def test_project_comes_from_dataset(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        run_loaders = self.run_main(monkeypatch, "--dataset", DATASET, "--data-dir", str(tmp_path))

        lw.bigquery.Client.assert_called_once_with(project="test-project")
        loaders, only = run_loaders.call_args.args
        assert list(loaders) == list(lw.TABLES)
        assert only is None

    def test_only_and_bucket(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gcs_cls = MagicMock(name="GcsStorage")
        monkeypatch.setattr(lw, "GcsStorage", gcs_cls)

        run_loaders = self.run_main(
            monkeypatch, "--dataset", DATASET, "--bucket", "b", "--only", "records", "garmin_laps"
        )

        gcs_cls.assert_called_once_with("b")
        assert run_loaders.call_args.args[1] == ["records", "garmin_laps"]

    def test_any_failure_exits_non_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(SystemExit) as exit_info:
            self.run_main(monkeypatch, "--dataset", DATASET, ok=False)

        assert exit_info.value.code == 1

    def test_dataset_is_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(SystemExit):
            self.run_main(monkeypatch, "--bucket", "b")
