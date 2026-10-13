# Phase 6A — Service-Layer and FastAPI Planning / Readiness Assessment

**Status: PLANNING ONLY. No services, routes, authentication, rate limiting, application wiring, production infrastructure, or deployment were implemented by this batch. No web server was started, no dependency was installed, no PostgreSQL instance was provisioned, no production import was run.** This document is the complete, evidence-based deliverable authorized by the Phase 6A batch. It records findings, an architecture recommendation, an ordered implementation slicing plan, and a set of open decisions and stop conditions requiring separate, explicit approval before any Phase 6 implementation batch begins.

Authoritative Phase 5 handoff: [`docs/phase5_final_acceptance.md`](phase5_final_acceptance.md). Authoritative schema: [`docs/database_schema.md`](database_schema.md). Authoritative identity design: [`docs/identity_scheme.md`](identity_scheme.md).

**Phase 6B update (2026-09-15): §21's recommended first slice (Slices 0–2) has been implemented and is recorded as a factual note at the end of §21 below.** The rest of this document is unchanged from the original Phase 6A planning pass and still describes proposed, not-yet-approved work for every domain beyond diagnostics.

---

## 1. Phase 5 handoff summary

Phase 5 delivered a complete, tested PostgreSQL persistence layer (`src/db/`) and a controlled bulk-import subsystem (`src/pipeline/bulk_import_*`, `src/pipeline/*_import_adapter.py`) capable of committing the **entire** real source tree (`TeamDump/CBB`, `TeamDump/ESPN`, season 2026 — 3,531 planned artifacts) into a disposable PostgreSQL 16 database, verified via two full rehearsals (Phase 5C3 representative subset, Phase 5C5 complete source) plus an identical replay of each, with zero unexplained drift. Final verdict: **Phase 5 COMPLETE, PASSED**, with three documented non-blocking limitations carried into Phase 6:

1. **Three teams have two real, cell-for-cell-identical AllStats source directories each** (Miami FL, Miami OH, Saint Joseph's) — both persist as fully independent, coexisting rows (natural keys include `source_artifact_id`); no row is silently "the real one."
2. **Unresolved ESPN abbreviation codes** (49 of 50, after Phase 5B6's audit) have no team mapping — any ESPN-sourced value for those codes cannot be attached to a canonical team.
3. Certain reconciliation metrics are explicitly documented as unavailable in `docs/controlled_import_rehearsal_design.md`, not silently defaulted.

**Production PostgreSQL has never been provisioned. No production import has been run.** Every table in `docs/database_schema.md` exists only inside disposable, since-destroyed rehearsal containers. Alembic head is `5b02bea0eb85` (six migrations), unchanged since Phase 5B7. `docs/database_schema.md`'s own "What is deliberately NOT in this schema yet" section explicitly defers Torvik normalized metrics, injury tables, ranking tables, market-snapshot/odds tables, prediction/simulation/recommendation/publication tables, and any result-beyond-final-score table — this is the direct, authoritative reason four of the eight Phase 6 domains (injury, odds, rankings, model performance) have zero database backing today.

## 2. Current service/API inventory

There is no service layer and no API layer to inventory as "existing Phase 6 work" — this section instead inventories what already plays an adjacent role:

| Category | What exists | Notes |
|---|---|---|
| Web framework | **None.** `grep -rl "FastAPI\|fastapi" .` returns zero matches anywhere in the repo. | Phase 6 starts from nothing, not a partial migration. |
| Existing HTTP server | `src/discord_bot/stripe_webhook.py` — a minimal Flask app (`app = Flask(__name__)`), one route, `POST /stripe/webhook`, verifies `stripe.Webhook.construct_event` and maps events to entitlement tiers. | A real, deployed precedent process that any new FastAPI layer must coexist with (separate process/port) or eventually absorb — not something Phase 6 can ignore. |
| Declared dependencies | `requirements.txt`/`requirements-dev.txt` declare **no** FastAPI, SQLAlchemy, Alembic, Pydantic, or uvicorn. | SQLAlchemy 2.0.52 and Alembic 1.19.1 are nonetheless **installed and load-bearing** for the entire Phase 5 pipeline — a real, pre-existing dependency-declaration gap, not something Phase 6 introduces. Pydantic is genuinely absent from the environment today (`grep -rl "pydantic\|BaseModel" src/` → no matches) — adding it is new. |
| Presentation layers | Streamlit (`app.py` + `pages/01`–`09`), Discord bot (`src/discord_bot/bot.py`, 504 lines, 9+ slash commands) | Both already call into shared `src/` functions directly (`run_matchup_pipeline`, `build_team_summary`) rather than duplicating analytics — a favorable precedent for "one service, multiple consumers." |
| Auth/entitlement precedent | `src/discord_bot/entitlements.py` (`TIER_ORDER`, `AccessDecision`, `set_user_entitlement`, `get_user_tier`, `check_access`, JSON-file-backed) + `grant_tier.py` CLI + `stripe_webhook.py` | A real, working paid-tier concept keyed by Discord user ID. Any Phase 6 authentication boundary decision must account for this, not invent a parallel scheme. |
| Typed contracts | No Pydantic, but extensive `@dataclass` usage across `src/identity/contracts.py`, `src/ingestion/*.py` (e.g. `AutoMarketResult`, `NetRankingIngestionResult`), `src/pipeline/live_data_adapter.py` (`LiveMarketFetchResult`, `MarketInputs`), `src/db/config.py` (`DatabaseConfig`). | These are candidate sources to wrap in Pydantic schemas, not re-derive — see §7. |
| Exception hierarchy | `src/exceptions.py`: `NapsEdgeError` (base), `ConfigurationError`, `IngestionError`, `IdentityResolutionError`, `ValidationError`, `SourceUnavailableError`, `IdentityConflictError`, `DatabaseTargetVerificationError`, `AdvisoryLockUnavailableError`. | Already a clean, documented, single-root hierarchy — see §10. |
| Session/DB ownership | `src/db/session.py`: `build_engine()`, `build_session_factory()`, `unit_of_work(session_factory)` context manager — commits once, rolls back the whole block on any exception, never autocommits, no repository method ever commits. | Directly reusable as a FastAPI dependency pattern — see §9. |
| Config injection point | `src/paths.py` (source of truth, `NAPSEDGE_ROOT_DIR`-overridable) vs. `src/config/paths.py` (a 41-line pure re-export shim, no independent behavior). `src/db/config.py`: `get_database_url()` reads `NAPSEDGE_DATABASE_URL`, falls back to a local dev SQLite file; never connects at import time. | No real dual-config risk today, just two import surfaces. |
| Tests | `146/146` passing in `tests/postgres/` (disposable-PostgreSQL suite), `4,259 passed + 1 expected skip` in the full non-live suite (unchanged baselines, not rerun this batch — see §16). **Zero** test files for `src/agents/*`, `src/ovr_model.py`, or `src/discord_bot/*`. | The layers Phase 6 would wrap most directly at the top (agents, ovr_model, discord bot) are exactly the untested ones. |

## 3. Eight-domain readiness matrix

Legend for **Service-wrapper readiness**: 🟢 ready for a thin read-only wrapper now · 🟡 ready with a named prerequisite · 🔴 not ready, needs a separately-approved batch first.

### 3.1 Player — 🟡 (split domain: raw stats 🟢, computed ratings/cards 🔴)

- **Available authoritative logic**: Two distinct sub-domains that must not be conflated:
  - *Raw persisted player data* (🟢): `RosterMembership`, `PlayerStatSnapshot` (`src/db/allstats_models.py`), `PlayerGameLogObservation` (`src/db/player_game_log_models.py`) — fully migrated, rehearsed at complete-source scale in Phase 5C5, zero notebook dependency. Repository access via `src/db/repositories.py`/`player_game_log_repositories.py`.
  - *Computed OVR ratings / player cards* (🔴): `src/ovr_model.py` (742 lines). Its `_load_notebook_engine()` (lines ~446–481) reads the live notebook file's raw JSON (`NOTEBOOK_PATH.read_text()`, `json.loads(...)`) and `exec()`s four specific notebook cells (indices 195, 196, 204, 207) at call time to obtain card-generation logic. This is not an importable, side-effect-free function — it has a live, fragile runtime dependency on the current contents of `cbb_teamScrape_v15_TEST.ipynb`.
- **Required inputs**: raw path — `team_id`/`player_id`, `season`. Computed path — same, plus the notebook's own in-memory globals the exec'd cells expect.
- **Returned outputs**: raw path — typed ORM rows (roster membership, stat snapshot, game-log observation). Computed path — an untyped dict/plot object from notebook cell execution.
- **Database dependencies**: raw path — yes, fully backed (Phase 5). Computed path — none (notebook-only).
- **External-source dependencies**: none for either sub-domain.
- **Current notebook dependencies**: raw path — none (fully extracted). Computed path — total, and specifically fragile (four hardcoded cell indices).
- **Output parity already proven?**: raw path — yes, via Phase 5C3/5C5 rehearsal reconciliation. Computed path — no parity test exists (§2 test-coverage table: zero test files touch `ovr_model.py`'s generation logic).
- **Missing prerequisites**: computed path needs its own separately-approved extraction/parity batch (mirroring the Phase 1 `scraper.py`/`visualizations.py` treatment) *before* any service wraps it — wrapping `_load_notebook_engine()` directly in an HTTP endpoint would mean a request handler executing live notebook cells, which is both fragile and a poor separation of concerns.
- **Recommended first service operation**: `GET /v1/players/{player_id}/stat-snapshots?season=` reading only the DB-backed raw tables — zero notebook exposure, proven parity, no new calculation.

### 3.2 Team — 🟡

- **Available authoritative logic**: Two parallel implementations exist for "team data," not yet unified (this duplication was already flagged in Phase 0/`migration_state.md`'s "three competing team-identity systems" finding and remains true today):
  - *New DB layer* (🟢, unused by any live read path): `Team`, `TeamAlias`, `Conference`, `TeamStatSnapshot`, `Game`, `GameResult`, `TeamGameLogObservation` — fully populated in rehearsal, zero production reads yet performed by anything.
  - *Existing filesystem layer* (🟢, actively used today): `src/team_data.py` (`load_team_all_stats`, `load_team_game_log`, `load_team_roster`, `load_team_advanced`, `list_teams`, `list_conferences`) and `src/features/team_summary.py`'s `build_team_summary(team_slug, season=2026) -> dict[str, Any]`, which reads AllStats/game-log/injury files directly from `TeamDump/CBB/` and is the actual function Streamlit and the Discord bot call today.
- **Required inputs**: `team_slug`/`team_id`, `season`.
- **Returned outputs**: DB path — typed ORM rows. Filesystem path — one large untyped dict spanning team ratings, top-player trends, and an injury sub-report (i.e. `build_team_summary` itself already crosses the player/team/injury domain lines the Phase 6 domain split assumes are separate — a genuine service-boundary design question, see §19).
- **Database dependencies**: DB path — full. Filesystem path — none.
- **External-source dependencies**: none for either.
- **Current notebook dependencies**: none for either (both are already `src/`-native).
- **Output parity already proven?**: DB path — yes (Phase 5C3/5C5). Filesystem path — implicitly proven only by ongoing production use, no regression test found for `build_team_summary` itself.
- **Missing prerequisites**: an explicit decision on which layer a Phase 6 "team" service reads from (§19, open decision — not silently resolved here).
- **Recommended first service operation**: `GET /v1/teams/{team_id}/stat-snapshots?season=` against the new DB layer — the safest possible first real read-path consumer of Phase 5's own tables, decoupled from the untyped, multi-domain `build_team_summary` dict.

### 3.3 Matchup — 🟡

- **Available authoritative logic**: `src/features/matchup_features.py::build_matchup_features(...)` — pure, deterministic diffing of two team summaries plus game context, no I/O. Wrapped one level up by `src/pipeline/orchestrator.py::run_matchup_pipeline(...)`, the function `pages/09_AI_Research_Control_Room.py` and the Discord bot actually call.
- **Required inputs**: `team_a_slug, team_b_slug, season=2026, home_team=None, neutral_site=True, spread/total/open_spread/close_spread/public_pct/money_pct=None, auto_market=False, run_agents=False, update_memory=False, report_path=None, output_path=None`.
- **Returned outputs**: one large untyped `dict[str, Any]` (`{"matchup_package", "agents", "memory", "report_path"}`), serialized today via `json.dumps(..., default=str)` — no schema.
- **Database dependencies**: none — reads local team data via `src/team_data.py`/`team_summary.py`, not the DB.
- **External-source dependencies**: **live**, conditionally — when `auto_market=True` (the Streamlit page's own default), `run_matchup_pipeline` calls `fetch_auto_market`, which hits `https://api.the-odds-api.com/...` in real time.
- **Current notebook dependencies**: none directly, but `run_agents=True` (also defaulted `True` in the Streamlit page) invokes `src/agents/orchestrator.run_hermes_agents`, whose six sub-agents (`coach_style_agent`, `market_signal_agent`, `matchup_reasoning_agent`, `quality_control_agent`, `report_agent`, `team_context_agent`) are pure, deterministic, rule-based computations over already-computed numeric features — **no LLM calls despite the "Hermes agents" name** — but are entirely untested (§2).
- **Output parity already proven?**: no — no test file exists for `run_matchup_pipeline`, `run_hermes_agents`, or any individual agent module.
- **Missing prerequisites**: (a) separating pure computation from the side effects `run_matchup_pipeline` currently bundles into one function (memory-file writes, markdown export, live odds fetch); (b) a decision on how a request-scoped endpoint should behave when `auto_market=True` would otherwise mean "this HTTP request blocks on a third-party API call" (§19/§20 — this is one of the explicit stop-condition triggers, "an endpoint depends on live scraping").
- **Recommended first service operation**: none in the first Phase 6 slice — defer until the player/team read-only slice proves the DI/session pattern, then wrap `build_matchup_features` (the pure function) directly rather than `run_matchup_pipeline` (the side-effecting orchestrator), with live-odds and agent invocation as explicit, separately-approved, opt-in extensions.

### 3.4 Strategy — 🔴

- **Available authoritative logic**: **none in `src/`.** `src/betting_engine.py` and `src/conference_dash.py` are both confirmed 0 bytes (`wc -l` = 0), unchanged since the initial commit. The notebook's own betting-model training section (~cell 641, using `sklearn`/`joblib`) has never been extracted — `docs/notebook_src_coverage.csv` lists roughly 31 functions in that section as "Missing from src."
- **Required inputs / returned outputs**: unknown — no `src/` contract exists to describe.
- **Database dependencies**: none possible today.
- **External-source dependencies**: unknown.
- **Current notebook dependencies**: total — this logic exists only as notebook cells.
- **Output parity already proven?**: not applicable — nothing to compare against.
- **Missing prerequisites**: a full extraction-and-parity batch (a proper Phase 2/4-style effort), which is out of scope for Phase 6 entirely until that extraction happens.
- **Recommended first service operation**: **none.** This domain cannot receive even a design-level endpoint proposal beyond a placeholder — see §8's explicit note. Attempting to build a "strategy" service today would mean writing new betting-edge calculation logic directly inside the service layer, which is exactly what the Phase 6 instructions prohibit ("do not recreate calculations... inside the service layer") since there is no calculation to wrap, only one to invent.

### 3.5 Injury — 🟡

- **Available authoritative logic**: Pure, tested identity/transformation modules (`src/identity/covers_identity.py`, `src/identity/injury_player_identity.py`, `src/transformations/injury_*_transformation.py`) — 6 test files, real coverage. The actual production read path is `src/features/team_summary.py::_injury_report(team_slug) -> dict[str, Any]` (line 166), which reads **local filesystem cache only**: `TeamDump/CBB/{team_slug}/{team_slug}_injuries.json` or `TeamDump/injury_cache/{team_slug}_injuries.json`, falling back to `{"injuries": [], "message": "No confirmed local injury report loaded."}`.
- **Required inputs**: `team_slug`.
- **Returned outputs**: a small typed-shape dict (injuries list + message).
- **Database dependencies**: none — `docs/database_schema.md` confirms no injury table exists.
- **External-source dependencies**: the **live-scraping functions** (`scrape_covers_injuries_for_teams`, `scrape_espn_injuries`, `compute_injury_deduction` in `src/scraper.py`) are confirmed **orphaned** — no caller anywhere outside `src/scraper.py` itself. Nothing currently populates the JSON cache files automatically; that population is presumably still manual/notebook-driven. This is a real operational gap, not a Phase 6 concern to silently fix.
- **Current notebook dependencies**: none for the cache-read path.
- **Output parity already proven?**: the pure identity/transformation pieces are well-tested; `_injury_report`/the cache-read path itself has no dedicated regression test found.
- **Missing prerequisites**: a decision on whether Phase 6's injury service reads the existing cache files as-is (🟢, honest about staleness) or waits for a scheduled scrape→cache job to be built (🔴, out of scope) — recommend the former, with an explicit "as of" staleness field surfaced rather than fabricated freshness.
- **Recommended first service operation**: `GET /v1/teams/{team_id}/injuries` reading the existing JSON cache directly, explicitly returning the existing "no confirmed local injury report loaded" message rather than inventing a default when the cache is absent (per the accepted-limitation handling in §17).

### 3.6 Odds — 🟡

- **Available authoritative logic**: `src/identity/odds_identity.py`, `src/identity/sportsbook_identity.py` — pure, deterministic, well-tested (`test_odds_identity.py` 24 tests, `test_sportsbook_identity.py` 12 tests). `src/ingestion/odds_ingestion.py` — pure parsing of already-fetched JSON, no network call, well-tested (38 tests).
- **Required inputs**: identity path — team slugs/names. Live-fetch path — `team_a_slug, team_b_slug`.
- **Returned outputs**: identity path — resolved team/sportsbook IDs. Live-fetch path — `AutoMarketResult(status, spread, total, open_spread, close_spread, source, matched_game, message)`, `status ∈ {"missing_key", "fetch_failed", "matched", ...}`.
- **Database dependencies**: none — `Sportsbook` identity table exists in `src/db/models.py`, but there is no odds/line/market-snapshot table (confirmed absent from `docs/database_schema.md`'s deferred list).
- **External-source dependencies**: **live and hard-required for any current value** — `src/ingestion/sportsbook_loader.py::fetch_auto_market` calls `https://api.the-odds-api.com/v4/sports/basketball_ncaab/odds` directly, keyed by `ODDS_API_KEY` from `.env`. A parallel, DB-pipeline-oriented wrapper already exists but has zero production consumers: `src/pipeline/live_data_adapter.py::fetch_live_market_inputs` (accepts an injectable `fetch_fn`, returns `LiveMarketFetchResult`/`MarketInputs`) — built during Phase 5 rehearsal work, never wired into anything live.
- **Current notebook dependencies**: none.
- **Output parity already proven?**: identity/ingestion layers yes; the live-fetch function itself has no meaningful "parity" concept (it's a live third-party call) and no test beyond whatever mocks exist for `fetch_auto_market`'s error branches.
- **Missing prerequisites**: a decision on whether a Phase 6 odds endpoint (a) calls the live API synchronously inside the request (creates a hard third-party dependency and latency/availability risk directly in the API's request path — a stop-condition trigger, §20) or (b) only serves the already-fetched value from `MarketInputs`/cached memory (`src/memory/market_memory.py`) and treats "fetch fresh odds" as a distinct, explicitly-labeled operation.
- **Recommended first service operation**: none in the first slice; when built, prefer serving `src/memory/market_memory.py`'s cached last-known value with an explicit `as_of`/`stale` field, with live refresh as a separate, clearly-labeled, rate-limited operation — not the default read path.

### 3.7 Rankings — 🟡

- **Available authoritative logic**: `src/ingestion/net_ranking_ingestion.py::run_net_ranking_ingestion(df, season=...)` and `load_and_ingest_net_rankings_csv(csv_path, season=...)` — explicitly documented as **never** performing a live request; reads a cached CSV fixture (`Reference Py Files/net_rankings_2026.csv`). `src/identity/net_ranking_identity.py::build_net_rankings_index(...)` + `attach_net_rank_via_registry(team_id, rankings_by_team_id) -> int | None` — pure, deterministic dict lookups; well-tested (63 tests across ingestion/identity/transformation).
- **Required inputs**: `team_id`, `season`.
- **Returned outputs**: `int | None` (NET rank) via the registry path; a full rankings DataFrame via ingestion.
- **Database dependencies**: none — confirmed absent from `docs/database_schema.md`.
- **External-source dependencies**: the one function capable of a live fetch, `build_net_rankings_from_ncaa_v15(url, ..., fetch_fn=None)`, **raises `ValidationError` if `fetch_fn` is omitted** — i.e. it is structurally incapable of a silent live call; nothing in current production wiring supplies a `fetch_fn`, so today's actual data source is the cached CSV only.
- **Current notebook dependencies**: none.
- **Output parity already proven?**: yes, well-tested at the ingestion/identity/transformation layers.
- **Missing prerequisites**: confirm exactly where `build_team_summary`/`build_matchup_features` currently surface NET rank (not yet directly traced this batch — a small follow-up read, not a blocker).
- **Recommended first service operation**: `GET /v1/teams/{team_id}/net-rank?season=` reading the cached CSV via the existing ingestion/identity functions — pure, safe, zero live dependency, strong existing test coverage to build parity tests against.

### 3.8 Model performance — 🔴

- **Available authoritative logic**: **none.** `src/results_service.py::load_postmortems()`/`load_line_snapshots()` and `src/bet_tracker_service.py::load_bet_log()`/`load_pending_bets()` are raw file loaders (jsonl/json/parquet → DataFrame) with zero computation. No hit-rate, ROI, calibration, or accuracy calculation was found anywhere in `src/`, `pages/02_Bet_Tracker.py`, or `pages/03_Historical_Results.py` (confirmed via targeted keyword search: no `.mean()`/`groupby`/hit-rate-style computation in those pages).
- **Required inputs / returned outputs**: unknown — no contract exists.
- **Database dependencies**: none — no prediction/result/performance table exists in the schema (confirmed deferred).
- **External-source dependencies**: none.
- **Current notebook dependencies**: none found, but also nothing to point to — this is a genuine gap, not an unmigrated notebook feature.
- **Output parity already proven?**: not applicable.
- **Missing prerequisites**: this domain requires **net-new analytical-module work** (not extraction) before any service wrapper is meaningful — computing hit-rate/ROI/calibration from `bet_log.parquet` + `postmortem_log.jsonl` is a real design task with its own edge cases (open bets, pushes, void bets) that must not be invented ad hoc inside a service or route.
- **Recommended first service operation**: **none.** Same posture as Strategy (§3.4) — a placeholder in the endpoint catalog only, pending a separately-approved analytical-module design batch.

**Summary**: of 8 domains, 2 have zero wrappable logic today (Strategy, Model performance) and must be explicitly excluded from Phase 6's first implementation batches; the remaining 6 all have at least a read-only, DB-or-cache-backed, low-risk sub-slice, but 4 of those 6 (Player's computed half, Matchup, Injury, Odds) carry a named live-dependency or notebook-dependency risk that must not be silently absorbed into a route handler.

## 4. Authoritative dependency map

| Domain | Authoritative computation | Authoritative persistence | Authoritative external source |
|---|---|---|---|
| Player (raw) | none needed (pure data) | `src/db/allstats_models.py` (`RosterMembership`, `PlayerStatSnapshot`), `src/db/player_game_log_models.py` (`PlayerGameLogObservation`) via `src/db/repositories.py`/`player_game_log_repositories.py` | none |
| Player (computed) | `src/ovr_model.py` (notebook-exec, not portable) | none | notebook file itself (`NOTEBOOK_PATH`) |
| Team | `src/features/team_summary.py::build_team_summary` (filesystem) | `src/db/models.py` (`Team`, `Conference`, `TeamAlias`), `src/db/allstats_models.py` (`TeamStatSnapshot`), `src/db/game_log_models.py` (`Game`, `GameResult`, `TeamGameLogObservation`) | none |
| Matchup | `src/features/matchup_features.py::build_matchup_features`; orchestrated by `src/pipeline/orchestrator.py::run_matchup_pipeline` | none directly (composes Team/Player/Odds) | The Odds API (conditional, via Odds domain) |
| Strategy | none exists | none | none |
| Injury | `src/transformations/injury_*_transformation.py`, `src/identity/covers_identity.py`, `src/identity/injury_player_identity.py`; read path `team_summary.py::_injury_report` | none (file cache only: `TeamDump/CBB/*/*.json`) | Covers.com / ESPN (orphaned scrape functions, not wired to the cache) |
| Odds | `src/identity/odds_identity.py`, `src/identity/sportsbook_identity.py`, `src/ingestion/odds_ingestion.py` | `src/db/models.py::Sportsbook` (identity only, no line data) | The Odds API, via `src/ingestion/sportsbook_loader.py::fetch_auto_market` |
| Rankings | `src/identity/net_ranking_identity.py`, `src/ingestion/net_ranking_ingestion.py` | none (cached CSV: `Reference Py Files/net_rankings_2026.csv`) | NCAA.com (only reachable via an injected `fetch_fn` that nothing currently supplies) |
| Model performance | none exists | none (raw files: `bet_tracker/bet_log.parquet`, `TeamDump/postmortem_log.jsonl`) | none |

## 5. Proposed service-layer architecture

**Recommendation**: a synchronous FastAPI application, one process, with services organized by domain module under a new `src/services/` package — **not** by wrapping `src/agents/`, `src/ovr_model.py`, or `run_matchup_pipeline` directly, but by composing the already-tested lower-layer functions named in §4, mirroring exactly the "repositories own persistence, services coordinate, analytical modules own calculations" separation the user's instructions require.

```
src/services/
    __init__.py
    contracts.py          # shared result/error types, pagination envelope
    player_service.py      # wraps src/db/repositories.py + player_game_log_repositories.py
    team_service.py         # wraps src/db/repositories.py (Team/TeamStatSnapshot/Game)
    injury_service.py       # wraps src/features/team_summary._injury_report (extracted to a public fn)
    rankings_service.py     # wraps src/identity/net_ranking_identity + ingestion
    # matchup_service.py, odds_service.py, strategy_service.py, model_performance_service.py:
    # deferred — see §3.3/§3.4/§3.6/§3.8

src/api/
    __init__.py
    main.py                 # FastAPI() app, router includes, exception handlers
    deps.py                  # DI: get_session(), get_settings()
    schemas/
        player.py, team.py, injury.py, rankings.py   # Pydantic request/response models
    routers/
        player.py, team.py, injury.py, rankings.py, health.py
```

Each service function takes a `Session` (injected, never created internally) plus typed, primitive arguments, and returns either a `@dataclass` result or raises a `src.exceptions.NapsEdgeError` subclass — never an HTTP-aware object. Routers translate service results/exceptions into Pydantic response models / HTTP status codes and do nothing else. This mirrors the existing `unit_of_work`/repository discipline exactly — Phase 6 is additive on top of an already-correct pattern, not a new one.

## 6. Service-to-repository/calculation boundaries

- **Repositories** (`src/db/repositories.py` and siblings) remain the only code that issues SQLAlchemy queries. Services never construct a `select(...)` themselves.
- **Analytical modules** (`src/features/*`, `src/identity/*`, `src/transformations/*`) remain the only code that computes a derived value (a diff, a rank attachment, an injury impact). Services never re-derive a number a module already produces.
- **Services** call one or more repositories and/or analytical functions, assemble the result, and apply no business rule of their own beyond "which repository/module to call and how to shape the response." A service function that starts containing an `if`/`else` deciding a *numeric outcome* (not just a not-found/ambiguous branch) is a signal that logic is leaking into the wrong layer — a stop condition per the user's own instruction ("a service would require duplicate business logic").
- **Concrete example of the boundary working today**: `src/discord_bot/bot.py` already calls `run_matchup_pipeline`/`build_team_summary` directly rather than reimplementing them — the Discord bot is already, informally, behaving like a Phase 6 "consumer of a shared service," which is a favorable existing precedent, not something Phase 6 has to invent.
- **Concrete example of a boundary NOT yet clean**: `build_team_summary` (§3.2) itself mixes team, player-trend, and injury concerns in one untyped dict — a Phase 6 `team_service`/`player_service`/`injury_service` split would need to either (a) keep calling `build_team_summary` once and re-shape its output into three typed responses, or (b) decompose `build_team_summary` itself into three smaller functions. Decomposing is out of scope for Phase 6A (it would touch an active, unverified-parity function) — recorded as an open decision, §19.

## 7. Typed contract strategy

**Recommendation**: adopt Pydantic v2 (net-new dependency, currently absent) for the API's external request/response schemas only — never inside `src/db/`, `src/identity/`, `src/ingestion/`, or `src/transformations/`, which keep their existing `@dataclass`/plain-dict conventions untouched. Two explicit conversion points:

1. **ORM → Pydantic** at the service boundary: a `player_service` function returns a small `@dataclass` (matching the existing codebase convention) or the ORM row itself; the router converts it to a Pydantic response model via `model_validate(obj, from_attributes=True)`. Repositories and services are never annotated with Pydantic types directly, keeping `src/db/` fully decoupled from the web layer.
2. **Existing dataclasses → Pydantic** for external-source results that already have a shape (`AutoMarketResult`, `NetRankingIngestionResult`, `MarketInputs`) — wrap, do not reimplement, their fields in a matching Pydantic model so the API layer's schema and the ingestion layer's dataclass cannot silently drift (a shared "field name and type" contract test is one of the testing-strategy items in §16).

This keeps Pydantic strictly at the API's edge, consistent with "Pydantic schemas own external serialization/validation" and "routes must not contain... business rules" from the required separation of responsibilities.

## 8. Proposed versioned endpoint catalog

All paths under `/v1`. "Readiness" mirrors §3.

| Method & Path | Purpose | Request | Response schema | Service method | Auth | Expected errors | Pagination/caching | Readiness |
|---|---|---|---|---|---|---|---|---|
| `GET /v1/health` | Liveness only | — | `{status: "ok"}` | none | none | — | none | 🟢 |
| `GET /v1/ready` | Readiness incl. DB connectivity | — | `{status, checks: {db: bool}}` | `deps.get_session` ping | none | 503 if DB unreachable | none | 🟢 |
| `GET /v1/teams/{team_id}` | Team identity + latest stat snapshot | path: `team_id`; query: `season` | `TeamSummaryResponse` | `team_service.get_team_snapshot` | required (public-vs-tiered TBD, §19) | 404 unknown team, 404 no snapshot for season | cache: short-TTL per season | 🟢 |
| `GET /v1/teams/{team_id}/net-rank` | NET rank lookup | query: `season` | `NetRankResponse{team_id, season, net_rank: int\|None}` | `rankings_service.get_net_rank` | required | 404 unknown team; `net_rank: None` (not 404) if unranked/unmapped | cache: long-TTL (cached CSV source) | 🟢 |
| `GET /v1/teams/{team_id}/injuries` | Current injury report | — | `InjuryReportResponse{injuries: [...], as_of: str\|None, message: str\|None}` | `injury_service.get_injury_report` | required | never 404 — returns the existing "no confirmed local injury report loaded" message instead (§17) | cache: short-TTL, cache-bust on file mtime | 🟡 (needs `_injury_report` extracted to a public, testable function first) |
| `GET /v1/players/{player_id}/stat-snapshots` | Raw persisted player stats | query: `season` | `list[PlayerStatSnapshotResponse]` | `player_service.list_stat_snapshots` | required | 404 unknown player | pagination: `limit`/`cursor` | 🟢 |
| `GET /v1/players/{player_id}/game-log` | Raw persisted player game log | query: `season` | `list[PlayerGameLogResponse]` | `player_service.list_game_log` | required | 404 unknown player | pagination | 🟢 |
| `GET /v1/players/{player_id}/card` | Computed OVR card/rating | query: `season` | — | — | — | — | — | 🔴 blocked on §3.1's computed-path prerequisite; **not proposed for implementation until that batch lands** |
| `GET /v1/matchups/{team_a}/{team_b}/features` | Pure matchup feature diff | query: `season`, `home_team`, `neutral_site` | `MatchupFeaturesResponse` | `matchup_service.get_features` (new, wraps `build_matchup_features` only, not `run_matchup_pipeline`) | required | 404 unknown team(s) | none (cheap, pure) | 🟡 (needs `matchup_service` built; excludes live odds/agents) |
| `POST /v1/matchups/{team_a}/{team_b}/report` | Full matchup pipeline incl. live odds + agents | body: game context, `auto_market: bool` | `MatchupReportResponse` | `matchup_service.run_full_report` | required, tier-gated (paid feature) | 502 if live odds fetch fails and `auto_market=true` | none (explicit action, not cached) | 🔴 stop condition — live-scraping-in-request-path decision required first (§19/§20) |
| `GET /v1/odds/{team_a}/{team_b}` | Last-known cached market line | — | `MarketSnapshotResponse{..., as_of, stale: bool}` | `odds_service.get_cached_market` | required, tier-gated | 404 no cached line | short-TTL cache | 🔴 needs `odds_service` + memory-read wrapper built (currently no service exists) |
| `POST /v1/odds/{team_a}/{team_b}/refresh` | Force a live odds fetch | — | `MarketSnapshotResponse` | `odds_service.refresh_market` | required, tier-gated, rate-limited harder than reads | 502/503 on API failure, `missing_key` mapped to 500 (server misconfiguration, not client error) | explicit rate limit (third-party API quota) | 🔴 same as above; live-dependency stop condition |
| `GET /v1/strategy/**` | Betting edge/recommendation | — | — | — | — | — | — | 🔴 **not proposed** — no authoritative logic exists (§3.4) |
| `GET /v1/model-performance/**` | Historical accuracy/ROI | — | — | — | — | — | — | 🔴 **not proposed** — no authoritative logic exists (§3.8) |

Per the user's explicit instruction, **none of these endpoints are implemented by this batch** — this table is a proposal for future, separately-approved slices.

## 9. Dependency-injection strategy

**Recommendation**: reuse `src/db/session.py`'s existing `build_session_factory`/`unit_of_work` primitives directly as the basis for a FastAPI dependency, rather than inventing a second session-management convention:

```python
# src/api/deps.py (proposed, not implemented)
def get_session() -> Iterator[Session]:
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

This is `unit_of_work`'s own body, adapted to FastAPI's `yield`-dependency shape — one request = one transaction, committed once at the end of a successful request, rolled back whole on any exception, exactly matching the invariant Phase 5A already established ("no commit call inside any repository method"). Routers depend on `Session = Depends(get_session)`, pass it straight to a service function, which passes it straight to a repository constructor — no new session-ownership rule is introduced, only a new entry point into the existing one.

Configuration (`get_database_url()`, `NAPSEDGE_ROOT_DIR`) is injected the same way — a `get_settings()` dependency wrapping the existing `src.db.config`/`src.paths` functions, not a parallel settings object, so `NAPSEDGE_DATABASE_URL`/`NAPSEDGE_ROOT_DIR` overrides continue to be the single source of truth for both the CLI/notebook pipeline and the API.

## 10. Exception and HTTP-error mapping

**Recommendation**: add one FastAPI exception handler per existing `src.exceptions.NapsEdgeError` subclass rather than inventing new HTTP-specific exception types:

| `src.exceptions` type | HTTP status | Notes |
|---|---|---|
| `ValidationError` | 422 | Matches FastAPI's own convention for request-shape problems |
| `IdentityConflictError` | 409 | A genuine conflict, not a client input error |
| `IdentityResolutionError` | 500 | Programmer/registry error, not a normal "not found" |
| `SourceUnavailableError` | 503 | External source unreachable (odds API, live scrape) |
| `ConfigurationError` | 500 | Missing/invalid server config (e.g. `ODDS_API_KEY` absent) — never surfaced as a client error |
| `DatabaseTargetVerificationError`, `AdvisoryLockUnavailableError` | 500 | Bulk-import-specific; should never actually surface through the read-oriented API — included for completeness only |
| `IngestionError` | 500 | Not expected on a read path; relevant only if a future write endpoint is added |
| unresolved/ambiguous identity (not an exception — `UnresolvedIdentity` per `src.identity.contracts`) | 404 or 200 with `null` field, per-endpoint (§8 shows both patterns) | The codebase's own doc note is explicit that "didn't find a match" is a normal outcome, not an error — routers must not force it through the exception-handler path |

A base handler for `NapsEdgeError` itself (500, generic message, full detail only in server logs) catches anything not explicitly mapped, so no unhandled `NapsEdgeError` subclass added later silently produces a raw 500 traceback to a client.

## 11. Authentication options

**Open decision — not silently selected.** Three real options, given the existing entitlement precedent (§2):

1. **API-key header, backed by the existing `src/discord_bot/entitlements.py` JSON store.** Fastest to build, reuses `TIER_ORDER`/`check_access` as-is. Weakness: that store is keyed by Discord user ID and is a flat JSON file — not designed for concurrent API traffic or a non-Discord identity.
2. **Migrate entitlements into the Phase 5 database** (a new `UserEntitlement`/`ApiKey` table) and authenticate against it. Cleaner long-term, but **requires a new migration** — an explicit stop condition per the user's own list ("a new migration appears necessary"). Not something this batch decides to do.
3. **OAuth/SSO via a third-party provider**, decoupled entirely from the Discord-tier system. Most standard for a general API, but throws away the existing entitlement precedent and requires reconciling two identity systems (Discord user ID vs. OAuth subject) — a real new design problem, not a Phase 6 detail.

**Recommendation for the first slice**: none of the three — the first implementation slice (§18) is internal-only (localhost, no auth), explicitly deferring the auth decision until a slice actually needs to protect paid content (Odds/Matchup-report/Strategy). Building auth prematurely, before deciding which of the three fits, risks exactly the "silently choose a decision that would materially constrain later applications, security, or deployment" the user's instructions prohibit.

## 12. Rate-limiting options

**Open decision.** Two boundaries, not one:

1. **Client-facing rate limiting** (protect the API from abusive callers) — a standard token-bucket middleware (e.g. `slowapi`, another new dependency) keyed by API key/IP.
2. **Upstream-facing rate limiting** (protect The Odds API quota from being exhausted by Phase 6 traffic) — this is the more urgent of the two, since `fetch_auto_market`'s failure mode on quota exhaustion is `status="fetch_failed"`, not a clean 429; a shared, server-side cache/backoff in `odds_service` (not per-request) is needed before any odds endpoint goes live, independent of client-facing limiting.

**Recommendation**: design the upstream-facing limiter as part of `odds_service` itself (§8's `odds_service.refresh_market`) when that slice is built; defer client-facing rate limiting to whichever slice first exposes the API beyond localhost.

## 13. Health/readiness design

Two distinct, standard endpoints (already reflected in §8): `/v1/health` (process is up, no dependency checks — used for basic liveness) and `/v1/ready` (checks `get_session()` can open and query, per the DI pattern in §9 — used before registering the process as ready to receive traffic). Neither performs a live external call (odds/scrape) — a readiness check that depends on The Odds API's uptime would make the service's own health falsely coupled to a third party's, which is worth naming explicitly so a later batch doesn't add that coupling by habit.

## 14. Caching and pagination strategy

- **Pagination**: cursor-based (`limit` + opaque `cursor`) for the two genuinely large collections (`player_service.list_stat_snapshots`, `list_game_log`) — matches the scale already proven in Phase 5C5 (tens of thousands of rows in `player_game_log_observations`). Offset-based pagination is explicitly not recommended given that scale.
- **Caching boundary**: cache only at the service layer (e.g. an in-process TTL cache in front of `rankings_service`/`injury_service`'s file reads), never inside a repository (which must always reflect the current transaction) and never inside a router (which must stay pure translation). NET rankings (cached CSV, changes rarely) and injuries (cached JSON, changes per-scrape) both benefit from a short-to-medium TTL cache in front of file I/O; team/player DB reads do not need caching in the first slice (SQLAlchemy + a local disposable/dev database is already fast).

## 15. Structured-logging strategy

**Recommendation**: adopt a request-scoped correlation ID (a FastAPI middleware assigning a UUID per request, attached to Python's `logging` via a `contextvars`-backed filter) and structured (JSON) log records for the API layer specifically. `src/` modules keep their current logging conventions unchanged — this is additive at the API boundary only, not a retrofit of the entire codebase. No existing structured-logging convention was found in `src/` to reuse or conflict with (not treated as a gap to fix outside Phase 6's own scope).

## 16. Testing and parity plan

Per the batch's own instruction, this section **designs, but does not implement**, the following:

1. **Deterministic service results**: unit tests calling each service function directly (no HTTP layer) against a SQLite in-memory or tmp-path database (via `build_session_factory`/`unit_of_work`, already proven testable in the Phase 5 suite) — fixture rows inserted via the existing repositories, then read back through the new service function.
2. **Typed-schema validation**: Pydantic model tests asserting required/optional fields and type coercion for each response schema, independent of any live service call.
3. **Serialization stability**: snapshot/contract tests on the OpenAPI schema itself (see §16.13) plus explicit UTC-timestamp round-trip tests (serialize → parse → compare).
4. **Missing/ambiguous identities**: tests asserting the 404-vs-`null`-field pattern (§8, §10) is applied consistently — an unresolved team/player must never silently produce a 200 with fabricated data.
5. **Unavailable/partial data**: tests for the injury "no confirmed local injury report loaded" path and the three duplicate-directory teams (§17) explicitly, since both are named Phase 5 limitations that must surface honestly.
6. **Repository/session ownership**: a test that a raised exception inside a service function results in zero committed rows (reusing the existing `unit_of_work` rollback test pattern from `tests/postgres/`).
7. **API status-code translation**: one test per row of the §10 exception-mapping table, asserting the exact HTTP status.
8. **Authentication**: deferred until §11 is resolved and an auth mechanism actually exists to test.
9. **Rate limiting**: deferred until §12 is resolved.
10. **Health/readiness**: tests that `/v1/ready` returns 503 when the injected session dependency raises (simulated DB-down), and 200 otherwise.
11. **Pagination**: tests asserting cursor stability across a page boundary using a seeded multi-row fixture.
12. **Dependency overrides**: FastAPI's own `app.dependency_overrides` mechanism to swap `get_session` for a test session in every API-level test — standard, no new pattern needed.
13. **OpenAPI contract stability**: a committed golden-file snapshot of `app.openapi()`'s JSON, diffed in CI/test runs so an accidental schema change is caught explicitly rather than silently shipped.
14. **Output parity**: for `team_service`/`player_service`, parity means "matches what the Phase 5C5-rehearsed database actually contains" (already proven at the DB layer) — the new parity surface is service-layer shaping, not recomputation, so tests assert the service's typed output matches the raw ORM row's fields, not a fresh independent calculation.

**No live scraping requirement**: every test above uses fixture data (in-memory/tmp-path SQLite, or fixture JSON/CSV files mirroring `TeamDump/CBB/*` and `Reference Py Files/net_rankings_2026.csv`) — consistent with the existing non-live test suite's own convention. **Disposable PostgreSQL** (`tests/postgres/conftest.py`'s `PostgresContainer` primitives) is reserved, per the user's own instruction, for later integration tests that genuinely need real PostgreSQL semantics (e.g. verifying the FastAPI DI session dependency behaves correctly against a real transaction, not just SQLite) — not for the bulk of service-layer unit tests, which do not need it.

## 17. Phase 5 limitation handling at the service boundary

| Limitation | How it must surface (not change the underlying data) |
|---|---|
| Unresolved ESPN abbreviations (49 codes) | Any ESPN-sourced field must be capable of returning `null`/absent rather than a fabricated team association; an endpoint must never silently drop or guess-map an unresolved code. |
| Explicitly unavailable reconciliation metrics (`docs/controlled_import_rehearsal_design.md`) | Any endpoint touching reconciliation-adjacent data must expose an explicit "unavailable" state distinct from zero/empty, matching the existing reconciliation-reader's own documented behavior. |
| Partial-but-persistable data (Phase 5C2A) | A `planned_insert`-but-partial artifact's resulting rows are ordinary persisted rows once committed — no special service-layer handling needed; the distinction is a bulk-import-time concept, not a query-time one. |
| Three teams with two coexisting, cell-identical AllStats directories (Miami FL, Miami OH, Saint Joseph's) | **This is a genuine open service-design question, not resolved here.** A "get the latest team stat snapshot" query for one of these three teams will find two valid, non-overwriting rows for the same team/season. Options: return both with their distinguishing `source_artifact_id`/timestamp; pick the newer one by convention with an explicit note in the response; or require season+source_artifact_id as a compound key for these three teams only. None is silently chosen — see §19. |
| Production PostgreSQL not yet provisioned | Every Phase 6 slice through local development targets the SQLite dev fallback or a disposable PostgreSQL test container — never a claim of "production-ready" until a separate, explicitly-approved provisioning batch happens. |

No uncertain or unavailable value is converted into a fabricated default anywhere in this design — every "missing" case above resolves to `null`, an explicit message field, or an explicit 404/503, never a guessed number.

## 18. Ordered Phase 6 implementation slices

Each slice is small, read-only where possible, and independently testable — no single large batch.

**Slice 0 — Dependency and scaffolding hygiene.** Add `fastapi`, `pydantic`, `uvicorn` to `requirements.txt` (and formally declare the already-installed `sqlalchemy`/`alembic`, closing the real gap named in §2). Create `src/api/`, `src/services/` package skeletons (empty `__init__.py` + `main.py` with a bare `FastAPI()` instance and `/v1/health`). No database access yet. Tests: app imports and `/v1/health` returns 200 via `TestClient`. Dependencies: none. Stop conditions: none expected. Exclusions: no DI, no other routes.

**Slice 1 — Shared contracts and exception mapping.** Build `src/services/contracts.py` (pagination envelope, shared error/result shapes) and the `NapsEdgeError`-to-HTTP handler set from §10, registered on the Slice 0 app. Tests: one test per exception-mapping row, using a deliberately-raising dummy route. Dependencies: Slice 0. Stop conditions: none expected — this is pure plumbing over an already-decided exception hierarchy.

**Slice 2 — DI and `/v1/ready`.** Implement `src/api/deps.py::get_session`/`get_settings` per §9, and `/v1/ready` per §13. Tests: `/v1/ready` returns 200 against a tmp-path SQLite DB, 503 when the dependency is overridden to raise. Dependencies: Slices 0–1. Stop conditions: none expected.

**Slice 3 — Read-only `team_service` + `/v1/teams/{team_id}`, DB-backed.** The first real service, reading `Team`/`TeamStatSnapshot`/`Conference` via `src/db/repositories.py`, and the first real production read path for the Phase 5 database. Tests: service-level (SQLite fixture) + one API-level test via `TestClient` + `dependency_overrides`. Parity evidence: response fields match the ORM row fields the Phase 5C5 rehearsal already proved correct. Dependencies: Slices 0–2. Stop conditions: if `Team`/`TeamStatSnapshot` field shapes turn out to need a schema change to serve sensibly over HTTP, stop and report per "a new migration appears necessary." Exclusions: no team-summary dict, no player/injury data, no live source.

**Slice 4 — Read-only `player_service` (raw tables only) + `/v1/players/{player_id}/stat-snapshots`, `/game-log`.** Mirrors Slice 3 exactly for `RosterMembership`/`PlayerStatSnapshot`/`PlayerGameLogObservation`, including cursor pagination (§14). Explicitly excludes any OVR/card computation (§3.1, §8). Dependencies: Slices 0–3 (reuses the same DI/exception plumbing). Stop conditions: none expected — same low-risk shape as Slice 3.

**Slice 5 — `rankings_service` + `/v1/teams/{team_id}/net-rank`.** Wraps the cached-CSV-backed identity/ingestion functions (§3.7) — zero live dependency, strong existing test coverage to build the parity test against. Dependencies: Slice 3 (needs `team_id` resolution). Stop conditions: none expected.

**Slice 6 — `injury_service` + `/v1/teams/{team_id}/injuries`.** Requires first extracting `team_summary.py::_injury_report` into a standalone, public, directly-testable function (a small, scoped refactor of one already-tested-adjacent function, not a new calculation) — flagged explicitly here rather than silently done inside the service. Dependencies: Slice 3. Stop conditions: if extracting `_injury_report` turns out to change `build_team_summary`'s own behavior or require touching `src/ovr_model.py`/other protected calculation code, stop and report rather than proceed.

**Slice 7 — `matchup_service.get_features` (pure) + `/v1/matchups/{a}/{b}/features`.** Wraps `build_matchup_features` only — no live odds, no agents, no orchestrator side effects. Dependencies: Slices 3–4 (needs team/player data to build the two summaries it diffs). Stop conditions: none expected for the pure-diff endpoint itself.

**Slices 8+ (explicitly deferred, not scheduled by this batch)**: `odds_service`/live-market endpoints (blocked on the §11/§12/§19 auth and rate-limit decisions plus the live-scraping-in-request-path stop condition), `matchup_service.run_full_report` (same), the computed-player-card path (blocked on the ovr_model extraction batch, §3.1), Strategy and Model-performance domains (blocked on net-new analytical-module design batches, §3.4/§3.8), and any authentication/production-deployment work.

## 19. Open decisions requiring approval

1. **Team/Player read source**: new DB tables (proven, zero production precedent) vs. existing filesystem layer (`team_data.py`/`team_summary.py`, actively used today) — this design recommends the DB for Slices 3–4 specifically to give Phase 5's own tables their first real consumer, but this is a recommendation, not a decision already made.
2. **Duplicate-team-directory representation** (§17) — return both coexisting rows, pick-by-convention, or require a compound key, for exactly the three affected teams (Miami FL, Miami OH, Saint Joseph's).
3. **Authentication mechanism** (§11) — reuse/extend the Discord-JSON entitlement store, migrate it into a new DB table (requires a migration), or adopt OAuth/SSO.
4. **Rate-limiting scope** (§12) — client-facing, upstream-API-quota-facing, or both, and on what implementation.
5. **`build_team_summary` decomposition** (§6, §3.2) — leave it as one multi-domain function and re-shape its output per-service, or split it into team/player-trend/injury sub-functions (a scoped refactor of an active, currently-unparity-tested function).
6. **Live-dependency-in-request-path policy** for Matchup's `run_full_report` and any Odds refresh endpoint (§3.3, §3.6, §8) — synchronous inline call, cached-only default with an explicit separate refresh action (recommended), or an async background-job pattern.
7. **`_injury_report` extraction** (Slice 6) — confirmed safe to extract as a pure refactor, or does it carry hidden coupling to `build_team_summary`'s other fields that needs its own review first.
8. **Whether/when to formally deprecate `src/config/paths.py`** (§2) — no functional risk today, purely a housekeeping decision, deliberately not acted on in this batch.

None of the above was silently decided by this document — each is recorded with its alternatives and this design's own recommendation where one is offered, per the user's explicit instruction.

## 20. Risks and stop conditions

**Named risks** (not necessarily blocking, but must inform slice ordering): (a) `run_matchup_pipeline`/`src/agents/*` are entirely untested — any slice touching them needs new regression tests before being trusted, not just before being wrapped; (b) the Odds API's rate limit/uptime is an external dependency the API has no control over; (c) `_load_notebook_engine()`'s notebook-cell-index coupling (§3.1) means the "player card" domain can silently break if the notebook is ever edited, independent of any Phase 6 work; (d) the injury cache has no automated population job — a Phase 6 injury endpoint's data can go stale indefinitely without anyone noticing unless the `as_of`/staleness field (§17) is actually surfaced in the UI layer that consumes it.

**Stop conditions actually triggered by this assessment** (per the user's own verbatim list — recorded here, not acted on):
- *"An endpoint depends on live scraping"* — triggered by the proposed Odds-refresh and Matchup-full-report endpoints (§8, §3.3, §3.6). **Not implemented; explicitly deferred to Slices 8+ pending the decision in §19.6.**
- *"Authentication/public-access policy must be selected"* — triggered; see §19.3. **Not selected by this batch.**
- *"The proposed API would expose sensitive information"* — the Odds/Matchup-report/Strategy domains represent the product's paid content (already tier-gated via Discord entitlements); an unauthenticated API exposing them would bypass that gating. **No such endpoint is scheduled before §19.3 is resolved.**
- *"A documented Phase 5 limitation prevents a truthful service response"* — triggered by the three duplicate-team-directory teams (§17, §19.2). **Recorded, not resolved, here.**
- *"A service would require duplicate business logic"* — would be triggered by attempting Strategy or Model-performance service wrappers today, since no authoritative logic exists to wrap (§3.4, §3.8). **Neither domain is scheduled for implementation; both require a separate, prior analytical-module design batch.**

No other listed stop condition (new migration required for a scheduled slice, ownership unresolved, production infrastructure required for a scheduled slice) is currently triggered by the slices actually proposed in §18 — each of Slices 0–7 was deliberately scoped to avoid them.

## 21. Exact recommended first implementation slice

**Slices 0–2** (dependency/scaffolding hygiene, shared contracts and exception mapping, DI and `/v1/ready`), as a single small, low-risk, separately-approved Phase 6 batch:

- **Exact scope**: add `fastapi`/`pydantic`/`uvicorn` (and declare `sqlalchemy`/`alembic`) to `requirements.txt`; create `src/api/main.py`, `src/api/deps.py`, `src/services/contracts.py`; implement `/v1/health`, `/v1/ready`, and the full `src.exceptions`-to-HTTP mapping from §10 wired as FastAPI exception handlers.
- **Intended files**: `requirements.txt` (edit); `src/api/__init__.py`, `src/api/main.py`, `src/api/deps.py` (new); `src/services/__init__.py`, `src/services/contracts.py` (new); `tests/api/test_health.py`, `tests/api/test_ready.py`, `tests/api/test_exception_mapping.py` (new).
- **Tests**: `TestClient`-based tests for `/v1/health` (always 200), `/v1/ready` (200 against a tmp-path SQLite session, 503 with an overridden failing dependency), and one test per `src.exceptions` subclass confirming its mapped HTTP status via a dummy route that raises it.
- **Parity evidence**: not applicable — no existing calculation is being wrapped in this slice.
- **Dependencies**: none beyond what's already installed plus the three new packages.
- **Stop conditions**: none expected to trigger in this slice; if adding FastAPI/Pydantic surfaces an unexpected dependency conflict with the existing pinned versions in `requirements.txt`, stop and report rather than force a version change.
- **Explicit exclusions**: no database read of real data, no team/player/injury/rankings endpoints, no authentication, no rate limiting, no live server left running, no production dependency install performed by this batch itself (the requirements.txt edit only *declares* the dependency — installing and running it is deliberately left to the actual approved Slice 0 implementation batch, not this planning document).

### Phase 6B implementation record (2026-09-15)

Slices 0–2 were implemented exactly as scoped above, under separate explicit approval ("PHASE 6B — IMPLEMENT PHASE 6 SLICES 0–2"). Recorded here as fact, not as a design change:

- **Dependencies**: `fastapi==0.141.1`, `pydantic==2.13.5`, `uvicorn==0.46.0` added to `requirements.txt`; `sqlalchemy==2.0.52`/`alembic==1.19.1` (already installed, previously undeclared) formally declared there too. `httpx==0.28.1` (already installed transitively; required by `fastapi.testclient`) declared in `requirements-dev.txt`.
- **Files**: `src/services/contracts.py` (`HealthResult`, `ReadinessResult`), `src/services/health.py` (`check_health`, `check_readiness`), `src/api/schemas.py` (`HealthResponse`, `ReadyResponse`, `ErrorResponse`), `src/api/deps.py` (`get_engine`, `get_session`), `src/api/errors.py` (`register_exception_handlers`), `src/api/main.py` (`create_app`).
- **Deviation from this section's originally proposed file list**: no `src/api/routers/` package was created — the approved Slice 0–2 batch's own package-structure instruction specified a flatter `src/api/{main,deps,errors,schemas}.py` layout for a two-endpoint diagnostics-only batch, so the two routes are registered directly inside `create_app()` rather than in a separate router module. A `routers/` package remains the right structure once Slice 3+ adds real domain routes.
- **Readiness semantics went slightly beyond this section's original one-line description**: `/v1/ready` checks not just "can `get_session()` open and query" but specifically compares the database's current Alembic revision(s) (via `alembic.runtime.migration.MigrationContext`) against this repo's own expected head(s) (via `alembic.script.ScriptDirectory`) — required by the Slice 0–2 approval's own diagnostic-endpoint policy ("compatible with the current migration state"). No SQL literal was added; both reads use Alembic's existing APIs.
- **`get_session`** was implemented per §9 but is not yet called by any route — no Slice 3+ service exists yet to use it.
- **Tests**: 38 new non-live tests (`tests/regression/test_services_health.py`, `tests/regression/test_api_schemas.py`, `tests/api/test_app_construction.py`, `tests/api/test_health.py`, `tests/api/test_ready.py`, `tests/api/test_exception_mapping.py`) plus 3 new PostgreSQL integration tests (`tests/postgres/test_api_readiness_postgres.py`, reusing the existing `pg_migrated_database_url`/`pg_empty_database_url` fixtures — no new container harness).
- **Not started**: Slice 3 (read-only Team service) and everything after it.

### Phase 6 Slice 3A implementation record (2026-09-15)

Under a separate explicit approval ("PHASE 6 SLICE 3 — READ-ONLY TEAM SERVICE AND LOCAL-DEVELOPMENT TEAM API"), a read-only Team service and two HTTP routes (`GET /v1/teams/{team_id}`, `GET /v1/teams/resolve`) were fully implemented and passed every planned unit/API/PostgreSQL test. A follow-up message from the same authorization then changed the access decision to **service-first**, explicitly to avoid an unauthenticated data route becoming a de facto precedent ahead of the still-unresolved authentication decision in §11. This record reflects the corrected, currently-true state:

- **What is retained and exposed to Python callers only**: `src/services/contracts.py` (`TeamRecord`, `TeamLookupResult`), `src/services/team.py` (`get_team`, `resolve_team`), `src/db/repositories.py`'s additive `TeamRepository.get_by_id`. All framework-independent -- zero FastAPI/Starlette/Pydantic imports, verified by a static test.
- **What is NOT in the running application**: no Team route, no Team Pydantic schema (`TeamResponse` was built, then deleted along with the routes). `/v1/teams/...` returns FastAPI's own plain unmatched-route 404.
- **Identity resolution boundary confirmed as designed**: `resolve_team` delegates entirely to the existing, unmodified `src.identity.team_identity.resolve_team_identity` -- no second normalizer, no new alias table, no fuzzy fallback. An ambiguous query (e.g. "Miami") raises the existing `IdentityConflictError` rather than being silently resolved. A query that resolves in the identity registry but has no persisted `Team` row (a real, reachable state, since the registry covers all ~365 D1 programs while the database only has real committed source data) is distinguished internally (`detail="resolved_but_missing_row"`) from a genuinely unresolved query (`detail="unresolved"`) -- both currently collapse to the same `outcome="not_found"` result since there is no HTTP layer to give them different status codes right now.
- **Duplicate-source-directory proof retained**: Miami (FL) (one of the three real Phase 5C5/5C6 duplicate-directory teams) is used as the concrete test case, on both SQLite and real disposable PostgreSQL -- two idempotent `TeamRepository.upsert` calls for the same `team_id` produce exactly one `Team` row and one `TeamLookupResult`.
- **Tests, final state**: `tests/regression/test_services_team.py` (14 unit tests) + `tests/postgres/test_services_team_postgres.py` (12 tests calling the service directly against real PostgreSQL, no FastAPI application involved) + 2 scope-verification tests added to `tests/api/test_app_construction.py`. The originally-built `tests/api/test_teams.py` and `tests/postgres/test_api_team_postgres.py` (HTTP-based) were deleted once the routes were removed -- their scenarios were converted, not lost.
- **Not started**: Slice 3B (the authentication decision plus Team route restoration) and every domain beyond Team.

### Phase 6 Slice 3B implementation record (2026-09-15)

Resolves §11 ("Authentication options") for internal, non-deployed data endpoints: **opaque Bearer API keys, verified against configured SHA-256 digests.** This is a working decision for local development/testing, not a final production policy -- rate limiting, OAuth/JWT, user accounts, roles, and a hosting/deployment boundary remain separate, explicitly deferred decisions (§12 and the auth-mechanism alternatives in §11 are otherwise unchanged).

- **Authentication**: `src/auth/api_keys.py` (`AuthenticatedClient`, `ApiKeyAuthenticator`) -- framework-independent, `secrets.compare_digest` only, configured via one new env var `NAPSEDGE_API_KEY_HASHES_JSON` (a JSON object of `client_id -> sha256_hex_digest`), read only per-authentication-attempt (never at import time, never in `create_app()`). Two new `src.exceptions` classes (`AuthenticationError` -> 401, `AuthenticationConfigurationError` -> 503) plug directly into the existing centralized handler from §10 with no change to any existing entry.
- **Protected endpoints**: `GET /v1/teams/resolve` and `GET /v1/teams/{team_id}` (`src/api/routes/teams.py`) -- exactly the two endpoints §8's catalog proposed for the Team domain, now actually implemented, both behind the new authentication dependency. Both call `src.services.team` directly, unmodified since Slice 3A.
- **Auth-before-session ordering**: the authentication dependency is declared before the session dependency in both route signatures specifically so a failed authentication attempt never triggers `get_session` at all -- confirmed by a dedicated test that spies on `get_session` and asserts it is never called when a request has no valid credential. This directly satisfies §9's DI strategy without any change to `get_session`'s own implementation.
- **Deviation from §5's originally sketched structure**: a `src/api/routes/` package (not `src/api/routers/`) was introduced, per this batch's own explicit instruction -- the first non-diagnostic router in the application. Future domains (player, rankings, etc.) should follow the same `src/api/routes/<domain>.py` pattern.
- **§19 open decision partially resolved**: "Authentication mechanism" is now resolved for internal use (Bearer + SHA-256 digest, not the Discord-JSON entitlement store and not a new DB table) -- still open: whether/how this same mechanism (or a different one) governs a future *public* deployment, and how the Discord-tier entitlement system and this mechanism eventually relate, if at all.
- **Tests, final state**: 23 unit tests (`tests/regression/test_auth_api_keys.py`) + 27 rewritten API tests (`tests/api/test_teams.py` plus 5 new assertions in `tests/api/test_app_construction.py`) + 8 PostgreSQL integration tests (`tests/postgres/test_api_team_postgres.py`), all against the existing PostgreSQL fixture harness.
- **Not started**: Slice 4 and every domain beyond Team; rate limiting; any production/public-deployment decision.

### Phase 6 Slice 4A implementation record (2026-09-15)

Extends §3.1's Player readiness finding with the concrete result: the domain splits cleanly into a **raw-persisted half (now implemented)** and a **computed-OVR half (still blocked)**, exactly as §3.1 predicted.

- **Service**: `src/services/player.py::get_player_raw_statistics(session, player_id, *, season_id=None, stat_table=None)` -- read-only, framework-independent, mirrors `src.services.team`'s structure. Wraps only `player_stat_snapshots` (season-level AllStats box-score/advanced columns); `player_game_log_observations` (per-game rows) and `roster_memberships` (biographical fields) are structurally incompatible shapes and remain explicitly deferred rather than flattened into one misleading contract.
- **No name-based resolution**: confirmed, directly from the codebase's own docstring, that `src.identity.player_identity.build_player_identity` is "a constructor, not a lookup" -- there is no authoritative Player-name registry to query, unlike `slug_utils.TEAM_REGISTRY` for teams. This service therefore accepts only an already-known `player_id`; a future name-based Player lookup would need its own, separately-designed identity resolver -- not something this slice invents.
- **Missing vs. empty, same pattern as Team**: an unknown `player_id` is `outcome="not_found"`; a known player with zero qualifying stat rows is `outcome="found"` with an empty `stats` tuple -- deliberately mirrors the Team service's own found/not_found discipline rather than inventing a third convention.
- **Not started**: Slice 4B (Player HTTP exposure -- endpoint shape, auth reuse, and whatever the incompatible game-log/roster tables mean for a future endpoint design all remain open); every domain beyond Team and Player.

### Phase 6 Slice 4B implementation record (2026-09-15)

Realizes §8's proposed `GET /v1/players/{player_id}/stat-snapshots`-shaped endpoint (implemented here as `GET /v1/players/{player_id}/raw-statistics`, the approved name) with zero new architecture -- every DI/auth/error-handling primitive from §9-§11 is reused exactly as designed, unmodified.

- **Endpoint**: `GET /v1/players/{player_id}/raw-statistics?season_id=&stat_table=` (`src/api/routes/players.py`), Bearer-authenticated identically to the Team routes, auth declared before session in the route signature (same ordering guarantee as §9 intended).
- **Field-exposure review, resolved for this domain (corrected after a follow-up audit)**: only `player_key` is excluded (an internal matching detail redundant with `source_player_name`). `validation_status` was originally excluded too, but a follow-up audit found `PlayerStatSnapshotRepository.list_for_player` enforces no invariant restricting results to `"valid"` rows -- a non-valid row can genuinely reach this endpoint, and hiding the label would let a caller mistake unresolved/conflicting data for validated data. `validation_status` is now exposed unchanged, with a permanent test (`test_exposure_contract_matches_the_approved_allowlist_exactly`) proving the exact field set programmatically rather than by prose alone. `source_artifact_id` is exposed per the design doc's own "opaque source-artifact identifiers... for traceability" allowance, now exercised for the first time.
- **No new error type needed**: unknown-player -> 404 (direct JSON, matching the Team not-found pattern), ambiguous is not applicable to this domain (no name resolution exists), malformed input -> FastAPI's own 422, auth failures -> the existing `AuthenticationError`/`AuthenticationConfigurationError` handlers from Slice 3B, completely unchanged.
- **End-to-end output parity established**: database row -> `PlayerStatRecord` -> `PlayerStatResponse` -> JSON verified field-identical, including null and stored-zero preservation, confirming the whole Slice 4A/4B boundary is a faithful pass-through with zero silent transformation.
- **Not started**: Slice 5 and every domain beyond Team and Player; rate limiting; any production/public-deployment decision.

### Phase 6 Slice 5A implementation record (2026-09-15)

Confirms §3.7's Rankings readiness finding was accurate and builds directly on it: the domain already had a complete, pure, cache-only ingestion+identity layer (`src.ingestion.net_ranking_ingestion`, `src.identity.net_ranking_identity`) built in Phase 3 -- this slice adds only the thinnest possible service layer on top, per the Team/Player precedent, with zero new normalization or crosswalk logic.

- **Cache authority confirmed truthfully**: `Reference Py Files/net_rankings_2026.csv` -- a static file with no season/freshness column of its own. `RankingRecord` deliberately has no `as_of`/`is_current` field as a result; `season` is a caller-asserted label, exactly matching the existing ingestion function's own convention.
- **Real cache defect surfaced, not hidden**: `"louisiana-state"` genuinely appears twice with different ranks (77, 245) in the real cached fixture -- the service reports this as `outcome="conflicting"`, never silently picking one value, and this exact row is used as a real (not synthetic) integration test.
- **New exception, narrowly scoped**: `RankingsCacheIntegrityError`, reserved for an *identical* duplicate cache row (same team, same rank) with no existing dedup rule -- the more common *conflicting* case (different ranks) is a typed outcome instead, since it is fully and truthfully representable without an exception.
- **No Rankings HTTP endpoint** -- matches this document's own repeated "service-first" pattern from Slices 3A and 4A. §8's proposed `GET /v1/teams/{team_id}/net-rank` endpoint remains a Slice 5B decision, not built here.
- **Not started**: Slice 5B (Rankings HTTP exposure) and every domain beyond Team/Player/Rankings; rate limiting; any production/public-deployment decision.

### Phase 6 Slice 5B implementation record (2026-09-15)

Realizes §8's proposed Rankings endpoint shape (implemented as `GET /v1/rankings/net` and `GET /v1/rankings/net/teams/{team_id}`, the approved names) on top of Slice 5A's service, reusing every DI/auth/error-handling primitive from §9-§11 unmodified, plus one new, narrowly-scoped exception-translation rule this domain specifically required.

- **Server-controlled cache context -- the central design constraint of this slice**: unlike Team/Player (backed by a real database with real primary keys), the Rankings cache is a static file with no season/as-of column. Allowing a client to choose `season` would let the API imply a claim ("this is the ranking as of season X") the cache cannot actually support. Resolved by making `season` and the cache path exclusively server-configured (`NAPSEDGE_NET_RANKINGS_SEASON` required, `NAPSEDGE_NET_RANKINGS_CACHE_PATH` optional override), read fresh per-request by a new `get_rankings_context` dependency -- no query parameter, no request body field, no header carries either value. The configured `season` is still echoed back in every 200 response, explicitly documented as server-asserted context, not a freshness claim.
- **New exception-boundary rule, distinguishing two different meanings of the same exception type**: `ValidationError` already meant "client input failed a check" (422) everywhere else in this API (Team/Player request parameters). Rankings introduced a second, unrelated meaning for the same exception class -- "the server's own cache file is malformed" -- which needed a 503, not a 422, per this slice's approval. Since Starlette's handler lookup is global-by-type, not per-route, the fix could not live in `src/api/errors.py`'s existing tables without affecting Team/Player too. Instead, `src.services.rankings._load_cache` catches `ValidationError` at the one specific call site where it can only mean "malformed cache" (`cache_repository.load(...)`) and re-raises it as the already-503-mapped `SourceUnavailableError`, chaining the original exception for server-side log context. This is the first place in the API where the same internal exception type needed two different HTTP meanings depending on which layer raised it -- worth recording as a pattern for any future domain that reuses `ValidationError` for a non-client-input purpose.
- **Field-exposure review found a real documentation/implementation mismatch, not just confirmed the schemas were correct**: the initial `RankingSourceRowResponse` docstring claimed `team_id`/`net_rank` were "populated only when resolved" -- but the route's own (correct) implementation treats team-resolution and rank-validity as independent, matching `NetRankingRecord`'s own orthogonal design. A test written against the docstring's claim failed against the actual (correct) behavior, which is exactly the kind of gap the Slice 4B `validation_status` review was meant to catch going forward -- confirming that lesson generalizes.
- **Conflict preservation, end-to-end**: `RankingLookupResult` was additively extended with `conflicting_records: tuple[RankingRecord, ...]`, populated by the same matching logic `get_team_ranking` already performed internally, so the 409 response for a conflicting team (e.g. the real cache's own `"louisiana-state"`, ranks 77/245) can present every contributing record without the route re-deriving them or selecting one.
- **Tests, final state**: 43 new tests (2 in `tests/regression/test_rankings_cache_repository.py`, bringing it to 33; 41 in the new `tests/api/test_rankings.py`) plus updated route-count/security/scope assertions in `tests/api/test_app_construction.py`. Complete `tests/api`: 136 passed (95 + 41). Complete `tests/postgres` (`-m postgres`): 189 passed, unchanged (no database dependency). Complete non-live suite: 4,495 passed, 1 skipped, 189 deselected -- reconciles exactly to 4,452 (prior baseline) + 2 (new Rankings service tests) + 41 (new Rankings API tests), with the one expected skip and the 189 PostgreSQL deselections both preserved.
- **Manifest merge-convention correction**: the two new route handlers (`list_net_rankings`, `get_net_ranking_for_team`) were briefly, incorrectly folded into the existing `list_rankings`/`get_team_ranking` manifest rows under the Slice 3B/4B multi-occurrence merge convention -- but that convention applies only to an actual bare-name collision (true for `get_team`/`get_player_raw_statistics` in those slices, since the route handler and service function shared the identical name), which does not hold here since this slice's route handlers were deliberately given distinct names. Corrected: both now have their own manifest rows; final count is 1,104 rows in each file (1,094 + 10 genuinely new, uniquely-named public symbols), not 1,102.
- **A genuine test-infrastructure hazard found and documented, not an implementation defect**: an initial dynamic no-network test monkeypatched `socket.socket` globally while driving the app through FastAPI's `TestClient`, which deadlocked indefinitely on Windows -- `TestClient`/httpx/anyio bridge the synchronous test call onto a background event-loop thread using `socket.socketpair()` for inter-thread wakeup, which Windows emulates via real `socket.socket()` calls; patching that function globally breaks the bridge itself, not any actual outbound network path. Removed in favor of relying on the already-proven service-level dynamic no-network test (which calls the service function directly, with no ASGI transport involved) plus two AST-level static import guards (route module, service module) -- worth recording for any future domain's API-layer test suite that wants to repeat a service-level network-disable pattern.
- **Not started**: Slice 6A (public injury-report function extraction and read-only injury service) and every domain beyond Team/Player/Rankings; rate limiting; any production/public-deployment decision.

### Phase 6 Slice 6A implementation record (2026-09-21)

Resolves the open decision §6/§3.2 recorded and explicitly deferred: `build_team_summary` mixes team, player-trend, and injury concerns in one untyped dict, and full decomposition was called out of scope for Phase 6A "since it would touch an active, unverified-parity function." This slice takes the narrower path §6 itself named as acceptable -- extract only the injury-specific piece (`_injury_report`) into a public, typed-adjacent function and build a service around it, while leaving `build_team_summary` itself, and its team/player-trend logic, completely untouched. Unlike Rankings (whose ingestion/identity layer was already fully typed and public), Injury's only existing local-data-reading authority was a small, *private*, ad hoc, dict-returning function -- so this slice's actual work was extraction-first: make the existing logic public, exactly as approved, before any service or API layer could be built on it.

- **Two, not one, pre-existing Injury-adjacent systems found -- only one was in scope**: a search for `_injury_report` also surfaced a separate, much more mature, already-migrated Covers.com injury ingestion/identity/transformation pipeline (`src.ingestion.covers_injury_ingestion`, `src.identity.injury_player_identity`, `src.transformations.injury_availability_transformation`, from Phases 2B/3). That pipeline is the *producer* of richer, canonically-resolved injury data elsewhere in the system; `_injury_report` is a separate, simpler *consumer* that reads a local JSON cache (`TeamDump/CBB/<team>/`, `TeamDump/injury_cache/`) with no identity resolution of its own. This slice's approval named `_injury_report` specifically -- the Covers pipeline was left completely untouched, per the explicit instruction not to invent a parallel implementation or expand scope beyond the named authority.
- **A real, pre-existing data-quality gap found and preserved, not silently fixed**: `_injury_report`'s own `impactful_count` computation calls `item.get("status", ...)` unconditionally on every element of a successfully-loaded list, without checking that each element is itself a dict -- so a non-dict list element raises `AttributeError` and crashes the whole function, both before and after this slice's extraction (traced and confirmed identical). Per this slice's own stop condition ("extraction changes a calculation... output parity fails... do not silently bless an intentional behavior change as extraction"), this was left exactly as-is and covered by a dedicated parity test proving the crash is unchanged, rather than "fixed" as an undocumented scope expansion.
- **Deliberately narrow outcome typing**: `TeamInjuryReport.outcome` has exactly two values (`"loaded"`/`"not_loaded"`), matching only what `_injury_report` itself can truthfully distinguish -- no "malformed" or "unknown team" tag was invented, since the authority performs no team-identity check and does not itself separate a missing file from a malformed one. Adding that granularity would have required new interpretive logic beyond "expose the existing authority," which this slice's scope does not authorize.
- **A Phase-0-era heuristic manifest classification corrected by direct verification**: `docs/notebook_src_coverage.csv`'s original row for `_injury_report` (from the earliest bulk keyword-scan batch) classified it as `"network, file-io"` with an `"HTTP request, reads/writes disk"` side effect and `"Medium-High"` risk -- a false positive from that scan's keyword heuristic, not a hand-verified finding (the migration's own standing caveat already flagged these columns as "a triage aid, not ground truth"). Direct code reading plus this slice's own AST-level no-network test confirmed the function performs no network access and never writes -- corrected to `"file-io only"` / read-only / `"Low"` risk.
- **No new exception added**: every *typed* Injury failure mode already folds into the existing `"not_loaded"` outcome, exactly mirroring the "not-found is an expected outcome, not an exception" philosophy already established for Team/Player/Rankings -- no `NapsEdgeError` subclass was needed. One *untyped* failure mode remains, deliberately not converted into a third outcome: a non-dict element in an otherwise-valid injury list raises an uncaught `AttributeError` (see the preserved-crash finding above). A follow-up review of this slice strengthened the test suite to prove that exception originates specifically inside the extracted authority's own module (not the service), and that the compatibility wrapper and existing caller raise the identical crash -- and made explicit, in both `src/services/injury.py`'s module docstring and `TeamInjuryReport`'s own docstring, that Slice 6B must add its own sanitized translation for this case before any Injury route can expose it.
- **No Injury HTTP endpoint** -- matches this document's own repeated "service-first" pattern from Slices 3A, 4A, and 5A. A future `GET /v1/teams/{team_id}/injuries`-shaped endpoint remains a Slice 6B decision, not built here.
- **Not started**: Slice 6B (Injury HTTP exposure) and every domain beyond Team/Player/Rankings/Injury; rate limiting; any production/public-deployment decision.

### Phase 6 Slice 6B implementation record (2026-09-21)

Realizes §8's implicit Injury endpoint expectation as `GET /v1/teams/{team_id}/injuries` -- a Team subresource, the shape explicitly preferred by this batch's own approval once the underlying `team_id -> sr_slug` mapping was confirmed tested and correct -- reusing every DI/auth/error-handling primitive from §9-§11 unmodified, plus one new, narrowly-scoped exception this domain specifically required to resolve Slice 6A's own flagged prerequisite.

- **The pre-edit identity decision was the load-bearing step, not a formality**: before writing any route code, this batch confirmed empirically (not by assumption) that `src.services.team.get_team`'s `TeamRecord.canonical_slug` (== `Team.sr_slug`) is exactly the slug `build_injury_report`'s candidate paths already expect -- verified by resolving "Duke" through the real identity registry and confirming `sr_slug="duke"` matches the real cached file at `TeamDump/CBB/duke/`. Had this mapping not existed, or not been tested, the correct outcome would have been to stop and report the identity blocker, per the approval's own stop condition -- it did exist, so the smallest coordinating operation (`get_injury_report_for_team`) was added to `src/services/injury.py` rather than inventing a slug-based endpoint or a new identity system.
- **A genuine testing-discovered defect, caught and fixed before it ever reached documentation as "done"**: the first implementation of the malformed-element sanitization used a bare `except AttributeError`, which a dedicated adversarial test then proved would mislabel *any* unrelated `AttributeError` (not just the known Slice 6A limitation) as an Injury cache integrity problem -- exactly the anti-pattern the approval's own instructions warned against ("confirm unrelated AttributeError exceptions... are not mislabeled"). Fixed by walking the caught exception's own traceback to confirm its deepest frame genuinely originates inside `src/features/team_summary.py` before translating it; anything else now propagates unchanged to the generic 500 handler. This is the second time in this project a test caught an overly-broad or incorrect first-draft implementation before it shipped (the first was the Slice 6A `RankingSourceRowResponse` field-independence correction) -- both times the fix was found by writing the adversarial test the approval asked for, not by inspection alone.
- **Loaded-empty vs. unavailable, made structurally impossible to conflate**: a successfully-loaded, zero-injury report is `200` with `injuries: []`; an unavailable report is a `503` with a completely different body shape (`{"error": "service_unavailable", ...}`) -- the two cannot collide at the JSON level even if a caller only checks the body loosely, and a dedicated test asserts the 503 body is never equal to the 200-empty body.
- **First real-PostgreSQL Injury test needed, and a familiar gotcha repeated once more**: unlike Rankings (zero database dependency), Injury's new canonical-ID coordination genuinely depends on a real `Team` row, so `tests/postgres/test_api_injury_postgres.py` was required. One of its own tests initially failed the exact `pg_session_factory`-engine-disposed-only-at-teardown gotcha already documented from Slice 3B -- fixed the same way, by seeding through a separate, explicitly-disposed engine when a test also asserts zero residual connections. Worth restating: this is now the second time this exact pattern has recurred, reinforcing it as a standing rule for any future PostgreSQL test asserting connection-count invariants.
- **Not started**: Slice 7A (read-only Matchup pure-feature service) and every domain beyond Team/Player/Rankings/Injury; rate limiting; any production/public-deployment decision.

### Phase 6 Slice 7A implementation record (2026-09-21)

Realizes §6/§3.2's own long-standing observation about `build_matchup_features` -- "mixes team, player-trend, and injury concerns in one untyped dict... decomposing is out of scope for Phase 6A [now: earlier slices], recorded as an open decision" -- but takes the narrowest possible slice of it: not a full decomposition of `build_matchup_features`, only its two purely descriptive, already-pure internal helpers.

- **The authority inventory step changed the shape of the deliverable**: this slice's own approval anticipated possibly needing to extract a single pure function; the actual codebase inventory found *two* tightly-coupled private helpers (`_delta`, `_build_key_edges`) already doing exactly the approved "descriptive feature comparison" job, plus a genuine gap -- no existing function combined "two teams' raw feature values" into "deltas + labeled edges" without first calling the file-and-Injury-dependent `build_team_summary`. Rather than force-fitting the exposure around whichever single function looked closest, the correct, honestly-scoped answer was: extract both existing helpers unchanged, and add exactly one new, minimal orchestration function (`build_matchup_feature_comparison`) that reuses them without introducing any new formula.
- **A no-git-history complication, worked around without weakening the parity proof**: `src/features/matchup_features.py` turned out to be untracked in git (part of the project's long-standing pile of never-committed files), so `git show`/`git diff` could not recover the pre-edit version for an automated diff. The original function was instead reconstructed verbatim from this session's own prior reading of the file, run side-by-side against the refactored version against real cached Duke/Connecticut data, and only after that manual proof succeeded was the same parity re-established as a permanent, independently-re-derived test (not a copy of the reconstructed script) -- satisfying the requirement to prove parity via literal expectations or independent fixtures, not merely a wrapper comparison.
- **A second Phase-0 heuristic false positive found and corrected, same pattern as Slice 6A's `_injury_report`**: `_build_key_edges` and `build_matchup_features` were both originally classified `"network"`/`"HTTP request"`/`"Medium-High"` risk in the earliest bulk keyword-scan batch. This slice's own direct code reading plus its AST-level no-network test confirmed neither performs any network access at all -- corrected in the manifest to reflect the now-verified truth, exactly as the standing "these columns are a triage aid, not ground truth" caveat anticipated would eventually be needed again.
- **The explicit-input service boundary was the right call, not a shortcut**: rather than have the new service accept team identifiers and internally resolve/acquire feature data (which would have required either a database lookup inconsistent with "the pure authority requires caller-supplied precomputed features" or a `build_team_summary` call dragging in file I/O and Injury acquisition, both explicitly out of scope), the service was designed to accept the two teams' feature values directly -- consistent with the approval's own worked example and with the "don't recreate identity/acquisition a caller already has" boundary already established for every other Phase 6 domain.
- **Not started**: Slice 7B (Matchup HTTP exposure) and every domain beyond Team/Player/Rankings/Injury/Matchup; live odds; prediction generation; Strategy; Model Performance; rate limiting; any production/public-deployment decision.

### Phase 6 Slice 7B implementation record (2026-09-21)

Realizes §8's own Matchup endpoint sketch, but with a documented correction rather than a literal reuse: the originally proposed `GET /v1/matchups/{team_a}/{team_b}/features` assumed a team-slug-based lookup wrapping the full `build_matchup_features` (file I/O plus, since Slice 6A, Injury acquisition) -- incompatible with what Slice 7A actually built (`compare_team_matchup_features`, an explicit-two-feature-dict operation with no acquisition of its own). Per this slice's own approval, the incompatible sketch was set aside for its named fallback, `POST /v1/matchups/feature-comparison`, and that substitution is recorded explicitly here rather than silently reusing the old path name for a semantically different operation.

- **The first request-body endpoint in this application surfaced a genuine, previously-unreachable framework defect**: every prior Phase 6 route validated only path/query parameters; this is the first with a Pydantic-modeled JSON body. Sending a raw `NaN`/`Infinity`/`-Infinity` float value crashed the server outright, because FastAPI's default `RequestValidationError` handler echoes the raw offending value back into the error response, and Starlette's `JSONResponse` (`allow_nan=False`) cannot serialize that echoed value -- an unhandled `ValueError` while rendering what should have been a clean 422. Fixed with one narrowly-scoped custom handler that sanitizes only non-finite floats inside error details, verified not to affect any other validation-error shape or any existing route's own 422 behavior. This is exactly the kind of defect this project's own testing discipline is designed to surface before it reaches documentation as "done" -- caught by writing the adversarial test the approval's own testing checklist asked for (items 22-25), not found by inspection.
- **Field-exposure review found nothing to exclude, for the first time in this project**: every prior domain's response schema excluded at least one internal field (a cache path, a database identifier, an internal diagnostic). Matchup's `FeatureComparisonEntry`/`MatchupFeatureComparisonResult` contracts, by contrast, were already fully external-safe by construction (Slice 7A's own design never included an internal path, identifier, or diagnostic field in the first place) -- so both response schemas mirror their dataclasses with zero exclusions, confirmed programmatically rather than assumed from that observation alone.
- **No database session dependency exists on this route at all** -- the first Phase 6 data-returning endpoint with that property. Verified both statically (no `Session`/`get_session` token anywhere in the route module) and dynamically (pointing the endpoint's own `create_app` at a genuinely unreachable database path and confirming the request still succeeds).
- **Not started**: Odds, Strategy, Model Performance, live-odds-in-request-path, prediction/win-probability generation, and any production/public-deployment decision. The next step is explicitly a planning/decision batch (Phase 6C), not another domain implementation.

### Phase 6C -- remaining-domain readiness and phase-closeout decision (2026-09-21)

**Planning/audit only** -- see the full deliverable, [`docs/phase6_closeout_readiness.md`](phase6_closeout_readiness.md), for the complete implemented-scope inventory, the original-requirements audit (every requirement classified individually, no partial domain folded into "complete"), the final eight-domain readiness matrix, the cross-cutting controls audit (§11's authentication decision and §12's rate-limiting decision are both still exactly as open as this document originally left them -- neither was silently resolved by any implementation slice), the three-tier deployment-readiness boundary, the Discord/Streamlit reuse-readiness check, and three closeout options with ten unresolved approval decisions. This section of the present document is not modified further -- every subsequent Phase 6 decision or implementation record belongs in `docs/phase6_closeout_readiness.md` and `docs/migration_state.md`'s own Phase 6C entry, not here.

### Phase 6D implementation record (2026-09-21)

Resolves §12 ("Rate-limiting options") for the client-facing half only -- Phase 6C's Option B, selected explicitly by this batch's own approval message. **Fixed window, keyed by authenticated `client_id` (never IP), 60 requests per 60 seconds by default** (`NAPSEDGE_RATE_LIMIT_REQUESTS`/`NAPSEDGE_RATE_LIMIT_WINDOW_SECONDS`, both overridable within a defensible upper bound), applied to exactly the seven protected data routes -- `/v1/health`/`/v1/ready` remain exempt. In-memory, single-process, application-scoped (`src/auth/rate_limit.py`, no FastAPI import, mirroring `src.auth.api_keys`'s own framework-independence) -- an internal-MVP control only, explicitly not claimed to survive a process restart or to be consistent across worker processes or hosts. §12's other half -- upstream-facing rate limiting to protect The Odds API's own quota inside `odds_service` -- remains exactly as deferred as before, since no Odds endpoint exists yet.

- **Dependency ordering enforced by nesting, not by a manually-sequenced check**: `src.api.deps.get_rate_limited_client` declares `client: AuthenticatedClient = Depends(get_authenticated_client)` as its own parameter, so FastAPI's dependency resolution -- not any code in this new function's own body -- guarantees a failed authentication check always raises before the rate-limit check ever runs. Every protected route now depends on this combined dependency in place of `get_authenticated_client` directly, still declared before every other route dependency (`get_session`, `get_rankings_context`), extending the existing auth-before-session convention rather than introducing a new one.
- **State and policy deliberately split across two different lifetimes**: the counting state (`RateLimiter`, holding the actual per-client windows) is constructed once per `create_app()` call and stored on `app.state.rate_limiter` -- an application-scoped object, never a module-level global, so two application instances (or two tests) never share counters. The policy (`RateLimitPolicy`, via `load_rate_limit_policy_from_env`) is instead re-read from the environment on every single request, mirroring `get_rankings_context`'s own established per-request configuration-read pattern -- letting a configuration change take effect on the very next request without restarting the process, while the counts themselves persist across requests for the application's whole lifetime, which a rate limiter's entire purpose requires.
- **A misconfigured limiter is a server problem, never a client's rate-limit violation**: an out-of-range or malformed `NAPSEDGE_RATE_LIMIT_REQUESTS`/`NAPSEDGE_RATE_LIMIT_WINDOW_SECONDS` raises `RateLimitConfigurationError`, mapped to a sanitized 503 -- the same precedent as `AuthenticationConfigurationError`/`RankingsConfigurationError` -- never a 429. An *absent* variable, by contrast, is never an error: it silently falls back to the documented 60/60 default, so rate limiting can never be accidentally disabled by omission.
- **The 429 error path required its own dedicated handler, unlike every prior sanitized error**: every previous `NapsEdgeError` subclass mapped to a fixed status/body via one of two small generic factory functions (`_expose_handler`/`_sanitized_handler`). `RateLimitExceededError` could not reuse either, since it is the first error in this project that must set a *per-request* HTTP header (`Retry-After`, computed from `exc.retry_after_seconds`) -- so it gets its own handler function, following the same one-off precedent already set by `_authentication_error_handler`, rather than forcing a header parameter into the shared factories for a need only one exception type has.
- **Proved, not assumed, that a 429 short-circuits before any downstream work**: dedicated tests point a route at a genuinely unreachable database path, an unconfigured Rankings environment, and a monkeypatched Matchup-service spy, and confirm the allowed (first) request reaches that downstream failure/call while the blocked (second) request returns 429 without ever reaching it -- the same "prove it dynamically, not just by code reading" standard used for every prior auth-ordering claim in this project.
- **Test isolation required no special fixture at all**: every `*_client` fixture in `tests/api/conftest.py` already builds a fresh `create_app()` per test function (never module- or session-scoped), and a review of every existing test's request-loop found none making more than 5 requests to one app instance -- an order of magnitude under the 60-request default -- so the pre-existing suite needed zero changes to avoid spuriously tripping the new limiter. Every dedicated rate-limiting test instead sets its own small, explicit `NAPSEDGE_RATE_LIMIT_REQUESTS`/`NAPSEDGE_RATE_LIMIT_WINDOW_SECONDS` via `monkeypatch`, per-test -- the production default (60/60) itself was never weakened anywhere to make a test pass.
- **Not started**: upstream-facing (Odds-API-quota) rate limiting; Odds, Strategy, Model Performance; OAuth/JWT/user accounts/roles; CORS; any production/public-deployment configuration or claim. The next step is Phase 6 Final Acceptance (internal MVP, with formal deferrals) -- not started by this batch, per its own instruction not to begin automatically.

### Phase 6 Final Acceptance (2026-09-21)

**Documentation, verification, and handoff only** -- no code, test, or service changed. Full deliverable: [`docs/phase6_final_acceptance.md`](phase6_final_acceptance.md). Recorded final decision: **"PHASE 6 -- COMPLETE AND PASSED FOR THE APPROVED INTERNAL, SINGLE-PROCESS MVP SCOPE, WITH FORMAL DOMAIN AND DEPLOYMENT DEFERRALS"** -- explicitly not an unqualified completion claim; every deferred domain (Odds, Strategy, Model Performance, computed Player cards, full Matchup orchestration) and every deployment gap (§12 above) is named with its own reopen prerequisite, not silently dropped. This closes the batch sequence this document has tracked since Phase 6A; any further Phase 6 domain or deployment work requires its own separate, explicit approval per the reopen conditions in `docs/phase6_final_acceptance.md` §18. Recommended next planning step: **PHASE 7 PLANNING/READINESS ASSESSMENT** (no dedicated Phase 7 charter exists in this repository to cite a different authoritative name from) -- not started.

---

*This document was produced entirely from direct repository inspection (AST/grep-level reads of `src/`, `docs/database_schema.md`, `docs/phase5_final_acceptance.md`, `requirements.txt`) plus one delegated read-only exploration pass covering `src/agents/*`, the odds/rankings/injury/model-performance domains, the Discord bot's call patterns, and existing test coverage. No notebook cell was re-read in full; no Phase 0 discovery or Phase 5 validation was repeated.*
