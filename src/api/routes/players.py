"""
Player HTTP routes (Phase 6, Slice 4B) -- HTTP translation only.

No SQL, no repository call, no identity/normalization rule, no
calculation, and no notebook execution lives here: the one route
delegates entirely to the existing, unmodified
`src.services.player.get_player_raw_statistics`. It requires Bearer
authentication *and* an in-memory, per-client fixed-window rate limit
(`src.api.deps.get_rate_limited_client`, Phase 6D), declared *before* the
session dependency in the function signature so a failed authentication
check or an exhausted rate limit short-circuits the request before
`get_session` ever runs -- no database connection is opened for an
unauthenticated or rate-limited request, exactly mirroring
`src.api.routes.teams`.

**Internal, non-deployed validation only.** No production
hosting/deployment story yet -- a separate, not-yet-approved decision. Do
not expose this application on a public network.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import JSONResponse

from sqlalchemy.orm import Session

from src.api.deps import get_rate_limited_client, get_session
from src.api.schemas import ErrorResponse, PlayerRawStatisticsResponse, PlayerStatResponse
from src.auth.api_keys import AuthenticatedClient
from src.services import player as player_service
from src.services.contracts import PlayerStatRecord

router = APIRouter(prefix="/v1/players", tags=["players"])


def _to_stat_response(record: PlayerStatRecord) -> PlayerStatResponse:
    return PlayerStatResponse.model_validate(record, from_attributes=True)


@router.get(
    "/{player_id}/raw-statistics",
    response_model=PlayerRawStatisticsResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Missing or invalid Bearer credentials"},
        404: {"model": ErrorResponse, "description": "Not found"},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
        503: {"model": ErrorResponse, "description": "Authentication is temporarily unavailable"},
    },
)
def get_player_raw_statistics(
    player_id: str = Path(..., min_length=1, max_length=200),
    client: AuthenticatedClient = Depends(get_rate_limited_client),
    season_id: int | None = Query(None),
    stat_table: str | None = Query(None),
    session: Session = Depends(get_session),
) -> PlayerRawStatisticsResponse | JSONResponse:
    result = player_service.get_player_raw_statistics(session, player_id, season_id=season_id, stat_table=stat_table)
    if result.outcome == "not_found":
        return JSONResponse(status_code=404, content={"error": "not_found", "message": "player not found"})

    assert result.player_id is not None and result.display_name is not None
    assert result.team_id is not None and result.season_id is not None  # players.team_id/season_id are NOT NULL

    return PlayerRawStatisticsResponse(
        player_id=result.player_id,
        display_name=result.display_name,
        team_id=result.team_id,
        season_id=result.season_id,
        stats=[_to_stat_response(record) for record in result.stats],
    )
