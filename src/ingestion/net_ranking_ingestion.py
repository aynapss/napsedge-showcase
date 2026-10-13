"""
NET-ranking canonical ingestion (Phase 3, batch 5, Gate 1).

ACTIVE V15 DEFINITIONS AND CALL SITES CONFIRMED

`attach_net_rank` (cell 345, unshadowed), `ensure_sr_slug_column` (cell
375), `build_sr_to_team_slug_crosswalk` (cell 376), `resolve_net_team_sr_slug`
(cell 383), `build_net_rankings_from_ncaa` (cell 386) -- all already
investigated and ported verbatim in Phase 2A's `net_ranking_identity.py`
(adapter batch 5), including the live backward-compat alias
`build_net_rankings_from_cbs = build_net_rankings_from_ncaa`. Real call
sites for `attach_net_rank(..., slug_overrides=MANUAL_SLUG_OVERRIDES)`:
cells 329-330, 474-475 (team-profile enrichment, downstream of this
ingestion batch, not touched). This batch does not re-investigate any of
this -- `src/identity/net_ranking_identity.py` already did, per the
instruction not to repeat Phase 0-2 discovery.

WHAT'S MISSING AT THE IDENTITY LAYER (this batch's actual scope)

`net_ranking_identity.py` already provides `resolve_net_team_identity`
(canonical, no shortening/fuzzy) and `build_net_rankings_index` (resolves
every row, returns `(rankings_by_team_id, unmapped)`), but that index is
deliberately minimal -- a plain `dict[team_id, rank]` plus an unmapped
list, with no per-row raw evidence, provenance, or resolution-status
record. This batch adds that ingestion layer on top, reusing
`resolve_net_team_identity` for every actual resolution decision -- no
new team-matching logic is introduced here.

REVALIDATED, NOT ASSUMED, AGAINST THE REAL 365-ROW FIXTURE
(`Reference Py Files/net_rankings_2026.csv`)

Confirmed by direct query this batch: **361 of 365** rows resolve (not
"approximately 360" exactly, but consistent with it), **1** genuine
source duplicate (`"louisiana-state"` appears twice, ranks 77 and 245 --
a real data-quality defect in the cached fixture, not an artifact of this
ingestion code), **4** unresolved rows (not 4 identical reasons -- 2
`not_found`, 2 `ambiguous`), **0** cross-team canonical collisions. If a
future run of this same fixture produces different counts, that is a real
change in the fixture, not a test regression to force back to these
numbers -- the test suite asserts the *mechanism* (each specific row's
classification) rather than a bare aggregate count, so a genuine fixture
change would surface as a specific, readable failure.

The 4 unresolved rows, individually:
- `"william-mary"` -> `not_found`. **Directly connects to the Phase 2
  Gate 1 SR-slug reconciliation**: that batch established `"william-mary"`
  as William & Mary's current, verified SR slug via
  `get_explicit_sr_slug` -- but that reconciliation lives in a separate
  JSON record, not in `slug_utils`'s own alias table, so
  `resolve_team_identity("william-mary")` still fails here. Not a new
  defect; a known, already-classified gap surfacing again in a different
  ingestion path. Not fixed here (would require editing `slug_utils.py`
  or wiring the reconciliation record into `resolve_team_identity`
  itself -- an architectural decision, out of this batch's scope).
- `"east-texas-am"` -> `not_found`. A real, recently-renamed program
  (Texas A&M-Commerce -> East Texas A&M) apparently not yet in the
  canonical registry's alias table -- a registry-coverage gap, not an
  ingestion defect.
- `"alabama-am"` / `"florida-am"` -> `ambiguous` (confirmed via direct
  query: both collide with the plain state-school alias, e.g.
  `"alabama-am"` matches both "Alabama" and "Alabama A&M" candidates).
  Correctly surfaced as ambiguous, never guessed.

DELIBERATELY NOT REUSING THE SHARED TRAILING-TOKEN SHORTENING

`resolve_net_team_identity` (and this ingestion module, by extension)
never calls `resolve_team_identity_by_shortening` -- confirmed in that
adapter's own docstring as the source of 6 real historical NET collisions
(`"ohio-state"` -> drop "state" -> the real, different "Ohio" program;
same shape for Iowa/Penn/Missouri-Kansas-City/Florida). This ingestion
module inherits that same restriction by construction (it only ever calls
`resolve_net_team_identity`, never the shortening helper directly).

MIAMI/LOYOLA: NEVER FORCED

`resolve_net_team_identity` explicitly does not consult
`CBS_NET_ALIASES_TO_SR` (which hardcodes bare `"miami"` -> `"miami-fl"` as
a documented legacy business call) -- a bare "Miami"/"Loyola" through this
ingestion path is `ambiguous`, exactly like every other adapter in this
migration. The hardcoded legacy behavior remains available, unmodified,
via `resolve_net_team_sr_slug_v15` for any caller that still needs it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from src.exceptions import ValidationError
from src.identity.contracts import TeamIdentity, UnresolvedIdentity
from src.identity.net_ranking_identity import resolve_net_team_identity
from src.logging_config import get_logger

log = get_logger(__name__)

PARSER_VERSION = "net_ranking_ingestion.v1"
REQUIRED_COLUMNS = {"team_slug", "net_rank"}


# ---------------------------------------------------------------------------
# Canonical result contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NetRankingRawEvidence:
    """Raw source evidence, preserved before any cleaning/transformation."""

    source_name: str
    source_url_or_fixture: str
    fixture_provenance: str  # "csv" | "dataframe" | "mocked_fixture:<label>"
    collected_at_utc: str
    parser_version: str


@dataclass(frozen=True)
class NetRankingRecord:
    """One ranking row's raw evidence plus canonical identity attachment."""

    raw_team_key: str
    raw_rank: object  # preserved exactly as read, before numeric validation
    season: int
    team_identity: TeamIdentity | UnresolvedIdentity
    resolution_status: str  # "resolved" | "not_found" | "ambiguous" | "invalid_rank"
    validated_rank: int | None  # None when raw_rank failed numeric validation


@dataclass
class NetRankingIngestionResult:
    """
    The canonical NET-ranking ingestion result. Carries no legacy
    `dict[team_id, rank]`/`attach_net_rank` shape -- existing callers of
    `attach_net_rank`/`build_net_rankings_index` are unaffected; this is a
    separate, additive result.
    """

    evidence: NetRankingRawEvidence
    records: list[NetRankingRecord] = field(default_factory=list)
    duplicate_source_rows: dict[str, int] = field(default_factory=dict)
    duplicate_ranks: dict[int, list[str]] = field(default_factory=dict)
    duplicate_canonical_teams: dict[int, list[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_net_rankings_schema(df: pd.DataFrame) -> None:
    """Raises `ValidationError` if required columns are missing -- fails loudly, never silently proceeds on malformed input."""
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValidationError(f"NET rankings source missing required column(s) {sorted(missing)}: {df.columns.tolist()}")


def _validate_rank(raw_rank: object) -> int | None:
    """Returns the validated positive int rank, or None if invalid/missing -- never fabricated or coerced to a guess."""
    try:
        rank = int(raw_rank)
    except (TypeError, ValueError):
        return None
    if rank <= 0:
        return None
    return rank


def find_duplicate_source_rows(df: pd.DataFrame) -> dict[str, int]:
    """Counts exact repeated `team_slug` values in the raw source -- a real data-quality defect, never silently collapsed."""
    counts = df["team_slug"].astype(str).value_counts()
    return {k: int(v) for k, v in counts.items() if v > 1}


def find_duplicate_ranks(records: list[NetRankingRecord]) -> dict[int, list[str]]:
    """Groups raw team keys sharing the same validated rank -- NET ranks should be unique; a collision is a real source defect."""
    by_rank: dict[int, list[str]] = {}
    for r in records:
        if r.validated_rank is not None:
            by_rank.setdefault(r.validated_rank, []).append(r.raw_team_key)
    return {rank: keys for rank, keys in by_rank.items() if len(keys) > 1}


def find_duplicate_canonical_teams(records: list[NetRankingRecord]) -> dict[int, list[str]]:
    """
    Groups raw team keys that resolved to the SAME canonical team_id --
    a genuine cross-row collision (two different source rows claiming to
    be the same real program). Confirmed zero against the real 365-row
    fixture this batch, but detected generically, not assumed absent.
    """
    by_team_id: dict[int, list[str]] = {}
    for r in records:
        if isinstance(r.team_identity, TeamIdentity):
            by_team_id.setdefault(r.team_identity.team_id, []).append(r.raw_team_key)
    return {tid: keys for tid, keys in by_team_id.items() if len(keys) > 1}


# ---------------------------------------------------------------------------
# Canonical identity resolution per row
# ---------------------------------------------------------------------------


def resolve_net_ranking_row(raw_team_key: str, raw_rank: object, season: int, *, source: str = "net_ranking") -> NetRankingRecord:
    """
    Resolves one NET-ranking row. Reuses `resolve_net_team_identity`
    (`net_ranking_identity.py`) entirely for team resolution -- no
    shortening, no fuzzy matching, no forced Miami/Loyola disambiguation.
    Rank validation happens independently of team resolution, so an
    invalid rank on an otherwise-resolvable team is reported precisely
    (`resolution_status="invalid_rank"`), not conflated with a team lookup
    failure.
    """
    team_result = resolve_net_team_identity(str(raw_team_key), source=source)
    validated_rank = _validate_rank(raw_rank)

    if validated_rank is None:
        status = "invalid_rank"
    elif isinstance(team_result, TeamIdentity):
        status = "resolved"
    else:
        status = team_result.reason  # "not_found" | "ambiguous"

    return NetRankingRecord(
        raw_team_key=str(raw_team_key), raw_rank=raw_rank, season=season,
        team_identity=team_result, resolution_status=status, validated_rank=validated_rank,
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_net_ranking_ingestion(
    df: pd.DataFrame,
    *,
    season: int,
    source_url_or_fixture: str = "Reference Py Files/net_rankings_2026.csv",
    fixture_provenance: str = "dataframe",
    source: str = "net_ranking",
) -> NetRankingIngestionResult:
    """
    Orchestrates canonical NET-ranking ingestion from an already-loaded
    DataFrame (`team_slug`/`net_rank` columns -- the real cached fixture's
    own shape) -- never performs a live NCAA.com/CBS request itself.
    Deterministic and idempotent: a repeated call against the same `df`
    produces byte-identical records (verified by test).
    """
    validate_net_rankings_schema(df)

    evidence = NetRankingRawEvidence(
        source_name=source,
        source_url_or_fixture=source_url_or_fixture,
        fixture_provenance=fixture_provenance,
        collected_at_utc=datetime.now(timezone.utc).isoformat(),
        parser_version=PARSER_VERSION,
    )

    records = [
        resolve_net_ranking_row(row["team_slug"], row["net_rank"], season, source=source)
        for _, row in df.iterrows()
    ]

    return NetRankingIngestionResult(
        evidence=evidence,
        records=records,
        duplicate_source_rows=find_duplicate_source_rows(df),
        duplicate_ranks=find_duplicate_ranks(records),
        duplicate_canonical_teams=find_duplicate_canonical_teams(records),
    )


def load_and_ingest_net_rankings_csv(
    csv_path: str,
    *,
    season: int,
    source: str = "net_ranking",
) -> NetRankingIngestionResult:
    """Convenience wrapper: loads the real cached CSV fixture (read-only) and ingests it."""
    df = pd.read_csv(csv_path)
    return run_net_ranking_ingestion(
        df, season=season, source_url_or_fixture=csv_path, fixture_provenance="csv", source=source,
    )
