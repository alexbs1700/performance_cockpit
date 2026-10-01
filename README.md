# performance_cockpit

A personal pipeline that ingests Garmin running data into Google Cloud, built as a hands-on way
to learn GCP: Cloud Storage, BigQuery, Secret Manager, Cloud Run and Cloud Scheduler, and later
Terraform and GitHub Actions.

## Architecture

```
Garmin Connect ──> downloader ──> Cloud Storage (raw zone) ──> BigQuery (warehouse)
                     │                                           activities, garmin_laps,
                     └── tokens from Secret Manager               records, segments
```

| Piece | Status |
|---|---|
| Downloader (local or Cloud Storage, idempotent) | done |
| Raw zone bucket with the full history backfilled | done |
| Garmin tokens in Secret Manager, refreshed tokens written back | done |
| Cloud Run Job, triggered daily by Cloud Scheduler | next |
| BigQuery tables: `activities`, `garmin_laps`, `records`, `segments` | planned |
| FastAPI service on Cloud Run | planned |

Every storage path and table includes a `user_id`, so a second user can be added later.

### Raw zone layout

Files are stored exactly as received from Garmin, under `data/raw/` locally or
`gs://training-performance-cockpit-raw/raw/` in the cloud:

```
{user_id}/summary/{activity_id}.json   full activity summary (written last: marks completion)
{user_id}/splits/{activity_id}.json    laps as recorded by Garmin
{user_id}/fit/{activity_id}.fit        original file from the watch
```

Reruns are safe: an activity whose summary already exists is skipped, and an interrupted
activity is downloaded again on the next run.

## GCP resources

Project `training-performance-cockpit`, region `europe-west1`.

| Resource | Notes |
|---|---|
| Bucket `training-performance-cockpit-raw` | single region, uniform bucket-level access, public access prevention |
| Secret `garmin-tokens` | replicated in `europe-west1` only; one enabled version at a time |
| Service account `garmin-ingest` | runs the ingestion job |

`garmin-ingest` follows least privilege, with every role granted on a single resource:

| Role | Granted on | Why |
|---|---|---|
| `roles/storage.objectUser` | the bucket | read, create and overwrite raw files |
| `roles/secretmanager.secretAccessor` | the secret | read the Garmin tokens |
| `roles/secretmanager.secretVersionManager` | the secret | save refreshed tokens, disable old versions |

## Setup

Requires [uv](https://docs.astral.sh/uv/) and the [gcloud CLI](https://cloud.google.com/sdk/docs/install).

```bash
uv sync
uv run pre-commit install
gcloud auth application-default login   # credentials for the Python Google Cloud clients
```

For the first Garmin login, create a `.env` file (gitignored) with `GARMIN_EMAIL=...`. The
password and MFA code are asked interactively and never stored; only tokens are cached.

## Usage

Check the Garmin connection (prints your 5 most recent activities):

```bash
uv run --env-file .env python -m performance_cockpit.garmin_client
```

Download to the local raw zone, with tokens cached in `~/.garminconnect`:

```bash
uv run --env-file .env python -m performance_cockpit.download_garmin_data --max-new 5
```

Download to Cloud Storage, with tokens from Secret Manager (how the cloud job runs):

```bash
uv run python -m performance_cockpit.download_garmin_data \
    --bucket training-performance-cockpit-raw \
    --token-secret projects/training-performance-cockpit/secrets/garmin-tokens
```

Once the secret is in use, treat it as the only copy of the tokens: a refresh may invalidate
the refresh token cached on your laptop.

| Option | Meaning |
|---|---|
| `--max-new N` | stop after N new activities |
| `--data-dir PATH` | local raw zone (default `data/raw`) |
| `--bucket NAME` | write to Cloud Storage instead of disk |
| `--token-secret NAME` | load and save Garmin tokens in Secret Manager |
| `COCKPIT_USER_ID` env var | user id in storage paths (default `me`) |

## Development

```bash
uv run pytest                        # unit tests, fully offline
uv run pre-commit run --all-files    # ruff, gitleaks, uv-lock, nbstripout, pytest
```

Tests mock Garmin and every GCP client, so they need no network or credentials. Test fixtures
in `tests/fixtures/` are synthetic and contain no personal data. Conventions for contributors
(human or AI) are in [CLAUDE.md](CLAUDE.md).

```
src/performance_cockpit/
    garmin_client.py          Garmin login: local token cache or Secret Manager
    download_garmin_data.py   idempotent downloader (CLI entry point)
    storage.py                local and Cloud Storage backends
    token_secret.py           Garmin tokens in Secret Manager
tests/performance_cockpit/    one test module per source module
notebooks/                    exploration; outputs stripped on commit
```
