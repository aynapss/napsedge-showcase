"""Phase 2, Gate 2 tests: conference identity. No live requests."""
import pytest

from src.identity.contracts import ConferenceIdentity, UnresolvedIdentity
from src.identity.conference_identity import (
    find_conference_alias_collisions,
    resolve_conference_identity,
)


def test_resolve_conference_identity_exact_canonical_name():
    result = resolve_conference_identity("Big Ten")
    assert isinstance(result, ConferenceIdentity)
    assert result.canonical_name == "Big Ten"


def test_resolve_conference_identity_alias():
    result = resolve_conference_identity("b1g")
    assert isinstance(result, ConferenceIdentity)
    assert result.canonical_name == "Big Ten"


def test_resolve_conference_identity_alias_case_insensitive():
    a = resolve_conference_identity("B1G")
    b = resolve_conference_identity("b1g")
    assert a.conference_id == b.conference_id


def test_independent_conferences_get_distinct_ids():
    a = resolve_conference_identity("Big Ten")
    b = resolve_conference_identity("SEC")
    assert a.conference_id != b.conference_id


def test_unknown_conference_is_not_found():
    result = resolve_conference_identity("Not A Real Conference")
    assert not isinstance(result, ConferenceIdentity)
    assert result.reason == "not_found"


def test_empty_conference_is_not_found():
    result = resolve_conference_identity("")
    assert not isinstance(result, ConferenceIdentity)
    assert result.reason == "not_found"


def test_deterministic_repeated_resolution():
    ids = {resolve_conference_identity("Atlantic 10").conference_id for _ in range(3)}
    assert len(ids) == 1


def test_all_alias_spellings_of_one_conference_share_one_id():
    a10_aliases = ["a-10", "a10", "atlantic-10", "atlantic ten", "atlantic 10 conference"]
    ids = {resolve_conference_identity(alias).conference_id for alias in a10_aliases}
    assert len(ids) == 1


def test_renamed_conference_alias_and_current_name_share_one_id():
    """
    "Coastal Athletic Association" (CAA's renamed form) and "Colonial
    Athletic Association" (its prior name) both alias to the same
    canonical "CAA" -- a display-name/rename must not create two ids.
    """
    old = resolve_conference_identity("Colonial Athletic Association")
    new = resolve_conference_identity("Coastal Athletic Association")
    assert isinstance(old, ConferenceIdentity) and isinstance(new, ConferenceIdentity)
    assert old.conference_id == new.conference_id


def test_legacy_alias_not_in_current_universe_still_resolves_by_alias():
    """
    A synthetic 'legacy' conference name not in the current 33-conference
    universe still resolves consistently via its own canonical form,
    rather than being silently dropped -- conference identity persists
    independent of current-membership bookkeeping.
    """
    from src.identity import conference_identity as mod

    mod.CONFERENCE_ALIASES_V15["defunct legacy conf"] = "Defunct Legacy Conference"
    try:
        a = resolve_conference_identity("defunct legacy conf")
        b = resolve_conference_identity("defunct legacy conf")
        assert isinstance(a, ConferenceIdentity)
        assert a.conference_id == b.conference_id
    finally:
        del mod.CONFERENCE_ALIASES_V15["defunct legacy conf"]


def test_conference_identity_does_not_encode_team_membership():
    """ConferenceIdentity carries no team list/roster at all."""
    result = resolve_conference_identity("Big Ten")
    assert not hasattr(result, "teams")
    assert not hasattr(result, "members")


def test_find_conference_alias_collisions_clean_by_default():
    assert find_conference_alias_collisions() == {}


def test_no_fuzzy_matching_in_module():
    import ast
    import inspect

    import src.identity.conference_identity as mod

    tree = ast.parse(inspect.getsource(mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert "difflib" not in imported
