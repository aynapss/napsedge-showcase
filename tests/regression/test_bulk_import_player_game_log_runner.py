"""
Controlled bulk-import design, Slice 7 (docs/controlled_bulk_import_design.md
section 16 item 7, approved for this slice only): deterministic unit
tests for `src/pipeline/bulk_import_player_game_log_runner.py` that need
no real database connection at all. Real-PostgreSQL behavior (target
verification success, advisory locking, actual persisted writes,
rollback, games/game_results reconciliation, lock release) lives in
`tests/postgres/test_postgres_bulk_import_player_game_log_runner.py`.

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
from src.identity.player_identity import build_player_identity
from src.identity.season_identity import resolve_season_identity
from src.identity.team_identity import resolve_team_identity
from src.pipeline.bulk_import_manifest import (
    MANIFEST_SCHEMA_VERSION,
    PlannedArtifact,
    SourceFamilyPlan,
    build_dry_run_manifest,
)
from src.pipeline.bulk_import_player_game_log_runner import (
    COMMITTABLE_MANIFEST_STATUSES,
    EXPECTED_SOURCE_ROOT_KEY,
    FAILED_ARTIFACT_ISSUE_CODE,
    RECONCILIATION_MISMATCH_ISSUE_CODE,
    PlayerGameLogArtifactOutcome,
    PlayerGameLogCommitArtifact,
    PlayerGameLogCommittedImportRunResult,
    _artifact_identity_text,
    _validate_artifacts,
    _validate_manifest,
    _verify_reconciliation,
    run_player_game_log_committed_import,
)
from src.pipeline.bulk_import_safety import DatabaseTargetDesignation, build_expected_acknowledgement
from src.pipeline.player_game_log_import_adapter import (
    PLAYER_GAMELOG_IMPORT_CONTRACT_VERSION,
    DiscoveredPlayerGameLogFile,
    build_player_game_log_import_plan,
    parse_player_game_log_workbook,
)

SEASON = 2026

# ---------------------------------------------------------------------------
# Synthetic fixture builders (mirrors tests/regression/test_player_game_log_import_adapter.py's own conventions)
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


def _build_artifact(*, team_slug: str = "michigan-state", team_query: str = "Michigan State", player_slug: str = "a_player", player_name: str = "A Player", season: int = SEASON, hash_suffix: str = "", opponent: str = "Colgate") -> tuple[PlayerGameLogCommitArtifact, DiscoveredPlayerGameLogFile, str]:
    team_identity = resolve_team_identity(team_query, source="sports_reference")
    df = _gamelog_df([{"opponent": opponent}])
    parsed = parse_player_game_log_workbook(
        df, team_identity=team_identity, season_identity=resolve_season_identity(season),
        roster_identities=_roster([player_name], team_identity.team_id),
        team_slug=team_slug, player_slug=player_slug, season=season,
    )
    plan = build_player_game_log_import_plan(parsed)
    discovered, content_hash = _discovered_and_hash(team_slug, player_slug, season=season, hash_suffix=hash_suffix)
    return PlayerGameLogCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan), discovered, content_hash


def _manifest_for(*discovered_hash_pairs, contract_version: str | None = None, family: str = "player_game_log", source_root_key: str = EXPECTED_SOURCE_ROOT_KEY, status: str = "planned_insert") -> object:
    planned = [
        PlannedArtifact(source_root_key=source_root_key, relative_path=discovered.relative_path, content_hash=content_hash, status=status)
        for discovered, content_hash in discovered_hash_pairs
    ]
    family_plan = SourceFamilyPlan(family=family, contract_version=contract_version or PLAYER_GAMELOG_IMPORT_CONTRACT_VERSION, artifacts=tuple(planned))
    return build_dry_run_manifest(generated_at=dt.datetime.now(dt.timezone.utc), families=[family_plan])


# ---------------------------------------------------------------------------
# PlayerGameLogCommitArtifact construction/validation
# ---------------------------------------------------------------------------


def test_artifact_derives_identity_from_discovered_and_cross_checks_plan():
    """Unlike Slices 5/6, plan.team_slug/player_slug ARE safe to compare against discovered's own fields here -- parse_player_game_log_workbook stores them verbatim, confirmed by direct inspection."""
    artifact, discovered, _ = _build_artifact(team_slug="michigan-state", team_query="Michigan State", player_slug="a_player")
    assert artifact.team_slug == "michigan-state"
    assert artifact.player_slug == "a_player"
    assert artifact.season == SEASON
    assert artifact.plan.team_slug == "michigan-state"
    assert artifact.plan.player_slug == "a_player"
    assert artifact.identity == ("michigan-state", "a_player", SEASON)


def test_artifact_rejects_plan_season_mismatch():
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{"opponent": "Colgate"}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    plan = build_player_game_log_import_plan(parsed)
    discovered, content_hash = _discovered_and_hash("michigan-state", "a_player", season=SEASON + 1)
    with pytest.raises(ValidationError):
        PlayerGameLogCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan)


def test_artifact_rejects_plan_team_slug_mismatch():
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{"opponent": "Colgate"}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    plan = build_player_game_log_import_plan(parsed)
    discovered, content_hash = _discovered_and_hash("some-other-team", "a_player")
    with pytest.raises(ValidationError):
        PlayerGameLogCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan)


def test_artifact_rejects_plan_player_slug_mismatch():
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{"opponent": "Colgate"}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    plan = build_player_game_log_import_plan(parsed)
    discovered, content_hash = _discovered_and_hash("michigan-state", "different_player")
    with pytest.raises(ValidationError):
        PlayerGameLogCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan)


def test_artifact_rejects_non_discovered_type():
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{"opponent": "Colgate"}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    plan = build_player_game_log_import_plan(parsed)
    with pytest.raises(ValidationError):
        PlayerGameLogCommitArtifact(discovered="not-a-discovered-file", content_hash="x", plan=plan)


def test_artifact_rejects_empty_content_hash():
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{"opponent": "Colgate"}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    plan = build_player_game_log_import_plan(parsed)
    discovered, _ = _discovered_and_hash("michigan-state", "a_player")
    with pytest.raises(ValidationError):
        PlayerGameLogCommitArtifact(discovered=discovered, content_hash="", plan=plan)


def test_artifact_rejects_non_plan_type():
    discovered, content_hash = _discovered_and_hash("michigan-state", "a_player")
    with pytest.raises(ValidationError):
        PlayerGameLogCommitArtifact(discovered=discovered, content_hash=content_hash, plan="not-a-plan")


def test_artifact_is_frozen():
    artifact, _, _ = _build_artifact()
    with pytest.raises(Exception):
        artifact.plan = None  # type: ignore[misc]


def test_artifact_touched_game_ids_matches_resolved_rows():
    artifact, _, _ = _build_artifact()
    assert len(artifact.touched_game_ids) == 1
    assert all(isinstance(g, int) for g in artifact.touched_game_ids)


def test_artifact_does_not_mutate_plan_or_discovered_across_construction():
    """No input mutation: constructing the artifact must never alter the caller's own plan/discovered objects."""
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{"opponent": "Colgate"}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    plan = build_player_game_log_import_plan(parsed)
    discovered, content_hash = _discovered_and_hash("michigan-state", "a_player")
    plan_specs_before = list(plan.observation_specs)
    PlayerGameLogCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan)
    assert plan.observation_specs == plan_specs_before
    assert discovered.relative_path == "michigan-state/a_player_2026_gamelog.csv"


# ---------------------------------------------------------------------------
# _validate_manifest
# ---------------------------------------------------------------------------


def test_validate_manifest_accepts_a_matching_single_family_manifest():
    _, discovered, content_hash = _build_artifact()
    manifest = _manifest_for((discovered, content_hash))
    family_plan = _validate_manifest(manifest, MANIFEST_SCHEMA_VERSION)
    assert family_plan.family == "player_game_log"


def test_validate_manifest_rejects_schema_version_mismatch():
    _, discovered, content_hash = _build_artifact()
    manifest = _manifest_for((discovered, content_hash))
    with pytest.raises(ValidationError):
        _validate_manifest(manifest, "not-the-real-schema-version")


def test_validate_manifest_rejects_stale_contract_version():
    import src.pipeline.bulk_import_manifest as manifest_mod

    original = dict(manifest_mod.SOURCE_FAMILY_CONTRACT_VERSIONS)
    try:
        manifest_mod.SOURCE_FAMILY_CONTRACT_VERSIONS["player_game_log"] = "0-stale"
        family_plan = SourceFamilyPlan(family="player_game_log", contract_version="0-stale", artifacts=())
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
    player_plan = SourceFamilyPlan(family="player_game_log", contract_version=PLAYER_GAMELOG_IMPORT_CONTRACT_VERSION, artifacts=(
        PlannedArtifact(source_root_key=EXPECTED_SOURCE_ROOT_KEY, relative_path=discovered.relative_path, content_hash=content_hash, status="planned_insert"),
    ))
    from src.pipeline.allstats_import_adapter import ALLSTATS_IMPORT_CONTRACT_VERSION
    allstats_plan = SourceFamilyPlan(family="allstats", contract_version=ALLSTATS_IMPORT_CONTRACT_VERSION, artifacts=())
    mixed_manifest = build_dry_run_manifest(generated_at=dt.datetime.now(dt.timezone.utc), families=[player_plan, allstats_plan])
    with pytest.raises(ValidationError):
        _validate_manifest(mixed_manifest, MANIFEST_SCHEMA_VERSION)


def test_validate_manifest_accepts_family_only_manifest_with_zero_planned_artifacts():
    empty_family = SourceFamilyPlan(family="player_game_log", contract_version=PLAYER_GAMELOG_IMPORT_CONTRACT_VERSION, artifacts=())
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


def test_validate_artifacts_rejects_duplicate_identity():
    artifact, discovered, content_hash = _build_artifact()
    manifest = _manifest_for((discovered, content_hash))
    with pytest.raises(ValidationError):
        _validate_artifacts(manifest.families[0], [artifact, artifact])


def test_validate_artifacts_rejects_unplanned_artifact():
    artifact, discovered, content_hash = _build_artifact()
    empty_family_plan = SourceFamilyPlan(family="player_game_log", contract_version=PLAYER_GAMELOG_IMPORT_CONTRACT_VERSION, artifacts=())
    with pytest.raises(ValidationError):
        _validate_artifacts(empty_family_plan, [artifact])


def test_validate_artifacts_rejects_missing_planned_artifact():
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
    artifact_a, discovered_a, hash_a = _build_artifact(team_slug="michigan-state", team_query="Michigan State", player_slug="a_player", player_name="A Player")
    artifact_b, discovered_b, hash_b = _build_artifact(team_slug="michigan-state", team_query="Michigan State", player_slug="b_player", player_name="B Player")
    manifest = _manifest_for((discovered_a, hash_a), (discovered_b, hash_b))
    family_plan = manifest.families[0]

    order_1 = _validate_artifacts(family_plan, [artifact_a, artifact_b])
    order_2 = _validate_artifacts(family_plan, [artifact_b, artifact_a])
    assert order_1 == order_2
    assert [a.player_slug for a in order_1] == ["a_player", "b_player"]


def test_validate_artifacts_rejects_a_bare_sequence_type_error():
    _, discovered, content_hash = _build_artifact()
    manifest = _manifest_for((discovered, content_hash))
    with pytest.raises(ValidationError):
        _validate_artifacts(manifest.families[0], "not-a-sequence-of-artifacts")


# ---------------------------------------------------------------------------
# _verify_reconciliation (pure -- no database needed)
# ---------------------------------------------------------------------------


def test_reconciliation_accepts_game_row_present_and_no_result_change():
    game_ids = frozenset({7})
    assert _verify_reconciliation(game_ids, frozenset(), frozenset({7}), {}, {}) is None


def test_reconciliation_accepts_already_existing_game_row_and_stable_result():
    game_ids = frozenset({7})
    results = {7: ("single_sided_provisional", 1)}
    assert _verify_reconciliation(game_ids, frozenset({7}), frozenset({7}), results, results) is None


def test_reconciliation_rejects_missing_game_row_after_commit():
    game_ids = frozenset({7})
    assert _verify_reconciliation(game_ids, frozenset(), frozenset(), {}, {}) is not None


def test_reconciliation_rejects_game_result_appearing_unexpectedly():
    game_ids = frozenset({7})
    after_results = {7: ("single_sided_provisional", 1)}
    assert _verify_reconciliation(game_ids, frozenset({7}), frozenset({7}), {}, after_results) is not None


def test_reconciliation_rejects_game_result_changing_unexpectedly():
    game_ids = frozenset({7})
    before_results = {7: ("single_sided_provisional", 1)}
    after_results = {7: ("reconciled", 2)}
    assert _verify_reconciliation(game_ids, frozenset({7}), frozenset({7}), before_results, after_results) is not None


def test_reconciliation_empty_game_ids_always_passes():
    assert _verify_reconciliation(frozenset(), frozenset(), frozenset(), {}, {}) is None


# ---------------------------------------------------------------------------
# run_player_game_log_committed_import: target-verification failure (no real Postgres needed)
# ---------------------------------------------------------------------------


def test_runner_refuses_a_non_postgres_connection_before_any_other_step():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    designation = DatabaseTargetDesignation(expected_database_name="napsedge_pg_test_abc", acknowledgement=build_expected_acknowledgement("napsedge_pg_test_abc"))
    with engine.connect() as conn:
        with pytest.raises(DatabaseTargetVerificationError):
            run_player_game_log_committed_import(
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
    import src.pipeline.bulk_import_player_game_log_runner as mod

    tree = ast.parse(inspect.getsource(mod))
    called_names = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not ({"create_engine", "sessionmaker", "Session", "build_engine", "build_session_factory", "connect"} & called_names)


def test_module_never_reads_environment_or_dotenv():
    import src.pipeline.bulk_import_player_game_log_runner as mod

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
    import src.pipeline.bulk_import_player_game_log_runner as mod

    tree = ast.parse(inspect.getsource(mod))
    called_names = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "hash" not in called_names


def test_module_never_calls_commit_rollback_flush_add_delete_on_a_session():
    """Every write in this module goes through unit_of_work or an existing repository -- this module's own code never calls session.commit()/.rollback()/.flush()/.add()/.delete()/.merge() itself. Scoped to calls whose receiver is named 'session' so plain-Python set/list methods are never false positives."""
    import src.pipeline.bulk_import_player_game_log_runner as mod

    tree = ast.parse(inspect.getsource(mod))
    session_calls = {
        n.func.attr for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "session"
    }
    assert not ({"commit", "rollback", "flush", "add", "delete", "merge"} & session_calls)


def test_module_never_imports_filesystem_or_discovery_machinery():
    """This module never discovers/scans TeamDump itself (including the per-team Roster_<season>.xlsx read) -- structurally confirmed by never importing pathlib or the adapter's own discover_*/load_*/parse_*/resolve_team_roster_for_player_gamelogs stages (only persist_* and typed contracts)."""
    import src.pipeline.bulk_import_player_game_log_runner as mod

    tree = ast.parse(inspect.getsource(mod))
    imported_names: set[str] = set()
    imported_froms: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_froms.update(a.name for a in node.names)
    assert "pathlib" not in imported_names
    assert not ({"discover_player_game_log_artifacts", "load_player_game_log_dataframe", "parse_player_game_log_workbook", "resolve_team_roster_for_player_gamelogs"} & imported_froms)


def test_module_never_invokes_another_source_family_adapter():
    """Import-based (not raw substring) check: the module's own docstring legitimately mentions 'player_game_log_import_adapter.py' in prose, which itself contains the substring 'game_log_import_adapter' -- a naive substring check would false-positive on that."""
    import src.pipeline.bulk_import_player_game_log_runner as mod

    tree = ast.parse(inspect.getsource(mod))
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    assert not any(m.endswith(("allstats_import_adapter", "espn_import_adapter")) or m.endswith(".game_log_import_adapter") for m in imported_modules)


def test_module_does_not_import_the_other_runners():
    """No shared orchestration framework this slice -- every single-family runner remains an independent module."""
    import src.pipeline.bulk_import_player_game_log_runner as mod

    source = inspect.getsource(mod)
    assert "bulk_import_allstats_runner" not in source
    assert "bulk_import_game_log_runner" not in source


# ---------------------------------------------------------------------------
# No credential / absolute-path / source-content leakage in errors
# ---------------------------------------------------------------------------


def test_validation_errors_never_leak_an_absolute_path():
    artifact, discovered, content_hash = _build_artifact()
    empty_family_plan = SourceFamilyPlan(family="player_game_log", contract_version=PLAYER_GAMELOG_IMPORT_CONTRACT_VERSION, artifacts=())
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
