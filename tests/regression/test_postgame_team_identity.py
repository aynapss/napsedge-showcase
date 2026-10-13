"""
Phase 2A targeted re-review tests: postgame `_resolve_slug_from_name`
(Phase 2B batch 6 companion). No live requests -- uses the real, already
locally cached `cache/sportsref_cbb_schools.parquet` fixture (the same one
used throughout Phase 2A) plus small synthetic frames for edge cases.
"""
from pathlib import Path

import pandas as pd
import pytest

from src.identity.contracts import TeamIdentity, UnresolvedIdentity
from src.identity.postgame_team_identity import (
    find_slug_resolution_divergence,
    resolve_postgame_team_identity,
    resolve_slug_from_name_v15,
)

SCHOOLS_FIXTURE_PATH = Path("cache/sportsref_cbb_schools.parquet")


@pytest.fixture(scope="module")
def all_schools_df():
    if not SCHOOLS_FIXTURE_PATH.exists():
        pytest.skip("real local Sports-Reference schools fixture cache/sportsref_cbb_schools.parquet not present in this checkout")
    return pd.read_parquet(SCHOOLS_FIXTURE_PATH)


# ---------------------------------------------------------------------------
# v15 compat wrapper: exact behavior preserved, including the fuzzy risk
# ---------------------------------------------------------------------------

def test_resolve_slug_from_name_v15_exact_match(all_schools_df):
    assert resolve_slug_from_name_v15("Duke", all_schools_df=all_schools_df) == "duke"


def test_resolve_slug_from_name_v15_case_insensitive_exact_match(all_schools_df):
    assert resolve_slug_from_name_v15("duke", all_schools_df=all_schools_df) == "duke"


def test_resolve_slug_from_name_v15_manual_override_takes_priority(all_schools_df):
    result = resolve_slug_from_name_v15(
        "Duke", all_schools_df=all_schools_df, manual_slug_overrides={"Duke": "custom-override-slug"}
    )
    assert result == "custom-override-slug"


def test_resolve_slug_from_name_v15_disambiguated_miami_variants_are_exact(all_schools_df):
    assert resolve_slug_from_name_v15("Miami (FL)", all_schools_df=all_schools_df) == "miami-fl"
    assert resolve_slug_from_name_v15("Miami (OH)", all_schools_df=all_schools_df) == "miami-oh"


def test_resolve_slug_from_name_v15_bare_miami_returns_none_below_fuzzy_cutoff():
    """
    Checked empirically, not assumed: SequenceMatcher("miami", "miami (fl)")
    scores 0.667, below the 0.72 cutoff, so pass 3 finds nothing for a bare
    "Miami" query. v15 returns a bare None here -- indistinguishable from
    "genuinely unknown school" -- see test_resolve_postgame_team_identity_
    bare_miami_is_correctly_ambiguous for the canonical adapter's more
    informative structured result on the same input.
    """
    df = pd.DataFrame({"team_name": ["Miami (FL)", "Miami (OH)"], "team_slug": ["miami-fl", "miami-oh"]})
    assert resolve_slug_from_name_v15("Miami", all_schools_df=df) is None


def test_resolve_slug_from_name_v15_unknown_name_with_no_close_match_returns_none(all_schools_df):
    assert resolve_slug_from_name_v15("Definitely Not A Real School Xyzzy123", all_schools_df=all_schools_df) is None


def test_resolve_slug_from_name_v15_close_but_incorrect_name_still_fuzzy_matches(all_schools_df):
    # A one-character typo is well within cutoff=0.72 and resolves to the real school -- silently.
    result = resolve_slug_from_name_v15("Dukr", all_schools_df=all_schools_df)
    assert result == "duke"


# ---------------------------------------------------------------------------
# Canonical target-path adapter
# ---------------------------------------------------------------------------

def test_resolve_postgame_team_identity_resolves_exact_names():
    result = resolve_postgame_team_identity("Duke")
    assert isinstance(result, TeamIdentity)
    assert result.sources["sports_reference"].raw_value == "duke"


def test_resolve_postgame_team_identity_resolves_disambiguated_miami():
    fl = resolve_postgame_team_identity("Miami (FL)")
    oh = resolve_postgame_team_identity("Miami (OH)")
    assert isinstance(fl, TeamIdentity) and isinstance(oh, TeamIdentity)
    assert fl.team_id != oh.team_id


def test_resolve_postgame_team_identity_bare_miami_is_correctly_ambiguous():
    result = resolve_postgame_team_identity("Miami")
    assert not isinstance(result, TeamIdentity)
    assert result.reason == "ambiguous"


def test_resolve_postgame_team_identity_bare_loyola_is_correctly_ambiguous_or_unresolved():
    """
    Loyola has 4 real disambiguated programs in the fixture; the canonical
    registry must never silently collapse this to one, whatever reason it
    reports for the bare query.
    """
    result = resolve_postgame_team_identity("Loyola")
    assert not isinstance(result, TeamIdentity)
    assert result.reason in {"ambiguous", "not_found"}


def test_resolve_postgame_team_identity_unknown_name_is_not_found():
    result = resolve_postgame_team_identity("Definitely Not A Real School Xyzzy123")
    assert not isinstance(result, TeamIdentity)
    assert result.reason == "not_found"


def test_resolve_postgame_team_identity_close_but_incorrect_name_is_not_silently_resolved():
    """
    The canonical adapter must NOT reproduce v15's fuzzy auto-pick for a
    typo'd name -- this is the exact behavior this batch replaces.
    """
    result = resolve_postgame_team_identity("Dukr")
    assert not isinstance(result, TeamIdentity)
    assert result.reason == "not_found"


def test_resolve_postgame_team_identity_deterministic():
    ids = {resolve_postgame_team_identity("Duke").team_id for _ in range(3)}
    assert len(ids) == 1


# ---------------------------------------------------------------------------
# Divergence audit against every real, currently-cached team name
# ---------------------------------------------------------------------------

# Locked-in snapshot of every real, D1-registry-covered team_name where
# all_schools_df's pass-2 slug and the canonical registry's slug disagree
# (found by running the full comparison, not assumed) -- see module
# docstring "THE ACTUAL BLOCKER" section for the analysis. This is the
# unresolved conflict this batch's Phase 2A re-review reports to the user
# rather than silently picking a side.
#
# UPDATED (Phase 3 pre-batch identity correction 1): "Colorado College
# Tigers", "Washington (MO) Bears", and "Brigham Young College" are no
# longer in this set. The meaningful-token shortening guard
# (src/identity/team_shortening_guard.py) fixed the underlying
# query-resolution defect that caused all three to falsely resolve to an
# unrelated D1 team_id (Colorado, Washington, Brigham Young respectively)
# -- they now correctly return `not_found` from `resolve_postgame_team_identity`
# and so fall into `out_of_registry_scope` rather than "diverging on slug
# value" (a divergence requires canonical to resolve to *something* first).
KNOWN_SLUG_DIVERGENCES = {
    "FDU", "Houston Christian",
    "IU Indy", "Little Rock", "NC State", "North Carolina A&T", "Omaha",
    "Sam Houston", "Southern", "St. Thomas", "TCU", "Texas A&M",
    "Texas A&M-Corpus Christi", "The Citadel", "UAB", "UC Davis", "UC Irvine",
    "UC Riverside", "UC San Diego", "UC Santa Barbara", "UCF", "UNC Asheville",
    "UNC Greensboro", "UNC Wilmington", "UT Arlington", "Utah Tech", "UTEP",
    "UTSA", "William & Mary",
}


def test_slug_divergence_for_d1_registry_covered_names_matches_known_snapshot(all_schools_df):
    """
    Confirms objective #9, correctly scoped: `all_schools_df` (519 rows) is
    Sports-Reference's full multi-division school index, while the
    canonical registry is deliberately D1-only (365 teams, per Phase 2A).
    Restricting the comparison to names the canonical registry actually
    resolves (the only names a real D1 boxscore's away/home team could
    ever be) does NOT confirm zero divergence -- it confirms exactly the
    32-name divergence set above, a real and reproducible disagreement
    about the correct SR slug string for otherwise-unambiguous D1
    programs (not an ambiguity, not a missing name). This is asserted as
    a locked-in snapshot rather than "no divergence" so a real change in
    either fixture is caught here rather than silently passing.
    """
    real_names = [n for n in all_schools_df["team_name"].dropna().unique() if n != "School"]

    compared = 0
    diverging_names = set()
    out_of_registry_scope = 0
    for name in real_names:
        canonical_result = resolve_postgame_team_identity(name)
        if not isinstance(canonical_result, TeamIdentity):
            out_of_registry_scope += 1
            continue
        compared += 1
        if find_slug_resolution_divergence(name, all_schools_df=all_schools_df) is not None:
            diverging_names.add(name)

    assert diverging_names == KNOWN_SLUG_DIVERGENCES
    assert compared > 0
    # The 519-vs-365 gap is a real, documented scope difference (see module
    # docstring), not a defect -- assert it exists rather than silently
    # ignoring it, so a future change to either fixture is noticed here.
    assert out_of_registry_scope > 0


def test_texas_am_divergence_matches_documented_ampersand_quirk():
    """
    Ties this batch's finding back to the earlier-documented "&" -> "and"
    substitution-without-word-boundaries quirk: all_schools_df's cached
    slug for Texas A&M reproduces that exact quirk ("texas-aandm"), while
    the canonical registry's hand-curated SR override does not
    ("texas-am") -- strong evidence all_schools_df's team_slug column is,
    for names like this, a locally slugified display name rather than an
    always-authoritative scraped SR slug.
    """
    import pandas as pd

    df = pd.DataFrame({"team_name": ["Texas A&M"], "team_slug": ["texas-aandm"]})
    assert resolve_slug_from_name_v15("Texas A&M", all_schools_df=df) == "texas-aandm"

    canonical = resolve_postgame_team_identity("Texas A&M")
    assert isinstance(canonical, TeamIdentity)
    assert canonical.sources["sports_reference"].raw_value == "texas-am"


def test_bare_ambiguous_query_v15_and_canonical_agree_on_slug_but_not_on_informativeness():
    """
    Both functions decline to resolve a bare "Miami" (v15 via a failed
    fuzzy match, canonical via an explicit ambiguity check), so the
    slug-level comparison finds no divergence (None == None) -- but v15's
    bare None cannot be distinguished from "genuinely unknown school",
    while canonical's structured result names both real candidates. This
    is the actual, verified shape of the gap (see module docstring for the
    corrected finding).
    """
    df = pd.DataFrame({"team_name": ["Miami (FL)", "Miami (OH)"], "team_slug": ["miami-fl", "miami-oh"]})
    assert find_slug_resolution_divergence("Miami", all_schools_df=df) is None  # both fail; no slug-level divergence

    canonical_result = resolve_postgame_team_identity("Miami")
    assert isinstance(canonical_result, UnresolvedIdentity)
    assert canonical_result.reason == "ambiguous"
    assert len(canonical_result.candidates) == 2


def test_no_divergence_when_both_fail_to_resolve(all_schools_df):
    divergence = find_slug_resolution_divergence("Definitely Not A Real School Xyzzy123", all_schools_df=all_schools_df)
    assert divergence is None


# ---------------------------------------------------------------------------
# Compatibility / isolation
# ---------------------------------------------------------------------------

def test_postgame_team_identity_module_does_not_import_ovr_model_or_slug_utils_directly():
    import ast
    import inspect

    import src.identity.postgame_team_identity as mod

    tree = ast.parse(inspect.getsource(mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert not any("ovr_model" in name for name in imported)
    assert "src.slug_utils" not in imported


def test_postgame_team_resolution_does_not_mutate_team_registry():
    from src import slug_utils

    before_size = len(slug_utils.TEAM_REGISTRY)
    before_aliases = dict(slug_utils._DISPLAY_NAME_ALIASES)
    resolve_postgame_team_identity("Duke")
    resolve_postgame_team_identity("Miami")
    assert len(slug_utils.TEAM_REGISTRY) == before_size
    assert slug_utils._DISPLAY_NAME_ALIASES == before_aliases
