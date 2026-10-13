"""
Controlled bulk-import design, Slice 5 (docs/controlled_bulk_import_design.md
section 16 item 5, approved for this slice only): deterministic unit tests
for `src/pipeline/bulk_import_allstats_runner.py` that need no real
database connection at all. Real-PostgreSQL behavior (target verification
success, advisory locking, actual persisted writes, rollback, per-artifact
reconciliation, lock release) lives in
`tests/postgres/test_postgres_bulk_import_allstats_runner.py`.

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
    AllStatsArtifactOutcome,
    AllStatsCommitArtifact,
    AllStatsCommittedImportRunResult,
    _validate_artifacts,
    _validate_manifest,
    run_allstats_committed_import,
)
from src.pipeline.bulk_import_manifest import (
    MANIFEST_SCHEMA_VERSION,
    PlannedArtifact,
    SourceFamilyPlan,
    build_dry_run_manifest,
)
from src.pipeline.bulk_import_safety import DatabaseTargetDesignation, build_expected_acknowledgement

SEASON = 2026

# ---------------------------------------------------------------------------
# Synthetic fixture builders (mirrors tests/regression/test_allstats_import_adapter.py's own conventions)
# ---------------------------------------------------------------------------


def _roster_df(rows: list[dict]) -> pd.DataFrame:
    base = {"Player": None, "#": None, "Class": "SR", "Pos": "G", "Height": "6-2", "Weight": 190.0,
            "Hometown": None, "High School": None, "RSCI Top 100": None, "Summary": None}
    return pd.DataFrame([{**base, **r} for r in rows])


def _minimal_dfs(roster_rows: list[dict]) -> dict[str, pd.DataFrame]:
    return {"Roster": _roster_df(roster_rows)}


def _discovered_and_hashes(team_slug: str, dfs: dict[str, pd.DataFrame], *, season: int = SEASON, hash_suffix: str = "") -> tuple[DiscoveredAllStatsWorkbook, dict[str, str]]:
    files, hashes = {}, {}
    for label in dfs:
        files[label] = AllStatsFileMetadata(
            relative_path=f"{team_slug}/{label}_{season}.xlsx", file_size_bytes=100,
            filesystem_mtime=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        )
        hashes[label] = f"hash-{label}{hash_suffix}"
    return DiscoveredAllStatsWorkbook(team_slug=team_slug, season=season, files=files), hashes


def _build_artifact(*, team_slug: str = "test-team", team_query: str = "St. Bonaventure", season: int = SEASON, hash_suffix: str = "", player_name: str = "Frank Mitchell") -> tuple[AllStatsCommitArtifact, DiscoveredAllStatsWorkbook, dict[str, str]]:
    dfs = _minimal_dfs([{"Player": player_name, "#": 0}])
    parsed = parse_allstats_workbook(dfs, team_query, season)
    plan = build_allstats_import_plan(parsed)
    discovered, hashes = _discovered_and_hashes(team_slug, dfs, season=season, hash_suffix=hash_suffix)
    return AllStatsCommitArtifact(discovered=discovered, content_hashes=hashes, plan=plan), discovered, hashes


def _manifest_for(*discovered_hashes_pairs, contract_version: str | None = None, family: str = "allstats") -> object:
    planned = [
        PlannedArtifact(source_root_key="teamdump_cbb", relative_path=meta.relative_path, content_hash=hashes[label], status="planned_insert")
        for discovered, hashes in discovered_hashes_pairs
        for label, meta in discovered.files.items()
    ]
    family_plan = SourceFamilyPlan(family=family, contract_version=contract_version or ALLSTATS_IMPORT_CONTRACT_VERSION, artifacts=tuple(planned))
    return build_dry_run_manifest(generated_at=dt.datetime.now(dt.timezone.utc), families=[family_plan])


# ---------------------------------------------------------------------------
# AllStatsCommitArtifact construction/validation
# ---------------------------------------------------------------------------


def test_artifact_derives_team_slug_and_season_from_discovered_not_plan():
    """The confirmed real-world case: plan.team_slug ('St. Bonaventure', a display name) legitimately differs from discovered.team_slug ('test-team', a directory slug) -- construction must succeed, never compare the two."""
    artifact, discovered, _ = _build_artifact(team_slug="test-team", team_query="St. Bonaventure")
    assert artifact.team_slug == "test-team"
    assert artifact.season == SEASON
    assert artifact.plan.team_slug == "St. Bonaventure"
    assert artifact.identity == ("test-team", SEASON)


def test_artifact_rejects_plan_season_mismatch():
    dfs = _minimal_dfs([{"Player": "Frank Mitchell", "#": 0}])
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    plan = build_allstats_import_plan(parsed)
    discovered, hashes = _discovered_and_hashes("test-team", dfs, season=SEASON + 1)  # mismatched season
    with pytest.raises(ValidationError):
        AllStatsCommitArtifact(discovered=discovered, content_hashes=hashes, plan=plan)


def test_artifact_rejects_empty_files():
    dfs = _minimal_dfs([{"Player": "Frank Mitchell", "#": 0}])
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    plan = build_allstats_import_plan(parsed)
    empty_discovered = DiscoveredAllStatsWorkbook(team_slug="test-team", season=SEASON, files={})
    with pytest.raises(ValidationError):
        AllStatsCommitArtifact(discovered=empty_discovered, content_hashes={}, plan=plan)


def test_artifact_rejects_content_hashes_key_mismatch():
    _, discovered, hashes = _build_artifact()
    bad_hashes = dict(hashes)
    bad_hashes["ExtraLabelNotInFiles"] = "hash-extra"
    dfs = _minimal_dfs([{"Player": "Frank Mitchell", "#": 0}])
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    plan = build_allstats_import_plan(parsed)
    with pytest.raises(ValidationError):
        AllStatsCommitArtifact(discovered=discovered, content_hashes=bad_hashes, plan=plan)


def test_artifact_rejects_non_discovered_workbook_type():
    dfs = _minimal_dfs([{"Player": "Frank Mitchell", "#": 0}])
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    plan = build_allstats_import_plan(parsed)
    with pytest.raises(ValidationError):
        AllStatsCommitArtifact(discovered="not-a-discovered-workbook", content_hashes={}, plan=plan)


def test_artifact_rejects_non_plan_type():
    _, discovered, hashes = _build_artifact()
    with pytest.raises(ValidationError):
        AllStatsCommitArtifact(discovered=discovered, content_hashes=hashes, plan="not-a-plan")


def test_artifact_is_frozen():
    artifact, _, _ = _build_artifact()
    with pytest.raises(Exception):
        artifact.plan = None  # type: ignore[misc]


def test_artifact_defensively_copies_content_hashes():
    artifact, _, hashes = _build_artifact()
    hashes["Roster"] = "mutated-after-construction"
    assert artifact.content_hashes["Roster"] != "mutated-after-construction"


# ---------------------------------------------------------------------------
# _validate_manifest
# ---------------------------------------------------------------------------


def test_validate_manifest_accepts_a_matching_single_allstats_family_manifest():
    _, discovered, hashes = _build_artifact()
    manifest = _manifest_for((discovered, hashes))
    family_plan = _validate_manifest(manifest, MANIFEST_SCHEMA_VERSION)
    assert family_plan.family == "allstats"


def test_validate_manifest_rejects_schema_version_mismatch():
    _, discovered, hashes = _build_artifact()
    manifest = _manifest_for((discovered, hashes))
    with pytest.raises(ValidationError):
        _validate_manifest(manifest, "not-the-real-schema-version")


def test_validate_manifest_rejects_stale_contract_version():
    """Simulates a manifest built under an earlier AllStats adapter contract version by monkeypatching the SourceFamilyPlan's own construction-time check, then confirms the runner's own redundant, independent re-check catches it."""
    import src.pipeline.bulk_import_manifest as manifest_mod

    original = dict(manifest_mod.SOURCE_FAMILY_CONTRACT_VERSIONS)
    try:
        manifest_mod.SOURCE_FAMILY_CONTRACT_VERSIONS["allstats"] = "0-stale"
        family_plan = SourceFamilyPlan(family="allstats", contract_version="0-stale", artifacts=())
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
    _, discovered, hashes = _build_artifact()
    allstats_plan = SourceFamilyPlan(family="allstats", contract_version=ALLSTATS_IMPORT_CONTRACT_VERSION, artifacts=tuple(
        PlannedArtifact(source_root_key="teamdump_cbb", relative_path=meta.relative_path, content_hash=hashes[label], status="planned_insert")
        for label, meta in discovered.files.items()
    ))
    from src.pipeline.bulk_import_manifest import ESPN_IMPORT_CONTRACT_VERSION
    espn_plan = SourceFamilyPlan(family="espn", contract_version=ESPN_IMPORT_CONTRACT_VERSION, artifacts=())
    mixed_manifest = build_dry_run_manifest(generated_at=dt.datetime.now(dt.timezone.utc), families=[allstats_plan, espn_plan])
    with pytest.raises(ValidationError):
        _validate_manifest(mixed_manifest, MANIFEST_SCHEMA_VERSION)


def test_validate_manifest_accepts_allstats_only_manifest_with_zero_planned_artifacts():
    empty_family = SourceFamilyPlan(family="allstats", contract_version=ALLSTATS_IMPORT_CONTRACT_VERSION, artifacts=())
    manifest = build_dry_run_manifest(generated_at=dt.datetime.now(dt.timezone.utc), families=[empty_family])
    family_plan = _validate_manifest(manifest, MANIFEST_SCHEMA_VERSION)
    assert family_plan.artifacts == ()


# ---------------------------------------------------------------------------
# _validate_artifacts
# ---------------------------------------------------------------------------


def test_validate_artifacts_accepts_matching_artifact():
    artifact, discovered, hashes = _build_artifact()
    manifest = _manifest_for((discovered, hashes))
    family_plan = manifest.families[0]
    ordered = _validate_artifacts(family_plan, [artifact])
    assert ordered == (artifact,)


def test_validate_artifacts_rejects_duplicate_team_season():
    artifact, discovered, hashes = _build_artifact()
    manifest = _manifest_for((discovered, hashes))
    family_plan = manifest.families[0]
    with pytest.raises(ValidationError):
        _validate_artifacts(family_plan, [artifact, artifact])


def test_validate_artifacts_rejects_file_not_present_in_manifest():
    artifact, discovered, hashes = _build_artifact()
    empty_family_plan = SourceFamilyPlan(family="allstats", contract_version=ALLSTATS_IMPORT_CONTRACT_VERSION, artifacts=())
    with pytest.raises(ValidationError):
        _validate_artifacts(empty_family_plan, [artifact])


def test_validate_artifacts_rejects_stale_changed_content_hash():
    artifact, discovered, hashes = _build_artifact()
    manifest = _manifest_for((discovered, hashes))
    family_plan = manifest.families[0]
    # A changed artifact cannot masquerade as the approved dry-run artifact:
    # rebuild the SAME artifact with a DIFFERENT content_hash than what the manifest approved.
    changed_artifact, _, _ = _build_artifact(hash_suffix="-CHANGED")
    with pytest.raises(ValidationError):
        _validate_artifacts(family_plan, [changed_artifact])


def test_validate_artifacts_rejects_non_artifact_element():
    _, discovered, hashes = _build_artifact()
    manifest = _manifest_for((discovered, hashes))
    family_plan = manifest.families[0]
    with pytest.raises(ValidationError):
        _validate_artifacts(family_plan, ["not-an-artifact"])


def test_validate_artifacts_deterministic_order_independent_of_input_order():
    artifact_a, discovered_a, hashes_a = _build_artifact(team_slug="team-a", team_query="St. Bonaventure", player_name="Player A")
    artifact_b, discovered_b, hashes_b = _build_artifact(team_slug="team-b", team_query="Michigan State", player_name="Player B")
    manifest = _manifest_for((discovered_a, hashes_a), (discovered_b, hashes_b))
    family_plan = manifest.families[0]

    order_1 = _validate_artifacts(family_plan, [artifact_a, artifact_b])
    order_2 = _validate_artifacts(family_plan, [artifact_b, artifact_a])
    assert order_1 == order_2
    assert [a.team_slug for a in order_1] == ["team-a", "team-b"]


def test_validate_artifacts_rejects_a_bare_sequence_type_error():
    _, discovered, hashes = _build_artifact()
    manifest = _manifest_for((discovered, hashes))
    family_plan = manifest.families[0]
    with pytest.raises(ValidationError):
        _validate_artifacts(family_plan, "not-a-sequence-of-artifacts")


# ---------------------------------------------------------------------------
# run_allstats_committed_import: target-verification failure (no real Postgres needed)
# ---------------------------------------------------------------------------


def test_runner_refuses_a_non_postgres_connection_before_any_other_step():
    """Target verification happens first, before manifest validation, pipeline-run creation, or any write -- proven by using an intentionally-broken (empty) manifest/artifact list that would itself fail validation, and confirming the *connection* error surfaces instead."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    designation = DatabaseTargetDesignation(expected_database_name="napsedge_pg_test_abc", acknowledgement=build_expected_acknowledgement("napsedge_pg_test_abc"))
    with engine.connect() as conn:
        with pytest.raises(DatabaseTargetVerificationError):
            run_allstats_committed_import(
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
    import src.pipeline.bulk_import_allstats_runner as mod

    tree = ast.parse(inspect.getsource(mod))
    called_names = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not ({"create_engine", "sessionmaker", "Session", "build_engine", "build_session_factory", "connect"} & called_names)


def test_module_never_reads_environment_or_dotenv():
    import src.pipeline.bulk_import_allstats_runner as mod

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
    import src.pipeline.bulk_import_allstats_runner as mod

    tree = ast.parse(inspect.getsource(mod))
    called_names = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "hash" not in called_names


def test_module_never_calls_commit_rollback_flush_add_delete_on_a_session():
    """Every write in this module goes through unit_of_work or an existing repository -- this module's own code never calls session.commit()/.rollback()/.flush()/.add()/.delete()/.merge() itself. Scoped to calls whose receiver is named 'session' so plain-Python set/list methods (e.g. seen_identities.add(...)) are never false positives."""
    import src.pipeline.bulk_import_allstats_runner as mod

    tree = ast.parse(inspect.getsource(mod))
    session_calls = {
        n.func.attr for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "session"
    }
    assert not ({"commit", "rollback", "flush", "add", "delete", "merge"} & session_calls)


def test_module_never_imports_filesystem_or_discovery_machinery():
    """This module never discovers/scans TeamDump itself -- structurally confirmed by never importing pathlib or any of the four adapters' own discover_*/load_*/parse_* pure stages (only the persist_* stage and typed contracts)."""
    import src.pipeline.bulk_import_allstats_runner as mod

    tree = ast.parse(inspect.getsource(mod))
    imported_names: set[str] = set()
    imported_froms: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_froms.update(a.name for a in node.names)
    assert "pathlib" not in imported_names
    assert not ({"discover_allstats_artifacts", "load_allstats_dataframes", "parse_allstats_workbook"} & imported_froms)


def test_module_never_invokes_another_source_family_adapter():
    import src.pipeline.bulk_import_allstats_runner as mod

    source = inspect.getsource(mod)
    for forbidden in ("espn_import_adapter", "game_log_import_adapter", "player_game_log_import_adapter"):
        assert forbidden not in source


# ---------------------------------------------------------------------------
# No credential / absolute-path / source-content leakage in errors
# ---------------------------------------------------------------------------


def test_validation_errors_never_leak_an_absolute_path():
    """Every relative_path this module ever handles is source-relative (see bulk_import_manifest's own absolute-path rejection) -- a validation error naming a file must never surface anything resembling an absolute path."""
    artifact, discovered, hashes = _build_artifact()
    empty_family_plan = SourceFamilyPlan(family="allstats", contract_version=ALLSTATS_IMPORT_CONTRACT_VERSION, artifacts=())
    with pytest.raises(ValidationError) as excinfo:
        _validate_artifacts(empty_family_plan, [artifact])
    message = str(excinfo.value)
    assert "C:\\" not in message
    assert not message.startswith("/")


def test_failure_context_text_never_includes_password_or_url_shaped_content():
    from src.pipeline.bulk_import_allstats_runner import _artifact_file_identity_text

    artifact, _, _ = _build_artifact()
    text = _artifact_file_identity_text(artifact)
    assert "password" not in text.lower()
    assert "://" not in text
