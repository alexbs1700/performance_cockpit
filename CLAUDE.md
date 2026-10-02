# performance_cockpit

Personal learning project: a GCP pipeline that ingests Garmin running data. The owner wants to
understand every step, so explain what you are doing and why. Ask before running any command
that creates, modifies or deletes cloud resources or could cost money.

- GCP project `training-performance-cockpit`, region `europe-west1`.
- Raw data lake in Cloud Storage under `raw/{user_id}/...`; warehouse in BigQuery.
- Every path and table includes a `user_id`, so a second user can be added later.

## Commands

- Tests: `uv run pytest`
- All checks (ruff, gitleaks, uv-lock, nbstripout, pytest): `uv run pre-commit run --all-files`
- Downloader: `uv run --env-file .env python -m performance_cockpit.download_garmin_data --max-new 5`
- Warehouse load: `uv run load-warehouse --dataset training-performance-cockpit.cockpit --data-dir data/raw`

## Conventions

- snake_case for modules, files, functions and variables; PascalCase for classes;
  UPPER_SNAKE_CASE for constants.
- Self-documenting code: descriptive names, type hints on every function, and a docstring on
  every module, class and public function. Comments explain why, not what.
- Unit tests for everything you create: pytest, in `tests/` mirroring the `src/` structure.
  Tests must run offline: mock the Garmin client and GCP clients, and use `tmp_path` for files.
  Fixtures go in `tests/fixtures/`, never contain GPS coordinates or other personal data, and
  stay small.
- Before declaring a task done, run `uv run pytest` and `uv run pre-commit run --all-files`,
  and fix anything that fails.
- Never commit, log or print secrets, tokens or personal data.
- IAM follows least privilege: grant roles on specific resources (a bucket, a secret), not on
  the whole project. Cost guardrails always apply, for example `--max-instances=1` on Cloud Run.
- Never use the em dash character in code, comments, docs or messages.
- Prefer small, reviewable changes, and summarize what changed at the end of each task.
