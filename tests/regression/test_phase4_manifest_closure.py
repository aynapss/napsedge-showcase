"""
Phase 4C5R -- public-symbol and manifest-closure regression for Phase 4C5's
own extraction (11 previously-deferred matchup components). Fixture-
independent: only reads committed, tracked files (both manifest CSVs, the
one touched src module), never TeamDump/cache/the notebook's own content.

Scoped deliberately narrow, per Phase 4C5R's own instruction not to become
an uncontrolled repository-wide manifest audit: this file only asserts
properties of the 7 notebook-function names Phase 4C5 touched, plus two
whole-file sanity checks (both CSVs parse; identical key sets; zero
duplicate keys) that are cheap to run and meaningful regardless of scope.
It does NOT assert that every row in either 1,156-row CSV is well-formed --
a handful of pre-existing rows from an unrelated, earlier Phase 6 batch
have a confirmed, out-of-scope unescaped-comma field-splitting defect
(discovered during Phase 4C5R's own investigation, not introduced or
fixed by it); asserting global row-shape correctness here would make this
test fail for a defect this batch did not create and was not asked to fix.
"""
from __future__ import annotations

import csv
from pathlib import Path

from src.paths import ROOT_DIR

MANIFEST_PATH = ROOT_DIR / "docs" / "function_manifest.csv"
COVERAGE_PATH = ROOT_DIR / "docs" / "notebook_src_coverage.csv"

#: The 7 real v15 notebook function names Phase 4C5 extracted -- each
#: already had a row in both manifests before Phase 4C5 (confirmed during
#: Phase 4C5R's own investigation: the manifests are keyed by notebook
#: function name, not by every new Python symbol a batch introduces, and
#: several Python symbols can map to one notebook-function row -- e.g.
#: team_matchup_overall_score_v15 and team_matchup_overall_score_full_v15
#: both map to the single team_matchup_overall_score row). Phase 4C5R
#: updated these 7 existing rows' field values; it added zero new rows.
PHASE_4C5_NOTEBOOK_FUNCTIONS = frozenset({
    "team_matchup_overall_score",
    "build_matchup_features",
    "compute_team_identity_profile",
    "compute_matchup_conflicts",
    "get_coach_profile",
    "compute_coaching_edge",
    "detect_situational_spots",
})

#: Phase 4C5's own new private Python helpers (leading underscore) --
#: confirmed excluded from both manifests, per the established convention
#: that private helpers are never manifested.
PHASE_4C5_PRIVATE_HELPERS = frozenset({
    "_team_archetype_label_v15",
    "_clean_coach_name_v15",
    "_derive_dynamic_coach_profile_v15",
    "_resolve_coach_name_v15",
    "_evidence_to_team_dict",
})

#: Phase 4C5's own new additive canonical constructs with no 1:1 real
#: notebook-function name (a composition of several ported functions'
#: worth of logic) -- confirmed, by precedent (the pre-existing
#: build_matchup_projection/MatchupProjectionResult/TeamMatchupSideResult
#: in the same module are never manifested either), correctly excluded
#: from both manifests under the established "one row per real notebook
#: function name" convention.
PHASE_4C5_UNMANIFESTED_CANONICAL_SYMBOLS = frozenset({
    "build_matchup_feature_extension",
    "MatchupFeatureExtensionEvidence",
    "MatchupFeatureExtensionResult",
})

DEST_MODULE = "src/transformations/matchup_projection_transformation.py"


def _read_manifest_rows() -> list[dict]:
    with open(MANIFEST_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _read_coverage_rows() -> list[dict]:
    with open(COVERAGE_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_both_manifests_parse_without_exception():
    manifest_rows = _read_manifest_rows()
    coverage_rows = _read_coverage_rows()
    assert len(manifest_rows) == 1156
    assert len(coverage_rows) == 1156


def test_identical_key_sets_between_manifests():
    manifest_keys = {r["name"] for r in _read_manifest_rows()}
    coverage_keys = {r["notebook_function"] for r in _read_coverage_rows()}
    assert manifest_keys == coverage_keys


def test_no_duplicate_keys_in_either_manifest():
    manifest_rows = _read_manifest_rows()
    coverage_rows = _read_coverage_rows()
    manifest_names = [r["name"] for r in manifest_rows]
    coverage_names = [r["notebook_function"] for r in coverage_rows]
    assert len(manifest_names) == len(set(manifest_names))
    assert len(coverage_names) == len(set(coverage_names))


def test_phase4c5_notebook_functions_represented_exactly_once_in_each_manifest():
    manifest_rows = _read_manifest_rows()
    coverage_rows = _read_coverage_rows()
    for name in PHASE_4C5_NOTEBOOK_FUNCTIONS:
        manifest_matches = [r for r in manifest_rows if r["name"] == name]
        coverage_matches = [r for r in coverage_rows if r["notebook_function"] == name]
        assert len(manifest_matches) == 1, f"{name} appears {len(manifest_matches)} times in function_manifest.csv, expected 1"
        assert len(coverage_matches) == 1, f"{name} appears {len(coverage_matches)} times in notebook_src_coverage.csv, expected 1"


def test_phase4c5_notebook_functions_cite_phase_4c5_and_the_destination_module():
    manifest_rows = {r["name"]: r for r in _read_manifest_rows()}
    coverage_rows = {r["notebook_function"]: r for r in _read_coverage_rows()}
    for name in PHASE_4C5_NOTEBOOK_FUNCTIONS:
        m = manifest_rows[name]
        c = coverage_rows[name]
        combined_manifest_text = m["name"] + m["in_src_files"] + m["status"]
        combined_coverage_text = (
            c["existing_src_destination"] + c["migration_status"] + c["parity_test_status"]
        )
        assert "Phase 4C5" in combined_manifest_text, f"{name}'s function_manifest.csv row does not cite Phase 4C5"
        assert "Phase 4C5" in combined_coverage_text, f"{name}'s notebook_src_coverage.csv row does not cite Phase 4C5"
        assert DEST_MODULE in combined_manifest_text, f"{name}'s function_manifest.csv row does not cite {DEST_MODULE}"
        assert DEST_MODULE in combined_coverage_text, f"{name}'s notebook_src_coverage.csv row does not cite {DEST_MODULE}"


def test_destination_module_exists():
    assert (ROOT_DIR / DEST_MODULE).exists()
    assert (ROOT_DIR / DEST_MODULE).is_file()


def test_phase4c5_private_helpers_not_listed_in_either_manifest():
    manifest_keys = {r["name"] for r in _read_manifest_rows()}
    coverage_keys = {r["notebook_function"] for r in _read_coverage_rows()}
    for helper in PHASE_4C5_PRIVATE_HELPERS:
        assert helper not in manifest_keys, f"private helper {helper} unexpectedly listed in function_manifest.csv"
        assert helper not in coverage_keys, f"private helper {helper} unexpectedly listed in notebook_src_coverage.csv"


def test_phase4c5_canonical_only_symbols_not_listed_as_their_own_row():
    """
    build_matchup_feature_extension/MatchupFeatureExtensionEvidence/
    MatchupFeatureExtensionResult have no single corresponding real
    notebook function name, matching the established precedent of
    build_matchup_projection/MatchupProjectionResult/TeamMatchupSideResult
    in the same module -- confirmed never given their own row, documented
    instead as prose inside the relevant notebook-function row's own
    status/parity_test_status text.
    """
    manifest_keys = {r["name"] for r in _read_manifest_rows()}
    coverage_keys = {r["notebook_function"] for r in _read_coverage_rows()}
    for symbol in PHASE_4C5_UNMANIFESTED_CANONICAL_SYMBOLS:
        assert symbol not in manifest_keys
        assert symbol not in coverage_keys


def test_matchup_projection_module_defines_every_phase4c5_public_symbol():
    """Confirms every symbol this test file names actually exists in the touched module -- a stale test name would be caught here."""
    import src.transformations.matchup_projection_transformation as mod

    public_v15_functions = (
        "compute_team_identity_profile_v15", "compute_matchup_conflicts_v15", "get_coach_profile_v15",
        "compute_coaching_edge_v15", "detect_situational_spots_v15", "build_matchup_features_v15",
        "team_matchup_overall_score_full_v15",
    )
    for name in public_v15_functions:
        assert hasattr(mod, name), f"{name} not found in matchup_projection_transformation.py"
    for name in PHASE_4C5_PRIVATE_HELPERS:
        assert hasattr(mod, name), f"private helper {name} not found in matchup_projection_transformation.py"
    for name in PHASE_4C5_UNMANIFESTED_CANONICAL_SYMBOLS:
        assert hasattr(mod, name), f"{name} not found in matchup_projection_transformation.py"
