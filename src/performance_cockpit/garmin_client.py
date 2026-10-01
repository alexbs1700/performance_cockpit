"""Garmin Connect client plus a quick connection check.

Run with:
    uv run --env-file .env python -m performance_cockpit.garmin_client
"""

import os
from getpass import getpass
from pathlib import Path

from garminconnect import Garmin

TOKEN_DIR = Path("~/.garminconnect").expanduser()
TOKEN_FILE = TOKEN_DIR / "garmin_tokens.json"


def get_client() -> Garmin:
    """Return a logged-in Garmin client.

    First run: asks for your password (and MFA code if enabled), then caches
    tokens in TOKEN_DIR. Later runs reuse the cached tokens, which refresh
    automatically, so no password is needed.
    """
    if TOKEN_FILE.exists():
        client = Garmin()
    else:
        client = Garmin(
            os.environ["GARMIN_EMAIL"],
            getpass("Garmin password: "),
            prompt_mfa=lambda: input("MFA code: "),
        )
    client.login(str(TOKEN_DIR))
    return client


def main() -> None:
    """Log in and print the five most recent activities as a connection check."""
    client = get_client()
    activities = client.get_activities(0, 5)  # the 5 most recent activities

    print(f"Connected. Last {len(activities)} activities:\n")
    for a in activities:
        km = (a.get("distance") or 0) / 1000
        minutes = (a.get("duration") or 0) / 60
        sport = a.get("activityType", {}).get("typeKey", "?")
        print(
            f"{a['startTimeLocal']}  {sport:<16} {km:6.2f} km  {minutes:6.1f} min"
            f"  id={a['activityId']}"
        )


if __name__ == "__main__":
    main()
