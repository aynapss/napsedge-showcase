"""
Phase 3, batch 2: ESPN team/player-stat canonical ingestion.
No live requests -- real cached CSV fixture + narrowly-mocked HTML shaped
from extract_espn_player_row_v15/extract_espn_athlete_id's own expectations.
"""
from pathlib import Path

import pytest

from src.identity.contracts import PlayerIdentity, TeamIdentity, UnresolvedIdentity
from src.identity.player_identity import build_player_identity
from src.ingestion.espn_ingestion import (
    EspnPlayerIngestionRecord,
    parse_espn_player_rows_from_csv,
    parse_espn_player_rows_from_html,
    resolve_espn_ingestion_row,
    run_espn_player_ingestion_from_csv,
    run_espn_player_ingestion_from_html,
)

REAL_ESPN_CSV = Path("TeamDump/ESPN/espn_full_player_stats.csv")

MOCK_HTML_TEMPLATE = """
<table><tbody>
{rows}
</tbody></table>
<table><thead><tr><th>PTS</th></tr></thead><tbody>{stat_rows}</tbody></table>
"""


def _mock_html(entries: list[tuple[str, str, str | None]]) -> str:
    """entries: (name, team_code, athlete_id_or_None)"""
    rows = []
    stat_rows = []
    for name, team, aid in entries:
        href = f"/mens-college-basketball/player/_/id/{aid}/{name.lower().replace(' ', '-')}" if aid else "#"
        rows.append(
            f'<tr><td>1</td><td><a href="{href}">{name}</a>'
            f'<span class="athleteCell__teamAbbrev">{team}</span></td></tr>'
        )
        stat_rows.append("<tr><td>10.0</td></tr>")
    return MOCK_HTML_TEMPLATE.format(rows="\n".join(rows), stat_rows="\n".join(stat_rows))


# ---------------------------------------------------------------------------
# Parsing: HTML (with athlete IDs) and CSV (without)
# ---------------------------------------------------------------------------

def test_parse_espn_player_rows_from_html_extracts_athlete_id():
    html = _mock_html([("AJ Dybantsa", "BYU", "4433137")])
    rows = parse_espn_player_rows_from_html(html)
    assert len(rows) == 1
    assert rows[0]["raw_display_name"] == "AJ Dybantsa"
    assert rows[0]["raw_team_code"] == "BYU"
    assert rows[0]["espn_athlete_id"] == "4433137"


def test_parse_espn_player_rows_from_html_missing_athlete_id():
    html = _mock_html([("No Id Player", "ECU", None)])
    rows = parse_espn_player_rows_from_html(html)
    assert rows[0]["espn_athlete_id"] is None


def test_parse_espn_player_rows_from_html_malformed_html_no_crash():
    rows = parse_espn_player_rows_from_html("<html><body>not a table</body></html>")
    assert rows == []


def test_parse_espn_player_rows_from_html_empty_string():
    assert parse_espn_player_rows_from_html("") == []


@pytest.mark.skipif(not REAL_ESPN_CSV.exists(), reason="real ESPN fixture not present in this checkout")
def test_parse_espn_player_rows_from_csv_real_fixture_no_athlete_id():
    rows = parse_espn_player_rows_from_csv(str(REAL_ESPN_CSV))
    assert len(rows) == 3121
    assert all(r["espn_athlete_id"] is None for r in rows)  # never fabricated
    assert rows[0]["raw_display_name"]
    assert rows[0]["raw_team_code"]


# ---------------------------------------------------------------------------
# Team + season scoping, canonical resolution
# ---------------------------------------------------------------------------

def test_resolve_espn_ingestion_row_exact_team_resolution():
    record = resolve_espn_ingestion_row("AJ Dybantsa", "BYU", 2026)
    assert isinstance(record.team_identity, TeamIdentity)
    assert record.team_identity.display_name == "Brigham Young"
    assert record.resolution_status == "resolved"


def test_resolve_espn_ingestion_row_unknown_team_code_unresolved():
    record = resolve_espn_ingestion_row("Someone", "ECU", 2026)
    assert not isinstance(record.team_identity, TeamIdentity)
    assert record.resolution_status == "team_unresolved"


def test_resolve_espn_ingestion_row_requires_season_no_default():
    import inspect

    sig = inspect.signature(resolve_espn_ingestion_row)
    assert sig.parameters["season"].default is inspect.Parameter.empty


def test_resolve_espn_ingestion_row_scopes_player_by_team_and_season():
    record = resolve_espn_ingestion_row("AJ Dybantsa", "BYU", 2026)
    assert isinstance(record.player_identity, PlayerIdentity)
    assert record.player_identity.team_id == record.team_identity.team_id
    assert record.player_identity.season == 2026


# ---------------------------------------------------------------------------
# Athlete-ID capture, missing IDs, source-id preference
# ---------------------------------------------------------------------------

def test_resolve_espn_ingestion_row_captures_real_athlete_id():
    record = resolve_espn_ingestion_row("AJ Dybantsa", "BYU", 2026, espn_athlete_id="4433137")
    assert isinstance(record.player_identity, PlayerIdentity)
    assert record.player_identity.player_id == "espn:4433137"
    assert record.espn_athlete_id == "4433137"


def test_resolve_espn_ingestion_row_missing_athlete_id_falls_back_to_scoped_key():
    record = resolve_espn_ingestion_row("Walk On Guy", "BYU", 2026, espn_athlete_id=None)
    assert isinstance(record.player_identity, PlayerIdentity)
    assert "espn" not in record.player_identity.sources
    assert record.espn_athlete_id is None


def test_reused_athlete_id_across_legitimate_transfer_is_not_flagged():
    from src.ingestion.espn_ingestion import _build_conflict_findings

    r1 = resolve_espn_ingestion_row("Jane Doe", "BYU", 2025, espn_athlete_id="999")
    r2 = resolve_espn_ingestion_row("Jane Doe", "Duke", 2026, espn_athlete_id="999")
    reused, _ = _build_conflict_findings([r1, r2])
    assert reused == []  # different (team, season) -- a legitimate transfer, not a conflict


def test_conflicting_athlete_ids_within_one_roster_detected():
    from src.ingestion.espn_ingestion import _build_conflict_findings

    r1 = resolve_espn_ingestion_row("Player A", "BYU", 2026, espn_athlete_id="777")
    r2 = resolve_espn_ingestion_row("Player B", "BYU", 2026, espn_athlete_id="777")
    reused, _ = _build_conflict_findings([r1, r2])
    assert len(reused) == 1


# ---------------------------------------------------------------------------
# Same-name players: different teams, same roster
# ---------------------------------------------------------------------------

def test_same_name_players_on_different_teams_not_merged():
    r1 = resolve_espn_ingestion_row("Josh Smith", "BYU", 2026)
    r2 = resolve_espn_ingestion_row("Josh Smith", "Duke", 2026)
    assert r1.player_identity.team_id != r2.player_identity.team_id
    assert r1.player_identity.player_id != r2.player_identity.player_id


def test_same_name_players_within_one_roster_via_link_ambiguous():
    roster = [
        build_player_identity("John Smith", 383203, 2026, source="sports_reference", source_player_id="a"),
        build_player_identity("John Smith Jr.", 383203, 2026, source="sports_reference", source_player_id="b"),
    ]
    record = resolve_espn_ingestion_row("John Smith", "BYU", 2026, roster_identities=roster)
    assert isinstance(record.linked_roster_identity, UnresolvedIdentity)
    assert record.linked_roster_identity.reason == "ambiguous"


def test_multi_id_roster_conflict_detection():
    from src.ingestion.espn_ingestion import _build_conflict_findings

    roster = [build_player_identity("Frank Mitchell", 383203, 2026, source="sports_reference", source_player_id="sr-1")]
    r1 = resolve_espn_ingestion_row("Frank Mitchell", "BYU", 2026, espn_athlete_id="111", roster_identities=roster)
    r2 = resolve_espn_ingestion_row("Frank Mitchell", "BYU", 2026, espn_athlete_id="222", roster_identities=roster)
    _, multi_id = _build_conflict_findings([r1, r2])
    assert len(multi_id) == 1


# ---------------------------------------------------------------------------
# Unknown / ambiguous, no fuzzy matching
# ---------------------------------------------------------------------------

def test_ambiguous_team_code_not_silently_resolved():
    record = resolve_espn_ingestion_row("Someone", "MVSU", 2026)  # confirmed real ambiguous code
    assert not isinstance(record.team_identity, TeamIdentity)


def test_no_fuzzy_matching_in_module():
    import ast
    import inspect

    import src.ingestion.espn_ingestion as mod

    tree = ast.parse(inspect.getsource(mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert "difflib" not in imported


# ---------------------------------------------------------------------------
# Determinism / idempotency
# ---------------------------------------------------------------------------

def test_deterministic_repeated_resolution():
    ids = {resolve_espn_ingestion_row("AJ Dybantsa", "BYU", 2026, espn_athlete_id="4433137").player_identity.player_id for _ in range(3)}
    assert len(ids) == 1


def test_html_ingestion_idempotent_repeated_execution():
    html = _mock_html([("AJ Dybantsa", "BYU", "4433137"), ("Frank Mitchell", "SBU", None)])
    r1 = run_espn_player_ingestion_from_html(html, season=2026)
    r2 = run_espn_player_ingestion_from_html(html, season=2026)
    ids1 = [r.player_identity.player_id if isinstance(r.player_identity, PlayerIdentity) else None for r in r1.records]
    ids2 = [r.player_identity.player_id if isinstance(r.player_identity, PlayerIdentity) else None for r in r2.records]
    assert ids1 == ids2
    assert len(r1.records) == len(r2.records)


@pytest.mark.skipif(not REAL_ESPN_CSV.exists(), reason="real ESPN fixture not present in this checkout")
def test_csv_ingestion_idempotent_no_duplicates_on_rerun():
    r1 = run_espn_player_ingestion_from_csv(str(REAL_ESPN_CSV), season=2026)
    r2 = run_espn_player_ingestion_from_csv(str(REAL_ESPN_CSV), season=2026)
    assert len(r1.records) == len(r2.records) == 3121


# ---------------------------------------------------------------------------
# Raw provenance retention
# ---------------------------------------------------------------------------

def test_html_evidence_provenance_fields():
    html = _mock_html([("AJ Dybantsa", "BYU", "4433137")])
    result = run_espn_player_ingestion_from_html(html, season=2026)
    assert result.evidence.fixture_provenance == "html"
    assert result.evidence.source_url_or_endpoint == "https://www.espn.com/mens-college-basketball/stats/player"
    assert result.evidence.parser_version
    assert result.evidence.collected_at_utc


@pytest.mark.skipif(not REAL_ESPN_CSV.exists(), reason="real ESPN fixture not present in this checkout")
def test_csv_evidence_provenance_fields():
    result = run_espn_player_ingestion_from_csv(str(REAL_ESPN_CSV), season=2026)
    assert result.evidence.fixture_provenance == f"cached_csv:{REAL_ESPN_CSV}"


def test_raw_display_name_and_team_code_preserved_verbatim():
    record = resolve_espn_ingestion_row("AJ Dybantsa", "BYU", 2026, espn_athlete_id="4433137")
    assert record.raw_display_name == "AJ Dybantsa"
    assert record.raw_team_code == "BYU"


# ---------------------------------------------------------------------------
# Legacy DataFrame/CSV parity: no mutation, no schema change
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not REAL_ESPN_CSV.exists(), reason="real ESPN fixture not present in this checkout")
def test_csv_ingestion_does_not_modify_the_real_csv_file():
    import os

    before_mtime = os.path.getmtime(REAL_ESPN_CSV)
    before_size = os.path.getsize(REAL_ESPN_CSV)
    run_espn_player_ingestion_from_csv(str(REAL_ESPN_CSV), season=2026)
    assert os.path.getmtime(REAL_ESPN_CSV) == before_mtime
    assert os.path.getsize(REAL_ESPN_CSV) == before_size


def test_result_object_carries_no_legacy_dataframe_field():
    from dataclasses import fields

    from src.ingestion.espn_ingestion import EspnIngestionResult

    field_names = {f.name for f in fields(EspnIngestionResult)}
    assert not any("dataframe" in n.lower() or "csv_df" in n.lower() for n in field_names)


def test_module_never_calls_scrape_espn_players():
    import ast
    import inspect

    import src.ingestion.espn_ingestion as mod

    tree = ast.parse(inspect.getsource(mod))
    called_names = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "scrape_espn_players" not in called_names


def test_does_not_mutate_team_registry():
    from src import slug_utils

    before_size = len(slug_utils.TEAM_REGISTRY)
    resolve_espn_ingestion_row("AJ Dybantsa", "BYU", 2026, espn_athlete_id="4433137")
    assert len(slug_utils.TEAM_REGISTRY) == before_size
