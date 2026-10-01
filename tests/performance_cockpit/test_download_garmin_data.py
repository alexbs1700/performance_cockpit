"""Tests for performance_cockpit.download_garmin_data."""

import json
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from garminconnect import (
    GarminConnectConnectionError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)

from performance_cockpit import download_garmin_data as dl
from performance_cockpit.storage import LocalStorage

MakeZip = Callable[[dict[str, bytes]], bytes]


@pytest.fixture
def garmin(make_zip: MakeZip, activity_summary: dict[str, Any]) -> MagicMock:
    """A fake Garmin client serving one page of two activities."""
    client = MagicMock(name="garmin_client")
    activities = [
        {**activity_summary, "activityId": 1001},
        {**activity_summary, "activityId": 1002},
    ]
    client.get_activities.side_effect = lambda start, _limit: activities if start == 0 else []
    client.get_activity_splits.side_effect = lambda aid: {"activityId": int(aid), "lapDTOs": []}
    client.download_activity.return_value = make_zip({"1001_ACTIVITY.fit": b"FITDATA"})
    client.get_activity.side_effect = lambda aid: {**activity_summary, "activityId": int(aid)}
    return client


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the politeness pause so tests run instantly."""
    monkeypatch.setattr(dl.time, "sleep", lambda _seconds: None)


def run_main(monkeypatch: pytest.MonkeyPatch, client: MagicMock, *args: str) -> None:
    """Run the CLI entry point against a fake client with the given arguments."""
    monkeypatch.setattr(dl, "get_client", lambda: client)
    monkeypatch.setenv("COCKPIT_USER_ID", "test_user")
    monkeypatch.setattr(sys, "argv", ["download_garmin_data", *args])
    dl.main()


class TestIterActivities:
    """Pagination over the activity list."""

    def test_follows_pages_until_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dl, "PAGE_SIZE", 2)
        pages = {0: [{"activityId": 1}, {"activityId": 2}], 2: [{"activityId": 3}]}
        client = MagicMock()
        client.get_activities.side_effect = lambda start, _limit: pages.get(start, [])

        ids = [a["activityId"] for a in dl.iter_activities(client)]

        assert ids == [1, 2, 3]
        assert [c.args[0] for c in client.get_activities.call_args_list] == [0, 2, 3]

    def test_empty_account_yields_nothing(self) -> None:
        client = MagicMock()
        client.get_activities.return_value = []

        assert list(dl.iter_activities(client)) == []


class TestExtractOriginal:
    """Unpacking Garmin's ORIGINAL zip."""

    def test_prefers_fit_file(self, make_zip: MakeZip) -> None:
        archive = make_zip({"readme.txt": b"x", "1001_ACTIVITY.FIT": b"FITDATA"})

        assert dl.extract_original(archive) == (b"FITDATA", ".fit")

    def test_falls_back_to_first_file(self, make_zip: MakeZip) -> None:
        archive = make_zip({"1001.gpx": b"<gpx/>"})

        assert dl.extract_original(archive) == (b"<gpx/>", ".gpx")

    def test_empty_archive_raises(self, make_zip: MakeZip) -> None:
        with pytest.raises(ValueError, match="empty archive"):
            dl.extract_original(make_zip({}))

    def test_not_a_zip_raises(self) -> None:
        with pytest.raises(zipfile.BadZipFile):
            dl.extract_original(b"not a zip")


def test_to_json_keeps_non_ascii() -> None:
    assert json.loads(dl.to_json({"name": "Carrera"})) == {"name": "Carrera"}
    assert "ñ".encode() in dl.to_json({"name": "Mañana"})


def test_activity_key_includes_user_id() -> None:
    assert dl.activity_key("test_user", "summary", "1001.json") == "test_user/summary/1001.json"


class InMemoryStorage:
    """A Storage that keeps objects in a dict, preserving write order."""

    def __init__(self) -> None:
        """Start empty."""
        self.objects: dict[str, bytes] = {}

    def exists(self, key: str) -> bool:
        """Return True if ``key`` was written."""
        return key in self.objects

    def write(self, key: str, data: bytes) -> None:
        """Record ``data`` under ``key``."""
        self.objects[key] = data


class TestDownloadOne:
    """Storing a single activity."""

    def test_writes_splits_fit_then_summary_last(self, garmin: MagicMock) -> None:
        storage = InMemoryStorage()

        dl.download_one(garmin, 1001, storage, "test_user")

        assert list(storage.objects) == [
            "test_user/splits/1001.json",
            "test_user/fit/1001.fit",
            "test_user/summary/1001.json",
        ]
        assert storage.objects["test_user/fit/1001.fit"] == b"FITDATA"
        assert json.loads(storage.objects["test_user/summary/1001.json"])["activityId"] == 1001

    def test_missing_original_still_writes_summary(self, garmin: MagicMock) -> None:
        garmin.download_activity.side_effect = GarminConnectNotFoundError("no file")
        storage = InMemoryStorage()

        dl.download_one(garmin, 1001, storage, "test_user")

        assert list(storage.objects) == [
            "test_user/splits/1001.json",
            "test_user/summary/1001.json",
        ]

    def test_failure_before_summary_leaves_activity_incomplete(self, garmin: MagicMock) -> None:
        garmin.get_activity.side_effect = GarminConnectConnectionError("boom")
        storage = InMemoryStorage()

        with pytest.raises(GarminConnectConnectionError):
            dl.download_one(garmin, 1001, storage, "test_user")

        assert not storage.exists("test_user/summary/1001.json")


class TestMain:
    """The CLI loop: idempotency, limits and error handling."""

    def test_downloads_all_new_activities(
        self, garmin: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_main(monkeypatch, garmin, "--data-dir", str(tmp_path))

        summaries = sorted(p.name for p in (tmp_path / "test_user" / "summary").iterdir())
        assert summaries == ["1001.json", "1002.json"]

    def test_skips_activities_already_stored(
        self, garmin: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        LocalStorage(tmp_path).write("test_user/summary/1001.json", b"{}")

        run_main(monkeypatch, garmin, "--data-dir", str(tmp_path))

        garmin.get_activity.assert_called_once_with("1002")

    def test_max_new_limits_downloads(
        self, garmin: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_main(monkeypatch, garmin, "--data-dir", str(tmp_path), "--max-new", "1")

        assert garmin.get_activity.call_count == 1

    def test_connection_error_skips_activity_and_continues(
        self, garmin: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def flaky_splits(aid: str) -> dict[str, Any]:
            if aid == "1001":
                raise GarminConnectConnectionError("boom")
            return {}

        garmin.get_activity_splits.side_effect = flaky_splits

        run_main(monkeypatch, garmin, "--data-dir", str(tmp_path))

        summaries = [p.name for p in (tmp_path / "test_user" / "summary").iterdir()]
        assert summaries == ["1002.json"]

    def test_rate_limit_stops_cleanly(
        self, garmin: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        garmin.get_activities.side_effect = GarminConnectTooManyRequestsError("slow down")

        run_main(monkeypatch, garmin, "--data-dir", str(tmp_path))

        garmin.get_activity.assert_not_called()

    def test_bucket_flag_writes_to_cloud_storage(
        self, garmin: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = InMemoryStorage()
        storage.write("test_user/summary/1001.json", b"{}")
        gcs_storage_cls = MagicMock(return_value=storage)
        monkeypatch.setattr(dl, "GcsStorage", gcs_storage_cls)

        run_main(monkeypatch, garmin, "--bucket", "test-bucket")

        gcs_storage_cls.assert_called_once_with("test-bucket")
        garmin.get_activity.assert_called_once_with("1002")
        assert storage.exists("test_user/summary/1002.json")

    def test_bucket_and_data_dir_are_mutually_exclusive(
        self, garmin: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(SystemExit):
            run_main(monkeypatch, garmin, "--bucket", "b", "--data-dir", str(tmp_path))
