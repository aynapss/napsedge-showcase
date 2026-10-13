# Phase 9A — Deployment Contract and Container Runtime

**This is a deployment-contract implementation batch, not a production deployment.** It adds a secure, reproducible container build/run contract for the three accepted processes (FastAPI, Streamlit, Discord) without changing any domain behavior, exposing any service publicly, or claiming production readiness. No application route, service, command, page, formula, or output changed. No infrastructure was provisioned and no cloud provider was selected.

Authoritative predecessor documents: [`docs/phase9_deployment_hardening_readiness.md`](phase9_deployment_hardening_readiness.md), [`docs/release_candidate_runbook.md`](release_candidate_runbook.md).

---

## 1. Process topology

Exactly Option A from the Phase 9 readiness assessment (§5): a controlled single-host containerized internal runtime, three separate image targets sharing one dependency/base stage.

| Process | Image target | Starts by default under `docker compose up`? | Network exposure |
|---|---|---|---|
| FastAPI | `api` | **Yes** | `127.0.0.1:8000` only |
| Streamlit | `streamlit` | **Yes** | `127.0.0.1:8501` only |
| Discord bot | `discord` | **No** — requires `--profile discord` | Outbound only (no `EXPOSE`, no port mapping) |
| Local PostgreSQL rehearsal | `local-db` | **No** — requires `--profile local-db` | None (no port mapping at all — reachable only on the private Compose network) |

**Explicitly excluded from every image and from `compose.yaml`**, per this batch's own scope: the Stripe webhook, port 8787, the legacy Flask server, notebook execution, the live Odds/agent worker, a scraping scheduler, a public reverse proxy, TLS termination, Redis, and any production PostgreSQL service. Confirmed absent by both static tests (`tests/regression/test_deployment_container_contract.py`) and runtime tests (`tests/regression/test_deployment_container_runtime.py`).

---

## 2. Image targets

One shared multi-stage `Dockerfile`, base image `python:3.13-slim` (matches `.python-version`'s declared `3.13`; verified by an actual `docker build`, never silently substituted). A `base` stage installs runtime dependencies once (`pip install -c constraints-release-py313.txt -r requirements.txt` — **never** `requirements-dev.txt`), creates a non-root user (`napsedge`, UID 10001), and copies only an explicit, named allowlist of files:

```
src/, pages/, app.py, alembic/, alembic.ini, .python-version,
"Reference Py Files/net_rankings_2026.csv"   (the one ~6 KB tracked data
                                               file NAPSEDGE_NET_RANKINGS_CACHE_PATH
                                               defaults to -- not the
                                               surrounding 33 MB directory)
```

No `.git`, `.env`, `TeamDump`, `cache`, `PlayerCards`, notebook, test, scratch file, local database, or virtual environment ever enters any layer — confirmed directly inside each built image (`tests/regression/test_deployment_container_runtime.py::TestImageBuilds`), not merely asserted from the Dockerfile's own text. `COPY . .` is never used anywhere.

Three named targets (`api`, `streamlit`, `discord`) each inherit the shared base and differ only in `EXPOSE`/`ENTRYPOINT`. Build commands:

```bash
docker build --target api       -t napsedge-api:phase9a .
docker build --target streamlit -t napsedge-streamlit:phase9a .
docker build --target discord   -t napsedge-discord:phase9a .
```

All three were built successfully this batch, from this exact committed `Dockerfile`/`.dockerignore`, with zero package requiring a build-from-source fallback (every pinned dependency resolved a manylinux wheel for Python 3.13 on this base image).

---

## 3. Runtime entry points (`src/deployment/`)

A new, minimal package: `src/deployment/env_validation.py` (process-specific startup validation) and `src/deployment/entrypoints.py` (the three container `CMD`/`ENTRYPOINT` targets).

**Validation design**: each validator calls the *existing* authority for anything that already has one (`ApiKeyAuthenticator` for `NAPSEDGE_API_KEY_HASHES_JSON`, `load_rate_limit_policy_from_env` for the rate-limit pair, `load_rankings_runtime_config_from_env` for the Rankings pair) rather than re-implementing bounds/format rules. `DISCORD_BOT_TOKEN` has no existing authority object, so its check mirrors `src.discord_bot.bot.main()`'s own exact inline check. No validator ever runs at import time, during test collection, inside `create_app()`, or while AST-enumerating Discord commands — confirmed by `tests/regression/test_deployment_entrypoints.py::TestImportSafety`.

| Process | Required (fails closed, startup) | Never requires |
|---|---|---|
| API | `NAPSEDGE_API_KEY_HASHES_JSON`, `NAPSEDGE_NET_RANKINGS_SEASON` | `DISCORD_BOT_TOKEN` |
| Streamlit | `NAPSEDGE_NET_RANKINGS_SEASON` | `NAPSEDGE_API_KEY_HASHES_JSON`, `DISCORD_BOT_TOKEN` |
| Discord | `DISCORD_BOT_TOKEN` | `NAPSEDGE_API_KEY_HASHES_JSON`, rate-limit pair |

The rate-limit pair is never made newly required — both already have safe defaults; the API validator only rejects a *present-but-malformed* value, preserving the established lazy/default behavior exactly (confirmed by `test_rate_limit_absence_is_not_an_error`).

**Process startup commands** (inside each container, as `ENTRYPOINT`):

```bash
python -m src.deployment.entrypoints api                     # -> uvicorn.run("src.api.main:create_app", factory=True, host="0.0.0.0", port=8000)
python -m src.deployment.entrypoints streamlit                # -> execs into: streamlit run app.py --server.address=0.0.0.0 --server.port=8501 --server.headless=true --browser.gatherUsageStats=false
python -m src.deployment.entrypoints discord                  # -> src.discord_bot.bot.main() (the real, live bot)
python -m src.deployment.entrypoints discord --check-only      # -> validates configuration and exits; NEVER calls bot.main()
```

Every `ENTRYPOINT`/`CMD` in the Dockerfile uses exec-form (`["..."]`), confirmed by a dedicated test. The Streamlit entrypoint uses `os.execvp` (true process replacement, so OS signals reach the real `streamlit` process directly); the API entrypoint calls `uvicorn.run(...)` in-process (uvicorn's own designed invocation pattern, with its own graceful-shutdown signal handling); the Discord entrypoint calls the existing, unmodified `src.discord_bot.bot.main()` directly, which already blocks on `client.run()`.

---

## 4. Discord safety contract

| Requirement | Evidence |
|---|---|
| Never starts during image build | `Dockerfile`'s `discord` target has no `RUN`/`CMD` that executes the entrypoint; only `ENTRYPOINT` is declared, which never runs at build time |
| Never connects during tests | `tests/regression/test_deployment_entrypoints.py::TestRunDiscord` monkeypatches `src.discord_bot.bot.main` to raise if called in `check_only=True` mode — confirmed it is never called |
| Never connects during Compose configuration validation | `docker compose config` never executes any container |
| Never starts in the default Compose profile | `compose.yaml`'s `discord` service declares `profiles: ["discord"]`; confirmed by `TestComposeDefaultProfile::test_default_up_starts_only_api_and_streamlit` — the running service set after plain `docker compose up` is exactly `{api, streamlit}` |
| Requires an explicit `discord` profile | `docker compose --profile discord run --rm discord --check-only`, confirmed working this batch |
| Preserves the permanent `client.run` main-guard test | `tests/regression/test_discord_bot_commands.py::test_client_run_is_reachable_only_from_main_never_at_module_level` — unmodified, still passing; this batch's own entrypoint calls `bot.main()`, never `client.run()` directly, so the guard's own invariant is unaffected |
| Fails before connection if configuration unavailable | `run_discord` calls `validate_discord_environment()` before importing `src.discord_bot.bot` at all; confirmed: an unset token exits 1 with a configuration error, never reaching `main()` |
| Keeps all 15 existing commands unchanged | Zero lines of `src/discord_bot/bot.py` were touched by this batch; reconfirmed via AST enumeration (§7) |
| A safe configuration-check mode exists | `python -m src.deployment.entrypoints discord --check-only` — validates and exits 0/1, never imports `src.discord_bot.bot` in the `check_only=True` branch (confirmed by its own dedicated import-safety test) |

**Live verification performed this batch** (synthetic token, no real Discord credential used): `docker run --rm -e DISCORD_BOT_TOKEN=synthetic-fake-token-not-real napsedge-discord:phase9a --check-only` printed `discord configuration check: OK (no connection attempted)` and exited 0; the same command with no token set exited 1 with `configuration error: DISCORD_BOT_TOKEN is not set.`. Neither invocation produced any outbound network connection.

---

## 5. Compose contract

`compose.yaml`, default services `api`/`streamlit`; optional profiles `local-db` (PostgreSQL rehearsal only) and `discord` (explicit opt-in).

- **API**: `uvicorn` via the accepted factory command, single worker (no horizontal scaling introduced), health check against `/v1/health` (via `python -c "import urllib.request; ..."`, avoiding an extra `curl` install), host port `127.0.0.1:8000` only.
- **Streamlit**: the tracked `app.py` entry point, `--server.headless=true` (disables the local-browser auto-launch Streamlit would otherwise attempt), health check against Streamlit's own supported `/_stcore/health` endpoint, host port `127.0.0.1:8501` only.
- **`local-db` profile**: `postgres:16-alpine` (major version pinned), a named volume (`napsedge-local-db-data`), a `pg_isready` health check, **no host port mapping at all** (reachable only on the private `napsedge-internal` Compose network), explicitly labeled non-production in its own comment block. **No backup/restore, TLS, least-privilege role separation, or secrets rotation is solved or claimed by this profile** — it exists solely for local container rehearsal.
- **`discord` profile**: receives only Discord-owned secrets (`DISCORD_BOT_TOKEN`, `DISCORD_ADMIN_USER_IDS`) plus the genuinely shared Rankings/database variables the 3 Phase 7 commands use — never `NAPSEDGE_API_KEY_HASHES_JSON` or the rate-limit pair, and never any Odds/Stripe credential.

**A real bug found and fixed during this batch's own validation**: a Compose `${VAR:?error}` hard-required-variable declaration on `local-db`'s `POSTGRES_PASSWORD` broke plain `docker compose up` entirely — Compose interpolates every service's environment during config resolution regardless of which profile is actually active, so a required-with-no-default variable on a profile-gated service blocks every other service too. Confirmed by an actual `docker compose up` failure (`error while interpolating services.local-db.environment.POSTGRES_PASSWORD: required variable ... is missing a value`). **Fixed** by changing to an empty default (`${NAPSEDGE_LOCAL_DB_PASSWORD:-}`) and relying on a second, independently-confirmed safety net instead: the official `postgres` image itself refuses to start with an empty `POSTGRES_PASSWORD` (confirmed directly — `Error: Database is uninitialized and superuser password is not specified`), and `POSTGRES_HOST_AUTH_METHOD=trust` (the one escape hatch that would bypass this) is never set anywhere in this file.

---

## 6. Configuration template

`.env.example` (new, committed) — every variable each of the three processes actually reads (confirmed by direct `os.environ`/`os.getenv` source inspection, not inferred from documentation), grouped by owning process, each with a safe placeholder, required/optional status, and a one-line purpose. Contains no usable secret — every secret-shaped line uses an obvious `REPLACE_...` placeholder, confirmed by a dedicated test. Explicitly lists the four variables belonging to excluded processes (`STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_WEBHOOK_PORT`, `ODDS_API_KEY`) as **not** part of this Phase 9A contract, so their absence is deliberate, not merely an oversight.

---

## 7. Validation evidence

**Static/unit tests** (no Docker required, always run; exact counts from `pytest --collect-only`, not estimated): 17 in `test_deployment_env_validation.py`, 14 in `test_deployment_entrypoints.py`, 44 in `test_deployment_container_contract.py` — **75 total, all passing**.

**Container-runtime tests** (`test_deployment_container_runtime.py`, module-skipped as a tool/platform skip if Docker is unavailable — never a failure): 20 tests, all passing this batch (**95 new tests overall**), covering: all three targets build; every image runs as non-root; no `.env`/`.git`/local-data/notebook/`requirements-dev.txt` in any image layer; the Rankings CSV is present without the surrounding directory; the API container's `/v1/health` returns 200 and `/v1/ready` **accurately** returns 503 against a freshly-created, unmigrated database (flipping to 200 only after `alembic upgrade head` was run inside the container — verified live, not assumed); a protected route rejects a missing credential with 401; the Streamlit container's `/_stcore/health` returns 200; both host ports are confirmed loopback-only via `docker port`; the Discord image's `--check-only` mode exits 0 without connecting and exits 1 when the token is missing, with zero lingering containers afterward; `docker compose config` parses with synthetic values; plain `docker compose up` starts exactly `{api, streamlit}`; the `discord` profile's check-only mode works via `docker compose run`.

**Complete non-live suite, exact reconciliation** (run in this machine's own local development environment, which has `TeamDump`/`cache` data present, unlike the sterile Phase 8 audit's clean checkout): **5,209 passed, 1 skipped** (5,210 collected). The last collected-count baseline for this exact branch tip (`037c7ca`, before Phase 9A) is the Phase 8 audit's own sterile-checkout figure, **5,115 collected** — identical regardless of local-data presence, since collection count depends only on which test functions exist, never on whether they pass or skip. `5,210 - 5,115 = 95`, exactly matching this batch's own 95 new test nodes, with zero unexplained difference. The single skip (the pre-existing, already-documented Windows symlink-permission limitation) is unchanged from this machine's own established baseline; no new skip category was introduced. **Zero failures.**

**Live validation performed directly this batch** (beyond what the permanent test suite re-runs): manual `alembic upgrade head` inside a running API container, confirmed `/v1/ready` flip from 503→200; manual `docker compose --profile local-db up` with an empty password, confirmed Postgres's own refusal; manual `docker compose --profile local-db up` with a properly generated ephemeral password (`secrets.token_urlsafe(24)`, held only in a shell variable, never written to any file), confirmed the service reaches `health: starting`/healthy state with no host port published, then removed (container, network, and named volume all deleted).

**Classification of tool/platform skips**: none occurred in this environment — Docker and Compose were both available throughout. The skip path (`pytest.mark.skipif` keyed to `docker info`'s own exit code) exists so this suite degrades gracefully, never falsely, in an environment without Docker.

---

## 8. Manifest handling

Two new module-level public functions/classes/constants were added: `DeploymentConfigurationError`, `validate_api_environment`, `validate_streamlit_environment`, `validate_discord_environment`, `DISCORD_BOT_TOKEN_ENV_VAR` (`src/deployment/env_validation.py`), and `run_api`, `run_streamlit`, `run_discord`, `build_parser`, `main` (`src/deployment/entrypoints.py`).

**Collision check performed**: of these 10 names, exactly **one** — `main` — already exists as a manifest row (`src/run_pipeline.py;src/discord_bot/bot.py;src/discord_bot/grant_tier.py;src/pipeline/orchestrator.py`, status `src_only (new/refactored, no notebook match) | DUP_ACROSS_SRC_FILES`). This is the manifest's own established, pre-existing pattern for "the same bare name, independently defined in multiple `src/` files, no notebook origin" — not a new kind of entry. **Both manifests' `main` row was updated** to add `src/deployment/entrypoints.py` to its file list; no other field changed, since the existing classification (`src_only, no notebook match, duplicated across files`) remains accurate. Both manifests remain at **1,157 physical lines / 1,156 data rows**, identical key sets, **zero duplicate keys** — confirmed by direct re-parse after the edit, using the established safe-write pattern (`csv.reader`/`csv.writer`, write-to-temp-then-move, row-count verified before replacing the original).

The remaining 9 symbols (`DeploymentConfigurationError`, `validate_api_environment`, `validate_streamlit_environment`, `validate_discord_environment`, `DISCORD_BOT_TOKEN_ENV_VAR`, `run_api`, `run_streamlit`, `run_discord`, `build_parser`) have **no manifest row of any kind** — consistent with this project's own established, repeatedly-confirmed convention (used throughout Phase 4C5/4C5R) that both manifests are keyed by **real notebook function name**, not by every new Python symbol a batch introduces; a symbol with zero notebook origin (not a port, not a refactor of any notebook cell) was never given its own row anywhere in this project's history, and is not given one here.

---

## 9. Loopback-only limitation (disclosed explicitly)

**Binding a container's host port to `127.0.0.1` is a network-reachability boundary, not authentication.** It prevents the API/Streamlit services from being reached by anything other than the host machine itself; it does **not** satisfy Phase 9C's own Streamlit access-control requirement (`docs/phase9_deployment_hardening_readiness.md` §6), which remains fully open. Any process on the same host (or anyone with shell access to it) can still reach both services unauthenticated.

---

## 10. Local PostgreSQL profile limitations (disclosed explicitly)

The `local-db` Compose profile is for **local/internal container rehearsal only**. It does not solve, and does not claim to solve, any of the production PostgreSQL gaps named in `docs/phase9_deployment_hardening_readiness.md` §8: no backup schedule, no retention policy, no restore test beyond this batch's own one-off manual rehearsal, no TLS/SSL configuration, no least-privilege role separation (the single `napsedge_local` role is both the connection role and, implicitly, a superuser-equivalent for this disposable instance), and no secrets-rotation mechanism. It must never be pointed at by `NAPSEDGE_DATABASE_URL` in any deployment beyond this same local rehearsal.

---

## 11. Remaining Phase 9B–9F gaps (unchanged, reconfirmed)

Phase 9A does not close, and was never scoped to close: production PostgreSQL operations (9B), Streamlit access control (9C — the loopback binding above is explicitly not a substitute), shared rate limiting for multi-worker/multi-host deployment (9D), observability/CI-CD (9E), or a final security/deployment acceptance gate (9F). The deployment-tier classification in `docs/phase9_deployment_hardening_readiness.md` §4 (Tier B: **CONDITIONAL**) is **unchanged** by this batch — a reproducible container contract is one of several prerequisites, not the whole of what Tier B's own six-item decision rule requires.

---

## 12. Rollback procedure

- **Images**: `docker rmi napsedge-api:phase9a napsedge-streamlit:phase9a napsedge-discord:phase9a` — removes the built artifacts; no state is lost, since none of these images holds any persistent data of their own.
- **Containers/networks**: `docker compose down --remove-orphans` — stops and removes every container and the private network this contract creates.
- **The `local-db` profile's volume**: `docker compose --profile local-db down -v` — additionally removes the named volume; safe, since this profile is explicitly non-production and holds no data this project depends on.
- **Source**: this entire batch is additive — `Dockerfile`, `.dockerignore`, `compose.yaml`, `.env.example`, `src/deployment/`, and the new test files can each be deleted independently with zero effect on any existing route, service, command, page, or test, since nothing outside this new surface imports anything from `src/deployment/`.
- **Git**: revert the three Phase 9A commits (`git revert`) or simply do not merge the branch; no migration, schema, or data change is involved anywhere in this batch.

---

## 13. This is not a production deployment

Nothing in this document, in `compose.yaml`, or in any image built this batch constitutes a production deployment or a claim of production readiness. No infrastructure was provisioned, no cloud provider was selected or contacted, and the deployment-tier classification remains **CONDITIONAL** pending Phase 9B–9F.


---

## 14. Correction note (Phase 9B/9C remediation, 2026-10-02)

An end-to-end rehearsal of these containers against a real Postgres, with authenticated data requests and a browser, found that several statements above were wrong. Sections 1-13 are left as originally written; this section supersedes them where they conflict.

| Statement above | Correction |
|---|---|
| "No ... notebook ... ever enters any layer" (section 2); the image-content tests asserting no notebooks | **Exactly one notebook is now copied**: `cbb_teamScrape_v15_TEST.ipynb`. `src.ingestion.load_data.load_conferences()` reads it as data (`json.loads` + `ast.literal_eval`, never executed) to build the team identity registry that every team-resolving route, command and page depends on. Without it every such request failed with `ConfigurationError`. No other notebook enters any image. |
| Copy allowlist in section 2 | **One additional file is copied**: `docs/sr_slug_reconciliation.json`, read on every NET-ranking identity resolution. Without it the Rankings route returned 500. No other file under `docs/` is copied. |
| "Copy only committed runtime files" | True only for a clean checkout. `COPY pages/` and `COPY src/` copy whatever the build context holds, so an image built from a dirty working tree shipped the untracked pages 01-09 (page 09 triggers the live Odds/agent pipeline) and uncommitted local edits to `src/ovr_model.py` and `src/scraper.py`. `.dockerignore` now excludes `pages/0*.py`. Uncommitted edits to tracked `src/` files cannot be excluded this way, so **images must be built from a clean checkout**. |
| Section 7 validation evidence | Container validation exercised health, readiness, auth rejection and image contents, but **no authenticated data route**, which is why the two missing data inputs went unnoticed. The runtime tests now also load the team registry and the reconciliation record inside every built image. |
| Section 3 / 5 (process and compose contract) | `compose.yaml` gained an optional, streamlit-only `STREAMLIT_SERVER_CORS_ALLOWED_ORIGINS` pass-through (empty by default, no hostname committed). `app.py` now renders links only for pages that exist, so the deployed landing page no longer raises `StreamlitPageNotFoundError`. See `docs/phase9_deployment_hardening_readiness.md` section 21. |

**Test counts after remediation** (exact, from `pytest --collect-only`): `test_deployment_env_validation.py` 17, `test_deployment_entrypoints.py` 14, `test_deployment_container_contract.py` 59, `test_deployment_container_runtime.py` 24 = **114 deployment tests** (the 95 reported in section 7 plus the remediation's additions), plus 13 in `test_streamlit_landing_page.py`.


---

## 15. Scope note (Phase 9B-1, 2026-10-02)

The "Phase 9B/9C remediation" that produced the correction in section 14 was a narrow fix of this document's own container contract. It did not deliver the operational Phase 9B slice (production PostgreSQL contract, backup and restore, secrets) or the Phase 9C slice (Streamlit access control) named in `docs/phase9_deployment_hardening_readiness.md` section 15. Phase 9B-1 (least-privilege roles and a synthetic restore drill, disposable PostgreSQL only) is the first part of 9B and does not complete it -- see that document's section 22.

> **Note (Phase 9B-2, 2026-10-02):** the DSN requirement `sslmode=verify-full` with an explicit CA file is now exercised end to end against a synthetic-certificate test server (`docs/phase9_deployment_hardening_readiness.md` section 23). Two operator-facing cautions from that work: use a DSN with no percent-encoding in `NAPSEDGE_DATABASE_URL` (`alembic/env.py` cannot accept `%`), and set `verify-full` explicitly (`require` with a root certificate is `verify-ca`, which does not check the host name). No production certificate, instance or secret is configured by this note.

> **Note (Phase 9B-3, 2026-10-02):** the percent-encoding caution in the 9B-2 note above is superseded for `NAPSEDGE_DATABASE_URL`: `alembic/env.py` now accepts a valid percent-encoded URL and passes it to the driver unchanged (`docs/phase9_deployment_hardening_readiness.md` section 24). The advice to set `sslmode=verify-full` explicitly stands. No production certificate, instance or secret is configured by this note.
