"""
Controlled bulk-import design, Slice 3 (docs/controlled_bulk_import_design.md
sections 8/13/16, approved for this slice only): PostgreSQL-specific proof
for `src/pipeline/bulk_import_reconciliation.py` -- everything a real
PostgreSQL backend can prove that SQLite cannot (strict UTC-tzinfo
round-trip, real transaction/visibility semantics, and a live SQL-statement
capture proving the reader never issues anything but SELECT).

Reuses the exact disposable-container fixtures from tests/postgres/conftest.py
(pg_container / pg_session_factory) -- no new container logic, no new
safety guard. Synthetic records only, never TeamDump, never a real adapter.
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import event, text

from src.db import game_log_models
from src.db.allstats_repositories import RosterMembershipRepository
from src.db.game_log_repositories import GameResultRepository, TeamGameLogObservationRepository
from src.db.repositories import (
    ConferenceRepository,
    DataSourceRepository,
    GameRepository,
    PipelineRunRepository,
    PlayerRepository,
    SeasonRepository,
    SourceArtifactRepository,
    TeamRepository,
)
from src.db.session import build_session_factory, unit_of_work
from src.exceptions import ValidationError
from src.identity.game_identity import build_provisional_game_identity
from src.pipeline.bulk_import_reconciliation import read_pipeline_run_reconciliation, read_source_artifact_reconciliation

pytestmark = pytest.mark.postgres

UTC = dt.timezone.utc


def _seed_reference(session_factory):
    with unit_of_work(session_factory) as session:
        ConferenceRepository(session).upsert(conference_id=1, canonical_name="Atlantic 10")
        TeamRepository(session).upsert(team_id=1, display_name="St. Bonaventure", sr_slug="st-bonaventure", conference_id=1)
        TeamRepository(session).upsert(team_id=2, display_name="Bradley", sr_slug="bradley")
        SeasonRepository(session).upsert(season_id=2026, start_year=2025, end_year=2026, label="2025-26")
        for source_key in ("sports_reference_allstats", "sports_reference_game_log"):
            DataSourceRepository(session).upsert(source_key=source_key, display_name=source_key, acquisition_type="static_file", is_rescrapable=False)
        PipelineRunRepository(session).start(run_id="run1", pipeline_name="bulk_import_test", started_at=dt.datetime(2026, 1, 1, tzinfo=UTC))


def test_strict_utc_tzinfo_round_trips_through_real_postgresql(pg_session_factory):
    """The one proof SQLite structurally cannot make -- a real TIMESTAMP WITH TIME ZONE column preserves tzinfo exactly, not just the naive wall-clock value."""
    _seed_reference(pg_session_factory)
    collected = dt.datetime(2026, 1, 2, 12, 0, 0, tzinfo=UTC)
    fs_mtime = dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    with unit_of_work(pg_session_factory) as session:
        artifact = SourceArtifactRepository(session).upsert(
            source_key="sports_reference_allstats", source_root_key="teamdump_cbb", relative_path="st-bonaventure/Roster_2026.xlsx",
            original_filename="Roster_2026.xlsx", file_type="xlsx", content_hash="h1", team_id=1, season_id=2026,
            filesystem_mtime=fs_mtime, collection_timestamp=collected,
            validation_status="valid", point_in_time_classification="historical_but_not_point_in_time", pipeline_run_id="run1",
        )
        artifact_id = artifact.id
    with unit_of_work(pg_session_factory) as session:
        result = read_source_artifact_reconciliation(session, artifact_id)
    assert result.collection_timestamp.tzinfo is not None
    assert result.filesystem_mtime.tzinfo is not None
    assert result.collection_timestamp == collected
    assert result.filesystem_mtime == fs_mtime


def test_wide_63_bit_game_id_reconciliation_against_real_postgresql(pg_session_factory):
    _seed_reference(pg_session_factory)
    gid = build_provisional_game_identity(home_team_id=1, away_team_id=2, season=2026, game_date="2025-11-03", neutral_site=True).game_id
    assert gid.bit_length() <= 63 and gid > 2**31
    with unit_of_work(pg_session_factory) as session:
        artifact = SourceArtifactRepository(session).upsert(
            source_key="sports_reference_game_log", source_root_key="teamdump_cbb", relative_path="st-bonaventure/x.csv",
            original_filename="x.csv", file_type="csv", content_hash="h2", team_id=1, season_id=2026,
            validation_status="valid", point_in_time_classification="historical_but_not_point_in_time", pipeline_run_id="run1",
        )
        GameRepository(session).upsert(game_id=gid, home_team_id=1, away_team_id=2, season_id=2026, game_date="2025-11-03", neutral_site=True, is_provisional=True)
        TeamGameLogObservationRepository(session).upsert(team_id=1, season_id=2026, source_artifact_id=artifact.id, source_row_position=0, raw_opponent_name="Bradley", opponent_team_id=2, game_id=gid, venue_status="home", schema_variant="basic", validation_status="valid", pipeline_run_id="run1")
        GameResultRepository(session).upsert(game_id=gid, season_id=2026, team_a_id=1, team_b_id=2, result_status="single_sided_provisional", validation_status="valid", source_count=1, pipeline_run_id="run1")

    with unit_of_work(pg_session_factory) as session:
        report = read_pipeline_run_reconciliation(session, "run1")
    team_game_log = next(f for f in report.families if f.family == "team_game_log")
    assert team_game_log.game_results.persisted_rows == 1
    assert team_game_log.tables[0].persisted_rows == 1


def test_reader_sees_callers_uncommitted_work_same_session(pg_session_factory):
    _seed_reference(pg_session_factory)
    session = pg_session_factory()
    try:
        artifact = SourceArtifactRepository(session).upsert(
            source_key="sports_reference_allstats", source_root_key="teamdump_cbb", relative_path="st-bonaventure/Roster_2026.xlsx",
            original_filename="Roster_2026.xlsx", file_type="xlsx", content_hash="h1", team_id=1, season_id=2026,
            validation_status="valid", point_in_time_classification="historical_but_not_point_in_time", pipeline_run_id="run1",
        )
        RosterMembershipRepository(session).upsert(team_id=1, season_id=2026, source_artifact_id=artifact.id, player_key="frank mitchell", source_player_name="Frank Mitchell", validation_status="unresolved")
        # No commit.
        report = read_pipeline_run_reconciliation(session, "run1")
        allstats = next(f for f in report.families if f.family == "allstats")
        assert sum(t.persisted_rows for t in allstats.tables) == 1
    finally:
        session.rollback()
        session.close()


def test_reader_never_commits_uncommitted_work_becomes_invisible_after_rollback(pg_session_factory):
    """Confirms the reader itself never commits -- an explicit caller rollback after reading must discard the work the reader just reported on."""
    _seed_reference(pg_session_factory)
    engine = pg_session_factory.kw["bind"]
    session = pg_session_factory()
    try:
        artifact = SourceArtifactRepository(session).upsert(
            source_key="sports_reference_allstats", source_root_key="teamdump_cbb", relative_path="st-bonaventure/Roster_2026.xlsx",
            original_filename="Roster_2026.xlsx", file_type="xlsx", content_hash="h1", team_id=1, season_id=2026,
            validation_status="valid", point_in_time_classification="historical_but_not_point_in_time", pipeline_run_id="run1",
        )
        RosterMembershipRepository(session).upsert(team_id=1, season_id=2026, source_artifact_id=artifact.id, player_key="frank mitchell", source_player_name="Frank Mitchell", validation_status="unresolved")
        read_pipeline_run_reconciliation(session, "run1")  # the read itself must not commit this work
        session.rollback()
    finally:
        session.close()

    with engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM roster_memberships")).scalar()
    assert count == 0  # confirms the reader's own read never committed the caller's uncommitted insert


def test_sql_statement_capture_proves_only_select_issued(pg_session_factory):
    """Live SQL-statement capture (not just AST inspection) against a real PostgreSQL connection -- every statement the reader itself issues during one full report must be a SELECT."""
    _seed_reference(pg_session_factory)
    with unit_of_work(pg_session_factory) as session:
        artifact = SourceArtifactRepository(session).upsert(
            source_key="sports_reference_allstats", source_root_key="teamdump_cbb", relative_path="st-bonaventure/Roster_2026.xlsx",
            original_filename="Roster_2026.xlsx", file_type="xlsx", content_hash="h1", team_id=1, season_id=2026,
            validation_status="valid", point_in_time_classification="historical_but_not_point_in_time", pipeline_run_id="run1",
        )
        RosterMembershipRepository(session).upsert(team_id=1, season_id=2026, source_artifact_id=artifact.id, player_key="frank mitchell", source_player_name="Frank Mitchell", validation_status="unresolved")

    engine = pg_session_factory.kw["bind"]
    captured: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        captured.append(statement.strip().split()[0].upper())

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        with unit_of_work(pg_session_factory) as session:
            read_pipeline_run_reconciliation(session, "run1")
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert captured, "expected at least one statement to be captured"
    assert set(captured) <= {"SELECT"}


def test_missing_run_raises_against_real_postgresql(pg_session_factory):
    _seed_reference(pg_session_factory)
    with unit_of_work(pg_session_factory) as session:
        with pytest.raises(ValidationError):
            read_pipeline_run_reconciliation(session, "does-not-exist")


def test_repeated_reads_return_equivalent_results_against_real_postgresql(pg_session_factory):
    _seed_reference(pg_session_factory)
    with unit_of_work(pg_session_factory) as session:
        artifact = SourceArtifactRepository(session).upsert(
            source_key="sports_reference_allstats", source_root_key="teamdump_cbb", relative_path="st-bonaventure/Roster_2026.xlsx",
            original_filename="Roster_2026.xlsx", file_type="xlsx", content_hash="h1", team_id=1, season_id=2026,
            validation_status="valid", point_in_time_classification="historical_but_not_point_in_time", pipeline_run_id="run1",
        )
        RosterMembershipRepository(session).upsert(team_id=1, season_id=2026, source_artifact_id=artifact.id, player_key="frank mitchell", source_player_name="Frank Mitchell", validation_status="unresolved")

    with unit_of_work(pg_session_factory) as session:
        first = read_pipeline_run_reconciliation(session, "run1")
        second = read_pipeline_run_reconciliation(session, "run1")
    assert first == second
