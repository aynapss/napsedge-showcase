"""
Controlled bulk-import design, Slice 8 (docs/controlled_bulk_import_design.md
section 16 item 8, approved for this slice only): deterministic unit
tests for `src/pipeline/bulk_import_espn_runner.py` that need no real
database connection at all. Real-PostgreSQL behavior (target verification
success, advisory locking, actual persisted writes, rollback, source/
benchmark reconciliation, lock release) lives in
`tests/postgres/test_postgres_bulk_import_espn_runner.py`.

Every fixture here is synthetic and in-memory -- never the real three
ESPN files under TeamDump/ESPN.
"""
from __future__ import annotations

import ast
import datetime as dt
import inspect

import pandas as pd
import pytest
from sqlalchemy import create_engine

from src.exceptions import DatabaseTargetVerificationError, ValidationError
from src.pipeline.bulk_import_espn_runner import (
    COMMITTABLE_MANIFEST_STATUSES,
    ESPN_BENCHMARK_FILENAME,
    ESPN_ENRICHED_FILENAME,
    ESPN_RAW_PLAYER_STATS_FILENAME,
    EXPECTED_SOURCE_ROOT_KEY,
    FAILED_ARTIFACT_ISSUE_CODE,
    RECONCILIATION_MISMATCH_ISSUE_CODE,
    EspnArtifactOutcome,
    EspnBenchmarkCommitArtifact,
    EspnCommittedImportRunResult,
    EspnSourceStatsCommitArtifact,
    _artifact_identity_text,
    _validate_artifacts,
    _validate_manifest,
    run_espn_committed_import,
)
from src.pipeline.bulk_import_manifest import (
    MANIFEST_SCHEMA_VERSION,
    PlannedArtifact,
    SourceFamilyPlan,
    build_dry_run_manifest,
)
from src.pipeline.bulk_import_safety import DatabaseTargetDesignation, build_expected_acknowledgement
from src.pipeline.espn_import_adapter import (
    ESPN_IMPORT_CONTRACT_VERSION,
    DiscoveredEspnFile,
    build_espn_benchmark_import_plan,
    build_espn_source_import_plan,
    parse_espn_benchmark_workbook,
    parse_espn_player_stats_workbook,
)

SEASON = 2026

# ---------------------------------------------------------------------------
# Synthetic fixture builders (mirrors tests/regression/test_espn_import_adapter.py's own conventions)
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


def _build_source_artifact(*, rows: list[dict] | None = None, season: int = SEASON, hash_suffix: str = "") -> tuple[EspnSourceStatsCommitArtifact, DiscoveredEspnFile, str]:
    df = _raw_stats_df(rows or [{"Name": "Some Player", "Team": "BYU"}])
    parsed = parse_espn_player_stats_workbook(df, season)
    plan = build_espn_source_import_plan(parsed)
    discovered, content_hash = _discovered_and_hash(ESPN_RAW_PLAYER_STATS_FILENAME, hash_suffix=hash_suffix)
    return EspnSourceStatsCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan, season_id=season), discovered, content_hash


def _build_benchmark_artifact(*, rows: list[dict] | None = None, hash_suffix: str = "") -> tuple[EspnBenchmarkCommitArtifact, DiscoveredEspnFile, str]:
    df = _benchmark_df(rows or [{"Metric": "PTS", "NCAA_Mean": 8.4, "NCAA_Std": 4.6}])
    parsed = parse_espn_benchmark_workbook(df)
    plan = build_espn_benchmark_import_plan(parsed)
    discovered, content_hash = _discovered_and_hash(ESPN_BENCHMARK_FILENAME, hash_suffix=hash_suffix)
    return EspnBenchmarkCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan, season_id=SEASON), discovered, content_hash


def _manifest_for(*discovered_hash_pairs, contract_version: str | None = None, family: str = "espn", source_root_key: str = EXPECTED_SOURCE_ROOT_KEY, status: str = "planned_insert") -> object:
    planned = [
        PlannedArtifact(source_root_key=source_root_key, relative_path=discovered.relative_path, content_hash=content_hash, status=status)
        for discovered, content_hash in discovered_hash_pairs
    ]
    family_plan = SourceFamilyPlan(family=family, contract_version=contract_version or ESPN_IMPORT_CONTRACT_VERSION, artifacts=tuple(planned))
    return build_dry_run_manifest(generated_at=dt.datetime.now(dt.timezone.utc), families=[family_plan])


# ---------------------------------------------------------------------------
# Artifact-unit enforcement / construction
# ---------------------------------------------------------------------------


def test_source_stats_artifact_accepts_the_correct_filename():
    artifact, discovered, _ = _build_source_artifact()
    assert artifact.relative_path == ESPN_RAW_PLAYER_STATS_FILENAME
    assert artifact.identity == ("source_stats", SEASON)


def test_source_stats_artifact_rejects_the_benchmark_filename():
    discovered, content_hash = _discovered_and_hash(ESPN_BENCHMARK_FILENAME)
    df = _raw_stats_df([{"Name": "Some Player", "Team": "BYU"}])
    plan = build_espn_source_import_plan(parse_espn_player_stats_workbook(df, SEASON))
    with pytest.raises(ValidationError):
        EspnSourceStatsCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan, season_id=SEASON)


def test_source_stats_artifact_rejects_the_enriched_filename():
    """No committable artifact type exists for the enriched file at all -- structurally enforced here too."""
    discovered, content_hash = _discovered_and_hash(ESPN_ENRICHED_FILENAME)
    df = _raw_stats_df([{"Name": "Some Player", "Team": "BYU"}])
    plan = build_espn_source_import_plan(parse_espn_player_stats_workbook(df, SEASON))
    with pytest.raises(ValidationError):
        EspnSourceStatsCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan, season_id=SEASON)


def test_source_stats_artifact_rejects_season_mismatch():
    discovered, content_hash = _discovered_and_hash(ESPN_RAW_PLAYER_STATS_FILENAME)
    df = _raw_stats_df([{"Name": "Some Player", "Team": "BYU"}])
    plan = build_espn_source_import_plan(parse_espn_player_stats_workbook(df, SEASON))
    with pytest.raises(ValidationError):
        EspnSourceStatsCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan, season_id=SEASON + 1)


def test_source_stats_artifact_rejects_non_plan_type():
    discovered, content_hash = _discovered_and_hash(ESPN_RAW_PLAYER_STATS_FILENAME)
    with pytest.raises(ValidationError):
        EspnSourceStatsCommitArtifact(discovered=discovered, content_hash=content_hash, plan="not-a-plan", season_id=SEASON)


def test_benchmark_artifact_accepts_the_correct_filename():
    artifact, discovered, _ = _build_benchmark_artifact()
    assert artifact.relative_path == ESPN_BENCHMARK_FILENAME
    assert artifact.identity == ("benchmark", SEASON)


def test_benchmark_artifact_rejects_the_source_stats_filename():
    discovered, content_hash = _discovered_and_hash(ESPN_RAW_PLAYER_STATS_FILENAME)
    plan = build_espn_benchmark_import_plan(parse_espn_benchmark_workbook(_benchmark_df([{"Metric": "PTS"}])))
    with pytest.raises(ValidationError):
        EspnBenchmarkCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan)


def test_benchmark_artifact_accepts_none_season():
    discovered, content_hash = _discovered_and_hash(ESPN_BENCHMARK_FILENAME)
    plan = build_espn_benchmark_import_plan(parse_espn_benchmark_workbook(_benchmark_df([{"Metric": "PTS"}])))
    artifact = EspnBenchmarkCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan)
    assert artifact.season_id is None
    assert artifact.identity == ("benchmark", None)


def test_artifacts_are_frozen():
    source_artifact, _, _ = _build_source_artifact()
    benchmark_artifact, _, _ = _build_benchmark_artifact()
    with pytest.raises(Exception):
        source_artifact.plan = None  # type: ignore[misc]
    with pytest.raises(Exception):
        benchmark_artifact.plan = None  # type: ignore[misc]


def test_no_input_mutation_across_artifact_construction():
    df = _raw_stats_df([{"Name": "Some Player", "Team": "BYU"}])
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    plan = build_espn_source_import_plan(parsed)
    discovered, content_hash = _discovered_and_hash(ESPN_RAW_PLAYER_STATS_FILENAME)
    specs_before = list(plan.observation_specs)
    EspnSourceStatsCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan, season_id=SEASON)
    assert plan.observation_specs == specs_before


# ---------------------------------------------------------------------------
# _validate_manifest -- schema/family/contract-version, including the
# ESPN_IMPORT_CONTRACT_VERSION vs TRANSFORMATION_VERSION separation
# ---------------------------------------------------------------------------


def test_validate_manifest_accepts_a_matching_single_family_manifest():
    _, discovered, content_hash = _build_source_artifact()
    manifest = _manifest_for((discovered, content_hash))
    family_plan = _validate_manifest(manifest, MANIFEST_SCHEMA_VERSION)
    assert family_plan.family == "espn"


def test_validate_manifest_uses_import_contract_version_not_transformation_version():
    """A manifest whose own contract_version equals the pre-existing TRANSFORMATION_VERSION (a different concept entirely) must be refused -- they must never be conflated. SourceFamilyPlan's own construction-time check already catches this (proving the separation even earlier than this runner's own _validate_manifest call)."""
    from src.pipeline.espn_import_adapter import TRANSFORMATION_VERSION

    assert TRANSFORMATION_VERSION != ESPN_IMPORT_CONTRACT_VERSION
    _, discovered, content_hash = _build_source_artifact()
    with pytest.raises(ValidationError):
        _manifest_for((discovered, content_hash), contract_version=TRANSFORMATION_VERSION)


def test_validate_manifest_rejects_schema_version_mismatch():
    _, discovered, content_hash = _build_source_artifact()
    manifest = _manifest_for((discovered, content_hash))
    with pytest.raises(ValidationError):
        _validate_manifest(manifest, "not-the-real-schema-version")


def test_validate_manifest_rejects_stale_contract_version():
    import src.pipeline.bulk_import_manifest as manifest_mod

    original = dict(manifest_mod.SOURCE_FAMILY_CONTRACT_VERSIONS)
    try:
        manifest_mod.SOURCE_FAMILY_CONTRACT_VERSIONS["espn"] = "0-stale"
        family_plan = SourceFamilyPlan(family="espn", contract_version="0-stale", artifacts=())
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
    _, discovered, content_hash = _build_source_artifact()
    espn_plan = SourceFamilyPlan(family="espn", contract_version=ESPN_IMPORT_CONTRACT_VERSION, artifacts=(
        PlannedArtifact(source_root_key=EXPECTED_SOURCE_ROOT_KEY, relative_path=discovered.relative_path, content_hash=content_hash, status="planned_insert"),
    ))
    from src.pipeline.allstats_import_adapter import ALLSTATS_IMPORT_CONTRACT_VERSION
    allstats_plan = SourceFamilyPlan(family="allstats", contract_version=ALLSTATS_IMPORT_CONTRACT_VERSION, artifacts=())
    mixed_manifest = build_dry_run_manifest(generated_at=dt.datetime.now(dt.timezone.utc), families=[espn_plan, allstats_plan])
    with pytest.raises(ValidationError):
        _validate_manifest(mixed_manifest, MANIFEST_SCHEMA_VERSION)


def test_validate_manifest_accepts_family_only_manifest_with_zero_planned_artifacts():
    empty_family = SourceFamilyPlan(family="espn", contract_version=ESPN_IMPORT_CONTRACT_VERSION, artifacts=())
    manifest = build_dry_run_manifest(generated_at=dt.datetime.now(dt.timezone.utc), families=[empty_family])
    family_plan = _validate_manifest(manifest, MANIFEST_SCHEMA_VERSION)
    assert family_plan.artifacts == ()


# ---------------------------------------------------------------------------
# _validate_artifacts
# ---------------------------------------------------------------------------


def test_validate_artifacts_accepts_both_kinds_together():
    source_artifact, s_discovered, s_hash = _build_source_artifact()
    benchmark_artifact, b_discovered, b_hash = _build_benchmark_artifact()
    manifest = _manifest_for((s_discovered, s_hash), (b_discovered, b_hash))
    ordered = _validate_artifacts(manifest.families[0], [benchmark_artifact, source_artifact])
    assert ordered == (benchmark_artifact, source_artifact)  # deterministic: "benchmark" < "source_stats"


def test_validate_artifacts_rejects_duplicate_identity():
    artifact, discovered, content_hash = _build_source_artifact()
    manifest = _manifest_for((discovered, content_hash))
    with pytest.raises(ValidationError):
        _validate_artifacts(manifest.families[0], [artifact, artifact])


def test_validate_artifacts_rejects_unplanned_artifact():
    artifact, discovered, content_hash = _build_source_artifact()
    empty_family_plan = SourceFamilyPlan(family="espn", contract_version=ESPN_IMPORT_CONTRACT_VERSION, artifacts=())
    with pytest.raises(ValidationError):
        _validate_artifacts(empty_family_plan, [artifact])


def test_validate_artifacts_rejects_missing_planned_artifact():
    _, discovered, content_hash = _build_source_artifact()
    manifest = _manifest_for((discovered, content_hash))
    with pytest.raises(ValidationError):
        _validate_artifacts(manifest.families[0], [])


def test_validate_artifacts_exempts_the_enriched_file_from_the_completeness_check():
    """A manifest may legitimately record the enriched file as a discovered, dry-run-only entry -- no committable artifact type exists for it, so it must never trigger 'missing planned artifact'."""
    artifact, discovered, content_hash = _build_source_artifact()
    enriched_discovered, enriched_hash = _discovered_and_hash(ESPN_ENRICHED_FILENAME)
    manifest = _manifest_for((discovered, content_hash), (enriched_discovered, enriched_hash))
    ordered = _validate_artifacts(manifest.families[0], [artifact])
    assert ordered == (artifact,)


def test_validate_artifacts_rejects_stale_changed_content_hash():
    artifact, discovered, content_hash = _build_source_artifact()
    manifest = _manifest_for((discovered, content_hash))
    changed_artifact, _, _ = _build_source_artifact(hash_suffix="-CHANGED")
    with pytest.raises(ValidationError):
        _validate_artifacts(manifest.families[0], [changed_artifact])


def test_validate_artifacts_rejects_mismatched_source_root_key():
    artifact, discovered, content_hash = _build_source_artifact()
    manifest = _manifest_for((discovered, content_hash), source_root_key="wrong_root_key")
    with pytest.raises(ValidationError):
        _validate_artifacts(manifest.families[0], [artifact])


def test_validate_artifacts_rejects_non_committable_manifest_status():
    artifact, discovered, content_hash = _build_source_artifact()
    manifest = _manifest_for((discovered, content_hash), status="unresolved")
    with pytest.raises(ValidationError):
        _validate_artifacts(manifest.families[0], [artifact])


@pytest.mark.parametrize("status", sorted(COMMITTABLE_MANIFEST_STATUSES))
def test_validate_artifacts_accepts_every_committable_status(status):
    artifact, discovered, content_hash = _build_source_artifact()
    manifest = _manifest_for((discovered, content_hash), status=status)
    ordered = _validate_artifacts(manifest.families[0], [artifact])
    assert ordered == (artifact,)


def test_validate_artifacts_rejects_non_artifact_element():
    _, discovered, content_hash = _build_source_artifact()
    manifest = _manifest_for((discovered, content_hash))
    with pytest.raises(ValidationError):
        _validate_artifacts(manifest.families[0], ["not-an-artifact"])


def test_validate_artifacts_rejects_a_bare_sequence_type_error():
    _, discovered, content_hash = _build_source_artifact()
    manifest = _manifest_for((discovered, content_hash))
    with pytest.raises(ValidationError):
        _validate_artifacts(manifest.families[0], "not-a-sequence-of-artifacts")


# ---------------------------------------------------------------------------
# run_espn_committed_import: target-verification failure (no real Postgres needed)
# ---------------------------------------------------------------------------


def test_runner_refuses_a_non_postgres_connection_before_any_other_step():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    designation = DatabaseTargetDesignation(expected_database_name="napsedge_pg_test_abc", acknowledgement=build_expected_acknowledgement("napsedge_pg_test_abc"))
    with engine.connect() as conn:
        with pytest.raises(DatabaseTargetVerificationError):
            run_espn_committed_import(
                connection=conn, target_designation=designation, session_factory=None,
                manifest="not-even-a-real-manifest", manifest_schema_version="wrong-too",
                artifacts=["not-even-a-real-artifact"], run_id="run-1",
                started_at=dt.datetime.now(dt.timezone.utc),
            )


# ---------------------------------------------------------------------------
# AST guards -- no engine/session construction, no env/dotenv reads, no
# Python hash(), no direct commit/rollback/flush calls in this module's own code.
# ---------------------------------------------------------------------------


def test_module_never_constructs_its_own_engine_session_or_connection():
    import src.pipeline.bulk_import_espn_runner as mod

    tree = ast.parse(inspect.getsource(mod))
    called_names = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not ({"create_engine", "sessionmaker", "Session", "build_engine", "build_session_factory", "connect"} & called_names)


def test_module_never_reads_environment_or_dotenv():
    import src.pipeline.bulk_import_espn_runner as mod

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
    import src.pipeline.bulk_import_espn_runner as mod

    tree = ast.parse(inspect.getsource(mod))
    called_names = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "hash" not in called_names


def test_module_never_calls_commit_rollback_flush_add_delete_on_a_session():
    import src.pipeline.bulk_import_espn_runner as mod

    tree = ast.parse(inspect.getsource(mod))
    session_calls = {
        n.func.attr for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "session"
    }
    assert not ({"commit", "rollback", "flush", "add", "delete", "merge"} & session_calls)


def test_module_never_imports_filesystem_or_discovery_machinery():
    import src.pipeline.bulk_import_espn_runner as mod

    tree = ast.parse(inspect.getsource(mod))
    imported_names: set[str] = set()
    imported_froms: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_froms.update(a.name for a in node.names)
    assert "pathlib" not in imported_names
    assert not ({"discover_espn_artifacts", "load_espn_dataframe", "parse_espn_player_stats_workbook", "parse_espn_benchmark_workbook"} & imported_froms)


def test_module_never_invokes_another_source_family_adapter():
    import src.pipeline.bulk_import_espn_runner as mod

    tree = ast.parse(inspect.getsource(mod))
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    assert not any(m.endswith(("allstats_import_adapter", "game_log_import_adapter", "player_game_log_import_adapter")) for m in imported_modules)


def test_module_does_not_import_the_other_runners():
    import src.pipeline.bulk_import_espn_runner as mod

    source = inspect.getsource(mod)
    assert "bulk_import_allstats_runner" not in source
    assert "bulk_import_game_log_runner" not in source
    assert "bulk_import_player_game_log_runner" not in source


# ---------------------------------------------------------------------------
# No credential / absolute-path / source-content leakage in errors
# ---------------------------------------------------------------------------


def test_validation_errors_never_leak_an_absolute_path():
    artifact, discovered, content_hash = _build_source_artifact()
    empty_family_plan = SourceFamilyPlan(family="espn", contract_version=ESPN_IMPORT_CONTRACT_VERSION, artifacts=())
    with pytest.raises(ValidationError) as excinfo:
        _validate_artifacts(empty_family_plan, [artifact])
    message = str(excinfo.value)
    assert "C:\\" not in message
    assert not message.startswith("/")


def test_failure_context_text_never_includes_password_or_url_shaped_content():
    artifact, _, _ = _build_source_artifact()
    text = _artifact_identity_text(artifact)
    assert "password" not in text.lower()
    assert "://" not in text
