"""
The Odds API canonical ingestion (Phase 3, batch 4).

ACTIVE V15 DEFINITIONS AND CALL SITES

`get_live_ncaab_odds()` (cell 448) is the only real retrieval function --
hits `https://api.the-odds-api.com/v4/sports/basketball_ncaab/odds` with
`ODDS_API_KEY = os.getenv("ODDS_API_KEY")` (an env var, never a hardcoded
credential; not read, logged, or otherwise touched by this module) and
`bookmakers="fanduel"` as its only real default (confirmed, same finding
as the sportsbook-identity Gate 2 batch). Returns `res.json()` directly --
the real Odds API v4 response shape, confirmed independently by reading
`build_market_consensus` (cell 487, still unmigrated -- a downstream
consensus calculation, not touched by this batch):
``game_odds["bookmakers"][i]["markets"][j]["outcomes"][k]["point"]`` --
i.e. one event dict has a `bookmakers` list, each bookmaker has a
`markets` list, each market has an `outcomes` list. `find_game_odds`
(cell 473) is the real, heavily-used team-matching entry point over a
*list* of such event dicts (`g.get("away_team", "")`/`g.get("home_team", "")`)
-- already fully ported as `find_game_odds_via_registry`/
`resolve_odds_team_identity` in `src/identity/odds_identity.py` (Phase 2A),
reused here unmodified. `normalize_for_odds` (shadowed 3x) and
`canonical_school` (one incidental call site) are dead/compatibility-only,
already documented in `odds_identity.py`'s own docstring -- not
re-investigated this batch.

SOURCE-ID FINDINGS

The Odds API's real event `id` field is genuinely present in the schema
(confirmed by `game_identity.py`'s own Gate-2-era finding: "plausible,
not confirmed" against a live payload in this codebase). Per this batch's
explicit instruction #7 ("an Odds API event ID must remain source-specific
or provisional unless existing Phase 2 rules independently establish it
as authoritative"), this module does **not** promote it to a trusted
canonical `game_id` -- `source_event_id` is preserved verbatim on every
record as its own evidence field, while the canonical `game_identity` is
always built via `build_provisional_game_identity` (canonical `team_id`s
+ season + commence date only, `is_provisional=True`). A future batch
that independently verifies the Odds API id against a live payload could
then use `build_game_identity_from_source` instead -- not done here.
Bookmaker `key`/`title` are always present in the real schema (The Odds
API's own documented shape); no bookmaker id beyond the `key` string
exists to preserve or fabricate.

DESIGN

Mirrors the SR/ESPN/Covers ingestion modules' pattern exactly: request
construction and retrieval (`get_live_ncaab_odds`, untouched, not called
by this module -- it takes already-fetched JSON as input, matching "do
not perform live requests") are separate from parsing
(`parse_odds_event_outcomes`), team resolution (`resolve_odds_team_identity`,
Phase 2A, reused unmodified), sportsbook resolution
(`resolve_sportsbook_identity`, Gate 2, reused unmodified), provisional
game resolution (`build_provisional_game_identity`, Gate 2, reused
unmodified), and canonical record construction. No consensus, best-line,
implied-probability, edge, or recommendation calculation exists anywhere
in this module (confirmed via AST test) -- that remains
`build_market_consensus`/downstream betting-engine territory, untouched.

Season is a required parameter, never inferred from `commence_time` --
consistent with `resolve_season_identity`'s own "do not silently infer a
season" rule. Neutral-site status defaults to `False` (The Odds API's
real schema has no neutral-site field) -- never guessed; a caller with
independent knowledge of a neutral-site game may pass `neutral_site=True`
explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.identity.contracts import (
    GameIdentity,
    SportsbookIdentity,
    TeamIdentity,
    UnresolvedIdentity,
)
from src.identity.game_identity import build_provisional_game_identity
from src.identity.odds_identity import resolve_odds_team_identity
from src.identity.sportsbook_identity import resolve_sportsbook_identity
from src.logging_config import get_logger

log = get_logger(__name__)

PARSER_VERSION = "odds_ingestion.v1"
DEFAULT_SOURCE_ENDPOINT = "https://api.the-odds-api.com/v4/sports/basketball_ncaab/odds"


# ---------------------------------------------------------------------------
# Canonical result contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OddsRawEvidence:
    """Raw source evidence, preserved before any cleaning/transformation."""

    source_name: str
    source_endpoint: str
    fixture_provenance: str  # "json" | "mocked_fixture:<label>"
    collected_at_utc: str
    parser_version: str


@dataclass(frozen=True)
class OddsOutcomeRecord:
    """One (event, bookmaker, market, outcome) row's raw evidence plus canonical identity attachment."""

    source_event_id: str | None
    sport_key: str | None
    commence_time_raw: str | None
    raw_home_team: str | None
    raw_away_team: str | None
    home_team_identity: TeamIdentity | UnresolvedIdentity
    away_team_identity: TeamIdentity | UnresolvedIdentity
    game_identity: GameIdentity | None  # None only when team resolution failed on either side
    season: int
    bookmaker_key_raw: str | None
    bookmaker_title_raw: str | None
    bookmaker_last_update: str | None
    bookmaker_identity: SportsbookIdentity | UnresolvedIdentity
    market_key: str | None
    outcome_name: str | None
    price: float | None
    point: float | None
    resolution_status: str  # "resolved" | "team_unresolved"


@dataclass
class OddsIngestionResult:
    """
    The canonical Odds API ingestion result. Carries no legacy
    `list[dict]`/consensus shape -- existing callers of `get_live_ncaab_odds`/
    `find_game_odds`/`build_market_consensus` are unaffected; this is a
    separate, additive result.
    """

    evidence: OddsRawEvidence
    records: list[OddsOutcomeRecord] = field(default_factory=list)
    duplicate_rows: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Raw JSON parsing -- assumes nothing about bookmaker/market completeness
# ---------------------------------------------------------------------------


def parse_odds_event_outcomes(event: dict) -> list[dict]:
    """
    Parses one Odds API v4 event object into a flat list of one raw dict
    per (bookmaker, market, outcome) combination. Confirmed field access
    against `build_market_consensus`'s own real usage
    (`bookmakers[i]["markets"][j]["outcomes"][k]["point"]`). Assumes
    nothing about completeness: a missing/empty `bookmakers`, `markets`,
    or `outcomes` list simply yields zero rows for that branch, never a
    crash or a fabricated placeholder. `point` is preserved as `None`
    when absent (real for moneyline/`h2h` outcomes) rather than defaulted
    to a number.
    """
    event_id = event.get("id")
    sport_key = event.get("sport_key")
    commence_time = event.get("commence_time")
    home_team = event.get("home_team")
    away_team = event.get("away_team")

    rows = []
    for book in event.get("bookmakers") or []:
        book_key = book.get("key")
        book_title = book.get("title")
        book_update = book.get("last_update")
        for market in book.get("markets") or []:
            market_key = market.get("key")
            for outcome in market.get("outcomes") or []:
                rows.append({
                    "source_event_id": event_id,
                    "sport_key": sport_key,
                    "commence_time": commence_time,
                    "raw_home_team": home_team,
                    "raw_away_team": away_team,
                    "bookmaker_key": book_key,
                    "bookmaker_title": book_title,
                    "bookmaker_last_update": book_update,
                    "market_key": market_key,
                    "outcome_name": outcome.get("name"),
                    "price": outcome.get("price"),
                    "point": outcome.get("point"),
                })
    return rows


def parse_commence_time_utc(commence_time: str | None) -> datetime | None:
    """
    Parses the Odds API's ISO8601 UTC `commence_time` string (e.g.
    ``"2026-01-15T00:00:00Z"``) into a timezone-aware UTC `datetime`.
    Returns `None` for missing/malformed input rather than raising --
    never fabricates a date.
    """
    if not commence_time or not isinstance(commence_time, str):
        return None
    try:
        return datetime.fromisoformat(commence_time.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Canonical identity resolution per row
# ---------------------------------------------------------------------------


def resolve_odds_outcome_row(
    row: dict,
    *,
    season: int,
    neutral_site: bool = False,
    source: str = "odds",
) -> OddsOutcomeRecord:
    """
    Resolves one parsed outcome row to canonical team, sportsbook, and
    provisional game identity. Team resolution never falls back to
    string similarity (`resolve_odds_team_identity` has no fuzzy step --
    see `odds_identity.py`). Sportsbook resolution never infers one brand
    from another (`resolve_sportsbook_identity`, exact/normalized-key
    match only). Home/away orientation is preserved exactly as the source
    provided it -- never swapped or inferred.
    """
    home_result = resolve_odds_team_identity(row.get("raw_home_team") or "", source=source)
    away_result = resolve_odds_team_identity(row.get("raw_away_team") or "", source=source)

    game_identity: GameIdentity | None = None
    resolution_status = "team_unresolved"

    if isinstance(home_result, TeamIdentity) and isinstance(away_result, TeamIdentity):
        commence_dt = parse_commence_time_utc(row.get("commence_time"))
        game_date = commence_dt.strftime("%Y-%m-%d") if commence_dt else (row.get("commence_time") or "")
        built = build_provisional_game_identity(
            home_team_id=home_result.team_id, away_team_id=away_result.team_id,
            season=season, game_date=game_date, neutral_site=neutral_site,
        )
        if isinstance(built, GameIdentity):
            game_identity = built
            resolution_status = "resolved"

    bookmaker_key = row.get("bookmaker_key") or ""
    bookmaker_result = resolve_sportsbook_identity(bookmaker_key, source=source)

    return OddsOutcomeRecord(
        source_event_id=row.get("source_event_id"),
        sport_key=row.get("sport_key"),
        commence_time_raw=row.get("commence_time"),
        raw_home_team=row.get("raw_home_team"),
        raw_away_team=row.get("raw_away_team"),
        home_team_identity=home_result,
        away_team_identity=away_result,
        game_identity=game_identity,
        season=season,
        bookmaker_key_raw=row.get("bookmaker_key"),
        bookmaker_title_raw=row.get("bookmaker_title"),
        bookmaker_last_update=row.get("bookmaker_last_update"),
        bookmaker_identity=bookmaker_result,
        market_key=row.get("market_key"),
        outcome_name=row.get("outcome_name"),
        price=row.get("price"),
        point=row.get("point"),
        resolution_status=resolution_status,
    )


# ---------------------------------------------------------------------------
# Conflict detection -- never resolved silently
# ---------------------------------------------------------------------------


def find_duplicate_odds_rows(records: list[OddsOutcomeRecord]) -> list[dict]:
    """
    Detects a repeated (event, bookmaker, market, outcome) combination --
    e.g. the same event appearing twice in one payload, or a bookmaker
    listing the same outcome twice. Never merges or discards a duplicate;
    only reports it.
    """
    seen: dict[tuple, int] = {}
    for r in records:
        key = (r.source_event_id, r.bookmaker_key_raw, r.market_key, r.outcome_name, r.point)
        seen[key] = seen.get(key, 0) + 1

    return [
        {"source_event_id": k[0], "bookmaker_key": k[1], "market_key": k[2], "outcome_name": k[3], "point": k[4], "count": v}
        for k, v in seen.items() if v > 1
    ]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_odds_ingestion_from_json(
    events: list[dict],
    *,
    season: int,
    neutral_site: bool = False,
    source_endpoint: str = DEFAULT_SOURCE_ENDPOINT,
    fixture_provenance: str = "json",
    source: str = "odds",
) -> OddsIngestionResult:
    """
    Orchestrates canonical Odds API ingestion from already-retrieved JSON
    (a list of Odds API v4 event objects, `get_live_ncaab_odds()`'s own
    real return shape) -- never performs a live request itself, never
    reads `ODDS_API_KEY`. Deterministic and idempotent: a repeated call
    against the same `events` list produces byte-identical records
    (verified by test).
    """
    evidence = OddsRawEvidence(
        source_name=source,
        source_endpoint=source_endpoint,
        fixture_provenance=fixture_provenance,
        collected_at_utc=datetime.now(timezone.utc).isoformat(),
        parser_version=PARSER_VERSION,
    )

    records = []
    for event in events or []:
        for row in parse_odds_event_outcomes(event):
            records.append(resolve_odds_outcome_row(row, season=season, neutral_site=neutral_site, source=source))

    duplicates = find_duplicate_odds_rows(records)

    return OddsIngestionResult(evidence=evidence, records=records, duplicate_rows=duplicates)
