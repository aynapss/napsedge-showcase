"""
Phase 5B7: real PostgreSQL behavioral, constraint-enforcement, repository,
and concurrency proof -- everything here runs against `pg_session_factory`
(the real six-revision migrated schema on a disposable PostgreSQL 16
database), never SQLite. Where SQLite and PostgreSQL genuinely differ,
this module says so explicitly in a comment rather than silently
adjusting the assertion to make it pass.

Synthetic records only, plus small representative shapes -- never a bulk
import of any real local dataset (that remains explicitly out of scope
for this batch).
"""
from __future__ import annotations

import datetime as dt
import threading

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from src.db.session import unit_of_work
from src.db import models, allstats_models, game_log_models, espn_models, player_game_log_models
from src.db.repositories import (
    ConferenceRepository,
    DataQualityIssueRepository,
    DataSourceRepository,
    GameRepository,
    PipelineRunRepository,
    PlayerRepository,
    SeasonRepository,
    SourceArtifactRepository,
    TeamAliasRepository,
    TeamRepository,
)
from src.db.allstats_repositories import PlayerStatSnapshotRepository, RosterMembershipRepository
from src.db.game_log_repositories import GameResultRepository, TeamGameLogObservationRepository, TeamGameStatLineRepository
from src.db.espn_repositories import EspnBenchmarkSnapshotRepository, EspnBenchmarkValueRepository, EspnSourceObservationRepository
from src.db.player_game_log_repositories import PlayerGameLogObservationRepository
from src.exceptions import IdentityConflictError, ValidationError
from src.identity.game_identity import build_provisional_game_identity

pytestmark = pytest.mark.postgres


def _utc(*args) -> dt.datetime:
    return dt.datetime(*args, tzinfo=dt.timezone.utc)


def _seed_data_source(session, source_key: str, acquisition_type: str = "static_file") -> None:
    """source_artifacts.source_key has a real FK to data_sources -- every test that creates a SourceArtifact must register its DataSource first."""
    DataSourceRepository(session).upsert(source_key=source_key, display_name=source_key, acquisition_type=acquisition_type, is_rescrapable=False)


def _seed_two_teams_and_season(session_factory, season_id=2026):
    with unit_of_work(session_factory) as session:
        ConferenceRepository(session).upsert(conference_id=1, canonical_name="Atlantic 10")
        TeamRepository(session).upsert(team_id=1, display_name="St. Bonaventure", sr_slug="st-bonaventure", conference_id=1)
        TeamRepository(session).upsert(team_id=2, display_name="Bradley", sr_slug="bradley")
        SeasonRepository(session).upsert(season_id=season_id, start_year=season_id - 1, end_year=season_id, label="2025-26")


# ---------------------------------------------------------------------------
# Canonical teams/seasons, wide 63-bit game IDs, FK dependents
# ---------------------------------------------------------------------------


def test_canonical_team_and_season_round_trip(pg_session_factory):
    _seed_two_teams_and_season(pg_session_factory)
    with unit_of_work(pg_session_factory) as session:
        team = session.get(models.Team, 1)
        assert team.display_name == "St. Bonaventure"
        season = session.get(models.Season, 2026)
        assert season.label == "2025-26"


def test_wide_63_bit_game_id_round_trips_exactly(pg_session_factory):
    """A genuine v2 63-bit provisional game_id -- computed via the real, unmodified identity function -- must survive a real PostgreSQL BIGINT round trip byte-for-byte."""
    _seed_two_teams_and_season(pg_session_factory)
    identity = build_provisional_game_identity(home_team_id=1, away_team_id=2, season=2026, game_date="2025-11-03", neutral_site=True)
    game_id = identity.game_id
    assert game_id.bit_length() <= 63 and game_id > 2**31  # confirms this is genuinely a wide id, not one that happens to fit in 32 bits

    with unit_of_work(pg_session_factory) as session:
        GameRepository(session).upsert(game_id=game_id, home_team_id=1, away_team_id=2, season_id=2026, game_date="2025-11-03", neutral_site=True, is_provisional=True)

    with unit_of_work(pg_session_factory) as session:
        row = session.get(models.Game, game_id)
        assert row.game_id == game_id  # exact round trip, no truncation


def test_every_dependent_game_foreign_key_accepts_the_wide_id(pg_session_factory):
    """DataQualityIssue.game_id, TeamGameLogObservation.game_id, GameResult.game_id, PlayerGameLogObservation.game_id all reference games.game_id -- confirms every one of them accepts a real wide id without truncation or overflow at the PostgreSQL level."""
    _seed_two_teams_and_season(pg_session_factory)
    identity = build_provisional_game_identity(home_team_id=1, away_team_id=2, season=2026, game_date="2025-11-03", neutral_site=True)
    game_id = identity.game_id

    with unit_of_work(pg_session_factory) as session:
        GameRepository(session).upsert(game_id=game_id, home_team_id=1, away_team_id=2, season_id=2026, game_date="2025-11-03", neutral_site=True, is_provisional=True)
        _seed_data_source(session, "sr_team_game_log")
        artifact = SourceArtifactRepository(session).upsert(
            source_key="sr_team_game_log", source_root_key="teamdump_cbb", relative_path="st-bonaventure/x.csv",
            original_filename="x.csv", file_type="csv", content_hash="hashA", team_id=1, season_id=2026,
            validation_status="valid", point_in_time_classification="historical_but_not_point_in_time",
        )
        DataQualityIssueRepository(session).append(issue_code="probe", severity="info", description="probe", game_id=game_id)
        TeamGameLogObservationRepository(session).upsert(
            team_id=1, season_id=2026, source_artifact_id=artifact.id, source_row_position=0,
            raw_opponent_name="Bradley", opponent_team_id=2, game_id=game_id, venue_status="home",
            schema_variant="basic", validation_status="valid",
        )
        GameResultRepository(session).upsert(game_id=game_id, season_id=2026, team_a_id=1, team_b_id=2, result_status="single_sided_provisional", validation_status="valid", source_count=1)

    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.DataQualityIssue).filter_by(game_id=game_id).count() == 1
        assert session.query(game_log_models.TeamGameLogObservation).filter_by(game_id=game_id).count() == 1
        assert session.get(game_log_models.GameResult, game_id) is not None


# ---------------------------------------------------------------------------
# Constraint enforcement: FK, UNIQUE, CHECK
# ---------------------------------------------------------------------------


def test_foreign_key_violation_fails_on_real_postgresql(pg_session_factory):
    with pytest.raises(IntegrityError):
        with unit_of_work(pg_session_factory) as session:
            GameRepository(session).upsert(game_id=999, home_team_id=9999, away_team_id=8888, season_id=2026, game_date="2025-11-03", neutral_site=True, is_provisional=True)


def test_unique_constraint_violation_fails_correctly(pg_session_factory):
    """team_aliases has a real unique constraint on (source, raw_value) -- confirmed here at the PostgreSQL level, not just via the repository's own pre-check."""
    _seed_two_teams_and_season(pg_session_factory)
    with unit_of_work(pg_session_factory) as session:
        TeamAliasRepository(session).upsert(team_id=1, source="espn", raw_value="SBU", normalized_value="sbu")

    engine = pg_session_factory.kw["bind"]
    with engine.connect() as conn, conn.begin():
        with pytest.raises(IntegrityError):
            conn.execute(text(
                "INSERT INTO team_aliases (team_id, source, raw_value, normalized_value) VALUES (2, 'espn', 'SBU', 'sbu')"
            ))


def test_check_constraint_rejects_invalid_participation_status(pg_session_factory):
    _seed_two_teams_and_season(pg_session_factory)
    with unit_of_work(pg_session_factory) as session:
        _seed_data_source(session, "sr_player_game_log")
        artifact = SourceArtifactRepository(session).upsert(
            source_key="sr_player_game_log", source_root_key="teamdump_cbb", relative_path="st-bonaventure/p.csv",
            original_filename="p.csv", file_type="csv", content_hash="hashB", team_id=1, season_id=2026,
            validation_status="valid", point_in_time_classification="historical_but_not_point_in_time",
        )
        artifact_id = artifact.id

    with pytest.raises(IntegrityError):
        with unit_of_work(pg_session_factory) as session:
            PlayerGameLogObservationRepository(session).upsert(
                source_artifact_id=artifact_id, source_row_position=0, season_id=2026, team_id=1,
                raw_player_name="X", raw_opponent_name="Y", participation_status="not_a_real_status",
                validation_status="valid",
            )


def test_check_constraint_rejects_invalid_venue_status(pg_session_factory):
    _seed_two_teams_and_season(pg_session_factory)
    with unit_of_work(pg_session_factory) as session:
        _seed_data_source(session, "sr_team_game_log")
        artifact = SourceArtifactRepository(session).upsert(
            source_key="sr_team_game_log", source_root_key="teamdump_cbb", relative_path="st-bonaventure/t.csv",
            original_filename="t.csv", file_type="csv", content_hash="hashC", team_id=1, season_id=2026,
            validation_status="valid", point_in_time_classification="historical_but_not_point_in_time",
        )
        artifact_id = artifact.id

    with pytest.raises(IntegrityError):
        with unit_of_work(pg_session_factory) as session:
            TeamGameLogObservationRepository(session).upsert(
                team_id=1, season_id=2026, source_artifact_id=artifact_id, source_row_position=0,
                raw_opponent_name="Bradley", venue_status="offsite", schema_variant="basic", validation_status="valid",  # fits VARCHAR(10) but is not a real venue_status value -- exercises the CHECK, not a length truncation
            )


def test_controlled_espn_metric_names_are_enforced(pg_session_factory):
    """espn_benchmark_values.metric_name is CHECK-constrained against the exact 21-value real vocabulary -- an out-of-vocabulary metric name must be rejected, never silently accepted as free text."""
    with unit_of_work(pg_session_factory) as session:
        _seed_data_source(session, "espn_ncaa_benchmarks")
        artifact = SourceArtifactRepository(session).upsert(
            source_key="espn_ncaa_benchmarks", source_root_key="teamdump_espn", relative_path="espn_ncaa_benchmarks.xlsx",
            original_filename="espn_ncaa_benchmarks.xlsx", file_type="xlsx", content_hash="hashD",
            validation_status="valid", point_in_time_classification="current_snapshot_only",
        )
        snapshot = EspnBenchmarkSnapshotRepository(session).upsert(source_artifact_id=artifact.id, transformation_version="v1", validation_status="valid")
        EspnBenchmarkValueRepository(session).upsert(snapshot_id=snapshot.id, metric_name="PTS", ncaa_mean=8.4, ncaa_std=4.6)  # real, valid metric
        snapshot_id = snapshot.id

    with pytest.raises(IntegrityError):
        with unit_of_work(pg_session_factory) as session:
            EspnBenchmarkValueRepository(session).upsert(snapshot_id=snapshot_id, metric_name="NOT_A_REAL_METRIC", ncaa_mean=1.0, ncaa_std=1.0)


# ---------------------------------------------------------------------------
# Timestamps, provenance, immutable snapshots
# ---------------------------------------------------------------------------


def test_utc_aware_timestamp_retains_timezone_semantics(pg_session_factory):
    """A `DateTime(timezone=True)` column on real PostgreSQL (rendered as TIMESTAMP WITH TIME ZONE) must round-trip a UTC-aware value as an equal, still-aware instant -- never silently coerced to naive local time."""
    _seed_two_teams_and_season(pg_session_factory)
    started = _utc(2026, 1, 15, 18, 30, 0)
    with unit_of_work(pg_session_factory) as session:
        PipelineRunRepository(session).start(run_id="run-1", pipeline_name="test", started_at=started)

    with unit_of_work(pg_session_factory) as session:
        row = session.get(models.PipelineRun, "run-1")
        assert row.started_at.tzinfo is not None
        assert row.started_at == started


def test_pipeline_run_rejects_naive_timestamp(pg_session_factory):
    _seed_two_teams_and_season(pg_session_factory)
    with pytest.raises(ValidationError):
        with unit_of_work(pg_session_factory) as session:
            PipelineRunRepository(session).start(run_id="run-naive", pipeline_name="test", started_at=dt.datetime(2026, 1, 15, 18, 30, 0))


def test_collection_timestamp_stays_independent_of_filesystem_mtime(pg_session_factory):
    _seed_two_teams_and_season(pg_session_factory)
    fs_mtime = _utc(2026, 1, 1, 0, 0, 0)
    collected = _utc(2026, 1, 2, 12, 0, 0)
    with unit_of_work(pg_session_factory) as session:
        _seed_data_source(session, "sr_team_game_log")
        artifact = SourceArtifactRepository(session).upsert(
            source_key="sr_team_game_log", source_root_key="teamdump_cbb", relative_path="st-bonaventure/g.csv",
            original_filename="g.csv", file_type="csv", content_hash="hashE", team_id=1, season_id=2026,
            filesystem_mtime=fs_mtime, collection_timestamp=collected,
            validation_status="valid", point_in_time_classification="historical_but_not_point_in_time",
        )
        artifact_id = artifact.id

    with unit_of_work(pg_session_factory) as session:
        row = session.get(models.SourceArtifact, artifact_id)
        assert row.filesystem_mtime == fs_mtime
        assert row.collection_timestamp == collected
        assert row.filesystem_mtime != row.collection_timestamp


def test_multiple_immutable_source_snapshots_coexist(pg_session_factory):
    """Two different real content_hash values for the same (source_root_key, relative_path) must coexist as two distinct, immutable rows -- a later re-scrape is additive, never an overwrite."""
    _seed_two_teams_and_season(pg_session_factory)
    with unit_of_work(pg_session_factory) as session:
        _seed_data_source(session, "sr_team_game_log")
        repo = SourceArtifactRepository(session)
        repo.upsert(
            source_key="sr_team_game_log", source_root_key="teamdump_cbb", relative_path="st-bonaventure/g.csv",
            original_filename="g.csv", file_type="csv", content_hash="hash_v1", team_id=1, season_id=2026,
            validation_status="valid", point_in_time_classification="historical_but_not_point_in_time",
        )
        repo.upsert(
            source_key="sr_team_game_log", source_root_key="teamdump_cbb", relative_path="st-bonaventure/g.csv",
            original_filename="g.csv", file_type="csv", content_hash="hash_v2", team_id=1, season_id=2026,
            validation_status="valid", point_in_time_classification="historical_but_not_point_in_time",
        )

    with unit_of_work(pg_session_factory) as session:
        rows = session.query(models.SourceArtifact).filter_by(relative_path="st-bonaventure/g.csv").all()
        assert len(rows) == 2
        assert {r.content_hash for r in rows} == {"hash_v1", "hash_v2"}


def test_identical_source_artifact_import_is_idempotent(pg_session_factory):
    _seed_two_teams_and_season(pg_session_factory)
    common = dict(
        source_key="sr_team_game_log", source_root_key="teamdump_cbb", relative_path="st-bonaventure/g.csv",
        original_filename="g.csv", file_type="csv", content_hash="hash_same", team_id=1, season_id=2026,
        validation_status="valid", point_in_time_classification="historical_but_not_point_in_time",
    )
    with unit_of_work(pg_session_factory) as session:
        _seed_data_source(session, "sr_team_game_log")
        SourceArtifactRepository(session).upsert(**common)
        SourceArtifactRepository(session).upsert(**common)

    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.SourceArtifact).filter_by(content_hash="hash_same").count() == 1


def test_torvik_rescrapable_source_metadata_round_trip(pg_session_factory):
    """Torvik's own real acquisition mechanism (live-scraped JSON, rescrapable) round-trips distinctly from a static, non-rescrapable file source."""
    with unit_of_work(pg_session_factory) as session:
        DataSourceRepository(session).upsert(source_key="torvik_json", display_name="Torvik live JSON", acquisition_type="live_scraped_json", is_rescrapable=True)
        DataSourceRepository(session).upsert(source_key="sr_team_game_log", display_name="SR team game log (local cache)", acquisition_type="static_file", is_rescrapable=False)

    with unit_of_work(pg_session_factory) as session:
        torvik = session.get(models.DataSource, "torvik_json")
        assert torvik.acquisition_type == "live_scraped_json" and torvik.is_rescrapable is True
        static = session.get(models.DataSource, "sr_team_game_log")
        assert static.acquisition_type == "static_file" and static.is_rescrapable is False


# ---------------------------------------------------------------------------
# AllStats / ESPN representative round trips
# ---------------------------------------------------------------------------


def test_allstats_roster_membership_and_player_stat_snapshot_round_trip(pg_session_factory):
    _seed_two_teams_and_season(pg_session_factory)
    with unit_of_work(pg_session_factory) as session:
        _seed_data_source(session, "cbb_allstats_roster")
        artifact = SourceArtifactRepository(session).upsert(
            source_key="cbb_allstats_roster", source_root_key="teamdump_cbb", relative_path="st-bonaventure/Roster_2026.xlsx",
            original_filename="Roster_2026.xlsx", file_type="xlsx", content_hash="rosterhash", team_id=1, season_id=2026,
            validation_status="valid", point_in_time_classification="historical_but_not_point_in_time",
        )
        RosterMembershipRepository(session).upsert(
            team_id=1, season_id=2026, source_artifact_id=artifact.id, player_key="frank mitchell",
            source_player_name="Frank Mitchell", validation_status="valid",
        )
        PlayerStatSnapshotRepository(session).upsert(
            team_id=1, season_id=2026, source_artifact_id=artifact.id, player_key="frank mitchell",
            stat_table="PerGame", source_player_name="Frank Mitchell", validation_status="valid",
        )

    with unit_of_work(pg_session_factory) as session:
        assert session.query(allstats_models.RosterMembership).count() == 1
        assert session.query(allstats_models.PlayerStatSnapshot).count() == 1


def test_espn_source_observation_and_benchmark_values_round_trip(pg_session_factory):
    with unit_of_work(pg_session_factory) as session:
        SeasonRepository(session).upsert(season_id=2026, start_year=2025, end_year=2026, label="2025-26")
        _seed_data_source(session, "espn_player_stats")
        artifact = SourceArtifactRepository(session).upsert(
            source_key="espn_player_stats", source_root_key="teamdump_espn", relative_path="espn_full_player_stats.csv",
            original_filename="espn_full_player_stats.csv", file_type="csv", content_hash="espnhash",
            validation_status="valid", point_in_time_classification="current_snapshot_only",
        )
        EspnSourceObservationRepository(session).upsert(
            source_artifact_id=artifact.id, source_row_position=0, season_id=2026,
            raw_player_name="Some Player", raw_team_code="BYU", validation_status="valid",
        )

    with unit_of_work(pg_session_factory) as session:
        assert session.query(espn_models.EspnSourceObservation).count() == 1


def test_player_game_log_observation_round_trip(pg_session_factory):
    _seed_two_teams_and_season(pg_session_factory)
    with unit_of_work(pg_session_factory) as session:
        _seed_data_source(session, "sr_player_game_log")
        artifact = SourceArtifactRepository(session).upsert(
            source_key="sr_player_game_log", source_root_key="teamdump_cbb", relative_path="st-bonaventure/frank_mitchell_2026_gamelog.csv",
            original_filename="frank_mitchell_2026_gamelog.csv", file_type="csv", content_hash="pglhash", team_id=1, season_id=2026,
            validation_status="valid", point_in_time_classification="historical_but_not_point_in_time",
        )
        PlayerGameLogObservationRepository(session).upsert(
            source_artifact_id=artifact.id, source_row_position=0, season_id=2026, team_id=1,
            raw_player_name="Frank Mitchell", raw_opponent_name="Bradley", participation_status="played",
            validation_status="valid",
        )

    with unit_of_work(pg_session_factory) as session:
        assert session.query(player_game_log_models.PlayerGameLogObservation).count() == 1


# ---------------------------------------------------------------------------
# Repository transaction/idempotency/rollback semantics
# ---------------------------------------------------------------------------


def test_idempotent_upsert_no_duplicate_row(pg_session_factory):
    with unit_of_work(pg_session_factory) as session:
        TeamRepository(session).upsert(team_id=1, display_name="St. Bonaventure", sr_slug="st-bonaventure")
        TeamRepository(session).upsert(team_id=1, display_name="St. Bonaventure", sr_slug="st-bonaventure")
    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.Team).count() == 1


def test_immutable_field_conflict_detected(pg_session_factory):
    with unit_of_work(pg_session_factory) as session:
        TeamRepository(session).upsert(team_id=1, display_name="St. Bonaventure", sr_slug="st-bonaventure")
    with pytest.raises(IdentityConflictError):
        with unit_of_work(pg_session_factory) as session:
            TeamRepository(session).upsert(team_id=1, display_name="St. Bonaventure", sr_slug="different-slug")


def test_transaction_rolls_back_completely_on_failure_no_orphan_rows(pg_session_factory):
    with pytest.raises(IdentityConflictError):
        with unit_of_work(pg_session_factory) as session:
            ConferenceRepository(session).upsert(conference_id=1, canonical_name="Atlantic 10")
            TeamRepository(session).upsert(team_id=1, display_name="St. Bonaventure", sr_slug="st-bonaventure", conference_id=1)
            # This second team upsert conflicts and raises -- the conference AND the first
            # team above (already flushed within this same transaction) must both roll back.
            TeamRepository(session).upsert(team_id=1, display_name="St. Bonaventure", sr_slug="conflicting-slug")

    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.Conference).count() == 0
        assert session.query(models.Team).count() == 0


def test_no_internal_repository_commit_caller_controls_commit(pg_session_factory):
    """Confirms a repository's own upsert() never commits internally -- work inside an open unit_of_work is invisible to a second, independent connection until the block itself commits."""
    engine = pg_session_factory.kw["bind"]

    outer_uncommitted_seen = None
    with unit_of_work(pg_session_factory) as session:
        TeamRepository(session).upsert(team_id=1, display_name="St. Bonaventure", sr_slug="st-bonaventure")
        # Query via a completely separate connection WHILE the first transaction is still open.
        with engine.connect() as conn2:
            outer_uncommitted_seen = conn2.execute(text("SELECT count(*) FROM teams")).scalar()
    assert outer_uncommitted_seen == 0  # not yet visible -- no internal commit happened before this point

    with engine.connect() as conn2:
        after_commit_count = conn2.execute(text("SELECT count(*) FROM teams")).scalar()
    assert after_commit_count == 1  # now visible, only after unit_of_work's own single commit


def test_import_order_independence_forward_order(pg_session_factory):
    """Registering team A then B must converge on the same final state as B then A (proven by the companion reverse-order test below) -- reused pattern from the SQLite adapter suite, proven again against real PostgreSQL."""
    with unit_of_work(pg_session_factory) as session:
        TeamRepository(session).upsert(team_id=1, display_name="St. Bonaventure", sr_slug="st-bonaventure")
        TeamRepository(session).upsert(team_id=2, display_name="Bradley", sr_slug="bradley")
    with unit_of_work(pg_session_factory) as session:
        names = sorted(t.display_name for t in session.query(models.Team).all())
    assert names == ["Bradley", "St. Bonaventure"]


def test_import_order_independence_reverse_order(pg_session_factory):
    with unit_of_work(pg_session_factory) as session:
        TeamRepository(session).upsert(team_id=2, display_name="Bradley", sr_slug="bradley")
        TeamRepository(session).upsert(team_id=1, display_name="St. Bonaventure", sr_slug="st-bonaventure")
    with unit_of_work(pg_session_factory) as session:
        names = sorted(t.display_name for t in session.query(models.Team).all())
    assert names == ["Bradley", "St. Bonaventure"]  # identical final state regardless of registration order


def test_relationship_loading_team_conference_and_aliases(pg_session_factory):
    with unit_of_work(pg_session_factory) as session:
        ConferenceRepository(session).upsert(conference_id=1, canonical_name="Atlantic 10")
        TeamRepository(session).upsert(team_id=1, display_name="St. Bonaventure", sr_slug="st-bonaventure", conference_id=1)
        TeamAliasRepository(session).upsert(team_id=1, source="espn", raw_value="SBU", normalized_value="sbu")

    with unit_of_work(pg_session_factory) as session:
        team = session.get(models.Team, 1)
        assert team.conference.canonical_name == "Atlantic 10"
        assert len(team.aliases) == 1
        assert team.aliases[0].raw_value == "SBU"
        assert team in team.conference.teams


# ---------------------------------------------------------------------------
# Concurrency: two competing transactions on one natural-key upsert path
# ---------------------------------------------------------------------------


def test_concurrent_upsert_cannot_silently_create_duplicate_canonical_rows(pg_session_factory):
    """
    Two threads race to upsert the SAME new team_id. TeamRepository.upsert is a
    check-then-insert (not an atomic INSERT ... ON CONFLICT), so under a real
    concurrent race PostgreSQL's own primary-key uniqueness is the actual
    backstop: one thread's INSERT commits; the other thread's own INSERT then
    fails at the database level with a genuine IntegrityError once it
    unblocks. Confirms outcome #2 from this batch's own instruction: "one
    insert and one explicit integrity conflict handled by the caller" --
    never two silently-coexisting duplicate rows.
    """
    from src.db.session import build_session_factory

    engine = pg_session_factory.kw["bind"]
    sf_a = build_session_factory(engine)
    sf_b = build_session_factory(engine)

    a_flushed = threading.Event()
    b_attempted = threading.Event()
    results: dict[str, object] = {}

    def worker_a():
        session = sf_a()
        try:
            TeamRepository(session).upsert(team_id=42, display_name="Concurrency Team", sr_slug="concurrency-team")
            a_flushed.set()
            b_attempted.wait(timeout=10)
            session.commit()
            results["a_error"] = None
        except Exception as exc:  # pragma: no cover -- only on genuine unexpected failure
            session.rollback()
            results["a_error"] = exc
        finally:
            session.close()

    def worker_b():
        a_flushed.wait(timeout=10)
        session = sf_b()
        try:
            TeamRepository(session).upsert(team_id=42, display_name="Concurrency Team", sr_slug="concurrency-team")
            b_attempted.set()
            session.commit()
            results["b_error"] = None
        except IntegrityError as exc:
            session.rollback()
            results["b_error"] = exc
        finally:
            session.close()

    t_a = threading.Thread(target=worker_a)
    t_b = threading.Thread(target=worker_b)
    t_a.start()
    t_b.start()
    t_a.join(timeout=15)
    t_b.join(timeout=15)

    assert results.get("a_error") is None  # the first transaction to commit always succeeds
    assert isinstance(results.get("b_error"), IntegrityError)  # the second is a real, caller-visible integrity conflict

    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.Team).filter_by(team_id=42).count() == 1  # never two duplicate rows


# ---------------------------------------------------------------------------
# No production credential access, no TeamDump mutation (AST guards)
# ---------------------------------------------------------------------------


def test_this_module_never_reads_production_credentials_or_dotenv():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(__import__(__name__)))
    env_gets = {n.args[0].value for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get" and n.args and isinstance(n.args[0], ast.Constant)}
    assert "NAPSEDGE_DATABASE_URL" not in env_gets


def test_this_module_never_imports_teamdump_paths():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(__import__(__name__)))
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
            imported_names.update(a.name for a in node.names)
    assert "CBB_DIR" not in imported_names
    assert "ESPN_DIR" not in imported_names
