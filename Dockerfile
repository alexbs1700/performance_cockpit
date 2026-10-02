# One image for every pipeline step; each Cloud Run Job chooses which command to run.
FROM python:3.12-slim

# Same uv version as the uv-lock pre-commit hook, so uv.lock is read exactly as it was written.
COPY --from=ghcr.io/astral-sh/uv:0.12.21 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Dependencies before code: this layer stays cached until uv.lock changes, so code-only
# changes rebuild in seconds.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN uv sync --locked --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# Unprivileged user: the job needs no root access inside the container.
RUN useradd --create-home --uid 10001 app
USER app

# No default command: each Cloud Run Job picks one of the console scripts from pyproject.toml
# with --command (garmin-download or load-warehouse) and passes its own --args.
