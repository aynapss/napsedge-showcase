"""
Controlled bulk-import design, Slice 9 (docs/controlled_bulk_import_design.md
section 16 item 9, approved for this slice only): deterministic unit
tests for `src/pipeline/bulk_import_coordinator.py` that need no real
database connection at all. Real-PostgreSQL behavior (target verification
success, one continuous outer advisory lock spanning all families, actual
delegated writes, rollback, lock release) lives in
`tests/postgres/test_postgres_bulk_import_coordinator.py`.

Every fixture here is synthetic and in-memory -- never a real TeamDump or
ESPN source file.
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
    CoordinatedFamilyOutcome,
    CoordinatedImportRunResult,
    EspnFamilyRequest,
    PlayerGameLogFamilyRequest,
    TeamGameLogFamilyRequest,
    _extract_family_manifest,
    _validate_family_requests,
    run_controlled_bulk_import,
)
from src.pipeline.bulk_import_espn_runner import EXPECTED_SOURCE_ROOT_KEY as ESPN_SOURCE_ROOT_KEY
from src.pipeline.bulk_import_espn_runner import EspnSourceStatsCommitArtifact
from src.pipeline.bulk_import_game_log_runner import EXPECTED_SOURCE_ROOT_KEY as GAME_LOG_SOURCE_ROOT_KEY
from src.pipeline.bulk_import_game_log_runner import TeamGameLogCommitArtifact
from src.pipeline.bulk_import_manifest import (
    MANIFEST_SCHEMA_VERSION,
    SOURCE_FAMILY_ORDER,
    PlannedArtifact,
    SourceFamilyPlan,
    build_dry_run_manifest,
)
from src.pipeline.bulk_import_player_game_log_runner import EXPECTED_SOURCE_ROOT_KEY as PLAYER_GAME_LOG_SOURCE_ROOT_KEY
from src.pipeline.bulk_import_player_game_log_runner import PlayerGameLogCommitArtifact
from src.pipeline.bulk_import_safety import DatabaseTargetDesignation, build_expected_acknowledgement
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

SEASON = 2026
ALLSTATS_SOURCE_ROOT_KEY = "teamdump_cbb"


# ---------------------------------------------------------------------------
# One minimal, synthetic artifact per family + its own approved manifest
# entry -- mirrors each runner's own established test conventions exactly.
# ---------------------------------------------------------------------------


def _allstats_family():
    dfs = {"Roster": pd.DataFrame([{"Player": "A Player", "#": 0, "Class": "SR", "Pos": "G", "Height": "6-2", "Weight": 190.0,
                                     "Hometown": None, "High School": None, "RSCI Top 100": None, "Summary": None}])}
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    plan = build_allstats_import_plan(parsed)
    files = {"Roster": AllStatsFileMetadata(relative_path="st-bonaventure/Roster_2026.xlsx", file_size_bytes=100, filesystem_mtime=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))}
    discovered = DiscoveredAllStatsWorkbook(team_slug="st-bonaventure", season=SEASON, files=files)
    content_hashes = {"Roster": "hash-roster"}
    artifact = AllStatsCommitArtifact(discovered=discovered, content_hashes=content_hashes, plan=plan)
    planned = tuple(
        PlannedArtifact(source_root_key=ALLSTATS_SOURCE_ROOT_KEY, relative_path=meta.relative_path, content_hash=content_hashes[label], status="planned_insert")
        for label, meta in discovered.files.items()
    )
    family_plan = SourceFamilyPlan(family="allstats", contract_version=ALLSTATS_IMPORT_CONTRACT_VERSION, artifacts=planned)
    return artifact, family_plan


def _team_game_log_family():
    df = pd.DataFrame([{"game_num": 1, "date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70, "ORtg": 100.0, "DRtg": 95.0, "net_rtg": 5.0}])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    plan = build_game_log_import_plan(parsed)
    discovered = DiscoveredGameLogFile(team_slug="michigan-state", season=SEASON, relative_path="michigan-state/michigan-state_2026_game_log.csv", file_size_bytes=100, filesystem_mtime=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    content_hash = "hash-team-game-log"
    artifact = TeamGameLogCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan)
    planned = (PlannedArtifact(source_root_key=GAME_LOG_SOURCE_ROOT_KEY, relative_path=discovered.relative_path, content_hash=content_hash, status="planned_insert"),)
    family_plan = SourceFamilyPlan(family="team_game_log", contract_version=TEAM_GAMELOG_IMPORT_CONTRACT_VERSION, artifacts=planned)
    return artifact, family_plan


def _player_game_log_family():
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = pd.DataFrame([{"game_num": 1, "date": "2026-01-15", "opponent": "Colgate", "result": "W 80-70", "mp": 30, "pts": 10, "trb": 5, "ast": 3, "stl": 1, "blk": 0, "tov": 2, "fg": 4, "fga": 8}])
    from src.identity.season_identity import resolve_season_identity
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=[build_player_identity("A Player", team_identity.team_id, SEASON)], team_slug="michigan-state", player_slug="a_player", season=SEASON)
    plan = build_player_game_log_import_plan(parsed)
    discovered = DiscoveredPlayerGameLogFile(team_slug="michigan-state", player_slug="a_player", season=SEASON, relative_path="michigan-state/a_player_2026_gamelog.csv", file_size_bytes=100, filesystem_mtime=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    content_hash = "hash-player-game-log"
    artifact = PlayerGameLogCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan)
    planned = (PlannedArtifact(source_root_key=PLAYER_GAME_LOG_SOURCE_ROOT_KEY, relative_path=discovered.relative_path, content_hash=content_hash, status="planned_insert"),)
    family_plan = SourceFamilyPlan(family="player_game_log", contract_version=PLAYER_GAMELOG_IMPORT_CONTRACT_VERSION, artifacts=planned)
    return artifact, family_plan


def _espn_family():
    df = pd.DataFrame([{
        "RK": 1, "Name": "Some Player", "Team": "BYU", "POS": "G", "GP": 20, "MIN": 25.0, "PTS": 10.0,
        "FGM": 4.0, "FGA": 8.0, "FG%": 50.0, "3PM": 1.0, "3PA": 3.0, "3P%": 33.0, "FTM": 2.0, "FTA": 3.0,
        "FT%": 66.0, "REB": 4.0, "AST": 3.0, "STL": 1.0, "BLK": 0.5, "TO": 2.0,
    }])
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    plan = build_espn_source_import_plan(parsed)
    discovered = DiscoveredEspnFile(filename="espn_full_player_stats.csv", relative_path="espn_full_player_stats.csv", file_size_bytes=100, filesystem_mtime=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    content_hash = "hash-espn"
    artifact = EspnSourceStatsCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan, season_id=SEASON)
    planned = (PlannedArtifact(source_root_key=ESPN_SOURCE_ROOT_KEY, relative_path=discovered.relative_path, content_hash=content_hash, status="planned_insert"),)
    family_plan = SourceFamilyPlan(family="espn", contract_version=ESPN_IMPORT_CONTRACT_VERSION, artifacts=planned)
    return artifact, family_plan


def _full_manifest(*family_plans):
    return build_dry_run_manifest(generated_at=dt.datetime.now(dt.timezone.utc), families=list(family_plans))


def _all_four_requests_and_manifest():
    allstats_artifact, allstats_plan = _allstats_family()
    team_gl_artifact, team_gl_plan = _team_game_log_family()
    player_gl_artifact, player_gl_plan = _player_game_log_family()
    espn_artifact, espn_plan = _espn_family()
    manifest = _full_manifest(allstats_plan, team_gl_plan, player_gl_plan, espn_plan)
    requests = [
        AllStatsFamilyRequest(artifacts=[allstats_artifact], run_id="a" * 32, started_at=dt.datetime.now(dt.timezone.utc)),
        TeamGameLogFamilyRequest(artifacts=[team_gl_artifact], run_id="b" * 32, started_at=dt.datetime.now(dt.timezone.utc)),
        PlayerGameLogFamilyRequest(artifacts=[player_gl_artifact], run_id="c" * 32, started_at=dt.datetime.now(dt.timezone.utc)),
        EspnFamilyRequest(artifacts=[espn_artifact], run_id="d" * 32, started_at=dt.datetime.now(dt.timezone.utc)),
    ]
    return requests, manifest


# ---------------------------------------------------------------------------
# Frozen request/result contracts
# ---------------------------------------------------------------------------


def test_family_requests_expose_their_own_family_name():
    allstats_artifact, _ = _allstats_family()
    request = AllStatsFamilyRequest(artifacts=[allstats_artifact], run_id="x" * 32, started_at=dt.datetime.now(dt.timezone.utc))
    assert request.family == "allstats"


def test_family_requests_are_frozen():
    allstats_artifact, _ = _allstats_family()
    request = AllStatsFamilyRequest(artifacts=[allstats_artifact], run_id="x" * 32, started_at=dt.datetime.now(dt.timezone.utc))
    with pytest.raises(Exception):
        request.run_id = "y" * 32  # type: ignore[misc]


def test_coordinated_outcomes_are_frozen():
    outcome = CoordinatedFamilyOutcome(family="allstats", status="committed")
    with pytest.raises(Exception):
        outcome.status = "failed"  # type: ignore[misc]
    result = CoordinatedImportRunResult(status="succeeded", family_outcomes=(outcome,))
    with pytest.raises(Exception):
        result.status = "failed"  # type: ignore[misc]


def test_result_aggregation_properties():
    outcomes = (
        CoordinatedFamilyOutcome(family="allstats", status="committed"),
        CoordinatedFamilyOutcome(family="team_game_log", status="failed"),
        CoordinatedFamilyOutcome(family="player_game_log", status="not_started"),
    )
    result = CoordinatedImportRunResult(status="failed", family_outcomes=outcomes)
    assert result.committed_families == ("allstats",)
    assert result.failed_families == ("team_game_log",)
    assert result.not_started_families == ("player_game_log",)


# ---------------------------------------------------------------------------
# _extract_family_manifest -- lossless partitioning
# ---------------------------------------------------------------------------


def test_extract_family_manifest_preserves_the_exact_same_family_plan_object():
    _, allstats_plan = _allstats_family()
    _, espn_plan = _espn_family()
    manifest = _full_manifest(allstats_plan, espn_plan)
    extracted = _extract_family_manifest(manifest, "espn")
    assert len(extracted.families) == 1
    assert extracted.families[0] is espn_plan  # the SAME object, never rebuilt
    assert extracted.generated_at == manifest.generated_at


def test_extract_family_manifest_rejects_a_family_the_manifest_never_planned():
    _, allstats_plan = _allstats_family()
    manifest = _full_manifest(allstats_plan)
    with pytest.raises(ValidationError):
        _extract_family_manifest(manifest, "espn")


def test_extract_family_manifest_preserves_every_planned_artifact_field():
    _, allstats_plan = _allstats_family()
    manifest = _full_manifest(allstats_plan)
    extracted = _extract_family_manifest(manifest, "allstats")
    original_artifact = allstats_plan.artifacts[0]
    extracted_artifact = extracted.families[0].artifacts[0]
    assert extracted_artifact.source_root_key == original_artifact.source_root_key
    assert extracted_artifact.relative_path == original_artifact.relative_path
    assert extracted_artifact.content_hash == original_artifact.content_hash
    assert extracted_artifact.status == original_artifact.status
    assert extracted.families[0].contract_version == allstats_plan.contract_version


# ---------------------------------------------------------------------------
# _validate_family_requests -- preflight, ordering, refusal
# ---------------------------------------------------------------------------


def test_validate_family_requests_orders_by_source_family_order_never_input_order():
    requests, manifest = _all_four_requests_and_manifest()
    reversed_requests = list(reversed(requests))
    prepared = _validate_family_requests(manifest, MANIFEST_SCHEMA_VERSION, reversed_requests)
    assert [pair[0].family for pair in prepared] == list(SOURCE_FAMILY_ORDER)


def test_validate_family_requests_accepts_a_single_family_subset():
    _, espn_plan = _espn_family()
    espn_artifact, _ = _espn_family()
    manifest = _full_manifest(espn_plan)
    request = EspnFamilyRequest(artifacts=[espn_artifact], run_id="x" * 32, started_at=dt.datetime.now(dt.timezone.utc))
    prepared = _validate_family_requests(manifest, MANIFEST_SCHEMA_VERSION, [request])
    assert [pair[0].family for pair in prepared] == ["espn"]


def test_validate_family_requests_rejects_duplicate_family():
    espn_artifact, espn_plan = _espn_family()
    manifest = _full_manifest(espn_plan)
    request = EspnFamilyRequest(artifacts=[espn_artifact], run_id="x" * 32, started_at=dt.datetime.now(dt.timezone.utc))
    with pytest.raises(ValidationError):
        _validate_family_requests(manifest, MANIFEST_SCHEMA_VERSION, [request, request])


def test_validate_family_requests_rejects_duplicate_run_id_across_different_families():
    """Refused upfront during preflight -- never left to surface reactively as a mid-sequence IdentityConflictError from the second family's own PipelineRunRepository.start()."""
    espn_artifact, espn_plan = _espn_family()
    allstats_artifact, allstats_plan = _allstats_family()
    manifest = _full_manifest(espn_plan, allstats_plan)
    shared_run_id = "shared-run-id" + "0" * 19
    espn_request = EspnFamilyRequest(artifacts=[espn_artifact], run_id=shared_run_id, started_at=dt.datetime.now(dt.timezone.utc))
    allstats_request = AllStatsFamilyRequest(artifacts=[allstats_artifact], run_id=shared_run_id, started_at=dt.datetime.now(dt.timezone.utc))
    with pytest.raises(ValidationError):
        _validate_family_requests(manifest, MANIFEST_SCHEMA_VERSION, [espn_request, allstats_request])


def test_validate_family_requests_rejects_unknown_request_type():
    _, espn_plan = _espn_family()
    manifest = _full_manifest(espn_plan)
    with pytest.raises(ValidationError):
        _validate_family_requests(manifest, MANIFEST_SCHEMA_VERSION, ["not-a-family-request"])


def test_validate_family_requests_rejects_a_family_missing_from_the_manifest():
    espn_artifact, _ = _espn_family()
    _, allstats_plan = _allstats_family()
    manifest = _full_manifest(allstats_plan)  # espn never planned
    request = EspnFamilyRequest(artifacts=[espn_artifact], run_id="x" * 32, started_at=dt.datetime.now(dt.timezone.utc))
    with pytest.raises(ValidationError):
        _validate_family_requests(manifest, MANIFEST_SCHEMA_VERSION, [request])


def test_validate_family_requests_rejects_stale_content_hash_via_the_familys_own_validator():
    """The coordinator never reimplements artifact validation -- it delegates to each family's own existing _validate_artifacts, proven here by a stale-hash refusal exactly like that family's own runner-level test."""
    espn_artifact, espn_plan = _espn_family()
    manifest = _full_manifest(espn_plan)
    changed_discovered = espn_artifact.discovered
    changed_artifact = EspnSourceStatsCommitArtifact(discovered=changed_discovered, content_hash="a-different-hash-entirely", plan=espn_artifact.plan, season_id=espn_artifact.season_id)
    request = EspnFamilyRequest(artifacts=[changed_artifact], run_id="x" * 32, started_at=dt.datetime.now(dt.timezone.utc))
    with pytest.raises(ValidationError):
        _validate_family_requests(manifest, MANIFEST_SCHEMA_VERSION, [request])


def test_validate_family_requests_rejects_a_bare_sequence_type_error():
    _, espn_plan = _espn_family()
    manifest = _full_manifest(espn_plan)
    with pytest.raises(ValidationError):
        _validate_family_requests(manifest, MANIFEST_SCHEMA_VERSION, "not-a-sequence")


def test_validate_family_requests_rejects_non_manifest_type():
    espn_artifact, _ = _espn_family()
    request = EspnFamilyRequest(artifacts=[espn_artifact], run_id="x" * 32, started_at=dt.datetime.now(dt.timezone.utc))
    with pytest.raises(ValidationError):
        _validate_family_requests("not-a-manifest", MANIFEST_SCHEMA_VERSION, [request])


def test_validate_family_requests_does_not_mutate_the_manifest_or_artifacts():
    requests, manifest = _all_four_requests_and_manifest()
    families_before = manifest.families
    _validate_family_requests(manifest, MANIFEST_SCHEMA_VERSION, requests)
    assert manifest.families == families_before
    assert manifest.families is families_before


# ---------------------------------------------------------------------------
# run_controlled_bulk_import: target-verification failure (no real Postgres needed)
# ---------------------------------------------------------------------------


def test_coordinator_refuses_a_non_postgres_connection_before_any_other_step():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    designation = DatabaseTargetDesignation(expected_database_name="napsedge_pg_test_abc", acknowledgement=build_expected_acknowledgement("napsedge_pg_test_abc"))
    with engine.connect() as conn:
        with pytest.raises(DatabaseTargetVerificationError):
            run_controlled_bulk_import(
                connection=conn, target_designation=designation, session_factory=None,
                manifest="not-even-a-real-manifest", manifest_schema_version="wrong-too",
                family_requests=["not-even-a-real-request"],
            )


# ---------------------------------------------------------------------------
# AST guards -- no adapter calls, no engine/session construction, no
# environment reads, no Python hash(), no direct commit/rollback/flush.
# ---------------------------------------------------------------------------


def test_module_never_imports_any_adapter_directly():
    import src.pipeline.bulk_import_coordinator as mod

    tree = ast.parse(inspect.getsource(mod))
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    forbidden_suffixes = ("allstats_import_adapter", "game_log_import_adapter", "player_game_log_import_adapter", "espn_import_adapter")
    assert not any(m.endswith(forbidden_suffixes) for m in imported_modules)


def test_module_never_constructs_its_own_engine_session_or_connection():
    import src.pipeline.bulk_import_coordinator as mod

    tree = ast.parse(inspect.getsource(mod))
    called_names = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not ({"create_engine", "sessionmaker", "Session", "build_engine", "build_session_factory", "connect"} & called_names)


def test_module_never_reads_environment_or_dotenv():
    import src.pipeline.bulk_import_coordinator as mod

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
    import src.pipeline.bulk_import_coordinator as mod

    tree = ast.parse(inspect.getsource(mod))
    called_names = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "hash" not in called_names


def test_module_never_opens_a_unit_of_work_or_calls_a_repository():
    """This coordinator writes nothing of its own -- every write is delegated to the four family runners. Import-based (not raw substring) check: the module's own docstring legitimately mentions 'unit_of_work' in prose."""
    import src.pipeline.bulk_import_coordinator as mod

    tree = ast.parse(inspect.getsource(mod))
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.update(a.name for a in node.names)
    assert "unit_of_work" not in imported_names
    assert not any(name.endswith("Repository") for name in imported_names)


def test_module_never_imports_a_manifest_writer_or_discovery_machinery():
    import src.pipeline.bulk_import_coordinator as mod

    tree = ast.parse(inspect.getsource(mod))
    imported_names: set[str] = set()
    imported_froms: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_froms.update(a.name for a in node.names)
    assert "pathlib" not in imported_names
    assert not ({"discover_allstats_artifacts", "discover_espn_artifacts", "discover_game_log_artifacts", "discover_player_game_log_artifacts", "serialize_manifest", "write_dry_run_manifest"} & imported_froms)


# ---------------------------------------------------------------------------
# No credential / absolute-path leakage
# ---------------------------------------------------------------------------


def test_validation_errors_never_leak_an_absolute_path():
    espn_artifact, _ = _espn_family()
    _, allstats_plan = _allstats_family()
    manifest = _full_manifest(allstats_plan)
    request = EspnFamilyRequest(artifacts=[espn_artifact], run_id="x" * 32, started_at=dt.datetime.now(dt.timezone.utc))
    with pytest.raises(ValidationError) as excinfo:
        _validate_family_requests(manifest, MANIFEST_SCHEMA_VERSION, [request])
    message = str(excinfo.value)
    assert "C:\\" not in message
    assert not message.startswith("/")
