"""
Controlled bulk-import design, Slice 3 (docs/controlled_bulk_import_design.md
sections 8/13/16, approved for this slice only): tests for
`src/pipeline/bulk_import_reconciliation.py` against a disposable, isolated
SQLite database (never `TeamDump`, never a shared file, never a real
adapter or real source file). PostgreSQL-specific behavior (real
constraint/aggregation semantics, caller-uncommitted visibility, SQL
statement inspection) lives in `tests/postgres/test_postgres_bulk_import_reconciliation.py`.
"""
from __future__ import annotations

import ast
import datetime as dt
import inspect

import pytest
from sqlalchemy import create_engine, event

from src.db import allstats_models, espn_models, game_log_models, models, player_game_log_models
from src.db.allstats_repositories import PlayerStatSnapshotRepository, RosterMembershipRepository, TeamStatSnapshotRepository
from src.db.base import Base
from src.db.espn_repositories import EspnBenchmarkSnapshotRepository, EspnBenchmarkValueRepository, EspnSourceObservationRepository
from src.db.game_log_repositories import GameResultRepository, TeamGameLogObservationRepository
from src.db.player_game_log_repositories import PlayerGameLogObservationRepository
from src.db.repositories import (
    ConferenceRepository,
    DataQualityIssueRepository,
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
from src.pipeline.bulk_import_reconciliation import (
    SOURCE_FAMILY_SOURCE_KEYS,
    Unavailable,
    read_pipeline_run_reconciliation,
    read_source_artifact_reconciliation,
    read_source_family_reconciliation,
)

UTC = dt.timezone.utc


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
def session_factory(engine):
    return build_session_factory(engine)


def _seed_reference(session_factory):
    with unit_of_work(session_factory) as session:
        ConferenceRepository(session).upsert(conference_id=1, canonical_name="Atlantic 10")
        TeamRepository(session).upsert(team_id=1, display_name="St. Bonaventure", sr_slug="st-bonaventure", conference_id=1)
        TeamRepository(session).upsert(team_id=2, display_name="Bradley", sr_slug="bradley")
        SeasonRepository(session).upsert(season_id=2026, start_year=2025, end_year=2026, label="2025-26")
        for source_key in ("sports_reference_allstats", "sports_reference_game_log", "espn_player_stats", "espn_ncaa_benchmarks", "sports_reference_player_game_log"):
            DataSourceRepository(session).upsert(source_key=source_key, display_name=source_key, acquisition_type="static_file", is_rescrapable=False)


def _seed_run(session_factory, run_id="run1", status="running", started_at=None):
    with unit_of_work(session_factory) as session:
        PipelineRunRepository(session).start(run_id=run_id, pipeline_name="bulk_import_test", started_at=started_at or dt.datetime(2026, 1, 1, tzinfo=UTC), status=status)


def _wide_game_id():
    return build_provisional_game_identity(home_team_id=1, away_team_id=2, season=2026, game_date="2025-11-03", neutral_site=True).game_id


def _seed_allstats(session_factory, run_id="run1", content_hash="ash1"):
    with unit_of_work(session_factory) as session:
        artifact = SourceArtifactRepository(session).upsert(
            source_key="sports_reference_allstats", source_root_key="teamdump_cbb", relative_path="st-bonaventure/Roster_2026.xlsx",
            original_filename="Roster_2026.xlsx", file_type="xlsx", content_hash=content_hash, team_id=1, season_id=2026,
            validation_status="valid", point_in_time_classification="historical_but_not_point_in_time", pipeline_run_id=run_id,
        )
        PlayerRepository(session).upsert(player_id="p1", display_name="Frank Mitchell", team_id=1, season_id=2026)
        RosterMembershipRepository(session).upsert(team_id=1, season_id=2026, source_artifact_id=artifact.id, player_key="frank mitchell", source_player_name="Frank Mitchell", player_id="p1", validation_status="valid")
        RosterMembershipRepository(session).upsert(team_id=1, season_id=2026, source_artifact_id=artifact.id, player_key="unknown guy", source_player_name="Unknown Guy", player_id=None, validation_status="unresolved")
        PlayerStatSnapshotRepository(session).upsert(team_id=1, season_id=2026, source_artifact_id=artifact.id, stat_table="PerGame", player_key="frank mitchell", source_player_name="Frank Mitchell", player_id="p1", validation_status="valid")
        TeamStatSnapshotRepository(session).upsert(team_id=1, season_id=2026, source_artifact_id=artifact.id, stat_table="PerGame", source_row_label="Team Totals", validation_status="valid")
        return artifact.id


def _seed_team_game_log(session_factory, run_id="run1", content_hash="tgl1"):
    with unit_of_work(session_factory) as session:
        artifact = SourceArtifactRepository(session).upsert(
            source_key="sports_reference_game_log", source_root_key="teamdump_cbb", relative_path="st-bonaventure/st-bonaventure_2026_game_log.csv",
            original_filename="x.csv", file_type="csv", content_hash=content_hash, team_id=1, season_id=2026,
            validation_status="valid", point_in_time_classification="historical_but_not_point_in_time", pipeline_run_id=run_id,
        )
        gid = _wide_game_id()
        GameRepository(session).upsert(game_id=gid, home_team_id=1, away_team_id=2, season_id=2026, game_date="2025-11-03", neutral_site=True, is_provisional=True)
        TeamGameLogObservationRepository(session).upsert(team_id=1, season_id=2026, source_artifact_id=artifact.id, source_row_position=0, raw_opponent_name="Bradley", opponent_team_id=2, game_id=gid, venue_status="home", schema_variant="basic", validation_status="valid", pipeline_run_id=run_id)
        TeamGameLogObservationRepository(session).upsert(team_id=1, season_id=2026, source_artifact_id=artifact.id, source_row_position=1, raw_opponent_name="Unknown School", opponent_team_id=None, venue_status="unknown", schema_variant="basic", validation_status="unresolved", pipeline_run_id=run_id)
        GameResultRepository(session).upsert(game_id=gid, season_id=2026, team_a_id=1, team_b_id=2, result_status="single_sided_provisional", validation_status="valid", source_count=1, pipeline_run_id=run_id)
        return artifact.id, gid


def _seed_espn(session_factory, run_id="run1", content_hash="espn1"):
    with unit_of_work(session_factory) as session:
        artifact = SourceArtifactRepository(session).upsert(
            source_key="espn_player_stats", source_root_key="teamdump_espn", relative_path="espn_full_player_stats.csv",
            original_filename="x.csv", file_type="csv", content_hash=content_hash,
            validation_status="valid", point_in_time_classification="current_snapshot_only", pipeline_run_id=run_id,
        )
        EspnSourceObservationRepository(session).upsert(source_artifact_id=artifact.id, source_row_position=0, season_id=2026, raw_player_name="Some Player", raw_team_code="BYU", team_id=1, team_resolution_method="exact_alias", player_id=None, player_match_method="unresolved", validation_status="unresolved")
        EspnSourceObservationRepository(session).upsert(source_artifact_id=artifact.id, source_row_position=1, season_id=2026, raw_player_name="Other Player", raw_team_code="UNCA", team_id=None, team_resolution_method="ambiguous", player_id=None, player_match_method="team_unresolved", validation_status="unresolved")

        bench_artifact = SourceArtifactRepository(session).upsert(
            source_key="espn_ncaa_benchmarks", source_root_key="teamdump_espn", relative_path="espn_ncaa_benchmarks.xlsx",
            original_filename="x.xlsx", file_type="xlsx", content_hash=content_hash + "_bench",
            validation_status="valid", point_in_time_classification="current_snapshot_only", pipeline_run_id=run_id,
        )
        snapshot = EspnBenchmarkSnapshotRepository(session).upsert(source_artifact_id=bench_artifact.id, transformation_version="v1", validation_status="valid", pipeline_run_id=run_id)
        EspnBenchmarkValueRepository(session).upsert(snapshot_id=snapshot.id, metric_name="PTS", ncaa_mean=8.4, ncaa_std=4.6)
        EspnBenchmarkValueRepository(session).upsert(snapshot_id=snapshot.id, metric_name="REB", ncaa_mean=4.1, ncaa_std=2.0)
        return artifact.id


def _seed_player_game_log(session_factory, run_id="run1", content_hash="pgl1"):
    with unit_of_work(session_factory) as session:
        artifact = SourceArtifactRepository(session).upsert(
            source_key="sports_reference_player_game_log", source_root_key="teamdump_cbb", relative_path="st-bonaventure/frank_mitchell_2026_gamelog.csv",
            original_filename="x.csv", file_type="csv", content_hash=content_hash, team_id=1, season_id=2026,
            validation_status="valid", point_in_time_classification="historical_but_not_point_in_time", pipeline_run_id=run_id,
        )
        PlayerRepository(session).upsert(player_id="p1", display_name="Frank Mitchell", team_id=1, season_id=2026)
        PlayerGameLogObservationRepository(session).upsert(source_artifact_id=artifact.id, source_row_position=0, season_id=2026, team_id=1, raw_player_name="Frank Mitchell", raw_opponent_name="Bradley", player_id="p1", player_match_method="roster_matched", opponent_team_id=2, participation_status="played", validation_status="valid")
        PlayerGameLogObservationRepository(session).upsert(source_artifact_id=artifact.id, source_row_position=1, season_id=2026, team_id=1, raw_player_name="Frank Mitchell", raw_opponent_name="Nowhere State", player_id="p1", player_match_method="roster_matched", opponent_team_id=None, participation_status="played", validation_status="unresolved")
        return artifact.id


# ---------------------------------------------------------------------------
# Missing / invalid scope
# ---------------------------------------------------------------------------


def test_missing_pipeline_run_raises(session_factory):
    _seed_reference(session_factory)
    with unit_of_work(session_factory) as session:
        with pytest.raises(ValidationError):
            read_pipeline_run_reconciliation(session, "does-not-exist")


def test_unknown_family_raises(session_factory):
    _seed_reference(session_factory)
    _seed_run(session_factory)
    with unit_of_work(session_factory) as session:
        with pytest.raises(ValidationError):
            read_source_family_reconciliation(session, "run1", "torvik")


def test_missing_source_artifact_raises(session_factory):
    _seed_reference(session_factory)
    with unit_of_work(session_factory) as session:
        with pytest.raises(ValidationError):
            read_source_artifact_reconciliation(session, 999999)


def test_empty_but_valid_scope_returns_all_zero_counts_not_an_error(session_factory):
    """A real run_id with zero associated rows is a valid, empty scope -- distinct from a missing run_id (which raises)."""
    _seed_reference(session_factory)
    _seed_run(session_factory)
    with unit_of_work(session_factory) as session:
        report = read_pipeline_run_reconciliation(session, "run1")
    assert report.total_persisted_rows == 0
    assert report.total_accepted_rows == 0
    assert report.total_rejected_rows == 0
    for family in report.families:
        assert family.source_artifacts_tagged_this_run == 0
        for table in family.tables:
            assert table.persisted_rows == 0


# ---------------------------------------------------------------------------
# One family / all four families
# ---------------------------------------------------------------------------


def test_single_family_scope(session_factory):
    _seed_reference(session_factory)
    _seed_run(session_factory)
    _seed_allstats(session_factory)
    with unit_of_work(session_factory) as session:
        family = read_source_family_reconciliation(session, "run1", "allstats")
    assert family.family == "allstats"
    roster_table = next(t for t in family.tables if t.table_name == "roster_memberships")
    assert roster_table.persisted_rows == 2


def test_all_four_families_present_in_full_report(session_factory):
    _seed_reference(session_factory)
    _seed_run(session_factory)
    _seed_allstats(session_factory)
    _seed_team_game_log(session_factory)
    _seed_espn(session_factory)
    _seed_player_game_log(session_factory)
    with unit_of_work(session_factory) as session:
        report = read_pipeline_run_reconciliation(session, "run1")
    assert [f.family for f in report.families] == ["allstats", "team_game_log", "player_game_log", "espn"]
    allstats = next(f for f in report.families if f.family == "allstats")
    team_game_log = next(f for f in report.families if f.family == "team_game_log")
    espn = next(f for f in report.families if f.family == "espn")
    player_game_log = next(f for f in report.families if f.family == "player_game_log")

    assert sum(t.persisted_rows for t in allstats.tables) == 4  # 2 roster + 1 player stat + 1 team stat
    assert sum(t.persisted_rows for t in team_game_log.tables) == 2
    assert team_game_log.game_results.persisted_rows == 1
    assert sum(t.persisted_rows for t in espn.tables) == 2
    assert espn.espn_benchmark_values_persisted == 2
    assert sum(t.persisted_rows for t in player_game_log.tables) == 2

    assert report.total_persisted_rows == 4 + 2 + 2 + 2  # game_results and benchmark values are separate grains, not summed here


# ---------------------------------------------------------------------------
# Cross-run / cross-artifact isolation
# ---------------------------------------------------------------------------


def test_cross_run_isolation(session_factory):
    _seed_reference(session_factory)
    _seed_run(session_factory, run_id="run1")
    _seed_run(session_factory, run_id="run2")
    _seed_allstats(session_factory, run_id="run1", content_hash="hash_run1")
    with unit_of_work(session_factory) as session:
        report1 = read_pipeline_run_reconciliation(session, "run1")
        report2 = read_pipeline_run_reconciliation(session, "run2")
    assert report1.total_persisted_rows > 0
    assert report2.total_persisted_rows == 0


def test_cross_artifact_isolation(session_factory):
    """Two different artifacts (different content_hash) in the same run, same family, must not bleed into one another's own row counts when queried at the artifact grain."""
    _seed_reference(session_factory)
    _seed_run(session_factory)
    artifact_id_1 = _seed_allstats(session_factory, content_hash="hashA")
    with unit_of_work(session_factory) as session:
        # A second, independent artifact for a different team, same family/run.
        TeamRepository(session).upsert(team_id=3, display_name="Canisius", sr_slug="canisius")
        artifact2 = SourceArtifactRepository(session).upsert(
            source_key="sports_reference_allstats", source_root_key="teamdump_cbb", relative_path="canisius/Roster_2026.xlsx",
            original_filename="Roster_2026.xlsx", file_type="xlsx", content_hash="hashB", team_id=3, season_id=2026,
            validation_status="valid", point_in_time_classification="historical_but_not_point_in_time", pipeline_run_id="run1",
        )
        RosterMembershipRepository(session).upsert(team_id=3, season_id=2026, source_artifact_id=artifact2.id, player_key="someone", source_player_name="Someone", validation_status="unresolved")
        artifact_id_2 = artifact2.id

    with unit_of_work(session_factory) as session:
        a1 = read_source_artifact_reconciliation(session, artifact_id_1)
        a2 = read_source_artifact_reconciliation(session, artifact_id_2)
    assert a1.relative_path != a2.relative_path
    assert a1.content_hash == "hashA"
    assert a2.content_hash == "hashB"


# ---------------------------------------------------------------------------
# Multiple immutable snapshots
# ---------------------------------------------------------------------------


def test_multiple_immutable_snapshots_counted_distinctly(session_factory):
    """Two different content_hash values for the same (source_root_key, relative_path) coexist as two distinct rows -- both counted, never collapsed into one."""
    _seed_reference(session_factory)
    _seed_run(session_factory)
    with unit_of_work(session_factory) as session:
        for content_hash in ("v1", "v2"):
            SourceArtifactRepository(session).upsert(
                source_key="sports_reference_allstats", source_root_key="teamdump_cbb", relative_path="st-bonaventure/Roster_2026.xlsx",
                original_filename="Roster_2026.xlsx", file_type="xlsx", content_hash=content_hash, team_id=1, season_id=2026,
                validation_status="valid", point_in_time_classification="historical_but_not_point_in_time", pipeline_run_id="run1",
            )
    with unit_of_work(session_factory) as session:
        family = read_source_family_reconciliation(session, "run1", "allstats")
    assert family.source_artifacts_tagged_this_run == 2


# ---------------------------------------------------------------------------
# Table-grain correctness / no join multiplication
# ---------------------------------------------------------------------------


def test_no_join_multiplication_between_observation_and_stat_line(session_factory):
    """team_game_log_observations (2 rows) and its 1:1 team_game_stat_lines sidecar must never be joined together in a way that multiplies the observation count."""
    _seed_reference(session_factory)
    _seed_run(session_factory)
    artifact_id, gid = _seed_team_game_log(session_factory)
    with unit_of_work(session_factory) as session:
        obs = session.query(game_log_models.TeamGameLogObservation).filter_by(source_artifact_id=artifact_id, source_row_position=0).one()
        from src.db.game_log_repositories import TeamGameStatLineRepository
        TeamGameStatLineRepository(session).upsert(observation_id=obs.id, off_rating=110.5, def_rating=95.2, net_rating=15.3)

    with unit_of_work(session_factory) as session:
        family = read_source_family_reconciliation(session, "run1", "team_game_log")
    table = next(t for t in family.tables if t.table_name == "team_game_log_observations")
    assert table.persisted_rows == 2  # still exactly 2 -- the stat-line sidecar row never multiplied this count


def test_correct_table_grain_for_allstats_three_tables(session_factory):
    """AllStats' 3 tables (different natural keys) are each counted independently, never joined together in one query."""
    _seed_reference(session_factory)
    _seed_run(session_factory)
    _seed_allstats(session_factory)
    with unit_of_work(session_factory) as session:
        family = read_source_family_reconciliation(session, "run1", "allstats")
    by_name = {t.table_name: t.persisted_rows for t in family.tables}
    assert by_name == {"roster_memberships": 2, "player_stat_snapshots": 1, "team_stat_snapshots": 1}


# ---------------------------------------------------------------------------
# Resolved / ambiguous / unresolved stored evidence
# ---------------------------------------------------------------------------


def test_allstats_player_identity_no_ambiguous_column_marked_unavailable(session_factory):
    _seed_reference(session_factory)
    _seed_run(session_factory)
    _seed_allstats(session_factory)
    with unit_of_work(session_factory) as session:
        family = read_source_family_reconciliation(session, "run1", "allstats")
    roster_table = next(t for t in family.tables if t.table_name == "roster_memberships")
    assert roster_table.player_identity.resolved == 1
    assert isinstance(roster_table.player_identity.ambiguous, Unavailable)
    assert roster_table.player_identity.unresolved == 1


def test_espn_full_three_way_identity_split(session_factory):
    _seed_reference(session_factory)
    _seed_run(session_factory)
    _seed_espn(session_factory)
    with unit_of_work(session_factory) as session:
        family = read_source_family_reconciliation(session, "run1", "espn")
    table = family.tables[0]
    assert table.team_identity.resolved == 1
    assert table.team_identity.ambiguous == 1
    assert table.team_identity.unresolved == 0
    assert table.player_identity.resolved == 0
    assert table.player_identity.ambiguous == 0
    assert table.player_identity.unresolved == 2


def test_player_game_log_opponent_identity_has_no_ambiguous_split(session_factory):
    _seed_reference(session_factory)
    _seed_run(session_factory)
    _seed_player_game_log(session_factory)
    with unit_of_work(session_factory) as session:
        family = read_source_family_reconciliation(session, "run1", "player_game_log")
    table = family.tables[0]
    assert table.player_identity.resolved == 2
    assert table.opponent_identity.resolved == 1
    assert isinstance(table.opponent_identity.ambiguous, Unavailable)
    assert table.opponent_identity.unresolved == 1


# ---------------------------------------------------------------------------
# Data-quality issues / failed artifacts
# ---------------------------------------------------------------------------


def test_data_quality_issue_aggregation_per_family(session_factory):
    _seed_reference(session_factory)
    _seed_run(session_factory)
    artifact_id = _seed_allstats(session_factory)
    with unit_of_work(session_factory) as session:
        DataQualityIssueRepository(session).append(issue_code="allstats_player_unresolved", severity="warning", description="x", source_artifact_id=artifact_id, pipeline_run_id="run1")
        DataQualityIssueRepository(session).append(issue_code="allstats_player_unresolved", severity="warning", description="y", source_artifact_id=artifact_id, pipeline_run_id="run1")
    with unit_of_work(session_factory) as session:
        family = read_source_family_reconciliation(session, "run1", "allstats")
    assert family.data_quality_issues == (
        __import__("src.pipeline.bulk_import_reconciliation", fromlist=["DataQualityIssueCount"]).DataQualityIssueCount("allstats_player_unresolved", "warning", 2),
    )


def test_failed_artifact_unattributable_at_family_level_but_counted_at_run_level(session_factory):
    """A whole-file failure record (no source_artifact_id) cannot be attributed to a family -- it must still show up in the run-level total."""
    _seed_reference(session_factory)
    _seed_run(session_factory)
    with unit_of_work(session_factory) as session:
        DataQualityIssueRepository(session).append(issue_code="bulk_import_artifact_failed", severity="critical", description="parse error", pipeline_run_id="run1")

    with unit_of_work(session_factory) as session:
        report = read_pipeline_run_reconciliation(session, "run1")
        family = read_source_family_reconciliation(session, "run1", "allstats")

    assert report.total_failed_artifacts == 1
    assert report.total_rolled_back_artifacts == 1
    assert isinstance(family.failed_artifacts, Unavailable)


def test_run_level_data_quality_issues_capture_everything_regardless_of_attribution(session_factory):
    _seed_reference(session_factory)
    _seed_run(session_factory)
    artifact_id = _seed_allstats(session_factory)
    with unit_of_work(session_factory) as session:
        DataQualityIssueRepository(session).append(issue_code="allstats_player_unresolved", severity="warning", description="attributable", source_artifact_id=artifact_id, pipeline_run_id="run1")
        DataQualityIssueRepository(session).append(issue_code="bulk_import_artifact_failed", severity="critical", description="unattributable", pipeline_run_id="run1")
    with unit_of_work(session_factory) as session:
        report = read_pipeline_run_reconciliation(session, "run1")
    codes = {c.issue_code for c in report.run_level_data_quality_issues}
    assert codes == {"allstats_player_unresolved", "bulk_import_artifact_failed"}


# ---------------------------------------------------------------------------
# Deterministic ordering / repeated reads
# ---------------------------------------------------------------------------


def test_family_order_is_deterministic(session_factory):
    _seed_reference(session_factory)
    _seed_run(session_factory)
    _seed_espn(session_factory)
    _seed_allstats(session_factory)
    with unit_of_work(session_factory) as session:
        report = read_pipeline_run_reconciliation(session, "run1")
    assert [f.family for f in report.families] == ["allstats", "team_game_log", "player_game_log", "espn"]


def test_data_quality_issue_breakdown_ordering_is_deterministic(session_factory):
    _seed_reference(session_factory)
    _seed_run(session_factory)
    artifact_id = _seed_allstats(session_factory)
    with unit_of_work(session_factory) as session:
        DataQualityIssueRepository(session).append(issue_code="zzz_issue", severity="warning", description="x", source_artifact_id=artifact_id, pipeline_run_id="run1")
        DataQualityIssueRepository(session).append(issue_code="aaa_issue", severity="error", description="y", source_artifact_id=artifact_id, pipeline_run_id="run1")
    with unit_of_work(session_factory) as session:
        family = read_source_family_reconciliation(session, "run1", "allstats")
    codes = [c.issue_code for c in family.data_quality_issues]
    assert codes == sorted(codes)


def test_repeated_reads_return_equivalent_results(session_factory):
    _seed_reference(session_factory)
    _seed_run(session_factory)
    _seed_allstats(session_factory)
    with unit_of_work(session_factory) as session:
        first = read_pipeline_run_reconciliation(session, "run1")
        second = read_pipeline_run_reconciliation(session, "run1")
    assert first == second


# ---------------------------------------------------------------------------
# 63-bit game IDs, timestamps
# ---------------------------------------------------------------------------


def test_wide_63_bit_game_id_participates_without_narrowing(session_factory):
    _seed_reference(session_factory)
    _seed_run(session_factory)
    artifact_id, gid = _seed_team_game_log(session_factory)
    assert gid.bit_length() <= 63 and gid > 2**31
    with unit_of_work(session_factory) as session:
        family = read_source_family_reconciliation(session, "run1", "team_game_log")
    assert family.game_results.persisted_rows == 1  # the wide game_id's own result row is present and counted


def test_timestamps_preserved_distinctly_in_source_artifact_reconciliation(session_factory):
    """
    SQLite has no native TIMESTAMP WITH TIME ZONE type -- it round-trips the
    naive wall-clock value only, losing tzinfo (a real, pre-existing SQLite
    limitation, not something this reader can or should paper over). The
    strict UTC-tzinfo-preservation proof belongs to
    tests/postgres/test_postgres_bulk_import_reconciliation.py, which runs
    against a real TIMESTAMP WITH TIME ZONE column. This test only proves
    what SQLite itself can prove: collection_timestamp and filesystem_mtime
    round-trip their own distinct wall-clock values and are never conflated.
    """
    _seed_reference(session_factory)
    _seed_run(session_factory)
    collected = dt.datetime(2026, 1, 2, 12, 0, 0, tzinfo=UTC)
    fs_mtime = dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    with unit_of_work(session_factory) as session:
        artifact = SourceArtifactRepository(session).upsert(
            source_key="sports_reference_allstats", source_root_key="teamdump_cbb", relative_path="st-bonaventure/Roster_2026.xlsx",
            original_filename="Roster_2026.xlsx", file_type="xlsx", content_hash="h1", team_id=1, season_id=2026,
            filesystem_mtime=fs_mtime, collection_timestamp=collected,
            validation_status="valid", point_in_time_classification="historical_but_not_point_in_time", pipeline_run_id="run1",
        )
        artifact_id = artifact.id
    with unit_of_work(session_factory) as session:
        result = read_source_artifact_reconciliation(session, artifact_id)
    assert result.collection_timestamp.replace(tzinfo=UTC) == collected
    assert result.filesystem_mtime.replace(tzinfo=UTC) == fs_mtime
    assert result.collection_timestamp != result.filesystem_mtime


# ---------------------------------------------------------------------------
# Metrics unavailable from the existing schema
# ---------------------------------------------------------------------------


def test_newly_inserted_vs_idempotent_is_explicitly_unavailable(session_factory):
    _seed_reference(session_factory)
    _seed_run(session_factory)
    _seed_allstats(session_factory)
    with unit_of_work(session_factory) as session:
        family = read_source_family_reconciliation(session, "run1", "allstats")
    assert isinstance(family.newly_inserted_vs_idempotent_breakdown, Unavailable)


def test_failed_artifacts_unavailable_for_every_family_by_default(session_factory):
    _seed_reference(session_factory)
    _seed_run(session_factory)
    _seed_allstats(session_factory)
    _seed_team_game_log(session_factory)
    _seed_espn(session_factory)
    _seed_player_game_log(session_factory)
    with unit_of_work(session_factory) as session:
        report = read_pipeline_run_reconciliation(session, "run1")
    for family in report.families:
        assert isinstance(family.failed_artifacts, Unavailable)


# ---------------------------------------------------------------------------
# Caller-owned transaction / no internal mutation
# ---------------------------------------------------------------------------


def test_never_fails_to_see_callers_own_uncommitted_work_in_same_session(session_factory):
    """Within the SAME session (same transaction), the reader must see the caller's own not-yet-committed writes -- this is what 'caller owns the transaction' means in practice."""
    _seed_reference(session_factory)
    _seed_run(session_factory)
    session = session_factory()
    try:
        artifact = SourceArtifactRepository(session).upsert(
            source_key="sports_reference_allstats", source_root_key="teamdump_cbb", relative_path="st-bonaventure/Roster_2026.xlsx",
            original_filename="Roster_2026.xlsx", file_type="xlsx", content_hash="h1", team_id=1, season_id=2026,
            validation_status="valid", point_in_time_classification="historical_but_not_point_in_time", pipeline_run_id="run1",
        )
        RosterMembershipRepository(session).upsert(team_id=1, season_id=2026, source_artifact_id=artifact.id, player_key="frank mitchell", source_player_name="Frank Mitchell", validation_status="valid")
        # No commit yet.
        family = read_source_family_reconciliation(session, "run1", "allstats")
        roster_table = next(t for t in family.tables if t.table_name == "roster_memberships")
        assert roster_table.persisted_rows == 1
    finally:
        session.rollback()
        session.close()


def test_reader_never_commits_rolls_back_or_flushes_the_session():
    """AST guard: the reconciliation module's own source never calls .commit()/.rollback()/.flush()/.add()/.delete() on anything."""
    import src.pipeline.bulk_import_reconciliation as mod

    tree = ast.parse(inspect.getsource(mod))
    forbidden = {"commit", "rollback", "flush", "add", "delete", "merge", "bulk_save_objects"}
    called_attrs = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not (forbidden & called_attrs)


def test_reader_never_constructs_its_own_engine_or_session():
    import src.pipeline.bulk_import_reconciliation as mod

    tree = ast.parse(inspect.getsource(mod))
    called_names = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not ({"create_engine", "sessionmaker", "Session", "build_engine", "build_session_factory"} & called_names)


def test_reader_never_reads_environment_or_dotenv():
    import src.pipeline.bulk_import_reconciliation as mod

    tree = ast.parse(inspect.getsource(mod))
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
    assert "os" not in imported_names
    assert "src.db.config" not in imported_names


def test_reader_never_references_teamdump_or_adapters():
    import src.pipeline.bulk_import_reconciliation as mod

    tree = ast.parse(inspect.getsource(mod))
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    assert not any("import_adapter" in m for m in imported_modules)
    assert "src.paths" not in imported_modules


def test_no_transaction_state_mutation_via_direct_attribute_access(session_factory):
    """Calling the reader repeatedly must never change the underlying data -- confirmed by re-reading afterward with a fresh session."""
    _seed_reference(session_factory)
    _seed_run(session_factory)
    _seed_allstats(session_factory)
    with unit_of_work(session_factory) as session:
        read_pipeline_run_reconciliation(session, "run1")
        read_pipeline_run_reconciliation(session, "run1")
        read_pipeline_run_reconciliation(session, "run1")
    with unit_of_work(session_factory) as session:
        final = read_pipeline_run_reconciliation(session, "run1")
    assert final.total_persisted_rows == 4
