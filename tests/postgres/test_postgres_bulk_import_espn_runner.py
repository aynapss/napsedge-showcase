"""
Controlled bulk-import design, Slice 8 (docs/controlled_bulk_import_design.md
section 16 item 8, approved for this slice only): real PostgreSQL
integration tests for `src/pipeline/bulk_import_espn_runner.py`.

This is the only test file that exercises `run_espn_committed_import`
end-to-end -- target verification, advisory locking, real persisted
writes through the existing, unmodified `persist_espn_source_import_plan`/
`persist_espn_benchmark_import_plan`, real rollback on a forced conflict,
and real pipeline-run finalization. Every fixture is synthetic and
in-memory (never the real three ESPN files); the only "real" thing here
is the disposable PostgreSQL container from tests/postgres/conftest.py.
"""
from __future__ import annotations

import datetime as dt
import uuid

import pandas as pd
import pytest
from sqlalchemy import create_engine, event, text

from src.db import espn_models, models
from src.db.repositories import SeasonRepository
from src.db.session import build_engine, build_session_factory, unit_of_work
from src.exceptions import AdvisoryLockUnavailableError, DatabaseTargetVerificationError, IdentityConflictError, ValidationError
from src.pipeline.bulk_import_espn_runner import (
    ESPN_BENCHMARK_FILENAME,
    ESPN_RAW_PLAYER_STATS_FILENAME,
    EXPECTED_SOURCE_ROOT_KEY,
    FAILED_ARTIFACT_ISSUE_CODE,
    EspnBenchmarkCommitArtifact,
    EspnSourceStatsCommitArtifact,
    run_espn_committed_import,
)
from src.pipeline.bulk_import_manifest import MANIFEST_SCHEMA_VERSION, PlannedArtifact, SourceFamilyPlan, build_dry_run_manifest
from src.pipeline.bulk_import_safety import DatabaseTargetDesignation, build_expected_acknowledgement, bulk_import_advisory_lock, verify_database_target
from src.pipeline.espn_import_adapter import (
    ESPN_IMPORT_CONTRACT_VERSION,
    DiscoveredEspnFile,
    build_espn_benchmark_import_plan,
    build_espn_source_import_plan,
    parse_espn_benchmark_workbook,
    parse_espn_player_stats_workbook,
)

pytestmark = pytest.mark.postgres

SEASON = 2026


# ---------------------------------------------------------------------------
# Synthetic fixture builders (same convention as the SQLite runner tests
# and tests/regression/test_espn_import_adapter.py -- never TeamDump)
# ---------------------------------------------------------------------------


def _raw_stats_df(rows: list[dict]) -> pd.DataFrame:
    base = {
        "RK": 1, "Name": "", "Team": "", "POS": "G", "GP": 20, "MIN": 25.0, "PTS": 10.0,
        "FGM": 4.0, "FGA": 8.0, "FG%": 50.0, "3PM": 1.0, "3PA": 3.0, "3P%": 33.0,
        "FTM": 2.0, "FTA": 3.0, "FT%": 66.0, "REB": 4.0, "AST": 3.0, "STL": 1.0, "BLK": 0.5, "TO": 2.0,
    }
    return pd.DataFrame([{**base, **r} for r in rows])


def _benchmark_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{"Metric": r.get("Metric", "PTS"), "NCAA_Mean": r.get("NCAA_Mean", 8.4), "NCAA_Std": r.get("NCAA_Std", 4.6)} for r in rows])


def _discovered_and_hash(filename: str, *, hash_suffix: str = "") -> tuple[DiscoveredEspnFile, str]:
    discovered = DiscoveredEspnFile(
        filename=filename, relative_path=filename, file_size_bytes=100,
        filesystem_mtime=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
    )
    return discovered, f"hash-{filename}{hash_suffix}"


def _build_source_artifact(*, rows: list[dict], season: int = SEASON, hash_suffix: str = "") -> EspnSourceStatsCommitArtifact:
    df = _raw_stats_df(rows)
    parsed = parse_espn_player_stats_workbook(df, season)
    plan = build_espn_source_import_plan(parsed)
    discovered, content_hash = _discovered_and_hash(ESPN_RAW_PLAYER_STATS_FILENAME, hash_suffix=hash_suffix)
    return EspnSourceStatsCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan, season_id=season)


def _build_benchmark_artifact(*, rows: list[dict], hash_suffix: str = "") -> EspnBenchmarkCommitArtifact:
    """season_id defaults to None -- the benchmark file is not season-scoped (confirmed by the adapter's own docstring/tests); passing a non-null season_id without seeding that Season row would otherwise fail PostgreSQL's own enforced FK."""
    df = _benchmark_df(rows)
    parsed = parse_espn_benchmark_workbook(df)
    plan = build_espn_benchmark_import_plan(parsed)
    discovered, content_hash = _discovered_and_hash(ESPN_BENCHMARK_FILENAME, hash_suffix=hash_suffix)
    return EspnBenchmarkCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan, season_id=None)


def _manifest_for(*artifacts, contract_version: str | None = None) -> object:
    planned = [
        PlannedArtifact(source_root_key=EXPECTED_SOURCE_ROOT_KEY, relative_path=artifact.relative_path, content_hash=artifact.content_hash, status="planned_insert")
        for artifact in artifacts
    ]
    family_plan = SourceFamilyPlan(family="espn", contract_version=contract_version or ESPN_IMPORT_CONTRACT_VERSION, artifacts=tuple(planned))
    return build_dry_run_manifest(generated_at=dt.datetime.now(dt.timezone.utc), families=[family_plan])


def _seed_season(session_factory, season_id: int = SEASON) -> None:
    with unit_of_work(session_factory) as session:
        if session.get(models.Season, season_id) is None:
            SeasonRepository(session).upsert(season_id=season_id, start_year=season_id - 1, end_year=season_id, label=f"{season_id - 1}-{str(season_id)[-2:]}")


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


def test_valid_source_stats_import(pg_session_factory, pg_connection_engine):
    artifact = _build_source_artifact(rows=[{"Name": "Some Player", "Team": "BYU"}])
    _seed_season(pg_session_factory)
    manifest = _manifest_for(artifact)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_espn_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )

    assert result.status == "succeeded"
    assert result.committed_count == 1
    with unit_of_work(pg_session_factory) as session:
        assert session.query(espn_models.EspnSourceObservation).count() == 1
        assert session.get(models.PipelineRun, result.run_id).status == "succeeded"


def test_valid_benchmark_import(pg_session_factory, pg_connection_engine):
    artifact = _build_benchmark_artifact(rows=[{"Metric": "PTS", "NCAA_Mean": 8.4, "NCAA_Std": 4.6}, {"Metric": "REB", "NCAA_Mean": 3.5, "NCAA_Std": 1.8}])
    manifest = _manifest_for(artifact)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_espn_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )

    assert result.status == "succeeded"
    with unit_of_work(pg_session_factory) as session:
        assert session.query(espn_models.EspnBenchmarkSnapshot).count() == 1
        assert session.query(espn_models.EspnBenchmarkValue).count() == 2


def test_deterministic_grouped_artifact_processing(pg_session_factory, pg_connection_engine):
    source_artifact = _build_source_artifact(rows=[{"Name": "Some Player", "Team": "BYU"}])
    benchmark_artifact = _build_benchmark_artifact(rows=[{"Metric": "PTS"}])
    _seed_season(pg_session_factory)
    manifest = _manifest_for(source_artifact, benchmark_artifact)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_espn_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION,
            artifacts=[source_artifact, benchmark_artifact],  # input order irrelevant
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )

    assert result.status == "succeeded"
    assert [o.kind for o in result.artifact_outcomes] == ["benchmark", "source_stats"]  # deterministic order
    with unit_of_work(pg_session_factory) as session:
        assert session.query(espn_models.EspnSourceObservation).count() == 1
        assert session.query(espn_models.EspnBenchmarkValue).count() == 1


# ---------------------------------------------------------------------------
# Canonical / reviewed-alias / ambiguous / unresolved team resolution states
# ---------------------------------------------------------------------------


def test_canonical_reviewed_alias_ambiguous_and_unresolved_states_preserved(pg_session_factory, pg_connection_engine):
    """BYU = exact_alias, MIA = reviewed_alias, UNCA = ambiguous, ARST = unresolved -- the four established real states."""
    artifact = _build_source_artifact(rows=[
        {"Name": "Player Exact", "Team": "BYU"},
        {"Name": "Player Reviewed", "Team": "MIA"},
        {"Name": "Player Ambiguous", "Team": "UNCA"},
        {"Name": "Player Unresolved", "Team": "ARST"},
    ])
    _seed_season(pg_session_factory)
    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_espn_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=_manifest_for(artifact), manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
    assert result.status == "succeeded"
    with unit_of_work(pg_session_factory) as session:
        by_method = {
            row.raw_player_name: row.team_resolution_method
            for row in session.query(espn_models.EspnSourceObservation).all()
        }
        assert by_method["Player Exact"] == "exact_alias"
        assert by_method["Player Reviewed"] == "reviewed_alias"
        assert by_method["Player Ambiguous"] == "ambiguous"
        assert by_method["Player Unresolved"] == "unresolved"
        # UNCA must remain genuinely ambiguous, never collapsed into the generic unresolved bucket.
        unca_row = session.query(espn_models.EspnSourceObservation).filter_by(raw_player_name="Player Ambiguous").one()
        assert unca_row.team_id is None


def test_unca_eight_rows_remain_classified_as_ambiguous(pg_session_factory, pg_connection_engine):
    """The established real finding: UNCA's own rows must always classify as ambiguous, never plain unresolved -- exercised here with 8 rows, the real fixture's own count."""
    rows = [{"Name": f"UNCA Player {i}", "Team": "UNCA"} for i in range(8)]
    artifact = _build_source_artifact(rows=rows)
    _seed_season(pg_session_factory)
    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_espn_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=_manifest_for(artifact), manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
    assert result.status == "succeeded"
    with unit_of_work(pg_session_factory) as session:
        unca_rows = session.query(espn_models.EspnSourceObservation).filter_by(raw_team_code="UNCA").all()
        assert len(unca_rows) == 8
        assert all(r.team_resolution_method == "ambiguous" for r in unca_rows)
        assert all(r.team_id is None for r in unca_rows)


# ---------------------------------------------------------------------------
# Benchmark metric vocabulary
# ---------------------------------------------------------------------------


def test_valid_benchmark_metrics_accepted(pg_session_factory, pg_connection_engine):
    artifact = _build_benchmark_artifact(rows=[{"Metric": "eFG", "NCAA_Mean": 0.5, "NCAA_Std": 0.1}, {"Metric": "GameScore", "NCAA_Mean": 10.0, "NCAA_Std": 3.0}])
    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_espn_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=_manifest_for(artifact), manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
    assert result.status == "succeeded"
    with unit_of_work(pg_session_factory) as session:
        assert session.query(espn_models.EspnBenchmarkValue).count() == 2


def test_invalid_metric_vocabulary_rejected_by_the_established_constraint(pg_session_factory, pg_connection_engine):
    """A metric_name outside ESPN_BENCHMARK_METRIC_NAMES must be refused by the existing DB CHECK constraint -- this runner never widens or bypasses it."""
    from src.pipeline.espn_import_adapter import ParsedEspnBenchmarkFile, ParsedEspnBenchmarkRow, build_espn_benchmark_import_plan

    fake_parsed = ParsedEspnBenchmarkFile(rows=[ParsedEspnBenchmarkRow(source_row_position=0, metric_name="NOT_A_REAL_METRIC", ncaa_mean=1.0, ncaa_std=1.0, validation_status="valid")], status="valid")
    plan = build_espn_benchmark_import_plan(fake_parsed)
    discovered, content_hash = _discovered_and_hash(ESPN_BENCHMARK_FILENAME)
    artifact = EspnBenchmarkCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_espn_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=_manifest_for(artifact), manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
    assert result.status == "failed"
    with unit_of_work(pg_session_factory) as session:
        assert session.query(espn_models.EspnBenchmarkValue).count() == 0
        assert session.query(espn_models.EspnBenchmarkSnapshot).count() == 0


# ---------------------------------------------------------------------------
# Idempotency and coexistence
# ---------------------------------------------------------------------------


def test_identical_replay_is_idempotent_no_duplicate_rows(pg_session_factory, pg_connection_engine):
    artifact = _build_source_artifact(rows=[{"Name": "Some Player", "Team": "BYU"}])
    _seed_season(pg_session_factory)
    manifest = _manifest_for(artifact)

    def _run():
        with pg_connection_engine.connect() as conn:
            designation = _designation_for(_real_database_name(conn))
            return run_espn_committed_import(
                connection=conn, target_designation=designation, session_factory=pg_session_factory,
                manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
                run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
            )

    first = _run()
    second = _run()
    assert first.status == "succeeded"
    assert second.status == "succeeded"
    with unit_of_work(pg_session_factory) as session:
        assert session.query(espn_models.EspnSourceObservation).count() == 1
        assert session.query(models.SourceArtifact).count() == 1


def test_different_immutable_snapshot_coexists_with_the_original(pg_session_factory, pg_connection_engine):
    original = _build_source_artifact(rows=[{"Name": "Some Player", "Team": "BYU"}])
    changed = _build_source_artifact(rows=[{"Name": "Some Player", "Team": "BYU"}], hash_suffix="-v2")
    _seed_season(pg_session_factory)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result_1 = run_espn_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=_manifest_for(original), manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[original],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
        result_2 = run_espn_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=_manifest_for(changed), manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[changed],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )

    assert result_1.status == "succeeded"
    assert result_2.status == "succeeded"
    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.SourceArtifact).filter(models.SourceArtifact.relative_path == ESPN_RAW_PLAYER_STATS_FILENAME).count() == 2
        assert session.query(espn_models.EspnSourceObservation).count() == 2  # two coexisting snapshots' worth of rows


# ---------------------------------------------------------------------------
# Timestamp/provenance preservation
# ---------------------------------------------------------------------------


def test_timestamp_and_provenance_preservation(pg_session_factory, pg_connection_engine):
    artifact = _build_source_artifact(rows=[{"Name": "Some Player", "Team": "BYU"}])
    _seed_season(pg_session_factory)
    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_espn_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=_manifest_for(artifact), manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
    assert result.status == "succeeded"
    with unit_of_work(pg_session_factory) as session:
        art = session.query(models.SourceArtifact).one()
        assert art.collection_timestamp is None
        assert art.point_in_time_classification == "current_snapshot_only"
        assert art.content_hash == artifact.content_hash
        assert art.filesystem_mtime is not None and art.filesystem_mtime.tzinfo is not None


# ---------------------------------------------------------------------------
# No uncontrolled enriched-view table or rows
# ---------------------------------------------------------------------------


def test_no_uncontrolled_enriched_view_table_or_rows_created(pg_session_factory, pg_connection_engine):
    """A full run committing both known artifact types must never create any row referencing the enriched filename anywhere."""
    source_artifact = _build_source_artifact(rows=[{"Name": "Some Player", "Team": "BYU"}])
    benchmark_artifact = _build_benchmark_artifact(rows=[{"Metric": "PTS"}])
    _seed_season(pg_session_factory)
    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_espn_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=_manifest_for(source_artifact, benchmark_artifact), manifest_schema_version=MANIFEST_SCHEMA_VERSION,
            artifacts=[source_artifact, benchmark_artifact],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
    assert result.status == "succeeded"
    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.SourceArtifact).filter(models.SourceArtifact.original_filename == "espn_full_player_stats_enriched.csv").count() == 0
        # No 130-column table exists to check against, structurally -- only the two controlled ones do.
        assert set(t.name for t in models.Base.metadata.tables.values() if "espn" in t.name) == {"espn_source_observations", "espn_benchmark_snapshots", "espn_benchmark_values"}


# ---------------------------------------------------------------------------
# Target verification / advisory lock, before any write
# ---------------------------------------------------------------------------


def test_target_verification_failure_before_any_write(pg_session_factory, pg_connection_engine):
    artifact = _build_source_artifact(rows=[{"Name": "Some Player", "Team": "BYU"}])
    _seed_season(pg_session_factory)
    manifest = _manifest_for(artifact)

    with pg_connection_engine.connect() as conn:
        real_name = _real_database_name(conn)
        wrong_designation = _designation_for(real_name + "_wrong")
        with pytest.raises(DatabaseTargetVerificationError):
            run_espn_committed_import(
                connection=conn, target_designation=wrong_designation, session_factory=pg_session_factory,
                manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
                run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
            )

    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.PipelineRun).count() == 0
        assert session.query(espn_models.EspnSourceObservation).count() == 0


def test_advisory_lock_contention_before_any_write(pg_session_factory, pg_connection_engine, pg_migrated_database_url):
    artifact = _build_source_artifact(rows=[{"Name": "Some Player", "Team": "BYU"}])
    _seed_season(pg_session_factory)
    manifest = _manifest_for(artifact)

    other_engine = create_engine(pg_migrated_database_url)
    try:
        with pg_connection_engine.connect() as conn, other_engine.connect() as other_conn:
            designation = _designation_for(_real_database_name(conn))
            verified = verify_database_target(other_conn, designation)
            with bulk_import_advisory_lock(other_conn, verified):
                with pytest.raises(AdvisoryLockUnavailableError):
                    run_espn_committed_import(
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
    good = _build_benchmark_artifact(rows=[{"Metric": "PTS", "NCAA_Mean": 8.4, "NCAA_Std": 4.6}])
    conflicting = _build_source_artifact(rows=[{"Name": "Some Player", "Team": "BYU", "PTS": 10.0}])
    _seed_season(pg_session_factory)
    manifest = _manifest_for(good, conflicting)

    # Pre-seed a conflicting observation row under `conflicting`'s OWN natural key
    # (same source_artifact_id/source_row_position) but a DIFFERENT immutable field,
    # so persist_espn_source_import_plan's own repository raises IdentityConflictError.
    with unit_of_work(pg_session_factory) as session:
        from src.db.repositories import DataSourceRepository, SourceArtifactRepository
        from src.db.espn_repositories import EspnSourceObservationRepository
        DataSourceRepository(session).upsert(
            source_key="espn_player_stats", display_name="x", acquisition_type="static_file",
            is_rescrapable=False, description="x",
        )
        artifact_row = SourceArtifactRepository(session).upsert(
            source_key="espn_player_stats", source_root_key=EXPECTED_SOURCE_ROOT_KEY, relative_path=conflicting.discovered.relative_path,
            original_filename=conflicting.discovered.filename, file_type="csv", point_in_time_classification="current_snapshot_only",
            content_hash=conflicting.content_hash, season_id=conflicting.season_id,
        )
        EspnSourceObservationRepository(session).upsert(
            source_artifact_id=artifact_row.id, source_row_position=0, season_id=conflicting.season_id,
            raw_player_name="A Completely Different Raw Name", raw_team_code="BYU", validation_status="valid",
        )

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_espn_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION,
            artifacts=[good, conflicting],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )

    assert result.status == "failed"
    assert [o.status for o in result.artifact_outcomes] == ["committed", "failed"]  # "benchmark" sorts before "source_stats"
    assert result.committed_count == 1
    assert result.failed_count == 1

    with unit_of_work(pg_session_factory) as session:
        assert session.query(espn_models.EspnBenchmarkValue).count() == 1  # good's own row survives
        obs_rows = session.query(espn_models.EspnSourceObservation).all()
        assert len(obs_rows) == 1
        assert obs_rows[0].raw_player_name == "A Completely Different Raw Name"  # untouched by the failed attempt

        run_row = session.get(models.PipelineRun, result.run_id)
        assert run_row.status == "failed"

        failure_issues = session.query(models.DataQualityIssue).filter(
            models.DataQualityIssue.pipeline_run_id == result.run_id,
            models.DataQualityIssue.issue_code == FAILED_ARTIFACT_ISSUE_CODE,
        ).all()
        assert len(failure_issues) == 1
        assert failure_issues[0].source_artifact_id is None
        assert "source_stats" in failure_issues[0].context
        assert "password" not in failure_issues[0].context.lower()


# ---------------------------------------------------------------------------
# Manifest / artifact validation, wired end-to-end
# ---------------------------------------------------------------------------


def test_manifest_contract_version_mismatch_refused_end_to_end(pg_session_factory, pg_connection_engine):
    artifact = _build_source_artifact(rows=[{"Name": "Some Player", "Team": "BYU"}])
    _seed_season(pg_session_factory)

    import src.pipeline.bulk_import_manifest as manifest_mod
    original = dict(manifest_mod.SOURCE_FAMILY_CONTRACT_VERSIONS)
    try:
        manifest_mod.SOURCE_FAMILY_CONTRACT_VERSIONS["espn"] = "0-stale"
        stale_manifest = _manifest_for(artifact, contract_version="0-stale")
    finally:
        manifest_mod.SOURCE_FAMILY_CONTRACT_VERSIONS.clear()
        manifest_mod.SOURCE_FAMILY_CONTRACT_VERSIONS.update(original)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        with pytest.raises(ValidationError):
            run_espn_committed_import(
                connection=conn, target_designation=designation, session_factory=pg_session_factory,
                manifest=stale_manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
                run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
            )
    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.PipelineRun).count() == 0


def test_duplicate_artifact_refused_end_to_end(pg_session_factory, pg_connection_engine):
    artifact = _build_source_artifact(rows=[{"Name": "Some Player", "Team": "BYU"}])
    _seed_season(pg_session_factory)
    manifest = _manifest_for(artifact)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        with pytest.raises(ValidationError):
            run_espn_committed_import(
                connection=conn, target_designation=designation, session_factory=pg_session_factory,
                manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact, artifact],
                run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
            )
    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.PipelineRun).count() == 0


def test_empty_manifest_and_empty_artifact_list_succeeds_trivially(pg_session_factory, pg_connection_engine):
    empty_family = SourceFamilyPlan(family="espn", contract_version=ESPN_IMPORT_CONTRACT_VERSION, artifacts=())
    manifest = build_dry_run_manifest(generated_at=dt.datetime.now(dt.timezone.utc), families=[empty_family])
    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_espn_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
    assert result.status == "succeeded"
    assert result.artifact_outcomes == ()


# ---------------------------------------------------------------------------
# Lock release after success and after failure
# ---------------------------------------------------------------------------


def test_lock_released_after_a_successful_run(pg_session_factory, pg_connection_engine):
    artifact = _build_source_artifact(rows=[{"Name": "Some Player", "Team": "BYU"}])
    _seed_season(pg_session_factory)
    manifest = _manifest_for(artifact)
    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_espn_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
        assert result.status == "succeeded"
        remaining = conn.execute(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")).scalar_one()
    assert remaining == 0


def test_lock_released_after_a_failed_run(pg_session_factory, pg_connection_engine):
    from src.pipeline.espn_import_adapter import ParsedEspnBenchmarkFile, ParsedEspnBenchmarkRow

    fake_parsed = ParsedEspnBenchmarkFile(rows=[ParsedEspnBenchmarkRow(source_row_position=0, metric_name="NOT_A_REAL_METRIC", ncaa_mean=1.0, ncaa_std=1.0, validation_status="valid")], status="valid")
    plan = build_espn_benchmark_import_plan(fake_parsed)
    discovered, content_hash = _discovered_and_hash(ESPN_BENCHMARK_FILENAME)
    artifact = EspnBenchmarkCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan)
    manifest = _manifest_for(artifact)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_espn_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
        assert result.status == "failed"
        remaining = conn.execute(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")).scalar_one()
    assert remaining == 0


# ---------------------------------------------------------------------------
# Statement-shape isolation: no writes outside ESPN-owned tables
# ---------------------------------------------------------------------------


def test_no_writes_to_other_source_family_tables(pg_session_factory, pg_connection_engine, pg_migrated_database_url):
    artifact = _build_source_artifact(rows=[{"Name": "Some Player", "Team": "BYU"}])
    _seed_season(pg_session_factory)
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
            result = run_espn_committed_import(
                connection=conn, target_designation=designation, session_factory=write_session_factory,
                manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
                run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
            )
        assert result.status == "succeeded"
    finally:
        event.remove(write_engine, "before_cursor_execute", _capture)
        write_engine.dispose()

    forbidden_tables = (
        "roster_memberships", "player_stat_snapshots", "team_stat_snapshots",
        "team_game_log_observations", "team_game_stat_lines", "game_results", "games",
        "player_game_log_observations", "espn_benchmark_snapshots", "espn_benchmark_values",
    )
    for statement in captured:
        lowered = statement.lower()
        assert not any(f'"{t}"' in lowered or f" {t} " in lowered or lowered.endswith(t) for t in forbidden_tables), f"unexpected write touching a non-ESPN-source-stats table: {statement!r}"
