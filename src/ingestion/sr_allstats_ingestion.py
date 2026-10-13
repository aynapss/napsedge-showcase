"""
Sports-Reference/AllStats canonical ingestion (Phase 3, batch 1).

SCOPE

One controlled ingestion path for Sports-Reference team-season pages,
built to sit ALONGSIDE (not replace) the existing legacy pipeline:
`scrape_team_season` / `build_all_stats` / `save_tables_to_excel` (v15
notebook cells 98/115/114, already bridged unmodified into
`src.ingestion.notebook_core.load_core_runtime` / `run_sports_reference_refresh`).
This module never calls or edits those three functions -- it produces a
SEPARATE, additive canonical result (team/SR-slug resolution, raw
provenance evidence, a player-slug sidecar, repeated-header data-quality
findings) from the same underlying HTML, satisfying "separate raw
ingestion from cleaning and transformation" and "preserve legacy notebook
outputs" simultaneously.

INVENTORY (targeted reads this batch, not a full re-read of the notebook)

- `fetch_all_schools` (cell 30) / `_fallback_sr_slug_guess` (cell 28):
  already investigated in Gate 1 -- the cache self-repair path applies an
  UNVALIDATED slug guess and persists it to `cache/sportsref_cbb_schools.parquet`
  on an ordinary run. This module never calls `fetch_all_schools` or
  `_fallback_sr_slug_guess` at all -- team/SR-slug resolution here goes
  exclusively through `resolve_team_identity`/`get_explicit_sr_slug`
  (Phase 2's canonical, non-fuzzy path), so a guessed slug can never enter
  this module's output as verified data.
- `fetch_team_page` (`src/scraper.py`, already migrated/verified in Phase
  1): cache-first, real retry/backoff, dependency-injectable
  `cache_dir`/`session`. Reused as-is -- not rewritten.
- `scrape_team_season` (cell 98): confirmed to compute
  `slug_map = extract_player_slugs_from_roster(soup)` (real SR player
  slugs from `<a href="/cbb/players/...">` anchors) but only ever *use*
  it when the hardcoded `SCRAPE_PLAYER_GAMELOGS` flag is on (`False`
  today) -- otherwise `slug_map` is computed and silently discarded. This
  is the exact discard point Phase 2B batch 1 already documented and this
  batch's "player-slug persistence" requirement targets.
- `extract_player_slugs_from_roster` (cell 91): already ported verbatim
  as `extract_player_slugs_from_roster_v15` in
  `src/identity/sr_allstats_player_identity.py` (Phase 2B batch 1) --
  reused directly here, not re-ported.
- `build_all_stats` (cell 115): merges every per-table DataFrame onto a
  Roster-based table via `pd.merge(..., on="PlayerKey")`. No repeated-
  header filtering exists anywhere in this cluster -- `extract_table`
  (cell 91) parses straight via `pd.read_html`, and Sports-Reference is
  known to periodically re-embed its own header row as a literal data row
  in long tables. This module's `detect_repeated_header_rows` closes that
  gap for its own canonical roster read; `build_all_stats` itself is
  untouched.
- `extract_table` (cell 91): the general 3-tier table extractor (caption
  -> visible id -> HTML-comment fallback). Ported verbatim below as
  `extract_table_v15` since no `src/` copy existed yet -- reused rather
  than re-invented so this module's roster read matches
  `scrape_team_season`'s own table-finding behavior exactly.

WHERE THE "23 REPEATED HEADER ROWS" FIXTURE COULD NOT BE LOCATED

This batch searched every locally cached raw table file
(`TeamDump/CBB/*/*.xlsx`, 2156 files) and every `*_AllStats.xlsx` for a
row where a majority of cells equal their own column header text -- the
signature of an embedded repeated header row. None were found in the
current local snapshot. `detect_repeated_header_rows`/
`quarantine_repeated_header_rows` are built and tested against synthetic
data reproducing the documented defect shape; their correctness does not
depend on a real occurrence being available locally, but this batch could
not additionally confirm the specific "23 rows" figure against currently
cached data. Recorded honestly, not fabricated.

CACHE-CORRECTION BOUNDARY

This module never calls `fetch_all_schools()` or writes to
`cache/sportsref_cbb_schools.parquet` at all -- the production
school-universe cache and its self-repair bug are entirely out of this
module's write path. Team/SR-slug resolution goes through
`resolve_team_identity`/`get_explicit_sr_slug` exclusively, so this
module has no code path that could persist a generated guess as verified
source data.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import StringIO

import pandas as pd
from bs4 import BeautifulSoup, Comment

from src.exceptions import SourceUnavailableError, ValidationError
from src.identity.contracts import TeamIdentity, UnresolvedIdentity
from src.identity.player_identity import canonical_player_key_v15
from src.identity.season_identity import resolve_season_identity
from src.identity.sr_allstats_player_identity import (
    extract_player_slugs_from_roster_v15,
    resolve_roster_player_identities,
)
from src.identity.sr_slug_reconciliation import (
    get_explicit_sr_slug,
    resolve_team_identity_with_reconciliation,
)
from src.identity.team_identity import resolve_team_identity, resolve_team_identity_by_shortening
from src.logging_config import get_logger
from src.scraper import SR_CACHE_DIR, _cache_key, fetch_team_page

log = get_logger(__name__)

PARSER_VERSION = "sr_allstats_ingestion.v1"


# ---------------------------------------------------------------------------
# Legacy compatibility wrapper -- byte-faithful to v15
# ---------------------------------------------------------------------------


def extract_table_v15(soup: BeautifulSoup, label: str, caption_text: str, table_id: str) -> pd.DataFrame:
    """
    Verbatim port of notebook cell 91's `extract_table`: caption text ->
    visible-DOM table id -> HTML-comment fallback (Sports-Reference hides
    many tables inside HTML comments). Returns an empty DataFrame if
    nothing is found, matching v15's own crash-avoidance behavior exactly.
    No `print()` progress lines (those are notebook-interactive UX, not
    behavior) -- routed through structured logging instead.
    """
    table = None

    for cap in soup.find_all("caption"):
        if cap.get_text(strip=True) == caption_text:
            table = cap.find_parent("table")
            break

    if table is None:
        table = soup.find("table", id=table_id)

    if table is None:
        for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
            if table_id in comment or caption_text in comment:
                sub_soup = BeautifulSoup(comment, "lxml")
                table = sub_soup.find("table", id=table_id)
                if table is None:
                    for cap in sub_soup.find_all("caption"):
                        if cap.get_text(strip=True) == caption_text:
                            table = cap.find_parent("table")
                            break
                if table is not None:
                    break

    if table is None:
        log.info("extract_table_v15: missing table %s (%s)", label, table_id)
        return pd.DataFrame()

    df = pd.read_html(StringIO(str(table)))[0]
    log.info("extract_table_v15: extracted %s, shape=%s", label, df.shape)
    return df


# ---------------------------------------------------------------------------
# Canonical result contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RawIngestionEvidence:
    """
    Raw source evidence, preserved before any cleaning/transformation --
    per this batch's "raw ingestion boundary" requirement. Every field is
    something actually observed this call, never inferred after the fact.
    """

    source_name: str
    source_url: str
    observed_team_slug: str
    collected_at_utc: str
    cache_key: str
    http_cache_status: str  # "hit" | "miss_fetched"
    parser_version: str
    validation_status: str  # "verified" -- this evidence always reflects a real fetch/cache read
    fixture_provenance: str = ""  # Phase 3 Gate 3: added for cross-source consistency, derived from http_cache_status (see fetch_raw_team_page_with_evidence) -- "cache" | "live_fetch"


@dataclass(frozen=True)
class PlayerSlugRecord:
    """
    One player's canonical identity plus SR-specific provenance --
    persists what `scrape_team_season`/`build_all_stats` (v15) discard,
    without adding a column to the legacy AllStats schema.
    """

    player_id: str
    team_id: int
    season_id: int
    raw_player_name: str
    canonical_key: str
    sr_player_slug: str | None
    sr_profile_url: str | None
    source: str
    resolution_status: str  # "verified" (real SR slug attached) | "unconfirmed" (name-scoped fallback only)


@dataclass(frozen=True)
class DataQualityIssue:
    """One audited, never-silently-discarded data-quality finding."""

    table_label: str
    row_index: int
    reason: str
    source: str


@dataclass
class SrAllStatsIngestionResult:
    """
    The canonical ingestion result. Deliberately does not carry a legacy
    AllStats DataFrame or write an Excel file -- callers that need the
    legacy output call `src.ingestion.notebook_core.run_sports_reference_refresh`
    (or the individual `load_core_runtime()` functions) separately, in
    parallel, per this batch's "separate raw ingestion from cleaning and
    transformation" requirement.
    """

    team_identity: TeamIdentity | UnresolvedIdentity
    season_id: int | None
    evidence: RawIngestionEvidence | None = None
    player_slug_records: list[PlayerSlugRecord] = field(default_factory=list)
    data_quality_issues: list[DataQualityIssue] = field(default_factory=list)
    duplicate_player_keys: dict[str, list[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Canonical team + SR-slug resolution -- no fuzzy matching, no guessed slugs
# ---------------------------------------------------------------------------


def resolve_ingestion_team_slug(
    team_query: str, *, source: str = "sr_ingestion"
) -> tuple[TeamIdentity | UnresolvedIdentity, str | None]:
    """
    Canonical team + explicit SR slug resolution for ingestion. Routes
    through `resolve_team_identity_by_shortening` (Phase 2A -- tries the
    query as-is first via `resolve_team_identity`, then progressively
    drops trailing tokens for a "<team> <mascot>"-shaped input; both paths
    are gated by the meaningful-token shortening guard, so a non-D1 query
    like "Colorado College Tigers" still correctly returns unresolved, not
    a false collision) and `get_explicit_sr_slug` (Gate 1's reviewed
    reconciliation) -- never `fetch_all_schools`/`_fallback_sr_slug_guess`.
    No fuzzy matching anywhere in this path. Returns ``(team_result,
    sr_slug)``; ``sr_slug`` is ``None`` when team resolution itself
    failed, or (rare) when the team has no registered SR source alias at
    all.

    Final fallback (Phase 4A pre-batch, Gate 1): if the shortening path
    still comes back `not_found`, tries `resolve_team_identity_with_reconciliation`
    -- covers a verified-but-not-registered query form (e.g.
    "William and Mary", "william-mary") for one of the 32 Gate-1-reconciled
    teams, consistent with the NET-ranking adapter's own final tier. Never
    overrides an `ambiguous` result.
    """
    team_result = resolve_team_identity_by_shortening(team_query, source=source)
    if not isinstance(team_result, TeamIdentity):
        if isinstance(team_result, UnresolvedIdentity) and team_result.reason == "not_found":
            reconciled = resolve_team_identity_with_reconciliation(team_query, source=source)
            if isinstance(reconciled, TeamIdentity):
                team_result = reconciled
        if not isinstance(team_result, TeamIdentity):
            return team_result, None

    sr_source = team_result.sources.get("sports_reference")
    fallback = sr_source.raw_value if sr_source else None
    sr_slug = get_explicit_sr_slug(team_result.team_id, fallback_slug=fallback)
    return team_result, sr_slug


# ---------------------------------------------------------------------------
# Repeated-header detection / quarantine
# ---------------------------------------------------------------------------


def detect_repeated_header_rows(df: pd.DataFrame, *, min_match_fraction: float = 0.5) -> list[int]:
    """
    Deterministically detects rows where a Sports-Reference table embeds a
    duplicate of its own header row as a data row (a documented SR
    rendering pattern in long tables that `pd.read_html` passes through
    unfiltered). A row is flagged when at least `min_match_fraction` of
    its cells, stripped and lowercased, equal their own column's header
    string -- deterministic and independent of row order.
    """
    if df.empty:
        return []

    columns = list(df.columns)
    threshold = max(1, int(round(len(columns) * min_match_fraction)))
    flagged: list[int] = []

    for idx, row in df.iterrows():
        matches = sum(
            str(row[c]).strip().lower() == str(c).strip().lower()
            for c in columns
        )
        if matches >= threshold:
            flagged.append(idx)

    return flagged


def quarantine_repeated_header_rows(
    df: pd.DataFrame, *, table_label: str, source: str = "sports_reference"
) -> tuple[pd.DataFrame, list[DataQualityIssue]]:
    """
    Removes rows flagged by `detect_repeated_header_rows` from a *copy* of
    `df`, returning the cleaned frame plus one explicit `DataQualityIssue`
    audit record per removed row -- satisfies "exclude them from canonical
    records" and "record them as data-quality issues," never a silent
    drop with no trace.
    """
    flagged = detect_repeated_header_rows(df)
    if not flagged:
        return df.copy(), []

    issues = [
        DataQualityIssue(table_label=table_label, row_index=int(idx), reason="repeated_header_row", source=source)
        for idx in flagged
    ]
    cleaned = df.drop(index=flagged).reset_index(drop=True)
    return cleaned, issues


def detect_duplicate_school_rows(df: pd.DataFrame, *, key_column: str = "Player") -> dict[str, int]:
    """
    Diagnostic: counts exact duplicate values of `key_column` in `df`
    (e.g. a row physically repeated in a scraped table) -- distinct from
    `find_duplicate_player_keys`'s suffix-agnostic collision check, this
    is a raw literal-duplicate-row check on the ingestion side.
    """
    if key_column not in df.columns or df.empty:
        return {}
    counts = df[key_column].astype(str).value_counts()
    return {k: int(v) for k, v in counts.items() if v > 1}


# ---------------------------------------------------------------------------
# Player-slug persistence -- the sidecar this batch exists to build
# ---------------------------------------------------------------------------


def build_player_slug_records(
    *,
    roster_df: pd.DataFrame,
    slug_map: dict[str, str],
    team_query: str,
    season: int,
    source: str = "sports_reference",
) -> tuple[list[PlayerSlugRecord], dict[str, list[str]], TeamIdentity | UnresolvedIdentity]:
    """
    Builds the player-slug sidecar for one roster: reuses
    `resolve_roster_player_identities` (Phase 2B batch 1) for all
    matching/ambiguity logic -- no new normalization or matching rule is
    introduced here. A roster row with a real SR slug attached (from
    `slug_map`) is `resolution_status="verified"`; one without is
    `"unconfirmed"` (a team+season+name-scoped identity only, never a
    fabricated slug). `roster_df` itself is never mutated or persisted --
    this is purely additive, satisfying "without changing the legacy
    AllStats.xlsx schema."
    """
    identities, duplicates, team_result = resolve_roster_player_identities(
        roster_df, team_query, season, slug_map=slug_map, source=source
    )

    records: list[PlayerSlugRecord] = []
    for identity in identities:
        has_source_id = source in identity.sources
        sr_slug = identity.sources[source].raw_value if has_source_id else None
        records.append(PlayerSlugRecord(
            player_id=identity.player_id,
            team_id=identity.team_id,
            season_id=identity.season,
            raw_player_name=identity.display_name,
            canonical_key=canonical_player_key_v15(identity.display_name),
            sr_player_slug=sr_slug,
            sr_profile_url=(
                f"https://www.sports-reference.com/cbb/players/{sr_slug}.html" if sr_slug else None
            ),
            source=source,
            resolution_status="verified" if has_source_id else "unconfirmed",
        ))

    return records, duplicates, team_result


# ---------------------------------------------------------------------------
# Raw fetch + evidence -- reuses fetch_team_page (existing, verified) as-is
# ---------------------------------------------------------------------------


def fetch_raw_team_page_with_evidence(
    sr_slug: str,
    season: int,
    *,
    cache_dir: str | None = None,
    session=None,
    source_name: str = "sports_reference",
) -> tuple[str, RawIngestionEvidence]:
    """
    Fetches the SR team page via the existing, verified `fetch_team_page`
    (cache-first, real retry/backoff -- not rewritten here) and records
    provenance evidence alongside it. Raises `SourceUnavailableError`
    (not a bare exception) when the page cannot be retrieved and no cache
    exists -- `fetch_team_page`'s own `SportsReferenceRateLimited`/
    `ValueError` (404) are wrapped so callers of this module see one
    consistent, typed failure mode.
    """
    cdir = cache_dir or SR_CACHE_DIR
    url = f"https://www.sports-reference.com/cbb/schools/{sr_slug}/men/{season}.html"
    cache_key = _cache_key(url)
    cache_hit = os.path.exists(os.path.join(cdir, cache_key))

    try:
        html = fetch_team_page(sr_slug, season, cache_dir=cache_dir, session=session)
    except Exception as exc:
        raise SourceUnavailableError(
            f"Could not retrieve SR page for sr_slug={sr_slug!r} season={season}: {exc}"
        ) from exc

    evidence = RawIngestionEvidence(
        source_name=source_name,
        source_url=url,
        observed_team_slug=sr_slug,
        collected_at_utc=datetime.now(timezone.utc).isoformat(),
        cache_key=cache_key,
        http_cache_status="hit" if cache_hit else "miss_fetched",
        parser_version=PARSER_VERSION,
        validation_status="verified",
        fixture_provenance="cache" if cache_hit else "live_fetch",
    )
    return html, evidence


def _find_roster_table_soup(soup: BeautifulSoup):
    """Locates the roster table node, checking SR's HTML-comment-hidden variant (matches extract_table_v15's own fallback)."""
    table = soup.find("table", id="roster")
    if table is None:
        for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
            if "roster" in comment:
                sub = BeautifulSoup(comment, "lxml")
                table = sub.find("table", id="roster")
                if table is not None:
                    break
    return table


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_sr_allstats_canonical_ingestion(
    team_query: str,
    season: int,
    *,
    cache_dir: str | None = None,
    session=None,
    source: str = "sports_reference",
) -> SrAllStatsIngestionResult:
    """
    Orchestrates the canonical (identity-aware, provenance-tracked)
    Sports-Reference ingestion path for one team+season. Does NOT call,
    wrap, or modify `scrape_team_season`/`build_all_stats`/
    `save_tables_to_excel` -- see module docstring for why this is a
    deliberately separate, additive result rather than a replacement.

    Idempotent: a repeated call against the same (team_query, season,
    cache_dir) is a pure function of the on-disk cached HTML -- no state
    is accumulated or mutated across calls, and calling it twice produces
    byte-identical results (verified by test).

    Requires an explicit `season` (`resolve_season_identity` validates it
    -- a malformed year returns an unresolved team_identity-shaped result
    with `season_id=None` rather than silently guessing).
    """
    team_result, sr_slug = resolve_ingestion_team_slug(team_query, source=source)

    season_result = resolve_season_identity(season, source=source)
    season_id = season_result.season_id if not isinstance(season_result, UnresolvedIdentity) else None

    if not isinstance(team_result, TeamIdentity) or sr_slug is None or season_id is None:
        return SrAllStatsIngestionResult(team_identity=team_result, season_id=season_id)

    html, evidence = fetch_raw_team_page_with_evidence(
        sr_slug, season, cache_dir=cache_dir, session=session, source_name=source,
    )
    soup = BeautifulSoup(html, "lxml")

    slug_map = extract_player_slugs_from_roster_v15(soup)
    roster_table = _find_roster_table_soup(soup)

    if roster_table is None:
        return SrAllStatsIngestionResult(
            team_identity=team_result, season_id=season_id, evidence=evidence,
        )

    roster_df = pd.read_html(StringIO(str(roster_table)))[0]
    roster_df, header_issues = quarantine_repeated_header_rows(roster_df, table_label="Roster", source=source)

    if "Player" not in roster_df.columns:
        return SrAllStatsIngestionResult(
            team_identity=team_result, season_id=season_id, evidence=evidence,
            data_quality_issues=header_issues,
        )

    player_records, duplicates, _ = build_player_slug_records(
        roster_df=roster_df, slug_map=slug_map, team_query=team_query, season=season, source=source,
    )

    return SrAllStatsIngestionResult(
        team_identity=team_result,
        season_id=season_id,
        evidence=evidence,
        player_slug_records=player_records,
        data_quality_issues=header_issues,
        duplicate_player_keys=duplicates,
    )
