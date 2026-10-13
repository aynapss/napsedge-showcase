"""
Phase 6, Slice 7B: API-level tests for the Bearer-authenticated
`POST /v1/matchups/feature-comparison` endpoint.

This route has no database dependency at all -- every test constructs the
app against a disposable, never-touched SQLite path (the route never
opens `get_session`), confirmed explicitly by a dedicated test below.
Every request payload here is a synthetic, explicit, bounded feature set
-- no team lookup, no file, no odds, no agent, ever.
"""
from __future__ import annotations

import ast
import inspect
import math

import pytest
from fastapi.testclient import TestClient

from src.api.deps import get_authenticated_client
from src.api.main import create_app
from src.auth.api_keys import AuthenticatedClient

MATCHUP_PATH = "/v1/matchups/feature-comparison"


@pytest.fixture()
def matchup_client(migrated_db_url, auth_configured) -> TestClient:
    """
    Uses the shared migrated database only so `/v1/health`/`/v1/ready`
    behave normally in tests that also check them -- the Matchup route
    itself never touches this database at all (confirmed by a dedicated
    test below using a genuinely unreachable path).
    """
    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as test_client:
        yield test_client


def _payload(team_a=None, team_b=None, team_a_slug="duke", team_b_slug="unc"):
    return {
        "team_a_slug": team_a_slug,
        "team_b_slug": team_b_slug,
        "team_a_features": team_a or {},
        "team_b_features": team_b or {},
    }


# ---------------------------------------------------------------------------
# Application construction / route inventory
# ---------------------------------------------------------------------------


def test_application_constructs_without_executing_matchup_logic(tmp_path, monkeypatch):
    import src.features.matchup_features as matchup_features_module

    calls = []
    monkeypatch.setattr(matchup_features_module, "build_matchup_feature_comparison", lambda *a, **k: calls.append(1))

    bad_path = tmp_path / "no_db.db"
    app = create_app(database_url=f"sqlite+pysqlite:///{bad_path.as_posix()}")
    app.openapi()
    assert calls == []


def test_exactly_nine_routes_exist(tmp_path):
    bad_path = tmp_path / "no_db.db"
    app = create_app(database_url=f"sqlite+pysqlite:///{bad_path.as_posix()}")
    schema = app.openapi()
    paths = set(schema["paths"].keys())
    assert paths == {
        "/v1/health", "/v1/ready", "/v1/teams/resolve", "/v1/teams/{team_id}",
        "/v1/teams/{team_id}/injuries", "/v1/players/{player_id}/raw-statistics",
        "/v1/rankings/net", "/v1/rankings/net/teams/{team_id}",
        "/v1/matchups/feature-comparison",
    }


def test_matchup_route_is_post_not_get(tmp_path):
    bad_path = tmp_path / "no_db.db"
    app = create_app(database_url=f"sqlite+pysqlite:///{bad_path.as_posix()}")
    schema = app.openapi()
    assert list(schema["paths"]["/v1/matchups/feature-comparison"].keys()) == ["post"]


def test_openapi_applies_bearer_security_to_the_matchup_route(tmp_path):
    bad_path = tmp_path / "no_db.db"
    app = create_app(database_url=f"sqlite+pysqlite:///{bad_path.as_posix()}")
    schema = app.openapi()
    assert schema["paths"]["/v1/matchups/feature-comparison"]["post"]["security"] == [{"HTTPBearer": []}]
    assert schema["paths"]["/v1/health"]["get"].get("security") in (None, [])
    assert schema["paths"]["/v1/ready"]["get"].get("security") in (None, [])


def test_openapi_documents_expected_status_codes(tmp_path):
    bad_path = tmp_path / "no_db.db"
    app = create_app(database_url=f"sqlite+pysqlite:///{bad_path.as_posix()}")
    schema = app.openapi()
    responses = set(schema["paths"]["/v1/matchups/feature-comparison"]["post"]["responses"].keys())
    assert {"200", "401", "422", "503"}.issubset(responses)


def test_openapi_contains_no_credential_or_odds_example(tmp_path):
    bad_path = tmp_path / "no_db.db"
    app = create_app(database_url=f"sqlite+pysqlite:///{bad_path.as_posix()}")
    schema_text = str(app.openapi())
    for forbidden in ("test-suite-token", "NAPSEDGE_API_KEY_HASHES_JSON", "ODDS_API_KEY", "spread", "moneyline"):
        assert forbidden not in schema_text


# ---------------------------------------------------------------------------
# Authentication -- no session dependency at all
# ---------------------------------------------------------------------------


def test_missing_authentication_returns_established_401(matchup_client):
    response = matchup_client.post(MATCHUP_PATH, json=_payload())
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized", "message": "missing or invalid authentication credentials"}
    assert response.headers["www-authenticate"] == "Bearer"


def test_invalid_token_returns_the_same_401(matchup_client):
    response = matchup_client.post(MATCHUP_PATH, json=_payload(), headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


def test_unsupported_scheme_returns_the_same_401(matchup_client):
    response = matchup_client.post(MATCHUP_PATH, json=_payload(), headers={"Authorization": "Basic xyz"})
    assert response.status_code == 401


def test_unconfigured_authentication_returns_sanitized_503(tmp_path, auth_unconfigured):
    bad_path = tmp_path / "no_db.db"
    app = create_app(database_url=f"sqlite+pysqlite:///{bad_path.as_posix()}")
    with TestClient(app) as client:
        response = client.post(MATCHUP_PATH, json=_payload(), headers={"Authorization": "Bearer anything"})
    assert response.status_code == 503
    assert response.json()["error"] == "service_unavailable"


def test_authentication_failure_never_calls_the_matchup_service(matchup_client, monkeypatch):
    import src.api.routes.matchup as matchup_route_module

    calls = []
    monkeypatch.setattr(matchup_route_module, "compare_team_matchup_features", lambda *a, **k: calls.append(1))
    response = matchup_client.post(MATCHUP_PATH, json=_payload())
    assert response.status_code == 401
    assert calls == []


def test_authentication_dependency_override_bypasses_real_verification(tmp_path):
    bad_path = tmp_path / "no_db.db"
    app = create_app(database_url=f"sqlite+pysqlite:///{bad_path.as_posix()}")
    app.dependency_overrides[get_authenticated_client] = lambda: AuthenticatedClient(client_id="override-client")
    with TestClient(app) as client:
        response = client.post(MATCHUP_PATH, json=_payload({"avg_margin": 5.2}, {"avg_margin": 2.1}))  # no Authorization header
    assert response.status_code == 200


def test_matchup_route_requests_no_database_session(tmp_path, auth_configured):
    """Dynamic proof: pointing at a database path whose parent directory doesn't exist must not raise -- get_session is never invoked."""
    bad_path = tmp_path / "does_not_exist_dir" / "impossible.db"
    app = create_app(database_url=f"sqlite+pysqlite:///{bad_path.as_posix()}")
    with TestClient(app) as client:
        response = client.post(
            MATCHUP_PATH, json=_payload({"avg_margin": 5.2}, {"avg_margin": 2.1}), headers={"Authorization": "Bearer test-suite-token-" + "a" * 24}
        )
    assert response.status_code == 200


def test_matchup_route_has_no_session_dependency_declared():
    """Static guard: the route function signature must not declare a Session/get_session parameter at all."""
    import src.api.routes.matchup as matchup_route_module

    source = inspect.getsource(matchup_route_module)
    assert "get_session" not in source
    assert "Session" not in source


# ---------------------------------------------------------------------------
# Outcome translation -- valid comparisons
# ---------------------------------------------------------------------------


def test_valid_comparison_returns_200(matchup_client, auth_headers):
    response = matchup_client.post(
        MATCHUP_PATH, json=_payload({"avg_margin": 5.234, "off_impact": -8.75}, {"avg_margin": 0.0, "off_impact": 0.0}), headers=auth_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["team_a_slug"] == "duke"
    assert body["team_b_slug"] == "unc"
    assert body["deltas"]["avg_margin"] == 5.234
    assert body["deltas"]["off_impact"] == -8.75


def test_exact_seven_feature_keys_present_in_deltas(matchup_client, auth_headers):
    response = matchup_client.post(MATCHUP_PATH, json=_payload({"avg_margin": 1.0}, {}), headers=auth_headers)
    assert set(response.json()["deltas"].keys()) == {
        "avg_margin", "recent_avg_margin", "avg_net_rtg", "recent_avg_net_rtg",
        "player_value_index", "off_impact", "def_impact",
    }


def test_positive_delta(matchup_client, auth_headers):
    response = matchup_client.post(MATCHUP_PATH, json=_payload({"avg_margin": 10.0}, {"avg_margin": 3.0}), headers=auth_headers)
    assert response.json()["deltas"]["avg_margin"] == 7.0
    assert response.json()["key_edges"][0]["leader"] == "duke"


def test_negative_delta(matchup_client, auth_headers):
    response = matchup_client.post(MATCHUP_PATH, json=_payload({"avg_margin": 3.0}, {"avg_margin": 10.0}), headers=auth_headers)
    assert response.json()["deltas"]["avg_margin"] == -7.0
    assert response.json()["key_edges"][0]["leader"] == "unc"


def test_zero_delta_is_included_not_treated_as_missing(matchup_client, auth_headers):
    response = matchup_client.post(MATCHUP_PATH, json=_payload({"avg_margin": 5.0}, {"avg_margin": 5.0}), headers=auth_headers)
    body = response.json()
    assert body["deltas"]["avg_margin"] == 0.0
    assert body["key_edges"][0]["delta"] == 0.0
    assert body["key_edges"][0]["leader"] == "duke"  # tie defaults to team_a


def test_stored_zero_input_preserved(matchup_client, auth_headers):
    response = matchup_client.post(MATCHUP_PATH, json=_payload({"avg_margin": 0.0}, {"avg_margin": 5.0}), headers=auth_headers)
    assert response.json()["deltas"]["avg_margin"] == -5.0


def test_unavailable_value_serializes_as_null(matchup_client, auth_headers):
    response = matchup_client.post(MATCHUP_PATH, json=_payload({"avg_margin": 5.0}, {}), headers=auth_headers)
    body = response.json()
    assert body["deltas"]["recent_avg_margin"] is None
    assert "recent_avg_margin" not in {e["signal"] for e in body["key_edges"]}


def test_omitted_and_explicit_null_are_equivalent(matchup_client, auth_headers):
    omitted = matchup_client.post(MATCHUP_PATH, json=_payload({}, {"avg_margin": 2.0}), headers=auth_headers)
    explicit_null = matchup_client.post(
        MATCHUP_PATH, json=_payload({"avg_margin": None}, {"avg_margin": 2.0}), headers=auth_headers
    )
    assert omitted.json() == explicit_null.json()


def test_both_sides_unavailable_returns_200_with_empty_key_edges(matchup_client, auth_headers):
    response = matchup_client.post(MATCHUP_PATH, json=_payload({}, {}), headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert all(v is None for v in body["deltas"].values())
    assert body["key_edges"] == []


def test_three_decimal_rounding_preserved(matchup_client, auth_headers):
    response = matchup_client.post(
        MATCHUP_PATH, json=_payload({"avg_margin": 1.0 / 3.0}, {"avg_margin": 0.0}), headers=auth_headers
    )
    assert response.json()["deltas"]["avg_margin"] == round(1.0 / 3.0, 3)


def test_magnitude_descending_ordering_preserved(matchup_client, auth_headers):
    response = matchup_client.post(
        MATCHUP_PATH,
        json=_payload(
            {"avg_margin": 1.0, "off_impact": -9.0, "def_impact": 3.0},
            {"avg_margin": 0.0, "off_impact": 0.0, "def_impact": 0.0},
        ),
        headers=auth_headers,
    )
    magnitudes = [abs(e["delta"]) for e in response.json()["key_edges"]]
    assert magnitudes == sorted(magnitudes, reverse=True)


def test_swapped_team_order_reverses_sign_as_guaranteed_by_the_authority(matchup_client, auth_headers):
    """Only tests the swap behavior the underlying compute_feature_delta actually guarantees: delta(a,b) == -delta(b,a)."""
    forward = matchup_client.post(MATCHUP_PATH, json=_payload({"avg_margin": 7.0}, {"avg_margin": 2.0}), headers=auth_headers)
    backward = matchup_client.post(
        MATCHUP_PATH,
        json={"team_a_slug": "unc", "team_b_slug": "duke", "team_a_features": {"avg_margin": 2.0}, "team_b_features": {"avg_margin": 7.0}},
        headers=auth_headers,
    )
    assert forward.json()["deltas"]["avg_margin"] == -backward.json()["deltas"]["avg_margin"]


def test_repeated_requests_are_deterministic(matchup_client, auth_headers):
    payload = _payload({"avg_margin": 5.2, "off_impact": -1.1}, {"avg_margin": 2.1, "off_impact": 3.3})
    responses = [matchup_client.post(MATCHUP_PATH, json=payload, headers=auth_headers) for _ in range(5)]
    assert all(r.status_code == 200 for r in responses)
    assert all(r.json() == responses[0].json() for r in responses)


# ---------------------------------------------------------------------------
# Request validation -- 422s
# ---------------------------------------------------------------------------


def test_unknown_feature_field_returns_422(matchup_client, auth_headers):
    response = matchup_client.post(MATCHUP_PATH, json=_payload({"spread": 5.0}, {}), headers=auth_headers)
    assert response.status_code == 422


def test_unknown_top_level_field_returns_422(matchup_client, auth_headers):
    payload = _payload()
    payload["odds"] = {"spread": -3.5}
    response = matchup_client.post(MATCHUP_PATH, json=payload, headers=auth_headers)
    assert response.status_code == 422


def test_malformed_shape_returns_422(matchup_client, auth_headers):
    response = matchup_client.post(MATCHUP_PATH, json={"team_a_slug": "duke"}, headers=auth_headers)
    assert response.status_code == 422


def test_boolean_feature_value_returns_422(matchup_client, auth_headers):
    response = matchup_client.post(MATCHUP_PATH, json=_payload({"avg_margin": True}, {}), headers=auth_headers)
    assert response.status_code == 422


def _post_raw_json(client, headers, team_a_literal: str):
    """
    Python's `json` module emits bare, non-standard `NaN`/`Infinity`/
    `-Infinity` tokens by default (`allow_nan=True`) -- exactly the shape
    a real misbehaving/adversarial client could send. Constructed by hand
    here since `json.dumps` on an actual `float("nan")` produces the
    identical bare token, so this is equivalent to, not a workaround of,
    a real request body.
    """
    body = '{"team_a_slug": "duke", "team_b_slug": "unc", "team_a_features": {"avg_margin": %s}, "team_b_features": {}}' % team_a_literal
    return client.post(MATCHUP_PATH, content=body, headers={**headers, "Content-Type": "application/json"})


def test_nan_feature_value_returns_422(matchup_client, auth_headers):
    response = _post_raw_json(matchup_client, auth_headers, "NaN")
    assert response.status_code == 422


def test_positive_infinity_feature_value_returns_422(matchup_client, auth_headers):
    response = _post_raw_json(matchup_client, auth_headers, "Infinity")
    assert response.status_code == 422


def test_negative_infinity_feature_value_returns_422(matchup_client, auth_headers):
    response = _post_raw_json(matchup_client, auth_headers, "-Infinity")
    assert response.status_code == 422


def test_excessive_team_slug_length_returns_422(matchup_client, auth_headers):
    response = matchup_client.post(MATCHUP_PATH, json=_payload(team_a_slug="x" * 5000), headers=auth_headers)
    assert response.status_code == 422


def test_empty_team_slug_returns_422(matchup_client, auth_headers):
    response = matchup_client.post(MATCHUP_PATH, json=_payload(team_a_slug=""), headers=auth_headers)
    assert response.status_code == 422


def test_numeric_string_feature_value_returns_422(matchup_client, auth_headers):
    response = matchup_client.post(MATCHUP_PATH, json=_payload({"avg_margin": "5.2"}, {}), headers=auth_headers)
    assert response.status_code == 422


def test_nested_arbitrary_mapping_feature_value_returns_422(matchup_client, auth_headers):
    response = matchup_client.post(MATCHUP_PATH, json=_payload({"avg_margin": {"nested": 1}}, {}), headers=auth_headers)
    assert response.status_code == 422


def test_no_odds_prediction_or_injury_field_accepted(matchup_client, auth_headers):
    for forbidden_field, value in [("spread", -3.5), ("win_probability", 0.6), ("injury_report", {}), ("net_rank", 1)]:
        payload = _payload()
        payload[forbidden_field] = value
        response = matchup_client.post(MATCHUP_PATH, json=payload, headers=auth_headers)
        assert response.status_code == 422, f"expected 422 for unexpected top-level field {forbidden_field!r}"


# ---------------------------------------------------------------------------
# Sanitized internal-error handling
# ---------------------------------------------------------------------------


def test_unexpected_internal_error_is_sanitized(matchup_client, auth_headers, monkeypatch):
    import src.api.routes.matchup as matchup_route_module

    def _boom(*args, **kwargs):
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(matchup_route_module, "compare_team_matchup_features", _boom)
    with TestClient(matchup_client.app, raise_server_exceptions=False) as client:
        response = client.post(MATCHUP_PATH, json=_payload({"avg_margin": 5.2}, {"avg_margin": 2.1}), headers=auth_headers)
    assert response.status_code == 500
    assert response.json()["error"] == "internal_error"
    assert "RuntimeError" not in response.text
    assert "unexpected failure" not in response.text


# ---------------------------------------------------------------------------
# No-network / no-odds / no-agent static guards
# ---------------------------------------------------------------------------


def test_matchup_route_module_imports_no_network_odds_or_agent_module():
    import src.api.routes.matchup as matchup_route_module

    tree = ast.parse(inspect.getsource(matchup_route_module))
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
            imported_names.update(a.name for a in node.names)
    forbidden = ("requests", "httpx", "urllib", "selenium", "agents", "discord", "market_features")
    for name in imported_names:
        assert not any(f in name for f in forbidden), f"unexpected import {name!r} in src/api/routes/matchup.py"


def test_matchup_route_module_contains_no_filesystem_identity_or_calculation_logic():
    import src.api.routes.matchup as matchup_route_module

    source = inspect.getsource(matchup_route_module)
    forbidden = [
        "open(", "read_text(", "CBB_DIR", "resolve_team_identity", "TeamRepository(",
        "select(", "SELECT ", "difflib", "get_close_matches", "round(", "sorted(",
    ]
    for token in forbidden:
        assert token not in source, f"unexpected {token!r} found in src/api/routes/matchup.py"


def test_matchup_route_requires_no_odds_api_key(matchup_client, auth_headers, monkeypatch):
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    response = matchup_client.post(MATCHUP_PATH, json=_payload({"avg_margin": 5.2}, {"avg_margin": 2.1}), headers=auth_headers)
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Team/Player/Rankings/Injury unaffected
# ---------------------------------------------------------------------------


def test_team_player_rankings_and_injury_routes_unaffected(migrated_db_url, auth_headers, rankings_configured):
    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        team = client.get("/v1/teams/1", headers=auth_headers)
        player = client.get("/v1/players/x/raw-statistics", headers=auth_headers)
        rankings = client.get("/v1/rankings/net", headers=auth_headers)
        injury = client.get("/v1/teams/1/injuries", headers=auth_headers)
    assert team.status_code == 404
    assert player.status_code == 404
    assert rankings.status_code == 200
    assert injury.status_code == 404


def test_health_and_ready_remain_unauthenticated(matchup_client):
    assert matchup_client.get("/v1/health").status_code == 200
    assert matchup_client.get("/v1/ready").status_code == 200
