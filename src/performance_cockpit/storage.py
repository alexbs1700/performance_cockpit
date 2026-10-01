"""Storage backends for the raw zone of the data lake.

Keys are slash-separated paths relative to the raw zone, such as
``{user_id}/summary/{activity_id}.json``. The same key maps to ``data/raw/{key}`` on disk or
``gs://{bucket}/raw/{key}`` in Cloud Storage, so the downloader does not care where it writes.
"""

from pathlib import Path
from typing import Protocol

from google.cloud import storage

GCS_PREFIX = "raw"
JSON_CONTENT_TYPE = "application/json"
BINARY_CONTENT_TYPE = "application/octet-stream"


class Storage(Protocol):
    """The minimal interface the downloader needs from a storage backend."""

    def exists(self, key: str) -> bool:
        """Return True if an object is stored under ``key``."""
        ...

    def write(self, key: str, data: bytes) -> None:
        """Store ``data`` under ``key`` all or nothing, replacing any previous object."""
        ...


class LocalStorage:
    """Stores objects as files under a root directory."""

    def __init__(self, root: Path) -> None:
        """Create a backend rooted at ``root``; directories are created on first write."""
        self.root = root

    def exists(self, key: str) -> bool:
        """Return True if the file for ``key`` exists."""
        return (self.root / key).exists()

    def write(self, key: str, data: bytes) -> None:
        """Write through a temp file so a crash never leaves a half-written file."""
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)


class GcsStorage:
    """Stores objects in a Cloud Storage bucket under the ``raw/`` prefix."""

    def __init__(self, bucket_name: str, client: storage.Client | None = None) -> None:
        """Create a backend for ``bucket_name`` using Application Default Credentials.

        ``client.bucket()`` only builds a local handle without calling the API, so the caller
        needs object permissions on the bucket but not ``storage.buckets.get``.
        """
        self.bucket = (client or storage.Client()).bucket(bucket_name)

    def exists(self, key: str) -> bool:
        """Return True if the object for ``key`` exists in the bucket."""
        return self.bucket.blob(f"{GCS_PREFIX}/{key}").exists()

    def write(self, key: str, data: bytes) -> None:
        """Upload ``data``; GCS only makes an object visible once its upload has completed."""
        content_type = JSON_CONTENT_TYPE if key.endswith(".json") else BINARY_CONTENT_TYPE
        self.bucket.blob(f"{GCS_PREFIX}/{key}").upload_from_string(data, content_type=content_type)
