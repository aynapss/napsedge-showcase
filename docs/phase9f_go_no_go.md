# Phase 9F -- Security and Deployment Acceptance: Go/No-Go Record (interim, 2026-10-09)

**Verdict: Tier B is NO-GO today.** Every item that can be proven without a server is proven or consciously accepted. Every remaining blocker needs the target server (DigitalOcean, deferred until Anthony chooses to pay). This record verifies; it builds nothing (readiness section 15, 9F).

Statuses: **GO** (proven with evidence), **RESIDUAL** (consciously accepted, with the condition that reopens it), **BLOCKED: server** (needs live infrastructure). **Recovery readiness is not claimed.**

## 1. Sub-phase acceptance (readiness section 15)

| Phase | Acceptance criterion | Status | Evidence / what is missing |
|---|---|---|---|
| 9A | `docker compose up` brings up all three processes; `/v1/ready` 200 | **GO** | Accepted (readiness 21); the compose stack re-ran for the 9C rehearsal |
| 9B | A documented backup schedule | **GO** (policy) | 03:30 UTC daily, RPO 24 h, RTO 4 h, retention 7/4/6, monthly drill (approved; readiness 16 item 6) |
| 9B | A successfully executed restore test | **GO** locally / **BLOCKED: server** on the provider | Drills from the B2 download alone passed (runs 5-7); a drill on the provider's PostgreSQL 16 is required for "recovery ready" |
| 9B | A least-privilege role in active use | **BLOCKED: server** | Roles and grants tested (9B-1, 9B-2); "in active use" needs a production instance |
| 9C | Streamlit verifiably unreachable by an unauthenticated party in the target environment | **BLOCKED: server** | Rehearsed locally: LAN unreachable, `RESULT: private` (readiness 64); the on-host check, off-host check and a tunnel success must run on the server |
| 9D | Two concurrent API workers share one budget per `client_id` | **GO** | Real Redis 7: 4 processes, 80 attempts, exactly 25 admitted; 5 of 5 mutants caught (readiness 63) |
| 9E | Section 10's minimum: durable logs, alerting on `/v1/ready` and Discord crashes, cache-age visibility | **GO** (code) / **BLOCKED: server** (installation) | JSON logs + request IDs + redaction; `data-freshness`; check timers and journald retention prepared, not installed; alert channels tested once with authorization |
| 9E | CI runs the full 10-step workflow | Steps 1-6 **GO**; steps 7-10 **BLOCKED: server** | `tests`, `postgres` (+ Redis), `migrations`, `audit`, `images` all green on `52e487f`; approval, deploy, post-deploy health and rollback need a deploy target |

## 2. Threat controls (readiness section 13)

| Threat | Status | Basis |
|---|---|---|
| Credential leakage | **GO** | Secrets from read-only credential files (decision 9); log redaction; secret scans of each commit |
| Unauthorized Streamlit access | **BLOCKED: server** | See 9C |
| Token brute force / pre-auth DoS | **RESIDUAL** | Approved 2026-10-09: only SSH-key holders can reach the API. **Reopens before any public exposure.** |
| Trusted-proxy / IP spoofing | **GO** | No forwarded header is trusted (decision 3); the limiter keys on `client_id` only |
| SQL injection | **GO** | Parameterized SQLAlchemy throughout (unchanged) |
| Invalid request values | **GO** | Pydantic validation (unchanged) |
| Sensitive logging | **GO** | `RedactingFilter` registers every secret read from a file (9E-1) |
| Dependency vulnerabilities | **GO** | `pip-audit` in CI (`audit` job, green) |
| Legacy Discord commands / live outbound calls | **RESIDUAL** | Approved 2026-10-09: trusted guild only. **Reopens if any untrusted member joins the guild.** A membership review is required before each deployment. |
| Cross-process secret exposure | **GO** | Each process gets only its own credential files (`deploy/app/compose.secrets.example.yaml`) |
| Database over-privilege | **BLOCKED: server** | See 9B least-privilege role |
| Stale data | **GO** (Tier B) | Disclaimer shown; `data-freshness` reports cache age to the operator |
| Prediction misrepresentation | **GO** | Mandatory disclaimers, asserted verbatim by tests |

## 3. Release controls

| Control | Status | Basis |
|---|---|---|
| Branch protection on `main` | **RESIDUAL** | Approved, but GitHub returns 403 for a private repository without GitHub Pro. Substitute: fast-forward-only merges after green CI; no force pushes. **Reopens if the plan or visibility changes** (then enable it). |
| Log retention | **GO** (policy) | journald at most 1 GiB / 30 days, approved 2026-10-09 |

## 4. What turns NO-GO into GO

All of these need the paid server; none needs new code:
1. Provision bundle 1 (one Droplet + managed PostgreSQL 16) and run the provider probe (`napsedge-9b8c-handoff.tar`).
2. A restore drill on the provider's PostgreSQL 16 within the RTO; then `backup-status` can report recovery ready.
3. Install the systemd credentials, the backup timer, the app checks and journald retention; un-pause healthchecks.io.
4. Run the 9C on-host and off-host checks and one tunnel success.
5. Use the least-privilege runtime role in production; run CI steps 7-10 once against the server.
6. Review the Discord guild membership.

Then update this record and change the verdict only on evidence.
