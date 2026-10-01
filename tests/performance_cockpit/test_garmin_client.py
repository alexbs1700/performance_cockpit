"""Tests for performance_cockpit.garmin_client."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from performance_cockpit import garmin_client


@pytest.fixture
def fake_garmin(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the Garmin class so no network call is ever made."""
    garmin_cls = MagicMock(name="Garmin")
    monkeypatch.setattr(garmin_client, "Garmin", garmin_cls)
    return garmin_cls


@pytest.fixture
def token_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the token cache at a temporary directory."""
    monkeypatch.setattr(garmin_client, "TOKEN_DIR", tmp_path)
    monkeypatch.setattr(garmin_client, "TOKEN_FILE", tmp_path / "garmin_tokens.json")
    return tmp_path


def test_get_client_reuses_cached_tokens(fake_garmin: MagicMock, token_dir: Path) -> None:
    (token_dir / "garmin_tokens.json").write_text("{}")

    client = garmin_client.get_client()

    fake_garmin.assert_called_once_with()
    client.login.assert_called_once_with(str(token_dir))


def test_get_client_prompts_for_credentials_without_cache(
    fake_garmin: MagicMock, token_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GARMIN_EMAIL", "runner@example.com")
    monkeypatch.setattr(garmin_client, "getpass", lambda _prompt: "not-a-real-password")

    client = garmin_client.get_client()

    args, kwargs = fake_garmin.call_args
    assert args == ("runner@example.com", "not-a-real-password")
    assert callable(kwargs["prompt_mfa"])
    client.login.assert_called_once_with(str(token_dir))


def test_main_prints_recent_activities(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], activity_summary: dict
) -> None:
    client = MagicMock()
    client.get_activities.return_value = [activity_summary]
    monkeypatch.setattr(garmin_client, "get_client", lambda: client)

    garmin_client.main()

    out = capsys.readouterr().out
    client.get_activities.assert_called_once_with(0, 5)
    assert "running" in out
    assert "10.00 km" in out
    assert "id=1001" in out
