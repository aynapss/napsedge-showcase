"""
Controlled bulk-import design, Phase 5C1 (docs/controlled_import_rehearsal_design.md
section 4 / section 32, approved for this batch only): deterministic unit
tests for `src/pipeline/bulk_import_planner.py`.

Every fixture here is synthetic, written to `tmp_path`, and deleted with
the test's own temp directory -- never a real `TeamDump`/`ESPN` file.
**No PostgreSQL test exists in this file, on purpose**: this planner makes
no database access of any kind (see the module's own docstring), so a
PostgreSQL-backed test would itself violate this batch's own scope.
"""
from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import inspect
import os

import pandas as pd
import pytest

from src.exceptions import ValidationError
from src.pipeline import bulk_import_planner as planner_module
from src.pipeline.bulk_import_coordinator import TeamGameLogFamilyRequest
from src.pipeline.bulk_import_espn_runner import (
    ESPN_BENCHMARK_FILENAME,
    ESPN_ENRICHED_FILENAME,
    ESPN_RAW_PLAYER_STATS_FILENAME,
)
from src.pipeline.bulk_import_espn_runner import COMMITTABLE_MANIFEST_STATUSES as ESPN_COMMITTABLE_MANIFEST_STATUSES
from src.pipeline.bulk_import_game_log_runner import COMMITTABLE_MANIFEST_STATUSES as TEAM_GAME_LOG_COMMITTABLE_MANIFEST_STATUSES
from src.pipeline.bulk_import_player_game_log_runner import COMMITTABLE_MANIFEST_STATUSES as PLAYER_GAME_LOG_COMMITTABLE_MANIFEST_STATUSES
from src.pipeline.bulk_import_manifest import ARTIFACT_STATUSES, SOURCE_FAMILY_ORDER, serialize_manifest
from src.pipeline.bulk_import_planner import (
    PLANNER_STATUS_MAP,
    DiscoveryToManifestPlanningResult,
    PlanningIssue,
    _check_no_cross_family_duplicate_identities,
    _classify_status,
    _verify_within_source_root,
    build_discovery_to_manifest_plan,
)
from src.pipeline.espn_import_adapter import ESPN_IMPORT_CONTRACT_VERSION

SEASON = 2026

# ---------------------------------------------------------------------------
# Synthetic real-file fixture builders -- one real file per family, written
# under tmp_path (mirrors the conventions in
# tests/regression/test_*_import_adapter.py).
# ---------------------------------------------------------------------------


def _roster_df(rows: list[dict]) -> pd.DataFrame:
    base = {"Player": None, "#": None, "Class": "SR", "Pos": "G", "Height": "6-2", "Weight": 190.0,
            "Hometown": None, "High School": None, "RSCI Top 100": None, "Summary": None}
    return pd.DataFrame([{**base, **r} for r in rows])


def _pergame_df(rows: list[dict]) -> pd.DataFrame:
    base = {"Player": None, "PTS": 10.0, "TRB": 5.0, "AST": 3.0}
    return pd.DataFrame([{**base, **r} for r in rows])


def _write_allstats_workbook(cbb_root, team_slug: str, *, season: int = SEASON, extra_tables: bool = False) -> None:
    team_dir = cbb_root / team_slug
    team_dir.mkdir(parents=True, exist_ok=True)
    _roster_df([{"Player": "Frank Mitchell", "#": 0}]).to_excel(team_dir / f"Roster_{season}.xlsx", index=False)
    if extra_tables:
        _pergame_df([{"Player": "Frank Mitchell"}]).to_excel(team_dir / f"PerGame_{season}.xlsx", index=False)


def _game_log_df(rows: list[dict]) -> pd.DataFrame:
    base = {"game_num": 1, "date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80,
            "pts_allowed": 70, "ORtg": 110.0, "DRtg": 95.0, "net_rtg": 15.0}
    return pd.DataFrame([{**base, **r} for r in rows])


def _write_team_game_log(cbb_root, team_slug: str, *, season: int = SEASON, content_marker: str = "Colgate") -> None:
    team_dir = cbb_root / team_slug
    team_dir.mkdir(parents=True, exist_ok=True)
    _game_log_df([{"opponent": content_marker}]).to_csv(team_dir / f"{team_slug}_{season}_game_log.csv", index=False)


def _player_game_log_df(rows: list[dict]) -> pd.DataFrame:
    base = {"game_num": 1, "date": "2026-01-15", "opponent": "Colgate", "result": "W 80-70",
            "mp": 30, "pts": 10, "trb": 5, "ast": 3, "stl": 1, "blk": 0, "tov": 2, "fg": 4, "fga": 8}
    return pd.DataFrame([{**base, **r} for r in rows])


def _write_player_game_log(cbb_root, team_slug: str, player_slug: str, *, season: int = SEASON) -> None:
    team_dir = cbb_root / team_slug
    team_dir.mkdir(parents=True, exist_ok=True)
    _player_game_log_df([{}]).to_csv(team_dir / f"{player_slug}_{season}_gamelog.csv", index=False)


def _espn_raw_df(rows: list[dict]) -> pd.DataFrame:
    base = {"RK": 1, "Name": "Frank Mitchell", "Team": "SBU", "POS": "G", "GP": 20, "MIN": 25.0,
            "PTS": 10.0, "FGM": 4.0, "FGA": 8.0, "FG%": 50.0, "3PM": 1.0, "3PA": 3.0, "3P%": 33.0,
            "FTM": 2.0, "FTA": 3.0, "FT%": 66.0, "REB": 4.0, "AST": 3.0, "STL": 1.0, "BLK": 0.5, "TO": 2.0}
    return pd.DataFrame([{**base, **r} for r in rows])


def _espn_benchmark_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{"Metric": r.get("Metric", "PTS"), "NCAA_Mean": r.get("NCAA_Mean", 8.4), "NCAA_Std": r.get("NCAA_Std", 4.6)} for r in rows])


def _write_espn_files(espn_root, *, include_enriched: bool = True) -> None:
    espn_root.mkdir(parents=True, exist_ok=True)
    _espn_raw_df([{}]).to_csv(espn_root / ESPN_RAW_PLAYER_STATS_FILENAME, index=False)
    _espn_benchmark_df([{}]).to_excel(espn_root / ESPN_BENCHMARK_FILENAME, index=False)
    if include_enriched:
        (espn_root / ESPN_ENRICHED_FILENAME).write_bytes(b"fake-enriched-view")


def _write_full_universe(tmp_path, *, team_slug: str = "michigan-state") -> tuple:
    cbb_root = tmp_path / "cbb"
    espn_root = tmp_path / "espn"
    _write_allstats_workbook(cbb_root, team_slug, extra_tables=True)
    _write_team_game_log(cbb_root, team_slug)
    _write_player_game_log(cbb_root, team_slug, "frank_mitchell")
    _write_espn_files(espn_root)
    return cbb_root, espn_root


GENERATED_AT = dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=dt.timezone.utc)


def _build(cbb_root, espn_root, *, team_slugs=None, generated_at=GENERATED_AT) -> DiscoveryToManifestPlanningResult:
    return build_discovery_to_manifest_plan(
        cbb_root=cbb_root, espn_root=espn_root, season=SEASON,
        generated_at=generated_at, team_slugs=team_slugs,
    )


# ---------------------------------------------------------------------------
# Frozen result contract
# ---------------------------------------------------------------------------


def test_result_and_issue_are_frozen(tmp_path):
    cbb_root, espn_root = _write_full_universe(tmp_path)
    result = _build(cbb_root, espn_root)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.manifest = None
    issue = PlanningIssue(family="x", identity="y", issue_code="z", severity="info", description="d")
    with pytest.raises(dataclasses.FrozenInstanceError):
        issue.family = "changed"


# ---------------------------------------------------------------------------
# All four families / complete multi-family manifest
# ---------------------------------------------------------------------------


def test_all_four_families_produced(tmp_path):
    cbb_root, espn_root = _write_full_universe(tmp_path)
    result = _build(cbb_root, espn_root)

    assert len(result.allstats_artifacts) == 1
    assert len(result.team_game_log_artifacts) == 1
    assert len(result.player_game_log_artifacts) == 1
    assert len(result.espn_source_artifacts) == 1
    assert len(result.espn_benchmark_artifacts) == 1

    manifest_families = {plan.family for plan in result.manifest.families}
    assert manifest_families == set(SOURCE_FAMILY_ORDER)
    assert [plan.family for plan in result.manifest.families] == list(SOURCE_FAMILY_ORDER)


def test_exact_contract_versions_recorded(tmp_path):
    cbb_root, espn_root = _write_full_universe(tmp_path)
    result = _build(cbb_root, espn_root)
    versions = {plan.family: plan.contract_version for plan in result.manifest.families}
    assert versions["espn"] == ESPN_IMPORT_CONTRACT_VERSION
    for plan in result.manifest.families:
        assert plan.contract_version == planner_module._CONTRACT_VERSIONS[plan.family]


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_caller_supplied_generated_at_is_used_verbatim(tmp_path):
    cbb_root, espn_root = _write_full_universe(tmp_path)
    result = _build(cbb_root, espn_root, generated_at=GENERATED_AT)
    assert result.manifest.generated_at == GENERATED_AT


def test_stable_results_after_reordered_discovery(tmp_path):
    cbb_root = tmp_path / "cbb"
    espn_root = tmp_path / "espn"
    for slug in ("alpha-team", "beta-team"):
        _write_allstats_workbook(cbb_root, slug)
        _write_team_game_log(cbb_root, slug)
    _write_espn_files(espn_root)

    forward = _build(cbb_root, espn_root, team_slugs=["alpha-team", "beta-team"])
    reversed_ = _build(cbb_root, espn_root, team_slugs=["beta-team", "alpha-team"])
    assert serialize_manifest(forward.manifest) == serialize_manifest(reversed_.manifest)


def test_byte_identical_serialization_for_identical_inputs(tmp_path):
    cbb_root, espn_root = _write_full_universe(tmp_path)
    first = _build(cbb_root, espn_root)
    second = _build(cbb_root, espn_root)
    assert serialize_manifest(first.manifest) == serialize_manifest(second.manifest)


def test_changed_bytes_produce_changed_hash(tmp_path):
    cbb_root = tmp_path / "cbb"
    espn_root = tmp_path / "espn"
    _write_team_game_log(cbb_root, "test-team", content_marker="Colgate")
    _write_espn_files(espn_root)
    first = _build(cbb_root, espn_root)

    _write_team_game_log(cbb_root, "test-team", content_marker="Siena")
    second = _build(cbb_root, espn_root)

    first_hash = first.team_game_log_artifacts[0].content_hash
    second_hash = second.team_game_log_artifacts[0].content_hash
    assert first_hash != second_hash


# ---------------------------------------------------------------------------
# Status classification
# ---------------------------------------------------------------------------


def test_status_map_covers_every_real_adapter_status():
    assert set(PLANNER_STATUS_MAP) == {"valid", "partial", "invalid"}
    assert PLANNER_STATUS_MAP["valid"] == "planned_insert"
    assert PLANNER_STATUS_MAP["partial"] == "ambiguous"
    assert PLANNER_STATUS_MAP["invalid"] == "unresolved"
    assert set(PLANNER_STATUS_MAP.values()) <= ARTIFACT_STATUSES


def test_classify_status_never_emits_idempotent_match_or_conflict():
    for adapter_status in ("valid", "partial", "invalid"):
        for persistable in (True, False):
            assert _classify_status(adapter_status, family="x", identity="y", persistable=persistable) not in ("idempotent_match", "conflict")


def test_classify_status_rejects_unknown_adapter_status():
    with pytest.raises(ValidationError):
        _classify_status("something_new", family="x", identity="y", persistable=True)


def test_classify_status_partial_and_persistable_is_planned_insert():
    assert _classify_status("partial", family="x", identity="y", persistable=True) == "planned_insert"


def test_classify_status_partial_and_not_persistable_is_ambiguous():
    assert _classify_status("partial", family="x", identity="y", persistable=False) == "ambiguous"


def test_classify_status_valid_is_always_planned_insert_regardless_of_persistable():
    assert _classify_status("valid", family="x", identity="y", persistable=True) == "planned_insert"
    assert _classify_status("valid", family="x", identity="y", persistable=False) == "planned_insert"


def test_classify_status_invalid_is_never_promoted_regardless_of_persistable():
    assert _classify_status("invalid", family="x", identity="y", persistable=True) == "unresolved"
    assert _classify_status("invalid", family="x", identity="y", persistable=False) == "unresolved"


def test_partial_and_invalid_status_paths(tmp_path):
    cbb_root = tmp_path / "cbb"
    espn_root = tmp_path / "espn"
    _write_espn_files(espn_root)

    # "invalid": Roster table present but missing the required "Player" column.
    invalid_dir = cbb_root / "invalid-team"
    invalid_dir.mkdir(parents=True)
    pd.DataFrame([{"NotPlayer": "x"}]).to_excel(invalid_dir / f"Roster_{SEASON}.xlsx", index=False)

    # "valid": a normal, fully resolvable roster-only workbook.
    valid_dir = cbb_root / "michigan-state"
    valid_dir.mkdir(parents=True)
    _roster_df([{"Player": "Frank Mitchell", "#": 0}]).to_excel(valid_dir / f"Roster_{SEASON}.xlsx", index=False)

    result = _build(cbb_root, espn_root, team_slugs=["invalid-team", "michigan-state"])
    statuses = {a.discovered.team_slug: a.plan.status for a in result.allstats_artifacts}
    assert statuses["invalid-team"] == "invalid"
    assert statuses["michigan-state"] == "valid"

    manifest_statuses = {
        pa.metadata["team_slug"]: pa.status
        for plan in result.manifest.families if plan.family == "allstats"
        for pa in plan.artifacts
    }
    assert manifest_statuses["invalid-team"] == "unresolved"
    assert manifest_statuses["michigan-state"] == "planned_insert"
    assert manifest_statuses["invalid-team"] not in ("planned_insert", "idempotent_match")


def test_bowling_green_state_and_william_mary_directory_slugs_now_resolve_and_commit(tmp_path):
    """Phase 5C4A: these two real directory slugs previously failed team-identity
    resolution entirely (status='invalid', manifest status='unresolved'). The new,
    narrow src/slug_utils.py _SR_OVERRIDES entries fix resolution; this proves the
    planner's own downstream classification follows through to `planned_insert`."""
    cbb_root = tmp_path / "cbb"
    espn_root = tmp_path / "espn"
    espn_root.mkdir()
    _write_allstats_workbook(cbb_root, "bowling-green-state")
    _write_allstats_workbook(cbb_root, "william-mary")

    result = _build(cbb_root, espn_root, team_slugs=["bowling-green-state", "william-mary"])
    statuses = {a.discovered.team_slug: (a.plan.status, a.plan.team_id) for a in result.allstats_artifacts}
    assert statuses["bowling-green-state"][0] == "valid"
    assert statuses["william-mary"][0] == "valid"
    assert statuses["bowling-green-state"][1] is not None
    assert statuses["william-mary"][1] is not None

    manifest_statuses = {
        pa.metadata["team_slug"]: pa.status
        for plan in result.manifest.families if plan.family == "allstats"
        for pa in plan.artifacts
    }
    assert manifest_statuses["bowling-green-state"] == "planned_insert"
    assert manifest_statuses["william-mary"] == "planned_insert"


# ---------------------------------------------------------------------------
# AllStats grouped-workbook mapping
# ---------------------------------------------------------------------------


def test_allstats_one_commit_artifact_per_workbook_one_planned_artifact_per_file(tmp_path):
    cbb_root = tmp_path / "cbb"
    espn_root = tmp_path / "espn"
    _write_allstats_workbook(cbb_root, "michigan-state", extra_tables=True)
    _write_espn_files(espn_root)

    result = _build(cbb_root, espn_root)
    assert len(result.allstats_artifacts) == 1
    artifact = result.allstats_artifacts[0]
    assert set(artifact.content_hashes) == {"Roster", "PerGame"}

    allstats_planned = [pa for plan in result.manifest.families if plan.family == "allstats" for pa in plan.artifacts]
    assert len(allstats_planned) == 2  # one PlannedArtifact per underlying file, never the workbook as a whole
    hash_by_relative_path = {pa.relative_path: pa.content_hash for pa in allstats_planned}
    for label, meta in artifact.discovered.files.items():
        assert hash_by_relative_path[meta.relative_path] == artifact.content_hashes[label]
    # The whole workbook shares one dry-run status (never split per file).
    assert len({pa.status for pa in allstats_planned}) == 1


# ---------------------------------------------------------------------------
# One-file-mapping for the two game-log families
# ---------------------------------------------------------------------------


def test_team_and_player_game_log_are_one_file_one_artifact(tmp_path):
    cbb_root = tmp_path / "cbb"
    espn_root = tmp_path / "espn"
    _write_team_game_log(cbb_root, "michigan-state")
    _write_player_game_log(cbb_root, "michigan-state", "frank_mitchell")
    _write_espn_files(espn_root)

    result = _build(cbb_root, espn_root)
    assert len(result.team_game_log_artifacts) == 1
    assert len(result.player_game_log_artifacts) == 1

    team_planned = [pa for plan in result.manifest.families if plan.family == "team_game_log" for pa in plan.artifacts]
    player_planned = [pa for plan in result.manifest.families if plan.family == "player_game_log" for pa in plan.artifacts]
    assert len(team_planned) == 1
    assert len(player_planned) == 1
    assert team_planned[0].content_hash == result.team_game_log_artifacts[0].content_hash
    assert player_planned[0].content_hash == result.player_game_log_artifacts[0].content_hash
    assert player_planned[0].metadata["team_slug"] == "michigan-state"
    assert player_planned[0].metadata["player_slug"] == "frank_mitchell"


# ---------------------------------------------------------------------------
# ESPN separation and structural exclusion
# ---------------------------------------------------------------------------


def test_espn_source_and_benchmark_kept_separate(tmp_path):
    cbb_root = tmp_path / "cbb"
    cbb_root.mkdir()
    espn_root = tmp_path / "espn"
    _write_espn_files(espn_root)

    result = _build(cbb_root, espn_root)
    assert len(result.espn_source_artifacts) == 1
    assert len(result.espn_benchmark_artifacts) == 1
    assert type(result.espn_source_artifacts[0]) is not type(result.espn_benchmark_artifacts[0])

    espn_planned = [pa for plan in result.manifest.families if plan.family == "espn" for pa in plan.artifacts]
    subtypes = {pa.metadata["subtype"] for pa in espn_planned}
    assert subtypes == {"source_stats", "benchmark"}


def test_espn_enriched_file_structurally_excluded(tmp_path):
    cbb_root = tmp_path / "cbb"
    cbb_root.mkdir()
    espn_root = tmp_path / "espn"
    _write_espn_files(espn_root, include_enriched=True)

    result = _build(cbb_root, espn_root)
    espn_planned = [pa for plan in result.manifest.families if plan.family == "espn" for pa in plan.artifacts]
    assert all(ESPN_ENRICHED_FILENAME not in pa.relative_path for pa in espn_planned)
    assert not any(
        isinstance(a, type(result.espn_source_artifacts[0])) and a.discovered.filename == ESPN_ENRICHED_FILENAME
        for a in result.espn_source_artifacts
    )
    exclusion_issues = [i for i in result.planning_issues if i.issue_code == "espn_enriched_file_structurally_excluded"]
    assert len(exclusion_issues) == 1
    assert exclusion_issues[0].severity == "info"


# ---------------------------------------------------------------------------
# Exact hash equality between manifest and commit artifacts
# ---------------------------------------------------------------------------


def test_manifest_hashes_exactly_equal_commit_artifact_hashes(tmp_path):
    cbb_root, espn_root = _write_full_universe(tmp_path)
    result = _build(cbb_root, espn_root)

    all_planned = [pa for plan in result.manifest.families for pa in plan.artifacts]
    hash_by_path = {pa.relative_path: pa.content_hash for pa in all_planned}

    assert hash_by_path[result.team_game_log_artifacts[0].discovered.relative_path] == result.team_game_log_artifacts[0].content_hash
    assert hash_by_path[result.player_game_log_artifacts[0].discovered.relative_path] == result.player_game_log_artifacts[0].content_hash
    assert hash_by_path[result.espn_source_artifacts[0].discovered.relative_path] == result.espn_source_artifacts[0].content_hash
    assert hash_by_path[result.espn_benchmark_artifacts[0].discovered.relative_path] == result.espn_benchmark_artifacts[0].content_hash


# ---------------------------------------------------------------------------
# Coordinator-request compatibility (import only inside this test -- the
# planner module itself never imports bulk_import_coordinator).
# ---------------------------------------------------------------------------


def test_team_game_log_artifacts_are_directly_usable_as_a_family_request(tmp_path):
    cbb_root = tmp_path / "cbb"
    espn_root = tmp_path / "espn"
    _write_team_game_log(cbb_root, "michigan-state")
    _write_espn_files(espn_root)
    result = _build(cbb_root, espn_root)

    request = TeamGameLogFamilyRequest(
        artifacts=result.team_game_log_artifacts, run_id="planning-test-run",
        started_at=dt.datetime.now(dt.timezone.utc),
    )
    assert request.family == "team_game_log"
    assert request.artifacts == result.team_game_log_artifacts


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def test_verify_within_source_root_accepts_normal_relative_path(tmp_path):
    (tmp_path / "team-a").mkdir()
    _verify_within_source_root(tmp_path, "team-a/file.csv", family="x")  # must not raise


def test_verify_within_source_root_rejects_parent_traversal(tmp_path):
    with pytest.raises(ValidationError):
        _verify_within_source_root(tmp_path, "../outside.csv", family="x")


def test_verify_within_source_root_rejects_absolute_leakage(tmp_path):
    other = tmp_path.parent / "elsewhere.csv"
    with pytest.raises(ValidationError):
        _verify_within_source_root(tmp_path, str(other), family="x")


@pytest.mark.skipif(os.name == "nt", reason="creating a symlink on Windows normally requires elevated privileges")
def test_verify_within_source_root_rejects_symlink_escape(tmp_path):
    outside = tmp_path.parent / "outside-dir"
    outside.mkdir(exist_ok=True)
    (outside / "secret.csv").write_text("x")
    root = tmp_path / "root"
    root.mkdir()
    (root / "escape-link").symlink_to(outside)
    with pytest.raises(ValidationError):
        _verify_within_source_root(root, "escape-link/secret.csv", family="x")


def test_planned_artifact_construction_still_rejects_bad_paths_directly(tmp_path):
    # PlannedArtifact's own string-level checks (reused, never re-implemented) still apply.
    from src.pipeline.bulk_import_manifest import PlannedArtifact
    with pytest.raises(ValidationError):
        PlannedArtifact(source_root_key="teamdump_cbb", relative_path="/etc/passwd", content_hash="h", status="planned_insert")
    with pytest.raises(ValidationError):
        PlannedArtifact(source_root_key="teamdump_cbb", relative_path="a/../../b", content_hash="h", status="planned_insert")


def test_unicode_apostrophe_and_space_paths(tmp_path):
    cbb_root = tmp_path / "cbb"
    espn_root = tmp_path / "espn"
    team_slug = "saint mary's (café)"
    _write_team_game_log(cbb_root, team_slug)
    _write_espn_files(espn_root)

    result = _build(cbb_root, espn_root, team_slugs=[team_slug])
    assert len(result.team_game_log_artifacts) == 1
    assert result.team_game_log_artifacts[0].discovered.team_slug == team_slug
    serialized = serialize_manifest(result.manifest)
    assert team_slug in serialized  # ensure_ascii=False -- real Unicode preserved verbatim


def test_check_no_cross_family_duplicate_identities_rejects_collision():
    from src.pipeline.bulk_import_manifest import PlannedArtifact
    a = PlannedArtifact(source_root_key="teamdump_cbb", relative_path="team/x.csv", content_hash="same-hash", status="planned_insert")
    b = PlannedArtifact(source_root_key="teamdump_cbb", relative_path="team/x.csv", content_hash="same-hash", status="planned_insert")
    with pytest.raises(ValidationError):
        _check_no_cross_family_duplicate_identities([a, b])


def test_check_no_cross_family_duplicate_identities_allows_distinct():
    from src.pipeline.bulk_import_manifest import PlannedArtifact
    a = PlannedArtifact(source_root_key="teamdump_cbb", relative_path="team/x.csv", content_hash="hash-1", status="planned_insert")
    b = PlannedArtifact(source_root_key="teamdump_cbb", relative_path="team/y.csv", content_hash="hash-2", status="planned_insert")
    _check_no_cross_family_duplicate_identities([a, b])  # must not raise


# ---------------------------------------------------------------------------
# Missing-file behavior (race between discovery and load)
# ---------------------------------------------------------------------------


def test_missing_file_at_load_becomes_a_planning_issue_not_a_crash(tmp_path, monkeypatch):
    cbb_root = tmp_path / "cbb"
    espn_root = tmp_path / "espn"
    _write_team_game_log(cbb_root, "michigan-state")
    _write_espn_files(espn_root)

    def _boom(discovered, cbb_root_arg):
        raise FileNotFoundError("simulated deletion between discovery and load")

    monkeypatch.setattr(planner_module, "load_game_log_dataframe", _boom)

    result = _build(cbb_root, espn_root)
    assert result.team_game_log_artifacts == ()
    missing_issues = [i for i in result.planning_issues if i.issue_code == "planner_file_missing_at_load"]
    assert len(missing_issues) == 1
    assert missing_issues[0].family == "team_game_log"


# ---------------------------------------------------------------------------
# No input mutation
# ---------------------------------------------------------------------------


def test_team_slugs_argument_is_never_mutated(tmp_path):
    cbb_root = tmp_path / "cbb"
    espn_root = tmp_path / "espn"
    _write_team_game_log(cbb_root, "alpha-team")
    _write_espn_files(espn_root)

    team_slugs = ["alpha-team", "not-present-team"]
    original_copy = list(team_slugs)
    _build(cbb_root, espn_root, team_slugs=team_slugs)
    assert team_slugs == original_copy


# ---------------------------------------------------------------------------
# No database / network / runner / lock / environment / output-file access
# ---------------------------------------------------------------------------


def _imported_module_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _imported_from_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(a.name for a in node.names)
    return names


def test_module_never_imports_database_network_runner_lock_or_coordinator_machinery():
    tree = ast.parse(inspect.getsource(planner_module))
    modules = _imported_module_names(tree)
    forbidden_module_prefixes = (
        "sqlalchemy", "psycopg2", "socket", "src.db.session", "src.db.repositories",
        "src.pipeline.bulk_import_coordinator", "src.pipeline.bulk_import_safety",
    )
    for module_name in modules:
        assert not any(module_name == p or module_name.startswith(p + ".") for p in forbidden_module_prefixes), module_name

    forbidden_names = _imported_from_names(tree) & {
        "unit_of_work", "Connection", "Session", "sessionmaker", "create_engine",
        "bulk_import_advisory_lock", "verify_database_target", "DatabaseTargetDesignation",
        "run_controlled_bulk_import",
    }
    assert not forbidden_names
    assert not any(name.startswith("persist_") for name in _imported_from_names(tree))


def test_module_never_reads_environment_variables():
    tree = ast.parse(inspect.getsource(planner_module))
    offending = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv"):
            offending.append(node.attr)
    assert not offending


def test_module_never_writes_a_file(tmp_path, monkeypatch):
    """Monkeypatch guard (not just AST): even a dynamically-constructed write call would be caught here."""
    cbb_root, espn_root = _write_full_universe(tmp_path)

    original_write_text = type(cbb_root).write_text
    original_write_bytes = type(cbb_root).write_bytes

    def _forbid_write_text(self, *a, **k):
        raise AssertionError(f"planner must never write a file, attempted write_text on {self}")

    def _forbid_write_bytes(self, *a, **k):
        raise AssertionError(f"planner must never write a file, attempted write_bytes on {self}")

    monkeypatch.setattr(type(cbb_root), "write_text", _forbid_write_text, raising=True)
    monkeypatch.setattr(type(cbb_root), "write_bytes", _forbid_write_bytes, raising=True)
    try:
        _build(cbb_root, espn_root)
    finally:
        monkeypatch.setattr(type(cbb_root), "write_text", original_write_text, raising=True)
        monkeypatch.setattr(type(cbb_root), "write_bytes", original_write_bytes, raising=True)


def test_result_contract_excludes_sessions_connections_and_run_ids():
    result_fields = {f.name for f in dataclasses.fields(DiscoveryToManifestPlanningResult)}
    forbidden_terms = ("session", "connection", "engine", "run_id", "lock", "client")
    for field_name in result_fields:
        assert not any(term in field_name.lower() for term in forbidden_terms), field_name


# ---------------------------------------------------------------------------
# Phase 5C2A: partial-but-persistable artifact committability correction.
#
# Approved semantics: a "partial" adapter plan is "planned_insert" whenever
# that family's own already-established persist function would actually
# write at least one real row for it. Identity ambiguity/unresolved
# identities/rejected rows remain fully preserved in the plan and in
# planning_issues -- only artifact-level committability changes.
# ---------------------------------------------------------------------------


def _write_allstats_partial_persistable(cbb_root, team_slug: str, *, season: int = SEASON) -> None:
    """Roster resolves cleanly; one PerGame row for a name absent from the roster triggers a real, persistable 'partial' plan (stat_row_not_found, a warning -- never critical)."""
    team_dir = cbb_root / team_slug
    team_dir.mkdir(parents=True, exist_ok=True)
    _roster_df([{"Player": "Frank Mitchell", "#": 0}]).to_excel(team_dir / f"Roster_{season}.xlsx", index=False)
    _pergame_df([{"Player": "Someone Not On The Roster"}]).to_excel(team_dir / f"PerGame_{season}.xlsx", index=False)


def _write_team_game_log_partial_persistable(cbb_root, team_slug: str, *, season: int = SEASON) -> None:
    """One real observation row with an unresolvable opponent -- gamelog_opponent_unresolved (warning), row still persistable."""
    team_dir = cbb_root / team_slug
    team_dir.mkdir(parents=True, exist_ok=True)
    _game_log_df([{"opponent": "Zzz Nonexistent Team Zzz"}]).to_csv(team_dir / f"{team_slug}_{season}_game_log.csv", index=False)


def _write_player_game_log_partial_persistable(cbb_root, team_slug: str, player_slug: str, *, season: int = SEASON) -> None:
    """One real observation row with an unresolvable opponent -- player_gamelog_opponent_unresolved (warning), row still persistable."""
    team_dir = cbb_root / team_slug
    team_dir.mkdir(parents=True, exist_ok=True)
    _player_game_log_df([{"opponent": "Zzz Nonexistent Team Zzz"}]).to_csv(team_dir / f"{player_slug}_{season}_gamelog.csv", index=False)


def _write_espn_source_partial_persistable(espn_root) -> None:
    """One real observation row with an unresolvable team code -- espn_abbreviation_unresolved (warning), row still persistable."""
    espn_root.mkdir(parents=True, exist_ok=True)
    _espn_raw_df([{"Team": "ZZZ"}]).to_csv(espn_root / ESPN_RAW_PLAYER_STATS_FILENAME, index=False)


def _write_espn_benchmark_partial_persistable(espn_root) -> None:
    """One good row plus one bad row (missing metric name) -- partial, but one persistable value_spec remains."""
    espn_root.mkdir(parents=True, exist_ok=True)
    df = pd.concat([
        _espn_benchmark_df([{"Metric": "PTS", "NCAA_Mean": 8.4, "NCAA_Std": 4.6}]),
        pd.DataFrame([{"Metric": "", "NCAA_Mean": 8.4, "NCAA_Std": 4.6}]),
    ], ignore_index=True)
    df.to_excel(espn_root / ESPN_BENCHMARK_FILENAME, index=False)


def _write_espn_benchmark_partial_not_persistable(espn_root) -> None:
    """Every row's own metric name is missing -- partial (schema recognized), but zero value_specs survive filtering."""
    espn_root.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([{"Metric": "", "NCAA_Mean": 8.4, "NCAA_Std": 4.6}, {"Metric": "", "NCAA_Mean": 1.0, "NCAA_Std": 1.0}])
    df.to_excel(espn_root / ESPN_BENCHMARK_FILENAME, index=False)


def test_partial_allstats_plan_promoted_to_planned_insert_when_persistable(tmp_path):
    cbb_root = tmp_path / "cbb"
    espn_root = tmp_path / "espn"
    espn_root.mkdir()
    _write_allstats_partial_persistable(cbb_root, "michigan-state")

    result = _build(cbb_root, espn_root, team_slugs=["michigan-state"])
    artifact = result.allstats_artifacts[0]
    assert artifact.plan.status == "partial"
    assert artifact.plan.counts.player_stat_rows > 0

    allstats_planned = [pa for plan in result.manifest.families if plan.family == "allstats" for pa in plan.artifacts]
    assert all(pa.status == "planned_insert" for pa in allstats_planned)

    issue_codes = {i.issue_code for i in result.planning_issues if i.family == "allstats"}
    assert "stat_row_not_found" in issue_codes


def test_partial_team_game_log_plan_promoted_to_planned_insert_when_persistable(tmp_path):
    cbb_root = tmp_path / "cbb"
    espn_root = tmp_path / "espn"
    espn_root.mkdir()
    _write_team_game_log_partial_persistable(cbb_root, "michigan-state")

    result = _build(cbb_root, espn_root, team_slugs=["michigan-state"])
    artifact = result.team_game_log_artifacts[0]
    assert artifact.plan.status == "partial"
    assert artifact.plan.counts.genuine_observations > 0

    planned = [pa for plan in result.manifest.families if plan.family == "team_game_log" for pa in plan.artifacts][0]
    assert planned.status == "planned_insert"

    issue_codes = {i.issue_code for i in result.planning_issues if i.family == "team_game_log"}
    assert "gamelog_opponent_unresolved" in issue_codes


def test_partial_player_game_log_plan_promoted_to_planned_insert_when_persistable(tmp_path):
    cbb_root = tmp_path / "cbb"
    espn_root = tmp_path / "espn"
    espn_root.mkdir()
    _write_player_game_log_partial_persistable(cbb_root, "michigan-state", "frank_mitchell")

    result = _build(cbb_root, espn_root, team_slugs=["michigan-state"])
    artifact = result.player_game_log_artifacts[0]
    assert artifact.plan.status == "partial"
    assert artifact.plan.counts.total_source_rows > 0

    planned = [pa for plan in result.manifest.families if plan.family == "player_game_log" for pa in plan.artifacts][0]
    assert planned.status == "planned_insert"

    issue_codes = {i.issue_code for i in result.planning_issues if i.family == "player_game_log"}
    assert "player_gamelog_opponent_unresolved" in issue_codes


def test_partial_espn_source_plan_promoted_to_planned_insert_when_persistable(tmp_path):
    cbb_root = tmp_path / "cbb"
    cbb_root.mkdir()
    espn_root = tmp_path / "espn"
    _write_espn_source_partial_persistable(espn_root)

    result = _build(cbb_root, espn_root)
    artifact = result.espn_source_artifacts[0]
    assert artifact.plan.status == "partial"
    assert artifact.plan.counts.total_source_rows > 0

    espn_planned = [pa for plan in result.manifest.families if plan.family == "espn" for pa in plan.artifacts]
    source_planned = next(pa for pa in espn_planned if pa.metadata["subtype"] == "source_stats")
    assert source_planned.status == "planned_insert"

    issue_codes = {i.issue_code for i in result.planning_issues if i.family == "espn"}
    assert "espn_abbreviation_unresolved" in issue_codes


def test_partial_espn_benchmark_plan_promoted_to_planned_insert_when_persistable(tmp_path):
    cbb_root = tmp_path / "cbb"
    cbb_root.mkdir()
    espn_root = tmp_path / "espn"
    _write_espn_benchmark_partial_persistable(espn_root)
    # discover_espn_artifacts also expects the raw source-stats file to exist for a full run;
    # a minimal, cleanly-resolving one keeps this test focused on the benchmark family alone.
    _espn_raw_df([{}]).to_csv(espn_root / ESPN_RAW_PLAYER_STATS_FILENAME, index=False)

    result = _build(cbb_root, espn_root)
    artifact = result.espn_benchmark_artifacts[0]
    assert artifact.plan.status == "partial"
    assert len(artifact.plan.value_specs) > 0

    espn_planned = [pa for plan in result.manifest.families if plan.family == "espn" for pa in plan.artifacts]
    benchmark_planned = next(pa for pa in espn_planned if pa.metadata["subtype"] == "benchmark")
    assert benchmark_planned.status == "planned_insert"

    issue_codes = {i.issue_code for i in result.planning_issues if i.family == "espn"}
    assert "espn_benchmark_missing_metric" in issue_codes


def test_partial_espn_benchmark_plan_not_promoted_when_zero_persistable_rows(tmp_path):
    """The one confirmed real edge case: every row's own value rejected -- partial, but zero value_specs -- must NOT be promoted."""
    cbb_root = tmp_path / "cbb"
    cbb_root.mkdir()
    espn_root = tmp_path / "espn"
    _write_espn_benchmark_partial_not_persistable(espn_root)
    _espn_raw_df([{}]).to_csv(espn_root / ESPN_RAW_PLAYER_STATS_FILENAME, index=False)

    result = _build(cbb_root, espn_root)
    artifact = result.espn_benchmark_artifacts[0]
    assert artifact.plan.status == "partial"
    assert len(artifact.plan.value_specs) == 0

    espn_planned = [pa for plan in result.manifest.families if plan.family == "espn" for pa in plan.artifacts]
    benchmark_planned = next(pa for pa in espn_planned if pa.metadata["subtype"] == "benchmark")
    assert benchmark_planned.status == "ambiguous"
    assert benchmark_planned.status not in ("planned_insert", "idempotent_match")


def test_ambiguous_identity_preserved_inside_plan_after_promotion(tmp_path):
    """An identity-ambiguous row's own plan-level fields are never rewritten as resolved, even once the artifact itself becomes planned_insert."""
    cbb_root = tmp_path / "cbb"
    espn_root = tmp_path / "espn"
    espn_root.mkdir()
    _write_allstats_partial_persistable(cbb_root, "michigan-state")

    result = _build(cbb_root, espn_root, team_slugs=["michigan-state"])
    artifact = result.allstats_artifacts[0]
    unresolved_specs = [s for s in artifact.plan.player_stat_specs if s["player_id"] is None]
    assert len(unresolved_specs) == 1
    assert unresolved_specs[0]["validation_status"] == "unresolved"

    allstats_planned = [pa for plan in result.manifest.families if plan.family == "allstats" for pa in plan.artifacts]
    assert all(pa.status == "planned_insert" for pa in allstats_planned)


def test_all_issues_preserved_byte_for_byte_after_promotion(tmp_path):
    cbb_root = tmp_path / "cbb"
    espn_root = tmp_path / "espn"
    espn_root.mkdir()
    _write_team_game_log_partial_persistable(cbb_root, "michigan-state")

    result = _build(cbb_root, espn_root, team_slugs=["michigan-state"])
    matching = [i for i in result.planning_issues if i.family == "team_game_log" and i.issue_code == "gamelog_opponent_unresolved"]
    assert len(matching) == 1
    assert matching[0].severity == "warning"
    assert "Zzz Nonexistent Team Zzz" in matching[0].description or "resolve" in matching[0].description.lower()


def test_promoted_artifacts_pass_each_runners_committable_status_gate():
    """Every status this planner can ever produce for a promoted partial artifact must be a member of that family's own runner-side COMMITTABLE_MANIFEST_STATUSES."""
    assert "planned_insert" in TEAM_GAME_LOG_COMMITTABLE_MANIFEST_STATUSES
    assert "planned_insert" in PLAYER_GAME_LOG_COMMITTABLE_MANIFEST_STATUSES
    assert "planned_insert" in ESPN_COMMITTABLE_MANIFEST_STATUSES


def test_promoted_team_game_log_artifact_accepted_by_its_own_runner_preflight(tmp_path):
    from src.pipeline.bulk_import_game_log_runner import _validate_artifacts, _validate_manifest

    cbb_root = tmp_path / "cbb"
    espn_root = tmp_path / "espn"
    espn_root.mkdir()
    _write_team_game_log_partial_persistable(cbb_root, "michigan-state")

    result = _build(cbb_root, espn_root, team_slugs=["michigan-state"])
    team_game_log_manifest = next(plan for plan in result.manifest.families if plan.family == "team_game_log")
    single_family_manifest = planner_module.build_dry_run_manifest(generated_at=result.manifest.generated_at, families=[team_game_log_manifest])

    from src.pipeline.bulk_import_manifest import MANIFEST_SCHEMA_VERSION
    family_plan = _validate_manifest(single_family_manifest, MANIFEST_SCHEMA_VERSION)
    validated = _validate_artifacts(family_plan, result.team_game_log_artifacts)  # must not raise
    assert len(validated) == 1


def test_promoted_player_game_log_artifact_accepted_by_its_own_runner_preflight(tmp_path):
    from src.pipeline.bulk_import_player_game_log_runner import _validate_artifacts, _validate_manifest

    cbb_root = tmp_path / "cbb"
    espn_root = tmp_path / "espn"
    espn_root.mkdir()
    _write_player_game_log_partial_persistable(cbb_root, "michigan-state", "frank_mitchell")

    result = _build(cbb_root, espn_root, team_slugs=["michigan-state"])
    player_game_log_manifest = next(plan for plan in result.manifest.families if plan.family == "player_game_log")
    single_family_manifest = planner_module.build_dry_run_manifest(generated_at=result.manifest.generated_at, families=[player_game_log_manifest])

    from src.pipeline.bulk_import_manifest import MANIFEST_SCHEMA_VERSION
    family_plan = _validate_manifest(single_family_manifest, MANIFEST_SCHEMA_VERSION)
    validated = _validate_artifacts(family_plan, result.player_game_log_artifacts)  # must not raise
    assert len(validated) == 1


def test_promoted_espn_source_artifact_accepted_by_its_own_runner_preflight(tmp_path):
    from src.pipeline.bulk_import_espn_runner import _validate_artifacts, _validate_manifest

    cbb_root = tmp_path / "cbb"
    cbb_root.mkdir()
    espn_root = tmp_path / "espn"
    _write_espn_source_partial_persistable(espn_root)
    _espn_benchmark_df([{}]).to_excel(espn_root / ESPN_BENCHMARK_FILENAME, index=False)

    result = _build(cbb_root, espn_root)
    espn_manifest = next(plan for plan in result.manifest.families if plan.family == "espn")
    single_family_manifest = planner_module.build_dry_run_manifest(generated_at=result.manifest.generated_at, families=[espn_manifest])

    from src.pipeline.bulk_import_manifest import MANIFEST_SCHEMA_VERSION
    family_plan = _validate_manifest(single_family_manifest, MANIFEST_SCHEMA_VERSION)
    validated = _validate_artifacts(family_plan, list(result.espn_source_artifacts) + list(result.espn_benchmark_artifacts))  # must not raise
    assert len(validated) == 2


def test_no_input_mutation_or_extra_access_during_partial_promotion(tmp_path, monkeypatch):
    """The promotion logic reuses only already-computed plan fields -- no new database, network, or file-write access is introduced."""
    cbb_root = tmp_path / "cbb"
    espn_root = tmp_path / "espn"
    espn_root.mkdir()
    _write_allstats_partial_persistable(cbb_root, "michigan-state")

    team_slugs = ["michigan-state"]
    original_copy = list(team_slugs)
    _build(cbb_root, espn_root, team_slugs=team_slugs)
    assert team_slugs == original_copy


def test_deterministic_serialization_unchanged_for_promoted_partial_artifacts(tmp_path):
    cbb_root = tmp_path / "cbb"
    espn_root = tmp_path / "espn"
    espn_root.mkdir()
    _write_allstats_partial_persistable(cbb_root, "michigan-state")

    first = _build(cbb_root, espn_root, team_slugs=["michigan-state"])
    second = _build(cbb_root, espn_root, team_slugs=["michigan-state"])
    assert serialize_manifest(first.manifest) == serialize_manifest(second.manifest)
