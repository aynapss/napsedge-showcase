"""
Phase 5B4: schema-level tests for `src/db/player_game_log_models.py`
(`player_game_log_observations`). Same two-kind-of-proof split as every
earlier Phase 5 model test file: PostgreSQL dialect DDL compilation (no
connection) vs. real constraint enforcement against a disposable
in-memory SQLite database.
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
from src.db import models, player_game_log_models
from src.db.enums import PLAYER_GAME_PARTICIPATION_STATUSES, VALIDATION_STATUSES


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


def test_new_table_registered_on_shared_metadata():
    assert "player_game_log_observations" in Base.metadata.tables


def test_new_table_compiles_against_postgresql_dialect():
    pg = postgresql.dialect()
    ddl = str(CreateTable(Base.metadata.tables["player_game_log_observations"]).compile(dialect=pg))
    assert "CREATE TABLE" in ddl


def test_game_id_column_is_biginteger_v2_scheme_compatible():
    """Confirms this new table's game_id inherits the widened (63-bit-compatible) BigInteger type from games.game_id -- never a plain 32-bit Integer."""
    col = Base.metadata.tables["player_game_log_observations"].c["game_id"]
    assert type(col.type).__name__ == "BigInteger"


def _seed(session, team_id=1, opponent_id=2, season_id=2026):
    session.add(models.Team(team_id=team_id, display_name="Team A", sr_slug=f"team-{team_id}"))
    session.add(models.Team(team_id=opponent_id, display_name="Team B", sr_slug=f"team-{opponent_id}"))
    session.add(models.Season(season_id=season_id, start_year=season_id - 1, end_year=season_id, label="2025-26"))
    session.flush()


def _seed_artifact(session, *, source_key="sr_player_game_log", team_id=1, season_id=2026, relative_path="team-1/frank_mitchell_2026_gamelog.csv", content_hash="hashA"):
    if session.get(models.DataSource, source_key) is None:
        session.add(models.DataSource(source_key=source_key, display_name="SR player game log", acquisition_type="static_file", is_rescrapable=False))
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
    common = dict(source_artifact_id=artifact.id, source_row_position=0, season_id=2026, team_id=1, raw_player_name="Frank Mitchell", raw_opponent_name="Team B", participation_status="played", validation_status="unresolved")
    session.add(player_game_log_models.PlayerGameLogObservation(**common))
    session.flush()
    session.add(player_game_log_models.PlayerGameLogObservation(**common))
    with pytest.raises(IntegrityError):
        session.flush()


def test_observation_allows_same_row_position_different_artifact(session):
    """A later re-scrape (new source_artifact) of the same file gets its own additive row -- never overwrites the prior one."""
    _seed(session)
    a1 = _seed_artifact(session, content_hash="hashA")
    a2 = _seed_artifact(session, content_hash="hashB")
    session.add(player_game_log_models.PlayerGameLogObservation(source_artifact_id=a1.id, source_row_position=0, season_id=2026, team_id=1, raw_player_name="Frank Mitchell", raw_opponent_name="Team B", participation_status="played", validation_status="unresolved"))
    session.add(player_game_log_models.PlayerGameLogObservation(source_artifact_id=a2.id, source_row_position=0, season_id=2026, team_id=1, raw_player_name="Frank Mitchell", raw_opponent_name="Team B", participation_status="played", validation_status="unresolved"))
    session.flush()  # must not raise


def test_observation_foreign_key_to_team_enforced(session):
    session.add(player_game_log_models.PlayerGameLogObservation(source_artifact_id=1, source_row_position=0, season_id=2026, team_id=999999, raw_player_name="X", raw_opponent_name="Y", participation_status="played", validation_status="unresolved"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_observation_player_opponent_and_game_nullable_for_unresolved(session):
    _seed(session)
    artifact = _seed_artifact(session)
    row = player_game_log_models.PlayerGameLogObservation(
        source_artifact_id=artifact.id, source_row_position=0, season_id=2026, team_id=1,
        raw_player_name="Frank Mitchell", raw_opponent_name="Unresolvable School",
        player_id=None, opponent_team_id=None, game_id=None,
        participation_status="played", validation_status="unresolved",
    )
    session.add(row)
    session.flush()  # must not raise
    assert row.player_id is None and row.opponent_team_id is None and row.game_id is None


@pytest.mark.parametrize("status", PLAYER_GAME_PARTICIPATION_STATUSES)
def test_observation_accepts_all_participation_statuses(session, status):
    _seed(session)
    artifact = _seed_artifact(session)
    session.add(player_game_log_models.PlayerGameLogObservation(source_artifact_id=artifact.id, source_row_position=0, season_id=2026, team_id=1, raw_player_name="X", raw_opponent_name="Y", participation_status=status, validation_status="valid"))
    session.flush()  # must not raise for any real value


def test_observation_rejects_invalid_participation_status(session):
    _seed(session)
    artifact = _seed_artifact(session)
    session.add(player_game_log_models.PlayerGameLogObservation(source_artifact_id=artifact.id, source_row_position=0, season_id=2026, team_id=1, raw_player_name="X", raw_opponent_name="Y", participation_status="benched_forever", validation_status="valid"))
    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.parametrize("status", VALIDATION_STATUSES)
def test_observation_accepts_all_validation_statuses(session, status):
    _seed(session)
    artifact = _seed_artifact(session)
    session.add(player_game_log_models.PlayerGameLogObservation(source_artifact_id=artifact.id, source_row_position=0, season_id=2026, team_id=1, raw_player_name="X", raw_opponent_name="Y", participation_status="played", validation_status=status))
    session.flush()  # must not raise


def test_observation_links_to_game_and_opponent(session):
    _seed(session)
    _seed_game(session)
    artifact = _seed_artifact(session)
    row = player_game_log_models.PlayerGameLogObservation(
        source_artifact_id=artifact.id, source_row_position=0, season_id=2026, team_id=1,
        raw_player_name="Frank Mitchell", raw_opponent_name="Team B", opponent_team_id=2, game_id=100,
        participation_status="played", validation_status="valid",
        team_score=69, opponent_score=63, raw_result_marker="W",
    )
    session.add(row)
    session.flush()
    assert row.game_id == 100
    assert row.opponent_team_id == 2


def test_reuses_existing_games_table_no_second_identity():
    """No new game-identity table or column exists anywhere in this batch's schema -- player_game_log_observations.game_id FKs directly to the existing games.game_id."""
    fk_targets = {fk.target_fullname for fk in Base.metadata.tables["player_game_log_observations"].c["game_id"].foreign_keys}
    assert fk_targets == {"games.game_id"}


def test_reuses_existing_players_table_no_second_identity():
    fk_targets = {fk.target_fullname for fk in Base.metadata.tables["player_game_log_observations"].c["player_id"].foreign_keys}
    assert fk_targets == {"players.player_id"}


def test_no_derived_recency_or_ovr_column_exists():
    """Architecture-decision guard: this table must carry only raw evidence and structural identity-resolution fields -- never a recency/volatility/OVR-style derived column."""
    cols = set(Base.metadata.tables["player_game_log_observations"].c.keys())
    forbidden = ("recency", "volatility", "ovr", "composite_score", "classification", "tier")
    assert not any(any(f in c.lower() for f in forbidden) for c in cols)


def test_stat_columns_sparse_not_eav(session):
    """Confirms the single-wide-table design (no schema variant exists for this source) -- never a generic (stat_name, stat_value) EAV pair."""
    _seed(session)
    artifact = _seed_artifact(session)
    row = player_game_log_models.PlayerGameLogObservation(
        source_artifact_id=artifact.id, source_row_position=0, season_id=2026, team_id=1,
        raw_player_name="X", raw_opponent_name="Y", participation_status="played", validation_status="valid",
        points=12, rebounds=9, assists=2,
    )
    session.add(row)
    session.flush()
    assert row.points == 12
    assert not hasattr(row, "stat_name") and not hasattr(row, "stat_value")
