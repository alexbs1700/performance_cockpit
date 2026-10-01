"""Garmin tokens kept in a Secret Manager secret.

Garmin may hand out a new refresh token whenever the access token is refreshed, so a job
that only read the secret would eventually hold a stale token. The store therefore also
writes refreshed tokens back as a new version and disables the version they replace,
which keeps a single enabled version and stays within the free tier.
"""

from dataclasses import dataclass

from google.cloud import secretmanager


@dataclass(frozen=True)
class SecretTokens:
    """Token JSON together with the exact secret version it was read from."""

    payload: str
    version_name: str


class SecretTokenStore:
    """Reads and writes Garmin token JSON in one Secret Manager secret."""

    def __init__(
        self, secret_name: str, client: secretmanager.SecretManagerServiceClient | None = None
    ) -> None:
        """Create a store for ``secret_name``, e.g. ``projects/my-project/secrets/garmin-tokens``.

        Credentials come from Application Default Credentials: your gcloud login locally,
        the job's service account on Cloud Run.
        """
        self.secret_name = secret_name
        self.client = client or secretmanager.SecretManagerServiceClient()

    def load(self) -> SecretTokens:
        """Return the latest enabled version of the tokens."""
        response = self.client.access_secret_version(name=f"{self.secret_name}/versions/latest")
        # response.name is the concrete version (".../versions/3"), not the "latest" alias,
        # so we know exactly which version to disable after saving a replacement.
        return SecretTokens(payload=response.payload.data.decode(), version_name=response.name)

    def replace(self, payload: str, previous: SecretTokens) -> None:
        """Store ``payload`` as the new latest version, then disable ``previous``."""
        self.client.add_secret_version(
            request={"parent": self.secret_name, "payload": {"data": payload.encode()}}
        )
        # Disable rather than destroy: a disabled version can be re-enabled if the new
        # tokens turn out to be bad.
        self.client.disable_secret_version(request={"name": previous.version_name})
