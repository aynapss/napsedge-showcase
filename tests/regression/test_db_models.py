"""
Phase 5A: schema-level tests for `src/db/models.py`.

Two distinct kinds of proof, deliberately kept separate (Phase 5A
instruction: "do not claim live PostgreSQL integration parity from
SQLite-only execution"):

1. DDL COMPILATION against the real PostgreSQL dialect -- proves the
   schema is valid PostgreSQL syntax (types, constraints), without ever
   opening a connection (no PostgreSQL server exists in this environment).
2. CONSTRAINT ENFORCEMENT against a real, disposable, in-memory SQLite
   database -- proves primary keys, foreign keys, unique constraints, and
   check constraints actually reject bad data, using a real engine and
   real `IntegrityError`s (not just declared-and-trusted). SQLite's own
   dialect differences from PostgreSQL (no native ENUM, weaker type
   coercion) do not weaken any of the specific constraints this batch
   relies on: PK/FK/UNIQUE/CHECK all execute identically in effect on
   both engines for this schema.

Neither connects to a production database.
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
from src.db import models
from src.db.enums import POINT_IN_TIME_CLASSIFICATIONS


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


EXPECTED_TABLES = {
    "conferences", "teams", "team_aliases", "players", "player_aliases",
    "seasons", "games", "sportsbooks", "model_versions", "data_sources",
    "source_artifacts", "pipeline_runs", "data_quality_issues",
}


def test_all_thirteen_phase_5a_foundation_tables_exist():
    """Phase 5A's own 13 tables must always be present, regardless of what later batches (Phase 5B1's AllStats source-evidence tables, imported by other test modules onto this same shared Base.metadata) additively register alongside them. See test_db_migrations.py's EXPECTED_TABLES_PHASE_5A / EXPECTED_TABLES_AT_HEAD for the exact, named milestone boundary this project tracks as the schema grows."""
    assert EXPECTED_TABLES <= set(Base.metadata.tables.keys())


def test_all_tables_compile_against_postgresql_dialect():
    pg_dialect = postgresql.dialect()
    for table in Base.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=pg_dialect))
        assert "CREATE TABLE" in ddl
        assert table.name in ddl


def test_identity_primary_keys_are_not_autoincrement():
    """The Phase 5A instruction 'never create a second team/player/season/game/sportsbook/model-version identifier' means these PKs must be caller-supplied, not database-generated."""
    for table_name, pk_column in [
        ("conferences", "conference_id"), ("teams", "team_id"), ("seasons", "season_id"),
        ("games", "game_id"), ("sportsbooks", "sportsbook_id"), ("model_versions", "model_version_id"),
    ]:
        column = Base.metadata.tables[table_name].c[pk_column]
        assert column.autoincrement is False, f"{table_name}.{pk_column} must not autoincrement"


def test_player_primary_key_is_caller_supplied_string():
    column = Base.metadata.tables["players"].c["player_id"]
    assert column.primary_key
    assert not column.autoincrement or str(column.type).startswith("VARCHAR")


def _season(session):
    session.add(models.Season(season_id=2026, start_year=2025, end_year=2026, label="2025-26"))
    session.flush()


def _team(session, team_id=111111, sr_slug="test-team", conference_id=None):
    session.add(models.Team(team_id=team_id, display_name="Test Team", sr_slug=sr_slug, conference_id=conference_id))
    session.flush()


def test_team_unique_sr_slug_enforced(session):
    _team(session, team_id=1, sr_slug="dup-slug")
    session.add(models.Team(team_id=2, display_name="Other Team", sr_slug="dup-slug"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_team_alias_unique_source_raw_value_enforced(session):
    _team(session, team_id=1, sr_slug="team-a")
    _team(session, team_id=2, sr_slug="team-b")
    session.add(models.TeamAlias(team_id=1, source="espn", raw_value="TA", normalized_value="ta"))
    session.flush()
    session.add(models.TeamAlias(team_id=2, source="espn", raw_value="TA", normalized_value="ta"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_team_alias_same_raw_value_different_source_allowed(session):
    """Alias uniqueness is scoped by source -- the same raw_value under two different sources is not a collision."""
    _team(session, team_id=1, sr_slug="team-a")
    session.add(models.TeamAlias(team_id=1, source="espn", raw_value="TA", normalized_value="ta"))
    session.add(models.TeamAlias(team_id=1, source="covers", raw_value="TA", normalized_value="ta"))
    session.flush()  # must not raise


def test_team_foreign_key_to_conference_enforced(session):
    session.add(models.Team(team_id=1, display_name="X", sr_slug="x", conference_id=999999))
    with pytest.raises(IntegrityError):
        session.flush()


def test_game_home_away_distinct_check_constraint(session):
    _season(session)
    _team(session, team_id=1, sr_slug="a")
    session.add(models.Game(
        game_id=1, home_team_id=1, away_team_id=1, season_id=2026,
        game_date=dt.date(2026, 1, 1), neutral_site=False, is_provisional=True,
    ))
    with pytest.raises(IntegrityError):
        session.flush()


def test_game_foreign_key_to_team_enforced(session):
    _season(session)
    _team(session, team_id=1, sr_slug="a")
    session.add(models.Game(
        game_id=1, home_team_id=1, away_team_id=999999, season_id=2026,
        game_date=dt.date(2026, 1, 1), neutral_site=False, is_provisional=True,
    ))
    with pytest.raises(IntegrityError):
        session.flush()


def test_game_provisional_and_confirmed_rows_are_distinguishable(session):
    _season(session)
    _team(session, team_id=1, sr_slug="a")
    _team(session, team_id=2, sr_slug="b")
    provisional = models.Game(
        game_id=100, home_team_id=1, away_team_id=2, season_id=2026,
        game_date=dt.date(2026, 1, 1), neutral_site=False, is_provisional=True,
    )
    confirmed = models.Game(
        game_id=200, home_team_id=1, away_team_id=2, season_id=2026,
        game_date=dt.date(2026, 1, 1), neutral_site=False, is_provisional=False,
        source="espn", source_game_id="evt-123",
    )
    session.add_all([provisional, confirmed])
    session.flush()
    assert provisional.is_provisional is True
    assert confirmed.is_provisional is False
    assert provisional.game_id != confirmed.game_id


@pytest.mark.parametrize("classification", POINT_IN_TIME_CLASSIFICATIONS)
def test_all_six_point_in_time_classifications_are_accepted(session, classification):
    session.add(models.DataSource(source_key="src", display_name="Src", acquisition_type="static_file", is_rescrapable=False))
    session.flush()
    row = models.SourceArtifact(
        source_key="src", source_root_key="teamdump_cbb", relative_path=f"x/{classification}.csv",
        original_filename=f"{classification}.csv", file_type="csv",
        validation_status="pending", point_in_time_classification=classification,
    )
    session.add(row)
    session.flush()  # must not raise for any of the six


def test_invalid_point_in_time_classification_rejected(session):
    session.add(models.DataSource(source_key="src", display_name="Src", acquisition_type="static_file", is_rescrapable=False))
    session.flush()
    session.add(models.SourceArtifact(
        source_key="src", source_root_key="teamdump_cbb", relative_path="x.csv",
        original_filename="x.csv", file_type="csv",
        validation_status="pending", point_in_time_classification="not_a_real_value",
    ))
    with pytest.raises(IntegrityError):
        session.flush()


def test_invalid_validation_status_rejected(session):
    session.add(models.DataSource(source_key="src", display_name="Src", acquisition_type="static_file", is_rescrapable=False))
    session.flush()
    session.add(models.SourceArtifact(
        source_key="src", source_root_key="teamdump_cbb", relative_path="x.csv",
        original_filename="x.csv", file_type="csv",
        validation_status="not_a_real_status", point_in_time_classification="current_snapshot_only",
    ))
    with pytest.raises(IntegrityError):
        session.flush()


def test_torvik_data_source_registers_as_live_scraped_json(session):
    """Confirmed source classification (this batch): Torvik's real acquisition mechanism is a live-scraped JSON response containing AdjOE/AdjDE and other live metrics -- rescrapable, but each scrape is its own immutable snapshot (see SourceArtifact tests)."""
    row = models.DataSource(source_key="torvik", display_name="Torvik ratings", acquisition_type="live_scraped_json", is_rescrapable=True)
    session.add(row)
    session.flush()
    assert row.acquisition_type == "live_scraped_json"
    assert row.is_rescrapable is True


def test_invalid_acquisition_type_rejected(session):
    session.add(models.DataSource(source_key="bad", display_name="Bad", acquisition_type="not_a_real_type", is_rescrapable=True))
    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.parametrize("acquisition_type", ["live_scraped_html", "live_scraped_json", "api", "static_file", "manual"])
def test_all_five_acquisition_types_accepted(session, acquisition_type):
    session.add(models.DataSource(source_key=f"src_{acquisition_type}", display_name="X", acquisition_type=acquisition_type, is_rescrapable=False))
    session.flush()  # must not raise for any of the five


def test_source_artifact_natural_key_unique(session):
    session.add(models.DataSource(source_key="src", display_name="Src", acquisition_type="static_file", is_rescrapable=False))
    session.flush()
    common = dict(
        source_key="src", source_root_key="teamdump_cbb", relative_path="x.csv",
        original_filename="x.csv", file_type="csv", content_hash="abc123",
        validation_status="pending", point_in_time_classification="current_snapshot_only",
    )
    session.add(models.SourceArtifact(**common))
    session.flush()
    session.add(models.SourceArtifact(**common))
    with pytest.raises(IntegrityError):
        session.flush()


def test_torvik_two_snapshots_with_different_content_hash_both_persist(session):
    """A rescrapable source (Torvik) produces a NEW row per distinct scrape, never an overwrite -- proven at the model/constraint level: two rows sharing source_root_key+relative_path but differing content_hash are both allowed to exist simultaneously."""
    session.add(models.DataSource(source_key="torvik", display_name="Torvik ratings", acquisition_type="live_scraped_json", is_rescrapable=True))
    session.flush()
    snapshot_1 = models.SourceArtifact(
        source_key="torvik", source_root_key="torvik_cache", relative_path="torvik_2026.json",
        original_filename="torvik_2026.json", file_type="json", content_hash="hash-day-1",
        collection_timestamp=dt.datetime(2026, 1, 10, tzinfo=dt.timezone.utc),
        season_id=None, point_in_time_classification="historical_but_not_point_in_time",
    )
    snapshot_2 = models.SourceArtifact(
        source_key="torvik", source_root_key="torvik_cache", relative_path="torvik_2026.json",
        original_filename="torvik_2026.json", file_type="json", content_hash="hash-day-15",  # ratings recalculated -- different content
        collection_timestamp=dt.datetime(2026, 1, 25, tzinfo=dt.timezone.utc),
        season_id=None, point_in_time_classification="historical_but_not_point_in_time",
    )
    session.add_all([snapshot_1, snapshot_2])
    session.flush()  # must not raise -- both snapshots coexist
    assert snapshot_1.id != snapshot_2.id
    assert snapshot_1.content_hash != snapshot_2.content_hash


def test_invalid_pipeline_run_status_rejected(session):
    session.add(models.PipelineRun(
        run_id="run-1", pipeline_name="test", status="not_a_real_status",
        started_at=dt.datetime.now(dt.timezone.utc),
    ))
    with pytest.raises(IntegrityError):
        session.flush()


def test_invalid_data_quality_severity_rejected(session):
    session.add(models.DataQualityIssue(issue_code="x", severity="not_a_real_severity", description="d"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_data_quality_issue_valid_severity_accepted(session):
    session.add(models.DataQualityIssue(issue_code="x", severity="warning", description="d"))
    session.flush()  # must not raise
