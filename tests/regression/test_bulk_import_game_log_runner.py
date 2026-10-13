"""
Controlled bulk-import design, Slice 6 (docs/controlled_bulk_import_design.md
section 16 item 6, approved for this slice only): deterministic unit tests
for `src/pipeline/bulk_import_game_log_runner.py` that need no real
database connection at all. Real-PostgreSQL behavior (target verification
success, advisory locking, actual persisted writes, rollback, mirrored
game/game-result reconciliation, lock release) lives in
`tests/postgres/test_postgres_bulk_import_game_log_runner.py`.

Every fixture here is synthetic and in-memory -- never the real 304-team
AllStats universe, never a real TeamDump file on disk.
"""
from __future__ import annotations

import ast
import datetime as dt
import inspect

import pandas as pd
import pytest
from sqlalchemy import create_engine

from src.exceptions import DatabaseTargetVerificationError, ValidationError
from src.pipeline.bulk_import_game_log_runner import (
    COMMITTABLE_MANIFEST_STATUSES,
    EXPECTED_SOURCE_ROOT_KEY,
    FAILED_ARTIFACT_ISSUE_CODE,
    RECONCILIATION_MISMATCH_ISSUE_CODE,
    TeamGameLogArtifactOutcome,
    TeamGameLogCommitArtifact,
    TeamGameLogCommittedImportRunResult,
    _artifact_identity_text,
    _validate_artifacts,
    _validate_manifest,
    _verify_game_result_transitions,
    run_team_game_log_committed_import,
)
from src.pipeline.bulk_import_manifest import (
    MANIFEST_SCHEMA_VERSION,
    PlannedArtifact,
    SourceFamilyPlan,
    build_dry_run_manifest,
)
from src.pipeline.bulk_import_safety import DatabaseTargetDesignation, build_expected_acknowledgement
from src.pipeline.game_log_import_adapter import (
    TEAM_GAMELOG_IMPORT_CONTRACT_VERSION,
    DiscoveredGameLogFile,
    build_game_log_import_plan,
    parse_game_log_workbook,
)

SEASON = 2026


# ---------------------------------------------------------------------------
# Synthetic fixture builders (mirrors tests/regression/test_game_log_import_adapter.py's own conventions)
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


def _build_artifact(*, team_slug: str = "test-team", team_query: str = "Michigan State", season: int = SEASON, hash_suffix: str = "", opponent: str = "Colgate") -> tuple[TeamGameLogCommitArtifact, DiscoveredGameLogFile, str]:
    df = _basic_df([{"date": "2026-01-15", "opponent": opponent, "pts_scored": 80, "pts_allowed": 70}])
    parsed = parse_game_log_workbook(df, team_query, season)
    plan = build_game_log_import_plan(parsed)
    discovered, content_hash = _discovered_and_hash(team_slug, season=season, hash_suffix=hash_suffix)
    return TeamGameLogCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan), discovered, content_hash


def _manifest_for(*discovered_hash_pairs, contract_version: str | None = None, family: str = "team_game_log", source_root_key: str = EXPECTED_SOURCE_ROOT_KEY, status: str = "planned_insert") -> object:
    planned = [
        PlannedArtifact(source_root_key=source_root_key, relative_path=discovered.relative_path, content_hash=content_hash, status=status)
        for discovered, content_hash in discovered_hash_pairs
    ]
    family_plan = SourceFamilyPlan(family=family, contract_version=contract_version or TEAM_GAMELOG_IMPORT_CONTRACT_VERSION, artifacts=tuple(planned))
    return build_dry_run_manifest(generated_at=dt.datetime.now(dt.timezone.utc), families=[family_plan])


# ---------------------------------------------------------------------------
# TeamGameLogCommitArtifact construction/validation
# ---------------------------------------------------------------------------


def test_artifact_derives_team_slug_and_season_from_discovered_not_plan():
    """Confirmed real-world case: plan.team_slug ('Michigan State', a display name) legitimately differs from discovered.team_slug ('test-team', a directory slug) -- construction must succeed, never compare the two."""
    artifact, discovered, _ = _build_artifact(team_slug="test-team", team_query="Michigan State")
    assert artifact.team_slug == "test-team"
    assert artifact.season == SEASON
    assert artifact.plan.team_slug == "Michigan State"
    assert artifact.identity == ("test-team", SEASON)


def test_artifact_rejects_plan_season_mismatch():
    df = _basic_df([{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    plan = build_game_log_import_plan(parsed)
    discovered, content_hash = _discovered_and_hash("test-team", season=SEASON + 1)  # mismatched season
    with pytest.raises(ValidationError):
        TeamGameLogCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan)


def test_artifact_rejects_non_discovered_type():
    df = _basic_df([{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    plan = build_game_log_import_plan(parsed)
    with pytest.raises(ValidationError):
        TeamGameLogCommitArtifact(discovered="not-a-discovered-file", content_hash="x", plan=plan)


def test_artifact_rejects_empty_content_hash():
    df = _basic_df([{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    plan = build_game_log_import_plan(parsed)
    discovered, _ = _discovered_and_hash("test-team")
    with pytest.raises(ValidationError):
        TeamGameLogCommitArtifact(discovered=discovered, content_hash="", plan=plan)


def test_artifact_rejects_non_plan_type():
    discovered, content_hash = _discovered_and_hash("test-team")
    with pytest.raises(ValidationError):
        TeamGameLogCommitArtifact(discovered=discovered, content_hash=content_hash, plan="not-a-plan")


def test_artifact_is_frozen():
    artifact, _, _ = _build_artifact()
    with pytest.raises(Exception):
        artifact.plan = None  # type: ignore[misc]


def test_artifact_touched_game_ids_matches_resolved_rows():
    artifact, _, _ = _build_artifact()
    assert len(artifact.touched_game_ids) == 1
    assert all(isinstance(g, int) for g in artifact.touched_game_ids)


def test_artifact_touched_game_sides_matches_touched_game_ids():
    """Regression for a real bug found during Postgres testing: a naive source_count-only transition check incorrectly treated 'the same team re-submits a changed snapshot of its own side' as a required mirrored-convergence, when the correct expectation is that it stays single-sided. touched_game_sides is the fix's own data source."""
    artifact, _, _ = _build_artifact(team_query="Michigan State", opponent="Colgate")
    assert set(artifact.touched_game_sides) == artifact.touched_game_ids
    assert set(artifact.touched_game_sides.values()) <= {"team_a", "team_b"}


def test_artifact_touched_game_sides_empty_when_team_unresolved():
    df = _basic_df([{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    parsed = parse_game_log_workbook(df, "Not A Real Team Name Xyz123", SEASON)
    plan = build_game_log_import_plan(parsed)
    discovered, content_hash = _discovered_and_hash("test-team")
    artifact = TeamGameLogCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan)
    assert artifact.touched_game_sides == {}
    assert artifact.touched_game_ids == frozenset()


# ---------------------------------------------------------------------------
# _validate_manifest
# ---------------------------------------------------------------------------


def test_validate_manifest_accepts_a_matching_single_family_manifest():
    _, discovered, content_hash = _build_artifact()
    manifest = _manifest_for((discovered, content_hash))
    family_plan = _validate_manifest(manifest, MANIFEST_SCHEMA_VERSION)
    assert family_plan.family == "team_game_log"


def test_validate_manifest_rejects_schema_version_mismatch():
    _, discovered, content_hash = _build_artifact()
    manifest = _manifest_for((discovered, content_hash))
    with pytest.raises(ValidationError):
        _validate_manifest(manifest, "not-the-real-schema-version")


def test_validate_manifest_rejects_stale_contract_version():
    import src.pipeline.bulk_import_manifest as manifest_mod

    original = dict(manifest_mod.SOURCE_FAMILY_CONTRACT_VERSIONS)
    try:
        manifest_mod.SOURCE_FAMILY_CONTRACT_VERSIONS["team_game_log"] = "0-stale"
        family_plan = SourceFamilyPlan(family="team_game_log", contract_version="0-stale", artifacts=())
        stale_manifest = build_dry_run_manifest(generated_at=dt.datetime.now(dt.timezone.utc), families=[family_plan])
    finally:
        manifest_mod.SOURCE_FAMILY_CONTRACT_VERSIONS.clear()
        manifest_mod.SOURCE_FAMILY_CONTRACT_VERSIONS.update(original)

    with pytest.raises(ValidationError):
        _validate_manifest(stale_manifest, MANIFEST_SCHEMA_VERSION)


def test_validate_manifest_rejects_empty_manifest_with_zero_families():
    empty_manifest = build_dry_run_manifest(generated_at=dt.datetime.now(dt.timezone.utc), families=[])
    with pytest.raises(ValidationError):
        _validate_manifest(empty_manifest, MANIFEST_SCHEMA_VERSION)


def test_validate_manifest_rejects_mixed_family_manifest():
    _, discovered, content_hash = _build_artifact()
    game_log_plan = SourceFamilyPlan(family="team_game_log", contract_version=TEAM_GAMELOG_IMPORT_CONTRACT_VERSION, artifacts=(
        PlannedArtifact(source_root_key=EXPECTED_SOURCE_ROOT_KEY, relative_path=discovered.relative_path, content_hash=content_hash, status="planned_insert"),
    ))
    from src.pipeline.allstats_import_adapter import ALLSTATS_IMPORT_CONTRACT_VERSION
    allstats_plan = SourceFamilyPlan(family="allstats", contract_version=ALLSTATS_IMPORT_CONTRACT_VERSION, artifacts=())
    mixed_manifest = build_dry_run_manifest(generated_at=dt.datetime.now(dt.timezone.utc), families=[game_log_plan, allstats_plan])
    with pytest.raises(ValidationError):
        _validate_manifest(mixed_manifest, MANIFEST_SCHEMA_VERSION)


def test_validate_manifest_accepts_family_only_manifest_with_zero_planned_artifacts():
    empty_family = SourceFamilyPlan(family="team_game_log", contract_version=TEAM_GAMELOG_IMPORT_CONTRACT_VERSION, artifacts=())
    manifest = build_dry_run_manifest(generated_at=dt.datetime.now(dt.timezone.utc), families=[empty_family])
    family_plan = _validate_manifest(manifest, MANIFEST_SCHEMA_VERSION)
    assert family_plan.artifacts == ()


# ---------------------------------------------------------------------------
# _validate_artifacts
# ---------------------------------------------------------------------------


def test_validate_artifacts_accepts_matching_artifact():
    artifact, discovered, content_hash = _build_artifact()
    manifest = _manifest_for((discovered, content_hash))
    ordered = _validate_artifacts(manifest.families[0], [artifact])
    assert ordered == (artifact,)


def test_validate_artifacts_rejects_duplicate_team_season():
    artifact, discovered, content_hash = _build_artifact()
    manifest = _manifest_for((discovered, content_hash))
    with pytest.raises(ValidationError):
        _validate_artifacts(manifest.families[0], [artifact, artifact])


def test_validate_artifacts_rejects_unplanned_artifact():
    artifact, discovered, content_hash = _build_artifact()
    empty_family_plan = SourceFamilyPlan(family="team_game_log", contract_version=TEAM_GAMELOG_IMPORT_CONTRACT_VERSION, artifacts=())
    with pytest.raises(ValidationError):
        _validate_artifacts(empty_family_plan, [artifact])


def test_validate_artifacts_rejects_missing_planned_artifact():
    """Deliberate Slice 6 divergence from Slice 5: the manifest approves an artifact, but no matching caller artifact is supplied -- this must be refused, not merely skipped."""
    _, discovered, content_hash = _build_artifact()
    manifest = _manifest_for((discovered, content_hash))
    with pytest.raises(ValidationError):
        _validate_artifacts(manifest.families[0], [])


def test_validate_artifacts_rejects_stale_changed_content_hash():
    artifact, discovered, content_hash = _build_artifact()
    manifest = _manifest_for((discovered, content_hash))
    changed_artifact, _, _ = _build_artifact(hash_suffix="-CHANGED")
    with pytest.raises(ValidationError):
        _validate_artifacts(manifest.families[0], [changed_artifact])


def test_validate_artifacts_rejects_mismatched_source_root_key():
    artifact, discovered, content_hash = _build_artifact()
    manifest = _manifest_for((discovered, content_hash), source_root_key="wrong_root_key")
    with pytest.raises(ValidationError):
        _validate_artifacts(manifest.families[0], [artifact])


def test_validate_artifacts_rejects_non_committable_manifest_status():
    artifact, discovered, content_hash = _build_artifact()
    manifest = _manifest_for((discovered, content_hash), status="unresolved")
    with pytest.raises(ValidationError):
        _validate_artifacts(manifest.families[0], [artifact])


@pytest.mark.parametrize("status", sorted(COMMITTABLE_MANIFEST_STATUSES))
def test_validate_artifacts_accepts_every_committable_status(status):
    artifact, discovered, content_hash = _build_artifact()
    manifest = _manifest_for((discovered, content_hash), status=status)
    ordered = _validate_artifacts(manifest.families[0], [artifact])
    assert ordered == (artifact,)


def test_validate_artifacts_rejects_non_artifact_element():
    _, discovered, content_hash = _build_artifact()
    manifest = _manifest_for((discovered, content_hash))
    with pytest.raises(ValidationError):
        _validate_artifacts(manifest.families[0], ["not-an-artifact"])


def test_validate_artifacts_deterministic_order_independent_of_input_order():
    artifact_a, discovered_a, hash_a = _build_artifact(team_slug="team-a", team_query="Michigan State", opponent="Colgate")
    artifact_b, discovered_b, hash_b = _build_artifact(team_slug="team-b", team_query="St. Bonaventure", opponent="Bucknell")
    manifest = _manifest_for((discovered_a, hash_a), (discovered_b, hash_b))
    family_plan = manifest.families[0]

    order_1 = _validate_artifacts(family_plan, [artifact_a, artifact_b])
    order_2 = _validate_artifacts(family_plan, [artifact_b, artifact_a])
    assert order_1 == order_2
    assert [a.team_slug for a in order_1] == ["team-a", "team-b"]


def test_validate_artifacts_rejects_a_bare_sequence_type_error():
    _, discovered, content_hash = _build_artifact()
    manifest = _manifest_for((discovered, content_hash))
    with pytest.raises(ValidationError):
        _validate_artifacts(manifest.families[0], "not-a-sequence-of-artifacts")


# ---------------------------------------------------------------------------
# run_team_game_log_committed_import: target-verification failure (no real Postgres needed)
# ---------------------------------------------------------------------------


def test_runner_refuses_a_non_postgres_connection_before_any_other_step():
    """Target verification happens first, before manifest validation, pipeline-run creation, or any write -- proven by using intentionally-broken (non-manifest/non-artifact) inputs and confirming the *connection* error surfaces instead."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    designation = DatabaseTargetDesignation(expected_database_name="napsedge_pg_test_abc", acknowledgement=build_expected_acknowledgement("napsedge_pg_test_abc"))
    with engine.connect() as conn:
        with pytest.raises(DatabaseTargetVerificationError):
            run_team_game_log_committed_import(
                connection=conn, target_designation=designation, session_factory=None,
                manifest="not-even-a-real-manifest", manifest_schema_version="wrong-too",
                artifacts=["not-even-a-real-artifact"], run_id="run-1",
                started_at=dt.datetime.now(dt.timezone.utc),
            )


# ---------------------------------------------------------------------------
# _verify_game_result_transitions (pure -- no database needed)
# ---------------------------------------------------------------------------


def test_transition_fresh_single_sided_row_accepted():
    before = {}
    after = {7: ("single_sided_provisional", 1, True, False)}
    assert _verify_game_result_transitions({7: "team_a"}, before, after) is None


def test_transition_same_team_resubmits_own_side_stays_single_sided():
    """The exact bug found during Postgres testing: a prior single-sided row for MY OWN side, still no other side present -- must stay single-sided, not be treated as a required mirrored convergence."""
    before = {7: ("single_sided_provisional", 1, True, False)}
    after = {7: ("single_sided_provisional", 1, True, False)}
    assert _verify_game_result_transitions({7: "team_a"}, before, after) is None


def test_transition_mirrored_convergence_to_reconciled_accepted():
    before = {7: ("single_sided_provisional", 1, False, True)}  # other side (team_b) already present
    after = {7: ("reconciled", 2, True, True)}
    assert _verify_game_result_transitions({7: "team_a"}, before, after) is None


def test_transition_mirrored_convergence_to_conflicting_accepted():
    before = {7: ("single_sided_provisional", 1, False, True)}
    after = {7: ("conflicting", 2, True, True)}
    assert _verify_game_result_transitions({7: "team_a"}, before, after) is None


def test_transition_idempotent_replay_of_already_mirrored_row_must_be_unchanged():
    before = {7: ("reconciled", 2, True, True)}
    after = {7: ("reconciled", 2, True, True)}
    assert _verify_game_result_transitions({7: "team_a"}, before, after) is None


def test_transition_rejects_already_mirrored_row_changing_on_replay():
    before = {7: ("reconciled", 2, True, True)}
    after = {7: ("conflicting", 2, True, True)}  # changed when it should have been stable
    assert _verify_game_result_transitions({7: "team_a"}, before, after) is not None


def test_transition_rejects_missing_other_side_but_result_shows_mirrored():
    before = {}  # nothing existed yet
    after = {7: ("reconciled", 2, True, True)}  # claims mirrored despite no prior other-side row
    assert _verify_game_result_transitions({7: "team_a"}, before, after) is not None


def test_transition_rejects_other_side_present_but_result_stays_single_sided():
    before = {7: ("single_sided_provisional", 1, False, True)}  # other side present
    after = {7: ("single_sided_provisional", 1, True, False)}  # failed to converge
    assert _verify_game_result_transitions({7: "team_a"}, before, after) is not None


def test_transition_rejects_missing_row_after_commit():
    assert _verify_game_result_transitions({7: "team_a"}, {}, {}) is not None


def test_transition_rejects_own_side_not_populated_after_commit():
    after = {7: ("single_sided_provisional", 1, False, False)}  # neither side populated -- impossible after this artifact's own write
    assert _verify_game_result_transitions({7: "team_a"}, {}, after) is not None


# ---------------------------------------------------------------------------
# AST guards -- no engine/session construction, no env/dotenv reads, no
# Python hash(), no direct commit/rollback/flush calls in this module's own code.
# ---------------------------------------------------------------------------


def test_module_never_constructs_its_own_engine_session_or_connection():
    import src.pipeline.bulk_import_game_log_runner as mod

    tree = ast.parse(inspect.getsource(mod))
    called_names = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not ({"create_engine", "sessionmaker", "Session", "build_engine", "build_session_factory", "connect"} & called_names)


def test_module_never_reads_environment_or_dotenv():
    import src.pipeline.bulk_import_game_log_runner as mod

    tree = ast.parse(inspect.getsource(mod))
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
    assert "os" not in imported_names
    assert "src.db.config" not in imported_names


def test_module_never_uses_pythons_randomized_hash():
    import src.pipeline.bulk_import_game_log_runner as mod

    tree = ast.parse(inspect.getsource(mod))
    called_names = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "hash" not in called_names


def test_module_never_calls_commit_rollback_flush_add_delete_on_a_session():
    """Every write in this module goes through unit_of_work or an existing repository -- this module's own code never calls session.commit()/.rollback()/.flush()/.add()/.delete()/.merge() itself. Scoped to calls whose receiver is named 'session' so plain-Python set/list methods are never false positives."""
    import src.pipeline.bulk_import_game_log_runner as mod

    tree = ast.parse(inspect.getsource(mod))
    session_calls = {
        n.func.attr for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "session"
    }
    assert not ({"commit", "rollback", "flush", "add", "delete", "merge"} & session_calls)


def test_module_never_imports_filesystem_or_discovery_machinery():
    """This module never discovers/scans TeamDump itself -- structurally confirmed by never importing pathlib or the adapter's own discover_*/load_*/parse_* pure stages (only persist_* and typed contracts)."""
    import src.pipeline.bulk_import_game_log_runner as mod

    tree = ast.parse(inspect.getsource(mod))
    imported_names: set[str] = set()
    imported_froms: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_froms.update(a.name for a in node.names)
    assert "pathlib" not in imported_names
    assert not ({"discover_game_log_artifacts", "load_game_log_dataframe", "parse_game_log_workbook"} & imported_froms)


def test_module_never_invokes_another_source_family_adapter():
    import src.pipeline.bulk_import_game_log_runner as mod

    source = inspect.getsource(mod)
    for forbidden in ("allstats_import_adapter", "espn_import_adapter", "player_game_log_import_adapter"):
        assert forbidden not in source


def test_module_does_not_import_the_allstats_runner():
    """No shared orchestration framework this slice -- the two single-family runners remain independent modules."""
    import src.pipeline.bulk_import_game_log_runner as mod

    source = inspect.getsource(mod)
    assert "bulk_import_allstats_runner" not in source


# ---------------------------------------------------------------------------
# No credential / absolute-path / source-content leakage in errors
# ---------------------------------------------------------------------------


def test_validation_errors_never_leak_an_absolute_path():
    artifact, discovered, content_hash = _build_artifact()
    empty_family_plan = SourceFamilyPlan(family="team_game_log", contract_version=TEAM_GAMELOG_IMPORT_CONTRACT_VERSION, artifacts=())
    with pytest.raises(ValidationError) as excinfo:
        _validate_artifacts(empty_family_plan, [artifact])
    message = str(excinfo.value)
    assert "C:\\" not in message
    assert not message.startswith("/")


def test_failure_context_text_never_includes_password_or_url_shaped_content():
    artifact, _, _ = _build_artifact()
    text = _artifact_identity_text(artifact)
    assert "password" not in text.lower()
    assert "://" not in text
