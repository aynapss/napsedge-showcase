# NapsEdge: NCAA basketball analytics platform (engineering showcase)

NapsEdge collects NCAA men's basketball data, models matchups and serves the results through an API, a Streamlit app and a Discord bot. This repository is a **curated, view-only excerpt** of the private project. It shows the production engineering. The modelling, projection and betting-analysis components are proprietary and are not included (see [What is not here](#what-is-not-here)).

**Copyright (c) 2026 Anthony Napoli. All rights reserved.** Published for viewing only; see [NOTICE](NOTICE).

## Architecture

```
            SSH tunnel only (no public ingress)
                         │
        ┌────────────────┼─────────────────┐
        ▼                ▼                 ▼
   FastAPI service   Streamlit app    Discord worker
   (Bearer keys,     (private only)   (trusted guild)
   rate limiting)
        │                │                 │
        └────────► PostgreSQL 16 ◄─────────┘
                         │
          nightly logical backup, age-encrypted,
          copied off-site to Backblaze B2, restore-drilled
```

- **Python 3.13**, FastAPI, Streamlit, discord.py, SQLAlchemy 2 and Alembic, PostgreSQL 16, Redis (optional), Docker and Compose, systemd, GitHub Actions.
- One container image per process from a pinned base; every dependency is pinned exactly in [`constraints-release-py313.txt`](constraints-release-py313.txt).

## Engineering highlights

| Area | What it does | Where |
|---|---|---|
| API | Versioned read-only routes; sanitized errors that never leak internals; health and readiness endpoints | [`src/api/main.py`](src/api/main.py), [`src/api/routes/`](src/api/routes/) |
| Authentication | Bearer API keys stored only as SHA-256 digests and compared in constant time | [`src/auth/api_keys.py`](src/auth/api_keys.py) |
| Shared rate limiting | A fixed-window limiter shared across workers through one atomic Redis Lua script; fails **closed** on an outage | [`src/auth/rate_limit_redis.py`](src/auth/rate_limit_redis.py), [`src/auth/rate_limit_backend.py`](src/auth/rate_limit_backend.py) |
| Secrets | Read from read-only credential files (systemd credentials); URLs may not carry passwords; every secret read is registered for log redaction | [`src/secret_files.py`](src/secret_files.py) |
| Observability | Structured JSON logs, a request-correlation ID per request, a redaction filter, a data-freshness check | [`src/logging_config.py`](src/logging_config.py), [`src/api/request_context.py`](src/api/request_context.py), [`src/services/freshness.py`](src/services/freshness.py) |
| Backups | Consistent `pg_dump` snapshots under an advisory lock; a manifest-verified artifact format; `age` encryption; off-site copies with read-back verification; grandfather-father-son retention; restore drills that compare row-level evidence | [`src/deployment/`](src/deployment/), [`docs/backup_artifact_contract_proposal.md`](docs/backup_artifact_contract_proposal.md), [`docs/backup_operations_runbook.md`](docs/backup_operations_runbook.md) |
| Deployment | Startup validation that fails fast on bad configuration; systemd units and timers; an on-host check that the private services listen on loopback only | [`src/deployment/env_validation.py`](src/deployment/env_validation.py), [`deploy/`](deploy/), [`deploy/app/check-private-exposure.sh`](deploy/app/check-private-exposure.sh) |
| Database | SQLAlchemy models, repositories with idempotent upserts, Alembic migrations, least-privilege roles | [`src/db/`](src/db/), [`alembic/`](alembic/), [`docs/database_schema.md`](docs/database_schema.md) |
| Readiness | A threat assessment, a dependency-ordered hardening roadmap and an evidence-based go/no-go record | [`docs/phase9_deployment_hardening_readiness.md`](docs/phase9_deployment_hardening_readiness.md), [`docs/phase9f_go_no_go.md`](docs/phase9f_go_no_go.md) |

## Tests

```bash
python -m pip install -c constraints-release-py313.txt -r requirements-dev.txt -r requirements-backup.txt
python -m pytest -q
```

The default suite is self-contained (no network, no database). The PostgreSQL and Redis suites (`-m postgres`, `-m redis`) start disposable containers through Docker. CI runs the default suite and a `pip-audit` dependency scan on every push.

## What is not here

These stay in the private repository:
- the research notebooks;
- matchup feature engineering, the projection model and market-edge evaluation ([`src/features/matchup_features.py`](src/features/matchup_features.py) is a labelled stand-in that raises `NotImplementedError`, so the matchup comparison endpoint returns an error here);
- player ratings, simulation, staking, calibration and back-testing;
- the LLM research agents and the legacy Discord commands;
- raw data, caches and team logos.

Because of this, the container images and the PostgreSQL suite are reading material here: they expect the private notebook and data files.
