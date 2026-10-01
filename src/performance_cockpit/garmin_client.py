"""Garmin Connect client plus a quick connection check.

Run with:
    uv run --env-file .env python -m performance_cockpit.garmin_client
"""

import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from getpass import getpass
from pathlib import Path

from garminconnect import Garmin

from performance_cockpit.token_secret import SecretTokenStore

log = logging.getLogger(__name__)

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


@contextmanager
def client_from_secret(store: SecretTokenStore) -> Iterator[Garmin]:
    """Yield a Garmin client logged in with tokens from Secret Manager.

    Never prompts for a password: if the stored tokens are unusable, login raises. On exit,
    even after an error, tokens refreshed during the run are written back to the secret,
    because the refresh token they replaced may no longer work.
    """
    loaded = store.load()
    client = Garmin()
    try:
        client.login(loaded.payload)
        yield client
    finally:
        current = client.client.dumps()
        current_tokens = json.loads(current)
        # A failed login clears the tokens; never overwrite the secret with an empty set.
        if current_tokens.get("di_refresh_token") and current_tokens != json.loads(loaded.payload):
            store.replace(current, previous=loaded)
            log.info("Saved refreshed Garmin tokens to Secret Manager")


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
