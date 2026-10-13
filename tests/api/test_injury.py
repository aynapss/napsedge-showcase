"""
Phase 6, Slice 6B: API-level tests for the Bearer-authenticated
`GET /v1/teams/{team_id}/injuries` endpoint.

Every test uses a synthetic, disposable local Injury cache -- `CBB_DIR` is
monkeypatched to a temporary directory (never the real `TeamDump/`) so
these tests are fully hermetic and never depend on, or mutate, real cached
data. Real-cache end-to-end parity for this exact endpoint was confirmed
manually against the real Duke fixture during implementation (documented
in the completion report); Slice 6A's own real-cache integration tests
already cover the underlying authority directly.
"""
from __future__ import annotations

import ast
import inspect
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from src.api.deps import get_authenticated_client, get_session
from src.api.main import create_app
from src.auth.api_keys import AuthenticatedClient
from src.db.repositories import TeamRepository
from src.db.session import build_session_factory, unit_of_work
from src.features import team_summary as team_summary_module

INJURY_API_TEAM_ID = 990001
INJURY_API_TEAM_SLUG = "injury-api-test-team"


def _write_team_injuries(cbb_dir, team_slug: str, payload) -> None:
    team_dir = cbb_dir / team_slug
    team_dir.mkdir(parents=True, exist_ok=True)
    (team_dir / f"{team_slug}_injuries.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture()
def injury_cbb_dir(tmp_path, monkeypatch):
    """Redirects the extracted authority's default candidate-path lookup to a disposable directory -- never the real TeamDump/CBB tree."""
    cbb_dir = tmp_path / "CBB"
    monkeypatch.setattr(team_summary_module, "CBB_DIR", cbb_dir)
    return cbb_dir


@pytest.fixture()
def seeded_injury_api_team_db_url(migrated_db_url):
    engine = create_engine(migrated_db_url, connect_args={"check_same_thread": False})
    try:
        session_factory = build_session_factory(engine)
        with unit_of_work(session_factory) as session:
            TeamRepository(session).upsert(team_id=INJURY_API_TEAM_ID, display_name="Injury API Test Team", sr_slug=INJURY_API_TEAM_SLUG)
    finally:
        engine.dispose()
    return migrated_db_url


@pytest.fixture()
def injury_api_client(seeded_injury_api_team_db_url, auth_configured, injury_cbb_dir) -> TestClient:
    app = create_app(database_url=seeded_injury_api_team_db_url)
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Application construction / route inventory
# ---------------------------------------------------------------------------


def test_application_constructs_without_touching_the_injury_source(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(team_summary_module, "build_injury_report", lambda *a, **k: calls.append(1))

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
        "/v1/players/{player_id}/raw-statistics",
        "/v1/rankings/net", "/v1/rankings/net/teams/{team_id}",
        "/v1/teams/{team_id}/injuries",
        "/v1/matchups/feature-comparison",
    }


def test_openapi_applies_bearer_security_to_the_injury_route(tmp_path):
    bad_path = tmp_path / "no_db.db"
    app = create_app(database_url=f"sqlite+pysqlite:///{bad_path.as_posix()}")
    schema = app.openapi()
    assert schema["paths"]["/v1/teams/{team_id}/injuries"]["get"]["security"] == [{"HTTPBearer": []}]
    assert schema["paths"]["/v1/health"]["get"].get("security") in (None, [])
    assert schema["paths"]["/v1/ready"]["get"].get("security") in (None, [])


def test_openapi_documents_expected_status_codes_on_the_injury_route(tmp_path):
    bad_path = tmp_path / "no_db.db"
    app = create_app(database_url=f"sqlite+pysqlite:///{bad_path.as_posix()}")
    schema = app.openapi()
    responses = set(schema["paths"]["/v1/teams/{team_id}/injuries"]["get"]["responses"].keys())
    assert {"200", "401", "404", "503"}.issubset(responses)


def test_openapi_contains_no_filesystem_or_credential_example(tmp_path):
    bad_path = tmp_path / "no_db.db"
    app = create_app(database_url=f"sqlite+pysqlite:///{bad_path.as_posix()}")
    schema_text = str(app.openapi())
    for forbidden in ("TeamDump", "CBB", "injury_cache", "search_paths", "test-suite-token", "NAPSEDGE_API_KEY_HASHES_JSON"):
        assert forbidden not in schema_text


def test_no_client_controlled_path_query_parameter_exists(tmp_path):
    bad_path = tmp_path / "no_db.db"
    app = create_app(database_url=f"sqlite+pysqlite:///{bad_path.as_posix()}")
    schema = app.openapi()
    params = schema["paths"]["/v1/teams/{team_id}/injuries"]["get"].get("parameters", [])
    param_names = {p["name"] for p in params}
    assert param_names == {"team_id"}  # only the path parameter -- no season/path/search_paths/source query param


# ---------------------------------------------------------------------------
# Authentication -- short-circuits everything downstream
# ---------------------------------------------------------------------------


def test_missing_authentication_returns_established_401(injury_api_client):
    response = injury_api_client.get(f"/v1/teams/{INJURY_API_TEAM_ID}/injuries")
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized", "message": "missing or invalid authentication credentials"}
    assert response.headers["www-authenticate"] == "Bearer"


def test_invalid_token_returns_the_same_401(injury_api_client):
    response = injury_api_client.get(f"/v1/teams/{INJURY_API_TEAM_ID}/injuries", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


def test_unsupported_scheme_returns_the_same_401(injury_api_client):
    response = injury_api_client.get(f"/v1/teams/{INJURY_API_TEAM_ID}/injuries", headers={"Authorization": "Basic xyz"})
    assert response.status_code == 401


def test_unconfigured_authentication_returns_sanitized_503(seeded_injury_api_team_db_url, auth_unconfigured, injury_cbb_dir):
    app = create_app(database_url=seeded_injury_api_team_db_url)
    with TestClient(app) as client:
        response = client.get(f"/v1/teams/{INJURY_API_TEAM_ID}/injuries", headers={"Authorization": "Bearer anything"})
    assert response.status_code == 503
    assert response.json()["error"] == "service_unavailable"


def test_authentication_failure_never_opens_a_database_session(seeded_injury_api_team_db_url, auth_configured):
    app = create_app(database_url=seeded_injury_api_team_db_url)
    called = []

    def _spy(request):
        called.append(True)
        yield from get_session(request)

    app.dependency_overrides[get_session] = _spy
    with TestClient(app) as client:
        response = client.get(f"/v1/teams/{INJURY_API_TEAM_ID}/injuries")

    assert response.status_code == 401
    assert called == []


def test_authentication_failure_never_calls_the_injury_service(seeded_injury_api_team_db_url, auth_configured, monkeypatch):
    import src.api.routes.injury as injury_route_module

    calls = []
    monkeypatch.setattr(injury_route_module, "get_injury_report_for_team", lambda *a, **k: calls.append(1))

    app = create_app(database_url=seeded_injury_api_team_db_url)
    with TestClient(app) as client:
        response = client.get(f"/v1/teams/{INJURY_API_TEAM_ID}/injuries")

    assert response.status_code == 401
    assert calls == []


def test_authentication_failure_never_touches_the_filesystem(seeded_injury_api_team_db_url, auth_configured, monkeypatch):
    calls = []
    monkeypatch.setattr(team_summary_module, "build_injury_report", lambda *a, **k: calls.append(1))

    app = create_app(database_url=seeded_injury_api_team_db_url)
    with TestClient(app) as client:
        response = client.get(f"/v1/teams/{INJURY_API_TEAM_ID}/injuries")

    assert response.status_code == 401
    assert calls == []


def test_authentication_dependency_override_bypasses_real_verification(seeded_injury_api_team_db_url, injury_cbb_dir):
    _write_team_injuries(injury_cbb_dir, INJURY_API_TEAM_SLUG, [])
    app = create_app(database_url=seeded_injury_api_team_db_url)
    app.dependency_overrides[get_authenticated_client] = lambda: AuthenticatedClient(client_id="override-client")
    with TestClient(app) as client:
        response = client.get(f"/v1/teams/{INJURY_API_TEAM_ID}/injuries")  # no Authorization header at all
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Outcome translation
# ---------------------------------------------------------------------------


def test_unknown_team_returns_typed_404(injury_api_client, auth_headers):
    response = injury_api_client.get("/v1/teams/1/injuries", headers=auth_headers)
    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "not_found"
    assert "message" in body


def test_loaded_report_with_entries_returns_200(injury_api_client, auth_headers, injury_cbb_dir):
    _write_team_injuries(
        injury_cbb_dir, INJURY_API_TEAM_SLUG,
        [{"player": "smith", "status": "out", "detail": "Out - Ankle"}, {"player": "jones", "status": "questionable"}],
    )
    response = injury_api_client.get(f"/v1/teams/{INJURY_API_TEAM_ID}/injuries", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["team_id"] == INJURY_API_TEAM_ID
    assert body["impactful_count"] == 2
    assert len(body["injuries"]) == 2
    assert body["injuries"][0] == {"player": "smith", "status": "out", "detail": "Out - Ankle"}
    assert body["injuries"][1] == {"player": "jones", "status": "questionable", "detail": None}


def test_loaded_report_with_zero_entries_returns_200_with_empty_list(injury_api_client, auth_headers, injury_cbb_dir):
    """A confirmed, successfully-loaded empty report must be 200 + [], never 503."""
    _write_team_injuries(injury_cbb_dir, INJURY_API_TEAM_SLUG, [])
    response = injury_api_client.get(f"/v1/teams/{INJURY_API_TEAM_ID}/injuries", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["injuries"] == []
    assert body["impactful_count"] == 0


def test_unavailable_report_returns_sanitized_503_not_200_empty(injury_api_client, auth_headers):
    """No cache file at all for a known team -- must be 503, never 200 + []."""
    response = injury_api_client.get(f"/v1/teams/{INJURY_API_TEAM_ID}/injuries", headers=auth_headers)
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "service_unavailable"
    assert "message" in body
    assert body != {"team_id": INJURY_API_TEAM_ID, "injuries": [], "impactful_count": 0}


def test_malformed_element_returns_dedicated_sanitized_503(injury_api_client, auth_headers, injury_cbb_dir):
    _write_team_injuries(injury_cbb_dir, INJURY_API_TEAM_SLUG, ["not-a-dict-entry"])
    response = injury_api_client.get(f"/v1/teams/{INJURY_API_TEAM_ID}/injuries", headers=auth_headers)
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "service_unavailable"


def test_malformed_element_response_leaks_no_raw_exception_or_path(injury_api_client, auth_headers, injury_cbb_dir):
    _write_team_injuries(injury_cbb_dir, INJURY_API_TEAM_SLUG, ["not-a-dict-entry"])
    response = injury_api_client.get(f"/v1/teams/{INJURY_API_TEAM_ID}/injuries", headers=auth_headers)
    text_body = response.text
    for forbidden in ("not-a-dict-entry", "AttributeError", "Traceback", "site-packages", "C:\\", str(injury_api_client.app.state.engine.url)):
        assert forbidden not in text_body


def test_invalid_team_id_path_parameter_returns_established_422(injury_api_client, auth_headers):
    response = injury_api_client.get("/v1/teams/not-a-number/injuries", headers=auth_headers)
    assert response.status_code == 422


def test_unrelated_attribute_error_is_not_mapped_to_the_injury_integrity_error(
    seeded_injury_api_team_db_url, auth_configured, auth_headers, monkeypatch
):
    import src.services.injury as injury_service_module

    def _boom(*args, **kwargs):
        raise AttributeError("unrelated failure elsewhere")

    monkeypatch.setattr(injury_service_module, "get_team_injury_report", _boom)
    app = create_app(database_url=seeded_injury_api_team_db_url)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(f"/v1/teams/{INJURY_API_TEAM_ID}/injuries", headers=auth_headers)

    # An unrelated AttributeError is not a NapsEdgeError -- it must fall through
    # to the generic, centrally-handled 500, not the Injury-specific 503.
    assert response.status_code == 500
    assert response.json()["error"] == "internal_error"


# ---------------------------------------------------------------------------
# Field fidelity -- service -> Pydantic -> JSON parity
# ---------------------------------------------------------------------------


def test_none_serializes_as_json_null(injury_api_client, auth_headers, injury_cbb_dir):
    _write_team_injuries(injury_cbb_dir, INJURY_API_TEAM_SLUG, [{"player": "smith", "status": "out"}])
    response = injury_api_client.get(f"/v1/teams/{INJURY_API_TEAM_ID}/injuries", headers=auth_headers)
    assert response.json()["injuries"][0]["detail"] is None


def test_status_designation_text_preserved_verbatim(injury_api_client, auth_headers, injury_cbb_dir):
    _write_team_injuries(injury_cbb_dir, INJURY_API_TEAM_SLUG, [{"player": "smith", "status": "Day-To-Day (Ankle)"}])
    response = injury_api_client.get(f"/v1/teams/{INJURY_API_TEAM_ID}/injuries", headers=auth_headers)
    assert response.json()["injuries"][0]["status"] == "Day-To-Day (Ankle)"


def test_deterministic_ordering_preserved(injury_api_client, auth_headers, injury_cbb_dir):
    payload = [{"player": "a", "status": "out"}, {"player": "b", "status": "questionable"}, {"player": "c", "status": "doubtful"}]
    _write_team_injuries(injury_cbb_dir, INJURY_API_TEAM_SLUG, payload)
    response = injury_api_client.get(f"/v1/teams/{INJURY_API_TEAM_ID}/injuries", headers=auth_headers)
    assert [item["player"] for item in response.json()["injuries"]] == ["a", "b", "c"]


def test_no_fabricated_field_in_the_response(injury_api_client, auth_headers, injury_cbb_dir):
    _write_team_injuries(injury_cbb_dir, INJURY_API_TEAM_SLUG, [{"player": "smith", "status": "out"}])
    response = injury_api_client.get(f"/v1/teams/{INJURY_API_TEAM_ID}/injuries", headers=auth_headers)
    body = response.json()
    for forbidden in ("as_of", "is_current", "freshness", "severity", "probability", "recovery_date", "raw"):
        assert forbidden not in body
        assert forbidden not in body["injuries"][0]


def test_no_internal_path_or_identifier_exposed_in_a_successful_response(injury_api_client, auth_headers, injury_cbb_dir):
    _write_team_injuries(injury_cbb_dir, INJURY_API_TEAM_SLUG, [{"player": "smith", "status": "out"}])
    response = injury_api_client.get(f"/v1/teams/{INJURY_API_TEAM_ID}/injuries", headers=auth_headers)
    assert str(injury_cbb_dir) not in response.text
    assert INJURY_API_TEAM_SLUG not in response.text
    assert "team_slug" not in response.json()
    assert "source_path" not in response.json()


def test_repeated_requests_are_deterministic(injury_api_client, auth_headers, injury_cbb_dir):
    _write_team_injuries(injury_cbb_dir, INJURY_API_TEAM_SLUG, [{"player": "a", "status": "out"}, {"player": "b", "status": "questionable"}])
    responses = [injury_api_client.get(f"/v1/teams/{INJURY_API_TEAM_ID}/injuries", headers=auth_headers) for _ in range(5)]
    assert all(r.status_code == 200 for r in responses)
    assert all(r.json() == responses[0].json() for r in responses)


def test_route_performs_no_write_to_the_injury_cache_file(injury_api_client, auth_headers, injury_cbb_dir):
    path = injury_cbb_dir / INJURY_API_TEAM_SLUG / f"{INJURY_API_TEAM_SLUG}_injuries.json"
    _write_team_injuries(injury_cbb_dir, INJURY_API_TEAM_SLUG, [{"player": "smith", "status": "out"}])
    before = path.read_bytes()
    for _ in range(3):
        injury_api_client.get(f"/v1/teams/{INJURY_API_TEAM_ID}/injuries", headers=auth_headers)
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# No-network / static purity guards
# ---------------------------------------------------------------------------


def test_injury_route_module_imports_no_network_capable_function():
    from src.api.routes import injury as injury_route_module

    tree = ast.parse(inspect.getsource(injury_route_module))
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
            imported_names.update(a.name for a in node.names)
    forbidden = ("requests", "httpx", "urllib", "selenium")
    for name in imported_names:
        assert not any(f in name for f in forbidden), f"unexpected network-capable import {name!r}"


def test_injury_route_module_contains_no_filesystem_identity_or_calculation_logic():
    from src.api.routes import injury as injury_route_module

    source = inspect.getsource(injury_route_module)
    forbidden = [
        "open(", "read_text(", "json.loads(", "CBB_DIR", "resolve_team_identity", "TeamRepository(",
        "select(", "SELECT ", "difflib", "get_close_matches",
    ]
    for token in forbidden:
        assert token not in source, f"unexpected {token!r} found in src/api/routes/injury.py"


# ---------------------------------------------------------------------------
# Team/Player/Rankings unaffected
# ---------------------------------------------------------------------------


def test_team_player_and_rankings_routes_unaffected(migrated_db_url, auth_headers, rankings_configured):
    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        team = client.get("/v1/teams/1", headers=auth_headers)
        player = client.get("/v1/players/x/raw-statistics", headers=auth_headers)
        rankings = client.get("/v1/rankings/net", headers=auth_headers)
    assert team.status_code == 404
    assert player.status_code == 404
    assert rankings.status_code == 200


def test_health_and_ready_remain_unauthenticated(injury_api_client):
    assert injury_api_client.get("/v1/health").status_code == 200
    assert injury_api_client.get("/v1/ready").status_code == 200
