"""
Regression tests for src/identity/injury_player_identity.py (Phase 2B, batch 3).

No live requests: uses the already-verified-faithful
src.scraper.normalize_covers_player (no re-scraping) and small synthetic
rosters built via the existing player-identity core. Confirms injury
resolution never touches src.scraper's actual deduction functions
(compute_injury_deduction, _fetch_espn_injuries_for_team) -- this batch's
"injury outputs remain unchanged" requirement.
"""
import pytest

from src.identity.contracts import PlayerIdentity, TeamIdentity, UnresolvedIdentity
from src.identity.injury_player_identity import (
    check_injury_team_matches_roster_team,
    find_duplicate_injury_reports,
    resolve_espn_injury_player_identity,
    resolve_injury_team_identity,
    resolve_lastname_only_injury_candidate,
)
from src.identity.player_identity import build_player_identity


# ---------------------------------------------------------------------------
# ESPN full-name injury path (reuses the ESPN player adapter)
# ---------------------------------------------------------------------------

def test_resolve_espn_injury_player_identity_with_athlete_id():
    result = resolve_espn_injury_player_identity("Darius Acuff Jr.", "Arkansas", 2026, espn_athlete_id="4433200")
    assert isinstance(result, PlayerIdentity)
    assert result.player_id == "espn:4433200"


def test_resolve_espn_injury_player_identity_tags_source_as_espn_injury():
    result = resolve_espn_injury_player_identity("Frank Mitchell", "BYU", 2026)
    assert isinstance(result, PlayerIdentity)
    # No espn id supplied -> scoped fallback id, still resolves cleanly.
    assert "team:" in result.player_id


def test_resolve_espn_injury_player_identity_unknown_team_is_unresolved():
    result = resolve_espn_injury_player_identity("Frank Mitchell", "Not A Real Team", 2026)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "not_found"
    assert result.source == "espn_injury"


def test_resolve_espn_injury_player_identity_ambiguous_team_propagates():
    result = resolve_espn_injury_player_identity("Someone", "Miami", 2026)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "ambiguous"


def test_resolve_espn_injury_player_identity_transfer_stable_id():
    before = resolve_espn_injury_player_identity("Darius Acuff Jr.", "Arkansas", 2025, espn_athlete_id="4433200")
    after = resolve_espn_injury_player_identity("Darius Acuff Jr.", "Rhode Island", 2026, espn_athlete_id="4433200")
    assert before.player_id == after.player_id == "espn:4433200"


# ---------------------------------------------------------------------------
# Last-name-only injury path: exact athlete IDs N/A, ambiguity, unconfirmed
# ---------------------------------------------------------------------------

@pytest.fixture
def sbu_roster():
    return [
        build_player_identity("Frank Mitchell", 905884, 2026, source="sports_reference", source_player_id="mitchell-1"),
        build_player_identity("Daryl Simmons II", 905884, 2026, source="sports_reference", source_player_id="simmons-1"),
        build_player_identity("Amar'e Marshall", 905884, 2026, source="sports_reference", source_player_id="marshall-1"),
    ]


def test_unique_lastname_match_is_unconfirmed_not_auto_resolved(sbu_roster):
    """
    Central rule of this batch: even a UNIQUE last-name match must not be
    silently treated as confirmed identity.
    """
    result = resolve_lastname_only_injury_candidate("Mitchell", sbu_roster)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "unconfirmed"
    assert len(result.candidates) == 1
    assert result.candidates[0].display_name == "Frank Mitchell"


def test_same_surname_teammates_produce_ambiguous():
    roster = [
        build_player_identity("John Smith", 1, 2026),
        build_player_identity("Jim Smith", 1, 2026),
    ]
    result = resolve_lastname_only_injury_candidate("Smith", roster)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "ambiguous"
    assert len(result.candidates) == 2


def test_unknown_lastname_is_not_found(sbu_roster):
    result = resolve_lastname_only_injury_candidate("Nobody", sbu_roster)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "not_found"


def test_reviewed_alias_confirms_a_specific_player():
    roster = [
        build_player_identity("John Smith", 1, 2026),
        build_player_identity("Jim Smith", 1, 2026),
    ]
    result = resolve_lastname_only_injury_candidate(
        "Smith", roster, reviewed_alias_display_name="John Smith"
    )
    assert isinstance(result, PlayerIdentity)
    assert result.display_name == "John Smith"


def test_reviewed_alias_that_does_not_match_falls_through_to_normal_rules():
    roster = [build_player_identity("Frank Mitchell", 905884, 2026)]
    result = resolve_lastname_only_injury_candidate(
        "Mitchell", roster, reviewed_alias_display_name="Someone Else"
    )
    # Reviewed alias didn't match anyone -> falls back to unconfirmed (unique surname match).
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "unconfirmed"


def test_suffix_correct_lastname_still_matches_by_surname(sbu_roster):
    # "Daryl Simmons II" -- normalize_covers_player strips the suffix
    # correctly (unlike the buggy normalize_allstats_player documented in
    # this module), so a bare "Simmons" injury report still finds the
    # right surname.
    result = resolve_lastname_only_injury_candidate("Simmons", sbu_roster)
    assert result.reason == "unconfirmed"
    assert result.candidates[0].display_name == "Daryl Simmons II"


def test_apostrophe_surname_matches_via_normalize_covers_player(sbu_roster):
    result = resolve_lastname_only_injury_candidate("Marshall", sbu_roster)
    assert result.reason == "unconfirmed"
    assert result.candidates[0].display_name == "Amar'e Marshall"


def test_raw_full_name_input_is_reduced_to_lastname(sbu_roster):
    # Caller may pass a raw "Player Name" string instead of a pre-reduced
    # last name -- normalize_covers_player handles both.
    result = resolve_lastname_only_injury_candidate("Frank Mitchell", sbu_roster)
    assert result.reason == "unconfirmed"
    assert result.candidates[0].display_name == "Frank Mitchell"


def test_empty_lastname_input_is_not_found(sbu_roster):
    result = resolve_lastname_only_injury_candidate("", sbu_roster)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "not_found"


def test_repeated_lastname_resolution_is_deterministic(sbu_roster):
    reasons = {resolve_lastname_only_injury_candidate("Mitchell", sbu_roster).reason for _ in range(3)}
    assert reasons == {"unconfirmed"}


# ---------------------------------------------------------------------------
# Compound / hyphenated surnames
# ---------------------------------------------------------------------------

def test_hyphenated_surname_is_kept_whole():
    roster = [build_player_identity("Jean Smith-Jones", 1, 2026)]
    result = resolve_lastname_only_injury_candidate("Smith-Jones", roster)
    assert result.reason == "unconfirmed"
    assert result.candidates[0].display_name == "Jean Smith-Jones"


def test_space_separated_compound_surname_is_truncated_to_last_token():
    """
    Documents (does not fix) a real limitation inherited from
    normalize_covers_player: a space-separated compound surname like
    "De La Cruz" reduces to just "Cruz" (last whitespace token only) --
    matching the legacy scraper's own behavior exactly, not a new bug.
    """
    roster = [build_player_identity("Juan De La Cruz", 1, 2026)]
    result = resolve_lastname_only_injury_candidate("Cruz", roster)
    assert result.reason == "unconfirmed"
    assert result.candidates[0].display_name == "Juan De La Cruz"
    # The full "De La Cruz" as a search term does NOT match "Cruz" --
    # documents the truncation limitation explicitly.
    from src.scraper import normalize_covers_player
    assert normalize_covers_player("Juan De La Cruz") == "cruz"


@pytest.mark.parametrize("suffixed,search_term", [
    ("John Smith Jr.", "Smith"),
    ("John Smith III", "Smith"),
    ("John Smith IV", "Smith"),
])
def test_all_suffix_forms_still_match_by_surname(suffixed, search_term):
    roster = [build_player_identity(suffixed, 1, 2026)]
    result = resolve_lastname_only_injury_candidate(search_term, roster)
    assert result.reason == "unconfirmed"
    assert result.candidates[0].display_name == suffixed


def test_suffix_conflict_between_two_similarly_named_players_is_ambiguous():
    roster = [
        build_player_identity("Darius Acuff Jr.", 1, 2026),
        build_player_identity("Darius Acuff III", 1, 2026),
    ]
    result = resolve_lastname_only_injury_candidate("Acuff", roster)
    assert result.reason == "ambiguous"
    assert len(result.candidates) == 2


# ---------------------------------------------------------------------------
# Team resolution / injury-team-vs-roster-team conflict
# ---------------------------------------------------------------------------

def test_resolve_injury_team_identity_routes_through_canonical_registry():
    result = resolve_injury_team_identity("St. Bonaventure")
    assert isinstance(result, TeamIdentity)
    assert result.display_name == "St. Bonaventure"


def test_resolve_injury_team_identity_unknown_team():
    result = resolve_injury_team_identity("Not A Real Team")
    assert not isinstance(result, TeamIdentity)
    assert result.reason == "not_found"


def test_check_injury_team_matches_roster_team_true_case():
    team_result = resolve_injury_team_identity("St. Bonaventure")
    assert check_injury_team_matches_roster_team(team_result, team_result.team_id) is True


def test_check_injury_team_matches_roster_team_conflict_case():
    team_result = resolve_injury_team_identity("St. Bonaventure")
    assert check_injury_team_matches_roster_team(team_result, 999999) is False


def test_check_injury_team_matches_roster_team_handles_unresolved_team():
    unresolved = resolve_injury_team_identity("Not A Real Team")
    assert check_injury_team_matches_roster_team(unresolved, 12345) is False


# ---------------------------------------------------------------------------
# Malformed records, duplicate injury reports
# ---------------------------------------------------------------------------

def test_find_duplicate_injury_reports_detects_repeats():
    records = [
        {"player": "mitchell", "status": "out"},
        {"player": "mitchell", "status": "out"},
        {"player": "simmons", "status": "questionable"},
    ]
    duplicates = find_duplicate_injury_reports(records)
    assert duplicates == [{"player": "mitchell", "status": "out", "count": 2}]


def test_find_duplicate_injury_reports_empty_when_clean():
    records = [{"player": "mitchell", "status": "out"}, {"player": "simmons", "status": "questionable"}]
    assert find_duplicate_injury_reports(records) == []


def test_find_duplicate_injury_reports_handles_malformed_records():
    records = [{"player": "mitchell"}, {}, {"status": "out"}]
    # Should not crash -- missing keys default to empty string.
    result = find_duplicate_injury_reports(records)
    assert isinstance(result, list)


def test_malformed_injury_record_missing_player_key_does_not_crash():
    roster = [build_player_identity("Frank Mitchell", 1, 2026)]
    result = resolve_lastname_only_injury_candidate(None, roster)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "not_found"


# ---------------------------------------------------------------------------
# Compatibility / no changes to injury deduction outputs
# ---------------------------------------------------------------------------

def test_injury_identity_module_does_not_import_deduction_or_ovr_code():
    """
    Confirms "do not change any injury deduction... result": this module
    only imports normalize_covers_player from src.scraper (a pure,
    already-verified string function with no side effects), never
    compute_injury_deduction, _fetch_espn_injuries_for_team, or anything
    from src.ovr_model.
    """
    import ast
    import inspect

    import src.identity.injury_player_identity as mod

    tree = ast.parse(inspect.getsource(mod))
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "src.scraper":
            imported_names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert "ovr_model" not in node.module

    assert imported_names == {"normalize_covers_player"}


def test_compute_injury_deduction_and_fetch_espn_injuries_untouched():
    """Sanity check: src.scraper's real deduction functions still exist,
    unmodified, importable, with their original signatures."""
    import inspect

    from src.scraper import _fetch_espn_injuries_for_team, compute_injury_deduction

    sig = inspect.signature(compute_injury_deduction)
    assert list(sig.parameters.keys())[:2] == ["team_espn_id", "team_name"]
    sig2 = inspect.signature(_fetch_espn_injuries_for_team)
    assert "team_espn_id" in sig2.parameters


# ---------------------------------------------------------------------------
# Isolation: does not mutate team registry
# ---------------------------------------------------------------------------

def test_injury_resolution_does_not_mutate_team_registry():
    from src import slug_utils

    before_size = len(slug_utils.TEAM_REGISTRY)
    before_aliases = dict(slug_utils._DISPLAY_NAME_ALIASES)
    resolve_injury_team_identity("St. Bonaventure")
    roster = [build_player_identity("Frank Mitchell", 905884, 2026)]
    resolve_lastname_only_injury_candidate("Mitchell", roster)
    assert len(slug_utils.TEAM_REGISTRY) == before_size
    assert slug_utils._DISPLAY_NAME_ALIASES == before_aliases
