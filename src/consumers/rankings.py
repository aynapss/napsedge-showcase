"""
Phase 7 Slice 1: pure presentation adapter for
`src.services.rankings.get_team_ranking`'s existing `RankingLookupResult`
-- no cache-file access, no identity resolution, and no import of
`src.rankings`/`src.identity` anywhere in this module.

Only the per-team lookup outcome is adapted here, per this slice's own
approved default -- a full 365-row Rankings-list presentation is
explicitly out of scope for this slice.
"""
from __future__ import annotations

from src.consumers.contracts import ConsumerPresentation, PresentationField
from src.services.contracts import RankingLookupResult

_NOT_FOUND_MESSAGE = "No NET ranking found for that team."
_UNAVAILABLE_MESSAGE = "Rankings data is temporarily unavailable right now."
_CONFLICT_WARNING = "Multiple conflicting rank records exist for this team; none has been selected."
_SNAPSHOT_NOTE = "Rankings reflect the locally cached snapshot only and may not be current."


def present_team_ranking(result: RankingLookupResult) -> ConsumerPresentation:
    """
    Maps one `RankingLookupResult` to a `ConsumerPresentation`:

    - `outcome="not_found"` -> explicit not-found.
    - `outcome="cache_empty"` -> explicit unavailable, never an empty
      "no teams ranked" presentation.
    - `outcome="conflicting"` -> every `conflicting_records` entry is
      preserved as its own row, in the exact order the service already
      produced them -- this adapter never selects or drops one.
    - `outcome="found"` -> a successful presentation carrying the
      authoritative cached rank and the same team/source identifiers
      `src.api.schemas.RankingResponse` already exposes (`team_id`,
      `net_rank`, `raw_team_key`, `season`) -- no more. `season` is
      passed through exactly as the service supplied it (caller/service
      context only); no `as_of`/freshness date is invented, since the
      cache carries none -- the static disclaimer in `notes` is the
      truthful substitute.
    """
    if result.outcome == "not_found":
        return ConsumerPresentation(status="not_found", title="Ranking Not Found", message=_NOT_FOUND_MESSAGE)

    if result.outcome == "cache_empty":
        return ConsumerPresentation(status="unavailable", title="Rankings Unavailable", message=_UNAVAILABLE_MESSAGE)

    if result.outcome == "conflicting":
        rows = tuple(
            (
                PresentationField("Raw Key", record.raw_team_key),
                PresentationField("NET Rank", str(record.net_rank)),
            )
            for record in result.conflicting_records
        )
        return ConsumerPresentation(
            status="conflict",
            title="Conflicting Ranking Data",
            rows=rows,
            warnings=(_CONFLICT_WARNING,),
        )

    record = result.record
    assert record is not None  # outcome == "found" always carries a record, per RankingLookupResult's own contract

    fields = (
        PresentationField("Team ID", str(record.team_id)),
        PresentationField("NET Rank", str(record.net_rank)),
        PresentationField("Season", str(record.season)),
        PresentationField("Source Key", record.raw_team_key),
    )
    return ConsumerPresentation(
        status="ok",
        title=f"NET Rank #{record.net_rank}",
        fields=fields,
        notes=(_SNAPSHOT_NOTE,),
    )
