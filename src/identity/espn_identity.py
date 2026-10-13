"""
ESPN team-identity adapter (Phase 2A, adapter batch 1).

Source system: cbb_teamScrape_v15_TEST.ipynb cells 77-85 (`normalize_name`,
`build_slug_authority`, `build_resolved_slug_map`, `resolve_team_slug`,
`slug_to_filename`, `fetch_espn_teams`, `download_all_team_logos`). All six
functions are unshadowed (`defined_in_notebook_count == 1` each per
docs/function_manifest.csv), so no active-vs-stale ambiguity here, unlike
the normalize_team_name family.

Finding worth recording: `resolve_team_slug`'s only real call site is inside
`download_all_team_logos()` -- i.e. this entire cluster exists to match
ESPN's own team-list page (scraped fresh each run) to a local slug so the
pipeline knows which filename to save each team's ESPN-hosted logo PNG
under. It is NOT used for ESPN stat/injury lookups -- that's a separate,
already-migrated system (`src.scraper.lookup_espn_team_id` /
`compute_injury_deduction`, verified faithful in the previous session).
Consequently this module also satisfies the task's separately-listed "Logo
identity adapter" -- building a second one would just duplicate this code.

What changed vs. the notebook: `resolve_team_slug()` matched ESPN names
against two small, hand-maintained dicts (`TEAM_SLUG_MAP`: ~15 A-10 teams
plus 2 special cases; `TEAM_NAME_RESOLVER`: ~6 entries) OR a resolver built
by `build_resolved_slug_map(conferences, corrections)` at its actual call
site -- never validated against the full 365-team universe. This module's
`resolve_espn_team_identity()` keeps the same token-shortening algorithm
(strip trailing tokens -- almost always a mascot -- until a prefix
resolves) but routes each candidate prefix through
`src.identity.team_identity.resolve_team_identity()`, i.e. the full,
authoritative 365-team canonical registry, instead of either small dict.
"""
from __future__ import annotations

import re
import unicodedata

from src.identity.contracts import TeamIdentity, UnresolvedIdentity
from src.identity.team_identity import resolve_team_identity_by_shortening


def normalize_espn_name(name: str) -> str:
    """
    Verbatim port of notebook cell 77's ``normalize_name`` (unshadowed,
    single definition -- no notebook-parity ambiguity to resolve here).
    Its own docstring calls it "the backbone of all deterministic matching."
    """
    if not name:
        return ""

    name = unicodedata.normalize("NFKD", name)
    name = name.lower().strip()
    name = name.replace("&", "and")
    name = name.replace("’", "'")
    name = re.sub(r"[^\w\s()'-]", "", name)
    name = re.sub(r"\s+", " ", name)

    return name


def resolve_espn_team_identity(
    espn_name: str, *, source: str = "espn"
) -> TeamIdentity | UnresolvedIdentity:
    """
    Resolve an ESPN-scraped team name (often "<school> <mascot>", e.g.
    "St. Bonaventure Bonnies") to a canonical :class:`TeamIdentity`.

    Algorithm mirrors notebook cell 82's token-shortening approach, but
    resolves each candidate against the full canonical registry instead of
    a small hand-maintained dict -- see
    ``src.identity.team_identity.resolve_team_identity_by_shortening``
    (shared with the Covers adapter, which has the same "<team> <extra
    words>" shape) for the shortening/ambiguity-stopping logic itself.
    """
    return resolve_team_identity_by_shortening(espn_name, source=source)


def resolve_espn_team_slug(espn_name: str, *, prefer_source: str = "sports_reference") -> str | None:
    """
    Compatibility wrapper preserving the original ``resolve_team_slug()``'s
    output contract (``str | None``, a slug) for existing call sites like
    logo-download flows, while resolving through the canonical registry
    internally instead of ``TEAM_SLUG_MAP``/``TEAM_NAME_RESOLVER``.

    Returns ``None`` for not-found *and* for ambiguous (the old function had
    no ambiguous concept and would have returned a single guess or ``None``;
    since silently guessing is exactly what Phase 2A forbids, ambiguous
    collapses to ``None`` here -- callers that need to know *why* should use
    :func:`resolve_espn_team_identity` directly instead of this wrapper).
    """
    result = resolve_espn_team_identity(espn_name)
    if not isinstance(result, TeamIdentity):
        return None
    source_identity = result.sources.get(prefer_source)
    return source_identity.raw_value if source_identity else None
