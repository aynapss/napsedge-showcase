"""
Controlled bulk-import design, Slice 7 (docs/controlled_bulk_import_design.md
section 16 item 7, approved for this slice only): real PostgreSQL
integration tests for `src/pipeline/bulk_import_player_game_log_runner.py`.

This is the only test file that exercises `run_player_game_log_committed_import`
end-to-end -- target verification, advisory locking, real persisted
writes through the existing, unmodified `persist_player_game_log_import_plan`,
real rollback on a forced conflict, real Game-row reuse across multiple
player files, and real pipeline-run finalization. Every fixture is
synthetic and in-memory (never TeamDump); the only "real" thing here is
the disposable PostgreSQL container from tests/postgres/conftest.py.
"""
from __future__ import annotations

import datetime as dt
import uuid

import pandas as pd
import pytest
from sqlalchemy import create_engine, event, text

from src.db import game_log_models, models, player_game_log_models
from src.db.repositories import SeasonRepository
from src.db.session import build_engine, build_session_factory, unit_of_work
from src.exceptions import AdvisoryLockUnavailableError, DatabaseTargetVerificationError, IdentityConflictError, ValidationError
from src.identity.player_identity import build_player_identity
from src.identity.season_identity import resolve_season_identity
from src.identity.team_identity import resolve_team_identity
from src.pipeline.bulk_import_manifest import MANIFEST_SCHEMA_VERSION, PlannedArtifact, SourceFamilyPlan, build_dry_run_manifest
from src.pipeline.bulk_import_player_game_log_runner import (
    EXPECTED_SOURCE_ROOT_KEY,
    FAILED_ARTIFACT_ISSUE_CODE,
    PlayerGameLogCommitArtifact,
    run_player_game_log_committed_import,
)
from src.pipeline.bulk_import_safety import DatabaseTargetDesignation, build_expected_acknowledgement, bulk_import_advisory_lock, verify_database_target
from src.pipeline.player_game_log_import_adapter import (
    PLAYER_GAMELOG_IMPORT_CONTRACT_VERSION,
    DiscoveredPlayerGameLogFile,
    build_player_game_log_import_plan,
    parse_player_game_log_workbook,
)

pytestmark = pytest.mark.postgres

SEASON = 2026


# ---------------------------------------------------------------------------
# Synthetic fixture builders (same convention as the SQLite runner tests
# and tests/regression/test_player_game_log_import_adapter.py -- never TeamDump)
# ---------------------------------------------------------------------------


def _gamelog_df(rows: list[dict]) -> pd.DataFrame:
    base = {"game_num": 1, "date": "2026-01-15", "opponent": "Colgate", "result": "W 80-70", "mp": 30, "pts": 10, "trb": 5, "ast": 3, "stl": 1, "blk": 0, "tov": 2, "fg": 4, "fga": 8}
    return pd.DataFrame([{**base, **r} for r in rows])


def _roster(names, team_id):
    return [build_player_identity(n, team_id, SEASON) for n in names]


def _discovered_and_hash(team_slug: str, player_slug: str, *, season: int = SEASON, hash_suffix: str = "") -> tuple[DiscoveredPlayerGameLogFile, str]:
    discovered = DiscoveredPlayerGameLogFile(
        team_slug=team_slug, player_slug=player_slug, season=season,
        relative_path=f"{team_slug}/{player_slug}_{season}_gamelog.csv", file_size_bytes=100,
        filesystem_mtime=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
    )
    return discovered, f"hash-{team_slug}-{player_slug}{hash_suffix}"


def _build_artifact(*, team_slug: str, team_query: str, player_slug: str, player_name: str, rows: list[dict], season: int = SEASON, hash_suffix: str = "") -> PlayerGameLogCommitArtifact:
    team_identity = resolve_team_identity(team_query, source="sports_reference")
    df = _gamelog_df(rows)
    parsed = parse_player_game_log_workbook(
        df, team_identity=team_identity, season_identity=resolve_season_identity(season),
        roster_identities=_roster([player_name], team_identity.team_id),
        team_slug=team_slug, player_slug=player_slug, season=season,
    )
    plan = build_player_game_log_import_plan(parsed)
    discovered, content_hash = _discovered_and_hash(team_slug, player_slug, season=season, hash_suffix=hash_suffix)
    return PlayerGameLogCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan)


def _manifest_for(*artifacts: PlayerGameLogCommitArtifact, contract_version: str | None = None) -> object:
    planned = [
        PlannedArtifact(source_root_key=EXPECTED_SOURCE_ROOT_KEY, relative_path=artifact.discovered.relative_path, content_hash=artifact.content_hash, status="planned_insert")
        for artifact in artifacts
    ]
    family_plan = SourceFamilyPlan(family="player_game_log", contract_version=contract_version or PLAYER_GAMELOG_IMPORT_CONTRACT_VERSION, artifacts=tuple(planned))
    return build_dry_run_manifest(generated_at=dt.datetime.now(dt.timezone.utc), families=[family_plan])


def _seed_season(session_factory, season_id: int = SEASON) -> None:
    """persist_player_game_log_import_plan registers every team/player itself -- only Season needs pre-seeding, matching tests/regression/test_player_game_log_import_adapter.py's own convention."""
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


def test_valid_one_artifact_import(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(team_slug="michigan-state", team_query="Michigan State", player_slug="a_player", player_name="A Player", rows=[{"opponent": "Colgate"}])
    _seed_season(pg_session_factory)
    manifest = _manifest_for(artifact)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_player_game_log_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )

    assert result.status == "succeeded"
    assert result.committed_count == 1
    with unit_of_work(pg_session_factory) as session:
        assert session.query(player_game_log_models.PlayerGameLogObservation).count() == 1
        assert session.query(models.Game).count() == 1
        assert session.query(game_log_models.GameResult).count() == 0  # this adapter never writes game_results
        assert session.get(models.PipelineRun, result.run_id).status == "succeeded"


def test_valid_multiple_artifact_import_and_deterministic_order(pg_session_factory, pg_connection_engine):
    artifact_b = _build_artifact(team_slug="michigan-state", team_query="Michigan State", player_slug="b_player", player_name="B Player", rows=[{"opponent": "Colgate", "date": "2026-01-20"}])
    artifact_a = _build_artifact(team_slug="michigan-state", team_query="Michigan State", player_slug="a_player", player_name="A Player", rows=[{"opponent": "Colgate", "date": "2026-01-15"}])
    _seed_season(pg_session_factory)
    manifest = _manifest_for(artifact_a, artifact_b)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_player_game_log_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION,
            artifacts=[artifact_b, artifact_a],  # deliberately reversed input order
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )

    assert result.status == "succeeded"
    assert [o.player_slug for o in result.artifact_outcomes] == ["a_player", "b_player"]
    with unit_of_work(pg_session_factory) as session:
        assert session.query(player_game_log_models.PlayerGameLogObservation).count() == 2


# ---------------------------------------------------------------------------
# Multiple player files reusing one canonical Game, no duplication
# ---------------------------------------------------------------------------


def test_multiple_player_files_reuse_one_canonical_game_without_duplication(pg_session_factory, pg_connection_engine):
    """Two teammates on the SAME team, same game -- their two player files construct the identical deterministic game_id and must converge on one Game row, never two."""
    player_a = _build_artifact(team_slug="michigan-state", team_query="Michigan State", player_slug="a_player", player_name="A Player", rows=[{"opponent": "Colgate", "date": "2026-01-15"}])
    player_b = _build_artifact(team_slug="michigan-state", team_query="Michigan State", player_slug="b_player", player_name="B Player", rows=[{"opponent": "Colgate", "date": "2026-01-15"}])
    _seed_season(pg_session_factory)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_player_game_log_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=_manifest_for(player_a, player_b), manifest_schema_version=MANIFEST_SCHEMA_VERSION,
            artifacts=[player_a, player_b],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )

    assert result.status == "succeeded"
    with unit_of_work(pg_session_factory) as session:
        games = session.query(models.Game).all()
        assert len(games) == 1  # one shared Game, never duplicated
        observations = session.query(player_game_log_models.PlayerGameLogObservation).all()
        assert len(observations) == 2
        assert {o.game_id for o in observations} == {games[0].game_id}
        assert session.query(game_log_models.GameResult).count() == 0


def test_cross_team_mirrored_player_files_also_converge_on_one_game(pg_session_factory, pg_connection_engine):
    """A player from each of the two opposing teams, same real game -- must also converge on the identical provisional game_id (same scheme the team-level adapter uses)."""
    msu_player = _build_artifact(team_slug="michigan-state", team_query="Michigan State", player_slug="a_player", player_name="A Player", rows=[{"opponent": "Colgate", "date": "2026-01-15"}])
    colgate_player = _build_artifact(team_slug="colgate", team_query="Colgate", player_slug="b_player", player_name="B Player", rows=[{"opponent": "Michigan State", "date": "2026-01-15"}])
    _seed_season(pg_session_factory)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_player_game_log_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=_manifest_for(msu_player, colgate_player), manifest_schema_version=MANIFEST_SCHEMA_VERSION,
            artifacts=[msu_player, colgate_player],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )

    assert result.status == "succeeded"
    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.Game).count() == 1
        assert session.query(game_log_models.GameResult).count() == 0


# ---------------------------------------------------------------------------
# Idempotency and coexistence
# ---------------------------------------------------------------------------


def test_identical_replay_is_idempotent_no_duplicate_rows(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(team_slug="michigan-state", team_query="Michigan State", player_slug="a_player", player_name="A Player", rows=[{"opponent": "Colgate"}])
    _seed_season(pg_session_factory)
    manifest = _manifest_for(artifact)

    def _run():
        with pg_connection_engine.connect() as conn:
            designation = _designation_for(_real_database_name(conn))
            return run_player_game_log_committed_import(
                connection=conn, target_designation=designation, session_factory=pg_session_factory,
                manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
                run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
            )

    first = _run()
    second = _run()
    assert first.status == "succeeded"
    assert second.status == "succeeded"
    with unit_of_work(pg_session_factory) as session:
        assert session.query(player_game_log_models.PlayerGameLogObservation).count() == 1
        assert session.query(models.SourceArtifact).count() == 1


def test_different_immutable_snapshot_coexists_with_the_original(pg_session_factory, pg_connection_engine):
    original = _build_artifact(team_slug="michigan-state", team_query="Michigan State", player_slug="a_player", player_name="A Player", rows=[{"opponent": "Colgate"}])
    changed = _build_artifact(team_slug="michigan-state", team_query="Michigan State", player_slug="a_player", player_name="A Player", rows=[{"opponent": "Colgate"}], hash_suffix="-v2")
    _seed_season(pg_session_factory)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result_1 = run_player_game_log_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=_manifest_for(original), manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[original],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
        result_2 = run_player_game_log_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=_manifest_for(changed), manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[changed],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )

    assert result_1.status == "succeeded"
    assert result_2.status == "succeeded"
    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.SourceArtifact).filter(models.SourceArtifact.relative_path == original.discovered.relative_path).count() == 2
        assert session.query(player_game_log_models.PlayerGameLogObservation).count() == 2  # two coexisting snapshots' worth of rows


# ---------------------------------------------------------------------------
# Source-row-position and resolution states
# ---------------------------------------------------------------------------


def test_exact_source_row_position_persisted_for_every_row(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(
        team_slug="michigan-state", team_query="Michigan State", player_slug="a_player", player_name="A Player",
        rows=[{"opponent": "Colgate", "date": "2026-01-15"}, {"opponent": "Bucknell", "date": "2026-01-20"}],
    )
    _seed_season(pg_session_factory)
    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_player_game_log_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=_manifest_for(artifact), manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
    assert result.status == "succeeded"
    with unit_of_work(pg_session_factory) as session:
        rows = sorted(session.query(player_game_log_models.PlayerGameLogObservation).all(), key=lambda r: r.source_row_position)
        assert [r.source_row_position for r in rows] == [0, 1]
        assert [r.raw_opponent_name for r in rows] == ["Colgate", "Bucknell"]


def test_established_player_and_opponent_resolution_states_preserved(pg_session_factory, pg_connection_engine):
    """Unresolved opponent (never seen before) -> observation persisted as raw evidence, but no fake team/game created."""
    artifact = _build_artifact(
        team_slug="michigan-state", team_query="Michigan State", player_slug="a_player", player_name="A Player",
        rows=[{"opponent": "Definitely Not A Real School XYZ"}],
    )
    _seed_season(pg_session_factory)
    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_player_game_log_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=_manifest_for(artifact), manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
    assert result.status == "succeeded"
    with unit_of_work(pg_session_factory) as session:
        obs = session.query(player_game_log_models.PlayerGameLogObservation).one()
        assert obs.opponent_team_id is None
        assert obs.game_id is None
        assert session.query(models.Game).count() == 0


def test_real_63_bit_game_id_round_trips(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(team_slug="michigan-state", team_query="Michigan State", player_slug="a_player", player_name="A Player", rows=[{"opponent": "Colgate"}])
    _seed_season(pg_session_factory)
    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_player_game_log_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=_manifest_for(artifact), manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
    assert result.status == "succeeded"
    with unit_of_work(pg_session_factory) as session:
        obs = session.query(player_game_log_models.PlayerGameLogObservation).one()
        assert obs.game_id is not None
        assert 0 <= obs.game_id < 2**63
        assert session.get(models.Game, obs.game_id) is not None


# ---------------------------------------------------------------------------
# Target verification / advisory lock, before any write
# ---------------------------------------------------------------------------


def test_target_verification_failure_before_any_write(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(team_slug="michigan-state", team_query="Michigan State", player_slug="a_player", player_name="A Player", rows=[{"opponent": "Colgate"}])
    _seed_season(pg_session_factory)
    manifest = _manifest_for(artifact)

    with pg_connection_engine.connect() as conn:
        real_name = _real_database_name(conn)
        wrong_designation = _designation_for(real_name + "_wrong")
        with pytest.raises(DatabaseTargetVerificationError):
            run_player_game_log_committed_import(
                connection=conn, target_designation=wrong_designation, session_factory=pg_session_factory,
                manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
                run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
            )

    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.PipelineRun).count() == 0
        assert session.query(player_game_log_models.PlayerGameLogObservation).count() == 0


def test_advisory_lock_contention_before_any_write(pg_session_factory, pg_connection_engine, pg_migrated_database_url):
    artifact = _build_artifact(team_slug="michigan-state", team_query="Michigan State", player_slug="a_player", player_name="A Player", rows=[{"opponent": "Colgate"}])
    _seed_season(pg_session_factory)
    manifest = _manifest_for(artifact)

    other_engine = create_engine(pg_migrated_database_url)
    try:
        with pg_connection_engine.connect() as conn, other_engine.connect() as other_conn:
            designation = _designation_for(_real_database_name(conn))
            verified = verify_database_target(other_conn, designation)
            with bulk_import_advisory_lock(other_conn, verified):
                with pytest.raises(AdvisoryLockUnavailableError):
                    run_player_game_log_committed_import(
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
    good = _build_artifact(team_slug="michigan-state", team_query="Michigan State", player_slug="a_player", player_name="A Player", rows=[{"opponent": "Colgate", "date": "2026-01-15"}])
    conflicting = _build_artifact(team_slug="michigan-state", team_query="Michigan State", player_slug="b_player", player_name="B Player", rows=[{"opponent": "Bucknell", "date": "2026-01-20", "pts": 10}])
    never_reached = _build_artifact(team_slug="michigan-state", team_query="Michigan State", player_slug="c_player", player_name="C Player", rows=[{"opponent": "Army", "date": "2026-01-25"}])
    _seed_season(pg_session_factory)
    manifest = _manifest_for(good, conflicting, never_reached)

    with unit_of_work(pg_session_factory) as session:
        from src.db.repositories import DataSourceRepository, SourceArtifactRepository, TeamRepository
        from src.db.player_game_log_repositories import PlayerGameLogObservationRepository
        DataSourceRepository(session).upsert(
            source_key="sports_reference_player_game_log", display_name="x", acquisition_type="static_file",
            is_rescrapable=False, description="x",
        )
        TeamRepository(session).upsert(team_id=conflicting.plan.team_id, display_name=conflicting.plan.team_spec["display_name"], sr_slug=conflicting.plan.team_spec["sr_slug"])
        artifact_row = SourceArtifactRepository(session).upsert(
            source_key="sports_reference_player_game_log", source_root_key=EXPECTED_SOURCE_ROOT_KEY, relative_path=conflicting.discovered.relative_path,
            original_filename="conflicting.csv", file_type="csv", point_in_time_classification="historical_but_not_point_in_time",
            content_hash=conflicting.content_hash, team_id=conflicting.plan.team_id, season_id=conflicting.plan.season_id,
        )
        spec = conflicting.plan.observation_specs[0]
        PlayerGameLogObservationRepository(session).upsert(
            source_artifact_id=artifact_row.id, source_row_position=spec["source_row_position"], season_id=conflicting.plan.season_id,
            team_id=conflicting.plan.team_id, raw_player_name="A Completely Different Raw Player Name",
            raw_opponent_name=spec["raw_opponent_name"], participation_status=spec["participation_status"],
            validation_status=spec["validation_status"],
        )

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_player_game_log_committed_import(
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
        good_obs = session.query(player_game_log_models.PlayerGameLogObservation).filter(player_game_log_models.PlayerGameLogObservation.raw_player_name == "A Player").all()
        assert len(good_obs) == 1
        # Scoped by the pre-seeded row's own source_artifact_id (not team_id/source_row_position alone,
        # since `good`'s own row shares both of those values -- only source_artifact_id distinguishes them).
        conflicting_rows = session.query(player_game_log_models.PlayerGameLogObservation).filter(player_game_log_models.PlayerGameLogObservation.source_artifact_id == artifact_row.id).all()
        assert len(conflicting_rows) == 1
        assert conflicting_rows[0].raw_player_name == "A Completely Different Raw Player Name"  # untouched by the failed attempt
        never_reached_obs = session.query(player_game_log_models.PlayerGameLogObservation).filter(player_game_log_models.PlayerGameLogObservation.raw_player_name == "C Player").count()
        assert never_reached_obs == 0

        run_row = session.get(models.PipelineRun, result.run_id)
        assert run_row.status == "failed"

        failure_issues = session.query(models.DataQualityIssue).filter(
            models.DataQualityIssue.pipeline_run_id == result.run_id,
            models.DataQualityIssue.issue_code == FAILED_ARTIFACT_ISSUE_CODE,
        ).all()
        assert len(failure_issues) == 1
        assert failure_issues[0].source_artifact_id is None
        assert "player_slug='b_player'" in failure_issues[0].context
        assert "password" not in failure_issues[0].context.lower()


# ---------------------------------------------------------------------------
# Manifest / artifact validation, wired end-to-end
# ---------------------------------------------------------------------------


def test_manifest_contract_version_mismatch_refused_end_to_end(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(team_slug="michigan-state", team_query="Michigan State", player_slug="a_player", player_name="A Player", rows=[{"opponent": "Colgate"}])
    _seed_season(pg_session_factory)

    import src.pipeline.bulk_import_manifest as manifest_mod
    original = dict(manifest_mod.SOURCE_FAMILY_CONTRACT_VERSIONS)
    try:
        manifest_mod.SOURCE_FAMILY_CONTRACT_VERSIONS["player_game_log"] = "0-stale"
        stale_manifest = _manifest_for(artifact, contract_version="0-stale")
    finally:
        manifest_mod.SOURCE_FAMILY_CONTRACT_VERSIONS.clear()
        manifest_mod.SOURCE_FAMILY_CONTRACT_VERSIONS.update(original)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        with pytest.raises(ValidationError):
            run_player_game_log_committed_import(
                connection=conn, target_designation=designation, session_factory=pg_session_factory,
                manifest=stale_manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
                run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
            )
    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.PipelineRun).count() == 0


def test_duplicate_artifact_refused_end_to_end(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(team_slug="michigan-state", team_query="Michigan State", player_slug="a_player", player_name="A Player", rows=[{"opponent": "Colgate"}])
    _seed_season(pg_session_factory)
    manifest = _manifest_for(artifact)

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        with pytest.raises(ValidationError):
            run_player_game_log_committed_import(
                connection=conn, target_designation=designation, session_factory=pg_session_factory,
                manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact, artifact],
                run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
            )
    with unit_of_work(pg_session_factory) as session:
        assert session.query(models.PipelineRun).count() == 0


def test_empty_manifest_and_empty_artifact_list_succeeds_trivially(pg_session_factory, pg_connection_engine):
    empty_family = SourceFamilyPlan(family="player_game_log", contract_version=PLAYER_GAMELOG_IMPORT_CONTRACT_VERSION, artifacts=())
    manifest = build_dry_run_manifest(generated_at=dt.datetime.now(dt.timezone.utc), families=[empty_family])
    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_player_game_log_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
    assert result.status == "succeeded"
    assert result.artifact_outcomes == ()


# ---------------------------------------------------------------------------
# Independent reconciliation matching persisted state / no unexpected GameResult
# ---------------------------------------------------------------------------


def test_no_unexpected_game_result_creation_across_a_full_run(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(team_slug="michigan-state", team_query="Michigan State", player_slug="a_player", player_name="A Player", rows=[{"opponent": "Colgate"}])
    _seed_season(pg_session_factory)
    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_player_game_log_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=_manifest_for(artifact), manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
    assert result.status == "succeeded"
    with unit_of_work(pg_session_factory) as session:
        assert session.query(game_log_models.GameResult).count() == 0


# ---------------------------------------------------------------------------
# Lock release after success and after failure
# ---------------------------------------------------------------------------


def test_lock_released_after_a_successful_run(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(team_slug="michigan-state", team_query="Michigan State", player_slug="a_player", player_name="A Player", rows=[{"opponent": "Colgate"}])
    _seed_season(pg_session_factory)
    manifest = _manifest_for(artifact)
    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_player_game_log_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
        assert result.status == "succeeded"
        remaining = conn.execute(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")).scalar_one()
    assert remaining == 0


def test_lock_released_after_a_failed_run(pg_session_factory, pg_connection_engine):
    artifact = _build_artifact(team_slug="michigan-state", team_query="Michigan State", player_slug="a_player", player_name="A Player", rows=[{"opponent": "Colgate", "pts": 10}])
    _seed_season(pg_session_factory)
    manifest = _manifest_for(artifact)

    with unit_of_work(pg_session_factory) as session:
        from src.db.repositories import DataSourceRepository, SourceArtifactRepository, TeamRepository
        from src.db.player_game_log_repositories import PlayerGameLogObservationRepository
        DataSourceRepository(session).upsert(
            source_key="sports_reference_player_game_log", display_name="x", acquisition_type="static_file",
            is_rescrapable=False, description="x",
        )
        TeamRepository(session).upsert(team_id=artifact.plan.team_id, display_name=artifact.plan.team_spec["display_name"], sr_slug=artifact.plan.team_spec["sr_slug"])
        artifact_row = SourceArtifactRepository(session).upsert(
            source_key="sports_reference_player_game_log", source_root_key=EXPECTED_SOURCE_ROOT_KEY, relative_path=artifact.discovered.relative_path,
            original_filename="x.csv", file_type="csv", point_in_time_classification="historical_but_not_point_in_time",
            content_hash=artifact.content_hash, team_id=artifact.plan.team_id, season_id=artifact.plan.season_id,
        )
        spec = artifact.plan.observation_specs[0]
        PlayerGameLogObservationRepository(session).upsert(
            source_artifact_id=artifact_row.id, source_row_position=spec["source_row_position"], season_id=artifact.plan.season_id,
            team_id=artifact.plan.team_id, raw_player_name="A Different Raw Player Name",
            raw_opponent_name=spec["raw_opponent_name"], participation_status=spec["participation_status"],
            validation_status=spec["validation_status"],
        )

    with pg_connection_engine.connect() as conn:
        designation = _designation_for(_real_database_name(conn))
        result = run_player_game_log_committed_import(
            connection=conn, target_designation=designation, session_factory=pg_session_factory,
            manifest=manifest, manifest_schema_version=MANIFEST_SCHEMA_VERSION, artifacts=[artifact],
            run_id=uuid.uuid4().hex, started_at=dt.datetime.now(dt.timezone.utc),
        )
        assert result.status == "failed"
        remaining = conn.execute(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")).scalar_one()
    assert remaining == 0


# ---------------------------------------------------------------------------
# Statement-shape isolation: no writes outside player-game-log-owned tables
# ---------------------------------------------------------------------------


def test_no_writes_to_other_source_family_tables(pg_session_factory, pg_connection_engine, pg_migrated_database_url):
    artifact = _build_artifact(team_slug="michigan-state", team_query="Michigan State", player_slug="a_player", player_name="A Player", rows=[{"opponent": "Colgate"}])
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
            result = run_player_game_log_committed_import(
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
        "roster_memberships", "player_stat_snapshots", "team_stat_snapshots",
        "team_game_log_observations", "team_game_stat_lines", "game_results",
    )
    for statement in captured:
        lowered = statement.lower()
        assert not any(f'"{t}"' in lowered or f" {t} " in lowered or lowered.endswith(t) for t in forbidden_tables), f"unexpected write touching a non-player-game-log table: {statement!r}"
