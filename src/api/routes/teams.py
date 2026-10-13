"""
Team HTTP routes (Phase 6, Slice 3B) -- HTTP translation only.

No SQL, no repository call, no identity/normalization rule, and no
calculation lives here: every route delegates entirely to the existing,
unmodified `src.services.team`. Both routes require Bearer authentication
*and* an in-memory, per-client fixed-window rate limit
(`src.api.deps.get_rate_limited_client`, Phase 6D), declared *before* the
session dependency in each function signature so a failed authentication
check or an exhausted rate limit short-circuits the request before
`get_session` ever runs -- no database connection is opened for an
unauthenticated or rate-limited request.

**Internal, non-deployed validation only.** These routes have no
production hosting/deployment story yet -- a separate, not-yet-approved
decision. Do not expose this application on a public network.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from src.api.deps import get_rate_limited_client, get_session
from src.api.schemas import ErrorResponse, TeamResponse
from src.auth.api_keys import AuthenticatedClient
from src.services import team as team_service
from src.services.contracts import TeamLookupResult

router = APIRouter(prefix="/v1/teams", tags=["teams"])

#: Shared OpenAPI documentation for the authentication/rate-limiting
#: outcomes every route on this router can produce.
_AUTH_RESPONSES = {
    401: {"model": ErrorResponse, "description": "Missing or invalid Bearer credentials"},
    429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
    503: {"model": ErrorResponse, "description": "Authentication is temporarily unavailable"},
}


def _team_response_or_404(result: TeamLookupResult) -> TeamResponse | JSONResponse:
    if result.outcome == "not_found":
        return JSONResponse(status_code=404, content={"error": "not_found", "message": "team not found"})
    record = result.team
    assert record is not None  # outcome == "found" always carries a team
    return TeamResponse(
        team_id=record.team_id,
        canonical_name=record.canonical_name,
        canonical_slug=record.canonical_slug,
        conference_name=record.conference_name,
    )


# Registered before "/{team_id}" so Starlette's first-match routing never
# tries to coerce the literal segment "resolve" into the team_id: int path
# parameter.
@router.get(
    "/resolve",
    response_model=TeamResponse,
    responses={
        404: {"model": ErrorResponse, "description": "Not found"},
        409: {"model": ErrorResponse, "description": "Ambiguous identity"},
        422: {"description": "Invalid or missing query parameter"},
        **_AUTH_RESPONSES,
    },
)
def resolve_team(
    client: AuthenticatedClient = Depends(get_rate_limited_client),
    query: str = Query(..., min_length=1),
    session: Session = Depends(get_session),
) -> TeamResponse | JSONResponse:
    result = team_service.resolve_team(session, query)
    return _team_response_or_404(result)


@router.get(
    "/{team_id}",
    response_model=TeamResponse,
    responses={404: {"model": ErrorResponse, "description": "Not found"}, **_AUTH_RESPONSES},
)
def get_team(
    team_id: int,
    client: AuthenticatedClient = Depends(get_rate_limited_client),
    session: Session = Depends(get_session),
) -> TeamResponse | JSONResponse:
    result = team_service.get_team(session, team_id)
    return _team_response_or_404(result)
