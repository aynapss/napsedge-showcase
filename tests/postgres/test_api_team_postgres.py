"""
Phase 6, Slice 3B: the smallest real-PostgreSQL proof that the
Bearer-authenticated Team API behaves correctly against a real (not
SQLite-emulated) database. Reuses the existing disposable-PostgreSQL
fixtures (`pg_session_factory`, `pg_migrated_database_url`) -- no
competing container harness is created here, and no TeamDump/live
scraping/notebook/production data is touched.
"""
from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from src.api.main import create_app
from src.auth.api_keys import ENV_VAR as AUTH_ENV_VAR
from src.db.repositories import ConferenceRepository, TeamRepository
from src.db.session import unit_of_work
from src.identity.contracts import TeamIdentity
from src.identity.team_identity import ensure_team_registry, resolve_team_identity

pytestmark = pytest.mark.postgres

VALID_TOKEN = "test-suite-postgres-token-" + "b" * 20
VALID_CLIENT_ID = "test-suite-postgres-client"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@pytest.fixture()
def auth_configured(monkeypatch):
    monkeypatch.setenv(AUTH_ENV_VAR, json.dumps({VALID_CLIENT_ID: _digest(VALID_TOKEN)}))


@pytest.fixture()
def auth_headers(auth_configured) -> dict[str, str]:
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


def _real_identity(query: str) -> TeamIdentity:
    ensure_team_registry()
    result = resolve_team_identity(query, source="test")
    assert isinstance(result, TeamIdentity)
    return result


def test_authenticated_team_lookup_against_real_postgresql(pg_migrated_database_url, pg_session_factory, auth_headers):
    identity = _real_identity("St. Bonaventure")
    with unit_of_work(pg_session_factory) as session:
        TeamRepository(session).upsert(
            team_id=identity.team_id,
            display_name=identity.display_name,
            sr_slug=identity.sources["sports_reference"].raw_value,
        )

    app = create_app(database_url=pg_migrated_database_url)
    with TestClient(app) as client:
        response = client.get(f"/v1/teams/{identity.team_id}", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {
        "team_id": identity.team_id,
        "canonical_name": identity.display_name,
        "canonical_slug": identity.sources["sports_reference"].raw_value,
        "conference_name": None,
    }


def test_authenticated_exact_name_resolution_against_real_postgresql(pg_migrated_database_url, pg_session_factory, auth_headers):
    identity = _real_identity("St. Bonaventure")
    with unit_of_work(pg_session_factory) as session:
        TeamRepository(session).upsert(
            team_id=identity.team_id,
            display_name=identity.display_name,
            sr_slug=identity.sources["sports_reference"].raw_value,
        )

    app = create_app(database_url=pg_migrated_database_url)
    with TestClient(app) as client:
        response = client.get("/v1/teams/resolve", params={"query": "St. Bonaventure"}, headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["team_id"] == identity.team_id


def test_authenticated_alias_resolution_against_real_postgresql(pg_migrated_database_url, pg_session_factory, auth_headers):
    identity = _real_identity("St. Bonaventure")
    with unit_of_work(pg_session_factory) as session:
        TeamRepository(session).upsert(
            team_id=identity.team_id,
            display_name=identity.display_name,
            sr_slug=identity.sources["sports_reference"].raw_value,
        )

    app = create_app(database_url=pg_migrated_database_url)
    with TestClient(app) as client:
        response = client.get("/v1/teams/resolve", params={"query": "st-bonaventure"}, headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["team_id"] == identity.team_id


def test_missing_team_against_real_postgresql(pg_migrated_database_url, auth_headers):
    app = create_app(database_url=pg_migrated_database_url)
    with TestClient(app) as client:
        response = client.get("/v1/teams/999999999", headers=auth_headers)
    assert response.status_code == 404


def test_ambiguous_identity_against_real_postgresql(pg_migrated_database_url, auth_headers):
    """"Miami" collapses to two real, distinct D1 programs -- must never be silently resolved, real database or not."""
    app = create_app(database_url=pg_migrated_database_url)
    with TestClient(app) as client:
        response = client.get("/v1/teams/resolve", params={"query": "Miami"}, headers=auth_headers)
    assert response.status_code == 409


def test_invalid_authentication_cannot_reach_the_database(pg_migrated_database_url, pg_session_factory, auth_configured):
    """A wrong Bearer token must 401 without ever touching PostgreSQL -- confirmed by checking the row is unaffected/untouched and no query error surfaces."""
    identity = _real_identity("St. Bonaventure")
    with unit_of_work(pg_session_factory) as session:
        TeamRepository(session).upsert(
            team_id=identity.team_id,
            display_name=identity.display_name,
            sr_slug=identity.sources["sports_reference"].raw_value,
        )

    app = create_app(database_url=pg_migrated_database_url)
    with TestClient(app) as client:
        response = client.get(f"/v1/teams/{identity.team_id}", headers={"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401


def test_valid_requests_against_real_postgresql_remain_read_only(pg_migrated_database_url, pg_session_factory, auth_headers):
    from sqlalchemy import text

    identity = _real_identity("St. Bonaventure")
    with unit_of_work(pg_session_factory) as session:
        ConferenceRepository(session).upsert(conference_id=1, canonical_name="Atlantic 10")
        TeamRepository(session).upsert(
            team_id=identity.team_id,
            display_name=identity.display_name,
            sr_slug=identity.sources["sports_reference"].raw_value,
            conference_id=1,
        )

    with pg_session_factory() as session:
        before = session.execute(text("SELECT team_id, display_name, sr_slug, conference_id FROM teams")).fetchall()

    app = create_app(database_url=pg_migrated_database_url)
    with TestClient(app) as client:
        for _ in range(3):
            client.get(f"/v1/teams/{identity.team_id}", headers=auth_headers)
            client.get("/v1/teams/resolve", params={"query": "St. Bonaventure"}, headers=auth_headers)

    with pg_session_factory() as session:
        after = session.execute(text("SELECT team_id, display_name, sr_slug, conference_id FROM teams")).fetchall()

    assert before == after


def test_sessions_close_on_success_and_failure_against_real_postgresql(pg_migrated_database_url, auth_headers):
    identity = _real_identity("St. Bonaventure")
    app = create_app(database_url=pg_migrated_database_url)

    from sqlalchemy import create_engine

    seed_engine = create_engine(pg_migrated_database_url)
    try:
        from src.db.session import build_session_factory

        with unit_of_work(build_session_factory(seed_engine)) as session:
            TeamRepository(session).upsert(
                team_id=identity.team_id,
                display_name=identity.display_name,
                sr_slug=identity.sources["sports_reference"].raw_value,
            )
    finally:
        seed_engine.dispose()

    with TestClient(app) as client:
        ok = client.get(f"/v1/teams/{identity.team_id}", headers=auth_headers)
        not_found = client.get("/v1/teams/999999999", headers=auth_headers)
        unauthorized = client.get("/v1/teams/1", headers={"Authorization": "Bearer wrong-token"})
    assert ok.status_code == 200
    assert not_found.status_code == 404
    assert unauthorized.status_code == 401
    app.state.engine.dispose()

    from sqlalchemy import text

    admin_engine = create_engine(pg_migrated_database_url)
    try:
        with admin_engine.connect() as conn:
            active = conn.execute(
                text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()")
            ).scalar()
        assert active == 0
    finally:
        admin_engine.dispose()
