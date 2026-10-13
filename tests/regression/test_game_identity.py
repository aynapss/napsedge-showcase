"""Phase 2, Gate 2 tests: game identity (highest-risk identifier). No live requests."""
import pytest

from src.identity.contracts import GameIdentity, UnresolvedIdentity
from src.identity.game_identity import (
    build_game_identity_from_source,
    build_provisional_game_identity,
    find_game_id_conflicts,
    find_home_away_mismatch,
)


# ---------------------------------------------------------------------------
# Provisional identity: structural key over canonical team_id, never names
# ---------------------------------------------------------------------------

def test_provisional_game_identity_basic():
    result = build_provisional_game_identity(
        home_team_id=905884, away_team_id=319204, season=2026, game_date="2026-01-15"
    )
    assert isinstance(result, GameIdentity)
    assert result.is_provisional is True
    assert result.home_team_id == 905884
    assert result.away_team_id == 319204


def test_deterministic_repeated_generation():
    ids = {
        build_provisional_game_identity(
            home_team_id=905884, away_team_id=319204, season=2026, game_date="2026-01-15"
        ).game_id
        for _ in range(3)
    }
    assert len(ids) == 1


def test_same_matchup_different_dates_do_not_share_id():
    a = build_provisional_game_identity(home_team_id=1, away_team_id=2, season=2026, game_date="2026-01-15")
    b = build_provisional_game_identity(home_team_id=1, away_team_id=2, season=2026, game_date="2026-02-20")
    assert a.game_id != b.game_id


def test_different_matchups_same_date_do_not_collide():
    a = build_provisional_game_identity(home_team_id=1, away_team_id=2, season=2026, game_date="2026-01-15")
    b = build_provisional_game_identity(home_team_id=3, away_team_id=4, season=2026, game_date="2026-01-15")
    assert a.game_id != b.game_id


def test_home_away_reversal_produces_different_id_for_non_neutral_game():
    """A real home/away reversal for a non-neutral game IS a different fact, not silently normalized."""
    a = build_provisional_game_identity(home_team_id=1, away_team_id=2, season=2026, game_date="2026-01-15")
    b = build_provisional_game_identity(home_team_id=2, away_team_id=1, season=2026, game_date="2026-01-15")
    assert a.game_id != b.game_id


def test_neutral_site_games_are_symmetric_to_home_away_label_swap():
    a = build_provisional_game_identity(
        home_team_id=1, away_team_id=2, season=2026, game_date="2026-03-20", neutral_site=True
    )
    b = build_provisional_game_identity(
        home_team_id=2, away_team_id=1, season=2026, game_date="2026-03-20", neutral_site=True
    )
    assert a.game_id == b.game_id


def test_neutral_vs_non_neutral_same_teams_same_date_are_distinct():
    a = build_provisional_game_identity(home_team_id=1, away_team_id=2, season=2026, game_date="2026-01-15", neutral_site=False)
    b = build_provisional_game_identity(home_team_id=1, away_team_id=2, season=2026, game_date="2026-01-15", neutral_site=True)
    assert a.game_id != b.game_id


def test_requires_explicit_season_no_silent_inference():
    result = build_provisional_game_identity(home_team_id=1, away_team_id=2, season=None, game_date="2026-01-15")
    assert not isinstance(result, GameIdentity)
    assert result.reason == "not_found"


def test_missing_game_date_is_not_found():
    result = build_provisional_game_identity(home_team_id=1, away_team_id=2, season=2026, game_date="")
    assert not isinstance(result, GameIdentity)
    assert result.reason == "not_found"


def test_rescheduled_game_gets_a_new_provisional_id():
    """A postponed/rescheduled game (new date) is treated as a distinct provisional id, not merged with the original."""
    original = build_provisional_game_identity(home_team_id=1, away_team_id=2, season=2026, game_date="2026-01-15")
    rescheduled = build_provisional_game_identity(home_team_id=1, away_team_id=2, season=2026, game_date="2026-01-18")
    assert original.game_id != rescheduled.game_id


# ---------------------------------------------------------------------------
# Source-authoritative identity: prefers real source id, never a slug/name
# ---------------------------------------------------------------------------

def test_build_from_source_prefers_source_id():
    result = build_game_identity_from_source(
        source="sports_reference", source_game_id="202601150sbu",
        home_team_id=905884, away_team_id=319204, season=2026, game_date="2026-01-15",
    )
    assert isinstance(result, GameIdentity)
    assert result.is_provisional is False
    assert result.sources["sports_reference"].raw_value == "202601150sbu"


def test_build_from_source_missing_source_id_is_not_found():
    result = build_game_identity_from_source(
        source="sports_reference", source_game_id="",
        home_team_id=1, away_team_id=2, season=2026, game_date="2026-01-15",
    )
    assert not isinstance(result, GameIdentity)


def test_source_id_deterministic():
    ids = {
        build_game_identity_from_source(
            source="espn", source_game_id="401600123",
            home_team_id=1, away_team_id=2, season=2026, game_date="2026-01-15",
        ).game_id
        for _ in range(3)
    }
    assert len(ids) == 1


def test_corrected_display_name_or_slug_cannot_change_game_identity():
    """
    Game identity never takes a team display name or slug as input at all
    -- only canonical team_id -- so a display-name/slug correction upstream
    cannot possibly change it, by construction.
    """
    import inspect

    sig = inspect.signature(build_provisional_game_identity)
    assert "team_name" not in sig.parameters
    assert "team_slug" not in sig.parameters
    assert "home_team_id" in sig.parameters
    assert "away_team_id" in sig.parameters


# ---------------------------------------------------------------------------
# Conflict detection: source-id conflicts, home/away mismatches
# ---------------------------------------------------------------------------

def test_find_game_id_conflicts_detects_same_source_id_different_context():
    g1 = build_game_identity_from_source(
        source="espn", source_game_id="999", home_team_id=1, away_team_id=2, season=2026, game_date="2026-01-15",
    )
    g2 = build_game_identity_from_source(
        source="espn", source_game_id="999", home_team_id=3, away_team_id=4, season=2026, game_date="2026-01-15",
    )
    conflicts = find_game_id_conflicts([g1, g2])
    assert len(conflicts) == 1
    assert conflicts[0]["source_game_id"] == "999"
    assert len(conflicts[0]["conflicting_contexts"]) == 2


def test_find_game_id_conflicts_clean_for_consistent_source_id():
    g1 = build_game_identity_from_source(
        source="espn", source_game_id="999", home_team_id=1, away_team_id=2, season=2026, game_date="2026-01-15",
    )
    g2 = build_game_identity_from_source(
        source="espn", source_game_id="999", home_team_id=1, away_team_id=2, season=2026, game_date="2026-01-15",
    )
    assert find_game_id_conflicts([g1, g2]) == []


def test_find_home_away_mismatch_detects_reversal():
    a = build_provisional_game_identity(home_team_id=1, away_team_id=2, season=2026, game_date="2026-01-15")
    b = build_provisional_game_identity(home_team_id=2, away_team_id=1, season=2026, game_date="2026-01-15")
    assert find_home_away_mismatch(a, b) is True


def test_find_home_away_mismatch_false_for_neutral_site():
    a = build_provisional_game_identity(home_team_id=1, away_team_id=2, season=2026, game_date="2026-03-20", neutral_site=True)
    b = build_provisional_game_identity(home_team_id=2, away_team_id=1, season=2026, game_date="2026-03-20", neutral_site=True)
    assert find_home_away_mismatch(a, b) is False


def test_find_home_away_mismatch_false_for_different_dates():
    a = build_provisional_game_identity(home_team_id=1, away_team_id=2, season=2026, game_date="2026-01-15")
    b = build_provisional_game_identity(home_team_id=2, away_team_id=1, season=2026, game_date="2026-02-01")
    assert find_home_away_mismatch(a, b) is False


# ---------------------------------------------------------------------------
# Serialization / reconstruction
# ---------------------------------------------------------------------------

def test_game_identity_serializes_and_reconstructs():
    from dataclasses import asdict

    original = build_provisional_game_identity(home_team_id=1, away_team_id=2, season=2026, game_date="2026-01-15")
    data = asdict(original)
    reconstructed = GameIdentity(**data)
    assert reconstructed.game_id == original.game_id
    assert reconstructed == original


# ---------------------------------------------------------------------------
# Phase 5 identity-key remediation: scheme v2 (stable_wide_id), season
# separation, and provisional-vs-confirmed namespace independence
# ---------------------------------------------------------------------------


def test_provisional_game_id_uses_scheme_v2_wide_id():
    result = build_provisional_game_identity(home_team_id=1, away_team_id=2, season=2026, game_date="2026-01-15")
    assert 0 <= result.game_id < 2**63
    assert not (100_000 <= result.game_id <= 999_999)  # not the old, narrow v1 range


def test_confirmed_source_game_id_uses_scheme_v2_wide_id():
    result = build_game_identity_from_source(
        source="sports_reference", source_game_id="202601150sbu",
        home_team_id=1, away_team_id=2, season=2026, game_date="2026-01-15",
    )
    assert 0 <= result.game_id < 2**63
    assert not (100_000 <= result.game_id <= 999_999)


def test_same_teams_different_season_do_not_share_id():
    a = build_provisional_game_identity(home_team_id=1, away_team_id=2, season=2026, game_date="2026-01-15")
    b = build_provisional_game_identity(home_team_id=1, away_team_id=2, season=2027, game_date="2026-01-15")
    assert a.game_id != b.game_id


def test_provisional_and_confirmed_never_share_a_namespace():
    """`game` (provisional) and `game_source` (confirmed) are distinct namespaces by construction -- a provisional and a confirmed identity for conceptually 'the same' real game get different ids, never silently unified. Reconciling them is find_game_id_conflicts's job at the application layer, never automatic here."""
    provisional = build_provisional_game_identity(home_team_id=1, away_team_id=2, season=2026, game_date="2026-01-15")
    confirmed = build_game_identity_from_source(
        source="sports_reference", source_game_id="some-real-id",
        home_team_id=1, away_team_id=2, season=2026, game_date="2026-01-15",
    )
    assert provisional.game_id != confirmed.game_id
    assert provisional.is_provisional is True
    assert confirmed.is_provisional is False


def test_confirmed_four_collision_buckets_now_distinct_end_to_end():
    """The exact 4 real Phase 5B2 dry-run collisions, reproduced through the public builder function (not just the internal structural key), proven fixed."""
    buckets = [
        (185503, 599252, "2025-12-30"), (143834, 772030, "2026-02-21"),
        (247859, 115212, "2026-02-19"), (394237, 460807, "2026-03-11"),
        (157913, 923441, "2025-12-20"), (996703, 514850, "2026-02-21"),
        (468989, 977583, "2026-01-14"), (539023, 960106, "2026-02-07"),
    ]
    ids = [
        build_provisional_game_identity(home_team_id=h, away_team_id=a, season=2026, game_date=d, neutral_site=True).game_id
        for h, a, d in buckets
    ]
    assert len(ids) == len(set(ids)) == 8


def test_no_schedule_boxscore_odds_or_postgame_calculation_touched():
    import src.identity.game_identity as mod

    forbidden = {"compute_ovr", "build_postgame_report", "compute_game_ovr_from_boxscore", "get_live_ncaab_odds"}
    assert not (forbidden & set(dir(mod)))
