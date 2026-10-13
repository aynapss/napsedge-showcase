"""
Regression tests for src/identity/model_slug_identity.py
(Phase 2A, Model-slug adapter -- final of 6).

Folder-existence tests use a small, controlled tmp_path directory (via the
injectable `cbb_dir` parameter) rather than the real `TeamDump/CBB/` tree,
so results don't depend on which teams happen to have been scraped on this
particular machine. A few tests additionally use the REAL tree (skipped if
absent) to document the two genuine duplicate-folder findings from this
session (the "st"->"saint" corruption bug, and St. Bonaventure's 3-way
folder duplication) without depending on their exact folder count -- these
are read-only checks, nothing here writes into the real tree.
"""
import os

import pytest

from src.identity.contracts import TeamIdentity
from src.identity.model_slug_identity import (
    resolve_model_team_folder,
    resolve_model_team_identity,
    resolve_team_slug_v15_compat,
    team_name_to_slug_v15,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REAL_CBB_DIR = os.path.join(REPO_ROOT, "TeamDump", "CBB")


@pytest.fixture
def cbb_dir(tmp_path):
    """A throwaway folder tree with a small, known set of team folders --
    named after real sr_slugs so resolve_model_team_folder can find them."""
    for slug in ["st-bonaventure", "alabama-birmingham", "college-of-charleston", "alabama-state"]:
        (tmp_path / slug).mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# Exact model slugs, canonical-name-to-slug, reverse resolution
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query,expected_display_name", [
    ("st-bonaventure", "St. Bonaventure"),
    ("St. Bonaventure", "St. Bonaventure"),
    ("Saint Joseph's", "Saint Joseph's"),
    ("uab", "Alabama-Birmingham"),
    ("tcu", "Texas Christian"),
    ("ucf", "Central Florida"),
    ("smu", "Southern Methodist"),
    ("nc-state", "North Carolina State"),
    ("college-of-charleston", "Charleston"),
])
def test_exact_and_abbreviated_model_slugs_resolve(query, expected_display_name):
    result = resolve_model_team_identity(query)
    assert isinstance(result, TeamIdentity), f"{query!r} should resolve, got {result}"
    assert result.display_name == expected_display_name


def test_hyphenated_alias_resolves_without_space_conversion_breaking_it():
    # Same fix as the NET-ranking adapter: try the hyphenated form first.
    result = resolve_model_team_identity("college-of-charleston")
    assert isinstance(result, TeamIdentity)
    assert result.display_name == "Charleston"


# ---------------------------------------------------------------------------
# Miami / Loyola ambiguity, state-school distinctions (no shortening bug)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query,expected_display_name", [
    ("miami-fl", "Miami (FL)"),
    ("miami-oh", "Miami (OH)"),
    ("loyola-chicago", "Loyola Chicago"),
    ("loyola-il", "Loyola Chicago"),
])
def test_disambiguated_miami_and_loyola_resolve(query, expected_display_name):
    result = resolve_model_team_identity(query)
    assert isinstance(result, TeamIdentity)
    assert result.display_name == expected_display_name


def test_bare_miami_is_ambiguous():
    result = resolve_model_team_identity("miami")
    assert not isinstance(result, TeamIdentity)
    assert result.reason == "ambiguous"


def test_state_school_and_bare_school_are_not_conflated():
    # Confirms the model-slug adapter avoids the same
    # resolve_team_identity_by_shortening bug fixed in the NET adapter
    # (Ohio vs Ohio State would silently conflate if shortening were used).
    ohio_state = resolve_model_team_identity("ohio-state")
    ohio = resolve_model_team_identity("ohio")
    assert isinstance(ohio_state, TeamIdentity) and isinstance(ohio, TeamIdentity)
    assert ohio_state.display_name == "Ohio State"
    assert ohio.display_name == "Ohio"
    assert ohio_state.team_id != ohio.team_id


# ---------------------------------------------------------------------------
# Unknown model slugs / deterministic repeated resolution
# ---------------------------------------------------------------------------

def test_unknown_model_slug_is_unresolved():
    result = resolve_model_team_identity("Not A Real Team Xyz")
    assert not isinstance(result, TeamIdentity)
    assert result.reason == "not_found"


def test_repeated_resolution_is_deterministic():
    ids = {resolve_model_team_identity("uab").team_id for _ in range(3)}
    assert len(ids) == 1


# ---------------------------------------------------------------------------
# Folder resolution: exact match, alias fallback, unresolved
# ---------------------------------------------------------------------------

def test_resolve_model_team_folder_finds_sr_slug_folder(cbb_dir):
    result = resolve_model_team_folder("St. Bonaventure", cbb_dir=cbb_dir)
    assert isinstance(result, tuple)
    identity, slug = result
    assert identity.display_name == "St. Bonaventure"
    assert slug == "st-bonaventure"


def test_resolve_model_team_folder_finds_via_abbreviation_query(cbb_dir):
    identity, slug = resolve_model_team_folder("uab", cbb_dir=cbb_dir)
    assert identity.display_name == "Alabama-Birmingham"
    assert slug == "alabama-birmingham"


def test_resolve_model_team_folder_returns_unresolved_when_no_folder_exists(tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    result = resolve_model_team_folder("St. Bonaventure", cbb_dir=empty_dir)
    assert not isinstance(result, tuple)
    assert result.reason == "not_found"


def test_resolve_model_team_folder_returns_unresolved_for_unknown_team(cbb_dir):
    result = resolve_model_team_folder("Not A Real Team", cbb_dir=cbb_dir)
    assert not isinstance(result, tuple)
    assert result.reason == "not_found"


def test_resolve_model_team_folder_does_not_guess_via_fuzzy_matching(tmp_path):
    # A near-miss folder name must NOT be silently accepted for a
    # different, resolvable team.
    (tmp_path / "ohio-stat").mkdir()  # deliberately misspelled, close to "ohio-state"
    result = resolve_model_team_folder("ohio-state", cbb_dir=tmp_path)
    assert not isinstance(result, tuple), "must not fuzzy-match a near-miss folder name"


# ---------------------------------------------------------------------------
# Duplicate model keys / conflicting canonical vs. legacy resolution
# ---------------------------------------------------------------------------

def test_resolve_model_team_folder_prefers_canonical_slug_over_duplicate_folders(tmp_path):
    """
    Simulates the real St. Bonaventure situation (3 folders for one team:
    space/hyphen/period variants) using a controlled fixture. The
    canonical path must deterministically prefer the clean sr_slug form
    regardless of which duplicate folders also happen to exist.
    """
    (tmp_path / "st bonaventure").mkdir()
    (tmp_path / "st-bonaventure").mkdir()
    (tmp_path / "st. bonaventure").mkdir()
    identity, slug = resolve_model_team_folder("St. Bonaventure", cbb_dir=tmp_path)
    assert slug == "st-bonaventure"


@pytest.mark.skipif(not os.path.isdir(REAL_CBB_DIR), reason="TeamDump/CBB not present on this machine")
def test_real_teamdump_cbb_has_the_documented_st_bonaventure_duplicate_folders():
    """Documents (does not fix) the real 3-way folder duplication found
    this session -- a genuine data-quality artifact in the actual tree."""
    entries = {name.lower() for name in os.listdir(REAL_CBB_DIR)}
    variants_present = sum(1 for v in ("st bonaventure", "st-bonaventure", "st. bonaventure") if v in entries)
    assert variants_present >= 2, "expected at least 2 of the 3 known St. Bonaventure folder variants"


@pytest.mark.skipif(not os.path.isdir(REAL_CBB_DIR), reason="TeamDump/CBB not present on this machine")
def test_real_teamdump_cbb_has_the_documented_saint_corruption_folders():
    """Documents (does not fix) the real "st"->"saint" ingestion corruption
    bug found this session, e.g. "alabama-saint-hornets" instead of a
    State-university name."""
    entries = os.listdir(REAL_CBB_DIR)
    corrupted = [e for e in entries if "-saint-" in e.lower() or e.lower().endswith("-saint")]
    assert len(corrupted) > 0, "expected at least one known corrupted 'saint' folder in the real tree"
    # And the correctly-named sibling should also exist for at least one case.
    assert "alabama-state" in {e.lower() for e in entries}


# ---------------------------------------------------------------------------
# Compatibility wrappers
# ---------------------------------------------------------------------------

def test_team_name_to_slug_v15_matches_notebook_cell_366_behavior():
    assert team_name_to_slug_v15("St. Bonaventure Bonnies") == "st-bonaventure-bonnies"
    # "&" -> "and" is a literal, non-word-boundary-aware substitution here
    # too (same quirk already documented for normalize_espn_name in the
    # ESPN/Logo batch): "A&M" -> "Aandm", not "A and M".
    assert team_name_to_slug_v15("Texas A&M Aggies") == "texas-aandm-aggies"
    assert team_name_to_slug_v15("Miami (OH) RedHawks") == "miami-redhawks"


def test_resolve_team_slug_v15_compat_returns_first_existing_candidate(cbb_dir):
    assert resolve_team_slug_v15_compat("St. Bonaventure", cbb_dir=cbb_dir) is not None
    result = resolve_team_slug_v15_compat("st-bonaventure", cbb_dir=cbb_dir)
    assert result == "st-bonaventure"


def test_resolve_team_slug_v15_compat_falls_back_to_simple_slug_even_if_nonexistent(tmp_path):
    """
    Documents the ORIGINAL (unmodified) src/ovr_model.py behavior being
    faithfully preserved: when nothing matches, it returns a plausible-
    looking slug anyway, not None/an error -- unlike the new canonical
    resolve_model_team_folder(), which reports unresolved instead.
    """
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    result = resolve_team_slug_v15_compat("Not A Real Team Xyz", cbb_dir=empty_dir)
    assert result == "not-a-real-team-xyz"
    assert not (empty_dir / result).exists()  # the returned slug does NOT actually exist


def test_resolve_team_slug_v15_compat_uses_live_sr_overrides_alias():
    # Confirms MANUAL_SLUG_OVERRIDES here really is slug_utils._SR_OVERRIDES,
    # matching ovr_model.py's own top-of-file import -- not a separate guess.
    from src import slug_utils
    assert slug_utils._SR_OVERRIDES.get("TCU") == "texas-christian"


# ---------------------------------------------------------------------------
# Full Division I coverage (as far as the canonical registry supports)
# ---------------------------------------------------------------------------

def test_full_d1_universe_resolves_via_model_identity():
    from src import slug_utils
    from src.identity.team_identity import ensure_team_registry

    ensure_team_registry()
    failures = []
    for rec in slug_utils.TEAM_REGISTRY.values():
        result = resolve_model_team_identity(rec.display_name)
        if not isinstance(result, TeamIdentity) or result.team_id != rec.team_id:
            failures.append(rec.display_name)
    assert not failures, f"{len(failures)} registered teams failed to resolve via model identity: {failures}"


# ---------------------------------------------------------------------------
# Isolation: model-slug resolution does not mutate other alias tables,
# and does not touch model calculations/predictions/artifacts
# ---------------------------------------------------------------------------

def test_model_resolution_does_not_mutate_slug_utils_tables():
    from src import slug_utils

    before_overrides = dict(slug_utils._SR_OVERRIDES)
    before_aliases = dict(slug_utils._DISPLAY_NAME_ALIASES)
    resolve_model_team_identity("uab")
    resolve_team_slug_v15_compat("St. Bonaventure")
    assert slug_utils._SR_OVERRIDES == before_overrides
    assert slug_utils._DISPLAY_NAME_ALIASES == before_aliases


def test_model_slug_module_does_not_import_ovr_model_module():
    # src/ovr_model.py is a read-only reference for this batch -- this
    # adapter must not import it (that would create a real runtime
    # dependency on a module we were told not to modify or couple to).
    # Checked via the module's actual import statements (AST), not string
    # scanning -- this module's own docstring legitimately *mentions*
    # "ovr_model.py" as documentation, which a naive string search would
    # wrongly flag.
    import ast
    import inspect

    import src.identity.model_slug_identity as mod

    tree = ast.parse(inspect.getsource(mod))
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert not any("ovr_model" in name for name in imported_modules), imported_modules


def test_net_and_odds_and_covers_resolution_still_agree_with_model_resolution():
    from src.identity.covers_identity import resolve_covers_team_identity
    from src.identity.net_ranking_identity import resolve_net_team_identity
    from src.identity.odds_identity import resolve_odds_team_identity

    model = resolve_model_team_identity("st-bonaventure")
    net = resolve_net_team_identity("st-bonaventure")
    odds = resolve_odds_team_identity("St. Bonaventure Bonnies")
    covers = resolve_covers_team_identity("st-bonaventure-bonnies")
    ids = {model.team_id, net.team_id, odds.team_id, covers.team_id}
    assert len(ids) == 1
