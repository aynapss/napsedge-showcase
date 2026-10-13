"""
Phase 7 Slice 1: pure presentation adapter for
`src.services.injury.get_injury_report_for_team`'s existing
`TeamInjuryLookupResult` -- no filesystem/cache access and no import of
`src.features`/`src.services.injury` anywhere in this module.

A malformed-source integrity failure is represented as a raised
`InjuryDataIntegrityError` at the service boundary, never as a
`TeamInjuryLookupResult` outcome value -- a future caller maps that
exception with `src.consumers.errors.present_error` directly; this
module has nothing to do for that case, since it never receives an
exception, only a result.
"""
from __future__ import annotations

from src.consumers.contracts import ConsumerPresentation, PresentationField
from src.services.contracts import TeamInjuryLookupResult

_TEAM_NOT_FOUND_MESSAGE = "No team matched that lookup."
_UNAVAILABLE_MESSAGE = "Injury data is temporarily unavailable for this team."
_KNOWN_EMPTY_SUMMARY = "No injuries currently reported."
_FRESHNESS_NOTE = "Injury data reflects the locally cached report only and may not be current."


def present_team_injury(result: TeamInjuryLookupResult) -> ConsumerPresentation:
    """
    Maps one `TeamInjuryLookupResult` to a `ConsumerPresentation`:

    - `outcome="team_not_found"` -> explicit not-found (there is no team
      to look Injury data up for).
    - `report.outcome="not_loaded"` -> explicit unavailable -- **never**
      an empty "no injuries" presentation, which would silently claim a
      health status the underlying cache never confirmed.
    - `report.outcome="loaded"`, zero entries -> a successful,
      structurally-distinct **known-empty** presentation (`status="ok"`,
      a summary line, and an empty `rows` tuple) -- distinguishable from
      `"unavailable"` by `status` alone, never conflated.
    - `report.outcome="loaded"`, one or more entries -> a successful
      presentation with one row per `InjuryEntry`, in the exact order
      `report.injuries` already carries -- `player`/`status`/`detail`
      are passed through verbatim (a `None` stays `None`); `raw`,
      `source_path`, `team_slug`, and the top-level `report.detail` are
      never read anywhere in this module, mirroring the same exclusions
      `src.api.schemas.InjuryEntryResponse`/`TeamInjuryReportResponse`
      already established. `impactful_count` is exposed unchanged in
      every populated/known-empty case, never recomputed here.
    """
    if result.outcome == "team_not_found":
        return ConsumerPresentation(status="not_found", title="Team Not Found", message=_TEAM_NOT_FOUND_MESSAGE)

    report = result.report
    assert report is not None  # outcome == "found" always carries a report, per TeamInjuryLookupResult's own contract

    if report.outcome == "not_loaded":
        return ConsumerPresentation(status="unavailable", title="Injury Report Unavailable", message=_UNAVAILABLE_MESSAGE)

    impactful_count_field = (PresentationField("Impactful Count", str(report.impactful_count)),)

    if not report.injuries:
        return ConsumerPresentation(
            status="ok",
            title="Injury Report",
            summary=_KNOWN_EMPTY_SUMMARY,
            fields=impactful_count_field,
            notes=(_FRESHNESS_NOTE,),
        )

    rows = tuple(
        (
            PresentationField("Player", entry.player),
            PresentationField("Status", entry.status),
            PresentationField("Detail", entry.detail),
        )
        for entry in report.injuries
    )
    return ConsumerPresentation(
        status="ok",
        title="Injury Report",
        fields=impactful_count_field,
        rows=rows,
        notes=(_FRESHNESS_NOTE,),
    )
