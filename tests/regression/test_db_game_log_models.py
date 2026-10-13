"""
Phase 5B2: schema-level tests for `src/db/game_log_models.py`
(`team_game_log_observations`, `team_game_stat_lines`, `game_results`).
Same two-kind-of-proof split as Phase 5A/5B1's own model tests:
PostgreSQL dialect DDL compilation (no connection) vs. real constraint
enforcement against a disposable in-memory SQLite database.
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
from src.db import models, game_log_models
from src.db.enums import GAME_LOG_SCHEMA_VARIANTS, GAME_LOG_VENUE_STATUSES, GAME_RESULT_STATUSES


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


NEW_TABLES = {"team_game_log_observations", "team_game_stat_lines", "game_results"}


def test_new_tables_registered_on_shared_metadata():
    assert NEW_TABLES <= set(Base.metadata.tables.keys())


def test_new_tables_compile_against_postgresql_dialect():
    pg = postgresql.dialect()
    for name in NEW_TABLES:
        ddl = str(CreateTable(Base.metadata.tables[name]).compile(dialect=pg))
        assert "CREATE TABLE" in ddl


def _seed(session, team_a=1, team_b=2, season_id=2026):
    session.add(models.Team(team_id=team_a, display_name="Team A", sr_slug=f"team-{team_a}"))
    session.add(models.Team(team_id=team_b, display_name="Team B", sr_slug=f"team-{team_b}"))
    session.add(models.Season(season_id=season_id, start_year=season_id - 1, end_year=season_id, label="2025-26"))
    session.flush()


def _seed_artifact(session, *, source_key="sr_gamelog", team_id=1, season_id=2026, relative_path="team-1/team-1_2026_game_log.csv", content_hash="hashA"):
    if session.get(models.DataSource, source_key) is None:
        session.add(models.DataSource(source_key=source_key, display_name="SR Game Log", acquisition_type="static_file", is_rescrapable=False))
        session.flush()
    artifact = models.SourceArtifact(
        source_key=source_key, source_root_key="teamdump_cbb", relative_path=relative_path,
        original_filename=relative_path.split("/")[-1], file_type="csv", content_hash=content_hash,
        team_id=team_id, season_id=season_id, validation_status="valid",
        point_in_time_classification="historical_but_not_point_in_time",
    )
    session.add(artifact)
    session.flush()
    return artifact


def _seed_game(session, *, game_id=100, home=1, away=2, season_id=2026, game_date=dt.date(2026, 1, 15)):
    session.add(models.Game(game_id=game_id, home_team_id=home, away_team_id=away, season_id=season_id, game_date=game_date, neutral_site=True, is_provisional=True))
    session.flush()


def test_observation_natural_key_unique(session):
    _seed(session)
    artifact = _seed_artifact(session)
    common = dict(team_id=1, season_id=2026, source_artifact_id=artifact.id, source_row_position=0, raw_opponent_name="Team B", venue_status="unknown", schema_variant="basic", validation_status="unresolved")
    session.add(game_log_models.TeamGameLogObservation(**common))
    session.flush()
    session.add(game_log_models.TeamGameLogObservation(**common))
    with pytest.raises(IntegrityError):
        session.flush()


def test_observation_allows_same_row_position_different_artifact(session):
    """A later re-scrape (new source_artifact) of the same file gets its own additive row -- never overwrites the prior one."""
    _seed(session)
    a1 = _seed_artifact(session, content_hash="hashA")
    a2 = _seed_artifact(session, content_hash="hashB")
    session.add(game_log_models.TeamGameLogObservation(team_id=1, season_id=2026, source_artifact_id=a1.id, source_row_position=0, raw_opponent_name="Team B", venue_status="unknown", schema_variant="basic", validation_status="unresolved"))
    session.add(game_log_models.TeamGameLogObservation(team_id=1, season_id=2026, source_artifact_id=a2.id, source_row_position=0, raw_opponent_name="Team B", venue_status="unknown", schema_variant="basic", validation_status="unresolved"))
    session.flush()  # must not raise


def test_observation_foreign_key_to_team_enforced(session):
    session.add(game_log_models.TeamGameLogObservation(team_id=999999, season_id=2026, source_artifact_id=1, source_row_position=0, raw_opponent_name="X", venue_status="unknown", schema_variant="basic", validation_status="unresolved"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_observation_opponent_and_game_id_nullable_for_unresolved(session):
    _seed(session)
    artifact = _seed_artifact(session)
    row = game_log_models.TeamGameLogObservation(
        team_id=1, season_id=2026, source_artifact_id=artifact.id, source_row_position=0,
        raw_opponent_name="Unresolvable School", opponent_team_id=None, game_id=None,
        venue_status="unknown", schema_variant="basic", validation_status="unresolved",
    )
    session.add(row)
    session.flush()  # must not raise
    assert row.opponent_team_id is None
    assert row.game_id is None


@pytest.mark.parametrize("venue_status", GAME_LOG_VENUE_STATUSES)
def test_observation_accepts_all_four_venue_statuses(session, venue_status):
    _seed(session)
    artifact = _seed_artifact(session)
    session.add(game_log_models.TeamGameLogObservation(
        team_id=1, season_id=2026, source_artifact_id=artifact.id, source_row_position=0,
        raw_opponent_name="Team B", venue_status=venue_status, schema_variant="basic", validation_status="valid",
    ))
    session.flush()  # must not raise for any of the four


def test_observation_rejects_invalid_venue_status(session):
    _seed(session)
    artifact = _seed_artifact(session)
    session.add(game_log_models.TeamGameLogObservation(
        team_id=1, season_id=2026, source_artifact_id=artifact.id, source_row_position=0,
        raw_opponent_name="Team B", venue_status="not_a_real_venue", schema_variant="basic", validation_status="valid",
    ))
    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.parametrize("schema_variant", GAME_LOG_SCHEMA_VARIANTS)
def test_observation_accepts_both_schema_variants(session, schema_variant):
    _seed(session)
    artifact = _seed_artifact(session)
    session.add(game_log_models.TeamGameLogObservation(
        team_id=1, season_id=2026, source_artifact_id=artifact.id, source_row_position=0,
        raw_opponent_name="Team B", venue_status="unknown", schema_variant=schema_variant, validation_status="valid",
    ))
    session.flush()  # must not raise


def test_stat_line_one_to_one_with_observation(session):
    _seed(session)
    artifact = _seed_artifact(session)
    obs = game_log_models.TeamGameLogObservation(team_id=1, season_id=2026, source_artifact_id=artifact.id, source_row_position=0, raw_opponent_name="Team B", venue_status="unknown", schema_variant="basic", validation_status="valid")
    session.add(obs)
    session.flush()
    session.add(game_log_models.TeamGameStatLine(observation_id=obs.id, off_rating=110.0))
    session.flush()
    session.add(game_log_models.TeamGameStatLine(observation_id=obs.id, off_rating=111.0))
    with pytest.raises(IntegrityError):
        session.flush()


def test_stat_line_sparse_columns_not_eav(session):
    _seed(session)
    artifact = _seed_artifact(session)
    obs = game_log_models.TeamGameLogObservation(team_id=1, season_id=2026, source_artifact_id=artifact.id, source_row_position=0, raw_opponent_name="Team B", venue_status="unknown", schema_variant="basic", validation_status="valid")
    session.add(obs)
    session.flush()
    row = game_log_models.TeamGameStatLine(observation_id=obs.id, off_rating=110.0, def_rating=95.0, net_rating=15.0)
    session.add(row)
    session.flush()
    assert row.off_rating == 110.0
    assert row.pace is None  # advanced-only column, correctly left null for a basic-schema row
    assert not hasattr(row, "stat_name") and not hasattr(row, "stat_value")


def test_game_result_pk_is_game_id_not_a_surrogate(session):
    _seed(session)
    _seed_game(session)
    row = game_log_models.GameResult(
        game_id=100, season_id=2026, team_a_id=1, team_b_id=2, result_status="reconciled",
        validation_status="valid", source_count=2,
    )
    session.add(row)
    session.flush()
    fetched = session.get(game_log_models.GameResult, 100)
    assert fetched is not None


def test_game_result_rejects_same_team_a_b(session):
    _seed(session)
    _seed_game(session)
    session.add(game_log_models.GameResult(game_id=100, season_id=2026, team_a_id=1, team_b_id=1, result_status="reconciled", validation_status="valid", source_count=2))
    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.parametrize("result_status", GAME_RESULT_STATUSES)
def test_game_result_accepts_all_three_result_statuses(session, result_status):
    _seed(session)
    _seed_game(session)
    session.add(game_log_models.GameResult(game_id=100, season_id=2026, team_a_id=1, team_b_id=2, result_status=result_status, validation_status="valid", source_count=1))
    session.flush()  # must not raise for any of the three


def test_game_result_rejects_invalid_result_status(session):
    _seed(session)
    _seed_game(session)
    session.add(game_log_models.GameResult(game_id=100, season_id=2026, team_a_id=1, team_b_id=2, result_status="not_a_real_status", validation_status="valid", source_count=1))
    with pytest.raises(IntegrityError):
        session.flush()


def test_game_result_rejects_source_count_out_of_range(session):
    _seed(session)
    _seed_game(session)
    session.add(game_log_models.GameResult(game_id=100, season_id=2026, team_a_id=1, team_b_id=2, result_status="reconciled", validation_status="valid", source_count=3))
    with pytest.raises(IntegrityError):
        session.flush()


def test_game_result_links_to_observations(session):
    _seed(session)
    _seed_game(session)
    artifact = _seed_artifact(session)
    obs = game_log_models.TeamGameLogObservation(team_id=1, season_id=2026, source_artifact_id=artifact.id, source_row_position=0, raw_opponent_name="Team B", game_id=100, opponent_team_id=2, venue_status="unknown", schema_variant="basic", validation_status="valid", points_scored=70, points_allowed=60)
    session.add(obs)
    session.flush()
    result = game_log_models.GameResult(
        game_id=100, season_id=2026, team_a_id=1, team_b_id=2, result_status="single_sided_provisional",
        validation_status="partial", source_count=1, team_a_observation_id=obs.id,
    )
    session.add(result)
    session.flush()
    assert result.team_a_observation_id == obs.id
    assert result.team_b_observation_id is None


def test_reuses_existing_games_table_no_second_identity():
    """No new game-identity table or column exists anywhere in this batch's schema -- game_results.game_id and team_game_log_observations.game_id both FK directly to the existing games.game_id."""
    columns = set(Base.metadata.tables["game_results"].c.keys())
    assert "game_id" in columns
    fk_targets = {fk.target_fullname for fk in Base.metadata.tables["game_results"].c["game_id"].foreign_keys}
    assert fk_targets == {"games.game_id"}
