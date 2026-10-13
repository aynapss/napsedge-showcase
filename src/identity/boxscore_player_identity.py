"""
Box-score player-identity adapter (Phase 2B, batch 4).

THREE PARALLEL BOX-SCORE SYSTEMS FOUND (not fully disentangled -- documented
as a finding, not resolved by executing the notebook)

1. **`resolve_boxscore_url` / `extract_players_from_boxscore`** (cells
   249-250, unshadowed) -- the system explicitly named in this batch's
   investigation scope. Scrapes a Sports-Reference box-score HTML page
   directly, parses each team's table via ``pd.read_html``, and returns
   ``{team_slug: {"team_name": ..., "players": [raw name strings]}}``.
2. **`fetch_boxscore_html` / `_extract_sr_table` / `parse_boxscore` /
   `build_boxscore_url`** (cells 643-644) -- a more elaborate, apparently
   newer SR box-score parser (custom table extraction handling header
   rows/column mismatches, real retry/backoff via
   `_safe_get_with_backoff`) targeting the same Sports-Reference pages.
   Whether this superseded system 1 or runs alongside it was not
   determined without executing the notebook -- out of scope to resolve
   this batch.
3. **`fetch_cbbpy_game_boxscore` / `get_cbbpy_boxscore_cached`** (cell
   318) -- wraps the third-party `cbbpy` (ESPN-based) library. **Its own
   comment says "Additive only -- no downstream engine changes yet"** --
   confirmed, by the notebook's own words, not yet wired into the active
   pipeline. `_build_boxscore_profile` (cell 647) consumes a `boxscore`
   *object* with `.home_team`/`.away_team`/`.score_line` attributes,
   which matches cbbpy's structured return shape, not system 1's plain
   dict -- suggesting cells 643-647 are actually built around cbbpy
   output, with the raw-HTML parser (system 2) as a possible fallback.
   This module treats player identity resolution as agnostic to which of
   these three actually produced the raw names, by design -- see below.

SOURCE IDENTIFIER FINDINGS

`extract_players_from_boxscore` uses ``pd.read_html(str(table))[0]`` to
parse the player table -- this **structurally discards** any `<a href>`
player-profile link Sports-Reference's real markup would contain, not
merely by an extraction oversight (as with the roster-page and ESPN-stats
cases in earlier batches) but because `pd.read_html` converts the whole
table to plain text cells, with no path to recover a link from its output.
Confirmed: no player ID, URL, or slug survives this parsing path. Whether
cbbpy's structured payload (system 3) carries an ESPN athlete id was not
verified this session (would require inspecting the third-party library's
schema, out of scope) -- treated honestly as unconfirmed, not assumed.

FIELDS PROVIDED

`extract_players_from_boxscore` provides only a raw player name and a
team (via `normalize_sr_team_name`, a sixth distinct team-slug convention
found in this codebase -- not reused here, routed through the canonical
registry instead like every other adapter). No season, no explicit
game/source identifier (though the box-score URL itself is one, just not
threaded into this function), no game date, no explicit opponent field
(implicit: the *other* key in the always-exactly-2-team result dict). Home
and away are **not labeled** in the function's own return value -- the
caller must already know which team is home/away (exactly how
`_build_boxscore_profile` calls it: `boxscore.away_team`/`.home_team` are
resolved as two independently-labeled halves, never merged). This
adapter's entry point mirrors that convention explicitly, rather than
guessing home/away from an unlabeled pair.

NON-PLAYER ROW EXCLUSION (already present in v15, preserved verbatim)

`extract_players_from_boxscore` already filters out rows matching
``"Reserves|Team Totals|Did Not Play"`` (case-insensitive) before
returning its player list -- confirmed, not something this batch adds.
Reproduced verbatim below and additionally *enforced* again inside this
module's own canonical resolution function, so a caller feeding rows from
a *different* box-score source (e.g. a future cbbpy-based path) still gets
the same protection.

POSTGAME CONSUMERS (identified, not implemented -- per this batch's scope)

`build_postgame_player_record` (cell 256), `compute_postgame_player_ovr`
(cell 255), and `build_postgame_report`/`print_postgame_report` (referenced
in `_build_boxscore_profile`, cell 647, not yet located precisely) are the
downstream consumers of matched box-score player rows. None inspected
beyond confirming their role as postgame consumers -- "identify... but do
not implement postgame analysis," per this batch's instruction.
"""
from __future__ import annotations

import re

from src.identity.contracts import PlayerIdentity, TeamIdentity, UnresolvedIdentity
from src.identity.player_identity import build_player_identity, match_stat_row_to_roster
from src.identity.team_identity import resolve_team_identity
from src.logging_config import get_logger

log = get_logger(__name__)

#: Verbatim from notebook cell 250's `extract_players_from_boxscore`.
_NON_PLAYER_ROW_RX = re.compile(r"Reserves|Team Totals|Did Not Play", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Legacy compatibility wrapper -- byte-faithful non-player-row filter
# ---------------------------------------------------------------------------


def filter_boxscore_player_rows_v15(raw_names: list[str]) -> list[str]:
    """
    Verbatim port of the row-exclusion logic already inside notebook cell
    250's `extract_players_from_boxscore` (the
    ``~df[player_col].str.contains("Reserves|Team Totals|Did Not Play",
    case=False, na=False)`` line), lifted out so it's usable/testable on a
    plain list of already-extracted name strings without a live HTTP
    fetch or a DataFrame. Not a new behavior -- the real function already
    does exactly this filtering before returning its player list.
    """
    return [name for name in raw_names if name and not _NON_PLAYER_ROW_RX.search(name)]


# ---------------------------------------------------------------------------
# New canonical path
# ---------------------------------------------------------------------------


def is_non_player_boxscore_row(raw_name: str) -> bool:
    """True for a team-totals/header/DNP row that must never enter identity matching."""
    return bool(raw_name) and bool(_NON_PLAYER_ROW_RX.search(raw_name))


def resolve_boxscore_player_identity(
    raw_name: str,
    team_query: str,
    season: int,
    *,
    roster_identities: list[PlayerIdentity] | None = None,
    source: str = "boxscore_sr",
    source_player_id: str | None = None,
) -> PlayerIdentity | UnresolvedIdentity:
    """
    Resolve one box-score row to a canonical :class:`PlayerIdentity`.

    Non-player rows (team totals, "Reserves"/"Did Not Play" section
    headers) are rejected before any matching is attempted, regardless of
    which upstream box-score system produced ``raw_name`` -- this
    enforcement does not depend on the caller having already filtered
    with :func:`filter_boxscore_player_rows_v15`.

    ``team_query`` is routed through the Phase 2A canonical registry, per
    every other adapter in this migration.

    If ``roster_identities`` (the team+season-scoped output of the
    previous batch's `resolve_roster_player_identities`, or an ESPN
    roster, per "reuse existing Sports-Reference and ESPN player
    identities") is supplied, matching delegates entirely to
    `player_identity.match_stat_row_to_roster` -- the same full-name,
    suffix-aware, ambiguity-safe logic already used for AllStats and ESPN
    rows. Box-score names are full names (unlike injury reports' last-name-
    only shape), so this is the correct strictness level here, not the
    stricter injury-adapter rule.

    Without a roster (or with no match in it), falls back to
    `build_player_identity` -- a stable, team+season-scoped (or
    ``source_player_id``-based, when a caller has one) box-score-only
    identity, never a fabricated cross-source link.
    """
    if is_non_player_boxscore_row(raw_name):
        log.info("rejected non-player box-score row: %r", raw_name)
        return UnresolvedIdentity(query=str(raw_name), source=source, reason="not_found")

    if not raw_name or not raw_name.strip():
        return UnresolvedIdentity(query=str(raw_name), source=source, reason="not_found")

    team_result = resolve_team_identity(team_query, source=source)
    if not isinstance(team_result, TeamIdentity):
        return team_result

    if roster_identities:
        scoped_roster = [
            p for p in roster_identities
            if p.team_id == team_result.team_id and p.season == season
        ]
        match = match_stat_row_to_roster(raw_name, scoped_roster, source=source)
        if isinstance(match, PlayerIdentity):
            return match
        if match.reason == "ambiguous":
            return match
        # "not_found" against the roster -- fall through to a standalone
        # box-score identity rather than erroring; the player may simply
        # be missing from the supplied roster snapshot.

    return build_player_identity(
        raw_name, team_result.team_id, season, source=source, source_player_id=source_player_id
    )


def resolve_boxscore_game_players(
    *,
    home_team_query: str,
    away_team_query: str,
    season: int,
    home_player_names: list[str],
    away_player_names: list[str],
    home_roster: list[PlayerIdentity] | None = None,
    away_roster: list[PlayerIdentity] | None = None,
    source: str = "boxscore_sr",
) -> dict:
    """
    Resolve a full game's box score, home and away sides kept explicitly
    and permanently separate -- "do not match players across opposing
    teams" is satisfied by construction: each side is resolved against
    its own team and its own (optional) roster, never the other's.

    Mirrors the real calling convention found in `_build_boxscore_profile`
    (cell 647), which independently resolves ``boxscore.away_team`` and
    ``boxscore.home_team`` rather than inferring which is which from an
    unlabeled pair -- this function requires the same explicit labeling
    from its caller, deliberately not guessing.

    Does **not** construct or return a canonical ``game_id`` -- per this
    batch's instruction to preserve game context "without prematurely
    implementing the final canonical game_id." Returns team results and
    per-player resolutions for each side; a caller with a real game
    identifier (a box-score URL, a future cbbpy game id, etc.) attaches it
    itself.
    """
    def _resolve_side(team_query, player_names, roster):
        team_result = resolve_team_identity(team_query, source=source)
        players = [
            resolve_boxscore_player_identity(
                name, team_query, season, roster_identities=roster, source=source
            )
            for name in player_names
        ]
        return {"team": team_result, "players": players}

    return {
        "home": _resolve_side(home_team_query, home_player_names, home_roster),
        "away": _resolve_side(away_team_query, away_player_names, away_roster),
    }


# ---------------------------------------------------------------------------
# Conflict detection -- never resolved silently
# ---------------------------------------------------------------------------


def find_duplicate_boxscore_rows(raw_names: list[str]) -> dict[str, int]:
    """
    Detects a raw name appearing more than once in one team's box-score
    row list (`extract_players_from_boxscore` returns a plain list, not a
    set, so a repeated row -- a real, if rare, HTML/parsing artifact --
    would survive into the list unchanged unless caught here).
    """
    counts: dict[str, int] = {}
    for name in raw_names:
        if is_non_player_boxscore_row(name):
            continue
        counts[name] = counts.get(name, 0) + 1
    return {name: count for name, count in counts.items() if count > 1}


def find_boxscore_id_conflicts(identities: list[PlayerIdentity]) -> list[dict]:
    """
    Detects the same source id appearing under more than one distinct
    display name for the *same* (team_id, season) -- a genuine conflict,
    as opposed to the same id appearing under different (team_id, season)
    pairs, which is a legitimate transfer/multi-game appearance, not
    flagged. Generic over whatever ``source`` label the identities carry
    (unlike the ESPN batch's detector, which is hardcoded to "espn"),
    since box-score rows could in principle carry any source's id.
    """
    by_scope: dict[tuple[str, str, int, int], list[PlayerIdentity]] = {}
    for pid in identities:
        for source_name, source_identity in pid.sources.items():
            key = (source_name, source_identity.raw_value, pid.team_id, pid.season)
            by_scope.setdefault(key, []).append(pid)

    conflicts = []
    for (source_name, source_id, team_id, season), group in by_scope.items():
        distinct_names = {p.display_name for p in group}
        if len(distinct_names) > 1:
            conflicts.append({
                "source": source_name,
                "source_id": source_id,
                "team_id": team_id,
                "season": season,
                "display_names": sorted(distinct_names),
            })
    return conflicts
