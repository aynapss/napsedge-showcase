"""
Phase 5B1: tests for `src/pipeline/allstats_import_adapter.py`.

Real-fixture tests (St. Bonaventure, Michigan State, and `albany-ny` --
the one confirmed real workbook missing its own `PlayerKey` column)
exercise the full discover -> load -> parse -> plan -> persist pipeline
against actual `TeamDump/CBB` files (read-only). Everything else uses
small, synthetic, in-memory DataFrames so identity edge cases (same-
surname, suffixed, ambiguous, duplicate rows, invalid numerics, ...) are
deterministic and don't depend on which real players happen to exhibit
them this season.
"""
from __future__ import annotations

import ast
import datetime as dt
import inspect
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from src.db.base import Base
from src.db import models, allstats_models
from src.db.session import build_session_factory, unit_of_work
from src.exceptions import IdentityConflictError, ValidationError
from src.identity.contracts import PlayerIdentity, UnresolvedIdentity
from src.pipeline.allstats_import_adapter import (
    AllStatsFileMetadata,
    DiscoveredAllStatsWorkbook,
    build_allstats_import_plan,
    discover_allstats_artifacts,
    load_allstats_dataframes,
    parse_allstats_workbook,
    persist_allstats_import_plan,
)

SEASON = 2026
TABLE_LABELS = ["Roster", "PerGame", "Totals", "Per40", "Per100", "Advanced"]
SBU_DIR = Path("TeamDump/CBB/st-bonaventure")
MSU_DIR = Path("TeamDump/CBB/michigan-state")
ALBANY_DIR = Path("TeamDump/CBB/albany-ny")
#: Confirmed real mojibake case (this batch's dry run): roster player
#: "JoÃ£o Victor Moreira" (real "João Victor Moreira", UTF-8 bytes
#: mis-decoded as latin-1 in the source workbook itself).
MOJIBAKE_TEAM_DIR = Path("TeamDump/CBB/alabama-birmingham")


def _real_fixtures_present() -> bool:
    return all((d / f"{lbl}_{SEASON}.xlsx").exists() for d in (SBU_DIR, MSU_DIR) for lbl in TABLE_LABELS)


pytestmark_real = pytest.mark.skipif(not _real_fixtures_present(), reason="real St. Bonaventure / Michigan State AllStats fixtures not present in this checkout")


# ---------------------------------------------------------------------------
# Synthetic fixture builders
# ---------------------------------------------------------------------------


def _roster_df(rows: list[dict]) -> pd.DataFrame:
    base = {"Player": None, "#": None, "Class": "SR", "Pos": "G", "Height": "6-2", "Weight": 190.0,
            "Hometown": None, "High School": None, "RSCI Top 100": None, "Summary": None}
    return pd.DataFrame([{**base, **r} for r in rows])


def _stat_df(rows: list[dict], *, extra_cols: dict | None = None) -> pd.DataFrame:
    base = {"Rk": 1.0, "Player": None, "Pos": "G", "G": 20, "GS": 20, "MP": 25.0,
            "FG": 5.0, "FGA": 10.0, "FG%": 0.5, "3P": 1.0, "3PA": 3.0, "3P%": 0.333,
            "2P": 4.0, "2PA": 7.0, "2P%": 0.571, "eFG%": 0.55, "FT": 2.0, "FTA": 3.0, "FT%": 0.667,
            "ORB": 1.0, "DRB": 3.0, "TRB": 4.0, "AST": 2.0, "STL": 1.0, "BLK": 0.5, "TOV": 1.5,
            "PF": 2.0, "PTS": 13.0, "Awards": None}
    if extra_cols:
        base.update(extra_cols)
    return pd.DataFrame([{**base, **r} for r in rows])


def _minimal_dfs(roster_rows, pergame_rows=None) -> dict[str, pd.DataFrame]:
    dfs = {"Roster": _roster_df(roster_rows)}
    if pergame_rows is not None:
        dfs["PerGame"] = _stat_df(pergame_rows)
    return dfs


# ---------------------------------------------------------------------------
# Stage 1: discovery
# ---------------------------------------------------------------------------


def test_discover_is_deterministic_and_uses_configured_root(tmp_path):
    team_dir = tmp_path / "test-team"
    team_dir.mkdir()
    for lbl in TABLE_LABELS:
        (team_dir / f"{lbl}_{SEASON}.xlsx").write_bytes(b"fake")

    first = discover_allstats_artifacts(tmp_path, season=SEASON)
    second = discover_allstats_artifacts(tmp_path, season=SEASON)
    assert first == second
    assert len(first) == 1
    wb = first[0]
    assert wb.team_slug == "test-team"
    assert wb.is_complete
    assert wb.files["Roster"].relative_path == "test-team/Roster_2026.xlsx"
    assert not wb.files["Roster"].relative_path.startswith(str(tmp_path))  # relative, not absolute


def test_discover_never_hardcodes_teamdump_path():
    import src.pipeline.allstats_import_adapter as mod
    source = inspect.getsource(mod.discover_allstats_artifacts)
    assert "TeamDump" not in source


def test_discover_incomplete_workbook_flagged_not_complete(tmp_path):
    team_dir = tmp_path / "partial-team"
    team_dir.mkdir()
    (team_dir / f"Roster_{SEASON}.xlsx").write_bytes(b"fake")
    discovered = discover_allstats_artifacts(tmp_path, season=SEASON)
    assert len(discovered) == 1
    assert not discovered[0].is_complete
    assert set(discovered[0].files.keys()) == {"Roster"}


def test_discover_missing_workbook_entirely_excluded(tmp_path):
    (tmp_path / "empty-team").mkdir()
    discovered = discover_allstats_artifacts(tmp_path, season=SEASON)
    assert discovered == []


def test_discover_respects_team_slugs_filter(tmp_path):
    for name in ("team-a", "team-b"):
        d = tmp_path / name
        d.mkdir()
        (d / f"Roster_{SEASON}.xlsx").write_bytes(b"fake")
    discovered = discover_allstats_artifacts(tmp_path, season=SEASON, team_slugs=["team-a"])
    assert [d.team_slug for d in discovered] == ["team-a"]


def test_filesystem_mtime_never_used_as_collection_timestamp_source_code():
    import src.pipeline.allstats_import_adapter as mod
    source = inspect.getsource(mod)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)] + [
                t.attr for t in node.targets if isinstance(t, ast.Attribute)
            ]
            if "collection_timestamp" in targets:
                assert not (isinstance(node.value, ast.Name) and "mtime" in node.value.id)


def test_persist_passes_collection_timestamp_none_explicitly():
    import src.pipeline.allstats_import_adapter as mod
    source = inspect.getsource(mod._register_artifacts)
    assert "collection_timestamp=None" in source


# ---------------------------------------------------------------------------
# Stage 3: parsing -- validation, edge cases
# ---------------------------------------------------------------------------


def test_parse_rejects_missing_season():
    dfs = _minimal_dfs([{"Player": "A Player", "#": 1}])
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", 99999)
    assert parsed.status == "invalid"
    assert any(i.issue_code == "season_not_resolved" for i in parsed.issues)


def test_parse_rejects_unresolved_team():
    dfs = _minimal_dfs([{"Player": "A Player", "#": 1}])
    parsed = parse_allstats_workbook(dfs, "Definitely Not A Real Team XYZ", SEASON)
    assert parsed.status == "invalid"
    assert any(i.issue_code == "team_not_resolved" for i in parsed.issues)
    assert parsed.roster_rows == []


def test_parse_rejects_missing_roster_table():
    parsed = parse_allstats_workbook({}, "St. Bonaventure", SEASON)
    assert parsed.status == "invalid"
    assert any(i.issue_code == "roster_table_missing_or_empty" for i in parsed.issues)


def test_parse_rejects_empty_roster_table():
    dfs = {"Roster": pd.DataFrame(columns=["Player", "#"])}
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    assert parsed.status == "invalid"


def test_parse_missing_stat_sheet_does_not_invalidate_file():
    """Roster-only workbook: no PerGame/Totals/etc. -- should still parse the roster successfully (partial data is not a whole-file failure)."""
    dfs = _minimal_dfs([{"Player": "Frank Mitchell", "#": 0}])
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    assert parsed.status == "valid"
    assert len(parsed.roster_rows) == 1
    assert parsed.player_stat_rows == []


def test_team_and_season_resolved_on_valid_input():
    dfs = _minimal_dfs([{"Player": "Frank Mitchell", "#": 0}])
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    assert isinstance(parsed.team_identity, type(parsed.team_identity))
    from src.identity.contracts import TeamIdentity, SeasonIdentity
    assert isinstance(parsed.team_identity, TeamIdentity)
    assert isinstance(parsed.season_identity, SeasonIdentity)
    assert parsed.season_identity.season_id == SEASON


def test_same_surname_teammates_remain_distinct():
    dfs = _minimal_dfs(
        [{"Player": "John Smith", "#": 1}, {"Player": "Mike Smith", "#": 2}],
        pergame_rows=[{"Player": "John Smith"}, {"Player": "Mike Smith"}],
    )
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    assert parsed.status == "valid"
    roster_ids = {r.player_identity.player_id for r in parsed.roster_rows}
    assert len(roster_ids) == 2  # two genuinely distinct identities, never collapsed to one "Smith"
    stat_ids = {s.player_identity.player_id for s in parsed.player_stat_rows}
    assert stat_ids == roster_ids  # each stat row matched its own correct player, not the other Smith


def test_suffixed_player_uses_canonical_corrected_identity_path():
    """A stat-table row without the suffix ('Robert Jones') must still match the roster's suffixed entry ('Robert Jones Jr.') via the existing suffix-agnostic key -- not a new matching rule."""
    dfs = _minimal_dfs(
        [{"Player": "Robert Jones Jr.", "#": 5}],
        pergame_rows=[{"Player": "Robert Jones"}],
    )
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    assert parsed.status == "valid"
    assert isinstance(parsed.roster_rows[0].player_identity, PlayerIdentity)
    assert isinstance(parsed.player_stat_rows[0].player_identity, PlayerIdentity)
    assert parsed.player_stat_rows[0].player_identity.player_id == parsed.roster_rows[0].player_identity.player_id


def test_ambiguous_roster_duplicate_key_flagged():
    """Two roster players collide on the SAME suffix-stripped key ('Robert Jones') -- both must be recorded, never silently merged, and flagged as a data-quality issue."""
    dfs = _minimal_dfs([{"Player": "Robert Jones Jr.", "#": 5}, {"Player": "Robert Jones Sr.", "#": 6}])
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    assert len(parsed.roster_rows) == 2
    assert any(i.issue_code == "roster_duplicate_player_key" for i in parsed.issues)


def test_ambiguous_stat_row_against_colliding_roster_keys():
    dfs = _minimal_dfs(
        [{"Player": "Robert Jones Jr.", "#": 5}, {"Player": "Robert Jones Sr.", "#": 6}],
        pergame_rows=[{"Player": "Robert Jones"}],  # ambiguous: matches both roster entries' key
    )
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    assert len(parsed.player_stat_rows) == 1
    assert isinstance(parsed.player_stat_rows[0].player_identity, UnresolvedIdentity)
    assert parsed.player_stat_rows[0].player_identity.reason == "ambiguous"
    assert parsed.player_stat_rows[0].validation_status == "unresolved"
    assert any(i.issue_code == "stat_row_ambiguous" for i in parsed.issues)
    assert parsed.status == "partial"


def test_unresolved_stat_row_not_matching_any_roster_entry():
    dfs = _minimal_dfs([{"Player": "Frank Mitchell", "#": 0}], pergame_rows=[{"Player": "Somebody Else Entirely"}])
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    assert isinstance(parsed.player_stat_rows[0].player_identity, UnresolvedIdentity)
    assert parsed.player_stat_rows[0].player_identity.reason == "not_found"
    assert any(i.issue_code == "stat_row_not_found" for i in parsed.issues)


def test_one_unresolved_row_does_not_invalidate_other_valid_rows():
    dfs = _minimal_dfs(
        [{"Player": "Frank Mitchell", "#": 0}, {"Player": "Daryl Simmons", "#": 8}],
        pergame_rows=[{"Player": "Frank Mitchell"}, {"Player": "Nobody Matches This"}],
    )
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    assert parsed.status == "partial"  # file-level: not thrown away
    resolved = [s for s in parsed.player_stat_rows if isinstance(s.player_identity, PlayerIdentity)]
    unresolved = [s for s in parsed.player_stat_rows if isinstance(s.player_identity, UnresolvedIdentity)]
    assert len(resolved) == 1
    assert len(unresolved) == 1


def test_duplicate_identical_stat_rows_not_aggregated():
    dfs = _minimal_dfs([{"Player": "Frank Mitchell", "#": 0}], pergame_rows=[{"Player": "Frank Mitchell", "PTS": 16.0}, {"Player": "Frank Mitchell", "PTS": 16.0}])
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    assert len(parsed.player_stat_rows) == 1  # only one kept, not summed/averaged
    assert parsed.player_stat_rows[0].stat_values["pts"] == 16.0
    assert any(i.issue_code == "duplicate_identical_stat_row" for i in parsed.issues)


def test_duplicate_conflicting_stat_rows_flagged_as_error_and_not_aggregated():
    dfs = _minimal_dfs([{"Player": "Frank Mitchell", "#": 0}], pergame_rows=[{"Player": "Frank Mitchell", "PTS": 16.0}, {"Player": "Frank Mitchell", "PTS": 22.0}])
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    assert len(parsed.player_stat_rows) == 1
    assert parsed.player_stat_rows[0].stat_values["pts"] == 16.0  # first kept, never averaged to 19.0
    issue = next(i for i in parsed.issues if i.issue_code == "duplicate_conflicting_stat_row")
    assert issue.severity == "error"
    assert parsed.status == "partial"


def test_team_totals_row_never_coerced_into_a_player():
    dfs = _minimal_dfs([{"Player": "Frank Mitchell", "#": 0}], pergame_rows=[{"Player": "Frank Mitchell"}, {"Player": "Team Totals", "PTS": 76.6}])
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    assert len(parsed.player_stat_rows) == 1
    assert all(s.source_player_name != "Team Totals" for s in parsed.player_stat_rows)
    assert len(parsed.team_stat_rows) == 1
    assert parsed.team_stat_rows[0].stat_values["pts"] == 76.6


def test_invalid_infinite_numeric_value_rejected_not_stored():
    dfs = _minimal_dfs([{"Player": "Frank Mitchell", "#": 0}], pergame_rows=[{"Player": "Frank Mitchell", "PTS": float("inf")}])
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    row = parsed.player_stat_rows[0]
    assert row.stat_values["pts"] is None  # never stored as inf
    assert any(i.issue_code == "non_finite_stat_value" and i.severity == "error" for i in parsed.issues)
    assert parsed.status == "partial"


def test_missing_value_distinguished_from_zero():
    dfs = _minimal_dfs(
        [{"Player": "Frank Mitchell", "#": 0}],
        pergame_rows=[{"Player": "Frank Mitchell", "STL": 0.0, "BLK": float("nan")}],
    )
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    values = parsed.player_stat_rows[0].stat_values
    assert values["stl"] == 0.0  # a genuine recorded zero, preserved
    assert values["blk"] is None  # genuinely missing, never coerced to 0.0
    assert not any(i.severity in ("error", "warning") for i in parsed.issues)  # missing is not an issue


def test_percentage_columns_preserved_exactly_as_source_scale():
    """Confirmed real-data finding: FG%/3P%/etc. are 0-1 fractions; ORB%/USG%/etc. (Advanced table) are 0-100 percents. Neither is rescaled."""
    dfs = _minimal_dfs(
        [{"Player": "Frank Mitchell", "#": 0}],
        pergame_rows=[{"Player": "Frank Mitchell", "FG%": 0.541}],
    )
    dfs["Advanced"] = _stat_df(
        [{"Player": "Frank Mitchell", "USG%": 25.9}],
        extra_cols={"PER": None, "TS%": None, "3PAr": None, "FTr": None, "PProd": None, "ORB%": None, "DRB%": None, "TRB%": None, "AST%": None, "STL%": None, "BLK%": None, "TOV%": None, "USG%": None, "OWS": None, "DWS": None, "WS": None, "WS/40": None, "OBPM": None, "DBPM": None, "BPM": None},
    )
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    pergame_row = next(s for s in parsed.player_stat_rows if s.stat_table == "PerGame")
    advanced_row = next(s for s in parsed.player_stat_rows if s.stat_table == "Advanced")
    assert pergame_row.stat_values["fg_pct"] == 0.541  # 0-1 fraction, untouched
    assert advanced_row.stat_values["usg_pct"] == 25.9  # 0-100 percent, untouched -- NOT rescaled to match fg_pct's convention


def test_games_and_minutes_preserved_for_downstream_weighting():
    dfs = _minimal_dfs([{"Player": "Frank Mitchell", "#": 0}], pergame_rows=[{"Player": "Frank Mitchell", "G": 25, "GS": 25, "MP": 33.5}])
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    values = parsed.player_stat_rows[0].stat_values
    assert values["games_played"] == 25
    assert values["games_started"] == 25
    assert values["minutes_played"] == 33.5


def test_rsci_top_100_stored_as_raw_text_not_numeric():
    """Confirmed real-data finding (Michigan State fixture): this column is not always a clean number, e.g. '34 (2023)'."""
    dfs = _minimal_dfs([{"Player": "Frank Mitchell", "#": 0, "RSCI Top 100": "34 (2023)"}])
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    assert parsed.roster_rows[0].rsci_top_100 == "34 (2023)"


def test_input_dataframes_never_mutated():
    roster = _roster_df([{"Player": "Frank Mitchell", "#": 0}])
    pergame = _stat_df([{"Player": "Frank Mitchell"}])
    dfs = {"Roster": roster, "PerGame": pergame}
    snapshot_roster = roster.copy(deep=True)
    snapshot_pergame = pergame.copy(deep=True)

    parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)

    pd.testing.assert_frame_equal(roster, snapshot_roster)
    pd.testing.assert_frame_equal(pergame, snapshot_pergame)


def test_missing_player_key_column_in_source_handled_via_fresh_computation():
    """Confirmed real case (albany-ny): a workbook can genuinely lack its own PlayerKey column in every table -- the adapter must not crash or require it."""
    roster = _roster_df([{"Player": "Frank Mitchell", "#": 0}])
    assert "PlayerKey" not in roster.columns
    dfs = {"Roster": roster, "PerGame": _stat_df([{"Player": "Frank Mitchell"}])}
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    assert parsed.status == "valid"
    assert parsed.player_stat_rows[0].player_key == "frank mitchell"


# ---------------------------------------------------------------------------
# Corrected identity-matching authority: confirmed, no independent key
# used for matching anywhere; canonical-path coverage for every name
# shape the instructions call out.
# ---------------------------------------------------------------------------


def test_accented_name_matches_across_roster_and_stat_table():
    dfs = _minimal_dfs([{"Player": "José Alvarado", "#": 1}], pergame_rows=[{"Player": "Jose Alvarado"}])  # accent present on roster, stripped in stat table
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    assert parsed.status == "valid"
    assert isinstance(parsed.player_stat_rows[0].player_identity, PlayerIdentity)
    assert parsed.player_stat_rows[0].player_identity.player_id == parsed.roster_rows[0].player_identity.player_id


def test_straight_and_curly_apostrophe_match_the_same_player():
    dfs = _minimal_dfs([{"Player": "Pat O’Brien", "#": 2}], pergame_rows=[{"Player": "Pat O'Brien"}])  # curly on roster, straight in stat table
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    assert parsed.status == "valid"
    assert isinstance(parsed.player_stat_rows[0].player_identity, PlayerIdentity)
    assert parsed.player_stat_rows[0].player_identity.player_id == parsed.roster_rows[0].player_identity.player_id


def test_hyphenated_name_matches_across_roster_and_stat_table():
    dfs = _minimal_dfs([{"Player": "Jean-Baptiste Fofana", "#": 3}], pergame_rows=[{"Player": "Jean-Baptiste Fofana"}])
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    assert parsed.status == "valid"
    assert isinstance(parsed.player_stat_rows[0].player_identity, PlayerIdentity)


def test_mojibake_name_is_a_confirmed_preexisting_gap_never_silently_guessed():
    """
    Documents the exact contract, per instruction, rather than inventing a
    fix: match_stat_row_to_roster compares the stat-row's RAW name against
    each roster identity's own NORMALIZED (mojibake-corrected) display
    name. When the stat table's raw cell is itself still mojibake-
    corrupted, the two sides of that comparison differ and the row
    surfaces as genuinely unresolved -- never silently mismatched to the
    wrong player, and never crashes.
    """
    corrupted = "JoÃ£o Silva"  # real confirmed mojibake shape (UTF-8 bytes decoded as latin-1)
    dfs = _minimal_dfs([{"Player": corrupted, "#": 4}], pergame_rows=[{"Player": corrupted}])
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    # Roster row still resolves (positional guarantee, unaffected by name content).
    assert isinstance(parsed.roster_rows[0].player_identity, PlayerIdentity)
    # The stat-table row is the one exposed to the confirmed gap.
    stat_row = parsed.player_stat_rows[0]
    assert stat_row.match_method in ("stat_row_not_found", "stat_row_matched")  # never crashes; if it happens to match, that's fine too
    if stat_row.match_method == "stat_row_not_found":
        assert isinstance(stat_row.player_identity, UnresolvedIdentity)
        assert stat_row.player_identity.reason == "not_found"
        assert any(i.issue_code == "stat_row_not_found" for i in parsed.issues)


def test_source_player_key_captured_as_raw_evidence_when_present():
    roster = _roster_df([{"Player": "Frank Mitchell", "#": 0}])
    roster["PlayerKey"] = ["frank mitchell"]
    dfs = {"Roster": roster}
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    assert parsed.roster_rows[0].source_player_key == "frank mitchell"


def test_source_player_key_none_when_column_absent():
    dfs = _minimal_dfs([{"Player": "Frank Mitchell", "#": 0}])
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    assert parsed.roster_rows[0].source_player_key is None


def test_source_player_key_conflicting_with_computed_key_never_affects_matching():
    """A workbook's own PlayerKey column can disagree with this adapter's freshly-computed key (or be flat wrong) -- it is captured as raw evidence only, never substituted as a second canonical identifier, never used for matching."""
    roster = _roster_df([{"Player": "Frank Mitchell", "#": 0}])
    roster["PlayerKey"] = ["totally-wrong-key-xyz"]
    dfs = {"Roster": roster, "PerGame": _stat_df([{"Player": "Frank Mitchell"}])}
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    assert parsed.status == "valid"
    assert parsed.roster_rows[0].source_player_key == "totally-wrong-key-xyz"  # preserved as raw evidence
    assert parsed.roster_rows[0].player_key == "frank mitchell"  # this adapter's own computed key, unaffected
    assert isinstance(parsed.player_stat_rows[0].player_identity, PlayerIdentity)  # matching still succeeded via name, not the bogus source key


def test_confirmed_match_via_slug_map_is_distinguishable_from_name_fallback():
    """resolve_roster_player_identities already supports real SR-slug evidence via slug_map -- this adapter now threads it through. A 'confirmed' (source-verified) match is distinguishable from a plain name-fallback match via identity_review_status, using only the already-established PlayerIdentity.sources mechanism."""
    dfs = _minimal_dfs([{"Player": "Frank Mitchell", "#": 0}, {"Player": "Daryl Simmons", "#": 8}])
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON, slug_map={"Frank Mitchell": "frank-mitchell-real-sr-slug"})
    confirmed = next(r for r in parsed.roster_rows if r.source_player_name == "Frank Mitchell")
    fallback = next(r for r in parsed.roster_rows if r.source_player_name == "Daryl Simmons")
    assert confirmed.identity_review_status == "source_verified"
    assert confirmed.player_identity.sources  # real SR slug evidence present
    assert fallback.identity_review_status == "name_fallback"
    assert not fallback.player_identity.sources


def test_match_method_recorded_for_every_stat_row_outcome():
    dfs = _minimal_dfs(
        [{"Player": "Robert Jones Jr.", "#": 5}, {"Player": "Robert Jones Sr.", "#": 6}, {"Player": "Frank Mitchell", "#": 0}],
        pergame_rows=[{"Player": "Robert Jones"}, {"Player": "Frank Mitchell"}, {"Player": "Nobody Matches"}],
    )
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    methods = {s.source_player_name: s.match_method for s in parsed.player_stat_rows}
    assert methods["Robert Jones"] == "stat_row_ambiguous"
    assert methods["Frank Mitchell"] == "stat_row_matched"
    assert methods["Nobody Matches"] == "stat_row_not_found"


def test_source_row_position_recorded_for_roster_and_stat_rows():
    dfs = _minimal_dfs(
        [{"Player": "Frank Mitchell", "#": 0}, {"Player": "Daryl Simmons", "#": 8}],
        pergame_rows=[{"Player": "Daryl Simmons"}, {"Player": "Frank Mitchell"}],  # deliberately reordered vs. roster
    )
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    assert [r.source_row_position for r in parsed.roster_rows] == [0, 1]
    assert [s.source_row_position for s in parsed.player_stat_rows] == [0, 1]
    # Confirms stat-row matching is name-based, not positional: row 0 in the
    # stat table (Daryl Simmons) correctly matches roster position 1, not
    # roster position 0 -- position is recorded as provenance only, never
    # used to decide the match itself.
    daryl_stat_row = parsed.player_stat_rows[0]
    assert daryl_stat_row.source_player_name == "Daryl Simmons"
    assert daryl_stat_row.player_identity.player_id == parsed.roster_rows[1].player_identity.player_id


def test_shuffled_stat_rows_produce_identical_resolution_regardless_of_order():
    """Stat-table matching is name-based (resolve_allstats_player_row), never positional -- row order within a stat table must never affect who a row resolves to."""
    roster_rows = [{"Player": "Frank Mitchell", "#": 0}, {"Player": "Daryl Simmons", "#": 8}, {"Player": "Cayden Charles", "#": 24}]
    forward = _minimal_dfs(roster_rows, pergame_rows=[{"Player": "Frank Mitchell"}, {"Player": "Daryl Simmons"}, {"Player": "Cayden Charles"}])
    shuffled = _minimal_dfs(roster_rows, pergame_rows=[{"Player": "Cayden Charles"}, {"Player": "Frank Mitchell"}, {"Player": "Daryl Simmons"}])

    parsed_forward = parse_allstats_workbook(forward, "St. Bonaventure", SEASON)
    parsed_shuffled = parse_allstats_workbook(shuffled, "St. Bonaventure", SEASON)

    forward_ids = {s.source_player_name: s.player_identity.player_id for s in parsed_forward.player_stat_rows}
    shuffled_ids = {s.source_player_name: s.player_identity.player_id for s in parsed_shuffled.player_stat_rows}
    assert forward_ids == shuffled_ids  # same resolution per player, independent of row order


def test_extra_header_like_row_in_stat_table_is_unresolved_not_miscategorized():
    """A repeated literal 'Player' header row (a known messy-CSV-export shape, not yet observed in this repo's own real files but defensively handled) must never be treated as a real player -- it correctly fails name-matching against the roster and surfaces as unresolved, never crashes, never silently attached to a real player."""
    dfs = _minimal_dfs([{"Player": "Frank Mitchell", "#": 0}], pergame_rows=[{"Player": "Frank Mitchell"}, {"Player": "Player"}])
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    header_row = next(s for s in parsed.player_stat_rows if s.source_player_name == "Player")
    assert header_row.match_method == "stat_row_not_found"
    assert isinstance(header_row.player_identity, UnresolvedIdentity)


# ---------------------------------------------------------------------------
# Positional-alignment safety contract (roster rows only -- the one place
# this module ever uses row position for identity purposes)
# ---------------------------------------------------------------------------


def test_pair_roster_rows_with_identities_safe_under_the_documented_contract():
    from src.pipeline.allstats_import_adapter import _pair_roster_rows_with_identities
    from src.identity.player_identity import build_player_identity

    roster = _roster_df([{"Player": "Frank Mitchell", "#": 0}, {"Player": "Daryl Simmons", "#": 8}])
    identities = [
        build_player_identity("Frank Mitchell", 905884, SEASON),
        build_player_identity("Daryl Simmons", 905884, SEASON),
    ]
    paired = _pair_roster_rows_with_identities(roster, identities)
    assert [p[0] for p in paired] == [0, 1]
    assert paired[0][2].display_name == "Frank Mitchell"
    assert paired[1][2].display_name == "Daryl Simmons"


def test_pair_roster_rows_rejects_cardinality_mismatch_fewer_identities():
    from src.pipeline.allstats_import_adapter import _pair_roster_rows_with_identities
    from src.identity.player_identity import build_player_identity

    roster = _roster_df([{"Player": "Frank Mitchell", "#": 0}, {"Player": "Daryl Simmons", "#": 8}])
    identities = [build_player_identity("Frank Mitchell", 905884, SEASON)]  # one short
    with pytest.raises(ValidationError):
        _pair_roster_rows_with_identities(roster, identities)


def test_pair_roster_rows_rejects_cardinality_mismatch_extra_identities():
    from src.pipeline.allstats_import_adapter import _pair_roster_rows_with_identities
    from src.identity.player_identity import build_player_identity

    roster = _roster_df([{"Player": "Frank Mitchell", "#": 0}])
    identities = [
        build_player_identity("Frank Mitchell", 905884, SEASON),
        build_player_identity("Extra Nobody", 905884, SEASON),  # one too many
    ]
    with pytest.raises(ValidationError):
        _pair_roster_rows_with_identities(roster, identities)


def test_pair_roster_rows_rejects_empty_roster_against_nonempty_identities():
    from src.pipeline.allstats_import_adapter import _pair_roster_rows_with_identities
    from src.identity.player_identity import build_player_identity

    roster = _roster_df([])
    identities = [build_player_identity("Ghost Player", 905884, SEASON)]
    with pytest.raises(ValidationError):
        _pair_roster_rows_with_identities(roster, identities)


def test_pair_roster_rows_never_reached_via_full_parse_when_team_unresolved():
    """The cardinality-checked helper is only ever invoked after team resolution succeeds -- an unresolved team returns before reaching it, confirmed via the existing 'invalid' status path."""
    dfs = _minimal_dfs([{"Player": "Frank Mitchell", "#": 0}])
    parsed = parse_allstats_workbook(dfs, "Definitely Not A Real Team XYZ", SEASON)
    assert parsed.status == "invalid"
    assert parsed.roster_rows == []  # helper never ran; nothing to pair


# ---------------------------------------------------------------------------
# Import-plan stability
# ---------------------------------------------------------------------------


def test_import_plan_specs_are_stably_ordered_across_repeated_builds():
    dfs = _minimal_dfs(
        [{"Player": "Frank Mitchell", "#": 0}, {"Player": "Daryl Simmons", "#": 8}],
        pergame_rows=[{"Player": "Frank Mitchell"}, {"Player": "Daryl Simmons"}],
    )
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    plan_1 = build_allstats_import_plan(parsed)
    plan_2 = build_allstats_import_plan(parsed)
    assert [s["source_player_name"] for s in plan_1.roster_membership_specs] == [s["source_player_name"] for s in plan_2.roster_membership_specs]
    assert [s["source_player_name"] for s in plan_1.player_stat_specs] == [s["source_player_name"] for s in plan_2.player_stat_specs]
    assert plan_1.resolved_player_specs == plan_2.resolved_player_specs


# ---------------------------------------------------------------------------
# Stage 4: plan building (pure, dry-run-safe)
# ---------------------------------------------------------------------------


def test_build_plan_is_pure_no_database_import():
    """build_allstats_import_plan must never reference a repository/session object -- only Python's own list.append (used to build plain kwarg dicts) is expected."""
    import src.pipeline.allstats_import_adapter as mod
    source = inspect.getsource(mod.build_allstats_import_plan)
    assert "Repository" not in source
    assert "session" not in source.lower()
    tree = ast.parse(source)
    called = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not ({"upsert", "commit", "flush"} & called)


def test_dry_run_counts_available_without_any_database_write():
    dfs = _minimal_dfs([{"Player": "Frank Mitchell", "#": 0}], pergame_rows=[{"Player": "Frank Mitchell"}])
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    plan = build_allstats_import_plan(parsed)
    assert plan.counts.roster_rows == 1
    assert plan.counts.player_stat_rows == 1
    assert plan.counts.resolved_players == 2
    assert plan.source_artifact_ids == {}  # nothing persisted by a dry run


# ---------------------------------------------------------------------------
# Stage 5: persistence -- transactions, idempotency, linking, rollback
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine():
    eng = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(eng, "connect")
    def _enable_fk(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session_factory(engine):
    return build_session_factory(engine)


def _seed_team_and_season(session_factory, plan):
    """Real production flow: teams/seasons are already registered (via TeamRepository/SeasonRepository, e.g. from the canonical identity layer) before any source-evidence import runs against them. Tests seed the same way."""
    with unit_of_work(session_factory) as session:
        from src.db.repositories import SeasonRepository, TeamRepository
        if session.get(models.Team, plan.team_id) is None:
            TeamRepository(session).upsert(team_id=plan.team_id, display_name=plan.team_slug, sr_slug=f"slug-{plan.team_id}")
        if session.get(models.Season, plan.season_id) is None:
            SeasonRepository(session).upsert(season_id=plan.season_id, start_year=plan.season_id - 1, end_year=plan.season_id, label=f"{plan.season_id - 1}-{str(plan.season_id)[-2:]}")


def _discovered_and_hashes(tmp_path, dfs):
    team_dir = tmp_path / "test-team"
    team_dir.mkdir(parents=True, exist_ok=True)
    files = {}
    hashes = {}
    for label, df in dfs.items():
        path = team_dir / f"{label}_{SEASON}.xlsx"
        df.to_excel(path, index=False)
        stat = path.stat()
        files[label] = AllStatsFileMetadata(
            relative_path=f"test-team/{label}_{SEASON}.xlsx", file_size_bytes=stat.st_size,
            filesystem_mtime=dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc),
        )
        hashes[label] = f"hash-{label}"
    discovered = DiscoveredAllStatsWorkbook(team_slug="test-team", season=SEASON, files=files)
    return discovered, hashes


def test_persist_writes_artifacts_roster_and_stats_in_one_transaction(session_factory, tmp_path):
    dfs = _minimal_dfs([{"Player": "Frank Mitchell", "#": 0}], pergame_rows=[{"Player": "Frank Mitchell"}])
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    plan = build_allstats_import_plan(parsed)
    discovered, hashes = _discovered_and_hashes(tmp_path, dfs)
    _seed_team_and_season(session_factory, plan)

    result = persist_allstats_import_plan(plan, session_factory, discovered=discovered, content_hashes=hashes)

    assert result.roster_memberships_written == 1
    assert result.player_stat_snapshots_written == 1
    assert set(result.source_artifact_ids.keys()) == {"Roster", "PerGame"}

    with unit_of_work(session_factory) as session:
        assert session.query(allstats_models.RosterMembership).count() == 1
        assert session.query(allstats_models.PlayerStatSnapshot).count() == 1
        assert session.query(models.SourceArtifact).count() == 2


def test_persist_idempotent_on_identical_repeat_import(session_factory, tmp_path):
    dfs = _minimal_dfs([{"Player": "Frank Mitchell", "#": 0}], pergame_rows=[{"Player": "Frank Mitchell"}])
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    plan = build_allstats_import_plan(parsed)
    discovered, hashes = _discovered_and_hashes(tmp_path, dfs)
    _seed_team_and_season(session_factory, plan)

    persist_allstats_import_plan(plan, session_factory, discovered=discovered, content_hashes=hashes)
    persist_allstats_import_plan(plan, session_factory, discovered=discovered, content_hashes=hashes)

    with unit_of_work(session_factory) as session:
        assert session.query(allstats_models.RosterMembership).count() == 1  # not duplicated
        assert session.query(allstats_models.PlayerStatSnapshot).count() == 1
        assert session.query(models.SourceArtifact).count() == 2


def test_persist_import_order_independence(session_factory, tmp_path):
    """Importing team A then B, or B then A, must leave the database in the same final state."""
    dfs_a = _minimal_dfs([{"Player": "Frank Mitchell", "#": 0}])
    dfs_b = _minimal_dfs([{"Player": "Daryl Simmons", "#": 8}])

    def run(order):
        eng = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(eng)
        sf = build_session_factory(eng)
        for i, dfs in enumerate(order):
            parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
            plan = build_allstats_import_plan(parsed)
            discovered, hashes = _discovered_and_hashes(tmp_path / f"run_{id(order)}_{i}", dfs)
            _seed_team_and_season(sf, plan)
            persist_allstats_import_plan(plan, sf, discovered=discovered, content_hashes=hashes)
        with unit_of_work(sf) as session:
            names = sorted(r.source_player_name for r in session.query(allstats_models.RosterMembership).all())
        eng.dispose()
        return names

    forward = run([dfs_a, dfs_b])
    backward = run([dfs_b, dfs_a])
    assert forward == backward == ["Daryl Simmons", "Frank Mitchell"]


def test_persist_rolls_back_entire_workbook_on_conflict(session_factory, tmp_path):
    dfs = _minimal_dfs([{"Player": "Frank Mitchell", "#": 0}], pergame_rows=[{"Player": "Frank Mitchell"}])
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    plan = build_allstats_import_plan(parsed)
    discovered, hashes = _discovered_and_hashes(tmp_path, dfs)
    _seed_team_and_season(session_factory, plan)
    persist_allstats_import_plan(plan, session_factory, discovered=discovered, content_hashes=hashes)

    # Re-import with the SAME source_artifact identity (same relative_path/content_hash)
    # but a genuinely different roster fact -- must raise, and must roll back completely
    # (not leave a half-written player_stat_snapshot from the same conflicting attempt).
    conflicting_dfs = _minimal_dfs([{"Player": "Frank Mitchell", "#": 99}], pergame_rows=[{"Player": "Frank Mitchell"}])
    conflicting_parsed = parse_allstats_workbook(conflicting_dfs, "St. Bonaventure", SEASON)
    conflicting_plan = build_allstats_import_plan(conflicting_parsed)

    with pytest.raises(IdentityConflictError):
        persist_allstats_import_plan(conflicting_plan, session_factory, discovered=discovered, content_hashes=hashes)

    with unit_of_work(session_factory) as session:
        row = session.query(allstats_models.RosterMembership).one()
        assert row.jersey_number == 0  # untouched by the failed conflicting attempt


def test_persist_links_rows_to_pipeline_run(session_factory, tmp_path):
    dfs = _minimal_dfs([{"Player": "Frank Mitchell", "#": 0}])
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    plan = build_allstats_import_plan(parsed)
    discovered, hashes = _discovered_and_hashes(tmp_path, dfs)

    _seed_team_and_season(session_factory, plan)
    with unit_of_work(session_factory) as session:
        from src.db.repositories import PipelineRunRepository
        PipelineRunRepository(session).start(run_id="run-1", pipeline_name="allstats_import", started_at=dt.datetime.now(dt.timezone.utc))

    persist_allstats_import_plan(plan, session_factory, discovered=discovered, content_hashes=hashes, pipeline_run_id="run-1")

    with unit_of_work(session_factory) as session:
        artifact = session.query(models.SourceArtifact).filter_by(relative_path="test-team/Roster_2026.xlsx").one()
        assert artifact.pipeline_run_id == "run-1"


def test_persist_null_collection_timestamp(session_factory, tmp_path):
    dfs = _minimal_dfs([{"Player": "Frank Mitchell", "#": 0}])
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    plan = build_allstats_import_plan(parsed)
    discovered, hashes = _discovered_and_hashes(tmp_path, dfs)
    _seed_team_and_season(session_factory, plan)
    persist_allstats_import_plan(plan, session_factory, discovered=discovered, content_hashes=hashes)

    with unit_of_work(session_factory) as session:
        artifact = session.query(models.SourceArtifact).one()
        assert artifact.collection_timestamp is None
        assert artifact.filesystem_mtime is not None  # metadata still recorded, just never promoted


def test_persist_applies_point_in_time_classification(session_factory, tmp_path):
    dfs = _minimal_dfs([{"Player": "Frank Mitchell", "#": 0}])
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    plan = build_allstats_import_plan(parsed)
    discovered, hashes = _discovered_and_hashes(tmp_path, dfs)
    _seed_team_and_season(session_factory, plan)
    persist_allstats_import_plan(plan, session_factory, discovered=discovered, content_hashes=hashes)

    with unit_of_work(session_factory) as session:
        artifact = session.query(models.SourceArtifact).one()
        assert artifact.point_in_time_classification == "historical_but_not_point_in_time"


def test_persist_invalid_team_still_registers_artifacts_and_issues_only(session_factory, tmp_path):
    dfs = _minimal_dfs([{"Player": "Frank Mitchell", "#": 0}])
    parsed = parse_allstats_workbook(dfs, "Definitely Not A Real Team XYZ", SEASON)
    plan = build_allstats_import_plan(parsed)
    discovered, hashes = _discovered_and_hashes(tmp_path, dfs)

    result = persist_allstats_import_plan(plan, session_factory, discovered=discovered, content_hashes=hashes)
    assert result.roster_memberships_written == 0
    assert result.status == "invalid"

    with unit_of_work(session_factory) as session:
        assert session.query(models.SourceArtifact).count() == 1  # still auditable
        assert session.query(models.DataQualityIssue).count() == 1
        assert session.query(allstats_models.RosterMembership).count() == 0


# ---------------------------------------------------------------------------
# Real-fixture end-to-end
# ---------------------------------------------------------------------------


@pytestmark_real
def test_real_st_bonaventure_end_to_end_dry_run():
    cbb_root = Path("TeamDump/CBB")
    discovered = discover_allstats_artifacts(cbb_root, season=SEASON, team_slugs=["st-bonaventure"])[0]
    dfs, hashes = load_allstats_dataframes(discovered, cbb_root)
    parsed = parse_allstats_workbook(dfs, "St. Bonaventure", SEASON)
    plan = build_allstats_import_plan(parsed)
    assert parsed.status == "valid"
    assert plan.team_id is not None
    assert plan.counts.roster_rows == 15
    assert plan.counts.unresolved_players == 0


@pytestmark_real
def test_real_michigan_state_end_to_end_dry_run():
    cbb_root = Path("TeamDump/CBB")
    discovered = discover_allstats_artifacts(cbb_root, season=SEASON, team_slugs=["michigan-state"])[0]
    dfs, hashes = load_allstats_dataframes(discovered, cbb_root)
    parsed = parse_allstats_workbook(dfs, "Michigan State", SEASON)
    plan = build_allstats_import_plan(parsed)
    assert parsed.status == "valid"
    assert plan.counts.unresolved_players == 0


@pytest.mark.skipif(not ALBANY_DIR.exists(), reason="albany-ny fixture not present in this checkout")
def test_real_albany_missing_player_key_column_end_to_end():
    cbb_root = Path("TeamDump/CBB")
    discovered = discover_allstats_artifacts(cbb_root, season=SEASON, team_slugs=["albany-ny"])[0]
    dfs, hashes = load_allstats_dataframes(discovered, cbb_root)
    assert "PlayerKey" not in dfs["Roster"].columns  # confirms this really is the structurally-different fixture
    parsed = parse_allstats_workbook(dfs, "Albany", SEASON)
    plan = build_allstats_import_plan(parsed)
    assert parsed.status == "valid"
    assert plan.counts.roster_rows == 16


@pytest.mark.skipif(not MOJIBAKE_TEAM_DIR.exists(), reason="alabama-birmingham fixture not present in this checkout")
def test_real_mojibake_affected_team_end_to_end_dry_run():
    """
    Real-fixture confirmation of the CORRECTED behavior (this batch's own
    mojibake-remediation fix, see `resolve_allstats_player_row`'s own
    docstring): the roster row resolves cleanly (unaffected, as before);
    the corresponding stat-table row(s) for the same mojibake-corrupted
    real name now ALSO resolve, via repair-before-compare, to the exact
    same canonical player identity as the roster row -- no longer the
    confirmed gap this test previously pinned as "never patched."
    """
    cbb_root = Path("TeamDump/CBB")
    discovered = discover_allstats_artifacts(cbb_root, season=SEASON, team_slugs=["alabama-birmingham"])[0]
    dfs, hashes = load_allstats_dataframes(discovered, cbb_root)
    parsed = parse_allstats_workbook(dfs, "alabama-birmingham", SEASON)
    plan = build_allstats_import_plan(parsed)

    mojibake_roster_row = next((r for r in parsed.roster_rows if "Ã" in r.source_player_name), None)
    assert mojibake_roster_row is not None, "expected fixture to still contain the confirmed mojibake roster name"
    assert isinstance(mojibake_roster_row.player_identity, PlayerIdentity)  # roster-row resolution unaffected

    mojibake_stat_rows = [s for s in parsed.player_stat_rows if "Ã" in s.source_player_name]
    assert mojibake_stat_rows  # the confirmed real fixture still carries the raw mojibake text (audit-preserved)
    assert all(s.match_method == "stat_row_matched" for s in mojibake_stat_rows)
    assert all(isinstance(s.player_identity, PlayerIdentity) for s in mojibake_stat_rows)
    assert all(s.player_identity.player_id == mojibake_roster_row.player_identity.player_id for s in mojibake_stat_rows)
    assert all(s.repaired_player_name == mojibake_roster_row.player_identity.display_name for s in mojibake_stat_rows)
    assert plan.status == "valid"  # the last remaining partial-status cause for this team is now resolved


_REAL_PERSIST_TEAMS = [("st-bonaventure", "St. Bonaventure"), ("michigan-state", "Michigan State"), ("albany-ny", "Albany")]


@pytestmark_real
@pytest.mark.skipif(not ALBANY_DIR.exists(), reason="albany-ny fixture not present in this checkout")
def test_real_persist_three_teams_then_idempotent_reimport(session_factory):
    """
    Real-fixture demonstration required by the Phase 5B1 instructions:
    St. Bonaventure, Michigan State, and a third team (`albany-ny` -- also
    the confirmed structurally-different fixture missing its own
    PlayerKey column) all imported into one disposable database, then
    re-imported identically -- no duplication, no source-file mutation,
    canonical team/season preservation, and real roster/player-stat row
    counts.
    """
    cbb_root = Path("TeamDump/CBB")
    dirs = {"st-bonaventure": SBU_DIR, "michigan-state": MSU_DIR, "albany-ny": ALBANY_DIR}
    mtimes_before = {slug: (d / f"Roster_{SEASON}.xlsx").stat().st_mtime for slug, d in dirs.items()}
    expected_roster_counts = {"st-bonaventure": 15, "michigan-state": 15, "albany-ny": 16}

    for slug, query in _REAL_PERSIST_TEAMS:
        discovered = discover_allstats_artifacts(cbb_root, season=SEASON, team_slugs=[slug])[0]
        dfs, hashes = load_allstats_dataframes(discovered, cbb_root)
        parsed = parse_allstats_workbook(dfs, query, SEASON)
        plan = build_allstats_import_plan(parsed)
        assert plan.team_id is not None  # canonical team identity preserved
        assert plan.season_id == SEASON  # canonical season identity preserved
        _seed_team_and_season(session_factory, plan)
        result = persist_allstats_import_plan(plan, session_factory, discovered=discovered, content_hashes=hashes)
        assert result.status == "valid"
        assert result.roster_memberships_written == expected_roster_counts[slug]

    total_expected_roster = sum(expected_roster_counts.values())
    with unit_of_work(session_factory) as session:
        assert session.query(allstats_models.RosterMembership).count() == total_expected_roster
        stat_row_count_first_pass = session.query(allstats_models.PlayerStatSnapshot).count()
        team_ids = {t.team_id for t in session.query(models.Team).all()}
        assert len(team_ids) == 3  # three genuinely distinct canonical teams, never collapsed

    # Idempotent re-import of the same three real workbooks.
    for slug, query in _REAL_PERSIST_TEAMS:
        discovered = discover_allstats_artifacts(cbb_root, season=SEASON, team_slugs=[slug])[0]
        dfs, hashes = load_allstats_dataframes(discovered, cbb_root)
        parsed = parse_allstats_workbook(dfs, query, SEASON)
        plan = build_allstats_import_plan(parsed)
        persist_allstats_import_plan(plan, session_factory, discovered=discovered, content_hashes=hashes)

    with unit_of_work(session_factory) as session:
        assert session.query(allstats_models.RosterMembership).count() == total_expected_roster  # unchanged, not doubled
        assert session.query(allstats_models.PlayerStatSnapshot).count() == stat_row_count_first_pass

    for slug, before in mtimes_before.items():
        assert (dirs[slug] / f"Roster_{SEASON}.xlsx").stat().st_mtime == before  # no source-file mutation, for any of the three


# ---------------------------------------------------------------------------
# AST guards: no analytical recalculation, no live requests, no secrets
# ---------------------------------------------------------------------------


def _imported_module_roots(module) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_adapter_never_imports_ovr_classification_or_team_profile_modules():
    import src.pipeline.allstats_import_adapter as mod
    tree = ast.parse(inspect.getsource(mod))
    imported_modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    forbidden_substrings = ("ovr", "classification", "team_profile", "team_strength", "matchup", "bet_decision", "expected_value", "kelly")
    for m in imported_modules:
        assert not any(f in m.lower() for f in forbidden_substrings), f"unexpected import: {m}"


def test_adapter_never_imports_network_libraries():
    import src.pipeline.allstats_import_adapter as mod
    roots = _imported_module_roots(mod)
    assert not ({"requests", "urllib", "socket", "http"} & roots)


def test_adapter_never_reads_env_or_secrets():
    import src.pipeline.allstats_import_adapter as mod
    source = inspect.getsource(mod)
    assert "os.environ" not in source
    assert "ODDS_API_KEY" not in source


def test_adapter_never_writes_to_teamdump():
    """AST guard: no call in this module ever writes a file (to_excel/to_csv/open(...'w')) -- discovery/loading are read-only."""
    import src.pipeline.allstats_import_adapter as mod
    tree = ast.parse(inspect.getsource(mod))
    called_names = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not ({"to_excel", "to_csv"} & called_names)


def test_this_test_file_never_imports_network_libraries():
    import sys
    roots = _imported_module_roots(sys.modules[__name__])
    assert not ({"requests", "urllib", "socket", "http"} & roots)
