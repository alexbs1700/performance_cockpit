"""Tests for performance_cockpit.bigquery_loading."""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from google.cloud import bigquery

from performance_cockpit import bigquery_loading as bql
from performance_cockpit.storage import LocalStorage

TABLE_ID = "test-project.cockpit.records"


@pytest.fixture
def table(make_table: Callable[..., bigquery.Table]) -> bigquery.Table:
    """The records table, as BigQuery would return it."""
    return make_table("records")


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    """A raw zone holding three FIT files for test_user, plus an unrelated summary."""
    storage = LocalStorage(tmp_path)
    for activity_id in (1001, 1002, 1003):
        storage.write(f"test_user/fit/{activity_id}.fit", f"fit-{activity_id}".encode())
    storage.write("test_user/summary/1004.json", b"{}")
    return storage


def fake_parse(data: bytes, user_id: str, activity_id: int, loaded_at: datetime) -> list[dict]:
    """Return two rows per file, tagged with the file content so tests can trace them."""
    return [{"user_id": user_id, "activity_id": activity_id, "source": data.decode()}] * 2


def test_activity_id_from_key() -> None:
    assert bql.activity_id_from_key("test_user/fit/1001.fit") == 1001


def test_table_path(table: bigquery.Table) -> None:
    assert bql.table_path(table) == TABLE_ID


def test_insert_missing_sql_skips_activities_already_loaded() -> None:
    sql = bql.insert_missing_sql("p.d.t", "p.d.t_staging")

    assert sql.startswith("INSERT INTO `p.d.t`\nSELECT * FROM `p.d.t_staging` AS source")
    assert "WHERE NOT EXISTS" in sql
    assert "existing.user_id = source.user_id AND existing.activity_id = source.activity_id" in sql


class TestStagingTable:
    """The temporary table each batch goes through."""

    def test_creates_expiring_copy_and_deletes_it(self, table: bigquery.Table) -> None:
        client = MagicMock()

        with bql.staging_table(client, table) as staging_id:
            created = client.create_table.call_args.args[0]
            client.delete_table.assert_not_called()

        assert staging_id.startswith(f"{TABLE_ID}_staging_")
        assert created.schema == table.schema
        assert created.expires is not None
        client.delete_table.assert_called_once_with(staging_id, not_found_ok=True)

    def test_deleted_even_when_the_body_fails(self, table: bigquery.Table) -> None:
        client = MagicMock()

        with pytest.raises(RuntimeError), bql.staging_table(client, table):
            raise RuntimeError("load failed")

        client.delete_table.assert_called_once()

    def test_names_are_unique_per_run(self, table: bigquery.Table) -> None:
        client = MagicMock()

        with bql.staging_table(client, table) as first, bql.staging_table(client, table) as second:
            assert first != second


def test_loaded_activity_ids_is_parameterised(
    table: bigquery.Table, make_bigquery_client: Callable[..., MagicMock]
) -> None:
    client = make_bigquery_client(table, [1001])

    assert bql.loaded_activity_ids(client, TABLE_ID, "test_user") == {1001}

    sql = client.query.call_args.args[0]
    params = client.query.call_args.kwargs["job_config"].query_parameters
    assert sql == f"SELECT DISTINCT activity_id FROM `{TABLE_ID}` WHERE user_id = @user_id"
    assert params[0].value == "test_user"


class TestLoadPerActivity:
    """The incremental, batched load shared by laps and records."""

    def run(self, storage: LocalStorage, client: MagicMock, **kwargs: Any) -> int:
        """Load test_user's FIT files with ``fake_parse``."""
        return bql.load_per_activity(
            storage, client, TABLE_ID, "test_user", "fit", ".fit", fake_parse, **kwargs
        )

    def test_loads_only_activities_not_yet_loaded(
        self,
        storage: LocalStorage,
        table: bigquery.Table,
        make_bigquery_client: Callable[..., MagicMock],
    ) -> None:
        client = make_bigquery_client(table, [1001])

        assert self.run(storage, client) == 2

        rows = client.load_table_from_json.call_args.args[0]
        assert sorted({r["activity_id"] for r in rows}) == [1002, 1003]
        assert len(rows) == 4
        insert_sql = client.query.call_args_list[-1].args[0]
        assert insert_sql.startswith(f"INSERT INTO `{TABLE_ID}`")

    def test_splits_work_into_batches(
        self,
        storage: LocalStorage,
        table: bigquery.Table,
        make_bigquery_client: Callable[..., MagicMock],
    ) -> None:
        client = make_bigquery_client(table, [])

        self.run(storage, client, batch_size=2)

        assert client.load_table_from_json.call_count == 2
        assert client.create_table.call_count == client.delete_table.call_count == 2

    def test_unparseable_file_is_skipped(
        self,
        storage: LocalStorage,
        table: bigquery.Table,
        make_bigquery_client: Callable[..., MagicMock],
    ) -> None:
        def flaky_parse(data: bytes, user_id: str, activity_id: int, loaded_at: datetime) -> list:
            if activity_id == 1002:
                raise ValueError("corrupt file")
            return fake_parse(data, user_id, activity_id, loaded_at)

        client = make_bigquery_client(table, [])

        loaded = bql.load_per_activity(
            storage, client, TABLE_ID, "test_user", "fit", ".fit", flaky_parse
        )

        assert loaded == 2
        rows = client.load_table_from_json.call_args.args[0]
        assert sorted({r["activity_id"] for r in rows}) == [1001, 1003]

    def test_files_without_rows_create_no_staging_table(
        self,
        storage: LocalStorage,
        table: bigquery.Table,
        make_bigquery_client: Callable[..., MagicMock],
    ) -> None:
        client = make_bigquery_client(table, [])

        loaded = bql.load_per_activity(
            storage, client, TABLE_ID, "test_user", "fit", ".fit", lambda *_args: []
        )

        assert loaded == 0
        client.create_table.assert_not_called()

    def test_everything_loaded_touches_nothing(
        self,
        storage: LocalStorage,
        table: bigquery.Table,
        make_bigquery_client: Callable[..., MagicMock],
    ) -> None:
        client = make_bigquery_client(table, [1001, 1002, 1003])

        assert self.run(storage, client) == 0

        client.create_table.assert_not_called()
        client.load_table_from_json.assert_not_called()
