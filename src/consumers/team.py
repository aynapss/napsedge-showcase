"""
Phase 7 Slice 1: pure presentation adapter for `src.services.team`'s
existing `TeamLookupResult` -- no database, no identity resolution, no
network, and no import of `src.db`/`src.identity`/`sqlalchemy` anywhere
in this module. The adapter only ever reads the fields
`src.services.contracts.TeamLookupResult`/`TeamRecord` already carry.

Team's own ambiguous-query outcome is represented as a raised
`IdentityConflictError` at the service boundary (`src.services.team.
resolve_team`), never as a `TeamLookupResult` outcome value -- a future
caller maps that exception with `src.consumers.errors.present_error`
directly; this module has nothing to do for that case, since it never
receives an exception, only a result.
"""
from __future__ import annotations

from src.consumers.contracts import ConsumerPresentation, PresentationField
from src.services.contracts import TeamLookupResult

_NOT_FOUND_MESSAGE = "No team matched that lookup."


def present_team_lookup(result: TeamLookupResult) -> ConsumerPresentation:
    """
    Maps one `TeamLookupResult` to a `ConsumerPresentation`:

    - `outcome="not_found"` -> an explicit not-found presentation, never
      an empty successful one.
    - `outcome="found"` -> a successful presentation whose fields are a
      1:1, unmodified mirror of `TeamRecord` -- the same field set
      `src.api.schemas.TeamResponse` already exposes, and no more.
      `conference_name` is passed through exactly as supplied (`None`
      stays `None`, never fabricated as an empty string or a placeholder
      conference).
    """
    if result.outcome == "not_found":
        return ConsumerPresentation(status="not_found", title="Team Not Found", message=_NOT_FOUND_MESSAGE)

    team = result.team
    assert team is not None  # outcome == "found" always carries a team, per TeamLookupResult's own contract

    fields = (
        PresentationField("Team", team.canonical_name),
        PresentationField("Slug", team.canonical_slug),
        PresentationField("Conference", team.conference_name),
        PresentationField("Team ID", str(team.team_id)),
    )
    return ConsumerPresentation(status="ok", title=team.canonical_name, fields=fields)
