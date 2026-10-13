"""
Phase 7 Slice 1: sanitized exception-to-presentation mapping for direct-
service consumers, scoped to exactly the internal exceptions relevant to
Team, Rankings, and Injury.

This is a **sibling** utility, not something the adapters in `team.py`/
`rankings.py`/`injury.py` call internally -- those adapters only ever
receive an already-computed service *result* object (never an
exception), since a service call that raises never produces a result to
adapt in the first place. `present_error` exists for a future caller
(e.g. a Discord command, Slice 2A) to use directly around its own
service call, in its own `except` block.

This module mirrors the same semantic sanitization policy already
established in `src.api.errors` (which exception is safe to expose
verbatim versus which must be sanitized to a generic message) -- it does
**not** import `src.api.errors` or anything from `src.api.*`, matching
this whole package's framework-independence requirement.
"""
from __future__ import annotations

from src.consumers.contracts import ConsumerPresentation
from src.exceptions import (
    IdentityConflictError,
    InjuryDataIntegrityError,
    RankingsCacheIntegrityError,
    RankingsConfigurationError,
)

#: `IdentityConflictError` (raised by `src.services.team.resolve_team` for
#: an ambiguous query) already carries a safe, hand-authored message
#: (e.g. "team query 'Miami' is ambiguous between: Miami (FL), Miami
#: (OH)") -- exposed verbatim here, mirroring `src.api.errors`'s own
#: `_EXPOSE_DETAIL` precedent for this exact exception type.
_CONFLICT_EXCEPTIONS: tuple[type[Exception], ...] = (IdentityConflictError,)

#: Each of these represents a server-side configuration/integrity
#: problem, never a real client-facing conflict or not-found outcome --
#: sanitized to one generic, non-specific message, mirroring
#: `src.api.errors`'s own `_SANITIZED` treatment of the identical
#: exception types. Never exposes the malformed configuration value, the
#: cache path, or the original exception text.
_SANITIZED_UNAVAILABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    RankingsConfigurationError,
    RankingsCacheIntegrityError,
    InjuryDataIntegrityError,
)

_GENERIC_UNAVAILABLE_MESSAGE = "This data is temporarily unavailable right now. Please try again later."
_GENERIC_ERROR_MESSAGE = "An internal error occurred."


def present_error(exc: Exception) -> ConsumerPresentation:
    """
    Maps `exc` to a sanitized `ConsumerPresentation`. Every exception
    type not explicitly named above -- including a genuine programming
    defect -- falls through to the generic `"error"` presentation; this
    function never re-raises and never inspects `exc`'s own message for
    any exception outside `_CONFLICT_EXCEPTIONS`, so no path, credential,
    SQL fragment, configuration value, traceback, or host detail can ever
    reach the returned presentation.
    """
    if isinstance(exc, _CONFLICT_EXCEPTIONS):
        return ConsumerPresentation(status="conflict", title="Ambiguous Result", message=str(exc))
    if isinstance(exc, _SANITIZED_UNAVAILABLE_EXCEPTIONS):
        return ConsumerPresentation(status="unavailable", title="Temporarily Unavailable", message=_GENERIC_UNAVAILABLE_MESSAGE)
    return ConsumerPresentation(status="error", title="Internal Error", message=_GENERIC_ERROR_MESSAGE)
