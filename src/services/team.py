"""
Read-only Team service (Phase 6, Slice 3).

This module is independent of FastAPI -- it takes an already-open
`Session` (owned and closed by the caller's own dependency, never by this
module) and returns plain `@dataclass` results or raises an existing
`src.exceptions.NapsEdgeError` subclass. It never constructs a `select(...)`
itself: all persistence access goes through `src.db.repositories.TeamRepository`.
It never re-derives a canonical slug/name/id and never adds a second alias
table -- name/slug/alias resolution is delegated entirely to the existing
`src.identity.team_identity.resolve_team_identity`.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from src.db.models import Team
from src.db.repositories import TeamRepository
from src.exceptions import IdentityConflictError
from src.identity.contracts import TeamIdentity, UnresolvedIdentity
from src.identity.team_identity import resolve_team_identity
from src.services.contracts import TeamLookupResult, TeamRecord


def _to_record(row: Team) -> TeamRecord:
    return TeamRecord(
        team_id=row.team_id,
        canonical_name=row.display_name,
        canonical_slug=row.sr_slug,
        conference_name=row.conference.canonical_name if row.conference_id is not None and row.conference else None,
    )


def get_team(session: Session, team_id: int) -> TeamLookupResult:
    """Canonical Team by its stable, already-persisted team_id -- one row per team_id by construction (the primary key), regardless of how many source directories fed it."""
    row = TeamRepository(session).get_by_id(team_id)
    if row is None:
        return TeamLookupResult(outcome="not_found", detail="no database row for this team_id")
    return TeamLookupResult(outcome="found", team=_to_record(row))


def resolve_team(session: Session, query: str) -> TeamLookupResult:
    """
    Resolves a caller-supplied team name/slug/alias via the existing
    authoritative identity resolver, then loads the resulting canonical
    Team row from PostgreSQL.

    Raises `IdentityConflictError` when the resolver itself reports more
    than one plausible program (a real data collision) -- never silently
    picks one. Returns `outcome="not_found"` (never raises) both when the
    resolver finds nothing at all and when it resolves to a team_id that
    has no persisted Team row yet -- these are two distinct, internally
    logged conditions (see `detail`), collapsed to the same client-facing
    outcome because neither has a Team to return.
    """
    result: TeamIdentity | UnresolvedIdentity = resolve_team_identity(query, source="api")

    if isinstance(result, UnresolvedIdentity):
        if result.reason == "ambiguous":
            candidate_names = ", ".join(c.display_name for c in result.candidates if isinstance(c, TeamIdentity))
            raise IdentityConflictError(f"team query {query!r} is ambiguous between: {candidate_names}")
        return TeamLookupResult(outcome="not_found", detail="unresolved")

    row = TeamRepository(session).get_by_id(result.team_id)
    if row is None:
        return TeamLookupResult(outcome="not_found", detail="resolved_but_missing_row")
    return TeamLookupResult(outcome="found", team=_to_record(row))
