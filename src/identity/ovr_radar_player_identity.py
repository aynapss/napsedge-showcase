"""
OVR/radar player-identity adapter (Phase 2B, batch 6 -- final player subsystem).

ACTIVE / SHADOWED / LOCAL-CLOSURE INVENTORY (objective #1)

- `build_composite_radar_v9` (cell 207) -- top-level, active, unshadowed
  (only one `def build_composite_radar_v9` found in the notebook). Called
  by `compute_player_ovr_only`.
- `compute_player_ovr_only` (cell 208) -- top-level, active, unshadowed.
  Called by `build_team_ovr_leaderboard_cached` (used by `apply_injury_penalty`)
  and by the confirmed-dead postgame cluster (batch 5).
- **`_find_espn_row`** (cell 207, defined *inside* `build_composite_radar_v9`,
  around its own line 85) -- a **local closure**, not independently
  importable or testable outside the 4700-line host function. Ported
  verbatim below as `find_espn_radar_row_v15` specifically so it becomes
  inspectable/testable in isolation.
- `apply_injury_penalty` (cell 290), `get_radar_roster_lastnames` (cell 293),
  `get_meaningful_lastnames` / `injury_sensitivity_check` (cell 294) --
  all top-level, active, unshadowed.

PLAYER KEY USED BY EACH LOOKUP (objective #2) -- FOUR INDEPENDENT
NORMALIZATION AUTHORITIES CONFIRMED, NONE OF THEM THE CANONICAL CORE
(objectives #3, #10)

1. **AllStats row lookup** (`build_composite_radar_v9`'s own `df_team[col]`
   match, `col = "Players"`): exact-string match on the *raw* column,
   falling back to case-insensitive -- no suffix stripping, no accent
   normalization, no punctuation stripping. Verbatim below as
   `resolve_radar_allstats_row_v15`.
2. **ESPN row lookup** (`_find_espn_row`): its own **four-tier** ladder --
   (1) exact `Name ==`, (2) case-insensitive, (3) an inline
   `re.sub(r"[^a-z0-9 ]", "", s.lower().strip())` normalizer (punctuation
   stripped, but **no accent/diacritic handling** -- unlike the canonical
   core's `normalize_player_name_v15`), (4) first-initial + last-name
   (period-stripped) -- the loosest tier, and the one most likely to
   collide.
3. **Injury last-name lookup** (`normalize_covers_player`, already migrated
   in batch 3): suffix-stripped correctly -- "Daryl Simmons II" -> "simmons".
4. **Radar/injury OVR gate** (`normalize_allstats_player` / the `__last_name`
   column set by `load_team_allstats_df`, cell 211): the already-documented
   buggy function (`name.lower().split()[-1]`) -- "Daryl Simmons II" ->
   "ii", not "simmons".

None of the four calls `canonical_player_key_v15` or `normalize_player_name_v15`
(the Phase 2B batch-1 canonical core). `_find_espn_row`'s tiers 3-4 are
confirmed as a genuinely new (5th, counting `normalize_covers_player`)
normalization authority introduced specifically by this function --
objective #10's answer is yes.

CONFIRMED CROSS-SYSTEM MISMATCH: `apply_injury_penalty` DOES REACH THE
BUGGY PATH (objective #9, correcting batch 1's docstring)

Batch 1's `player_identity.py` docstring stated "no confirmed callers found
this session" for `normalize_allstats_player`. This batch found real,
load-bearing callers by tracing actual call chains:
`apply_injury_penalty` (cell 290) directly runs
``ovr_df["__last"] = ovr_df["Player"].apply(normalize_allstats_player)``,
and both `get_radar_roster_lastnames` (cell 293) and `load_team_allstats_df`'s
own `__last_name` column (cell 211) build their last-name sets the same
buggy way. Since `injury_sensitivity_check`'s injured-player keys come from
`normalize_covers_player` (correct, suffix-stripped -- e.g. "simmons"), and
`apply_injury_penalty`'s OVR-side keys come from the buggy function (e.g.
"ii" for the same real player), **these two key spaces cannot align for
any suffixed player** -- a suffixed player's true availability is silently
never matched (`.map(inactive_map).fillna(1.0)` always falls through to
the "available" default for them), and `get_radar_roster_lastnames`'s
roster-gate similarly never recognizes a suffixed player by their real
surname. This is a **confirmed identity defect**, not a hypothetical --
this batch's docstring correction and tests demonstrate it precisely. Per
the explicit boundary ("do not change any ... injury-impact ... calculation"),
this is documented, not fixed.

CONFIRMED, REAL (NOT HYPOTHETICAL) SAME-NAME COLLISION RISK (objectives
#4, #5, #6)

`TeamDump/ESPN/espn_full_player_stats.csv` (the real fixture used
throughout this migration, 3121 rows) contains a `Team` column (short
codes, e.g. "BYU", "ECU") that `_find_espn_row` **never uses** to scope its
match -- season is also entirely absent from this source (single-season
snapshot, consistent with the ESPN batch's finding). Checked directly
against this real fixture: **13 real duplicate `Name` values** exist
across different teams (e.g. "Josh Smith", "Josh Reed", "Nate Johnson").
For any of these, `_find_espn_row`'s tier-1 exact match returns
`df_espn[...].iloc[0]` with **no uniqueness check and no team filter** --
whichever row happens to sort first silently wins, regardless of which
team the caller actually asked about. This is a confirmed, live defect
with real data, not a theoretical edge case. The AllStats row lookup in
the same host function has the identical shape of gap: `matches.iloc[0]`
is taken with no check that `matches` has exactly one row -- two players
sharing an exact "Players" string on one team+season roster would silently
resolve to whichever sorts first.

WHETHER MISSING ROWS SILENTLY SELECT ANOTHER PLAYER (objective #7)

No -- confirmed clean on this specific point. All four of `_find_espn_row`'s
tiers require an actual string match; a genuinely absent player produces
`None` cleanly (checked via `if _espn_row_for_player is not None:` at each
call site), not a fallback guess. The risk is not "missing produces a
wrong pick" but "present-and-ambiguous produces a wrong pick" (see above).
Injury lookup (already migrated, batch 3) was already designed this way on
purpose (`resolve_lastname_only_injury_candidate` returns `unconfirmed` for
a unique-but-weak match, `not_found` for a genuinely absent one).

SUFFIX / ACCENT / PUNCTUATION BEHAVIOR DIFFERENCES (objective #8)

AllStats row lookup: no normalization of any kind beyond case-folding --
strict, so a legitimate accent/punctuation variant fails closed (raises
`ValueError`) rather than silently guessing; safe but unforgiving.
`_find_espn_row` tier 3: strips punctuation (so "Amar'e" vs "Amare" would
match) but has **no accent/diacritic stripping** (unlike the canonical
core's NFD-based `normalize_player_name_v15`) -- a confirmed gap distinct
from the AllStats side. Suffixes are not stripped anywhere in
`_find_espn_row` except implicitly by tier 4's last-token comparison
(which is also its most collision-prone tier).

WHAT WAS BUILT

This module never introduces a new normalizer: it reuses
`canonical_player_key_v15`/`build_player_identity`/`match_stat_row_to_roster`
from the Phase 2B core, and `resolve_espn_player_identity` from the batch-2
ESPN adapter, for all real matching decisions. The v15 closures above are
ported verbatim for compatibility/testing only, clearly labeled as
reproducing known-risky legacy behavior (never called from the canonical
path). The canonical replacements return `ambiguous`/`not_found` instead
of an unchecked `.iloc[0]`, and support optional ESPN team-code scoping
using data (the real `Team` column) the legacy function already had
available but never used.
"""
from __future__ import annotations

import re

import pandas as pd

from src.identity.contracts import PlayerIdentity, TeamIdentity, UnresolvedIdentity
from src.identity.espn_player_identity import resolve_espn_player_identity
from src.identity.player_identity import (
    build_player_identity,
    canonical_player_key_v15,
    match_stat_row_to_roster,
)
from src.identity.team_identity import resolve_team_identity
from src.logging_config import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Legacy compatibility wrappers -- byte-faithful to v15, reproduce known-risky
# behavior on purpose (for compatibility testing only -- never the canonical
# path).
# ---------------------------------------------------------------------------


def find_espn_radar_row_v15(df_espn: pd.DataFrame | None, player_name: str) -> pd.Series | None:
    """
    Verbatim port of the local closure `_find_espn_row` (inside notebook
    cell 207's `build_composite_radar_v9`). Four-tier ladder: exact name,
    case-insensitive, punctuation-stripped (no accent handling), then
    first-initial + last-name -- the last tier is the documented collision
    risk (see module docstring). Preserved exactly, including the
    unchecked ``.iloc[0]`` on every tier and the total absence of team/
    season scoping, so this batch's tests can demonstrate the real
    collision against the real ESPN fixture before comparing against the
    canonical replacement below.
    """
    if df_espn is None or "Name" not in df_espn.columns:
        return None

    m = df_espn[df_espn["Name"] == player_name]
    if not m.empty:
        return m.iloc[0]

    m = df_espn[df_espn["Name"].str.lower() == player_name.lower()]
    if not m.empty:
        return m.iloc[0]

    def _norm(s):
        return re.sub(r"[^a-z0-9 ]", "", s.lower().strip())

    pnorm = _norm(player_name)
    m = df_espn[df_espn["Name"].apply(_norm) == pnorm]
    if not m.empty:
        return m.iloc[0]

    parts = player_name.split()
    if len(parts) >= 2:
        first_init = parts[0][0].lower()
        last = parts[-1].lower().replace(".", "")
        m = df_espn[df_espn["Name"].apply(
            lambda n: (n.split()[-1].lower().replace(".", "") == last
                       and n[0].lower() == first_init) if n else False
        )]
        if not m.empty:
            return m.iloc[0]

    return None


def resolve_radar_allstats_row_v15(df_team: pd.DataFrame, player_name: str) -> pd.Series:
    """
    Verbatim port of `build_composite_radar_v9`'s own AllStats player-row
    lookup: exact-then-case-insensitive match on the raw "Players" column,
    ``.iloc[0]`` with no ambiguity check, ``ValueError`` if nothing matches
    at all (the one part of this lookup that already fails loudly rather
    than guessing).
    """
    name = str(player_name).strip()
    col = "Players"

    matches = df_team[df_team[col].astype(str).str.strip() == name]
    if matches.empty:
        matches = df_team[df_team[col].astype(str).str.strip().str.lower() == name.lower()]

    if matches.empty:
        raise ValueError(f"Player not found in df_team: {player_name}")

    return matches.iloc[0].copy()


# ---------------------------------------------------------------------------
# New canonical path
# ---------------------------------------------------------------------------


def find_ambiguous_radar_allstats_rows(df_team: pd.DataFrame, player_name: str) -> int:
    """
    Counts how many rows `resolve_radar_allstats_row_v15` would have
    silently collapsed via ``.iloc[0]`` -- 0 (not found), 1 (safe), or 2+
    (a real ambiguity the legacy function does not detect). Reuses the
    exact same exact/case-insensitive matching rule as the v15 function,
    just counts instead of indexing, so this is a diagnostic over the same
    rule, not a new one.
    """
    name = str(player_name).strip()
    col = "Players"

    matches = df_team[df_team[col].astype(str).str.strip() == name]
    if matches.empty:
        matches = df_team[df_team[col].astype(str).str.strip().str.lower() == name.lower()]
    return len(matches)


def resolve_radar_stat_row_identity(
    player_name: str,
    team_query: str,
    season: int,
    *,
    roster_identities: list[PlayerIdentity] | None = None,
    source: str = "ovr_radar_allstats",
    source_player_id: str | None = None,
) -> PlayerIdentity | UnresolvedIdentity:
    """
    Resolve the AllStats-row player this batch's radar/OVR functions look
    up, to a canonical `PlayerIdentity` -- same shape as batch 4/5's
    `resolve_boxscore_player_identity`/`resolve_postgame_player_identity`:
    team routed through the canonical registry, optional roster-scoped
    full-name matching (delegates to `match_stat_row_to_roster`, unique
    match only, ambiguous/not-found surfaced structurally), falling back
    to a standalone team+season-scoped identity.

    This is the safe replacement for `build_composite_radar_v9`'s own
    unchecked `.iloc[0]` row lookup -- callers wanting to know *whether*
    the AllStats-row match itself was ambiguous should also call
    `find_ambiguous_radar_allstats_rows` against the raw `df_team`, since
    that lookup and this identity resolution are against the same
    underlying "Players" column by construction.
    """
    if not player_name or not str(player_name).strip():
        return UnresolvedIdentity(query=str(player_name), source=source, reason="not_found")

    team_result = resolve_team_identity(team_query, source=source)
    if not isinstance(team_result, TeamIdentity):
        return team_result

    if roster_identities:
        scoped_roster = [
            p for p in roster_identities
            if p.team_id == team_result.team_id and p.season == season
        ]
        match = match_stat_row_to_roster(player_name, scoped_roster, source=source)
        if isinstance(match, PlayerIdentity):
            return match
        if match.reason == "ambiguous":
            return match

    return build_player_identity(
        player_name, team_result.team_id, season, source=source, source_player_id=source_player_id
    )


def find_espn_stat_row(
    espn_df: pd.DataFrame,
    player_name: str,
    *,
    espn_team_code: str | None = None,
    team_column: str = "Team",
    name_column: str = "Name",
) -> tuple[pd.Series | None, str]:
    """
    Safe replacement for `_find_espn_row`'s row-finding, keyed by the
    **same canonical `canonical_player_key_v15`** the rest of this
    migration already uses -- not a new normalizer. Optionally scoped by
    ESPN's own ``Team`` column (short codes like "BYU"/"ECU", present in
    the real fixture but never used by the legacy closure) when the caller
    supplies ``espn_team_code`` -- exact match, no fuzzy team resolution
    here (the caller is responsible for knowing the ESPN code; this
    function does not attempt to map it to/from the canonical registry).

    Returns ``(row_or_None, method)`` where ``method`` is one of
    ``"unique"``, ``"ambiguous"``, ``"not_found"`` -- never silently picks
    among multiple candidates the way `find_espn_radar_row_v15` does.
    """
    if espn_df is None or name_column not in espn_df.columns:
        return None, "not_found"

    key = canonical_player_key_v15(player_name)
    candidates = espn_df[espn_df[name_column].apply(canonical_player_key_v15) == key]

    if espn_team_code is not None and team_column in espn_df.columns:
        team_scoped = candidates[candidates[team_column] == espn_team_code]
        if not team_scoped.empty:
            candidates = team_scoped

    if candidates.empty:
        return None, "not_found"
    if len(candidates) > 1:
        log.warning(
            "ambiguous ESPN radar row for %r: %d rows share key %r (team_code=%r)",
            player_name, len(candidates), key, espn_team_code,
        )
        return None, "ambiguous"
    return candidates.iloc[0], "unique"


def resolve_radar_espn_identity(
    player_name: str,
    team_query: str,
    season: int,
    *,
    espn_athlete_id: str | None = None,
    source: str = "espn_radar",
) -> PlayerIdentity | UnresolvedIdentity:
    """
    Thin wrapper reusing batch 2's `resolve_espn_player_identity` for the
    identity side of ESPN radar enrichment -- team+season scoped via the
    canonical registry, prefers a real `espn_athlete_id` when supplied. No
    new identity-resolution logic; this exists purely so a caller working
    in the OVR/radar context does not need to import from a differently-
    named module for the identity half of the lookup.
    """
    return resolve_espn_player_identity(
        player_name, team_query, season, espn_athlete_id=espn_athlete_id, source=source
    )


def resolve_ovr_radar_player_context(
    player_name: str,
    team_query: str,
    season: int,
    *,
    df_team: pd.DataFrame | None = None,
    espn_df: pd.DataFrame | None = None,
    espn_team_code: str | None = None,
    roster_identities: list[PlayerIdentity] | None = None,
    source: str = "ovr_radar",
) -> dict:
    """
    Single entry point bundling everything one `build_composite_radar_v9`
    call site would need identity-wise: the canonical AllStats-row
    identity, whether that AllStats row lookup was itself ambiguous, and
    the canonical ESPN identity + matched ESPN stat row (or an explicit
    "missing"/"ambiguous" marker -- never a silent drop or a silent wrong
    pick). All fields are always present, even when a piece is
    unavailable, per "preserve missing ESPN ... records explicitly."
    """
    allstats_identity = resolve_radar_stat_row_identity(
        player_name, team_query, season, roster_identities=roster_identities, source=source
    )
    allstats_ambiguous_count = (
        find_ambiguous_radar_allstats_rows(df_team, player_name) if df_team is not None else None
    )

    espn_identity = resolve_radar_espn_identity(player_name, team_query, season, source=f"{source}_espn")

    espn_row, espn_method = (
        find_espn_stat_row(espn_df, player_name, espn_team_code=espn_team_code)
        if espn_df is not None
        else (None, "not_found")
    )

    return {
        "allstats_identity": allstats_identity,
        "allstats_row_ambiguous": (allstats_ambiguous_count or 0) > 1,
        "espn_identity": espn_identity,
        "espn_row": espn_row,
        "espn_method": espn_method,
    }


# ---------------------------------------------------------------------------
# Conflict detection -- never resolved silently
# ---------------------------------------------------------------------------


def find_duplicate_espn_radar_names(espn_df: pd.DataFrame, *, name_column: str = "Name") -> dict[str, int]:
    """
    Canonical-key-level duplicate-name census over a real ESPN radar
    DataFrame -- quantifies the collision risk `_find_espn_row` never
    checks for. Against the real fixture
    (`TeamDump/ESPN/espn_full_player_stats.csv`), this confirms 13 real
    duplicate names exist (see module docstring) -- this function is what
    a caller would run to know, for any given ESPN snapshot, exactly which
    names are unsafe to resolve without team scoping.
    """
    if espn_df is None or name_column not in espn_df.columns:
        return {}
    counts = espn_df[name_column].dropna().apply(canonical_player_key_v15).value_counts()
    dupes = counts[counts > 1]
    # Map back to a representative original display name per key for readability.
    key_to_name: dict[str, str] = {}
    for raw in espn_df[name_column].dropna():
        key_to_name.setdefault(canonical_player_key_v15(raw), raw)
    return {key_to_name[key]: int(count) for key, count in dupes.items()}
