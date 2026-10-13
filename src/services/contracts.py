"""
Shared typed result contracts for the service layer.

Services never return a raw dict or a Pydantic model -- Pydantic is
reserved for the API's external boundary (`src/api/schemas.py`); these are
plain `@dataclass` types, matching the convention already used throughout
`src/identity/`, `src/ingestion/`, and `src/pipeline/live_data_adapter.py`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping


@dataclass(frozen=True)
class HealthResult:
    ok: bool


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    #: A short, non-sensitive reason when `ready` is False (e.g. "database
    #: unavailable", "migration state does not match expected head") --
    #: never a raw exception message, connection string, or stack trace.
    detail: str | None = None


@dataclass(frozen=True)
class TeamRecord:
    """One canonical, persisted team -- built only from `src.db.models.Team`/`Conference` rows, never from a source-directory count."""

    team_id: int
    canonical_name: str
    canonical_slug: str
    #: None when the team has no conference assignment in the database --
    #: never fabricated.
    conference_name: str | None = None


@dataclass(frozen=True)
class TeamLookupResult:
    outcome: Literal["found", "not_found"]
    team: TeamRecord | None = None
    #: Internal-only diagnostic (e.g. "unresolved", "resolved_but_missing_row")
    #: distinguishing *why* outcome is "not_found" -- never sent to a client.
    detail: str | None = None


@dataclass(frozen=True)
class PlayerStatRecord:
    """
    One persisted `src.db.allstats_models.PlayerStatSnapshot` row, exactly
    as stored -- every field is a direct 1:1 mirror of a real column, never
    a calculation. `stat_table` is one of `src.db.enums.ALLSTATS_STAT_TABLES`
    ("PerGame"/"Totals"/"Per40"/"Per100"/"Advanced"); a `None` field means
    the source genuinely never reported that value for this stat_table row
    (e.g. `usg_pct` is always `None` on a "PerGame" row) -- never a
    fabricated zero or default.
    """

    stat_table: str
    season_id: int
    team_id: int
    source_artifact_id: int
    player_key: str
    source_player_name: str
    source_rank: float | None
    source_position: str | None
    awards_raw: str | None
    games_played: int | None
    games_started: int | None
    minutes_played: float | None
    fg_made: float | None
    fg_attempted: float | None
    fg_pct: float | None
    three_p_made: float | None
    three_p_attempted: float | None
    three_p_pct: float | None
    two_p_made: float | None
    two_p_attempted: float | None
    two_p_pct: float | None
    efg_pct: float | None
    ft_made: float | None
    ft_attempted: float | None
    ft_pct: float | None
    orb: float | None
    drb: float | None
    trb: float | None
    ast: float | None
    stl: float | None
    blk: float | None
    tov: float | None
    pf: float | None
    pts: float | None
    off_rating: float | None
    def_rating: float | None
    per: float | None
    ts_pct: float | None
    three_par: float | None
    ftr: float | None
    pprod: float | None
    orb_pct: float | None
    drb_pct: float | None
    trb_pct: float | None
    ast_pct: float | None
    stl_pct: float | None
    blk_pct: float | None
    tov_pct: float | None
    usg_pct: float | None
    ows: float | None
    dws: float | None
    ws: float | None
    ws_per_40: float | None
    obpm: float | None
    dbpm: float | None
    bpm: float | None
    validation_status: str


@dataclass(frozen=True)
class PlayerRawStatisticsResult:
    outcome: Literal["found", "not_found"]
    player_id: str | None = None
    display_name: str | None = None
    #: The player's own canonical (team_id, season_id) from `src.db.models.Player`
    #: -- distinct from each `PlayerStatRecord`'s own team_id/season_id, which
    #: reflects that specific persisted snapshot row.
    team_id: int | None = None
    season_id: int | None = None
    #: Empty on a known player with no qualifying statistics -- never
    #: conflated with `outcome="not_found"` (an unknown player_id).
    stats: tuple[PlayerStatRecord, ...] = ()
    #: Internal-only diagnostic (e.g. "no database row for this player_id")
    #: distinguishing *why* outcome is "not_found" -- never sent to a client.
    detail: str | None = None


@dataclass(frozen=True)
class RankingRecord:
    """
    One resolved NET-ranking row from the local cache -- exactly as
    stored, no calculation. `season` is caller-asserted (the cache file
    carries no season/freshness column of its own, so none is fabricated
    here); there is deliberately no `as_of`/`is_current` field, since the
    cache provides no authoritative freshness metadata to support one.
    """

    team_id: int
    net_rank: int
    #: The cache's own source-specific key (its `team_slug` column) --
    #: kept for traceability, exactly as `source_artifact_id` is kept on
    #: `PlayerStatRecord`.
    raw_team_key: str
    season: int


@dataclass(frozen=True)
class RankingLookupResult:
    outcome: Literal["found", "not_found", "conflicting", "cache_empty"]
    record: RankingRecord | None = None
    #: Populated only for `outcome="conflicting"` -- one `RankingRecord`
    #: per raw cache row that produced a different rank for this same
    #: team_id (e.g. two `RankingRecord`s for "louisiana-state", ranks 77
    #: and 245). Lets a caller (Slice 5B's API layer) present every
    #: conflicting value without the route re-deriving them itself --
    #: this is the same matching the service already performs, exposed as
    #: structured data instead of a formatted string.
    conflicting_records: tuple[RankingRecord, ...] = ()
    #: Internal-only diagnostic -- never sent to a client. For
    #: `outcome="conflicting"`, names the differing raw rows/ranks found
    #: for this team; never resolved by picking one silently.
    detail: str | None = None


@dataclass(frozen=True)
class InjuryEntry:
    """
    One player's injury entry exactly as read from the local injury
    cache JSON (Phase 6, Slice 6A). `player`/`status`/`detail` are copied
    verbatim from the source dict -- `status` is the source's own
    free-text designation (e.g. "out", "doubtful", "questionable", or any
    other string the source used), never normalized, mapped, or used to
    infer medical severity, playing probability, or a return date. `raw`
    preserves the complete original item exactly as parsed, including any
    additional keys these named fields do not cover, so no source-provided
    field is ever silently discarded.
    """

    player: str | None
    status: str | None
    detail: str | None
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class TeamInjuryReport:
    """
    Local-cache-only injury report for one team (Phase 6, Slice 6A),
    mirroring `src.features.team_summary.build_injury_report`'s own dict
    shape field-for-field. `outcome` distinguishes exactly the two states
    the underlying local-file authority can truthfully support:

    - `"loaded"`: a valid, list-shaped cache file was found -- `injuries`
      may legitimately be empty (a known, confirmed zero-injury report),
      never conflated with `"not_loaded"`.
    - `"not_loaded"`: no candidate path yielded a valid, list-shaped cache
      file. This covers a missing file, malformed JSON, and a non-list
      JSON payload alike, since the existing authoritative function does
      not itself distinguish those three causes -- no richer outcome
      (e.g. a separate "malformed" or "unknown team" tag) is invented
      here, since doing so would require new logic beyond what the
      existing authority performs (it runs no team-identity check and
      does not itself distinguish a missing file from a malformed one).

    `source_path` is the local file path used, if any -- kept here for
    traceability at the service layer only; any future HTTP exposure of
    this contract must exclude it, mirroring the cache-path-never-exposed
    precedent already established for Rankings (Phase 6, Slice 5B).

    **This contract only ever describes the `"loaded"`/`"not_loaded"`
    outcomes above -- it makes no promise about a third, non-typed
    failure mode**: a cache file whose JSON list contains a non-dict
    element causes the underlying authority to raise `AttributeError`
    rather than return either outcome (a genuine, preserved-not-fixed
    pre-existing limitation -- see `src.services.injury`'s own module
    docstring). This dataclass does not represent that case; Phase 6,
    Slice 6B's API-capable `get_injury_report_for_team` translates it
    into the dedicated `InjuryDataIntegrityError` instead of returning a
    `TeamInjuryReport` at all.
    """

    outcome: Literal["loaded", "not_loaded"]
    team_slug: str
    source_path: str | None
    injuries: tuple[InjuryEntry, ...]
    impactful_count: int
    #: Internal-only diagnostic message (populated only for
    #: `outcome="not_loaded"`, verbatim from the underlying authority) --
    #: mirrors the never-exposed-to-a-client `detail` convention already
    #: established for Rankings.
    detail: str | None = None


@dataclass(frozen=True)
class TeamInjuryLookupResult:
    """
    Coordinates canonical Team resolution with the local-cache Injury
    report for one team (Phase 6, Slice 6B). Distinguishes two genuinely
    different failure dimensions that must never be conflated:

    - `outcome="team_not_found"`: the canonical `team_id` itself does not
      exist (mirrors `TeamLookupResult.outcome="not_found"`) -- `report`
      is always `None` here, since there is no team to look Injury data
      up for.
    - `outcome="found"`: the canonical team exists; `report` is always
      populated, and its own `report.outcome` (`"loaded"`/`"not_loaded"`)
      tells the caller whether Injury data itself is available -- a
      known team with unavailable Injury data is never conflated with an
      unknown team.
    """

    outcome: Literal["found", "team_not_found"]
    report: TeamInjuryReport | None = None


@dataclass(frozen=True)
class FeatureComparisonEntry:
    """
    One named statistical feature's descriptive Team-A-vs-Team-B
    comparison (Phase 6, Slice 7A), mirroring one element of
    `src.features.matchup_features.build_key_edges`'s own output list.
    `leader` names which team has the higher value for this ONE feature
    -- purely descriptive (e.g. "Duke's season scoring margin is higher
    than UNC's"), never a prediction of the game's actual outcome, a
    score projection, or a betting recommendation (those remain
    `build_matchup_features`'s own separate `projection`/`market_edge`
    concerns, explicitly out of scope for this slice).
    """

    signal: str
    label: str
    leader: str
    delta: float


@dataclass(frozen=True)
class MatchupFeatureComparisonResult:
    """
    Immutable, read-only comparison of two teams' already-available
    feature values (Phase 6, Slice 7A) -- wraps
    `src.features.matchup_features.build_matchup_feature_comparison`
    unchanged; no filtering, no re-deriving deltas, no normalization
    happens here.

    `team_a_slug`/`team_b_slug` are caller-supplied labels only -- this
    service performs no Team identity resolution and no database lookup
    of its own (matching the "retain the explicit-input boundary" design
    this slice was approved for); a caller with only a name/team_id must
    resolve it to feature values first, exactly matching the
    Team/Player/Rankings/Injury precedent of not re-deriving identity.

    `deltas` preserves every one of the fixed comparison keys in
    `src.features.matchup_features.MATCHUP_FEATURE_LABELS`, including
    `None` for any key missing on either side -- a missing value is
    never silently omitted from this mapping, and never fabricated as
    zero, league-average, or an "even" comparison. `key_edges` is the
    same magnitude-sorted, missing-excluded view
    `build_key_edges`/`build_matchup_feature_comparison` already
    produce -- an empty tuple here (e.g. when every feature is missing
    on at least one side) truthfully means "nothing was comparable,"
    never a fabricated "even matchup."
    """

    team_a_slug: str
    team_b_slug: str
    deltas: dict[str, float | None]
    key_edges: tuple[FeatureComparisonEntry, ...]
