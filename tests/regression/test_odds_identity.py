"""
Regression tests for src/identity/odds_identity.py (Phase 2A, odds/sportsbook adapter).

No live odds requests anywhere -- all `odds_data` fixtures here are small,
hand-built dicts shaped like the notebook's own `find_game_odds` input
(a list of ``{"home_team": ..., "away_team": ...}`` dicts), using team-name
strings taken from the notebook's own test cells (461, 463) and its
`_FIND_GAME_ALIASES`/`ODDS_TEAM_ALIASES` tables.
"""
import pytest

from src.identity.contracts import TeamIdentity
from src.identity.odds_identity import (
    ODDS_ALIAS_LOOKUP,
    ODDS_TEAM_ALIASES,
    canonical_odds_team_v15,
    find_game_odds_via_registry,
    find_game_odds_v15_verbatim,
    resolve_odds_team_identity,
)


# ---------------------------------------------------------------------------
# Exact canonical names, punctuation, apostrophes, Saint/St., directional
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query,expected_display_name", [
    ("St. Bonaventure", "St. Bonaventure"),
    ("St Bonaventure", "St. Bonaventure"),
    ("Bonnies", "St. Bonaventure"),
    ("St. Bonaventure Bonnies", "St. Bonaventure"),  # with mascot, as odds feeds send it
    ("Saint Joseph's", "Saint Joseph's"),
    ("Saint Joseph's Hawks", "Saint Joseph's"),
    ("Saint Louis", "Saint Louis"),
    ("Saint Louis Billikens", "Saint Louis"),
    ("UNC Wilmington", "UNC Wilmington"),  # directional abbreviation
])
def test_exact_names_and_common_variants_resolve(query, expected_display_name):
    result = resolve_odds_team_identity(query)
    assert isinstance(result, TeamIdentity), f"{query!r} should resolve, got {result}"
    assert result.display_name == expected_display_name


@pytest.mark.parametrize("query,expected_display_name", [
    ("UAB", "Alabama-Birmingham"),
    ("UAB Blazers", "Alabama-Birmingham"),
    ("VCU", "Virginia Commonwealth"),
    ("VCU Rams", "Virginia Commonwealth"),
    ("SMU", "Southern Methodist"),
    ("TCU", "Texas Christian"),
    ("TCU Horned Frogs", "Texas Christian"),
    ("NC State", "North Carolina State"),
])
def test_sportsbook_abbreviations_resolve(query, expected_display_name):
    result = resolve_odds_team_identity(query)
    assert isinstance(result, TeamIdentity), f"{query!r} should resolve, got {result}"
    assert result.display_name == expected_display_name


def test_ampersand_punctuation_and_hyphen_variants():
    a = resolve_odds_team_identity("Texas A&M Aggies")
    b = resolve_odds_team_identity("Texas AM")
    assert isinstance(a, TeamIdentity) and isinstance(b, TeamIdentity)
    assert a.team_id == b.team_id == resolve_odds_team_identity("Texas A&M").team_id


# ---------------------------------------------------------------------------
# State vs. St. distinction (instruction #8: no global state->st collapse)
# ---------------------------------------------------------------------------

def test_state_school_and_saint_school_are_not_conflated():
    saint_louis = resolve_odds_team_identity("Saint Louis")
    assert isinstance(saint_louis, TeamIdentity)
    assert saint_louis.display_name == "Saint Louis"
    # A real state school, resolved independently, must not collapse onto
    # anything Saint-prefixed.
    michigan_state = resolve_odds_team_identity("Michigan State Spartans")
    assert isinstance(michigan_state, TeamIdentity)
    assert michigan_state.display_name == "Michigan State"
    assert saint_louis.team_id != michigan_state.team_id


def test_general_canonicalizer_does_not_globally_convert_state_to_st():
    from src import slug_utils
    # canonical_team_slug/normalize_team_name (used by the general canonical
    # path every adapter shares) must still preserve "state" -- confirms
    # this adapter hasn't regressed that guarantee from earlier batches.
    assert "state" in slug_utils.canonical_team_slug("Michigan State")
    assert "st" not in slug_utils.canonical_team_slug("Michigan State").split("-")[1:2]


def test_odds_specific_state_abbreviations_are_isolated_to_this_module():
    # ODDS_TEAM_ALIASES' "utah st"->"utah-state" style entries exist ONLY in
    # this module -- they must not have leaked into the general registry.
    from src import slug_utils
    assert "utah st" not in slug_utils._DISPLAY_NAME_ALIASES
    assert "utah-state" in ODDS_TEAM_ALIASES
    assert "utah st" in ODDS_TEAM_ALIASES["utah-state"]


# ---------------------------------------------------------------------------
# Miami vs Miami (OH), Loyola ambiguity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query,expected_display_name", [
    ("Miami (FL) Hurricanes", "Miami (FL)"),
    ("Miami (OH) RedHawks", "Miami (OH)"),
    ("Loyola Chicago Ramblers", "Loyola Chicago"),
    ("Loyola (MD) Greyhounds", "Loyola (MD)"),
])
def test_miami_and_loyola_disambiguated_forms_resolve_correctly(query, expected_display_name):
    result = resolve_odds_team_identity(query)
    assert isinstance(result, TeamIdentity)
    assert result.display_name == expected_display_name


@pytest.mark.parametrize("query", ["Miami Hurricanes", "Miami", "Loyola"])
def test_bare_ambiguous_odds_names_are_not_silently_resolved(query):
    result = resolve_odds_team_identity(query)
    assert not isinstance(result, TeamIdentity), f"{query!r} should be ambiguous, got {result}"
    assert result.reason == "ambiguous"


# ---------------------------------------------------------------------------
# Unknown teams / repeated determinism
# ---------------------------------------------------------------------------

def test_unknown_odds_team_is_unresolved():
    result = resolve_odds_team_identity("Not A Real Team Xyz")
    assert not isinstance(result, TeamIdentity)
    assert result.reason == "not_found"


def test_repeated_resolution_is_deterministic():
    ids = {resolve_odds_team_identity("VCU Rams").team_id for _ in range(3)}
    assert len(ids) == 1


# ---------------------------------------------------------------------------
# find_game_odds_via_registry: reversed order, unknowns, missing entries
# ---------------------------------------------------------------------------

ODDS_DATA_FIXTURE = [
    {"home_team": "St. Bonaventure Bonnies", "away_team": "VCU Rams"},
    {"home_team": "Miami (OH) RedHawks", "away_team": "Toledo Rockets"},
    {"home_team": "Saint Joseph's Hawks", "away_team": "La Salle Explorers"},
]


def test_find_game_odds_via_registry_matches_regardless_of_query_order():
    forward = find_game_odds_via_registry("VCU", "St. Bonaventure", ODDS_DATA_FIXTURE)
    reversed_order = find_game_odds_via_registry("St. Bonaventure", "VCU", ODDS_DATA_FIXTURE)
    assert forward == reversed_order
    assert forward["away_team"] == "VCU Rams"


def test_find_game_odds_via_registry_disambiguates_miami_oh_from_the_slate():
    game = find_game_odds_via_registry("Toledo", "Miami (OH)", ODDS_DATA_FIXTURE)
    assert game is not None
    assert game["home_team"] == "Miami (OH) RedHawks"


def test_find_game_odds_via_registry_returns_none_for_teams_not_on_the_slate():
    assert find_game_odds_via_registry("Duke", "Kansas", ODDS_DATA_FIXTURE) is None


def test_find_game_odds_via_registry_returns_none_for_unresolvable_query_team():
    assert find_game_odds_via_registry("Not A Real Team", "VCU", ODDS_DATA_FIXTURE) is None


def test_find_game_odds_via_registry_handles_empty_odds_data():
    assert find_game_odds_via_registry("VCU", "St. Bonaventure", []) is None


def test_find_game_odds_via_registry_resolves_apostrophe_and_directional_teams():
    game = find_game_odds_via_registry("Saint Joseph's", "La Salle", ODDS_DATA_FIXTURE)
    assert game is not None
    assert game["home_team"] == "Saint Joseph's Hawks"


# ---------------------------------------------------------------------------
# Compatibility wrapper: canonical_odds_team_v15 + find_game_odds_v15_verbatim
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query,expected_slug", [
    ("VCU Rams", "virginia-commonwealth"),
    ("Dayton Flyers", "dayton"),
    ("UConn Huskies", "connecticut"),
    ("Ole Miss Rebels", "mississippi"),
    ("Miami (OH) RedHawks", "miami-oh"),
    ("UCF Knights", "central-florida"),
    ("St. John's Red Storm", "st-johns-ny"),
])
def test_canonical_odds_team_v15_matches_notebook_cell_461_self_test(query, expected_slug):
    """These are the notebook's OWN test_cases from cell 461 -- 7 of its
    10 original expected values still match today; the other 3
    (Tenn-Martin, SE Missouri St, Murray St) don't, because
    slug_utils._DISPLAY_NAME_ALIASES (TEAM_NAME_CORRECTIONS at runtime) has
    drifted since that cell's expectations were written -- see
    docs/migration_state.md for the full writeup. Not a bug in this port."""
    assert canonical_odds_team_v15(query) == expected_slug


def test_canonical_odds_team_v15_cell_461_stale_expectations_are_documented_not_hidden():
    # Confirms the drift is real and reproducible, not something this port
    # papers over.
    assert canonical_odds_team_v15("Murray St Racers") != "murray-state"
    # ...but the NEW canonical path (which has an isolated, additive fix)
    # gets it right anyway:
    result = resolve_odds_team_identity("Murray St Racers")
    assert isinstance(result, TeamIdentity)
    assert result.display_name == "Murray State"


def test_find_game_odds_v15_verbatim_returns_same_dict_contract():
    game = find_game_odds_v15_verbatim("VCU", "St. Bonaventure", ODDS_DATA_FIXTURE)
    assert isinstance(game, dict)
    assert game["away_team"] == "VCU Rams"


def test_find_game_odds_v15_verbatim_returns_none_for_no_match():
    assert find_game_odds_v15_verbatim("Duke", "Kansas", []) is None


# ---------------------------------------------------------------------------
# Confirmation: odds normalization does not leak into other adapters
# ---------------------------------------------------------------------------

def test_odds_normalization_does_not_affect_espn_adapter():
    from src.identity.espn_identity import resolve_espn_team_identity
    result = resolve_espn_team_identity("St. Bonaventure Bonnies")
    assert isinstance(result, TeamIdentity)
    assert result.display_name == "St. Bonaventure"


def test_odds_normalization_does_not_affect_covers_adapter():
    from src.identity.covers_identity import resolve_covers_team_identity
    result = resolve_covers_team_identity("st-bonaventure-bonnies")
    assert isinstance(result, TeamIdentity)
    assert result.display_name == "St. Bonaventure"


def test_odds_normalization_does_not_affect_general_canonical_resolution():
    from src.identity.team_identity import resolve_team_identity
    result = resolve_team_identity("St. Bonaventure")
    assert isinstance(result, TeamIdentity)
    assert result.display_name == "St. Bonaventure"


def test_odds_alias_lookup_is_correctly_flattened_from_odds_team_aliases():
    for canonical, aliases in ODDS_TEAM_ALIASES.items():
        for alias in aliases:
            key = alias.replace(" ", "-")
            assert ODDS_ALIAS_LOOKUP.get(key) == canonical
