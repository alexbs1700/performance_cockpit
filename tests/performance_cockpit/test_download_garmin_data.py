"""Tests for performance_cockpit.download_garmin_data."""

import json
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from garminconnect import (
    GarminConnectConnectionError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)

from performance_cockpit import download_garmin_data as dl

MakeZip = Callable[[dict[str, bytes]], bytes]


@pytest.fixture
def garmin(make_zip: MakeZip, activity_summary: dict[str, Any]) -> MagicMock:
    """A fake Garmin client serving one page of two activities."""
    client = MagicMock(name="garmin_client")
    activities = [
        {**activity_summary, "activityId": 1001},
        {**activity_summary, "activityId": 1002},
    ]
    client.get_activities.side_effect = lambda start, _limit: activities if start == 0 else []
    client.get_activity_splits.side_effect = lambda aid: {"activityId": int(aid), "lapDTOs": []}
    client.download_activity.return_value = make_zip({"1001_ACTIVITY.fit": b"FITDATA"})
    client.get_activity.side_effect = lambda aid: {**activity_summary, "activityId": int(aid)}
    return client


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the politeness pause so tests run instantly."""
    monkeypatch.setattr(dl.time, "sleep", lambda _seconds: None)


def run_main(monkeypatch: pytest.MonkeyPatch, client: MagicMock, *args: str) -> None:
    """Run the CLI entry point against a fake client with the given arguments."""
    monkeypatch.setattr(dl, "get_client", lambda: client)
    monkeypatch.setenv("COCKPIT_USER_ID", "test_user")
    monkeypatch.setattr(sys, "argv", ["download_garmin_data", *args])
    dl.main()


class TestIterActivities:
    """Pagination over the activity list."""

    def test_follows_pages_until_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dl, "PAGE_SIZE", 2)
        pages = {0: [{"activityId": 1}, {"activityId": 2}], 2: [{"activityId": 3}]}
        client = MagicMock()
        client.get_activities.side_effect = lambda start, _limit: pages.get(start, [])

        ids = [a["activityId"] for a in dl.iter_activities(client)]

        assert ids == [1, 2, 3]
        assert [c.args[0] for c in client.get_activities.call_args_list] == [0, 2, 3]

    def test_empty_account_yields_nothing(self) -> None:
        client = MagicMock()
        client.get_activities.return_value = []

        assert list(dl.iter_activities(client)) == []


class TestExtractOriginal:
    """Unpacking Garmin's ORIGINAL zip."""

    def test_prefers_fit_file(self, make_zip: MakeZip) -> None:
        archive = make_zip({"readme.txt": b"x", "1001_ACTIVITY.FIT": b"FITDATA"})

        assert dl.extract_original(archive) == (b"FITDATA", ".fit")

    def test_falls_back_to_first_file(self, make_zip: MakeZip) -> None:
        archive = make_zip({"1001.gpx": b"<gpx/>"})

        assert dl.extract_original(archive) == (b"<gpx/>", ".gpx")

    def test_empty_archive_raises(self, make_zip: MakeZip) -> None:
        with pytest.raises(ValueError, match="empty archive"):
            dl.extract_original(make_zip({}))

    def test_not_a_zip_raises(self) -> None:
        with pytest.raises(zipfile.BadZipFile):
            dl.extract_original(b"not a zip")


def test_to_json_keeps_non_ascii() -> None:
    assert json.loads(dl.to_json({"name": "Carrera"})) == {"name": "Carrera"}
    assert "ñ".encode() in dl.to_json({"name": "Mañana"})


def test_write_atomic_creates_parents_and_leaves_no_temp(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "file.json"

    dl.write_atomic(target, b"{}")

    assert target.read_bytes() == b"{}"
    assert list(target.parent.iterdir()) == [target]


class TestDownloadOne:
    """Storing a single activity."""

    def test_writes_splits_fit_and_summary(self, garmin: MagicMock, tmp_path: Path) -> None:
        dl.download_one(garmin, 1001, tmp_path)

        assert json.loads((tmp_path / "splits" / "1001.json").read_bytes())["activityId"] == 1001
        assert (tmp_path / "fit" / "1001.fit").read_bytes() == b"FITDATA"
        assert json.loads((tmp_path / "summary" / "1001.json").read_bytes())["activityId"] == 1001

    def test_missing_original_still_writes_summary(self, garmin: MagicMock, tmp_path: Path) -> None:
        garmin.download_activity.side_effect = GarminConnectNotFoundError("no file")

        dl.download_one(garmin, 1001, tmp_path)

        assert not (tmp_path / "fit").exists()
        assert (tmp_path / "summary" / "1001.json").exists()

    def test_failure_before_summary_leaves_activity_incomplete(
        self, garmin: MagicMock, tmp_path: Path
    ) -> None:
        garmin.get_activity.side_effect = GarminConnectConnectionError("boom")

        with pytest.raises(GarminConnectConnectionError):
            dl.download_one(garmin, 1001, tmp_path)

        assert not (tmp_path / "summary" / "1001.json").exists()


class TestMain:
    """The CLI loop: idempotency, limits and error handling."""

    def test_downloads_all_new_activities(
        self, garmin: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_main(monkeypatch, garmin, "--data-dir", str(tmp_path))

        summaries = sorted(p.name for p in (tmp_path / "test_user" / "summary").iterdir())
        assert summaries == ["1001.json", "1002.json"]

    def test_skips_activities_already_stored(
        self, garmin: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dl.write_atomic(tmp_path / "test_user" / "summary" / "1001.json", b"{}")

        run_main(monkeypatch, garmin, "--data-dir", str(tmp_path))

        garmin.get_activity.assert_called_once_with("1002")

    def test_max_new_limits_downloads(
        self, garmin: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_main(monkeypatch, garmin, "--data-dir", str(tmp_path), "--max-new", "1")

        assert garmin.get_activity.call_count == 1

    def test_connection_error_skips_activity_and_continues(
        self, garmin: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def flaky_splits(aid: str) -> dict[str, Any]:
            if aid == "1001":
                raise GarminConnectConnectionError("boom")
            return {}

        garmin.get_activity_splits.side_effect = flaky_splits

        run_main(monkeypatch, garmin, "--data-dir", str(tmp_path))

        summaries = [p.name for p in (tmp_path / "test_user" / "summary").iterdir()]
        assert summaries == ["1002.json"]

    def test_rate_limit_stops_cleanly(
        self, garmin: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        garmin.get_activities.side_effect = GarminConnectTooManyRequestsError("slow down")

        run_main(monkeypatch, garmin, "--data-dir", str(tmp_path))

        garmin.get_activity.assert_not_called()
