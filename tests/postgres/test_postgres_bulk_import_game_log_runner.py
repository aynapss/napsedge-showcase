"""
Controlled bulk-import design, Slice 6 (docs/controlled_bulk_import_design.md
section 16 item 6, approved for this slice only): real PostgreSQL
integration tests for `src/pipeline/bulk_import_game_log_runner.py`.

This is the only test file that exercises `run_team_game_log_committed_import`
end-to-end -- target verification, advisory locking, real persisted
writes through the existing, unmodified `persist_game_log_import_plan`,
real rollback on a forced conflict, real mirrored games/game_results
convergence, and real pipeline-run finalization. Every fixture is
synthetic and in-memory (never TeamDump); the only "real" thing here is
the disposable PostgreSQL container from tests/postgres/conftest.py.
"""
from __future__ import annotations

import datetime as dt
import uuid

import pandas as pd
import pytest
from sqlalchemy import create_engine, event, text

from src.db import game_log_models, models
from src.db.repositories import SeasonRepository
from src.db.session import build_engine, build_session_factory, unit_of_work
from src.exceptions import AdvisoryLockUnavailableError, DatabaseTargetVerificationError, IdentityConflictError, ValidationError
from src.pipeline.bulk_import_game_log_runner import (
    EXPECTED_SOURCE_ROOT_KEY,
    FAILED_ARTIFACT_ISSUE_CODE,
    TeamGameLogCommitArtifact,
    run_team_game_log_committed_import,
)
from src.pipeline.bulk_import_manifest import MANIFEST_SCHEMA_VERSION, PlannedArtifact, SourceFamilyPlan, build_dry_run_manifest
from src.pipeline.bulk_import_safety import DatabaseTargetDesignation, build_expected_acknowledgement, bulk_import_advisory_lock, verify_database_target
from src.pipeline.game_log_import_adapter import (
    TEAM_GAMELOG_IMPORT_CONTRACT_VERSION,
    DiscoveredGameLogFile,
    build_game_log_import_plan,
    parse_game_log_workbook,
)

pytestmark = pytest.mark.postgres

SEASON = 2026


# ---------------------------------------------------------------------------
# Synthetic fixture builders (same convention as the SQLite runner tests
# and tests/regression/test_game_log_import_adapter.py -- never TeamDump)
# ---------------------------------------------------------------------------


def _basic_df(rows: list[dict]) -> pd.DataFrame:
    base = {"game_num": 1, "date": None, "opponent": None, "pts_scored": None, "pts_allowed": None, "ORtg": 100.0, "DRtg": 95.0, "net_rtg": 5.0}
    return pd.DataFrame([{**base, **r} for r in rows])


def _discovered_and_hash(team_slug: str, *, season: int = SEASON, hash_suffix: str = "") -> tuple[DiscoveredGameLogFile, str]:
    discovered = DiscoveredGameLogFile(
        team_slug=team_slug, season=season, relative_path=f"{team_slug}/{team_slug}_{season}_game_log.csv",
        file_size_bytes=100, filesystem_mtime=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
    )
    return discovered, f"hash-{team_slug}{hash_suffix}"


def _build_artifact(*, team_slug: str, team_query: str, rows: list[dict], season: int = SEASON, hash_suffix: str = "") -> TeamGameLogCommitArtifact:
    df = _basic_df(rows)
    parsed = parse_game_log_workbook(df, team_query, season)
    plan = build_game_log_import_plan(parsed)
    discovered, content_hash = _discovered_and_hash(team_slug, season=season, hash_suffix=hash_suffix)
    return TeamGameLogCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan)


def _manifest_for(*artifacts: TeamGameLogCommitArtifact, contract_version: str | None = None) -> object:
    planned = [
        PlannedArtifact(source_root_key=EXPECTED_SOURCE_ROOT_KEY, relative_path=artifact.discovered.relative_path, content_hash=artifact.content_hash, status="planned_insert")
        for artifact in artifacts
    ]
    family_plan = SourceFamilyPlan(family="team_game_log", contract_version=contract_version or TEAM_GAMELOG_IMPORT_CONTRACT_VERSION, artifacts=tuple(planned))
    return build_dry_run_manifest(generated_at=dt.datetime.now(dt.timezone.utc), families=[family_plan])


def _seed_season(session_factory, artifact: TeamGameLogCommitArtifact) -> None:
    """persist_game_log_import_plan registers every team itself (perspective + resolved opponents) -- only Season needs pre-seeding, matching tests/regression/test_game_log_import_adapter.py's own convention."""
    plan = artifact.plan
    with unit_of_work(session_factory) as session:
        if plan.season_id is not None and session.get(models.Season, plan.season_id) is None:
            SeasonRepository(session).upsert(season_id=plan.season_id, start_year=plan.season_id - 1, end_year=plan.season_id, label=f"{plan.season_id - 1}-{str(plan.season_id)[-2:]}")


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
# Valid imports
# ---------------------------------------------------------------------------


def test_valid_one_artifact_import_single_sided(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(team_slug="test-team", team_query="Michigan State", rows=[{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    _seed_season(pg_session_factory, artifact)
    manifest = _manifest_for(artifact)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_team_game_log_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )

    assert result.status == "succeeded"
    assert result.committed_count == 1
    with unit_of_work(pg_session_factory) as session:
        assert session.query(game_log_models.TeamGameLogObservation).count() == 1
        gr = session.query(game_log_models.GameResult).one()
        assert gr.result_status == "single_sided_provisional"
        assert gr.source_count == 1
        assert session.get(models.PipelineRun, result.run_id).status == "succeeded"


def test_valid_multiple_artifact_import_and_deterministic_order(pg_session_factory, pg_connection_engine):
    artifact_b = _build_artifact(team_slug="team-b", team_query="St. Bonaventure", rows=[{"date": "2026-01-15", "opponent": "Bucknell", "pts_scored": 70, "pts_allowed": 65}])
    artifact_a = _build_artifact(team_slug="team-a", team_query="Michigan State", rows=[{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    _seed_season(pg_session_factory, artifact_a)
    _seed_season(pg_session_factory, artifact_b)
    manifest = _manifest_for(artifact_a, artifact_b)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_team_game_log_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION,
            artifacts=[artifact_b, artifact_a],  # deliberately reversed input order
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )

    assert result.status == "succeeded"
    assert [o.team_slug for o in result.artifact_outcomes] == ["team-a", "team-b"]
    with unit_of_work(pg_session_factory) as session:
        assert session.query(game_log_models.TeamGameLogObservation).count() == 2


# ---------------------------------------------------------------------------
# Mirrored games/game_results convergence -- the core Slice 6 proof
# ---------------------------------------------------------------------------


def test_mirrored_observations_converge_on_one_reconciled_game(pg_session_factory, pg_connection_engine):
    msu = _build_artifact(team_slug="michigan-state", team_query="Michigan State", rows=[{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    colgate = _build_artifact(team_slug="colgate", team_query="Colgate", rows=[{"date": "2026-01-15", "opponent": "Michigan State", "pts_scored": 70, "pts_allowed": 80}])
    _seed_season(pg_session_factory, msu)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))

        result_1 = run_team_game_log_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=_manifest_for(msu), manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[msu],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
        assert result_1.status == "succeeded"

        with unit_of_work(pg_session_factory) as session:
            single_sided = session.query(game_log_models.GameResult).one()
            game_id = single_sided.game_id
            assert single_sided.result_status == "single_sided_provisional"
            assert single_sided.source_count == 1

        result_2 = run_team_game_log_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=_manifest_for(colgate), manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[colgate],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
        assert result_2.status == "succeeded"

    with unit_of_work(pg_session_factory) as session:
        # SAME game_id, SAME row (no duplicate game or game-result created) -- now mirrored.
        assert session.query(game_log_models.GameResult).count() == 1
        mirrored = session.query(game_log_models.GameResult).filter(game_log_models.GameResult.game_id == game_id).one()
        assert mirrored.result_status == "reconciled"
        assert mirrored.source_count == 2
        assert mirrored.team_a_observation_id is not None
        assert mirrored.team_b_observation_id is not None
        assert mirrored.team_a_score is not None and mirrored.team_b_score is not None
        assert session.query(models.Game).filter(models.Game.game_id == game_id).count() == 1


def test_conflicting_mirrored_evidence_remains_explicit(pg_session_factory, pg_connection_engine):
    msu = _build_artifact(team_slug="michigan-state", team_query="Michigan State", rows=[{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    colgate = _build_artifact(team_slug="colgate", team_query="Colgate", rows=[{"date": "2026-01-15", "opponent": "Michigan State", "pts_scored": 99, "pts_allowed": 80}])  # disagreeing score
    _seed_season(pg_session_factory, msu)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result_1 = run_team_game_log_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=_manifest_for(msu), manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[msu],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
        assert result_1.status == "succeeded"

        result_2 = run_team_game_log_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=_manifest_for(colgate), manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[colgate],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
        assert result_2.status == "succeeded"  # persisting a conflicting mirrored observation is not itself a runner failure

    with unit_of_work(pg_session_factory) as session:
        gr = session.query(game_log_models.GameResult).one()
        assert gr.result_status == "conflicting"
        assert gr.source_count == 2
        assert gr.team_a_score is None and gr.team_b_score is None  # scores withheld on conflict, per the adapter's own contract
        issues = session.query(models.DataQualityIssue).filter(models.DataQualityIssue.game_id == gr.game_id).all()
        assert any("mirrored" in i.description.lower() for i in issues)


def test_identical_replay_of_a_mirrored_result_is_idempotent(pg_session_factory, pg_connection_engine):
    msu = _build_artifact(team_slug="michigan-state", team_query="Michigan State", rows=[{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    colgate = _build_artifact(team_slug="colgate", team_query="Colgate", rows=[{"date": "2026-01-15", "opponent": "Michigan State", "pts_scored": 70, "pts_allowed": 80}])
    _seed_season(pg_session_factory, msu)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        for artifact in (msu, colgate):
            result = run_team_game_log_committed_import(
                connection=conn, target_designation=designation, session_factory=pg_session_factory,
                manifest=_manifest_for(artifact), manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
                run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
            )
            assert result.status == "succeeded"

        # Replay Colgate's own identical file again under a brand-new run_id -- must stay idempotent
        # (source_count stays 2, result_status stays reconciled, no duplicate row).
        replay_result = run_team_game_log_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=_manifest_for(colgate), manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[colgate],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
        assert replay_result.status == "succeeded"

    with unit_of_work(pg_session_factory) as session:
        assert session.query(game_log_models.GameResult).count() == 1
        assert session.query(game_log_models.TeamGameLogObservation).count() == 2  # not duplicated
        assert session.query(models.SourceArtifact).count() == 2  # one per team's own file, not duplicated
        gr = session.query(game_log_models.GameResult).one()
        assert gr.result_status == "reconciled"
        assert gr.source_count == 2


def test_different_immutable_snapshot_coexists_alongside_the_original(pg_session_factory, pg_connection_engine):
    original = _build_artifact(team_slug="test-team", team_query="Michigan State", rows=[{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    changed = _build_artifact(team_slug="test-team", team_query="Michigan State", rows=[{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}], hash_suffix="-v2")
    _seed_season(pg_session_factory, original)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result_1 = run_team_game_log_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=_manifest_for(original), manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[original],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
        result_2 = run_team_game_log_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=_manifest_for(changed), manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[changed],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )

    assert result_1.status == "succeeded"
    assert result_2.status == "succeeded"
    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.SourceArtifact).filter(models.SourceArtifact.relative_path == original.discovered.relative_path).count() == 2
        assert session.query(game_log_models.TeamGameLogObservation).count() == 2  # two coexisting snapshots' worth of rows


# ---------------------------------------------------------------------------
# Target verification / advisory lock, before any write
# ---------------------------------------------------------------------------


def test_target_verification_failure_before_any_write(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(team_slug="test-team", team_query="Michigan State", rows=[{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    _seed_season(pg_session_factory, artifact)
    manifest = _manifest_for(artifact)

    with pg_connection_engine.connect() as conn:
        real_name = _real_database_name(conn)
        wrong_designation = _designation_for(real_name + "_wrong")
        with pytest.raises(DatabaseTargetVerificationError):
            run_team_game_log_committed_import(
                connection=conn, target_designation=wrong_designation, session_factory=pg_session_factory,
                manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
                run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
            )

    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.PipelineRun).count() == 0
        assert session.query(game_log_models.TeamGameLogObservation).count() == 0


def test_advisory_lock_contention_before_any_write(pg_session_factory, pg_connection_engine, pg_migrated_database_url):
    artifact = _build_artifact(team_slug="test-team", team_query="Michigan State", rows=[{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    _seed_season(pg_session_factory, artifact)
    manifest = _manifest_for(artifact)

    other_engine = create_engine(pg_migrated_database_url)
    try:
        with pg_connection_engine.connect() as conn, other_engine.connect() as other_conn:
            designation = _designation_for(_real_database_name(conn))
            verified = verify_database_target(other_conn, designation)
            with bulk_import_advisory_lock(other_conn, verified):
                with pytest.raises(AdvisoryLockUnavailableError):
                    run_team_game_log_committed_import(
                        connection=conn, target_designation=designation, session_factory=pg_session_factory,
                        manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
                        run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
                    )
    finally:
        other_engine.dispose()

    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.PipelineRun).count() == 0


# ---------------------------------------------------------------------------
# Failure / rollback behavior
# ---------------------------------------------------------------------------


def test_failed_artifact_rolls_back_leaves_earlier_committed_and_stops_processing(pg_session_factory, pg_connection_engine):
    good = _build_artifact(team_slug="team-a", team_query="Michigan State", rows=[{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    conflicting = _build_artifact(team_slug="team-b", team_query="St. Bonaventure", rows=[{"date": "2026-01-20", "opponent": "Bucknell", "pts_scored": 65, "pts_allowed": 60}])
    never_reached = _build_artifact(team_slug="team-c", team_query="Colgate", rows=[{"date": "2026-01-25", "opponent": "Army", "pts_scored": 55, "pts_allowed": 50}])
    _seed_season(pg_session_factory, good)
    _seed_season(pg_session_factory, conflicting)
    manifest = _manifest_for(good, conflicting, never_reached)

    # Pre-seed a conflicting observation row under `conflicting`'s OWN natural key
    # (same team/season/source_artifact/source_row_position) but a DIFFERENT immutable
    # field, so persist_game_log_import_plan's own repository raises IdentityConflictError.
    with unit_of_work(pg_session_factory) as session:
        from src.db.repositories import DataSourceRepository, SourceArtifactRepository, TeamRepository
        from src.db.game_log_repositories import TeamGameLogObservationRepository
        DataSourceRepository(session).upsert(
            source_key="sports_reference_game_log", display_name="x", acquisition_type="static_file",
            is_rescrapable=False, description="x",
        )
        TeamRepository(session).upsert(team_id=conflicting.plan.team_id, display_name=conflicting.plan.team_slug, sr_slug=f"slug-{conflicting.plan.team_id}")
        artifact_row = SourceArtifactRepository(session).upsert(
            source_key="sports_reference_game_log", source_root_key=EXPECTED_SOURCE_ROOT_KEY, relative_path=conflicting.discovered.relative_path,
            original_filename="conflicting.csv", file_type="csv", point_in_time_classification="historical_but_not_point_in_time",
            content_hash=conflicting.content_hash, team_id=conflicting.plan.team_id, season_id=conflicting.plan.season_id,
        )
        spec = conflicting.plan.observation_specs[0]
        TeamGameLogObservationRepository(session).upsert(
            team_id=conflicting.plan.team_id, season_id=conflicting.plan.season_id, source_artifact_id=artifact_row.id,
            source_row_position=spec["source_row_position"], raw_opponent_name="A Completely Different Opponent Name",
            venue_status=spec["venue_status"], schema_variant=spec["schema_variant"], validation_status=spec["validation_status"],
        )

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_team_game_log_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION,
            artifacts=[good, conflicting, never_reached],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )

    assert result.status == "failed"
    assert [o.status for o in result.artifact_outcomes] == ["committed", "failed"]  # never_reached never even appears
    assert result.committed_count == 1
    assert result.failed_count == 1

    with unit_of_work(pg_session_factory) as session:
        good_obs = session.query(game_log_models.TeamGameLogObservation).filter(game_log_models.TeamGameLogObservation.team_id == good.plan.team_id).all()
        assert len(good_obs) == 1
        conflicting_rows = session.query(game_log_models.TeamGameLogObservation).filter(game_log_models.TeamGameLogObservation.team_id == conflicting.plan.team_id).all()
        assert len(conflicting_rows) == 1
        assert conflicting_rows[0].raw_opponent_name == "A Completely Different Opponent Name"  # untouched by the failed attempt
        never_reached_obs = session.query(game_log_models.TeamGameLogObservation).filter(game_log_models.TeamGameLogObservation.team_id == never_reached.plan.team_id).count()
        assert never_reached_obs == 0

        run_row = session.get(models.PipelineRun, result.run_id)
        assert run_row.status == "failed"

        failure_issues = session.query(models.DataQualityIssue).filter(
            models.DataQualityIssue.pipeline_run_id == result.run_id,
            models.DataQualityIssue.issue_code == FAILED_ARTIFACT_ISSUE_CODE,
        ).all()
        assert len(failure_issues) == 1
        assert failure_issues[0].source_artifact_id is None
        assert "team_slug='team-b'" in failure_issues[0].context
        assert "password" not in failure_issues[0].context.lower()


# ---------------------------------------------------------------------------
# Manifest / artifact validation, wired end-to-end
# ---------------------------------------------------------------------------


def test_manifest_contract_version_mismatch_refused_end_to_end(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(team_slug="test-team", team_query="Michigan State", rows=[{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    _seed_season(pg_session_factory, artifact)

    import src.pipeline.bulk_import_manifest as manifest_mod
    original = dict(manifest_mod.SOURCE_FAMILY_CONTRACT_VERSIONS)
    try:
        manifest_mod.SOURCE_FAMILY_CONTRACT_VERSIONS["team_game_log"] = "0-stale"
        stale_manifest = _manifest_for(artifact, contract_version="0-stale")
    finally:
        manifest_mod.SOURCE_FAMILY_CONTRACT_VERSIONS.clear()
        manifest_mod.SOURCE_FAMILY_CONTRACT_VERSIONS.update(original)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        with pytest.raises(ValidationError):
            run_team_game_log_committed_import(
                connection=conn, target_designation=designation, session_factory=pg_session_factory,
                manifest=stale_manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
                run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
            )
    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.PipelineRun).count() == 0


def test_stale_changed_artifact_refused_end_to_end(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(team_slug="test-team", team_query="Michigan State", rows=[{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    _seed_season(pg_session_factory, artifact)
    manifest = _manifest_for(artifact)
    changed = _build_artifact(team_slug="test-team", team_query="Michigan State", rows=[{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}], hash_suffix="-CHANGED")

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        with pytest.raises(ValidationError):
            run_team_game_log_committed_import(
                connection=conn, target_designation=designation, session_factory=pg_session_factory,
                manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[changed],
                run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
            )
    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.PipelineRun).count() == 0


def test_duplicate_artifact_refused_end_to_end(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(team_slug="test-team", team_query="Michigan State", rows=[{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    _seed_season(pg_session_factory, artifact)
    manifest = _manifest_for(artifact)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        with pytest.raises(ValidationError):
            run_team_game_log_committed_import(
                connection=conn, target_designation=designation, session_factory=pg_session_factory,
                manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact, artifact],
                run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
            )
    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.PipelineRun).count() == 0


def test_empty_manifest_and_empty_artifact_list_succeeds_trivially(pg_session_factory, pg_connection_engine):
    empty_family = SourceFamilyPlan(family="team_game_log", contract_version=TEAM_GAMELOG_IMPORT_CONTRACT_VERSION, artifacts=())
    manifest = build_dry_run_manifest(generated_at=dt.datetime.now(dt.timezone.utc), families=[empty_family])
    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_team_game_log_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
    assert result.status == "succeeded"
    assert result.artifact_outcomes == ()


# ---------------------------------------------------------------------------
# 63-bit game_id round-trip through every affected foreign key
# ---------------------------------------------------------------------------


def test_real_63_bit_game_id_round_trips_through_every_affected_table(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(team_slug="test-team", team_query="Michigan State", rows=[{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    _seed_season(pg_session_factory, artifact)
    manifest = _manifest_for(artifact)
    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_team_game_log_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
    assert result.status == "succeeded"
    with unit_of_work(pg_session_factory) as session:
        obs = session.query(game_log_models.TeamGameLogObservation).one()
        game_id = obs.game_id
        assert game_id is not None
        assert 0 <= game_id < 2**63
        game_row = session.get(models.Game, game_id)
        assert game_row is not None
        result_row = session.get(game_log_models.GameResult, game_id)
        assert result_row is not None
        assert result_row.game_id == game_id == game_row.game_id == obs.game_id


# ---------------------------------------------------------------------------
# Lock release after success and after failure
# ---------------------------------------------------------------------------


def test_lock_released_after_a_successful_run(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(team_slug="test-team", team_query="Michigan State", rows=[{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    _seed_season(pg_session_factory, artifact)
    manifest = _manifest_for(artifact)
    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_team_game_log_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
        assert result.status == "succeeded"
        remaining = conn.execute(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")).scalar_one()
    assert remaining == 0


def test_lock_released_after_a_failed_run(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(team_slug="test-team", team_query="Michigan State", rows=[{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    _seed_season(pg_session_factory, artifact)
    manifest = _manifest_for(artifact)

    with unit_of_work(pg_session_factory) as session:
        from src.db.repositories import DataSourceRepository, SourceArtifactRepository, TeamRepository
        from src.db.game_log_repositories import TeamGameLogObservationRepository
        DataSourceRepository(session).upsert(
            source_key="sports_reference_game_log", display_name="x", acquisition_type="static_file",
            is_rescrapable=False, description="x",
        )
        TeamRepository(session).upsert(team_id=artifact.plan.team_id, display_name=artifact.plan.team_slug, sr_slug=f"slug-{artifact.plan.team_id}")
        artifact_row = SourceArtifactRepository(session).upsert(
            source_key="sports_reference_game_log", source_root_key=EXPECTED_SOURCE_ROOT_KEY, relative_path=artifact.discovered.relative_path,
            original_filename="x.csv", file_type="csv", point_in_time_classification="historical_but_not_point_in_time",
            content_hash=artifact.content_hash, team_id=artifact.plan.team_id, season_id=artifact.plan.season_id,
        )
        spec = artifact.plan.observation_specs[0]
        TeamGameLogObservationRepository(session).upsert(
            team_id=artifact.plan.team_id, season_id=artifact.plan.season_id, source_artifact_id=artifact_row.id,
            source_row_position=spec["source_row_position"], raw_opponent_name="A Different Opponent Name",
            venue_status=spec["venue_status"], schema_variant=spec["schema_variant"], validation_status=spec["validation_status"],
        )

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_team_game_log_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
        assert result.status == "failed"
        remaining = conn.execute(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")).scalar_one()
    assert remaining == 0


# ---------------------------------------------------------------------------
# Statement-shape isolation: no writes outside the team-game-log-owned tables
# ---------------------------------------------------------------------------


def test_no_writes_to_other_source_family_tables(pg_session_factory, pg_connection_engine, pg_migrated_database_url):
    artifact = _build_artifact(team_slug="test-team", team_query="Michigan State", rows=[{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    _seed_season(pg_session_factory, artifact)
    manifest = _manifest_for(artifact)

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
            result = run_team_game_log_committed_import(
                connection=conn, target_designation=designation, session_factory=write_session_factory,
                manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
                run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
            )
        assert result.status == "succeeded"
    finally:
        event.remove(write_engine, "before_cursor_execute", _capture)
        write_engine.dispose()

    forbidden_tables = (
        "espn_source_observations", "espn_benchmark_snapshots", "espn_benchmark_values",
        "roster_memberships", "player_stat_snapshots", "team_stat_snapshots", "player_game_log_observations",
    )
    for statement in captured:
        lowered = statement.lower()
        assert not any(f'"{t}"' in lowered or f" {t} " in lowered or lowered.endswith(t) for t in forbidden_tables), f"unexpected write touching a non-team-game-log table: {statement!r}"
