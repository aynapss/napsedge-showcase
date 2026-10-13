"""
Phase 3 pre-batch identity correction 1: meaningful-token shortening guard.
No live requests.
"""
import pytest

from src.identity.contracts import TeamIdentity
from src.identity.team_identity import (
    ensure_team_registry,
    resolve_team_identity,
    resolve_team_identity_by_shortening,
)
from src.identity.team_shortening_guard import (
    find_unproven_dropped_tokens,
    find_unproven_shortened_tokens,
    is_shortened_match_proven_safe,
)
from src import slug_utils


# ---------------------------------------------------------------------------
# The two confirmed defects: must no longer resolve to the wrong D1 team
# ---------------------------------------------------------------------------

def test_colorado_college_tigers_no_longer_collides_with_colorado():
    for fn in (resolve_team_identity, resolve_team_identity_by_shortening):
        result = fn("Colorado College Tigers")
        assert not isinstance(result, TeamIdentity), f"{fn.__name__} incorrectly resolved: {result}"


def test_washington_mo_bears_no_longer_collides_with_washington():
    for fn in (resolve_team_identity, resolve_team_identity_by_shortening):
        result = fn("Washington (MO) Bears")
        assert not isinstance(result, TeamIdentity), f"{fn.__name__} incorrectly resolved: {result}"


def test_bare_colorado_and_washington_still_resolve_correctly():
    """The real Colorado/Washington programs are unaffected by the guard."""
    colorado = resolve_team_identity("Colorado")
    washington = resolve_team_identity("Washington")
    assert isinstance(colorado, TeamIdentity) and colorado.display_name == "Colorado"
    assert isinstance(washington, TeamIdentity) and washington.display_name == "Washington"


# ---------------------------------------------------------------------------
# Valid D1 shortening behavior must be unchanged
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query,expected_display", [
    ("Duke Blue Devils", "Duke"),
    ("Miami (FL) Hurricanes", "Miami (FL)"),
    ("St. Bonaventure Bonnies", "St. Bonaventure"),
    ("Notre Dame Fighting Irish", "Notre Dame"),
    ("Texas A&M Aggies", "Texas A&M"),
])
def test_valid_d1_mascot_shortening_unaffected(query, expected_display):
    result = resolve_team_identity_by_shortening(query)
    assert isinstance(result, TeamIdentity), f"regression: {query!r} no longer resolves ({result})"
    assert result.display_name == expected_display


def test_boston_college_exact_alias_unaffected_by_college_guard():
    """A real "College" school resolves via exact alias -- untouched by the step-3/shortening guard."""
    result = resolve_team_identity("Boston College")
    assert isinstance(result, TeamIdentity)
    assert result.display_name == "Boston College"


def test_college_of_charleston_remains_unresolved_not_worse_not_better():
    """Already-unresolved before this correction; must not crash or newly resolve incorrectly."""
    result = resolve_team_identity_by_shortening("College of Charleston Cougars")
    assert not isinstance(result, TeamIdentity)


# ---------------------------------------------------------------------------
# Guard unit behavior
# ---------------------------------------------------------------------------

def test_is_shortened_match_proven_safe_true_for_mascot_only_loss():
    ensure_team_registry()
    duke_id = slug_utils._ALIAS_TO_ID["duke"]
    assert is_shortened_match_proven_safe("Duke Blue Devils", duke_id) is True


def test_find_unproven_shortened_tokens_flags_college():
    ensure_team_registry()
    colorado_id = slug_utils._ALIAS_TO_ID["colorado"]
    assert find_unproven_shortened_tokens("Colorado College Tigers", colorado_id) == {"college"}


def test_find_unproven_shortened_tokens_flags_parenthetical_content():
    ensure_team_registry()
    washington_id = slug_utils._ALIAS_TO_ID["washington"]
    assert find_unproven_shortened_tokens("Washington (MO) Bears", washington_id) == {"mo"}


def test_find_unproven_dropped_tokens_flags_state_word():
    ensure_team_registry()
    fake_target_id = slug_utils._ALIAS_TO_ID["duke"]
    assert find_unproven_dropped_tokens(["State", "Wolves"], fake_target_id) == {"state"}


def test_find_unproven_dropped_tokens_empty_for_ordinary_mascots():
    ensure_team_registry()
    duke_id = slug_utils._ALIAS_TO_ID["duke"]
    assert find_unproven_dropped_tokens(["Blue", "Devils"], duke_id) == set()


def test_meaningful_token_echoed_in_target_name_is_not_flagged():
    """"College" dropped but the MATCHED team's own name also contains "College" -- safe."""
    ensure_team_registry()
    bc_id = slug_utils._ALIAS_TO_ID["boston-college"] if "boston-college" in slug_utils._ALIAS_TO_ID else None
    if bc_id is None:
        pytest.skip("Boston College slug not present in this registry snapshot")
    assert find_unproven_dropped_tokens(["College"], bc_id) == set()


# ---------------------------------------------------------------------------
# Full D1-universe collision sweep (the explicitly requested broad test)
# ---------------------------------------------------------------------------

_GENERIC_MASCOTS = ["Tigers", "Bears", "Eagles", "Wolves", "Knights", "Bulldogs"]


def test_full_d1_universe_display_name_plus_generic_mascot_resolves_to_itself():
    """
    For every real D1 team's own display name, appending an arbitrary
    generic mascot word (not necessarily *its* real mascot) must still
    resolve back to that exact team, never a different one, and never
    incorrectly to `not_found` for the plain display name itself.
    """
    ensure_team_registry()
    failures = []
    for team_id, rec in slug_utils.TEAM_REGISTRY.items():
        for mascot in _GENERIC_MASCOTS:
            query = f"{rec.display_name} {mascot}"
            result = resolve_team_identity_by_shortening(query)
            if isinstance(result, TeamIdentity):
                if result.team_id != team_id:
                    failures.append((query, team_id, result.team_id))
            # not_found is acceptable (display name + generic mascot may not
            # always cleanly re-shorten, e.g. multi-word names with their
            # own parenthetical) -- only a WRONG team_id is a real failure.
    assert failures == [], f"{len(failures)} cross-team collisions found: {failures[:10]}"


def test_full_d1_universe_bare_display_names_still_resolve_to_themselves():
    """Every real D1 team's own bare display name must still resolve to itself -- no regression from the guard."""
    ensure_team_registry()
    failures = []
    for team_id, rec in slug_utils.TEAM_REGISTRY.items():
        result = resolve_team_identity(rec.display_name)
        if not isinstance(result, TeamIdentity) or result.team_id != team_id:
            failures.append((rec.display_name, team_id, result))
    assert failures == [], f"{len(failures)} regressions found: {failures[:10]}"
