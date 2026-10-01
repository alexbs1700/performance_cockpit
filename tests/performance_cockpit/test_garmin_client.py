"""Tests for performance_cockpit.garmin_client."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from performance_cockpit import garmin_client
from performance_cockpit.token_secret import SecretTokens


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


class TestClientFromSecret:
    """Logging in from Secret Manager and writing refreshed tokens back."""

    OLD_TOKENS = '{"di_token": "old", "di_refresh_token": "r1", "di_client_id": "c"}'
    NEW_TOKENS = '{"di_token": "new", "di_refresh_token": "r2", "di_client_id": "c"}'

    @pytest.fixture
    def store(self) -> MagicMock:
        """A fake SecretTokenStore holding OLD_TOKENS as version 3."""
        store = MagicMock(name="token_store")
        store.load.return_value = SecretTokens(self.OLD_TOKENS, "projects/p/secrets/s/versions/3")
        return store

    def test_logs_in_with_secret_payload(self, fake_garmin: MagicMock, store: MagicMock) -> None:
        fake_garmin.return_value.client.dumps.return_value = self.OLD_TOKENS

        with garmin_client.client_from_secret(store) as client:
            assert client is fake_garmin.return_value

        fake_garmin.assert_called_once_with()
        client.login.assert_called_once_with(self.OLD_TOKENS)

    def test_unchanged_tokens_are_not_saved(self, fake_garmin: MagicMock, store: MagicMock) -> None:
        # Same content, different formatting: compared as JSON, so not a change.
        fake_garmin.return_value.client.dumps.return_value = self.OLD_TOKENS.replace(" ", "")

        with garmin_client.client_from_secret(store):
            pass

        store.replace.assert_not_called()

    def test_refreshed_tokens_replace_loaded_version(
        self, fake_garmin: MagicMock, store: MagicMock
    ) -> None:
        fake_garmin.return_value.client.dumps.return_value = self.NEW_TOKENS

        with garmin_client.client_from_secret(store):
            pass

        store.replace.assert_called_once_with(self.NEW_TOKENS, previous=store.load.return_value)

    def test_refreshed_tokens_are_saved_even_when_run_fails(
        self, fake_garmin: MagicMock, store: MagicMock
    ) -> None:
        fake_garmin.return_value.client.dumps.return_value = self.NEW_TOKENS

        with pytest.raises(RuntimeError), garmin_client.client_from_secret(store):
            raise RuntimeError("download crashed")

        store.replace.assert_called_once()

    def test_cleared_tokens_after_failed_login_are_not_saved(
        self, fake_garmin: MagicMock, store: MagicMock
    ) -> None:
        fake_garmin.return_value.login.side_effect = RuntimeError("bad tokens")
        fake_garmin.return_value.client.dumps.return_value = (
            '{"di_token": null, "di_refresh_token": null, "di_client_id": null}'
        )

        with pytest.raises(RuntimeError), garmin_client.client_from_secret(store):
            pass

        store.replace.assert_not_called()
