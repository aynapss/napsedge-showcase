"""
ESPN team/player-stat canonical ingestion (Phase 3, batch 2).

ACTIVE V15 DEFINITIONS AND CALL SITES

`scrape_espn_players()` (cell 126, unshadowed) is the one active ESPN
player/stat source: a **Selenium** scrape of
``https://www.espn.com/mens-college-basketball/stats/player`` (not
``requests``, unlike every other scraper in this codebase -- confirmed by
reading the cell, not assumed), cached at
``TeamDump/ESPN/espn_full_player_stats.csv``. Per-row extraction
(``name_tag.get_text(strip=True)`` from ``cols[1].find("a")``, plus a
``span.athleteCell__teamAbbrev`` for the team code) was already ported
byte-faithfully as ``extract_espn_player_row_v15``/``extract_espn_athlete_id``
in ``src/identity/espn_player_identity.py`` (Phase 2B batch 2) -- reused
directly here, not re-ported. `fetch_espn_teams()` (cell 84) and the
token-shortening team-name resolver it feeds are a **separate** system
(ESPN's team-*list*/logo page, "<school> <mascot>"-shaped names) already
covered by ``src/identity/espn_identity.py`` (Phase 2A) -- not the same
data as the player-stats page's ``Team`` column, which uses short
abbreviation codes ("BYU", "ECU", "ARK") instead.

CONFIRMED, NOT INFERRED: ESPN ATHLETE IDS ARE DISCARDED BY V15

`scrape_espn_players`'s player cell contains a real ``<a href="/mens-
college-basketball/player/_/id/{id}/{slug}">`` anchor, but only
``.get_text()`` is read -- the href, and the numeric id inside it, is
discarded before the row ever reaches the DataFrame. Confirmed against
both real cached files (``espn_full_player_stats.csv``, ``..._enriched.csv``):
columns are ``RK, Name, Team, POS`` + stat columns -- no id column in
either, and no season column either (a single current-snapshot dataset).
This module's HTML-based ingestion path (`run_espn_player_ingestion_from_html`)
closes that gap when raw HTML evidence is available; its CSV-based path
(`run_espn_player_ingestion_from_csv`) honestly reflects today's real
data shape -- ``espn_athlete_id`` is always ``None`` there, never invented.

NEW FINDING THIS BATCH: A THIRD, UNRESOLVED ESPN TEAM-NAME CONVENTION

The player-stats page's ``Team`` column (short codes) is **not** the same
shape ``src/identity/espn_identity.py``'s ``resolve_espn_team_identity``
handles (that adapter shortens "<school> <mascot>" strings from the
team-*list* page). Checked directly against the real fixture: of 365
unique codes in ``espn_full_player_stats.csv``, only 84 resolve through
the existing canonical registry's own alias table (`resolve_team_identity`,
exact/deterministic, no fuzzy matching) -- the other 281 (e.g. "ECU",
"ARK", "STAN") are genuine abbreviations with no registered alias.
Building a dedicated ESPN-abbreviation-to-canonical crosswalk would
require either live verification or a hand-curated mapping this batch has
no local evidence for -- **out of this batch's bounded scope**, recorded
here as a real, unresolved finding rather than guessed at. Team
resolution in this module is therefore exact/deterministic only: a code
without a registered alias correctly returns ``UnresolvedIdentity``, not
a defect in this module, but a real gap in ESPN team-code coverage.

INJURY-FEED SHARING (investigated only as required, not migrated)

`src.scraper.scrape_espn_injuries`/`_fetch_espn_injuries_for_team`
(already migrated, verified faithful in the Covers/injury batch) has the
identical id-discard gap at the JSON-payload level (reads
``athlete.get("displayName")`` but never ``athlete.get("id")``) --
already documented in `src/identity/espn_player_identity.py`'s own
docstring (Phase 2B batch 2). Not touched, not re-migrated here; noted
for the later injury batch as instructed.

DESIGN

Mirrors `src/ingestion/sr_allstats_ingestion.py`'s pattern exactly:
explicit boundaries between raw-HTML/CSV evidence, parsing, team identity
resolution (`resolve_team_identity`, exact only), player identity
resolution (`resolve_espn_player_identity`/`link_espn_identity_to_roster`,
reused unmodified from Phase 2B batch 2), and an additive canonical
result -- never a legacy CSV/DataFrame column, filename, dtype, or value
change. `scrape_espn_players()` itself is not called, wrapped, or edited;
its live Selenium fetch is out of this batch's scope by instruction
("do not perform live ESPN requests").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd
from bs4 import BeautifulSoup

from src.identity.contracts import PlayerIdentity, TeamIdentity, UnresolvedIdentity
from src.identity.espn_player_identity import (
    extract_espn_athlete_id,
    extract_espn_player_row_v15,
    find_espn_id_reused_within_same_team_season,
    find_roster_players_linked_by_multiple_espn_ids,
    link_espn_identity_to_roster,
    resolve_espn_player_identity,
)
from src.identity.team_identity import resolve_team_identity
from src.logging_config import get_logger

log = get_logger(__name__)

PARSER_VERSION = "espn_ingestion.v1"


# ---------------------------------------------------------------------------
# Canonical result contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EspnRawEvidence:
    """Raw source evidence, preserved before any cleaning/transformation."""

    source_name: str
    source_url_or_endpoint: str
    fixture_provenance: str  # e.g. "html" | "cached_csv:<path>" | "mocked_fixture"
    collected_at_utc: str
    parser_version: str


@dataclass(frozen=True)
class EspnPlayerIngestionRecord:
    """One player row's raw evidence plus canonical identity attachment."""

    raw_display_name: str
    raw_team_code: str
    espn_athlete_id: str | None
    season: int
    team_identity: TeamIdentity | UnresolvedIdentity
    player_identity: PlayerIdentity | UnresolvedIdentity
    linked_roster_identity: PlayerIdentity | UnresolvedIdentity | None
    resolution_status: str  # "resolved" | "team_unresolved" | "player_unresolved" | "player_ambiguous"


@dataclass
class EspnIngestionResult:
    """
    The canonical ESPN ingestion result. Deliberately carries no legacy
    DataFrame/CSV -- existing callers of `scrape_espn_players()` are
    unaffected; this is a separate, additive result.
    """

    evidence: EspnRawEvidence
    records: list[EspnPlayerIngestionRecord] = field(default_factory=list)
    reused_athlete_id_conflicts: list[dict] = field(default_factory=list)
    multi_id_roster_conflicts: dict[str, list[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Raw HTML parsing -- reuses extract_espn_player_row_v15/extract_espn_athlete_id
# ---------------------------------------------------------------------------


def parse_espn_player_rows_from_html(html: str) -> list[dict]:
    """
    Parses the two-table structure `scrape_espn_players` (cell 126)
    expects (table 0: rank/name/team cells; table 1: stat columns),
    reusing the already-ported `extract_espn_player_row_v15`/
    `extract_espn_athlete_id` for each row's player cell -- no new
    normalization or extraction rule is introduced.

    Returns one dict per row: ``{"raw_display_name", "raw_team_code",
    "espn_athlete_id"}``. Malformed/incomplete HTML (missing tables, rows
    with fewer than 2 cells) yields an empty or partial list rather than
    raising -- matching v15's own "return what parsed, don't crash" style.
    """
    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table")
    if len(tables) < 1:
        return []

    rows = []
    for tr in tables[0].select("tbody tr"):
        cols = tr.find_all("td")
        if len(cols) < 2:
            continue
        player_cell = cols[1]
        name, team = extract_espn_player_row_v15(player_cell)
        athlete_id = extract_espn_athlete_id(player_cell)
        if not name:
            continue
        rows.append({
            "raw_display_name": name,
            "raw_team_code": team,
            "espn_athlete_id": athlete_id,
        })
    return rows


def parse_espn_player_rows_from_csv(csv_path: str) -> list[dict]:
    """
    Reads the existing cached CSV (`RK, Name, Team, POS` + stats -- no id
    column, confirmed against the real fixture). Honestly reflects
    today's real data shape: `espn_athlete_id` is always `None` here,
    never fabricated. Does not modify the CSV.
    """
    df = pd.read_csv(csv_path)
    if "Name" not in df.columns:
        return []

    rows = []
    for _, row in df.iterrows():
        name = str(row.get("Name", "")).strip()
        if not name or name.lower() == "nan":
            continue
        rows.append({
            "raw_display_name": name,
            "raw_team_code": str(row.get("Team", "")).strip(),
            "espn_athlete_id": None,
        })
    return rows


# ---------------------------------------------------------------------------
# Canonical identity resolution per row
# ---------------------------------------------------------------------------


def resolve_espn_ingestion_row(
    raw_display_name: str,
    raw_team_code: str,
    season: int,
    *,
    espn_athlete_id: str | None = None,
    roster_identities: list[PlayerIdentity] | None = None,
    source: str = "espn",
) -> EspnPlayerIngestionRecord:
    """
    Resolves one ESPN player row to canonical team + player identity.

    Team resolution is exact/deterministic only (`resolve_team_identity`,
    no fuzzy matching) -- an ESPN team code without a registered canonical
    alias correctly returns `UnresolvedIdentity`, per this batch's
    documented finding rather than a guessed crosswalk. Player resolution
    reuses `resolve_espn_player_identity` (Phase 2B batch 2) entirely,
    scoped to the resolved team_id and the required `season` -- a
    name-only record is never resolved without both. When
    `roster_identities` is supplied (a Sports-Reference roster, team+
    season scoped), also attempts `link_espn_identity_to_roster` (unique,
    suffix-compatible match only -- never `.iloc[0]`/first-match).
    """
    team_result = resolve_team_identity(raw_team_code, source=source)

    if not isinstance(team_result, TeamIdentity):
        return EspnPlayerIngestionRecord(
            raw_display_name=raw_display_name,
            raw_team_code=raw_team_code,
            espn_athlete_id=espn_athlete_id,
            season=season,
            team_identity=team_result,
            player_identity=team_result,
            linked_roster_identity=None,
            resolution_status="team_unresolved",
        )

    player_result = resolve_espn_player_identity(
        raw_display_name, raw_team_code, season, espn_athlete_id=espn_athlete_id, source=source,
    )

    linked = None
    status = "resolved" if isinstance(player_result, PlayerIdentity) else (
        "player_ambiguous" if isinstance(player_result, UnresolvedIdentity) and player_result.reason == "ambiguous"
        else "player_unresolved"
    )

    if isinstance(player_result, PlayerIdentity) and roster_identities:
        scoped_roster = [p for p in roster_identities if p.team_id == team_result.team_id and p.season == season]
        linked = link_espn_identity_to_roster(player_result, scoped_roster, source=f"{source}_to_sr_link")

    return EspnPlayerIngestionRecord(
        raw_display_name=raw_display_name,
        raw_team_code=raw_team_code,
        espn_athlete_id=espn_athlete_id,
        season=season,
        team_identity=team_result,
        player_identity=player_result,
        linked_roster_identity=linked,
        resolution_status=status,
    )


# ---------------------------------------------------------------------------
# Orchestrators
# ---------------------------------------------------------------------------


def _build_conflict_findings(records: list[EspnPlayerIngestionRecord]) -> tuple[list[dict], dict[str, list[str]]]:
    identities = [r.player_identity for r in records if isinstance(r.player_identity, PlayerIdentity)]
    reused = find_espn_id_reused_within_same_team_season(identities)

    linked = [r.linked_roster_identity for r in records if isinstance(r.linked_roster_identity, PlayerIdentity)]
    multi_id = find_roster_players_linked_by_multiple_espn_ids(linked) if linked else {}

    return reused, multi_id


def run_espn_player_ingestion_from_html(
    html: str,
    *,
    season: int,
    roster_identities: list[PlayerIdentity] | None = None,
    source_url: str = "https://www.espn.com/mens-college-basketball/stats/player",
    fixture_provenance: str = "html",
    source: str = "espn",
) -> EspnIngestionResult:
    """
    Canonical ingestion from raw ESPN player-stats HTML -- the only path
    that can recover a real athlete id (present in the source markup but
    discarded by v15's own parser). Deterministic and idempotent: a
    repeated call against the same `html` string produces byte-identical
    records (verified by test).
    """
    evidence = EspnRawEvidence(
        source_name=source,
        source_url_or_endpoint=source_url,
        fixture_provenance=fixture_provenance,
        collected_at_utc=datetime.now(timezone.utc).isoformat(),
        parser_version=PARSER_VERSION,
    )

    raw_rows = parse_espn_player_rows_from_html(html)
    records = [
        resolve_espn_ingestion_row(
            row["raw_display_name"], row["raw_team_code"], season,
            espn_athlete_id=row["espn_athlete_id"], roster_identities=roster_identities, source=source,
        )
        for row in raw_rows
    ]
    reused, multi_id = _build_conflict_findings(records)

    return EspnIngestionResult(
        evidence=evidence, records=records,
        reused_athlete_id_conflicts=reused, multi_id_roster_conflicts=multi_id,
    )


def run_espn_player_ingestion_from_csv(
    csv_path: str,
    *,
    season: int,
    roster_identities: list[PlayerIdentity] | None = None,
    source: str = "espn",
) -> EspnIngestionResult:
    """
    Canonical ingestion from the existing cached CSV
    (`TeamDump/ESPN/espn_full_player_stats.csv`) -- the real, current
    fixture shape. `espn_athlete_id` is always `None` here (the CSV never
    had one to begin with), never fabricated. `season` must be supplied
    explicitly by the caller: the real CSV has no season column at all.
    Never modifies the CSV.
    """
    evidence = EspnRawEvidence(
        source_name=source,
        source_url_or_endpoint=csv_path,
        fixture_provenance=f"cached_csv:{csv_path}",
        collected_at_utc=datetime.now(timezone.utc).isoformat(),
        parser_version=PARSER_VERSION,
    )

    raw_rows = parse_espn_player_rows_from_csv(csv_path)
    records = [
        resolve_espn_ingestion_row(
            row["raw_display_name"], row["raw_team_code"], season,
            espn_athlete_id=row["espn_athlete_id"], roster_identities=roster_identities, source=source,
        )
        for row in raw_rows
    ]
    reused, multi_id = _build_conflict_findings(records)

    return EspnIngestionResult(
        evidence=evidence, records=records,
        reused_athlete_id_conflicts=reused, multi_id_roster_conflicts=multi_id,
    )
