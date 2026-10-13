"""
Phase 5B3: schema-level tests for `src/db/espn_models.py`
(`espn_source_observations`, `espn_benchmark_snapshots`,
`espn_benchmark_values`). Same two-kind-of-proof split as every earlier
Phase 5 model test file: PostgreSQL dialect DDL compilation (no
connection) vs. real constraint enforcement against a disposable
in-memory SQLite database.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from src.db.base import Base
from src.db import models, espn_models
from src.db.enums import ESPN_BENCHMARK_METRIC_NAMES, ESPN_PLAYER_MATCH_METHODS, ESPN_TEAM_RESOLUTION_METHODS


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


NEW_TABLES = {"espn_source_observations", "espn_benchmark_snapshots", "espn_benchmark_values"}


def test_new_tables_registered_on_shared_metadata():
    assert NEW_TABLES <= set(Base.metadata.tables.keys())


def test_new_tables_compile_against_postgresql_dialect():
    pg = postgresql.dialect()
    for name in NEW_TABLES:
        ddl = str(CreateTable(Base.metadata.tables[name]).compile(dialect=pg))
        assert "CREATE TABLE" in ddl


def _seed(session, team_id=1, season_id=2026):
    session.add(models.Team(team_id=team_id, display_name="Team A", sr_slug=f"team-{team_id}"))
    session.add(models.Season(season_id=season_id, start_year=season_id - 1, end_year=season_id, label="2025-26"))
    session.flush()


def _seed_artifact(session, *, source_key="espn_player_stats", relative_path="espn_full_player_stats.csv", content_hash="hashA", season_id=2026):
    if session.get(models.DataSource, source_key) is None:
        session.add(models.DataSource(source_key=source_key, display_name="ESPN player stats", acquisition_type="static_file", is_rescrapable=False))
        session.flush()
    artifact = models.SourceArtifact(
        source_key=source_key, source_root_key="teamdump_espn", relative_path=relative_path,
        original_filename=relative_path, file_type="csv", content_hash=content_hash,
        season_id=season_id, validation_status="valid",
        point_in_time_classification="current_snapshot_only",
    )
    session.add(artifact)
    session.flush()
    return artifact


def test_observation_natural_key_unique(session):
    _seed(session)
    artifact = _seed_artifact(session)
    common = dict(source_artifact_id=artifact.id, source_row_position=0, season_id=2026, raw_player_name="A Player", raw_team_code="ABC", validation_status="unresolved")
    session.add(espn_models.EspnSourceObservation(**common))
    session.flush()
    session.add(espn_models.EspnSourceObservation(**common))
    with pytest.raises(IntegrityError):
        session.flush()


def test_observation_allows_same_row_position_different_artifact(session):
    _seed(session)
    a1 = _seed_artifact(session, content_hash="hashA")
    a2 = _seed_artifact(session, content_hash="hashB")
    session.add(espn_models.EspnSourceObservation(source_artifact_id=a1.id, source_row_position=0, season_id=2026, raw_player_name="A", raw_team_code="ABC", validation_status="unresolved"))
    session.add(espn_models.EspnSourceObservation(source_artifact_id=a2.id, source_row_position=0, season_id=2026, raw_player_name="A", raw_team_code="ABC", validation_status="unresolved"))
    session.flush()  # must not raise


def test_observation_foreign_key_to_source_artifact_enforced(session):
    session.add(espn_models.EspnSourceObservation(source_artifact_id=999999, source_row_position=0, season_id=2026, raw_player_name="A", raw_team_code="ABC", validation_status="unresolved"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_observation_team_and_player_nullable_for_unresolved(session):
    _seed(session)
    artifact = _seed_artifact(session)
    row = espn_models.EspnSourceObservation(
        source_artifact_id=artifact.id, source_row_position=0, season_id=2026,
        raw_player_name="Unresolved Player", raw_team_code="ZZZ",
        team_id=None, player_id=None, validation_status="unresolved",
    )
    session.add(row)
    session.flush()
    assert row.team_id is None and row.player_id is None


@pytest.mark.parametrize("method", ESPN_TEAM_RESOLUTION_METHODS)
def test_observation_accepts_all_team_resolution_methods(session, method):
    _seed(session)
    artifact = _seed_artifact(session)
    session.add(espn_models.EspnSourceObservation(source_artifact_id=artifact.id, source_row_position=0, season_id=2026, raw_player_name="A", raw_team_code="ABC", team_resolution_method=method, validation_status="valid"))
    session.flush()  # must not raise for any value


def test_observation_rejects_invalid_team_resolution_method(session):
    _seed(session)
    artifact = _seed_artifact(session)
    session.add(espn_models.EspnSourceObservation(source_artifact_id=artifact.id, source_row_position=0, season_id=2026, raw_player_name="A", raw_team_code="ABC", team_resolution_method="guessed", validation_status="valid"))
    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.parametrize("method", ESPN_PLAYER_MATCH_METHODS)
def test_observation_accepts_all_player_match_methods(session, method):
    _seed(session)
    artifact = _seed_artifact(session)
    session.add(espn_models.EspnSourceObservation(source_artifact_id=artifact.id, source_row_position=0, season_id=2026, raw_player_name="A", raw_team_code="ABC", player_match_method=method, validation_status="valid"))
    session.flush()  # must not raise for any value


def test_observation_null_resolution_methods_allowed(session):
    """A row parsed before any resolution attempt (or one this adapter never resolves at all) must not be forced into the fixed vocabulary."""
    _seed(session)
    artifact = _seed_artifact(session)
    session.add(espn_models.EspnSourceObservation(source_artifact_id=artifact.id, source_row_position=0, season_id=2026, raw_player_name="A", raw_team_code="ABC", team_resolution_method=None, player_match_method=None, validation_status="pending"))
    session.flush()  # must not raise


def test_snapshot_natural_key_unique(session):
    _seed(session)
    artifact = _seed_artifact(session, source_key="espn_ncaa_benchmarks", relative_path="espn_ncaa_benchmarks.xlsx")
    session.add(espn_models.EspnBenchmarkSnapshot(source_artifact_id=artifact.id, transformation_version="espn_player_benchmarks_v1", validation_status="valid"))
    session.flush()
    session.add(espn_models.EspnBenchmarkSnapshot(source_artifact_id=artifact.id, transformation_version="espn_player_benchmarks_v1", validation_status="valid"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_snapshot_season_id_nullable():
    """The real espn_ncaa_benchmarks.xlsx is not season-scoped at all -- season_id must be a real, honest null, never forced."""
    from sqlalchemy.orm import Session as _S
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def _fk(c, r):
        cur = c.cursor(); cur.execute("PRAGMA foreign_keys=ON"); cur.close()

    Base.metadata.create_all(engine)
    with _S(engine) as session:
        artifact = _seed_artifact(session, source_key="espn_ncaa_benchmarks", relative_path="espn_ncaa_benchmarks.xlsx", season_id=None)
        row = espn_models.EspnBenchmarkSnapshot(source_artifact_id=artifact.id, transformation_version="espn_player_benchmarks_v1", validation_status="valid", season_id=None)
        session.add(row)
        session.flush()
        assert row.season_id is None
    engine.dispose()


def test_value_natural_key_unique(session):
    _seed(session)
    artifact = _seed_artifact(session, source_key="espn_ncaa_benchmarks", relative_path="espn_ncaa_benchmarks.xlsx")
    snap = espn_models.EspnBenchmarkSnapshot(source_artifact_id=artifact.id, transformation_version="espn_player_benchmarks_v1", validation_status="valid")
    session.add(snap)
    session.flush()
    session.add(espn_models.EspnBenchmarkValue(snapshot_id=snap.id, metric_name="PTS", ncaa_mean=8.4, ncaa_std=4.6))
    session.flush()
    session.add(espn_models.EspnBenchmarkValue(snapshot_id=snap.id, metric_name="PTS", ncaa_mean=8.4, ncaa_std=4.6))
    with pytest.raises(IntegrityError):
        session.flush()


def test_value_foreign_key_to_snapshot_enforced(session):
    session.add(espn_models.EspnBenchmarkValue(snapshot_id=999999, metric_name="PTS", ncaa_mean=8.4, ncaa_std=4.6))
    with pytest.raises(IntegrityError):
        session.flush()


def test_benchmark_metric_names_exactly_21_real_values():
    """Locks the confirmed real vocabulary read directly from espn_ncaa_benchmarks.xlsx -- 21 exact values, no more, no less."""
    assert len(ESPN_BENCHMARK_METRIC_NAMES) == 21
    assert set(ESPN_BENCHMARK_METRIC_NAMES) == {
        "PTS", "PTS_40", "REB", "REB_40", "AST", "AST_40", "STL", "STL_40",
        "BLK", "BLK_40", "TO", "TO_40", "FG%", "3P%", "FT%", "eFG", "TS",
        "AST_TO", "Disruption", "GameScore", "Poss_per_min",
    }


@pytest.mark.parametrize("metric_name", ESPN_BENCHMARK_METRIC_NAMES)
def test_value_accepts_every_real_metric_name(session, metric_name):
    _seed(session)
    artifact = _seed_artifact(session, source_key="espn_ncaa_benchmarks", relative_path="espn_ncaa_benchmarks.xlsx")
    snap = espn_models.EspnBenchmarkSnapshot(source_artifact_id=artifact.id, transformation_version="espn_player_benchmarks_v1", validation_status="valid")
    session.add(snap)
    session.flush()
    session.add(espn_models.EspnBenchmarkValue(snapshot_id=snap.id, metric_name=metric_name, ncaa_mean=1.0, ncaa_std=1.0))
    session.flush()  # must not raise for any of the 21 real values


def test_value_rejects_unvalidated_metric_name_not_an_eav_table(session):
    """The controlled-vocabulary guard this batch added: an arbitrary/unvalidated metric name must be rejected at the database level, never silently accepted (avoids an uncontrolled EAV table)."""
    _seed(session)
    artifact = _seed_artifact(session, source_key="espn_ncaa_benchmarks", relative_path="espn_ncaa_benchmarks.xlsx")
    snap = espn_models.EspnBenchmarkSnapshot(source_artifact_id=artifact.id, transformation_version="espn_player_benchmarks_v1", validation_status="valid")
    session.add(snap)
    session.flush()
    session.add(espn_models.EspnBenchmarkValue(snapshot_id=snap.id, metric_name="NOT_A_REAL_METRIC", ncaa_mean=1.0, ncaa_std=1.0))
    with pytest.raises(IntegrityError):
        session.flush()


def test_no_second_game_or_player_identity_invented():
    """Confirms this batch's tables link to the EXISTING teams/players/games PKs only -- never a second identity."""
    obs_cols = Base.metadata.tables["espn_source_observations"].c
    team_fk_targets = {fk.target_fullname for fk in obs_cols["team_id"].foreign_keys}
    player_fk_targets = {fk.target_fullname for fk in obs_cols["player_id"].foreign_keys}
    assert team_fk_targets == {"teams.team_id"}
    assert player_fk_targets == {"players.player_id"}
    assert "game_id" not in obs_cols.keys()  # this data is not game-scoped at all -- confirmed by the real file's own shape


def test_raw_observation_never_stores_derived_columns():
    """Architecture-decision guard: espn_source_observations must carry only the 19 real raw stat columns -- never a BM-*/IMPACT-*/ARCH-Type-style derived column."""
    obs_cols = set(Base.metadata.tables["espn_source_observations"].c.keys())
    forbidden_prefixes = ("bm_", "val_", "pct_", "impact_", "arch_", "rank_", "tier_", "z_")
    assert not any(col.lower().startswith(forbidden_prefixes) for col in obs_cols)
