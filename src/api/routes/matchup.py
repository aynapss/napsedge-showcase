"""
Matchup HTTP route (Phase 6, Slice 7B) -- HTTP translation only.

No delta calculation, no rounding, no sorting, no feature selection, no
leader determination, no normalization, no data acquisition, no
repository query, and no betting/prediction rule lives here: the route
delegates entirely to the existing, unmodified
`src.services.matchup.compare_team_matchup_features`.

Requires Bearer authentication *and* an in-memory, per-client
fixed-window rate limit (`src.api.deps.get_rate_limited_client`, Phase
6D) -- **no database session dependency exists on this route at all**,
since Slice 7A's Matchup service has no database dependency of its own
(it accepts explicit, caller-supplied feature values; it never resolves a
canonical team_id and never acquires Team/Injury/Rankings data itself).
The rate limit is enforced *before* the Matchup service is ever invoked,
confirmed by dedicated tests.

`POST`, not `GET`: this operation compares two explicit, structured
feature payloads rather than retrieving a persisted resource by a short
identifier -- `team_a_slug`/`team_b_slug` are caller-supplied descriptive
labels only, never a validated canonical Team identity.

**Strictly descriptive.** No prediction, win probability, spread, total,
moneyline, implied probability, betting recommendation, confidence
score, or winner determination exists anywhere in this route, the
service it calls, or the analytical functions underneath -- only which
team has the higher value for each of 7 fixed, pre-existing statistical
features.

**Internal, non-deployed validation only.** No production
hosting/deployment story yet. Do not expose this application on a public
network.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.deps import get_rate_limited_client
from src.api.schemas import (
    ErrorResponse,
    FeatureComparisonEntryResponse,
    MatchupFeatureComparisonRequest,
    MatchupFeatureComparisonResponse,
)
from src.auth.api_keys import AuthenticatedClient
from src.services.matchup import compare_team_matchup_features

router = APIRouter(tags=["matchup"])

_AUTH_RESPONSES = {
    401: {"model": ErrorResponse, "description": "Missing or invalid Bearer credentials"},
    429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
    503: {"model": ErrorResponse, "description": "Authentication is temporarily unavailable"},
}


@router.post(
    "/v1/matchups/feature-comparison",
    response_model=MatchupFeatureComparisonResponse,
    responses={422: {"description": "Invalid or malformed request body"}, **_AUTH_RESPONSES},
)
def compare_matchup_features(
    request: MatchupFeatureComparisonRequest,
    client: AuthenticatedClient = Depends(get_rate_limited_client),
) -> MatchupFeatureComparisonResponse:
    result = compare_team_matchup_features(
        request.team_a_slug,
        request.team_b_slug,
        request.team_a_features.model_dump(),
        request.team_b_features.model_dump(),
    )
    return MatchupFeatureComparisonResponse(
        team_a_slug=result.team_a_slug,
        team_b_slug=result.team_b_slug,
        deltas=result.deltas,
        key_edges=[
            FeatureComparisonEntryResponse(signal=entry.signal, label=entry.label, leader=entry.leader, delta=entry.delta)
            for entry in result.key_edges
        ],
    )
