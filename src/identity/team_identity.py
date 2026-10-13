"""
Canonical team-identity service (Phase 2A).

This is a facade over the existing ``src.slug_utils`` registry -- chosen as
the foundation per docs/migration_state.md's Phase 0 finding that it is the
most complete of the three (now confirmed: four+) competing team-identity
systems found in this codebase. It does NOT replace or modify
``src.slug_utils``; ``resolve_team()``/``try_resolve_team()`` keep working
exactly as before for existing callers.

Why a new function instead of reusing ``resolve_team()`` directly: that
function's step 4 does a fuzzy ``get_close_matches(..., n=1)`` and silently
returns whatever it finds -- exactly what the Phase 2A instructions say a
canonical service must not do ("Unknown or ambiguous teams must return a
structured unresolved result. Do not silently select the closest school.").
So ``resolve_team_identity`` below reimplements only steps 1-3 (the
deterministic ones: exact alias, SR override, canonical-slug derivation) and
turns step 4 into *suggestions* on an ``UnresolvedIdentity``, never an
auto-selected answer.

Scope note: this module resolves team identity through the SR/Torvik/Covers
registry only. It intentionally does NOT yet cover the notebook's other
team-normalization systems (ESPN's ``normalize_name``/``resolve_team_slug``,
the SR-schools-dataframe ``resolve_team_name``/``normalize_team_search_name``,
or the odds-specific ``canonical_odds_team``/``normalize_for_odds``/
``canonical_school`` family) -- those are source-specific adapters that
still need their own migration batches. See docs/notebook_src_coverage.csv
and docs/migration_state.md for the full inventory and why they're deferred.
"""
from __future__ import annotations

from collections import defaultdict
from difflib import SequenceMatcher, get_close_matches

from src import slug_utils
from src.identity.contracts import SourceIdentity, TeamIdentity, UnresolvedIdentity
from src.identity.team_shortening_guard import (
    find_unproven_dropped_tokens,
    find_unproven_shortened_tokens,
    is_shortened_match_proven_safe,
)
from src.ingestion.load_data import load_conferences
from src.exceptions import ConfigurationError
from src.logging_config import get_logger

log = get_logger(__name__)

_REGISTRY_READY = False
_ALIAS_MULTIMAPS: tuple[dict[str, set[int]], dict[str, set[int]]] | None = None

#: Fuzzy-suggestion cutoff -- matches src.slug_utils.resolve_team()'s own
#: threshold so "would this have silently matched under the old function?"
#: stays answerable, even though we never act on it automatically here.
_SUGGESTION_CUTOFF = 0.75
_MAX_SUGGESTIONS = 3


def ensure_team_registry() -> None:
    """
    Build ``src.slug_utils.TEAM_REGISTRY`` from the authoritative D1
    conferences universe (parsed live from the v15_TEST notebook's cell 8 via
    ``load_conferences()`` -- 33 conferences / 365 teams as of 2026-08-27),
    idempotently. Safe to call from every entry point; the underlying
    ``build_registry()`` no-ops on repeat data and this wrapper additionally
    skips the call entirely once it has succeeded in this process.
    """
    global _REGISTRY_READY
    if _REGISTRY_READY:
        return
    conferences = load_conferences()
    if not conferences:
        raise ConfigurationError(
            "load_conferences() returned no conferences -- cannot build the "
            "team identity registry. Check that cbb_teamScrape_v15_TEST.ipynb "
            "exists at src.config.paths.NOTEBOOK_PATH and still has a cell "
            "containing a top-level `conferences = {...}` assignment."
        )
    slug_utils.build_registry(conferences)
    _REGISTRY_READY = True


def _alias_multimaps() -> tuple[dict[str, set[int]], dict[str, set[int]]]:
    """
    Independently re-derive (normalized key -> set of team_ids) from each
    team's own known alias set, *without* going through the collapsed
    ``_ALIAS_TO_ID`` dict (which silently overwrites on collision -- last
    write wins, so it cannot tell us a collision happened). Cached per
    process since the registry is built once.
    """
    global _ALIAS_MULTIMAPS
    if _ALIAS_MULTIMAPS is not None:
        return _ALIAS_MULTIMAPS

    slug_map: dict[str, set[int]] = defaultdict(set)
    norm_map: dict[str, set[int]] = defaultdict(set)
    for team_id, rec in slug_utils.TEAM_REGISTRY.items():
        alias_pool = set(rec.aliases) | {rec.display_name.lower(), rec.sr_slug.lower()}
        for alias in alias_pool:
            slug_key = slug_utils.canonical_team_slug(alias)
            if slug_key:
                slug_map[slug_key].add(team_id)
            norm_key = slug_utils.normalize_team_name(alias)
            if norm_key:
                norm_map[norm_key].add(team_id)
    _ALIAS_MULTIMAPS = (dict(slug_map), dict(norm_map))
    return _ALIAS_MULTIMAPS


#: Curly apostrophe variants folded to straight ``'`` before any resolution
#: step runs. Decision (2026-08-27): the canonical identity layer treats
#: "Saint Joseph's" and "Saint Joseph’s" as equivalent input. This is a
#: deliberate, documented deviation from src.slug_utils.normalize_team_name()
#: (which matches the STALE notebook cell 10 and happens to strip ’ via
#: an explicit replacement) and from the notebook's actual ACTIVE cell 43
#: (which does not strip ’ at all -- see normalize_team_name_v15_active()
#: below for that exact behavior, preserved for anyone who needs bit-exact
#: notebook parity). Only applied here, inside the canonical service --
#: neither existing notebook version is modified.
_CURLY_APOSTROPHES = ("’", "‘")


def _fold_apostrophes(s: str) -> str:
    for curly in _CURLY_APOSTROPHES:
        s = s.replace(curly, "'")
    return s


def _team_identity_from_id(team_id: int) -> TeamIdentity:
    rec = slug_utils.TEAM_REGISTRY[team_id]
    sources = {"sports_reference": SourceIdentity("sports_reference", rec.sr_slug, rec.sr_slug)}
    for source_name, slug in rec.source_slugs.items():
        sources[source_name] = SourceIdentity(source_name, slug, slug)
    return TeamIdentity(
        team_id=team_id,
        display_name=rec.display_name,
        conference=rec.conference,
        sources=sources,
    )


def resolve_team_identity(query: str, *, source: str = "unknown") -> TeamIdentity | UnresolvedIdentity:
    """
    Resolve ``query`` to a canonical :class:`TeamIdentity` using only
    deterministic matches (exact alias, SR override, canonical-slug
    derivation). Returns :class:`UnresolvedIdentity` instead of guessing.

    ``team_id`` is reproducible across runs and processes: it's
    ``stable_team_id(sr_slug)``, a SHA-256 digest of the SR slug, so it does
    not depend on dict/conference iteration order.
    """
    ensure_team_registry()

    if not query or not isinstance(query, str):
        return UnresolvedIdentity(query=str(query), source=source, reason="not_found")

    s = _fold_apostrophes(query.strip())
    s_lower = s.lower()
    tid: int | None = None
    match_key: str | None = None

    # Step 1: exact alias match (case-insensitive, then original case)
    tid = slug_utils._ALIAS_TO_ID.get(s_lower) or slug_utils._ALIAS_TO_ID.get(s)
    if tid is not None:
        match_key = s_lower

    # Step 2: SR override -> slug lookup
    if tid is None:
        sr_slug = slug_utils._SR_OVERRIDES.get(s) or slug_utils._SR_OVERRIDES.get(s_lower)
        if sr_slug:
            tid = slug_utils._SR_SLUG_TO_ID.get(sr_slug.lower())
            if tid is not None:
                match_key = sr_slug.lower()

    # Step 3: canonical slug derivation -- the one lossy, derived path.
    # Gated by the meaningful-token shortening guard (Phase 3 pre-batch
    # identity correction 1): canonical_team_slug() strips both real
    # mascots (safe) and structural identity words like "college" or an
    # entire parenthetical qualifier (not safe) via the same mechanism, so
    # a hit here is only trusted if no meaningful token was silently lost.
    if tid is None:
        canon = slug_utils.canonical_team_slug(s)
        if canon:
            candidate_tid = slug_utils._ALIAS_TO_ID.get(canon)
            if candidate_tid is not None:
                if is_shortened_match_proven_safe(s, candidate_tid):
                    tid = candidate_tid
                    match_key = canon
                else:
                    log.info(
                        "rejected step-3 shortened match for %r (source=%s): "
                        "unproven tokens %s would be silently discarded",
                        query, source, sorted(find_unproven_shortened_tokens(s, candidate_tid)),
                    )

    if tid is not None:
        # Cross-check against the independently-derived alias multimap: an
        # exact/deterministic hit can still be the collapsed result of a
        # real collision (e.g. bare "Miami" -> both Miami (OH) and
        # Miami (FL) reduce their naive slug to "miami"; _ALIAS_TO_ID only
        # kept whichever was registered last). Don't trust it silently.
        slug_map, norm_map = _alias_multimaps()
        colliding_ids = slug_map.get(match_key, set()) | norm_map.get(match_key, set())
        if len(colliding_ids) > 1:
            log.warning(
                "collision-masked team query %r (source=%s) matched via "
                "key %r, which %d programs share: %s",
                query, source, match_key, len(colliding_ids),
                [slug_utils.TEAM_REGISTRY[t].display_name for t in sorted(colliding_ids)],
            )
            return UnresolvedIdentity(
                query=query,
                source=source,
                reason="ambiguous",
                candidates=[_team_identity_from_id(t) for t in sorted(colliding_ids)],
            )
        return _team_identity_from_id(tid)

    # No deterministic match. Offer fuzzy near-misses as suggestions only --
    # never auto-select (that's the behavior this function exists to avoid).
    candidates = list(slug_utils._ALIAS_TO_ID.keys())
    close = get_close_matches(s_lower, candidates, n=_MAX_SUGGESTIONS, cutoff=_SUGGESTION_CUTOFF)

    suggestion_team_ids = {slug_utils._ALIAS_TO_ID[alias] for alias in close}
    suggestions = [
        (slug_utils.TEAM_REGISTRY[slug_utils._ALIAS_TO_ID[alias]].display_name,
         round(SequenceMatcher(None, s_lower, alias).ratio(), 3))
        for alias in close
    ]

    if len(suggestion_team_ids) > 1:
        # Multiple *distinct* programs are plausible matches -- a real
        # ambiguity, not just multiple aliases of the same team.
        log.warning("ambiguous team query %r (source=%s): candidates=%s", query, source, suggestions)
        return UnresolvedIdentity(
            query=query,
            source=source,
            reason="ambiguous",
            suggestions=suggestions,
            candidates=[_team_identity_from_id(t) for t in sorted(suggestion_team_ids)],
        )

    log.info("unresolved team query %r (source=%s) suggestions=%s", query, source, suggestions)
    return UnresolvedIdentity(query=query, source=source, reason="not_found", suggestions=suggestions)


def resolve_team_identity_by_shortening(
    text: str, *, source: str = "unknown"
) -> TeamIdentity | UnresolvedIdentity:
    """
    Resolve free text that may have a trailing qualifier the registry
    doesn't know about (most commonly a mascot, e.g. "St. Bonaventure
    Bonnies") by trying the full string, then progressively dropping
    trailing whitespace-separated tokens and retrying, stopping at the
    first candidate that resolves cleanly.

    Shared by source adapters whose raw input is "<team> <extra words>"
    shaped -- e.g. ESPN's and Covers' own team-list pages both format
    entries this way. Do not call this directly for input that's already a
    clean team name/alias; use :func:`resolve_team_identity` for that (it's
    what this function calls internally, so a first attempt using the full
    string is already covered).

    If a candidate resolves *ambiguously* (e.g. over-shortening down to
    bare "Miami"), stop there and return that ambiguous result rather than
    shortening further -- a shorter string can only ever be more ambiguous,
    never less, so continuing can't help.
    """
    if not text:
        return UnresolvedIdentity(query=str(text), source=source, reason="not_found")

    tokens = text.split()
    last_not_found: UnresolvedIdentity | None = None

    for i in range(len(tokens), 0, -1):
        candidate = " ".join(tokens[:i])
        result = resolve_team_identity(candidate, source=source)

        if isinstance(result, TeamIdentity):
            # Same meaningful-token risk as step 3 inside resolve_team_identity,
            # but here the tokens are lost by trailing-token-dropping rather
            # than canonical_team_slug's own replacements -- e.g. "Colorado
            # College Tigers" -> drop "Tigers" (safe) -> "Colorado College"
            # -> drop "College" (NOT safe) -> "Colorado" resolves via a real
            # exact alias for an unrelated program. A shorter candidate can
            # only drop a superset of these tokens, so stop here rather than
            # trying to shorten further.
            dropped = tokens[i:]
            unproven = find_unproven_dropped_tokens(dropped, result.team_id)
            if unproven:
                log.info(
                    "%r shortened to %r, a real match for team_id=%s, but "
                    "dropped unproven meaningful token(s) %s -- treating as unresolved",
                    text, candidate, result.team_id, sorted(unproven),
                )
                return UnresolvedIdentity(query=text, source=source, reason="not_found")
            return result

        if result.reason == "ambiguous":
            log.warning(
                "%r shortened to ambiguous candidate %r before any clean "
                "match -- stopping rather than shortening further",
                text, candidate,
            )
            return result

        last_not_found = result

    log.info("%r did not resolve at any token length", text)
    return last_not_found or UnresolvedIdentity(query=text, source=source, reason="not_found")


def check_team_alias_collisions() -> list[dict]:
    """
    Scan every known alias of every registered team for normalized values
    that two *different* Division I programs both derive -- i.e. a real data
    collision that ``build_registry()``'s dict-based alias index would
    otherwise silently paper over (last write wins).

    Checks both of the registry's own normalization functions
    (``canonical_team_slug`` and ``normalize_team_name``) since they use
    different rules and could collide independently.

    Returns a list of ``{"normalized_key", "method", "team_ids",
    "display_names"}`` dicts -- empty if no collisions exist.
    """
    ensure_team_registry()

    slug_map: dict[str, set[int]] = defaultdict(set)
    norm_map: dict[str, set[int]] = defaultdict(set)

    for team_id, rec in slug_utils.TEAM_REGISTRY.items():
        alias_pool = set(rec.aliases) | {rec.display_name.lower(), rec.sr_slug.lower()}
        for alias in alias_pool:
            slug_key = slug_utils.canonical_team_slug(alias)
            if slug_key:
                slug_map[slug_key].add(team_id)
            norm_key = slug_utils.normalize_team_name(alias)
            if norm_key:
                norm_map[norm_key].add(team_id)

    collisions: list[dict] = []
    for method_name, mapping in (("canonical_team_slug", slug_map), ("normalize_team_name", norm_map)):
        for key, team_ids in mapping.items():
            if len(team_ids) > 1:
                collisions.append({
                    "normalized_key": key,
                    "method": method_name,
                    "team_ids": sorted(team_ids),
                    "display_names": [slug_utils.TEAM_REGISTRY[t].display_name for t in sorted(team_ids)],
                })
    return collisions
