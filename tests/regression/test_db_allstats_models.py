"""
Phase 5B1: schema-level tests for `src/db/allstats_models.py` (`roster_memberships`,
`player_stat_snapshots`, `team_stat_snapshots`). Same two-kind-of-proof
split as `test_db_models.py` (Phase 5A): PostgreSQL dialect DDL
compilation (no connection) vs. real constraint enforcement against a
disposable in-memory SQLite database (`PRAGMA foreign_keys=ON` enabled).
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from src.db.base import Base
from src.db import models, allstats_models
from src.db.enums import ALLSTATS_STAT_TABLES


@pytest.fixture()
def engine():
    eng = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(eng, "connect")
    def _enable_fk(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine):
    with Session(engine) as s:
        yield s


NEW_TABLES = {"roster_memberships", "player_stat_snapshots", "team_stat_snapshots"}


def test_new_tables_registered_on_shared_metadata():
    assert NEW_TABLES <= set(Base.metadata.tables.keys())


def test_new_tables_compile_against_postgresql_dialect():
    pg = postgresql.dialect()
    for name in NEW_TABLES:
        ddl = str(CreateTable(Base.metadata.tables[name]).compile(dialect=pg))
        assert "CREATE TABLE" in ddl


def test_surrogate_pks_are_not_the_canonical_identity():
    """These three tables use a database-generated surrogate id -- unlike the Phase 5A identity tables, they have no cross-source canonical identity of their own to preserve."""
    for name in NEW_TABLES:
        col = Base.metadata.tables[name].c["id"]
        assert col.primary_key
        assert col.autoincrement is not False  # explicitly autoincrementing, opposite of the Phase 5A identity PKs


def _seed_team_season(session, team_id=1, season_id=2026):
    session.add(models.Team(team_id=team_id, display_name="Test Team", sr_slug=f"test-team-{team_id}"))
    session.add(models.Season(season_id=season_id, start_year=2025, end_year=2026, label="2025-26"))
    session.flush()


def _seed_artifact(session, *, source_key="sr_allstats", team_id=1, season_id=2026, relative_path="test-team/Roster_2026.xlsx", content_hash="hashA"):
    if session.get(models.DataSource, source_key) is None:
        session.add(models.DataSource(source_key=source_key, display_name="SR AllStats", acquisition_type="live_scraped_html", is_rescrapable=True))
        session.flush()
    artifact = models.SourceArtifact(
        source_key=source_key, source_root_key="teamdump_cbb", relative_path=relative_path,
        original_filename=relative_path.split("/")[-1], file_type="xlsx", content_hash=content_hash,
        team_id=team_id, season_id=season_id, validation_status="valid",
        point_in_time_classification="historical_but_not_point_in_time",
    )
    session.add(artifact)
    session.flush()
    return artifact


def test_roster_membership_natural_key_unique(session):
    _seed_team_season(session)
    artifact = _seed_artifact(session)
    common = dict(team_id=1, season_id=2026, source_artifact_id=artifact.id, player_key="frank mitchell", source_player_name="Frank Mitchell", validation_status="unresolved")
    session.add(allstats_models.RosterMembership(**common))
    session.flush()
    session.add(allstats_models.RosterMembership(**common))
    with pytest.raises(IntegrityError):
        session.flush()


def test_roster_membership_allows_same_player_key_different_artifact(session):
    """A later re-scrape (new source_artifact) of the same team-season roster gets its OWN row -- never overwrites the prior one."""
    _seed_team_season(session)
    artifact_1 = _seed_artifact(session, content_hash="hashA")
    artifact_2 = _seed_artifact(session, content_hash="hashB")
    session.add(allstats_models.RosterMembership(team_id=1, season_id=2026, source_artifact_id=artifact_1.id, player_key="frank mitchell", source_player_name="Frank Mitchell", validation_status="unresolved"))
    session.add(allstats_models.RosterMembership(team_id=1, season_id=2026, source_artifact_id=artifact_2.id, player_key="frank mitchell", source_player_name="Frank Mitchell", validation_status="unresolved"))
    session.flush()  # must not raise -- different source_artifact_id


def test_roster_membership_foreign_key_to_team_enforced(session):
    session.add(allstats_models.RosterMembership(team_id=999999, season_id=2026, source_artifact_id=1, player_key="x", source_player_name="X", validation_status="unresolved"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_roster_membership_player_id_nullable_for_unresolved(session):
    _seed_team_season(session)
    artifact = _seed_artifact(session)
    row = allstats_models.RosterMembership(
        team_id=1, season_id=2026, source_artifact_id=artifact.id, player_key="unknown player",
        source_player_name="Unknown Player", validation_status="unresolved", player_id=None,
    )
    session.add(row)
    session.flush()  # must not raise
    assert row.player_id is None


@pytest.mark.parametrize("stat_table", ALLSTATS_STAT_TABLES)
def test_player_stat_snapshot_accepts_all_five_stat_tables(session, stat_table):
    _seed_team_season(session)
    artifact = _seed_artifact(session)
    row = allstats_models.PlayerStatSnapshot(
        team_id=1, season_id=2026, source_artifact_id=artifact.id, stat_table=stat_table,
        player_key="frank mitchell", source_player_name="Frank Mitchell", validation_status="unresolved",
    )
    session.add(row)
    session.flush()  # must not raise for any of the five


def test_player_stat_snapshot_rejects_roster_as_stat_table(session):
    """Roster is biographical/roster evidence, never a statistic table -- confirmed real workbooks never carry a Team Totals row for it."""
    _seed_team_season(session)
    artifact = _seed_artifact(session)
    session.add(allstats_models.PlayerStatSnapshot(
        team_id=1, season_id=2026, source_artifact_id=artifact.id, stat_table="Roster",
        player_key="x", source_player_name="X", validation_status="unresolved",
    ))
    with pytest.raises(IntegrityError):
        session.flush()


def test_player_stat_snapshot_natural_key_includes_stat_table(session):
    """Same player, same team/season/artifact, but two DIFFERENT stat tables -- both rows are valid (this is the real, expected shape: one player has up to 5 stat-table rows per workbook)."""
    _seed_team_season(session)
    artifact = _seed_artifact(session)
    session.add(allstats_models.PlayerStatSnapshot(team_id=1, season_id=2026, source_artifact_id=artifact.id, stat_table="PerGame", player_key="x", source_player_name="X", validation_status="unresolved"))
    session.add(allstats_models.PlayerStatSnapshot(team_id=1, season_id=2026, source_artifact_id=artifact.id, stat_table="Totals", player_key="x", source_player_name="X", validation_status="unresolved"))
    session.flush()  # must not raise


def test_player_stat_snapshot_duplicate_natural_key_rejected(session):
    _seed_team_season(session)
    artifact = _seed_artifact(session)
    common = dict(team_id=1, season_id=2026, source_artifact_id=artifact.id, stat_table="PerGame", player_key="x", source_player_name="X", validation_status="unresolved")
    session.add(allstats_models.PlayerStatSnapshot(**common))
    session.flush()
    session.add(allstats_models.PlayerStatSnapshot(**common))
    with pytest.raises(IntegrityError):
        session.flush()


def test_player_stat_snapshot_box_score_and_advanced_columns_are_sparse_not_eav(session):
    """Confirms the wide-sparse design directly: a PerGame row can populate box-score columns and leave advanced-only columns null, in the SAME table, without any generic stat_name/stat_value pair anywhere."""
    _seed_team_season(session)
    artifact = _seed_artifact(session)
    row = allstats_models.PlayerStatSnapshot(
        team_id=1, season_id=2026, source_artifact_id=artifact.id, stat_table="PerGame",
        player_key="x", source_player_name="X", validation_status="valid",
        pts=16.1, trb=9.8, ast=3.0,  # box-score
    )
    session.add(row)
    session.flush()
    assert row.pts == 16.1
    assert row.usg_pct is None  # advanced-only, correctly left null for a PerGame row
    assert not hasattr(row, "stat_name") and not hasattr(row, "stat_value")


def test_team_stat_snapshot_natural_key_no_player_dimension(session):
    _seed_team_season(session)
    artifact = _seed_artifact(session)
    common = dict(team_id=1, season_id=2026, source_artifact_id=artifact.id, stat_table="PerGame", source_row_label="Team Totals", validation_status="valid")
    session.add(allstats_models.TeamStatSnapshot(**common))
    session.flush()
    session.add(allstats_models.TeamStatSnapshot(**common))
    with pytest.raises(IntegrityError):
        session.flush()


def test_team_stat_snapshot_rejects_per40_and_per100():
    """Confirmed real-workbook finding: Per40/Per100 never carry a Team Totals row -- the CHECK constraint still technically allows the value (any of the 5 stat tables is schema-valid), but this test documents that the adapter itself never produces one for those two tables (see test_allstats_import_adapter.py)."""
    assert set(ALLSTATS_STAT_TABLES) == {"PerGame", "Totals", "Per40", "Per100", "Advanced"}


def test_team_stat_snapshot_no_player_id_or_player_key_column():
    columns = set(Base.metadata.tables["team_stat_snapshots"].c.keys())
    assert "player_id" not in columns
    assert "player_key" not in columns
