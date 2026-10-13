"""
Phase 2, Gate 2: confirms the five new stable-ID types did not change any
existing team_id or player_id value or algorithm.
"""
from src.identity.player_identity import build_player_identity
from src.identity.team_identity import resolve_team_identity
from src.identity.contracts import TeamIdentity


def test_existing_team_id_for_known_team_unchanged():
    result = resolve_team_identity("St. Bonaventure")
    assert isinstance(result, TeamIdentity)
    assert result.team_id == 905884  # confirmed value used throughout this migration since Phase 2A


def test_existing_player_id_construction_unchanged():
    identity = build_player_identity("Frank Mitchell", 905884, 2026)
    assert identity.player_id == "team:905884:season:2026:key:frank mitchell"


def test_stable_team_id_function_itself_untouched():
    from src import slug_utils

    assert slug_utils.stable_team_id("st-bonaventure") == 905884
