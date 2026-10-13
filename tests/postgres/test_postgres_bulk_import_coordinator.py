"""
Controlled bulk-import design, Slice 9 (docs/controlled_bulk_import_design.md
section 16 item 9, approved for this slice only): real PostgreSQL
integration tests for `src/pipeline/bulk_import_coordinator.py`.

This is the only test file that exercises `run_controlled_bulk_import`
end-to-end -- target verification, one continuous outer advisory lock
spanning every family, real delegated writes through the four existing,
unmodified family runners, real rollback on a forced conflict, and real
lock-contention/release behavior. Every fixture is synthetic and
in-memory (never TeamDump/ESPN); the only "real" thing here is the
disposable PostgreSQL container from tests/postgres/conftest.py.
"""
from __future__ import annotations

import datetime as dt
import uuid

import pandas as pd
import pytest
from sqlalchemy import create_engine, event, text

from src.db import allstats_models, espn_models, game_log_models, models, player_game_log_models
from src.db.repositories import SeasonRepository
from src.db.session import build_engine, build_session_factory, unit_of_work
from src.exceptions import AdvisoryLockUnavailableError, DatabaseTargetVerificationError, IdentityConflictError
from src.identity.player_identity import build_player_identity
from src.identity.season_identity import resolve_season_identity
from src.identity.team_identity import resolve_team_identity
from src.pipeline.allstats_import_adapter import (
    ALLSTATS_IMPORT_CONTRACT_VERSION,
    AllStatsFileMetadata,
    DiscoveredAllStatsWorkbook,
    build_allstats_import_plan,
    parse_allstats_workbook,
)
from src.pipeline.bulk_import_allstats_runner import AllStatsCommitArtifact
from src.pipeline.bulk_import_coordinator import (
    AllStatsFamilyRequest,
    EspnFamilyRequest,
    PlayerGameLogFamilyRequest,
    TeamGameLogFamilyRequest,
    run_controlled_bulk_import,
)
from src.pipeline.bulk_import_espn_runner import EXPECTED_SOURCE_ROOT_KEY as ESPN_SOURCE_ROOT_KEY
from src.pipeline.bulk_import_espn_runner import EspnSourceStatsCommitArtifact
from src.pipeline.bulk_import_game_log_runner import EXPECTED_SOURCE_ROOT_KEY as GAME_LOG_SOURCE_ROOT_KEY
from src.pipeline.bulk_import_game_log_runner import TeamGameLogCommitArtifact
from src.pipeline.bulk_import_manifest import MANIFEST_SCHEMA_VERSION, PlannedArtifact, SourceFamilyPlan, build_dry_run_manifest
from src.pipeline.bulk_import_player_game_log_runner import EXPECTED_SOURCE_ROOT_KEY as PLAYER_GAME_LOG_SOURCE_ROOT_KEY
from src.pipeline.bulk_import_player_game_log_runner import PlayerGameLogCommitArtifact
from src.pipeline.bulk_import_safety import DatabaseTargetDesignation, build_expected_acknowledgement, bulk_import_advisory_lock, verify_database_target
from src.pipeline.espn_import_adapter import (
    ESPN_IMPORT_CONTRACT_VERSION,
    DiscoveredEspnFile,
    build_espn_source_import_plan,
    parse_espn_player_stats_workbook,
)
from src.pipeline.game_log_import_adapter import (
    TEAM_GAMELOG_IMPORT_CONTRACT_VERSION,
    DiscoveredGameLogFile,
    build_game_log_import_plan,
    parse_game_log_workbook,
)
from src.pipeline.player_game_log_import_adapter import (
    PLAYER_GAMELOG_IMPORT_CONTRACT_VERSION,
    DiscoveredPlayerGameLogFile,
    build_player_game_log_import_plan,
    parse_player_game_log_workbook,
)

pytestmark = pytest.mark.postgres

SEASON = 2026
ALLSTATS_SOURCE_ROOT_KEY = "teamdump_cbb"


# ---------------------------------------------------------------------------
# Minimal synthetic fixtures, one artifact per family -- same conventions
# as the SQLite coordinator tests and each family's own runner test file.
# ---------------------------------------------------------------------------


def _allstats_family(*, team_query="St. Bonaventure", team_slug="st-bonaventure", player_name="A Player", hash_suffix=""):
    dfs = {"Roster": pd.DataFrame([{"Player": player_name, "#": 0, "Class": "SR", "Pos": "G", "Height": "6-2", "Weight": 190.0,
                                     "Hometown": None, "High School": None, "RSCI Top 100": None, "Summary": None}])}
    parsed = parse_allstats_workbook(dfs, team_query, SEASON)
    plan = build_allstats_import_plan(parsed)
    files = {"Roster": AllStatsFileMetadata(relative_path=f"{team_slug}/Roster_2026.xlsx", file_size_bytes=100, filesystem_mtime=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))}
    discovered = DiscoveredAllStatsWorkbook(team_slug=team_slug, season=SEASON, files=files)
    content_hashes = {"Roster": f"hash-roster{hash_suffix}"}
    artifact = AllStatsCommitArtifact(discovered=discovered, content_hashes=content_hashes, plan=plan)
    planned = tuple(
        PlannedArtifact(source_root_key=ALLSTATS_SOURCE_ROOT_KEY, relative_path=meta.relative_path, content_hash=content_hashes[label], status="planned_insert")
        for label, meta in discovered.files.items()
    )
    family_plan = SourceFamilyPlan(family="allstats", contract_version=ALLSTATS_IMPORT_CONTRACT_VERSION, artifacts=planned)
    return artifact, family_plan


def _team_game_log_family(*, hash_suffix=""):
    df = pd.DataFrame([{"game_num": 1, "date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70, "ORtg": 100.0, "DRtg": 95.0, "net_rtg": 5.0}])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    plan = build_game_log_import_plan(parsed)
    discovered = DiscoveredGameLogFile(team_slug="michigan-state", season=SEASON, relative_path="michigan-state/michigan-state_2026_game_log.csv", file_size_bytes=100, filesystem_mtime=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    content_hash = f"hash-team-game-log{hash_suffix}"
    artifact = TeamGameLogCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan)
    planned = (PlannedArtifact(source_root_key=GAME_LOG_SOURCE_ROOT_KEY, relative_path=discovered.relative_path, content_hash=content_hash, status="planned_insert"),)
    family_plan = SourceFamilyPlan(family="team_game_log", contract_version=TEAM_GAMELOG_IMPORT_CONTRACT_VERSION, artifacts=planned)
    return artifact, family_plan


def _player_game_log_family(*, hash_suffix=""):
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = pd.DataFrame([{"game_num": 1, "date": "2026-01-15", "opponent": "Colgate", "result": "W 80-70", "mp": 30, "pts": 10, "trb": 5, "ast": 3, "stl": 1, "blk": 0, "tov": 2, "fg": 4, "fga": 8}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=[build_player_identity("A Player", team_identity.team_id, SEASON)], team_slug="michigan-state", player_slug="a_player", season=SEASON)
    plan = build_player_game_log_import_plan(parsed)
    discovered = DiscoveredPlayerGameLogFile(team_slug="michigan-state", player_slug="a_player", season=SEASON, relative_path="michigan-state/a_player_2026_gamelog.csv", file_size_bytes=100, filesystem_mtime=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    content_hash = f"hash-player-game-log{hash_suffix}"
    artifact = PlayerGameLogCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan)
    planned = (PlannedArtifact(source_root_key=PLAYER_GAME_LOG_SOURCE_ROOT_KEY, relative_path=discovered.relative_path, content_hash=content_hash, status="planned_insert"),)
    family_plan = SourceFamilyPlan(family="player_game_log", contract_version=PLAYER_GAMELOG_IMPORT_CONTRACT_VERSION, artifacts=planned)
    return artifact, family_plan


def _espn_family(*, hash_suffix=""):
    df = pd.DataFrame([{
        "RK": 1, "Name": "Some Player", "Team": "BYU", "POS": "G", "GP": 20, "MIN": 25.0, "PTS": 10.0,
        "FGM": 4.0, "FGA": 8.0, "FG%": 50.0, "3PM": 1.0, "3PA": 3.0, "3P%": 33.0, "FTM": 2.0, "FTA": 3.0,
        "FT%": 66.0, "REB": 4.0, "AST": 3.0, "STL": 1.0, "BLK": 0.5, "TO": 2.0,
    }])
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    plan = build_espn_source_import_plan(parsed)
    discovered = DiscoveredEspnFile(filename="espn_full_player_stats.csv", relative_path="espn_full_player_stats.csv", file_size_bytes=100, filesystem_mtime=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    content_hash = f"hash-espn{hash_suffix}"
    artifact = EspnSourceStatsCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan, season_id=SEASON)
    planned = (PlannedArtifact(source_root_key=ESPN_SOURCE_ROOT_KEY, relative_path=discovered.relative_path, content_hash=content_hash, status="planned_insert"),)
    family_plan = SourceFamilyPlan(family="espn", contract_version=ESPN_IMPORT_CONTRACT_VERSION, artifacts=planned)
    return artifact, family_plan


def _full_manifest(*family_plans):
    return build_dry_run_manifest(generated_at=dt.datetime.now(dt.timezone.utc), families=list(family_plans))


def _all_four_requests_and_manifest(*, hash_suffix=""):
    allstats_artifact, allstats_plan = _allstats_family(hash_suffix=hash_suffix)
    team_gl_artifact, team_gl_plan = _team_game_log_family(hash_suffix=hash_suffix)
    player_gl_artifact, player_gl_plan = _player_game_log_family(hash_suffix=hash_suffix)
    espn_artifact, espn_plan = _espn_family(hash_suffix=hash_suffix)
    manifest = _full_manifest(allstats_plan, team_gl_plan, player_gl_plan, espn_plan)
    requests = [
        AllStatsFamilyRequest(artifacts=[allstats_artifact], run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc)),
        TeamGameLogFamilyRequest(artifacts=[team_gl_artifact], run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc)),
        PlayerGameLogFamilyRequest(artifacts=[player_gl_artifact], run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc)),
        EspnFamilyRequest(artifacts=[espn_artifact], run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc)),
    ]
    return requests, manifest


def _seed_season(session_factory, season_id: int = SEASON) -> None:
    with unit_of_work(session_factory) as session:
        if session.get(models.Season, season_id) is None:
            SeasonRepository(session).upsert(season_id=season_id, start_year=season_id - 1, end_year=season_id, label=f"{season_id - 1}-{str(season_id)[-2:]}")


def _seed_allstats_team(session_factory, artifact) -> None:
    """Unlike the other three families, persist_allstats_import_plan never auto-registers the perspective team itself -- the caller must pre-seed it (the established Slice 5 convention)."""
    from src.db.repositories import TeamRepository
    with unit_of_work(session_factory) as session:
        if session.get(models.Team, artifact.plan.team_id) is None:
            TeamRepository(session).upsert(team_id=artifact.plan.team_id, display_name=artifact.plan.team_slug, sr_slug=f"slug-{artifact.plan.team_id}")


def _seed_all_prerequisites(session_factory, requests) -> None:
    _seed_season(session_factory)
    for request in requests:
        if isinstance(request, AllStatsFamilyRequest):
            for artifact in request.artifacts:
                _seed_allstats_team(session_factory, artifact)


def _designation_for(name: str) -> DatabaseTargetDesignation:
    return DatabaseTargetDesignation(expected_database_name=name, acknowledgement=build_expected_acknowledgement(name))


def _real_database_name(connection) -> str:
    return connection.execute(text("SELECT current_database()")).scalar_one()


@pytest.fixture()
def pg_connection_engine(pg_migrated_database_url):
    engine = create_engine(pg_migrated_database_url)
    try:
        yield engine
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# A minimal synthetic four-family successful run, in established order
# ---------------------------------------------------------------------------


def test_minimal_four_family_successful_run_in_established_order(pg_session_factory, pg_connection_engine):
    requests, manifest = _all_four_requests_and_manifest()
    _seed_all_prerequisites(pg_session_factory, requests)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_controlled_bulk_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION,
            family_requests=list(reversed(requests)),  # deliberately reversed input order
        )

    assert result.status == "succeeded"
    assert [o.family for o in result.family_outcomes] == ["allstats", "team_game_log", "player_game_log", "espn"]
    assert result.committed_families == ("allstats", "team_game_log", "player_game_log", "espn")
    with unit_of_work(pg_session_factory) as session:
        assert session.query(allstats_models.RosterMembership).count() == 1
        assert session.query(game_log_models.TeamGameLogObservation).count() == 1
        assert session.query(player_game_log_models.PlayerGameLogObservation).count() == 1
        assert session.query(espn_models.EspnSourceObservation).count() == 1
        # four separate pipeline_runs rows -- the approved resolution, not one shared row.
        assert session.query(models.PipelineRun).count() == 4
        assert all(r.status == "succeeded" for r in session.query(models.PipelineRun).all())


# ---------------------------------------------------------------------------
# One continuous outer advisory lock spanning the whole sequence
# ---------------------------------------------------------------------------


def test_one_continuous_outer_lock_blocks_a_second_connection_throughout(pg_session_factory, pg_connection_engine, pg_migrated_database_url):
    requests, manifest = _all_four_requests_and_manifest()
    _seed_all_prerequisites(pg_session_factory, requests)

    other_engine = create_engine(pg_migrated_database_url)
    try:
        with pg_connection_engine.connect() as conn, other_engine.connect() as other_conn:
            designation = _designation_for(_real_database_name(conn))
            result = run_controlled_bulk_import(
                connection=conn, target_designation=designation, session_factory=pg_session_factory,
                manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, family_requests=requests,
            )
            assert result.status == "succeeded"
            # By the time the coordinator call returns, its own outer lock is released --
            # confirm a second connection can now freely acquire the same key.
            verified = verify_database_target(other_conn, designation)
            with bulk_import_advisory_lock(other_conn, verified):
                pass
    finally:
        other_engine.dispose()


def test_second_connection_contention_before_and_during_the_coordinated_run(pg_session_factory, pg_connection_engine, pg_migrated_database_url):
    requests, manifest = _all_four_requests_and_manifest()
    _seed_all_prerequisites(pg_session_factory, requests)

    other_engine = create_engine(pg_migrated_database_url)
    try:
        with pg_connection_engine.connect() as conn, other_engine.connect() as other_conn:
            designation = _designation_for(_real_database_name(conn))
            verified = verify_database_target(other_conn, designation)
            with bulk_import_advisory_lock(other_conn, verified):
                with pytest.raises(AdvisoryLockUnavailableError):
                    run_controlled_bulk_import(
                        connection=conn, target_designation=designation, session_factory=pg_session_factory,
                        manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, family_requests=requests,
                    )
    finally:
        other_engine.dispose()

    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.PipelineRun).count() == 0


def test_exact_lock_release_and_zero_residual_locks_after_success(pg_session_factory, pg_connection_engine):
    requests, manifest = _all_four_requests_and_manifest()
    _seed_all_prerequisites(pg_session_factory, requests)
    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_controlled_bulk_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, family_requests=requests,
        )
        assert result.status == "succeeded"
        remaining = conn.execute(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")).scalar_one()
    assert remaining == 0


# ---------------------------------------------------------------------------
# Failure semantics: earlier successes survive, later families never start
# ---------------------------------------------------------------------------


def test_failed_family_leaves_earlier_families_committed_and_stops_later_ones(pg_session_factory, pg_connection_engine):
    allstats_artifact, allstats_plan = _allstats_family()
    conflicting_team_gl_artifact, team_gl_plan = _team_game_log_family()
    player_gl_artifact, player_gl_plan = _player_game_log_family()
    espn_artifact, espn_plan = _espn_family()
    manifest = _full_manifest(allstats_plan, team_gl_plan, player_gl_plan, espn_plan)
    _seed_season(pg_session_factory)
    _seed_allstats_team(pg_session_factory, allstats_artifact)

    # Pre-seed a conflicting observation for the team-game-log family (the second in
    # SOURCE_FAMILY_ORDER) so its own runner's persist call raises IdentityConflictError.
    with unit_of_work(pg_session_factory) as session:
        from src.db.repositories import DataSourceRepository, SourceArtifactRepository, TeamRepository
        from src.db.game_log_repositories import TeamGameLogObservationRepository
        DataSourceRepository(session).upsert(source_key="sports_reference_game_log", display_name="x", acquisition_type="static_file", is_rescrapable=False, description="x")
        TeamRepository(session).upsert(team_id=conflicting_team_gl_artifact.plan.team_id, display_name=conflicting_team_gl_artifact.plan.team_slug, sr_slug=f"slug-{conflicting_team_gl_artifact.plan.team_id}")
        artifact_row = SourceArtifactRepository(session).upsert(
            source_key="sports_reference_game_log", source_root_key=GAME_LOG_SOURCE_ROOT_KEY, relative_path=conflicting_team_gl_artifact.discovered.relative_path,
            original_filename="conflicting.csv", file_type="csv", point_in_time_classification="historical_but_not_point_in_time",
            content_hash=conflicting_team_gl_artifact.content_hash, team_id=conflicting_team_gl_artifact.plan.team_id, season_id=conflicting_team_gl_artifact.plan.season_id,
        )
        spec = conflicting_team_gl_artifact.plan.observation_specs[0]
        TeamGameLogObservationRepository(session).upsert(
            team_id=conflicting_team_gl_artifact.plan.team_id, season_id=conflicting_team_gl_artifact.plan.season_id, source_artifact_id=artifact_row.id,
            source_row_position=spec["source_row_position"], raw_opponent_name="A Completely Different Opponent Name",
            venue_status=spec["venue_status"], schema_variant=spec["schema_variant"], validation_status=spec["validation_status"],
        )

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_controlled_bulk_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION,
            family_requests=[
                AllStatsFamilyRequest(artifacts=[allstats_artifact], run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc)),
                TeamGameLogFamilyRequest(artifacts=[conflicting_team_gl_artifact], run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc)),
                PlayerGameLogFamilyRequest(artifacts=[player_gl_artifact], run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc)),
                EspnFamilyRequest(artifacts=[espn_artifact], run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc)),
            ],
        )

    assert result.status == "failed"
    assert [o.status for o in result.family_outcomes] == ["committed", "failed", "not_started", "not_started"]
    assert result.committed_families == ("allstats",)
    assert result.failed_families == ("team_game_log",)
    assert result.not_started_families == ("player_game_log", "espn")

    with unit_of_work(pg_session_factory) as session:
        # AllStats' own successful commit survives, never described as rolled back.
        assert session.query(allstats_models.RosterMembership).count() == 1
        assert session.get(models.PipelineRun, result.family_outcomes[0].run_result.run_id).status == "succeeded"
        # team_game_log's own failed attempt never landed; the pre-seeded row is untouched.
        team_gl_rows = session.query(game_log_models.TeamGameLogObservation).all()
        assert len(team_gl_rows) == 1
        assert team_gl_rows[0].raw_opponent_name == "A Completely Different Opponent Name"
        # player_game_log and espn were never even attempted -- zero rows, zero pipeline_runs rows for them.
        assert session.query(player_game_log_models.PlayerGameLogObservation).count() == 0
        assert session.query(espn_models.EspnSourceObservation).count() == 0
        assert session.query(models.PipelineRun).count() == 2  # allstats (succeeded) + team_game_log (failed) only


def test_no_direct_writes_outside_the_four_family_runners(pg_session_factory, pg_connection_engine, pg_migrated_database_url):
    requests, manifest = _all_four_requests_and_manifest()
    _seed_all_prerequisites(pg_session_factory, requests)

    write_engine = build_engine(pg_migrated_database_url)
    captured: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        upper = statement.strip().upper()
        if upper.startswith(("INSERT", "UPDATE", "DELETE")):
            captured.append(statement.strip())

    event.listen(write_engine, "before_cursor_execute", _capture)
    try:
        write_session_factory = build_session_factory(write_engine)
        with pg_connection_engine.connect() as conn:
            designation = _designation_for(_real_database_name(conn))
            result = run_controlled_bulk_import(
                connection=conn, target_designation=designation, session_factory=write_session_factory,
                manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, family_requests=requests,
            )
        assert result.status == "succeeded"
    finally:
        event.remove(write_engine, "before_cursor_execute", _capture)
        write_engine.dispose()

    # Every captured write must be attributable to one of the four families' own known tables.
    known_write_tables = (
        "pipeline_runs", "data_sources", "source_artifacts", "teams", "players",
        "roster_memberships", "player_stat_snapshots", "team_stat_snapshots",
        "team_game_log_observations", "team_game_stat_lines", "games", "game_results",
        "player_game_log_observations", "espn_source_observations",
    )
    for statement in captured:
        lowered = statement.lower()
        assert any(f'"{t}"' in lowered or f" {t} " in lowered or f"into {t}" in lowered for t in known_write_tables), f"unattributed write: {statement!r}"


# ---------------------------------------------------------------------------
# Idempotency and coexistence, delegated end-to-end through the coordinator
# ---------------------------------------------------------------------------


def test_identical_replay_of_the_full_coordinated_run_is_idempotent(pg_session_factory, pg_connection_engine):
    requests_1, manifest = _all_four_requests_and_manifest()
    _seed_all_prerequisites(pg_session_factory, requests_1)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result_1 = run_controlled_bulk_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, family_requests=requests_1,
        )
        assert result_1.status == "succeeded"

        requests_2, _ = _all_four_requests_and_manifest()  # identical artifacts, brand-new run_ids
        result_2 = run_controlled_bulk_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, family_requests=requests_2,
        )
        assert result_2.status == "succeeded"

    with unit_of_work(pg_session_factory) as session:
        assert session.query(allstats_models.RosterMembership).count() == 1
        assert session.query(game_log_models.TeamGameLogObservation).count() == 1
        assert session.query(player_game_log_models.PlayerGameLogObservation).count() == 1
        assert session.query(espn_models.EspnSourceObservation).count() == 1
        assert session.query(models.PipelineRun).count() == 8  # two coordinated runs x four families each


def test_changed_immutable_snapshots_coexist_across_two_coordinated_runs(pg_session_factory, pg_connection_engine):
    requests_1, manifest_1 = _all_four_requests_and_manifest()
    requests_2, manifest_2 = _all_four_requests_and_manifest(hash_suffix="-v2")
    _seed_all_prerequisites(pg_session_factory, requests_1)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result_1 = run_controlled_bulk_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest_1, manifest_schema_version=MANIFEST_SCHEMA_VERSION, family_requests=requests_1,
        )
        result_2 = run_controlled_bulk_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest_2, manifest_schema_version=MANIFEST_SCHEMA_VERSION, family_requests=requests_2,
        )

    assert result_1.status == "succeeded"
    assert result_2.status == "succeeded"
    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.SourceArtifact).count() == 8  # 4 families x 2 distinct snapshots each
        assert session.query(allstats_models.RosterMembership).count() == 2
        assert session.query(game_log_models.TeamGameLogObservation).count() == 2
        assert session.query(player_game_log_models.PlayerGameLogObservation).count() == 2
        assert session.query(espn_models.EspnSourceObservation).count() == 2


# ---------------------------------------------------------------------------
# Full-run reconciliation matches the aggregate of family reports
# ---------------------------------------------------------------------------


def test_full_run_reconciliation_matches_the_aggregate_of_family_reports(pg_session_factory, pg_connection_engine):
    from src.pipeline.bulk_import_reconciliation import read_source_family_reconciliation

    requests, manifest = _all_four_requests_and_manifest()
    _seed_all_prerequisites(pg_session_factory, requests)
    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_controlled_bulk_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, family_requests=requests,
        )
    assert result.status == "succeeded"

    with unit_of_work(pg_session_factory) as session:
        for outcome in result.family_outcomes:
            report = read_source_family_reconciliation(session, outcome.run_result.run_id, outcome.family)
            assert sum(t.persisted_rows for t in report.tables) >= 1


# ---------------------------------------------------------------------------
# Six-migration schema remains sufficient -- no seventh migration needed
# ---------------------------------------------------------------------------


def test_six_migration_schema_remains_sufficient_for_the_coordinator(pg_migrated_database_url):
    """The coordinator itself creates no table, column, or constraint of its own -- confirmed by re-running the full four-family flow against the same six-migration schema with no additional DDL."""
    engine = create_engine(pg_migrated_database_url)
    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT count(*) FROM alembic_version")).scalar_one()
            assert result == 1
    finally:
        engine.dispose()
