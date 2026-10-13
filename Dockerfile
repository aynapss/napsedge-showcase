# syntax=docker/dockerfile:1
#
# Phase 9A -- controlled single-host containerized internal runtime.
#
# Three named build targets share one dependency/base stage:
#   docker build --target api       -t napsedge-api .
#   docker build --target streamlit -t napsedge-streamlit .
#   docker build --target discord   -t napsedge-discord .
#   docker build --target backup    -t napsedge-backup .   (Phase 9B-7A, see the target)
#
# NOT included anywhere in this file, by explicit Phase 9A scope
# (docs/phase9_deployment_hardening_readiness.md, docs/phase9a_deployment_contract.md):
# the Stripe webhook, port 8787, the legacy Flask server, notebook
# execution, the live Odds/agent worker, a scraping scheduler, a public
# reverse proxy, TLS termination, Redis, or a production PostgreSQL
# image. This is a local/internal rehearsal runtime, not a production
# deployment.
#
# Base image tag is pinned to the exact accepted runtime (.python-version
# declares 3.13) and verified by an actual `docker build` -- never
# silently substituted for a different tag if this one is unavailable.
FROM python:3.13-slim AS base

# Deterministic, non-interactive, no-bytecode-cache, no-pip-cache build.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

# Build tooling only -- needed only if a pinned package in
# constraints-release-py313.txt has no manylinux wheel for this exact
# Python/arch combination; removed from no layer (this is the only
# stage any of the three targets build from, and none of them need a
# compiler at runtime, only the already-built site-packages).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin napsedge

WORKDIR /app

# Runtime dependencies only -- requirements-dev.txt is never installed
# into any runtime image. Constraints applied exactly as the committed
# release-candidate runbook documents.
COPY requirements.txt constraints-release-py313.txt ./
RUN pip install -c constraints-release-py313.txt -r requirements.txt

# Only committed runtime files, copied explicitly (never `COPY . .`) --
# no .git, .env, TeamDump, cache, PlayerCards, notebooks, tests, scratch
# files, local databases, or virtual environments enter any layer.
COPY src/ ./src/
COPY pages/ ./pages/
COPY app.py ./app.py
COPY alembic/ ./alembic/
COPY alembic.ini ./alembic.ini
COPY .python-version ./.python-version
# Public showcase: the runtime's data inputs come from demo/ (fictional teams, synthetic values), copied to the exact paths the
# unchanged application reads. The private repository copies the real files here instead.
# - the NET rankings cache (NAPSEDGE_NET_RANKINGS_CACHE_PATH's default);
COPY ["demo/net_rankings_demo.csv", "./Reference Py Files/net_rankings_2026.csv"]
# - the team registry, read as data (never executed) from the notebook's `conferences = {...}` cell;
COPY demo/cbb_teamScrape_v15_TEST.ipynb ./cbb_teamScrape_v15_TEST.ipynb
# - the SR-slug reconciliation record (no disputes among the demo teams);
COPY demo/docs/sr_slug_reconciliation.json ./docs/sr_slug_reconciliation.json
# - per-team injury caches, so the injury route and page have data.
COPY demo/TeamDump/ ./TeamDump/

RUN chown -R napsedge:napsedge /app
USER napsedge

# ---------------------------------------------------------------------
FROM base AS api
EXPOSE 8000
ENTRYPOINT ["python", "-m", "src.deployment.entrypoints", "api"]

# ---------------------------------------------------------------------
FROM base AS streamlit
EXPOSE 8501
ENTRYPOINT ["python", "-m", "src.deployment.entrypoints", "streamlit"]

# ---------------------------------------------------------------------
FROM base AS discord
# No EXPOSE -- the Discord bot makes only outbound connections.
# Does NOT start by default under plain `docker run <image>` with no
# arguments in the sense of connecting live: the entrypoint always
# validates configuration first and fails closed if DISCORD_BOT_TOKEN
# is unset, exactly like `src.discord_bot.bot.main()` already does.
# Compose's own `discord` profile (compose.yaml) is the actual control
# that keeps this image from ever running under plain `docker compose up`.
ENTRYPOINT ["python", "-m", "src.deployment.entrypoints", "discord"]

# ---------------------------------------------------------------------
# Phase 9B-7A -- the one-shot backup capture job
# (`python -m src.deployment.entrypoints backup-capture`; docs/backup_artifact_contract_proposal.md 13.21 and
# docs/phase9b_deployment_decision_package.md). Built FROM the same `base` stage, so the job's whole dependency and
# migration closure (src/, alembic/, alembic.ini, the pinned requirements) is the one every other image uses; the api,
# streamlit and discord targets are unchanged and receive no PostgreSQL client.
#
# Adds ONLY the PostgreSQL 16 client from the official PGDG apt repository -- the accepted 9B-6G pins -- after verifying
# the PGDG signing key's fingerprint (docker/backup/fetch_pgdg_key.py); apt's own signature and checksum checks stay on.
# Runs as the non-root `napsedge` user. No credential, CA file, `.env`, test evidence or artifact enters any layer:
# they are supplied at run time as read-only mounts and environment (see the run example in
# docs/backup_artifact_contract_proposal.md 13.22). It needs no Docker socket and reads no test-transport setting.
#
# Writable locations (both must be writable mounts when the root filesystem is read-only):
#   /var/lib/napsedge-backup/scratch  TMPDIR: the dump's private 0700 work directory and the coordinator's scratch copy
#   /var/lib/napsedge-backup/store    the conventional artifact store mount (NAPSEDGE_BACKUP_STORE_DIR names it)
#   /var/lib/napsedge-backup/state    Phase 9B-8B: cycle and off-site state records (NAPSEDGE_BACKUP_STATE_DIR names it)
# Phase 9B-8B adds ONLY: Debian's `age` (the encryption tool; public recipients on this host, never the private key) and the four
# pinned boto3 packages of requirements-backup.txt (the S3-compatible off-site client). The other images receive neither.
FROM base AS backup
USER root
COPY docker/backup/fetch_pgdg_key.py /opt/napsedge-backup/fetch_pgdg_key.py
COPY requirements-backup.txt /opt/napsedge-backup/requirements-backup.txt
RUN set -eux; \
    . /etc/os-release; \
    test "$VERSION_CODENAME" = "trixie"; \
    mkdir -p /usr/share/postgresql-common/pgdg; \
    python /opt/napsedge-backup/fetch_pgdg_key.py /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc; \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt trixie-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      postgresql-client-16=16.15-1.pgdg13+2 \
      postgresql-client-common=293.pgdg13+1 \
      age=1.2.1-1+b5; \
    rm -rf /var/lib/apt/lists/*; \
    dpkg-query -W postgresql-client-16 postgresql-client-common age > /opt/napsedge-backup/dpkg-versions.txt; \
    /usr/lib/postgresql/16/bin/pg_dump --version; \
    /usr/lib/postgresql/16/bin/pg_restore --version; \
    /usr/bin/age --version; \
    python -m pip install --no-cache-dir -c /app/constraints-release-py313.txt -r /opt/napsedge-backup/requirements-backup.txt; \
    install -d -o napsedge -g napsedge -m 0700 /var/lib/napsedge-backup/scratch /var/lib/napsedge-backup/store /var/lib/napsedge-backup/state
ENV TMPDIR=/var/lib/napsedge-backup/scratch
USER napsedge
ENTRYPOINT ["python", "-m", "src.deployment.entrypoints", "backup-capture"]
