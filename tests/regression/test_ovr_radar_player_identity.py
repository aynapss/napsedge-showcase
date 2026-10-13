"""
Regression tests for src/identity/ovr_radar_player_identity.py (Phase 2B, batch 6).

No live requests: uses small synthetic DataFrames for most cases, plus the
real local ESPN fixture (TeamDump/ESPN/espn_full_player_stats.csv) to prove
the same-name collision finding against real data.
"""
from pathlib import Path

import pandas as pd
import pytest

from src.identity.contracts import PlayerIdentity, TeamIdentity, UnresolvedIdentity
from src.identity.player_identity import build_player_identity, normalize_allstats_player_v15
from src.identity.ovr_radar_player_identity import (
    find_ambiguous_radar_allstats_rows,
    find_duplicate_espn_radar_names,
    find_espn_radar_row_v15,
    find_espn_stat_row,
    resolve_ovr_radar_player_context,
    resolve_radar_allstats_row_v15,
    resolve_radar_espn_identity,
    resolve_radar_stat_row_identity,
)

ESPN_FIXTURE_PATH = Path("TeamDump/ESPN/espn_full_player_stats.csv")


# ---------------------------------------------------------------------------
# v15 compat wrappers: verbatim legacy behavior, including its known risks
# ---------------------------------------------------------------------------

def test_resolve_radar_allstats_row_v15_exact_match():
    df_team = pd.DataFrame({"Players": ["Frank Mitchell", "Cayden Charles"], "PTS": [18, 12]})
    row = resolve_radar_allstats_row_v15(df_team, "Frank Mitchell")
    assert row["Players"] == "Frank Mitchell"


def test_resolve_radar_allstats_row_v15_case_insensitive_fallback():
    df_team = pd.DataFrame({"Players": ["Frank Mitchell"], "PTS": [18]})
    row = resolve_radar_allstats_row_v15(df_team, "frank mitchell")
    assert row["Players"] == "Frank Mitchell"


def test_resolve_radar_allstats_row_v15_raises_when_not_found():
    df_team = pd.DataFrame({"Players": ["Frank Mitchell"], "PTS": [18]})
    with pytest.raises(ValueError):
        resolve_radar_allstats_row_v15(df_team, "Someone Else")


def test_resolve_radar_allstats_row_v15_silently_picks_first_on_ambiguous_names():
    """
    Documents the legacy defect exactly: two rows share the same "Players"
    string, and the v15 function silently returns the first with no error.
    """
    df_team = pd.DataFrame({"Players": ["John Smith", "John Smith"], "PTS": [10, 30]})
    row = resolve_radar_allstats_row_v15(df_team, "John Smith")
    assert row["PTS"] == 10  # first row wins, silently -- the confirmed defect


def test_find_ambiguous_radar_allstats_rows_detects_the_gap():
    df_team = pd.DataFrame({"Players": ["John Smith", "John Smith"], "PTS": [10, 30]})
    assert find_ambiguous_radar_allstats_rows(df_team, "John Smith") == 2
    assert find_ambiguous_radar_allstats_rows(df_team, "Nobody") == 0

    df_clean = pd.DataFrame({"Players": ["Frank Mitchell"], "PTS": [18]})
    assert find_ambiguous_radar_allstats_rows(df_clean, "Frank Mitchell") == 1


def test_find_espn_radar_row_v15_exact_and_tiers():
    df_espn = pd.DataFrame({"Name": ["Frank Mitchell", "A. Marshall"], "Team": ["SBU", "SBU"]})
    assert find_espn_radar_row_v15(df_espn, "Frank Mitchell")["Name"] == "Frank Mitchell"
    assert find_espn_radar_row_v15(df_espn, "frank mitchell")["Name"] == "Frank Mitchell"
    # tier 4 (first-initial + last-name) intentionally matches an initial-form ESPN entry
    # against a full first name sharing the same initial and exact last name.
    assert find_espn_radar_row_v15(df_espn, "Amare Marshall")["Name"] == "A. Marshall"


def test_find_espn_radar_row_v15_tier4_initial_lastname_can_collide():
    """
    Documents the real collision risk: two DIFFERENT players sharing a
    first initial + last name, on DIFFERENT teams, silently resolve to
    whichever sorts first -- no team scoping, no ambiguity check.
    """
    df_espn = pd.DataFrame({
        "Name": ["Josh Smith", "Jordan Smith"],
        "Team": ["TeamA", "TeamB"],
    })
    result = find_espn_radar_row_v15(df_espn, "J. Smith")
    assert result is not None
    assert result["Name"] == "Josh Smith"  # first match wins silently -- the confirmed defect
    assert result["Team"] == "TeamA"  # could easily be the wrong team's player


def test_find_espn_radar_row_v15_missing_player_returns_none_cleanly():
    df_espn = pd.DataFrame({"Name": ["Frank Mitchell"], "Team": ["SBU"]})
    assert find_espn_radar_row_v15(df_espn, "Nobody Here") is None


def test_find_espn_radar_row_v15_no_accent_stripping():
    """
    Confirms the documented gap: tier 3's punctuation-only regex deletes an
    accented character rather than folding it to its unaccented ASCII form
    (unlike the canonical core's NFD-based normalize_player_name_v15), and
    tier 4's last-name comparison is byte-exact, so an accented surname
    that differs from its unaccented spelling fails to match at every
    tier. (Names sharing an *unaccented* last name would still match via
    tier 4 regardless of a first-name accent -- this case isolates the
    surname itself to prove the gap.)
    """
    df_espn = pd.DataFrame({"Name": ["Frank Pena"], "Team": ["SBU"]})
    assert find_espn_radar_row_v15(df_espn, "Frank Peña") is None


# ---------------------------------------------------------------------------
# Canonical AllStats-row identity resolution
# ---------------------------------------------------------------------------

def test_resolve_radar_stat_row_identity_exact_without_roster():
    result = resolve_radar_stat_row_identity("Frank Mitchell", "St. Bonaventure", 2026)
    assert isinstance(result, PlayerIdentity)
    assert result.team_id == 905884


def test_resolve_radar_stat_row_identity_unknown_team():
    result = resolve_radar_stat_row_identity("Frank Mitchell", "Not A Real Team", 2026)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "not_found"


def test_resolve_radar_stat_row_identity_ambiguous_team():
    result = resolve_radar_stat_row_identity("Someone", "Miami", 2026)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "ambiguous"


def test_resolve_radar_stat_row_identity_ambiguous_roster_suffix_conflict():
    roster = [
        build_player_identity("Daryl Simmons", 905884, 2026),
        build_player_identity("Daryl Simmons II", 905884, 2026),
    ]
    result = resolve_radar_stat_row_identity("Daryl Simmons", "St. Bonaventure", 2026, roster_identities=roster)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "ambiguous"


def test_resolve_radar_stat_row_identity_reuses_sr_source_id():
    roster = [build_player_identity("Frank Mitchell", 905884, 2026, source="sports_reference", source_player_id="mitchell-1")]
    result = resolve_radar_stat_row_identity("Frank Mitchell", "St. Bonaventure", 2026, roster_identities=roster)
    assert isinstance(result, PlayerIdentity)
    assert result.player_id == "sports_reference:mitchell-1"


def test_resolve_radar_stat_row_identity_transfer_scoping():
    before = resolve_radar_stat_row_identity("Jane Doe", "St. Bonaventure", 2025)
    after = resolve_radar_stat_row_identity("Jane Doe", "VCU", 2026)
    assert before.player_id != after.player_id


def test_resolve_radar_stat_row_identity_transfer_with_source_id_stable():
    before = resolve_radar_stat_row_identity("Jane Doe", "St. Bonaventure", 2025, source_player_id="jd-1")
    after = resolve_radar_stat_row_identity("Jane Doe", "VCU", 2026, source_player_id="jd-1")
    assert before.player_id == after.player_id


def test_resolve_radar_stat_row_identity_deterministic():
    ids = {resolve_radar_stat_row_identity("Frank Mitchell", "St. Bonaventure", 2026).player_id for _ in range(3)}
    assert len(ids) == 1


@pytest.mark.parametrize("suffixed", ["Darius Acuff Jr.", "John Smith Sr.", "John Smith II", "John Smith III", "John Smith IV"])
def test_resolve_radar_stat_row_identity_all_suffix_forms(suffixed):
    result = resolve_radar_stat_row_identity(suffixed, "St. Bonaventure", 2026)
    assert isinstance(result, PlayerIdentity)
    assert result.display_name == suffixed


def test_resolve_radar_stat_row_identity_accents_and_hyphens_preserved():
    a = resolve_radar_stat_row_identity("Amar'e Marshall", "St. Bonaventure", 2026)
    b = resolve_radar_stat_row_identity("Jean-Paul Olukemi", "St. Bonaventure", 2026)
    assert a.display_name == "Amar'e Marshall"
    assert b.display_name == "Jean-Paul Olukemi"


def test_resolve_radar_stat_row_identity_compound_surname_preserved():
    result = resolve_radar_stat_row_identity("Van Der Berg", "St. Bonaventure", 2026)
    assert isinstance(result, PlayerIdentity)
    assert result.display_name == "Van Der Berg"


def test_resolve_radar_stat_row_identity_initials_distinct_from_full_name():
    a = resolve_radar_stat_row_identity("A. Marshall", "St. Bonaventure", 2026)
    b = resolve_radar_stat_row_identity("Amare Marshall", "St. Bonaventure", 2026)
    assert a.player_id != b.player_id


# ---------------------------------------------------------------------------
# Canonical ESPN row lookup (safe replacement for _find_espn_row)
# ---------------------------------------------------------------------------

def test_find_espn_stat_row_unique_match():
    espn_df = pd.DataFrame({"Name": ["Frank Mitchell"], "Team": ["SBU"]})
    row, method = find_espn_stat_row(espn_df, "Frank Mitchell")
    assert method == "unique"
    assert row["Name"] == "Frank Mitchell"


def test_find_espn_stat_row_not_found():
    espn_df = pd.DataFrame({"Name": ["Frank Mitchell"], "Team": ["SBU"]})
    row, method = find_espn_stat_row(espn_df, "Nobody")
    assert method == "not_found"
    assert row is None


def test_find_espn_stat_row_ambiguous_without_team_scoping():
    """
    Same collision that find_espn_radar_row_v15 resolves silently -- the
    canonical version returns "ambiguous" instead of a silent pick.
    """
    espn_df = pd.DataFrame({"Name": ["Josh Smith", "Josh Smith"], "Team": ["TeamA", "TeamB"]})
    row, method = find_espn_stat_row(espn_df, "Josh Smith")
    assert method == "ambiguous"
    assert row is None


def test_find_espn_stat_row_disambiguated_by_espn_team_code():
    """Real Team column data (present but unused by the legacy closure) resolves the same collision safely."""
    espn_df = pd.DataFrame({"Name": ["Josh Smith", "Josh Smith"], "Team": ["TeamA", "TeamB"]})
    row, method = find_espn_stat_row(espn_df, "Josh Smith", espn_team_code="TeamB")
    assert method == "unique"
    assert row["Team"] == "TeamB"


def test_find_espn_stat_row_suffix_agnostic_via_canonical_key():
    espn_df = pd.DataFrame({"Name": ["Daryl Simmons II"], "Team": ["SBU"]})
    row, method = find_espn_stat_row(espn_df, "Daryl Simmons")
    assert method == "unique"
    assert row["Name"] == "Daryl Simmons II"


def test_find_espn_stat_row_deterministic_repeated():
    espn_df = pd.DataFrame({"Name": ["Frank Mitchell"], "Team": ["SBU"]})
    results = {find_espn_stat_row(espn_df, "Frank Mitchell")[1] for _ in range(3)}
    assert results == {"unique"}


# ---------------------------------------------------------------------------
# Canonical ESPN identity resolution (reused from batch 2, not reinvented)
# ---------------------------------------------------------------------------

def test_resolve_radar_espn_identity_reuses_athlete_id():
    result = resolve_radar_espn_identity("Frank Mitchell", "St. Bonaventure", 2026, espn_athlete_id="500")
    assert isinstance(result, PlayerIdentity)
    assert result.player_id == "espn_radar:500"


def test_resolve_radar_espn_identity_missing_athlete_id_falls_back_to_scoped_key():
    result = resolve_radar_espn_identity("Frank Mitchell", "St. Bonaventure", 2026)
    assert isinstance(result, PlayerIdentity)
    assert "espn_radar" not in result.sources


def test_resolve_radar_espn_identity_unknown_team_unresolved():
    result = resolve_radar_espn_identity("Frank Mitchell", "Not A Real Team", 2026)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "not_found"


# ---------------------------------------------------------------------------
# Combined context resolution: preserves missing ESPN records explicitly
# ---------------------------------------------------------------------------

def test_resolve_ovr_radar_player_context_full_success():
    df_team = pd.DataFrame({"Players": ["Frank Mitchell"], "PTS": [18]})
    espn_df = pd.DataFrame({"Name": ["Frank Mitchell"], "Team": ["SBU"]})
    ctx = resolve_ovr_radar_player_context(
        "Frank Mitchell", "St. Bonaventure", 2026, df_team=df_team, espn_df=espn_df, espn_team_code="SBU"
    )
    assert isinstance(ctx["allstats_identity"], PlayerIdentity)
    assert ctx["allstats_row_ambiguous"] is False
    assert isinstance(ctx["espn_identity"], PlayerIdentity)
    assert ctx["espn_method"] == "unique"
    assert ctx["espn_row"]["Name"] == "Frank Mitchell"


def test_resolve_ovr_radar_player_context_missing_espn_is_explicit_not_dropped():
    df_team = pd.DataFrame({"Players": ["Frank Mitchell"], "PTS": [18]})
    espn_df = pd.DataFrame({"Name": ["Someone Else"], "Team": ["SBU"]})
    ctx = resolve_ovr_radar_player_context(
        "Frank Mitchell", "St. Bonaventure", 2026, df_team=df_team, espn_df=espn_df
    )
    assert ctx["espn_method"] == "not_found"
    assert ctx["espn_row"] is None
    assert "espn_identity" in ctx  # field present even though unmatched -- not silently omitted
    assert isinstance(ctx["allstats_identity"], PlayerIdentity)  # AllStats side still resolved independently


def test_resolve_ovr_radar_player_context_flags_allstats_ambiguity():
    df_team = pd.DataFrame({"Players": ["John Smith", "John Smith"], "PTS": [10, 30]})
    ctx = resolve_ovr_radar_player_context("John Smith", "St. Bonaventure", 2026, df_team=df_team)
    assert ctx["allstats_row_ambiguous"] is True


def test_resolve_ovr_radar_player_context_without_any_dataframes():
    ctx = resolve_ovr_radar_player_context("Frank Mitchell", "St. Bonaventure", 2026)
    assert isinstance(ctx["allstats_identity"], PlayerIdentity)
    assert ctx["allstats_row_ambiguous"] is False
    assert ctx["espn_method"] == "not_found"
    assert ctx["espn_row"] is None


# ---------------------------------------------------------------------------
# The documented normalize_allstats_player suffix defect, and its confirmed
# reach into apply_injury_penalty / get_radar_roster_lastnames (objective #9)
# ---------------------------------------------------------------------------

def test_normalize_allstats_player_suffix_bug_confirmed():
    assert normalize_allstats_player_v15("Daryl Simmons II") == "ii"  # NOT "simmons"


def test_injury_and_radar_lastname_keys_cannot_align_for_suffixed_players():
    """
    Demonstrates the confirmed cross-system mismatch: normalize_covers_player
    (injury side, correct) and normalize_allstats_player (apply_injury_penalty's
    OVR-side gate, buggy) produce DIFFERENT keys for the same real player,
    so apply_injury_penalty's `.map(inactive_map)` can never match a
    suffixed player's true injury status. Formula/gate logic itself is not
    touched -- this test only proves the documented finding.
    """
    from src.identity.injury_player_identity import _last_name_key

    injury_side_key = _last_name_key("Daryl Simmons II")
    radar_side_key = normalize_allstats_player_v15("Daryl Simmons II")
    assert injury_side_key != radar_side_key
    assert radar_side_key == "ii"


# ---------------------------------------------------------------------------
# Real-fixture collision census
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not ESPN_FIXTURE_PATH.exists(), reason="real ESPN fixture not present in this checkout")
def test_find_duplicate_espn_radar_names_against_real_fixture():
    espn_df = pd.read_csv(ESPN_FIXTURE_PATH)
    dupes = find_duplicate_espn_radar_names(espn_df)
    # Confirmed during investigation: at least these real duplicate names exist.
    assert "Josh Smith" in dupes
    assert dupes["Josh Smith"] == 2
    assert len(dupes) >= 13


@pytest.mark.skipif(not ESPN_FIXTURE_PATH.exists(), reason="real ESPN fixture not present in this checkout")
def test_real_fixture_duplicate_name_is_ambiguous_via_canonical_path_but_silent_via_v15():
    espn_df = pd.read_csv(ESPN_FIXTURE_PATH)
    v15_result = find_espn_radar_row_v15(espn_df, "Josh Smith")
    assert v15_result is not None  # silently resolves to whichever row sorts first

    canonical_row, canonical_method = find_espn_stat_row(espn_df, "Josh Smith")
    assert canonical_method == "ambiguous"
    assert canonical_row is None


# ---------------------------------------------------------------------------
# Duplicate lookup keys / repeated resolution determinism
# ---------------------------------------------------------------------------

def test_find_duplicate_espn_radar_names_empty_when_clean():
    espn_df = pd.DataFrame({"Name": ["Frank Mitchell", "Cayden Charles"], "Team": ["SBU", "SBU"]})
    assert find_duplicate_espn_radar_names(espn_df) == {}


def test_find_duplicate_espn_radar_names_suffix_agnostic():
    espn_df = pd.DataFrame({"Name": ["Daryl Simmons", "Daryl Simmons II"], "Team": ["SBU", "SBU"]})
    dupes = find_duplicate_espn_radar_names(espn_df)
    assert len(dupes) == 1


# ---------------------------------------------------------------------------
# Compatibility / isolation
# ---------------------------------------------------------------------------

def test_ovr_radar_module_does_not_import_ovr_model_or_slug_utils_directly():
    import ast
    import inspect

    import src.identity.ovr_radar_player_identity as mod

    tree = ast.parse(inspect.getsource(mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert not any("ovr_model" in name for name in imported)
    assert "src.slug_utils" not in imported


def test_ovr_radar_resolution_does_not_mutate_team_registry():
    from src import slug_utils

    before_size = len(slug_utils.TEAM_REGISTRY)
    before_aliases = dict(slug_utils._DISPLAY_NAME_ALIASES)
    resolve_radar_stat_row_identity("Frank Mitchell", "St. Bonaventure", 2026)
    resolve_radar_espn_identity("Frank Mitchell", "St. Bonaventure", 2026)
    assert len(slug_utils.TEAM_REGISTRY) == before_size
    assert slug_utils._DISPLAY_NAME_ALIASES == before_aliases
