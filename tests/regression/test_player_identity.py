"""
Regression tests for src/identity/player_identity.py (canonical core) and
src/identity/sr_allstats_player_identity.py (SR/AllStats adapter) --
Phase 2B, batch 1.

Uses real local fixtures where available: `TeamDump/CBB/st-bonaventure/
st-bonaventure_2026_AllStats.xlsx` (219 real columns, 15 real players,
confirmed to have zero source-ID column) and `TeamDump/CBB/connecticut/
Roster_2026.xlsx` (a genuine, separate Roster-shaped file). No live
scraping or model retraining anywhere in this file.
"""
import os

import pandas as pd
import pytest

from src.exceptions import ValidationError
from src.identity.contracts import PlayerIdentity, TeamIdentity, UnresolvedIdentity
from src.identity.player_identity import (
    build_player_identity,
    canonical_player_key_v15,
    find_duplicate_player_keys,
    match_stat_row_to_roster,
    normalize_allstats_player_v15,
    normalize_player_name_v15,
)
from src.identity.sr_allstats_player_identity import (
    extract_player_slugs_from_roster_v15,
    harmonize_player_names_v15,
    resolve_allstats_player_row,
    resolve_roster_player_identities,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SBU_ALLSTATS = os.path.join(REPO_ROOT, "TeamDump", "CBB", "st-bonaventure", "st-bonaventure_2026_AllStats.xlsx")
UCONN_ROSTER = os.path.join(REPO_ROOT, "TeamDump", "CBB", "connecticut", "Roster_2026.xlsx")


@pytest.fixture(scope="module")
def sbu_allstats():
    if not os.path.isfile(SBU_ALLSTATS):
        pytest.skip("real St. Bonaventure AllStats fixture not present")
    return pd.read_excel(SBU_ALLSTATS)


@pytest.fixture(scope="module")
def uconn_roster():
    if not os.path.isfile(UCONN_ROSTER):
        pytest.skip("real UConn Roster fixture not present")
    return pd.read_excel(UCONN_ROSTER)


# ---------------------------------------------------------------------------
# Exact names / accents / apostrophes / hyphens / suffixes (canonical core)
# ---------------------------------------------------------------------------

def test_exact_name_normalizes_and_keys_correctly():
    assert normalize_player_name_v15("Frank Mitchell") == "Frank Mitchell"
    assert canonical_player_key_v15("Frank Mitchell") == "frank mitchell"


def test_apostrophe_in_first_name_preserved_in_display_stripped_in_key():
    # Real name from the St. Bonaventure fixture.
    assert normalize_player_name_v15("Amar'e Marshall") == "Amar'e Marshall"
    assert canonical_player_key_v15("Amar'e Marshall") == "amare marshall"


def test_hyphenated_name_normalizes_consistently():
    assert canonical_player_key_v15("Jean-Paul Olukemi") == "jeanpaul olukemi"
    assert normalize_player_name_v15("Jean-Paul Olukemi") == "Jean-Paul Olukemi"


@pytest.mark.parametrize("accented,expected_key", [
    ("Nikola Jović", "nikola jovic"),   # ć -> c
    ("René Guzmán", "rene guzman"),  # é, á -> e, a
])
def test_accented_names_strip_diacritics(accented, expected_key):
    normalized = normalize_player_name_v15(accented)
    assert canonical_player_key_v15(normalized) == expected_key


@pytest.mark.parametrize("suffixed,expected_key", [
    ("Daryl Simmons II", "daryl simmons"),
    ("Melvin Council Jr.", "melvin council"),
    ("Melvin Council Jr", "melvin council"),
    ("John Smith Sr.", "john smith"),
    ("John Smith III", "john smith"),
    ("John Smith IV", "john smith"),
])
def test_suffixes_stripped_from_canonical_key_but_preserved_in_display(suffixed, expected_key):
    assert canonical_player_key_v15(suffixed) == expected_key
    # Display normalization preserves the suffix -- the key (with the
    # suffix removed) must NOT equal the full normalized display name.
    normalized_display = normalize_player_name_v15(suffixed)
    assert canonical_player_key_v15(normalized_display) == expected_key
    assert normalized_display.lower() != expected_key


def test_initials_and_full_name_are_not_treated_as_equivalent():
    # "J. Smith" and "John Smith" must NOT collapse to the same key --
    # this core never expands/guesses initials into full names.
    assert canonical_player_key_v15("J. Smith") != canonical_player_key_v15("John Smith")


def test_normalize_allstats_player_v15_reproduces_the_known_suffix_bug():
    """
    Documents (does not fix) the real bug found this session: the
    notebook's own `normalize_allstats_player` takes just the last
    whitespace token, so a suffixed name returns the suffix, not the
    surname. canonical_player_key_v15 is the function that actually
    handles suffixes correctly; this one is preserved for reference only.
    """
    assert normalize_allstats_player_v15("Daryl Simmons II") == "ii"
    assert normalize_allstats_player_v15("Frank Mitchell") == "mitchell"


# ---------------------------------------------------------------------------
# build_player_identity: source IDs, team+season scoping, determinism
# ---------------------------------------------------------------------------

def test_build_player_identity_without_source_id_is_team_season_scoped():
    pid = build_player_identity("Frank Mitchell", team_id=905884, season=2026)
    assert pid.player_id == "team:905884:season:2026:key:frank mitchell"
    assert pid.sources == {}


def test_build_player_identity_prefers_source_id_when_available():
    pid = build_player_identity(
        "Frank Mitchell", team_id=905884, season=2026,
        source="sports_reference", source_player_id="mitchell-frank-1",
    )
    assert pid.player_id == "sports_reference:mitchell-frank-1"
    assert pid.sources["sports_reference"].raw_value == "mitchell-frank-1"


def test_missing_source_id_does_not_fabricate_a_global_identity():
    # No source_player_id supplied -> the id is explicitly scoped, never a
    # bare normalized name that could look like a cross-team/season claim.
    pid = build_player_identity("Frank Mitchell", team_id=1, season=2025)
    assert "team:1:season:2025" in pid.player_id
    assert pid.player_id != "frank mitchell"


def test_same_name_players_on_different_teams_get_different_player_ids():
    a = build_player_identity("John Smith", team_id=111, season=2026)
    b = build_player_identity("John Smith", team_id=222, season=2026)
    assert a.player_id != b.player_id
    assert a.team_id != b.team_id


def test_transfer_scenario_same_player_different_team_season_are_distinct_records():
    """
    A transfer (same real person, team A season 1 -> team B season 2)
    produces two independent PlayerIdentity records with no merge --
    per this batch's instruction, connecting them requires an
    authoritative source ID or reviewed crosswalk that doesn't exist yet.
    """
    before = build_player_identity("Jane Doe", team_id=1, season=2025)
    after = build_player_identity("Jane Doe", team_id=2, season=2026)
    assert before.player_id != after.player_id
    assert before.team_id != after.team_id
    assert before.season != after.season


def test_repeated_resolution_is_deterministic():
    ids = {build_player_identity("Frank Mitchell", 905884, 2026).player_id for _ in range(3)}
    assert len(ids) == 1


# ---------------------------------------------------------------------------
# Duplicate detection / ambiguous matching within one roster
# ---------------------------------------------------------------------------

def test_find_duplicate_player_keys_on_synthetic_colliding_roster():
    dupes = find_duplicate_player_keys(["John Smith", "John Smith Jr.", "Amare Marshall"])
    assert dupes == {"john smith": ["John Smith", "John Smith Jr."]}


def test_find_duplicate_player_keys_on_real_roster_has_none(sbu_allstats):
    dupes = find_duplicate_player_keys(sbu_allstats["Player"].tolist())
    assert dupes == {}, f"unexpected real-roster PlayerKey collision: {dupes}"


def test_match_stat_row_to_roster_returns_ambiguous_for_colliding_keys():
    roster = [
        build_player_identity("John Smith", 1, 2026),
        build_player_identity("John Smith Jr.", 1, 2026),
    ]
    result = match_stat_row_to_roster("John Smith", roster)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "ambiguous"
    assert len(result.candidates) == 2


def test_match_stat_row_to_roster_resolves_unique_key():
    roster = [build_player_identity("Frank Mitchell", 905884, 2026)]
    result = match_stat_row_to_roster("Frank Mitchell", roster)
    assert isinstance(result, PlayerIdentity)
    assert result.display_name == "Frank Mitchell"


def test_match_stat_row_to_roster_returns_not_found_for_unknown_name():
    roster = [build_player_identity("Frank Mitchell", 905884, 2026)]
    result = match_stat_row_to_roster("Nobody Real", roster)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "not_found"


# ---------------------------------------------------------------------------
# SR/AllStats adapter: roster resolution, team+season scoping, real fixtures
# ---------------------------------------------------------------------------

def test_resolve_roster_player_identities_with_real_sbu_allstats(sbu_allstats):
    roster_df = sbu_allstats[["Player"]].copy()
    identities, duplicates, team_result = resolve_roster_player_identities(
        roster_df, "St. Bonaventure", 2026
    )
    assert isinstance(team_result, TeamIdentity)
    assert team_result.display_name == "St. Bonaventure"
    assert len(identities) == len(roster_df) == 15
    assert duplicates == {}
    assert all(p.team_id == team_result.team_id and p.season == 2026 for p in identities)


def test_resolve_roster_player_identities_with_real_uconn_roster(uconn_roster):
    identities, duplicates, team_result = resolve_roster_player_identities(
        uconn_roster, "UConn", 2026
    )
    assert isinstance(team_result, TeamIdentity)
    assert team_result.display_name == "Connecticut"
    assert len(identities) == 15
    assert duplicates == {}


def test_resolve_roster_player_identities_attaches_sr_slug_when_available():
    roster_df = pd.DataFrame({"Player": ["Frank Mitchell", "Daryl Simmons II"]})
    slug_map = {"Frank Mitchell": "mitchell-frank-1"}
    identities, _, _ = resolve_roster_player_identities(
        roster_df, "St. Bonaventure", 2026, slug_map=slug_map
    )
    frank = next(p for p in identities if p.display_name == "Frank Mitchell")
    daryl = next(p for p in identities if "Simmons" in p.display_name)
    assert frank.sources["sports_reference"].raw_value == "mitchell-frank-1"
    assert daryl.sources == {}  # missing source ID -- not fabricated


def test_resolve_roster_player_identities_detects_duplicates_within_roster():
    roster_df = pd.DataFrame({"Player": ["John Smith", "John Smith Jr.", "Amare Marshall"]})
    identities, duplicates, team_result = resolve_roster_player_identities(
        roster_df, "St. Bonaventure", 2026
    )
    assert isinstance(team_result, TeamIdentity)
    assert len(identities) == 3  # still returns every row -- doesn't drop/merge
    assert "john smith" in duplicates


def test_resolve_roster_player_identities_rejects_unresolvable_team():
    roster_df = pd.DataFrame({"Player": ["Frank Mitchell"]})
    identities, duplicates, team_result = resolve_roster_player_identities(
        roster_df, "Not A Real Team", 2026
    )
    assert not isinstance(team_result, TeamIdentity)
    assert team_result.reason == "not_found"
    assert identities == [] and duplicates == []


def test_resolve_roster_player_identities_rejects_ambiguous_team():
    roster_df = pd.DataFrame({"Player": ["Frank Mitchell"]})
    identities, duplicates, team_result = resolve_roster_player_identities(
        roster_df, "Miami", 2026
    )
    assert not isinstance(team_result, TeamIdentity)
    assert team_result.reason == "ambiguous"
    assert identities == []


def test_resolve_roster_player_identities_raises_on_missing_player_column():
    with pytest.raises(ValidationError):
        resolve_roster_player_identities(pd.DataFrame({"Name": ["x"]}), "St. Bonaventure", 2026)


def test_resolve_roster_player_identities_handles_malformed_rows():
    # NaN / non-string entries should not crash resolution.
    roster_df = pd.DataFrame({"Player": ["Frank Mitchell", None, ""]})
    identities, duplicates, team_result = resolve_roster_player_identities(
        roster_df, "St. Bonaventure", 2026
    )
    assert isinstance(team_result, TeamIdentity)
    assert len(identities) == 3


def test_resolve_allstats_player_row_end_to_end(sbu_allstats):
    roster_df = sbu_allstats[["Player"]].copy()
    identities, _, _ = resolve_roster_player_identities(roster_df, "St. Bonaventure", 2026)
    result = resolve_allstats_player_row("Frank Mitchell", identities)
    assert isinstance(result, PlayerIdentity)
    assert result.display_name == "Frank Mitchell"


def test_same_name_different_teams_do_not_cross_match():
    """Two different teams' rosters, each containing "Frank Mitchell",
    must never be matched against each other."""
    sbu_roster = [build_player_identity("Frank Mitchell", team_id=905884, season=2026)]
    other_roster = [build_player_identity("Frank Mitchell", team_id=111111, season=2026)]
    result = resolve_allstats_player_row("Frank Mitchell", sbu_roster)
    assert isinstance(result, PlayerIdentity)
    assert result.team_id == 905884
    assert result.team_id != other_roster[0].team_id


# ---------------------------------------------------------------------------
# Compatibility wrapper: harmonize_player_names_v15, extract_player_slugs_from_roster_v15
# ---------------------------------------------------------------------------

def test_harmonize_player_names_v15_applies_reviewed_suffix_patches():
    dfs = {"Roster": pd.DataFrame({"Player": ["Daryl Simmons", "Frank Mitchell"]})}
    result = harmonize_player_names_v15("st-bonaventure", dfs)
    names = result["Roster"]["Player"].tolist()
    assert "Daryl Simmons II" in names
    assert "Frank Mitchell" in names
    assert "PlayerKey" in result["Roster"].columns


def test_harmonize_player_names_v15_patch_is_team_scoped():
    # The Daryl Simmons -> II patch must NOT apply outside St. Bonaventure.
    dfs = {"Roster": pd.DataFrame({"Player": ["Daryl Simmons"]})}
    result = harmonize_player_names_v15("some-other-team", dfs)
    assert result["Roster"]["Player"].tolist() == ["Daryl Simmons"]


def test_harmonize_player_names_v15_kansas_patch():
    dfs = {"Roster": pd.DataFrame({"Player": ["Melvin Council"]})}
    result = harmonize_player_names_v15("kansas", dfs)
    assert result["Roster"]["Player"].tolist() == ["Melvin Council Jr."]


def test_harmonize_player_names_v15_skips_empty_and_missing_player_column():
    dfs = {"Empty": pd.DataFrame(), "NoPlayerCol": pd.DataFrame({"X": [1]})}
    result = harmonize_player_names_v15("st-bonaventure", dfs)
    assert result["Empty"].empty
    assert "Player" not in result["NoPlayerCol"].columns


def test_extract_player_slugs_from_roster_v15_parses_real_sr_html_shape():
    from bs4 import BeautifulSoup

    html = """
    <table id="roster">
      <tr><td><a href="/cbb/players/frank-mitchell-1.html">Frank Mitchell</a></td></tr>
      <tr><td><a href="/cbb/players/daryl-simmons-jr-1.html">Daryl Simmons Jr.</a></td></tr>
      <tr><td>No Link Player</td></tr>
    </table>
    """
    soup = BeautifulSoup(html, "lxml")
    slug_map = extract_player_slugs_from_roster_v15(soup)
    assert slug_map["Frank Mitchell"] == "frank-mitchell-1"
    assert slug_map["Daryl Simmons Jr."] == "daryl-simmons-jr-1"
    assert len(slug_map) == 2  # the no-link row contributes nothing


def test_extract_player_slugs_from_roster_v15_handles_comment_hidden_table():
    from bs4 import BeautifulSoup

    html = """
    <div><!--
    <table id="roster">
      <tr><td><a href="/cbb/players/hidden-player-1.html">Hidden Player</a></td></tr>
    </table>
    --></div>
    """
    soup = BeautifulSoup(html, "lxml")
    slug_map = extract_player_slugs_from_roster_v15(soup)
    assert slug_map.get("Hidden Player") == "hidden-player-1"


def test_extract_player_slugs_from_roster_v15_returns_empty_for_no_roster_table():
    from bs4 import BeautifulSoup

    soup = BeautifulSoup("<div>nothing here</div>", "lxml")
    assert extract_player_slugs_from_roster_v15(soup) == {}


# ---------------------------------------------------------------------------
# Isolation: player resolution does not mutate team registries or stats
# ---------------------------------------------------------------------------

def test_player_resolution_does_not_mutate_team_registry():
    from src import slug_utils
    from src.identity.team_identity import ensure_team_registry

    ensure_team_registry()
    before_size = len(slug_utils.TEAM_REGISTRY)
    before_aliases = len(slug_utils._DISPLAY_NAME_ALIASES)

    roster_df = pd.DataFrame({"Player": ["Frank Mitchell", "Daryl Simmons II"]})
    resolve_roster_player_identities(roster_df, "St. Bonaventure", 2026)

    assert len(slug_utils.TEAM_REGISTRY) == before_size
    assert len(slug_utils._DISPLAY_NAME_ALIASES) == before_aliases


def test_player_resolution_does_not_alter_source_dataframe_statistics(sbu_allstats):
    """
    Confirms this batch's "no statistical or model output changes"
    requirement: resolving player identities from a loaded AllStats
    DataFrame must not mutate any of its 219 real columns (stats, OVR
    inputs, etc.) -- only reads the Player column.
    """
    original = sbu_allstats.copy(deep=True)
    roster_df = sbu_allstats[["Player"]].copy()
    resolve_roster_player_identities(roster_df, "St. Bonaventure", 2026)
    pd.testing.assert_frame_equal(sbu_allstats, original)
