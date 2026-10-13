"""
FastAPI application factory.

Nine routes exist: `/v1/health`, `/v1/ready` (unauthenticated, exempt from
rate limiting), the Bearer-authenticated, rate-limited Team routes
`/v1/teams/resolve`/`/v1/teams/{team_id}` (`src.api.routes.teams`, Phase 6
Slice 3B), the Bearer-authenticated, rate-limited Player route
`/v1/players/{player_id}/raw-statistics` (`src.api.routes.players`, Phase
6 Slice 4B), the Bearer-authenticated, rate-limited Rankings routes
`/v1/rankings/net`/`/v1/rankings/net/teams/{team_id}`
(`src.api.routes.rankings`, Phase 6 Slice 5B, local-cache only), the
Bearer-authenticated, rate-limited Injury route
`/v1/teams/{team_id}/injuries` (`src.api.routes.injury`, Phase 6 Slice
6B, local-cache only), and the Bearer-authenticated, rate-limited Matchup
route `POST /v1/matchups/feature-comparison` (`src.api.routes.matchup`,
Phase 6 Slice 7B, explicit-input only -- no database, no filesystem, no
odds/agent invocation). No other domain (odds/strategy/model-performance)
route exists yet.

**Internal, non-deployed development/testing only.** The Team, Player,
Rankings, Injury, and Matchup routes each require a valid Bearer
credential (`src.auth.api_keys`) *and* an in-memory, single-process,
per-`client_id` fixed-window rate limit (`src.auth.rate_limit`, Phase
6D) -- an internal-MVP control, not a production-scale one (see
`src.api.deps.get_rate_limited_client`'s own docstring for the accepted
limitations). This application still has no production hosting/
deployment story -- that remains a separate, not-yet-approved decision.
Do not expose this application on a public network.

`create_app()` is deterministic and side-effect-free at *import* time:
building an `Engine` via `src.db.session.build_engine` does not open a
real connection (SQLAlchemy's `create_engine` is lazy), and no route
executes until an actual HTTP request arrives. Nothing here starts a
server -- that is a separate, later `uvicorn.run(...)` call this module
does not make. Authentication configuration (`NAPSEDGE_API_KEY_HASHES_JSON`)
is read only inside `src.api.deps.get_authenticated_client`, per request
-- never here, so `create_app()` itself never depends on whether
authentication is configured, and `/v1/health`/`/v1/ready` are entirely
unaffected by it.
"""
from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import Engine

from src.api.deps import get_engine
from src.api.errors import register_exception_handlers
from src.api.request_context import install_request_context
from src.api.routes.injury import router as injury_router
from src.api.routes.matchup import router as matchup_router
from src.api.routes.players import router as players_router
from src.api.routes.rankings import router as rankings_router
from src.api.routes.teams import router as teams_router
from src.api.schemas import ErrorResponse, HealthResponse, ReadyResponse
from src.auth.rate_limit_backend import build_rate_limiter
from src.db.session import build_engine, build_session_factory
from src.services.health import check_health, check_readiness


def create_app(*, database_url: str | None = None) -> FastAPI:
    """
    Build one application instance. `database_url` overrides
    `src.db.config.get_database_url()`'s own resolution (env var, else the
    local dev SQLite fallback) -- tests pass an explicit disposable URL
    here rather than relying on ambient configuration.
    """
    app = FastAPI(
        title="NapsEdge API",
        version="0.1.0",
        openapi_url="/v1/openapi.json",
        docs_url="/v1/docs",
        redoc_url=None,
    )

    engine = build_engine(database_url)
    app.state.engine = engine
    app.state.session_factory = build_session_factory(engine)
    app.state.rate_limiter = build_rate_limiter()  # Phase 9D: memory (default) or the shared Redis limiter

    register_exception_handlers(app)
    install_request_context(app)  # Phase 9E-1: X-Request-ID and one access line per request

    @app.get("/v1/health", response_model=HealthResponse, tags=["diagnostics"])
    def health() -> HealthResponse:
        check_health()
        return HealthResponse(status="ok")

    @app.get(
        "/v1/ready",
        response_model=ReadyResponse,
        responses={503: {"model": ErrorResponse, "description": "Not ready"}},
        tags=["diagnostics"],
    )
    def ready(engine: Engine = Depends(get_engine)) -> ReadyResponse | JSONResponse:
        result = check_readiness(engine)
        if not result.ready:
            return JSONResponse(status_code=503, content={"error": "not_ready", "message": "service is not ready"})
        return ReadyResponse(status="ready")

    app.include_router(teams_router)
    app.include_router(players_router)
    app.include_router(rankings_router)
    app.include_router(injury_router)
    app.include_router(matchup_router)

    return app
