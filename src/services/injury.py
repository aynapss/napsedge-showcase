"""
Read-only Injury service using local cached data only (Phase 6, Slice 6A).

Mirrors `src.services.team`/`src.services.player`/`src.services.rankings`'s
structure: independent of FastAPI, calls only the existing, unmodified
`src.features.team_summary.build_injury_report` authority (extracted this
slice from the original private `_injury_report`), and returns plain
`@dataclass` results. Performs no scraping, no live request, no database
access, no medical inference, and no identity resolution of its own -- a
caller that has only a name/alias must resolve it to a `team_slug` first,
exactly matching the Team/Player/Rankings precedent.

**Phase 6, Slice 6B** adds `get_injury_report_for_team`, the one
API-capable operation that coordinates canonical Team resolution (via
the existing, unmodified `src.services.team.get_team`) with this same
local-cache Injury report, and is the ONLY function in this module that
opens a database session or translates the malformed-element limitation
below into a sanitized exception. `get_team_injury_report`/
`build_injury_report` remain completely unchanged, DB-free, and continue
to raise the original `AttributeError` for any caller outside that one
translation boundary -- Slice 6A's own extraction-parity tests keep
proving this directly.

**Known, preserved-not-fixed service-boundary limitation**: if a cache
file's JSON list contains an element that is not itself a dict, the
extracted authority (`build_injury_report`) raises an uncaught
`AttributeError` -- true of the original `_injury_report` before Slice
6A touched anything, and deliberately preserved rather than papered over
(see `_to_entry`'s own docstring and
`tests/regression/test_services_injury.py`'s dedicated parity tests).
`get_injury_report_for_team` (only) catches this specific exception and
re-raises it as `src.exceptions.InjuryDataIntegrityError` (chained via
`raise ... from exc`), which `src.api.errors` maps to a sanitized HTTP
503 -- never exposing the malformed element, the cache path, or the
original exception text.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from sqlalchemy.orm import Session

from src.exceptions import InjuryDataIntegrityError
from src.features.team_summary import build_injury_report
from src.services.contracts import InjuryEntry, TeamInjuryLookupResult, TeamInjuryReport
from src.services.team import get_team


def _to_entry(item: dict[str, Any]) -> InjuryEntry:
    """
    `item` is one element of the already-validated list `build_injury_report`
    returned for `outcome="loaded"`. Note this service does not add any new
    per-element shape validation of its own: the extracted authority itself
    (`build_injury_report`, preserved byte-for-byte from the original
    `_injury_report`) already computes `impactful_count` by unconditionally
    calling `.get("status", ...)` on every list element -- a non-dict
    element raises `AttributeError` there, before this function is ever
    reached, exactly matching the original (unmodified) function's own
    pre-existing behavior. Fixing that would be a behavior change beyond
    this slice's extraction-only scope, so it is preserved, not papered
    over here.
    """
    return InjuryEntry(player=item.get("player"), status=item.get("status"), detail=item.get("detail"), raw=dict(item))


def get_team_injury_report(team_slug: str, *, search_paths: Sequence[str | Path] | None = None) -> TeamInjuryReport:
    """
    Local-cache-only injury report for one team. Calls
    `build_injury_report` exactly once; every field is preserved from its
    result truthfully -- no filtering, no re-deriving `impactful_count`,
    no normalization of `status` strings, no fabricated `as_of`/freshness
    value (this cache carries none, exactly like the Rankings cache).
    """
    raw = build_injury_report(team_slug, search_paths=search_paths)
    injuries = tuple(_to_entry(item) for item in raw["injuries"])
    return TeamInjuryReport(
        outcome=raw["status"],
        team_slug=team_slug,
        source_path=raw["source"],
        injuries=injuries,
        impactful_count=raw["impactful_count"],
        detail=raw.get("message"),
    )


def get_injury_report_for_team(
    session: Session, team_id: int, *, search_paths: Sequence[str | Path] | None = None
) -> TeamInjuryLookupResult:
    """
    Phase 6, Slice 6B: the one API-capable Injury operation. Resolves the
    canonical `team_id` through the existing, unmodified
    `src.services.team.get_team` (never a second identity/normalization
    system of its own), then calls `get_team_injury_report` with that
    team's own `canonical_slug` -- the same slug convention
    `build_injury_report`'s candidate paths already expect (confirmed:
    `TeamRecord.canonical_slug` is `Team.sr_slug`, and the real local
    Injury cache is keyed by that identical slug, e.g. `"duke"`).

    Never opens its own database transaction beyond the one `session` it
    is handed by the caller (the API route's own `get_session` dependency,
    exactly like every other route); never constructs a repository or a
    SQL statement directly.

    Translates the one preserved-not-fixed Slice 6A limitation (a
    malformed cache element raising `AttributeError`) into
    `InjuryDataIntegrityError` -- this is the ONLY call site in this
    module (or anywhere else) that does so; `get_team_injury_report`
    itself is untouched and keeps raising the original `AttributeError`
    for any other caller. The translation is deliberately narrow: an
    `AttributeError` is only ever re-raised as `InjuryDataIntegrityError`
    when its own deepest traceback frame is inside
    `src/features/team_summary.py` (the extracted authority's own
    module) -- an `AttributeError` from any other cause (a genuinely
    unrelated bug, a different dependency) propagates completely
    unchanged, so it is never mislabeled as an Injury cache integrity
    problem and still reaches the generic, centrally-handled 500 path.
    """
    team_lookup = get_team(session, team_id)
    if team_lookup.outcome == "not_found":
        return TeamInjuryLookupResult(outcome="team_not_found")

    canonical_slug = team_lookup.team.canonical_slug
    try:
        report = get_team_injury_report(canonical_slug, search_paths=search_paths)
    except AttributeError as exc:
        if not _originates_in_injury_authority(exc):
            raise
        raise InjuryDataIntegrityError(
            "the configured injury cache contains a malformed entry for this team"
        ) from exc
    return TeamInjuryLookupResult(outcome="found", report=report)


def _originates_in_injury_authority(exc: AttributeError) -> bool:
    """True only if `exc`'s own deepest traceback frame is inside `src/features/team_summary.py`."""
    tb = exc.__traceback__
    if tb is None:
        return False
    while tb.tb_next is not None:
        tb = tb.tb_next
    return tb.tb_frame.f_code.co_filename.endswith("team_summary.py")
