"""Phase 2, Gate 2 tests: sportsbook identity. No live requests."""
from src.identity.contracts import SportsbookIdentity, UnresolvedIdentity
from src.identity.sportsbook_identity import (
    find_sportsbook_id_collisions,
    resolve_sportsbook_identity,
)


def test_resolve_sportsbook_identity_canonical_key():
    result = resolve_sportsbook_identity("fanduel")
    assert isinstance(result, SportsbookIdentity)
    assert result.canonical_key == "fanduel"
    assert result.display_name == "FanDuel"


def test_resolve_sportsbook_identity_display_name():
    result = resolve_sportsbook_identity("FanDuel")
    assert isinstance(result, SportsbookIdentity)
    assert result.canonical_key == "fanduel"


def test_capitalization_variants_share_one_id():
    a = resolve_sportsbook_identity("FANDUEL")
    b = resolve_sportsbook_identity("fanduel")
    c = resolve_sportsbook_identity("FanDuel")
    assert a.sportsbook_id == b.sportsbook_id == c.sportsbook_id


def test_punctuation_variants_share_one_id():
    a = resolve_sportsbook_identity("Fan Duel")
    b = resolve_sportsbook_identity("fanduel")
    assert isinstance(a, SportsbookIdentity)
    assert a.sportsbook_id == b.sportsbook_id


def test_display_name_change_does_not_change_id():
    """A hypothetical rebrand of the display name (not the API key) leaves the id unchanged."""
    from src.identity import sportsbook_identity as mod

    original_display = mod.SPORTSBOOK_REGISTRY["fanduel"]
    before = resolve_sportsbook_identity("fanduel")
    mod.SPORTSBOOK_REGISTRY["fanduel"] = "FanDuel Sportsbook (Rebranded)"
    try:
        after = resolve_sportsbook_identity("fanduel")
        assert before.sportsbook_id == after.sportsbook_id
        assert after.display_name == "FanDuel Sportsbook (Rebranded)"
    finally:
        mod.SPORTSBOOK_REGISTRY["fanduel"] = original_display


def test_unknown_provider_is_not_found():
    result = resolve_sportsbook_identity("SomeSportsbookThatDoesNotExist")
    assert not isinstance(result, SportsbookIdentity)
    assert result.reason == "not_found"


def test_does_not_infer_one_brand_from_another():
    """'Draft Kings'-style brands are not registered; must not silently map to FanDuel or anything else."""
    result = resolve_sportsbook_identity("DraftKings")
    assert not isinstance(result, SportsbookIdentity)
    assert result.reason == "not_found"


def test_empty_query_is_not_found():
    result = resolve_sportsbook_identity("")
    assert not isinstance(result, SportsbookIdentity)


def test_deterministic_repeated_resolution():
    ids = {resolve_sportsbook_identity("fanduel").sportsbook_id for _ in range(3)}
    assert len(ids) == 1


def test_find_sportsbook_id_collisions_groups_known_aliases():
    collisions = find_sportsbook_id_collisions(["fanduel", "FanDuel", "FANDUEL", "unknownbook"])
    assert len(collisions) == 1
    (aliases,) = collisions.values()
    assert set(aliases) == {"fanduel", "FanDuel", "FANDUEL"}


def test_find_sportsbook_id_collisions_excludes_unknown():
    collisions = find_sportsbook_id_collisions(["unknownbook1", "unknownbook2"])
    assert collisions == {}


def test_odds_calculations_not_touched_by_this_module():
    """This module exposes no odds/spread/total computation of any kind."""
    import src.identity.sportsbook_identity as mod

    forbidden = {"spread", "total", "odds_ratio", "compute_edge", "margin"}
    assert not (forbidden & set(dir(mod)))
