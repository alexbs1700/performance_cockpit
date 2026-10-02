"""Shared pytest fixtures."""

import io
import json
import zipfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from google.cloud import bigquery

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def activity_summary() -> dict[str, Any]:
    """A small, synthetic activity summary with no personal data."""
    return json.loads((FIXTURES_DIR / "activity_summary.json").read_text())


def build_zip(files: dict[str, bytes]) -> bytes:
    """Build an in-memory zip archive, mimicking Garmin's ORIGINAL download."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buffer.getvalue()


@pytest.fixture
def make_zip() -> Callable[[dict[str, bytes]], bytes]:
    """Factory fixture that builds zip archives from a {name: content} mapping."""
    return build_zip


SCHEMA_DIR = Path(__file__).parents[1] / "infra" / "bigquery"


def schema_table(name: str, table_id: str | None = None) -> bigquery.Table:
    """Build a Table from ``infra/bigquery/{name}.json``, as BigQuery would return it."""
    fields = json.loads((SCHEMA_DIR / f"{name}.json").read_text())
    schema = [bigquery.SchemaField.from_api_repr(field) for field in fields]
    return bigquery.Table(table_id or f"test-project.cockpit.{name}", schema=schema)


def fake_bigquery_client(table: bigquery.Table, loaded_ids: list[int]) -> MagicMock:
    """A BigQuery client mock: ``table`` exists and already holds ``loaded_ids``.

    SELECT queries return those ids; any other statement (MERGE, INSERT) returns no rows.
    """
    client = MagicMock(name="bigquery_client")
    client.get_table.return_value = table

    def query(sql: str, **_kwargs: Any) -> MagicMock:
        job = MagicMock(name="query_job")
        is_select = sql.startswith("SELECT")
        job.result.return_value = (
            [SimpleNamespace(activity_id=i) for i in loaded_ids] if is_select else []
        )
        return job

    client.query.side_effect = query
    return client


@pytest.fixture
def make_table() -> Callable[..., bigquery.Table]:
    """Factory fixture: ``make_table("records")`` builds a Table from its schema file."""
    return schema_table


@pytest.fixture
def make_bigquery_client() -> Callable[[bigquery.Table, list[int]], MagicMock]:
    """Factory fixture: ``make_bigquery_client(table, loaded_ids)`` builds a fake client."""
    return fake_bigquery_client
