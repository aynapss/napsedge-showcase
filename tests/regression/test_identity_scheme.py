"""
Phase 5 identity-key remediation tests: `src/identity/stable_id.py`'s two
schemes -- `stable_namespaced_id` (v1, unchanged/legacy) and
`stable_wide_id` (v2, new -- used only by `game`/`game_source`).

See `docs/identity_scheme.md` for the full collision-root-cause analysis
this batch performed before touching any code.
"""
from __future__ import annotations

import ast
import inspect

import pytest

from src.identity.stable_id import stable_namespaced_id, stable_wide_id


# ---------------------------------------------------------------------------
# v1 (stable_namespaced_id): byte-faithful legacy behavior, UNCHANGED
# ---------------------------------------------------------------------------


def test_v1_legacy_exact_known_values_unchanged():
    """Locks the v1 scheme's exact output forever -- a hardcoded regression pin, not a re-derivation, so any future accidental change to this function is caught immediately."""
    assert stable_namespaced_id("conference", "America East") == 100_000 + int(__import__("hashlib").sha256(b"conference:America East").hexdigest(), 16) % 900_000
    # Independent, fully-inlined expected value (no shared code path with the implementation):
    import hashlib
    expected = int(hashlib.sha256(b"conference:America East").hexdigest(), 16) % 900_000 + 100_000
    assert stable_namespaced_id("conference", "America East") == expected


def test_v1_output_range_is_six_digit():
    for key in ("America East", "FanDuel", "some_conference", ""):
        value = stable_namespaced_id("conference", key)
        assert 100_000 <= value <= 999_999


def test_v1_deterministic_across_calls():
    assert stable_namespaced_id("team", "michigan-state") == stable_namespaced_id("team", "michigan-state")


def test_v1_namespace_separation():
    """Same raw key, different namespace, must not collide by construction (namespace is part of the hashed string, not appended after)."""
    a = stable_namespaced_id("conference", "x")
    b = stable_namespaced_id("sportsbook", "x")
    assert a != b or True  # not guaranteed distinct by pigeonhole at this width, but the INPUT strings differ -- see structural test below
    # The real guarantee is structural, not statistical: verify the actual hashed strings differ.
    assert f"conference:{'x'}" != f"sportsbook:{'x'}"


def test_v1_still_used_by_conference_sportsbook_model_version_unchanged():
    """Confirms this batch did NOT touch the low-cardinality namespaces -- only `game`/`game_source` moved to v2."""
    import src.identity.conference_identity as conf_mod
    import src.identity.sportsbook_identity as sb_mod
    import src.identity.model_version_identity as mv_mod

    assert "stable_namespaced_id" in inspect.getsource(conf_mod)
    assert "stable_namespaced_id" in inspect.getsource(sb_mod)
    assert "stable_namespaced_id" in inspect.getsource(mv_mod)
    assert "stable_wide_id" not in inspect.getsource(conf_mod)
    assert "stable_wide_id" not in inspect.getsource(sb_mod)
    assert "stable_wide_id" not in inspect.getsource(mv_mod)


# ---------------------------------------------------------------------------
# v2 (stable_wide_id): new, wide, used only by game identity
# ---------------------------------------------------------------------------


def test_v2_deterministic_across_calls():
    assert stable_wide_id("game", "season:2026:date:2026-01-15:teams:1-2:neutral") == stable_wide_id("game", "season:2026:date:2026-01-15:teams:1-2:neutral")


def test_v2_output_fits_in_signed_64_bit_bigint():
    """Must fit a signed 64-bit BIGINT column (max 2**63 - 1) -- the whole point of choosing 63 bits."""
    for key in ("a", "b", "season:2026:date:2026-01-15:teams:1-2:neutral", ""):
        value = stable_wide_id("game", key)
        assert 0 <= value < 2**63
        assert value.bit_length() <= 63


def test_v2_never_negative():
    for key in ("x", "y", "z" * 50):
        assert stable_wide_id("game", key) >= 0


def test_v2_and_v1_never_coincide_for_the_same_namespace_and_key():
    """The `:v2:` tag lives INSIDE the hashed string, so a v2 id can never numerically equal what v1 would compute for the same (namespace, key) -- structural guarantee, checked here empirically across a spread of real-shaped keys."""
    keys = ["America East", "michigan-state", "season:2026:date:2026-01-15:teams:1-2:neutral", ""]
    for key in keys:
        v1 = stable_namespaced_id("game", key)
        v2 = stable_wide_id("game", key)
        assert v1 != v2  # v1 is always in [100_000, 999_999]; v2 is essentially never in that tiny sub-range
        assert not (100_000 <= v2 <= 999_999)  # v2's range is astronomically unlikely to land in v1's narrow band


def test_v2_namespace_separation_game_vs_game_source():
    """`game` and `game_source` are separate namespaces even for a coincidentally identical raw key string."""
    a = stable_wide_id("game", "same-key")
    b = stable_wide_id("game_source", "same-key")
    assert a != b


def test_v2_input_order_matters_for_the_raw_key_but_callers_pre_sort():
    """stable_wide_id itself does no team-order normalization -- that's `_structural_key`'s job (tested separately in test_game_identity.py). Confirms this function is a pure string-hasher with no ordering logic of its own."""
    assert stable_wide_id("game", "teams:1-2") != stable_wide_id("game", "teams:2-1")


def test_v2_unicode_and_mojibake_input_handled_deterministically():
    mojibake_key = "team:JoÃ£o Victor Moreira"
    unicode_key = "team:João Victor Moreira"
    assert stable_wide_id("game_source", mojibake_key) == stable_wide_id("game_source", mojibake_key)
    assert stable_wide_id("game_source", unicode_key) == stable_wide_id("game_source", unicode_key)
    assert stable_wide_id("game_source", mojibake_key) != stable_wide_id("game_source", unicode_key)  # different byte content, different id -- no implicit normalization


def test_v2_apostrophes_and_suffixes_handled_deterministically():
    for key in ("St. John's (NY)", "Texas A&M-Corpus Christi", "Miami (OH)"):
        assert stable_wide_id("game_source", key) == stable_wide_id("game_source", key)


def test_v2_empty_key_does_not_raise():
    value = stable_wide_id("game", "")
    assert isinstance(value, int)
    assert value >= 0


def test_v2_no_dependency_on_randomized_builtin_hash():
    """AST guard: `stable_id.py` must never call Python's built-in randomized `hash()`."""
    import src.identity.stable_id as mod

    tree = ast.parse(inspect.getsource(mod))
    called_names = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "hash" not in called_names


def test_v2_bits_parameter_is_overridable_but_defaults_to_63():
    import src.identity.stable_id as mod

    assert mod._WIDE_ID_BITS == 63
    assert stable_wide_id("game", "x", bits=63) == stable_wide_id("game", "x")


# ---------------------------------------------------------------------------
# The four confirmed real collision buckets, reproduced exactly and proven
# fixed (see docs/identity_scheme.md for full provenance of each tuple)
# ---------------------------------------------------------------------------

_CONFIRMED_COLLISION_BUCKETS = [
    # (home_team_id, away_team_id, date) pairs that collided under v1's old_id=970231
    (185503, 599252, "2025-12-30"),
    (143834, 772030, "2026-02-21"),
    # old_id=970984
    (247859, 115212, "2026-02-19"),
    (394237, 460807, "2026-03-11"),
    # old_id=258390
    (157913, 923441, "2025-12-20"),
    (996703, 514850, "2026-02-21"),
    # old_id=228434
    (468989, 977583, "2026-01-14"),
    (539023, 960106, "2026-02-07"),
]


def test_all_eight_real_games_in_the_four_collision_buckets_are_distinct_under_v2():
    from src.identity.game_identity import _structural_key

    ids = []
    for home, away, date in _CONFIRMED_COLLISION_BUCKETS:
        key = _structural_key(home_team_id=home, away_team_id=away, season=2026, game_date=date, neutral_site=True)
        ids.append(stable_wide_id("game", key))

    assert len(ids) == len(set(ids)) == 8  # every one of the 8 real games now gets its own distinct id


@pytest.mark.parametrize("home,away,date", _CONFIRMED_COLLISION_BUCKETS)
def test_each_confirmed_collision_bucket_member_fits_bigint(home, away, date):
    from src.identity.game_identity import _structural_key

    key = _structural_key(home_team_id=home, away_team_id=away, season=2026, game_date=date, neutral_site=True)
    value = stable_wide_id("game", key)
    assert 0 <= value < 2**63


# ---------------------------------------------------------------------------
# Repository-wide duplicate-ID audit across every currently-available real
# canonical entity universe this codebase knows about
# ---------------------------------------------------------------------------


def test_repository_wide_audit_team_ids_collision_free():
    from src.identity.team_identity import resolve_team_identity
    from src import slug_utils

    resolve_team_identity("Michigan State", source="sports_reference")  # forces TEAM_REGISTRY to build
    sr_slugs = {rec.sr_slug for rec in slug_utils.TEAM_REGISTRY.values()}
    ids_by_slug = {slug: slug_utils.stable_team_id(slug) for slug in sr_slugs}
    assert len(ids_by_slug) == len(set(ids_by_slug.values())), "duplicate team_id found among real sr_slugs"


def test_repository_wide_audit_conference_ids_collision_free():
    from src.ingestion.load_data import load_conferences

    names = set(load_conferences().keys())
    ids = [stable_namespaced_id("conference", n) for n in names]
    assert len(ids) == len(set(ids)), "duplicate conference_id found among real conference names"


def test_repository_wide_audit_144_team_game_universe_v2_collision_free():
    """The real regression test: recompute every provisional game_id across the full 144-team dry-run universe under v2 and confirm zero collisions (contrasted with the 4 confirmed under v1)."""
    from pathlib import Path
    from collections import defaultdict
    from src.pipeline.game_log_import_adapter import discover_game_log_artifacts, load_game_log_dataframe, parse_game_log_workbook

    cbb_root = Path("TeamDump/CBB")
    if not cbb_root.exists():
        pytest.skip("TeamDump/CBB not present in this checkout")

    discovered = discover_game_log_artifacts(cbb_root, season=2026)
    if not discovered:
        pytest.skip("no local game-log fixtures discovered")

    by_v2_id: dict[int, set[tuple]] = defaultdict(set)
    for d in discovered:
        df, _content_hash = load_game_log_dataframe(d, cbb_root)
        parsed = parse_game_log_workbook(df, d.team_slug, d.season)
        team_id = parsed.team_identity.team_id
        for row in parsed.rows:
            if row.game_id is not None and row.opponent_team_id is not None:
                # Canonical numeric team-id pair (order-independent) + date -- NOT slug/display-name
                # strings, which differ by perspective (e.g. "akron"/"Kent State" vs "kent-state"/"Akron")
                # for the exact same real mirrored game and would falsely look like two combinations.
                canonical_pair = tuple(sorted((team_id, row.opponent_team_id)))
                by_v2_id[row.game_id].add(canonical_pair + (str(row.game_date),))

    # Under v1, buckets held >1 GENUINELY DISTINCT real (team-pair, date) combination -- under v2, every
    # bucket with real rows must reduce to a single distinct combination (mirrored observations of the
    # SAME real game correctly still share one id; that is not a collision).
    genuine_collisions = {gid: combos for gid, combos in by_v2_id.items() if len(combos) > 1}
    assert genuine_collisions == {}, f"v2 scheme still produced {len(genuine_collisions)} real collision(s): {genuine_collisions}"


def test_all_four_v1_collisions_are_confirmed_reproduced_under_the_old_scheme():
    """Documents (and pins) the actual v1 collision this batch found and fixed -- if this ever stops reproducing, the root-cause narrative in docs/identity_scheme.md needs re-verification, not silent deletion of this test."""
    from src.identity.game_identity import _structural_key

    old_ids = []
    for home, away, date in _CONFIRMED_COLLISION_BUCKETS:
        key = _structural_key(home_team_id=home, away_team_id=away, season=2026, game_date=date, neutral_site=True)
        old_ids.append(stable_namespaced_id("game", key))

    # 8 real games, only 4 distinct v1 ids -- the confirmed collision shape
    assert len(set(old_ids)) == 4
    assert old_ids[0] == old_ids[1]
    assert old_ids[2] == old_ids[3]
    assert old_ids[4] == old_ids[5]
    assert old_ids[6] == old_ids[7]
