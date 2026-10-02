"""Load the raw zone into the BigQuery warehouse: activities, garmin_laps and records.

Each table is loaded incrementally and independently. If one fails, the others still run and
the command exits with an error at the end, so the Cloud Run Job is marked failed and alerts.

Run with:
    uv run load-warehouse --dataset PROJECT.cockpit --data-dir data/raw   # backfill from disk
    uv run load-warehouse --dataset PROJECT.cockpit --bucket NAME         # the cloud job
"""

import argparse
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

from google.cloud import bigquery

from performance_cockpit.bigquery_loading import load_per_activity
from performance_cockpit.load_activities import load_activities
from performance_cockpit.load_laps import parse_splits
from performance_cockpit.load_records import parse_fit
from performance_cockpit.storage import GcsStorage, LocalStorage, Storage

log = logging.getLogger(__name__)

TABLES = ("activities", "garmin_laps", "records")
# Records are the heavy table (hundreds of rows per activity); laps are a few rows each.
LAPS_BATCH_SIZE = 500
RECORDS_BATCH_SIZE = 200


def table_loaders(
    storage: Storage, client: bigquery.Client, dataset: str, user_id: str, reload_activities: bool
) -> dict[str, Callable[[], int]]:
    """Return a zero-argument loader per table, in the order they should run."""
    return {
        "activities": lambda: load_activities(
            storage, client, f"{dataset}.activities", user_id, reload=reload_activities
        ),
        "garmin_laps": lambda: load_per_activity(
            storage,
            client,
            f"{dataset}.garmin_laps",
            user_id,
            "splits",
            ".json",
            parse_splits,
            batch_size=LAPS_BATCH_SIZE,
        ),
        "records": lambda: load_per_activity(
            storage,
            client,
            f"{dataset}.records",
            user_id,
            "fit",
            ".fit",
            parse_fit,
            batch_size=RECORDS_BATCH_SIZE,
        ),
    }


def run_loaders(loaders: dict[str, Callable[[], int]], only: list[str] | None = None) -> bool:
    """Run each selected loader, logging failures without stopping; return True if all passed."""
    ok = True
    for name, load in loaders.items():
        if only and name not in only:
            continue
        try:
            load()
        except Exception:
            log.exception("Loading %s failed", name)
            ok = False
    return ok


def main() -> None:
    """Parse arguments and load every warehouse table from the raw zone."""
    parser = argparse.ArgumentParser(description="Load the raw zone into BigQuery.")
    parser.add_argument("--dataset", required=True, help="fully qualified dataset: PROJECT.DATASET")
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--data-dir", type=Path, default=Path("data/raw"), help="local raw zone (default)"
    )
    source.add_argument("--bucket", help="read from this Cloud Storage bucket instead")
    parser.add_argument("--only", nargs="+", choices=TABLES, help="load just these tables")
    parser.add_argument(
        "--reload-activities",
        action="store_true",
        help="rewrite every activities row, e.g. to fill newly added columns",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    user_id = os.environ.get("COCKPIT_USER_ID", "me")
    storage: Storage = GcsStorage(args.bucket) if args.bucket else LocalStorage(args.data_dir)
    # The project comes from the dataset id, so it never depends on local gcloud defaults.
    client = bigquery.Client(project=args.dataset.split(".")[0])
    loaders = table_loaders(storage, client, args.dataset, user_id, args.reload_activities)
    if not run_loaders(loaders, args.only):
        sys.exit(1)


if __name__ == "__main__":
    main()
