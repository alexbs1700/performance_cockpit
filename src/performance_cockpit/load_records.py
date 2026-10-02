"""Turn Garmin FIT files into rows of the BigQuery ``records`` table.

A FIT file holds one ``record`` message per data point the watch saved. With Garmin's Smart
Recording that is every few seconds rather than every second, so analysis should rely on
``recorded_at``, ``elapsed_s`` and ``distance_m`` rather than on row counts.
"""

import logging
from datetime import datetime
from typing import Any

from garmin_fit_sdk import Decoder, Stream

log = logging.getLogger(__name__)

# FIT stores positions as signed 32-bit "semicircles": 2^31 semicircles make 180 degrees.
DEGREES_PER_SEMICIRCLE = 180 / 2**31


def semicircles_to_degrees(value: int | None) -> float | None:
    """Convert a FIT position to degrees, keeping missing positions as None."""
    return value * DEGREES_PER_SEMICIRCLE if value is not None else None


def decode_fit(data: bytes) -> dict[str, list[dict[str, Any]]]:
    """Decode a FIT file into its messages, grouped by type (e.g. ``record_mesgs``).

    Raises ValueError if ``data`` is not a FIT file at all, so a corrupt download is reported
    instead of silently loading as an activity without records.
    """
    decoder = Decoder(Stream.from_byte_array(bytearray(data)))
    if not decoder.is_fit():
        raise ValueError("not a FIT file")
    messages, errors = decoder.read()
    if errors:
        # The decoder returns whatever it read before the problem, often almost everything.
        log.warning("FIT decoder reported %d problem(s); keeping what was decoded", len(errors))
    return messages


def records_from_messages(
    messages: dict[str, list[dict[str, Any]]], user_id: str, activity_id: int, loaded_at: datetime
) -> list[dict[str, Any]]:
    """Return one ``records`` row per FIT record message that has a timestamp."""
    records = [r for r in messages.get("record_mesgs", []) if r.get("timestamp") is not None]
    if not records:
        return []

    first_timestamp: datetime = records[0]["timestamp"]
    rows = []
    for record in records:
        cadence = record.get("cadence")
        rows.append(
            {
                "user_id": user_id,
                "activity_id": activity_id,
                "recorded_at": record["timestamp"].isoformat(),
                "elapsed_s": (record["timestamp"] - first_timestamp).total_seconds(),
                "distance_m": record.get("distance"),
                # Newer devices write the higher-resolution "enhanced" fields, older ones not.
                "speed_mps": _first_present(record, "enhanced_speed", "speed"),
                "altitude_m": _first_present(record, "enhanced_altitude", "altitude"),
                "heart_rate_bpm": record.get("heart_rate"),
                "cadence_rpm": (
                    cadence + (record.get("fractional_cadence") or 0)
                    if cadence is not None
                    else None
                ),
                "power_w": record.get("power"),
                "temperature_c": record.get("temperature"),
                "latitude_deg": semicircles_to_degrees(record.get("position_lat")),
                "longitude_deg": semicircles_to_degrees(record.get("position_long")),
                "loaded_at": loaded_at.isoformat(),
            }
        )
    return rows


def parse_fit(
    data: bytes, user_id: str, activity_id: int, loaded_at: datetime
) -> list[dict[str, Any]]:
    """Parse the raw bytes of a FIT file into ``records`` rows."""
    return records_from_messages(decode_fit(data), user_id, activity_id, loaded_at)


def _first_present(record: dict[str, Any], *fields: str) -> Any:
    """Return the value of the first field in ``fields`` that is set in ``record``."""
    return next((record[f] for f in fields if record.get(f) is not None), None)
