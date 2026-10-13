"""
Read-only NET-rankings service using local cached data only (Phase 6,
Slice 5A).

Mirrors `src.services.team`/`src.services.player`'s structure: independent
of FastAPI, takes an injected `NetRankingCacheRepository` (never
constructs its own), and returns plain `@dataclass` results. Performs no
calculation, no predictive ranking, no live CBS/NCAA request, and no
identity normalization of its own -- every team-resolution decision comes
from the existing `src.ingestion.net_ranking_ingestion`/
`src.identity.net_ranking_identity` chain, unmodified.

**Live-source isolation**: this module imports only
`src.rankings.cache_repository` (which itself imports only
`src.ingestion.net_ranking_ingestion` and `src.paths`) -- no
`build_net_rankings_from_cbs`/`build_net_rankings_from_ncaa_v15`, no
`requests`/`httpx`/`urllib`, no Selenium/browser module, anywhere in this
file or its import chain. Verified by a dedicated AST-level regression
test and by a monkeypatch test that disables every outbound network
function and confirms the service still succeeds unaffected.
"""
from __future__ import annotations

from src.exceptions import RankingsCacheIntegrityError, SourceUnavailableError, ValidationError
from src.identity.contracts import TeamIdentity
from src.rankings.cache_repository import NetRankingCacheRepository
from src.services.contracts import RankingLookupResult, RankingRecord


def _load_cache(cache_repository: NetRankingCacheRepository, *, season: int):
    """
    Phase 6, Slice 5B: a malformed cache (missing `team_slug`/`net_rank`
    columns) makes `cache_repository.load()` raise `ValidationError` --
    but that exception type is globally mapped to HTTP 422 elsewhere
    (client-input validation, e.g. Team/Player request bodies), which
    would be the wrong signal here: a broken *server-side* cache file is
    not a client input problem. Re-raised as `SourceUnavailableError`
    (already mapped to a sanitized 503) so the API layer can satisfy
    "malformed cache returns sanitized 503" without a route-level
    try/except and without changing `ValidationError`'s meaning for any
    other domain.
    """
    try:
        return cache_repository.load(season=season)
    except ValidationError as exc:
        raise SourceUnavailableError("the configured rankings cache could not be validated") from exc


def list_rankings(cache_repository: NetRankingCacheRepository, *, season: int):
    """
    Returns the existing, unmodified `NetRankingIngestionResult` for the
    configured cache -- no new wrapping contract, since that result
    already faithfully represents every row's raw evidence and resolution
    status. Reused as-is rather than duplicated: this is "the smallest
    useful Rankings service" for a complete listing.
    """
    return _load_cache(cache_repository, season=season)


def get_team_ranking(
    cache_repository: NetRankingCacheRepository,
    canonical_team_id: int,
    *,
    season: int,
) -> RankingLookupResult:
    """
    Looks up one canonical team's NET rank in the local cache.

    `canonical_team_id` must already be a resolved canonical team_id (this
    service performs no name-based resolution of its own -- callers that
    have only a name/slug must resolve it first via the existing Team
    identity/service layer). Distinguishes:

    - `outcome="cache_empty"`: the cache loaded successfully but contains
      zero rows -- never conflated with "team not found".
    - `outcome="not_found"`: the cache is non-empty, but no row resolved
      to this team_id.
    - `outcome="conflicting"`: two or more rows resolved to this team_id
      with *different* ranks (a real, confirmed defect in the cached
      fixture itself, e.g. "louisiana-state" appearing twice with ranks
      77 and 245) -- never silently picks one.
    - `outcome="found"`: exactly one rank is attributable to this team.

    Two or more rows resolving to this team_id with the *same* rank (an
    exact identical duplicate) has no existing documented deduplication
    rule, so it raises `RankingsCacheIntegrityError` rather than being
    silently collapsed or guessed at.

    Never mutates the cache; never performs a live request (delegated
    entirely to `cache_repository.load`, itself cache-only).
    """
    ingestion_result = _load_cache(cache_repository, season=season)

    if not ingestion_result.records:
        return RankingLookupResult(outcome="cache_empty", detail="the configured cache contains zero rows")

    matches = [
        record
        for record in ingestion_result.records
        if isinstance(record.team_identity, TeamIdentity)
        and record.team_identity.team_id == canonical_team_id
        and record.validated_rank is not None
    ]

    if not matches:
        return RankingLookupResult(outcome="not_found", detail="no cache row resolved to this team_id")

    distinct_ranks = {match.validated_rank for match in matches}
    if len(distinct_ranks) > 1:
        detail = ", ".join(f"{m.raw_team_key!r}={m.validated_rank}" for m in matches)
        conflicting_records = tuple(
            RankingRecord(team_id=canonical_team_id, net_rank=m.validated_rank, raw_team_key=m.raw_team_key, season=m.season)
            for m in matches
        )
        return RankingLookupResult(
            outcome="conflicting",
            conflicting_records=conflicting_records,
            detail=f"conflicting ranks for this team: {detail}",
        )

    if len(matches) > 1:
        raise RankingsCacheIntegrityError(
            f"{len(matches)} identical-rank duplicate cache rows resolved to the same team_id "
            f"with no existing deduplication rule to safely collapse them."
        )

    match = matches[0]
    return RankingLookupResult(
        outcome="found",
        record=RankingRecord(
            team_id=canonical_team_id,
            net_rank=match.validated_rank,
            raw_team_key=match.raw_team_key,
            season=match.season,
        ),
    )
