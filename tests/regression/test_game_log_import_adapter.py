"""
Phase 5B2: tests for `src/pipeline/game_log_import_adapter.py`.

Real-fixture tests (St. Bonaventure, Michigan State, the real Akron/Kent
State mirrored matchup [played three times in the same season, confirmed
real data], and Arizona [the real advanced-schema fixture]) exercise the
full discover -> load -> parse -> plan -> persist -> reconcile pipeline
against actual `TeamDump/CBB` files (read-only). Everything else uses
small, synthetic, in-memory DataFrames for deterministic edge cases.
"""
from __future__ import annotations

import ast
import datetime as dt
import inspect
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine, event

from src.db.base import Base
from src.db import models, game_log_models
from src.db.session import build_session_factory, unit_of_work
from src.db.repositories import SeasonRepository
from src.exceptions import IdentityConflictError, ValidationError
from src.identity.contracts import TeamIdentity, UnresolvedIdentity
from src.pipeline.game_log_import_adapter import (
    DiscoveredGameLogFile,
    ObservationForReconciliation,
    build_game_log_import_plan,
    classify_schema_variant,
    discover_game_log_artifacts,
    load_game_log_dataframe,
    parse_game_log_workbook,
    persist_game_log_import_plan,
    reconcile_game_observations,
)

SEASON = 2026
SBU_LOG = Path("TeamDump/CBB/st-bonaventure/st-bonaventure_2026_game_log.csv")
MSU_LOG = Path("TeamDump/CBB/michigan-state/michigan-state_2026_game_log.csv")
AKRON_LOG = Path("TeamDump/CBB/akron/akron_2026_game_log.csv")
KENT_STATE_LOG = Path("TeamDump/CBB/kent-state/kent-state_2026_game_log.csv")
ARIZONA_LOG = Path("TeamDump/CBB/arizona/arizona_2026_game_log.csv")


def _real_fixtures_present() -> bool:
    return all(p.exists() for p in (SBU_LOG, MSU_LOG, AKRON_LOG, KENT_STATE_LOG, ARIZONA_LOG))


pytestmark_real = pytest.mark.skipif(not _real_fixtures_present(), reason="real game-log fixtures not present in this checkout")


def _basic_df(rows: list[dict]) -> pd.DataFrame:
    base = {"game_num": 1, "date": None, "opponent": None, "pts_scored": None, "pts_allowed": None, "ORtg": 100.0, "DRtg": 95.0, "net_rtg": 5.0}
    return pd.DataFrame([{**base, **r} for r in rows])


def _advanced_df(rows: list[dict]) -> pd.DataFrame:
    base = {
        "game_num": 1, "date": None, "location": None, "opponent": None, "game_type": "REG (Non-Conf)",
        "result": None, "pts_scored": None, "pts_allowed": None, "overtimes": None,
        "ORtg": 100.0, "DRtg": 95.0, "pace": 68.0, "fta_rate": 0.3, "fg3a_rate": 0.35, "ts_pct": 0.55,
        "trb_pct": 0.5, "ast_pct": 0.5, "stl_pct": 0.08, "blk_pct": 0.08, "efg_pct": 0.52, "tov_pct": 0.15,
        "orb_pct": 0.28, "ft_rate": 0.25, "opp_efg_pct": 0.48, "opp_tov_pct": 0.15, "opp_orb_pct": 0.28,
        "opp_ft_rate": 0.25, "net_rtg": 5.0, "margin": 5,
    }
    return pd.DataFrame([{**base, **r} for r in rows])


# ---------------------------------------------------------------------------
# Stage 1: discovery
# ---------------------------------------------------------------------------


def test_discover_deterministic_and_relative_path(tmp_path):
    team_dir = tmp_path / "test-team"
    team_dir.mkdir()
    (team_dir / f"test-team_{SEASON}_game_log.csv").write_bytes(b"fake")

    first = discover_game_log_artifacts(tmp_path, season=SEASON)
    second = discover_game_log_artifacts(tmp_path, season=SEASON)
    assert first == second
    assert len(first) == 1
    assert first[0].relative_path == f"test-team/test-team_{SEASON}_game_log.csv"
    assert not first[0].relative_path.startswith(str(tmp_path))


def test_discover_never_hardcodes_teamdump_path():
    import src.pipeline.game_log_import_adapter as mod
    assert "TeamDump" not in inspect.getsource(mod.discover_game_log_artifacts)


def test_discover_missing_file_excluded(tmp_path):
    (tmp_path / "empty-team").mkdir()
    assert discover_game_log_artifacts(tmp_path, season=SEASON) == []


def test_discover_respects_team_slugs_filter(tmp_path):
    for name in ("team-a", "team-b"):
        d = tmp_path / name
        d.mkdir()
        (d / f"{name}_{SEASON}_game_log.csv").write_bytes(b"fake")
    discovered = discover_game_log_artifacts(tmp_path, season=SEASON, team_slugs=["team-a"])
    assert [d.team_slug for d in discovered] == ["team-a"]


# ---------------------------------------------------------------------------
# Schema classification
# ---------------------------------------------------------------------------


def test_classify_basic_schema():
    assert classify_schema_variant(_basic_df([{"date": "2026-01-01", "opponent": "X"}])) == "basic"


def test_classify_advanced_schema():
    assert classify_schema_variant(_advanced_df([{"date": "2026-01-01", "opponent": "X", "location": "N"}])) == "advanced"


# ---------------------------------------------------------------------------
# Season-total-row classifier (content-based, never positional)
# ---------------------------------------------------------------------------


def test_season_total_row_excluded_from_basic_schema():
    df = _basic_df([
        {"date": "2025-11-03", "opponent": "St. Bonaventure", "pts_scored": 70, "pts_allowed": 60},
        {"date": None, "opponent": None, "pts_scored": 2500, "pts_allowed": 2400},  # confirmed real defect shape
    ])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    assert parsed.excluded_row_count == 1
    assert len(parsed.rows) == 1
    assert any(i.issue_code == "gamelog_trailing_season_total_row" for i in parsed.issues)


def test_season_total_row_excluded_regardless_of_position_shuffled():
    """Instruction: do not rely on it being the final row, do not rely on sort order -- confirmed via shuffling."""
    df = _basic_df([
        {"date": None, "opponent": None, "pts_scored": 2500, "pts_allowed": 2400},  # now FIRST, not last
        {"date": "2025-11-03", "opponent": "St. Bonaventure", "pts_scored": 70, "pts_allowed": 60},
        {"date": "2025-11-08", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70},
    ])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    assert parsed.excluded_row_count == 1
    assert len(parsed.rows) == 2
    assert all(r.raw_opponent_name in ("St. Bonaventure", "Colgate") for r in parsed.rows)


def test_season_total_row_excluded_when_moved_to_the_middle():
    """The classifier must be purely content-based -- not "final row", not "first row" -- proven here with the aggregate row sandwiched between two genuine games."""
    df = _basic_df([
        {"date": "2025-11-03", "opponent": "St. Bonaventure", "pts_scored": 70, "pts_allowed": 60},
        {"date": None, "opponent": None, "pts_scored": 2500, "pts_allowed": 2400},  # in the middle, not first or last
        {"date": "2025-11-08", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70},
    ])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    assert parsed.excluded_row_count == 1
    assert len(parsed.rows) == 2
    assert {r.raw_opponent_name for r in parsed.rows} == {"St. Bonaventure", "Colgate"}


def test_legitimate_final_game_row_never_misclassified_as_season_total():
    """A file with NO season-total defect (every real row has both date and opponent) must exclude nothing, including its own last row."""
    df = _basic_df([
        {"date": "2025-11-03", "opponent": "St. Bonaventure", "pts_scored": 70, "pts_allowed": 60},
        {"date": "2026-03-22", "opponent": "Colgate", "pts_scored": 55, "pts_allowed": 50},  # last row, but a genuine game
    ])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    assert parsed.excluded_row_count == 0
    assert len(parsed.rows) == 2


def test_similar_looking_but_valid_blowout_row_never_misclassified():
    """A real, valid, wide-margin game (large point values, like a season-total row's own inflated numbers) must NOT be excluded -- the classifier keys only on blank date/opponent, never on point-value magnitude."""
    df = _basic_df([{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 130, "pts_allowed": 45}])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    assert parsed.excluded_row_count == 0
    assert len(parsed.rows) == 1
    assert parsed.rows[0].validation_status == "valid"


def test_season_total_row_never_creates_observation_game_or_stat_row():
    df = _basic_df([{"date": None, "opponent": None, "pts_scored": 2500, "pts_allowed": 2400}])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    plan = build_game_log_import_plan(parsed)
    assert plan.observation_specs == []
    assert plan.counts.season_total_rows_excluded == 1


def test_row_with_only_blank_date_but_real_opponent_not_classified_as_season_total():
    """Confirms the classifier requires BOTH blank date AND blank opponent -- a row with a real opponent but an unparseable date is a different, genuine issue (invalid date), not the season-total defect."""
    df = _basic_df([{"date": None, "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    assert parsed.excluded_row_count == 0
    assert len(parsed.rows) == 1
    assert parsed.rows[0].validation_status == "invalid"
    assert parsed.rows[0].exclusion_reason == "unparseable_date"


# ---------------------------------------------------------------------------
# Missing sheets/columns, empty logs
# ---------------------------------------------------------------------------


def test_missing_required_columns_invalid():
    df = pd.DataFrame([{"foo": 1}])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    assert parsed.status == "invalid"
    assert any(i.issue_code == "game_log_missing_required_columns" for i in parsed.issues)


def test_empty_log_invalid():
    df = pd.DataFrame(columns=["date", "opponent", "pts_scored", "pts_allowed"])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    assert parsed.status == "invalid"


def test_allstats_shaped_table_rejected_defensively():
    """A defensive guard: a table with a 'Player' column is not a game log -- never silently misparsed."""
    df = pd.DataFrame([{"Player": "X", "date": "2026-01-01", "opponent": "Y"}])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    assert parsed.status == "invalid"


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------


def test_valid_iso_date_parsed():
    df = _basic_df([{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    assert parsed.rows[0].game_date == dt.date(2026, 1, 15)


def test_invalid_date_format_marked_invalid():
    df = _basic_df([{"date": "not-a-date", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    assert parsed.rows[0].validation_status == "invalid"
    assert parsed.rows[0].exclusion_reason == "unparseable_date"
    assert parsed.rows[0].game_id is None


def test_date_only_precision_never_gets_a_fabricated_time():
    """Instruction: do not infer tipoff time from a date-only field. The parser never even produces a `scheduled_timestamp` value (no real source column supplies one) -- `ParsedGameLogRow` has no such field to fabricate, and the repository/model field it feeds defaults to None."""
    df = _basic_df([{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    assert not hasattr(parsed.rows[0], "scheduled_timestamp")
    assert isinstance(parsed.rows[0].game_date, dt.date)
    plan = build_game_log_import_plan(parsed)
    assert "scheduled_timestamp" not in plan.observation_specs[0]


# ---------------------------------------------------------------------------
# Venue: explicit only, never inferred
# ---------------------------------------------------------------------------


def test_basic_schema_venue_always_unknown():
    df = _basic_df([{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    assert parsed.rows[0].venue_status == "unknown"


@pytest.mark.parametrize("location_raw,expected", [(None, "home"), ("@", "away"), ("N", "neutral")])
def test_advanced_schema_venue_explicit(location_raw, expected):
    df = _advanced_df([{"date": "2026-01-15", "opponent": "Colgate", "location": location_raw, "pts_scored": 80, "pts_allowed": 70}])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    assert parsed.rows[0].venue_status == expected


def test_venue_never_inferred_from_score_or_team_order():
    """Two rows, identical team-order/score shape, different real venues -- proves venue comes only from the explicit location column, never derived from anything else."""
    df = _advanced_df([
        {"date": "2026-01-15", "opponent": "Colgate", "location": "@", "pts_scored": 80, "pts_allowed": 70},
        {"date": "2026-01-20", "opponent": "Bucknell", "location": None, "pts_scored": 80, "pts_allowed": 70},
    ])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    assert parsed.rows[0].venue_status == "away"
    assert parsed.rows[1].venue_status == "home"


# ---------------------------------------------------------------------------
# Score/winner/overtime evidence
# ---------------------------------------------------------------------------


def test_overtime_marker_preserved_raw():
    df = _advanced_df([{"date": "2026-01-15", "opponent": "Colgate", "overtimes": "OT", "pts_scored": 80, "pts_allowed": 78}])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    assert parsed.rows[0].overtime_marker == "OT"


def test_missing_overtime_marker_is_none_not_false():
    df = _basic_df([{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    assert parsed.rows[0].overtime_marker is None


def test_raw_result_marker_preserved():
    df = _advanced_df([{"date": "2026-01-15", "opponent": "Colgate", "result": "W", "pts_scored": 80, "pts_allowed": 70}])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    assert parsed.rows[0].raw_result_marker == "W"


# ---------------------------------------------------------------------------
# Opponent resolution
# ---------------------------------------------------------------------------


def test_opponent_exact_resolution():
    df = _basic_df([{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    assert parsed.rows[0].opponent_team_id is not None
    assert parsed.rows[0].validation_status == "valid"


def test_opponent_unresolved_remains_unresolved_and_auditable():
    df = _basic_df([{"date": "2026-01-15", "opponent": "Definitely Not A Real School XYZ", "pts_scored": 80, "pts_allowed": 70}])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    assert parsed.rows[0].opponent_team_id is None
    assert parsed.rows[0].validation_status == "unresolved"
    assert any(i.issue_code == "gamelog_opponent_unresolved" for i in parsed.issues)


def test_one_unresolved_row_does_not_invalidate_others():
    df = _basic_df([
        {"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70},
        {"date": "2026-01-20", "opponent": "Not A Real School", "pts_scored": 75, "pts_allowed": 65},
    ])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    assert parsed.status == "partial"
    assert len(parsed.rows) == 2
    assert parsed.rows[0].validation_status == "valid"
    assert parsed.rows[1].validation_status == "unresolved"


def test_same_team_game_rejected():
    df = _basic_df([{"date": "2026-01-15", "opponent": "Michigan State", "pts_scored": 80, "pts_allowed": 70}])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    assert parsed.rows[0].validation_status == "invalid"
    assert parsed.rows[0].exclusion_reason == "same_team_game"
    assert parsed.rows[0].game_id is None


def test_team_query_unresolved_whole_file_invalid():
    df = _basic_df([{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    parsed = parse_game_log_workbook(df, "Definitely Not A Real Team XYZ", SEASON)
    assert parsed.status == "invalid"
    assert parsed.rows == []


def test_season_unresolved_whole_file_invalid():
    df = _basic_df([{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    parsed = parse_game_log_workbook(df, "Michigan State", 99999)
    assert parsed.status == "invalid"


# ---------------------------------------------------------------------------
# Game identity: deterministic, order-independent, provisional
# ---------------------------------------------------------------------------


def test_game_id_deterministic_across_repeated_parses():
    df = _basic_df([{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    parsed_1 = parse_game_log_workbook(df, "Michigan State", SEASON)
    parsed_2 = parse_game_log_workbook(df, "Michigan State", SEASON)
    assert parsed_1.rows[0].game_id == parsed_2.rows[0].game_id


def test_game_id_independent_of_perspective_team():
    """The SAME real game, viewed from either team's own file, must produce the SAME game_id."""
    df_msu_side = _basic_df([{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    df_colgate_side = _basic_df([{"date": "2026-01-15", "opponent": "Michigan State", "pts_scored": 70, "pts_allowed": 80}])
    parsed_msu = parse_game_log_workbook(df_msu_side, "Michigan State", SEASON)
    parsed_colgate = parse_game_log_workbook(df_colgate_side, "Colgate", SEASON)
    assert parsed_msu.rows[0].game_id == parsed_colgate.rows[0].game_id


def test_game_id_never_fabricated_from_row_position():
    """Two different real games (different dates) must get different game_ids, regardless of both being 'row 0' in their own file."""
    df_a = _basic_df([{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    df_b = _basic_df([{"date": "2026-02-20", "opponent": "Colgate", "pts_scored": 60, "pts_allowed": 55}])
    parsed_a = parse_game_log_workbook(df_a, "Michigan State", SEASON)
    parsed_b = parse_game_log_workbook(df_b, "Michigan State", SEASON)
    assert parsed_a.rows[0].game_id != parsed_b.rows[0].game_id


# ---------------------------------------------------------------------------
# Reconciliation (pure)
# ---------------------------------------------------------------------------


def _obs(team_id, pts_scored, pts_allowed, *, venue="unknown", ot=None, result=None, observation_id=1):
    return ObservationForReconciliation(observation_id=observation_id, team_id=team_id, points_scored=pts_scored, points_allowed=pts_allowed, venue_status=venue, overtime_marker=ot, raw_result_marker=result)


def test_reconcile_mirrored_agreement():
    obs_a = _obs(1, 80, 70, observation_id=10)
    obs_b = _obs(2, 70, 80, observation_id=20)
    result = reconcile_game_observations([obs_a, obs_b], team_a_id=1, team_b_id=2)
    assert result.result_status == "reconciled"
    assert result.validation_status == "valid"
    assert result.team_a_score == 80
    assert result.team_b_score == 70
    assert result.winner_team_id == 1
    assert result.loser_team_id == 2
    assert result.source_count == 2


def test_reconcile_single_sided_team_a_only():
    obs_a = _obs(1, 80, 70, observation_id=10)
    result = reconcile_game_observations([obs_a], team_a_id=1, team_b_id=2)
    assert result.result_status == "single_sided_provisional"
    assert result.validation_status == "partial"
    assert result.team_a_score == 80
    assert result.team_b_score == 70
    assert result.source_count == 1
    assert result.team_a_observation_id == 10
    assert result.team_b_observation_id is None


def test_reconcile_single_sided_team_b_only():
    obs_b = _obs(2, 70, 80, observation_id=20)
    result = reconcile_game_observations([obs_b], team_a_id=1, team_b_id=2)
    assert result.result_status == "single_sided_provisional"
    assert result.team_a_score == 80  # obs_b's pts_allowed
    assert result.team_b_score == 70  # obs_b's pts_scored
    assert result.team_b_observation_id == 20
    assert result.team_a_observation_id is None


def test_reconcile_score_conflict_never_averaged_scores_stay_null():
    obs_a = _obs(1, 80, 70, observation_id=10)
    obs_b = _obs(2, 65, 75, observation_id=20)  # disagrees with obs_a
    result = reconcile_game_observations([obs_a, obs_b], team_a_id=1, team_b_id=2)
    assert result.result_status == "conflicting"
    assert result.validation_status == "conflicting"
    assert result.team_a_score is None
    assert result.team_b_score is None
    assert result.winner_team_id is None
    assert "score_mismatch" in result.conflict_reasons


def test_reconcile_venue_conflict():
    obs_a = _obs(1, 80, 70, venue="home", observation_id=10)
    obs_b = _obs(2, 70, 80, venue="home", observation_id=20)  # both claim home -- not complementary
    result = reconcile_game_observations([obs_a, obs_b], team_a_id=1, team_b_id=2)
    assert result.result_status == "conflicting"
    assert "venue_mismatch" in result.conflict_reasons


def test_reconcile_complementary_venue_not_a_conflict():
    obs_a = _obs(1, 80, 70, venue="home", observation_id=10)
    obs_b = _obs(2, 70, 80, venue="away", observation_id=20)
    result = reconcile_game_observations([obs_a, obs_b], team_a_id=1, team_b_id=2)
    assert result.result_status == "reconciled"


def test_reconcile_overtime_conflict():
    obs_a = _obs(1, 80, 78, ot="OT", observation_id=10)
    obs_b = _obs(2, 78, 80, ot=None, observation_id=20)  # obs_b's overtime field IS present-but-empty vs obs_a's real OT -- construct explicit disagreement
    # Force an explicit contradiction: obs_b claims a marker string that means "no OT" is not directly expressible
    # (overtime_marker=None means "no evidence", not "confirmed no OT") -- so use a value to force disagreement instead.
    obs_b_explicit_no_ot = ObservationForReconciliation(observation_id=20, team_id=2, points_scored=78, points_allowed=80, venue_status="unknown", overtime_marker="", raw_result_marker=None)
    result = reconcile_game_observations([obs_a, obs_b_explicit_no_ot], team_a_id=1, team_b_id=2)
    assert result.result_status == "conflicting"
    assert "overtime_mismatch" in result.conflict_reasons


def test_reconcile_result_marker_conflict():
    obs_a = _obs(1, 80, 70, result="L", observation_id=10)  # A claims it lost despite scoring more -- contradictory evidence
    obs_b = _obs(2, 70, 80, result="L", observation_id=20)  # both claim "L" -- impossible
    result = reconcile_game_observations([obs_a, obs_b], team_a_id=1, team_b_id=2)
    assert result.result_status == "conflicting"
    assert "result_marker_mismatch" in result.conflict_reasons


def test_reconcile_rejects_more_than_two_observations():
    obs_a, obs_b, obs_c = _obs(1, 80, 70), _obs(2, 70, 80), _obs(3, 1, 1)
    with pytest.raises(ValidationError):
        reconcile_game_observations([obs_a, obs_b, obs_c], team_a_id=1, team_b_id=2)


def test_reconcile_rejects_same_team_twice():
    obs_a1 = _obs(1, 80, 70, observation_id=10)
    obs_a2 = _obs(1, 81, 71, observation_id=11)
    with pytest.raises(ValidationError):
        reconcile_game_observations([obs_a1, obs_a2], team_a_id=1, team_b_id=2)


def test_reconcile_rejects_no_observations():
    with pytest.raises(ValidationError):
        reconcile_game_observations([], team_a_id=1, team_b_id=2)


def test_reconcile_rejects_same_team_a_and_b_identity():
    """Defensive guard added by this audit: a degenerate call with team_a_id == team_b_id must be rejected explicitly, never silently treat one observation as both sides of the same game."""
    obs = _obs(1, 80, 70, observation_id=10)
    with pytest.raises(ValidationError):
        reconcile_game_observations([obs], team_a_id=1, team_b_id=1)


def test_different_opponent_never_collides_game_id():
    """Two rows with the SAME perspective team and date but DIFFERENT opponents must never share a game_id."""
    df_a = _basic_df([{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    df_b = _basic_df([{"date": "2026-01-15", "opponent": "Bucknell", "pts_scored": 80, "pts_allowed": 70}])
    parsed_a = parse_game_log_workbook(df_a, "Michigan State", SEASON)
    parsed_b = parse_game_log_workbook(df_b, "Michigan State", SEASON)
    assert parsed_a.rows[0].game_id != parsed_b.rows[0].game_id


def test_reconcile_never_mutates_input():
    obs_a = _obs(1, 80, 70, observation_id=10)
    obs_b = _obs(2, 70, 80, observation_id=20)
    snapshot_a, snapshot_b = obs_a, obs_b  # frozen dataclasses -- any mutation attempt would raise
    reconcile_game_observations([obs_a, obs_b], team_a_id=1, team_b_id=2)
    assert obs_a == snapshot_a
    assert obs_b == snapshot_b
    with pytest.raises(Exception):
        obs_a.points_scored = 999


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


def _seed_team_and_season(session_factory, plan):
    """
    Only the Season row needs pre-seeding: `persist_game_log_import_plan`
    registers every team itself (perspective team + resolved opponents)
    via `plan.resolved_team_specs`, using each team's REAL canonical
    sr_slug. Pre-seeding a Team here with a placeholder sr_slug would
    make that later, correct registration collide as an IdentityConflict.
    """
    with unit_of_work(session_factory) as session:
        if plan.season_id is not None and session.get(models.Season, plan.season_id) is None:
            SeasonRepository(session).upsert(season_id=plan.season_id, start_year=plan.season_id - 1, end_year=plan.season_id, label="2025-26")


def _discovered_and_hash(tmp_path, df, team_slug="test-team"):
    import hashlib

    team_dir = tmp_path / team_slug
    team_dir.mkdir(exist_ok=True, parents=True)
    path = team_dir / f"{team_slug}_{SEASON}_game_log.csv"
    df.to_csv(path, index=False)
    stat = path.stat()
    discovered = DiscoveredGameLogFile(
        team_slug=team_slug, season=SEASON, relative_path=f"{team_slug}/{team_slug}_{SEASON}_game_log.csv",
        file_size_bytes=stat.st_size, filesystem_mtime=dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc),
    )
    return discovered, hashlib.sha256(path.read_bytes()).hexdigest()


def test_persist_writes_artifact_observation_stat_line_and_result(session_factory, tmp_path):
    df = _advanced_df([{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    plan = build_game_log_import_plan(parsed)
    _seed_team_and_season(session_factory, plan)
    discovered, content_hash = _discovered_and_hash(tmp_path, df)

    result = persist_game_log_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash)

    assert result.observations_written == 1
    assert result.stat_lines_written == 1
    assert result.results_upserted == 1  # single-sided (no Colgate file imported)

    with unit_of_work(session_factory) as session:
        assert session.query(game_log_models.TeamGameLogObservation).count() == 1
        assert session.query(game_log_models.TeamGameStatLine).count() == 1
        assert session.query(game_log_models.GameResult).count() == 1
        gr = session.query(game_log_models.GameResult).one()
        assert gr.result_status == "single_sided_provisional"


def test_persist_idempotent_on_identical_repeat_import(session_factory, tmp_path):
    df = _basic_df([{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    plan = build_game_log_import_plan(parsed)
    _seed_team_and_season(session_factory, plan)
    discovered, content_hash = _discovered_and_hash(tmp_path, df)

    persist_game_log_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash)
    persist_game_log_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash)

    with unit_of_work(session_factory) as session:
        assert session.query(game_log_models.TeamGameLogObservation).count() == 1
        assert session.query(game_log_models.GameResult).count() == 1
        assert session.query(models.SourceArtifact).count() == 1


def test_persist_rolls_back_entire_import_on_conflict(session_factory, tmp_path):
    df = _basic_df([{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    plan = build_game_log_import_plan(parsed)
    _seed_team_and_season(session_factory, plan)
    discovered, content_hash = _discovered_and_hash(tmp_path, df)
    persist_game_log_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash)

    conflicting_df = _basic_df([{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 99, "pts_allowed": 70}])  # same key, different score
    conflicting_parsed = parse_game_log_workbook(conflicting_df, "Michigan State", SEASON)
    conflicting_plan = build_game_log_import_plan(conflicting_parsed)

    with pytest.raises(IdentityConflictError):
        persist_game_log_import_plan(conflicting_plan, session_factory, discovered=discovered, content_hash=content_hash)

    with unit_of_work(session_factory) as session:
        obs = session.query(game_log_models.TeamGameLogObservation).one()
        assert obs.points_scored == 80  # untouched by the failed conflicting attempt
        assert session.query(game_log_models.GameResult).count() == 1  # no orphan/extra result either


def test_persist_no_orphan_stat_lines_after_rollback(session_factory, tmp_path):
    df = _basic_df([{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    plan = build_game_log_import_plan(parsed)
    _seed_team_and_season(session_factory, plan)
    discovered, content_hash = _discovered_and_hash(tmp_path, df)
    persist_game_log_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash)

    conflicting_df = _basic_df([{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 99, "pts_allowed": 70}])
    conflicting_parsed = parse_game_log_workbook(conflicting_df, "Michigan State", SEASON)
    conflicting_plan = build_game_log_import_plan(conflicting_parsed)
    with pytest.raises(IdentityConflictError):
        persist_game_log_import_plan(conflicting_plan, session_factory, discovered=discovered, content_hash=content_hash)

    with unit_of_work(session_factory) as session:
        assert session.query(game_log_models.TeamGameStatLine).count() == 1  # exactly the one from the successful import


def test_persist_import_order_independence(session_factory, tmp_path):
    """Importing team A then B, or B then A, must leave the database in the same final reconciled state."""
    df_a = _basic_df([{"date": "2026-01-15", "opponent": "Team B", "pts_scored": 80, "pts_allowed": 70}])
    df_b = _basic_df([{"date": "2026-01-15", "opponent": "Team A", "pts_scored": 70, "pts_allowed": 80}])

    def run(order):
        eng = create_engine("sqlite+pysqlite:///:memory:", future=True)

        @event.listens_for(eng, "connect")
        def _fk(c, r):
            cur = c.cursor(); cur.execute("PRAGMA foreign_keys=ON"); cur.close()

        Base.metadata.create_all(eng)
        sf = build_session_factory(eng)
        for i, (df, query) in enumerate(order):
            parsed = parse_game_log_workbook(df, query, SEASON)
            plan = build_game_log_import_plan(parsed)
            _seed_team_and_season(sf, plan)
            discovered, content_hash = _discovered_and_hash(tmp_path / f"run_{id(order)}_{i}", df, team_slug=query.lower().replace(" ", "-"))
            persist_game_log_import_plan(plan, sf, discovered=discovered, content_hash=content_hash)
        with unit_of_work(sf) as session:
            results = [(r.team_a_id, r.team_b_id, r.team_a_score, r.team_b_score, r.result_status) for r in session.query(game_log_models.GameResult).all()]
        eng.dispose()
        return sorted(results)

    forward = run([(df_a, "Team A"), (df_b, "Team B")])
    backward = run([(df_b, "Team B"), (df_a, "Team A")])
    assert forward == backward
    assert all(r[4] == "reconciled" for r in forward)


def test_persist_pipeline_run_linking(session_factory, tmp_path):
    df = _basic_df([{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    plan = build_game_log_import_plan(parsed)
    _seed_team_and_season(session_factory, plan)
    discovered, content_hash = _discovered_and_hash(tmp_path, df)
    run_id = "1" * 32
    with unit_of_work(session_factory) as session:
        session.add(models.PipelineRun(run_id=run_id, pipeline_name="game_log_import_test", status="running", started_at=dt.datetime.now(dt.timezone.utc)))

    result = persist_game_log_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash, pipeline_run_id=run_id)

    with unit_of_work(session_factory) as session:
        artifact = session.query(models.SourceArtifact).one()
        assert artifact.pipeline_run_id == run_id
        obs = session.query(game_log_models.TeamGameLogObservation).one()
        assert obs.pipeline_run_id == run_id
        gr = session.query(game_log_models.GameResult).one()
        assert gr.pipeline_run_id == run_id


def test_persist_invalid_row_never_enters_reconciliation_but_valid_row_does(session_factory, tmp_path):
    """A file with one valid row and one invalid (unparseable-date) row: the invalid row is still persisted as raw evidence (never dropped silently), but only the valid row's game ever gets a GameResult."""
    df = _basic_df([
        {"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70},
        {"date": "garbage-date", "opponent": "Bucknell", "pts_scored": 60, "pts_allowed": 55},
    ])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    plan = build_game_log_import_plan(parsed)
    _seed_team_and_season(session_factory, plan)
    discovered, content_hash = _discovered_and_hash(tmp_path, df)

    result = persist_game_log_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash)

    assert result.observations_written == 2  # both rows persisted as raw evidence
    assert result.results_upserted == 1  # only the valid (Colgate) row produced a game_id and reached reconciliation

    with unit_of_work(session_factory) as session:
        invalid_obs = session.query(game_log_models.TeamGameLogObservation).filter_by(raw_opponent_name="Bucknell").one()
        assert invalid_obs.validation_status == "invalid"
        assert invalid_obs.game_id is None
        assert session.query(game_log_models.GameResult).count() == 1


def test_persist_source_artifact_linking_and_null_collection_timestamp(session_factory, tmp_path):
    df = _basic_df([{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    plan = build_game_log_import_plan(parsed)
    _seed_team_and_season(session_factory, plan)
    discovered, content_hash = _discovered_and_hash(tmp_path, df)
    persist_game_log_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash)

    with unit_of_work(session_factory) as session:
        artifact = session.query(models.SourceArtifact).one()
        assert artifact.collection_timestamp is None
        assert artifact.content_hash == content_hash
        assert artifact.filesystem_mtime is not None
        obs = session.query(game_log_models.TeamGameLogObservation).one()
        assert obs.source_artifact_id == artifact.id


def test_persist_changed_content_creates_new_artifact_never_overwrites(session_factory, tmp_path):
    df_v1 = _basic_df([{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}])
    parsed_v1 = parse_game_log_workbook(df_v1, "Michigan State", SEASON)
    plan_v1 = build_game_log_import_plan(parsed_v1)
    _seed_team_and_season(session_factory, plan_v1)
    discovered_v1, hash_v1 = _discovered_and_hash(tmp_path, df_v1)
    persist_game_log_import_plan(plan_v1, session_factory, discovered=discovered_v1, content_hash=hash_v1)

    df_v2 = _basic_df([{"date": "2026-01-15", "opponent": "Colgate", "pts_scored": 80, "pts_allowed": 70}, {"date": "2026-01-20", "opponent": "Bucknell", "pts_scored": 90, "pts_allowed": 60}])
    parsed_v2 = parse_game_log_workbook(df_v2, "Michigan State", SEASON)
    plan_v2 = build_game_log_import_plan(parsed_v2)
    discovered_v2, hash_v2 = _discovered_and_hash(tmp_path, df_v2)
    persist_game_log_import_plan(plan_v2, session_factory, discovered=discovered_v2, content_hash=hash_v2)

    with unit_of_work(session_factory) as session:
        assert session.query(models.SourceArtifact).count() == 2  # both artifacts coexist
        # additive, never overwritten: v1's 1 row (under artifact 1) plus v2's 2 rows (under artifact 2) == 3
        assert session.query(game_log_models.TeamGameLogObservation).count() == 3


# ---------------------------------------------------------------------------
# Real-fixture end-to-end
# ---------------------------------------------------------------------------


@pytestmark_real
def test_real_st_bonaventure_dry_run():
    cbb_root = Path("TeamDump/CBB")
    discovered = discover_game_log_artifacts(cbb_root, season=SEASON, team_slugs=["st-bonaventure"])[0]
    df, content_hash = load_game_log_dataframe(discovered, cbb_root)
    parsed = parse_game_log_workbook(df, "St. Bonaventure", SEASON)
    plan = build_game_log_import_plan(parsed)
    assert parsed.schema_variant == "basic"
    assert plan.counts.season_total_rows_excluded == 1
    assert plan.counts.games_constructed > 0


@pytestmark_real
def test_real_michigan_state_dry_run():
    cbb_root = Path("TeamDump/CBB")
    discovered = discover_game_log_artifacts(cbb_root, season=SEASON, team_slugs=["michigan-state"])[0]
    df, content_hash = load_game_log_dataframe(discovered, cbb_root)
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    plan = build_game_log_import_plan(parsed)
    assert plan.counts.season_total_rows_excluded == 1


@pytestmark_real
def test_real_arizona_advanced_schema_dry_run():
    cbb_root = Path("TeamDump/CBB")
    discovered = discover_game_log_artifacts(cbb_root, season=SEASON, team_slugs=["arizona"])[0]
    df, content_hash = load_game_log_dataframe(discovered, cbb_root)
    parsed = parse_game_log_workbook(df, "arizona", SEASON)
    plan = build_game_log_import_plan(parsed)
    assert parsed.schema_variant == "advanced"
    assert plan.counts.season_total_rows_excluded == 0  # confirmed: advanced schema never has the defect
    venues = {r.venue_status for r in parsed.rows}
    assert venues <= {"home", "away", "neutral", "unknown"}
    assert "away" in venues and "neutral" in venues  # confirmed real Arizona has both '@' and 'N' rows


@pytestmark_real
def test_real_akron_kent_state_mirrored_matchup_reconciles(session_factory):
    """Real-fixture demonstration: Akron and Kent State played three times in 2025-26 -- all three must reconcile to the exact real scores."""
    cbb_root = Path("TeamDump/CBB")
    akron_mtime_before = AKRON_LOG.stat().st_mtime
    kent_mtime_before = KENT_STATE_LOG.stat().st_mtime

    for slug in ("akron", "kent-state"):
        discovered = discover_game_log_artifacts(cbb_root, season=SEASON, team_slugs=[slug])[0]
        df, content_hash = load_game_log_dataframe(discovered, cbb_root)
        parsed = parse_game_log_workbook(df, slug, SEASON)
        plan = build_game_log_import_plan(parsed)
        _seed_team_and_season(session_factory, plan)
        persist_game_log_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash)

    with unit_of_work(session_factory) as session:
        akron_id = session.query(models.Team).filter_by(sr_slug="akron").one().team_id
        kent_id = session.query(models.Team).filter_by(sr_slug="kent-state").one().team_id
        team_a_id, team_b_id = sorted((akron_id, kent_id))
        results = (
            session.query(game_log_models.GameResult)
            .filter(game_log_models.GameResult.team_a_id == team_a_id, game_log_models.GameResult.team_b_id == team_b_id)
            .all()
        )
        reconciled = [r for r in results if r.result_status == "reconciled"]
        assert len(reconciled) == 3  # confirmed real: played three times in 2025-26
        for r in reconciled:
            assert r.team_a_score is not None and r.team_b_score is not None
            assert r.winner_team_id is not None
            assert {r.team_a_score, r.team_b_score} in [{69, 52}, {92, 70}, {75, 68}]  # confirmed real scores

    assert AKRON_LOG.stat().st_mtime == akron_mtime_before
    assert KENT_STATE_LOG.stat().st_mtime == kent_mtime_before


@pytestmark_real
def test_real_single_sided_matchup_present(session_factory):
    """Confirmed real case: Michigan State's own opponents are overwhelmingly NOT among the 144 locally-logged teams -- importing only Michigan State's file must produce single-sided results for those games."""
    cbb_root = Path("TeamDump/CBB")
    discovered = discover_game_log_artifacts(cbb_root, season=SEASON, team_slugs=["michigan-state"])[0]
    df, content_hash = load_game_log_dataframe(discovered, cbb_root)
    parsed = parse_game_log_workbook(df, "Michigan State", SEASON)
    plan = build_game_log_import_plan(parsed)
    _seed_team_and_season(session_factory, plan)
    persist_game_log_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash)

    with unit_of_work(session_factory) as session:
        results = session.query(game_log_models.GameResult).all()
        single_sided = [r for r in results if r.result_status == "single_sided_provisional"]
        assert len(single_sided) > 0


@pytestmark_real
def test_real_unresolved_opponent_present():
    """Confirmed real case (this batch's own precheck): 'Southern California' does not resolve via the canonical, non-fuzzy resolver from at least one real team's game log."""
    cbb_root = Path("TeamDump/CBB")
    discovered = discover_game_log_artifacts(cbb_root, season=SEASON, team_slugs=["arizona-state"])[0]
    df, content_hash = load_game_log_dataframe(discovered, cbb_root)
    parsed = parse_game_log_workbook(df, "arizona-state", SEASON)
    unresolved = [r for r in parsed.rows if r.validation_status == "unresolved"]
    assert len(unresolved) > 0
    assert any("Southern California" in r.raw_opponent_name for r in unresolved)


# ---------------------------------------------------------------------------
# AST guards: no analytical calculation, no live requests, no source-file mutation
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


def test_adapter_never_imports_analytical_modules():
    import src.pipeline.game_log_import_adapter as mod
    imported_modules = {n.module for n in ast.walk(ast.parse(inspect.getsource(mod))) if isinstance(n, ast.ImportFrom) and n.module}
    forbidden = ("ovr", "classification", "team_profile", "team_strength", "matchup", "bet_decision", "expected_value", "kelly", "game_projection")
    for m in imported_modules:
        assert not any(f in m.lower() for f in forbidden), f"unexpected import: {m}"


def test_adapter_never_touches_gamelog_margin_std():
    import src.pipeline.game_log_import_adapter as mod
    source = inspect.getsource(mod)
    assert "gamelog_margin_std_v15" not in source
    assert "_gamelog_std" not in source


def test_adapter_never_imports_network_libraries():
    import src.pipeline.game_log_import_adapter as mod
    assert not ({"requests", "urllib", "socket", "http"} & _imported_module_roots(mod))


def test_adapter_never_writes_to_teamdump():
    import src.pipeline.game_log_import_adapter as mod
    tree = ast.parse(inspect.getsource(mod))
    called_names = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not ({"to_excel", "to_csv"} & called_names)


def test_this_test_file_never_imports_network_libraries():
    import sys
    assert not ({"requests", "urllib", "socket", "http"} & _imported_module_roots(sys.modules[__name__]))
