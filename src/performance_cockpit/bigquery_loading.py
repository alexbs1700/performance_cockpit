"""Shared BigQuery loading helpers: staging tables and incremental per-activity loads.

Every loader follows the same pattern: find what is missing in BigQuery, build rows from the
raw zone, load them into a short-lived staging table, then copy them into the target table
with a single SQL statement. Staging plus SQL makes each batch all or nothing, and the SQL is
written so that rerunning or overlapping runs never create duplicate rows.
"""

import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from itertools import batched
from pathlib import PurePosixPath
from typing import Any

from google.cloud import bigquery

from performance_cockpit.storage import Storage

log = logging.getLogger(__name__)

KEY_COLUMNS = ("user_id", "activity_id")
# A safety net: if a run dies before deleting its staging table, BigQuery drops it anyway.
STAGING_TABLE_LIFETIME = timedelta(hours=1)
DEFAULT_BATCH_SIZE = 100

Row = dict[str, Any]
# Turns the raw bytes of one activity file into rows: (data, user_id, activity_id, loaded_at).
ParseFile = Callable[[bytes, str, int, datetime], list[Row]]


def table_path(table: bigquery.Table) -> str:
    """Return ``project.dataset.table``, the form SQL expects."""
    return f"{table.project}.{table.dataset_id}.{table.table_id}"


def activity_id_from_key(key: str) -> int:
    """Return the activity id encoded in a raw-zone key such as ``me/fit/123.fit``."""
    return int(PurePosixPath(key).stem)


@contextmanager
def staging_table(client: bigquery.Client, table: bigquery.Table) -> Iterator[str]:
    """Create an empty, expiring copy of ``table``'s schema; yield its id; always delete it."""
    # A unique name per run, so two overlapping runs never share a staging table.
    staging_id = f"{table_path(table)}_staging_{uuid.uuid4().hex[:8]}"
    staging = bigquery.Table(staging_id, schema=table.schema)
    staging.expires = datetime.now(UTC) + STAGING_TABLE_LIFETIME
    client.create_table(staging)
    try:
        yield staging_id
    finally:
        client.delete_table(staging_id, not_found_ok=True)


def load_rows(
    client: bigquery.Client, table_id: str, schema: list[bigquery.SchemaField], rows: list[Row]
) -> None:
    """Append ``rows`` to ``table_id`` with a load job and wait for it to finish."""
    job_config = bigquery.LoadJobConfig(schema=schema)
    client.load_table_from_json(rows, table_id, job_config=job_config).result()


def loaded_activity_ids(client: bigquery.Client, target: str, user_id: str) -> set[int]:
    """Return the ids of this user's activities that already have rows in ``target``."""
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("user_id", "STRING", user_id)]
    )
    query = f"SELECT DISTINCT activity_id FROM `{target}` WHERE user_id = @user_id"
    return {row.activity_id for row in client.query(query, job_config=job_config).result()}


def insert_missing_sql(target: str, staging: str) -> str:
    """Return SQL copying staged rows of activities that ``target`` does not hold yet."""
    return (
        f"INSERT INTO `{target}`\n"
        f"SELECT * FROM `{staging}` AS source\n"
        "WHERE NOT EXISTS (\n"
        f"  SELECT 1 FROM `{target}` AS existing\n"
        "  WHERE existing.user_id = source.user_id AND existing.activity_id = source.activity_id\n"
        ")"
    )


def load_per_activity(
    storage: Storage,
    client: bigquery.Client,
    table_id: str,
    user_id: str,
    kind: str,
    suffix: str,
    parse: ParseFile,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Load rows for every activity whose ``{user_id}/{kind}/*{suffix}`` file is not loaded.

    Files are processed in batches to keep memory flat during a backfill. A file that fails to
    parse is logged and skipped, so one bad file never blocks the rest; it is retried next run.
    Returns the number of activities that produced rows.
    """
    table = client.get_table(table_id)
    target = table_path(table)
    known_ids = loaded_activity_ids(client, target, user_id)
    pending = [
        key
        for key in storage.list_keys(f"{user_id}/{kind}/")
        if key.endswith(suffix) and activity_id_from_key(key) not in known_ids
    ]
    if not pending:
        log.info("%s: nothing to load, %d activities already loaded", target, len(known_ids))
        return 0

    loaded_at = datetime.now(UTC)
    loaded_activities = 0
    for batch in batched(pending, batch_size):
        rows: list[Row] = []
        for key in batch:
            activity_id = activity_id_from_key(key)
            try:
                activity_rows = parse(storage.read(key), user_id, activity_id, loaded_at)
            except Exception:
                log.exception("%s: could not parse %s, skipping it", target, key)
                continue
            rows.extend(activity_rows)
            loaded_activities += bool(activity_rows)
        if rows:
            with staging_table(client, table) as staging_id:
                load_rows(client, staging_id, table.schema, rows)
                client.query(insert_missing_sql(target, staging_id)).result()
        log.info("%s: loaded batch of %d files (%d rows)", target, len(batch), len(rows))

    log.info("%s: loaded %d new activities", target, loaded_activities)
    return loaded_activities
