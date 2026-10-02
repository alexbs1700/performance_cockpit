"""Tests for performance_cockpit.load_activities."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from google.cloud import bigquery

from performance_cockpit import load_activities as la
from performance_cockpit.storage import LocalStorage

FIXTURES_DIR = Path(__file__).parents[1] / "fixtures"
TABLE_ID = "test-project.cockpit.activities"
LOADED_AT = datetime(2026, 1, 2, 6, 0, tzinfo=UTC)


@pytest.fixture
def activity_detail() -> dict[str, Any]:
    """A synthetic Garmin activity summary in the shape get_activity() returns."""
    return json.loads((FIXTURES_DIR / "activity_detail.json").read_text())


@pytest.fixture
def table(make_table: Callable[..., bigquery.Table]) -> bigquery.Table:
    """The activities table, as BigQuery would return it."""
    return make_table("activities")


@pytest.fixture
def bq(table: bigquery.Table, make_bigquery_client: Callable[..., MagicMock]) -> MagicMock:
    """A fake BigQuery client whose table already holds activity 1001."""
    return make_bigquery_client(table, [1001])


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
        assert row["start_latitude_deg"] is None
        assert row["location_name"] is None

    def test_rare_fields_stay_in_summary_json(self, activity_detail: dict[str, Any]) -> None:
        row = la.to_activity_row(activity_detail, "test_user", LOADED_AT)

        assert row["summary"]["averageSWOLF"] == 40.0

    def test_maps_location(self, activity_detail: dict[str, Any]) -> None:
        # Made-up coordinates, added here so the fixture file itself never holds any.
        activity_detail["summaryDTO"].update(
            {"startLatitude": 1.0, "startLongitude": 2.0, "endLatitude": 3.0, "endLongitude": 4.0}
        )
        activity_detail["locationName"] = "Testville"

        row = la.to_activity_row(activity_detail, "test_user", LOADED_AT)

        assert (row["start_latitude_deg"], row["start_longitude_deg"]) == (1.0, 2.0)
        assert (row["end_latitude_deg"], row["end_longitude_deg"]) == (3.0, 4.0)
        assert row["location_name"] == "Testville"


def test_merge_sql_updates_all_but_key_columns() -> None:
    sql = la.merge_sql("p.d.t", "p.d.t_staging", ["user_id", "activity_id", "distance_m"])

    assert "MERGE `p.d.t` AS target" in sql
    assert "USING `p.d.t_staging` AS source" in sql
    assert "ON target.user_id = source.user_id AND target.activity_id = source.activity_id" in sql
    assert "UPDATE SET distance_m = source.distance_m\n" in sql
    assert sql.endswith("WHEN NOT MATCHED THEN INSERT ROW")


class TestLoadActivities:
    """The incremental load and the full reload."""

    def test_loads_only_activities_not_in_table(self, bq: MagicMock, storage: LocalStorage) -> None:
        assert la.load_activities(storage, bq, TABLE_ID, "test_user") == 1

        rows = bq.load_table_from_json.call_args.args[0]
        assert [r["activity_id"] for r in rows] == [1002]
        assert bq.query.call_args_list[-1].args[0].startswith(f"MERGE `{TABLE_ID}`")

    def test_reload_rewrites_every_activity(self, bq: MagicMock, storage: LocalStorage) -> None:
        assert la.load_activities(storage, bq, TABLE_ID, "test_user", reload=True) == 2

        rows = bq.load_table_from_json.call_args.args[0]
        assert [r["activity_id"] for r in rows] == [1001, 1002]
        # A reload never asks which ids exist: the MERGE updates them all.
        assert not any(c.args[0].startswith("SELECT") for c in bq.query.call_args_list)

    def test_nothing_new_touches_nothing(self, bq: MagicMock, tmp_path: Path) -> None:
        assert la.load_activities(LocalStorage(tmp_path), bq, TABLE_ID, "test_user") == 0

        bq.create_table.assert_not_called()
        bq.load_table_from_json.assert_not_called()

    def test_staging_table_is_cleaned_up(self, bq: MagicMock, storage: LocalStorage) -> None:
        la.load_activities(storage, bq, TABLE_ID, "test_user")

        staging_id = bq.load_table_from_json.call_args.args[1]
        assert staging_id.startswith(f"{TABLE_ID}_staging_")
        bq.delete_table.assert_called_once_with(staging_id, not_found_ok=True)
