"""
Injury player-identity adapter (Phase 2B, batch 3).

ACTIVE DEFINITIONS AND CALL SITES -- TWO SEPARATE INJURY SYSTEMS

This session's investigation found the notebook has **two independent
injury systems**, not one, and they have very different identity needs:

1. **ESPN ppg-based deduction** (`_fetch_espn_injuries` / cell 521, and
   `compute_injury_deduction` / cell 521 -- both already faithfully
   migrated to `src/scraper.py` as `_fetch_espn_injuries_for_team` /
   `compute_injury_deduction`, verified in the Covers/injury batch;
   `_fetch_espn_injuries_for_team`'s notebook-match was previously
   mis-flagged "no notebook match" in `docs/function_manifest.csv` purely
   because of the rename -- corrected this session). This path needs
   **no player-to-roster matching at all**: it filters ESPN's injury feed
   by ESPN numeric ``team_espn_id`` directly, and computes its deduction
   from ESPN's own self-contained ``ppg`` stat on each injury entry. The
   player's ``athlete.get("displayName")`` (a full name) is used only for
   a log line, never joined against AllStats/roster data. Confirmed: no
   athlete ID captured here either, though ESPN's real payload almost
   certainly has ``athlete.id``.

2. **Last-name-based availability simulation** (`injury_sensitivity_check`
   / cell 294, `get_meaningful_lastnames` / cell 294 -- the latter already
   migrated to `src/scraper.py`, verified faithful -- and
   `apply_injury_penalty` / cell 290, all **unmigrated except
   get_meaningful_lastnames**, and out of scope here since
   `apply_injury_penalty` is OVR/radar-adjacent). This is where real
   last-name-only matching happens: `injury_sensitivity_check` loads a
   team+season's AllStats, gates by `get_radar_roster_lastnames()` (OVR/
   radar-scoped, not touched) and `get_meaningful_lastnames()` (already
   migrated), and matches `injury_data` -- the output of
   `src.scraper.scrape_covers_injuries_for_teams`/`scrape_espn_injuries`,
   **both already last-name-reduced via `normalize_covers_player`,
   verified faithful** -- purely by last-name string equality into
   ``inactive_players: dict[last_name -> availability]``.

   **Confirmed structural risk** (this batch's objective #5): because the
   result is a plain ``dict`` keyed by bare last name, two roster players
   sharing a surname cannot both be represented -- one would silently
   overwrite the other's availability value. Not observed to have actually
   happened in any fixture checked this session, but the mechanism is
   real and this adapter's whole purpose is to close exactly this gap for
   any *new* code that wants to attach an injury to a specific player.

   **Confirmed cross-system bug propagation** (found this session, not
   fixed, per "do not modify injury deductions... or scraping behavior"):
   `apply_injury_penalty` computes its own last-name key via
   ``ovr_df["Player"].apply(normalize_allstats_player)`` -- the *buggy*
   one-liner from the player-identity-core batch
   (``name.lower().split()[-1]``). For a suffix-corrected display name
   like "Daryl Simmons II" (exactly the kind `harmonize_player_names`
   produces), this yields ``"ii"``, not ``"simmons"`` -- meaning that
   specific player's OVR-side last-name key can never match an injury
   report keyed by "simmons". Recorded in the ledger below, not fixed.

3. **Covers team-key bridging** (`injury_sensitivity_check`'s own
   comment: *"Primary: match by team_slug... Fallback: covers_team_alias
   for legacy ESPN feed format"*) -- confirms Covers' injury dict is keyed
   by SR slug while the (older/legacy) ESPN injury dict was keyed by a
   `canonicalize_team()`-derived name string, needing a manual bridge.
   This is the same "different sources use different team-key
   namespaces" problem every prior adapter in this migration has solved
   by routing through the canonical registry -- this adapter does the
   same for injury team keys instead of reusing the ad-hoc bridge.

STABLE-ID STRATEGY FOR INJURY RECORDS

An injury record's own identity is never independently authoritative --
it always describes an *existing* player (from the ESPN or SR/AllStats
player adapters). So this module does not mint new player ids; it either
(a) resolves a full-name ESPN injury entry via the existing
`espn_player_identity.resolve_espn_player_identity` (reuse, not
reimplementation, per this batch's instruction), or (b) for a last-name-only
Covers/legacy-ESPN entry, looks for a *unique* surname match within an
already-resolved, team+season-scoped roster (the output of the previous
batch's `resolve_roster_player_identities`) and returns it as
`UnresolvedIdentity(reason="unconfirmed")` -- present, surfaced, but never
auto-assigned -- unless an explicit, caller-supplied reviewed alias
resolves it, mirroring the reviewed-suffix-patch pattern from batch 1.
"""
from __future__ import annotations

from src.identity.contracts import PlayerIdentity, TeamIdentity, UnresolvedIdentity
from src.identity.espn_player_identity import resolve_espn_player_identity
from src.identity.player_identity import canonical_player_key_v15
from src.identity.team_identity import resolve_team_identity
from src.logging_config import get_logger
from src.scraper import normalize_covers_player

log = get_logger(__name__)


def _last_name_key(display_name: str) -> str:
    """
    Last-name extraction consistent with `src.scraper.normalize_covers_player`
    (suffix-aware, unlike the buggy `normalize_allstats_player` this
    module's docstring documents but deliberately does not reuse).
    """
    return normalize_covers_player(display_name)


# ---------------------------------------------------------------------------
# ESPN ppg-based injury path -- reuses the ESPN player adapter directly
# ---------------------------------------------------------------------------


def resolve_espn_injury_player_identity(
    display_name: str,
    team_query: str,
    season: int,
    *,
    espn_athlete_id: str | None = None,
) -> PlayerIdentity | UnresolvedIdentity:
    """
    Resolve one ESPN injury-feed entry's player to a canonical identity.

    Thin, purpose-named wrapper over
    `espn_player_identity.resolve_espn_player_identity` -- the ESPN
    injury feed provides a full display name (unlike Covers), so this is
    exactly the same resolution problem the ESPN player adapter already
    solves; no new matching logic is needed, per this batch's instruction
    to reuse the existing adapters. Kept as a separate, injury-labeled
    entry point purely so callers get a ``source="espn_injury"``-tagged
    result distinguishable from a non-injury ESPN roster lookup.
    """
    result = resolve_espn_player_identity(
        display_name, team_query, season, espn_athlete_id=espn_athlete_id
    )
    if isinstance(result, PlayerIdentity):
        return result
    return UnresolvedIdentity(
        query=result.query, source="espn_injury", reason=result.reason,
        suggestions=result.suggestions, candidates=result.candidates,
    )


# ---------------------------------------------------------------------------
# Last-name-only injury path (Covers, legacy ESPN feed) -- the real
# last-name-matching system this batch's investigation centers on
# ---------------------------------------------------------------------------


def resolve_lastname_only_injury_candidate(
    last_name_or_raw: str,
    roster_identities: list[PlayerIdentity],
    *,
    source: str = "covers_injury",
    reviewed_alias_display_name: str | None = None,
) -> PlayerIdentity | UnresolvedIdentity:
    """
    Resolve a last-name-only injury report (Covers' actual format, and
    `src.scraper.scrape_espn_injuries`'s legacy last-name-reduced format)
    against an already team+season-scoped roster.

    Deliberately **stricter** than `player_identity.match_stat_row_to_roster`
    (which safely auto-confirms a *unique full-name* match): last-name-only
    evidence is inherently weaker, so per this batch's instruction, even a
    single unique surname match is returned as
    ``UnresolvedIdentity(reason="unconfirmed")`` with that one candidate
    populated -- never silently promoted to a confirmed identity -- unless
    ``reviewed_alias_display_name`` is supplied (an explicit, caller-known
    full name for this specific injury report, analogous to batch 1's
    hardcoded suffix patches) and it uniquely matches a roster entry.

    ``roster_identities`` is assumed already scoped to one team+season
    (the same convention `match_stat_row_to_roster` uses) -- this function
    never reaches across team/season boundaries on its own.
    """
    raw = (last_name_or_raw or "").strip()
    if not raw:
        return UnresolvedIdentity(query=str(last_name_or_raw), source=source, reason="not_found")

    # Normalize the same way the real scrapers do, so a caller can pass
    # either an already-reduced last name or a raw "Player Name" string.
    key = normalize_covers_player(raw) if " " in raw else raw.strip().lower()

    if reviewed_alias_display_name:
        reviewed_key = canonical_player_key_v15(reviewed_alias_display_name)
        confirmed = [p for p in roster_identities if canonical_player_key_v15(p.display_name) == reviewed_key]
        if len(confirmed) == 1:
            log.info(
                "reviewed alias resolved last-name injury report %r -> %r",
                raw, confirmed[0].display_name,
            )
            return confirmed[0]

    candidates = [p for p in roster_identities if _last_name_key(p.display_name) == key]

    if not candidates:
        return UnresolvedIdentity(query=raw, source=source, reason="not_found")

    if len(candidates) > 1:
        log.warning(
            "ambiguous last-name injury report %r: %d roster players share surname %r (%s)",
            raw, len(candidates), key, [p.display_name for p in candidates],
        )
        return UnresolvedIdentity(query=raw, source=source, reason="ambiguous", candidates=list(candidates))

    log.info(
        "last-name injury report %r matches exactly one roster player (%r) but "
        "is not auto-confirmed -- surfaced as unconfirmed, per this batch's rule",
        raw, candidates[0].display_name,
    )
    return UnresolvedIdentity(query=raw, source=source, reason="unconfirmed", candidates=list(candidates))


def resolve_injury_team_identity(team_query: str, *, source: str = "injury") -> TeamIdentity | UnresolvedIdentity:
    """
    Resolve an injury source's team key (an SR slug from Covers, a
    `canonicalize_team()`-derived name from the legacy ESPN feed, or an
    ESPN numeric id) through the Phase 2A canonical registry, replacing
    the ad-hoc ``covers_team_alias`` bridge `injury_sensitivity_check`
    uses to reconcile the two different team-key namespaces its two
    injury sources produce.
    """
    return resolve_team_identity(team_query, source=source)


def check_injury_team_matches_roster_team(
    injury_team_result: TeamIdentity | UnresolvedIdentity,
    roster_team_id: int,
) -> bool:
    """
    Detects "injury team conflicting with the player's roster team"
    (this batch's conflict-detection requirement): returns ``False`` if
    the injury source's own resolved team does not match the team_id the
    roster was built for -- a caller should treat that as a conflict, not
    silently attach the injury anyway.
    """
    return isinstance(injury_team_result, TeamIdentity) and injury_team_result.team_id == roster_team_id


# ---------------------------------------------------------------------------
# Conflict detection -- never resolved silently
# ---------------------------------------------------------------------------


def find_duplicate_injury_reports(
    injury_records: list[dict],
) -> list[dict]:
    """
    Detects duplicate injury records for the same (last-name-or-name,
    status) pair within one source's output for one team -- e.g. if a
    Covers injury table lists the same player twice (a collapse/detail
    row not filtered out upstream). Each record must have ``"player"``
    and ``"status"`` keys, matching the dict shape
    `scrape_covers_injuries_for_teams`/`scrape_espn_injuries` already
    produce -- this function does not alter or re-scrape anything, purely
    inspects a list already produced by those (unmodified) functions.
    """
    seen: dict[tuple[str, str], int] = {}
    duplicates = []
    for record in injury_records:
        key = (str(record.get("player", "")).strip().lower(), str(record.get("status", "")).strip().lower())
        seen[key] = seen.get(key, 0) + 1
    for (player, status), count in seen.items():
        if count > 1:
            duplicates.append({"player": player, "status": status, "count": count})
    return duplicates
