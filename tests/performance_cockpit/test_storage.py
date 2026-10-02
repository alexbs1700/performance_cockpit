"""Tests for performance_cockpit.storage."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from performance_cockpit.storage import GcsStorage, LocalStorage


class TestLocalStorage:
    """Files under a root directory."""

    def test_write_creates_parents_and_leaves_no_temp(self, tmp_path: Path) -> None:
        LocalStorage(tmp_path).write("test_user/summary/1001.json", b"{}")

        target = tmp_path / "test_user" / "summary" / "1001.json"
        assert target.read_bytes() == b"{}"
        assert list(target.parent.iterdir()) == [target]

    def test_write_replaces_existing_file(self, tmp_path: Path) -> None:
        storage = LocalStorage(tmp_path)
        storage.write("a.json", b"old")

        storage.write("a.json", b"new")

        assert (tmp_path / "a.json").read_bytes() == b"new"

    def test_exists(self, tmp_path: Path) -> None:
        storage = LocalStorage(tmp_path)
        assert not storage.exists("a.json")

        storage.write("a.json", b"{}")

        assert storage.exists("a.json")

    def test_read_returns_written_content(self, tmp_path: Path) -> None:
        storage = LocalStorage(tmp_path)
        storage.write("u/summary/1.json", b"{}")

        assert storage.read("u/summary/1.json") == b"{}"

    def test_list_keys_under_prefix_skips_temp_files(self, tmp_path: Path) -> None:
        storage = LocalStorage(tmp_path)
        storage.write("u/summary/2.json", b"{}")
        storage.write("u/summary/1.json", b"{}")
        storage.write("u/splits/1.json", b"{}")
        (tmp_path / "u" / "summary" / "3.json.tmp").write_bytes(b"partial")

        assert list(storage.list_keys("u/summary/")) == ["u/summary/1.json", "u/summary/2.json"]

    def test_list_keys_missing_prefix_is_empty(self, tmp_path: Path) -> None:
        assert list(LocalStorage(tmp_path).list_keys("nobody/summary/")) == []


class TestGcsStorage:
    """Objects in a bucket, with the Cloud Storage client mocked."""

    @pytest.fixture
    def gcs_client(self) -> MagicMock:
        """A fake storage.Client; its bucket hands out mock blobs."""
        return MagicMock(name="storage_client")

    def test_builds_bucket_handle_without_fetching_it(self, gcs_client: MagicMock) -> None:
        GcsStorage("test-bucket", client=gcs_client)

        gcs_client.bucket.assert_called_once_with("test-bucket")
        gcs_client.get_bucket.assert_not_called()

    @pytest.mark.parametrize("found", [True, False])
    def test_exists_checks_blob_under_raw_prefix(self, gcs_client: MagicMock, found: bool) -> None:
        bucket = gcs_client.bucket.return_value
        bucket.blob.return_value.exists.return_value = found

        assert GcsStorage("test-bucket", client=gcs_client).exists("u/summary/1.json") is found
        bucket.blob.assert_called_once_with("raw/u/summary/1.json")

    @pytest.mark.parametrize(
        ("key", "content_type"),
        [("u/summary/1.json", "application/json"), ("u/fit/1.fit", "application/octet-stream")],
    )
    def test_write_uploads_with_content_type(
        self, gcs_client: MagicMock, key: str, content_type: str
    ) -> None:
        bucket = gcs_client.bucket.return_value

        GcsStorage("test-bucket", client=gcs_client).write(key, b"data")

        bucket.blob.assert_called_once_with(f"raw/{key}")
        bucket.blob.return_value.upload_from_string.assert_called_once_with(
            b"data", content_type=content_type
        )

    def test_read_downloads_blob_under_raw_prefix(self, gcs_client: MagicMock) -> None:
        bucket = gcs_client.bucket.return_value
        bucket.blob.return_value.download_as_bytes.return_value = b"{}"

        assert GcsStorage("test-bucket", client=gcs_client).read("u/summary/1.json") == b"{}"
        bucket.blob.assert_called_once_with("raw/u/summary/1.json")

    def test_list_keys_strips_raw_prefix(self, gcs_client: MagicMock) -> None:
        bucket = gcs_client.bucket.return_value
        bucket.list_blobs.return_value = [
            SimpleNamespace(name="raw/u/summary/1.json"),
            SimpleNamespace(name="raw/u/summary/2.json"),
        ]

        keys = list(GcsStorage("test-bucket", client=gcs_client).list_keys("u/summary/"))

        bucket.list_blobs.assert_called_once_with(prefix="raw/u/summary/")
        assert keys == ["u/summary/1.json", "u/summary/2.json"]
