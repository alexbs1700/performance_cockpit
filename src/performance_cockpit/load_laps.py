"""Turn Garmin splits JSON into rows of the BigQuery ``garmin_laps`` table.

One row per lap as Garmin recorded it: manual lap presses, auto laps, or the steps of a
structured workout (``intensity_type`` says warm-up, active, recovery and so on). The full lap
is kept in a JSON column, so fields without a typed column, such as swim lengths, stay
queryable.
"""

import json
from datetime import datetime
from typing import Any

# Typed column name -> field in a Garmin lapDTO.
LAP_COLUMNS: dict[str, str] = {
    "distance_m": "distance",
    "duration_s": "duration",
    "moving_duration_s": "movingDuration",
    "elapsed_duration_s": "elapsedDuration",
    "avg_speed_mps": "averageSpeed",
    "avg_moving_speed_mps": "averageMovingSpeed",
    "max_speed_mps": "maxSpeed",
    "avg_hr_bpm": "averageHR",
    "max_hr_bpm": "maxHR",
    "avg_run_cadence_spm": "averageRunCadence",
    "max_run_cadence_spm": "maxRunCadence",
    "stride_length_cm": "strideLength",
    "elevation_gain_m": "elevationGain",
    "elevation_loss_m": "elevationLoss",
    "max_elevation_m": "maxElevation",
    "min_elevation_m": "minElevation",
    "avg_power_w": "averagePower",
    "normalized_power_w": "normalizedPower",
    "avg_temperature_c": "averageTemperature",
    "calories_kcal": "calories",
    "intensity_type": "intensityType",
    "workout_step_index": "wktStepIndex",
    "start_latitude_deg": "startLatitude",
    "start_longitude_deg": "startLongitude",
    "end_latitude_deg": "endLatitude",
    "end_longitude_deg": "endLongitude",
}


def laps_from_splits(
    splits: dict[str, Any], user_id: str, activity_id: int, loaded_at: datetime
) -> list[dict[str, Any]]:
    """Return one ``garmin_laps`` row per lap in a Garmin splits document."""
    rows = []
    for lap in splits.get("lapDTOs") or []:
        start_time_gmt = lap.get("startTimeGMT")
        rows.append(
            {
                "user_id": user_id,
                "activity_id": activity_id,
                "lap_index": lap.get("lapIndex"),
                # Garmin omits the zone on its GMT field; make UTC explicit rather than implied.
                "start_time_utc": f"{start_time_gmt}+00:00" if start_time_gmt else None,
                **{column: lap.get(field) for column, field in LAP_COLUMNS.items()},
                "lap": lap,
                "loaded_at": loaded_at.isoformat(),
            }
        )
    return rows


def parse_splits(
    data: bytes, user_id: str, activity_id: int, loaded_at: datetime
) -> list[dict[str, Any]]:
    """Parse the raw bytes of a splits file into ``garmin_laps`` rows."""
    return laps_from_splits(json.loads(data), user_id, activity_id, loaded_at)
