"""
Phase 6D: API-level tests for the combined authentication + fixed-window
rate-limiting dependency (`src.api.deps.get_rate_limited_client`) applied
to the seven protected data routes.

These tests exercise the *wiring* -- ordering, exemptions, error contract,
OpenAPI documentation, dependency overrides -- not the limiter's own core
counting logic, which is covered exhaustively (including concurrency and
clock injection) by the framework-independent
`tests/regression/test_rate_limit.py`. Every test here sets a small,
deterministic `NAPSEDGE_RATE_LIMIT_REQUESTS`/`NAPSEDGE_RATE_LIMIT_WINDOW_SECONDS`
via `monkeypatch` -- never the real 60/60 production default -- so no test
here can spuriously trip, or rely on, the production policy.
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import json

import pytest
from fastapi.testclient import TestClient

from src.api.deps import get_rate_limited_client
from src.api.main import create_app
from src.auth.api_keys import ENV_VAR as AUTH_ENV_VAR
from src.auth.api_keys import AuthenticatedClient
from src.auth.rate_limit import REQUESTS_ENV_VAR, WINDOW_SECONDS_ENV_VAR

PROTECTED_ROUTES = [
    ("GET", "/v1/teams/resolve?query=duke"),
    ("GET", "/v1/teams/1"),
    ("GET", "/v1/teams/1/injuries"),
    ("GET", "/v1/players/some-player/raw-statistics"),
    ("GET", "/v1/rankings/net"),
    ("GET", "/v1/rankings/net/teams/1"),
    ("POST", "/v1/matchups/feature-comparison"),
]

_MATCHUP_BODY = {
    "team_a_slug": "duke",
    "team_b_slug": "unc",
    "team_a_features": {},
    "team_b_features": {},
}


def _request(client: TestClient, method: str, path: str, headers: dict[str, str] | None = None):
    if method == "POST":
        return client.post(path, json=_MATCHUP_BODY, headers=headers)
    return client.get(path, headers=headers)


# ---------------------------------------------------------------------------
# Each protected endpoint is rate-limited; health/ready are exempt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path", PROTECTED_ROUTES)
def test_each_protected_endpoint_is_rate_limited(
    method, path, migrated_db_url, auth_headers, rankings_configured, monkeypatch
):
    monkeypatch.setenv(REQUESTS_ENV_VAR, "2")
    monkeypatch.setenv(WINDOW_SECONDS_ENV_VAR, "60")
    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        first = _request(client, method, path, auth_headers)
        second = _request(client, method, path, auth_headers)
        third = _request(client, method, path, auth_headers)

    assert first.status_code != 429
    assert second.status_code != 429
    assert third.status_code == 429


def test_health_and_ready_are_exempt_from_rate_limiting(migrated_db_url, monkeypatch):
    monkeypatch.setenv(REQUESTS_ENV_VAR, "1")
    monkeypatch.setenv(WINDOW_SECONDS_ENV_VAR, "60")
    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        health_responses = [client.get("/v1/health") for _ in range(5)]
        ready_responses = [client.get("/v1/ready") for _ in range(5)]

    assert all(r.status_code == 200 for r in health_responses)
    assert all(r.status_code == 200 for r in ready_responses)


# ---------------------------------------------------------------------------
# Ordering: authentication runs before rate limiting
# ---------------------------------------------------------------------------


def test_failed_authentication_never_consumes_a_rate_limit_token(migrated_db_url, auth_headers, monkeypatch):
    monkeypatch.setenv(REQUESTS_ENV_VAR, "1")
    monkeypatch.setenv(WINDOW_SECONDS_ENV_VAR, "60")
    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        unauthenticated = [client.get("/v1/teams/1") for _ in range(5)]
        authenticated = client.get("/v1/teams/1", headers=auth_headers)

    assert all(r.status_code == 401 for r in unauthenticated)
    # The single-request budget is still intact -- none of the five failed
    # authentication attempts consumed it.
    assert authenticated.status_code != 429


# ---------------------------------------------------------------------------
# Ordering: rate limiting runs before session/service/file access
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/v1/teams/1", "/v1/players/x/raw-statistics", "/v1/teams/1/injuries"])
def test_rate_limiting_precedes_database_session_creation(path, unreachable_db_url, auth_headers, monkeypatch):
    """
    An unreachable database path makes any real `get_session` call fail.
    The first (allowed) request must fail with a database error (not 429);
    the second (blocked) request must fail with 429 (not a database
    error) -- proving the limiter runs, and rejects, before `get_session`
    is ever invoked.
    """
    monkeypatch.setenv(REQUESTS_ENV_VAR, "1")
    monkeypatch.setenv(WINDOW_SECONDS_ENV_VAR, "60")
    app = create_app(database_url=unreachable_db_url)
    with TestClient(app, raise_server_exceptions=False) as client:
        first = client.get(path, headers=auth_headers)
        second = client.get(path, headers=auth_headers)

    assert first.status_code == 500
    assert second.status_code == 429


def test_rate_limiting_precedes_rankings_file_access(migrated_db_url, auth_headers, rankings_unconfigured, monkeypatch):
    """
    With Rankings configuration absent, a real `get_rankings_context` call
    fails closed with 503. The first (allowed) request must be 503; the
    second (blocked) request must be 429, not 503 -- proving the limiter
    rejects before the Rankings configuration/cache is ever touched.
    """
    monkeypatch.setenv(REQUESTS_ENV_VAR, "1")
    monkeypatch.setenv(WINDOW_SECONDS_ENV_VAR, "60")
    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        first = client.get("/v1/rankings/net", headers=auth_headers)
        second = client.get("/v1/rankings/net", headers=auth_headers)

    assert first.status_code == 503
    assert second.status_code == 429


def test_rate_limiting_precedes_the_matchup_service_call(migrated_db_url, auth_headers, monkeypatch):
    import src.api.routes.matchup as matchup_route_module

    monkeypatch.setenv(REQUESTS_ENV_VAR, "1")
    monkeypatch.setenv(WINDOW_SECONDS_ENV_VAR, "60")
    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        first = client.post("/v1/matchups/feature-comparison", json=_MATCHUP_BODY, headers=auth_headers)

        calls = []
        monkeypatch.setattr(matchup_route_module, "compare_team_matchup_features", lambda *a, **k: calls.append(1))
        second = client.post("/v1/matchups/feature-comparison", json=_MATCHUP_BODY, headers=auth_headers)

    assert first.status_code == 200
    assert second.status_code == 429
    assert calls == []


def test_matchup_route_still_requests_no_database_session_with_rate_limiting(unreachable_db_url, auth_headers, monkeypatch):
    """Confirms Phase 6D's new dependency did not introduce a session dependency on the Matchup route."""
    monkeypatch.setenv(REQUESTS_ENV_VAR, "60")
    monkeypatch.setenv(WINDOW_SECONDS_ENV_VAR, "60")
    app = create_app(database_url=unreachable_db_url)
    with TestClient(app) as client:
        response = client.post("/v1/matchups/feature-comparison", json=_MATCHUP_BODY, headers=auth_headers)
    assert response.status_code == 200


def test_rate_limiting_precedes_injury_file_access(migrated_db_url, auth_headers, monkeypatch):
    """
    A monkeypatched spy on the Injury authority proves a 429 never reaches
    it -- distinct from the session-precedence proof above (which only
    shows the Team lookup inside get_injury_report_for_team is never
    reached; this shows the Injury cache read specifically is never
    reached either, mirroring the existing Slice 6B
    `test_application_constructs_without_touching_the_injury_source`
    spy pattern).
    """
    import src.features.team_summary as team_summary_module

    monkeypatch.setenv(REQUESTS_ENV_VAR, "1")
    monkeypatch.setenv(WINDOW_SECONDS_ENV_VAR, "60")
    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        first = client.get("/v1/teams/1/injuries", headers=auth_headers)

        calls = []
        monkeypatch.setattr(team_summary_module, "build_injury_report", lambda *a, **k: calls.append(1))
        second = client.get("/v1/teams/1/injuries", headers=auth_headers)

    assert first.status_code == 404  # allowed request reaches the (empty-db) Team lookup and 404s cleanly
    assert second.status_code == 429
    assert calls == []


# ---------------------------------------------------------------------------
# Token accounting for non-200 admitted responses; independent clients
# ---------------------------------------------------------------------------


def test_a_downstream_404_still_consumes_exactly_one_admitted_token(migrated_db_url, auth_headers, monkeypatch):
    monkeypatch.setenv(REQUESTS_ENV_VAR, "2")
    monkeypatch.setenv(WINDOW_SECONDS_ENV_VAR, "60")
    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        first = client.get("/v1/teams/1", headers=auth_headers)   # admitted, 404 (empty db)
        second = client.get("/v1/teams/1", headers=auth_headers)  # admitted, 404 (empty db)
        third = client.get("/v1/teams/1", headers=auth_headers)   # budget of 2 exhausted

    assert first.status_code == 404
    assert second.status_code == 404
    assert third.status_code == 429


def test_a_downstream_200_also_consumes_exactly_one_admitted_token(migrated_db_url, auth_headers, rankings_configured, monkeypatch):
    monkeypatch.setenv(REQUESTS_ENV_VAR, "2")
    monkeypatch.setenv(WINDOW_SECONDS_ENV_VAR, "60")
    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        first = client.get("/v1/rankings/net", headers=auth_headers)
        second = client.get("/v1/rankings/net", headers=auth_headers)
        third = client.get("/v1/rankings/net", headers=auth_headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429


def test_separate_authenticated_clients_have_independent_rate_limits(migrated_db_url, monkeypatch):
    """Two distinct configured API keys (distinct client_id buckets) must not share one budget."""
    token_a = "test-suite-token-a-" + "a" * 20
    token_b = "test-suite-token-b-" + "b" * 20
    digest = lambda t: hashlib.sha256(t.encode("utf-8")).hexdigest()
    monkeypatch.setenv(AUTH_ENV_VAR, json.dumps({"client-a": digest(token_a), "client-b": digest(token_b)}))
    monkeypatch.setenv(REQUESTS_ENV_VAR, "1")
    monkeypatch.setenv(WINDOW_SECONDS_ENV_VAR, "60")

    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        a_first = client.get("/v1/teams/1", headers={"Authorization": f"Bearer {token_a}"})
        b_first = client.get("/v1/teams/1", headers={"Authorization": f"Bearer {token_b}"})
        a_second = client.get("/v1/teams/1", headers={"Authorization": f"Bearer {token_a}"})
        b_second = client.get("/v1/teams/1", headers={"Authorization": f"Bearer {token_b}"})

    assert a_first.status_code != 429
    assert b_first.status_code != 429
    assert a_second.status_code == 429  # client-a's own budget of 1 is exhausted
    assert b_second.status_code == 429  # client-b's own budget of 1 is exhausted, independently


# ---------------------------------------------------------------------------
# Malformed configuration scoping; application-instance isolation
# ---------------------------------------------------------------------------


def test_malformed_rate_limit_configuration_leaves_health_and_ready_unaffected(migrated_db_url, monkeypatch):
    monkeypatch.setenv(REQUESTS_ENV_VAR, "not-a-number")
    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        health = client.get("/v1/health")
        ready = client.get("/v1/ready")

    assert health.status_code == 200
    assert ready.status_code == 200


def test_two_application_instances_do_not_share_rate_limit_counters(migrated_db_url, auth_headers, monkeypatch):
    monkeypatch.setenv(REQUESTS_ENV_VAR, "1")
    monkeypatch.setenv(WINDOW_SECONDS_ENV_VAR, "60")

    app_one = create_app(database_url=migrated_db_url)
    app_two = create_app(database_url=migrated_db_url)
    assert app_one.state.rate_limiter is not app_two.state.rate_limiter

    with TestClient(app_one) as client_one, TestClient(app_two) as client_two:
        one_first = client_one.get("/v1/teams/1", headers=auth_headers)
        two_first = client_two.get("/v1/teams/1", headers=auth_headers)  # independent app -- must not inherit app_one's exhausted budget

    assert one_first.status_code != 429
    assert two_first.status_code != 429


# ---------------------------------------------------------------------------
# Static guard: every protected route uses the combined dependency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_path",
    [
        "src.api.routes.teams",
        "src.api.routes.players",
        "src.api.routes.rankings",
        "src.api.routes.injury",
        "src.api.routes.matchup",
    ],
)
def test_route_module_uses_the_combined_dependency_not_authentication_alone(module_path):
    """
    Static guard: no protected route module may reference
    `get_authenticated_client` directly (bare or via `Depends(...)`) --
    every one must go through `get_rate_limited_client` instead, so
    authentication and rate limiting can never accidentally drift apart
    on one route.
    """
    import importlib

    module = importlib.import_module(module_path)
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.update(a.name for a in node.names)

    assert "get_authenticated_client" not in names, f"{module_path} must depend on get_rate_limited_client, not get_authenticated_client directly"
    assert "get_rate_limited_client" in names, f"{module_path} must depend on get_rate_limited_client"


# ---------------------------------------------------------------------------
# Response-compatibility review: below the limit, every existing response
# shape (status, body, content-type) must be byte-identical to its
# pre-Phase-6D shape -- the only new externally-visible behavior anywhere
# in this application is the documented/enforced 429 and the sanitized 503
# for invalid limiter configuration.
# ---------------------------------------------------------------------------


def test_representative_responses_are_unchanged_below_the_rate_limit(
    migrated_db_url, auth_headers, rankings_configured, monkeypatch
):
    monkeypatch.setenv(REQUESTS_ENV_VAR, "60")  # production default -- well above every request made here
    monkeypatch.setenv(WINDOW_SECONDS_ENV_VAR, "60")
    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        health = client.get("/v1/health")
        ready = client.get("/v1/ready")
        team_missing_auth = client.get("/v1/teams/1")
        team_not_found = client.get("/v1/teams/1", headers=auth_headers)
        player_missing_auth = client.get("/v1/players/x/raw-statistics")
        rankings_ok = client.get("/v1/rankings/net", headers=auth_headers)
        injury_missing_auth = client.get("/v1/teams/1/injuries")
        matchup_ok = client.post(
            "/v1/matchups/feature-comparison",
            json={"team_a_slug": "duke", "team_b_slug": "unc", "team_a_features": {"avg_margin": 5.0}, "team_b_features": {"avg_margin": 2.0}},
            headers=auth_headers,
        )

    # Exact, pre-existing shapes -- unchanged from tests/api/test_health.py,
    # test_ready.py, and every prior slice's own auth/domain test files.
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert health.headers["content-type"] == "application/json"

    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}

    assert team_missing_auth.status_code == 401
    assert team_missing_auth.json() == {"error": "unauthorized", "message": "missing or invalid authentication credentials"}
    assert team_missing_auth.headers["www-authenticate"] == "Bearer"

    assert team_not_found.status_code == 404
    assert team_not_found.json() == {"error": "not_found", "message": "team not found"}

    assert player_missing_auth.status_code == 401

    assert rankings_ok.status_code == 200
    assert rankings_ok.json()["season"] == 2026
    assert rankings_ok.headers["content-type"] == "application/json"

    assert injury_missing_auth.status_code == 401

    assert matchup_ok.status_code == 200
    assert matchup_ok.json()["deltas"]["avg_margin"] == 3.0


# ---------------------------------------------------------------------------
# Error contract: sanitized 429 body, valid Retry-After
# ---------------------------------------------------------------------------


def test_429_body_is_sanitized_and_stable(migrated_db_url, auth_headers, monkeypatch):
    monkeypatch.setenv(REQUESTS_ENV_VAR, "1")
    monkeypatch.setenv(WINDOW_SECONDS_ENV_VAR, "60")
    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        client.get("/v1/teams/1", headers=auth_headers)
        blocked = client.get("/v1/teams/1", headers=auth_headers)

    assert blocked.status_code == 429
    body = blocked.json()
    assert set(body.keys()) == {"error", "message"}
    assert body["error"] == "rate_limited"
    forbidden_substrings = ["client_id", "test-suite-client", "test-suite-token", "sha256", "NAPSEDGE_RATE_LIMIT"]
    for forbidden in forbidden_substrings:
        assert forbidden not in blocked.text


def test_429_includes_a_valid_retry_after_header(migrated_db_url, auth_headers, monkeypatch):
    monkeypatch.setenv(REQUESTS_ENV_VAR, "1")
    monkeypatch.setenv(WINDOW_SECONDS_ENV_VAR, "30")
    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        client.get("/v1/teams/1", headers=auth_headers)
        blocked = client.get("/v1/teams/1", headers=auth_headers)

    assert blocked.status_code == 429
    retry_after = blocked.headers["retry-after"]
    assert retry_after.isdigit()
    assert 1 <= int(retry_after) <= 30


# ---------------------------------------------------------------------------
# Configuration failure: sanitized 503, never 429
# ---------------------------------------------------------------------------


def test_malformed_rate_limit_configuration_returns_sanitized_503(migrated_db_url, auth_headers, monkeypatch):
    monkeypatch.setenv(REQUESTS_ENV_VAR, "not-a-number")
    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        response = client.get("/v1/teams/1", headers=auth_headers)

    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "service_unavailable"
    assert "not-a-number" not in response.text


@pytest.mark.parametrize("bad_value", ["0", "-1", "100001"])
def test_zero_negative_and_excessive_request_configuration_returns_503(bad_value, migrated_db_url, auth_headers, monkeypatch):
    monkeypatch.setenv(REQUESTS_ENV_VAR, bad_value)
    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        response = client.get("/v1/teams/1", headers=auth_headers)
    assert response.status_code == 503


def test_unrelated_internal_errors_still_map_to_500_not_429(migrated_db_url, auth_headers, monkeypatch):
    """Confirms Phase 6D's new 429 handler is scoped to RateLimitExceededError only -- it must not swallow unrelated failures."""
    import src.api.routes.teams as teams_route_module

    def _boom(*args, **kwargs):
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(teams_route_module.team_service, "get_team", _boom)
    app = create_app(database_url=migrated_db_url)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/v1/teams/1", headers=auth_headers)

    assert response.status_code == 500
    assert response.json()["error"] == "internal_error"


# ---------------------------------------------------------------------------
# Dependency overrides
# ---------------------------------------------------------------------------


def test_rate_limited_client_dependency_override_bypasses_the_real_limiter_and_auth(migrated_db_url, monkeypatch):
    monkeypatch.setenv(REQUESTS_ENV_VAR, "1")
    app = create_app(database_url=migrated_db_url)
    app.dependency_overrides[get_rate_limited_client] = lambda: AuthenticatedClient(client_id="override-client")
    with TestClient(app) as client:
        # No Authorization header, and more requests than the tiny configured
        # limit would allow -- both are irrelevant once the dependency itself
        # is overridden.
        responses = [client.get("/v1/teams/1") for _ in range(5)]

    assert all(r.status_code == 404 for r in responses)  # authenticated + not rate-limited, team just doesn't exist


# ---------------------------------------------------------------------------
# OpenAPI documentation
# ---------------------------------------------------------------------------


def test_openapi_documents_429_on_every_protected_route_and_never_on_health_or_ready(tmp_path):
    bad_path = tmp_path / "no_db.db"
    app = create_app(database_url=f"sqlite+pysqlite:///{bad_path.as_posix()}")
    schema = app.openapi()

    protected_paths_and_methods = [
        ("/v1/teams/resolve", "get"),
        ("/v1/teams/{team_id}", "get"),
        ("/v1/teams/{team_id}/injuries", "get"),
        ("/v1/players/{player_id}/raw-statistics", "get"),
        ("/v1/rankings/net", "get"),
        ("/v1/rankings/net/teams/{team_id}", "get"),
        ("/v1/matchups/feature-comparison", "post"),
    ]
    for path, method in protected_paths_and_methods:
        assert "429" in schema["paths"][path][method]["responses"], f"missing 429 documentation on {method.upper()} {path}"

    assert "429" not in schema["paths"]["/v1/health"]["get"]["responses"]
    assert "429" not in schema["paths"]["/v1/ready"]["get"]["responses"]


def test_exactly_nine_routes_remain(tmp_path):
    bad_path = tmp_path / "no_db.db"
    app = create_app(database_url=f"sqlite+pysqlite:///{bad_path.as_posix()}")
    schema = app.openapi()
    assert set(schema["paths"].keys()) == {
        "/v1/health", "/v1/ready", "/v1/teams/resolve", "/v1/teams/{team_id}",
        "/v1/teams/{team_id}/injuries", "/v1/players/{player_id}/raw-statistics",
        "/v1/rankings/net", "/v1/rankings/net/teams/{team_id}",
        "/v1/matchups/feature-comparison",
    }
