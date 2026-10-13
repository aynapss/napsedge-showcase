"""
Minimal shared typed contracts for canonical identity resolution (Phase 2).

Deliberately small: only what Phase 2A (team identity) and Phase 2B (player
identity) actually need. Do not add fields speculatively for later phases --
extend this when a real caller needs it, not before.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional


@dataclass(frozen=True)
class SourceIdentity:
    """One source's view of an identity: its own slug/id plus what it was derived from."""

    source: str  # e.g. "sports_reference", "covers", "espn", "net_rankings", "odds", "torvik"
    raw_value: str
    normalized_value: str


@dataclass
class TeamIdentity:
    """
    A resolved, canonical team. ``team_id`` is the stable integer produced by
    ``src.slug_utils.stable_team_id`` (SHA-256 of the SR slug) -- reproducible
    across runs and independent of iteration/display order, satisfying the
    Phase 2A stability requirement without inventing a second ID scheme.
    """

    team_id: int
    display_name: str
    conference: Optional[str] = None
    sources: dict[str, SourceIdentity] = field(default_factory=dict)


@dataclass
class PlayerIdentity:
    """
    A resolved player, scoped by team + season (Phase 2B). Two players with
    the same normalized name on different teams, or the same team in
    different seasons, are different ``PlayerIdentity`` records.
    """

    player_id: str
    display_name: str
    team_id: int
    season: int
    sources: dict[str, SourceIdentity] = field(default_factory=dict)


@dataclass
class ConferenceIdentity:
    """
    A resolved, canonical conference (Phase 2, Gate 2). ``conference_id`` is
    a stable integer (SHA-256 of the canonical conference name), independent
    of any team's current membership -- a conference's identity persists
    across realignment; which teams belong to it at a point in time is
    tracked separately, never encoded into this id.
    """

    conference_id: int
    canonical_name: str
    sources: dict[str, SourceIdentity] = field(default_factory=dict)


@dataclass
class SeasonIdentity:
    """
    A resolved season, using this codebase's existing convention: an
    integer representing the season's ENDING year (e.g. ``2026`` means the
    2025-26 season). ``season_id`` is that same integer -- already
    deterministic and ordering-independent, so no hashing is needed.
    """

    season_id: int
    start_year: int
    end_year: int
    label: str  # e.g. "2025-26"


@dataclass
class SportsbookIdentity:
    """
    A resolved, canonical sportsbook (Phase 2, Gate 2). ``sportsbook_id``
    is a stable integer derived from the provider's canonical key (The Odds
    API's own lowercase ``key`` convention, e.g. ``"fanduel"``) -- a display
    name change (API ``title`` casing/punctuation) never changes it.
    """

    sportsbook_id: int
    canonical_key: str
    display_name: str
    sources: dict[str, SourceIdentity] = field(default_factory=dict)


@dataclass
class ModelVersionIdentity:
    """
    A resolved model/formula-configuration version (Phase 2, Gate 2).
    ``model_version_id`` is a stable integer derived from either an
    explicit version label or a deterministic fingerprint of named
    calculation-configuration values -- never from a file path, mtime,
    DataFrame order, or runtime timestamp.
    """

    model_version_id: int
    label: str
    config_fingerprint: str | None = None
    sources: dict[str, SourceIdentity] = field(default_factory=dict)


@dataclass
class GameIdentity:
    """
    A resolved (or explicitly provisional) game (Phase 2, Gate 2) --
    the highest-risk identifier in this migration. ``game_id`` prefers a
    real authoritative source id (``f"{source}:{source_game_id}"``) when
    one is supplied; otherwise it is a stable structural key over
    canonical ``team_id`` values (never display names or mutable slugs),
    season, date, and neutral-site status -- ``is_provisional`` is True in
    that fallback case, and this dataclass never silently claims more
    certainty than it has.
    """

    game_id: int
    home_team_id: int
    away_team_id: int
    season: int
    game_date: str
    neutral_site: bool
    is_provisional: bool
    sources: dict[str, SourceIdentity] = field(default_factory=dict)


@dataclass
class ResolutionResult:
    """Wraps a successful resolution with how confident/how it was found."""

    query: str
    matched: TeamIdentity | PlayerIdentity
    confidence: float
    method: Literal["exact_alias", "sr_override", "canonical_slug"]


@dataclass
class UnresolvedIdentity:
    """
    A query that could not be safely auto-resolved.

    ``reason="not_found"``: no exact/deterministic match, and no fuzzy
    candidate is close enough to even suggest.
    ``reason="ambiguous"``: more than one exact-tier candidate exists (a
    genuine data collision -- see ``check_team_alias_collisions``) so picking
    one silently would be a guess.
    ``reason="unconfirmed"`` (added Phase 2B, injury batch): exactly one
    plausible candidate exists, but the evidence that found it is
    inherently too weak to auto-assign identity from alone -- e.g. a
    last-name-only injury report matching exactly one roster player by
    surname. Distinct from ``"ambiguous"`` (multiple equally-plausible
    candidates) and ``"not_found"`` (nothing matches at all): here,
    something specific *was* found and is surfaced via ``candidates`` for
    a human or a stronger source to confirm, it just isn't strong enough
    evidence on its own.

    ``suggestions`` are informational near-misses (from fuzzy matching) for a
    human or calling layer to review -- never auto-selected.

    ``candidates`` holds the competing (or, for ``"unconfirmed"``, the
    single weakly-evidenced) matches. Widened in Phase 2B to also accept
    ``PlayerIdentity`` (e.g. two roster entries sharing a suffix-stripped
    ``PlayerKey`` within one team+season) -- purely a type widening, no
    existing team-identity usage changes. Widened again in Phase 2 Gate 2
    to accept the five new entity-identity types, for the same reason.
    """

    query: str
    source: str
    reason: Literal["not_found", "ambiguous", "unconfirmed"]
    suggestions: list[tuple[str, float]] = field(default_factory=list)
    candidates: list[
        "TeamIdentity | PlayerIdentity | ConferenceIdentity | SeasonIdentity | "
        "SportsbookIdentity | ModelVersionIdentity | GameIdentity"
    ] = field(default_factory=list)
