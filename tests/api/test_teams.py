"""
Phase 6, Slice 3B: API-level tests for the Bearer-authenticated Team
endpoints `GET /v1/teams/{team_id}` and `GET /v1/teams/resolve`.

**Internal, non-deployed development/testing only.** No live server is
left running: every test drives the app through FastAPI's TestClient.
"""
from __future__ import annotations

from sqlalchemy import create_engine, text

from src.api.deps import get_authenticated_client, get_session
from src.api.main import create_app
from src.auth.api_keys import AuthenticatedClient
from src.db.repositories import TeamRepository
from src.db.session import build_session_factory, unit_of_work


# ---------------------------------------------------------------------------
# Authentication behavior
# ---------------------------------------------------------------------------


def test_missing_credential_rejected_with_typed_401(team_client):
    response = team_client.get(f"/v1/teams/1")
    assert response.status_code == 401
    body = response.json()
    assert body == {"error": "unauthorized", "message": "missing or invalid authentication credentials"}
    assert response.headers["www-authenticate"] == "Bearer"


def test_invalid_credential_rejected_with_the_identical_typed_401(team_client):
    response = team_client.get("/v1/teams/1", headers={"Authorization": "Bearer not-a-real-token"})
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized", "message": "missing or invalid authentication credentials"}
    assert response.headers["www-authenticate"] == "Bearer"


def test_unsupported_authentication_scheme_is_rejected(team_client):
    response = team_client.get("/v1/teams/1", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert response.status_code == 401


def test_malformed_authorization_header_is_rejected(team_client):
    response = team_client.get("/v1/teams/1", headers={"Authorization": "NotBearerAtAll"})
    assert response.status_code == 401


def test_absent_authentication_configuration_returns_sanitized_503(seeded_team_db_url, auth_unconfigured, bonaventure_identity):
    app = create_app(database_url=seeded_team_db_url)
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        response = client.get(f"/v1/teams/{bonaventure_identity.team_id}", headers={"Authorization": "Bearer anything"})

    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "service_unavailable"
    assert "NAPSEDGE_API_KEY_HASHES_JSON" not in body["message"]


def test_valid_authentication_reaches_the_team_service(team_client, auth_headers, bonaventure_identity):
    response = team_client.get(f"/v1/teams/{bonaventure_identity.team_id}", headers=auth_headers)
    assert response.status_code == 200


def test_missing_credential_never_opens_a_database_session(seeded_team_db_url, auth_configured):
    """Proves the auth-before-session ordering: get_session must never be invoked when authentication fails."""
    app = create_app(database_url=seeded_team_db_url)
    called = []

    def _spy(request):
        called.append(True)
        yield from get_session(request)

    app.dependency_overrides[get_session] = _spy
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        response = client.get("/v1/teams/1")

    assert response.status_code == 401
    assert called == [], "get_session must not be invoked when authentication fails"


def test_authentication_dependency_override_bypasses_real_verification(seeded_team_db_url, bonaventure_identity):
    """Confirms app.dependency_overrides works for get_authenticated_client."""
    app = create_app(database_url=seeded_team_db_url)
    app.dependency_overrides[get_authenticated_client] = lambda: AuthenticatedClient(client_id="override-client")
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        response = client.get(f"/v1/teams/{bonaventure_identity.team_id}")  # no Authorization header needed
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Team lookup / resolution (authenticated)
# ---------------------------------------------------------------------------


def test_get_team_by_id_returns_200_and_the_exact_typed_schema(team_client, auth_headers, bonaventure_identity):
    response = team_client.get(f"/v1/teams/{bonaventure_identity.team_id}", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == {
        "team_id": bonaventure_identity.team_id,
        "canonical_name": bonaventure_identity.display_name,
        "canonical_slug": bonaventure_identity.sources["sports_reference"].raw_value,
        "conference_name": None,
    }


def test_get_team_by_id_returns_404_for_an_unknown_team(team_client, auth_headers):
    response = team_client.get("/v1/teams/999999999", headers=auth_headers)
    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "not_found"
    assert "message" in body


def test_resolve_team_by_canonical_name_returns_200(team_client, auth_headers, bonaventure_identity):
    response = team_client.get("/v1/teams/resolve", params={"query": "St. Bonaventure"}, headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["team_id"] == bonaventure_identity.team_id


def test_resolve_team_by_slug_variant_returns_the_same_team(team_client, auth_headers, bonaventure_identity):
    response = team_client.get("/v1/teams/resolve", params={"query": "st-bonaventure"}, headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["team_id"] == bonaventure_identity.team_id


def test_resolve_team_unresolved_returns_404(team_client, auth_headers):
    response = team_client.get("/v1/teams/resolve", params={"query": "Zzzzznotarealteam99"}, headers=auth_headers)
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_resolve_team_ambiguous_returns_409(team_client, auth_headers):
    response = team_client.get("/v1/teams/resolve", params={"query": "Miami"}, headers=auth_headers)
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "conflict"
    assert "Miami" in body["message"]


def test_resolve_team_resolved_but_missing_row_returns_404(team_client, auth_headers):
    """Davidson resolves cleanly in the identity registry but was never seeded into this test database."""
    response = team_client.get("/v1/teams/resolve", params={"query": "Davidson"}, headers=auth_headers)
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_resolve_team_empty_query_is_rejected_by_request_validation(team_client, auth_headers):
    response = team_client.get("/v1/teams/resolve", params={"query": ""}, headers=auth_headers)
    assert response.status_code == 422


def test_resolve_team_missing_query_parameter_is_rejected(team_client, auth_headers):
    response = team_client.get("/v1/teams/resolve", headers=auth_headers)
    assert response.status_code == 422


def test_resolve_is_not_captured_by_the_team_id_path(team_client, auth_headers):
    """"resolve" must never be coerced into team_id: int -- confirmed by getting a real 200/404 from the /resolve route, not a 422 int-coercion error."""
    response = team_client.get("/v1/teams/resolve", params={"query": "Zzzzznotarealteam99"}, headers=auth_headers)
    assert response.status_code == 404  # not 422 (which int-coercion of "resolve" would produce)


def test_invalid_int_team_id_returns_422(team_client, auth_headers):
    response = team_client.get("/v1/teams/not-a-number", headers=auth_headers)
    assert response.status_code == 422


def test_conference_present_when_actually_assigned(seeded_team_db_url, auth_headers, bonaventure_identity):
    engine = create_engine(seeded_team_db_url, connect_args={"check_same_thread": False})
    try:
        session_factory = build_session_factory(engine)
        with unit_of_work(session_factory) as session:
            from src.db.repositories import ConferenceRepository

            ConferenceRepository(session).upsert(conference_id=1, canonical_name="Atlantic 10")
            TeamRepository(session).upsert(
                team_id=bonaventure_identity.team_id,
                display_name=bonaventure_identity.display_name,
                sr_slug=bonaventure_identity.sources["sports_reference"].raw_value,
                conference_id=1,
            )
    finally:
        engine.dispose()

    app = create_app(database_url=seeded_team_db_url)
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        response = client.get(f"/v1/teams/{bonaventure_identity.team_id}", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["conference_name"] == "Atlantic 10"


def test_duplicate_directory_team_miami_fl_returns_exactly_one_canonical_record(team_client, auth_headers):
    """Simulates the real Miami (FL) duplicate-source-directory finding at the API level: two idempotent upserts for the same team_id, one canonical response."""
    from src.identity.contracts import TeamIdentity
    from src.identity.team_identity import ensure_team_registry, resolve_team_identity

    ensure_team_registry()
    identity = resolve_team_identity("Miami (FL)", source="test")
    assert isinstance(identity, TeamIdentity)

    app_state_engine = team_client.app.state.engine
    with unit_of_work(build_session_factory(app_state_engine)) as session:
        repo = TeamRepository(session)
        repo.upsert(team_id=identity.team_id, display_name=identity.display_name, sr_slug=identity.sources["sports_reference"].raw_value)
        repo.upsert(team_id=identity.team_id, display_name=identity.display_name, sr_slug=identity.sources["sports_reference"].raw_value)

    by_id = team_client.get(f"/v1/teams/{identity.team_id}", headers=auth_headers)
    by_name = team_client.get("/v1/teams/resolve", params={"query": "Miami (FL)"}, headers=auth_headers)
    assert by_id.status_code == by_name.status_code == 200
    assert by_id.json() == by_name.json() == {
        "team_id": identity.team_id,
        "canonical_name": identity.display_name,
        "canonical_slug": identity.sources["sports_reference"].raw_value,
        "conference_name": None,
    }


# ---------------------------------------------------------------------------
# Read-only guarantee / cleanup
# ---------------------------------------------------------------------------


def test_team_endpoints_perform_no_write_to_the_database(team_client, auth_headers, bonaventure_identity, seeded_team_db_url):
    engine = create_engine(seeded_team_db_url, connect_args={"check_same_thread": False})
    try:
        with engine.connect() as conn:
            before = conn.execute(text("SELECT team_id, display_name, sr_slug, conference_id FROM teams")).fetchall()

        for _ in range(3):
            team_client.get(f"/v1/teams/{bonaventure_identity.team_id}", headers=auth_headers)
            team_client.get("/v1/teams/resolve", params={"query": "St. Bonaventure"}, headers=auth_headers)

        with engine.connect() as conn:
            after = conn.execute(text("SELECT team_id, display_name, sr_slug, conference_id FROM teams")).fetchall()

        assert before == after
    finally:
        engine.dispose()


def test_get_session_cleans_up_after_a_service_raised_exception(team_client, auth_headers):
    """IdentityConflictError raised inside the service must still leave the session dependency's cleanup (rollback + close) intact."""
    response = team_client.get("/v1/teams/resolve", params={"query": "Miami"}, headers=auth_headers)
    assert response.status_code == 409
    response2 = team_client.get("/v1/teams/resolve", params={"query": "St. Bonaventure"}, headers=auth_headers)
    assert response2.status_code == 200


def test_repeated_team_requests_are_deterministic(team_client, auth_headers, bonaventure_identity):
    responses = [team_client.get(f"/v1/teams/{bonaventure_identity.team_id}", headers=auth_headers) for _ in range(5)]
    assert all(r.status_code == 200 for r in responses)
    assert all(r.json() == responses[0].json() for r in responses)


def test_health_and_ready_remain_accessible_without_authentication(team_client):
    health = team_client.get("/v1/health")
    ready = team_client.get("/v1/ready")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}


def test_no_raw_credential_appears_in_any_response_body(team_client, auth_headers, bonaventure_identity):
    """Sweeps every response this test module can cheaply produce for the literal token text."""
    from tests.api.conftest import VALID_TOKEN

    responses = [
        team_client.get(f"/v1/teams/{bonaventure_identity.team_id}", headers=auth_headers),
        team_client.get("/v1/teams/1"),  # no credential
        team_client.get("/v1/teams/1", headers={"Authorization": "Bearer wrong-token"}),
    ]
    for response in responses:
        assert VALID_TOKEN not in response.text
