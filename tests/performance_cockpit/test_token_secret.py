"""Tests for performance_cockpit.token_secret."""

from unittest.mock import MagicMock

import pytest

from performance_cockpit.token_secret import SecretTokens, SecretTokenStore

SECRET = "projects/test-project/secrets/test-tokens"


@pytest.fixture
def sm_client() -> MagicMock:
    """A fake SecretManagerServiceClient."""
    return MagicMock(name="secret_manager_client")


def test_load_reads_latest_and_remembers_concrete_version(sm_client: MagicMock) -> None:
    response = sm_client.access_secret_version.return_value
    response.payload.data = b'{"di_token": "fake"}'
    response.name = f"{SECRET}/versions/3"

    loaded = SecretTokenStore(SECRET, client=sm_client).load()

    sm_client.access_secret_version.assert_called_once_with(name=f"{SECRET}/versions/latest")
    assert loaded == SecretTokens(
        payload='{"di_token": "fake"}', version_name=f"{SECRET}/versions/3"
    )


def test_replace_adds_new_version_then_disables_previous(sm_client: MagicMock) -> None:
    previous = SecretTokens(payload="{}", version_name=f"{SECRET}/versions/3")
    calls = MagicMock()
    sm_client.add_secret_version.side_effect = lambda **_kw: calls("add")
    sm_client.disable_secret_version.side_effect = lambda **_kw: calls("disable")

    SecretTokenStore(SECRET, client=sm_client).replace('{"new": 1}', previous=previous)

    sm_client.add_secret_version.assert_called_once_with(
        request={"parent": SECRET, "payload": {"data": b'{"new": 1}'}}
    )
    sm_client.disable_secret_version.assert_called_once_with(
        request={"name": f"{SECRET}/versions/3"}
    )
    # Adding first means a failure in between never leaves the secret without an enabled version.
    assert [c.args[0] for c in calls.call_args_list] == ["add", "disable"]
