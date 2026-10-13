"""
Regression tests for src/identity/espn_player_identity.py (Phase 2B, batch 2).

No live requests: DOM-extraction tests use small hand-built HTML fragments
shaped like ESPN's real markup (confirmed against the real cached
`TeamDump/ESPN/espn_full_player_stats.csv` for column/name shape). No
Selenium anywhere in this file.
"""
import pytest
from bs4 import BeautifulSoup

from src.identity.contracts import PlayerIdentity, SourceIdentity, TeamIdentity, UnresolvedIdentity
from src.identity.espn_player_identity import (
    extract_espn_athlete_id,
    extract_espn_player_row_v15,
    find_espn_id_reused_within_same_team_season,
    find_roster_players_linked_by_multiple_espn_ids,
    link_espn_identity_to_roster,
    resolve_espn_player_identity,
)
from src.identity.player_identity import build_player_identity


def _cell(html: str):
    return BeautifulSoup(html, "lxml").find("td")


# ---------------------------------------------------------------------------
# DOM extraction: byte-faithful name/team (v15) + new athlete-id extraction
# ---------------------------------------------------------------------------

def test_extract_espn_player_row_v15_matches_real_markup_shape():
    cell = _cell(
        '<td><a href="/mens-college-basketball/player/_/id/4433137/aj-dybantsa">AJ Dybantsa</a>'
        '<span class="athleteCell__teamAbbrev">BYU</span></td>'
    )
    name, team = extract_espn_player_row_v15(cell)
    assert name == "AJ Dybantsa"
    assert team == "BYU"


def test_extract_espn_player_row_v15_handles_missing_link_or_team():
    cell = _cell("<td>No Link Here</td>")
    assert extract_espn_player_row_v15(cell) == ("", "")


def test_extract_espn_athlete_id_reads_the_real_id():
    cell = _cell('<td><a href="/mens-college-basketball/player/_/id/4433137/aj-dybantsa">AJ Dybantsa</a></td>')
    assert extract_espn_athlete_id(cell) == "4433137"


def test_extract_espn_athlete_id_returns_none_when_absent():
    # Confirms this session's finding: the real cached CSVs have no ID at
    # all, so a link-less/id-less cell must not fabricate one.
    cell = _cell("<td><a href='#'>No Id Here</a></td>")
    assert extract_espn_athlete_id(cell) is None
    cell2 = _cell("<td>Plain text player</td>")
    assert extract_espn_athlete_id(cell2) is None


# ---------------------------------------------------------------------------
# resolve_espn_player_identity: exact ids, team resolution, missing context
# ---------------------------------------------------------------------------

def test_resolve_espn_player_identity_with_athlete_id():
    result = resolve_espn_player_identity("Darius Acuff Jr.", "Arkansas", 2026, espn_athlete_id="4433200")
    assert isinstance(result, PlayerIdentity)
    assert result.player_id == "espn:4433200"
    assert result.sources["espn"].raw_value == "4433200"


def test_resolve_espn_player_identity_without_athlete_id_falls_back_to_scoped_id():
    result = resolve_espn_player_identity("AJ Dybantsa", "BYU", 2026)
    assert isinstance(result, PlayerIdentity)
    assert result.sources == {}
    assert f"season:2026" in result.player_id


def test_resolve_espn_player_identity_routes_team_through_canonical_registry():
    result = resolve_espn_player_identity("AJ Dybantsa", "BYU", 2026)
    assert isinstance(result, PlayerIdentity)
    # BYU is a real registered abbreviation alias -> Brigham Young.
    from src.identity.team_identity import resolve_team_identity
    expected_team = resolve_team_identity("BYU")
    assert result.team_id == expected_team.team_id


def test_resolve_espn_player_identity_unknown_team_is_unresolved():
    result = resolve_espn_player_identity("Frank Mitchell", "Not A Real Team", 2026)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "not_found"


def test_resolve_espn_player_identity_ambiguous_team_is_not_silently_resolved():
    result = resolve_espn_player_identity("Someone", "Miami", 2026)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "ambiguous"


def test_resolve_espn_player_identity_empty_name_is_unresolved():
    result = resolve_espn_player_identity("", "BYU", 2026)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "not_found"


def test_repeated_resolution_is_deterministic():
    ids = {resolve_espn_player_identity("Darius Acuff Jr.", "Arkansas", 2026, espn_athlete_id="4433200").player_id for _ in range(3)}
    assert len(ids) == 1


# ---------------------------------------------------------------------------
# Accents, apostrophes, hyphens, initials, suffixes -- via the shared core
# ---------------------------------------------------------------------------

def test_accented_and_unaccented_espn_names_produce_the_same_key():
    from src.identity.player_identity import canonical_player_key_v15
    a = resolve_espn_player_identity("Nikola Jović", "BYU", 2026)
    b = resolve_espn_player_identity("Nikola Jovic", "BYU", 2026)
    assert canonical_player_key_v15(a.display_name) == canonical_player_key_v15(b.display_name)


def test_apostrophe_and_hyphen_espn_names_preserved_in_display():
    a = resolve_espn_player_identity("Amar'e Marshall", "BYU", 2026)
    b = resolve_espn_player_identity("Jean-Paul Olukemi", "BYU", 2026)
    assert a.display_name == "Amar'e Marshall"
    assert b.display_name == "Jean-Paul Olukemi"


def test_initials_and_full_name_are_distinct_espn_identities():
    a = resolve_espn_player_identity("A. Marshall", "BYU", 2026)
    b = resolve_espn_player_identity("Amare Marshall", "BYU", 2026)
    assert a.player_id != b.player_id


@pytest.mark.parametrize("suffixed", ["Darius Acuff Jr.", "John Smith Sr.", "John Smith II", "John Smith III", "John Smith IV"])
def test_all_suffix_forms_resolve(suffixed):
    result = resolve_espn_player_identity(suffixed, "BYU", 2026)
    assert isinstance(result, PlayerIdentity)
    assert result.display_name == suffixed


# ---------------------------------------------------------------------------
# Cross-source linking: scoped, unique, suffix-compatible
# ---------------------------------------------------------------------------

def test_link_espn_identity_to_roster_succeeds_on_unique_scoped_match():
    roster = [build_player_identity("Darius Acuff Jr.", team_id=999, season=2026, source="sports_reference", source_player_id="acuff-darius-1")]
    espn_id = PlayerIdentity(
        player_id="espn:12345", display_name="Darius Acuff Jr.", team_id=999, season=2026,
        sources={"espn": SourceIdentity("espn", "12345", "12345")},
    )
    linked = link_espn_identity_to_roster(espn_id, roster)
    assert isinstance(linked, PlayerIdentity)
    assert "espn" in linked.sources and "sports_reference" in linked.sources
    assert linked.player_id == "espn:12345"  # ESPN id preferred as most stable


def test_link_espn_identity_to_roster_rejects_unscoped_different_team():
    roster = [build_player_identity("Darius Acuff Jr.", team_id=111, season=2026, source="sports_reference", source_player_id="acuff-darius-1")]
    espn_id = build_player_identity("Darius Acuff Jr.", team_id=999, season=2026, source="espn", source_player_id="12345")
    result = link_espn_identity_to_roster(espn_id, roster)
    # Different team_id -> roster is out of scope -> stays ESPN-only, not linked.
    assert result is espn_id or (isinstance(result, PlayerIdentity) and "sports_reference" not in result.sources)


def test_link_espn_identity_to_roster_rejects_unscoped_different_season():
    roster = [build_player_identity("Darius Acuff Jr.", team_id=999, season=2025, source="sports_reference", source_player_id="acuff-darius-1")]
    espn_id = build_player_identity("Darius Acuff Jr.", team_id=999, season=2026, source="espn", source_player_id="12345")
    result = link_espn_identity_to_roster(espn_id, roster)
    assert "sports_reference" not in result.sources


def test_link_espn_identity_to_roster_returns_ambiguous_for_duplicate_roster_names():
    roster = [
        build_player_identity("John Smith", team_id=999, season=2026),
        build_player_identity("John Smith Jr.", team_id=999, season=2026),
    ]
    espn_id = build_player_identity("John Smith", team_id=999, season=2026, source="espn", source_player_id="1")
    result = link_espn_identity_to_roster(espn_id, roster)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "ambiguous"


def test_link_espn_identity_to_roster_rejects_conflicting_suffixes():
    roster = [build_player_identity("Darius Acuff III", team_id=999, season=2026, source="sports_reference", source_player_id="acuff-2")]
    espn_id = build_player_identity("Darius Acuff Jr.", team_id=999, season=2026, source="espn", source_player_id="1")
    result = link_espn_identity_to_roster(espn_id, roster)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "ambiguous"
    assert len(result.candidates) == 2


def test_link_espn_identity_to_roster_keeps_espn_only_identity_when_no_match():
    roster = [build_player_identity("Someone Else", team_id=999, season=2026)]
    espn_id = build_player_identity("Darius Acuff Jr.", team_id=999, season=2026, source="espn", source_player_id="12345")
    result = link_espn_identity_to_roster(espn_id, roster)
    assert isinstance(result, PlayerIdentity)
    assert result.player_id == espn_id.player_id
    assert "sports_reference" not in result.sources


def test_link_espn_identity_to_roster_keeps_espn_only_identity_when_roster_empty():
    espn_id = build_player_identity("Darius Acuff Jr.", team_id=999, season=2026, source="espn", source_player_id="12345")
    result = link_espn_identity_to_roster(espn_id, [])
    assert result is espn_id


def test_link_prefers_sr_id_when_espn_has_none():
    roster = [build_player_identity("Frank Mitchell", team_id=999, season=2026, source="sports_reference", source_player_id="mitchell-frank-1")]
    espn_id = build_player_identity("Frank Mitchell", team_id=999, season=2026, source="espn")  # no espn id
    linked = link_espn_identity_to_roster(espn_id, roster)
    assert isinstance(linked, PlayerIdentity)
    assert linked.player_id == "sports_reference:mitchell-frank-1"


# ---------------------------------------------------------------------------
# Transfer behavior: ESPN id stable across team/season changes
# ---------------------------------------------------------------------------

def test_transfer_keeps_espn_id_stable_across_team_and_season():
    before = resolve_espn_player_identity("Darius Acuff Jr.", "Arkansas", 2025, espn_athlete_id="4433200")
    after = resolve_espn_player_identity("Darius Acuff Jr.", "Rhode Island", 2026, espn_athlete_id="4433200")
    assert before.player_id == after.player_id == "espn:4433200"
    assert before.team_id != after.team_id
    assert before.season != after.season


def test_transfer_without_espn_id_produces_distinct_scoped_records():
    # Without a real ESPN id, there is no way to prove continuity across a
    # transfer -- per this batch's instruction, no such merge is attempted.
    before = resolve_espn_player_identity("Jane Doe", "Arkansas", 2025)
    after = resolve_espn_player_identity("Jane Doe", "Rhode Island", 2026)
    assert before.player_id != after.player_id


# ---------------------------------------------------------------------------
# Same-name players on different teams / duplicate names on one roster
# ---------------------------------------------------------------------------

def test_same_name_players_on_different_teams_are_distinct():
    a = resolve_espn_player_identity("Frank Mitchell", "BYU", 2026)
    b = resolve_espn_player_identity("Frank Mitchell", "Arkansas", 2026)
    assert a.player_id != b.player_id
    assert a.team_id != b.team_id


def test_duplicate_names_on_one_roster_never_silently_overwrite():
    roster = [
        build_player_identity("John Smith", team_id=1, season=2026, source="sports_reference", source_player_id="a"),
        build_player_identity("John Smith Jr.", team_id=1, season=2026, source="sports_reference", source_player_id="b"),
    ]
    espn_a = build_player_identity("John Smith", team_id=1, season=2026, source="espn", source_player_id="100")
    result = link_espn_identity_to_roster(espn_a, roster)
    assert not isinstance(result, PlayerIdentity)
    assert result.reason == "ambiguous"
    assert {c.display_name for c in result.candidates} == {"John Smith", "John Smith Jr."}


# ---------------------------------------------------------------------------
# Malformed records / missing context
# ---------------------------------------------------------------------------

def test_malformed_espn_row_missing_name_does_not_crash():
    cell = _cell("<td></td>")
    name, team = extract_espn_player_row_v15(cell)
    assert name == "" and team == ""
    assert extract_espn_athlete_id(cell) is None


def test_resolve_espn_player_identity_requires_season_explicitly():
    # season has no default -- confirms the module's finding that ESPN's
    # real data has no season field, so nothing here silently assumes one.
    import inspect
    sig = inspect.signature(resolve_espn_player_identity)
    assert sig.parameters["season"].default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# Source-ID conflict detection
# ---------------------------------------------------------------------------

def test_find_espn_id_reused_within_same_team_season_detects_conflict():
    conflicting = [
        build_player_identity("Player A", 1, 2026, source="espn", source_player_id="999"),
        build_player_identity("Player B", 1, 2026, source="espn", source_player_id="999"),
    ]
    conflicts = find_espn_id_reused_within_same_team_season(conflicting)
    assert len(conflicts) == 1
    assert conflicts[0]["espn_id"] == "999"
    assert set(conflicts[0]["display_names"]) == {"Player A", "Player B"}


def test_find_espn_id_reused_within_same_team_season_allows_legitimate_transfers():
    # Same ESPN id, but DIFFERENT (team, season) -- this is a transfer, not a conflict.
    transfer = [
        build_player_identity("Darius Acuff Jr.", 1, 2025, source="espn", source_player_id="999"),
        build_player_identity("Darius Acuff Jr.", 2, 2026, source="espn", source_player_id="999"),
    ]
    assert find_espn_id_reused_within_same_team_season(transfer) == []


def test_find_roster_players_linked_by_multiple_espn_ids_detects_conflict():
    linked_a = PlayerIdentity(
        player_id="x", display_name="Frank Mitchell", team_id=1, season=2026,
        sources={
            "sports_reference": SourceIdentity("sports_reference", "mitchell-1", "mitchell-1"),
            "espn": SourceIdentity("espn", "100", "100"),
        },
    )
    linked_b = PlayerIdentity(
        player_id="y", display_name="Frank Mitchell", team_id=1, season=2026,
        sources={
            "sports_reference": SourceIdentity("sports_reference", "mitchell-1", "mitchell-1"),
            "espn": SourceIdentity("espn", "200", "200"),
        },
    )
    conflicts = find_roster_players_linked_by_multiple_espn_ids([linked_a, linked_b])
    assert conflicts == {"mitchell-1": ["100", "200"]}


def test_find_roster_players_linked_by_multiple_espn_ids_empty_when_clean():
    linked = [
        build_player_identity("Frank Mitchell", 1, 2026, source="espn", source_player_id="100"),
    ]
    assert find_roster_players_linked_by_multiple_espn_ids(linked) == {}


# ---------------------------------------------------------------------------
# Isolation: no mutation of team registries, player statistics
# ---------------------------------------------------------------------------

def test_espn_player_resolution_does_not_mutate_team_registry():
    from src import slug_utils

    before_size = len(slug_utils.TEAM_REGISTRY)
    before_aliases = dict(slug_utils._DISPLAY_NAME_ALIASES)
    resolve_espn_player_identity("Darius Acuff Jr.", "Arkansas", 2026, espn_athlete_id="1")
    assert len(slug_utils.TEAM_REGISTRY) == before_size
    assert slug_utils._DISPLAY_NAME_ALIASES == before_aliases


def test_espn_resolution_does_not_touch_allstats_or_injury_modules():
    import ast
    import inspect

    import src.identity.espn_player_identity as mod

    tree = ast.parse(inspect.getsource(mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert not any("scraper" in name or "ovr_model" in name for name in imported)
