# Phase 9 Deployment Hardening Readiness Assessment

**This is a planning and evidence-gathering batch only.** No deployment occurred, no paid resource was provisioned, no cloud account was created, no DNS was configured, no repository secret was modified, no production database was built, and no application behavior changed. `.env` was not opened. No cloud platform was contacted.

Authoritative predecessor documents (all re-read this batch): [`docs/phase6_final_acceptance.md`](phase6_final_acceptance.md), [`docs/phase6_final_authority_delta_audit.md`](phase6_final_authority_delta_audit.md), [`docs/phase7_final_acceptance.md`](phase7_final_acceptance.md), [`docs/phase7_final_authority_delta_audit.md`](phase7_final_authority_delta_audit.md), [`docs/phase8_final_release_delta_audit.md`](phase8_final_release_delta_audit.md), [`docs/phase8_remote_publication_readiness.md`](phase8_remote_publication_readiness.md), [`docs/release_candidate_runbook.md`](release_candidate_runbook.md), [`docs/database_schema.md`](database_schema.md), [`docs/migration_state.md`](migration_state.md), `requirements.txt`, `requirements-dev.txt`, `constraints-release-py313.txt`, `.python-version`, `alembic.ini`.

Source inspected directly this batch: `src/api/main.py`, `src/api/deps.py`, `src/api/errors.py`, `src/auth/api_keys.py`, `src/auth/rate_limit.py`, `src/db/config.py`, `src/db/session.py`, `src/discord_bot/bot.py`, `src/discord_bot/stripe_webhook.py`, `src/streamlit_consumer_controller.py`, `src/streamlit_consumer_renderer.py`, `src/logging_config.py`, `app.py`, tracked pages 10–12.

---

## 1. Executive recommendation

> **RECOMMENDED FIRST TARGET: TIER B — CONTROLLED SINGLE-INSTANCE INTERNAL DEPLOYMENT. CONFIRMED, NOT REJECTED, BUT CLASSIFIED `CONDITIONAL`, NOT `GO`, UNTIL SIX NAMED PREREQUISITES (ACCESS BOUNDARY, SECRETS HANDLING, POSTGRESQL OPERATIONS, BACKUP/RESTORE, MONITORING MINIMUMS, ROLLBACK) EACH HAVE A CONCRETE, DOCUMENTED ANSWER — PER THIS AUDIT'S OWN DECISION RULE (§19).**

The expected recommendation named in the approval's own §4 — controlled single-instance internal deployment as the smallest tier creating real operational value — is **confirmed by source evidence**, not rejected. Every one of the five accepted Phase 6/7 domains already runs correctly as a single, trusted, same-host process group throughout this project's own test history; nothing about the source requires a larger topology to deliver real value to a small number of trusted users. However, **none of the six prerequisite decisions this audit's own §19 requires has yet been made** — this repository today has zero Dockerfile, zero Compose file, zero CI workflow, zero `.env.example`, no access-control mechanism for Streamlit, no managed PostgreSQL contract, and no backup/restore procedure of any kind. Declaring Tier B a `GO` today would be exactly the "vague production-ready language" this audit is instructed to avoid. Tier B is therefore **CONDITIONAL**, with Phase 9A–9C (§15) named as the dependency-ordered path to resolving it.

---

## 2. Current deployment posture (unchanged facts, reconfirmed)

| Fact | Status |
|---|---|
| Local development | **GO** |
| Single-process trusted internal use | **GO, subject to documented configuration** (`docs/release_candidate_runbook.md` §1) |
| Multi-worker deployment | **NO-GO** — in-memory, single-process rate limiter (`src/auth/rate_limit.py`) |
| Public/multi-tenant deployment | **NO-GO** — no Streamlit access control, no production PostgreSQL, no secrets rotation |
| Production-profitability claims | **NO-GO** — no walk-forward or historical-profitability validation exists anywhere |
| Containerization | **None exists.** Confirmed by direct filesystem search this batch: zero `Dockerfile*`, zero `docker-compose*`/`compose*.yml`, no `.dockerignore`. |
| CI/CD | **None exists.** No `.github/` directory of any kind — zero workflows, zero branch protection, zero automated test gate. |
| Environment template | **None exists.** No `.env.example`, no `Procfile`, no hosting-platform config file of any kind. |
| Streamlit server config | `.streamlit/config.toml` exists but is **empty** (0 bytes, untracked) — no CORS, XSRF, or address-binding configuration of any kind is set anywhere. |

---

## 3. Process inventory

| Process | Start command | Required env vars | Network access | DB access | Filesystem access | Health check | Shutdown | Accepted scope | Must run independently | Horizontal scaling | Accidental-external-contact risk |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **FastAPI application** | `uvicorn src.api.main:create_app --factory --host 127.0.0.1 --port 8000` | `NAPSEDGE_DATABASE_URL` (optional, SQLite fallback), `NAPSEDGE_API_KEY_HASHES_JSON` (required for protected routes), `NAPSEDGE_RATE_LIMIT_REQUESTS`/`_WINDOW_SECONDS` (optional), `NAPSEDGE_NET_RANKINGS_SEASON` (required for Rankings), `NAPSEDGE_NET_RANKINGS_CACHE_PATH` (optional), `NAPSEDGE_DATABASE_ECHO` (optional), `NAPSEDGE_LOG_LEVEL` (optional) | Inbound HTTP only; zero outbound calls to any external service (confirmed — no `requests`/`httpx` client call exists anywhere in `src/api/`, `src/services/`, or `src/consumers/`) | Yes — one engine/session factory per process, built once in `create_app()` | Reads `Reference Py Files/net_rankings_2026.csv` (Rankings), `TeamDump/` (Injury) | `/v1/health` (liveness, no dependency check), `/v1/ready` (checks DB connectivity + Alembic revision) | Graceful — ASGI server's own SIGTERM handling; no custom shutdown hook | Yes (Team/Rankings/Injury/Player/Matchup) | Yes | No — `multi_requests_requests` counter and rate-limit state are process-local | None — pure HTTP, no reason it couldn't scale if the rate-limit/session concerns (§7) were solved |
| **Streamlit application** | `streamlit run app.py` | Same `NAPSEDGE_DATABASE_URL`/`NAPSEDGE_NET_RANKINGS_*` as the API (pages 10/11 only; page 12 is session-free) | Inbound HTTP (default port 8501) only; zero outbound external calls from any tracked page (10/11/12) | Yes — pages 10/11 open/close a session per interaction via the same `src.db.session` factory | Reads the same local Rankings CSV / `TeamDump/` cache as the API | **None** — Streamlit has no built-in `/health` endpoint distinct from the app itself responding; no custom readiness route exists | Streamlit's own process-level handling; no custom shutdown hook | Yes (pages 10, 11, 12) | Yes | **No** — Streamlit's own session/script-rerun model is single-process by design; a second instance would not share any per-viewer UI state (this is cosmetic, not a data-integrity concern, since it never holds the authoritative state) | **Yes, significant** — `app.py`'s own landing page links to 9 untracked pages (01–09); page 09 ("AI Research Control Room") and page 04 ("Player Cards") each reach the deferred live-Odds/agent pipeline and the notebook-`exec()` card engine respectively if clicked — accidental exposure of this process publicly would expose those triggers too, even though they are outside the accepted Phase 6/7 scope |
| **Discord bot** | `python -m src.discord_bot.bot` | `DISCORD_BOT_TOKEN` (required, process raises before connecting if unset), `DISCORD_ADMIN_USER_IDS` (optional), plus the API's own `NAPSEDGE_DATABASE_URL`/`NAPSEDGE_NET_RANKINGS_*` (the 3 Phase 7 commands call `src.services.*` directly) | Outbound only, to Discord's gateway (`discord.py`'s own WebSocket client) — confirmed no inbound listener | Yes — same direct `src.services.*` calls as Streamlit, for `/team_lookup`, `/net_ranking`, `/injuries` | Reads the same local caches | **None** — no liveness/readiness probe of any kind exists for this process | `client.run()` blocks until the process is killed; no graceful-shutdown hook beyond `discord.py`'s own signal handling | Partially — 3 of 15 commands are accepted; 12 pre-existing commands (`/odds`, `/research`, `/report`, `/matchup`, `/player_card`, etc.) are **not** | Yes — a long-lived WebSocket connection | **No** — a single `discord.py` client per bot token; running two instances of the same token is explicitly unsupported by Discord's own gateway protocol | **Yes, by design for the 12 legacy commands** — confirmed this session (Phase 7 delta audit) that `/odds`/`/research`/`/report`/`/matchup` invoke a live, third-party-calling pipeline (`run_matchup_pipeline`, Odds API, optional LLM agents) completely outside the accepted descriptive scope; starting this process at all makes those commands reachable by anyone with guild access |
| **Stripe webhook (Flask)** | `python -m src.discord_bot.stripe_webhook` (binds `0.0.0.0:8787` by default) | `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` (both required for correct signature verification; the process does not crash at import without them, only fails closed on first real webhook call), `STRIPE_WEBHOOK_PORT` (optional) | **Inbound, bound to all interfaces by default** (`host="0.0.0.0"`) — the one process in this inventory that listens on every network interface unless a reverse proxy or firewall restricts it | No — writes only to the same JSON-file entitlements store the Discord bot reads | Writes `TeamDump/memory/discord_entitlements.json` | **None** | Flask's own development-server default; not WSGI-production-hardened (`app.run()`, not `gunicorn`/`uwsgi`) | **No** — pre-existing, entirely separate from the accepted Phase 6/7 consumer scope; not touched by any Phase 7 slice | Yes — a distinct process from the bot itself | Not assessed — no accepted deployment scope includes it yet | **Yes** — `0.0.0.0` binding plus Flask's own non-production dev server is the single highest-risk item in this entire process inventory if ever started outside a fully firewalled/reverse-proxied boundary |
| **Alembic migration runner** | `python -m alembic upgrade head` | `NAPSEDGE_DATABASE_URL` | Only to the configured database | Yes, DDL-level | None | N/A — a one-shot command, not a long-running process | Exits after completion | Yes | Run once per deploy, not continuously | N/A | No — never contacts anything beyond the configured database |
| **PostgreSQL** | Not started by any `src/` code — an external dependency | `NAPSEDGE_DATABASE_URL` (consumer-side) | Listens for DB connections | N/A (is the database) | Its own data directory | Platform/image-dependent | Platform/image-dependent | Yes (Phase 5) | Yes | Yes, via standard PostgreSQL replication/pooling — outside this project's own code | No |
| **Cache-refresh / scheduled process** | **Does not exist.** No cron, systemd timer, Celery beat, or background worker of any kind was found anywhere in `src/` | N/A | N/A | N/A | N/A | N/A | N/A | **No** — Rankings/Injury caches are manually replaced files, confirmed unchanged since Phase 6 | N/A | N/A | N/A |
| **Live-odds / scraping / agent process** | `_run_matchup_research` → `run_matchup_pipeline` (`src.pipeline.orchestrator`), reachable only from the 4 legacy Discord commands or Streamlit page 09 | Odds-API credentials (not inventoried here — outside accepted scope), plus whatever the live-agent pipeline itself requires | Outbound, live, to a third-party Odds API and potentially an LLM provider | Not via `src.db` — reads/writes its own separate cache/output files | Writes to its own output directories | None | Whatever the pipeline's own process model does | **No — explicitly out of scope**, confirmed `LEGACY_SURFACE_OUTSIDE_ACCEPTED_SCOPE` by the Phase 7 delta audit | N/A | N/A | **Yes — this is the process class this entire deployment-hardening effort must not accidentally expose** |

**Process-separation conclusion**: the Discord bot, FastAPI application, and Streamlit application must remain three separate deployable processes — confirmed by direct source evidence, not merely convention. No shared in-process state exists between them (each builds its own engine/session factory and its own rate limiter, if any); the only shared *external* state is the PostgreSQL database and the local Rankings/Injury cache files on disk.

---

## 4. Deployment-tier matrix

| Tier | Classification | Exact prerequisites |
|---|---|---|
| **A. Local development** | **GO** | None — already the demonstrated, continuously-exercised state throughout this project's history. |
| **B. Controlled single-instance internal deployment** | **CONDITIONAL** (confirmed as the correct first target, not yet satisfied) | A concrete access boundary for Streamlit (§6); a defined secrets-injection mechanism (§9); a defined PostgreSQL operational contract — SSL, roles, backup/restore (§8); defined monitoring minimums (§10); a defined rollback procedure (§11, §15 Phase 9A/9F). None of these six exist today. |
| **C. Controlled multi-worker or multi-host deployment** | **NOT ASSESSED** | Depends on Tier B being satisfied first, plus a shared rate-limit backend (§7) and a load-balancer/proxy-trust decision — out of this audit's own scope to design in detail; named only as Phase 9D. |
| **D. Public/multi-tenant production deployment** | **NO-GO** | Everything in Tier C, plus an identity provider, production-grade secrets rotation, a formal security assessment, and (separately, for any profitability/betting-performance claim) walk-forward historical validation — none of which this audit authorizes or designs. |

This **confirms**, rather than rejects, the approval's own expected recommendation: Tier B is the smallest tier that creates real operational value (a small number of trusted users — e.g. the project owner plus a few friends in a private Discord server, exactly the "controlled internal application" posture named in `docs/phase8_readiness_assessment.md` §9) without pretending Tier D's gaps are solved.

---

## 5. Topology decision

### Option A — Single-host containerized internal deployment

- **Benefits**: matches the process-separation finding (§3) exactly — one container per process (API, Streamlit, Discord worker), a reverse proxy in front of the two HTTP-facing ones, a dedicated or managed PostgreSQL instance. Lowest new-infrastructure surface area of the three options; every process already runs correctly as a single instance today.
- **Risks**: process-local rate limiting (§7) and Streamlit's own lack of access control (§6) must still be solved even at this smallest scale, since "single-host" does not by itself imply "trusted network" — a container exposed on a public IP without a reverse-proxy access boundary is exactly as exposed as a bare-metal process would be.
- **Complexity**: Low-to-medium — requires writing Dockerfiles (none exist today) and a Compose file, but no new distributed-systems concern.
- **Cost class**: Low — one host, one managed-or-self-hosted PostgreSQL instance, no load balancer required beyond a single reverse proxy.
- **Operational burden**: Low-to-medium — one host to patch/monitor; backups are a single PostgreSQL instance's concern, not a fleet's.
- **Required source changes**: None to `src/` logic; new Dockerfiles, a Compose file, and an environment-variable injection mechanism (secrets manager or platform-native env-var store) are additive, outside `src/`.
- **Required infrastructure**: One host (VM or single-node container platform), one PostgreSQL instance (managed or self-hosted on the same host), a reverse proxy (e.g. Caddy/nginx) for TLS termination and the Streamlit access boundary (§6).
- **Rollback strategy**: Redeploy the previous container image tag; database rollback via Alembic's own `downgrade` path, exercised only if a migration is involved.
- **Suitability for the current project**: **High** — this is the topology that directly satisfies Tier B without introducing anything Tier C/D would require.

### Option B — Managed container platform

- **Benefits**: separately deployed API/Streamlit/Discord-worker services each scale and restart independently; a managed PostgreSQL offering typically includes backup/restore and TLS by default, closing several §8 gaps "for free"; centralized secrets and shared logs are usually first-class platform features.
- **Risks**: the in-memory rate limiter (§7) and Streamlit's lack of auth (§6) are **not** solved merely by moving to a managed platform — both still require the same application-level or gateway-level decisions as Option A. A managed platform also introduces a new dependency (the platform's own availability and pricing model) this project has never evaluated.
- **Complexity**: Medium — more moving parts than Option A (service definitions, platform-specific secrets integration, possibly a platform-specific health-check contract), but each individual piece is typically well-documented by the platform.
- **Cost class**: Medium — managed PostgreSQL and multiple always-on service instances generally cost more than one self-managed host, though exact pricing depends on the (not-yet-chosen) platform.
- **Operational burden**: Lower *per-process* than Option A once set up (the platform handles process supervision, restarts, and often TLS), but higher *initial* setup burden to learn and configure the platform correctly.
- **Required source changes**: None to `src/` logic; requires a platform-specific deployment manifest in addition to whatever Dockerfiles Option A would also need.
- **Required infrastructure**: A managed container-hosting account, a managed PostgreSQL instance, platform-native secrets storage.
- **Rollback strategy**: Most managed platforms provide built-in revision rollback; still requires the same Alembic `downgrade` consideration for database changes.
- **Suitability for the current project**: **Medium** — appropriate once Tier B's prerequisites are already solved and the project wants to move toward Tier C; premature as the *first* target, since it solves scaling concerns this project does not yet have while not automatically solving the two concerns (rate limiting, Streamlit auth) that actually block Tier B today.

### Option C — Public multi-tenant architecture

- **Benefits**: the only option that actually reaches Tier D.
- **Risks**: every NO-GO finding in §2/§13 must be closed first — an identity provider, shared rate limiting, a production PostgreSQL instance with a real backup/restore/rotation story, public TLS ingress, monitoring, incident response, and a security assessment are each a substantial, independent body of work this audit does not design.
- **Complexity**: High.
- **Cost class**: Medium-to-high, and ongoing (identity provider, monitoring, production database tier).
- **Operational burden**: High — incident response and security posture become ongoing responsibilities, not one-time setup tasks.
- **Required source changes**: Likely substantial (multi-tenant data isolation was never a design goal of the Phase 4–7 authority chain — e.g. `src.auth.api_keys.AuthenticatedClient` carries only a bare `client_id`, no tenant/organization concept at all).
- **Required infrastructure**: Everything in Option B plus an identity provider, a shared rate-limit backend, a production-grade PostgreSQL tier, TLS at a public ingress, and a monitoring/alerting stack.
- **Rollback strategy**: Must be formally defined as part of a dedicated incident-response plan (§13) — not assessed further here.
- **Suitability for the current project**: **Not suitable as a near-term target.** No decision gathered by this audit suggests the project is ready to take on Option C's own operational burden; it is named here only for completeness, per the approval's own instruction to compare at least three options.

**No specific commercial provider is selected by this document**, per the approval's own instruction — that remains a separate platform decision.

---

## 6. Streamlit access-control assessment

**Current condition, confirmed by direct source read** (`app.py`, `src/streamlit_consumer_controller.py`): Streamlit has **zero** authentication or authorization of any kind. Every `run_*` controller function opens a database session and calls a Phase 6 service directly, with no concept of a logged-in user, a session token, or a permission check anywhere in the call chain. `.streamlit/config.toml` is present but empty — no `[server]` section restricting CORS/XSRF/address binding exists. Any process that can reach a running `streamlit run app.py` can view every page, including page 09's live-Odds/agent trigger and page 04's notebook-`exec()` card engine (both outside the accepted scope, both reachable from the same running process if pages 01–09 are ever deployed alongside 10–12).

| Option | User identity | Session handling | Authorization roles | Logout | CSRF/cookie protection | Brute-force controls | Auditability | Secret storage | Maintenance burden |
|---|---|---|---|---|---|---|---|---|---|
| **Reverse-proxy/OIDC protection** (e.g. oauth2-proxy, Cloudflare Access, a platform-native auth gateway in front of Streamlit) | Delegated to the identity provider | Delegated (provider-issued session cookie) | Coarse (provider group/role, if configured) | Provider-native | Provider-native, well-tested | Provider-native (lockout/throttling typically built in) | Provider-native access logs | Provider-managed, outside this repo | **Low** — no application code change; the smallest new moving part is the proxy/gateway configuration itself |
| **Platform-level private access** (e.g. a VPN-only network, a private container-platform ingress, SSH tunnel/port-forward) | Implicit (network membership) | N/A | None beyond network membership | N/A | N/A | Network-boundary-dependent | Network/VPN logs only | N/A | **Low** — zero application change, but requires every trusted user to have network access configured |
| **Application-level authentication** (a hand-rolled or third-party Streamlit auth component) | Custom | Custom — Streamlit has no first-party session/auth primitive, confirmed by direct inspection; any approach here is either a third-party component or hand-rolled | Whatever is built | Must be built | Must be built or adopted from a component | Must be built | Must be built | Must be designed (where do credentials live?) | **High** — a genuinely new design surface this project has never built before, exactly as `docs/phase8_readiness_assessment.md` §10 (Option C) already identified |
| **Keep Streamlit private, expose only the authenticated API** | N/A for Streamlit itself | N/A | The API's existing Bearer-token model (`src.auth.api_keys`) already provides this | N/A (stateless Bearer tokens) | N/A (not cookie-based) | N/A (not applicable to a non-interactive API) | API's own structured 401/429 logging (`src/api/errors.py`) already exists | Reuses the existing `NAPSEDGE_API_KEY_HASHES_JSON` mechanism, no new secret type | **Lowest** — zero new code; Streamlit simply never becomes publicly reachable at all |

**Recommendation — the smallest acceptable control for a controlled internal deployment**: **reverse-proxy/OIDC protection in front of Streamlit**, or, if the user population is small enough that even that is unnecessary overhead, **platform-level private access** (keeping Streamlit reachable only over a private network/VPN/tunnel). Both options require **zero** change to `src/streamlit_consumer_controller.py` or any tracked page, and both close the single largest named gap in every prior Phase 7/8 document. **Application-level authentication is not recommended** as the first move — it is the highest-burden, highest-new-surface-area option for a problem the other two solve with existing, well-tested components. **No authentication is implemented in this batch.**

---

## 7. Rate-limiting assessment

**Current limiter, confirmed by direct source read** (`src/auth/rate_limit.py`): in-memory, single-process, application-scoped (one `RateLimiter` instance per `create_app()` call), cleared on every restart, never shared across workers or hosts, applied strictly **after** successful Bearer authentication (an unauthenticated/invalid-credential request never reaches `check_and_consume` at all — confirmed by `src/api/deps.get_rate_limited_client`'s own dependency ordering), and no `X-Forwarded-For`/proxy header is read or trusted anywhere in this module.

**When the current limiter remains acceptable**: for exactly the Tier B topology (§4) — a single API process instance, accessed by a small, known set of Bearer-token holders, with no reverse-proxy-induced IP ambiguity the limiter would need to resolve. **When it must be replaced**: the moment more than one API worker process (or more than one host) is run concurrently — a second worker's independent `RateLimiter` instance silently under-enforces the intended policy, since each worker tracks only the requests it personally received.

| Option | Key strategy | Retry behavior | Failure mode if backend unavailable | Proxy/IP trust requirements | Horizontal-scaling implications |
|---|---|---|---|---|---|
| **Single-instance in-memory limiter (current)** | `client_id` (post-authentication) | `Retry-After` header computed from the window's own remaining time (already implemented) | N/A — no external backend to fail | None — never reads a proxy header | **Does not scale horizontally at all** — this is the one must-fix item before Tier C |
| **Redis-backed shared limiter** | `client_id`, same key concept, now shared across all workers via a Redis key | Same `Retry-After` contract, computed from Redis TTL/counter state instead of local memory | Must be explicitly decided: fail-open (allow the request, log a warning) or fail-closed (503, matching this project's existing `RateLimitConfigurationError` precedent for every other misconfiguration) — **not yet decided** | None additional — still keyed by authenticated `client_id`, not IP | Solves Tier C's own named blocker directly |
| **API-gateway or reverse-proxy rate limiting** | Typically IP-based by default, which this project's own threat model (§13) already flags as weaker than `client_id`-based limiting for a Bearer-authenticated API (a single client could rotate source IPs; an IP-based limit also risks over-throttling multiple legitimate clients behind one NAT) | Gateway-native | Gateway-native (usually fails open unless explicitly configured otherwise) | **Requires** a defined trusted-proxy boundary (`X-Forwarded-For` trust), which this project has never configured anywhere | Solves Tier C's scaling concern but reintroduces the proxy-trust question this project has so far correctly avoided by never trusting any forwarded header |
| **Combined client + IP policy** | Both `client_id` (primary) and source IP (secondary, coarser) | Whichever limit is hit first determines the `Retry-After` | Same backend-failure question as the Redis option | Same proxy-trust question as the gateway option | Most defense-in-depth, also the most design work; not decided by this audit |

**Not decided by this audit** (named as open decisions, §18): fail-open vs. fail-closed behavior for a shared backend outage; whether a future reverse proxy will be trusted for `X-Forwarded-For` at all, and if so, which specific proxy IP ranges. **No Redis is added and no rate-limit policy changes in this audit.**

---

## 8. Production PostgreSQL assessment


> **Update (Phase 9B-1, 2026-10-02):** the table below is the original assessment and is unchanged. The "least-privilege application role", "separate migration role" and "restore test" rows now have local-test evidence against a disposable PostgreSQL, recorded in section 22. The rows for SSL/TLS, backup schedule and retention, connection pooling and timeouts, point-in-time recovery, secrets rotation, and any production instance remain open.
> **Update (Phase 9B-2, 2026-10-02):** the SSL/TLS row and the dedicated-backup-role gap now have *contract-level* local-test evidence against synthetic certificates and credentials (section 23). Nothing in production is configured, and production TLS, real certificates, secrets rotation, installed backups and retention remain open.

| Requirement | Current state | Gap |
|---|---|---|
| SSL/TLS connection | `src/db/session.build_engine` passes `connect_args={"check_same_thread": False}` only for SQLite; **no SSL/TLS parameter is set anywhere for a PostgreSQL DSN** — it would need to be embedded in `NAPSEDGE_DATABASE_URL` itself (e.g. `?sslmode=require`), which is possible today without any code change, but is not yet a documented requirement anywhere | Must be documented as a required DSN parameter before any production use |
| Least-privilege application role | Not defined anywhere — every test and local run uses whatever role `NAPSEDGE_DATABASE_URL` happens to name, with no documented "this role should only have SELECT/INSERT/UPDATE on these tables" contract | Open decision |
| Separate migration role | Not defined — `alembic upgrade head` and the application runtime currently use the same `NAPSEDGE_DATABASE_URL` | Open decision — a real deployment should likely separate a DDL-capable migration role from a DML-only runtime role |
| Connection pooling | `create_engine(..., future=True)` uses SQLAlchemy's own default pool (`QueuePool` for non-SQLite); **no explicit pool size, overflow, or recycle setting exists** | Needs an explicit, documented pool configuration before any multi-connection production load |
| Connection/timeout bounds | None configured — no `pool_timeout`, `connect_timeout`, or statement timeout is set anywhere | Open decision |
| Alembic upgrade procedure | Already defined and exercised repeatedly this project's history: `python -m alembic upgrade head`, `alembic check` for drift — confirmed working against both SQLite and a disposable PostgreSQL container throughout Phase 5–8 | **No gap** — the mechanism works; it has simply never been run against a persistent production instance |
| Backup schedule | **Does not exist.** No backup mechanism, schedule, or tooling was found anywhere in this repository | Open decision — likely delegated to a managed PostgreSQL provider's own backup feature if Option B/C topology is chosen, or a `pg_dump` cron job if self-hosted (Option A) |
| Retention policy | **Does not exist** | Open decision |
| Restore test | **Never performed** — no evidence anywhere in this project's history of a restore having been exercised | Required before Tier B can be `GO` (per this audit's own §19 decision rule) |
| Point-in-time recovery | Not assessed — depends entirely on which PostgreSQL hosting option is eventually chosen | Open decision |
| Data-volume expectations | Not formally sized — the accepted Phase 5 schema (23 tables, raw-evidence-only) has only ever been exercised at test/development scale | Open decision, likely small for Tier B's own intended user population |
| Import idempotency | Already a confirmed property of the Phase 5 ingestion/repository layer (re-running an import does not duplicate rows, per the Phase 5 acceptance record) | **No gap** |
| Transaction ownership | Already well-defined: `src.db.session.unit_of_work` commits once per unit of work, rolls back entirely on any exception — confirmed unchanged this batch | **No gap** |
| Secrets rotation | **No mechanism exists.** `NAPSEDGE_DATABASE_URL`, `NAPSEDGE_API_KEY_HASHES_JSON`, and `DISCORD_BOT_TOKEN` are each a single static value with no documented rotation procedure | Open decision |
| Development/test/production isolation | Already strong in practice — every test in this project's history uses either SQLite or a disposable `testcontainers` PostgreSQL instance; `NAPSEDGE_DATABASE_URL` is the one and only switch between "dev SQLite" and "a real database," so isolation is inherent in never setting that variable to the same value in two environments | **No gap in mechanism**, but the discipline of never reusing a production URL in a dev/test context is not enforced by any tooling — a documentation/runbook responsibility |

**Confirmed, per the approval's own required check**: **Phase 5 requires no new analytical tables.** `docs/database_schema.md`'s own "What is deliberately NOT in this schema yet" section (unchanged, re-read this batch) already explicitly defers prediction/market-snapshot/recommendation/publication tables, and this session's own Phase 5 delta audit (`docs/phase5_phase4_authority_delta_audit.md`) independently classified all 17 Phase 4 domains `NO_CHANGE` against the database. Deployment hardening does not reopen or require any schema change.

**No database was provisioned and no migration was run against any external environment by this audit.**

---

## 9. Configuration and secrets assessment

| Variable | Owning process | Required? | Safe default | Validation timing | Secret? | Rotation requirement | Failure behavior | In runbook? |
|---|---|---|---|---|---|---|---|---|
| `NAPSEDGE_DATABASE_URL` | API, Discord (3 Phase 7 commands), Streamlit (pages 10/11) | Optional | Local dev SQLite file | At first connection attempt, not at import | **Secret** (embeds credentials for a real DSN) | Yes, once rotation is designed (§8) | Any syntactically-valid DSN is accepted; a truly invalid one fails on first connection, not at startup | Yes |
| `NAPSEDGE_DATABASE_ECHO` | API | Optional | `false` | At engine construction | No | N/A | Any non-truthy value treated as off | Yes |
| `NAPSEDGE_DB_POOL_SIZE` / `_MAX_OVERFLOW` / `_POOL_TIMEOUT_SECONDS` (Phase 9B-8A) | API, Streamlit, Discord (each PostgreSQL engine) | Optional | 2 / 2 / 10 s (at most 4 connections per process) | At container startup (`src.deployment.env_validation`) and at engine construction | No | N/A | Malformed or out of bounds (1-10 / 0-10 / 1-60) stops the process at startup, naming the variable; pool exhaustion answers a sanitized 503 | `.env.example` |
| `NAPSEDGE_ROOT_DIR` | Everything under `src/paths.py` | Optional | Repository root | At import of `src/paths.py` | No | N/A | N/A | Yes |
| `NAPSEDGE_API_KEY_HASHES_JSON` | API (Bearer auth) | **Required for any protected route** | None — fails closed (503) | Per-request, on first authenticated call | **Secret** (SHA-256 digests, not raw keys, but still access-control-critical) | Yes — this is the one credential this project's own design already makes independently rotatable per `client_id` (`src/auth/api_keys.py`'s own duplicate-digest rejection exists specifically to keep clients independently revocable) | Malformed/duplicate/empty fails `AuthenticationConfigurationError` (503) | Yes |
| `NAPSEDGE_RATE_LIMIT_REQUESTS` / `_WINDOW_SECONDS` | API (rate limiter) | Optional | 60 req / 60 s | Per-request | No | N/A | Malformed/out-of-bounds fails closed (503) | Yes |
| `NAPSEDGE_NET_RANKINGS_SEASON` | API, Discord, Streamlit | **Required for any Rankings operation** | None — fails closed (503) | Per-request | No | N/A | Non-integer/non-positive fails `RankingsConfigurationError` | Yes |
| `NAPSEDGE_NET_RANKINGS_CACHE_PATH` | Same | Optional | `Reference Py Files/net_rankings_2026.csv` | Per-request | No | N/A | Missing/unreadable file surfaces as `unavailable`, not a crash | Yes |
| `NAPSEDGE_LOG_LEVEL` | Every module using `src.logging_config` | Optional | `INFO` | At first `get_logger()` call (process-lifetime, configured once) | No | N/A | Unknown level name silently falls back to `INFO` (confirmed by `getattr(logging, level_name, logging.INFO)`) | **No — not yet documented in the runbook** |
| `DISCORD_BOT_TOKEN` | Discord bot only | **Required to start the bot** | None — `main()` raises before connecting | At `main()` entry, before `client.run()` | **Secret** | Yes — no rotation mechanism exists today beyond "generate a new token in the Discord developer portal and redeploy" | `RuntimeError` before any connection | Yes |
| `DISCORD_ADMIN_USER_IDS` | Discord bot (entitlements) | Optional | Empty allowlist | At the point an admin check runs | No | N/A | Non-numeric entries ignored, not fatal | Yes |
| `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` | Stripe webhook (Flask) only | Required only if that process starts | Empty string | Per-request (`load_env_file()` is called inside the route handler itself) | **Secret** | Yes — no rotation mechanism exists | Empty string fails signature verification per-request, does not crash the process | Yes |
| `STRIPE_WEBHOOK_PORT` | Same | Optional | `8787` | At `app.run()` | No | N/A | N/A | Yes |
| Odds/external-service credentials | The legacy pipeline (`src.pipeline.orchestrator`), outside accepted scope | N/A to this audit | N/A | N/A | **Secret** | Not assessed — out of scope | N/A | Not in runbook, and should not be, since this process class is not part of the accepted deployment |
| Allowed origins/hosts | **Not configured anywhere** — no CORS middleware exists on the FastAPI app, no Streamlit `[server]` origin restriction exists | N/A today | N/A | N/A | No | N/A | N/A | **No — a genuine gap for any deployment beyond localhost** |
| Proxy trust | **Not configured anywhere** — confirmed no `X-Forwarded-For`/`X-Forwarded-Proto` header is read by any module | N/A today | N/A | N/A | No | N/A | N/A | **No — must be defined before any reverse proxy is introduced (§7)** |
| Streamlit access controls | **Does not exist as an environment variable or any other mechanism** (§6) | N/A today | N/A | N/A | N/A | N/A | N/A | **No** |

**Confirmed**:
- `.env` is development-only — confirmed by direct inspection this session (across multiple prior phases) that no tracked, accepted runtime module imports `python-dotenv`; the real Discord bot and Flask webhook each use a hand-rolled, dependency-free `load_env_file()` (`src.ingestion.sportsbook_loader`), called only inside their own entry points, never at import time.
- No production workflow should copy `.env` — this is a documentation/process discipline, not a code enforcement; nothing in this codebase prevents it today, so it must be stated explicitly in any future deployment runbook (Phase 9A, §15).
- Secrets must come from a managed secret mechanism or protected runtime environment — not yet designed; named as an open decision (§18) and as Phase 9B's own scope (§15).
- The Discord token must never be present in the API or Streamlit process environment — **confirmed structurally true today**: `DISCORD_BOT_TOKEN` is read only inside `src/discord_bot/bot.py`'s own `main()`, never by `src/api/` or `src/streamlit_consumer_controller.py`; this separation should be preserved explicitly (not merely incidentally) in whatever secrets-injection mechanism Phase 9B designs, so that the API and Streamlit processes are never handed a credential they have no use for.

**No secret value was read by this audit.**

---

## 10. Observability assessment

| Control | Current state | Required before controlled internal deployment? | Required before public deployment? | Optional future enhancement? |
|---|---|---|---|---|
| Structured application logs | **No** — `src.logging_config` uses plain `logging.basicConfig` with a human-readable text format, not structured JSON | Not strictly required for a small trusted-user Tier B deployment, but recommended | Yes | — |
| Request/correlation IDs | **No** — confirmed absent; `docs/phase6_service_api_design.md` §15 proposed this design once but it was never implemented, reconfirmed still absent this batch | Recommended, not required, for Tier B | Yes | — |
| Error-level logging | **Yes, exists** — `src/api/errors.py`'s `_sanitized_handler` logs every 500/503 server-side with `exc_info` before returning a sanitized response | — | — | — (already adequate) |
| Authentication-failure counts | Logged individually (`_authentication_error_handler` logs a warning per failure) but **not aggregated/counted** anywhere | Recommended for Tier B (to notice credential-stuffing attempts) | Yes | — |
| Rate-limit rejection counts | Logged individually (`_rate_limit_exceeded_handler`) but **not aggregated** | Recommended | Yes | — |
| Database connection failures | Surfaced via `/v1/ready`'s own check and via generic 500 handling, but not separately counted/alerted | Recommended | Yes | — |
| Service latency | **Not measured anywhere** | Optional for Tier B | Recommended for public | — |
| Readiness failures | `/v1/ready` exists and is exercised by every test; **not wired to any external alerting** | Required — the check exists, alerting on it does not | Yes | — |
| Discord connection state | **Not surfaced anywhere** beyond `discord.py`'s own internal logging | Optional for Tier B | Recommended | — |
| Cache age/freshness | **Not surfaced** — both Rankings and Injury caches carry only a static "locally cached snapshot only" disclaimer, no timestamp | Recommended — a Tier B operator should be able to tell how stale the data is | Yes | — |
| Import/migration results | Alembic's own command output; not captured or alerted anywhere beyond the terminal running it | Recommended (capture migration-run logs somewhere durable) | Yes | — |
| Secret/PII redaction in logs | **No explicit redaction logic exists** — relies entirely on the error-handling layer never passing a secret value into a log call in the first place (confirmed true by inspection today, but not enforced by any linter/guard) | Recommended — add a guard or convention before scaling the logging surface | Required | — |
| Log retention | **Not configured** — `logging.basicConfig` writes to stdout/stderr only; retention depends entirely on whatever process supervisor or container runtime captures that stream | Required to define (even a simple "container runtime keeps the last N days") before Tier B | Yes | — |
| Basic alerting | **Does not exist** | Required at least for `/v1/ready` failures and the Discord bot's own disconnect/crash before Tier B can be `GO` | Yes, more comprehensively | — |

**Minimum set required before controlled internal deployment can move from `CONDITIONAL` to `GO`**: a durable log destination (even a simple file or container-log aggregation), basic alerting on `/v1/ready` failures and Discord-process crashes, and cache-age visibility. **No monitoring vendor is selected or added by this batch.**

---

## 11. CI/CD and release controls

| Control | Current state |
|---|---|
| Automated test workflow | **Does not exist** — no `.github/workflows/` directory, confirmed by direct filesystem check |
| Dependency installation from constraints | The *mechanism* exists and is proven (`pip install -c constraints-release-py313.txt -r requirements.txt`, exercised repeatedly in Phase 8A/8), but **no automation runs it** |
| Static or security checks | **None exist** — no linter config, no `bandit`/`safety`/`pip-audit` invocation found anywhere |
| Migration verification | The *mechanism* exists (`alembic upgrade head` + `alembic check`), proven manually; **no automation runs it** |
| Image/build validation | N/A — no Dockerfile exists yet to build |
| Artifact provenance | N/A — nothing is built/published as an artifact today |
| Deployment approval | N/A — no deployment pipeline exists |
| Rollback | Git-level rollback is well-understood (this project's own established discipline — fast-forward-only merges, never force-pushed); **no deployment-level rollback mechanism exists**, since no deployment mechanism exists |
| Environment protection | N/A |
| Branch protection | **Not configured on the remote** — not assessed further here, since this is a GitHub repository-settings change outside this audit's own scope |
| Post-deployment health checks | The `/v1/health`/`/v1/ready` endpoints exist and are exercise-ready; **no automation calls them after a deployment event**, since no deployment event exists |

**Proposed minimum workflow** (not created or activated by this audit):

1. Install from committed constraints (`pip install -c constraints-release-py313.txt -r requirements.txt -r requirements-dev.txt`)
2. Run targeted and non-live tests (`pytest tests/ -q --ignore=tests/postgres`)
3. Run PostgreSQL tests (`pytest tests/postgres -q -m postgres`, requires a Docker-capable CI runner)
4. Run migration check (`alembic upgrade head` against a disposable database, then `alembic check`)
5. Build deployable artifact (container images for API/Streamlit/Discord worker, once Dockerfiles exist)
6. Scan dependencies/artifact (e.g. `pip-audit` against the constraints file, a container-image vulnerability scan)
7. Require deployment approval (a manual gate, even a single-person approval, before step 8)
8. Deploy
9. Run health/readiness checks (`/v1/health`, `/v1/ready`) against the newly deployed instance
10. Roll back on failure (redeploy the previous image tag; `alembic downgrade` only if a migration was part of the failed deploy)

**No pipeline was created or activated by this audit.**

---

## 12. Cache and scheduled-data operations

| Aspect | Rankings | Injury |
|---|---|---|
| Source | `Reference Py Files/net_rankings_2026.csv` | `TeamDump/CBB/*/*.json` per-team files |
| Refresh ownership | Manual, human-replaced file | Manual, human-populated; the scraping functions that could populate it (`scrape_covers_injuries_for_teams`, `scrape_espn_injuries`) remain confirmed orphaned (no caller in the accepted application, reconfirmed by this project's own repeated checks) |
| Current manual process | Replace the CSV file on disk | Replace/add JSON files on disk |
| Expected freshness | Unknown/undefined — no freshness metadata is embedded in the cache itself | Same |
| Failure behavior | Missing/unreadable file surfaces as `unavailable`, not a crash (confirmed, `RankingsConfigurationError`/service-layer mapping) | Malformed element sanitized to an "unavailable" presentation, not a crash |
| Stale-data disclosure | A static "locally cached snapshot only" disclaimer is shown by both Discord and Streamlit consumer surfaces — truthful, but not a true freshness indicator | Same |
| Atomic replacement | **Not guaranteed** — a human overwriting the CSV/JSON files is not an atomic operation; a reader mid-read during a replace could see a partial file. Not yet a confirmed real incident, but not ruled out either. | Same |
| Scheduling needs | None exist today; a scheduled refresh would be genuinely new capability | Same |
| Monitoring needs | None exist — no alert fires if the cache goes stale | Same |

**Disposition**: automated cache refresh is **optional for a portfolio demo**, **recommended but not strictly required for controlled internal deployment** (a small, trusted user base can tolerate a human remembering to refresh the file, especially if the existing staleness disclaimer remains visible), and **required before any public use**, where an unmonitored, manually-refreshed cache would silently present arbitrarily stale data to users with no way to know it. **No scraping or scheduled job is added by this batch.**

---

## 13. Security threat assessment

| Threat | Existing control | Remaining gap | Tier affected | Required remediation | Verification method |
|---|---|---|---|---|---|
| Credential leakage | `.env` untracked/gitignored; secrets never logged (confirmed — `ApiKeyAuthenticator` never logs the raw token); secret scan performed repeatedly this project's history found zero real credentials in git history | No managed-secret mechanism exists yet for a real deployment (§9) | B, C, D | Phase 9B secrets design | Repeat the existing secret-scan methodology against any future deployment artifact/image |
| Unauthorized Streamlit access | None today | No access boundary exists (§6) | B, C, D | Phase 9C | Attempt unauthenticated access to a deployed instance; confirm it is rejected |
| Token brute force | `secrets.compare_digest` prevents timing attacks on comparison; **no lockout or rate limiting on authentication attempts themselves** (the rate limiter applies only after successful auth, confirmed §7) | An attacker can attempt unlimited Bearer tokens against `/v1/teams/resolve` etc. with zero throttling | B, C, D | A future slice should add a pre-authentication rate limit keyed by source IP (not `client_id`, which doesn't exist yet for a failed attempt) | Load-test a sequence of invalid Bearer tokens against a deployed instance; confirm some throttling eventually engages |
| Trusted-proxy/IP spoofing | No forwarded header is ever trusted today — the safest possible current state | Must remain true even after a reverse proxy is introduced (§7) — any future IP-based logic must define a strict trusted-proxy boundary | C, D | Explicit proxy-trust configuration, documented, in Phase 9D | Code review of any future proxy-trust change |
| SQL injection | SQLAlchemy's ORM/Core parameterization is used throughout `src/db/`; no raw string-interpolated SQL was found in any reviewed module | None identified | All | — | Continue the existing convention; no new control needed |
| Mass assignment / invalid request values | Pydantic schemas validate every request body (`src/api/schemas.py`); the non-finite-float sanitization in `src/api/errors.py` closes one previously-reachable crash | None identified beyond what Pydantic already enforces | All | — | Existing test coverage already exercises this |
| Sensitive logging | Error responses are sanitized before leaving the process (§ above); server-side logs include `exc_info` for 500/503s, which could theoretically include a sensitive value if one ever appeared in a traceback's local variables | No explicit redaction scrubber exists for the rare case a secret does end up in a traceback | B, C, D | A logging-redaction convention, recommended as part of Phase 9E | Manual review of log output under induced-failure conditions |
| Dependency vulnerabilities | `constraints-release-py313.txt` pins 172 packages exactly, giving a stable, auditable surface; **no automated vulnerability scan has ever been run against it** | No `pip-audit`/`safety` scan exists | All | Add to the proposed CI pipeline (§11, step 6) | `pip-audit -r constraints-release-py313.txt` as a one-time manual check, then automate |
| Public exposure of legacy commands | Confirmed this session (Phase 7 delta audit): `/odds`, `/research`, `/report`, `/matchup` reach a live, third-party-calling pipeline, entirely outside accepted scope | Starting the Discord bot process at all exposes these 12 pre-existing commands to anyone with guild access — the bot has no command-level feature-flag to disable them independently of the 3 accepted ones | B (if the bot is deployed to any guild with untrusted members), C, D | A product decision: either accept this as part of a "trusted guild only" posture (matching Tier B's own definition), or build a command-level disable mechanism (new scope, not assessed further here) | Manual guild-membership review before any deployment |
| Uncontrolled live-network calls | The accepted API/Streamlit/Discord-command (3 of 15) surfaces make zero outbound calls to anything beyond the configured database and local cache files — confirmed by direct inspection this batch | The 12 legacy Discord commands and Streamlit pages 04/09 do make live outbound calls (Odds API, LLM agents) if reachable | B, C, D | Same as "public exposure of legacy commands" above | Network-traffic review of a running deployment |
| Cross-process secret exposure | Confirmed structurally sound today (§9) — `DISCORD_BOT_TOKEN` never reaches the API/Streamlit process environment | Must be preserved explicitly in any future secrets-injection design (not automatic once a shared secrets manager is introduced — a careless Phase 9B design could accidentally hand every process every secret) | B, C, D | Explicit least-privilege secret scoping in Phase 9B | Code/config review of the secrets-injection mechanism once designed |
| Database over-privilege | Not yet defined (§8) — no least-privilege role separation exists | Same as §8's own finding | B, C, D | Phase 9B | Review the granted role's privileges against the schema |
| Denial of service | The existing rate limiter provides some protection once authenticated; **pre-authentication requests are entirely unthrottled** (same root cause as "token brute force" above); Streamlit has no DoS protection of any kind | Significant for any network-reachable deployment | B, C, D | Reverse-proxy-level request throttling, independent of the application's own `client_id`-keyed limiter | Load test |
| Stale or misleading data | Already mitigated by the existing, truthful "locally cached snapshot only" disclaimer on every Rankings/Injury presentation | No freshness timestamp is shown, only a static disclaimer (§12) | B, C, D | Embed an actual as-of timestamp in the cache/presentation, a future (not Phase 9) slice | Visual/manual review of the presented disclaimer |
| Betting/prediction misrepresentation | Confirmed, repeatedly, across every prior Phase 6/7/8 audit this session: the accepted descriptive Matchup page carries mandatory disclaimers ("No live team data is retrieved. Results are not a prediction. Results are not betting advice.") and performs zero analytical/predictive calculation | None identified within the accepted scope | All | — (already correctly controlled) | Existing tests already assert the disclaimer text verbatim |

**No formal penetration test or security certification is claimed by this assessment.**

---

## 14. Separate non-deployment work (explicitly excluded)

The following are **product/model roadmap items**, not deployment-hardening work, and are explicitly out of Phase 9's own scope:

- Player OVR service and database-input adapter
- Analytical Matchup input-authority design
- New Odds or Strategy APIs
- Model Performance implementation
- Historical-profitability validation
- Walk-forward testing
- New Discord commands
- New Streamlit pages

**Clarification, as required**: historical-profitability validation is required **before any profitability or betting-performance claim** is made about this application — it is **not** required to deploy the currently accepted descriptive/internal functionality (Team, Rankings, Injury, Player raw statistics, Matchup descriptive comparison) at Tier B. Deployment hardening and model/product validation are independent tracks; this document addresses only the former.

---

## 15. Remediation roadmap — dependency-ordered slices

### Phase 9A — Deployment Contract and Container Runtime

- **Objective**: establish the smallest containerized, reproducible runtime contract for the three accepted processes (API, Streamlit, Discord worker).
- **Prerequisites**: none beyond this readiness assessment.
- **Exact code/config scope**: one Dockerfile per process (or a shared base image with three entrypoints); a Compose file wiring them together for local/single-host use; an environment-variable validation pass at each process's own startup (fail fast and loud, not silently, on a missing required variable — extending the existing fail-closed convention already used by `NAPSEDGE_API_KEY_HASHES_JSON`/`NAPSEDGE_NET_RANKINGS_SEASON`); no change to any existing route, service, or consumer logic.
- **Tests**: a smoke test that each container builds and the API's `/v1/health` responds inside it; confirm the existing non-live/API/Postgres test suites still pass unchanged (they test `src/`, not the container boundary, so no existing test should need modification).
- **Acceptance criteria**: `docker compose up` (or equivalent) brings up all three processes against a disposable PostgreSQL container and `/v1/ready` returns 200.
- **Exclusions**: no production secrets, no public ingress, no access control yet (those are 9B/9C).
- **Rollback**: delete/retag the container images; no database involvement at this slice.
- **Estimated operational complexity**: Low.

### Phase 9B — Production PostgreSQL and Secrets

- **Objective**: close every gap named in §8/§9 — a real PostgreSQL operational contract and a managed-secret injection mechanism.
- **Prerequisites**: 9A (a place to inject the secrets into).
- **Exact code/config scope**: document (and, where needed, parameterize) SSL/TLS DSN requirements, connection-pool settings, and a least-privilege role split; no change to `src/db/session.build_engine`'s own logic is strictly required (it already accepts any valid DSN), only its documented usage.
- **Tests**: a restore-drill rehearsal against a disposable instance; a connection using a least-privilege role passes the existing `tests/postgres` suite unmodified.
- **Acceptance criteria**: a documented backup schedule, a successfully executed restore test, and a least-privilege role in active use.
- **Exclusions**: no production data migration is implied — Phase 5's own schema already holds no sensitive production data, since none has ever been imported.
- **Rollback**: `alembic downgrade`, plus restoring from the now-tested backup if needed.
- **Estimated operational complexity**: Medium.

### Phase 9C — Streamlit Access Control and Ingress

- **Objective**: close the single largest named gap (§6) — Streamlit must never be reachable without an access boundary.
- **Prerequisites**: 9A (a container/ingress point to attach the boundary to).
- **Exact code/config scope**: reverse-proxy/OIDC configuration, or platform-level private networking — per §6's own recommendation, **no `src/` code change is required** for either option.
- **Tests**: an unauthenticated access attempt against the deployed Streamlit instance is rejected.
- **Acceptance criteria**: Streamlit is verifiably unreachable by an unauthenticated party in the target deployment environment.
- **Exclusions**: application-level authentication is not pursued unless the smaller options are later found insufficient.
- **Rollback**: remove the proxy/ingress rule; Streamlit reverts to local-only reachability.
- **Estimated operational complexity**: Low-to-medium.

### Phase 9D — Shared Rate Limiting

- **Objective**: close the Tier C blocker named in §7.
- **Prerequisites**: 9A/9B (a place to run a shared backend, if Redis is chosen).
- **Exact code/config scope**: a decision between a Redis-backed limiter and gateway-level limiting (§7); if Redis, a new `src/auth/rate_limit.py` backend implementation behind the same `check_and_consume` interface, preserving the existing fail-closed-on-misconfiguration precedent.
- **Tests**: a multi-worker/multi-process test proving the shared limiter enforces the same policy across more than one process — something the current single-process design cannot be tested for today.
- **Acceptance criteria**: two concurrently running API workers correctly share one rate-limit budget per `client_id`.
- **Exclusions**: no change to the authentication-first ordering (§7's own confirmed, unchanged invariant).
- **Rollback**: revert to the single-process limiter; only safe at Tier B, not Tier C.
- **Estimated operational complexity**: Medium.

### Phase 9E — Observability and CI/CD

- **Objective**: close the gaps named in §10/§11.
- **Prerequisites**: 9A (something to observe).
- **Exact code/config scope**: structured logging format, a request-correlation-ID middleware (the design `docs/phase6_service_api_design.md` §15 already proposed once), basic alerting rules, the CI workflow proposed in §11.
- **Tests**: CI itself becomes the test — a PR cannot merge without the pipeline passing.
- **Acceptance criteria**: every item in §10's "required before controlled internal deployment" row is in place; the CI pipeline runs the full proposed 10-step workflow.
- **Exclusions**: no monitoring vendor is pre-selected by this roadmap.
- **Rollback**: CI/observability additions are themselves low-risk and independently revertible.
- **Estimated operational complexity**: Medium.

### Phase 9F — Security and Deployment Acceptance

- **Objective**: a final, evidence-based go/no-go for Tier B, verifying every threat-control named in §13 that is marked "required" rather than "optional."
- **Prerequisites**: 9A–9E complete.
- **Exact code/config scope**: none new — this slice verifies, it does not build.
- **Tests**: a restore drill (if not already proven in 9B), a multi-instance proof (if 9D was pursued), an unauthenticated-access rejection test (9C), a dependency-vulnerability scan pass (9E/§11).
- **Acceptance criteria**: a final go/no-go matrix (mirroring §4/§13 of this document) with every previously-`CONDITIONAL` item now resolved to `GO` or explicitly, consciously accepted as a documented residual risk.
- **Exclusions**: does not claim a formal penetration test or certification.
- **Rollback**: N/A — this is an acceptance gate, not a change.
- **Estimated operational complexity**: Low (verification only).

---

## 16. Open decisions

1. Which topology option (§5: A, B, or C) — Option A is recommended as the first target, but the final choice is the user's own.
2. Fail-open vs. fail-closed behavior for a future shared rate-limit backend outage (§7).
3. Whether and which reverse-proxy IP ranges will ever be trusted for `X-Forwarded-For` (§7).
4. Managed vs. self-hosted PostgreSQL, and which specific provider if managed (§5, §8) — no provider is selected by this document.
5. Reverse-proxy/OIDC vs. platform-level private access for Streamlit (§6) — both are recommended over application-level auth, but the final choice between the two is open.
6. Backup schedule and retention policy specifics (§8).
7. Whether the 12 legacy Discord commands are accepted as part of a "trusted guild only" Tier B posture, or require a command-level disable mechanism (§13) — a product decision, not a technical one.
8. Logging/observability destination and basic alerting mechanism specifics (§10) — no vendor is selected.
9. Exact secrets-management mechanism for Phase 9B (a managed secrets store, platform-native env-var injection, or something else) — not decided by this audit.

**Resolved by Anthony on 2026-10-09 (no cost; recorded in `docs/phase9b_completion_checklist.md` section 3a):**
- **Item 6 (schedule and retention):**
  - a daily cycle at 03:30 UTC (`Persistent=true`); RPO 24 h; RTO 4 h;
  - retention 7 daily / 4 weekly / 6 monthly (UTC), at most 17 copies;
  - a monthly restore drill; overlap 4 attempts, 900 s apart, within 2 h; 3 upload attempts.
  - Re-check sizing once production data exists.
- **Point-in-time recovery (section 8):**
  - two layers: (a) the provider's daily backups, kept 7 days and destroyed with the cluster; (b) the encrypted off-site logical copies in B2, which survive cluster deletion;
  - no WAL archiving of our own in v1;
  - whether the provider offers point-in-time restore within its 7-day window is to be confirmed at provisioning and recorded.
  - Up to 24 h of data loss is accepted.
- **Item 9 (secrets):**
  - systemd encrypted credentials on the host, mounted read-only into each container as files;
  - the app reads `NAPSEDGE_DATABASE_PASSWORD_FILE`, `NAPSEDGE_API_KEY_HASHES_JSON_FILE` and `DISCORD_BOT_TOKEN_FILE`;
  - the database URL carries no password;
  - rotation = re-encrypt + restart.
- **Item 8 (logging and alerting):**
  - structured JSON logs to stdout, collected by journald, with a request correlation ID in the API;
  - alerting through the existing Discord webhook and healthchecks.io;
  - no paid vendor.
- **Item 2 (shared rate-limit backend outage):** fail closed.
- **Item 3 (`X-Forwarded-For` trust):** none until a reverse proxy exists.
- **Item 5 (Streamlit access):** private only (an SSH tunnel or private network; no public login); rehearsed locally.

---

## 17. Definition of Phase 9 completion

Phase 9 (deployment hardening, overall) is complete when:

- Phase 9A–9F (§15) have each been separately approved, implemented, and accepted;
- the deployment-tier matrix (§4) shows Tier B as `GO`, not `CONDITIONAL`;
- every "required before controlled internal deployment" item in §10/§13 is satisfied, verified, and documented;
- a final go/no-go acceptance record (Phase 9F's own deliverable) exists, mirroring the rigor of every prior Phase 4–8 acceptance document in this project's history.

**This document (Slice 0) completes only the readiness assessment itself** — it does not implement any of the six slices it proposes.

---

## 18. Validation

Performed this batch (documentation-only; no expensive suite rerun, no server/container/cloud contact):

| Check | Result |
|---|---|
| 9 API paths, via safe application construction | Confirmed — `/v1/health`, `/v1/ready`, `/v1/teams/resolve`, `/v1/teams/{team_id}`, `/v1/teams/{team_id}/injuries`, `/v1/players/{player_id}/raw-statistics`, `/v1/rankings/net`, `/v1/rankings/net/teams/{team_id}`, `POST /v1/matchups/feature-comparison` |
| 15 Discord commands, via AST only | Confirmed — matches every carried-forward figure exactly |
| Tracked pages 10–12 | Confirmed via `git ls-files pages/` |
| 6 migrations at head `5b02bea0eb85` | Confirmed |
| Both manifests, identical keys, zero duplicates | Confirmed — 1,156 data rows each |
| Touched documents free of broken links/conflict markers/malformed headings/trailing whitespace | Confirmed by direct inspection of this document, `docs/migration_state.md`, and `docs/release_candidate_runbook.md`'s own forward-pointing note |
| No source, test, notebook, migration, manifest, requirement, or constraint file changed | Confirmed — `git status` scoped to this batch shows only the three intended documentation files |
| Zero residual processes and containers | Confirmed |

**Discord, Streamlit, and Uvicorn were never started. `.env` was never accessed. No cloud platform was contacted. No infrastructure was provisioned.**

---

## 19. Decision-rule compliance

Per this audit's own explicit rule: controlled internal deployment may be recommended as `GO` only if a concrete access boundary, secrets handling, PostgreSQL operations, backup/restore, monitoring minimums, and rollback are each defined. **None of the six is yet defined with the specificity this rule requires** (each is named as an *option space* in §6–§11, not yet a *decision*). Accordingly, this document classifies Tier B **`CONDITIONAL`**, exactly as this rule requires, and does **not** declare public-production readiness anywhere in this document.

---

## 20. Forward-pointing completion note (Phase 9A, 2026-10-01)

Phase 9A (Deployment Contract and Container Runtime, §15) is now complete — a `Dockerfile`, `compose.yaml`, `.dockerignore`, `.env.example`, and a new `src/deployment/` validation/entrypoint package exist and were validated live (all three images build; health checks pass; the Discord container never connects during any validation step; `docker compose up` starts only API/Streamlit). This closes the "zero containerization" finding in §2's own table above for build/runtime purposes, but **does not change Tier B's own `CONDITIONAL` classification** (§4, §19): Phase 9A addresses the container-contract prerequisite only — Streamlit access control (§6), PostgreSQL production operations (§8), shared rate limiting (§7), observability (§10), and CI/CD (§11) all remain exactly as assessed above, unresolved. Full record: [`docs/phase9a_deployment_contract.md`](phase9a_deployment_contract.md).


---

## 21. Private-ingress rehearsal (Phase 9B/9C deployment-rehearsal remediation, 2026-10-02)

**Scope**: a short, disposable rehearsal that reaches the Streamlit container through a random Cloudflare Quick Tunnel. It is a rehearsal of the *ingress mechanics* only. It is **not** Phase 9C access control, **not** a deployment, and exposes nothing beyond the three accepted Streamlit pages.

**What this slice changed** (all committed on `phase9/postgres-secrets-contract`): the images now carry the two runtime data inputs the end-to-end rehearsal proved they need (`cbb_teamScrape_v15_TEST.ipynb`, read as data and never executed; `docs/sr_slug_reconciliation.json`) and only the tracked pages 10-12; `app.py` renders links only for pages that exist; `compose.yaml` and `.env.example` gained an optional, streamlit-only `STREAMLIT_SERVER_CORS_ALLOWED_ORIGINS` pass-through (empty by default, no hostname committed); `.gitignore` gained `.env.phase9.*`.

### 21.1 What stays private

| Component | Binding | Exposed through the tunnel? |
|---|---|---|
| Streamlit | `127.0.0.1:8501` | **Yes - the only thing the tunnel may point at** (`http://localhost:8501`) |
| FastAPI | `127.0.0.1:8000` | **No.** Do not tunnel it in this slice. |
| PostgreSQL (`local-db` profile) | no published port at all | **No** |
| Discord bot | no port; off unless `--profile discord` | **No** (keep the profile off) |
| Stripe webhook / legacy live-odds pipeline | not in any image or compose service | **No** |

**API documentation stays localhost-only.** `/v1/docs` and `/v1/openapi.json` are served by the API **without authentication** (verified: both return 200 with no token). Their only protection is network placement: because the API is bound to `127.0.0.1` and is not tunneled, they are reachable only from the host machine itself during this rehearsal. If the API is ever exposed through any ingress, those two paths become publicly readable unless an access policy or `docs_url=None`/`openapi_url=None` is applied first - a separate decision, deliberately not made here.

### 21.2 Read this before opening a tunnel

- A **Quick Tunnel URL is reachable by anyone who has it.** Its randomness is obscurity, not authentication, and Streamlit has no login of its own (`docs/phase9_deployment_hardening_readiness.md` section 6). Do not put real data behind it.
- Rehearse only against an **empty or disposable database** (the `local-db` profile with a freshly generated password, or the default SQLite fallback). No production import exists (section 21.4).
- Keep it **short-lived**: stop `cloudflared` when finished; the URL dies with it.
- `STREAMLIT_SERVER_CORS_ALLOWED_ORIGINS` is an **origin allowlist, not authentication**. It only decides which `Origin` Streamlit's websocket will accept.
- Nothing in this repository ran `cloudflared` or contacted Cloudflare. The websocket origin behavior below was verified against the real image by sending the `Host`/`Origin` headers a tunnel forwards, not through a real tunnel.

### 21.3 Quick Tunnel / CORS restart sequence

A Quick Tunnel's hostname is random and changes every time `cloudflared` starts, so the allowed origin must be updated and Streamlit restarted **each time**.

1. **Start the stack from a clean checkout** (not a dirty working tree - a dirty tree ships your uncommitted `src/` edits): `docker compose up -d` (default profile = `api` + `streamlit`, both loopback-bound). Leave `STREAMLIT_SERVER_CORS_ALLOWED_ORIGINS` unset. At this point the page works on `http://127.0.0.1:8501` only.
2. **Start the tunnel on the host**, pointing only at Streamlit: `cloudflared tunnel --url http://localhost:8501`. Note the printed `https://<random>.trycloudflare.com`. Running `cloudflared` on the host works with the loopback binding; no compose change is needed.
3. **Before updating anything, expect failure**: opening the tunnel URL now loads the page shell, but Streamlit rejects its websocket with **403**, so the app never runs. This is the symptom the setting fixes.
4. **Record the origin in a git-ignored file**: create `.env.phase9.tunnel` (covered by `.gitignore`, never committed) containing every variable the stack needs plus `STREAMLIT_SERVER_CORS_ALLOWED_ORIGINS=https://<random>.trycloudflare.com` - exactly **one** origin, scheme plus host, no path, no wildcard. `--env-file` **replaces** the default `.env` for Compose interpolation, so the file must be complete.
5. **Restart only Streamlit, naming the service explicitly**: `docker compose --env-file .env.phase9.tunnel up -d --force-recreate streamlit`. Naming the service matters: recreating without it would also recreate the API with whatever that file contains.
6. **Verify**: reload the tunnel URL; the page should run. Then confirm the API is still not reachable through the tunnel and `docker compose ps` shows only `127.0.0.1` bindings.
7. **If `cloudflared` restarts, the URL changes**: repeat steps 4-5 with the new origin. The old origin stops working immediately because it is no longer allowed.
8. **Tear down**: stop `cloudflared` (Ctrl+C), `docker compose down`, and delete `.env.phase9.tunnel`. Do not delete any named volume you did not create for this rehearsal.

**Verified behavior** (real image, simulated tunnel headers): with the setting unset or empty, a public `Host`/`Origin` pair is rejected with 403 and localhost connects; with exactly one origin set, that origin connects and every other origin is rejected with 403; an empty value behaves identically to unset. **Multiple origins through this variable are unreliable** (comma-separated with a space admitted only the second origin; comma-only and JSON-list forms admitted none), so set one.

### 21.4 Next separate gap: no approved database-import CLI

The container database is **empty** after `alembic upgrade head`, and there is **no approved command that populates it**. The only import code is library functions - `persist_allstats_import_plan`, `persist_espn_source_import_plan`, `persist_espn_benchmark_import_plan`, `persist_game_log_import_plan`, `persist_player_game_log_import_plan` (under `src/pipeline/`) - which consume local-only `TeamDump` data that is not in any image. No `__main__` entry point, CLI, or runbook step wraps them. Existing designs are `docs/controlled_bulk_import_design.md` and `docs/controlled_import_rehearsal_design.md`.

**Production data importing is explicitly outside this slice.** Designing, approving, and building an import command (target verification, advisory locking, idempotency, a rehearsal against a disposable database, and where raw source data lives) is the **next separate gap** and needs its own approval. Until it is closed, the only way to see data in the deployed app is to seed disposable rows by hand, which this slice did only inside throwaway test databases.


**Correction (2026-10-02, Phase 9B-1).** The inventory in this section understates what exists, and its "only import code is library functions" statement is superseded. `src/pipeline/` also contains the discovery-to-manifest planner (`bulk_import_planner.build_discovery_to_manifest_plan`), the dry-run manifest writer (`bulk_import_manifest`), four family runners (`bulk_import_allstats_runner`, `bulk_import_game_log_runner`, `bulk_import_player_game_log_runner`, `bulk_import_espn_runner`), `bulk_import_reconciliation`, the safety layer (`bulk_import_safety`: verified database target and advisory lock) and the coordinator (`bulk_import_coordinator.run_controlled_bulk_import`). `docs/migration_state.md` Phases 5C1-5C5 record them being built and rehearsed, culminating on 2026-09-14/15 in a complete-source import (3,531 source artifacts) and an identical replay against a disposable local PostgreSQL; evidence: `docs/rehearsal_evidence/phase5c5_complete_source_postgresql_rehearsal_report.md`. What is still missing is operator wiring: an approved, committed entry point (CLI or scheduler), a production-target decision, and approval to run against any non-disposable database -- separately scoped (`docs/migration_state.md`, "Exact next batch scope", item 20). The paragraphs above are retained as originally written.


---

## 22. Phase 9B-1 -- least-privilege roles and a synthetic restore drill (2026-10-02)

**Scope and status.** Local tests against the existing disposable PostgreSQL framework only (`tests/postgres/test_postgres_roles_and_restore.py`, 32 tests). Uncommitted when written. **This is part of Phase 9B, not overall Phase 9B completion** (see 22.7). Nothing here is deployed, installed or scheduled; no production configuration, real data, credential rotation, tunnel or external account was involved, and the pre-existing Docker volume `bonnieswebscraping_napsedge-local-db-data` was never connected to or removed.

### 22.1 Role contract (as exercised by the tests)

Two roles; an application *writer* role is deliberately deferred until an accepted write workflow needs one (all Phase 6/7 consumers are read-only).

```sql
-- 1. Cluster level, superuser, once per cluster. Passwords are generated and never committed.
CREATE ROLE napsedge_migrator   LOGIN PASSWORD '<generated>' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE napsedge_runtime_ro LOGIN PASSWORD '<generated>' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

-- 2. The migrator owns the database (needs superuser or CREATEDB to run this statement).
CREATE DATABASE napsedge OWNER napsedge_migrator;

-- 3. Database and schema level, run by the owner (the migrator -- no superuser needed).
REVOKE ALL ON DATABASE napsedge FROM PUBLIC;
GRANT  CONNECT ON DATABASE napsedge TO napsedge_runtime_ro;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT  USAGE ON SCHEMA public TO napsedge_runtime_ro;

-- 4. After `alembic upgrade head`, run by the migrator (the table owner): exactly the accepted read set.
GRANT SELECT ON TABLE public.alembic_version, public.conferences, public.players,
                      public.player_stat_snapshots, public.teams TO napsedge_runtime_ro;
```

**Why these five tables.** Derived by inspecting the accepted read paths, then confirmed by capturing every SQL statement they issue under runtime credentials (the test fails if the captured set and the grant set differ in either direction):

| Accepted path (API, Discord, Streamlit) | Tables read |
|---|---|
| Readiness (`check_readiness`, `/v1/ready`) | `alembic_version` |
| Team lookup (`resolve_team`, `get_team`; also the first step of Rankings and Injury lookups) | `teams`, `conferences` (lazy `Team.conference`) |
| Player raw statistics (`get_player_raw_statistics`) | `players`, `player_stat_snapshots` |
| Matchup feature comparison | none |

**Fail-closed default privileges.** No default privileges are configured. A table the migrator creates later carries no ACL and is unreadable by the runtime role (`42501`) until it is granted explicitly. Operational consequence: **step 4 must be re-run after any migration that adds a table the runtime role must read**; forgetting it fails loudly instead of silently widening access. Adding `ALTER DEFAULT PRIVILEGES ... GRANT SELECT` would reverse this trade-off and is a decision for Anthony; one of the mutation controls proves the battery would flag it.

### 22.2 What the permission tests prove

- **Genuine authentication.** `current_user` and `session_user` are both the runtime role, a backend authenticated as it is visible in `pg_stat_activity`, and a wrong password is rejected. These connections come from the host through the container's published port, which the image's `pg_hba.conf` covers with a password rule. `SET ROLE` is never used as evidence.
- **No bypass.** Role attributes are all `false` except `LOGIN`; no role memberships (not the migrator, `pg_read_all_data`, `pg_write_all_data`, `pg_execute_server_program`, the file/monitor/signal roles, `pg_database_owner`, or the admin); owns no relation or schema; the migrator owns the database and every table and sequence; PUBLIC holds nothing on any relation; no column-level ACLs.
- **Exact grants.** Runtime `SELECT` on exactly the five tables above; no INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER or grant option anywhere; no sequence privileges; `USAGE` but not `CREATE` on `public`; `CONNECT` but not `CREATE` or `TEMPORARY` on the database.
- **27 denied operations**, each over a fresh runtime connection and each failing with SQLSTATE `42501` (not a syntax or missing-object error): INSERT/UPDATE/DELETE/TRUNCATE on `teams` and `alembic_version`, `LOCK TABLE ... ACCESS EXCLUSIVE`, `SELECT` on ungranted tables, CREATE TABLE/TEMP TABLE/SCHEMA/FUNCTION/INDEX, ALTER TABLE (add column, owner), DROP TABLE, `nextval`, CREATE ROLE, ALTER ROLE SUPERUSER, ALTER SYSTEM, `SET ROLE` and `SET SESSION AUTHORIZATION` to the migrator, `COPY ... TO PROGRAM`, `pg_read_file`. A fingerprint of every table is unchanged afterward.
- **Mutation controls.** Eleven realistic misconfigurations are injected as superuser (extra INSERT grant, UPDATE on `alembic_version`, an extra readable table, PUBLIC read, sequence usage, schema CREATE, database TEMPORARY, membership in the migrator, membership in `pg_read_all_data`, `CREATEDB`, a default-privilege auto-grant). The battery must fail **for the expected reason** each time, and it passes again after each is reverted -- so a passing battery is evidence, not a tautology.
- **Accepted reads work under runtime credentials**: readiness, Team resolve and lookup (including the conference join), Player raw statistics (found, filtered counts, not-found), the FastAPI `/v1/ready`, `/v1/teams/resolve` and player route, and the Streamlit controller's team and player lookups.
- **Existing write tests keep their own credentials.** No existing Postgres test was run under the runtime role: the import runners and migrations legitimately need write and DDL privileges.

### 22.3 Recovery drill (synthetic data only)

Procedure as exercised, with every step's exit code captured and secrets scrubbed from diagnostics:

1. `pg_dump -Fc` as the migrator to a file on the database host; `pg_restore --list` and a non-zero size check.
2. Ensure the roles exist (they are cluster-global and **not** in a per-database dump).
3. `CREATE DATABASE <fresh> OWNER napsedge_migrator`.
4. `pg_restore --exit-on-error --single-transaction` as the non-superuser migrator.
5. **Re-apply database-level privileges** (role-contract step 3).
6. Verify.

What was verified after restore: per-table row counts and an md5 content hash over every table match the source; the seeded relationships (conference to team, player to team, snapshot to player and artifact) have no orphans; constraints and indexes match and are validated; constraints are *enforced* (foreign key `23503`, unique `23505`, check `23514`); ownership and table/sequence ACLs are identical; sequence state matches and the next generated id exceeds every existing id; `alembic_version` is `5b02bea0eb85` and `alembic check` is clean; readiness and the accepted reads work as the runtime role; the same permission battery from 22.2 passes on the restored database. Negative controls: restoring into an already-populated database and restoring a truncated archive both exit non-zero, and the single-transaction restore leaves the populated database unchanged and the scratch database empty.

### 22.4 Findings

1. **Database-level ACLs are not carried by a per-database dump.** After restore the database still showed PUBLIC's default `CONNECT` and `TEMPORARY`; the REVOKEs and the runtime `CONNECT` grant had to be re-applied explicitly. Ownership and table/sequence ACLs *were* carried.
2. **Roles are not in the dump.** The drill restored inside the same cluster. Disaster recovery to a *new* cluster must recreate the roles first (scripted, or `pg_dumpall --roles-only`), and role passwords must come from the secret store.
3. **22 `varchar` `IN (...)` CHECK constraints are re-rendered by `pg_restore`.** Alembic-created constraints are stored as `ANY ((ARRAY[...])::text[])`; the same constraint re-parsed from the dump is stored as `ANY (ARRAY[(...)::text, ...])` (observed on PostgreSQL 16.15). Same column, same accepted values. The test requires every textual difference to be exactly this family, compares them semantically (column skeleton plus literal set), and proves a restored CHECK still rejects a bad value. Everything else must match exactly.
4. **Restore did not need a superuser.** The non-superuser migrator restored ownership and ACLs successfully into a database it owns.
5. **`alembic/env.py` prefers `NAPSEDGE_DATABASE_URL` over a URL set on the Config object.** The existing `tests/postgres` fixtures do not clear it, so if that variable is exported in a shell, the existing migration fixtures would migrate *that* database. The new module removes it around every Alembic call; the existing fixtures were deliberately left unchanged. A separate, approved fix is recommended.
6. **The runtime role can read every row of its five tables.** There is no row-level security; this is by design for descriptive data and is not a confidentiality boundary within those tables.
7. **`pg_dump` and `pg_restore` are not password-authenticated evidence.** They ran inside the container over localhost, which the image's `pg_hba.conf` trusts, so they prove behavior *as the migrator role* but not authentication. Only the host-side connections (the runtime role, the migrator via Alembic and the seed, and the admin) go through the published port and a password rule; a wrong password was rejected on those.
8. **The drill dumps as the migrator, not a dedicated backup role**, and the container has no TLS, so `sslmode` enforcement is still untested (see 22.7).

### 22.5 Proposed backup schedule and retention -- NOT installed

> **Nothing below exists.** There is no scheduler, no backup role, no storage destination, no provider and no monitoring of backup success. These are starting values for Anthony to accept, change or reject.
>
> **Update (Phase 9B-4, 2026-10-02):** "no backup role" is superseded -- a dedicated backup role has been exercised in tests since 9B-2 (section 23). No backup *job*, schedule, destination or monitoring exists, and this schedule remains uninstalled. The recovery procedure is in `docs/recovery_runbook_fresh_cluster.md`.
>
> **Update (2026-10-02, section 27):** the "member of `pg_read_all_data`" wording in the first row is also superseded -- the backup role has explicit grants and no memberships since 9B-2 (23.3). `docs/backup_artifact_contract_proposal.md` reviews this section and proposes the artifact contract; it is a proposal only and nothing is installed.

| Item | Proposal |
|---|---|
| Logical backup | Nightly `pg_dump -Fc` of the application database, run by a dedicated read-only backup role (member of `pg_read_all_data`; not yet created -- the drill dumps as the migrator), verified with `pg_restore --list` and a size check |
| Storage | Off-host, encrypted at rest; archives contain application data and must be treated as sensitive |
| Retention | 7 daily, 4 weekly, 6 monthly |
| Restore drill | After every migration that changes the schema and at least monthly, using the 22.3 procedure against a disposable instance |
| Point-in-time recovery | Only if a managed provider with WAL archiving is chosen; otherwise the recovery point objective is about 24 hours |
| Success monitoring | Alert on a missing or empty nightly archive |

### 22.6 Evidence and totals

- **Re-executed 2026-10-02:** `python -m pytest tests/postgres/test_postgres_roles_and_restore.py -q -m postgres` -> 32 passed; `python -m pytest tests/postgres -q -m postgres` -> **240 passed** (208 existing + 32 new), 0 skipped, 311.86 s. In a clean worktree at `373b354` plus only the new test file (its `src/ovr_model.py` was the committed empty file and `src/scraper.py` matched the committed hash): the new module 32 passed, and the existing read-path and migration checks (`test_services_team_postgres`, `test_services_player_postgres`, `test_api_readiness_postgres`, `test_api_team_postgres`, `test_api_player_postgres`, `test_api_injury_postgres`, `test_streamlit_controller_postgres`, `test_player_streamlit_postgres`, `test_discord_bridge_postgres`, `test_postgres_migrations`) 67 passed.
- **Previously reported, not re-run in this batch and not retained as a repository artifact:** the full non-live suite result of 5,241 passed / 1 skipped / 5,242 collected (run after the 9B/9C remediation). This batch added only a `tests/postgres` file, which that run excluded, so the figure is neither confirmed nor invalidated here.
- No `src/`, migration, dependency, notebook, manifest or existing-fixture file changed.

### 22.7 What remains before Phase 9B can be called complete

Not covered by 9B-1: TLS/`sslmode` enforcement (the disposable container has no TLS, so the DSN requirement is still documentation only); a dedicated backup role; installed, scheduled and monitored backups with a retention policy; connection pooling, timeouts and statement limits (parameterization was out of scope); a production provider and instance; secrets injection and rotation; and an application writer role once an accepted write workflow exists. Phase 9C (Streamlit access control), the operator import entry point (21.4), shared rate limiting (9D), observability and CI/CD (9E) and the final acceptance gate (9F) are unchanged.

> **Update (Phase 9B-2, 2026-10-02):** items 1-3 of this list (TLS enforcement, a dedicated backup role, password-authenticated tool runs) are addressed at contract level in section 23; production provisioning, scheduled backups and the rest remain open as listed in 23.8. Finding 3 of 22.4 is superseded by 23.5.


## 23. Phase 9B-2 -- password-authenticated, TLS-verified backup and restore tests with a dedicated backup role (2026-10-02)

**Scope and status.** Local tests only, against a *second*, purpose-built disposable PostgreSQL container (`tests/postgres/test_postgres_tls_backup_restore.py`, 67 tests) plus a shared comparator (`tests/postgres/pg_restore_compare.py`, 28 pure unit tests in `tests/regression/test_pg_restore_compare_unit.py`). Uncommitted when written. **This tests *contracts*; it is not an operational deployment and not completion of Phase 9B** (see 23.8).

**Everything cryptographic here is synthetic.** The certificate authority, a second unrelated authority, the server certificate and every password are generated per run (host `openssl`, `secrets`), live in a temporary directory outside the repository and in the throwaway container, and are never written to a tracked file or printed. The CA keys are deleted as soon as the server certificate is signed; the server key is held in memory, injected with mode 0600 owned by `postgres`, and its host file is deleted (a test asserts only the three public certificates remain). None of it is a production certificate or credential, and nothing here shows that any real deployment is configured this way.

### 23.1 Server contract exercised

`ssl=on`, `ssl_min_protocol_version=TLSv1.2`, `password_encryption=scram-sha-256`, and a `pg_hba.conf` consisting only of `local ... reject`, `hostssl ... scram-sha-256` (IPv4 and IPv6) and `host ... reject`. There is no `trust`, `peer`, `md5`, plain-password or certificate-authentication rule and no usable Unix-socket path, so a client tool has no fallback. Tests read `pg_settings` and `pg_hba_file_rules` to assert this, check that every login role stores a `SCRAM-SHA-256$` verifier, and assert that the published port is bound to `127.0.0.1` only (`docker` port bindings, not an assumption). The server certificate's identity is carried by its subject alternative names (`DNS:localhost`, `IP:127.0.0.1`); its common name deliberately matches neither, so a `verify-full` success proves SAN matching.

### 23.2 Client contract and the independent demonstrations

Every successful connection uses `sslmode=verify-full`, an explicit CA file, an explicit `host` and an explicit `hostaddr`. For each of the four roles the test proves, on **one connection**, the role (`session_user`), the TLS state the *server* reports for that backend (`pg_stat_ssl`: protocol, cipher) and the server's own log lines for that backend (SCRAM authentication and an authorization carrying the TLS parameters). Each rejection has a control that differs in exactly one property, and is checked for its own reason:

| Rejection | Reason asserted (client message) | Isolation |
|---|---|---|
| wrong password | `password authentication failed`; the server logs the failure | TLS and certificate succeed first; the same URL with the right password authenticates |
| untrusted CA | `certificate verify failed` | `sslmode=require` without a root certificate connects (encrypted, unverified); `verify-ca` with the right CA connects |
| host name mismatch | `does not match host name` | `verify-ca` with the right CA and the wrong host name connects |
| TLS disabled | `pg_hba.conf rejects connection ... no encryption` | the same credentials over `verify-full` succeed |
| missing CA file | root certificate file does not exist | fail-closed, no silent downgrade |

In every rejection case the server's count of authenticated sessions did not change. Inside the container, a Unix-socket connection is rejected (`rejects connection for host "[local]"`) and so is unencrypted TCP, while the identical tool over TCP + `verify-full` works. Note (libpq behavior, verified): `sslmode=require` *with* a root certificate present is treated as `verify-ca`; the controls therefore omit the certificate.

**Environment isolation.** `PG*` and `NAPSEDGE_DATABASE_*` variables inherited from the developer's shell are removed for the whole module (only names are ever recorded). A control shows an inherited `PGOPTIONS` genuinely alters a connection and that scrubbing genuinely undoes it. **Application path:** `NAPSEDGE_DATABASE_URL` is set to a `verify-full` DSN and `src.db.session.build_engine()` (no argument), `check_readiness`, `create_app()` and `/v1/ready` are exercised with it -- no second connection implementation. Readiness fails closed (`False` / HTTP 503) for an untrusted CA, a host name mismatch, TLS disabled, a wrong password and a missing CA file, each paired with a success control. `alembic check` also succeeds through the real environment-variable path.

### 23.3 Dedicated backup role

`napsedge_backup_ro`: `LOGIN`, non-superuser, no `CREATEDB`/`CREATEROLE`/`REPLICATION`/`BYPASSRLS`, **no role memberships** (not the migrator, not `pg_read_all_data`), owns nothing, no DDL or DML authority. Explicit application-scope grants (chosen over `pg_read_all_data`, which would also read any future schema):

```sql
-- run by the migrator (the owner of every object); idempotent
GRANT CONNECT ON DATABASE napsedge TO napsedge_backup_ro;
GRANT USAGE   ON SCHEMA public     TO napsedge_backup_ro;
GRANT SELECT  ON ALL TABLES    IN SCHEMA public TO napsedge_backup_ro;
GRANT SELECT  ON ALL SEQUENCES IN SCHEMA public TO napsedge_backup_ro;
```

**Each grant is proven necessary:** on a scratch database, revoking `CONNECT`, schema `USAGE`, `SELECT` on a table, or `SELECT` on a sequence makes `pg_dump` fail for the specific reason (`does not have CONNECT privilege`, `permission denied for schema public`, `for table teams`, `for sequence team_aliases_id_seq`), and restoring the grant makes it succeed again. **Boundary battery** (from a real `verify-full` connection): attributes, memberships, ownership, exact table/sequence/schema/database privileges, no PUBLIC or column or default-ACL grants, 31 write/DDL/escalation statements each failing with SQLSTATE `42501` (including `setval`, `pg_authid`, self-granting `pg_read_all_data`, replication slots, `SET ROLE` to the migrator), and unchanged data afterward. Fourteen injected misconfigurations (extra INSERT, UPDATE on `alembic_version`, a missing table/sequence/schema grant, sequence UPDATE, schema CREATE, database TEMPORARY, PUBLIC read, membership in the migrator or `pg_read_all_data`, CREATEROLE, REPLICATION, a default-privilege auto-grant) must each fail the battery for the expected reason, and the battery passes again after each is reverted. The 9B-1 runtime battery (27 denials) was re-run over TLS with the backup role present and is unchanged.

**How newly migrated objects receive backup grants.** Fail-closed, like the runtime role: no default privileges. A table a later migration creates is **not** silently omitted from backups -- `pg_dump` fails with `permission denied for table <name>` -- until the migrator re-runs the four `GRANT` statements above; a test proves the failure, the fix, and that the new table and sequence then appear in the archive. **Operational consequence: the grant step must run after every migration that adds a table or sequence**, alongside the runtime role's grant step (22.1).

### 23.4 Recovery over authenticated TLS

`pg_dump -Fc` as the backup role and `pg_restore --exit-on-error --single-transaction` as the migrator, both over TCP with `sslmode=verify-full`, an explicit CA and `hostaddr`, run **inside the container**. The password never appears in argv, an environment value or a logged DSN: a temporary pgpass file is created *inside the container* through stdin with mode 0600, only its path is exported (`PGPASSFILE`), `--no-password` is set, and the file is removed afterward (asserted). Before the successful dump, `pg_dump` is run and fails for each of: wrong password, untrusted CA, host name mismatch, TLS disabled, Unix socket (valid credentials in every case but the first). Before the successful restore, `pg_restore` is run and fails the same five ways. Each failure is checked for its own reason, and the server's authenticated-session count did not change; `pg_dump` leaves at most an *empty* output file when it cannot connect (it opens the file before connecting -- observed), and the test asserts no archive content exists. Ordering is recorded and asserted. Then the real dump and restore succeed with empty stderr, and the server log shows `pg_dump` authenticated as the backup role and `pg_restore` as the migrator, each by SCRAM over TLS 1.2 or 1.3, and no `pg_dump` under the migrator or `pg_restore` under the backup role.

After restore, the 9B-1 checks are reused through the shared comparator: per-table counts and content hashes, constraints (with only the documented rendering difference), indexes, validation state, sequence state, ownership and table/sequence ACLs (which include the backup role's grants), constraint enforcement, `alembic_version`/`alembic check`, readiness and accepted reads as the runtime role (through the environment-variable application path), the runtime and backup boundary batteries on the restored database, and a second-generation dump of the restored database by the backup role. Database-level privileges are again **not** carried and are re-applied explicitly. Additional negatives: restoring into a populated database fails atomically; a truncated archive fails and leaves nothing; and **restoring into a cluster that lacks the roles the archive names fails atomically (`role "napsedge_backup_ro" does not exist`)** -- so recovery must provision cluster roles first.

### 23.5 Comparator tightening (correction of a 9B-1 weakness)

The 9B-1 normalization of re-rendered CHECK constraints stripped all parentheses and sorted the literals. A probe showed it treated `(A AND B) OR C` as equal to `A AND (B OR C)` and accepted a reordering of an `IN` list as "rendering only" -- a changed boolean grouping could have been reported as equivalent. It is replaced by `tests/postgres/pg_restore_compare.py`: one exact structural rewrite of the single observed restored rendering (`ANY (ARRAY[('a'::character varying)::text, ...])` becomes `ANY ((ARRAY['a'::character varying, ...])::text[])`), after which **byte equality** with the source text is required, and only for same-named, same-table, same-type, same-validation-state CHECKs. It performs no other normalization. 28 unit tests (default suite, no Docker) pin it: the known rendering is accepted exactly; every one of ten meaning-changing mutations (extra, removed or changed value, different column, operator flip, reordered or duplicated literal, numeric cast, extra OR clause, NOT) and the boolean-grouping change are flagged, each with a control that the same shape *is* accepted when only the known rendering differs; validation-state and non-CHECK differences are never rendering-only. A live control tampers one CHECK on a real migrated database and requires exactly that constraint in the genuine-difference bucket, then reverts. The 9B-1 module now uses the shared comparator and its Alembic URLs are percent-escaped (`escape_for_alembic`).

### 23.6 Findings

1. **`alembic/env.py` cannot take a DSN containing `%`.** It passes `NAPSEDGE_DATABASE_URL` to `Config.set_main_option`, which treats `%` as interpolation, so a percent-encoded password or certificate path fails with `ValueError` before any connection. Characterized by a test (no production change made). Operators must use a DSN with no percent-encoding (use passwords from the URL-safe alphabet and an unencoded `sslrootcert` path) until `env.py` escapes `%`. A separate, approved one-line fix is recommended.
   **Resolved in Phase 9B-3 (section 24), 2026-10-02:** `env.py` now escapes `%` at that one call; verified through the production path with raw percent-encoded URLs, including real TLS migrations. The paragraph above is retained as the historical finding.
2. **`sslmode=require` with a root certificate present is verify-ca** (libpq). A DSN that sets `require` and a CA file is therefore *verified* but does not check the host name; only `verify-full` does. The contract should say `verify-full` explicitly.
3. **`pg_dump` needs `SELECT` on sequences and `USAGE` on the schema** (proven necessary, 23.3). Granting tables alone is insufficient.
4. **`pg_dump` creates its output file before connecting**, so a refused dump can leave an empty file; operators and scripts must key off the exit status, never file existence.
5. **Archives name roles but do not create them** (23.4): disaster recovery to a new cluster must provision roles before `pg_restore`.
6. **Superseded 9B-1 caveats:** findings 7 and 8 (22.4) -- `pg_dump`/`pg_restore` were not password-authenticated and the drill dumped as the migrator -- are addressed by this section for the contract: the tools now run over TCP + verified TLS + SCRAM and the dump runs as the dedicated backup role. Finding 3 is superseded by 23.5.

### 23.7 Evidence and totals

- **Re-executed 2026-10-02:** `python -m pytest tests/postgres/test_postgres_tls_backup_restore.py -q -m postgres` -> 67 passed. `python -m pytest tests/postgres -q -m postgres` -> **308 passed**, 0 skipped, 353.15 s. Reconciliation: previous total 240 = 208 existing + 32 (9B-1 module); now 208 original + 33 (9B-1 module: one test replaced by a stricter equivalent and one live comparator-tamper test added) + 67 (new) = 308. Default-suite (non-Docker) addition: 28 tests in `tests/regression/test_pg_restore_compare_unit.py` (this file is new; the earlier 5,242-collected figure does not include it, and the full non-live suite was **not** re-run in this batch).
- **Clean worktree** (committed `373b354` plus only the 9B-1/9B-2 test files, helper and unit file): unit file 28 + 9B-1 module 33 + 9B-2 module 67 passed / 67 existing read-path and migration Postgres checks passed (195 total, 152.47 s; its src/ovr_model.py was the committed empty file).
- No `src/`, migration, dependency, notebook, manifest or existing-fixture file changed. The pre-existing Docker volume `bonnieswebscraping_napsedge-local-db-data` was neither connected to nor modified; the test container's resources were removed.

### 23.8 What remains before Phase 9B can be called complete

Still open and **not** done by 9B-2: production provisioning of roles, a database instance and a real certificate chain (the contract above is proven against synthetic ones only); secret injection and rotation; installed, scheduled and monitored backups with a retention policy and an off-host encrypted destination (the 22.5 schedule is still only a proposal -- the backup *role* now exists in tests, no backup *job* does); the operator import entry point (21.4); connection pooling, timeouts and statement limits; an application writer role once an accepted write workflow exists; the `env.py` percent-interpolation limitation (finding 1); and recovery to a *new* cluster (roles and passwords from the secret store). Phase 9C (Streamlit access control), 9D, 9E and 9F are unchanged.


## 24. Phase 9B-3 -- Alembic database-URL interpolation fix (2026-10-02)

**Scope and status.** One production line in `alembic/env.py`, a focused regression module and live TLS regression tests. Uncommitted when written. It resolves the one production-code defect Phase 9B-2 found (23.6 finding 1). It does **not** change what any URL means, any migration revision, any dependency, any notebook or any manifest, and it does not complete Phase 9B (see 24.6).

### 24.1 The failing boundary

`alembic/env.py` resolves its URL (the `NAPSEDGE_DATABASE_URL` environment variable, else a URL already set on the `Config`, else the development SQLite path from `get_database_url()`) and, for the first and third cases, stores it with `config.set_main_option("sqlalchemy.url", ...)`. `set_main_option` stores into a `ConfigParser` whose interpolation treats `%` as syntax. A raw, valid URL containing percent-encoding -- an encoded `@` or `%` in a password, an encoded space in a certificate path, or even a `%` in the fallback SQLite directory name -- therefore raised `ValueError: invalid interpolation syntax` inside `env.py` before any connection was attempted. Reproduced against the unmodified `env.py` with synthetic URLs through `alembic.command` (the environment URL was never routed through the test-only `escape_for_alembic` helper): 16 of the 20 focused tests failed with exactly that error and 4 passed (the percent-free and programmatic-config cases). **Side finding:** that error text echoes the entire URL, password included, so the defect could also leak a credential into logs.

### 24.2 The fix

```python
config.set_main_option("sqlalchemy.url", get_database_url().replace("%", "%%"))
```

(with an explanatory comment). The doubling is a storage escape local to the ConfigParser boundary: `get_main_option` -- and therefore `engine_from_config` online and the `url` used offline -- returns the original text exactly. Nothing is URL-decoded or re-encoded, interpolation is not disabled anywhere, no new configuration source exists, and resolution precedence is unchanged (environment over a programmatic URL over the SQLite fallback). The escape is applied only where `env.py` itself calls `set_main_option`. Callers that set their own URL programmatically (the existing test fixtures, `tests/postgres/conftest.alembic_config_for`, and `escape_for_alembic` in the 9B-1/9B-2 helpers) still must write `%` as `%%` themselves; that remains independently necessary and is unchanged.

### 24.3 Coverage added

- **`tests/regression/test_alembic_env_database_url.py` (20 tests, default suite, no Docker or network).** Runs the real `env.py` through `alembic.command` and captures what the driver would receive. Six synthetic URLs: no percent escapes; encoded `@` and `%` in the password; an encoded certificate path containing a space (POSIX and Windows-style); all of those together; and a password that contains the two characters `%%`. For each: the URL handed to the engine factory equals the original byte for byte, SQLAlchemy decodes the intended password and `sslrootcert`, and `Config.get_main_option` returns the original. Offline mode (`--sql`) resolves the same URLs. Repeated runs on one `Config` and on fresh ones never accumulate escapes. Unchanged behaviour: the environment overrides a programmatic URL; a programmatic URL (percent-free, or escaped by its caller) is kept when the variable is absent; the SQLite fallback still applies and survives a `%` in its path. A permanent sensitivity control runs a copy of the Alembic directory whose `env.py` lacks the escape and requires the original interpolation error from `env.py` before the engine is reached (and fails loudly if the marker disappears). Assertion helpers never print a URL or password.
- **`tests/postgres/test_postgres_tls_backup_restore.py` (74 tests; was 67).** The 9B-2 limitation test became a regression test (the same raw DSN now reaches the driver and fails only because nothing listens). New live tests use a role whose password needs `@`, `%` and `/` encoded and CA files under a directory whose name contains a space, with a DSN encoded the way a standards-following operator would write it: a fresh database is migrated with `alembic upgrade head` and checked with `alembic check` through the production `env.py` using only the raw environment URL (no programmatic URL), the server log shows the SCRAM + TLS session for that role, and application readiness works with the same URL. With every property correct as the control, a wrong password, an untrusted CA, a host name mismatch, TLS disabled and a missing CA file are each still refused by the server or driver for their own reason -- never by ConfigParser.
- **Revert check.** With the fix removed from the real file: the focused module gave 16 failed / 4 passed, and the live percent tests failed with `ValueError: invalid interpolation syntax` (2 failed and 6 fixture errors); the fix was restored and everything passed again.

### 24.4 Review of the 9B-2 assertions

The 9B-2 test `test_alembic_check_through_the_real_environment_path_works_with_a_verified_tls_dsn` and the application-path tests used a hand-written, **percent-free** DSN, so 9B-2 never exercised the defect through the production path; its statement that `alembic check` succeeds through the real environment-variable path holds only for DSNs without `%`. That qualification is now explicit, and the percent-encoded case is covered by the tests above. The `handwritten_dsn` docstring was updated accordingly.

### 24.5 Evidence and totals

- **Re-executed 2026-10-02:** focused DSN regression 20 passed; comparator unit tests 28 passed; existing default-suite migration tests (`test_db_migrations`, `test_services_health`) 45 passed; `python -m pytest tests/postgres/test_postgres_tls_backup_restore.py -q -m postgres` 74 passed; `python -m pytest tests/postgres -q -m postgres` **315 passed**, 0 skipped, 382.53 s.
- **Test history, stated precisely.** 208 original PostgreSQL tests; Phase 9B-1 added a 32-test module (240 total); Phase 9B-2 revised that module to 33 tests and added a 67-test module (208 + 33 + 67 = 308); Phase 9B-3 changes the 9B-2 module from 67 to 74 (one characterization test replaced by one regression test, plus seven new tests) for 208 + 33 + 74 = 315. Wording elsewhere that calls the 240 "existing" or "pre-existing" meant the 208 original tests plus the 32 tests of the 9B-1 module as it then stood. The default suite also gained the 28 comparator unit tests (9B-2) and the 20 focused regression tests (9B-3); the full non-live suite was not re-run in this batch.
- **Clean worktree** (committed `373b354` plus only the intended uncommitted files): focused regression 20 + comparator unit tests 28 + existing migration default-suite tests 45 + revised 9B-1 module 33 + 9B-2 module 74 + 67 existing read-path and migration Postgres checks = 267 passed in 222.27 s (committed 373b354 plus only the intended uncommitted files; its src/ovr_model.py was the committed empty file)
- **Manifests.** `docs/function_manifest.csv` and `docs/notebook_src_coverage.csv` index named functions and their notebook or src provenance. `alembic/env.py` is a module-level script, no named function was added, removed or renamed, and no notebook or `src/` function changed, so both manifests are deliberately unchanged, consistent with the earlier database batches. Unrelated inventory changes were avoided.

### 24.6 What remains open

Unchanged by 9B-3: production provisioning (instance, roles, a real certificate chain), secret injection and rotation, installed and scheduled backups with retention and an off-host destination, recovery to a new cluster, the operator import entry point, connection pooling and timeouts, the application writer role, and Streamlit access control (Phase 9C). A programmatic `Config` caller that sets its own percent-containing URL must still escape `%` as `%%`. The 9B-2 recommendation to use only percent-free DSNs is withdrawn for `NAPSEDGE_DATABASE_URL`; the recommendation to set `sslmode=verify-full` explicitly stands.

> **Update (Phase 9B-4, 2026-10-02):** section 24.5 records 74 TLS tests and 315 PostgreSQL tests for the tree measured in 9B-3; one test was later removed at review (73 and 314). Section 25.4 gives the reconciled history; the 9B-3 figures are retained as that tree's results.


## 25. Phase 9B-4 -- loss-of-source recovery onto an independently initialized cluster (2026-10-02)

**Scope and status.** Synthetic tests, disposable resources and documentation only (`tests/postgres/test_postgres_fresh_cluster_recovery.py`, 14 tests; a behaviour-preserving helper addition in `tests/postgres/pg_restore_compare.py`; the operator runbook `docs/recovery_runbook_fresh_cluster.md`). Uncommitted when written. **This is a rehearsal and a procedure, not an installed capability and not completion of Phase 9B** (see 25.6). The pre-existing Docker volume `bonnieswebscraping_napsedge-local-db-data` was never used.

### 25.1 What was already proved, and what needed a separate cluster

Phases 9B-1 and 9B-2 proved recovery *within one cluster*: content, constraints, indexes, sequences, ownership, ACLs, readiness, accepted reads, and (9B-2) authenticated `verify-full` TLS with a dedicated backup role. In that setup the restore target shares the source's roles, certificate chain, superuser and server, so a hidden dependency on any of them cannot appear. 9B-4 removes that shelter. The 9B-2 rejection matrices (wrong password, untrusted CA, host mismatch, TLS disabled, Unix socket) are not repeated; the only negatives added are the ones specific to a fresh cluster.

### 25.2 The scenario (one module-scoped, timed sequence)

> **Correction (2026-10-02, section 26):** the order below (bootstrap before archive transfer) is superseded -- archive verification now precedes bootstrap -- and the conditions described as checked are now enforced during execution. The original text is retained as the record of the first 9B-4 pass.

1. Two independently initialized `postgres:16-alpine` clusters, each with its own synthetic CA, server certificate, superuser and generated credentials (distinct `system_identifier`, container, port, CA fingerprint, every password).
2. Source: bootstrap the three roles and the database from the declared definitions, migrate to head, apply grants, seed synthetic records (all existing helpers). A baseline is captured **as data** before anything stops.
3. `pg_dump -Fc` as `napsedge_backup_ro` over `verify-full` TLS; the source's own log shows the SCRAM + TLS session. The archive is copied off the source and checksummed.
4. The source container (owned by the scenario) is removed. A connection to its address is refused; nothing is read from it afterwards.
5. Destination: an empty-destination gate (three counts that must be zero; shown to fail on the populated source), then bootstrap with **new** passwords, then archive transfer, checksum equality and `pg_restore --list`.
6. `pg_restore --exit-on-error --single-transaction` as the non-superuser migrator over the destination's own TLS; then database-level grants the archive does not carry are re-applied (before this step the restored database still showed PUBLIC's default `TEMPORARY` and `CONNECT`).
7. Verification: the comparison against the pre-loss baseline; the full 9B-1 runtime boundary battery (27 denied statements) and the 9B-2 backup boundary battery (31); `alembic check`, head `5b02bea0eb85` and application readiness through the environment-variable path under runtime credentials, with the accepted read paths reading exactly the five granted tables; the old source passwords for all three roles and the old superuser refused (`password authentication failed`) with the destination's own passwords succeeding as the control; the destination certificate trusted by its own CA and refused under the old CA (`certificate verify failed`), confirmed offline with `openssl verify`.
8. Cleanup: only the two containers, two temporary certificate directories and one archive directory the scenario created are removed, and the fixture asserts they are gone.

Roles are created through `psql` reading SQL from stdin and the dump and restore use protected pgpass files, so no password is ever an argument. No real password hash or globals dump is involved.

### 25.3 Helper change

`tests/postgres/pg_restore_compare.py` gained `DatabaseBaseline`, `capture_baseline` and `compare_to_baseline`, so a source that no longer exists can be compared against data captured earlier. `compare_databases` is now defined as capture-then-compare and behaves identically; it is exercised by the revised 9B-1 module (33 passed) and the 9B-2 module (73 passed) after the change. No shared fixture was altered, and the new module imports the existing TLS launch helpers rather than copying them.

### 25.4 Count adjustment and history (results belong to the trees that produced them)

Phase 9B-3 recorded the TLS module at 74 tests and `tests/postgres` at 315, measured on that tree. A later review removed one test from the TLS module, `test_alembic_env_accepts_a_percent_encoded_dsn_and_reaches_the_driver`, because it connected to `localhost:1` and accepted any unrelated `OperationalError`. The 20 deterministic DSN regression cases and the successful authenticated TLS migration coverage are retained and nothing replaced it. Consequently: TLS module 73, `tests/postgres` collection 314, and 314 + 14 new = **328 collected** (confirmed with `--collect-only`). The earlier 74 and 315 remain the results of their tree and are not edited. History: 208 original, then 240 (9B-1), 308 (9B-2: 208 + 33 + 67), 315 (9B-3: 208 + 33 + 74), 314 (test removed), 328 (9B-4: 208 + 33 + 73 + 14).

### 25.5 Validation

- Re-executed 2026-10-02: focused DSN regression 20 passed; comparator unit tests 28 passed; TLS module 73 passed; revised 9B-1 module 33 passed; new recovery module 14 passed on every one of its seven executions (three in the working tree, four in clean worktrees).
- Clean worktree (committed `373b354` plus only the intended uncommitted files): 20 + 28 + 33 + 73 + 14 + 67 existing read-path and migration Postgres checks = **235 passed**, 0 skipped, 190.85 s.
- **Disclosure of an intermittent failure.** The first clean-worktree run of that same 15-file set gave 234 passed and **1 failed**: the existing `test_ambiguous_team_query_raises_identity_conflict_error_against_real_postgresql` (`tests/postgres/test_services_team_postgres.py`). I could not reproduce it: that file alone passed (12), with the recovery module first it passed (26), the same six-file prefix passed (180), the full set passed again (235), and `resolve_team_identity("Miami")` returned `UnresolvedIdentity` / `ambiguous` under 12 different hash seeds. The failure message was not retained, so the cause is **unknown**. It is an existing test that does not use any file changed in 9B-4. Treat it as an unresolved intermittent, to be watched, not as explained.
- The 208 original tests other than the 67 existing read-path checks above were **not** re-run in this batch: no file they use changed. The full non-live suite was not re-run. Skips: none.
- Resource cleanup verified after every run: no containers, the volume list unchanged, no temporary certificate, archive or worktree directories.

### 25.6 What remains open

Everything the runbook marks **[FUTURE]**: a real destination cluster and provider, real certificates, secret injection and rotation, an off-host encrypted archive store, an installed, scheduled and monitored backup job with retention (the 22.5 proposal is still **not installed**), the operator import entry point, connection pooling and timeouts, the application writer role, application cutover, and Streamlit access control (Phase 9C). The runbook's timings are synthetic and are not a production recovery guarantee. Note: 22.5's statement that no backup role exists is superseded since 9B-2 (a dedicated backup role is exercised in tests); no backup job, schedule, destination or monitoring exists.


## 26. Phase 9B-4 verification correction (2026-10-02)

**Scope.** A narrow correction to the 9B-4 rehearsal: its preconditions are now enforced while it runs, owned-resource cleanup is verified on failure paths too, the runbook's claims were tightened, and the unresolved identity-test failure was re-examined. Synthetic and disposable resources only; uncommitted when written; no production code, migration, dependency, identity rule, formula, notebook or export changed, and `alembic/env.py`'s approved percent-escaping fix is untouched. Status vocabulary below: **implemented**, **tested**, **verified** (in a clean worktree), **unresolved**, **deferred**.

### 26.1 What changed in `tests/postgres/test_postgres_fresh_cluster_recovery.py` (implemented)

The 9B-4 fixture previously recorded conditions and let later tests assert them. Four conditions now stop execution at the stage they protect, by raising `PreconditionFailed` from guard functions used by the real scenario:

| Gate | Enforced before |
|---|---|
| E1 source-to-host archive checksum equality | the source is removed |
| E2 confirmed source removal **and** a failed connection to the source | any destination recovery step |
| E3 all three destination emptiness counts exactly zero (a missing count is not zero) | bootstrap SQL and restore |
| E4 destination checksum equality, successful `pg_restore --list`, and the expected `TABLE DATA public teams` entry | bootstrap and `run_restore` |

Orchestration stays in the test layer (small stage functions, an injectable `RecoveryActions` record for the side-effecting steps, and an `owned_resources` ledger); no recovery framework was created. Archive verification now runs **before** bootstrap, so a rejected archive leaves the destination untouched; section 25.2's step order (bootstrap before archive transfer) is superseded by this one.

### 26.2 Negative tests through the same guarded path (tested)

Against real disposable clusters: a checksum mismatch between the source and the host copy leaves the source running and records no host archive; an unconfirmed removal (a removal that does nothing, and a container reported gone that still answers) is refused, and recovery refuses an unconfirmed loss before any action runs; a real cluster that already holds the three roles blocks bootstrap, archive installation and restore (a recorder shows only the emptiness check ran; roles unchanged, target database never created, no archive on the destination); a one-byte-corrupted archive, a truncated archive whose own digest is recorded as expected (so only the listing can object), and a successful listing without the expected entry each block bootstrap and restore and leave the destination still empty. Pure tests pin each guard's exact unsafe condition (11 refusals) and its safe condition, and the ledger. The 9B-2 TLS and authentication rejection matrices were not duplicated. **Sensitivity check:** with `require` temporarily made a no-op, 17 of the guard-related tests failed (including the live negatives, which then reached a real, failing bootstrap), and the file was restored.

### 26.3 Cleanup on failure (tested)

Every container, temporary certificate directory and archive directory is registered with `owned_resources`, which removes them and then verifies they are gone on success **and on failure**; a verification failure is attached to the original exception as a note and never replaces it (pure test). The populated-destination test proves the failure path with live resources: it asserts, after the refusal, that its cluster and certificate directory no longer exist. The shared `pg_container` and the existing Docker volume were not used or touched.

### 26.4 Runbook corrections (implemented)

Rollback is described as preserving the **pre-restore state** (the empty, freshly bootstrapped database in this procedure), not as "emptying" anything, with a warning never to restore into a database holding data to keep. The pre-loss comparison baseline is a required artifact: the rehearsal keeps it **only in memory**, durable and backup-consistent capture and storage are **not implemented**, and capturing a baseline from a live source accepting writes is **not validated**. The contradictory `bootstrap.sql` redirection was replaced with the tested approach (SQL built in memory and piped to `psql -f -`). Sequence checking is claimed as **state equality only**; next-ID safety is stated as not checked in this rehearsal. Section 8 now labels its eleven items as post-recovery **observations** and section 4-5/E1-E4 as **enforced gates**, and section 10's timings as measured observations.

### 26.5 The unresolved identity-test failure (unresolved)

The existing test `test_ambiguous_team_query_raises_identity_conflict_error_against_real_postgresql` (`tests/postgres/test_services_team_postgres.py`) failed once, in the first clean-worktree run of the original 15-file set (234 passed, 1 failed; the message was not retained). **Inspection.** The test calls `resolve_team(session, "Miami")` and expects `IdentityConflictError`, raised when `resolve_team_identity` returns `UnresolvedIdentity(reason="ambiguous")`. That depends only on process-global identity state, not on the database: `slug_utils.TEAM_REGISTRY`, `slug_utils._ALIAS_TO_ID`, `team_identity._REGISTRY_READY`, and `team_identity._ALIAS_MULTIMAPS`, a cache built from the registry on the first resolution that reaches the collision cross-check and never rebuilt. Five files in the selection call `ensure_team_registry()` (idempotent, builds once per process); the two test files that call `build_registry` directly (`tests/regression/test_consumers_integration_isolation.py`, `test_espn_team_abbreviation_aliases.py`) are **not** in the selection. `slug_utils.resolve_team` (a different function) mutates `_ALIAS_TO_ID` as a side effect, but the identity path's multimaps derive from each team's own alias sets. Nothing found explains the failure, and no mechanism above is claimed as its cause. **Replay.** The original 15-file command was replayed once, in its original file order, in a clean worktree of revision `373b35452bb47f71d5f85cbca1e0624a9e4139f3` plus the intended uncommitted files, with `--tb=long -ra --junitxml` (no `--showlocals`), Python 3.13.3, pytest 9.1.1, Docker server 29.7.2, OpenSSL 3.1.4, no `PG*` or `NAPSEDGE_DATABASE_*` variables set: **exit status 0, 254 passed (20 + 28 + 33 + 73 + 33 + 67), 0 failed, 0 errors, 0 skipped, 204.07 s**. Full output, JUnit XML, environment and file order were retained (outside the repository), and a scan found no credential-bearing URL in them. **The failure did not recur, so it remains explicitly UNRESOLVED**: this passing run is not a root-cause explanation, and no identity rule or expected result was changed. Prior non-reproducing re-runs (the file alone, with the recovery module first, a six-file prefix, and the full set) are recorded in 25.5.

### 26.6 Validation and counts (verified)

- `python -m pytest tests/postgres/test_postgres_fresh_cluster_recovery.py -q -m postgres -rP`: **33 passed**, 0 skipped (the module had 14 tests; 19 net new: 6 live negative tests, 12 pure guard tests, 1 ledger test, with one test replaced by an equivalent that records the enforced-gate order).
- `python -m pytest tests/regression/test_pg_restore_compare_unit.py -q`: 28 passed (unchanged; the comparator was not modified in this correction).
- Original 15-file set in a clean worktree: 254 passed (above).
- `tests/postgres` now collects **347** tests (314 + 19), confirmed with `--collect-only`. This is a **new** count; the earlier recorded counts (74 / 315 for the 9B-3 tree; 73 / 314 / 328 after the 9B-4 changes) remain the results of their trees and are not edited. The full `tests/postgres` run and the full non-live suite were not repeated: no file shared with the other original tests changed.
- Whitespace on the touched files is clean (the unrelated `Conference_Ratings/big-ten_2026_ovr_dashboard.html` trailing-whitespace line is untouched). Resource cleanup was confirmed after every run: no containers, the volume list unchanged, no temporary directories or worktrees.
- **Manifests reviewed, not changed.** `docs/function_manifest.csv` and `docs/notebook_src_coverage.csv` index named notebook or `src/` functions with per-function test counts in named test files. This correction changed no function and no test file those entries name (for example the `check_readiness` row counts its tests in `test_services_health.py`, `tests/api/test_ready.py` and `test_api_readiness_postgres.py`, all unchanged). Both keep 1,156 data rows (1,157 physical lines including the header, parsed as CSV) with identical key sets, consistent with how 9B-1 to 9B-4 were handled. *(Wording corrected in 27.2: this sentence originally said "1,157 rows", which counted the header.)*

### 26.7 Deferred

Unchanged and not begun: durable, backup-consistent comparison-baseline capture and storage; a real destination, certificates and secret store; an off-host archive store; any installed, scheduled or monitored backup job (no backup-job work was started); the operator import entry point; pooling and timeouts; the application writer role; application cutover; Streamlit access control (9C). Overall Phase 9B remains incomplete.


## 27. Phase 9B-4 cleanup correction and the backup artifact contract proposal (2026-10-02)

**Scope.** (a) A narrow correction to the 9B-4 rehearsal's cleanup, implemented and verified; (b) a documentation-only proposal for the next Phase 9B unit. Synthetic and disposable resources only; uncommitted when written; no production code, migration, dependency, identity rule, formula, notebook or export changed, `alembic/env.py`'s approved percent-escaping fix is untouched, and no backup job, scheduler, storage integration or infrastructure was created. The earlier identity-test failure (26.5) is **unchanged and unresolved**; no further identity experiments were run and no matching rule was touched.

### 27.1 Cleanup correction (implemented, tested, verified)

- **Certificate directories are registered immediately.** `launched_cluster` now takes the ownership ledger and registers each generated certificate directory the moment it exists, before any later setup step (starting the container, injecting material, waiting for readiness) can fail; the container id is registered as soon as it is known. Previously only the archive directory was registered and the certificate directory was deleted with `ignore_errors=True` and never verified. `launched_cluster`'s own removal remains as a convenience; the ledger's verification is the guarantee. Residual limit: a container whose creation fails before Docker returns an id is not visible to the ledger (the testcontainers reaper still removes it).
- **Cleanup errors cannot replace a primary failure.** The ledger now attempts removal of every registered directory and inspection of every registered container and directory independently; a removal error, an inspection error (for example an unreachable Docker daemon), or a leak is collected, attached as a note to the failure already propagating, and never replaces it. With no primary failure, all problems are reported together in one `AssertionError` that says cleanup "failed or could not be verified".
- **Three focused tests** (the module is now 36 tests, was 33): certificate-directory removal is verified when setup fails (a simulated `start` failure; the directory is registered and gone and no notes are attached; a second case makes removal silently do nothing and shows the ledger still verifies and reports the surviving directory on the original failure); a cleanup inspection or removal exception preserves the original exception while every other resource is still inspected or removed, and is reported clearly when there is no primary failure; normal cleanup succeeds and inspects every resource. The 9B-2 Docker and TLS rejection coverage was not duplicated.
- **Sensitivity checks.** Removing the registration line made exactly the setup-failure test fail; removing the exception handling around inspection made exactly the inspection test fail; both were restored.
- **Verified.** `python -m pytest tests/postgres/test_postgres_fresh_cluster_recovery.py -q -m postgres -rP`: **36 passed**, 0 skipped, 56.0 s in the working tree and **36 passed**, 0 skipped, 41.1 s in a clean worktree of revision `373b35452bb47f71d5f85cbca1e0624a9e4139f3` plus the intended uncommitted files (no `PG*` or `NAPSEDGE_DATABASE_*` variable set; JUnit: 36 tests, 0 failures, 0 errors, 0 skipped in both). Full output and JUnit were retained outside the repository. No container, volume change, temporary certificate or archive directory, or worktree remained afterwards. `tests/postgres` now collects **350** tests (347 + 3); this is a new count and every earlier recorded count remains the result of its own tree. No shared file changed, so the wider selections were not re-run. Every existing caller of `launched_cluster` (the rehearsal fixture, the two negative-path fixtures and the populated-destination test) was updated mechanically to pass the ledger and still passes.

### 27.2 Manifest wording reconciled

Parsed as CSV, `docs/function_manifest.csv` and `docs/notebook_src_coverage.csv` each have **1,156 data rows** (1,157 physical lines and records including the header) with identical key sets. The "1,157 rows" in 26.6 counted the header and is corrected there; no manifest entry changed.

### 27.3 The backup artifact contract proposal (documentation only; not approved, not implemented)

`docs/backup_artifact_contract_proposal.md` was written after reviewing 22.5. It defines the artifact as one directory (`archive.dump`, `baseline.json`, `manifest.json`, and a `COMPLETE` marker written last); identity and integrity fields (source `system_identifier`, database name and OID, UTC capture times, snapshot Alembic head, repository revision, digests); a validity predicate and failure states so a partial artifact can never look valid; how the archive and baseline describe the same state under concurrent writes (an exported `REPEATABLE READ` snapshot shared by the baseline and `pg_dump --snapshot`, with a **monotonic, not equal,** rule for sequences because sequence values are not transactional, and quiescence as an explicit alternative); credential handling, bounded execution and overlap prevention (reusing the import safety layer's verified-target and advisory-lock pattern); and the later storage, retention, monitoring and restore-verification requirements. It also corrects 22.5's stale "member of `pg_read_all_data`" wording (superseded by the explicit grants of 9B-2). The PostgreSQL behaviors it relies on are established product behavior that the implementation unit must re-verify empirically; none is claimed as proven here.

- **Smallest subsequent implementation (proposed 9B-5; narrowed in section 28):** the contract as code with no execution -- manifest and baseline schemas with canonical serialization and digests, the completion-marker writer, and `verify_artifact_directory()` with default-suite unit tests on synthetic artifact directories. No database, `pg_dump`, Docker, scheduler or storage.
- **Decisions requiring Anthony** (proposal section 9 in revision 1; section 11 in revision 2, see 28.3): the consistency policy; where the contract code lives (a production-code authorization); the recovery-point objective, schedule and retention values; and any storage destination, key custody or spend.

### 27.4 Status and remaining work

**Implemented and verified:** the cleanup correction (27.1). **Documented, not implemented:** the artifact contract (27.3). **Unresolved:** the intermittent failure of `test_ambiguous_team_query_raises_identity_conflict_error_against_real_postgresql` (cause unknown; it did not recur in the 26.5 replay). **Deferred, unchanged:** durable baseline capture and storage, a real destination and secret store, an off-host archive store, any installed or scheduled backup job, the operator import entry point, pooling and timeouts, the application writer role, application cutover and Streamlit access control (9C). Overall Phase 9B remains incomplete.


## 28. Phase 9B-5 contract review and specification finalization (2026-10-02)

**Scope.** Documentation only: `docs/backup_artifact_contract_proposal.md` was revised (revision 2) and finalized as a specification for review. **No artifact contract, backup job, scheduler, storage integration, database job or infrastructure was implemented or created**, no test was run in this unit, and nothing was staged or committed. The earlier identity-test failure (26.5) is unchanged and **unresolved**. Overall Phase 9B remains incomplete.

### 28.1 What the specification now fixes (specified, not implemented)

- **Four separate claims** that `COMPLETE` must not blur: files complete (writer finished finalization); structurally and checksum verified (a repeatable verifier result); database consistency established (a claim by the capture procedure, with recorded evidence); successful restore verified (a separate, immutable `verification.json`). `COMPLETE` means the first only.
- **Exact schemas.** `manifest.json` (`napsedge.backup-artifact/1`) with identity, UTC timestamps, provenance, a file inventory with byte sizes and SHA-256 digests, and the consistency evidence; `baseline.json` (`napsedge.backup-baseline/1`) that is the existing `DatabaseBaseline` field for field under the same names plus a `sequence_details` block; a `COMPLETE` marker that binds the manifest's digest and size; strict canonical JSON (no duplicate or unknown keys, no floats, fixed timestamp pattern, size bounds); and a finalization order whose interruption is classified at every step.
- **An ordered validity predicate** with reason codes covering malformed JSON, duplicate keys, unsupported versions, missing files, mismatched digests and sizes, unsafe paths and symlinks, conflicting markers and interrupted finalization. A partial directory is never adopted.
- **A precise consistency design.** One snapshot-owning connection exports a `REPEATABLE READ` snapshot, reads the baseline, and outlives `pg_dump --snapshot`; non-snapshot reads precede the dump; schema, ownership and ACL observations (which use current-catalog functions) are protected by a required before/after catalog bracket and an optional lock layer whose availability risk is named.
- **An exact sequence rule.** NapsEdge's 13 sequences (all `BIGSERIAL id` columns) are read directly as `(last_value, is_called)`; the rule requires applicability (R0), no regression against the baseline (R1), **next-ID safety computed on the restored table (R2)**, and headroom (R3). The document states that monotonic comparison alone does **not** prove next-ID safety, and covers `is_called`, increments, caching, resets, cycling and manually supplied ids. The strict recovery comparator is unchanged.
- **A bounded, disposable validation plan** (ten deterministic cases, one cluster, two capped writers, about three minutes) contrasted with the explicit and weaker requirements of quiescent capture. **It has not been executed.**
- **The smallest next code unit:** the file contract only -- `src/deployment/backup_artifact.py` (standard library only) with default-suite tests in `tests/regression/`, and a test-layer drift guard and adapter tying the schema to `DatabaseBaseline`. Capture and comparison helpers are **not** promoted into production, and no second baseline representation or normalization rule is introduced.

### 28.2 Evidence status

**Verified in this repository (project facts):** offline migration SQL (no database connection) shows 24 tables and 13 sequence-bearing columns, all `BIGSERIAL id`, with no identity columns, no `CYCLE`, no `CACHE` clause and no explicit sequence DDL; an unfiltered static search of `src/` and `alembic/` finds no `setval`, `ALTER SEQUENCE`, `RESTART IDENTITY`, `nextval` or `CREATE SEQUENCE`; `src/deployment/` is an existing package whose tests live in `tests/regression/test_deployment_*.py`; the dependencies needed are in the standard library. **Not verified (PostgreSQL behavior the specification relies on, to be established by the 28.1 validation plan before any operational use):** snapshot export and import semantics and the exporter's required lifetime, which observations are and are not snapshot-governed, the lock-queue behavior of a pending `ACCESS EXCLUSIVE`, advisory-lock execution by the backup role, and the sequence behaviors. Until that plan has run and been reviewed, claim 3 may not be asserted for any real artifact.

### 28.3 Unresolved decisions and remaining work

**Decisions requiring Anthony** (proposal section 11): (1) consistency policy -- shared snapshot with the catalog bracket (recommended), quiescent only, or both; (2) whether to accept optional DDL locks during capture, which can delay migrations and, if one queues, stall reads; (3) authorization for production code limited to the file contract; (4) whether the test-oriented `max_alias_id` is carried 1:1 or later replaced by the general `column_max`; (5) recovery-point objective, schedule, retention, and any storage destination, key custody or cost (not needed for the file contract).
**Readiness.** The specification is ready for review. The file-contract unit is ready to implement once decision 3 is given (and 4 acknowledged); the capture unit is **not** ready until the validation plan has been executed. **Update (Phase 9B-5A, section 29):** the read side of the file contract has since been implemented; the write side, capture and the sequence rule are not.
**Unchanged and open:** durable baseline capture and storage, a real destination, certificates and secret store, an off-host archive store, any installed or scheduled backup job, the operator import entry point, pooling and timeouts, the application writer role, application cutover and Streamlit access control (9C); and the unresolved identity-test failure. Manifests were not touched (1,156 data rows each, parsed as CSV).


## 29. Phase 9B-5A: the artifact file contract, read side (2026-10-02)

**Scope.** The read side of the backup artifact file contract: `src/deployment/backup_artifact.py`, `tests/regression/test_deployment_backup_artifact.py`, a test-layer adapter (`tests/postgres/baseline_document.py`) and one live adapter check (`tests/postgres/test_postgres_baseline_document.py`). **No marker writer, finalizer, `FAILED` writer, capture, SQL, subprocess, database connection, scheduler or storage integration was implemented**, the sequence rule is not evaluated, and `alembic/env.py`'s approved percent-escaping fix, the comparator, `max_alias_id`, migrations, dependencies and the notebooks are untouched. Uncommitted when written; nothing was staged. Overall Phase 9B remains incomplete, and the earlier identity-test failure (26.5) is unchanged and **unresolved**.

### 29.1 Status

**Implemented:** typed manifest and baseline documents, strict JSON parsing and deterministic canonical serialization, completion-marker parsing, and `verify_artifact_directory()`, which returns structural and integrity status with actionable reason codes and states in the verdict itself that it does **not** establish database consistency, a successful restore, or authenticity. **Tested:** 475 new regression tests (469 passed, 6 skipped), a live adapter check (1 passed), and a mutation check (seven of eight deliberate defects caught; the eighth is an equivalent mutant). **Verified:** in a clean worktree of `373b354` plus the intended files, 631 passed, 6 skipped, 0 failed across the default-suite selection (new module, comparator unit tests, DSN regression, four existing deployment test files), and the live adapter check passed; the existing deployment checks also passed (114) in the working tree. **Skipped, exactly:** 6 real-symlink tests, because this user cannot create symbolic links; the Windows junction (reparse point) case ran. **Deferred:** everything on the write and capture side, the section 7 validation plan (not run), storage, retention, monitoring, scheduling, restore-verification records. **Correction (section 30):** two defects in the verifier were later reproduced and corrected; the wording above that links and symlinks were handled, and the skip list, describe the first delivery's tree and are superseded where section 30 says so.

### 29.2 Specification corrected

`docs/backup_artifact_contract_proposal.md` is now revision 3: the JSON value rule permits objects, arrays, strings, exact integers, booleans and documented nulls and rejects floats and non-finite values; `RUNNING` is removed before `COMPLETE` and before publication; endpoint session counts are stated not to establish continuous quiescence; the catalog bracket and the concurrent-sequence policy remain **proposed and unvalidated**, and passing the selected validation cases is stated not to prove safety against all unmanaged changes. Section 12 of that document is the implementation record.

### 29.3 Baseline compatibility

The adapter was written against shapes obtained by inspecting real captures, not assumed: `fingerprints` is a **dictionary** `{table: (rows, md5)}` whose md5 is 32 hex digits or the literal `empty`; the other comparison fields are lists of tuples; sequences' last values, `max_alias_id` and `version` can be null. The dictionary maps to a list sorted by table name with an explicit reverse mapping. A genuine capture round-trips through the production schema to an identical `DatabaseBaseline`, and the unchanged comparator reports no difference on the deserialized baseline. No database-capture or comparison helper was promoted into production.

### 29.4 Manifests

Reviewed by the established convention (9A, section 8): the manifests are keyed by real notebook function name and a symbol with no notebook origin gets no row; only a bare-name collision updates an existing row. A collision check of all 50 public top-level symbols of the new module found none, so no entry changed: both manifests keep **1,156 data rows** (1,157 physical lines including the header), identical key sets.

### 29.5 Remaining work and decisions

*[Historical to 9B-5A; the local write side was implemented in 9B-5B, section 31.]* Not begun: the write side of the file contract (marker writer, finalizer, `FAILED` writer); the section 7 validation plan; capture; the sequence rule; and the decisions for Anthony that remain open (consistency policy, DDL exclusion during capture, recovery-point objective, schedule, retention, storage and key custody, and cost). Committing the 9B-1 through 9B-5A batch needs Anthony's explicit authorization.


## 30. Phase 9B-5A verifier correction: artifact-path links and schema provenance (2026-10-02)

**Scope.** A focused correction of `src/deployment/backup_artifact.py` and its tests after two defects were independently reproduced against the first delivery. Phase 9B-5B was **not** begun: no writer, finalizer, `FAILED` writer, capture, scheduler or storage integration exists. The comparator, identity rules, notebooks, formulas, exports, migrations, dependencies and `alembic/env.py`'s percent-escaping fix are untouched, the unresolved identity-test failure (26.5) is unchanged, and nothing was staged. Overall Phase 9B remains incomplete.

### 30.1 The defects and the corrections

| Defect | Reproduced | Correction |
|---|---|---|
| Artifact-root link bypass: a link named like an artifact verified as valid when spelled `link/` or `link/.`, and links in ancestors were never checked | **Linux (real symlinks):** `link/`, `link//`, `link/.`, `link/./` and similar accepted; link beneath a linked parent accepted. **Windows (NTFS junctions):** all root variants already rejected (the bypass is POSIX-specific), but a linked parent was **accepted** | the path is validated component by component with no normalization first; any link or reparse point in the artifact directory or any ancestor is `SYMLINK`; rejected paths reach no directory listing and no content read |
| Schema provenance mismatch: `baseline.version` `ffffffffffff` beside manifest head `5b02bea0eb85` with recomputed digests and marker verified as valid | both platforms | `BASELINE_SCHEMA_VERSION_MISMATCH` when `baseline.version` is null or differs from `schema.alembic_head`; `expected_head` deliberately not compared; standalone baseline parsing and the adapter's null round trip unchanged |

The first delivery's wording that the verifier never follows symbolic links and rejected links was incomplete; specification 5.1 and section 12 supersede it (and the "Skipped, exactly" statement in 29.1 is historical for that tree).

### 30.2 Verified

Windows (real junctions): module 505 passed, 7 skipped (512 collected). Linux (real symlinks, disposable network-less container from the project's own local image; repository mounted read-only; `pytest` and its pure-Python dependencies copied from this machine's site-packages into a read-only scratch mount; nothing pulled, installed or modified): module 507 passed, 1 skipped (508 collected). Clean worktree of `373b354` plus only intended files: Windows default-suite selection 667 passed, 7 skipped (JUnit 674 tests, 0 failures); Linux 555 passed, 1 skipped (JUnit 556: module, comparator unit tests, DSN regression); live adapter check 1 passed. Output and JUnit were retained outside the repository. **Skips, exactly:** Windows -- six file-level symlink tests (this user cannot create symlinks) and one POSIX-only test; Linux -- one Windows-only junction test. Run against the unmodified module the new tests fail (33 on Windows, 29 on Linux); the mutation check caught every mutation of the new code except one that is not exploitable on Windows. Container runs execute as root, so permission-based read-only checks there are not evidence of enforcement.

### 30.3 Remaining limitations (stated, not claimed away)

The path walk is a check, not a lock: a component swapped for a link after its `lstat` and before the later reads is not prevented, and `O_NOFOLLOW` covers only the final component (and does not exist on Windows). An artifact store that legitimately sits behind a link or junction is rejected; accepting one needs a separately decided trusted-root mechanism. Case-insensitive aliasing, 8.3 names and alternate data streams are not specially handled. On Windows only NTFS junctions were exercised for real; Windows symbolic links were not creatable here. The schema check compares one field pair and nothing more; it does not establish database consistency or restore success. **Manifests** reviewed by the established convention: the new helpers are private, the 50 public symbols are unchanged, no collision, so no entry changed (1,156 data rows; 1,157 physical lines including the header).

### 30.4 Next step

The acceptance criteria for this correction pass, so Phase 9B-5B (the write side of the file contract) may be recommended; it was not begun by this correction. *(Superseded by section 31: 9B-5B was subsequently authorized and implemented without a commit as a prerequisite; committing the batch remains a separate decision for Anthony.)* The section 7 validation plan remains separately required before any capture code.


## 31. Phase 9B-5B: the local write side of the artifact contract (2026-10-02)

**Scope.** `src/deployment/backup_artifact_writer.py` and `tests/regression/test_deployment_backup_artifact_writer.py`, plus a behavior-preserving split of `src/deployment/backup_artifact.py` (`_verify_contents`, `_manifest_baseline_relations`) so the writer validates staged contents with the verifier's own logic. File operations only, on a supplied local archive and caller-supplied baseline and metadata. No capture, SQL, `pg_dump`, snapshot, sequence evaluation, scheduler, retention, external storage, real-system operation, staging, commit, push, deployment or spending, and no existing Docker volume was touched. The comparator, identity rules, notebooks, formulas, exports, migrations, dependencies and `alembic/env.py`'s percent-escaping fix are untouched, and the unresolved identity-test failure (26.5) is unchanged. This unit was authorized directly; **no commit was a prerequisite.** Overall Phase 9B remains incomplete.

### 31.1 Acceptance criteria and where each is met

| Criterion | Result |
|---|---|
| Success passes the unchanged public verifier; exactly four files; no `RUNNING`/`FAILED` | tested (Windows and Linux) |
| Input archive and unrelated directories unchanged | tested (name/content snapshots) |
| Invalid metadata, collisions and injected failures cannot report success | tested: 18 injected faults, 5 destination shapes, 3 partial shapes, and destinations appearing during the write, invalid-input table |
| Existing destination (including an empty directory) never replaced; no cross-filesystem fallback | tested; POSIX exclusive reservation killed by the mutation check on Linux |
| Durability distinguished (file flush, directory flush, atomic visibility); never silently skipped | tested; `REQUIRED` refuses on Windows, `BEST_EFFORT_REPORTED` reports gaps |
| Failure after the rename is separate: nothing deleted, published state reported | tested (`PostPublicationError`, `published` yes/unknown) |
| Bounded `FAILED` record; original failure preserved; failed partials never adopted | tested |
| Results establish file completion and integrity only | asserted in the result and the tests |

### 31.2 Verified

Exact command `python -m pytest tests/regression/test_deployment_backup_artifact_writer.py tests/regression/test_deployment_backup_artifact.py -q`: Windows **590 passed, 11 skipped** (601); Linux (real symlinks and directory flushes, disposable network-less container) **595 passed, 2 skipped** (597). Clean worktree of `373b354` plus only intended files: Windows default-suite selection **752 passed, 11 skipped** (JUnit 763, 0 failures); Linux **643 passed, 2 skipped** (JUnit 645); live adapter check **1 passed**. Mutation check of the writer: 11 of 11 caught on Linux, 10 of 11 on Windows (the eleventh is a POSIX-only code path). Commands, output and JUnit were retained outside the repository. **Skips, exactly:** Windows -- 1 POSIX permission-mode, 1 POSIX-only non-private-staging, 7 file-level symlink (cannot create symlinks), 1 POSIX-only publication-order, 1 POSIX-only `lstat` trailing-separator; Linux -- 1 Windows-only statement and 1 NTFS-junction. Container runs as root, so permission-based checks there are not evidence of enforcement. Specification 12.1 has the full account, including the three mutants that first survived and the one unreproduced snapshot-mtime observation (a test-helper fragility on NTFS, not a proven writer defect).

### 31.3 Unsupported and deferred

**Unsupported:** directory flushes on Windows through the standard library; POSIX privacy modes on Windows; network and removable filesystems (not tested); survival of power loss (fault injection does not prove it); cross-filesystem stores; non-cooperating writers; adoption, resumption, repair or automatic cleanup of partials (and of the empty reservation a crash can leave); checking the archive's table-of-contents fields; authenticity. **Deferred:** capture and `pg_dump`, snapshots, the section 7 validation plan (not executed), the sequence rule, scheduling, retention, external storage, restore-verification records, a trusted-root mechanism for linked stores, and the decisions in specification section 11. **Manifests:** no collision among the writer's 22 public symbols, so no entry changed (1,156 data rows; 1,157 physical lines).

### 31.4 Next step

The separate, disposable **capture-consistency validation** (the specification's section 7 plan, still unexecuted) is the next unit and is required before any capture code is written. It needs its own authorization. Committing the 9B-1 through 9B-5B batch is a separate decision for Anthony and is **not** a prerequisite for that unit.


## 32. Phase 9B-5B correction: descriptor closure (2026-10-02)

**Scope.** A narrow correction of `src/deployment/backup_artifact_writer.py` and its tests. Local implementation and targeted tests only; no database capture, scheduling, storage integration, deployment, staging, commit or push, and no commit was a prerequisite. The reader module, canonical documents, marker ordering, collision protections, shared verification rules, durability modes, comparator, identity rules, notebooks, exports, migrations, dependencies and unrelated modified files are untouched, and the unresolved identity-test failure (26.5) is unchanged. Overall Phase 9B remains incomplete.

### 32.1 Acceptance criteria and where each is met

| Criterion | Result |
|---|---|
| A primary failure keeps its type, stage, reason class and causal chain when a close also fails | tested: four primary-plus-close combinations (cause compared by identity), `PreconditionError`, `PostPublicationError` |
| Secondary cleanup information is sanitized (type and errno only; no messages, credentials or source content) | tested with planted credential-like text in every carrier |
| `KeyboardInterrupt`/`SystemExit` stay the propagating exception; an interrupt raised by a close is not swallowed | tested (2 types x 2 descriptors, plus the close-raised interrupt) |
| A close-only failure never returns success and never escapes as a plain `OSError` | tested for the source and each of the five staged files |
| After publication, `PostPublicationError` keeps the right status; the published artifact is untouched | tested; contents compared byte for byte with a control run and re-verified |
| Exactly one close attempt per owned descriptor, never retried | tested through descriptor-ownership tracking on every run |
| `fsync_directory()` audited; unsupported vs failed preserved | audit found the same defect; corrected; tested on every platform and, on POSIX, with real descriptors |
| `FAILED` stays best effort and cannot replace the primary | tested |

### 32.2 Defects reproduced and behavior corrected

(1) An `EIO` closing the source after otherwise successful processing raised a plain `OSError` while the final artifact existed and verified as valid; publication status was lost. (2) An archive flush failure followed by a close failure replaced the flush failure, so the cause chain and the `FAILED` classification followed the close error. (3) Audit: `fsync_directory()` let a close failure replace `DurabilityUnsupported` or a real flush failure (reproduced on Linux). **Corrected:** the source is closed before publication; every descriptor gets one close attempt; a close-only failure is a structured failure; a close failing under another failure is attached as a sanitized note and in `cleanup_failures`. Details: specification section 12.2.

### 32.3 Verified

Exact command `python -m pytest tests/regression/test_deployment_backup_artifact_writer.py tests/regression/test_deployment_backup_artifact.py -q -ra`: Windows **619 passed, 13 skipped** (632 collected); Linux (disposable network-less container, real symlinks and directory flushes) **626 passed, 2 skipped** (628). The same selection in a clean worktree of `373b354` with only the six files it needs: Windows 619 passed, 13 skipped; Linux 626 passed, 2 skipped. Run against the unmodified writer the module fails 28 tests on Windows and 30 on Linux. Mutation check of the new code: 12 of 12 caught on both platforms. Commands, output and JUnit were retained outside the repository. **Skips, exactly:** Windows -- 1 POSIX permission-mode, 1 POSIX-only non-private-staging, 7 file-level symlink (cannot create symlinks), 1 POSIX-only publication-order, 2 POSIX-only real-directory-descriptor, 1 POSIX-only `lstat`; Linux -- 1 Windows-only statement and 1 NTFS-junction. Container runs as root. PostgreSQL batteries, the comparator and DSN tests, the live adapter and the identity test were not re-run. The figures in sections 30 and 31 are historical to their trees.

### 32.4 Remaining limitations and deferred work

A second interrupt raised by a close during cleanup propagates and may leave the partial without a `FAILED` record (it keeps `RUNNING` and stays invalid). Fault injection shows how the writer handles a reported close failure, not how a filesystem behaves. Windows still cannot flush directories through the standard library. **Deferred, unchanged:** the section 7 capture-consistency validation (**capture consistency remains unvalidated**, and is required before any capture code), the sequence rule, scheduling, retention, external storage, restore-verification records, a trusted-root mechanism for linked stores, and the decisions in specification section 11. **Manifests:** the writer's 22 public top-level symbols are unchanged, no collision, no entry changed (1,156 data rows; 1,157 physical lines).


## 33. Phase 9B-5C1: disposable validation of shared-snapshot table data and snapshot-owner lifetime -- C1, C2, C8 (2026-10-02)

**Scope.** Test-layer implementation and execution against one disposable, synthetic PostgreSQL 16 cluster only. Specification section 7 cases C1, C2 and C8. No production capture, scheduler, retention, storage, monitoring or job entry point exists or was written; the artifact schemas, writer, reader, production code, formulas, identity rules, notebooks, exports, migrations and dependencies are unchanged; the unresolved identity-test failure (26.5) is unchanged; nothing was staged; no commit was a prerequisite. The shared helper `tests/postgres/pg_restore_compare.py` gained connection-aware reads. Overall Phase 9B remains incomplete.

### 33.1 Acceptance criteria and results

| Criterion | Result |
|---|---|
| All baseline table-data reads run through the snapshot-owning REPEATABLE READ, READ ONLY transaction | **passed**: `capture_baseline(Connection)`; the owner's engine opened no further connection; the owner re-read after the commit equals the baseline while a fresh transaction differs. Finding: `capture_baseline(Engine)` opens a connection per read and shares no snapshot |
| C1: exact table-data parity under a post-snapshot commit | **passed**: restored fingerprints equal the baseline for every table; the change is absent; the owner's visibility descriptor never changed; the exported id and the descriptor were kept distinct |
| C2: deterministic, explained mismatch | **passed**: differing tables exactly `teams` (+1 row) and `conferences` (content), change present after restore, restored equals the fresh committed state; the only command-line difference from C1 is `--snapshot` |
| C8: confirmed snapshot-import failure, downstream prevented | **passed**: owner confirmed absent, `pg_dump` exit 1 with `invalid snapshot identifier` and no authentication, connectivity or permission cause, a live-owner control succeeded, outcome `snapshot_unavailable`, no destination, restore, validation or `COMPLETE`; the leftover file was refused by the archive gate |
| Cleanup verified on success and failure; no unexpected skips; no credential-bearing evidence | **passed**: per-case ledger verification, a deliberate mid-case failure and a deliberate leak both tested; 0 skipped; evidence scanned |

### 33.2 Verified

Command `python -m pytest tests/postgres/test_postgres_backup_snapshot_consistency.py -q -m postgres -ra`: **42 passed, 0 skipped** in the working tree and in a clean worktree of `373b354` plus the files it needs (about 23 s, exit code 0; output, exit codes and JUnit retained outside the repository). Because the shared helper changed: `tests/regression/test_pg_restore_compare_unit.py` **30 passed** (28 before); live adapter check **1 passed**; `tests/postgres/test_postgres_roles_and_restore.py` **33 passed**; the 9B-4 rehearsal comparison test **1 passed**. Harness mutation check: 8 of 8 caught. The full PostgreSQL battery, the 9B-2 TLS module, the rest of the 9B-4 module and the identity test were not re-run. Earlier counts in this document are historical to their trees. Details, discrepancies and limits: specification section 12.3.

### 33.3 What this does and does not establish

It establishes, for table data on PostgreSQL 16 with default settings and one small committed change: the shared-snapshot mechanism works, a dump that does not import the snapshot is detectably different, and an unavailable snapshot fails loudly and is gated. It does **not** establish sequence safety, catalog safety, DDL or lock behavior, overlap handling, quiescent-mode behavior, behavior under sustained concurrent writes (one writer, one small change), `pg_dump -j`, other PostgreSQL versions, restore verification (claim 4) or operational readiness.

### 33.4 Remaining work

**Unvalidated and unexecuted:** C3 (sequence behavior), C4 (manual-id hazard), C5 (rule applicability), C6 (catalog bracket), C7 (lock mode), C9 (overlap), C10 (quiescent mode). **Not implemented:** production capture, the sequence rule, scheduling, retention, external storage, monitoring, restore-verification records. **Manifests:** the production modules were not touched and the test layer has no manifest rows, so no entry changed (1,156 data rows; 1,157 physical lines). **Next step:** none begun; a separately authorized unit for the remaining cases (C3 to C5, the sequence cases, are the natural next group) is recommended before any capture code. Committing the batch is a separate decision for Anthony and not a prerequisite.


## 34. Phase 9B-5C2: disposable sequence validation -- C3, C4, C5 (2026-10-04)

**Scope.** Test-layer implementation and execution against one disposable, synthetic PostgreSQL 16 cluster only. Specification section 7 cases C3, C4 and C5. No production capture, production sequence verifier, scheduler, retention, storage, monitoring or job entry point exists or was written; the artifact schemas, reader, writer, `pg_restore_compare.py` and its strict comparator, production code, formulas, identity rules, notebooks, exports, migrations and dependencies are unchanged; the unresolved identity-test failure (26.5) is unchanged; nothing was staged; no commit was a prerequisite. The C1 harness gained a pluggable writer action and two extracted functions (behavior-preserving). Overall Phase 9B remains incomplete.

### 34.1 Acceptance criteria and results

| Criterion | Result |
|---|---|
| All baseline reads (data, then sequences) through the snapshot owner; sequence state read directly as the backup role, with existing grants only; `pg_sequences.last_value` kept separately; baseline before `pg_dump` | **passed**: the owner's engine opened no further connection; `SequenceDetail` is the one representation (round-trips through `parse_baseline`); NULL for an uncalled sequence kept as NULL beside the direct value 1 |
| Inventory and owning columns discovered from the migrated database and verified against the documented schema | **passed** in every scenario: 13 sequences, `<table>_id_seq` owned by `id`, default bigint / increment 1 / cache 1 / no cycle (except the deliberately altered one) |
| C3: advancement after the baseline read and before the dump; exact data parity; N_r > N_b; R0-R3 from observed values; unchanged strict comparator reports the difference; no-advancement control; uncalled coverage; rollback example | **passed**: team_aliases 3 to 8 (N 4 to 9), data_quality_issues uncalled (1, false) to (1, true) (N 1 to 2); strict difference exactly those two; control equal; nine live uncalled sequences; rollback left a gap (next 5, column maximum 3) with R1-R3 true |
| C4: manual id above the sequence; parity and R1 true; R2 false from the restored maximum; unsafe condition reported, not repaired; no collision claim | **passed**: column maximum 1,000,000 against N_r = 4; only that sequence fails R2; the restored generator is exactly as dumped |
| C5: CYCLE and negative increment rejected by R0 in the harness path before any dump; no acceptance computed; positive control; R3 informational | **passed**: `cycles` and `increment_not_positive`, only the altered sequence; no dump, destination, restore or command; control accepted; R3 not turned into a policy |
| No `nextval`/`setval`/repair by verification; sensitivity of the tests | **passed**: static guard plus a second restored read; 9 of 9 mutants caught |

### 34.2 Verified

Command `python -m pytest tests/postgres/test_postgres_backup_sequence_consistency.py -q -m postgres -ra`: **68 passed, 0 skipped, 0 warnings** in the working tree and in a clean worktree of `373b354` plus the files it needs (about 38 s, exit code 0; output, exit codes and JUnit retained outside the repository). The shared C1 harness changed, so `test_postgres_backup_snapshot_consistency.py` was rerun: **42 passed** in both. `pg_restore_compare.py` did not change, so its 30 unit tests and the older integration checks were not re-run. Sensitivity check: 9 of 9 caught (missing advancement, `is_called` ignored, R2 omitted, CYCLE ignored, negative increment ignored, guard never called, R1 inverted, R2 on the baseline maximum, verification that draws from a sequence). Cleanup verified for every scenario; the existing Docker volume untouched; no credential in the evidence. The earlier counts in this document are historical to their trees. Details, discrepancies and limits: specification section 12.4.

### 34.3 What this does and does not establish

It establishes, for the 13 default sequences on PostgreSQL 16 with one bounded writer: a sequence advanced after the baseline read is carried into the archive (N_r > N_b) while table data stays at the snapshot, the strict equality comparison therefore cannot apply under concurrent capture and reports the difference unchanged, R2 detects a manual-id hazard that R1 misses, and R0 refuses CYCLE and negative increments before any dump. It does **not** establish behavior under sustained concurrent draws, non-default sequence parameters or cache values, crashes, `setval`/restart/truncate operations, catalog, DDL, lock, overlap or quiescent behavior, restore verification (claim 4), or production readiness. The `monotonic_v1` rule remains proposed.

### 34.4 Remaining work

**Unvalidated and unexecuted:** C6 (catalog bracket), C7 (lock mode), C9 (overlap), C10 (quiescent mode). **Not implemented:** production capture, the production sequence verifier and rule, scheduling, retention, external storage, monitoring, restore-verification records. **Manifests:** the production modules were not touched and the test layer has no manifest rows, so no entry changed (1,156 data rows; 1,157 physical lines). **Next step:** none begun; a separately authorized unit for the remaining cases (C6 and C7 together, then C9 and C10) is recommended before any capture code. Committing the batch is a separate decision for Anthony and not a prerequisite.


## 35. Phase 9B-5C3: catalog observations and locking -- C6, C7 (2026-10-04), and the 9B-5C2 export repair

**Scope.** Test-layer implementation and execution against one disposable, synthetic PostgreSQL 16 cluster, plus a repair of the Phase 9B-5C2 review export. No production capture, production lock policy, scheduler, retention, storage, monitoring or job entry point exists or was written; the backup role's grants, the artifact schemas, reader, writer, `pg_restore_compare.py` and its strict comparator, production code, formulas, identity rules, notebooks, exports, migrations and dependencies are unchanged; no shared harness file changed; the unresolved identity-test failure (26.5) is unchanged; nothing was staged; no commit was a prerequisite. Overall Phase 9B remains incomplete.

### 35.1 The 9B-5C2 export repair

Section 2 of the 9B-5C2 review attachment contained an `IndexError` traceback instead of the observed sequence values: the extraction filter also matched two pure tests whose names contain `observed_values`. The evidence was recovered **from the existing retained JUnit without rerunning any test** by a structural exporter (anchored testcase-name patterns, name/value properties, named-group parsing; no positional indexing) that **fails if anything required is missing**: all six scenario records, all 13 sequence observations in each (including the C5 rejection information), cleanup results, strict sequence differences, table-data differences and the recorded rule outcomes, with the source JUnit file name and SHA-256 (`5080ffd2d8e1f3054bcd93adcfa51e9aa474b71ebe3db7bcc6b5d6871113e16f`). The broken attachment is preserved unchanged as historical evidence; the corrected attachment supersedes it. The 9B-5C2 results themselves are unchanged.

### 35.2 Acceptance criteria and results

| Criterion | Result |
|---|---|
| C6: the proposed bracket implemented as written first, through the owner and its actual transaction boundaries; isolated index, ACL and owner scenarios plus a no-change control | done: 7 committing scenarios, 2 blocked, 1 reversal, 1 control, 2 candidate-gate flows |
| Each change proven committed and visible to a fresh observer; blocked or rolled-back changes never count | done: committed changes visible in their own category; 55P03 reported as blocked, with no detection claimed |
| Raw differences and digests recorded separately for constraints, indexes, ACL/ownership | done per endpoint and per variant |
| Does the owner's repeated read detect each committed change? | **No: missed every committed change** (index, ACL, owner; both windows) |
| A missed change keeps the safety requirement unresolved; no claim adjusted | **requirement UNMET**: 7 strict xfail tests; characterization tests retain the blind spot |
| Separate candidate with fresh endpoints, owner untouched, labeled for later design review; refusal exercised in the real flow, finalization prevented | done: detects all; flow stopped before restore and finalization; control passed |
| C7: backup role can take ACCESS SHARE with existing grants; ordinary versus explicit locks observed | done: 24 tables; denied for stronger modes; privileges unchanged; explicit changes WHEN, not WHICH |
| Queued ACCESS EXCLUSIVE confirmed via pg_locks, pg_stat_activity, pg_blocking_pids; bounded; DDL did not commit; reader waits behind the queued request and recovers while the owner stays open; control | done: reader waited 9.83 s against 0.0 s |

### 35.3 Verified

Command `python -m pytest tests/postgres/test_postgres_backup_catalog_consistency.py -q -m postgres -ra`: **120 passed, 7 xfailed, 0 skipped, 0 warnings** in the working tree and in a clean worktree of `373b354` plus the files it needs (about 2 minutes, exit code 0; output, exit codes and JUnit retained outside the repository). The 7 xfailed are the strict, expected failures that state the unmet safety requirement. No shared harness changed, so the snapshot and sequence modules were not rerun. Sensitivity check: 10 of 10 caught. Cleanup verified per scenario including the probe role; the existing Docker volume untouched; no credential in the evidence. The review attachment is generated from the retained JUnit by the same strict exporter. Earlier counts in this document are historical to their trees. Details, the full operations table and limits: specification section 12.5.

### 35.4 Status of the proposal

**Failed as written:** the catalog bracket (section 6.4 layer 1). **Partial:** DDL exclusion by explicit locks (layer 2): blocks ACCESS EXCLUSIVE operations only from the moment the locks are held, does not stop index or ACL changes, and a queued exclusive request stalls ordinary reads up to its lock timeout. **Unreviewed alternatives:** fresh committed endpoints; the owner observing after its transaction ends. **Undetectable by any endpoint comparison:** a change and its reversal.

### 35.5 Remaining work

**Unvalidated and unexecuted:** C9 (overlap) and C10 (quiescent mode). **Needs Anthony's design review before any capture code:** the replacement for the catalog bracket and the lock policy (specification decision 11). **Not implemented:** production capture, the production sequence verifier and rule, scheduling, retention, external storage, monitoring, restore-verification records. **Manifests:** the production modules were not touched and the test layer has no manifest rows, so no entry changed (1,156 data rows; 1,157 physical lines). **Smallest next step:** C9 and C10 (overlap and quiescent mode) as one test-layer unit, with the catalog-bracket redesign decision put to Anthony in parallel. Committing the batch is a separate decision and not a prerequisite.


## 36. Phase 9B-5C4: overlap and quiescence -- C9, C10 (2026-10-05), and a design decision memo

**Scope.** Test-layer implementation and execution against one disposable, synthetic PostgreSQL 16 cluster, plus documentation corrections. No production capture, lock policy, scheduler, retention, storage, monitoring or job entry point exists or was written; no production code (including `bulk_import_safety.py`), grant, schema, migration or dependency changed; no shared test file changed; the unresolved identity-test failure (26.5) is unchanged; nothing was staged; no commit was a prerequisite. **Production capture remains blocked by the unresolved C6 design. Phase 9B remains incomplete.**

### 36.1 Acceptance criteria and results

| Criterion | Result |
|---|---|
| C9: two distinct physical backup-role sessions, PIDs recorded, verified disposable database | **passed** |
| Existing target verification, key derivation and advisory lock reused; no key logic or namespace change | **passed** (static test; production code unchanged) |
| Second session refuses immediately with `SKIPPED:overlap` (a job outcome, not a pytest skip) | **passed**: 0.005 s |
| Refused run never reaches snapshot export, baseline, `pg_dump`, restore or finalization | **passed** in the real flow: 3 statements, 1 connection, no command, no database |
| Fresh session acquires after normal release; exception-path release; session loss; residual locks read from PostgreSQL | **passed** |
| Primary failures preserved | **passed in the harness guard; the production manager then let the release error win -- CORRECTED in the production manager itself (section 37)** |
| Session-level locks vs rollback and pool; physical cleanup verified, not assumed | **passed**: lock survives rollback; `close()` returns a live backend with the lock; `dispose()` alone leaves a checked-out session alive; cleanup verified from PostgreSQL |
| Does the key also exclude controlled imports? | **recorded: yes, both directions** (no policy chosen) |
| Existing grants suffice; none added | **passed** |
| C10 A: no-writer control with strict data and sequence equality | **passed** |
| C10 B: short-lived writer missed by endpoint counts; commit and sequence advancement proven independently | **characterized**: counts 0 and 0, write present in the dump |
| C10 C: retained writer counted; C10 D: positive count is presence, not a write | **characterized** |
| C10 safety requirements ("zero counts imply nothing wrote"; "a positive count implies a write") | **UNMET** (2 strict xfail) |

### 36.2 Verified

Command `python -m pytest tests/postgres/test_postgres_backup_overlap_quiescence.py -q -m postgres -ra`: **53 passed, 2 xfailed, 0 skipped, 0 warnings** in the working tree and in a clean worktree of `373b354` plus the files it needs (about 42 s, exit code 0; output, exit codes and JUnit retained outside the repository). No shared file changed, so no other module was rerun. Sensitivity check: 7 of 7 caught, counted only for expected assertion failures. Cleanup verified per scenario and on a deliberate failure; the existing Docker volume untouched; no credential in the evidence. The review attachment is generated from the retained JUnit by a strict exporter. Earlier counts in this document are historical to their trees. Details and limits: specification section 12.6.

### 36.3 Status, by kind

| Kind | Items |
|---|---|
| **Implemented** (test layer only) | the C1-C10 harnesses and their evidence exporters; the harness guard `run_guarded`; the fresh-transaction foreign-writer sampler |
| **Tested and verified** | shared-snapshot table-data parity (C1, C2); owner loss (C8); sequence behavior and rule R0-R3 (C3-C5); catalog and lock observations (C6, C7); overlap and lock lifecycle (C9); quiescence characterization (C10) -- each for its stated behavior only |
| **Failed** | the catalog bracket as written (C6); endpoint session counts as evidence of quiescence (C10) |
| **Proposed, unvalidated or unreviewed** | `monotonic_v1` as a production rule; the two catalog-observation alternatives; any lock policy; the manifest `consistency` fields as evidence |
| **Deferred / not implemented** | production capture, production sequence verifier, scheduling, retention, storage, monitoring, restore-verification records, any change to `bulk_import_safety.py` beyond the exception-preservation correction (section 37) |

### 36.4 Design decision memo (from C1-C10; nothing here is adopted)

**Observed guarantees** (test layer, PostgreSQL 16, default settings, small synthetic data): (1) a dump that imports the owner's exported snapshot reproduces the owner's table-data baseline exactly while other sessions commit (C1), a dump without it does not (C2), and a lost owner fails loudly and is gated (C8); (2) sequences are never frozen, so the restored next value can exceed the baseline's (C3), R2 detects the manual-id hazard that R1 misses (C4), and R0 refuses CYCLE and negative increments before any dump (C5); (3) the existing advisory lock refuses a concurrent backup immediately and is released on every tested path including session loss (C9).

**Known bypasses and limits:** the catalog bracket as written misses every committed catalog change and no endpoint comparison sees a change and its reversal (C6); ordinary reads take table locks only after the reads, explicit locks block `ACCESS EXCLUSIVE` operations only while held, do not stop index or ACL changes, and a queued exclusive request stalls ordinary reads for up to its lock timeout (C7); writers that do not take the advisory lock (migrations, ad hoc sessions) are not stopped by it, endpoint counts miss short-lived writers and cannot tell presence from a write, and the backup role sees other sessions only through restricted columns (C10); the production lock manager used to let a failed release replace a primary failure (CORRECTED, section 37); a session-level lock still survives rollback and pool return, and `dispose()` alone does not end a checked-out session (C9).

**Policy choices that belong to Anthony** (none made): (1) the consistency policy -- shared snapshot with `monotonic_v1` and a catalog design, quiescent capture, or both (specification decision 1); (2) the catalog-observation design -- fresh committed endpoints, the owner after its transaction ends, or accepted residual risk recorded in the manifest (decision 6); (3) whether to use explicit table locks and what `lock_timeout` the migrator carries (decisions 2 and 7); (4) backup-only versus import-shared advisory key (decision 7; the release-masking defect in `bulk_import_advisory_lock` was corrected, section 37); (5) whether `foreign_write_sessions_*` stays evidence-only and whether the backup role is ever given `pg_read_all_stats` (decision 8).

**Smallest next validation:** once decisions (1) and (2) are made, one test-layer unit that validates the chosen catalog-observation design against what C6 did not exercise: a constraint change (an `ACCESS EXCLUSIVE` `ALTER TABLE ... ADD CONSTRAINT`), changes during the dump itself, and a role rename or drop. Nothing else needs validating before design review. Completing these experiments does not authorize production capture or establish consistency for any artifact. **Executed for the fresh-endpoint candidate in Phase 9B-5C5 (section 38); decisions (1) and (2) are still open.**

### 36.5 Remaining work

**Unvalidated:** the items under "Limits" in specification sections 12.3 to 12.6, notably constraint changes, changes during the dump window and role rename or drop. **Not implemented:** everything listed as deferred above. **Manifests:** the production modules were not touched and the test layer has no manifest rows, so no entry changed (1,156 data rows; 1,157 physical lines). Committing the batch is a separate decision for Anthony and not a prerequisite.


## 37. Phase 9B-5C4 correction: `bulk_import_advisory_lock` exception preservation (2026-10-05)

**Scope.** A narrowly scoped local production-code correction of `src/pipeline/bulk_import_safety.py`, targeted tests, and documentation. No production capture, new lock namespace, grant, deployment, staging, commit or push; formulas, identity rules, notebooks, exports, schema, migrations, dependencies, the lock namespace and the backup role's grants are unchanged; the unresolved identity-test failure (26.5) is unchanged. **C6 remains failed as written; C10's endpoint-count limitations and its two expected failures are unchanged; production capture and Phase 9B remain incomplete.** The catalog-observation redesign is the subsequent unit, not part of this fix.

### 37.1 The defect and the direct before/after evidence

Before (unchanged function, retained): terminating the owning backend inside the protected block and raising a primary `RuntimeError` propagated **`sqlalchemy.exc.OperationalError`** from the unlock (its message embeds the SQL parameters, including the lock key); a body `DataError` was replaced by **`InternalError`** from the aborted transaction; a false unlock result left no record. Of the new tests, 6 of 16 fake-connection tests and 3 of 3 live tests failed. After: the same experiments propagate the original exception object with its cause, notes and traceback and exactly one bounded note (`the advisory lock release also failed (OperationalError); ...`), and all new tests pass.

### 37.2 Acceptance criteria and results

| Criterion | Result |
|---|---|
| Normal body and successful release succeed; unlock attempted exactly once | **passed** |
| Primary failure with a successful release: preserved untouched | **passed** |
| Primary failure with a raised release error: original object, type, cause, existing notes and traceback preserved; one bounded, sanitized note | **passed** (fake connection and real backend loss) |
| Primary failure with a false unlock result: preserved, note recorded | **passed** (fake connection and real backend) |
| Release-only failures stay visible; a refused acquisition never runs the body or unlocks; no retry | **passed** |
| Caller ownership: no commit, rollback, close, invalidation or disposal; signature, exports and nested acquisition unchanged | **passed** |
| Primary KeyboardInterrupt/SystemExit preserved; a new interrupt during cleanup not swallowed | **passed** |
| `run_guarded` consumes the shared behavior; the C9 regression requires the contract from the production manager | **passed** |
| A real backend-loss test through the production manager; physical cleanup and subsequent acquisition verified separately | **passed** |

### 37.3 Implemented, tested and verified (separately)

**Implemented:** the correction in `bulk_import_safety.py` (a single unlock call site, a failure-path helper, two note constants, `import sys` removed); `run_guarded` simplified; the C9 experiment and test updated. **Tested:** 16 fake-connection tests and 3 live tests added (plus 1 static harness test); the safety regression file went from 33 to 49 tests and the PostgreSQL safety file from 14 to 17. **Verified:** regression selection **74 passed** (58 before); PostgreSQL selection **81 passed, 2 xfailed** (77 passed, 2 xfailed before), in the working tree and in a clean worktree of `373b354` plus the files it needs (exit codes 0; output and JUnit retained outside the repository); the four runner regression modules that consume the lock **160 passed**; sensitivity check **9 of 9**. Cleanup, stated to match the actual assertions: **physical-session absence is asserted explicitly only in the backend-loss test** (the aborted-transaction and false-unlock live tests assert lock state from `pg_locks` and dispose their engines, without asserting that their backends ended; no separate retained evidence establishes more); the disposable container was removed and the existing Docker volume untouched; the only credential-shaped text in the retained evidence is the tests' own synthetic placeholder, echoed in the before-fix failure output (which itself shows the old behavior propagating an unsanitized message). Earlier counts in this document are historical to their trees.

### 37.4 Corrections to earlier statements

Section 36 and the checkpoint said that only the harness preserved primary failures and that the production manager let a release error win; that was true of the unchanged function and is **superseded**: the production manager now preserves the primary failure itself. The original defect evidence is retained in specification section 12.7 and in the pre-change evidence. A later review also found that the lock-fix review attachment and these documents said cleanup was verified on every live path and that every added live test verified physical-session absence; that was an overstatement and is **superseded** by the qualified wording in section 37.3 (the attachment is kept as history; a corrected one supersedes it; no test was rerun to edit the wording).

### 37.5 Remaining limitations

Preserving a primary failure is not releasing the lock: the note says a release failed or was unconfirmed, not what PostgreSQL holds. After a body SQL error aborts the transaction the unlock cannot run, so the lock stays held until the caller releases it or ends the session (a session-level lock survives `rollback()`), and this function deliberately does not roll back. The PostgreSQL runner modules that use the lock were not rerun. **Manifests:** the 8 rows of `bulk_import_safety.py` and its two exceptions in `notebook_src_coverage.csv` now state the new test counts (49 and 17, originally 33 and 14), and the `bulk_import_advisory_lock` rows in both manifests state the exception-preserving release; no row was added or removed (1,156 data rows; 1,157 physical lines in each).


## 38. Phase 9B-5C5: validation of the fresh catalog-observer candidate (2026-10-05), and a wording correction

**Scope.** Disposable experiments against one synthetic PostgreSQL 16 cluster (the existing local image) and documentation. It validates a candidate; it **does not adopt a production capture design and does not authorize production capture**. No production capture, lock policy, scheduler, storage, deployment, staging, commit or push; no production code (including the corrected `bulk_import_advisory_lock`), grant, schema, migration, dependency, formula, identity rule, notebook or export changed; no shared harness changed (the C6, snapshot and comparator files are byte-identical to their preflight hashes); the unresolved identity-test failure (26.5) is unchanged; C6 stays failed as written with its seven expected failures retained; C10's two endpoint-count expected failures are unchanged. **Phase 9B remains incomplete.**

### 38.1 Wording correction (first step; no test rerun)

The lock-fix review attachment and sections 37.3 and specification 12.7 said cleanup was verified on every live path, and the attachment that every added live test verified physical-session absence. Only the **backend-loss** test asserts physical-session absence (the owning backend absent from `pg_stat_activity` after the termination and after its engine was disposed); the aborted-transaction and false-unlock tests assert lock state from `pg_locks` and dispose their engines. The wording was qualified to the actual assertions in the three documents; the original attachment is kept unchanged as history and a corrected edition supersedes it. No result changed.

### 38.2 Acceptance criteria and results

| Criterion | Result |
|---|---|
| Existing catalog queries, digests, refusal guard and shared harness reused; no competing definition or second baseline representation | **passed** (imports; a static test) |
| Separate fresh observer transaction per endpoint; isolation and lifetime documented and recorded; baseline reads on the owner's Connection; owner alive through `pg_dump`; an Engine not assumed to supply one transaction | **passed** (observer READ COMMITTED, one transaction per endpoint, same backend reused; Engine = 3 checkouts, Connection = 1) |
| S1 no-change control: real dump, disposable restore, strict equality, guard permits | **passed** |
| S2 constraint change: committed before the owner's table locks; bounded attempt after; commit or refusal, visibility and SQLSTATE independently proven; a blocked change is not detection | **passed**: committed and detected (S2a); blocked 55P03, invisible, not counted (S2b); a lock-compatible additive constraint committed and detected (S2c) |
| S3 committed catalog and ACL operations during a real dump, ordered by server evidence and a deterministic barrier | **passed**: dump shown inside its snapshot transaction (xmin equal to the owner's) and parked behind the queued exclusive request; ACL, index and constraint operations committed, detected; barrier cancelled in 0.33 s; dump exited 0 with the owner open |
| S4 role changes with disposable roles only: rename effect on owner/ACL observations and archive; DROP ROLE rejection versus committed drop | **passed (observations recorded)**: detected; archives name old or both names and cannot be restored; 2BP01 rejection versus committed drop |
| S5 change-and-reversal control; limitation retained | **limitation confirmed**: equal endpoints, flow permitted; strict xfail |
| Every refusal ends the flow before restore/finalization and leaves no artifact; any forensic restore is a separately labeled diagnostic | **passed** |
| Multi-query behavior verified without blanket claims | **passed** (measured for three operations) |
| Discrepancies investigated; expected safety outcomes unchanged; failed requirements retained | **done**: the index read's lock wait was investigated from `pg_locks`; two strict xfail |

### 38.3 Verified

Command `python -m pytest tests/postgres/test_postgres_backup_catalog_candidate.py -q -m postgres -ra`: **143 passed, 2 xfailed, 0 skipped, 0 warnings (93.3 s, exit 0)** in the working tree and **143 passed, 2 xfailed, 0 skipped, 0 warnings (93.7 s, exit 0)** in a clean worktree of `373b354` plus only the files it needs (about 100 s each, exit codes 0; output, exit codes and JUnit retained outside the repository). No shared helper changed, so no accepted module was rerun. Sensitivity check **15 of 15 caught (plus one control correctly not counted)**, counted only for expected assertion failures; one deliberate control (a lock-timeout error) was correctly reported as not counted; the module was restored byte for byte. Cleanup verified per scenario including the disposable roles; containers 0 before and after; the existing Docker volume untouched; no credential in any command or output. The review attachment is generated from the retained JUnit by a structural exporter that fails on missing records (and was shown to fail on a removed one). Earlier counts in this document are historical to their trees. Details, the operations table and limits: specification section 12.8.

### 38.4 Status, by kind

| Kind | Items |
|---|---|
| **Implemented** (test layer only) | `test_postgres_backup_catalog_candidate.py`: the fresh-observer endpoints, the barrier orchestration, the change and role runners, the diagnostic restore, the probes |
| **Tested and verified** | the candidate detects every committed difference tried (constraint, index, ACL, role rename) including during a real dump, and its refusal stops the flow; blocked and rejected operations are not detection; observer semantics and lock behavior measured; cleanup |
| **Failed (retained, strict xfail)** | a change and its reversal is not detected; the observation does not complete while an exclusive request is queued. Unchanged: C6 as written, C10 endpoint evidence |
| **Proposed, unadopted** | the fresh-endpoint candidate as the production catalog protection; any observation timeout or lock policy; a DDL audit or freeze |
| **Deferred / not implemented** | production capture, restore-verification records, scheduling, storage, retention, monitoring; categories the observation does not read; a restore recipe that can restore a non-migrator owner |

### 38.5 Design recommendation (nothing here is adopted)

**Detects:** a committed difference in constraints, indexes or ACL/ownership text between the two endpoints, including changes that commit while `pg_dump` is inside its transaction and role renames. **Misses or does not establish:** a change and its reversal; anything outside those three categories (untested); whether the archive is consistent (the refused flows' archives matched their baselines; the role-rename archives could not be restored); a torn endpoint (each READ COMMITTED endpoint takes three snapshots). **Would require operationally:** no migrations, grants or role administration during the window (not enforced by the advisory lock); a bounded observation `lock_timeout` with a defined refuse-or-retry on timeout and a migrator `lock_timeout` so a queued exclusive request cannot stall observation (values not chosen); stable role names between capture and restore and a restore recipe able to restore ownership; restore verification as the independent archive check; and, only if a change and its reversal must be excluded, a server-side DDL audit or a true freeze (not designed or validated). **Open production decision (Anthony):** adopt the fresh-endpoint observation as the catalog protection and record its residual risks in the manifest, add enforcement, or rely on quiescence (specification decisions 6 and 7), together with the restore recipe's ownership limitation.

### 38.6 Remaining work

**Unvalidated:** the untested catalog categories, role attribute and membership changes, other PostgreSQL versions and settings, a torn-endpoint scenario beyond the measured read behavior. **Manifests:** reviewed by the established convention (keyed by real notebook function name; a symbol with no notebook origin gets no row; only a bare-name collision updates a row): none of the new module's 65 public top-level symbols collides with either manifest and no production module changed, so **no entry changed** (1,156 data rows; 1,157 physical lines in each, identical to their preflight hashes). Committing the batch is a separate decision for Anthony and not a prerequisite.

## 39. Phase 9B-6 design: proposed v1 capture and restore operating contract (2026-10-05)

**Documentation only; proposed, not implemented or verified.** The contract is specification section 13. It recommends:
- Exported-snapshot capture inside a controlled window. The existing advisory lock excludes controlled imports; migrations, grants, role administration and ad hoc writes are excluded operationally, which is not enforced.
- Preflight refusal of unsupported ownership and of sequences that fail R0.
- The fresh-endpoint observation as a bounded refusal check.
- `monotonic_v1`.
- No explicit table locks.
- Restore verification against the artifact's own baseline as the only route to claim 4.

Specification 13.4 and 13.6 name the gaps instead of filling them. The first production unit proposed is a read-only capture preflight (specification 13.7). The three decisions that block it are in specification 13.8. All earlier results stand unchanged: C6's original bracket remains failed; 12.8's two unmet requirements remain; C10's counts remain evidence only. No test was run and no count is reported. Phase 9B remains incomplete.

**Update (2026-10-05).** Anthony approved the three blocking decisions in specification 13.8. The read-only capture preflight (13.7) is unblocked as a design and remains unimplemented. Decisions 6, 7 and 8 stay open.

## 40. Phase 9B-6A: read-only capture preflight (2026-10-05)

**Implemented, tested and verified for this unit only; uncommitted.** Full record: specification section 13.9.

| Kind | Items |
|---|---|
| **Implemented** | `src/deployment/backup_capture_preflight.py`, a read-only observation on a caller-owned Connection: identity via `verify_database_target`, version, head, `system_identifier` readability, database and relation ownership, relation and column grantees, unsupported objects, sequence R0 and the validated configuration; one pure `accepted` |
| **Tested and verified** | regression 35 passed; PostgreSQL 12 passed (working tree and clean worktree of `373b354`); adjacent regression selection 817 passed, 13 skipped; sensitivity 11 of 11 caught; cleanup verified |
| **Measured** | the backup role can read `system_identifier` (PostgreSQL 16 defaults, 9B-2 grants); the preflight completes while an `ACCESS EXCLUSIVE` request is queued on an application table, while a control read of that table fails with 55P03 |
| **Corrected** | 13.7's "catalog reads only": the head read takes ACCESS SHARE on `alembic_version` |
| **Not implemented** | the lock connection and release recovery; production snapshot-owner reads; the production fresh-endpoint observation; the manifest-recording gaps; capture; restore verification. Decisions 6, 7 and 8 remain open |

All earlier results and expected failures are unchanged. Phase 9B remains incomplete.

## 41. Phase 9B-6B: dedicated backup lock session (2026-10-05)

**Implemented, tested and verified for this unit only; uncommitted.** Full record: specification section 13.10.

| Kind | Items |
|---|---|
| **Implemented** | `src/deployment/backup_capture_lock.py`: a dedicated `NullPool` lock session holding the existing advisory lock (unchanged manager, key and namespace); a TLS URL check; discard on failure; physical close; bounded verification through a fresh, validated verifier session; sanitized setup, cleanup and note text |
| **Tested (real PostgreSQL)** | scenarios A-K: the lock is held in the body and gone afterwards; primary failures are preserved; the session lock survives rollback and holds until physical closure; backend loss leaves no ownership and causes no reconnection; refusal is immediate with no body; wrong target and setup failure; a false unlock; an unavailable verifier gives no false success; interrupts; repeated runs; the caller's transaction is untouched |
| **Tested (injected)** | cleanup-step failures, verification outcomes, interrupts, credential scans, the second-connect refusal, static constraints |
| **Verified** | regression 86 passed; PostgreSQL 85 passed, 2 xfailed (C10, unchanged), 0 warnings with `-o junit_family=xunit1` (the first xunit2 run's 14 warnings were schema-incompatibility warnings; *corrected in 9B-6C: dropped properties were never established*); identical in a clean worktree; 14 of 14 evidence records exported; sensitivity 10 of 10 |
| **Corrected** | the 9B-6A boundaries (what `accepted` means; `system_identifier` access shown for the tested setup only; the head read can wait on the `alembic_version` lock); a failed R2 blocks recovery readiness |
| **Proposed / deferred** | the snapshot-owner session (next unit); the production fresh-endpoint observation; the manifest-recording gaps; capture; restore verification. Decisions 6, 7 and 8 open |

All earlier results and expected failures are unchanged. Phase 9B remains incomplete.

## 42. Phase 9B-6C: snapshot-owner session and baseline reads (2026-10-05)

**Implemented, tested and verified within its tested scope; uncommitted.** Full record: specification section 13.11.

| Kind | Items |
|---|---|
| **Reconciled** | the 9B-6B JUnit account (dropped properties were never established; warning versus schema versus missing XML versus exporter kept distinct); the exact commands recorded; the F failure stages separated; "verified within tested scope" |
| **Implemented** | `src/deployment/backup_capture_snapshot.py`: snapshot owner S in one REPEATABLE READ, READ ONLY transaction; preflight and identity on the actual sessions; export; baseline as `BaselineDocument` on S's Connection; lock checks at boundaries; S ended before L is released. Lock module: L's own identity facts and `confirm_held()` |
| **Tested (real PostgreSQL)** | A-K plus the lock additions: identities, server transaction settings, snapshot import and exact baseline equivalence, commit invisibility, a dead snapshot after exit, one connection and one transaction, refusals, failure preservation and cleanup, a bounded 55P03 refusal, S-before-L ordering, a caller untouched |
| **Verified** | regression 123 passed; PostgreSQL 40 passed (`-o junit_family=xunit1`); identical in a clean worktree; 14 of 14 records exported; sensitivity 7 of 7 |
| **Not established** | continuous catalog stability (decision 6 deferred); continuous lock ownership between checks; archive consistency; a recoverable backup; recovery readiness |
| **Next (not begun)** | `pg_dump --snapshot` as the consumer, to job scratch only |

All earlier results and expected failures are unchanged. Phase 9B remains incomplete.

## 43. Phase 9B-6D: real pg_dump consumer of the snapshot handle (2026-10-05)

**Implemented, tested and verified within its tested scope; uncommitted.** Full record: specification section 13.12.

| Kind | Items |
|---|---|
| **Fixed narrowly** | the snapshot handle: `confirm_active()` and a private copy of the exact validated source (needed for pre-launch and post-completion checks without fallback) |
| **Implemented** | `src/deployment/backup_capture_dump.py`: a `ClientHost` seam (production `LocalClientHost`); custom-format `--snapshot` dump as the backup role over verify-full; refusal of unsupported URL parameters; a private, early-removed passfile; client major 16; checks before and after; an offline TOC inventory against the baseline; bounded processes; stop and reap; only its own backend ended; work directory removed |
| **Tested (real PostgreSQL 16.15 client)** | A-K: inventory; dump backend xmin equal to S's; committed marker absent from the archive; expired handle; invalid snapshot; deadline kill (137) with no backend left; S or L loss defeats success although `pg_dump` exited 0; authentication, TLS and missing-client cleanup; passfile removal; caller untouched |
| **Tested (real processes, stand-in client)** | `LocalClientHost`: `PG*` scrubbing, deadline kill and reap, start failure, version refusal, exclusive private files |
| **Verified** | regression 138 passed; PostgreSQL 38 passed; identical in a clean worktree; 17 of 17 records; sensitivity 11 of 11 |
| **Not established** | artifact consistency; data correctness; recoverability; recovery readiness; continuous ownership between checks; catalog stability (decision 6 deferred) |
| **Next (not begun)** | disposable restore verification of the archive (unchanged comparator, `monotonic_v1` with R2 blocking) |

All earlier results and expected failures are unchanged. Phase 9B remains incomplete.

## 44. Phase 9B-6E: disposable round trip of the integrated capture path (2026-10-05)

**Tested disposable round-trip behavior, test layer only; uncommitted.** Full record: specification section 13.13.

| Kind | Items |
|---|---|
| **Added (test layer)** | `tests/postgres/test_postgres_backup_capture_roundtrip.py`: production capture path; exact archive and baseline copies with separate digests; contexts proven closed; guarded migrator-owned target; accepted restore recipe as the non-superuser migrator; unchanged strict comparator; separate `monotonic_v1` evaluation (R2 blocking). No existing file changed |
| **Tested** | A no change; B a safe advancement (strict difference kept, policy accepts, row excluded); C equal but unsafe (strict equal, R2 rejects); D data, E index, ACL and ownership tampering detected; F damaged archive and SQL error fail with no comparison; G real `pg_restore` killed at its deadline and backend ended; H source or unowned target refused before restore |
| **Verified** | 12 passed (working tree and clean worktree); 12 of 12 records exported; sensitivity 9 of 9 |
| **Not established** | production restore; recovery readiness; the production `LocalClientHost` route; catalog stability; continuous exclusion; frozen sequences; a timeout policy |
| **Blockers** | no PostgreSQL 16 client on a job host for `LocalClientHost`; strict sequence differences under concurrent capture (13.5 open question); decision 6; the 13.4 recording gaps |
| **Next (not begun)** | (a) the unchanged dump and round-trip tests through `LocalClientHost` in a disposable Linux job container with a PostgreSQL 16 client; (b) decision 6, then, if adopted, a production fresh-endpoint refusal check |

All earlier results and expected failures are unchanged. Phase 9B remains incomplete.

## 45. Phase 9B-6F: bounded fresh catalog observer and refusal check (2026-10-05)

**Implemented, tested and verified within its tested scope; uncommitted.** Full record: specification section 13.14.

| Kind | Items |
|---|---|
| **Corrected** | 9B-6E scenario B does not authorize capture with active writers; equal observations or session counts cannot prove writers were absent |
| **Implemented** | `src/deployment/backup_capture_catalog.py`: the canonical catalog definitions (moved from the C6 test module); a fresh, owned, bounded READ COMMITTED, READ ONLY endpoint with its own identity check; the `require_unchanged` refusal check (`NoDifferenceObserved` / `CatalogObservationFailed` / `CatalogChanged`) |
| **Tested (real PostgreSQL)** | A no change proceeds to restore; B a committed change during the real dump refuses before the normal path; C a blocked observation refuses in 5.1 s; D change and reversal is the known bypass (characterization); E freshness against S; F identity and missing records; G failure cleanup and caller untouched |
| **Verified** | regression 39 passed; PostgreSQL 7 passed; accepted C6 120 passed, 7 xfailed and candidate 143 passed, 2 xfailed after the move; identical in a clean worktree; 7 of 7 records; sensitivity 7 of 7 |
| **Proposed** | manifest `/2` `catalog_observation`; removal of `catalog_bracket_equal`; a capture-phase failure-recording path |
| **Not established** | catalog stability; writer exclusion; artifact consistency; writer integration; the `LocalClientHost` route; a timeout policy |
| **Next** | `LocalClientHost` validation in a disposable tooling image (local app image plus a PGDG `postgresql-client-16=16.15`), after approval of the download |

All earlier results and expected failures are unchanged. Phase 9B remains incomplete.

## 46. Phase 9B-6G: LocalClientHost validated with real PostgreSQL 16 tools on a Linux runner (2026-10-05)

**Tested and verified within its tested scope; uncommitted.** Full record: specification section 13.15.

| Kind | Items |
|---|---|
| **Validated** | the production `LocalClientHost` with real PostgreSQL 16.15 `pg_dump` and `pg_restore --list`, through its own `shell=False` path, with no client-side relocation, on Debian 13 amd64 (Python 3.13.16) |
| **Test-only additions** | `tests/postgres/capture_client_support.py` (`NAPSEDGE_TEST_CLIENT=local`); a guarded canonical-URL branch in `launched_cluster`; transport-neutral call sites in the dump, catalog and round-trip modules; `tests/regression/test_capture_client_support.py` |
| **Tooling** | a disposable runner from `napsedge-api:phase9a` plus PGDG `postgresql-client-16=16.15-1.pgdg13+2` and Debian `docker-cli`, pinned test packages; removed after the unit |
| **Verified** | native 27 passed, 2 declared skips (working tree and clean worktree); default container transport 29 passed; harness module 36 passed; support regression 8 passed; 27 of 27 records; sensitivity 6 of 6 |
| **Not established** | Windows `LocalClientHost`; a production host's network path; artifact consistency; recoverability; readiness |
| **Next** | the `/2` manifest amendment as a reviewed specification change; capture-failure recording via the writer's owned `.partial` at capture start (option i); then a guarded capture coordinator |

All earlier results and expected failures are unchanged. Phase 9B remains incomplete.

## 47. Phase 9B-6H: manifest /2 and the early owned-partial reservation (2026-10-05)

**Implemented, tested and verified within its tested scope; uncommitted.** Full record: specification section 13.16.

| Kind | Items |
|---|---|
| **Adopted** | manifest `napsedge.backup-artifact/2`: required `consistency.catalog_observation` (method, result, categories, source equal to the manifest's, two complete distinct endpoints with equal, derived digests); `catalog_bracket_equal` removed, never reused; `/1` unchanged |
| **Added** | `reserve_artifact` / `ArtifactReservation` (exclusive `.partial` plus `RUNNING` before capture; single-shot `record_failure` or `finalize`); `catalog_observation_record`; `capture_failure_reason` (type-based, onto existing classes; overlap is none) |
| **Unchanged** | baseline, `FAILED`, `RUNNING` and marker schemas; reason codes; `write_artifact`'s outputs and failures; capture modules; strict comparator; manifests |
| **Verified** | focused 784 passed, 13 platform skips (working tree and clean worktree); dependent capture regression 173 passed; pre- and post-change writer byte- and operation-identical on `/1`; sensitivity 7 of 7 |
| **Not established** | the guarded capture coordinator; authenticity of observation records; POSIX paths re-run in this unit; Windows `LocalClientHost`; artifact consistency; recoverability; readiness |
| **Next** | the guarded capture coordinator (13.16 interface, order and acceptance criteria) |

All earlier results and expected failures are unchanged. Phase 9B remains incomplete.

## 48. Phase 9B-6H follow-up: failed-partial markers, publication outcome, corrected coordinator order, Linux validation (2026-10-05)

**Implemented, tested and verified within its tested scope; uncommitted.** Full record: specification section 13.17.

| Kind | Items |
|---|---|
| **Corrected** | a failed partial no longer keeps the `COMPLETE` this run wrote (removed before `FAILED`; a failed removal keeps both markers, non-adoptable, with the original failure preserved); a rename outcome is "yes" only with a marker at the final name, otherwise "unknown" |
| **Documented** | the corrected coordinator order (before-observation before S; archive copied to coordinator-owned scratch with its digest checked; publication only after the dump, snapshot and lock contexts exit with verified cleanup); pre-reservation failures are not recorded in an artifact |
| **Verified** | Windows 811 passed, 13 skipped; Linux 820 passed, 2 skipped (all Windows skips executed); clean worktrees match; successful `/1` output unchanged; sensitivity 5 of 5 (Windows), 6 of 6 (Linux) |
| **Not established** | the coordinator; marker-removal durability (not flushed); non-adoptability when both cleanups fail rests on `NOT_FINAL` only; recoverability; readiness |

All earlier results and expected failures are unchanged. Phase 9B remains incomplete.

## 49. Phase 9B-6I: the guarded capture coordinator (2026-10-06)

**Implemented, tested and verified within its tested scope; uncommitted.** Full record: specification section 13.18.

| Kind | Items |
|---|---|
| **Added** | `run_guarded_capture` (accepted order; outcomes kept distinct by publication state; owns only its private scratch); `/2` null `foreign_write_sessions_*` = not sampled |
| **Verified** | regression Windows 1,035/13 skips, Linux 1,036/2 skips; container-transport PostgreSQL 29 passed (6 transport skips); native `LocalClientHost` PostgreSQL 16.15: 33 passed, 2 declared skips; clean worktree with a disclosed 39-file overlay matches; sensitivity 7 of 7 |
| **Not established** | production capture; Windows `LocalClientHost`; foreign-write-session sampling; catalog stability; continuous exclusion; recoverability; readiness |
| **Next** | not authorized here; candidates: a narrow expected-digest parameter for `finalize`, decision 8 (session sampling), the job entrypoint |

All earlier results and expected failures are unchanged. Phase 9B remains incomplete.

## 50. Phase 9B-6I follow-up: expected-archive guard in writer staging (2026-10-06)

**Implemented, tested and verified within its tested scope; uncommitted.** Full record: specification section 13.19.

| Kind | Items |
|---|---|
| **Closed** | the archive bytes the writer stages are checked against the capture's expected SHA-256 and size before `COMPLETE` and publication; a mismatch is an ordinary pre-publication `checksum_mismatch` |
| **Verified** | Windows regression 1,055/13 skips; Linux 1,056/2 skips (+1 environmental `git` failure in the full selection); container-transport PostgreSQL 29 passed (7 transport skips); native PostgreSQL 16.15 34 passed (2 declared skips); clean worktree identical; sensitivity 4 of 4 |
| **Not established** | archive recoverability; recovery readiness; protection against external mutation of the store after verification; foreign-write sampling (decision 8) |

All earlier results and expected failures are unchanged. Phase 9B remains incomplete.

## 51. Phase 9B-6J: published-artifact round-trip validation (2026-10-06)

**Test-layer integration, verified within its tested scope; uncommitted.** Full record: specification section 13.20.

| Kind | Items |
|---|---|
| **Demonstrated (this setup only)** | a coordinator-published `/2` artifact verifies on reopening, restores into a guarded disposable target as the migrator, and compares strictly equal to its own saved baseline (unchanged source); later writes are excluded with strict sequence differences retained; R2 failures are reported and reject the policy; corrupted copies, wrong targets, restore failures and deadlines never yield a comparison |
| **Runner** | pinned Debian `git` 2.47.3 added; the full Linux regression selection passes with no exclusion |
| **Verified** | Windows 1,055/13 skips; Linux 1,064/2 skips; container PostgreSQL 29 passed (17 transport skips); native 44 passed (2 declared skips); 10 of 10 evidence records with a refused negative control; clean worktree identical; sensitivity 7 of 7 |
| **Not established** | recovery readiness; a production restore path; foreign-write sampling (decision 8); catalog stability; continuous exclusion; frozen sequences; Windows `LocalClientHost` |

All earlier results and expected failures are unchanged. Phase 9B remains incomplete.

## 52. Phase 9B-6K: one-shot backup job entrypoint (2026-10-08)

**Implemented, tested and verified within its tested scope; uncommitted.** Full record: specification section 13.21.

| Kind | Items |
|---|---|
| **Added** | `entrypoints backup-capture`: one guarded capture, a sanitized JSON report, documented exit codes; environment configuration (password separate from the URL; a credential-bearing URL is refused) |
| **Verified** | Windows 1,135/13 skips; Linux full selection 1,144/2 skips; container PostgreSQL 29 passed (21 transport skips); native 48 passed (2 declared skips) including the real command as a subprocess; clean overlay identical; sensitivity 5 of 5 |
| **Not established** | a schedule; a production secrets mechanism; off-host storage; retention; a restore path or drill; recovery readiness; decision 8; Windows `LocalClientHost` |
| **Decisions needed** | provider and runtime; secrets storage and injection; frequency and RPO; off-host storage and encryption; retention; RTO and restore drill; decision 8 (13.21 checklist, with what each blocks) |

All earlier results and expected failures are unchanged. Phase 9B remains incomplete.

## 53. Phase 9B-6L: deployment decision package (2026-10-08)

**Research and documentation only; uncommitted.** Full record: `docs/phase9b_deployment_decision_package.md`.

| Kind | Items |
|---|---|
| **Documented support** (provider and PostgreSQL docs, 2026-10-08) | DigitalOcean Managed PostgreSQL v16 (EOL 2028-11-09), `verify-full` with a downloadable CA (Standard), no superuser, 22/47 connections on the 1/2 GiB plans, daily provider backups kept 7 days and destroyed with the cluster; PostgreSQL 16 lock and snapshot semantics; systemd timer overlap and credential semantics; B2 pricing, encryption and Object Lock |
| **Live verification required (not done)** | `pg_control_system()` for the backup role; migrator ownership without provider grantees or public-schema extensions; advisory locks and snapshot import via the direct endpoint; own-backend termination; connection headroom; the job end to end; the published round trip; the encrypted remote drill |
| **Recommended, NOT adopted** | Option 1 (managed v16 + Droplet + B2), with Option 2 as the fallback; F2 frequency; systemd encrypted credentials with a password-file input; client-side asymmetric encryption with an offline key; retention R1 |
| **Decisions needed** | bundle 1 (database, runtime, budget); bundle 2 (frequency, RPO, RTO); bundle 3 (secrets, encrypted storage, key custody, retention); plus the readiness rule for strict sequence differences, the definition of "recovery ready", the refusal-retry policy, and decision 8 |
| **Not established** | any deployment; remote storage; a restore drill; recovery readiness |

All earlier results and expected failures are unchanged. Phase 9B remains incomplete.

## 54. Phase 9B-7A: backup job container and password-file credential source (2026-10-08)

**Packaging and credential-file support only; implemented, tested and verified within its tested scope; uncommitted.** Full record: specification section 13.22.

| Kind | Items |
|---|---|
| **Added** | a password-file credential source (exactly one source; strict, documented file rules; never echoed); the `backup` Dockerfile target (the existing base stage plus the pinned PGDG PostgreSQL 16 client; non-root; read-only root; read-only credential and CA mounts; explicit writable scratch and store; no socket) |
| **Verified** | Windows 1,215/18 skips; Linux 1,227/2 skips plus 2 environmental git-ownership failures (59/59 with `safe.directory`); container PostgreSQL 29 passed (21 transport skips); native 48 passed (2 declared skips); the built image 5 passed against the disposable harness; clean overlay with a rebuilt image identical; sensitivity 3 of 3 |
| **Not established** | any deployment or provider; a production secrets mechanism (the file input is mechanism-neutral); scheduling; off-host encryption and upload; pruning; app pool caps; a restore drill; recovery readiness; decision 8; Windows `LocalClientHost` |
| **Decisions needed** | the 9B-6L bundles 1-3, the strict-sequence readiness rule, the definition of "recovery ready", the refusal-retry policy, and decision 8; then the live probe in 13.22 |

All earlier results and expected failures are unchanged. Phase 9B remains incomplete.

## 55. Phase 9B-7A follow-up: build-context bytecode hygiene and Linux validation correction (2026-10-08)

**Build-context hygiene and validation correction only; uncommitted.** Full record: specification section 13.23.

| Kind | Items |
|---|---|
| **Corrected** | host bytecode excluded from the build context at every depth (`**/__pycache__`, `**/*.pyc`, `**/*.pyo`, kept last), which affects every target; the disposable runner trusts only the mounted `/repo`; builds pin the accepted base digest |
| **Verified** | Windows 1,217/18 skips; Linux full selection 1,231/2 skips, 0 failures; the built image 5 passed; sentinel builds 4 passed; other-target smoke clean; negative controls 2 of 2; the clean clone identical |
| **Not established** | any deployment or provider; scheduling; off-host encryption and upload; retention; app pool caps; a restore drill; recovery readiness; decision 8; Windows `LocalClientHost`; rebuilt `phase9a` application images (unchanged, still carrying host bytecode) |
| **Decisions needed** | bundle 1 first (database provider, runtime, operating budget; the 9B-6L recommendation stays provisional), then bundles 2-3, the readiness policies and decision 8 |

All earlier results and expected failures are unchanged. Phase 9B remains incomplete.

## 56. Phase 9B-7B: live-provider compatibility probe (2026-10-08)

**Probe prepared; locally rehearsed; provider NOT verified. Uncommitted.** Full record: specification section 13.24 and `docs/phase9b_provider_probe_runbook.md`.

| Kind | Items |
|---|---|
| **Prepared** | one fixed probe procedure reusing the accepted job, SQL, migrations, preflight, lock, verifier, restore and comparator; a placeholder-only configuration; mutation-marked, ledger-scoped setup and cleanup; credential files only; a check mapping for decision-package checks 1-10 |
| **Locally rehearsed** | end to end against a non-superuser CREATEROLE stand-in admin on a disposable PostgreSQL 16 cluster; a provider-restriction blocker; an existing-resource refusal; Windows 1,242 passed, 18 skipped; Linux 1,256 passed, 2 skipped, 0 failed; rehearsal 3 passed; clean clone identical; sensitivity 4 of 4 |
| **Not established** | any provider's compatibility (runbook section 9); check 10 (encrypted remote drill); connection headroom under load (observed only); scheduling; remote storage; retention; pool caps; recovery readiness; decision 8; Windows `LocalClientHost` |
| **Decision needed now** | one bundle: managed or self-managed PostgreSQL, the runtime, and a monthly budget ceiling (9B-6L recommendation still provisional) |

All earlier results and expected failures are unchanged. Phase 9B remains incomplete.

**Bundle 1 decided (2026-10-08, revisable):** managed DigitalOcean PostgreSQL 16 Standard 1 GiB plus a Basic 2 GiB Droplet, about $27.15/month, under Anthony's about $30 ceiling. This requires the app pool-cap unit before the app uses the database. Provider still NOT verified; bundles 2-3, the readiness policies and decision 8 remain open.

## 57. Phase 9B-7B (continued): remote handoff (2026-10-08)

| State | Status |
|---|---|
| Handoff package verified | **yes**: inventory, images (content fingerprints), all modules from the package |
| Local rehearsal from the package passed | **yes**: 25 + 3 tests and the handoff's own `probe_run_all` |
| Provider probe executed | **no**: waiting for Anthony to create the DigitalOcean resources |
| Provider compatibility verified | **no** |

The package is an uncommitted working-tree snapshot, not a release. **Not established:** any provider behavior, encrypted off-host recovery, headroom under load, scheduling, retention, or recovery readiness. Phase 9B remains incomplete.

**Live probe deferred by Anthony (2026-10-08):** no billable resource exists. Provider compatibility remains unverified, and the provider-specific risk is accepted until the probe runs.

## 58. Phase 9B-8A: application connection caps (2026-10-08)

**Implemented, tested and verified within its tested scope; uncommitted.**

| Kind | Items |
|---|---|
| **Added** | `build_engine` caps every PostgreSQL engine (SQLite unchanged): `NAPSEDGE_DB_POOL_SIZE` 2, `NAPSEDGE_DB_MAX_OVERFLOW` 2, `NAPSEDGE_DB_POOL_TIMEOUT_SECONDS` 10 by default; startup validation in all three processes; pool exhaustion and pool misconfiguration answer a sanitized 503; the variables are documented in `.env.example` and passed by `compose.yaml` to api, streamlit and discord only |
| **Budget (1 GiB plan, 22 connections)** | 3 processes x 4 = 12, plus the backup job about 4, plus one operator session = 17 of 22 |
| **Verified** | full default suite on Windows 6,496 passed / 19 skips; Linux selection 1,293 / 2 skips; PostgreSQL suites that build engines 315 passed, including server-side observation (never more than the cap; a burst waits; a saturated API answers 503 and recovers); clean clone with an 80-file overlay identical (the extra skips are git-ignored local data fixtures); sensitivity 4 of 4 |
| **Not established** | headroom under real load on the provider (observed only by the deferred probe); the provider's own reserved connections; the app running against the managed database (still disconnected) |

Phase 9B remains incomplete.

## 59. Phase 9B-8B: scheduling, encrypted off-site transfer, retrieval, restore drills and retention (2026-10-08)

| Status | State |
|---|---|
| Local implementation | **implemented and verified within its tested scope; uncommitted** |
| Live storage validation | **not done**: an S3 emulator (moto) only; no storage account |
| Provider compatibility | **not verified**: the DigitalOcean probe is deferred |
| Demonstrated recovery on real infrastructure | **not done** (the drill passed locally, from a downloaded emulator copy) |

| Kind | Items |
|---|---|
| **Configured targets (not measured)** | daily at 03:30 UTC (`Persistent=true`), RPO 24 h, RTO 4 h, retention 7/4/6 (UTC), a monthly drill; remote operations off by default |
| **Verified locally** | Windows 6,546/21 skips; Linux 1,344/2; native 55/2 (real capture, `age`, boto3 to the emulator, drill from the download, refusals, interrupted upload, overlap, measured connections); host pool caps 3; built images 6; probe rehearsal 3; build context 4; sensitivity 6 of 6; the clean clone identical |
| **Connection budget** | measured job maximum 3 backends (budget 4), overlap 2; 17 of 22 (20 with a concurrent capped import) |
| **Handoff** | `napsedge-9b8b-handoff.tar`, SHA-256 `c9cee15cd890aa1d205f93a544987c33f9e3b59f82c9a85b7dee9f5fe858f567`, re-verified from a fresh unpack |
| **Remaining manual / decisions** | the B2 account, bucket and scoped keys; the off-host age key pair and two offline identity copies; systemd encrypted credentials; activation (runbook section 5); the notification channel; Object Lock (optional); the deferred provider probe; the first live drill; the readiness policies (strict-difference rule, the "recovery ready" definition, the refusal-retry policy); decision 8 |

All earlier results and expected failures are unchanged. Phase 9B remains incomplete.

## 60. Phase 9B-8C: first real Backblaze B2 validation and downloaded-artifact restore drill (2026-10-09)

| Status | State |
|---|---|
| Real B2 compatibility | **passed** (run 5; `<bucket>`, `s3.<region>.backblazeb2.com`), after one narrow fix: B2 returns 403 for a missing object under a prefix-restricted key |
| Recovery from a downloaded artifact | **passed**: B2 objects plus key copy A plus a new guarded database only; strict all equal; `monotonic_v1` 13/13, R2 none; drill record published and read back |
| Key custody | **verified**: two independent offline copies (USB, handwritten paper), each decrypting separately |
| Provider compatibility (DigitalOcean) | **not verified** (deferred) |
| Scheduled operation and alerts | **inactive**; both channels selected, destinations and test authorization outstanding; the heartbeat is bound to the run's own verified record |
| Recovery readiness | **not claimed** |

| Kind | Items |
|---|---|
| **Observed on B2** | `writeFiles` overwrites and hides (not immutability); version deletes refused; ro upload refused by connection close; missing object 403, hidden object 404; an interrupted upload leaves a partial version; capitals in bucket names are accepted by B2 but refused by the validator; two acknowledged uploads right after key/bucket creation did not persist (unexplained) |
| **Verified** | live validation passed in 83 s; Linux 59; native emulator 7; new image 6; Windows selection 50 / 2 skips; heartbeat gate 15 offline cases; leak check none |
| **Images** | new `napsedge-backup:9b8c-20261009022330` (`sha256:69e16f61...55dd`); 9B-8B images retained unchanged (the 9B-8B backup image cannot store to B2) |
| **Stale** | handoff `napsedge-9b8b-handoff.tar`: rebuild before the DigitalOcean step |
| **Remaining manual / decisions** | alert destinations and one authorized test; the deferred provider probe; activation; design-gap remedies (version-history anomaly check, version-id deletion, version-aware status); Object Lock (optional); key revocation and credential-volume removal after validation; the readiness policies; decision 8 |

All earlier results are unchanged. Phase 9B remains incomplete.

### 60a. Phase 9B-8C (continued): version-history remedies, alert tests, refreshed handoff (2026-10-09)

| Status | State |
|---|---|
| Version-history remedies | **implemented and verified** (live B2 run 6). Exact `version_id` binding from 9B-8C records on, with a timestamp fallback for older ones. Real-B2 version-id deletion not exercised |
| Alerts | both channels tested once, with authorization and confirmed receipt; not installed or scheduled |
| Handoff | `napsedge-9b8c-handoff.tar`, SHA-256 `72db706d...3a7c`, rehearsed from a fresh unpack |
| Provider compatibility (DigitalOcean) | **not verified** (deferred) |
| Recovery readiness | **not claimed** |

**Limits:** these are anomaly checks, not proof of authenticity. An artifact forged under a new name passes them. Object Lock and signed records remain separate decisions.

Phase 9B remains incomplete.

### 60b. Phase 9B-8C (continued): real-B2 retention deletion verified; entrypoint fix (2026-10-09)

| Status | State |
|---|---|
| Retention deletion by version id | **verified on real B2**. Upload key refused; deletion key removed every version; confirmed by a version listing |
| Entrypoint | `backup-retention --execute` fixed (it was unreachable through the container); image `9b8c-20261009035256` |
| Handoff | `napsedge-9b8c-handoff.tar`, SHA-256 `f092fcfc...215f`, rehearsed |
| Provider compatibility (DigitalOcean) | **not verified** (deferred) |
| Recovery readiness | **not claimed** |

Phase 9B remains incomplete.

### 60c. Phase 9B-8C close-out (2026-10-09)

| Status | State |
|---|---|
| Real B2 validation | **complete**. Validation keys revoked and confirmed refused; volumes removed |
| Claim-4 drill record | manifest and runbook digests plus a written-once check (local tests; a B2 re-run is due at deployment) |
| Final image / package | `napsedge-backup:9b8c-20261009150124`; `napsedge-9b8c-handoff.tar` SHA-256 `e63e7aaa12f1ff5c9cc05e872bef7c2254b9219962db028cab0f80ba567c4452` |
| Scheduled operation | **not deployed** (healthchecks.io paused) |
| DigitalOcean compatibility | **not verified** (deferred) |
| Recovery readiness | **not claimed** |

The remaining requirements, with recommendations, are in `docs/phase9b_completion_checklist.md`. Phase 9B remains incomplete.

### PHASE 9B-8C: POLICIES APPROVED (2026-10-09)

Anthony approved every recommendation in `docs/phase9b_completion_checklist.md` section 3, now recorded in specification section 11:
- decision 8: not sampled; quiescent mode retired; no `pg_read_all_stats`;
- decision 7: shared advisory key;
- strict sequence differences fail closed;
- the per-artifact and system-level "recovery ready" definitions;
- no automatic retry of refusals;
- Object Lock and signed records optional.

Still open: the schedule and retention values, PITR, secrets decision 9, and all spending. Anthony is still testing; DigitalOcean is deferred. Recovery readiness is still **not claimed**, because no provider drill exists. Phase 9B remains incomplete.

### 61. Phase 9E-1: structured logs, request IDs and redaction (2026-10-09)

| Section 10 control | State after 9E-1 |
|---|---|
| Structured application logs | **yes, optional:** `NAPSEDGE_LOG_FORMAT=json` (one object per line: `ts` UTC, `level`, `logger`, `message`, `request_id`, `exception`). `text` remains the default; anything else is refused at startup. |
| Request/correlation IDs | **yes:** `X-Request-ID` (a safe client value is kept, otherwise generated). It is returned in every response and attached to every log line of the request. |
| Service latency | **logged per request:** one access line (`napsedge.api.access`) with method, path only, status and duration. Uvicorn's own access log is off, because it logged query strings. |
| Secret/PII redaction in logs | **safety net added:** the Discord token, the password in NAPSEDGE_DATABASE_URL and every secret read from a credential file become `[REDACTED]` in messages and tracebacks. Code must still never log secrets. |

**Verification:**
- 50 targeted tests;
- 7 of 7 mutants caught (no redaction of messages or tracebacks, an unsafe request ID kept, a query string logged, uvicorn access log on, file secrets not registered, both secret sources allowed);
- the full default suites on Windows and Linux (see the checkpoint).

**Still open for 9E:**
- **9E-2:** CI steps 3-6 (PostgreSQL tests, the migration check, the dependency audit, image builds).
- **9E-3:** alerting on `/v1/ready` failures and Discord-process crashes; cache-age visibility; log retention (journald).
- Aggregated counts of authentication and rate-limit failures.

### 62. Phase 9E-2 and 9E-3: CI steps 3-6; alerting checks, cache freshness, log retention (2026-10-09)

| Item | State |
|---|---|
| CI step 3: the PostgreSQL suite | **added:** job `postgres` (`tests/postgres -m postgres`, testcontainers on the runner). Locally 853 passed / 38 skipped / 11 xfailed in 18 min; the job timeout is 60 min. |
| CI step 4: the migration check | **added:** job `migrations` (`alembic upgrade head` then `alembic check` against a PostgreSQL 16 service). Locally: no drift, 4 s. |
| CI step 5: image builds | **added:** job `images` (api, streamlit, discord, plus backup from the pinned base digest). Locally the three app targets built. |
| CI step 6: dependency scan | **added:** job `audit` (`pip-audit` over the pinned runtime and backup environment). The production constraints already pin the fixed Pygments 2.21.0 and python-dotenv 1.2.3; the two findings seen locally came only from the disposable test runner's own pins. |
| CI steps 7-10 (approval, deploy, health checks, rollback) | need the server; not added |
| Cache age/freshness | **operator command** `entrypoints data-freshness` (rankings cache and injury files: present, as-of, age; stale above NAPSEDGE_DATA_MAX_AGE_HOURS, default 48). No API route was added; the approved route set is unchanged. On the real local data: rankings 2026-03-31, injuries newest 2026-04-06, both stale (off-season). |
| Alerting on `/v1/ready` failures and Discord crashes | **prepared, not installed:** `deploy/app/napsedge-app-checks.example` (readiness every 5 min, the Discord container running every 5 min, freshness every 6 h). Each fires `OnFailure=` into the existing attention hook (journal today; Discord once activated). |
| Log retention | **prepared proposal:** `deploy/app/journald-napsedge.conf.example` (persistent, at most 1 GiB, 30 days); Anthony to confirm the values. |

### 63. Phase 9D: shared rate limiting (2026-10-09)

| Item | State |
|---|---|
| Shared backend | `src/auth/rate_limit_redis.py` (`RedisRateLimiter`): the same fixed-window semantics as the in-memory limiter, made atomic by one Lua script; the key is the authenticated `client_id` only (no proxy header, decision 3). |
| Selection | `src/auth/rate_limit_backend.py` (`build_rate_limiter`): `NAPSEDGE_RATE_LIMIT_BACKEND=memory` (default) or `redis`, with `NAPSEDGE_RATE_LIMIT_REDIS_URL` (no password in the URL) and an optional `NAPSEDGE_RATE_LIMIT_REDIS_PASSWORD_FILE`. Startup validation refuses bad settings. Kept out of `src/auth/rate_limit.py`, whose no-network-import guard stays intact (that module is byte-identical to before). |
| Outage | **fail closed** (decision 2): `RateLimitBackendUnavailableError` leads to the sanitized 503, with 0.5 s timeouts; health stays up. |
| Acceptance ("two workers share one budget per client_id") | **met against a real Redis 7:** two limiters share one budget; **4 spawned processes racing (80 attempts) admitted exactly 25 = the limit**; window expiry; independent clients; an unreachable backend refused in under 3 s with no URL in the error. 14 passed. |
| Sensitivity | 5 of 5 mutants caught (fail open, rejected requests counted, admission ignored, password-in-URL allowed, any backend accepted). |
| Review fix | the code review found that a URL the Redis client cannot parse (e.g. a non-numeric port) escaped as a raw `ValueError`; a regression test reproduced it first, and it is now refused as `RateLimitConfigurationError` without echoing the value. |
| Dependency | `redis==5.2.1` (redis-py, Redis Inc., MIT, no dependencies), pinned in the constraints. |
| Deployment | `deploy/app/compose.redis.example.yaml` (private Redis, no host port, no persistence), prepared and needed only above one API worker. CI's `postgres` job now also runs `tests/redis`. |

### 64. Phase 9C: private-only Streamlit access, prepared and rehearsed (2026-10-09)

| Item | State |
|---|---|
| Decision | private only: SSH tunnel or private network, no public login (section 16 item 5) |
| On-host check | `deploy/app/check-private-exposure.sh` (loopback-only listeners and Docker publications). Offline: 4 of 4 cases correct. |
| Local rehearsal | the real compose stack: loopback 200; via the LAN address `192.168.1.152` **unreachable**; check `RESULT: private`. Docker Desktop's `host.docker.internal` forwarding to loopback ports is recorded as a Desktop artifact. Stack removed. |
| Guide | `docs/phase9c_private_access.md` (the tunnel command, the DigitalOcean Cloud Firewall allowing TCP 22 only, the on-host and off-host checks) |
| Acceptance | **not yet**: needs the server (both checks plus a tunnel success) |
