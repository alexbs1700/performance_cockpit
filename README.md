# performance_cockpit

A personal pipeline that ingests Garmin running data into Google Cloud, built as a hands-on way
to learn GCP: Cloud Storage, BigQuery, Secret Manager, Cloud Run and Cloud Scheduler, and later
Terraform and GitHub Actions.

## Architecture

```
06:00  garmin-ingest job:  Garmin Connect ──> Cloud Storage (raw zone)
                             (tokens from Secret Manager)
06:30  cockpit-load job:   Cloud Storage ──> BigQuery: activities, garmin_laps, records
```

Both jobs run the same container image with a different command and service account, are
triggered by Cloud Scheduler, and email an alert when an execution fails.

| Piece | Status |
|---|---|
| Downloader (local or Cloud Storage, idempotent) | done |
| Raw zone bucket with the full history backfilled | done |
| Garmin tokens in Secret Manager, refreshed tokens written back | done |
| Daily Cloud Run Jobs with Cloud Scheduler and a failure alert | done |
| BigQuery tables `activities`, `garmin_laps`, `records` | done |
| BigQuery table `segments` (work and rest blocks) | next |
| CI/CD with GitHub Actions | planned |
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

### Warehouse

Dataset `cockpit` in BigQuery. Schemas live in [infra/bigquery/](infra/bigquery/); the loaders
read the schema from the live table, so the files are the single source of truth.

| Table | One row per | Source | Layout |
|---|---|---|---|
| `activities` | activity | `summary/*.json` | clustered by `user_id`, `activity_type` |
| `garmin_laps` | lap | `splits/*.json` | clustered by `user_id`, `activity_id` |
| `records` | FIT record (every 1 to 6 s) | `fit/*.fit` | partitioned by month of `recorded_at`, clustered by `user_id`, `activity_id` |

Loads are incremental and idempotent: only activities missing from a table are read, rows go
through a short-lived staging table, and the final SQL (MERGE for `activities`, insert-if-missing
for the others) never creates duplicates. Fields without a typed column stay available in the
`summary` and `lap` JSON columns. Location data is kept: start and end points on activities and
laps, and the full track in `records`.

## GCP resources

Project `training-performance-cockpit`, region `europe-west1`.

| Resource | Notes |
|---|---|
| Bucket `training-performance-cockpit-raw` | single region, uniform bucket-level access, public access prevention |
| Secret `garmin-tokens` | replicated in `europe-west1` only; one enabled version at a time |
| BigQuery dataset `cockpit` | `europe-west1`; tables above |
| Artifact Registry repository `cockpit` | container images, tagged `v1`, `v2`, ... |
| Cloud Run Jobs `garmin-ingest`, `cockpit-load` | one task, no retries, 30 min timeout |
| Cloud Scheduler `garmin-ingest-daily`, `cockpit-load-daily` | 06:00 and 06:30 Europe/Madrid |
| Alert policy "Pipeline job execution failed" | emails on any failed execution; [infra/monitoring/](infra/monitoring/) |

Service accounts follow least privilege, with roles granted on single resources wherever
BigQuery allows it:

| Service account | Role | Granted on | Why |
|---|---|---|---|
| `garmin-ingest` | `roles/storage.objectUser` | the bucket | read, create and overwrite raw files |
| `garmin-ingest` | `roles/secretmanager.secretAccessor` | the secret | read the Garmin tokens |
| `garmin-ingest` | `roles/secretmanager.secretVersionManager` | the secret | save refreshed tokens, disable old versions |
| `cockpit-loader` | `roles/storage.objectViewer` | the bucket | read raw files, never write them |
| `cockpit-loader` | `roles/bigquery.dataEditor` | the `cockpit` dataset | write the tables |
| `cockpit-loader` | `roles/bigquery.jobUser` | the project | run load and query jobs (only grantable per project) |
| `cockpit-scheduler` | `roles/run.invoker` | each job | start the jobs on schedule |

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

Load the raw zone into BigQuery, from local files (fast backfill) or from the bucket (what the
cloud job runs):

```bash
uv run load-warehouse --dataset training-performance-cockpit.cockpit --data-dir data/raw
uv run load-warehouse --dataset training-performance-cockpit.cockpit --bucket training-performance-cockpit-raw
```

| Option | Meaning |
|---|---|
| `--only TABLE ...` | load just some of `activities`, `garmin_laps`, `records` |
| `--reload-activities` | rewrite every `activities` row, e.g. after adding a column |

## Deploying

Pushing to git does not deploy anything yet. Build a new image with a new tag, then point the
jobs at it (their command, arguments and service account are kept):

```bash
gcloud builds submit --region=europe-west1 \
    --tag=europe-west1-docker.pkg.dev/training-performance-cockpit/cockpit/garmin-ingest:vN
gcloud run jobs update garmin-ingest --region=europe-west1 --image=...:vN
gcloud run jobs update cockpit-load --region=europe-west1 --image=...:vN
```

Never reuse a tag: jobs pin the image digest when updated, so a rebuilt tag is not picked up.

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
    download_garmin_data.py   idempotent downloader (garmin-download command)
    storage.py                local and Cloud Storage backends
    token_secret.py           Garmin tokens in Secret Manager
    load_warehouse.py         loads all tables (load-warehouse command)
    load_activities.py        summaries -> activities (MERGE)
    load_laps.py              splits -> garmin_laps
    load_records.py           FIT files -> records
    bigquery_loading.py       shared staging and incremental load helpers
infra/bigquery/               table schemas
infra/monitoring/             alert policy
tests/performance_cockpit/    one test module per source module
notebooks/                    exploration; outputs stripped on commit
```
