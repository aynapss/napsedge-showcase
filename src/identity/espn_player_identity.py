"""
ESPN player-identity adapter (Phase 2B, batch 2).

ACTIVE DEFINITIONS AND CALL SITES

`scrape_espn_players()` (cell 126, unshadowed) is the real, active ESPN
player-data source: a Selenium scrape of
``https://www.espn.com/mens-college-basketball/stats/player``, cached at
`TeamDump/ESPN/espn_full_player_stats.csv` (and a derived
`..._enriched.csv` with computed benchmark columns). `_attach_espn_ids`
(cell 411) and `_find_espn_row` (cell 207, nested inside the ~193KB
`build_composite_radar_v9` radar-building function) both looked initially
relevant but are **not** this: `_attach_espn_ids` attaches an
``espn_team_id`` to a *team* dict for logo rendering (already covered by
the ESPN/Logo adapter, Phase 2A), and `_find_espn_row` is scoped inside
the radar function this batch explicitly excludes.

ESPN SOURCE-ID FINDING (the central finding of this batch)

**ESPN athlete IDs do not survive ingestion.** `scrape_espn_players`
extracts a player's `<a>` tag via ``name_tag.get_text(strip=True)`` but
never reads ``name_tag["href"]`` -- even though ESPN's real markup puts a
numeric athlete ID right there (``/mens-college-basketball/player/_/id/
{id}/{name-slug}``). Confirmed against the real cached CSVs
(`espn_full_player_stats.csv`, `..._enriched.csv`): columns are
``RK, Name, Team, POS`` + stat columns -- **no ID column in either file**,
and **no season column either** (single current-snapshot only). ``Team``
is ESPN's own short abbreviation ("BYU", "ECU", "ARK"), a third
team-slug-ish convention needing resolution through the Phase 2A canonical
registry, same as every other source adapter in this migration.

The separate ESPN injury-feed path (`src.scraper.scrape_espn_injuries`,
already migrated and verified faithful in the Covers/injury batch) has the
identical gap: it reads ``athlete.get("displayName")``/``fullName`` from
the injury API's JSON but never ``athlete.get("id")``, even though ESPN's
real athlete objects carry one. That function is **not modified or
re-implemented here** -- it's the downstream injury call site this
batch's instructions say to identify, not touch. Player-level injury
matching there is entirely last-name-based (`src.scraper.
normalize_covers_player`), independent of anything in this module.

Because no real source lacks an ID at *both* the scraper and the payload
level examined, this adapter is built to *accept* a real ESPN athlete ID
as an optional input (for a caller who has one -- e.g. from ESPN's REST
API, which does structurally support ``athlete.id``, or a future improved
scraper) while correctly falling back to a team+season+name-scoped
identity for the current real-world case where one isn't available --
exactly the same honest-scoping pattern already used for Sports-Reference
player slugs in the previous batch.
"""
from __future__ import annotations

import re

from src.identity.contracts import PlayerIdentity, SourceIdentity, TeamIdentity, UnresolvedIdentity
from src.identity.player_identity import (
    build_player_identity,
    canonical_player_key_v15,
    match_stat_row_to_roster,
    normalize_player_name_v15,
)
from src.identity.team_identity import resolve_team_identity
from src.logging_config import get_logger

log = get_logger(__name__)

_SUFFIX_RX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\.?$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Legacy-shaped extraction helpers (byte-faithful name/team extraction;
# ID extraction is new, additive, not a v15 behavior)
# ---------------------------------------------------------------------------


def extract_espn_player_row_v15(player_cell) -> tuple[str, str]:
    """
    Byte-faithful reproduction of `scrape_espn_players`'s per-row
    extraction (cell 126): normalized display name + team abbreviation
    from one ``<td>`` player cell. Takes a BeautifulSoup ``Tag`` for the
    player cell (as the notebook's ``cols[1]`` would be), not a full page,
    so it's unit-testable without Selenium.
    """
    name_tag = player_cell.find("a")
    name = normalize_player_name_v15(name_tag.get_text(strip=True)) if name_tag else ""
    team_tag = player_cell.find("span", class_="athleteCell__teamAbbrev")
    team = team_tag.get_text(strip=True) if team_tag else ""
    return name, team


def extract_espn_athlete_id(player_cell) -> str | None:
    """
    NOT a v15 behavior -- the original scraper discards this. Extracts the
    numeric ESPN athlete ID from the same ``<a href>`` v15 already parses
    for its text, when present (ESPN's real markup:
    ``/mens-college-basketball/player/_/id/{id}/{slug}``). Returns
    ``None`` if no id is present, matching what the real cached CSVs show
    (id absent from both the raw and enriched files) rather than
    fabricating one.
    """
    name_tag = player_cell.find("a")
    if not name_tag or not name_tag.get("href"):
        return None
    match = re.search(r"/id/(\d+)", name_tag["href"])
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# New canonical path
# ---------------------------------------------------------------------------


def resolve_espn_player_identity(
    display_name: str,
    team_query: str,
    season: int,
    *,
    espn_athlete_id: str | None = None,
    source: str = "espn",
) -> PlayerIdentity | UnresolvedIdentity:
    """
    Build a canonical :class:`PlayerIdentity` for one ESPN player row.

    ``team_query`` is routed through Phase 2A's `resolve_team_identity` --
    an unresolvable or ambiguous ESPN team abbreviation returns that
    result directly rather than guessing (requirement: "use the canonical
    team registry for ESPN team resolution"). ``season`` is a required
    parameter, not defaulted: the real ESPN player-stats source has no
    season field at all (see module docstring), so silently assuming one
    would be a guess this adapter does not make.

    ``espn_athlete_id`` is preferred as the stable id when supplied
    (real-world today: usually absent, per the module docstring's
    finding) -- this is what keeps the identity stable across a transfer
    (same id, different team_id/season across two calls) rather than
    falling back to a team+season-scoped id, which necessarily changes.
    """
    if not display_name:
        return UnresolvedIdentity(query=str(display_name), source=source, reason="not_found")

    team_result = resolve_team_identity(team_query, source=source)
    if not isinstance(team_result, TeamIdentity):
        return team_result

    return build_player_identity(
        display_name,
        team_result.team_id,
        season,
        source=source,
        source_player_id=espn_athlete_id,
    )


def link_espn_identity_to_roster(
    espn_identity: PlayerIdentity,
    roster_identities: list[PlayerIdentity],
    *,
    source: str = "espn_to_sr_link",
) -> PlayerIdentity | UnresolvedIdentity:
    """
    Attempt to link an ESPN-sourced :class:`PlayerIdentity` to an existing
    Sports-Reference roster identity (the output of the previous batch's
    `resolve_roster_player_identities`), evidence-gated per this batch's
    requirements:

    1. **Scoped**: only roster entries with the *same* ``team_id`` and
       ``season`` as ``espn_identity`` are ever considered -- an unscoped
       name match is never attempted.
    2. **Unique**: if more than one scoped roster entry shares the
       suffix-stripped key, this returns ``ambiguous`` with all candidates
       -- never a first-match guess (delegates to
       `player_identity.match_stat_row_to_roster`, the same safe-merge
       logic used for AllStats).
    3. **Suffix-compatible**: if both names carry a suffix and the
       suffixes *disagree* (e.g. ESPN says "Jr." and the roster says
       "III"), that is treated as a conflict, not a match -- suffix
       stripping is for finding candidates, not for overriding a
       genuine disagreement once one is found.
    4. **No fabricated cross-source identity**: if no scoped roster
       candidate matches at all, the original ``espn_identity`` is
       returned **unchanged** -- per this batch's instruction, an ESPN
       athlete with no Sports-Reference counterpart keeps a stable,
       ESPN-only identity rather than being marked an error.

    On a successful link, returns a **new** `PlayerIdentity` merging both
    sources' `SourceIdentity` entries. Its ``player_id`` prefers (in
    order): a real ESPN athlete id, a real Sports-Reference source id,
    then the team+season+key fallback -- whichever is most stable.
    """
    scoped_roster = [
        p for p in roster_identities
        if p.team_id == espn_identity.team_id and p.season == espn_identity.season
    ]
    if not scoped_roster:
        log.info(
            "no roster in scope (team_id=%d, season=%d) to link ESPN player %r -- keeping ESPN-only identity",
            espn_identity.team_id, espn_identity.season, espn_identity.display_name,
        )
        return espn_identity

    match = match_stat_row_to_roster(espn_identity.display_name, scoped_roster, source=source)

    if isinstance(match, UnresolvedIdentity):
        if match.reason == "not_found":
            log.info(
                "ESPN player %r has no Sports-Reference roster match in scope -- keeping ESPN-only identity",
                espn_identity.display_name,
            )
            return espn_identity
        # "ambiguous": multiple roster candidates share the key -- propagate.
        return match

    # Unique candidate found -- check suffix compatibility before merging.
    espn_suffix = _extract_suffix(espn_identity.display_name)
    roster_suffix = _extract_suffix(match.display_name)
    if espn_suffix and roster_suffix and espn_suffix.lower() != roster_suffix.lower():
        log.warning(
            "suffix conflict linking ESPN %r to roster %r (%r vs %r) -- not merging",
            espn_identity.display_name, match.display_name, espn_suffix, roster_suffix,
        )
        return UnresolvedIdentity(
            query=espn_identity.display_name,
            source=source,
            reason="ambiguous",
            candidates=[espn_identity, match],
        )

    merged_sources = {**match.sources, **espn_identity.sources}
    espn_source = espn_identity.sources.get("espn")
    sr_source = match.sources.get("sports_reference")
    if espn_source:
        player_id = f"espn:{espn_source.raw_value}"
    elif sr_source:
        player_id = f"sports_reference:{sr_source.raw_value}"
    else:
        player_id = match.player_id

    log.info(
        "linked ESPN player %r to Sports-Reference roster entry %r (team_id=%d, season=%d)",
        espn_identity.display_name, match.display_name, match.team_id, match.season,
    )
    return PlayerIdentity(
        player_id=player_id,
        display_name=match.display_name,
        team_id=match.team_id,
        season=match.season,
        sources=merged_sources,
    )


def _extract_suffix(name: str) -> str | None:
    m = _SUFFIX_RX.search(name.strip())
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Source-ID conflict detection -- never resolved silently
# ---------------------------------------------------------------------------


def find_espn_id_reused_within_same_team_season(
    espn_identities: list[PlayerIdentity],
) -> list[dict]:
    """
    Detects a real data-quality conflict: the same ESPN athlete id
    appearing more than once for the *same* (team_id, season) pair, e.g.
    under two different display names. This is distinct from -- and must
    not be confused with -- a legitimate transfer, where the same id
    appears under *different* (team_id, season) pairs by design (that's
    exactly what "keep ESPN identities stable across a transfer" means).
    """
    by_id_and_scope: dict[tuple[str, int, int], list[PlayerIdentity]] = {}
    for pid in espn_identities:
        espn_source = pid.sources.get("espn")
        if not espn_source:
            continue
        key = (espn_source.raw_value, pid.team_id, pid.season)
        by_id_and_scope.setdefault(key, []).append(pid)

    conflicts = []
    for (espn_id, team_id, season), group in by_id_and_scope.items():
        distinct_names = {p.display_name for p in group}
        if len(distinct_names) > 1:
            conflicts.append({
                "espn_id": espn_id,
                "team_id": team_id,
                "season": season,
                "display_names": sorted(distinct_names),
            })
    return conflicts


def find_roster_players_linked_by_multiple_espn_ids(
    linked_results: list[PlayerIdentity],
) -> dict[str, list[str]]:
    """
    Detects the inverse conflict: two or more *different* ESPN athlete
    ids both successfully linking (via :func:`link_espn_identity_to_roster`)
    to what looks like the same Sports-Reference roster player. Grouped by
    the linked identity's Sports-Reference source value (falls back to
    ``team_id:season:display_name`` if no real SR id is present, so this
    still catches the conflict for name-scoped-only records).
    """
    by_roster_key: dict[str, set[str]] = {}
    for pid in linked_results:
        sr_source = pid.sources.get("sports_reference")
        espn_source = pid.sources.get("espn")
        if not espn_source:
            continue
        roster_key = sr_source.raw_value if sr_source else f"{pid.team_id}:{pid.season}:{canonical_player_key_v15(pid.display_name)}"
        by_roster_key.setdefault(roster_key, set()).add(espn_source.raw_value)

    return {key: sorted(ids) for key, ids in by_roster_key.items() if len(ids) > 1}
