"""
Phase 5B4: tests for `src/pipeline/player_game_log_import_adapter.py`.
Real-fixture tests exercise real local `TeamDump/CBB` player game-log
files (read-only). Everything else uses small synthetic DataFrames for
deterministic edge cases, following the established pattern from
`test_game_log_import_adapter.py`/`test_allstats_import_adapter.py`.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine, event

from src.db.base import Base
from src.db import models, player_game_log_models
from src.db.session import build_session_factory, unit_of_work
from src.db.repositories import SeasonRepository
from src.exceptions import IdentityConflictError
from src.identity.contracts import TeamIdentity, UnresolvedIdentity
from src.identity.player_identity import build_player_identity
from src.identity.season_identity import resolve_season_identity
from src.identity.team_identity import resolve_team_identity
from src.pipeline import player_game_log_import_adapter as pgl_adapter
from src.pipeline.player_game_log_import_adapter import (
    DiscoveredPlayerGameLogFile,
    build_player_game_log_import_plan,
    discover_player_game_log_artifacts,
    load_player_game_log_dataframe,
    parse_player_game_log_workbook,
    persist_player_game_log_import_plan,
    reconstruct_player_name_from_slug,
    resolve_reviewed_player_gamelog_filename_alias,
    resolve_team_roster_for_player_gamelogs,
)

SEASON = 2026
CBB_ROOT = Path("TeamDump/CBB")
SBU_TEAM_DIR = CBB_ROOT / "st-bonaventure"
MSU_TEAM_DIR = CBB_ROOT / "michigan-state"


def _real_fixtures_present() -> bool:
    return SBU_TEAM_DIR.exists() and (SBU_TEAM_DIR / "frank_mitchell_2026_gamelog.csv").exists()


pytestmark_real = pytest.mark.skipif(not _real_fixtures_present(), reason="real player-gamelog fixtures not present in this checkout")


def _gamelog_df(rows: list[dict]) -> pd.DataFrame:
    base = {"game_num": 1, "date": "2026-01-15", "opponent": "Colgate", "result": "W 80-70", "mp": 30, "pts": 10, "trb": 5, "ast": 3, "stl": 1, "blk": 0, "tov": 2, "fg": 4, "fga": 8}
    return pd.DataFrame([{**base, **r} for r in rows])


def _roster(names, team_id=1):
    return [build_player_identity(n, team_id, SEASON) for n in names]


# ---------------------------------------------------------------------------
# Stage 1: discovery
# ---------------------------------------------------------------------------


def test_discover_finds_player_gamelog_files_not_team_level(tmp_path):
    team_dir = tmp_path / "some-team"
    team_dir.mkdir()
    (team_dir / "frank_mitchell_2026_gamelog.csv").write_bytes(b"fake")
    (team_dir / "some-team_2026_game_log.csv").write_bytes(b"fake")  # team-level -- must NOT be discovered
    discovered = discover_player_game_log_artifacts(tmp_path, season=SEASON)
    assert len(discovered) == 1
    assert discovered[0].player_slug == "frank_mitchell"


def test_discover_deterministic_order(tmp_path):
    team_dir = tmp_path / "some-team"
    team_dir.mkdir()
    for name in ("zed_player", "alice_player"):
        (team_dir / f"{name}_2026_gamelog.csv").write_bytes(b"fake")
    first = discover_player_game_log_artifacts(tmp_path, season=SEASON)
    second = discover_player_game_log_artifacts(tmp_path, season=SEASON)
    assert [d.player_slug for d in first] == [d.player_slug for d in second]


def test_discover_team_with_no_player_files_contributes_nothing(tmp_path):
    (tmp_path / "empty-team").mkdir()
    assert discover_player_game_log_artifacts(tmp_path, season=SEASON) == []


def test_discover_never_hardcodes_teamdump_path():
    import inspect
    import src.pipeline.player_game_log_import_adapter as mod

    assert "TeamDump" not in inspect.getsource(mod.discover_player_game_log_artifacts)


def test_discover_respects_team_slugs_filter(tmp_path):
    for slug in ("team-a", "team-b"):
        d = tmp_path / slug
        d.mkdir()
        (d / f"player_{slug}_2026_gamelog.csv").write_bytes(b"fake")
    discovered = discover_player_game_log_artifacts(tmp_path, season=SEASON, team_slugs=["team-a"])
    assert [d.team_slug for d in discovered] == ["team-a"]


# ---------------------------------------------------------------------------
# Filename reconstruction
# ---------------------------------------------------------------------------


def test_reconstruct_simple_name():
    assert reconstruct_player_name_from_slug("frank_mitchell") == "Frank Mitchell"


def test_reconstruct_suffix_name():
    assert reconstruct_player_name_from_slug("daryl_simmons_ii") == "Daryl Simmons Ii"  # imperfect case is fine -- matching is case-insensitive


def test_reconstruct_matches_roster_despite_apostrophe_never_in_filename():
    """Real confirmed case: roster has "Amar'e Marshall", filename is "amare_marshall" (apostrophe never existed in a filename) -- canonical key matching must still succeed."""
    from src.identity.contracts import PlayerIdentity
    from src.identity.sr_allstats_player_identity import resolve_allstats_player_row

    reconstructed = reconstruct_player_name_from_slug("amare_marshall")
    roster = _roster(["Amar'e Marshall"])
    result = resolve_allstats_player_row(reconstructed, roster)
    assert isinstance(result, PlayerIdentity)


# ---------------------------------------------------------------------------
# Parsing: result-string parsing
# ---------------------------------------------------------------------------


def test_parse_result_win_no_overtime():
    df = _gamelog_df([{"result": "W 69-63"}])
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    season_identity = resolve_season_identity(SEASON)
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=season_identity, roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    assert parsed.rows[0].raw_result_marker == "W"
    assert parsed.rows[0].team_score == 69
    assert parsed.rows[0].opponent_score == 63
    assert parsed.rows[0].overtime_marker is None


def test_parse_result_loss_with_overtime():
    df = _gamelog_df([{"result": "L 75-78 (OT)"}])
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    season_identity = resolve_season_identity(SEASON)
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=season_identity, roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    assert parsed.rows[0].raw_result_marker == "L"
    assert parsed.rows[0].overtime_marker == "OT"


def test_parse_result_double_overtime():
    df = _gamelog_df([{"result": "W 117-115 (2OT)"}])
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    season_identity = resolve_season_identity(SEASON)
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=season_identity, roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    assert parsed.rows[0].overtime_marker == "2OT"


def test_parse_result_malformed_never_crashes():
    df = _gamelog_df([{"result": "garbage"}])
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    season_identity = resolve_season_identity(SEASON)
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=season_identity, roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    assert parsed.rows[0].raw_result_marker is None
    assert parsed.rows[0].team_score is None


# ---------------------------------------------------------------------------
# Parsing: general contract
# ---------------------------------------------------------------------------


def test_parse_missing_required_columns_invalid():
    df = pd.DataFrame([{"foo": 1}])
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    season_identity = resolve_season_identity(SEASON)
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=season_identity, roster_identities=[], team_slug="michigan-state", player_slug="a_player", season=SEASON)
    assert parsed.status == "invalid"


def test_parse_rejects_table_with_player_column():
    """A player-shaped roster/AllStats table (with its own 'Player' column) must never be misparsed as a per-game log."""
    df = _gamelog_df([{}])
    df["Player"] = "Someone"
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    season_identity = resolve_season_identity(SEASON)
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=season_identity, roster_identities=[], team_slug="michigan-state", player_slug="a_player", season=SEASON)
    assert parsed.status == "invalid"


def test_parse_missing_season_invalid():
    df = _gamelog_df([{}])
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    season_identity = resolve_season_identity(99999)
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=season_identity, roster_identities=[], team_slug="michigan-state", player_slug="a_player", season=99999)
    assert parsed.status == "invalid"


def test_parse_unresolved_team_invalid():
    df = _gamelog_df([{}])
    team_identity = resolve_team_identity("Definitely Not A Real Team XYZ", source="sports_reference")
    season_identity = resolve_season_identity(SEASON)
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=season_identity, roster_identities=[], team_slug="fake-team", player_slug="a_player", season=SEASON)
    assert parsed.status == "invalid"


def test_parse_player_resolves_against_roster():
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["Frank Mitchell"], team_identity.team_id), team_slug="michigan-state", player_slug="frank_mitchell", season=SEASON)
    assert parsed.player_id is not None
    assert parsed.player_match_method == "roster_matched"


def test_parse_player_unresolved_against_empty_roster():
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=[], team_slug="michigan-state", player_slug="frank_mitchell", season=SEASON)
    assert parsed.player_id is None
    assert parsed.player_match_method == "unresolved"
    assert any(i.issue_code == "player_gamelog_player_unresolved" for i in parsed.issues)


def test_same_surname_teammates_remain_distinct():
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{}])
    roster = _roster(["John Smith", "Michael Smith"], team_identity.team_id)
    parsed_john = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=roster, team_slug="michigan-state", player_slug="john_smith", season=SEASON)
    parsed_michael = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=roster, team_slug="michigan-state", player_slug="michael_smith", season=SEASON)
    assert parsed_john.player_id is not None and parsed_michael.player_id is not None
    assert parsed_john.player_id != parsed_michael.player_id


def test_never_resolves_by_surname_alone():
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{}])
    roster = _roster(["John Smith"], team_identity.team_id)
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=roster, team_slug="michigan-state", player_slug="smith", season=SEASON)
    assert parsed.player_id is None
    assert parsed.player_match_method == "unresolved"


def test_opponent_resolves_via_canonical_resolver():
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{"opponent": "Colgate"}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    assert parsed.rows[0].opponent_team_id is not None


def test_opponent_unresolved_stays_unresolved():
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{"opponent": "Definitely Not A Real School XYZ"}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    assert parsed.rows[0].opponent_team_id is None
    assert parsed.rows[0].validation_status == "unresolved"
    assert any(i.issue_code == "player_gamelog_opponent_unresolved" for i in parsed.issues)


def test_same_team_opponent_rejected():
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{"opponent": "Michigan State"}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    assert parsed.rows[0].validation_status == "invalid"
    assert parsed.rows[0].exclusion_reason == "same_team_game"
    assert parsed.rows[0].game_id is None


def test_missing_opponent_name_invalid():
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{"opponent": None}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    assert parsed.rows[0].validation_status == "invalid"
    assert parsed.rows[0].exclusion_reason == "missing_opponent_name"


def test_invalid_date_never_crashes():
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{"date": "not-a-date"}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    assert parsed.rows[0].validation_status == "invalid"
    assert parsed.rows[0].exclusion_reason == "unparseable_date"
    assert parsed.rows[0].game_id is None


def test_missing_tipoff_time_never_invented():
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    assert parsed.rows[0].game_date is not None
    assert not hasattr(parsed.rows[0], "scheduled_timestamp")  # never produced by the parser -- always null at persistence


def test_dnp_never_a_zero_minute_row_by_construction():
    """Confirms the parser never invents a DNP row -- every real parsed row is 'played' by construction, since this source's own contract never emits a non-appearance row."""
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{"mp": 1}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    assert parsed.rows[0].participation_status == "played"


def test_missing_versus_zero_stat_preserved_distinctly():
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{"blk": 0}, {"blk": None}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    assert parsed.rows[0].stat_values["blocks"] == 0
    assert parsed.rows[1].stat_values["blocks"] is None


def test_invalid_numeric_value_handled():
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{"pts": "not-a-number"}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    assert parsed.rows[0].stat_values["points"] is None  # non-numeric coerces to None, never crashes


def test_parse_never_mutates_input_dataframe():
    df = _gamelog_df([{}])
    snapshot = df.copy(deep=True)
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    pd.testing.assert_frame_equal(df, snapshot)


def test_resolved_opponent_identities_dict_populated():
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{"opponent": "Colgate"}, {"opponent": "Bucknell"}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    assert len(parsed.resolved_opponent_identities) == 2


# ---------------------------------------------------------------------------
# Phase 5B5: player-game-log filename identity remediation
# (reviewed, team-scoped aliases for irreversible/lossy filenames)
# ---------------------------------------------------------------------------


def test_reviewed_alias_helper_returns_none_when_absent():
    assert resolve_reviewed_player_gamelog_filename_alias("some-team", "some-slug") is None


def test_real_reviewed_alias_entry_present_for_rub_n_prey():
    """Confirms the one curated Phase 5B5 entry exists exactly where expected -- narrow, evidence-backed, not a placeholder."""
    assert resolve_reviewed_player_gamelog_filename_alias("st-johns-ny", "rub_n_prey") == "Ruben Prey"


def test_reviewed_alias_resolves_lossy_filename_synthetic(monkeypatch):
    monkeypatch.setitem(pgl_adapter._REVIEWED_PLAYER_GAMELOG_FILENAME_ALIASES, ("synthetic-team", "l_st_name"), "Lost Name")
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{}])
    roster = _roster(["Lost Name"], team_identity.team_id)
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=roster, team_slug="synthetic-team", player_slug="l_st_name", season=SEASON)
    assert parsed.player_id is not None
    assert parsed.player_match_method == "reviewed_alias"
    assert parsed.raw_player_name == "L St Name"  # the immutable, plain filename reconstruction is never overwritten by the alias


def test_reviewed_alias_absent_falls_back_to_plain_reconstruction():
    """No alias curated for this tuple -- ordinary roster_matched path, unaffected by this batch's addition."""
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{}])
    roster = _roster(["Frank Mitchell"], team_identity.team_id)
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=roster, team_slug="michigan-state", player_slug="frank_mitchell", season=SEASON)
    assert parsed.player_match_method == "roster_matched"


def test_reviewed_alias_wrong_team_never_fires(monkeypatch):
    """An alias curated for team A's exact (team_slug, player_slug) tuple must never fire for a same-named file under team B's own directory."""
    monkeypatch.setitem(pgl_adapter._REVIEWED_PLAYER_GAMELOG_FILENAME_ALIASES, ("team-a", "shared_slug"), "Team A Real Name")
    team_identity_b = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{}])
    roster_b = _roster(["Shared Slug"], team_identity_b.team_id)  # team B's own real (plain-reconstructed) roster entry
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity_b, season_identity=resolve_season_identity(SEASON), roster_identities=roster_b, team_slug="team-b", player_slug="shared_slug", season=SEASON)
    assert parsed.player_id is not None
    assert parsed.player_match_method == "roster_matched"  # never "reviewed_alias" -- team-a's alias must not leak into team-b


def test_reviewed_alias_collision_falls_back_to_ambiguous(monkeypatch):
    """Reviewed-alias evidence is still collision-checked dynamically against the full roster -- never blindly trusted."""
    monkeypatch.setitem(pgl_adapter._REVIEWED_PLAYER_GAMELOG_FILENAME_ALIASES, ("synthetic-team", "collision_case"), "Collision Name")
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{}])
    # Two roster entries share the alias target's own canonical key (e.g. two suffix variants of the same key).
    roster = _roster(["Collision Name", "Collision Name Jr"], team_identity.team_id)
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=roster, team_slug="synthetic-team", player_slug="collision_case", season=SEASON)
    assert parsed.player_id is None
    assert parsed.player_match_method == "ambiguous"


def test_reviewed_alias_target_not_on_roster_stays_unresolved(monkeypatch):
    """A curated alias whose target no longer matches anyone on the real roster must stay unresolved, never fabricated."""
    monkeypatch.setitem(pgl_adapter._REVIEWED_PLAYER_GAMELOG_FILENAME_ALIASES, ("synthetic-team", "no_match_case"), "Nobody Here")
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{}])
    roster = _roster(["Somebody Else"], team_identity.team_id)
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=roster, team_slug="synthetic-team", player_slug="no_match_case", season=SEASON)
    assert parsed.player_id is None
    assert parsed.player_match_method == "unresolved"


def test_reviewed_alias_takes_precedence_over_conflicting_filename_evidence(monkeypatch):
    """Explicit reviewed-alias evidence must win over the weaker, plain filename-reconstruction evidence when both point at different real roster players."""
    monkeypatch.setitem(pgl_adapter._REVIEWED_PLAYER_GAMELOG_FILENAME_ALIASES, ("synthetic-team", "conflict_case"), "Real Name Two")
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{}])
    # The plain filename reconstruction of "conflict_case" ("Conflict Case") is ALSO a real roster entry --
    # a genuine conflicting-evidence scenario. The curated alias must still win.
    roster = _roster(["Conflict Case", "Real Name Two"], team_identity.team_id)
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=roster, team_slug="synthetic-team", player_slug="conflict_case", season=SEASON)
    expected = next(p for p in roster if p.display_name == "Real Name Two")
    assert parsed.player_id == expected.player_id
    assert parsed.player_match_method == "reviewed_alias"


def test_reviewed_alias_never_used_for_no_alias_case_even_with_similar_name(monkeypatch):
    """No fuzzy/near-miss alias matching -- an alias keyed to a DIFFERENT exact slug must not fire for a merely-similar one."""
    monkeypatch.setitem(pgl_adapter._REVIEWED_PLAYER_GAMELOG_FILENAME_ALIASES, ("synthetic-team", "rub_n_prey"), "Ruben Prey")
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{}])
    roster = _roster(["Ruben Prey"], team_identity.team_id)
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=roster, team_slug="synthetic-team", player_slug="rob_n_pray", season=SEASON)  # near-miss, NOT the curated key
    assert parsed.player_id is None
    assert parsed.player_match_method == "unresolved"


def test_reviewed_alias_deterministic_repeated_resolution(monkeypatch):
    monkeypatch.setitem(pgl_adapter._REVIEWED_PLAYER_GAMELOG_FILENAME_ALIASES, ("synthetic-team", "det_case"), "Det Case Real")
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{}])
    roster = _roster(["Det Case Real"], team_identity.team_id)
    results = [
        parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=roster, team_slug="synthetic-team", player_slug="det_case", season=SEASON).player_id
        for _ in range(5)
    ]
    assert len(set(results)) == 1 and results[0] is not None


def test_reviewed_alias_roster_order_independent(monkeypatch):
    monkeypatch.setitem(pgl_adapter._REVIEWED_PLAYER_GAMELOG_FILENAME_ALIASES, ("synthetic-team", "order_case"), "Order Case Target")
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{}])
    names = ["Zeta Player", "Order Case Target", "Alpha Player"]
    roster_forward = _roster(names, team_identity.team_id)
    roster_reversed = _roster(list(reversed(names)), team_identity.team_id)
    parsed_forward = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=roster_forward, team_slug="synthetic-team", player_slug="order_case", season=SEASON)
    parsed_reversed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=roster_reversed, team_slug="synthetic-team", player_slug="order_case", season=SEASON)
    assert parsed_forward.player_id == parsed_reversed.player_id


def test_reviewed_alias_dict_not_mutated_by_resolution():
    before = dict(pgl_adapter._REVIEWED_PLAYER_GAMELOG_FILENAME_ALIASES)
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{}])
    roster = _roster(["Ruben Prey"], team_identity.team_id)
    parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=roster, team_slug="st-johns-ny", player_slug="rub_n_prey", season=SEASON)
    assert pgl_adapter._REVIEWED_PLAYER_GAMELOG_FILENAME_ALIASES == before


def test_reviewed_alias_season_independent_by_design():
    """The alias key is (team_slug, player_slug) only -- no season component -- since this batch's real evidence is single-season. Confirms the SAME alias fires identically regardless of the season value threaded through parsing (a genuine season MISMATCH is instead caught upstream by season_identity resolution, unaffected by this change)."""
    team_identity = resolve_team_identity("St. John's (NY)", source="sports_reference") if isinstance(resolve_team_identity("St. John's (NY)", source="sports_reference"), TeamIdentity) else resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{}])
    roster = _roster(["Ruben Prey"], team_identity.team_id)
    parsed_2026 = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(2026), roster_identities=roster, team_slug="st-johns-ny", player_slug="rub_n_prey", season=2026)
    parsed_2027 = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(2027), roster_identities=roster, team_slug="st-johns-ny", player_slug="rub_n_prey", season=2027)
    assert parsed_2026.player_match_method == parsed_2027.player_match_method == "reviewed_alias"


def test_missing_roster_player_distinct_from_empty_roster():
    """A non-empty roster that simply doesn't include this player is a distinct case from an empty roster -- both correctly unresolved."""
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{}])
    roster = _roster(["Someone Else", "Another Player"], team_identity.team_id)
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=roster, team_slug="michigan-state", player_slug="frank_mitchell", season=SEASON)
    assert parsed.player_id is None
    assert parsed.player_match_method == "unresolved"
    assert len(roster) == 2  # confirms this is genuinely non-empty, distinct from test_parse_player_unresolved_against_empty_roster


def test_empty_filename_stem_never_crashes():
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="", season=SEASON)
    assert parsed.player_id is None
    assert parsed.player_match_method == "unresolved"
    assert parsed.raw_player_name == ""


def test_malformed_filename_stem_never_crashes():
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="___!!!123_$$", season=SEASON)
    assert parsed.player_id is None  # never crashes; simply fails to match


def test_parenthetical_filename_stem_never_crashes_and_matches_via_shared_key_stripping():
    """No real player-gamelog filename carries parentheses (confirmed by this batch's own full inventory) -- this is a defensive guard, not a real-evidence case: reconstruct_player_name_from_slug never strips or interprets '(' ')' specially, but the SHARED canonical_player_key_v15 matching key (reused, not duplicated) strips all non-word punctuation on both sides, so this never crashes and safely still matches without needing a curated alias."""
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{}])
    raw = reconstruct_player_name_from_slug("john_(smith)")
    assert raw == "John (Smith)"
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["John Smith"], team_identity.team_id), team_slug="michigan-state", player_slug="john_(smith)", season=SEASON)
    assert parsed.player_id is not None
    assert parsed.player_match_method == "roster_matched"


def test_apostrophe_reconstructed_name_matches_without_alias_synthetic():
    """Apostrophes never survive into a filename slug (confirmed real: '_' is the only separator) -- the canonical matching key strips punctuation on both sides, so a roster entry carrying a real apostrophe still resolves from the plain, apostrophe-free filename reconstruction, with no alias needed."""
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{}])
    roster = _roster(["Amar'e Marshall"], team_identity.team_id)
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=roster, team_slug="michigan-state", player_slug="amare_marshall", season=SEASON)
    assert parsed.player_id is not None
    assert parsed.player_match_method == "roster_matched"


def test_suffix_reconstructed_name_matches_without_alias_synthetic():
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{}])
    roster = _roster(["Daryl Simmons II"], team_identity.team_id)
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=roster, team_slug="michigan-state", player_slug="daryl_simmons_ii", season=SEASON)
    assert parsed.player_id is not None
    assert parsed.player_match_method == "roster_matched"


def test_accented_name_reconstructed_matches_without_alias_synthetic():
    """The filename slug is plain ASCII (confirmed real: diacritics never survive into a filename) -- the shared mojibake-aware repair authority (reused, not duplicated) still lets an accented roster display name match the plain-ASCII filename reconstruction via the accent-insensitive canonical key."""
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{}])
    roster = _roster(["João Victor"], team_identity.team_id)
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=roster, team_slug="michigan-state", player_slug="joao_victor", season=SEASON)
    assert parsed.player_id is not None
    assert parsed.player_match_method == "roster_matched"


def test_mojibake_stat_row_style_name_still_resolves_shared_authority_reused():
    """Confirms this adapter's player resolution reuses the SAME shared mojibake-repair authority already validated by the AllStats mojibake-remediation suite -- never a second, adapter-local repair implementation."""
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{}])
    roster = _roster(["Joao Victor Moreira"], team_identity.team_id)  # already-repaired roster-side display name, per build_player_identity's own contract
    # This adapter's own filename reconstruction can never itself produce mojibake (plain ASCII slug), so this
    # test targets resolve_allstats_player_row directly to confirm the reused repair path this adapter calls into.
    result = pgl_adapter.resolve_allstats_player_row("JoÃ£o Victor Moreira", roster)
    assert not isinstance(result, type(None))
    assert result.display_name == "Joao Victor Moreira"


def test_reviewed_alias_helper_never_touches_disk(monkeypatch):
    """The alias lookup is a pure in-memory dict read -- confirms it performs no I/O even if disk access would raise."""
    def _boom(*a, **k):
        raise AssertionError("resolve_reviewed_player_gamelog_filename_alias must never touch disk")
    monkeypatch.setattr(pd, "read_csv", _boom)
    monkeypatch.setattr(pd, "read_excel", _boom)
    assert resolve_reviewed_player_gamelog_filename_alias("st-johns-ny", "rub_n_prey") == "Ruben Prey"
    assert resolve_reviewed_player_gamelog_filename_alias("nonexistent-team", "nonexistent-slug") is None


def test_reviewed_alias_never_referenced_by_shared_identity_modules():
    """The reviewed-alias table is adapter-local (player-game-log-specific) -- confirms it is never imported or referenced by the shared, broadly-used identity modules (AllStats/player-identity core), satisfying 'never a second, global normalization authority.'"""
    import inspect
    import src.identity.sr_allstats_player_identity as allstats_mod
    import src.identity.player_identity as core_mod

    for mod in (allstats_mod, core_mod):
        source = inspect.getsource(mod)
        assert "_REVIEWED_PLAYER_GAMELOG_FILENAME_ALIASES" not in source
        assert "resolve_reviewed_player_gamelog_filename_alias" not in source


@pytestmark_real
def test_real_rub_n_prey_resolves_via_reviewed_alias():
    team_identity, season_identity, roster = resolve_team_roster_for_player_gamelogs(CBB_ROOT, "st-johns-ny", SEASON)
    discovered = discover_player_game_log_artifacts(CBB_ROOT, season=SEASON, team_slugs=["st-johns-ny"])
    rub = next(d for d in discovered if d.player_slug == "rub_n_prey")
    df, _h = load_player_game_log_dataframe(rub, CBB_ROOT)
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=season_identity, roster_identities=roster, team_slug=rub.team_slug, player_slug=rub.player_slug, season=SEASON)
    assert parsed.player_id is not None
    assert parsed.player_match_method == "reviewed_alias"
    assert parsed.status == "valid"
    assert parsed.raw_player_name == "Rub N Prey"  # immutable raw filename reconstruction, never overwritten


@pytestmark_real
def test_real_rub_n_prey_persists_with_reviewed_alias_provenance(session_factory):
    team_identity, season_identity, roster = resolve_team_roster_for_player_gamelogs(CBB_ROOT, "st-johns-ny", SEASON)
    discovered = discover_player_game_log_artifacts(CBB_ROOT, season=SEASON, team_slugs=["st-johns-ny"])
    rub = next(d for d in discovered if d.player_slug == "rub_n_prey")
    df, content_hash = load_player_game_log_dataframe(rub, CBB_ROOT)
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=season_identity, roster_identities=roster, team_slug=rub.team_slug, player_slug=rub.player_slug, season=SEASON)
    plan = build_player_game_log_import_plan(parsed)
    _seed_season(session_factory)
    result = persist_player_game_log_import_plan(plan, session_factory, discovered=rub, content_hash=content_hash)
    assert result.observations_written == len(parsed.rows)
    with unit_of_work(session_factory) as session:
        rows = session.query(player_game_log_models.PlayerGameLogObservation).filter_by(raw_player_name="Rub N Prey").all()
        assert len(rows) == len(parsed.rows)
        assert all(r.player_match_method == "reviewed_alias" for r in rows)
        assert all(r.player_id is not None for r in rows)


# ---------------------------------------------------------------------------
# Game identity: 63-bit scheme, mirrored order, cross-adapter consistency
# ---------------------------------------------------------------------------


def test_game_id_fits_63_bit_scheme():
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{"opponent": "Colgate"}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    gid = parsed.rows[0].game_id
    assert gid is not None
    assert 0 <= gid < 2**63


def test_mirrored_team_order_produces_same_game_id():
    msu = resolve_team_identity("Michigan State", source="sports_reference")
    colgate = resolve_team_identity("Colgate", source="sports_reference")
    df_msu_side = _gamelog_df([{"opponent": "Colgate", "date": "2026-01-15"}])
    df_colgate_side = _gamelog_df([{"opponent": "Michigan State", "date": "2026-01-15"}])
    parsed_msu = parse_player_game_log_workbook(df_msu_side, team_identity=msu, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], msu.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    parsed_colgate = parse_player_game_log_workbook(df_colgate_side, team_identity=colgate, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["B Player"], colgate.team_id), team_slug="colgate", player_slug="b_player", season=SEASON)
    assert parsed_msu.rows[0].game_id == parsed_colgate.rows[0].game_id


@pytestmark_real
def test_real_game_id_matches_team_level_adapter_exactly():
    """The definitive cross-adapter consistency proof: the same real game must compute the identical game_id whether observed from the team-level or the player-level adapter."""
    from src.pipeline.game_log_import_adapter import discover_game_log_artifacts, load_game_log_dataframe, parse_game_log_workbook

    team_identity, season_identity, roster = resolve_team_roster_for_player_gamelogs(CBB_ROOT, "st-bonaventure", SEASON)
    discovered = discover_player_game_log_artifacts(CBB_ROOT, season=SEASON, team_slugs=["st-bonaventure"])
    frank = next(d for d in discovered if d.player_slug == "frank_mitchell")
    df, _h = load_player_game_log_dataframe(frank, CBB_ROOT)
    parsed_player = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=season_identity, roster_identities=roster, team_slug=frank.team_slug, player_slug=frank.player_slug, season=SEASON)
    bradley_row = next(r for r in parsed_player.rows if r.raw_opponent_name == "Bradley")

    team_discovered = discover_game_log_artifacts(CBB_ROOT, season=SEASON, team_slugs=["st-bonaventure"])[0]
    tdf, _th = load_game_log_dataframe(team_discovered, CBB_ROOT)
    parsed_team = parse_game_log_workbook(tdf, "st-bonaventure", SEASON)
    team_bradley_row = next(r for r in parsed_team.rows if r.raw_opponent_name == "Bradley")

    assert bradley_row.game_id == team_bradley_row.game_id


# ---------------------------------------------------------------------------
# Plan building (pure)
# ---------------------------------------------------------------------------


def test_build_plan_counts():
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{"opponent": "Colgate"}, {"opponent": "Definitely Not A Real School"}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    plan = build_player_game_log_import_plan(parsed)
    assert plan.counts.total_source_rows == 2
    assert plan.counts.resolved_opponents == 1
    assert plan.counts.unresolved_opponents == 1


def test_build_plan_never_mutates_across_repeated_calls():
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    plan1 = build_player_game_log_import_plan(parsed)
    plan2 = build_player_game_log_import_plan(parsed)
    assert plan1.observation_specs == plan2.observation_specs


# ---------------------------------------------------------------------------
# Persistence: transactions, idempotency, linking
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


def _seed_season(session_factory, season_id=SEASON):
    with unit_of_work(session_factory) as session:
        if session.get(models.Season, season_id) is None:
            SeasonRepository(session).upsert(season_id=season_id, start_year=season_id - 1, end_year=season_id, label="2025-26")


def _discovered_and_hash(tmp_path, df, team_slug="michigan-state", player_slug="a_player"):
    import hashlib

    team_dir = tmp_path / team_slug
    team_dir.mkdir(exist_ok=True, parents=True)
    path = team_dir / f"{player_slug}_{SEASON}_gamelog.csv"
    df.to_csv(path, index=False)
    stat = path.stat()
    discovered = DiscoveredPlayerGameLogFile(
        team_slug=team_slug, player_slug=player_slug, season=SEASON,
        relative_path=f"{team_slug}/{player_slug}_{SEASON}_gamelog.csv",
        file_size_bytes=stat.st_size, filesystem_mtime=dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc),
    )
    return discovered, hashlib.sha256(path.read_bytes()).hexdigest()


def test_persist_writes_observation_registers_team_player_and_game(session_factory, tmp_path):
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{"opponent": "Colgate"}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    plan = build_player_game_log_import_plan(parsed)
    _seed_season(session_factory)
    discovered, content_hash = _discovered_and_hash(tmp_path, df)

    result = persist_player_game_log_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash)
    assert result.observations_written == 1

    with unit_of_work(session_factory) as session:
        assert session.query(player_game_log_models.PlayerGameLogObservation).count() == 1
        assert session.query(models.Team).count() == 2  # perspective + opponent
        assert session.query(models.Player).count() == 1
        assert session.query(models.Game).count() == 1
        artifact = session.query(models.SourceArtifact).one()
        assert artifact.collection_timestamp is None
        assert artifact.point_in_time_classification == "historical_but_not_point_in_time"


def test_persist_unresolved_row_creates_no_fake_team_or_player(session_factory, tmp_path):
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{"opponent": "Definitely Not A Real School XYZ"}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=[], team_slug="michigan-state", player_slug="unresolvable_player", season=SEASON)
    plan = build_player_game_log_import_plan(parsed)
    _seed_season(session_factory)
    discovered, content_hash = _discovered_and_hash(tmp_path, df, player_slug="unresolvable_player")

    result = persist_player_game_log_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash)
    assert result.observations_written == 1

    with unit_of_work(session_factory) as session:
        assert session.query(models.Team).count() == 1  # only the (real) perspective team, no fake opponent
        assert session.query(models.Player).count() == 0  # no fake player created
        assert session.query(models.Game).count() == 0
        obs = session.query(player_game_log_models.PlayerGameLogObservation).one()
        assert obs.player_id is None and obs.opponent_team_id is None


def test_persist_idempotent_on_identical_repeat_import(session_factory, tmp_path):
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{"opponent": "Colgate"}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    plan = build_player_game_log_import_plan(parsed)
    _seed_season(session_factory)
    discovered, content_hash = _discovered_and_hash(tmp_path, df)

    persist_player_game_log_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash)
    persist_player_game_log_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash)

    with unit_of_work(session_factory) as session:
        assert session.query(player_game_log_models.PlayerGameLogObservation).count() == 1
        assert session.query(models.SourceArtifact).count() == 1


def test_persist_rolls_back_on_conflict(session_factory, tmp_path):
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{"opponent": "Colgate", "pts": 10}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    plan = build_player_game_log_import_plan(parsed)
    _seed_season(session_factory)
    discovered, content_hash = _discovered_and_hash(tmp_path, df)
    persist_player_game_log_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash)

    conflicting_df = _gamelog_df([{"opponent": "Colgate", "pts": 99}])
    conflicting_parsed = parse_player_game_log_workbook(conflicting_df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    conflicting_plan = build_player_game_log_import_plan(conflicting_parsed)

    with pytest.raises(IdentityConflictError):
        persist_player_game_log_import_plan(conflicting_plan, session_factory, discovered=discovered, content_hash=content_hash)

    with unit_of_work(session_factory) as session:
        obs = session.query(player_game_log_models.PlayerGameLogObservation).one()
        assert obs.points == 10  # untouched by the failed conflicting attempt


def test_persist_no_orphan_observations_after_rollback(session_factory, tmp_path):
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{"opponent": "Colgate", "pts": 10}])
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON)
    plan = build_player_game_log_import_plan(parsed)
    _seed_season(session_factory)
    discovered, content_hash = _discovered_and_hash(tmp_path, df)
    persist_player_game_log_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash)

    conflicting_df = _gamelog_df([{"opponent": "Colgate", "pts": 99}])
    conflicting_plan = build_player_game_log_import_plan(parse_player_game_log_workbook(conflicting_df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON))
    with pytest.raises(IdentityConflictError):
        persist_player_game_log_import_plan(conflicting_plan, session_factory, discovered=discovered, content_hash=content_hash)

    with unit_of_work(session_factory) as session:
        assert session.query(player_game_log_models.PlayerGameLogObservation).count() == 1
        assert session.query(models.Game).count() == 1


def test_persist_changed_content_creates_new_artifact(session_factory, tmp_path):
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df_v1 = _gamelog_df([{"opponent": "Colgate"}])
    plan_v1 = build_player_game_log_import_plan(parse_player_game_log_workbook(df_v1, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON))
    _seed_season(session_factory)
    discovered_v1, hash_v1 = _discovered_and_hash(tmp_path, df_v1)
    persist_player_game_log_import_plan(plan_v1, session_factory, discovered=discovered_v1, content_hash=hash_v1)

    df_v2 = _gamelog_df([{"opponent": "Colgate"}, {"opponent": "Bucknell", "date": "2026-01-20"}])
    plan_v2 = build_player_game_log_import_plan(parse_player_game_log_workbook(df_v2, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON))
    discovered_v2, hash_v2 = _discovered_and_hash(tmp_path, df_v2)
    persist_player_game_log_import_plan(plan_v2, session_factory, discovered=discovered_v2, content_hash=hash_v2)

    with unit_of_work(session_factory) as session:
        assert session.query(models.SourceArtifact).count() == 2
        assert session.query(player_game_log_models.PlayerGameLogObservation).count() == 3  # 1 (v1) + 2 (v2), additive


def test_persist_pipeline_run_linking(session_factory, tmp_path):
    team_identity = resolve_team_identity("Michigan State", source="sports_reference")
    df = _gamelog_df([{"opponent": "Colgate"}])
    plan = build_player_game_log_import_plan(parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug="michigan-state", player_slug="a_player", season=SEASON))
    _seed_season(session_factory)
    discovered, content_hash = _discovered_and_hash(tmp_path, df)
    run_id = "3" * 32
    with unit_of_work(session_factory) as session:
        session.add(models.PipelineRun(run_id=run_id, pipeline_name="player_gamelog_test", status="running", started_at=dt.datetime.now(dt.timezone.utc)))

    persist_player_game_log_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash, pipeline_run_id=run_id)

    with unit_of_work(session_factory) as session:
        obs = session.query(player_game_log_models.PlayerGameLogObservation).one()
        assert obs.pipeline_run_id == run_id
        artifact = session.query(models.SourceArtifact).one()
        assert artifact.pipeline_run_id == run_id


def test_persist_import_order_independence(session_factory, tmp_path):
    """Two different players' files, imported in either order, converge on the same final team/game state."""
    msu = resolve_team_identity("Michigan State", source="sports_reference")
    colgate = resolve_team_identity("Colgate", source="sports_reference")
    df_a = _gamelog_df([{"opponent": "Colgate", "date": "2026-01-15"}])
    df_b = _gamelog_df([{"opponent": "Michigan State", "date": "2026-01-15"}])

    def run(order):
        eng = create_engine("sqlite+pysqlite:///:memory:", future=True)

        @event.listens_for(eng, "connect")
        def _fk(c, r):
            cur = c.cursor(); cur.execute("PRAGMA foreign_keys=ON"); cur.close()

        Base.metadata.create_all(eng)
        sf = build_session_factory(eng)
        _seed_season(sf)
        for i, (df, team_identity, team_slug, player_slug) in enumerate(order):
            parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=resolve_season_identity(SEASON), roster_identities=_roster(["A Player"], team_identity.team_id), team_slug=team_slug, player_slug=player_slug, season=SEASON)
            plan = build_player_game_log_import_plan(parsed)
            discovered, content_hash = _discovered_and_hash(tmp_path / f"order_{id(order)}_{i}", df, team_slug=team_slug, player_slug=player_slug)
            persist_player_game_log_import_plan(plan, sf, discovered=discovered, content_hash=content_hash)
        with unit_of_work(sf) as session:
            teams = sorted(t.team_id for t in session.query(models.Team).all())
            games = sorted((g.game_id, g.home_team_id, g.away_team_id) for g in session.query(models.Game).all())
        eng.dispose()
        return teams, games

    forward = run([(df_a, msu, "michigan-state", "a_player"), (df_b, colgate, "colgate", "b_player")])
    backward = run([(df_b, colgate, "colgate", "b_player"), (df_a, msu, "michigan-state", "a_player")])
    assert forward == backward


# ---------------------------------------------------------------------------
# Real-fixture end-to-end
# ---------------------------------------------------------------------------


@pytestmark_real
def test_real_st_bonaventure_all_players_dry_run():
    team_identity, season_identity, roster = resolve_team_roster_for_player_gamelogs(CBB_ROOT, "st-bonaventure", SEASON)
    discovered = discover_player_game_log_artifacts(CBB_ROOT, season=SEASON, team_slugs=["st-bonaventure"])
    assert len(discovered) == 13  # confirmed real file count

    total_rows = 0
    resolved_players = 0
    for d in discovered:
        df, _h = load_player_game_log_dataframe(d, CBB_ROOT)
        parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=season_identity, roster_identities=roster, team_slug=d.team_slug, player_slug=d.player_slug, season=SEASON)
        total_rows += len(parsed.rows)
        resolved_players += int(parsed.player_id is not None)

    assert resolved_players == 13  # every real St. Bonaventure player file resolves
    assert total_rows == 291  # confirmed real total row count


@pytestmark_real
def test_real_michigan_state_dry_run():
    team_identity, season_identity, roster = resolve_team_roster_for_player_gamelogs(CBB_ROOT, "michigan-state", SEASON)
    discovered = discover_player_game_log_artifacts(CBB_ROOT, season=SEASON, team_slugs=["michigan-state"])
    assert len(discovered) == 14

    resolved_players = sum(
        1 for d in discovered
        if (lambda df: parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=season_identity, roster_identities=roster, team_slug=d.team_slug, player_slug=d.player_slug, season=SEASON).player_id is not None)(load_player_game_log_dataframe(d, CBB_ROOT)[0])
    )
    assert resolved_players == 14


@pytestmark_real
def test_real_suffix_player_daryl_simmons_end_to_end():
    team_identity, season_identity, roster = resolve_team_roster_for_player_gamelogs(CBB_ROOT, "st-bonaventure", SEASON)
    discovered = discover_player_game_log_artifacts(CBB_ROOT, season=SEASON, team_slugs=["st-bonaventure"])
    daryl = next(d for d in discovered if d.player_slug == "daryl_simmons_ii")
    df, _h = load_player_game_log_dataframe(daryl, CBB_ROOT)
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=season_identity, roster_identities=roster, team_slug=daryl.team_slug, player_slug=daryl.player_slug, season=SEASON)
    assert parsed.player_id is not None
    assert parsed.player_match_method == "roster_matched"


@pytestmark_real
def test_real_apostrophe_player_amare_marshall_end_to_end():
    team_identity, season_identity, roster = resolve_team_roster_for_player_gamelogs(CBB_ROOT, "st-bonaventure", SEASON)
    discovered = discover_player_game_log_artifacts(CBB_ROOT, season=SEASON, team_slugs=["st-bonaventure"])
    amare = next(d for d in discovered if d.player_slug == "amare_marshall")
    df, _h = load_player_game_log_dataframe(amare, CBB_ROOT)
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=season_identity, roster_identities=roster, team_slug=amare.team_slug, player_slug=amare.player_slug, season=SEASON)
    assert parsed.player_id is not None


@pytestmark_real
def test_real_bench_player_with_few_games():
    """Broek Ostrom (confirmed real bench player, only 4 games) -- proves a low-usage player's sparse file resolves correctly and never gets padded with fabricated DNP rows."""
    team_identity, season_identity, roster = resolve_team_roster_for_player_gamelogs(CBB_ROOT, "st-bonaventure", SEASON)
    discovered = discover_player_game_log_artifacts(CBB_ROOT, season=SEASON, team_slugs=["st-bonaventure"])
    broek = next(d for d in discovered if d.player_slug == "broek_ostrom")
    df, _h = load_player_game_log_dataframe(broek, CBB_ROOT)
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=season_identity, roster_identities=roster, team_slug=broek.team_slug, player_slug=broek.player_slug, season=SEASON)
    assert len(parsed.rows) == 4
    assert all(r.participation_status == "played" for r in parsed.rows)


@pytestmark_real
def test_real_unresolved_opponent_bloomsburg():
    """Confirmed real unresolved opponent: 'Bloomsburg' (a non-D1 exhibition opponent) appears in Frank Mitchell's real file and correctly stays unresolved."""
    team_identity, season_identity, roster = resolve_team_roster_for_player_gamelogs(CBB_ROOT, "st-bonaventure", SEASON)
    discovered = discover_player_game_log_artifacts(CBB_ROOT, season=SEASON, team_slugs=["st-bonaventure"])
    frank = next(d for d in discovered if d.player_slug == "frank_mitchell")
    df, _h = load_player_game_log_dataframe(frank, CBB_ROOT)
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=season_identity, roster_identities=roster, team_slug=frank.team_slug, player_slug=frank.player_slug, season=SEASON)
    bloomsburg_row = next(r for r in parsed.rows if r.raw_opponent_name == "Bloomsburg")
    assert bloomsburg_row.opponent_team_id is None
    assert bloomsburg_row.validation_status == "unresolved"


@pytestmark_real
def test_real_overtime_games_frank_mitchell():
    team_identity, season_identity, roster = resolve_team_roster_for_player_gamelogs(CBB_ROOT, "st-bonaventure", SEASON)
    discovered = discover_player_game_log_artifacts(CBB_ROOT, season=SEASON, team_slugs=["st-bonaventure"])
    frank = next(d for d in discovered if d.player_slug == "frank_mitchell")
    df, _h = load_player_game_log_dataframe(frank, CBB_ROOT)
    parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=season_identity, roster_identities=roster, team_slug=frank.team_slug, player_slug=frank.player_slug, season=SEASON)
    ot_rows = [r for r in parsed.rows if r.overtime_marker]
    assert len(ot_rows) == 2  # confirmed real: Ohio and George Washington games went to OT


@pytestmark_real
def test_real_persist_full_team_end_to_end(session_factory):
    team_identity, season_identity, roster = resolve_team_roster_for_player_gamelogs(CBB_ROOT, "st-bonaventure", SEASON)
    discovered = discover_player_game_log_artifacts(CBB_ROOT, season=SEASON, team_slugs=["st-bonaventure"])
    _seed_season(session_factory)

    total_written = 0
    for d in discovered:
        df, content_hash = load_player_game_log_dataframe(d, CBB_ROOT)
        parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=season_identity, roster_identities=roster, team_slug=d.team_slug, player_slug=d.player_slug, season=SEASON)
        plan = build_player_game_log_import_plan(parsed)
        result = persist_player_game_log_import_plan(plan, session_factory, discovered=d, content_hash=content_hash)
        total_written += result.observations_written

    assert total_written == 291
    with unit_of_work(session_factory) as session:
        assert session.query(player_game_log_models.PlayerGameLogObservation).count() == 291
        assert session.query(models.Player).count() == 13
        sbu = session.query(models.Team).filter_by(team_id=team_identity.team_id).one()
        assert sbu.display_name == "St. Bonaventure"  # confirms the real resolved identity, never a placeholder derived from the raw directory slug


# ---------------------------------------------------------------------------
# Protected team-slug remediation batch: real-fixture proof that the 30
# player-gamelog identities previously blocked by the unresolved
# "miami-(oh)"/"saint-joseph's" team-identity gap (Phase 5B5 finding) now
# resolve, following the src/slug_utils.py correction. St. Thomas (MN)
# remains, correctly, still unresolved -- that gap is genuine local-roster
# absence, never a team-slug problem, and this batch does not touch it.
# ---------------------------------------------------------------------------


@pytestmark_real
def test_real_miami_oh_team_and_all_15_players_now_resolve():
    team_identity, season_identity, roster = resolve_team_roster_for_player_gamelogs(CBB_ROOT, "miami-(oh)", SEASON)
    assert isinstance(team_identity, TeamIdentity), "miami-(oh) team identity must now resolve (protected team-slug remediation)"
    assert team_identity.display_name == "Miami (OH)"
    assert len(roster) > 0, "the real local Roster_2026.xlsx must now actually load"

    discovered = discover_player_game_log_artifacts(CBB_ROOT, season=SEASON, team_slugs=["miami-(oh)"])
    assert len(discovered) == 15  # confirmed real file count, Phase 5B5

    resolved_players = 0
    for d in discovered:
        df, _h = load_player_game_log_dataframe(d, CBB_ROOT)
        parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=season_identity, roster_identities=roster, team_slug=d.team_slug, player_slug=d.player_slug, season=SEASON)
        resolved_players += int(parsed.player_id is not None)
        assert parsed.status != "invalid"  # no longer fails before player resolution even runs

    assert resolved_players == 15


@pytestmark_real
def test_real_saint_josephs_team_and_all_15_players_now_resolve():
    team_identity, season_identity, roster = resolve_team_roster_for_player_gamelogs(CBB_ROOT, "saint-joseph's", SEASON)
    assert isinstance(team_identity, TeamIdentity), "saint-joseph's team identity must now resolve (protected team-slug remediation)"
    assert team_identity.display_name == "Saint Joseph's"
    assert len(roster) > 0

    discovered = discover_player_game_log_artifacts(CBB_ROOT, season=SEASON, team_slugs=["saint-joseph's"])
    assert len(discovered) == 15

    resolved_players = 0
    for d in discovered:
        df, _h = load_player_game_log_dataframe(d, CBB_ROOT)
        parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=season_identity, roster_identities=roster, team_slug=d.team_slug, player_slug=d.player_slug, season=SEASON)
        resolved_players += int(parsed.player_id is not None)
        assert parsed.status != "invalid"

    assert resolved_players == 15


@pytestmark_real
def test_real_st_thomas_mn_remains_unresolved_genuine_roster_absence():
    """Confirms this batch's fix is narrowly scoped: st-thomas-(mn)'s own team identity already resolved before this batch (a pre-existing _SR_OVERRIDES entry, unrelated to this correction) -- its 2 players stay unresolved because no local Roster_2026.xlsx/AllStats evidence exists for this team at all, not because of any team-slug gap. Never worked around."""
    team_identity, season_identity, roster = resolve_team_roster_for_player_gamelogs(CBB_ROOT, "st-thomas-(mn)", SEASON)
    assert isinstance(team_identity, TeamIdentity)
    assert len(roster) == 0  # confirmed real: no local roster evidence exists for this team

    discovered = discover_player_game_log_artifacts(CBB_ROOT, season=SEASON, team_slugs=["st-thomas-(mn)"])
    assert len(discovered) == 2

    unresolved_players = 0
    for d in discovered:
        df, _h = load_player_game_log_dataframe(d, CBB_ROOT)
        parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=season_identity, roster_identities=roster, team_slug=d.team_slug, player_slug=d.player_slug, season=SEASON)
        unresolved_players += int(parsed.player_id is None)

    assert unresolved_players == 2


@pytestmark_real
def test_real_full_dry_run_after_slug_remediation_matches_reproduced_counts():
    """Locks in the exact, independently-reproduced before/after counts for this batch (see docs/migration_state.md's team-slug-remediation section) -- 32 unresolved players before, 2 after (only the genuine St. Thomas roster-absence gap remains)."""
    discovered = discover_player_game_log_artifacts(CBB_ROOT, season=SEASON)
    assert len(discovered) == 1561

    roster_cache: dict[str, tuple] = {}
    total_rows = 0
    resolved_players = 0
    unresolved_players = 0
    by_status = {"valid": 0, "partial": 0, "invalid": 0}

    for d in discovered:
        if d.team_slug not in roster_cache:
            roster_cache[d.team_slug] = resolve_team_roster_for_player_gamelogs(CBB_ROOT, d.team_slug, SEASON)
        team_identity, season_identity, roster = roster_cache[d.team_slug]
        df, _h = load_player_game_log_dataframe(d, CBB_ROOT)
        parsed = parse_player_game_log_workbook(df, team_identity=team_identity, season_identity=season_identity, roster_identities=roster, team_slug=d.team_slug, player_slug=d.player_slug, season=SEASON)
        by_status[parsed.status] += 1
        total_rows += len(parsed.rows)
        if parsed.player_id is not None:
            resolved_players += 1
        else:
            unresolved_players += 1

    assert by_status == {"valid": 588, "partial": 973, "invalid": 0}
    assert total_rows == 37623
    assert resolved_players == 1559
    assert unresolved_players == 2


# ---------------------------------------------------------------------------
# AST guards: no live access, no TeamDump mutation, no OVR/volatility recalculation
# ---------------------------------------------------------------------------


def test_adapter_never_imports_network_libraries():
    import ast
    import inspect
    import src.pipeline.player_game_log_import_adapter as mod

    tree = ast.parse(inspect.getsource(mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not ({"requests", "urllib", "socket", "selenium"} & imported)


def test_adapter_never_writes_to_teamdump():
    import ast
    import inspect
    import src.pipeline.player_game_log_import_adapter as mod

    tree = ast.parse(inspect.getsource(mod))
    called = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not ({"to_excel", "to_csv"} & called)


def test_adapter_never_imports_ovr_or_volatility_modules():
    import ast
    import inspect
    import src.pipeline.player_game_log_import_adapter as mod

    imported_modules = {n.module for n in ast.walk(ast.parse(inspect.getsource(mod))) if isinstance(n, ast.ImportFrom) and n.module}
    forbidden = ("ovr_model", "player_ovr_transformation", "player_benchmarks_transformation", "game_projection", "bet_decision", "expected_value", "kelly")
    for m in imported_modules:
        assert not any(f in m.lower() for f in forbidden), f"unexpected import: {m}"


def test_adapter_never_imports_ovr_model_at_all():
    """Explicit guard: this batch must never touch the protected src/ovr_model.py, including its own live _player_recency_factor -- confirmed by import-graph inspection, not just a formula-change check."""
    import ast
    import inspect
    import src.pipeline.player_game_log_import_adapter as mod

    tree = ast.parse(inspect.getsource(mod))
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    assert "src.ovr_model" not in imported_modules
