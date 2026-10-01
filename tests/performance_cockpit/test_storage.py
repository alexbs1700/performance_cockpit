"""Tests for performance_cockpit.storage."""

from pathlib import Path
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
