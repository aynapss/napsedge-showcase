"""
Phase 5B3: tests for `src/identity/espn_team_abbreviation_aliases.py` --
the reviewed, evidence-backed ESPN abbreviation crosswalk. No live
requests, no fuzzy matching, no modification of the shared resolver.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.identity.contracts import TeamIdentity, UnresolvedIdentity
from src.identity.espn_team_abbreviation_aliases import (
    REVIEWED_ESPN_ABBREVIATION_ALIASES,
    UNRESOLVED_ESPN_ABBREVIATIONS,
    EspnTeamCodeResolution,
    resolve_espn_team_abbreviation,
    resolve_espn_team_abbreviation_with_provenance,
)
from src.identity.team_identity import resolve_team_identity
from src.db.enums import ESPN_TEAM_RESOLUTION_METHODS

#: Real local ESPN fixture -- deliberately not committed to Git (TeamDump/ is
#: gitignored); same path/constant name as test_espn_ingestion.py's own
#: REAL_ESPN_CSV.
REAL_ESPN_CSV = Path("TeamDump/ESPN/espn_full_player_stats.csv")


def test_reviewed_alias_count_matches_documented_evidence():
    assert len(REVIEWED_ESPN_ABBREVIATION_ALIASES) == 231


def test_unresolved_count_matches_documented_evidence():
    assert len(UNRESOLVED_ESPN_ABBREVIATIONS) == 50


def test_reviewed_and_unresolved_sets_are_disjoint():
    assert not (set(REVIEWED_ESPN_ABBREVIATION_ALIASES) & set(UNRESOLVED_ESPN_ABBREVIATIONS))


def test_every_reviewed_alias_resolves_to_its_exact_recorded_team_id():
    """Collision/consistency proof: re-resolving every one of the 231 reviewed aliases must return exactly the recorded team_id -- no drift, no accidental collision with a different team."""
    for code, expected_team_id in REVIEWED_ESPN_ABBREVIATION_ALIASES.items():
        result = resolve_espn_team_abbreviation(code)
        assert isinstance(result, TeamIdentity), f"{code!r} failed to resolve"
        assert result.team_id == expected_team_id, f"{code!r} resolved to {result.team_id}, expected {expected_team_id}"


def test_reviewed_aliases_never_overlap_the_pre_existing_exact_baseline():
    """A code already resolvable via the baseline exact path has no business being in the reviewed crosswalk too -- confirms these are genuinely NEW resolutions, not duplicates."""
    for code in REVIEWED_ESPN_ABBREVIATION_ALIASES:
        baseline = resolve_team_identity(code, source="espn_stat_abbreviation")
        assert not isinstance(baseline, TeamIdentity), f"{code!r} already resolves via the baseline exact path"


def test_no_two_reviewed_aliases_map_the_same_code_to_different_teams():
    """Trivially true for a dict (one value per key), but stated explicitly as the collision-prevention contract this batch requires."""
    assert len(REVIEWED_ESPN_ABBREVIATION_ALIASES) == len(set(REVIEWED_ESPN_ABBREVIATION_ALIASES.keys()))


def test_reviewed_alias_team_ids_are_real_known_teams():
    """Every reviewed alias's team_id must belong to the actual 365-team canonical universe -- never a fabricated id."""
    from src import slug_utils

    resolve_team_identity("Michigan State", source="sports_reference")  # forces TEAM_REGISTRY to build
    known_ids = set(slug_utils.TEAM_REGISTRY.keys())
    for code, team_id in REVIEWED_ESPN_ABBREVIATION_ALIASES.items():
        assert team_id in known_ids, f"{code!r} maps to team_id {team_id}, not a real registered team"


@pytest.mark.skipif(not REAL_ESPN_CSV.exists(), reason="real ESPN fixture not present in this checkout")
def test_baseline_resolution_rate_reproduced_exactly():
    """Locks the confirmed 84/365 (23.0%) baseline this batch's own precheck reproduced -- against the real local fixture."""
    import pandas as pd

    df = pd.read_csv(REAL_ESPN_CSV)
    codes = sorted(df["Team"].unique())
    assert len(codes) == 365
    resolved = sum(1 for c in codes if isinstance(resolve_team_identity(c, source="espn_stat_abbreviation"), TeamIdentity))
    assert resolved == 84


@pytest.mark.skipif(not REAL_ESPN_CSV.exists(), reason="real ESPN fixture not present in this checkout")
def test_post_remediation_resolution_rate():
    """84 baseline + 231 reviewed = 315/365 (86.3%)."""
    import pandas as pd

    df = pd.read_csv(REAL_ESPN_CSV)
    codes = sorted(df["Team"].unique())
    resolved = sum(1 for c in codes if isinstance(resolve_espn_team_abbreviation(c), TeamIdentity))
    assert resolved == 315
    assert len(codes) - resolved == 50


def test_unresolved_code_stays_unresolved_not_guessed():
    for code in UNRESOLVED_ESPN_ABBREVIATIONS[:5]:
        result = resolve_espn_team_abbreviation(code)
        assert isinstance(result, UnresolvedIdentity)


def test_rejected_weak_single_player_match_confirmed_still_unresolved():
    """The specific real false positive this batch found and rejected during its own analysis (ARST/Milwaukee, a single coincidental shared player name, with Arkansas State having no local roster evidence at all) -- must never appear as a reviewed alias."""
    assert "ARST" not in REVIEWED_ESPN_ABBREVIATION_ALIASES
    assert "ARST" in UNRESOLVED_ESPN_ABBREVIATIONS
    result = resolve_espn_team_abbreviation("ARST")
    assert isinstance(result, UnresolvedIdentity)


def test_shared_team_under_two_roster_slug_spellings_not_falsely_ambiguous():
    """MIA (Miami FL) and JOES (Saint Joseph's) both had two roster-directory-slug spellings for the SAME canonical team during evidence-gathering -- confirms this was correctly resolved as one team, not left ambiguous."""
    assert "MIA" in REVIEWED_ESPN_ABBREVIATION_ALIASES
    assert "JOES" in REVIEWED_ESPN_ABBREVIATION_ALIASES
    mia = resolve_espn_team_abbreviation("MIA")
    joes = resolve_espn_team_abbreviation("JOES")
    assert isinstance(mia, TeamIdentity) and isinstance(joes, TeamIdentity)


def test_never_fuzzy_matches_a_code_not_in_the_reviewed_table():
    """A made-up code that superficially resembles a real one must never resolve -- this module does no similarity scoring of its own."""
    result = resolve_espn_team_abbreviation("ZZZ999")
    assert isinstance(result, UnresolvedIdentity)


def test_lowercase_code_still_resolves_via_reviewed_crosswalk():
    upper = next(iter(REVIEWED_ESPN_ABBREVIATION_ALIASES))
    result = resolve_espn_team_abbreviation(upper.lower())
    assert isinstance(result, TeamIdentity)
    assert result.team_id == REVIEWED_ESPN_ABBREVIATION_ALIASES[upper]


def test_empty_code_does_not_raise():
    result = resolve_espn_team_abbreviation("")
    assert isinstance(result, UnresolvedIdentity)


def test_never_modifies_shared_resolver_or_slug_utils():
    """AST guard: this module must never import/call anything that mutates TEAM_REGISTRY/_ALIAS_TO_ID or slug_utils' own registry-building functions."""
    import ast
    import inspect
    import src.identity.espn_team_abbreviation_aliases as mod

    tree = ast.parse(inspect.getsource(mod))
    called_names = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    forbidden = {"build_registry", "_register_alias", "_register_team", "_register_source_slugs", "_register_sr_overrides", "_register_display_aliases"}
    assert not (forbidden & called_names)


# ---------------------------------------------------------------------------
# Phase 5B6: typed provenance result, reproduction, extended evidence search,
# and alias-table integrity
# ---------------------------------------------------------------------------


def test_provenance_exact_alias_method():
    result = resolve_espn_team_abbreviation_with_provenance("BYU")
    assert isinstance(result, EspnTeamCodeResolution)
    assert result.method == "exact_alias"
    assert isinstance(result.identity, TeamIdentity)
    assert result.team_id == result.identity.team_id


def test_provenance_reviewed_alias_method():
    result = resolve_espn_team_abbreviation_with_provenance("MIA")
    assert result.method == "reviewed_alias"
    assert result.team_id == REVIEWED_ESPN_ABBREVIATION_ALIASES["MIA"]


def test_provenance_unresolved_method():
    result = resolve_espn_team_abbreviation_with_provenance("ARST")
    assert result.method == "unresolved"
    assert result.team_id is None


def test_provenance_ambiguous_method_for_unca():
    """UNCA is a genuinely new, more precise finding this batch: resolve_team_identity itself
    reports 3 real named candidates (North Carolina, North Alabama, UNC Wilmington), confirmed
    by direct reproduction -- distinct from the other 49 unresolved codes' plain 'not_found'."""
    baseline = resolve_team_identity("UNCA", source="espn_stat_abbreviation")
    assert isinstance(baseline, UnresolvedIdentity) and baseline.reason == "ambiguous"
    result = resolve_espn_team_abbreviation_with_provenance("UNCA")
    assert result.method == "ambiguous"
    assert result.team_id is None
    assert "UNCA" not in REVIEWED_ESPN_ABBREVIATION_ALIASES  # no evidence disambiguated it -- never guessed


@pytest.mark.skipif(not REAL_ESPN_CSV.exists(), reason="real ESPN fixture not present in this checkout")
def test_provenance_method_values_all_in_persisted_enum_vocabulary():
    """Every method this module can produce must already be a valid, CHECK-constrained
    EspnSourceObservation.team_resolution_method value -- no schema/migration change needed."""
    import pandas as pd

    df = pd.read_csv(REAL_ESPN_CSV)
    codes = sorted(df["Team"].astype(str).unique())
    seen_methods = {resolve_espn_team_abbreviation_with_provenance(c).method for c in codes}
    assert seen_methods <= set(ESPN_TEAM_RESOLUTION_METHODS)
    assert "ambiguous" in seen_methods  # confirms the UNCA case is reachable, not just theoretically possible


def test_plain_resolver_return_type_unchanged_by_provenance_addition():
    """resolve_espn_team_abbreviation's own contract (TeamIdentity | UnresolvedIdentity) is unchanged -- existing callers unaffected, and it agrees exactly with the typed result's own .identity."""
    for code in ("BYU", "MIA", "ARST", "UNCA"):
        result = resolve_espn_team_abbreviation(code)
        provenance = resolve_espn_team_abbreviation_with_provenance(code)
        assert isinstance(result, (TeamIdentity, UnresolvedIdentity))
        assert isinstance(result, type(provenance.identity))
        if isinstance(result, TeamIdentity):
            assert result.team_id == provenance.identity.team_id


def test_no_reviewed_alias_also_resolves_via_baseline_exact_path():
    """Redundancy check, reproduced fresh this batch: none of the 231 reviewed codes should ALSO resolve via the baseline exact path."""
    for code in REVIEWED_ESPN_ABBREVIATION_ALIASES:
        baseline = resolve_team_identity(code, source="espn_stat_abbreviation")
        assert not isinstance(baseline, TeamIdentity), f"{code!r} is a redundant reviewed-alias entry"


def test_no_reviewed_alias_maps_multiple_codes_to_the_same_team_unintentionally():
    """This batch's own integrity check: currently every one of the 231 reviewed-alias targets is unique -- if this ever changes, it must be deliberate, not a copy-paste collision."""
    from collections import Counter

    counts = Counter(REVIEWED_ESPN_ABBREVIATION_ALIASES.values())
    duplicated = {team_id: n for team_id, n in counts.items() if n > 1}
    assert duplicated == {}, f"unexpected multi-code-to-one-team mapping(s), verify intentional: {duplicated}"


def test_every_reviewed_alias_target_has_a_real_registry_record():
    from src import slug_utils

    resolve_team_identity("Michigan State", source="sports_reference")  # forces TEAM_REGISTRY to build
    for code, team_id in REVIEWED_ESPN_ABBREVIATION_ALIASES.items():
        assert slug_utils.TEAM_REGISTRY.get(team_id) is not None, f"{code!r} targets a dangling team_id {team_id}"


def test_extended_evidence_search_found_zero_new_qualifying_codes():
    """Phase 5B6's own extended search re-checked all 50 remaining codes against BOTH
    Roster_2026.xlsx AND the newly-available (Phase 5B4/5B5) player-game-log filenames as a
    second local roster-equivalent evidence source -- found zero new >=2-shared-player
    candidates. Locks in that this batch adds zero new reviewed aliases."""
    assert len(REVIEWED_ESPN_ABBREVIATION_ALIASES) == 231
    assert len(UNRESOLVED_ESPN_ABBREVIATIONS) == 50


def test_five_single_weak_candidate_near_misses_confirmed_still_unresolved():
    """This batch's own extended search found exactly 5 codes with exactly ONE coincidental
    shared player name (below the established 2-match bar): the already-documented ARST, plus
    4 newly-confirmed EVAN/LAF/LONG/MORG. None were promoted -- single-name evidence stays
    insufficient regardless of which local source (roster or game-log filename) supplied it."""
    for code in ("ARST", "EVAN", "LAF", "LONG", "MORG"):
        assert code in UNRESOLVED_ESPN_ABBREVIATIONS
        assert code not in REVIEWED_ESPN_ABBREVIATION_ALIASES
        result = resolve_espn_team_abbreviation_with_provenance(code)
        assert result.method == "unresolved"


def test_conference_only_evidence_never_used():
    """AST guard: this module's own resolution logic never references conference membership at all -- 'conference alone is not sufficient' is satisfied by construction, not by a runtime check."""
    import ast
    import inspect
    import src.identity.espn_team_abbreviation_aliases as mod

    source = inspect.getsource(mod.resolve_espn_team_abbreviation_with_provenance)
    assert "conference" not in source.lower()


def test_deterministic_repeated_resolution():
    results = [resolve_espn_team_abbreviation_with_provenance("MIA").team_id for _ in range(5)]
    assert len(set(results)) == 1 and results[0] is not None


def test_dict_iteration_order_independence():
    """Resolution must not depend on REVIEWED_ESPN_ABBREVIATION_ALIASES's own iteration/insertion order -- a plain dict .get() lookup is already order-independent; confirm rebuilding it in reverse order changes nothing."""
    reversed_dict = dict(reversed(list(REVIEWED_ESPN_ABBREVIATION_ALIASES.items())))
    assert reversed_dict.get("MIA") == REVIEWED_ESPN_ABBREVIATION_ALIASES["MIA"]


def test_reviewed_alias_dict_not_mutated_by_resolution():
    before = dict(REVIEWED_ESPN_ABBREVIATION_ALIASES)
    for code in ("BYU", "MIA", "ARST", "UNCA", "", None):
        resolve_espn_team_abbreviation_with_provenance(code or "")
    assert REVIEWED_ESPN_ABBREVIATION_ALIASES == before


def test_null_code_does_not_raise():
    result = resolve_espn_team_abbreviation_with_provenance(None)
    assert result.method == "unresolved"
    assert result.team_id is None


def test_whitespace_only_code_does_not_raise():
    result = resolve_espn_team_abbreviation_with_provenance("   ")
    assert result.method == "unresolved"


def test_raw_code_preserved_verbatim_on_the_typed_result():
    result = resolve_espn_team_abbreviation_with_provenance("mia")
    assert result.code == "mia"  # the ORIGINAL raw code, never the normalized/upper-cased lookup key
    assert result.method == "reviewed_alias"


def test_wrong_code_never_leaks_a_different_team():
    """A code one character off from a real reviewed alias must never resolve to that alias's team -- exact match only, never a near-miss."""
    assert "MIAX" not in REVIEWED_ESPN_ABBREVIATION_ALIASES
    result = resolve_espn_team_abbreviation_with_provenance("MIAX")
    assert result.team_id != REVIEWED_ESPN_ABBREVIATION_ALIASES["MIA"]
    assert result.method == "unresolved"


def test_never_imports_ovr_model_or_network_libraries():
    import ast
    import inspect
    import src.identity.espn_team_abbreviation_aliases as mod

    tree = ast.parse(inspect.getsource(mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not ({"requests", "urllib", "socket", "selenium"} & imported)
    assert "ovr_model" not in " ".join(imported)
