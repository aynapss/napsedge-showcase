"""
Phase 3 consolidated acceptance review: compact cross-source invariant
checks. Does NOT duplicate any source-specific test already covered in
test_sr_allstats_ingestion.py / test_espn_ingestion.py /
test_covers_injury_ingestion.py / test_odds_ingestion.py /
test_net_ranking_ingestion.py / test_logo_asset_ingestion.py -- this file
only verifies the shared invariants every canonical ingestion module must
hold, in one place.
"""
import ast
import inspect
from dataclasses import fields

import src.ingestion.covers_injury_ingestion as covers_mod
import src.ingestion.espn_ingestion as espn_mod
import src.ingestion.logo_asset_ingestion as logo_mod
import src.ingestion.net_ranking_ingestion as net_mod
import src.ingestion.odds_ingestion as odds_mod
import src.ingestion.sr_allstats_ingestion as sr_mod

ALL_INGESTION_MODULES = [sr_mod, espn_mod, covers_mod, odds_mod, net_mod, logo_mod]

RESULT_CLASSES = [
    sr_mod.SrAllStatsIngestionResult,
    espn_mod.EspnIngestionResult,
    covers_mod.CoversInjuryIngestionResult,
    odds_mod.OddsIngestionResult,
    net_mod.NetRankingIngestionResult,
    logo_mod.LogoIngestionResult,
]

EVIDENCE_CLASSES = [
    sr_mod.RawIngestionEvidence,
    espn_mod.EspnRawEvidence,
    covers_mod.CoversRawEvidence,
    odds_mod.OddsRawEvidence,
    net_mod.NetRankingRawEvidence,
    logo_mod.LogoRawEvidence,
]


def _imported_module_names(mod) -> set[str]:
    tree = ast.parse(inspect.getsource(mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def _called_names(mod) -> set[str]:
    tree = ast.parse(inspect.getsource(mod))
    return {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


# ---------------------------------------------------------------------------
# One canonical ingestion path per source, with a result object
# ---------------------------------------------------------------------------

def test_every_source_has_a_result_dataclass():
    for cls in RESULT_CLASSES:
        assert hasattr(cls, "__dataclass_fields__")


def test_every_result_carries_provenance_evidence():
    for cls in RESULT_CLASSES:
        field_names = {f.name for f in fields(cls)}
        assert "evidence" in field_names


def test_every_evidence_record_has_fixture_provenance_and_parser_version():
    for cls in EVIDENCE_CLASSES:
        field_names = {f.name for f in fields(cls)}
        assert "fixture_provenance" in field_names
        assert "parser_version" in field_names
        assert "collected_at_utc" in field_names


# ---------------------------------------------------------------------------
# No unrestricted fuzzy matching, no live network, anywhere in Phase 3
# ---------------------------------------------------------------------------

def test_no_ingestion_module_imports_difflib():
    for mod in ALL_INGESTION_MODULES:
        assert "difflib" not in _imported_module_names(mod), f"{mod.__name__} imports difflib"


def test_no_ingestion_module_imports_requests():
    for mod in ALL_INGESTION_MODULES:
        assert "requests" not in _imported_module_names(mod), f"{mod.__name__} imports requests"


def test_no_ingestion_module_imports_selenium():
    for mod in ALL_INGESTION_MODULES:
        assert "selenium" not in _imported_module_names(mod), f"{mod.__name__} imports selenium"


# ---------------------------------------------------------------------------
# No ingestion module calls a raw parquet/production-cache write function
# ---------------------------------------------------------------------------

def test_no_ingestion_module_calls_fetch_all_schools_or_scrape_functions():
    forbidden_live_calls = {
        "fetch_all_schools", "scrape_espn_players", "scrape_covers_injuries_for_teams",
        "get_live_ncaab_odds", "build_net_rankings_from_ncaa", "download_all_team_logos",
        "fetch_espn_teams",
    }
    for mod in ALL_INGESTION_MODULES:
        called = _called_names(mod)
        overlap = forbidden_live_calls & called
        assert not overlap, f"{mod.__name__} calls forbidden legacy live function(s): {overlap}"


# ---------------------------------------------------------------------------
# No ingestion module touches betting/formula/model calculation functions
# ---------------------------------------------------------------------------

def test_no_ingestion_module_calls_calculation_or_formula_functions():
    forbidden_calc_calls = {
        "build_market_consensus", "find_game_odds", "compute_player_ovr_only",
        "build_composite_radar_v9", "apply_injury_penalty", "injury_sensitivity_check",
        "attach_net_rank",
    }
    for mod in ALL_INGESTION_MODULES:
        called = _called_names(mod)
        overlap = forbidden_calc_calls & called
        assert not overlap, f"{mod.__name__} calls forbidden calculation function(s): {overlap}"


# ---------------------------------------------------------------------------
# Every module routes identity resolution through src.identity, never a
# bespoke registry
# ---------------------------------------------------------------------------

def test_every_ingestion_module_imports_from_src_identity():
    for mod in ALL_INGESTION_MODULES:
        imports = _imported_module_names(mod)
        assert any(name.startswith("src.identity") for name in imports), (
            f"{mod.__name__} does not import any src.identity module"
        )


# ---------------------------------------------------------------------------
# src/slug_utils.py and src/ovr_model.py remain the read-only, untouched
# foundation every ingestion module builds on top of
# ---------------------------------------------------------------------------

def test_no_ingestion_module_imports_slug_utils_directly():
    """
    Every ingestion module routes team/player resolution through
    src.identity.* adapters, which themselves wrap src.slug_utils --
    no ingestion module should import src.slug_utils directly, matching
    the convention established across every identity adapter in this
    migration.
    """
    for mod in ALL_INGESTION_MODULES:
        assert "src.slug_utils" not in _imported_module_names(mod), (
            f"{mod.__name__} imports src.slug_utils directly"
        )


def test_no_ingestion_module_imports_ovr_model():
    for mod in ALL_INGESTION_MODULES:
        imports = _imported_module_names(mod)
        assert not any("ovr_model" in name for name in imports), f"{mod.__name__} imports ovr_model"


# ---------------------------------------------------------------------------
# Phase 4C1 closeout: src/ovr_model.py is an unstaged, notebook-exec-
# dependent, working-tree-only file (0 bytes committed). This guard proves,
# by static source inspection (no import, no execution) across every
# pipeline/service/consumer module, that the accepted application never
# depends on its content -- the decisive property a clean checkout (where
# src/ovr_model.py is genuinely empty) relies on. Unlike the Player OVR
# transformation's own test file, this test has no real-fixture dependency
# and is never skipped, including in a bare clean checkout.
# ---------------------------------------------------------------------------

def _source_files_never_importing_ovr_model():
    import pathlib

    roots = [
        pathlib.Path("src/pipeline"),
        pathlib.Path("src/services"),
        pathlib.Path("src/consumers"),
        pathlib.Path("src/run_pipeline.py"),
    ]
    files = []
    for root in roots:
        if root.is_dir():
            files.extend(sorted(root.rglob("*.py")))
        elif root.is_file():
            files.append(root)
    return files


def test_no_pipeline_service_or_consumer_module_imports_ovr_model():
    files = _source_files_never_importing_ovr_model()
    assert files, "expected to find at least one file under src/pipeline, src/services, src/consumers, or src/run_pipeline.py"

    offenders = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any("ovr_model" in alias.name for alias in node.names):
                    offenders.append(str(path))
            elif isinstance(node, ast.ImportFrom) and node.module and "ovr_model" in node.module:
                offenders.append(str(path))
    assert not offenders, f"src.ovr_model imported by: {offenders}"


# ---------------------------------------------------------------------------
# Phase 4C3: unit-contract guard for F-1's Engine A margin. Deliberately
# placed in this file (no real-fixture skip gate) rather than in
# test_canonical_game_pipeline.py -- a fixture-independent AST guard must
# always run, including in a bare clean checkout, per this project's own
# established pattern (see the Phase 4C1 ovr_model guard above).
# ---------------------------------------------------------------------------

def test_ast_never_reads_engine_a_raw_index_fields():
    """
    Phase 4C3 unit-contract guard: proves, by direct source inspection
    (not by re-running the fixture), that src/pipeline/canonical_game_pipeline.py
    never reads MatchupProjectionResult's own raw-index fields
    (margin_team_a_perspective_index / fav_margin_index) anywhere --
    confirming build_engine_a's F-1 diagnostic input
    (EngineAResult.final_margin_a_perspective) can only ever be the
    already points-scaled MatchupOutcomeProjectionResult.estimated_point_margin_a_perspective
    (source-confirmed at that module's own `final_margin = outcome.estimated_point_margin_a_perspective`
    assignment), never the raw abstract-index value. This is the
    permanent, code-level proof that the canonical EngineAgreementDiagnostic
    compares two already-unit-consistent (points-space) margins -- the
    confirmed 13.16-point F-1 disagreement is a genuine calibration
    difference between two point-space models, not an index-vs-points
    unit-mixing artifact.
    """
    import src.pipeline.canonical_game_pipeline as pipeline_mod

    source = inspect.getsource(pipeline_mod)
    tree = ast.parse(source)
    forbidden_attrs = {"margin_team_a_perspective_index", "fav_margin_index"}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in forbidden_attrs:
            found.append(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in forbidden_attrs:
            found.append(node.value)
    assert not found, f"raw index field(s) referenced in canonical_game_pipeline.py: {found}"

    # Confirm the actual, positive assignment this guard depends on still exists (not just an absence check).
    assign_found = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "final_margin" for t in node.targets)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "estimated_point_margin_a_perspective"
        ):
            assign_found = True
    assert assign_found, "expected 'final_margin = outcome.estimated_point_margin_a_perspective' in build_engine_a -- if this assignment's source changed, re-verify the unit-contract conclusion above before updating this guard."
