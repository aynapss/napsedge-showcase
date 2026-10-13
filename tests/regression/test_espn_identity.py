"""
Regression tests for src/identity/espn_identity.py (Phase 2A, adapter batch 1).

resolve_espn_team_identity() mirrors notebook cell 82's token-shortening
algorithm but routes through the full 365-team canonical registry instead
of the notebook's small hand-maintained TEAM_SLUG_MAP/TEAM_NAME_RESOLVER
dicts (~15 and ~6 entries respectively, A-10-only). These tests use
realistic "<school> <mascot>" strings as ESPN's team-list page would
actually produce them.
"""
import pytest

from src.identity.contracts import TeamIdentity
from src.identity.espn_identity import (
    normalize_espn_name,
    resolve_espn_team_identity,
    resolve_espn_team_slug,
)

ESPN_STYLE_NAMES = [
    ("St. Bonaventure Bonnies", "St. Bonaventure"),
    ("VCU Rams", "Virginia Commonwealth"),
    ("Saint Joseph's Hawks", "Saint Joseph's"),
    ("La Salle Explorers", "La Salle"),
    ("George Washington Revolutionaries", "George Washington"),
    ("Rhode Island Rams", "Rhode Island"),
]


@pytest.mark.parametrize("espn_name,expected_display_name", ESPN_STYLE_NAMES)
def test_espn_style_school_plus_mascot_resolves_to_correct_program(espn_name, expected_display_name):
    result = resolve_espn_team_identity(espn_name)
    assert isinstance(result, TeamIdentity), f"{espn_name!r} should resolve, got {result}"
    assert result.display_name == expected_display_name


@pytest.mark.parametrize("espn_name,expected_display_name", [
    ("Miami (FL) Hurricanes", "Miami (FL)"),
    ("Miami (OH) RedHawks", "Miami (OH)"),
    ("Loyola Chicago Ramblers", "Loyola Chicago"),
    ("Loyola Maryland Greyhounds", "Loyola (MD)"),
])
def test_espn_names_that_include_the_disambiguator_resolve_correctly(espn_name, expected_display_name):
    """
    The whole point of routing through the canonical registry: ESPN's own
    names for these programs include the disambiguating detail (FL/OH,
    Chicago/Maryland), so the *longest* candidate the token-shortening loop
    tries should already resolve cleanly -- it should never need to shorten
    all the way down into the "Miami"/"Loyola" ambiguous zone.
    """
    result = resolve_espn_team_identity(espn_name)
    assert isinstance(result, TeamIdentity), f"{espn_name!r} should resolve, got {result}"
    assert result.display_name == expected_display_name


def test_ambiguous_shortening_stops_at_ambiguity_instead_of_over_shortening():
    # Synthetic: no real mascot for bare "Miami", so this exercises the
    # stopping condition directly -- shortening further than "Miami" can't
    # resolve any better, so the loop must not try to.
    result = resolve_espn_team_identity("Miami Somemascot")
    assert not isinstance(result, TeamIdentity)
    assert result.reason == "ambiguous"
    assert {c.display_name for c in result.candidates} == {"Miami (OH)", "Miami (FL)"}


def test_unknown_espn_team_returns_unresolved_after_exhausting_all_token_lengths():
    result = resolve_espn_team_identity("Not A Real Team Xyz")
    assert not isinstance(result, TeamIdentity)
    assert result.reason == "not_found"


def test_resolve_espn_team_slug_compatibility_wrapper_returns_sr_slug_string():
    assert resolve_espn_team_slug("St. Bonaventure Bonnies") == "st-bonaventure"
    assert resolve_espn_team_slug("VCU Rams") == "virginia-commonwealth"


def test_resolve_espn_team_slug_returns_none_for_unresolved_and_ambiguous():
    assert resolve_espn_team_slug("Not A Real Team Xyz") is None
    assert resolve_espn_team_slug("Miami Somemascot") is None


def test_normalize_espn_name_is_verbatim_notebook_cell_77():
    # Locks in the exact, unshadowed cell-77 behavior: NFKD unicode
    # normalize, lowercase, "&"->"and", curly apostrophe folded to straight,
    # strip anything outside [\w\s()'-].
    # Note: "&" -> "and" is a literal character substitution in the
    # notebook, not word-boundary-aware, so "A&M" -> "AandM" verbatim --
    # a pre-existing notebook quirk, faithfully preserved here, not a bug
    # introduced by this port.
    assert normalize_espn_name("Texas A&M Aggies") == "texas aandm aggies"
    assert normalize_espn_name("Saint Joseph’s") == "saint joseph's"
    assert normalize_espn_name("") == ""
