"""
Regression tests for src/identity/postgame_player_identity.py (Phase 2B, batch 5).

No live requests: uses small synthetic postgame/box-score-shaped data
(raw name lists / DataFrames) rather than fetching real Sports-Reference
boxscore pages.
"""
import pandas as pd
import pytest

from src.identity.contracts import PlayerIdentity, TeamIdentity, UnresolvedIdentity
from src.identity.player_identity import build_player_identity
from src.identity.postgame_player_identity import (
    SR_NON_PLAYER_ROWS_V15,
    attach_postgame_identity_columns,
    build_postgame_game_context,
    check_no_cross_team_leakage,
    filter_postgame_player_rows_v15,
    find_duplicate_postgame_rows,
    find_missing_between_boxscore_and_allstats,
    is_postgame_non_player_row,
    is_sr_boxscore_non_player_row_v15,
    resolve_postgame_game_players,
    resolve_postgame_player_identity,
)


# ---------------------------------------------------------------------------
# Non-player row exclusion: v15 compat (broader set) + canonical superset
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("row", sorted(SR_NON_PLAYER_ROWS_V15 - {""}))
def test_sr_non_player_rows_v15_are_excluded(row):
    assert is_sr_boxscore_non_player_row_v15(row) is True
    assert is_postgame_non_player_row(row) is True


@pytest.mark.parametrize("row", ["Starters", "Did Not Dress", "Not With Team", "Player Suspended"])
def test_gap_rows_missed_by_batch4_filter_are_caught_here(row):
    """
    These four labels are excluded by the ACTIVE parser's SR_NON_PLAYER_ROWS
    (cell 642) but are NOT matched by batch 4's narrower regex
    (Reserves|Team Totals|Did Not Play) -- confirming the documented gap,
    and that this module's superset check closes it.
    """
    from src.identity.boxscore_player_identity import is_non_player_boxscore_row

    assert is_non_player_boxscore_row(row) is False  # the gap, confirmed
    assert is_postgame_non_player_row(row) is True    # closed here


def test_real_player_rows_are_not_excluded():
    assert is_postgame_non_player_row("Frank Mitchell") is False
    assert is_postgame_non_player_row("Darius Acuff Jr.") is False


def test_filter_postgame_player_rows_v15_preserves_order():
    raw = ["Frank Mitchell", "Starters", "Daryl Simmons II", "Did Not Dress", "Cayden Charles"]
    assert filter_postgame_player_rows_v15(raw) == ["Frank Mitchell", "Daryl Simmons II", "Cayden Charles"]


def test_resolve_postgame_player_identity_rejects_gap_rows_directly():
    for row in ["Starters", "Did Not Dress", "Not With Team", "Player Suspended"]:
        result = resolve_postgame_player_identity(row, "St. Bonaventure", 2026)
        assert not isinstance(result, PlayerIdentity)
        assert result.reason == "not_found"


def test_resolve_postgame_player_identity_still_rejects_batch4_rows():
    for row in ["Reserves", "Team Totals", "Did Not Play"]:
        result = resolve_postgame_player_identity(row, "St. Bonaventure", 2026)
        assert not isinstance(result, PlayerIdentity)
        assert result.reason == "not_found"


# ---------------------------------------------------------------------------
# Exact resolution, team routing (delegates to batch 4, reused not reimplemented)
# ---------------------------------------------------------------------------

def test_exact_full_name_resolution_without_roster():
    result = resolve_postgame_player_identity("Frank Mitchell", "St. Bonaventure", 2026)
    assert isinstance(result, PlayerIdentity)
    assert result.display_name == "Frank Mitchell"
    assert result.team_id == 905884  # confirmed St. Bonaventure team_id used throughout this migration


def test_unknown_team_is_unresolved():
    result = resolve_postgame_player_identity("Frank Mitchell", "Not A Real Team", 2026)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "not_found"


def test_ambiguous_team_is_not_silently_resolved():
    result = resolve_postgame_player_identity("Someone", "Miami", 2026)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "ambiguous"


def test_repeated_resolution_is_deterministic():
    ids = {resolve_postgame_player_identity("Frank Mitchell", "St. Bonaventure", 2026).player_id for _ in range(3)}
    assert len(ids) == 1


def test_malformed_row_none_does_not_crash():
    result = resolve_postgame_player_identity(None, "St. Bonaventure", 2026)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "not_found"


# ---------------------------------------------------------------------------
# Exact source-ID matching / roster reuse (via batch 4 delegation)
# ---------------------------------------------------------------------------

def test_resolve_with_roster_reuses_sr_source_id():
    roster = [build_player_identity("Frank Mitchell", 905884, 2026, source="sports_reference", source_player_id="mitchell-1")]
    result = resolve_postgame_player_identity("Frank Mitchell", "St. Bonaventure", 2026, roster_identities=roster)
    assert isinstance(result, PlayerIdentity)
    assert result.player_id == "sports_reference:mitchell-1"


def test_ambiguous_roster_candidates_within_correct_team_scope():
    roster = [
        build_player_identity("John Smith", 905884, 2026),
        build_player_identity("John Smith Jr.", 905884, 2026),
    ]
    result = resolve_postgame_player_identity("John Smith", "St. Bonaventure", 2026, roster_identities=roster)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "ambiguous"
    assert len(result.candidates) == 2


# ---------------------------------------------------------------------------
# Accents, apostrophes, hyphens, suffixes, initials
# ---------------------------------------------------------------------------

def test_apostrophe_and_hyphen_names_preserved():
    a = resolve_postgame_player_identity("Amar'e Marshall", "St. Bonaventure", 2026)
    b = resolve_postgame_player_identity("Jean-Paul Olukemi", "St. Bonaventure", 2026)
    assert a.display_name == "Amar'e Marshall"
    assert b.display_name == "Jean-Paul Olukemi"


@pytest.mark.parametrize("suffixed", ["Darius Acuff Jr.", "John Smith Sr.", "John Smith II", "John Smith III", "John Smith IV"])
def test_all_suffix_forms_resolve(suffixed):
    result = resolve_postgame_player_identity(suffixed, "St. Bonaventure", 2026)
    assert isinstance(result, PlayerIdentity)
    assert result.display_name == suffixed


def test_suffix_conflict_is_flagged_ambiguous_not_guessed():
    # Box-score row lacks the suffix; roster has two suffix-distinct entries.
    roster = [
        build_player_identity("Daryl Simmons", 905884, 2026),
        build_player_identity("Daryl Simmons II", 905884, 2026),
    ]
    result = resolve_postgame_player_identity("Daryl Simmons", "St. Bonaventure", 2026, roster_identities=roster)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "ambiguous"


def test_initials_and_full_name_are_distinct():
    a = resolve_postgame_player_identity("A. Marshall", "St. Bonaventure", 2026)
    b = resolve_postgame_player_identity("Amare Marshall", "St. Bonaventure", 2026)
    assert a.player_id != b.player_id


# ---------------------------------------------------------------------------
# Same-name opponents / same-roster duplicates / transfers / per-side season
# ---------------------------------------------------------------------------

def test_same_name_players_on_opposing_teams_are_never_cross_matched():
    game = resolve_postgame_game_players(
        away_team_query="VCU", away_season=2026, away_player_names=["Frank Mitchell"],
        home_team_query="St. Bonaventure", home_season=2026, home_player_names=["Frank Mitchell"],
    )
    home_player = game["home"]["players"][0]
    away_player = game["away"]["players"][0]
    assert isinstance(home_player, PlayerIdentity) and isinstance(away_player, PlayerIdentity)
    assert home_player.team_id != away_player.team_id


def test_resolve_postgame_game_players_allows_independent_seasons_per_side():
    game = resolve_postgame_game_players(
        away_team_query="VCU", away_season=2025, away_player_names=["Someone"],
        home_team_query="St. Bonaventure", home_season=2026, home_player_names=["Frank Mitchell"],
    )
    assert game["away"]["season"] == 2025
    assert game["home"]["season"] == 2026
    assert game["home"]["players"][0].season == 2026


def test_game_result_does_not_include_a_fabricated_game_id():
    game = resolve_postgame_game_players(
        away_team_query="VCU", away_season=2026, away_player_names=["Someone"],
        home_team_query="St. Bonaventure", home_season=2026, home_player_names=["Frank Mitchell"],
    )
    assert "game_id" not in game
    assert set(game.keys()) == {"home", "away"}


def test_transfer_uses_distinct_scoped_records_without_source_id():
    before = resolve_postgame_player_identity("Jane Doe", "St. Bonaventure", 2025)
    after = resolve_postgame_player_identity("Jane Doe", "VCU", 2026)
    assert before.player_id != after.player_id


def test_transfer_with_source_id_stays_stable():
    before = resolve_postgame_player_identity("Jane Doe", "St. Bonaventure", 2025, source_player_id="jd-1")
    after = resolve_postgame_player_identity("Jane Doe", "VCU", 2026, source_player_id="jd-1")
    assert before.player_id == after.player_id


def test_transfer_mismatch_does_not_leak_roster_across_seasons():
    roster = [build_player_identity("Frank Mitchell", 905884, 2025, source="sports_reference", source_player_id="mitchell-1")]
    result = resolve_postgame_player_identity("Frank Mitchell", "St. Bonaventure", 2026, roster_identities=roster)
    assert isinstance(result, PlayerIdentity)
    assert "sports_reference" not in result.sources  # 2025 roster entry must not match 2026 row


# ---------------------------------------------------------------------------
# check_no_cross_team_leakage
# ---------------------------------------------------------------------------

def test_check_no_cross_team_leakage_clean_game():
    game = resolve_postgame_game_players(
        away_team_query="VCU", away_season=2026, away_player_names=["Frank Mitchell"],
        home_team_query="St. Bonaventure", home_season=2026, home_player_names=["Frank Mitchell"],
    )
    conflicts = check_no_cross_team_leakage(game["away"]["players"], game["home"]["players"])
    assert conflicts == []


def test_check_no_cross_team_leakage_detects_shared_source_id_misuse():
    away = [build_player_identity("Player A", 1, 2026, source="postgame_sr", source_player_id="dup-1")]
    home = [build_player_identity("Player B", 2, 2026, source="postgame_sr", source_player_id="dup-1")]
    conflicts = check_no_cross_team_leakage(away, home)
    assert len(conflicts) == 1
    assert conflicts[0]["player_id"] == "postgame_sr:dup-1"
    assert conflicts[0]["away_team_id"] == 1
    assert conflicts[0]["home_team_id"] == 2


# ---------------------------------------------------------------------------
# Duplicate rows
# ---------------------------------------------------------------------------

def test_find_duplicate_postgame_rows_detects_repeats():
    raw = ["Frank Mitchell", "Frank Mitchell", "Starters", "Did Not Dress", "Cayden Charles"]
    assert find_duplicate_postgame_rows(raw) == {"Frank Mitchell": 2}


def test_find_duplicate_postgame_rows_ignores_non_player_rows_including_gap_rows():
    raw = ["Starters", "Starters", "Did Not Dress", "Did Not Dress"]
    assert find_duplicate_postgame_rows(raw) == {}


def test_find_duplicate_postgame_rows_empty_when_clean():
    assert find_duplicate_postgame_rows(["Frank Mitchell", "Cayden Charles"]) == {}


# ---------------------------------------------------------------------------
# Missing-record detection: box score vs AllStats
# ---------------------------------------------------------------------------

def test_find_missing_between_boxscore_and_allstats_both_directions():
    boxscore_ids = [
        build_player_identity("Frank Mitchell", 905884, 2026),
        build_player_identity("Walk On Guy", 905884, 2026),
    ]
    allstats_ids = [
        build_player_identity("Frank Mitchell", 905884, 2026),
        build_player_identity("Cayden Charles", 905884, 2026),
    ]
    result = find_missing_between_boxscore_and_allstats(boxscore_ids, allstats_ids)
    assert result["in_boxscore_not_allstats"] == ["Walk On Guy"]
    assert result["in_allstats_not_boxscore"] == ["Cayden Charles"]


def test_find_missing_between_boxscore_and_allstats_empty_when_matching():
    ids = [build_player_identity("Frank Mitchell", 905884, 2026)]
    result = find_missing_between_boxscore_and_allstats(ids, ids)
    assert result == {"in_boxscore_not_allstats": [], "in_allstats_not_boxscore": []}


def test_find_missing_between_boxscore_and_allstats_suffix_insensitive():
    # "Daryl Simmons" (box score, no suffix) vs "Daryl Simmons II" (AllStats)
    # share a canonical key -- not treated as missing on either side.
    boxscore_ids = [build_player_identity("Daryl Simmons", 905884, 2026)]
    allstats_ids = [build_player_identity("Daryl Simmons II", 905884, 2026)]
    result = find_missing_between_boxscore_and_allstats(boxscore_ids, allstats_ids)
    assert result == {"in_boxscore_not_allstats": [], "in_allstats_not_boxscore": []}


# ---------------------------------------------------------------------------
# build_postgame_game_context: preserves context, no fabricated game_id
# ---------------------------------------------------------------------------

def test_build_postgame_game_context_preserves_url_and_date():
    ctx = build_postgame_game_context("https://www.sports-reference.com/cbb/boxscores/2026-01-15-19-vcu.html", "2026-01-15")
    assert ctx == {
        "source_url": "https://www.sports-reference.com/cbb/boxscores/2026-01-15-19-vcu.html",
        "game_date": "2026-01-15",
    }
    assert "game_id" not in ctx


# ---------------------------------------------------------------------------
# attach_postgame_identity_columns: additive parity vs. real OVR output shape
# ---------------------------------------------------------------------------

def _fake_compute_game_ovr_from_boxscore(stats_df: pd.DataFrame) -> pd.DataFrame:
    """
    Minimal stand-in for the real notebook function `compute_game_ovr_from_boxscore`
    (cell 645) -- reproduces only its OUTPUT SHAPE (Player/PTS/.../OVR/OVR_Rank,
    sorted descending by OVR) so this test can prove attaching identity columns
    changes nothing about that shape, without executing notebook code (out of
    scope / no live formula changes permitted this batch).
    """
    df = stats_df.copy()
    df["OVR"] = [72.4, 55.1, 40.0]
    df["OVR_Rank"] = df["OVR"].rank(ascending=False, method="min").astype("Int64")
    return df.sort_values("OVR", ascending=False, na_position="last").reset_index(drop=True)


def test_attach_postgame_identity_columns_preserves_ovr_output_exactly():
    raw_stats = pd.DataFrame({
        "Player": ["Frank Mitchell", "Daryl Simmons II", "Team Totals"],
        "PTS": [18, 12, 30],
        "MP": [32.5, 28.1, None],
    })
    ovr_df = _fake_compute_game_ovr_from_boxscore(raw_stats)
    original = ovr_df.copy(deep=True)

    identities = [
        resolve_postgame_player_identity(name, "St. Bonaventure", 2026)
        for name in ovr_df["Player"]
    ]
    attached = attach_postgame_identity_columns(ovr_df, identities)

    # Every original column, value, dtype, and row order untouched.
    for col in original.columns:
        pd.testing.assert_series_equal(attached[col], original[col])
    assert list(attached.columns[: len(original.columns)]) == list(original.columns)

    # New columns are additive and correctly reflect resolution/exclusion.
    assert {"player_id", "identity_source", "identity_method"} <= set(attached.columns)
    mitchell_row = attached.loc[attached["Player"] == "Frank Mitchell"].iloc[0]
    assert mitchell_row["player_id"] is not None
    totals_row = attached.loc[attached["Player"] == "Team Totals"].iloc[0]
    assert totals_row["player_id"] is None
    assert totals_row["identity_method"] == "unresolved:not_found"


def test_attach_postgame_identity_columns_preserves_dtypes_exactly():
    df = pd.DataFrame({"Player": ["Frank Mitchell"], "PTS": [18], "OVR": [72.4]})
    original_dtypes = df.dtypes.copy()
    identities = [resolve_postgame_player_identity("Frank Mitchell", "St. Bonaventure", 2026)]
    attached = attach_postgame_identity_columns(df, identities)
    pd.testing.assert_series_equal(attached.dtypes[original_dtypes.index], original_dtypes)


def test_attach_postgame_identity_columns_rejects_length_mismatch():
    df = pd.DataFrame({"Player": ["Frank Mitchell", "Cayden Charles"]})
    with pytest.raises(ValueError):
        attach_postgame_identity_columns(df, [resolve_postgame_player_identity("Frank Mitchell", "St. Bonaventure", 2026)])


def test_attach_postgame_identity_columns_does_not_mutate_input_df():
    df = pd.DataFrame({"Player": ["Frank Mitchell"]})
    identities = [resolve_postgame_player_identity("Frank Mitchell", "St. Bonaventure", 2026)]
    attach_postgame_identity_columns(df, identities)
    assert list(df.columns) == ["Player"]  # unchanged -- a copy was returned, not a mutation


# ---------------------------------------------------------------------------
# Compatibility / isolation
# ---------------------------------------------------------------------------

def test_postgame_module_does_not_import_ovr_model_or_slug_utils_directly():
    """
    This module must reuse the canonical team/player/box-score adapters,
    never src.ovr_model directly, and never edit/import src.slug_utils
    directly (it goes through src.identity.team_identity, same as every
    other adapter in this migration).
    """
    import ast
    import inspect

    import src.identity.postgame_player_identity as mod

    tree = ast.parse(inspect.getsource(mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert not any("ovr_model" in name for name in imported)
    assert "src.slug_utils" not in imported


def test_postgame_resolution_does_not_mutate_team_registry():
    from src import slug_utils

    before_size = len(slug_utils.TEAM_REGISTRY)
    before_aliases = dict(slug_utils._DISPLAY_NAME_ALIASES)
    resolve_postgame_player_identity("Frank Mitchell", "St. Bonaventure", 2026)
    resolve_postgame_game_players(
        away_team_query="VCU", away_season=2026, away_player_names=["Someone"],
        home_team_query="St. Bonaventure", home_season=2026, home_player_names=["Frank Mitchell"],
    )
    assert len(slug_utils.TEAM_REGISTRY) == before_size
    assert slug_utils._DISPLAY_NAME_ALIASES == before_aliases
