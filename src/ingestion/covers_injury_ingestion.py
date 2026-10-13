"""
Covers.com injury canonical ingestion (Phase 3, batch 3).

ACTIVE V15 DEFINITIONS AND CALL SITES

`scrape_covers_injuries_for_teams` (`src/scraper.py`, already migrated and
verified faithful) is the active Covers injury scraper -- called with a
list of Sports-Reference team slugs plus a `covers_slug_map` (the output
of `build_covers_ncaab_slug_map`, also already migrated). Downstream,
`injury_sensitivity_check` (cell 294, unmigrated, OVR/radar-adjacent) and
`apply_injury_penalty` (cell 290, unmigrated, OVR/radar-adjacent) are the
calculation consumers -- neither touched here, per this batch's explicit
boundary ("do not modify downstream injury formulas"). `covers_team_alias`
(`src/scraper.py`) is the ad-hoc bridge those functions use to reconcile
Covers/legacy-ESPN team-key namespaces -- not used by this module, which
routes through `covers_identity.py`/`injury_player_identity.py` instead
(requirement #6: "do not use cross-source slug substitution").

CONFIRMED SOURCE-EVIDENCE FINDING: `scrape_covers_injuries_for_teams`
DISCARDS THE RAW PLAYER NAME BEFORE RETURNING

Read in full (`src/scraper.py`, the per-row parsing loop): the raw player
cell text (`tds[0].get_text(...)`, a full display name -- e.g. "Darius
McGhee Jr.", per `normalize_covers_player`'s own docstring example) is
computed as `player_raw`, but **only printed, never stored** -- the
function's own output dict only ever contains `normalize_covers_player(player_raw)`
(the reduced last name). By the time any caller receives
`scrape_covers_injuries_for_teams`'s return value, the raw full name,
suffix, and any hyphenation are already gone. This is the same
"discarded-before-persistence" shape as Phase 3 batches 1-2's SR-slug and
ESPN-athlete-id findings, just for the player's raw name string instead of
a source id.

Because that data loss happens **inside** the already-verified scraper
function (not touched here, per instruction), this module's raw-evidence
preservation is achieved by an independent, verbatim-equivalent parse of
the same HTML (`parse_covers_injury_rows_v15`) -- same selectors
(`div#TP_injuries`, skip `tr[id^="injuryCollapse"]`, `tds[0]`/`tds[2]`),
same reduction functions (`normalize_covers_player`/`normalize_covers_status`,
reused unmodified), but returning the raw strings *alongside* the reduced
ones instead of discarding them.

RESOLUTION BEHAVIOR: UNCHANGED FROM THE EXISTING CONSERVATIVE ADAPTER

This module does **not** introduce a new full-name-matching tier, even
though the raw full name is now preserved as evidence. Per "retain the
established conservative behavior," player resolution here calls
`resolve_lastname_only_injury_candidate` (Phase 2B batch 3) exactly as it
already exists: a unique surname match is `unconfirmed`, multiple matches
are `ambiguous`, only an explicit caller-supplied
`reviewed_alias_display_name` confirms a candidate. The raw full name is
carried in `CoversInjuryRecord.raw_player_name` purely as preserved
evidence/provenance (available to a future batch that might build a
stronger full-name-first matching tier) -- not used to alter this batch's
resolution outcome.

LEGACY LIMITATIONS DOCUMENTED, NOT REPAIRED

- Compound surnames (e.g. "Van Der Berg") still reduce to their final
  token only (`normalize_covers_player`'s own documented behavior,
  reused unmodified) -- a real, pre-existing limitation, not introduced
  or fixed here.
- `apply_injury_penalty` (cell 290, OVR/radar-adjacent, unmigrated) still
  reaches the buggy legacy `normalize_allstats_player` path for its own,
  separate last-name key -- documented in `injury_player_identity.py`'s
  docstring (Phase 2B batch 3) and the Phase 2 consolidated ledger. Not
  touched by this batch; this module's canonical records are entirely
  additive and do not feed into `apply_injury_penalty`.
- v15's existing injury calculations (`injury_sensitivity_check`,
  `apply_injury_penalty`) may continue to exhibit these known matching
  defects -- this batch changes nothing about their behavior or output.

FIXTURE PROVENANCE

No real cached Covers injury HTML page exists locally (`covers_slug_cache.json`
is present but empty -- 0 entries). Per instruction #15, this batch uses a
realistic mocked Covers HTML fixture (shaped exactly from
`scrape_covers_injuries_for_teams`'s own real selectors, confirmed by
reading its source) rather than performing a live request. This limitation
is documented, not glossed over.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from src.identity.contracts import PlayerIdentity, TeamIdentity, UnresolvedIdentity
from src.identity.covers_identity import resolve_covers_team_identity
from src.identity.injury_player_identity import (
    find_duplicate_injury_reports,
    resolve_lastname_only_injury_candidate,
)
from src.logging_config import get_logger
from src.scraper import normalize_covers_player, normalize_covers_status

log = get_logger(__name__)

PARSER_VERSION = "covers_injury_ingestion.v1"


# ---------------------------------------------------------------------------
# Canonical result contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoversRawEvidence:
    """Raw source evidence, preserved before any cleaning/transformation."""

    source_name: str
    source_url: str
    covers_team_slug: str
    fixture_provenance: str  # "html" | "mocked_fixture:<label>"
    collected_at_utc: str
    parser_version: str


@dataclass(frozen=True)
class CoversInjuryRecord:
    """One injury row's raw evidence plus canonical identity attachment."""

    raw_player_name: str
    raw_status: str
    normalized_last_name: str
    normalized_status: str
    season: int
    team_identity: TeamIdentity | UnresolvedIdentity
    player_identity: PlayerIdentity | UnresolvedIdentity
    resolution_status: str  # "confirmed_via_reviewed_alias" | "unconfirmed" | "ambiguous" | "not_found" | "team_unresolved"


@dataclass
class CoversInjuryIngestionResult:
    """
    The canonical Covers injury ingestion result. Carries no legacy
    `dict[sr_slug, list[dict]]` shape -- `scrape_covers_injuries_for_teams`'s
    existing callers are unaffected; this is a separate, additive result.
    """

    evidence: CoversRawEvidence
    records: list[CoversInjuryRecord] = field(default_factory=list)
    duplicate_reports: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Legacy-shaped raw parse -- verbatim-equivalent to scrape_covers_injuries_for_teams's
# own per-row logic, but preserving raw fields it discards
# ---------------------------------------------------------------------------


def parse_covers_injury_rows_v15(html: str) -> list[dict]:
    """
    Verbatim-equivalent reproduction of `scrape_covers_injuries_for_teams`'s
    own per-row parsing (`src/scraper.py`) -- same selectors
    (`div#TP_injuries`, skip `tr[id^="injuryCollapse"]`, `tds[0]`/`tds[2]`),
    same reduction functions (`normalize_covers_player`/`normalize_covers_status`,
    reused unmodified) -- but returns the raw player/status strings
    alongside the reduced ones, closing the exact gap where the legacy
    function computes them and only prints them, never persists them.
    Malformed HTML (missing injury pane, rows with fewer than 3 cells)
    yields an empty or partial list rather than raising.
    """
    soup = BeautifulSoup(html, "lxml")
    injury_pane = soup.find("div", id="TP_injuries")
    if not injury_pane:
        return []

    rows = []
    for tr in injury_pane.find_all("tr"):
        if tr.get("id", "").startswith("injuryCollapse"):
            continue
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue

        player_raw = re.sub(r"\s+", " ", tds[0].get_text(" ", strip=True)).strip()
        b = tds[2].find("b")
        status_raw = b.get_text(strip=True) if b else tds[2].get_text(" ", strip=True)
        status_raw = re.sub(r"\s+", " ", status_raw).strip()

        player_last = normalize_covers_player(player_raw)
        status = normalize_covers_status(status_raw)

        if player_last and status != "active":
            rows.append({
                "player_raw": player_raw,
                "status_raw": status_raw,
                "player_last": player_last,
                "status": status,
            })
    return rows


# ---------------------------------------------------------------------------
# Canonical identity resolution per row
# ---------------------------------------------------------------------------


def resolve_covers_injury_row(
    *,
    player_raw: str,
    status_raw: str,
    player_last: str,
    status: str,
    covers_team_slug: str,
    season: int,
    roster_identities: list[PlayerIdentity] | None = None,
    reviewed_alias_display_name: str | None = None,
    source: str = "covers_injury",
) -> CoversInjuryRecord:
    """
    Resolves one Covers injury row to canonical team + player identity.

    Team resolution routes through `resolve_covers_team_identity`
    (`covers_identity.py`, Phase 2A) -- never `covers_slug_map.get(sr_slug)`
    cross-source substitution. Player resolution reuses
    `resolve_lastname_only_injury_candidate` (Phase 2B batch 3) unchanged
    -- a unique surname match is `unconfirmed`, multiple matches are
    `ambiguous`, only `reviewed_alias_display_name` confirms a candidate.
    `roster_identities` (if supplied) is scoped to the resolved
    `team_id`+`season` before matching -- never an unscoped name-only merge.
    """
    team_result = resolve_covers_team_identity(covers_team_slug, source=source)

    if not isinstance(team_result, TeamIdentity):
        return CoversInjuryRecord(
            raw_player_name=player_raw, raw_status=status_raw,
            normalized_last_name=player_last, normalized_status=status, season=season,
            team_identity=team_result, player_identity=team_result,
            resolution_status="team_unresolved",
        )

    scoped_roster = [
        p for p in (roster_identities or [])
        if p.team_id == team_result.team_id and p.season == season
    ]

    player_result = resolve_lastname_only_injury_candidate(
        player_last, scoped_roster, source=source, reviewed_alias_display_name=reviewed_alias_display_name,
    )

    if isinstance(player_result, PlayerIdentity):
        resolution_status = "confirmed_via_reviewed_alias" if reviewed_alias_display_name else "confirmed"
    else:
        resolution_status = player_result.reason  # "unconfirmed" | "ambiguous" | "not_found"

    return CoversInjuryRecord(
        raw_player_name=player_raw, raw_status=status_raw,
        normalized_last_name=player_last, normalized_status=status, season=season,
        team_identity=team_result, player_identity=player_result,
        resolution_status=resolution_status,
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_covers_injury_ingestion_from_html(
    html: str,
    covers_team_slug: str,
    season: int,
    *,
    roster_identities: list[PlayerIdentity] | None = None,
    reviewed_aliases: dict[str, str] | None = None,
    source_url: str | None = None,
    fixture_provenance: str = "html",
    source: str = "covers_injury",
) -> CoversInjuryIngestionResult:
    """
    Orchestrates canonical Covers injury ingestion for one team+season from
    raw HTML evidence. Deterministic and idempotent: a repeated call
    against the same `html` produces byte-identical records (verified by
    test). `reviewed_aliases` maps a normalized last name to an explicit,
    caller-reviewed full display name (mirrors the reviewed-suffix-patch
    pattern already used elsewhere in this migration) -- never invented
    here, only threaded through to `resolve_lastname_only_injury_candidate`.
    """
    evidence = CoversRawEvidence(
        source_name=source,
        source_url=source_url or (
            f"https://www.covers.com/sport/basketball/ncaab/teams/main/{covers_team_slug}/injuries"
        ),
        covers_team_slug=covers_team_slug,
        fixture_provenance=fixture_provenance,
        collected_at_utc=datetime.now(timezone.utc).isoformat(),
        parser_version=PARSER_VERSION,
    )

    raw_rows = parse_covers_injury_rows_v15(html)
    reviewed = reviewed_aliases or {}

    records = [
        resolve_covers_injury_row(
            player_raw=row["player_raw"], status_raw=row["status_raw"],
            player_last=row["player_last"], status=row["status"],
            covers_team_slug=covers_team_slug, season=season,
            roster_identities=roster_identities,
            reviewed_alias_display_name=reviewed.get(row["player_last"]),
            source=source,
        )
        for row in raw_rows
    ]

    duplicates = find_duplicate_injury_reports([
        {"player": r.normalized_last_name, "status": r.normalized_status} for r in records
    ])

    return CoversInjuryIngestionResult(evidence=evidence, records=records, duplicate_reports=duplicates)
