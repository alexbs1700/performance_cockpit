"""Load activity summaries from the raw zone into the BigQuery ``activities`` table.

Incremental and idempotent: only summaries whose activity is not yet in the table are read,
and rows are MERGEd on (user_id, activity_id), so rerunning never creates duplicates. With
``reload=True`` every summary is read again and existing rows are updated in place, which is
how new columns get filled for activities loaded before the columns existed.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from google.cloud import bigquery

from performance_cockpit.bigquery_loading import (
    KEY_COLUMNS,
    activity_id_from_key,
    load_rows,
    loaded_activity_ids,
    staging_table,
    table_path,
)
from performance_cockpit.storage import Storage

log = logging.getLogger(__name__)

# Typed column name -> field in Garmin's summaryDTO. Everything else stays in the JSON column.
SUMMARY_COLUMNS: dict[str, str] = {
    "distance_m": "distance",
    "duration_s": "duration",
    "moving_duration_s": "movingDuration",
    "elapsed_duration_s": "elapsedDuration",
    "avg_speed_mps": "averageSpeed",
    "max_speed_mps": "maxSpeed",
    "avg_hr_bpm": "averageHR",
    "max_hr_bpm": "maxHR",
    "min_hr_bpm": "minHR",
    "avg_run_cadence_spm": "averageRunCadence",
    "max_run_cadence_spm": "maxRunCadence",
    "stride_length_cm": "strideLength",
    "steps": "steps",
    "elevation_gain_m": "elevationGain",
    "elevation_loss_m": "elevationLoss",
    "calories_kcal": "calories",
    "aerobic_training_effect": "trainingEffect",
    "anaerobic_training_effect": "anaerobicTrainingEffect",
    "training_load": "activityTrainingLoad",
    "avg_power_w": "averagePower",
    "normalized_power_w": "normalizedPower",
    "avg_temperature_c": "averageTemperature",
    "rpe": "directWorkoutRpe",
    "feel": "directWorkoutFeel",
}

# Added after the first release; BigQuery only appends new columns, so they come last.
LOCATION_COLUMNS: dict[str, str] = {
    "start_latitude_deg": "startLatitude",
    "start_longitude_deg": "startLongitude",
    "end_latitude_deg": "endLatitude",
    "end_longitude_deg": "endLongitude",
}


def to_activity_row(activity: dict[str, Any], user_id: str, loaded_at: datetime) -> dict[str, Any]:
    """Turn one raw Garmin activity summary into an ``activities`` row."""
    summary = activity.get("summaryDTO") or {}
    metadata = activity.get("metadataDTO") or {}
    start_time_gmt = summary.get("startTimeGMT")
    return {
        "user_id": user_id,
        "activity_id": activity["activityId"],
        "activity_name": activity.get("activityName"),
        "activity_type": (activity.get("activityTypeDTO") or {}).get("typeKey"),
        "event_type": (activity.get("eventTypeDTO") or {}).get("typeKey"),
        # Garmin omits the zone on its GMT field; make UTC explicit rather than implied.
        "start_time_utc": f"{start_time_gmt}+00:00" if start_time_gmt else None,
        "start_time_local": summary.get("startTimeLocal"),
        "time_zone": (activity.get("timeZoneUnitDTO") or {}).get("timeZone"),
        **{column: summary.get(field) for column, field in SUMMARY_COLUMNS.items()},
        "lap_count": metadata.get("lapCount"),
        "is_manual": metadata.get("manualActivity"),
        "device_manufacturer": metadata.get("manufacturer"),
        "summary": summary,
        "loaded_at": loaded_at.isoformat(),
        **{column: summary.get(field) for column, field in LOCATION_COLUMNS.items()},
        "location_name": activity.get("locationName"),
    }


def merge_sql(target: str, staging: str, columns: list[str]) -> str:
    """Return a MERGE that inserts new activities and overwrites ones already present."""
    updates = ", ".join(f"{c} = source.{c}" for c in columns if c not in KEY_COLUMNS)
    return (
        f"MERGE `{target}` AS target\n"
        f"USING `{staging}` AS source\n"
        "ON target.user_id = source.user_id AND target.activity_id = source.activity_id\n"
        f"WHEN MATCHED THEN UPDATE SET {updates}\n"
        "WHEN NOT MATCHED THEN INSERT ROW"
    )


def merge_rows(client: bigquery.Client, table: bigquery.Table, rows: list[dict[str, Any]]) -> None:
    """Load ``rows`` into a temporary staging table, then MERGE it into ``table``."""
    with staging_table(client, table) as staging_id:
        load_rows(client, staging_id, table.schema, rows)
        columns = [field.name for field in table.schema]
        client.query(merge_sql(table_path(table), staging_id, columns)).result()


def load_activities(
    storage: Storage, client: bigquery.Client, table_id: str, user_id: str, reload: bool = False
) -> int:
    """Load summaries of ``user_id`` into ``table_id``; return how many rows were written.

    By default only activities missing from the table are read. ``reload`` rewrites them all.
    """
    table = client.get_table(table_id)
    target = table_path(table)
    known_ids = set() if reload else loaded_activity_ids(client, target, user_id)
    keys = [
        key
        for key in storage.list_keys(f"{user_id}/summary/")
        if key.endswith(".json") and activity_id_from_key(key) not in known_ids
    ]
    if not keys:
        log.info("%s: nothing to load, %d activities already loaded", target, len(known_ids))
        return 0

    loaded_at = datetime.now(UTC)
    rows = [to_activity_row(json.loads(storage.read(k)), user_id, loaded_at) for k in keys]
    merge_rows(client, table, rows)
    log.info("%s: wrote %d activities", target, len(rows))
    return len(rows)
