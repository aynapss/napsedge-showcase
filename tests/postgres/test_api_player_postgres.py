"""
Phase 6, Slice 4B: the smallest real-PostgreSQL proof of the
HTTP-to-service-to-database boundary for the Bearer-authenticated
`GET /v1/players/{player_id}/raw-statistics` endpoint. Reuses the
existing disposable-PostgreSQL fixtures (`pg_session_factory`,
`pg_migrated_database_url`) -- no competing container harness is created
here. Does not duplicate the service-level PostgreSQL proof already in
tests/postgres/test_services_player_postgres.py -- this file proves the
route/auth/serialization layer specifically.
"""
from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from src.api.main import create_app
from src.auth.api_keys import ENV_VAR as AUTH_ENV_VAR
from src.db.allstats_repositories import PlayerStatSnapshotRepository
from src.db.repositories import DataSourceRepository, PlayerRepository, SeasonRepository, SourceArtifactRepository, TeamRepository
from src.db.session import unit_of_work
from src.identity.player_identity import build_player_identity

pytestmark = pytest.mark.postgres

TEAM_ID = 940001
SEASON_ID = 2026
VALID_TOKEN = "test-suite-player-api-token-" + "c" * 16
VALID_CLIENT_ID = "test-suite-player-api-client"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@pytest.fixture()
def auth_headers(monkeypatch) -> dict[str, str]:
    monkeypatch.setenv(AUTH_ENV_VAR, json.dumps({VALID_CLIENT_ID: _digest(VALID_TOKEN)}))
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


def _seed(pg_session_factory, *, content_hash: str, display_name: str = "PG API Player"):
    identity = build_player_identity(display_name, TEAM_ID, SEASON_ID)
    with unit_of_work(pg_session_factory) as session:
        TeamRepository(session).upsert(team_id=TEAM_ID, display_name="PG API Team", sr_slug=f"pg-api-team-{content_hash}")
        SeasonRepository(session).upsert(season_id=SEASON_ID, start_year=SEASON_ID - 1, end_year=SEASON_ID, label=f"{SEASON_ID - 1}-{SEASON_ID}")
        DataSourceRepository(session).upsert(source_key="sr_allstats", display_name="SR AllStats", acquisition_type="live_scraped_html", is_rescrapable=True)
        artifact = SourceArtifactRepository(session).upsert(
            source_key="sr_allstats", source_root_key="teamdump_cbb", relative_path=f"pg-api-team/Roster_{content_hash}.xlsx",
            original_filename="Roster_2026.xlsx", file_type="xlsx", content_hash=content_hash,
            team_id=TEAM_ID, season_id=SEASON_ID, validation_status="valid",
            point_in_time_classification="historical_but_not_point_in_time",
        )
        PlayerRepository(session).upsert(player_id=identity.player_id, display_name=identity.display_name, team_id=TEAM_ID, season_id=SEASON_ID)
        return identity, artifact.id


def test_authenticated_retrieval_from_migrated_postgresql(pg_migrated_database_url, pg_session_factory, auth_headers):
    identity, artifact_id = _seed(pg_session_factory, content_hash="api-pg-1")
    with unit_of_work(pg_session_factory) as session:
        PlayerStatSnapshotRepository(session).upsert(
            team_id=TEAM_ID, season_id=SEASON_ID, source_artifact_id=artifact_id, stat_table="PerGame",
            player_key="pg api player", source_player_name="PG API Player", validation_status="valid",
            player_id=identity.player_id, pts=16.1,
        )

    app = create_app(database_url=pg_migrated_database_url)
    with TestClient(app) as client:
        response = client.get(f"/v1/players/{identity.player_id}/raw-statistics", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["player_id"] == identity.player_id
    assert body["stats"][0]["pts"] == 16.1


def test_multiple_records_and_deterministic_ordering_against_real_postgresql(pg_migrated_database_url, pg_session_factory, auth_headers):
    identity, artifact_id = _seed(pg_session_factory, content_hash="api-pg-2")
    with unit_of_work(pg_session_factory) as session:
        repo = PlayerStatSnapshotRepository(session)
        repo.upsert(team_id=TEAM_ID, season_id=SEASON_ID, source_artifact_id=artifact_id, stat_table="Totals", player_key="pg api player", source_player_name="PG API Player", validation_status="valid", player_id=identity.player_id)
        repo.upsert(team_id=TEAM_ID, season_id=SEASON_ID, source_artifact_id=artifact_id, stat_table="Advanced", player_key="pg api player", source_player_name="PG API Player", validation_status="valid", player_id=identity.player_id)
        repo.upsert(team_id=TEAM_ID, season_id=SEASON_ID, source_artifact_id=artifact_id, stat_table="PerGame", player_key="pg api player", source_player_name="PG API Player", validation_status="valid", player_id=identity.player_id)

    app = create_app(database_url=pg_migrated_database_url)
    with TestClient(app) as client:
        response = client.get(f"/v1/players/{identity.player_id}/raw-statistics", headers=auth_headers)

    assert [s["stat_table"] for s in response.json()["stats"]] == ["Advanced", "PerGame", "Totals"]


def test_season_id_filtering_against_real_postgresql(pg_migrated_database_url, pg_session_factory, auth_headers):
    identity, artifact_id = _seed(pg_session_factory, content_hash="api-pg-3")
    with unit_of_work(pg_session_factory) as session:
        PlayerStatSnapshotRepository(session).upsert(
            team_id=TEAM_ID, season_id=SEASON_ID, source_artifact_id=artifact_id, stat_table="PerGame",
            player_key="pg api player", source_player_name="PG API Player", validation_status="valid",
            player_id=identity.player_id, pts=16.1,
        )

    app = create_app(database_url=pg_migrated_database_url)
    with TestClient(app) as client:
        matching = client.get(f"/v1/players/{identity.player_id}/raw-statistics", params={"season_id": SEASON_ID}, headers=auth_headers)
        nonmatching = client.get(f"/v1/players/{identity.player_id}/raw-statistics", params={"season_id": SEASON_ID - 1}, headers=auth_headers)

    assert len(matching.json()["stats"]) == 1
    assert nonmatching.status_code == 200
    assert nonmatching.json()["stats"] == []


def test_stat_table_filtering_against_real_postgresql(pg_migrated_database_url, pg_session_factory, auth_headers):
    identity, artifact_id = _seed(pg_session_factory, content_hash="api-pg-4")
    with unit_of_work(pg_session_factory) as session:
        repo = PlayerStatSnapshotRepository(session)
        repo.upsert(team_id=TEAM_ID, season_id=SEASON_ID, source_artifact_id=artifact_id, stat_table="PerGame", player_key="pg api player", source_player_name="PG API Player", validation_status="valid", player_id=identity.player_id, pts=16.1)
        repo.upsert(team_id=TEAM_ID, season_id=SEASON_ID, source_artifact_id=artifact_id, stat_table="Advanced", player_key="pg api player", source_player_name="PG API Player", validation_status="valid", player_id=identity.player_id, usg_pct=24.5)

    app = create_app(database_url=pg_migrated_database_url)
    with TestClient(app) as client:
        response = client.get(f"/v1/players/{identity.player_id}/raw-statistics", params={"stat_table": "Advanced"}, headers=auth_headers)

    stats = response.json()["stats"]
    assert len(stats) == 1
    assert stats[0]["stat_table"] == "Advanced"
    assert stats[0]["usg_pct"] == 24.5


def test_known_player_empty_filtered_result_against_real_postgresql(pg_migrated_database_url, pg_session_factory, auth_headers):
    identity, _ = _seed(pg_session_factory, content_hash="api-pg-5")

    app = create_app(database_url=pg_migrated_database_url)
    with TestClient(app) as client:
        response = client.get(f"/v1/players/{identity.player_id}/raw-statistics", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["stats"] == []


def test_unknown_player_against_real_postgresql(pg_migrated_database_url, auth_headers):
    app = create_app(database_url=pg_migrated_database_url)
    with TestClient(app) as client:
        response = client.get("/v1/players/nobody-real/raw-statistics", headers=auth_headers)
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_null_and_zero_preservation_against_real_postgresql(pg_migrated_database_url, pg_session_factory, auth_headers):
    identity, artifact_id = _seed(pg_session_factory, content_hash="api-pg-6")
    with unit_of_work(pg_session_factory) as session:
        PlayerStatSnapshotRepository(session).upsert(
            team_id=TEAM_ID, season_id=SEASON_ID, source_artifact_id=artifact_id, stat_table="PerGame",
            player_key="pg api player", source_player_name="PG API Player", validation_status="valid",
            player_id=identity.player_id, stl=0.0, blk=0.0,
        )

    app = create_app(database_url=pg_migrated_database_url)
    with TestClient(app) as client:
        response = client.get(f"/v1/players/{identity.player_id}/raw-statistics", headers=auth_headers)

    record = response.json()["stats"][0]
    assert record["stl"] == 0
    assert record["blk"] == 0
    assert record["usg_pct"] is None
    assert record["awards_raw"] is None


def test_validation_status_exposed_unchanged_for_non_valid_records_against_real_postgresql(pg_migrated_database_url, pg_session_factory, auth_headers):
    """
    Slice 4B correction: validation_status must reach the client exactly
    as stored, including non-"valid" values -- PlayerStatSnapshotRepository.list_for_player
    enforces no invariant restricting results to valid rows.
    """
    identity, artifact_id = _seed(pg_session_factory, content_hash="api-pg-9")
    with unit_of_work(pg_session_factory) as session:
        repo = PlayerStatSnapshotRepository(session)
        repo.upsert(team_id=TEAM_ID, season_id=SEASON_ID, source_artifact_id=artifact_id, stat_table="PerGame", player_key="pg api player", source_player_name="PG API Player", validation_status="partial", player_id=identity.player_id)
        repo.upsert(team_id=TEAM_ID, season_id=SEASON_ID, source_artifact_id=artifact_id, stat_table="Advanced", player_key="pg api player", source_player_name="PG API Player", validation_status="conflicting", player_id=identity.player_id)

    app = create_app(database_url=pg_migrated_database_url)
    with TestClient(app) as client:
        response = client.get(f"/v1/players/{identity.player_id}/raw-statistics", headers=auth_headers)

    statuses = {s["stat_table"]: s["validation_status"] for s in response.json()["stats"]}
    assert statuses == {"PerGame": "partial", "Advanced": "conflicting"}


def test_authentication_failure_prevents_database_access_against_real_postgresql(pg_migrated_database_url, pg_session_factory, auth_headers):
    """auth_headers establishes valid server-side configuration; the request itself still sends a wrong token, so this must be a true 401, not a 503 (unconfigured)."""
    identity, artifact_id = _seed(pg_session_factory, content_hash="api-pg-7")
    with unit_of_work(pg_session_factory) as session:
        PlayerStatSnapshotRepository(session).upsert(
            team_id=TEAM_ID, season_id=SEASON_ID, source_artifact_id=artifact_id, stat_table="PerGame",
            player_key="pg api player", source_player_name="PG API Player", validation_status="valid",
            player_id=identity.player_id, pts=16.1,
        )

    app = create_app(database_url=pg_migrated_database_url)
    with TestClient(app) as client:
        response = client.get(f"/v1/players/{identity.player_id}/raw-statistics", headers={"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401


def test_read_only_behavior_against_real_postgresql(pg_migrated_database_url, pg_session_factory, auth_headers):
    from sqlalchemy import text

    identity, artifact_id = _seed(pg_session_factory, content_hash="api-pg-8")
    with unit_of_work(pg_session_factory) as session:
        PlayerStatSnapshotRepository(session).upsert(
            team_id=TEAM_ID, season_id=SEASON_ID, source_artifact_id=artifact_id, stat_table="PerGame",
            player_key="pg api player", source_player_name="PG API Player", validation_status="valid",
            player_id=identity.player_id, pts=16.1,
        )

    with pg_session_factory() as session:
        before = session.execute(text("SELECT id, player_id, pts FROM player_stat_snapshots WHERE player_id = :pid"), {"pid": identity.player_id}).fetchall()

    app = create_app(database_url=pg_migrated_database_url)
    with TestClient(app) as client:
        for _ in range(3):
            client.get(f"/v1/players/{identity.player_id}/raw-statistics", headers=auth_headers)

    with pg_session_factory() as session:
        after = session.execute(text("SELECT id, player_id, pts FROM player_stat_snapshots WHERE player_id = :pid"), {"pid": identity.player_id}).fetchall()

    assert before == after


def test_session_cleanup_on_success_and_failure_against_real_postgresql(pg_migrated_database_url):
    """
    Seeds via a short-lived engine that this test disposes itself (rather
    than the long-lived `pg_session_factory` fixture, whose own engine is
    only disposed at fixture teardown) so the only engine that could hold
    a connection at assertion time is the one created inside `create_app()`.
    """
    import hashlib as _hashlib
    import json as _json

    from sqlalchemy import create_engine, text

    from src.auth.api_keys import ENV_VAR
    from src.db.session import build_session_factory

    token = "session-cleanup-token-" + "d" * 16
    import os

    os.environ[ENV_VAR] = _json.dumps({"cleanup-client": _hashlib.sha256(token.encode()).hexdigest()})
    try:
        seed_engine = create_engine(pg_migrated_database_url)
        try:
            seed_session_factory = build_session_factory(seed_engine)
            identity = build_player_identity("Cleanup Player", TEAM_ID, SEASON_ID)
            with unit_of_work(seed_session_factory) as session:
                TeamRepository(session).upsert(team_id=TEAM_ID, display_name="Cleanup Team", sr_slug="cleanup-team-api")
                SeasonRepository(session).upsert(season_id=SEASON_ID, start_year=SEASON_ID - 1, end_year=SEASON_ID, label=f"{SEASON_ID - 1}-{SEASON_ID}")
                PlayerRepository(session).upsert(player_id=identity.player_id, display_name=identity.display_name, team_id=TEAM_ID, season_id=SEASON_ID)
        finally:
            seed_engine.dispose()

        app = create_app(database_url=pg_migrated_database_url)
        with TestClient(app) as client:
            ok = client.get(f"/v1/players/{identity.player_id}/raw-statistics", headers={"Authorization": f"Bearer {token}"})
            not_found = client.get("/v1/players/nobody-real/raw-statistics", headers={"Authorization": f"Bearer {token}"})
            unauthorized = client.get(f"/v1/players/{identity.player_id}/raw-statistics", headers={"Authorization": "Bearer wrong"})
        assert ok.status_code == 200
        assert not_found.status_code == 404
        assert unauthorized.status_code == 401
        app.state.engine.dispose()

        admin_engine = create_engine(pg_migrated_database_url)
        try:
            with admin_engine.connect() as conn:
                active = conn.execute(
                    text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()")
                ).scalar()
            assert active == 0
        finally:
            admin_engine.dispose()
    finally:
        os.environ.pop(ENV_VAR, None)
