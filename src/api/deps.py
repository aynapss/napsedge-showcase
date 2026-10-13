"""
Request-scoped dependencies: database access (Phase 6B), Bearer
authentication (Phase 6, Slice 3B), Rankings cache context (Phase 6,
Slice 5B), and rate limiting (Phase 6D).

Database: reuses `src.db.session`'s existing engine/session-factory
construction -- this module adds no new engine configuration and no new
commit/rollback convention. The engine and session factory are built once
in `src.api.main.create_app()` and stored on `app.state`, never at import
time and never as a module-level global here, so `app.dependency_overrides`
can replace either dependency per test without any shared mutable state
leaking between tests or requests.

Authentication: `get_authenticated_client` is the only FastAPI-aware piece
of the authentication story -- it extracts the Bearer credential via
`fastapi.security.HTTPBearer` and delegates the actual verification to
`src.auth.api_keys.ApiKeyAuthenticator`, which knows nothing about HTTP.
`HTTPBearer(auto_error=False)` is used deliberately so a missing header, a
non-Bearer scheme, and an empty credential all collapse to `None` here,
letting this module raise one single, centrally-handled
`AuthenticationError` for every "not authenticated" case rather than
FastAPI's own default 403.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from src.auth.api_keys import ApiKeyAuthenticator, AuthenticatedClient
from src.auth.rate_limit import load_rate_limit_policy_from_env
from src.exceptions import AuthenticationError
from src.rankings.cache_repository import NetRankingCacheRepository
from src.rankings.config import (
    RANKINGS_CACHE_PATH_ENV_VAR,
    RANKINGS_SEASON_ENV_VAR,
    load_rankings_runtime_config_from_env,
)

_bearer_scheme = HTTPBearer(auto_error=False)


def get_engine(request: Request) -> Engine:
    """Read-only infrastructure access (diagnostics). Never used to open an application transaction."""
    return request.app.state.engine


def get_session(request: Request) -> Iterator[Session]:
    """
    One request, one transaction -- mirrors `src.db.session.unit_of_work`:
    commits once if the request completes without raising, rolls back the
    whole request on any exception, always closes.
    """
    session = request.app.state.session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_authenticated_client(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> AuthenticatedClient:
    """
    Reads a fresh `ApiKeyAuthenticator` (and therefore a fresh read of
    `NAPSEDGE_API_KEY_HASHES_JSON`) per call rather than caching one on
    `app.state` -- keeps this dependency fully independent of
    `create_app()`'s own construction, so `/v1/health`/`/v1/ready` are
    never affected by whether authentication is configured at all, and a
    test can flip the environment between requests without rebuilding the
    app. A missing/malformed configuration surfaces as
    `AuthenticationConfigurationError` (503) only when a route actually
    needs authentication -- never health/ready, which never call this.
    """
    if credentials is None:
        raise AuthenticationError("missing or invalid credential")
    return ApiKeyAuthenticator().verify(credentials.credentials)


@dataclass(frozen=True)
class RankingsContext:
    """Server-controlled Rankings request context -- never accepts a caller-supplied path or season."""

    repository: NetRankingCacheRepository
    season: int


def get_rankings_context() -> RankingsContext:
    """
    Reads `NAPSEDGE_NET_RANKINGS_SEASON` (required, must be a positive
    integer) and the optional `NAPSEDGE_NET_RANKINGS_CACHE_PATH` override
    (defaults to `src.rankings.cache_repository.DEFAULT_CACHE_PATH`) fresh
    on every call -- never at import time, never cached on `app.state` --
    mirroring `get_authenticated_client`'s own pattern so this dependency
    stays fully independent of `create_app()`'s construction. Neither
    value is ever accepted from the request itself: the CSV cache carries
    no authoritative season/freshness metadata, so allowing a client to
    choose a season would let the API imply a claim the cache cannot
    support. Fails closed with `RankingsConfigurationError` (mapped to
    503) on a missing/malformed season -- never a fabricated default.

    Phase 7, Slice 2A: the actual environment-variable parsing was
    extracted to `src.rankings.config.load_rankings_runtime_config_from_env`
    (a framework-independent module) so the Discord bridge can reuse the
    identical resolution logic without importing FastAPI. This function's
    own signature, return type, and exception behavior are unchanged.
    """
    config = load_rankings_runtime_config_from_env()
    return RankingsContext(repository=config.repository, season=config.season)


def get_rate_limited_client(
    request: Request,
    client: AuthenticatedClient = Depends(get_authenticated_client),
) -> AuthenticatedClient:
    """
    Phase 6D: the combined authentication + rate-limiting dependency
    every protected data route depends on (in place of
    `get_authenticated_client` directly). Declaring `client` via
    `Depends(get_authenticated_client)` as this function's own parameter
    means FastAPI resolves authentication *first* -- a missing/invalid
    credential raises `AuthenticationError` (401) before this function's
    own body ever runs, so an unauthenticated request never reaches the
    limiter and never consumes a token.

    The `RateLimiter` instance itself lives on `request.app.state`,
    constructed once per `create_app()` call (see `src.api.main`) --
    never a module-level global, so two application instances (or two
    tests) never share counters. The policy is read fresh from the
    environment on every call (`load_rate_limit_policy_from_env`,
    mirroring `get_rankings_context`'s own per-request configuration-read
    pattern) -- a missing variable falls back to the documented default,
    a malformed one fails closed with `RateLimitConfigurationError`
    (mapped to a sanitized 503), and a change to the configured policy
    takes effect on the very next request without restarting the process.

    Raises `RateLimitExceededError` (mapped to 429 with a `Retry-After`
    header) if `client.client_id` has already exhausted its allowance for
    the current window -- this happens *before* the route's own other
    dependencies (a database session, the Rankings/Injury file lookup,
    the Matchup service call) ever run, confirmed by dedicated tests.

    **Accepted internal-MVP limitations** (not a production-readiness
    claim): invalid credentials are never IP-rate-limited (only a
    *successfully authenticated* client consumes a bucket, since the
    bucket key is `client_id`, which only exists after authentication
    succeeds); this in-memory state is never shared across worker
    processes or hosts; restarting the process clears every bucket; no
    `X-Forwarded-For`/proxy header is trusted or consulted anywhere in
    this module; a distributed or public deployment would need a shared
    backend (e.g. Redis) or a gateway-level policy instead of this
    limiter, which this Phase 6D slice does not build.
    """
    policy = load_rate_limit_policy_from_env()
    request.app.state.rate_limiter.check_and_consume(client.client_id, policy)
    return client
