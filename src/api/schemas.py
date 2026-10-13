"""
Pydantic response contracts for the API's external boundary. Pydantic is
used here and nowhere else in `src/` -- `src/db/`, `src/identity/`,
`src/ingestion/`, and `src/services/` keep their existing `@dataclass`/ORM
conventions untouched.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["ok"]


class ReadyResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["ready"]


class ErrorResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    #: Stable, machine-readable error code (e.g. "not_ready",
    #: "validation_error", "internal_error") -- never a raw exception
    #: class name or message.
    error: str
    #: Short, human-readable, already-sanitized message safe to expose
    #: externally -- never a stack trace, SQL statement, filesystem path,
    #: or connection string.
    message: str


class TeamResponse(BaseModel):
    """Faithfully mirrors `src.services.contracts.TeamRecord` -- no field added beyond what that authoritative contract already carries."""

    model_config = ConfigDict(frozen=True)

    team_id: int
    canonical_name: str
    canonical_slug: str
    #: None when the database has no conference assignment for this team --
    #: never a fabricated default.
    conference_name: str | None = None


class PlayerStatResponse(BaseModel):
    """
    Mirrors `src.services.contracts.PlayerStatRecord` -- with exactly one
    field deliberately excluded after an explicit field-exposure review
    (Phase 6, Slice 4B): `player_key` (an internal, lowercased matching
    key -- redundant with `source_player_name`, which already carries the
    same information in its actual source-reported form).

    `validation_status` IS exposed, unchanged, as data-quality context --
    an earlier draft of this schema excluded it too, but
    `PlayerStatSnapshotRepository.list_for_player` filters only by
    `player_id`/`season_id`/`stat_table` (confirmed by direct inspection,
    not assumed): nothing anywhere enforces that only `"valid"` rows can
    have a resolved `player_id`, so a `"pending"`/`"unresolved"`/
    `"conflicting"`/`"partial"`/`"invalid"` row can genuinely reach this
    endpoint. Hiding that label would let a caller mistake
    unresolved/conflicting data for fully validated data -- worse than
    exposing an internal-sounding field name. Every raw statistic column
    and `validation_status` are exposed exactly as persisted -- no
    rounding, no renaming, no derived value, no silent filtering.
    """

    model_config = ConfigDict(frozen=True, from_attributes=True)

    stat_table: str
    season_id: int
    team_id: int
    #: Opaque `source_artifacts.id` surrogate key -- exposed for
    #: traceability only; never a filesystem path or other operational detail.
    source_artifact_id: int
    source_player_name: str
    #: One of `src.db.enums.VALIDATION_STATUSES` exactly as stored --
    #: data-quality context, never normalized or defaulted.
    validation_status: str
    source_rank: float | None = None
    source_position: str | None = None
    awards_raw: str | None = None
    games_played: int | None = None
    games_started: int | None = None
    minutes_played: float | None = None
    fg_made: float | None = None
    fg_attempted: float | None = None
    fg_pct: float | None = None
    three_p_made: float | None = None
    three_p_attempted: float | None = None
    three_p_pct: float | None = None
    two_p_made: float | None = None
    two_p_attempted: float | None = None
    two_p_pct: float | None = None
    efg_pct: float | None = None
    ft_made: float | None = None
    ft_attempted: float | None = None
    ft_pct: float | None = None
    orb: float | None = None
    drb: float | None = None
    trb: float | None = None
    ast: float | None = None
    stl: float | None = None
    blk: float | None = None
    tov: float | None = None
    pf: float | None = None
    pts: float | None = None
    off_rating: float | None = None
    def_rating: float | None = None
    per: float | None = None
    ts_pct: float | None = None
    three_par: float | None = None
    ftr: float | None = None
    pprod: float | None = None
    orb_pct: float | None = None
    drb_pct: float | None = None
    trb_pct: float | None = None
    ast_pct: float | None = None
    stl_pct: float | None = None
    blk_pct: float | None = None
    tov_pct: float | None = None
    usg_pct: float | None = None
    ows: float | None = None
    dws: float | None = None
    ws: float | None = None
    ws_per_40: float | None = None
    obpm: float | None = None
    dbpm: float | None = None
    bpm: float | None = None


class PlayerRawStatisticsResponse(BaseModel):
    """
    Mirrors `src.services.contracts.PlayerRawStatisticsResult` for the
    `outcome="found"` case only -- an unknown player_id is translated to a
    404 before this model is ever constructed, so `player_id`/`display_name`/
    `team_id`/`season_id` are declared non-optional here (stricter than the
    internal dataclass's own defensive `| None` typing, which exists only
    to represent the not-found placeholder that never reaches this schema).
    `stats` preserves the service's own deterministic ordering exactly.
    """

    model_config = ConfigDict(frozen=True)

    player_id: str
    display_name: str
    team_id: int
    season_id: int
    stats: list[PlayerStatResponse] = []


class RankingResponse(BaseModel):
    """
    Mirrors `src.services.contracts.RankingRecord` field-for-field -- no
    field added, none renamed. `season` is server-configured context
    (`NAPSEDGE_NET_RANKINGS_SEASON`), preserved here exactly as supplied;
    it is **not** a claim that this ranking is current -- the cache
    carries no as-of/freshness metadata, so no such field exists here.
    """

    model_config = ConfigDict(frozen=True)

    team_id: int
    net_rank: int
    raw_team_key: str
    season: int


class RankingConflictEntryResponse(BaseModel):
    """One of two-or-more raw cache rows that produced a different rank for the same team -- see RankingConflictResponse."""

    model_config = ConfigDict(frozen=True)

    raw_team_key: str
    net_rank: int


class RankingConflictResponse(BaseModel):
    """
    Backs the 409 response for `GET /v1/rankings/net/teams/{team_id}` when
    `RankingLookupResult.outcome == "conflicting"` -- e.g. the real cached
    fixture's own "louisiana-state" rows, ranks 77 and 245. Preserves
    every contributing record exactly as the service computed them;
    neither this schema nor the route that builds it selects one value.
    """

    model_config = ConfigDict(frozen=True)

    error: Literal["conflict"] = "conflict"
    message: str
    team_id: int
    conflicting_records: list[RankingConflictEntryResponse]


class RankingSourceRowResponse(BaseModel):
    """
    One row from the local cache exactly as the existing
    `src.ingestion.net_ranking_ingestion` ingestion result classified it.

    Team resolution and rank validity are independent, truthfully-preserved
    dimensions -- exactly as `NetRankingRecord` itself already tracks them
    (never conflated): `team_id` is populated only when the row's raw team
    key resolved to a canonical `TeamIdentity` (None for "not_found"/
    "ambiguous"); `net_rank` is populated whenever the raw rank value
    itself parsed as a valid positive integer, *regardless of* whether the
    team resolved (None only for `resolution_status == "invalid_rank"`).
    A row can therefore have a resolved `net_rank` with `team_id=None`
    (an unresolved team with an otherwise-valid rank), or a resolved
    `team_id` with `net_rank=None` (a resolved team with a malformed
    rank) -- neither is fabricated into the other.
    """

    model_config = ConfigDict(frozen=True)

    raw_team_key: str
    #: One of "resolved" | "not_found" | "ambiguous" | "invalid_rank" --
    #: preserved exactly as `src.ingestion.net_ranking_ingestion.NetRankingRecord.resolution_status`
    #: reports it, never recalculated or renamed.
    resolution_status: str
    team_id: int | None = None
    net_rank: int | None = None


class RankingsListResponse(BaseModel):
    """
    Backs `GET /v1/rankings/net`. `rows` includes every source row --
    resolved and unresolved alike -- so the response never silently
    implies that all rows resolved successfully; a genuinely conflicting
    raw key (e.g. "louisiana-state") appears twice, each with its own
    rank, exactly as the cache itself contains it. Bounded to the one
    configured local snapshot (currently 365 rows) -- no pagination is
    added, since this fixed-size local file cannot grow without a
    separate, explicitly-approved cache-format change.
    """

    model_config = ConfigDict(frozen=True)

    #: Server-configured context (`NAPSEDGE_NET_RANKINGS_SEASON`) -- not a
    #: claim of current/latest data.
    season: int
    total_rows: int
    rows: list[RankingSourceRowResponse]


class InjuryEntryResponse(BaseModel):
    """
    Mirrors `src.services.contracts.InjuryEntry` -- with one field
    deliberately excluded after an explicit field-exposure review (Phase
    6, Slice 6B): `raw` (the complete original, unvalidated source dict,
    kept internally only so no source-provided field is silently
    discarded before this schema exists -- an unbounded, untyped blob is
    never a suitable external HTTP contract field on its own). `player`,
    `status`, `detail` are copied verbatim -- `status` is the source's
    own free-text designation (e.g. "out", "doubtful", "questionable", or
    any other string the source used), never normalized or used to infer
    medical severity, playing probability, or a return date. `None`
    serializes as JSON `null`; an authoritative empty string (if the
    source ever supplies one) is preserved as `""`, never coerced to
    `null`.
    """

    model_config = ConfigDict(frozen=True)

    player: str | None = None
    status: str | None = None
    detail: str | None = None


class TeamInjuryReportResponse(BaseModel):
    """
    Backs `GET /v1/teams/{team_id}/injuries` for the loaded case (200)
    only -- `outcome="not_loaded"` is translated to a sanitized 503
    before this model is ever constructed (see `src.api.routes.injury`),
    so this schema itself never needs to represent "unavailable"; a
    successfully-loaded, zero-injury report is fully expressible here as
    `injuries: []`, never confused with a 503.

    Mirrors `src.services.contracts.TeamInjuryReport` except: `outcome`
    is dropped (translated into the HTTP status code itself, exactly
    like Rankings' own `RankingLookupResult.outcome`), `team_slug` and
    `source_path` are dropped (never expose an internal identifier or a
    filesystem path -- mirrors the cache-path-never-exposed precedent
    already established for Rankings, Phase 6, Slice 5B), and `detail`
    is dropped (internal-only diagnostic, never sent to a client, and
    moot here since it is only ever populated for the excluded
    `"not_loaded"` outcome). `team_id` is added for HTTP presentation
    only -- sourced from the resolved canonical Team lookup, not from
    `TeamInjuryReport` itself, matching the existing convention of
    echoing the requested identifier in the response body (e.g.
    `RankingResponse.team_id`, `PlayerRawStatisticsResponse.player_id`).
    `impactful_count` is exposed unchanged -- an aggregate the
    authoritative function itself already computes, not a new inference
    added at this layer.
    """

    model_config = ConfigDict(frozen=True)

    team_id: int
    injuries: list[InjuryEntryResponse]
    impactful_count: int


class TeamFeatureValuesRequest(BaseModel):
    """
    Explicit, bounded request payload mirroring exactly the seven
    authoritative Matchup comparison features
    (`src.features.matchup_features.MATCHUP_FEATURE_LABELS`) -- no
    arbitrary/dynamic feature dict is accepted (`extra="forbid"`
    rejects any unknown key with a 422). Every field is optional; an
    omitted field and a field explicitly set to `null` are treated
    identically by the underlying service -- confirmed by direct
    inspection: `build_matchup_feature_comparison` reads each value via
    `.get(key)` on the caller-supplied dict, which returns `None` for
    both an absent key and a key explicitly mapped to `None`. Fields are
    `strict=True` floats (rejects a JSON boolean, which Python/Pydantic
    would otherwise treat as an int subtype) and `allow_inf_nan=False`
    at the model level rejects `NaN`/`Infinity`/`-Infinity`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    avg_margin: float | None = Field(default=None, strict=True)
    recent_avg_margin: float | None = Field(default=None, strict=True)
    avg_net_rtg: float | None = Field(default=None, strict=True)
    recent_avg_net_rtg: float | None = Field(default=None, strict=True)
    player_value_index: float | None = Field(default=None, strict=True)
    off_impact: float | None = Field(default=None, strict=True)
    def_impact: float | None = Field(default=None, strict=True)


class MatchupFeatureComparisonRequest(BaseModel):
    """
    Backs `POST /v1/matchups/feature-comparison`. `team_a_slug`/
    `team_b_slug` are **caller-supplied descriptive labels only** --
    exactly the same untyped strings `src.services.matchup.compare_team_matchup_features`
    itself accepts (it performs no Team identity resolution and no
    database lookup of its own; a canonical `team_id` is not required or
    validated here, matching the Team/Player/Rankings/Injury precedent
    of never re-deriving identity a caller already has, and matching
    what this service was actually built to accept -- not a value this
    slice invents a new "canonical ID" meaning for).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    team_a_slug: str = Field(..., min_length=1, max_length=100)
    team_b_slug: str = Field(..., min_length=1, max_length=100)
    team_a_features: TeamFeatureValuesRequest
    team_b_features: TeamFeatureValuesRequest


class FeatureComparisonEntryResponse(BaseModel):
    """Mirrors `src.services.contracts.FeatureComparisonEntry` field-for-field -- no field added, none renamed, none excluded."""

    model_config = ConfigDict(frozen=True)

    signal: str
    label: str
    leader: str
    delta: float


class MatchupFeatureComparisonResponse(BaseModel):
    """
    Backs `POST /v1/matchups/feature-comparison`. Mirrors
    `src.services.contracts.MatchupFeatureComparisonResult`
    field-for-field -- no field added, none renamed, none excluded.
    `deltas` preserves every one of the 7 fixed comparison keys,
    including `None` for any unavailable value -- never fabricated as
    zero or "even". `key_edges` is empty when nothing was comparable
    (e.g. both teams' feature payloads are fully unavailable) -- a
    truthful, valid 200 outcome, never converted into an error. No
    prediction, winner, edge, confidence, or advice field exists here.
    """

    model_config = ConfigDict(frozen=True)

    team_a_slug: str
    team_b_slug: str
    deltas: dict[str, float | None]
    key_edges: list[FeatureComparisonEntryResponse]
