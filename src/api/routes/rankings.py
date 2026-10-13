"""
Rankings HTTP routes (Phase 6, Slice 5B) -- HTTP translation only.

No file parsing, no identity reconciliation, no ranking calculation, and
no network call lives here: both routes delegate entirely to the
existing, unmodified `src.services.rankings.list_rankings`/
`get_team_ranking`. Both require Bearer authentication *and* an
in-memory, per-client fixed-window rate limit
(`src.api.deps.get_rate_limited_client`, Phase 6D), declared *before* the
Rankings context dependency in each function signature so a failed
authentication check or an exhausted rate limit short-circuits the
request before the cache path/season are even resolved -- no
configuration lookup and no file read happens for an unauthenticated or
rate-limited request, exactly mirroring `src.api.routes.teams`/`players`.

**Server-controlled context only.** `season` and the cache path come
exclusively from `src.api.deps.get_rankings_context` (server-side
configuration) -- neither route accepts either as a client-supplied
parameter, since the local cache carries no authoritative season/as-of
metadata a client-chosen value could be checked against.

**Internal, non-deployed validation only.** No production
hosting/deployment story yet. Do not expose this application on a public
network.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from src.api.deps import RankingsContext, get_rankings_context, get_rate_limited_client
from src.api.schemas import (
    ErrorResponse,
    RankingConflictEntryResponse,
    RankingConflictResponse,
    RankingResponse,
    RankingSourceRowResponse,
    RankingsListResponse,
)
from src.auth.api_keys import AuthenticatedClient
from src.identity.contracts import TeamIdentity
from src.services import rankings as rankings_service

router = APIRouter(prefix="/v1/rankings/net", tags=["rankings"])

_AUTH_RESPONSES = {
    401: {"model": ErrorResponse, "description": "Missing or invalid Bearer credentials"},
    429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
    503: {"model": ErrorResponse, "description": "Rankings cache or authentication is temporarily unavailable"},
}


@router.get("", response_model=RankingsListResponse, responses={**_AUTH_RESPONSES})
def list_net_rankings(
    client: AuthenticatedClient = Depends(get_rate_limited_client),
    rankings: RankingsContext = Depends(get_rankings_context),
) -> RankingsListResponse:
    ingestion_result = rankings_service.list_rankings(rankings.repository, season=rankings.season)

    rows = [
        RankingSourceRowResponse(
            raw_team_key=record.raw_team_key,
            resolution_status=record.resolution_status,
            team_id=record.team_identity.team_id if isinstance(record.team_identity, TeamIdentity) else None,
            net_rank=record.validated_rank,
        )
        for record in ingestion_result.records
    ]
    return RankingsListResponse(season=rankings.season, total_rows=len(rows), rows=rows)


@router.get(
    "/teams/{team_id}",
    response_model=RankingResponse,
    responses={
        404: {"model": ErrorResponse, "description": "Not found"},
        409: {"model": RankingConflictResponse, "description": "Conflicting cache rows for this team"},
        **_AUTH_RESPONSES,
    },
)
def get_net_ranking_for_team(
    team_id: int,
    client: AuthenticatedClient = Depends(get_rate_limited_client),
    rankings: RankingsContext = Depends(get_rankings_context),
) -> RankingResponse | JSONResponse:
    result = rankings_service.get_team_ranking(rankings.repository, team_id, season=rankings.season)

    if result.outcome == "found":
        record = result.record
        assert record is not None
        return RankingResponse(team_id=record.team_id, net_rank=record.net_rank, raw_team_key=record.raw_team_key, season=record.season)

    if result.outcome == "not_found":
        return JSONResponse(status_code=404, content={"error": "not_found", "message": "team not found in the rankings cache"})

    if result.outcome == "conflicting":
        body = RankingConflictResponse(
            message="the rankings cache contains conflicting ranks for this team",
            team_id=team_id,
            conflicting_records=[
                RankingConflictEntryResponse(raw_team_key=r.raw_team_key, net_rank=r.net_rank) for r in result.conflicting_records
            ],
        )
        return JSONResponse(status_code=409, content=body.model_dump())

    # outcome == "cache_empty"
    return JSONResponse(status_code=503, content={"error": "service_unavailable", "message": "the rankings cache is empty"})
