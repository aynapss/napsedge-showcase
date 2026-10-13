"""
Canonical player-identity core (Phase 2B, batch 1).

INVENTORY (objective #1-#10, see docs/migration_state.md for the full writeup)

Active, unshadowed notebook functions: `normalize_allstats_player` (cell 45),
`canonical_player_key` (cell 92), `normalize_player_name` +
`harmonize_player_names` (cell 93), `extract_player_slugs_from_roster`
(cell 91), `build_all_stats` (cell 115), `scrape_team_season` (cell 98).

Key findings:
- `normalize_allstats_player` (``name.lower().split()[-1]``) is a real bug
  trap: applied to a suffixed display name like "Daryl Simmons II" it
  returns ``"ii"``, not ``"simmons"``. No confirmed callers found this
  session (distinct from -- and not to be confused with --
  `canonical_player_key`, which correctly strips suffixes first). Ported
  verbatim below for reference/compatibility only.
- `canonical_player_key` is the real, actively-used, suffix-agnostic
  matching key: lowercase, strip suffix (Jr/Sr/II/III/IV/V), strip
  punctuation, collapse spaces.
- `normalize_player_name` is the display-facing normalizer: strips accents
  (NFD decomposition + combining-mark removal, plus a latin-1/utf-8
  mojibake-repair attempt), collapses double-spaces, and standardizes
  suffix punctuation (" Jr" -> " Jr.", " SR" -> " Sr.", etc.) -- it
  **preserves** suffixes; only `canonical_player_key` removes them.
  Answers objective #8: both preserved (display) and removed (matching
  key) depending on which function is used, by design.
- `harmonize_player_names(team_slug, dfs)` applies both, then a small
  **hardcoded, team+key-scoped patch table** (2 entries found: Daryl
  Simmons -> "Daryl Simmons II" at St. Bonaventure; Melvin Council ->
  "Melvin Council Jr." at Kansas) for known missing-suffix cases. Note it
  takes `team_slug` but **not** `season` -- season scope is available at
  its call site (`scrape_team_season`) but not threaded into this
  function itself.
- `build_all_stats` merges every per-table DataFrame (PerGame/Totals/
  Per40/Per100/Advanced) onto the base Roster table via
  ``pd.merge(how="left", on="PlayerKey")`` -- **solely by the normalized
  key**, no team/season disambiguation needed at that point since all
  inputs are already scoped to one team+season by the caller. Answers
  objective #6 conclusively, and exposes a real risk objective #9 asks
  about: if two different players on the *same* roster share a
  suffix-stripped `PlayerKey` (e.g. two players with the same name),
  `pd.merge` would silently produce duplicated/cross-joined rows rather
  than erroring -- not observed in the one real roster fixture checked
  this session (St. Bonaventure, 15/15 unique keys), but a real,
  structural gap this core's :func:`match_stat_row_to_roster` closes.
- `extract_player_slugs_from_roster` **does** capture real, authoritative
  Sports-Reference player slugs (from `<a href="/cbb/players/{slug}.html">`
  anchors in the roster table) -- objective #4's answer is "yes, when
  scraping a live roster page." But that `slug_map` is **never persisted**
  into AllStats: `scrape_team_season` only uses it when a hardcoded flag
  (`SCRAPE_PLAYER_GAMELOGS`, currently `False`) is on, and only in-memory
  for gamelog fetching, never written to the saved workbook. Confirmed
  against a real fixture (`TeamDump/CBB/st-bonaventure/..._AllStats.xlsx`,
  219 columns): **no source-ID/URL/slug column exists anywhere in
  AllStats** -- objective #5's answer is "display name + normalized
  matching key only, no source identifier." This is the central design
  constraint for the stable-ID strategy below.

STABLE-ID STRATEGY

Because AllStats itself carries no source ID, and because there is no
existing player registry anywhere in this codebase to resolve *against*
(unlike team identity, which had `slug_utils.TEAM_REGISTRY` as ground
truth), this core does not attempt a Team-identity-style "resolve an
unknown string against a known universe" lookup. Instead it *constructs*
a `PlayerIdentity` from known-good, already-scoped inputs (a display name
plus the `team_id`/`season` the record came from), preferring a real
Sports-Reference player slug when the caller has one (e.g. from a fresh
roster scrape via :func:`extract_player_slugs_from_roster_v15`):

    player_id = f"sports_reference:{sr_slug}"          -- if an SR slug is available
    player_id = f"team:{team_id}:season:{season}:key:{canonical_key}"  -- otherwise

The fallback form is **explicitly team+season+name scoped, not a claim of
global identity** -- per this batch's instruction, a player lacking a
reliable source ID is represented as source-scoped, never silently
promoted to a cross-team/cross-season canonical identity. Connecting the
same real person across two different (team, season) pairs (a transfer)
is out of scope for this batch: the source data doesn't support it
without the SR slug the notebook currently discards, and this batch's
instructions are explicit that "do not merge transfers ... unless an
authoritative source ID or reviewed crosswalk proves continuity" -- no
such crosswalk exists yet, so no such merge is attempted.
"""
from __future__ import annotations

import re
import unicodedata

from src.identity.contracts import PlayerIdentity, SourceIdentity, UnresolvedIdentity
from src.logging_config import get_logger

log = get_logger(__name__)

_SUFFIX_PATTERN = re.compile(r"\b(jr|sr|ii|iii|iv|v)\.?$", re.IGNORECASE)

_SUFFIX_DISPLAY_MAP = {
    " Jr": " Jr.", " Jr.": " Jr.",
    " SR": " Sr.", " Sr": " Sr.", " Sr.": " Sr.",
    " II": " II", " III": " III", " IV": " IV",
}


# ---------------------------------------------------------------------------
# Legacy compatibility wrappers -- byte-faithful to v15
# ---------------------------------------------------------------------------


def normalize_allstats_player_v15(name: str) -> str:
    """
    Verbatim port of notebook cell 45's ``normalize_allstats_player``.
    Preserved for reference/compatibility only -- no confirmed callers
    found this session, and it has the suffix bug documented in this
    module's docstring (``"Daryl Simmons II".lower().split()[-1] == "ii"``,
    not ``"simmons"``). New code should use :func:`canonical_player_key_v15`
    instead, which strips the suffix before taking the surname.
    """
    return name.lower().split()[-1]


def canonical_player_key_v15(name: str) -> str:
    """
    Verbatim port of notebook cell 92's ``canonical_player_key`` -- the
    real, actively-used suffix-agnostic matching key `build_all_stats`
    merges on.
    """
    if not isinstance(name, str):
        return ""

    name = name.strip().lower()
    name = _SUFFIX_PATTERN.sub("", name)
    name = re.sub(r"[^\w\s]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def normalize_player_name_v15(name: str) -> str:
    """
    Verbatim port of notebook cell 93's ``normalize_player_name`` -- the
    display-facing normalizer. Strips accents/diacritics and a latin-1/
    utf-8 mojibake artifact, standardizes suffix punctuation, but
    (unlike :func:`canonical_player_key_v15`) preserves the suffix itself.
    """
    if not isinstance(name, str):
        return name
    try:
        name = name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass

    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")

    name = name.strip()
    while "  " in name:
        name = name.replace("  ", " ")

    for raw, norm in _SUFFIX_DISPLAY_MAP.items():
        if name.endswith(raw):
            name = name[: -len(raw)].rstrip() + norm
            break

    return name


# ---------------------------------------------------------------------------
# New canonical core
# ---------------------------------------------------------------------------


def build_player_identity(
    display_name: str,
    team_id: int,
    season: int,
    *,
    source: str = "sports_reference",
    source_player_id: str | None = None,
) -> PlayerIdentity:
    """
    Construct a team+season-scoped :class:`PlayerIdentity`.

    This is a *constructor*, not a lookup -- there is no pre-existing
    player registry anywhere in this codebase to resolve an unknown name
    against (unlike team identity's ``slug_utils.TEAM_REGISTRY``). Callers
    supply a display name plus the team/season it was scraped from; this
    function normalizes the name, computes the suffix-agnostic matching
    key, and picks a stable ``player_id`` per the strategy documented in
    this module's docstring -- preferring ``source_player_id`` (e.g. a
    real Sports-Reference player slug) when the caller has one, falling
    back to an explicitly team+season+name-scoped id otherwise (never a
    silently-assumed global identity).
    """
    normalized_display = normalize_player_name_v15(display_name)
    key = canonical_player_key_v15(display_name)

    sources: dict[str, SourceIdentity] = {}
    if source_player_id:
        sources[source] = SourceIdentity(source, source_player_id, source_player_id)
        player_id = f"{source}:{source_player_id}"
    else:
        player_id = f"team:{team_id}:season:{season}:key:{key}"

    return PlayerIdentity(
        player_id=player_id,
        display_name=normalized_display,
        team_id=team_id,
        season=season,
        sources=sources,
    )


def find_duplicate_player_keys(display_names: list[str]) -> dict[str, list[str]]:
    """
    Diagnostic: given a roster's raw display names, return
    ``{canonical_key: [original names]}`` for every key shared by more
    than one name -- i.e. exactly the case `build_all_stats`'s
    ``pd.merge(..., on="PlayerKey")`` would silently cross-join rather
    than error on. Empty dict means the roster has no colliding keys.
    """
    by_key: dict[str, list[str]] = {}
    for name in display_names:
        key = canonical_player_key_v15(name)
        by_key.setdefault(key, []).append(name)
    return {key: names for key, names in by_key.items() if len(names) > 1}


def match_stat_row_to_roster(
    stat_player_name: str,
    roster_identities: list[PlayerIdentity],
    *,
    source: str = "roster_match",
) -> PlayerIdentity | UnresolvedIdentity:
    """
    Match one stat-table row's player name to a known roster's
    :class:`PlayerIdentity` list -- the safe replacement for
    `build_all_stats`'s bare ``pd.merge(on="PlayerKey")``.

    ``roster_identities`` is assumed already scoped to a single
    team+season (that scoping happens by construction: it's whatever list
    the caller built for one roster, never a cross-team/cross-season
    pool) -- this function does not itself reach across team or season
    boundaries, satisfying "reject cross-team name-only matches" and
    "scope name-only matching by team_id and season."

    Returns the single matching identity, or a structured
    :class:`UnresolvedIdentity` with ``reason="ambiguous"`` (more than one
    roster entry shares this stat row's key -- e.g. two same-surname
    players) or ``reason="not_found"`` (no roster entry shares it) --
    never a silent first-match pick.
    """
    key = canonical_player_key_v15(stat_player_name)
    matches = [p for p in roster_identities if canonical_player_key_v15(p.display_name) == key]

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        log.warning(
            "ambiguous stat-row match for %r: %d roster entries share key %r (%s)",
            stat_player_name, len(matches), key, [p.display_name for p in matches],
        )
        return UnresolvedIdentity(
            query=stat_player_name, source=source, reason="ambiguous", candidates=list(matches)
        )
    return UnresolvedIdentity(query=stat_player_name, source=source, reason="not_found")
