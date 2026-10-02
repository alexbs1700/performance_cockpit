"""Load activity summaries from the raw zone into the BigQuery ``activities`` table.

Incremental and idempotent: only summaries whose activity is not yet in the table are read,
and rows are MERGEd on (user_id, activity_id), so rerunning never creates duplicates. GPS
coordinates are dropped: the warehouse never stores where activities happened.

Run with:
    uv run load-activities --table PROJECT.cockpit.activities --data-dir data/raw   # backfill
    uv run load-activities --table PROJECT.cockpit.activities --bucket NAME         # cloud job
"""

import argparse
import json
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from google.cloud import bigquery

from performance_cockpit.storage import GcsStorage, LocalStorage, Storage

log = logging.getLogger(__name__)

KEY_COLUMNS = ("user_id", "activity_id")
GPS_FIELDS = frozenset({"startLatitude", "startLongitude", "endLatitude", "endLongitude"})
# A safety net: if the job dies before deleting its staging table, BigQuery drops it anyway.
STAGING_TABLE_LIFETIME = timedelta(hours=1)

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


def to_activity_row(activity: dict[str, Any], user_id: str, loaded_at: datetime) -> dict[str, Any]:
    """Turn one raw Garmin activity summary into an ``activities`` row, without GPS fields."""
    summary = {k: v for k, v in (activity.get("summaryDTO") or {}).items() if k not in GPS_FIELDS}
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
    }


def activity_id_from_key(key: str) -> int:
    """Return the activity id encoded in a summary key such as ``me/summary/123.json``."""
    return int(PurePosixPath(key).stem)


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


def existing_activity_ids(client: bigquery.Client, target: str, user_id: str) -> set[int]:
    """Return the ids of this user's activities already in the table."""
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("user_id", "STRING", user_id)]
    )
    query = f"SELECT activity_id FROM `{target}` WHERE user_id = @user_id"
    return {row.activity_id for row in client.query(query, job_config=job_config).result()}


def merge_rows(client: bigquery.Client, table: bigquery.Table, rows: list[dict[str, Any]]) -> None:
    """Load ``rows`` into a temporary staging table, then MERGE it into ``table``."""
    target = f"{table.project}.{table.dataset_id}.{table.table_id}"
    # A unique name per run, so two overlapping runs never share a staging table.
    staging_id = f"{target}_staging_{uuid.uuid4().hex[:8]}"
    staging = bigquery.Table(staging_id, schema=table.schema)
    staging.expires = datetime.now(UTC) + STAGING_TABLE_LIFETIME
    client.create_table(staging)
    try:
        load_config = bigquery.LoadJobConfig(schema=table.schema)
        client.load_table_from_json(rows, staging_id, job_config=load_config).result()
        columns = [field.name for field in table.schema]
        client.query(merge_sql(target, staging_id, columns)).result()
    finally:
        client.delete_table(staging_id, not_found_ok=True)


def load_activities(storage: Storage, client: bigquery.Client, table_id: str, user_id: str) -> int:
    """Load every summary of ``user_id`` not yet in ``table_id``; return how many were loaded."""
    table = client.get_table(table_id)
    target = f"{table.project}.{table.dataset_id}.{table.table_id}"
    known_ids = existing_activity_ids(client, target, user_id)
    new_keys = [
        key
        for key in storage.list_keys(f"{user_id}/summary/")
        if key.endswith(".json") and activity_id_from_key(key) not in known_ids
    ]
    if not new_keys:
        log.info("Nothing to load: all %d activities already in %s", len(known_ids), target)
        return 0

    loaded_at = datetime.now(UTC)
    rows = [to_activity_row(json.loads(storage.read(k)), user_id, loaded_at) for k in new_keys]
    merge_rows(client, table, rows)
    log.info("Loaded %d new activities into %s", len(rows), target)
    return len(rows)


def main() -> None:
    """Parse arguments and load new activity summaries into BigQuery."""
    parser = argparse.ArgumentParser(description="Load Garmin activities into BigQuery.")
    parser.add_argument(
        "--table", required=True, help="fully qualified table id: PROJECT.DATASET.TABLE"
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--data-dir", type=Path, default=Path("data/raw"), help="local raw zone (default)"
    )
    source.add_argument("--bucket", help="read from this Cloud Storage bucket instead")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    user_id = os.environ.get("COCKPIT_USER_ID", "me")
    storage: Storage = GcsStorage(args.bucket) if args.bucket else LocalStorage(args.data_dir)
    # The project comes from the table id, so it never depends on local gcloud defaults.
    client = bigquery.Client(project=args.table.split(".")[0])
    load_activities(storage, client, args.table, user_id)


if __name__ == "__main__":
    main()
