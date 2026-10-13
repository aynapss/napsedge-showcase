"""
Regression tests for src/identity/net_ranking_identity.py
(Phase 2A, NET-ranking adapter).

No live scraping anywhere: DataFrame-mode / list-mode tests inject a
`fetch_fn` returning a small fixture DataFrame instead of hitting NCAA.com;
"exact CBS name" and full-index tests use the real cached
`Reference Py Files/net_rankings_2026.csv` (365 rows) and, for the
DataFrame-crosswalk mode, the real `cache/sportsref_cbb_schools.parquet`
fixture from the Sports-Reference/DataFrame batch (which includes its
already-documented 23 repeated-header garbage rows -- not fixed here,
per this batch's instructions, just confirmed non-fatal).
"""
import os

import pandas as pd
import pytest

from src.exceptions import ValidationError
from src.identity.contracts import TeamIdentity
from src.identity.net_ranking_identity import (
    NCAA_NET_TO_TEAM_SLUG_FALLBACK,
    attach_net_rank_v15,
    attach_net_rank_via_registry,
    build_net_rankings_from_ncaa_v15,
    build_net_rankings_index,
    build_sr_to_team_slug_crosswalk_v15,
    ensure_sr_slug_column_v15,
    resolve_net_team_identity,
    resolve_net_team_sr_slug_v15,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NET_CSV = os.path.join(REPO_ROOT, "Reference Py Files", "net_rankings_2026.csv")
SCHOOLS_PARQUET = os.path.join(REPO_ROOT, "cache", "sportsref_cbb_schools.parquet")


@pytest.fixture(scope="module")
def net_rankings_df():
    return pd.read_csv(NET_CSV)


@pytest.fixture(scope="module")
def all_schools_df():
    if not os.path.isfile(SCHOOLS_PARQUET):
        pytest.skip("cache/sportsref_cbb_schools.parquet fixture not present")
    return pd.read_parquet(SCHOOLS_PARQUET)


# A small, realistic NCAA.com-shaped Rank/Team table for fetch_fn injection.
FAKE_NCAA_TABLE = pd.DataFrame({
    "Rank": [1, 2, 3, 4, 5],
    "Team": ["Duke", "UAB", "TCU", "Miami FL", "Saint Joseph's"],
})


def _fake_fetch(url: str) -> pd.DataFrame:
    return FAKE_NCAA_TABLE.copy()


# ---------------------------------------------------------------------------
# Exact CBS/NCAA names, sportsbook-style abbreviations
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query,expected_display_name", [
    ("St. Bonaventure", "St. Bonaventure"),
    ("uab", "Alabama-Birmingham"),
    ("tcu", "Texas Christian"),
    ("nc-state", "North Carolina State"),
    ("vcu", "Virginia Commonwealth"),
    ("smu", "Southern Methodist"),
])
def test_exact_and_abbreviated_names_resolve(query, expected_display_name):
    result = resolve_net_team_identity(query)
    assert isinstance(result, TeamIdentity), f"{query!r} should resolve, got {result}"
    assert result.display_name == expected_display_name


@pytest.mark.parametrize("query,expected_display_name", [
    ("miami-fl", "Miami (FL)"),
    ("miami-oh", "Miami (OH)"),
    ("loyola-chicago", "Loyola Chicago"),
    ("loyola-il", "Loyola Chicago"),
])
def test_disambiguated_miami_and_loyola_resolve(query, expected_display_name):
    result = resolve_net_team_identity(query)
    assert isinstance(result, TeamIdentity)
    assert result.display_name == expected_display_name


def test_bare_miami_is_ambiguous_not_hardcoded_to_florida():
    """
    CBS_NET_ALIASES_TO_SR hardcodes "miami"->"miami-fl" in the legacy
    compat wrapper -- the canonical path does NOT adopt that shortcut.
    """
    result = resolve_net_team_identity("miami")
    assert not isinstance(result, TeamIdentity)
    assert result.reason == "ambiguous"


def test_unknown_net_team_is_unresolved():
    result = resolve_net_team_identity("Not A Real Team Xyz")
    assert not isinstance(result, TeamIdentity)
    assert result.reason == "not_found"


def test_repeated_resolution_is_deterministic():
    ids = {resolve_net_team_identity("vcu").team_id for _ in range(3)}
    assert len(ids) == 1


# ---------------------------------------------------------------------------
# The state-university prefix-collision bug found and fixed this session
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state_school,expected_state,bare_school,expected_bare", [
    ("ohio-state", "Ohio State", "ohio", "Ohio"),
    ("iowa-state", "Iowa State", "iowa", "Iowa"),
    ("penn-state", "Penn State", "pennsylvania", "Pennsylvania"),
])
def test_state_university_and_bare_school_are_not_conflated(
    state_school, expected_state, bare_school, expected_bare
):
    """
    resolve_team_identity_by_shortening (used by ESPN/Covers/Odds) would
    incorrectly drop "state" as if it were a droppable mascot, landing on
    the bare school instead. This adapter does not use that helper for
    exactly this reason -- confirmed here with the specific pairs found
    colliding against the real 365-row fixture before the fix.
    """
    state_result = resolve_net_team_identity(state_school)
    bare_result = resolve_net_team_identity(bare_school)
    assert isinstance(state_result, TeamIdentity) and isinstance(bare_result, TeamIdentity)
    assert state_result.display_name == expected_state
    assert bare_result.display_name == expected_bare
    assert state_result.team_id != bare_result.team_id


def test_kansas_city_and_missouri_are_not_conflated():
    kc = resolve_net_team_identity("kansas-city")
    mizzou = resolve_net_team_identity("missouri")
    assert isinstance(kc, TeamIdentity) and isinstance(mizzou, TeamIdentity)
    assert kc.display_name == "Kansas City"
    assert mizzou.display_name == "Missouri"
    assert kc.team_id != mizzou.team_id


def test_florida_international_and_florida_are_not_conflated():
    fiu = resolve_net_team_identity("florida-intl")
    uf = resolve_net_team_identity("florida")
    assert isinstance(fiu, TeamIdentity) and isinstance(uf, TeamIdentity)
    assert fiu.display_name == "Florida International"
    assert uf.display_name == "Florida"
    assert fiu.team_id != uf.team_id


def test_hyphenated_slug_alias_resolves_without_space_conversion_breaking_it():
    # "college-of-charleston" only matches as an exact alias in its
    # hyphenated form -- converting to spaces first (as the ESPN/Covers
    # adapters do) breaks it. Confirms the as-is-first ordering fix.
    result = resolve_net_team_identity("college-of-charleston")
    assert isinstance(result, TeamIdentity)
    assert result.display_name == "Charleston"


# ---------------------------------------------------------------------------
# Full real-fixture sweep: resolution rate, collisions, unmapped tracking
# ---------------------------------------------------------------------------

def test_real_net_rankings_csv_resolves_the_overwhelming_majority(net_rankings_df):
    rankings_by_team_id, unmapped = build_net_rankings_index(net_rankings_df)
    assert len(rankings_by_team_id) >= 355, (
        f"only {len(rankings_by_team_id)}/365 resolved -- regression in resolution coverage"
    )
    assert len(unmapped) <= 10


def test_real_net_rankings_csv_has_zero_cross_team_collisions(net_rankings_df):
    """
    The specific regression this session found and fixed: different rows
    (different real teams) silently resolving to the SAME team_id. The one
    known exception is a genuine duplicate row in the source CSV itself
    (same slug appears twice) -- not a resolution bug.
    """
    from collections import defaultdict

    by_team_id = defaultdict(set)
    for slug, rank in zip(net_rankings_df["team_slug"], net_rankings_df["net_rank"]):
        result = resolve_net_team_identity(str(slug))
        if isinstance(result, TeamIdentity):
            by_team_id[result.team_id].add(str(slug))

    cross_team_collisions = {tid: slugs for tid, slugs in by_team_id.items() if len(slugs) > 1}
    assert not cross_team_collisions, f"different slugs resolved to the same team_id: {cross_team_collisions}"


def test_real_net_rankings_csv_has_a_known_genuine_duplicate_row(net_rankings_df):
    """Documents the real, pre-existing duplicate ranking row (same slug,
    two different ranks) found in the fixture -- a data-quality issue in
    the source file, not something this adapter introduces or hides."""
    counts = net_rankings_df["team_slug"].value_counts()
    duplicated = counts[counts > 1]
    assert "louisiana-state" in duplicated.index


def test_unmapped_net_teams_are_recorded_with_reasons(net_rankings_df):
    _, unmapped = build_net_rankings_index(net_rankings_df)
    assert unmapped, "expected at least one genuinely unmapped team in the real fixture"
    for entry in unmapped:
        assert entry["reason"] in ("not_found", "ambiguous")
        assert "raw_key" in entry


# ---------------------------------------------------------------------------
# build_net_rankings_from_ncaa_v15: DataFrame mode, list mode
# ---------------------------------------------------------------------------

def test_build_net_rankings_from_ncaa_v15_requires_fetch_fn():
    with pytest.raises(ValidationError):
        build_net_rankings_from_ncaa_v15("http://example.invalid", None)


def test_build_net_rankings_from_ncaa_v15_raw_mode_returns_pipeline_slug_output():
    result = build_net_rankings_from_ncaa_v15(
        "http://example.invalid", None, fetch_fn=_fake_fetch
    )
    assert list(result.columns) == ["team_slug", "net_rank"]
    assert result.iloc[0]["team_slug"] == "duke"
    assert result.iloc[0]["net_rank"] == 1


def test_build_net_rankings_from_ncaa_v15_list_mode_uses_canonical_set_as_key_space():
    canonical_slugs = ["duke", "alabama-birmingham", "texas-christian", "miami-fl", "saint-josephs"]
    result = build_net_rankings_from_ncaa_v15(
        "http://example.invalid", canonical_slugs, fetch_fn=_fake_fetch
    )
    resolved_slugs = set(result["team_slug"])
    # UAB/TCU/Miami FL should resolve via NCAA_NET_TO_TEAM_SLUG_FALLBACK
    # into the supplied canonical list's own slug forms.
    assert "alabama-birmingham" in resolved_slugs
    assert "texas-christian" in resolved_slugs
    assert "miami-fl" in resolved_slugs


def test_build_net_rankings_from_ncaa_v15_dataframe_mode_with_real_schools_fixture(all_schools_df):
    """
    Uses the real cached SR schools index (including its 23 known
    repeated-header garbage rows, per the Sports-Reference/DataFrame
    batch) as the crosswalk source -- confirms this mode doesn't crash on
    that pre-existing data-quality issue. Not fixing that bug here, per
    this batch's explicit instruction; just confirming it isn't fatal.
    """
    result = build_net_rankings_from_ncaa_v15(
        "http://example.invalid", all_schools_df, fetch_fn=_fake_fetch
    )
    assert set(result.columns) == {"team_slug", "net_rank"}
    assert len(result) > 0


def test_ensure_sr_slug_column_v15_raises_on_missing_team_slug_column():
    with pytest.raises(KeyError):
        ensure_sr_slug_column_v15(pd.DataFrame({"not_team_slug": ["x"]}))


def test_build_sr_to_team_slug_crosswalk_v15_raises_on_missing_columns():
    with pytest.raises(KeyError):
        build_sr_to_team_slug_crosswalk_v15(pd.DataFrame({"team_slug": ["x"]}))


def test_build_sr_to_team_slug_crosswalk_v15_handles_collisions_by_keeping_first():
    df = pd.DataFrame({
        "sr_slug": ["dup-slug", "dup-slug", "unique-slug"],
        "team_slug": ["first-team", "second-team", "unique-team"],
    })
    crosswalk = build_sr_to_team_slug_crosswalk_v15(df)
    assert crosswalk["dup-slug"] == "first-team"
    assert crosswalk["unique-slug"] == "unique-team"


# ---------------------------------------------------------------------------
# attach_net_rank_v15 (legacy) / attach_net_rank_via_registry (canonical)
# ---------------------------------------------------------------------------

def test_attach_net_rank_v15_direct_lookup_dict_mode():
    profile = {"team": "Duke", "team_slug": "duke"}
    result = attach_net_rank_v15(profile, {"duke": 1})
    assert result["net_rank"] == 1
    assert "net_rating" in result


def test_attach_net_rank_v15_dataframe_mode():
    profile = {"team": "Duke", "team_slug": "duke"}
    df = pd.DataFrame({"team_slug": ["duke"], "net_rank": [1]})
    result = attach_net_rank_v15(profile, df)
    assert result["net_rank"] == 1


def test_attach_net_rank_v15_missing_ranking_gives_up_gracefully():
    profile = {"team": "Nowhere State", "team_slug": "nowhere-state-xyz"}
    result = attach_net_rank_v15(profile, {"duke": 1})
    assert result["net_rank"] is None


def test_attach_net_rank_via_registry_looks_up_by_team_id():
    rankings_by_team_id = {123: 5}
    assert attach_net_rank_via_registry(123, rankings_by_team_id) == 5
    assert attach_net_rank_via_registry(999, rankings_by_team_id) is None


def test_attach_net_rank_via_registry_end_to_end_with_real_csv(net_rankings_df):
    rankings_by_team_id, _ = build_net_rankings_index(net_rankings_df)
    sbu = resolve_net_team_identity("st-bonaventure")
    assert isinstance(sbu, TeamIdentity)
    rank = attach_net_rank_via_registry(sbu.team_id, rankings_by_team_id)
    assert rank is not None
    assert isinstance(rank, int)


# ---------------------------------------------------------------------------
# resolve_net_team_sr_slug_v15 -- legacy compat, including its Miami hardcode
# ---------------------------------------------------------------------------

def test_resolve_net_team_sr_slug_v15_hardcodes_bare_miami_to_florida():
    index = {"miami-fl", "miami-oh", "duke"}
    assert resolve_net_team_sr_slug_v15("Miami", index) == "miami-fl"


def test_resolve_net_team_sr_slug_v15_returns_none_for_unresolvable():
    index = {"duke", "kansas"}
    assert resolve_net_team_sr_slug_v15("Not A Real Team", index) is None


# ---------------------------------------------------------------------------
# Cross-adapter isolation
# ---------------------------------------------------------------------------

def test_net_ranking_data_does_not_mutate_the_general_canonical_alias_table():
    """
    NCAA_NET_TO_TEAM_SLUG_FALLBACK and slug_utils._DISPLAY_NAME_ALIASES are
    two independently-curated tables -- some coincidental key overlap
    (e.g. both happen to alias "uab") is expected and fine. What matters
    is that this module never merges its own table INTO slug_utils's, i.e.
    they stay genuinely separate dict objects with slug_utils's own size
    unaffected by importing/using this module.
    """
    from src import slug_utils

    before_size = len(slug_utils._DISPLAY_NAME_ALIASES)
    assert NCAA_NET_TO_TEAM_SLUG_FALLBACK is not slug_utils._DISPLAY_NAME_ALIASES
    resolve_net_team_identity("uab")  # exercise the module
    assert len(slug_utils._DISPLAY_NAME_ALIASES) == before_size


def test_net_resolution_does_not_affect_espn_or_covers_adapters():
    from src.identity.covers_identity import resolve_covers_team_identity
    from src.identity.espn_identity import resolve_espn_team_identity

    espn_result = resolve_espn_team_identity("St. Bonaventure Bonnies")
    covers_result = resolve_covers_team_identity("st-bonaventure-bonnies")
    assert isinstance(espn_result, TeamIdentity) and espn_result.display_name == "St. Bonaventure"
    assert isinstance(covers_result, TeamIdentity) and covers_result.display_name == "St. Bonaventure"
