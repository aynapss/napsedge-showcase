"""
Sportsbook-identity adapter (Phase 2, Gate 2).

INVENTORY (Gate 2 investigation)

`get_live_ncaab_odds()` (notebook cell 448) is the only place v15 actually
queries The Odds API for sportsbook data, and it hardcodes
``bookmakers="fanduel"`` as its default -- confirmed by reading the source,
not inferred. No other sportsbook brand string (DraftKings, BetMGM,
Caesars, PointsBet, etc.) appears anywhere else in the notebook. The Odds
API's own response shape uses a stable lowercase ``key`` per bookmaker
(e.g. ``"fanduel"``) alongside a human-readable ``title`` (e.g.
``"FanDuel"``) -- `bookmaker`/`bookmakers` references elsewhere in the
notebook (`build_market_consensus` and friends, cells 476-525) consume
that same shape without introducing a second naming convention.

DESIGN

Per instruction ("Create a stable canonical sportsbook registry from the
sportsbooks actually used by v15"), this registry is seeded with exactly
the one confirmed real entry -- FanDuel -- rather than pre-populating
other well-known books that were never observed in this codebase's own
usage. `sportsbook_id` is `stable_namespaced_id("sportsbook", canonical_key)`,
keyed on the API's own lowercase `key`, so a `title` capitalization/
punctuation change never changes the id. An unrecognized provider key is
`not_found` -- this module does not infer one brand from another (e.g. it
never assumes "draft kings" means "draftkings"; only exact, case/
punctuation-normalized matches against the seeded registry resolve).
"""
from __future__ import annotations

import re

from src.identity.contracts import SportsbookIdentity, UnresolvedIdentity
from src.identity.stable_id import stable_namespaced_id
from src.logging_config import get_logger

log = get_logger(__name__)

#: canonical_key -> display_name. Seeded ONLY with sportsbooks confirmed by
#: reading v15's actual usage (see module docstring) -- not a general
#: industry list.
SPORTSBOOK_REGISTRY: dict[str, str] = {
    "fanduel": "FanDuel",
}

_NORMALIZE_RX = re.compile(r"[^a-z0-9]")


def _normalize_key(raw: str) -> str:
    """Case/punctuation-insensitive normalization only -- no fuzzy matching."""
    return _NORMALIZE_RX.sub("", raw.lower().strip())


_NORMALIZED_LOOKUP: dict[str, str] = {_normalize_key(k): k for k in SPORTSBOOK_REGISTRY}


def resolve_sportsbook_identity(query: str, *, source: str = "sportsbook") -> SportsbookIdentity | UnresolvedIdentity:
    """
    Resolve a sportsbook API key or display name to a canonical
    `SportsbookIdentity`. Matches exactly against `SPORTSBOOK_REGISTRY`'s
    keys and display names, tolerant only of case/punctuation/whitespace
    differences ("DraftKings" / "draftkings" / "Draft Kings" would all
    normalize the same way if that provider were ever added) -- never
    fuzzy, never inferred from a different brand. An unknown provider is
    `not_found`.
    """
    if not query or not isinstance(query, str):
        return UnresolvedIdentity(query=str(query), source=source, reason="not_found")

    normalized = _normalize_key(query)

    canonical_key = _NORMALIZED_LOOKUP.get(normalized)
    if canonical_key is None:
        # Also allow matching by normalized display name (e.g. "FanDuel").
        for key, display in SPORTSBOOK_REGISTRY.items():
            if _normalize_key(display) == normalized:
                canonical_key = key
                break

    if canonical_key is None:
        return UnresolvedIdentity(query=query, source=source, reason="not_found")

    return SportsbookIdentity(
        sportsbook_id=stable_namespaced_id("sportsbook", canonical_key),
        canonical_key=canonical_key,
        display_name=SPORTSBOOK_REGISTRY[canonical_key],
    )


def find_sportsbook_id_collisions(queries: list[str]) -> dict[int, list[str]]:
    """
    Diagnostic: groups a list of raw sportsbook query strings by the
    `sportsbook_id` they'd resolve to -- surfaces any case where two
    differently-spelled inputs collapse onto the same canonical provider
    (expected/benign), without ever silently treating two *different*
    real providers as the same one (unresolved inputs are excluded, never
    grouped together under a fabricated shared id).
    """
    by_id: dict[int, list[str]] = {}
    for q in queries:
        result = resolve_sportsbook_identity(q)
        if isinstance(result, SportsbookIdentity):
            by_id.setdefault(result.sportsbook_id, []).append(q)
    return {sid: qs for sid, qs in by_id.items() if len(qs) > 1}
