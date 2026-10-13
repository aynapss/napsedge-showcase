"""
Phase 6B: contract tests for `src.api.errors.register_exception_handlers`.

Uses a throwaway FastAPI app with dummy routes that deliberately raise
each `src.exceptions.NapsEdgeError` subclass -- these routes exist only in
this test module, never in `src/api/main.py`, so production code carries
no test-only route.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from src.api.errors import register_exception_handlers
from src.exceptions import (
    AdvisoryLockUnavailableError,
    ConfigurationError,
    DatabaseTargetVerificationError,
    IdentityConflictError,
    IdentityResolutionError,
    IngestionError,
    SourceUnavailableError,
    ValidationError,
)


def _make_app_raising(exc: Exception) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    def boom():
        raise exc

    return app


EXPOSED_CASES = [
    (ValidationError("season must be a four-digit year"), 422, "validation_error", "season must be a four-digit year"),
    (IdentityConflictError("team_id 1 already exists with a different sr_slug"), 409, "conflict", "team_id 1 already exists with a different sr_slug"),
]

SANITIZED_CASES = [
    (SourceUnavailableError("the odds API could not be reached"), 503, "service_unavailable"),
    (IdentityResolutionError("registry never built"), 500, "internal_error"),
    (ConfigurationError("NAPSEDGE_DATABASE_URL is malformed"), 500, "internal_error"),
    (DatabaseTargetVerificationError("connection points at an administrative database"), 500, "internal_error"),
    (AdvisoryLockUnavailableError("lock already held"), 500, "internal_error"),
    (IngestionError("scrape step failed"), 500, "internal_error"),
]


@pytest.mark.parametrize("exc,status_code,error_code,expected_message", EXPOSED_CASES)
def test_exposed_exceptions_map_to_expected_status_and_carry_their_own_message(exc, status_code, error_code, expected_message):
    app = _make_app_raising(exc)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom")

    assert response.status_code == status_code
    body = response.json()
    assert body["error"] == error_code
    assert body["message"] == expected_message


@pytest.mark.parametrize("exc,status_code,error_code", SANITIZED_CASES)
def test_sanitized_exceptions_map_to_expected_status_with_a_generic_message(exc, status_code, error_code):
    app = _make_app_raising(exc)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom")

    assert response.status_code == status_code
    body = response.json()
    assert body["error"] == error_code
    # The exception's own message must never reach the client for sanitized cases.
    assert str(exc) not in body["message"]


def test_unknown_exception_maps_to_a_sanitized_500():
    app = _make_app_raising(RuntimeError("some unexpected internal failure with a secret path C:\\Users\\Anthony\\.env"))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom")

    assert response.status_code == 500
    body = response.json()
    assert body["error"] == "internal_error"
    assert "secret" not in body["message"]
    assert ".env" not in body["message"]


def test_sanitized_response_never_contains_a_stack_trace_or_traceback_marker():
    app = _make_app_raising(ConfigurationError("ODDS_API_KEY is missing"))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom")

    text = response.text
    assert "Traceback" not in text
    assert "File \"" not in text


# ---------------------------------------------------------------------------
# RequestValidationError -- Phase 6, Slice 7B fix for a non-finite-float
# crash. Matchup's own POST /v1/matchups/feature-comparison endpoint was
# the first route in this application with a Pydantic-validated JSON
# request body -- every earlier route only ever validated path/query
# parameters, so this exact crash path was previously unreachable. A
# throwaway app+route here proves the fix generically, independent of
# the Matchup route itself.
# ---------------------------------------------------------------------------


class _EchoPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    value: float | None = None


def _make_app_with_body_route():
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/echo")
    def echo(payload: _EchoPayload):
        return {"value": payload.value}

    return app


def test_non_finite_float_in_request_body_returns_422_without_crashing():
    """The exact crash this fix addresses: FastAPI's own default handler tries to echo a raw NaN/Infinity value back in the error body, which Starlette's strict JSONResponse (allow_nan=False) cannot serialize."""
    app = _make_app_with_body_route()
    with TestClient(app, raise_server_exceptions=False) as client:
        for raw_literal in ("NaN", "Infinity", "-Infinity"):
            body = '{"value": %s}' % raw_literal
            response = client.post("/echo", content=body, headers={"Content-Type": "application/json"})
            assert response.status_code == 422, f"expected 422 for {raw_literal!r}, got {response.status_code}"
            assert "detail" in response.json()


def test_non_finite_float_error_detail_contains_no_raw_infinity_or_nan_token():
    app = _make_app_with_body_route()
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/echo", content='{"value": Infinity}', headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["input"] == "<non-finite value>"


def test_ordinary_validation_error_still_returns_standard_fastapi_shape():
    """Confirms this fix did not change behavior for the normal (finite, JSON-safe) validation-error case."""
    app = _make_app_with_body_route()
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/echo", json={"unexpected_field": 1})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["type"] == "extra_forbidden"
    assert detail[0]["input"] == 1


def test_valid_finite_body_is_unaffected_by_the_validation_error_handler():
    app = _make_app_with_body_route()
    with TestClient(app) as client:
        response = client.post("/echo", json={"value": 5.2})
    assert response.status_code == 200
    assert response.json() == {"value": 5.2}
