"""
Regression tests for src/identity/sr_dataframe_identity.py
(Phase 2A, Sports-Reference/DataFrame adapter).

Uses the REAL cached Sports-Reference schools index
(cache/sportsref_cbb_schools.parquet, 519 rows) as the "authoritative
Division I DataFrame" fixture -- no live scraping, no reimplemented data.
This is the same file cbb_teamScrape_v15_TEST.ipynb's fetch_all_schools()
reads from disk when force_refresh=False.
"""
import os

import pandas as pd
import pytest

from src.exceptions import ValidationError
from src.identity.contracts import TeamIdentity
from src.identity.sr_dataframe_identity import (
    find_sr_slug_mismatches,
    normalize_team_search_name_v15,
    resolve_sr_team_identity,
    resolve_team_name_v15_verbatim,
    validate_schools_df,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCHOOLS_PARQUET = os.path.join(REPO_ROOT, "cache", "sportsref_cbb_schools.parquet")

pytestmark = pytest.mark.skipif(
    not os.path.isfile(SCHOOLS_PARQUET),
    reason="cache/sportsref_cbb_schools.parquet fixture not present",
)


@pytest.fixture(scope="module")
def all_schools_df():
    return pd.read_parquet(SCHOOLS_PARQUET)


# ---------------------------------------------------------------------------
# Exact names/slugs, punctuation, apostrophes, Saint/St., directional abbrevs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query,expected_display_name", [
    ("St. Bonaventure", "St. Bonaventure"),
    ("St Bonaventure", "St. Bonaventure"),
    ("Bonnies", "St. Bonaventure"),
    ("Saint Joseph's", "Saint Joseph's"),
    ("St. Joseph's", "Saint Joseph's"),
    ("St Joes", "Saint Joseph's"),
    ("Saint Louis", "Saint Louis"),
    ("La Salle", "La Salle"),
    ("Lasalle", "La Salle"),
    ("George Washington", "George Washington"),
    ("GW", "George Washington"),
    ("Rhode Island", "Rhode Island"),
    ("URI", "Rhode Island"),
    ("UNC Wilmington", "UNC Wilmington"),  # directional abbreviation
])
def test_known_names_and_variants_resolve_correctly(all_schools_df, query, expected_display_name):
    result = resolve_sr_team_identity(query, all_schools_df)
    assert isinstance(result, TeamIdentity), f"{query!r} should resolve, got {result}"
    assert result.display_name == expected_display_name


@pytest.mark.parametrize("query,expected_display_name", [
    ("Miami (FL)", "Miami (FL)"),
    ("Miami (OH)", "Miami (OH)"),
    ("Loyola Chicago", "Loyola Chicago"),
    ("Loyola (MD)", "Loyola (MD)"),
])
def test_miami_and_loyola_disambiguation(all_schools_df, query, expected_display_name):
    result = resolve_sr_team_identity(query, all_schools_df)
    assert isinstance(result, TeamIdentity)
    assert result.display_name == expected_display_name


def test_bare_ambiguous_miami_still_flagged_ambiguous_with_df_supplied(all_schools_df):
    result = resolve_sr_team_identity("Miami", all_schools_df)
    assert not isinstance(result, TeamIdentity)
    assert result.reason == "ambiguous"


# ---------------------------------------------------------------------------
# Name/slug disagreement (the primary finding of this batch)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query,registry_slug,verified_slug", [
    ("UAB", "alabama-birmingham", "uab"),
    ("NC State", "north-carolina-state", "nc-state"),
    ("TCU", "texas-christian", "tcu"),
])
def test_registry_and_sr_index_slug_disagreement_is_surfaced_not_hidden(
    all_schools_df, query, registry_slug, verified_slug
):
    result = resolve_sr_team_identity(query, all_schools_df)
    assert isinstance(result, TeamIdentity)
    assert result.sources["sports_reference"].raw_value == registry_slug
    assert result.sources["sports_reference_verified"].raw_value == verified_slug
    assert registry_slug != verified_slug  # confirms this IS a real disagreement case


def test_agreeing_team_has_matching_registry_and_verified_slugs(all_schools_df):
    result = resolve_sr_team_identity("St. Bonaventure", all_schools_df)
    assert isinstance(result, TeamIdentity)
    assert (
        result.sources["sports_reference"].raw_value
        == result.sources["sports_reference_verified"].raw_value
        == "st-bonaventure"
    )


def test_find_sr_slug_mismatches_reports_the_known_disagreements(all_schools_df):
    mismatches = find_sr_slug_mismatches(all_schools_df)
    display_names = {m["display_name"] for m in mismatches}
    assert "Alabama-Birmingham" in display_names
    assert "North Carolina State" in display_names
    for m in mismatches:
        assert m["registry_sr_slug"]  # every mismatch has a value to compare against


# ---------------------------------------------------------------------------
# Garbage/duplicate rows (real, confirmed-present in the fixture)
# ---------------------------------------------------------------------------

def test_fixture_has_confirmed_duplicate_header_garbage_rows(all_schools_df):
    """Documents the real data-quality bug this adapter defends against:
    SR's paginated table repeats its header row; fetch_all_schools's scrape
    captured 23 of them as if they were real teams."""
    garbage = all_schools_df[all_schools_df["team_name"].str.lower() == "school"]
    assert len(garbage) > 0, "expected the known repeated-header rows in this fixture"


def test_garbage_rows_never_satisfy_a_real_query(all_schools_df):
    result = resolve_sr_team_identity("School", all_schools_df)
    assert not isinstance(result, TeamIdentity)


def test_duplicate_real_team_rows_use_first_match_deterministically():
    duplicated_df = pd.DataFrame({
        "team_name": ["St. Bonaventure", "St. Bonaventure"],
        "team_slug": ["st-bonaventure", "st-bonaventure-DUPLICATE"],
    })
    result = resolve_sr_team_identity("St. Bonaventure", duplicated_df)
    assert isinstance(result, TeamIdentity)
    assert result.sources["sports_reference_verified"].raw_value == "st-bonaventure"


# ---------------------------------------------------------------------------
# Missing required columns / empty df
# ---------------------------------------------------------------------------

def test_validate_schools_df_rejects_empty_dataframe():
    with pytest.raises(ValidationError):
        validate_schools_df(pd.DataFrame())


def test_validate_schools_df_rejects_missing_required_columns():
    with pytest.raises(ValidationError, match="team_slug"):
        validate_schools_df(pd.DataFrame({"team_name": ["Davidson"]}))


def test_resolve_sr_team_identity_without_df_still_resolves_via_registry_only():
    result = resolve_sr_team_identity("St. Bonaventure", all_schools_df=None)
    assert isinstance(result, TeamIdentity)
    assert "sports_reference_verified" not in result.sources


# ---------------------------------------------------------------------------
# Unknown teams / determinism
# ---------------------------------------------------------------------------

def test_unknown_team_is_unresolved(all_schools_df):
    result = resolve_sr_team_identity("Not A Real Team Xyz", all_schools_df)
    assert not isinstance(result, TeamIdentity)
    assert result.reason == "not_found"


def test_repeated_resolution_is_deterministic(all_schools_df):
    ids = {resolve_sr_team_identity("VCU", all_schools_df).team_id for _ in range(3)}
    assert len(ids) == 1


# ---------------------------------------------------------------------------
# Compatibility wrapper (byte-faithful v15 behavior, fuzzy fallback included)
# ---------------------------------------------------------------------------

def test_legacy_wrapper_returns_pandas_series_matching_original_contract(all_schools_df):
    row = resolve_team_name_v15_verbatim("St. Bonaventure", all_schools_df)
    assert isinstance(row, pd.Series)
    assert row["team_name"] == "St. Bonaventure"
    assert row["team_slug"] == "st-bonaventure"


def test_legacy_wrapper_raises_valueerror_on_empty_df():
    with pytest.raises(ValueError):
        resolve_team_name_v15_verbatim("Davidson", pd.DataFrame())


def test_legacy_wrapper_raises_keyerror_on_missing_columns():
    with pytest.raises(KeyError):
        resolve_team_name_v15_verbatim("Davidson", pd.DataFrame({"team_name": ["Davidson"]}))


def test_legacy_wrapper_returns_none_for_unknown_team(all_schools_df):
    assert resolve_team_name_v15_verbatim("Zzznotarealteam99xyz", all_schools_df) is None


def test_normalize_team_search_name_v15_uses_live_display_name_aliases():
    # Confirms TEAM_NAME_CORRECTIONS really is src.slug_utils._DISPLAY_NAME_ALIASES
    # at runtime (cell 15's backward-compat alias), not a separate/dead dict.
    assert normalize_team_search_name_v15("st josephs") == "Saint Joseph's"
    assert normalize_team_search_name_v15("St Johns") == "St. John's (NY)"


# ---------------------------------------------------------------------------
# Full 365-team coverage
# ---------------------------------------------------------------------------

def test_every_registered_team_resolves_by_its_own_display_name(all_schools_df):
    from src import slug_utils
    from src.identity.team_identity import ensure_team_registry

    ensure_team_registry()
    failures = []
    for rec in slug_utils.TEAM_REGISTRY.values():
        result = resolve_sr_team_identity(rec.display_name, all_schools_df)
        if not isinstance(result, TeamIdentity) or result.team_id != rec.team_id:
            failures.append(rec.display_name)
    assert not failures, f"{len(failures)} registered teams failed to resolve by their own display name: {failures}"
