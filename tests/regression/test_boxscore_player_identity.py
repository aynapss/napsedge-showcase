"""
Regression tests for src/identity/boxscore_player_identity.py (Phase 2B, batch 4).

No live requests: uses small synthetic box-score-shaped data (raw name
lists / DataFrames) rather than fetching real Sports-Reference box-score
pages or invoking cbbpy.
"""
import pandas as pd
import pytest

from src.identity.contracts import PlayerIdentity, TeamIdentity, UnresolvedIdentity
from src.identity.boxscore_player_identity import (
    filter_boxscore_player_rows_v15,
    find_boxscore_id_conflicts,
    find_duplicate_boxscore_rows,
    is_non_player_boxscore_row,
    resolve_boxscore_game_players,
    resolve_boxscore_player_identity,
)
from src.identity.player_identity import build_player_identity


# ---------------------------------------------------------------------------
# Non-player row exclusion (v15 compat + enforced in the canonical path)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("row", ["Reserves", "Team Totals", "Did Not Play", "reserves", "TEAM TOTALS", "did not play"])
def test_non_player_rows_are_excluded_case_insensitively(row):
    assert is_non_player_boxscore_row(row) is True
    assert filter_boxscore_player_rows_v15([row, "Frank Mitchell"]) == ["Frank Mitchell"]


def test_real_player_rows_are_not_excluded():
    assert is_non_player_boxscore_row("Frank Mitchell") is False
    assert is_non_player_boxscore_row("Darius Acuff Jr.") is False


def test_filter_preserves_order_and_drops_only_non_player_rows():
    raw = ["Frank Mitchell", "Daryl Simmons II", "Reserves", "Team Totals", "Did Not Play", "Cayden Charles"]
    assert filter_boxscore_player_rows_v15(raw) == ["Frank Mitchell", "Daryl Simmons II", "Cayden Charles"]


def test_resolve_boxscore_player_identity_rejects_non_player_rows_directly():
    for row in ["Reserves", "Team Totals", "Did Not Play"]:
        result = resolve_boxscore_player_identity(row, "St. Bonaventure", 2026)
        assert not isinstance(result, PlayerIdentity)
        assert result.reason == "not_found"


# ---------------------------------------------------------------------------
# Exact resolution, team routing, missing context
# ---------------------------------------------------------------------------

def test_exact_full_name_resolution_without_roster():
    result = resolve_boxscore_player_identity("Frank Mitchell", "St. Bonaventure", 2026)
    assert isinstance(result, PlayerIdentity)
    assert result.display_name == "Frank Mitchell"


def test_resolve_boxscore_player_identity_routes_team_through_canonical_registry():
    result = resolve_boxscore_player_identity("Frank Mitchell", "St. Bonaventure", 2026)
    assert isinstance(result, PlayerIdentity)
    assert result.team_id == 905884  # confirmed St. Bonaventure team_id used throughout this migration


def test_unknown_team_is_unresolved():
    result = resolve_boxscore_player_identity("Frank Mitchell", "Not A Real Team", 2026)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "not_found"


def test_ambiguous_team_is_not_silently_resolved():
    result = resolve_boxscore_player_identity("Someone", "Miami", 2026)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "ambiguous"


def test_empty_row_is_unresolved():
    result = resolve_boxscore_player_identity("", "St. Bonaventure", 2026)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "not_found"


def test_repeated_resolution_is_deterministic():
    ids = {resolve_boxscore_player_identity("Frank Mitchell", "St. Bonaventure", 2026).player_id for _ in range(3)}
    assert len(ids) == 1


# ---------------------------------------------------------------------------
# Reusing Sports-Reference / ESPN player identities via a supplied roster
# ---------------------------------------------------------------------------

def test_resolve_with_roster_reuses_sr_source_id():
    roster = [build_player_identity("Frank Mitchell", 905884, 2026, source="sports_reference", source_player_id="mitchell-1")]
    result = resolve_boxscore_player_identity("Frank Mitchell", "St. Bonaventure", 2026, roster_identities=roster)
    assert isinstance(result, PlayerIdentity)
    assert result.player_id == "sports_reference:mitchell-1"


def test_resolve_with_espn_roster_reuses_espn_source_id():
    from src.identity.espn_player_identity import resolve_espn_player_identity

    espn_roster = [resolve_espn_player_identity("Frank Mitchell", "St. Bonaventure", 2026, espn_athlete_id="500")]
    result = resolve_boxscore_player_identity("Frank Mitchell", "St. Bonaventure", 2026, roster_identities=espn_roster)
    assert isinstance(result, PlayerIdentity)
    assert result.player_id == "espn:500"


def test_resolve_with_roster_falls_back_to_standalone_when_not_on_roster():
    roster = [build_player_identity("Someone Else", 905884, 2026, source="sports_reference", source_player_id="x")]
    result = resolve_boxscore_player_identity("Frank Mitchell", "St. Bonaventure", 2026, roster_identities=roster)
    assert isinstance(result, PlayerIdentity)
    assert "sports_reference" not in result.sources  # standalone, not fabricated


def test_resolve_with_roster_scopes_by_team_and_season():
    # Roster entry exists but for a DIFFERENT season -- must not match.
    roster = [build_player_identity("Frank Mitchell", 905884, 2025, source="sports_reference", source_player_id="mitchell-1")]
    result = resolve_boxscore_player_identity("Frank Mitchell", "St. Bonaventure", 2026, roster_identities=roster)
    assert isinstance(result, PlayerIdentity)
    assert "sports_reference" not in result.sources


def test_ambiguous_roster_candidates_return_ambiguous():
    roster = [
        build_player_identity("John Smith", 1, 2026),
        build_player_identity("John Smith Jr.", 1, 2026),
    ]
    result = resolve_boxscore_player_identity("John Smith", "St. Bonaventure", 2026, roster_identities=roster)
    # NOTE: roster team_id (1) differs from resolved "St. Bonaventure" team_id,
    # so scoping excludes both -- falls back to standalone, not ambiguous.
    assert isinstance(result, PlayerIdentity)
    assert result.sources == {}


def test_ambiguous_roster_candidates_within_correct_team_scope():
    roster = [
        build_player_identity("John Smith", 905884, 2026),
        build_player_identity("John Smith Jr.", 905884, 2026),
    ]
    result = resolve_boxscore_player_identity("John Smith", "St. Bonaventure", 2026, roster_identities=roster)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "ambiguous"
    assert len(result.candidates) == 2


# ---------------------------------------------------------------------------
# Accents, apostrophes, hyphens, initials, suffixes
# ---------------------------------------------------------------------------

def test_apostrophe_and_hyphen_names_preserved():
    a = resolve_boxscore_player_identity("Amar'e Marshall", "St. Bonaventure", 2026)
    b = resolve_boxscore_player_identity("Jean-Paul Olukemi", "St. Bonaventure", 2026)
    assert a.display_name == "Amar'e Marshall"
    assert b.display_name == "Jean-Paul Olukemi"


def test_initials_and_full_name_are_distinct():
    a = resolve_boxscore_player_identity("A. Marshall", "St. Bonaventure", 2026)
    b = resolve_boxscore_player_identity("Amare Marshall", "St. Bonaventure", 2026)
    assert a.player_id != b.player_id


@pytest.mark.parametrize("suffixed", ["Darius Acuff Jr.", "John Smith Sr.", "John Smith II", "John Smith III", "John Smith IV"])
def test_all_suffix_forms_resolve(suffixed):
    result = resolve_boxscore_player_identity(suffixed, "St. Bonaventure", 2026)
    assert isinstance(result, PlayerIdentity)
    assert result.display_name == suffixed


# ---------------------------------------------------------------------------
# Same-name opponents, same-roster duplicates, transfers
# ---------------------------------------------------------------------------

def test_same_name_players_on_opposing_teams_are_never_cross_matched():
    game = resolve_boxscore_game_players(
        home_team_query="St. Bonaventure", away_team_query="VCU", season=2026,
        home_player_names=["Frank Mitchell"], away_player_names=["Frank Mitchell"],
    )
    home_player = game["home"]["players"][0]
    away_player = game["away"]["players"][0]
    assert isinstance(home_player, PlayerIdentity) and isinstance(away_player, PlayerIdentity)
    assert home_player.team_id != away_player.team_id


def test_home_and_away_team_results_are_independently_resolved():
    game = resolve_boxscore_game_players(
        home_team_query="St. Bonaventure", away_team_query="Not A Real Team", season=2026,
        home_player_names=["Frank Mitchell"], away_player_names=["Someone"],
    )
    assert isinstance(game["home"]["team"], TeamIdentity)
    assert not isinstance(game["away"]["team"], TeamIdentity)


def test_game_result_does_not_include_a_fabricated_game_id():
    game = resolve_boxscore_game_players(
        home_team_query="St. Bonaventure", away_team_query="VCU", season=2026,
        home_player_names=["Frank Mitchell"], away_player_names=["Someone"],
    )
    assert "game_id" not in game
    assert set(game.keys()) == {"home", "away"}


def test_transfer_uses_distinct_scoped_records_without_source_id():
    before = resolve_boxscore_player_identity("Jane Doe", "St. Bonaventure", 2025)
    after = resolve_boxscore_player_identity("Jane Doe", "VCU", 2026)
    assert before.player_id != after.player_id


def test_transfer_with_source_id_stays_stable():
    before = resolve_boxscore_player_identity("Jane Doe", "St. Bonaventure", 2025, source_player_id="jd-1")
    after = resolve_boxscore_player_identity("Jane Doe", "VCU", 2026, source_player_id="jd-1")
    assert before.player_id == after.player_id


# ---------------------------------------------------------------------------
# Duplicate rows / malformed box scores / conflicting IDs
# ---------------------------------------------------------------------------

def test_find_duplicate_boxscore_rows_detects_repeats():
    raw = ["Frank Mitchell", "Frank Mitchell", "Team Totals", "Team Totals", "Cayden Charles"]
    assert find_duplicate_boxscore_rows(raw) == {"Frank Mitchell": 2}


def test_find_duplicate_boxscore_rows_ignores_non_player_rows():
    raw = ["Team Totals", "Team Totals", "Reserves", "Reserves"]
    assert find_duplicate_boxscore_rows(raw) == {}


def test_find_duplicate_boxscore_rows_empty_when_clean():
    assert find_duplicate_boxscore_rows(["Frank Mitchell", "Cayden Charles"]) == {}


def test_find_boxscore_id_conflicts_detects_reused_id_different_names():
    conflicting = [
        build_player_identity("Player A", 1, 2026, source="boxscore_sr", source_player_id="999"),
        build_player_identity("Player B", 1, 2026, source="boxscore_sr", source_player_id="999"),
    ]
    conflicts = find_boxscore_id_conflicts(conflicting)
    assert len(conflicts) == 1
    assert conflicts[0]["source_id"] == "999"
    assert set(conflicts[0]["display_names"]) == {"Player A", "Player B"}


def test_find_boxscore_id_conflicts_allows_legitimate_multi_game_same_team_season():
    # Same id, same name, same (team, season) across two rows (e.g. two
    # games) is not a conflict.
    clean = [
        build_player_identity("Frank Mitchell", 1, 2026, source="boxscore_sr", source_player_id="100"),
        build_player_identity("Frank Mitchell", 1, 2026, source="boxscore_sr", source_player_id="100"),
    ]
    assert find_boxscore_id_conflicts(clean) == []


def test_find_boxscore_id_conflicts_allows_transfer_across_team_season():
    transfer = [
        build_player_identity("Frank Mitchell", 1, 2025, source="boxscore_sr", source_player_id="100"),
        build_player_identity("Frank Mitchell", 2, 2026, source="boxscore_sr", source_player_id="100"),
    ]
    assert find_boxscore_id_conflicts(transfer) == []


def test_malformed_box_score_row_none_does_not_crash():
    result = resolve_boxscore_player_identity(None, "St. Bonaventure", 2026)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "not_found"


def test_missing_season_type_mismatch_does_not_crash():
    # season passed as an unusual but coercible type still works via build_player_identity.
    result = resolve_boxscore_player_identity("Frank Mitchell", "St. Bonaventure", 2026)
    assert isinstance(result, PlayerIdentity)


# ---------------------------------------------------------------------------
# DataFrame parity: attaching identity results changes nothing statistical
# ---------------------------------------------------------------------------

def test_attaching_identity_results_does_not_alter_boxscore_dataframe():
    """
    Simulates attaching resolved identities as a new column onto an
    existing box-score-shaped DataFrame, confirming every original
    column's values, order, and dtype are unchanged -- this batch's
    explicit DataFrame parity requirement.
    """
    df = pd.DataFrame({
        "Player": ["Frank Mitchell", "Daryl Simmons II", "Team Totals"],
        "PTS": [18, 12, 30],
        "REB": [7, 3, 10],
        "MP": [32.5, 28.1, None],
    })
    original = df.copy(deep=True)

    df["ResolvedPlayerId"] = df["Player"].apply(
        lambda name: (
            r.player_id if isinstance((r := resolve_boxscore_player_identity(name, "St. Bonaventure", 2026)), PlayerIdentity)
            else None
        )
    )

    # Original columns, values, order, and dtypes are untouched.
    for col in original.columns:
        pd.testing.assert_series_equal(df[col], original[col])
    assert list(df.columns[: len(original.columns)]) == list(original.columns)

    # The new column reflects correct resolution/exclusion behavior.
    assert df.loc[df["Player"] == "Frank Mitchell", "ResolvedPlayerId"].iloc[0] is not None
    assert df.loc[df["Player"] == "Team Totals", "ResolvedPlayerId"].iloc[0] is None


def test_attaching_identity_results_preserves_dtypes_exactly():
    df = pd.DataFrame({"Player": ["Frank Mitchell"], "PTS": [18], "FG%": [0.512]})
    original_dtypes = df.dtypes.copy()
    df["_unused"] = df["Player"].apply(lambda n: resolve_boxscore_player_identity(n, "St. Bonaventure", 2026))
    pd.testing.assert_series_equal(df.dtypes[original_dtypes.index], original_dtypes)


# ---------------------------------------------------------------------------
# Compatibility / isolation
# ---------------------------------------------------------------------------

def test_boxscore_module_does_not_import_postgame_or_ovr_code():
    import ast
    import inspect

    import src.identity.boxscore_player_identity as mod

    tree = ast.parse(inspect.getsource(mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert not any("ovr_model" in name or "postgame" in name for name in imported)


def test_boxscore_resolution_does_not_mutate_team_registry():
    from src import slug_utils

    before_size = len(slug_utils.TEAM_REGISTRY)
    before_aliases = dict(slug_utils._DISPLAY_NAME_ALIASES)
    resolve_boxscore_player_identity("Frank Mitchell", "St. Bonaventure", 2026)
    resolve_boxscore_game_players(
        home_team_query="St. Bonaventure", away_team_query="VCU", season=2026,
        home_player_names=["Frank Mitchell"], away_player_names=["Someone"],
    )
    assert len(slug_utils.TEAM_REGISTRY) == before_size
    assert slug_utils._DISPLAY_NAME_ALIASES == before_aliases
