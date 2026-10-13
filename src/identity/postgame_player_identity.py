"""
Postgame player-identity adapter (Phase 2B, batch 5).

ACTIVE VS. DEAD POSTGAME CLUSTER (investigation objective #1)

Two non-overlapping postgame clusters exist in the notebook:

1. **`run_postgame_analysis` / `build_postgame_matchup_leaderboard_with_deltas`
   / `build_postgame_player_record` / `compute_postgame_player_ovr`** (cells
   259 / 257 / 256 / 255). **Confirmed broken, not merely unused**:
   `compute_postgame_player_ovr` (cell 255) calls
   ``compute_player_ovr_only(player_name=player_name, team_name=team_name,
   conference=conference)`` -- but the real `compute_player_ovr_only` (cell
   208) declares ``team_slug`` and ``season`` as required keyword-only
   parameters with no defaults. Calling this chain as written raises
   ``TypeError: missing required keyword-only argument`` every time.
   `run_postgame_analysis` is additionally an interactive ``input()``-driven
   CLI tool, not part of any automated pipeline. Treated as dead legacy
   code: not wrapped, not exercised beyond confirming this defect (a hard
   runtime bug, not an identity-ambiguity finding).
2. **`run_postgame_pipeline` / `build_postgame_report` /
   `compute_game_ovr_from_boxscore` / `print_postgame_report`** (cells 647 /
   645 / 645 / 646), fed by **`parse_boxscore`/`fetch_boxscore_html`/
   `_extract_sr_table`** (cells 643-644 -- box-score "system 2" from the
   batch 4 investigation). This is the **active** cluster: fully wired,
   no missing/broken arguments, and confirmed as the box-score system that
   actually reaches a postgame report. This resolves batch 4's open
   question of whether box-score system 1 or system 2 is authoritative --
   it is system 2, for the postgame path specifically.

WHETHER THE ACTIVE PATH JOINS TO ALLSTATS/ROSTER AT ALL (objectives #3, #6)

`compute_game_ovr_from_boxscore`'s own docstring says "No Z-score blending
(no ESPN lookup data available)" and `build_postgame_report`'s says "No
AllStats scraping required" -- confirmed by reading the code: game-relative
OVR is computed **entirely from the boxscore's own counting stats**,
benchmarked against that same game's median (`_compute_game_benchmarks`),
with **zero join to AllStats, season data, or a roster**. Its only "merge"
(``df.merge(ovr_df, on="Player", how="left")`` in
`compute_game_ovr_from_boxscore`) is a **self-join**: `ovr_df` is built by
iterating the exact same `df` the merge is joining back into, so both sides
carry identical `Player` strings from the same single-source DataFrame --
this cannot produce a same-name collision or a silent drop, by construction,
regardless of how common a player's name is. So the active cluster has *no*
existing cross-source player-identity bug to fix. `compute_player_ovr_only`
(cell 208, called only by the confirmed-dead cluster above) is the function
that *does* look a player up by name against season/AllStats-derived radar
data (`build_composite_radar_v9`) -- that is OVR/radar territory, explicitly
out of scope this batch ("do not begin the general OVR/radar identity
adapter").

WHAT THIS ADAPTER ACTUALLY DOES, THEN

Given the active pipeline has no existing player-matching bug, this
adapter's job is forward-looking: make the box-score rows inside a
`PostgameReport`'s `away_df`/`home_df` (or the raw name lists upstream of
them) **attachable** to a canonical `PlayerIdentity`, additively, so a
caller who *wants* to cross-reference a postgame performance against a
roster/AllStats/OVR record can do so safely -- reusing batch 4's box-score
adapter for all actual matching, never introducing a second normalization
authority.

NON-PLAYER ROW EXCLUSION GAP (objective #11, found by evidence)

`_extract_sr_table` (cell 644, the active parser) filters rows against
notebook cell 642's ``SR_NON_PLAYER_ROWS`` frozenset:
``{"", "Starters", "Reserves", "Team Totals", "Did Not Play",
"Did Not Dress", "Not With Team", "Player Suspended"}`` -- a **superset** of
batch 4's regex (``Reserves|Team Totals|Did Not Play``, reverse-engineered
from the older, dead `extract_players_from_boxscore`, cell 250). In
practice `parse_boxscore` already filters this set out before a
`BoxscoreResult` ever reaches a caller, so this gap causes no live bug --
but a caller feeding *unfiltered* raw names (e.g. from a different upstream
source, or a future refactor) through batch 4's narrower filter alone would
let "Starters"/"Did Not Dress"/"Not With Team"/"Player Suspended" rows
through as if they were players. This module closes that gap with a
superset check (`is_postgame_non_player_row`) without modifying batch 4's
own function.

TEAM-RESOLUTION FINDING, DOCUMENTED NOT FIXED (out of scope: Phase 2A is
closed, and this batch's scope is player identity)

`run_postgame_pipeline`'s local closure `_resolve_slug_from_name` (cell
647) is a **previously unknown, seventh team-name-resolution path**: exact
override -> exact match against `all_schools_df` -> **unrestricted
``difflib.get_close_matches(team_name, names, n=1, cutoff=0.72)`` fuzzy
match**, auto-selected with no ambiguity check. This is a local closure
inside `run_postgame_pipeline`, invisible to a manifest scan keyed on
top-level function names, so it was not covered by any of Phase 2A's six
team-identity adapters. It resolves the *team*, not the player, feeding
`resolve_team_profile`/`attach_net_rank` for team-level context only --
out of scope to fix here per this batch's player-identity focus and the
"do not begin general OVR/radar" boundary, but recorded in
`docs/migration_state.md` as a Phase 2A-adjacent gap for a future batch.
This adapter itself never reproduces that fuzzy path: it always resolves
team context through the canonical `resolve_team_identity` registry.

STABLE-ID / MATCHING STRATEGY

Identical to batch 4's box-score adapter, reused directly: prefer a real
source player ID when a caller has one; otherwise fall back to
`build_player_identity`'s explicit team+season+name-key scope. Roster-
scoped full-name matching (when a roster is supplied) delegates entirely to
`player_identity.match_stat_row_to_roster` -- unique match only, ambiguous
or not-found surfaced structurally, never guessed. No unrestricted fuzzy
matching anywhere in this module.
"""
from __future__ import annotations

import pandas as pd

from src.identity.boxscore_player_identity import (
    is_non_player_boxscore_row,
    resolve_boxscore_player_identity,
)
from src.identity.contracts import PlayerIdentity, UnresolvedIdentity
from src.identity.player_identity import canonical_player_key_v15
from src.identity.team_identity import resolve_team_identity
from src.logging_config import get_logger

log = get_logger(__name__)

#: Verbatim from notebook cell 642's `SR_NON_PLAYER_ROWS` -- the exclusion
#: set used by `_extract_sr_table`/`parse_boxscore`, the parser feeding the
#: active postgame pipeline (cells 643-647). Broader than batch 4's
#: regex-based filter (see module docstring).
SR_NON_PLAYER_ROWS_V15: frozenset[str] = frozenset({
    "", "Starters", "Reserves", "Team Totals",
    "Did Not Play", "Did Not Dress",
    "Not With Team", "Player Suspended",
})


# ---------------------------------------------------------------------------
# Legacy compatibility wrappers -- byte-faithful to v15
# ---------------------------------------------------------------------------


def is_sr_boxscore_non_player_row_v15(raw_name: str) -> bool:
    """Verbatim membership check against cell 642's `SR_NON_PLAYER_ROWS`."""
    return raw_name in SR_NON_PLAYER_ROWS_V15


def filter_postgame_player_rows_v15(raw_names: list[str]) -> list[str]:
    """
    Verbatim-equivalent port of notebook cell 644's `_extract_sr_table`
    exclusion line (``df[~df["Player"].isin(SR_NON_PLAYER_ROWS)]``) -- the
    filter actually applied by the parser feeding the *active* postgame
    pipeline, as distinct from batch 4's `filter_boxscore_player_rows_v15`
    (the older, confirmed-dead `extract_players_from_boxscore`'s narrower
    regex, cell 250).
    """
    return [name for name in raw_names if name and name not in SR_NON_PLAYER_ROWS_V15]


def build_postgame_game_context(boxscore_url: str, game_date: str) -> dict:
    """
    Preserves source game context (the boxscore URL + game date already
    carried on `BoxscoreResult`/`PostgameReport`) without implementing a
    final canonical `game_id`, per this batch's explicit instruction. A
    future batch that defines a canonical game identifier can derive it
    from these fields; this one does not guess at that format.
    """
    return {"source_url": boxscore_url, "game_date": game_date}


# ---------------------------------------------------------------------------
# New canonical path
# ---------------------------------------------------------------------------


def is_postgame_non_player_row(raw_name: str) -> bool:
    """
    Superset non-player check: rejects a row if *either* box-score system's
    exclusion rule would reject it -- batch 4's regex (the older,
    confirmed-dead `extract_players_from_boxscore`, cell 250) OR this
    module's verbatim `SR_NON_PLAYER_ROWS_V15` (the active `parse_boxscore`,
    cells 643-644). A caller that already filtered upstream (both v15
    parsers filter their own set before returning) gets a redundant but
    harmless second check; a caller passing unfiltered raw names gets full
    coverage regardless of which system produced them.
    """
    if not raw_name:
        return True
    return is_non_player_boxscore_row(raw_name) or is_sr_boxscore_non_player_row_v15(raw_name)


def resolve_postgame_player_identity(
    raw_name: str,
    team_query: str,
    season: int,
    *,
    roster_identities: list[PlayerIdentity] | None = None,
    source: str = "postgame_sr",
    source_player_id: str | None = None,
) -> PlayerIdentity | UnresolvedIdentity:
    """
    Resolve one postgame box-score row to a canonical `PlayerIdentity`.

    Delegates all real matching to batch 4's
    `resolve_boxscore_player_identity` (team resolution via the canonical
    registry, optional roster-scoped full-name matching, stable-ID
    construction) -- no second normalization authority is introduced. The
    only addition is `is_postgame_non_player_row`'s broadened check,
    applied first, so rows the active parser's own `SR_NON_PLAYER_ROWS`
    excludes are rejected even if a caller bypasses `parse_boxscore`'s own
    filtering (defense in depth against the gap documented at module
    level).
    """
    if is_postgame_non_player_row(raw_name):
        log.info("rejected non-player postgame row: %r", raw_name)
        return UnresolvedIdentity(query=str(raw_name), source=source, reason="not_found")

    return resolve_boxscore_player_identity(
        raw_name,
        team_query,
        season,
        roster_identities=roster_identities,
        source=source,
        source_player_id=source_player_id,
    )


def resolve_postgame_game_players(
    *,
    away_team_query: str,
    away_season: int,
    away_player_names: list[str],
    home_team_query: str,
    home_season: int,
    home_player_names: list[str],
    away_roster: list[PlayerIdentity] | None = None,
    home_roster: list[PlayerIdentity] | None = None,
    source: str = "postgame_sr",
) -> dict:
    """
    Resolve both sides of one postgame boxscore. Home and away are kept
    permanently separate by construction -- each side is resolved against
    only its own team, season, and (optional) roster, never the other's --
    satisfying "do not merge opposing-team players" structurally, the same
    way batch 4's `resolve_boxscore_game_players` does.

    Unlike batch 4's version (one shared ``season``), this accepts
    *independent* ``away_season``/``home_season`` -- mirroring
    `build_postgame_report`'s own `away_season`/`home_season` parameters,
    which really can differ (e.g. a resolved team profile falling back to
    a different season per side). Each side's matching is scoped to its
    own season, never the other's.

    Does not construct a canonical ``game_id`` -- see
    `build_postgame_game_context`.
    """
    def _resolve_side(team_query, season, player_names, roster):
        team_result = resolve_team_identity(team_query, source=source)
        players = [
            resolve_postgame_player_identity(
                name, team_query, season, roster_identities=roster, source=source
            )
            for name in player_names
        ]
        return {"team": team_result, "season": season, "players": players}

    return {
        "away": _resolve_side(away_team_query, away_season, away_player_names, away_roster),
        "home": _resolve_side(home_team_query, home_season, home_player_names, home_roster),
    }


def attach_postgame_identity_columns(
    df: pd.DataFrame,
    identities: list[PlayerIdentity | UnresolvedIdentity],
    *,
    player_column: str = "Player",
) -> pd.DataFrame:
    """
    Return a **copy** of ``df`` with three columns appended --
    ``player_id``, ``identity_source``, ``identity_method`` -- computed
    row-for-row from ``identities`` (must be the same length and row order
    as ``df[player_column]``; a caller resolving one identity per
    ``df[player_column]`` value, in order, via
    `resolve_postgame_player_identity`/`resolve_postgame_game_players`
    satisfies this by construction). Raises ``ValueError`` on a length
    mismatch rather than silently zipping mismatched rows.

    Purely additive: every existing column, value, dtype, and row order is
    left untouched -- this is what "attaching identity metadata
    additively" (the OVR-boundary instruction) means in practice, and is
    exactly what this batch's parity tests verify against
    `compute_game_ovr_from_boxscore`'s real output shape.
    """
    if len(identities) != len(df):
        raise ValueError(
            f"identities length ({len(identities)}) does not match df row "
            f"count ({len(df)}) -- resolve exactly one identity per "
            f"df[{player_column!r}] row, in row order, before attaching."
        )

    out = df.copy()
    player_ids: list[str | None] = []
    id_sources: list[str | None] = []
    methods: list[str] = []

    for identity in identities:
        if isinstance(identity, PlayerIdentity):
            player_ids.append(identity.player_id)
            id_sources.append(next(iter(identity.sources), None))
            methods.append("source_id" if identity.sources else "team_season_key")
        else:
            player_ids.append(None)
            id_sources.append(None)
            methods.append(f"unresolved:{identity.reason}")

    out["player_id"] = player_ids
    out["identity_source"] = id_sources
    out["identity_method"] = methods
    return out


# ---------------------------------------------------------------------------
# Conflict detection -- never resolved silently
# ---------------------------------------------------------------------------


def find_duplicate_postgame_rows(raw_names: list[str]) -> dict[str, int]:
    """
    Detects a raw player name appearing more than once in one side's
    box-score row list -- same shape as batch 4's
    `find_duplicate_boxscore_rows`, using the broadened
    `is_postgame_non_player_row` check instead of the narrower one.
    """
    counts: dict[str, int] = {}
    for name in raw_names:
        if is_postgame_non_player_row(name):
            continue
        counts[name] = counts.get(name, 0) + 1
    return {name: count for name, count in counts.items() if count > 1}


def check_no_cross_team_leakage(
    away_identities: list[PlayerIdentity | UnresolvedIdentity],
    home_identities: list[PlayerIdentity | UnresolvedIdentity],
) -> list[dict]:
    """
    Defensive verification that "away and home never merge" actually held,
    rather than only asserting it holds by construction: flags any
    ``player_id`` string produced on *both* sides of the same game (which
    would only happen if a caller mistakenly ran the same roster/team
    query for both sides, or supplied the same explicit ``source_player_id``
    for two different real people). Returns a list of conflict dicts;
    empty means no leakage found.
    """
    conflicts = []
    away_ids = {p.player_id: p for p in away_identities if isinstance(p, PlayerIdentity)}
    home_ids = {p.player_id: p for p in home_identities if isinstance(p, PlayerIdentity)}

    for pid in set(away_ids) & set(home_ids):
        conflicts.append({
            "player_id": pid,
            "away_team_id": away_ids[pid].team_id,
            "home_team_id": home_ids[pid].team_id,
            "away_display_name": away_ids[pid].display_name,
            "home_display_name": home_ids[pid].display_name,
        })
    return conflicts


def find_missing_between_boxscore_and_allstats(
    boxscore_identities: list[PlayerIdentity],
    allstats_identities: list[PlayerIdentity],
) -> dict[str, list[str]]:
    """
    Set-difference by `canonical_player_key_v15`. Both lists are assumed
    already scoped by the caller to the same team+season (this function
    does not itself check ``team_id``/``season``, the same convention as
    `match_stat_row_to_roster`'s ``roster_identities`` parameter).

    Returns display names (not identity objects) under
    ``"in_boxscore_not_allstats"`` and ``"in_allstats_not_boxscore"`` --
    meant for human review ("preserve missing-player records for review
    instead of silently dropping them"), not further automated matching.
    """
    box_by_key = {
        canonical_player_key_v15(p.display_name): p.display_name for p in boxscore_identities
    }
    all_by_key = {
        canonical_player_key_v15(p.display_name): p.display_name for p in allstats_identities
    }

    return {
        "in_boxscore_not_allstats": [
            name for key, name in box_by_key.items() if key not in all_by_key
        ],
        "in_allstats_not_boxscore": [
            name for key, name in all_by_key.items() if key not in box_by_key
        ],
    }
