"""
Centralized translation of `src.exceptions.NapsEdgeError` subclasses into
stable, sanitized HTTP responses (Phase 6B; extended in Phase 6D for
rate-limiting errors).

`src.exceptions` remains the sole authority for what an error *means* --
this module only decides what HTTP status and message a client sees for
each one. It never raises, catches-and-recovers, or reinterprets an
exception's own semantics, and no route in this repo is expected to
duplicate this try/except logic itself: registering these handlers once
on the app is enough for every route.

Starlette resolves a raised exception's handler by walking its MRO and
using the first registered match, so a specific subclass's own handler
(e.g. `ValidationError`) always wins over the `NapsEdgeError` catch-all
below it, which in turn wins over the final bare-`Exception` catch-all --
no explicit ordering or isinstance-chain is needed here.
"""
from __future__ import annotations

import math

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import TimeoutError as SqlAlchemyPoolTimeout

from src.exceptions import (
    AdvisoryLockUnavailableError,
    AuthenticationConfigurationError,
    AuthenticationError,
    ConfigurationError,
    DatabasePoolConfigurationError,
    DatabaseTargetVerificationError,
    IdentityConflictError,
    IdentityResolutionError,
    IngestionError,
    InjuryDataIntegrityError,
    NapsEdgeError,
    RankingsCacheIntegrityError,
    RankingsConfigurationError,
    RateLimitConfigurationError,
    RateLimitExceededError,
    SourceUnavailableError,
    ValidationError,
)
from src.logging_config import get_logger

logger = get_logger(__name__)

#: Exposed to the client as written -- these are already short, safe,
#: hand-authored strings in `src/exceptions.py`'s own raise sites, never
#: raw internals (SQL, stack traces, connection strings).
_EXPOSE_DETAIL: dict[type[NapsEdgeError], tuple[int, str]] = {
    ValidationError: (422, "validation_error"),
    IdentityConflictError: (409, "conflict"),
}

#: Never exposed to the client -- logged in full server-side (with
#: traceback) and returned as a fixed generic message, since these may
#: wrap a lower-level failure (a bad connection string, a filesystem
#: path, a raw driver error) that must not reach the response body.
#: `AuthenticationConfigurationError`/`RankingsConfigurationError` are
#: `ConfigurationError` subclasses each given their own, more specific 503
#: entry here -- Starlette's MRO-based handler lookup picks the subclass
#: entry over the generic `ConfigurationError` (500) entry, leaving every
#: other `ConfigurationError` unaffected. `RankingsCacheIntegrityError`
#: (an unresolvable identical-duplicate cache row -- see its own
#: docstring) and `InjuryDataIntegrityError` (a malformed local Injury
#: cache element -- see its own docstring) are each treated as a
#: temporary data-unavailability condition, not a client error, hence
#: 503 rather than 500. `RateLimitConfigurationError` (Phase 6D, malformed
#: `NAPSEDGE_RATE_LIMIT_REQUESTS`/`NAPSEDGE_RATE_LIMIT_WINDOW_SECONDS`)
#: follows the exact same precedent -- a misconfigured limiter is a
#: server-side problem, never a 429.
_SANITIZED: dict[type[NapsEdgeError], tuple[int, str]] = {
    SourceUnavailableError: (503, "service_unavailable"),
    AuthenticationConfigurationError: (503, "service_unavailable"),
    RankingsConfigurationError: (503, "service_unavailable"),
    RankingsCacheIntegrityError: (503, "service_unavailable"),
    InjuryDataIntegrityError: (503, "service_unavailable"),
    RateLimitConfigurationError: (503, "service_unavailable"),
    DatabasePoolConfigurationError: (503, "service_unavailable"),
    IdentityResolutionError: (500, "internal_error"),
    ConfigurationError: (500, "internal_error"),
    DatabaseTargetVerificationError: (500, "internal_error"),
    AdvisoryLockUnavailableError: (500, "internal_error"),
    IngestionError: (500, "internal_error"),
    NapsEdgeError: (500, "internal_error"),
}

_GENERIC_MESSAGE_BY_CODE = {
    "internal_error": "an internal error occurred",
    "service_unavailable": "a required external dependency is unavailable",
}


def _expose_handler(status_code: int, error_code: str):
    def _handle(request: Request, exc: NapsEdgeError) -> JSONResponse:
        logger.warning("%s on %s %s: %s", type(exc).__name__, request.method, request.url.path, exc)
        return JSONResponse(status_code=status_code, content={"error": error_code, "message": str(exc)})

    return _handle


def _sanitized_handler(status_code: int, error_code: str, generic_message: str):
    def _handle(request: Request, exc: Exception) -> JSONResponse:
        logger.error("%s on %s %s", type(exc).__name__, request.method, request.url.path, exc_info=exc)
        return JSONResponse(status_code=status_code, content={"error": error_code, "message": generic_message})

    return _handle


def _authentication_error_handler(request: Request, exc: AuthenticationError) -> JSONResponse:
    """
    401 for every "not authenticated" case -- missing header, wrong scheme,
    unknown client, wrong token -- with an identical sanitized body and the
    `WWW-Authenticate: Bearer` header a Bearer-auth client expects. Never
    logs or includes the presented credential; `str(exc)` is intentionally
    not used here (unlike `_expose_handler`) so the response body can never
    drift from this one fixed message regardless of the raise site's text.
    """
    logger.warning("authentication failed on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=401,
        content={"error": "unauthorized", "message": "missing or invalid authentication credentials"},
        headers={"WWW-Authenticate": "Bearer"},
    )


def _rate_limit_exceeded_handler(request: Request, exc: RateLimitExceededError) -> JSONResponse:
    """
    Phase 6D: 429 for a client that has exhausted its fixed-window
    allowance. Given its own dedicated handler (like
    `_authentication_error_handler`) rather than the generic
    `_sanitized_handler` factory, because it needs to set a per-request
    `Retry-After` header from `exc.retry_after_seconds` -- something no
    other registered exception carries. Logs `exc.client_id` server-side
    only; the response body carries a stable `"rate_limited"` error code
    and a generic message -- never the client_id, the configured
    threshold, or any internal timestamp.
    """
    logger.warning(
        "rate limit exceeded for client_id=%s on %s %s", exc.client_id, request.method, request.url.path
    )
    return JSONResponse(
        status_code=429,
        content={"error": "rate_limited", "message": "rate limit exceeded, please retry later"},
        headers={"Retry-After": str(exc.retry_after_seconds)},
    )


def _sanitize_non_finite(value):
    """
    Replaces a non-finite float (`NaN`/`Infinity`/`-Infinity`) with a
    safe string placeholder, recursively through nested dicts/lists --
    everything else is returned unchanged.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return "<non-finite value>"
    if isinstance(value, dict):
        return {key: _sanitize_non_finite(v) for key, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_non_finite(v) for v in value]
    return value


def _request_validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """
    Phase 6, Slice 7B: FastAPI's own default handler for this exception
    (used automatically whenever none is registered) echoes each error's
    raw offending `input` value back in the response body -- almost
    always JSON-safe, but a client-supplied `NaN`/`Infinity`/`-Infinity`
    float is not: Starlette's own `JSONResponse` renders with
    `allow_nan=False`, so echoing that raw value back crashes the
    response entirely instead of returning a clean 422 (first surfaced
    by Slice 7B's Matchup endpoint, the first route in this application
    with a Pydantic-validated JSON request body -- no earlier route,
    which only ever validated path/query parameters, could reach this
    path). Sanitizes only that one narrow, previously-unreachable case;
    every other validation-error detail is preserved exactly as
    FastAPI's own default handler would have produced it.
    """
    sanitized_errors = [{**error, "input": _sanitize_non_finite(error.get("input"))} for error in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(sanitized_errors)})


def register_exception_handlers(app: FastAPI) -> None:
    for exc_type, (status_code, error_code) in _EXPOSE_DETAIL.items():
        app.add_exception_handler(exc_type, _expose_handler(status_code, error_code))
    for exc_type, (status_code, error_code) in _SANITIZED.items():
        app.add_exception_handler(
            exc_type, _sanitized_handler(status_code, error_code, _GENERIC_MESSAGE_BY_CODE[error_code])
        )
    app.add_exception_handler(AuthenticationError, _authentication_error_handler)
    app.add_exception_handler(RateLimitExceededError, _rate_limit_exceeded_handler)
    app.add_exception_handler(RequestValidationError, _request_validation_error_handler)
    # Phase 9B-8A: every capped connection is in use and none freed within the pool timeout -- a temporary,
    # server-side capacity condition (sanitized 503, retryable), not the generic 500.
    app.add_exception_handler(
        SqlAlchemyPoolTimeout,
        _sanitized_handler(503, "service_unavailable", _GENERIC_MESSAGE_BY_CODE["service_unavailable"]),
    )
    # Anything not a NapsEdgeError at all -- unknown/unexpected -- sanitized 500.
    app.add_exception_handler(
        Exception, _sanitized_handler(500, "internal_error", _GENERIC_MESSAGE_BY_CODE["internal_error"])
    )
