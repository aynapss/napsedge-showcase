"""
Phase 6, Slice 4A: the smallest real-PostgreSQL proof that the read-only
Player raw-statistics service (`src.services.player`) behaves correctly
against a real (not SQLite-emulated) database. Calls the service
directly -- no FastAPI application is constructed or used anywhere in
this file, since the Player service is not exposed via HTTP in this
batch. Reuses the existing disposable-PostgreSQL fixtures
(`pg_session_factory`, `pg_migrated_database_url`) -- no competing
container harness is created here, and no TeamDump/live scraping/
notebook/production data is touched.
"""
from __future__ import annotations

import pytest

from src.db.allstats_models import PlayerStatSnapshot
from src.db.allstats_repositories import PlayerStatSnapshotRepository
from src.db.repositories import DataSourceRepository, PlayerRepository, SeasonRepository, SourceArtifactRepository, TeamRepository
from src.db.session import build_session_factory, unit_of_work
from src.identity.player_identity import build_player_identity
from src.services.contracts import PlayerRawStatisticsResult
from src.services.player import get_player_raw_statistics

pytestmark = pytest.mark.postgres

TEAM_ID = 910001
SEASON_ID = 2026


def _seed_base(session_factory):
    with unit_of_work(session_factory) as session:
        TeamRepository(session).upsert(team_id=TEAM_ID, display_name="PG Test Team", sr_slug="pg-test-team-player-slice")
        SeasonRepository(session).upsert(season_id=SEASON_ID, start_year=SEASON_ID - 1, end_year=SEASON_ID, label=f"{SEASON_ID - 1}-{SEASON_ID}")
        DataSourceRepository(session).upsert(source_key="sr_allstats", display_name="SR AllStats", acquisition_type="live_scraped_html", is_rescrapable=True)


def _seed_artifact(session_factory, *, content_hash: str) -> int:
    with unit_of_work(session_factory) as session:
        artifact = SourceArtifactRepository(session).upsert(
            source_key="sr_allstats", source_root_key="teamdump_cbb",
            relative_path=f"pg-test-team/Roster_{SEASON_ID}_{content_hash}.xlsx",
            original_filename=f"Roster_{SEASON_ID}.xlsx", file_type="xlsx", content_hash=content_hash,
            team_id=TEAM_ID, season_id=SEASON_ID, validation_status="valid",
            point_in_time_classification="historical_but_not_point_in_time",
        )
        return artifact.id


def _seed_player(session_factory, display_name="Postgres Test Player"):
    identity = build_player_identity(display_name, TEAM_ID, SEASON_ID)
    with unit_of_work(session_factory) as session:
        PlayerRepository(session).upsert(player_id=identity.player_id, display_name=identity.display_name, team_id=TEAM_ID, season_id=SEASON_ID)
    return identity


def _seed_stat_row(session_factory, *, player_id, source_artifact_id, stat_table="PerGame", **stat_values):
    with unit_of_work(session_factory) as session:
        PlayerStatSnapshotRepository(session).upsert(
            team_id=TEAM_ID, season_id=SEASON_ID, source_artifact_id=source_artifact_id, stat_table=stat_table,
            player_key="postgres test player", source_player_name="Postgres Test Player", validation_status="valid",
            player_id=player_id, **stat_values,
        )


def test_actual_persisted_player_retrieval_against_real_postgresql(pg_migrated_database_url, pg_session_factory):
    _seed_base(pg_session_factory)
    identity = _seed_player(pg_session_factory)

    with pg_session_factory() as session:
        result = get_player_raw_statistics(session, identity.player_id)

    assert result.outcome == "found"
    assert result.player_id == identity.player_id
    assert result.display_name == identity.display_name
    assert result.team_id == TEAM_ID
    assert result.season_id == SEASON_ID


def test_actual_raw_statistics_retrieval_against_real_postgresql(pg_migrated_database_url, pg_session_factory):
    _seed_base(pg_session_factory)
    artifact_id = _seed_artifact(pg_session_factory, content_hash="pg-hash-a")
    identity = _seed_player(pg_session_factory)
    _seed_stat_row(pg_session_factory, player_id=identity.player_id, source_artifact_id=artifact_id, pts=16.1, trb=9.8, ast=3.0)

    with pg_session_factory() as session:
        result = get_player_raw_statistics(session, identity.player_id)

    assert len(result.stats) == 1
    assert result.stats[0].pts == 16.1
    assert result.stats[0].trb == 9.8
    assert result.stats[0].ast == 3.0


def test_multiple_records_and_deterministic_ordering_against_real_postgresql(pg_migrated_database_url, pg_session_factory):
    _seed_base(pg_session_factory)
    artifact_id = _seed_artifact(pg_session_factory, content_hash="pg-hash-b")
    identity = _seed_player(pg_session_factory)
    _seed_stat_row(pg_session_factory, player_id=identity.player_id, source_artifact_id=artifact_id, stat_table="Totals", pts=450.0)
    _seed_stat_row(pg_session_factory, player_id=identity.player_id, source_artifact_id=artifact_id, stat_table="Advanced", usg_pct=24.5)
    _seed_stat_row(pg_session_factory, player_id=identity.player_id, source_artifact_id=artifact_id, stat_table="PerGame", pts=16.1)

    with pg_session_factory() as session:
        result_a = get_player_raw_statistics(session, identity.player_id)
    with pg_session_factory() as session:
        result_b = get_player_raw_statistics(session, identity.player_id)

    order_a = [s.stat_table for s in result_a.stats]
    order_b = [s.stat_table for s in result_b.stats]
    assert order_a == order_b == ["Advanced", "PerGame", "Totals"]  # alphabetical: season_id/source_artifact_id constant here


def test_season_and_stat_table_filters_against_real_postgresql(pg_migrated_database_url, pg_session_factory):
    _seed_base(pg_session_factory)
    artifact_id = _seed_artifact(pg_session_factory, content_hash="pg-hash-c")
    identity = _seed_player(pg_session_factory)
    _seed_stat_row(pg_session_factory, player_id=identity.player_id, source_artifact_id=artifact_id, stat_table="PerGame", pts=16.1)
    _seed_stat_row(pg_session_factory, player_id=identity.player_id, source_artifact_id=artifact_id, stat_table="Advanced", usg_pct=24.5)

    with pg_session_factory() as session:
        by_season = get_player_raw_statistics(session, identity.player_id, season_id=SEASON_ID)
        by_wrong_season = get_player_raw_statistics(session, identity.player_id, season_id=SEASON_ID - 1)
        by_stat_table = get_player_raw_statistics(session, identity.player_id, stat_table="Advanced")

    assert len(by_season.stats) == 2
    assert by_wrong_season.stats == ()
    assert by_wrong_season.outcome == "found"  # known player, just no rows for that season
    assert len(by_stat_table.stats) == 1
    assert by_stat_table.stats[0].stat_table == "Advanced"


def test_unknown_player_against_real_postgresql(pg_migrated_database_url, pg_session_factory):
    with pg_session_factory() as session:
        result = get_player_raw_statistics(session, "team:999999:season:2026:key:nobody-real")

    assert result == PlayerRawStatisticsResult(outcome="not_found", detail="no database row for this player_id")


def test_known_player_without_qualifying_statistics_against_real_postgresql(pg_migrated_database_url, pg_session_factory):
    _seed_base(pg_session_factory)
    identity = _seed_player(pg_session_factory, display_name="No Stats Player")

    with pg_session_factory() as session:
        result = get_player_raw_statistics(session, identity.player_id)

    assert result.outcome == "found"
    assert result.stats == ()


def test_null_value_preservation_against_real_postgresql(pg_migrated_database_url, pg_session_factory):
    _seed_base(pg_session_factory)
    artifact_id = _seed_artifact(pg_session_factory, content_hash="pg-hash-d")
    identity = _seed_player(pg_session_factory, display_name="Null Fields Player")
    _seed_stat_row(pg_session_factory, player_id=identity.player_id, source_artifact_id=artifact_id, stat_table="PerGame", pts=16.1)

    with pg_session_factory() as session:
        result = get_player_raw_statistics(session, identity.player_id)

    record = result.stats[0]
    assert record.usg_pct is None
    assert record.ows is None
    assert record.awards_raw is None


def test_read_only_behavior_against_real_postgresql(pg_migrated_database_url, pg_session_factory):
    from sqlalchemy import text

    _seed_base(pg_session_factory)
    artifact_id = _seed_artifact(pg_session_factory, content_hash="pg-hash-e")
    identity = _seed_player(pg_session_factory, display_name="Read Only Player")
    _seed_stat_row(pg_session_factory, player_id=identity.player_id, source_artifact_id=artifact_id, pts=16.1)

    with pg_session_factory() as session:
        before = session.execute(
            text("SELECT id, player_id, pts FROM player_stat_snapshots WHERE player_id = :pid"), {"pid": identity.player_id}
        ).fetchall()

    for _ in range(3):
        with pg_session_factory() as session:
            get_player_raw_statistics(session, identity.player_id)
            get_player_raw_statistics(session, "some-unknown-player-id")

    with pg_session_factory() as session:
        after = session.execute(
            text("SELECT id, player_id, pts FROM player_stat_snapshots WHERE player_id = :pid"), {"pid": identity.player_id}
        ).fetchall()

    assert before == after


def test_session_resource_cleanup_after_direct_service_use_against_real_postgresql(pg_migrated_database_url):
    """
    Seeds and reads via a short-lived engine that this test disposes
    itself (rather than the long-lived `pg_session_factory` fixture,
    whose own engine is only disposed at fixture teardown) so the only
    engine that could hold a connection at assertion time is the one
    this test built and disposed.
    """
    from sqlalchemy import create_engine, text

    seed_engine = create_engine(pg_migrated_database_url)
    try:
        seed_session_factory = build_session_factory(seed_engine)
        _seed_base(seed_session_factory)
        identity = _seed_player(seed_session_factory, display_name="Cleanup Player")
        with seed_session_factory() as session:
            result = get_player_raw_statistics(session, identity.player_id)
        assert result.outcome == "found"
    finally:
        seed_engine.dispose()

    admin_engine = create_engine(pg_migrated_database_url)
    try:
        with admin_engine.connect() as conn:
            active = conn.execute(
                text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()")
            ).scalar()
        assert active == 0
    finally:
        admin_engine.dispose()
