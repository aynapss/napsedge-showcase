"""
Phase 6, Slice 6B: the smallest real-PostgreSQL proof that the
Bearer-authenticated Injury API's canonical-Team-to-Injury-slug
coordination behaves correctly against a real (not SQLite-emulated)
database. Reuses the existing disposable-PostgreSQL fixtures
(`pg_session_factory`, `pg_migrated_database_url`) -- no competing
container harness is created here. The local Injury cache lookup itself
is isolated to a disposable tmp_path directory (via a monkeypatched
`CBB_DIR`) -- no real `TeamDump/` file is ever read or written by this
file, only PostgreSQL is real here.
"""
from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from src.api.main import create_app
from src.auth.api_keys import ENV_VAR as AUTH_ENV_VAR
from src.db.repositories import TeamRepository
from src.db.session import unit_of_work
from src.features import team_summary as team_summary_module

pytestmark = pytest.mark.postgres

VALID_TOKEN = "test-suite-postgres-injury-token-" + "c" * 16
VALID_CLIENT_ID = "test-suite-postgres-injury-client"

PG_INJURY_TEAM_ID = 991001
PG_INJURY_TEAM_SLUG = "postgres-injury-test-team"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@pytest.fixture()
def auth_configured(monkeypatch):
    monkeypatch.setenv(AUTH_ENV_VAR, json.dumps({VALID_CLIENT_ID: _digest(VALID_TOKEN)}))


@pytest.fixture()
def auth_headers(auth_configured) -> dict[str, str]:
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


@pytest.fixture()
def injury_cbb_dir(tmp_path, monkeypatch):
    cbb_dir = tmp_path / "CBB"
    monkeypatch.setattr(team_summary_module, "CBB_DIR", cbb_dir)
    return cbb_dir


def _write_team_injuries(cbb_dir, team_slug: str, payload) -> None:
    team_dir = cbb_dir / team_slug
    team_dir.mkdir(parents=True, exist_ok=True)
    (team_dir / f"{team_slug}_injuries.json").write_text(json.dumps(payload), encoding="utf-8")


def _seed_team(pg_session_factory) -> None:
    with unit_of_work(pg_session_factory) as session:
        TeamRepository(session).upsert(team_id=PG_INJURY_TEAM_ID, display_name="PG Injury Test Team", sr_slug=PG_INJURY_TEAM_SLUG)


def test_authenticated_injury_lookup_resolves_canonical_slug_against_real_postgresql(
    pg_migrated_database_url, pg_session_factory, auth_headers, injury_cbb_dir
):
    """Proves the canonical team_id -> Team.sr_slug handoff (a real PostgreSQL row) reaches the Injury authority correctly."""
    _seed_team(pg_session_factory)
    _write_team_injuries(injury_cbb_dir, PG_INJURY_TEAM_SLUG, [{"player": "smith", "status": "out", "detail": "Out - Ankle"}])

    app = create_app(database_url=pg_migrated_database_url)
    with TestClient(app) as client:
        response = client.get(f"/v1/teams/{PG_INJURY_TEAM_ID}/injuries", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {
        "team_id": PG_INJURY_TEAM_ID,
        "injuries": [{"player": "smith", "status": "out", "detail": "Out - Ankle"}],
        "impactful_count": 1,
    }


def test_missing_team_against_real_postgresql(pg_migrated_database_url, auth_headers, injury_cbb_dir):
    app = create_app(database_url=pg_migrated_database_url)
    with TestClient(app) as client:
        response = client.get("/v1/teams/999999999/injuries", headers=auth_headers)
    assert response.status_code == 404


def test_known_team_with_unavailable_injury_source_against_real_postgresql(
    pg_migrated_database_url, pg_session_factory, auth_headers, injury_cbb_dir
):
    """A known team with no Injury cache file must be 503, never 404 -- the canonical team genuinely exists."""
    _seed_team(pg_session_factory)
    app = create_app(database_url=pg_migrated_database_url)
    with TestClient(app) as client:
        response = client.get(f"/v1/teams/{PG_INJURY_TEAM_ID}/injuries", headers=auth_headers)
    assert response.status_code == 503


def test_invalid_authentication_cannot_reach_the_database_or_filesystem(
    pg_migrated_database_url, pg_session_factory, auth_configured, injury_cbb_dir, monkeypatch
):
    """A wrong Bearer token must 401 without ever touching PostgreSQL or the Injury cache."""
    _seed_team(pg_session_factory)
    calls = []
    monkeypatch.setattr(team_summary_module, "build_injury_report", lambda *a, **k: calls.append(1))

    app = create_app(database_url=pg_migrated_database_url)
    with TestClient(app) as client:
        response = client.get(f"/v1/teams/{PG_INJURY_TEAM_ID}/injuries", headers={"Authorization": "Bearer wrong-token"})

    assert response.status_code == 401
    assert calls == []


def test_valid_requests_against_real_postgresql_remain_read_only(
    pg_migrated_database_url, pg_session_factory, auth_headers, injury_cbb_dir
):
    from sqlalchemy import text

    _seed_team(pg_session_factory)
    _write_team_injuries(injury_cbb_dir, PG_INJURY_TEAM_SLUG, [{"player": "smith", "status": "out"}])

    with pg_session_factory() as session:
        before = session.execute(text("SELECT team_id, display_name, sr_slug FROM teams")).fetchall()

    app = create_app(database_url=pg_migrated_database_url)
    with TestClient(app) as client:
        for _ in range(3):
            client.get(f"/v1/teams/{PG_INJURY_TEAM_ID}/injuries", headers=auth_headers)

    with pg_session_factory() as session:
        after = session.execute(text("SELECT team_id, display_name, sr_slug FROM teams")).fetchall()

    assert before == after


def test_sessions_close_on_success_and_failure_against_real_postgresql(
    pg_migrated_database_url, auth_headers, injury_cbb_dir
):
    """
    Seeds via a separate, test-owned, explicitly-disposed engine rather
    than the shared `pg_session_factory` fixture -- that fixture's own
    engine is disposed only at fixture teardown (after this test function
    returns), so using it here would leave a residual connection open at
    the exact moment this test checks for zero, a known gotcha already
    hit and fixed once before (Slice 3B's own `test_api_team_postgres.py`).
    """
    from sqlalchemy import create_engine as _create_engine

    from src.db.session import build_session_factory as _build_session_factory

    seed_engine = _create_engine(pg_migrated_database_url)
    try:
        with unit_of_work(_build_session_factory(seed_engine)) as session:
            TeamRepository(session).upsert(team_id=PG_INJURY_TEAM_ID, display_name="PG Injury Test Team", sr_slug=PG_INJURY_TEAM_SLUG)
    finally:
        seed_engine.dispose()

    _write_team_injuries(injury_cbb_dir, PG_INJURY_TEAM_SLUG, [])
    app = create_app(database_url=pg_migrated_database_url)

    with TestClient(app) as client:
        ok = client.get(f"/v1/teams/{PG_INJURY_TEAM_ID}/injuries", headers=auth_headers)
        not_found = client.get("/v1/teams/999999999/injuries", headers=auth_headers)
        unauthorized = client.get(f"/v1/teams/{PG_INJURY_TEAM_ID}/injuries", headers={"Authorization": "Bearer wrong-token"})
    assert ok.status_code == 200
    assert not_found.status_code == 404
    assert unauthorized.status_code == 401
    app.state.engine.dispose()

    from sqlalchemy import create_engine, text

    admin_engine = create_engine(pg_migrated_database_url)
    try:
        with admin_engine.connect() as conn:
            active = conn.execute(
                text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()")
            ).scalar()
        assert active == 0
    finally:
        admin_engine.dispose()
