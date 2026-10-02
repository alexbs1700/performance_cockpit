"""Tests for performance_cockpit.load_records."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from garmin_fit_sdk import Encoder, Profile
from google.cloud import bigquery

from performance_cockpit import load_records

MESG_NUM = Profile["mesg_num"]
START = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
LOADED_AT = datetime(2026, 1, 2, 6, 0, tzinfo=UTC)
# Made-up positions: 2^29 semicircles is exactly 45 degrees, -2^28 is -22.5 degrees.
FAKE_LAT, FAKE_LONG = 2**29, -(2**28)


def encode_fit(records: list[dict[str, Any]]) -> bytes:
    """Build a minimal activity FIT file in memory holding ``records``."""
    encoder = Encoder()
    encoder.on_mesg(
        MESG_NUM["FILE_ID"],
        {"type": "activity", "manufacturer": "development", "product": 0, "time_created": START},
    )
    for record in records:
        encoder.on_mesg(MESG_NUM["RECORD"], record)
    return encoder.close()


@pytest.fixture
def fit_file() -> bytes:
    """Three records three seconds apart, the second one without GPS or cadence."""
    base = {"enhanced_speed": 3.25, "enhanced_altitude": 100.0, "heart_rate": 140}
    return encode_fit(
        [
            {**base, "timestamp": START, "distance": 0.0, "cadence": 85,
             "fractional_cadence": 0.5, "position_lat": FAKE_LAT, "position_long": FAKE_LONG},
            {**base, "timestamp": START + timedelta(seconds=3), "distance": 10.0},
            {**base, "timestamp": START + timedelta(seconds=6), "distance": 20.0,
             "power": 250, "temperature": 21},
        ]
    )  # fmt: skip


def test_semicircles_to_degrees() -> None:
    assert load_records.semicircles_to_degrees(FAKE_LAT) == 45.0
    assert load_records.semicircles_to_degrees(FAKE_LONG) == -22.5
    assert load_records.semicircles_to_degrees(None) is None


class TestParseFit:
    """Decoding a real (synthetic) FIT file into rows."""

    def test_columns_match_schema_file(
        self, fit_file: bytes, make_table: Callable[..., bigquery.Table]
    ) -> None:
        rows = load_records.parse_fit(fit_file, "test_user", 1001, LOADED_AT)

        assert list(rows[0]) == [field.name for field in make_table("records").schema]

    def test_one_row_per_record_with_elapsed_time(self, fit_file: bytes) -> None:
        rows = load_records.parse_fit(fit_file, "test_user", 1001, LOADED_AT)

        assert [r["elapsed_s"] for r in rows] == [0.0, 3.0, 6.0]
        assert rows[0]["recorded_at"] == "2026-01-01T08:00:00+00:00"
        assert [r["distance_m"] for r in rows] == [0.0, 10.0, 20.0]
        assert {(r["user_id"], r["activity_id"]) for r in rows} == {("test_user", 1001)}

    def test_converts_units(self, fit_file: bytes) -> None:
        first = load_records.parse_fit(fit_file, "test_user", 1001, LOADED_AT)[0]

        assert first["speed_mps"] == 3.25
        # FIT stores altitude in fifths of a meter, so allow for that rounding.
        assert first["altitude_m"] == pytest.approx(100.0, abs=0.2)
        assert first["heart_rate_bpm"] == 140
        assert first["cadence_rpm"] == 85.5
        assert (first["latitude_deg"], first["longitude_deg"]) == (45.0, -22.5)

    def test_missing_fields_become_null(self, fit_file: bytes) -> None:
        second = load_records.parse_fit(fit_file, "test_user", 1001, LOADED_AT)[1]

        assert second["latitude_deg"] is None
        assert second["cadence_rpm"] is None
        assert second["power_w"] is None

    def test_reads_power_and_temperature(self, fit_file: bytes) -> None:
        third = load_records.parse_fit(fit_file, "test_user", 1001, LOADED_AT)[2]

        assert (third["power_w"], third["temperature_c"]) == (250, 21)

    def test_falls_back_to_standard_speed_and_altitude(self) -> None:
        fit = encode_fit([{"timestamp": START, "speed": 2.5, "altitude": 50.0}])

        row = load_records.parse_fit(fit, "test_user", 1001, LOADED_AT)[0]

        assert row["speed_mps"] == 2.5
        assert row["altitude_m"] == pytest.approx(50.0, abs=0.2)

    def test_file_without_records_gives_no_rows(self) -> None:
        assert load_records.parse_fit(encode_fit([]), "test_user", 1001, LOADED_AT) == []

    def test_not_a_fit_file_raises(self) -> None:
        with pytest.raises(ValueError, match="not a FIT file"):
            load_records.parse_fit(b"not a fit file", "test_user", 1001, LOADED_AT)
