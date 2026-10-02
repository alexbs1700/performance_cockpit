"""Tests for performance_cockpit.load_activities."""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from google.cloud import bigquery

from performance_cockpit import load_activities as la
from performance_cockpit.storage import LocalStorage

SCHEMA_FILE = Path(__file__).parents[2] / "infra" / "bigquery" / "activities.json"
FIXTURES_DIR = Path(__file__).parents[1] / "fixtures"
TABLE_ID = "test-project.cockpit.activities"
LOADED_AT = datetime(2026, 1, 2, 6, 0, tzinfo=UTC)


@pytest.fixture
def activity_detail() -> dict[str, Any]:
    """A synthetic Garmin activity summary in the shape get_activity() returns."""
    return json.loads((FIXTURES_DIR / "activity_detail.json").read_text())


@pytest.fixture
def table() -> bigquery.Table:
    """The activities table as BigQuery would return it, built from the schema file."""
    schema = [bigquery.SchemaField.from_api_repr(f) for f in json.loads(SCHEMA_FILE.read_text())]
    return bigquery.Table(TABLE_ID, schema=schema)


@pytest.fixture
def bq(table: bigquery.Table) -> MagicMock:
    """A fake BigQuery client whose table already holds activity 1001."""
    client = MagicMock(name="bigquery_client")
    client.get_table.return_value = table

    def query(sql: str, **_kwargs: Any) -> MagicMock:
        job = MagicMock()
        rows = [SimpleNamespace(activity_id=1001)] if sql.startswith("SELECT") else []
        job.result.return_value = rows
        return job

    client.query.side_effect = query
    return client


@pytest.fixture
def storage(tmp_path: Path, activity_detail: dict[str, Any]) -> LocalStorage:
    """A raw zone holding summaries 1001 and 1002 for test_user."""
    storage = LocalStorage(tmp_path)
    for activity_id in (1001, 1002):
        summary = {**activity_detail, "activityId": activity_id}
        storage.write(f"test_user/summary/{activity_id}.json", json.dumps(summary).encode())
    storage.write("test_user/splits/1002.json", b"{}")
    return storage


class TestToActivityRow:
    """Mapping a raw summary to a table row."""

    def test_columns_match_schema_file(
        self, activity_detail: dict[str, Any], table: bigquery.Table
    ) -> None:
        row = la.to_activity_row(activity_detail, "test_user", LOADED_AT)

        assert list(row) == [field.name for field in table.schema]

    def test_maps_fields(self, activity_detail: dict[str, Any]) -> None:
        row = la.to_activity_row(activity_detail, "test_user", LOADED_AT)

        assert row["user_id"] == "test_user"
        assert row["activity_id"] == 1001
        assert row["activity_type"] == "running"
        assert row["start_time_utc"] == "2026-01-01T08:00:00.0+00:00"
        assert row["start_time_local"] == "2026-01-01T08:00:00.0"
        assert row["distance_m"] == 10000.0
        assert row["avg_hr_bpm"] == 150.0
        assert row["rpe"] == 50
        assert row["lap_count"] == 3
        assert row["is_manual"] is False
        assert row["loaded_at"] == "2026-01-02T06:00:00+00:00"

    def test_missing_fields_become_null(self, activity_detail: dict[str, Any]) -> None:
        row = la.to_activity_row(activity_detail, "test_user", LOADED_AT)

        assert row["avg_power_w"] is None
        assert row["elevation_gain_m"] is None

    def test_rare_fields_stay_in_summary_json(self, activity_detail: dict[str, Any]) -> None:
        row = la.to_activity_row(activity_detail, "test_user", LOADED_AT)

        assert row["summary"]["averageSWOLF"] == 40.0

    def test_drops_gps_coordinates(self, activity_detail: dict[str, Any]) -> None:
        # Made-up coordinates, added here so the fixture file itself never holds any.
        gps = {"startLatitude": 1.0, "startLongitude": 2.0, "endLatitude": 3.0, "endLongitude": 4.0}
        activity_detail["summaryDTO"].update(gps)

        row = la.to_activity_row(activity_detail, "test_user", LOADED_AT)

        assert not set(gps) & set(row["summary"])
        assert not set(gps) & set(row)

    def test_does_not_mutate_input(self, activity_detail: dict[str, Any]) -> None:
        activity_detail["summaryDTO"]["startLatitude"] = 1.0

        la.to_activity_row(activity_detail, "test_user", LOADED_AT)

        assert "startLatitude" in activity_detail["summaryDTO"]


def test_activity_id_from_key() -> None:
    assert la.activity_id_from_key("test_user/summary/1001.json") == 1001


def test_merge_sql_updates_all_but_key_columns() -> None:
    sql = la.merge_sql("p.d.t", "p.d.t_staging", ["user_id", "activity_id", "distance_m"])

    assert "MERGE `p.d.t` AS target" in sql
    assert "USING `p.d.t_staging` AS source" in sql
    assert "ON target.user_id = source.user_id AND target.activity_id = source.activity_id" in sql
    assert "UPDATE SET distance_m = source.distance_m\n" in sql
    assert sql.endswith("WHEN NOT MATCHED THEN INSERT ROW")


class TestLoadActivities:
    """The incremental load."""

    def test_loads_only_activities_not_in_table(
        self, bq: MagicMock, storage: LocalStorage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        merge_rows = MagicMock()
        monkeypatch.setattr(la, "merge_rows", merge_rows)

        loaded = la.load_activities(storage, bq, TABLE_ID, "test_user")

        assert loaded == 1
        _client, _table, rows = merge_rows.call_args.args
        assert [r["activity_id"] for r in rows] == [1002]

    def test_queries_existing_ids_for_this_user_only(
        self, bq: MagicMock, storage: LocalStorage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(la, "merge_rows", MagicMock())

        la.load_activities(storage, bq, TABLE_ID, "test_user")

        sql = bq.query.call_args_list[0].args[0]
        params = bq.query.call_args_list[0].kwargs["job_config"].query_parameters
        assert sql == f"SELECT activity_id FROM `{TABLE_ID}` WHERE user_id = @user_id"
        assert params[0].value == "test_user"

    def test_nothing_new_touches_nothing(self, bq: MagicMock, tmp_path: Path) -> None:
        empty = LocalStorage(tmp_path)

        assert la.load_activities(empty, bq, TABLE_ID, "test_user") == 0

        bq.create_table.assert_not_called()
        bq.load_table_from_json.assert_not_called()


class TestMergeRows:
    """Staging table, load, MERGE and cleanup."""

    def test_stages_merges_and_cleans_up(self, bq: MagicMock, table: bigquery.Table) -> None:
        rows = [{"user_id": "test_user", "activity_id": 1002}]

        la.merge_rows(bq, table, rows)

        staging = bq.create_table.call_args.args[0]
        staging_id = f"{staging.project}.{staging.dataset_id}.{staging.table_id}"
        assert staging_id.startswith(f"{TABLE_ID}_staging_")
        assert staging.schema == table.schema
        assert staging.expires is not None
        assert bq.load_table_from_json.call_args.args[:2] == (rows, staging_id)
        merge_query = bq.query.call_args.args[0]
        assert merge_query.startswith(f"MERGE `{TABLE_ID}`")
        assert f"USING `{staging_id}`" in merge_query
        bq.delete_table.assert_called_once_with(staging_id, not_found_ok=True)

    def test_staging_table_deleted_even_when_merge_fails(
        self, bq: MagicMock, table: bigquery.Table
    ) -> None:
        bq.query.side_effect = RuntimeError("merge failed")

        with pytest.raises(RuntimeError):
            la.merge_rows(bq, table, [{"user_id": "test_user", "activity_id": 1002}])

        bq.delete_table.assert_called_once()


class TestMain:
    """Command line wiring."""

    def run_main(self, monkeypatch: pytest.MonkeyPatch, *args: str) -> MagicMock:
        """Run main() with a fake BigQuery client class; return the load_activities mock."""
        monkeypatch.setattr(la.bigquery, "Client", MagicMock(name="Client"))
        load = MagicMock(name="load_activities")
        monkeypatch.setattr(la, "load_activities", load)
        monkeypatch.setenv("COCKPIT_USER_ID", "test_user")
        monkeypatch.setattr(sys, "argv", ["load-activities", *args])
        la.main()
        return load

    def test_project_comes_from_table_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        load = self.run_main(monkeypatch, "--table", TABLE_ID, "--data-dir", str(tmp_path))

        la.bigquery.Client.assert_called_once_with(project="test-project")
        storage, _client, table_id, user_id = load.call_args.args
        assert isinstance(storage, LocalStorage)
        assert (table_id, user_id) == (TABLE_ID, "test_user")

    def test_bucket_reads_from_cloud_storage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gcs_cls = MagicMock(name="GcsStorage")
        monkeypatch.setattr(la, "GcsStorage", gcs_cls)

        load = self.run_main(monkeypatch, "--table", TABLE_ID, "--bucket", "test-bucket")

        gcs_cls.assert_called_once_with("test-bucket")
        assert load.call_args.args[0] is gcs_cls.return_value

    def test_table_is_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(SystemExit):
            self.run_main(monkeypatch, "--bucket", "test-bucket")
