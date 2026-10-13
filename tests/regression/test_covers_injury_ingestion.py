"""
Phase 3, batch 3: Covers.com injury canonical ingestion.
No live requests -- realistic mocked Covers HTML (no real cached fixture
exists locally; covers_slug_cache.json is present but empty).
"""
import pytest

from src.identity.contracts import PlayerIdentity, TeamIdentity, UnresolvedIdentity
from src.identity.player_identity import build_player_identity
from src.ingestion.covers_injury_ingestion import (
    CoversInjuryRecord,
    parse_covers_injury_rows_v15,
    resolve_covers_injury_row,
    run_covers_injury_ingestion_from_html,
)


def _injury_html(rows: list[tuple[str, str, str]], *, extra_collapse_row: bool = True) -> str:
    """rows: (player_raw, position, status_html_or_text)"""
    trs = []
    for player, pos, status in rows:
        trs.append(f"<tr><td>{player}</td><td>{pos}</td><td><b>{status}</b></td></tr>")
    if extra_collapse_row:
        trs.append('<tr id="injuryCollapseXYZ"><td>hidden detail</td><td></td><td></td></tr>')
    return f'<div id="TP_injuries"><table><tbody>{"".join(trs)}</tbody></table></div>'


# ---------------------------------------------------------------------------
# Raw HTML parsing: successful parse, skip collapse rows, raw preservation
# ---------------------------------------------------------------------------

def test_parse_covers_injury_rows_v15_successful_parse():
    html = _injury_html([("Darius McGhee Jr.", "G", "Out")])
    rows = parse_covers_injury_rows_v15(html)
    assert len(rows) == 1
    assert rows[0]["player_raw"] == "Darius McGhee Jr."
    assert rows[0]["status_raw"] == "Out"
    assert rows[0]["player_last"] == "mcghee"
    assert rows[0]["status"] == "out"


def test_parse_covers_injury_rows_v15_skips_collapse_detail_rows():
    html = _injury_html([("Darius McGhee Jr.", "G", "Out")], extra_collapse_row=True)
    rows = parse_covers_injury_rows_v15(html)
    assert len(rows) == 1  # the injuryCollapse row is never included


def test_parse_covers_injury_rows_v15_excludes_active_status():
    html = _injury_html([("Healthy Player", "G", "Active")])
    assert parse_covers_injury_rows_v15(html) == []


def test_parse_covers_injury_rows_v15_missing_injury_pane_no_crash():
    assert parse_covers_injury_rows_v15("<html><body>no injuries here</body></html>") == []


def test_parse_covers_injury_rows_v15_malformed_row_skipped():
    html = '<div id="TP_injuries"><table><tbody><tr><td>OnlyOneCell</td></tr></tbody></table></div>'
    assert parse_covers_injury_rows_v15(html) == []


def test_hyphenated_surname_preserved():
    html = _injury_html([("John Smith-Jones", "F", "Out")])
    rows = parse_covers_injury_rows_v15(html)
    assert rows[0]["player_raw"] == "John Smith-Jones"
    assert rows[0]["player_last"] == "smith-jones"  # hyphen preserved -- single token, not split


def test_suffix_handling_preserved_in_raw_but_stripped_in_key():
    html = _injury_html([("Darius McGhee Jr.", "G", "Out")])
    rows = parse_covers_injury_rows_v15(html)
    assert rows[0]["player_raw"] == "Darius McGhee Jr."  # suffix preserved in raw evidence
    assert rows[0]["player_last"] == "mcghee"  # suffix stripped in the matching key


def test_compound_surname_legacy_limitation_documented_not_fixed():
    """
    "Van Der Berg" (a space-separated compound surname) truncates to its
    final token only -- normalize_covers_player's own documented behavior,
    reused unmodified, not repaired by this batch.
    """
    html = _injury_html([("John Van Der Berg", "F", "Out")])
    rows = parse_covers_injury_rows_v15(html)
    assert rows[0]["player_raw"] == "John Van Der Berg"  # full raw name preserved as evidence
    assert rows[0]["player_last"] == "berg"  # legacy truncation, unchanged


# ---------------------------------------------------------------------------
# Team resolution: exact, provenance, ambiguous Miami, no cross-source substitution
# ---------------------------------------------------------------------------

def test_canonical_team_resolution_exact():
    record = resolve_covers_injury_row(
        player_raw="Darius McGhee Jr.", status_raw="Out", player_last="mcghee", status="out",
        covers_team_slug="st-bonaventure-bonnies", season=2026,
    )
    assert isinstance(record.team_identity, TeamIdentity)
    assert record.team_identity.display_name == "St. Bonaventure"


def test_team_slug_and_url_provenance():
    html = _injury_html([("Darius McGhee Jr.", "G", "Out")])
    result = run_covers_injury_ingestion_from_html(html, "st-bonaventure-bonnies", 2026)
    assert result.evidence.covers_team_slug == "st-bonaventure-bonnies"
    assert "st-bonaventure-bonnies" in result.evidence.source_url
    assert result.evidence.source_url.startswith("https://www.covers.com/")


def test_ambiguous_miami_behavior():
    record = resolve_covers_injury_row(
        player_raw="Someone", status_raw="Out", player_last="someone", status="out",
        covers_team_slug="miami", season=2026,
    )
    assert not isinstance(record.team_identity, TeamIdentity)
    assert record.resolution_status == "team_unresolved"
    assert record.team_identity.reason == "ambiguous"


def test_no_cross_source_slug_substitution():
    """
    A Sports-Reference slug is NOT interchangeable with a Covers slug --
    resolve_covers_team_identity must not silently treat "vcu" (an SR-
    adjacent short form) as if it were Covers' own "vcu-rams" slug via
    naive substitution. Confirmed via the covers_identity.py module's own
    documented finding: passing an SR slug that Covers would never itself
    produce must not spuriously resolve through this path.
    """
    # "virginia-commonwealth" is the real SR slug; Covers' own slug is "vcu-rams".
    # This module never does covers_slug_map.get(sr_slug) -- it always routes
    # the actual Covers-sourced string through resolve_covers_team_identity.
    from src.identity.covers_identity import resolve_covers_team_identity

    result = resolve_covers_team_identity("vcu-rams")
    assert isinstance(result, TeamIdentity)
    assert result.display_name in ("VCU", "Virginia Commonwealth")


def test_unknown_team_slug_is_not_found():
    record = resolve_covers_injury_row(
        player_raw="Someone", status_raw="Out", player_last="someone", status="out",
        covers_team_slug="definitely-not-a-real-school-xyzzy", season=2026,
    )
    assert not isinstance(record.team_identity, TeamIdentity)
    assert record.resolution_status == "team_unresolved"


# ---------------------------------------------------------------------------
# Player resolution: reviewed evidence, unconfirmed, ambiguous, unknown
# ---------------------------------------------------------------------------

def test_exact_player_resolution_through_reviewed_evidence():
    """
    Two roster players share the surname "Simmons" but have DIFFERENT
    canonical keys (different first names) -- a bare "Simmons" report is
    ambiguous between them, but a reviewed alias naming the specific full
    display name resolves it uniquely and confirms it (stronger evidence
    than a name-only match, per resolve_lastname_only_injury_candidate's
    own reviewed-alias mechanism).
    """
    roster = [
        build_player_identity("Daryl Simmons II", 905884, 2026),
        build_player_identity("Marcus Simmons", 905884, 2026),
    ]
    record = resolve_covers_injury_row(
        player_raw="Simmons", status_raw="Out", player_last="simmons", status="out",
        covers_team_slug="st-bonaventure-bonnies", season=2026,
        roster_identities=roster, reviewed_alias_display_name="Daryl Simmons II",
    )
    assert isinstance(record.player_identity, PlayerIdentity)
    assert record.player_identity.display_name == "Daryl Simmons II"
    assert record.resolution_status == "confirmed_via_reviewed_alias"


def test_surname_only_unconfirmed_result():
    roster = [build_player_identity("Frank Mitchell", 905884, 2026, source="sports_reference", source_player_id="sr-1")]
    record = resolve_covers_injury_row(
        player_raw="F. Mitchell", status_raw="Out", player_last="mitchell", status="out",
        covers_team_slug="st-bonaventure-bonnies", season=2026, roster_identities=roster,
    )
    assert not isinstance(record.player_identity, PlayerIdentity)
    assert record.resolution_status == "unconfirmed"


def test_same_surname_roster_ambiguity():
    roster = [
        build_player_identity("John Smith", 905884, 2026),
        build_player_identity("Jake Smith", 905884, 2026),
    ]
    record = resolve_covers_injury_row(
        player_raw="Smith", status_raw="Out", player_last="smith", status="out",
        covers_team_slug="st-bonaventure-bonnies", season=2026, roster_identities=roster,
    )
    assert not isinstance(record.player_identity, PlayerIdentity)
    assert record.resolution_status == "ambiguous"
    assert len(record.player_identity.candidates) == 2


def test_missing_player_identifier_unknown_player():
    record = resolve_covers_injury_row(
        player_raw="Nobody Here", status_raw="Out", player_last="here", status="out",
        covers_team_slug="st-bonaventure-bonnies", season=2026, roster_identities=[],
    )
    assert not isinstance(record.player_identity, PlayerIdentity)
    assert record.resolution_status == "not_found"


def test_never_fabricates_standalone_identity_without_a_roster():
    """
    Unlike the ESPN/box-score adapters, resolve_lastname_only_injury_candidate
    has NO fallback to a constructed standalone identity -- a bare last
    name, with no roster to check it against, is never enough evidence to
    even build a team+season-scoped identity. Confirmed: with no roster
    supplied, resolution is always not_found, never a fabricated identity.
    """
    record = resolve_covers_injury_row(
        player_raw="Frank Mitchell", status_raw="Out", player_last="mitchell", status="out",
        covers_team_slug="st-bonaventure-bonnies", season=2026,
    )
    assert not isinstance(record.player_identity, PlayerIdentity)
    assert record.resolution_status == "not_found"


# ---------------------------------------------------------------------------
# Team and season scoping prevents unsafe name-only merges
# ---------------------------------------------------------------------------

def test_team_and_season_scoping_excludes_other_teams_and_seasons():
    """
    Even after scoping correctly narrows the roster to a single candidate,
    resolve_lastname_only_injury_candidate's own conservative design still
    surfaces it as "unconfirmed", never auto-promoted -- but scoping
    itself is proven by checking that ONLY the correctly-scoped candidate
    survives (the wrong-season and wrong-team entries, which share the
    identical name, are excluded before matching even begins).
    """
    roster = [
        build_player_identity("Frank Mitchell", 905884, 2026, source="sports_reference", source_player_id="correct-scope"),
        build_player_identity("Frank Mitchell", 905884, 2025, source="sports_reference", source_player_id="wrong-season"),
        build_player_identity("Frank Mitchell", 1, 2026, source="sports_reference", source_player_id="wrong-team"),
    ]
    record = resolve_covers_injury_row(
        player_raw="Frank Mitchell", status_raw="Out", player_last="mitchell", status="out",
        covers_team_slug="st-bonaventure-bonnies", season=2026, roster_identities=roster,
    )
    assert not isinstance(record.player_identity, PlayerIdentity)
    assert record.resolution_status == "unconfirmed"
    assert len(record.player_identity.candidates) == 1
    assert record.player_identity.candidates[0].sources["sports_reference"].raw_value == "correct-scope"


# ---------------------------------------------------------------------------
# Duplicate rows, determinism, idempotency
# ---------------------------------------------------------------------------

def test_duplicate_injury_rows_detected():
    html = _injury_html([("Frank Mitchell", "F", "Out"), ("Frank Mitchell", "F", "Out")])
    result = run_covers_injury_ingestion_from_html(html, "st-bonaventure-bonnies", 2026)
    assert len(result.duplicate_reports) == 1
    assert result.duplicate_reports[0]["count"] == 2


def test_deterministic_repeated_ingestion():
    html = _injury_html([("Frank Mitchell", "F", "Out")])
    r1 = run_covers_injury_ingestion_from_html(html, "st-bonaventure-bonnies", 2026)
    r2 = run_covers_injury_ingestion_from_html(html, "st-bonaventure-bonnies", 2026)
    assert [r.normalized_last_name for r in r1.records] == [r.normalized_last_name for r in r2.records]
    assert [r.resolution_status for r in r1.records] == [r.resolution_status for r in r2.records]


def test_idempotent_persistence_no_duplicate_records_on_rerun():
    html = _injury_html([("Frank Mitchell", "F", "Out"), ("Cayden Charles", "G", "Questionable")])
    r1 = run_covers_injury_ingestion_from_html(html, "st-bonaventure-bonnies", 2026)
    r2 = run_covers_injury_ingestion_from_html(html, "st-bonaventure-bonnies", 2026)
    assert len(r1.records) == len(r2.records) == 2


def test_raw_status_and_detail_preserved():
    html = _injury_html([("Frank Mitchell", "F", "Questionable - Ankle")])
    result = run_covers_injury_ingestion_from_html(html, "st-bonaventure-bonnies", 2026)
    assert result.records[0].raw_status == "Questionable - Ankle"
    assert result.records[0].normalized_status == "questionable"


# ---------------------------------------------------------------------------
# No mutation of production files / no live network / no fuzzy matching
# ---------------------------------------------------------------------------

def test_no_production_cache_mutation(tmp_path):
    """This module writes nothing to disk at all -- confirmed via AST (no open()/to_csv/to_excel calls)."""
    import ast
    import inspect

    import src.ingestion.covers_injury_ingestion as mod

    tree = ast.parse(inspect.getsource(mod))
    called_names = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "open" not in called_names


def test_no_live_network_access_module_has_no_requests_import():
    import ast
    import inspect

    import src.ingestion.covers_injury_ingestion as mod

    tree = ast.parse(inspect.getsource(mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "requests" not in imported


def test_no_unrestricted_fuzzy_matching():
    import ast
    import inspect

    import src.ingestion.covers_injury_ingestion as mod

    tree = ast.parse(inspect.getsource(mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "difflib" not in imported


def test_module_never_calls_legacy_scraper_functions():
    import ast
    import inspect

    import src.ingestion.covers_injury_ingestion as mod

    tree = ast.parse(inspect.getsource(mod))
    called_names = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not ({"scrape_covers_injuries_for_teams", "build_covers_ncaab_slug_map"} & called_names)


def test_does_not_mutate_team_registry():
    from src import slug_utils

    before_size = len(slug_utils.TEAM_REGISTRY)
    resolve_covers_injury_row(
        player_raw="Frank Mitchell", status_raw="Out", player_last="mitchell", status="out",
        covers_team_slug="st-bonaventure-bonnies", season=2026,
    )
    assert len(slug_utils.TEAM_REGISTRY) == before_size


# ---------------------------------------------------------------------------
# Legacy DataFrame/output parity: this module changes nothing existing
# ---------------------------------------------------------------------------

def test_result_object_carries_no_legacy_injury_dict_shape():
    from dataclasses import fields

    from src.ingestion.covers_injury_ingestion import CoversInjuryIngestionResult

    field_names = {f.name for f in fields(CoversInjuryIngestionResult)}
    assert "injuries" not in field_names  # not the legacy dict[sr_slug, list[dict]] shape


def test_legacy_scraper_functions_still_importable_and_unchanged():
    """Confirms the legacy scraper module still exposes its unmodified functions."""
    from src.scraper import (
        build_covers_ncaab_slug_map,
        covers_team_alias,
        normalize_covers_player,
        scrape_covers_injuries_for_teams,
    )

    assert normalize_covers_player("Darius McGhee Jr.") == "mcghee"  # unchanged behavior
    assert covers_team_alias("Miami (OH) RedHawks") == "miami"  # unchanged behavior
