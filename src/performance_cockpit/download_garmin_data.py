"""Download all Garmin activities to the local raw zone.

For each activity it stores:
    data/raw/{user_id}/summary/{activity_id}.json   full activity summary
    data/raw/{user_id}/splits/{activity_id}.json    laps as recorded by Garmin
    data/raw/{user_id}/fit/{activity_id}.fit        original file from the watch

Idempotent: activities whose summary file already exists are skipped. The
summary is written last, so an interrupted run simply resumes next time.

Run with:
    uv run --env-file .env python -m performance_cockpit.download_garmin_data --max-new 5
"""

import argparse
import io
import json
import logging
import os
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from garminconnect import (
    Garmin,
    GarminConnectConnectionError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)

from performance_cockpit.garmin_client import get_client

log = logging.getLogger(__name__)

PAGE_SIZE = 100
PAUSE_SECONDS = 1.0  # Between activities, to stay well under Garmin's rate limit


def iter_activities(client: Garmin) -> Iterator[dict[str, Any]]:
    """Yield every activity in the account, newest first."""
    start = 0
    while True:
        page = client.get_activities(start, PAGE_SIZE)
        if not page:
            return
        yield from page
        start += len(page)


def write_atomic(path: Path, data: bytes) -> None:
    """Write through a temp file so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def to_json(obj: Any) -> bytes:
    """Serialize to pretty-printed UTF-8 JSON, keeping non-ASCII characters readable."""
    return json.dumps(obj, indent=2, ensure_ascii=False).encode()


def extract_original(archive: bytes) -> tuple[bytes, str]:
    """Return (content, extension) of the activity file inside Garmin's ORIGINAL zip."""
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        names = zf.namelist()
        if not names:
            raise ValueError("empty archive")
        fit_files = [n for n in names if n.lower().endswith(".fit")]
        name = (fit_files or names)[0]
        return zf.read(name), Path(name).suffix.lower() or ".bin"


def download_one(client: Garmin, activity_id: int, root: Path) -> None:
    """Store splits, the original file and the summary of one activity."""
    aid = str(activity_id)

    splits = client.get_activity_splits(aid)
    write_atomic(root / "splits" / f"{aid}.json", to_json(splits))

    try:
        archive = client.download_activity(aid, dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL)
        content, ext = extract_original(archive)
        write_atomic(root / "fit" / f"{aid}{ext}", content)
    except (GarminConnectNotFoundError, zipfile.BadZipFile, ValueError) as e:
        # Manually created activities have no device file: keep going without it.
        log.warning("No original file for %s: %s", aid, e)

    # Summary goes last: its presence marks the activity as complete.
    summary = client.get_activity(aid)
    write_atomic(root / "summary" / f"{aid}.json", to_json(summary))


def main() -> None:
    """Download every activity not yet stored, stopping early on rate limits."""
    parser = argparse.ArgumentParser(description="Download Garmin activities.")
    parser.add_argument(
        "--max-new", type=int, default=None, help="stop after N new activities (for testing)"
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    user_id = os.environ.get("COCKPIT_USER_ID", "me")
    root = args.data_dir / user_id
    client = get_client()

    new = skipped = failed = 0
    try:
        for activity in iter_activities(client):
            activity_id = activity["activityId"]
            if (root / "summary" / f"{activity_id}.json").exists():
                skipped += 1
                continue
            if args.max_new is not None and new >= args.max_new:
                break
            try:
                download_one(client, activity_id, root)
                new += 1
                log.info(
                    "Saved %s  %s  %s",
                    activity_id,
                    activity.get("startTimeLocal"),
                    activity.get("activityType", {}).get("typeKey"),
                )
            except GarminConnectConnectionError as e:
                failed += 1
                log.error("Failed %s: %s", activity_id, e)
            time.sleep(PAUSE_SECONDS)
    except GarminConnectTooManyRequestsError:
        log.warning("Rate limited by Garmin. Stopping here; rerun later to resume.")

    log.info("Done: %d new, %d already stored, %d failed", new, skipped, failed)


if __name__ == "__main__":
    main()
