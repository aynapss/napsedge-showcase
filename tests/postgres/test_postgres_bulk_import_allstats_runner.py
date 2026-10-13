"""
Controlled bulk-import design, Slice 5 (docs/controlled_bulk_import_design.md
section 16 item 5, approved for this slice only): real PostgreSQL
integration tests for `src/pipeline/bulk_import_allstats_runner.py`.

This is the only test file that exercises `run_allstats_committed_import`
end-to-end -- target verification, advisory locking, real persisted
writes through the existing, unmodified `persist_allstats_import_plan`,
real rollback on a forced conflict, real reconciliation reads, and real
pipeline-run finalization. Every fixture is synthetic and in-memory
(never TeamDump, never the real 304-team universe); the only "real" thing
here is the disposable PostgreSQL container from tests/postgres/conftest.py.
"""
from __future__ import annotations

import datetime as dt
import uuid

import pandas as pd
import pytest
from sqlalchemy import create_engine, event, text

from src.db import allstats_models, models
from src.db.repositories import SeasonRepository, TeamRepository
from src.db.session import build_engine, build_session_factory, unit_of_work
from src.exceptions import DatabaseTargetVerificationError, IdentityConflictError, ValidationError
from src.pipeline.allstats_import_adapter import (
    ALLSTATS_IMPORT_CONTRACT_VERSION,
    AllStatsFileMetadata,
    DiscoveredAllStatsWorkbook,
    build_allstats_import_plan,
    parse_allstats_workbook,
)
from src.pipeline.bulk_import_allstats_runner import (
    FAILED_ARTIFACT_ISSUE_CODE,
    RECONCILIATION_MISMATCH_ISSUE_CODE,
    AllStatsCommitArtifact,
    run_allstats_committed_import,
)
from src.pipeline.bulk_import_manifest import MANIFEST_SCHEMA_VERSION, PlannedArtifact, SourceFamilyPlan, build_dry_run_manifest
from src.pipeline.bulk_import_safety import DatabaseTargetDesignation, build_expected_acknowledgement, bulk_import_advisory_lock, verify_database_target

pytestmark = pytest.mark.postgres

SEASON = 2026


# ---------------------------------------------------------------------------
# Synthetic fixture builders (same convention as the SQLite runner tests
# and tests/regression/test_allstats_import_adapter.py -- never TeamDump)
# ---------------------------------------------------------------------------


def _roster_df(rows: list[dict]) -> pd.DataFrame:
    base = {"Player": None, "#": None, "Class": "SR", "Pos": "G", "Height": "6-2", "Weight": 190.0,
            "Hometown": None, "High School": None, "RSCI Top 100": None, "Summary": None}
    return pd.DataFrame([{**base, **r} for r in rows])


def _discovered_and_hashes(team_slug: str, dfs: dict[str, pd.DataFrame], *, season: int = SEASON, hash_suffix: str = ""):
    files, hashes = {}, {}
    for label in dfs:
        files[label] = AllStatsFileMetadata(
            relative_path=f"{team_slug}/{label}_{season}.xlsx", file_size_bytes=100,
            filesystem_mtime=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        )
        hashes[label] = f"hash-{label}{hash_suffix}"
    return DiscoveredAllStatsWorkbook(team_slug=team_slug, season=season, files=files), hashes


def _build_artifact(*, team_slug: str, team_query: str, roster_rows: list[dict], season: int = SEASON, hash_suffix: str = "") -> AllStatsCommitArtifact:
    dfs = {"Roster": _roster_df(roster_rows)}
    parsed = parse_allstats_workbook(dfs, team_query, season)
    plan = build_allstats_import_plan(parsed)
    discovered, hashes = _discovered_and_hashes(team_slug, dfs, season=season, hash_suffix=hash_suffix)
    return AllStatsCommitArtifact(discovered=discovered, content_hashes=hashes, plan=plan)


def _manifest_for(*artifacts: AllStatsCommitArtifact, contract_version: str | None = None) -> object:
    planned = [
        PlannedArtifact(source_root_key="teamdump_cbb", relative_path=meta.relative_path, content_hash=artifact.content_hashes[label], status="planned_insert")
        for artifact in artifacts
        for label, meta in artifact.discovered.files.items()
    ]
    family_plan = SourceFamilyPlan(family="allstats", contract_version=contract_version or ALLSTATS_IMPORT_CONTRACT_VERSION, artifacts=tuple(planned))
    return build_dry_run_manifest(generated_at=dt.datetime.now(dt.timezone.utc), families=[family_plan])


def _seed_team_and_season(session_factory, artifact: AllStatsCommitArtifact) -> None:
    """Real production flow: teams/seasons are already registered before any source-evidence import runs against them (unchanged from tests/regression/test_allstats_import_adapter.py's own convention)."""
    plan = artifact.plan
    with unit_of_work(session_factory) as session:
        if plan.team_id is not None and session.get(models.Team, plan.team_id) is None:
            TeamRepository(session).upsert(team_id=plan.team_id, display_name=plan.team_slug, sr_slug=f"slug-{plan.team_id}")
        if plan.season_id is not None and session.get(models.Season, plan.season_id) is None:
            SeasonRepository(session).upsert(season_id=plan.season_id, start_year=plan.season_id - 1, end_year=plan.season_id, label=f"{plan.season_id - 1}-{str(plan.season_id)[-2:]}")


def _designation_for(name: str) -> DatabaseTargetDesignation:
    return DatabaseTargetDesignation(expected_database_name=name, acknowledgement=build_expected_acknowledgement(name))


def _real_database_name(connection) -> str:
    return connection.execute(text("SELECT current_database()")).scalar_one()


@pytest.fixture()
def pg_connection_engine(pg_migrated_database_url):
    """A second, independent engine (distinct from pg_session_factory's own) targeting the exact same disposable database -- used for the runner's own verify+lock `connection` argument, mirroring how a real caller would supply a connection separate from its ORM session factory."""
    engine = create_engine(pg_migrated_database_url)
    try:
        yield engine
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Valid imports
# ---------------------------------------------------------------------------


def test_valid_one_artifact_import(pg_session_factory, pg_connection_engine, pg_migrated_database_url):
    artifact = _build_artifact(team_slug="test-team", team_query="St. Bonaventure", roster_rows=[{"Player": "Frank Mitchell", "#": 0}])
    _seed_team_and_season(pg_session_factory, artifact)
    manifest = _manifest_for(artifact)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_allstats_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )

    assert result.status == "succeeded"
    assert result.committed_count == 1
    assert result.failed_count == 0
    with unit_of_work(pg_session_factory) as session:
        assert session.query(allstats_models.RosterMembership).count() == 1
        assert session.get(models.PipelineRun, result.run_id).status == "succeeded"


def test_valid_multiple_artifact_import_and_deterministic_order(pg_session_factory, pg_connection_engine):
    artifact_b = _build_artifact(team_slug="team-b", team_query="Michigan State", roster_rows=[{"Player": "Player B", "#": 1}])
    artifact_a = _build_artifact(team_slug="team-a", team_query="St. Bonaventure", roster_rows=[{"Player": "Player A", "#": 0}])
    _seed_team_and_season(pg_session_factory, artifact_a)
    _seed_team_and_season(pg_session_factory, artifact_b)
    manifest = _manifest_for(artifact_a, artifact_b)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_allstats_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION,
            artifacts=[artifact_b, artifact_a],  # deliberately reversed input order
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )

    assert result.status == "succeeded"
    assert [o.team_slug for o in result.artifact_outcomes] == ["team-a", "team-b"]
    with unit_of_work(pg_session_factory) as session:
        assert session.query(allstats_models.RosterMembership).count() == 2


# ---------------------------------------------------------------------------
# Target verification / advisory lock, before any write
# ---------------------------------------------------------------------------


def test_target_verification_failure_before_any_write(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(team_slug="test-team", team_query="St. Bonaventure", roster_rows=[{"Player": "Frank Mitchell", "#": 0}])
    _seed_team_and_season(pg_session_factory, artifact)
    manifest = _manifest_for(artifact)

    with pg_connection_engine.connect() as conn:
        real_name = _real_database_name(conn)
        wrong_designation = _designation_for(real_name + "_wrong")
        with pytest.raises(DatabaseTargetVerificationError):
            run_allstats_committed_import(
                connection=conn, target_designation=wrong_designation, session_factory=pg_session_factory,
                manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
                run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
            )

    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.PipelineRun).count() == 0
        assert session.query(allstats_models.RosterMembership).count() == 0


def test_advisory_lock_contention_before_any_write(pg_session_factory, pg_connection_engine, pg_migrated_database_url):
    artifact = _build_artifact(team_slug="test-team", team_query="St. Bonaventure", roster_rows=[{"Player": "Frank Mitchell", "#": 0}])
    _seed_team_and_season(pg_session_factory, artifact)
    manifest = _manifest_for(artifact)

    other_engine = create_engine(pg_migrated_database_url)
    try:
        with pg_connection_engine.connect() as conn, other_engine.connect() as other_conn:
            designation = _designation_for(_real_database_name(conn))
            verified = verify_database_target(other_conn, designation)
            with bulk_import_advisory_lock(other_conn, verified):
                from src.exceptions import AdvisoryLockUnavailableError
                with pytest.raises(AdvisoryLockUnavailableError):
                    run_allstats_committed_import(
                        connection=conn, target_designation=designation, session_factory=pg_session_factory,
                        manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
                        run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
                    )
    finally:
        other_engine.dispose()

    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.PipelineRun).count() == 0
        assert session.query(allstats_models.RosterMembership).count() == 0


# ---------------------------------------------------------------------------
# Failure / rollback behavior
# ---------------------------------------------------------------------------


def test_failed_artifact_rolls_back_leaves_earlier_committed_and_stops_processing(pg_session_factory, pg_connection_engine):
    good = _build_artifact(team_slug="team-a", team_query="St. Bonaventure", roster_rows=[{"Player": "Player A", "#": 0}])
    conflicting = _build_artifact(team_slug="team-b", team_query="Michigan State", roster_rows=[{"Player": "Player B", "#": 1}])
    never_reached = _build_artifact(team_slug="team-c", team_query="St. Bonaventure", roster_rows=[{"Player": "Player C", "#": 2}], season=SEASON)
    # never_reached shares team-a's team identity but a distinct team_slug/relative_path -- it must never be processed at all once team-b fails.
    _seed_team_and_season(pg_session_factory, good)
    _seed_team_and_season(pg_session_factory, conflicting)
    manifest = _manifest_for(good, conflicting, never_reached)

    # Pre-seed a conflicting roster_memberships row under conflicting's OWN natural key
    # (same player_key/team/season/source_artifact_id) but a DIFFERENT immutable field,
    # so persist_allstats_import_plan's own repository raises IdentityConflictError.
    with unit_of_work(pg_session_factory) as session:
        from src.db.repositories import DataSourceRepository, SourceArtifactRepository
        DataSourceRepository(session).upsert(
            source_key="sports_reference_allstats", display_name="x", acquisition_type="static_file",
            is_rescrapable=False, description="x",
        )
        roster_meta = conflicting.discovered.files["Roster"]
        artifact_row = SourceArtifactRepository(session).upsert(
            source_key="sports_reference_allstats", source_root_key="teamdump_cbb", relative_path=roster_meta.relative_path,
            original_filename="Roster_2026.xlsx", file_type="xlsx", point_in_time_classification="historical_but_not_point_in_time",
            content_hash=conflicting.content_hashes["Roster"], team_id=conflicting.plan.team_id, season_id=conflicting.plan.season_id,
        )
        conflicting_spec = conflicting.plan.roster_membership_specs[0]
        allstats_models.RosterMembership.__table__  # sanity import touch
        from src.db.allstats_repositories import RosterMembershipRepository
        RosterMembershipRepository(session).upsert(
            team_id=conflicting.plan.team_id, season_id=conflicting.plan.season_id, source_artifact_id=artifact_row.id,
            player_key=conflicting_spec["player_key"], source_player_name="A Completely Different Pre-Seeded Name",
            validation_status=conflicting_spec["validation_status"], jersey_number=99,
        )

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_allstats_committed_import(
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
        # Exactly two rows total: good's own newly-committed row, plus the row pre-seeded
        # onto conflicting's own natural key before the run started. never_reached shares
        # good's own team identity (both resolve to "St. Bonaventure") but a distinct player --
        # if it had been processed despite the earlier failure, a third row would exist here.
        all_rows = session.query(allstats_models.RosterMembership).all()
        assert len(all_rows) == 2
        assert {r.source_player_name for r in all_rows} == {"Player A", "A Completely Different Pre-Seeded Name"}
        # conflicting's own attempted new row never landed (only the pre-seeded row remains, unchanged).
        conflicting_rows = session.query(allstats_models.RosterMembership).filter(allstats_models.RosterMembership.team_id == conflicting.plan.team_id).all()
        assert len(conflicting_rows) == 1
        assert conflicting_rows[0].source_player_name == "A Completely Different Pre-Seeded Name"

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


def test_overall_success_status_when_every_artifact_commits(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(team_slug="test-team", team_query="St. Bonaventure", roster_rows=[{"Player": "Frank Mitchell", "#": 0}])
    _seed_team_and_season(pg_session_factory, artifact)
    manifest = _manifest_for(artifact)
    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_allstats_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
    assert result.status == "succeeded"
    assert result.failed_count == 0


# ---------------------------------------------------------------------------
# Idempotency and coexistence
# ---------------------------------------------------------------------------


def test_identical_replay_is_idempotent_no_duplicate_rows(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(team_slug="test-team", team_query="St. Bonaventure", roster_rows=[{"Player": "Frank Mitchell", "#": 0}])
    _seed_team_and_season(pg_session_factory, artifact)
    manifest = _manifest_for(artifact)

    def _run():
        with pg_connection_engine.connect() as conn:
            designation = _designation_for(_real_database_name(conn))
            return run_allstats_committed_import(
                connection=conn, target_designation=designation, session_factory=pg_session_factory,
                manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
                run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
            )

    first = _run()
    second = _run()
    assert first.status == "succeeded"
    assert second.status == "succeeded"
    with unit_of_work(pg_session_factory) as session:
        assert session.query(allstats_models.RosterMembership).count() == 1  # not duplicated
        assert session.query(models.SourceArtifact).count() == 1  # same natural key, upserted not duplicated


def test_different_immutable_snapshot_coexists_with_the_original(pg_session_factory, pg_connection_engine):
    original = _build_artifact(team_slug="test-team", team_query="St. Bonaventure", roster_rows=[{"Player": "Frank Mitchell", "#": 0}])
    changed = _build_artifact(team_slug="test-team", team_query="St. Bonaventure", roster_rows=[{"Player": "Frank Mitchell", "#": 0}], hash_suffix="-v2")
    _seed_team_and_season(pg_session_factory, original)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        manifest_1 = _manifest_for(original)
        result_1 = run_allstats_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest_1, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[original],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
        manifest_2 = _manifest_for(changed)
        result_2 = run_allstats_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest_2, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[changed],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )

    assert result_1.status == "succeeded"
    assert result_2.status == "succeeded"
    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.SourceArtifact).filter(models.SourceArtifact.relative_path == original.discovered.files["Roster"].relative_path).count() == 2
        assert session.query(allstats_models.RosterMembership).count() == 2  # two coexisting snapshots' worth of rows


# ---------------------------------------------------------------------------
# Manifest / artifact validation, wired end-to-end
# ---------------------------------------------------------------------------


def test_manifest_contract_version_mismatch_refused_end_to_end(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(team_slug="test-team", team_query="St. Bonaventure", roster_rows=[{"Player": "Frank Mitchell", "#": 0}])
    _seed_team_and_season(pg_session_factory, artifact)

    import src.pipeline.bulk_import_manifest as manifest_mod
    original = dict(manifest_mod.SOURCE_FAMILY_CONTRACT_VERSIONS)
    try:
        manifest_mod.SOURCE_FAMILY_CONTRACT_VERSIONS["allstats"] = "0-stale"
        stale_manifest = _manifest_for(artifact, contract_version="0-stale")
    finally:
        manifest_mod.SOURCE_FAMILY_CONTRACT_VERSIONS.clear()
        manifest_mod.SOURCE_FAMILY_CONTRACT_VERSIONS.update(original)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        with pytest.raises(ValidationError):
            run_allstats_committed_import(
                connection=conn, target_designation=designation, session_factory=pg_session_factory,
                manifest=stale_manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
                run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
            )
    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.PipelineRun).count() == 0


def test_stale_changed_artifact_refused_end_to_end(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(team_slug="test-team", team_query="St. Bonaventure", roster_rows=[{"Player": "Frank Mitchell", "#": 0}])
    _seed_team_and_season(pg_session_factory, artifact)
    manifest = _manifest_for(artifact)
    changed = _build_artifact(team_slug="test-team", team_query="St. Bonaventure", roster_rows=[{"Player": "Frank Mitchell", "#": 0}], hash_suffix="-CHANGED")

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        with pytest.raises(ValidationError):
            run_allstats_committed_import(
                connection=conn, target_designation=designation, session_factory=pg_session_factory,
                manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[changed],
                run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
            )
    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.PipelineRun).count() == 0


def test_duplicate_artifact_refused_end_to_end(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(team_slug="test-team", team_query="St. Bonaventure", roster_rows=[{"Player": "Frank Mitchell", "#": 0}])
    _seed_team_and_season(pg_session_factory, artifact)
    manifest = _manifest_for(artifact)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        with pytest.raises(ValidationError):
            run_allstats_committed_import(
                connection=conn, target_designation=designation, session_factory=pg_session_factory,
                manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact, artifact],
                run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
            )
    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.PipelineRun).count() == 0


def test_empty_artifact_list_against_nonempty_manifest_succeeds_trivially(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(team_slug="test-team", team_query="St. Bonaventure", roster_rows=[{"Player": "Frank Mitchell", "#": 0}])
    manifest = _manifest_for(artifact)  # approved, but nothing actually committed this call
    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_allstats_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
    assert result.status == "succeeded"
    assert result.artifact_outcomes == ()
    with unit_of_work(pg_session_factory) as session:
        assert session.query(allstats_models.RosterMembership).count() == 0


# ---------------------------------------------------------------------------
# UTC timestamp behavior
# ---------------------------------------------------------------------------


def test_naive_started_at_is_rejected(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(team_slug="test-team", team_query="St. Bonaventure", roster_rows=[{"Player": "Frank Mitchell", "#": 0}])
    _seed_team_and_season(pg_session_factory, artifact)
    manifest = _manifest_for(artifact)
    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        with pytest.raises(ValidationError):
            run_allstats_committed_import(
                connection=conn, target_designation=designation, session_factory=pg_session_factory,
                manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
                run_id=uuid.uuid4().hex, started_at=dt.datetime(2026, 1, 1),  # naive, no tzinfo
            )


# ---------------------------------------------------------------------------
# Adapter pass-through behaviors, exercised end-to-end through the runner
# ---------------------------------------------------------------------------


def test_missing_playerkey_rsci_and_mojibake_preserved_end_to_end(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(
        team_slug="test-team", team_query="St. Bonaventure",
        roster_rows=[{"Player": "JoÃ£o Victor Moreira", "#": 0, "RSCI Top 100": "5-star, RSCI #12"}],
    )
    assert "PlayerKey" not in _roster_df([{}]).columns  # confirms this synthetic fixture never had one to begin with
    _seed_team_and_season(pg_session_factory, artifact)
    manifest = _manifest_for(artifact)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_allstats_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
    assert result.status == "succeeded"
    with unit_of_work(pg_session_factory) as session:
        row = session.query(allstats_models.RosterMembership).one()
        assert row.source_player_name == "JoÃ£o Victor Moreira"  # raw mojibake text preserved verbatim, unrepaired
        assert row.rsci_top_100 == "5-star, RSCI #12"  # raw RSCI text preserved verbatim


# ---------------------------------------------------------------------------
# Statement-shape isolation: no writes outside the AllStats tables
# ---------------------------------------------------------------------------


def test_no_writes_to_other_source_family_tables(pg_session_factory, pg_connection_engine, pg_migrated_database_url):
    artifact = _build_artifact(team_slug="test-team", team_query="St. Bonaventure", roster_rows=[{"Player": "Frank Mitchell", "#": 0}])
    _seed_team_and_season(pg_session_factory, artifact)
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
            result = run_allstats_committed_import(
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
        "team_game_log_observations", "game_results", "games", "player_game_log_observations",
    )
    for statement in captured:
        lowered = statement.lower()
        assert not any(f'"{t}"' in lowered or f" {t} " in lowered or lowered.endswith(t) for t in forbidden_tables), f"unexpected write touching a non-AllStats table: {statement!r}"


# ---------------------------------------------------------------------------
# Lock release after success and after failure
# ---------------------------------------------------------------------------


def test_lock_released_after_a_successful_run(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(team_slug="test-team", team_query="St. Bonaventure", roster_rows=[{"Player": "Frank Mitchell", "#": 0}])
    _seed_team_and_season(pg_session_factory, artifact)
    manifest = _manifest_for(artifact)
    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_allstats_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
        assert result.status == "succeeded"
        remaining = conn.execute(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")).scalar_one()
    assert remaining == 0


def test_lock_released_after_a_failed_run(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(team_slug="test-team", team_query="St. Bonaventure", roster_rows=[{"Player": "Frank Mitchell", "#": 0}])
    _seed_team_and_season(pg_session_factory, artifact)
    manifest = _manifest_for(artifact)

    with unit_of_work(pg_session_factory) as session:
        from src.db.repositories import DataSourceRepository, SourceArtifactRepository
        from src.db.allstats_repositories import RosterMembershipRepository
        DataSourceRepository(session).upsert(
            source_key="sports_reference_allstats", display_name="x", acquisition_type="static_file",
            is_rescrapable=False, description="x",
        )
        roster_meta = artifact.discovered.files["Roster"]
        artifact_row = SourceArtifactRepository(session).upsert(
            source_key="sports_reference_allstats", source_root_key="teamdump_cbb", relative_path=roster_meta.relative_path,
            original_filename="Roster_2026.xlsx", file_type="xlsx", point_in_time_classification="historical_but_not_point_in_time",
            content_hash=artifact.content_hashes["Roster"], team_id=artifact.plan.team_id, season_id=artifact.plan.season_id,
        )
        spec = artifact.plan.roster_membership_specs[0]
        RosterMembershipRepository(session).upsert(
            team_id=artifact.plan.team_id, season_id=artifact.plan.season_id, source_artifact_id=artifact_row.id,
            player_key=spec["player_key"], source_player_name="A Different Pre-Seeded Name",
            validation_status=spec["validation_status"], jersey_number=99,
        )

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_allstats_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
        assert result.status == "failed"
        remaining = conn.execute(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")).scalar_one()
    assert remaining == 0
