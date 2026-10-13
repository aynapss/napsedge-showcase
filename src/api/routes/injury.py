"""
Injury HTTP route (Phase 6, Slice 6B) -- HTTP translation only.

No filesystem discovery, no Team identity/normalization logic, no SQL
query, no medical interpretation, and no exception-policy branching beyond
invoking the service lives here: the route delegates entirely to
`src.services.injury.get_injury_report_for_team`, which itself coordinates
the existing, unmodified `src.services.team.get_team` (canonical Team
resolution) with the existing, unmodified local-cache Injury authority.

Requires Bearer authentication *and* an in-memory, per-client
fixed-window rate limit (`src.api.deps.get_rate_limited_client`, Phase
6D), declared *before* the session dependency in the route's own
signature so a failed authentication check or an exhausted rate limit
short-circuits the request before `get_session` ever runs -- no database
connection is opened, no Team lookup happens, and no filesystem access
occurs for an unauthenticated or rate-limited request, exactly mirroring
`src.api.routes.teams`/`players`/`rankings`.

**No client-controlled filesystem path.** The service's own `search_paths`
parameter is never exposed here -- the route accepts only a canonical
`team_id` path parameter; the local Injury cache's directory/file
selection remains entirely server-controlled, resolved from the team's own
already-persisted canonical slug.

**Internal, non-deployed validation only.** No production
hosting/deployment story yet. Do not expose this application on a public
network.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from src.api.deps import get_rate_limited_client, get_session
from src.api.schemas import ErrorResponse, TeamInjuryReportResponse
from src.auth.api_keys import AuthenticatedClient
from src.services.injury import get_injury_report_for_team

router = APIRouter(tags=["injury"])

_AUTH_RESPONSES = {
    401: {"model": ErrorResponse, "description": "Missing or invalid Bearer credentials"},
    429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
    503: {"model": ErrorResponse, "description": "Injury data or authentication is temporarily unavailable"},
}


@router.get(
    "/v1/teams/{team_id}/injuries",
    response_model=TeamInjuryReportResponse,
    responses={404: {"model": ErrorResponse, "description": "Not found"}, **_AUTH_RESPONSES},
)
def get_team_injuries(
    team_id: int,
    client: AuthenticatedClient = Depends(get_rate_limited_client),
    session: Session = Depends(get_session),
) -> TeamInjuryReportResponse | JSONResponse:
    result = get_injury_report_for_team(session, team_id)

    if result.outcome == "team_not_found":
        return JSONResponse(status_code=404, content={"error": "not_found", "message": "team not found"})

    report = result.report
    assert report is not None  # outcome == "found" always carries a report

    if report.outcome == "not_loaded":
        return JSONResponse(
            status_code=503,
            content={"error": "service_unavailable", "message": "injury data is not currently available for this team"},
        )

    return TeamInjuryReportResponse(
        team_id=team_id,
        injuries=[
            {"player": entry.player, "status": entry.status, "detail": entry.detail} for entry in report.injuries
        ],
        impactful_count=report.impactful_count,
    )
