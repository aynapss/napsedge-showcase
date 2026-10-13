"""
Phase 7 Slice 1: shared, framework-independent presentation contracts for
direct-service consumers (Discord, Streamlit, or any other same-process
caller) of the Phase 6 Team/Rankings/Injury services.

This module contains no `discord`/`streamlit`/`fastapi`/`starlette`/
`pydantic` import, no database/session/repository reference, no network,
filesystem, subprocess, or notebook access, and no module-level mutable
state -- it defines plain, frozen `@dataclass` types only, exactly
mirroring the framework-independence convention already established by
`src.services.contracts` and `src.auth.api_keys`.

These types are intentionally the smallest shared shape that can express
every outcome the three approved domains (Team, Rankings, Injury) need to
present truthfully -- a stable status, a short title, an optional summary
line, an ordered set of labeled scalar fields, an ordered set of tabular
rows (each itself an ordered set of labeled fields -- used only where a
domain outcome genuinely has more than one record, e.g. a Rankings
conflict), warnings, traceability notes, and a safe display message. It
deliberately contains no Discord embed/color, no Streamlit component, no
HTML/Markdown formatting assumption, no HTTP status code, no
character-limit chunking policy, and no pagination policy -- those are
presentation-*rendering* decisions for a future, separately-approved
consumer slice (Discord in Slice 2A), not this shared layer's concern.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: The five outcomes every domain adapter in this package can produce.
#: "ok" covers both a normal successful result and a truthfully-empty one
#: (e.g. a team with zero reported injuries) -- the two are distinguished
#: by `ConsumerPresentation.summary`/`rows`, never by a separate status,
#: since both are genuinely successful outcomes, not failures.
PresentationStatus = Literal["ok", "not_found", "conflict", "unavailable", "error"]


@dataclass(frozen=True)
class PresentationField:
    """One labeled scalar value. `value` is `None` exactly when the
    underlying service contract's own field was `None` -- never coerced
    to an empty string or a fabricated default, since doing so could
    change what the value means to whoever renders it."""

    label: str
    value: str | None


#: An ordered set of labeled fields describing one record -- used for
#: `ConsumerPresentation.rows` when a domain outcome genuinely has more
#: than one record to show without picking one (e.g. every conflicting
#: Rankings cache row, every reported Injury entry).
PresentationRow = tuple[PresentationField, ...]


@dataclass(frozen=True)
class ConsumerPresentation:
    """
    The one shared, immutable presentation result every adapter in this
    package returns. Two separate instances built from equal inputs
    compare equal (ordinary `@dataclass` field-by-field equality over
    immutable fields), and repeated calls with the same service result
    always produce an identical `ConsumerPresentation` -- no current-time
    insertion, no random identifier, no locale-dependent formatting.
    """

    status: PresentationStatus
    title: str
    #: A short, optional one-line summary distinct from `title` -- used,
    #: for example, to say "No injuries currently reported" without
    #: overloading `title` itself.
    summary: str | None = None
    fields: tuple[PresentationField, ...] = ()
    rows: tuple[PresentationRow, ...] = ()
    #: Non-fatal caveats a renderer should surface alongside an otherwise
    #: successful or conflicting result (e.g. "multiple conflicting rank
    #: records exist; none has been selected").
    warnings: tuple[str, ...] = ()
    #: Traceability/context notes that are not warnings (e.g. "Rankings
    #: reflect the locally cached snapshot only and may not be current")
    #: -- always a static, already-known disclaimer, never a fabricated
    #: freshness timestamp the underlying source does not actually carry.
    notes: tuple[str, ...] = ()
    #: A safe, ready-to-display message for `not_found`/`unavailable`/
    #: `conflict`/`error` outcomes -- `None` for a normal "ok" result
    #: that has nothing further to say beyond its own `fields`/`rows`.
    message: str | None = None
