"""Tests for performance_cockpit.load_laps."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from google.cloud import bigquery

from performance_cockpit import load_laps

FIXTURES_DIR = Path(__file__).parents[1] / "fixtures"
LOADED_AT = datetime(2026, 1, 2, 6, 0, tzinfo=UTC)


@pytest.fixture
def splits() -> dict[str, Any]:
    """A synthetic splits document: a warm-up lap and an active lap."""
    return json.loads((FIXTURES_DIR / "splits.json").read_text())


def test_columns_match_schema_file(
    splits: dict[str, Any], make_table: Callable[..., bigquery.Table]
) -> None:
    rows = load_laps.laps_from_splits(splits, "test_user", 1001, LOADED_AT)

    assert list(rows[0]) == [field.name for field in make_table("garmin_laps").schema]


def test_one_row_per_lap(splits: dict[str, Any]) -> None:
    rows = load_laps.laps_from_splits(splits, "test_user", 1001, LOADED_AT)

    assert [(r["lap_index"], r["intensity_type"]) for r in rows] == [(1, "WARMUP"), (2, "ACTIVE")]
    assert {(r["user_id"], r["activity_id"]) for r in rows} == {("test_user", 1001)}


def test_maps_fields(splits: dict[str, Any]) -> None:
    lap = load_laps.laps_from_splits(splits, "test_user", 1001, LOADED_AT)[1]

    assert lap["start_time_utc"] == "2026-01-01T08:06:00.0+00:00"
    assert lap["distance_m"] == 1000.0
    assert lap["avg_speed_mps"] == 4.545
    assert lap["avg_hr_bpm"] == 172.0
    assert lap["workout_step_index"] == 1
    assert lap["moving_duration_s"] is None
    assert lap["loaded_at"] == "2026-01-02T06:00:00+00:00"


def test_full_lap_kept_as_json(splits: dict[str, Any]) -> None:
    lap = load_laps.laps_from_splits(splits, "test_user", 1001, LOADED_AT)[1]

    assert lap["lap"]["averageSWOLF"] == 40.0


def test_maps_lap_location(splits: dict[str, Any]) -> None:
    # Made-up coordinates, added here so the fixture file itself never holds any.
    splits["lapDTOs"][0].update(
        {"startLatitude": 1.0, "startLongitude": 2.0, "endLatitude": 3.0, "endLongitude": 4.0}
    )

    lap = load_laps.laps_from_splits(splits, "test_user", 1001, LOADED_AT)[0]

    assert (lap["start_latitude_deg"], lap["start_longitude_deg"]) == (1.0, 2.0)
    assert (lap["end_latitude_deg"], lap["end_longitude_deg"]) == (3.0, 4.0)


def test_no_laps_gives_no_rows() -> None:
    assert load_laps.laps_from_splits({"lapDTOs": []}, "test_user", 1001, LOADED_AT) == []


def test_parse_splits_reads_bytes(splits: dict[str, Any]) -> None:
    rows = load_laps.parse_splits(json.dumps(splits).encode(), "test_user", 1001, LOADED_AT)

    assert len(rows) == 2
