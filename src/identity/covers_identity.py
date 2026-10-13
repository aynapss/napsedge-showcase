"""
Covers.com team-identity adapter (Phase 2A, adapter batch: Covers/injury).

Source system: cbb_teamScrape_v15_TEST.ipynb cells 301, 304, 306 (all
unshadowed, `defined_in_notebook_count == 1` each) -- `canonicalize_team`,
`covers_team_alias`, `is_relevant_injury`, `filter_relevant_injuries`,
`normalize_covers_status`, `normalize_covers_player`,
`build_covers_ncaab_slug_map`, `scrape_covers_injuries_for_teams`. All of
these were already migrated to `src/scraper.py` and verified faithful to
v15 in a prior session (see docs/notebook_src_coverage.csv) -- NOT modified
here. This module only adds identity-*routing* on top; the actual HTTP
scraping, player-name normalization, and injury-relevance logic are
untouched.

PRIMARY INVESTIGATION FINDING (see docs/migration_state.md for the full
writeup): `scrape_covers_injuries_for_teams(team_slugs, covers_slug_map)`
does `covers_slug_map.get(sr_slug)`, i.e. it looks up a Covers-derived dict
using a Sports-Reference slug as the key. This was checked empirically by
replicating `build_covers_ncaab_slug_map`'s exact key-generation logic
(4 key types per team) against 8 representative team/slug pairs -- 6 of them
taken verbatim from that function's own docstring examples
("saint-joseph-s-hawks", "miami-fl-hurricanes", "vcu-rams", "uab-blazers",
"smu-mustangs", "university-at-albany-great-danes") -- and comparing against
this codebase's actual `src.slug_utils` SR slugs:

    Saint Joseph's  sr_slug='saint-josephs'         -> NOT a covers-derived key (FAILS)
    Miami (FL)      sr_slug='miami-fl'              -> IS a covers-derived key (works, coincidentally)
    Miami (OH)      sr_slug='miami-oh'              -> IS a covers-derived key (works, coincidentally)
    Albany          sr_slug='albany-ny'             -> NOT a covers-derived key (FAILS)
    VCU             sr_slug='virginia-commonwealth'  -> NOT a covers-derived key (FAILS)
    UAB             sr_slug='alabama-birmingham'     -> NOT a covers-derived key (FAILS)
    SMU             sr_slug='southern-methodist'     -> NOT a covers-derived key (FAILS)
    St. Bonaventure sr_slug='st-bonaventure'         -> IS a covers-derived key (works, coincidentally)

Conclusion: this is scenario (2)/(3) from the investigation brief -- Covers-
derived keys merely *happen* to overlap SR-slug values for the narrow case
of a two-word "<School> <OneWordMascot>" Covers slug where dropping the
mascot exactly reconstructs the SR slug. It fails for apostrophe schools
(the "-s-" apostrophe encoding never round-trips to SR's plain "s"), for
schools where Covers brands with an abbreviation but SR spells out the full
name (VCU/UAB/SMU-shaped), and for full-legal-name schools (Albany). It also
confirmed scenario (4): both Miami teams' key sets include a bare "miami"
key (from the mascot-stripping step going one word too far), which
`build_covers_ncaab_slug_map`'s own `if ... not in slug_map` guard resolves
by silent first-write-wins -- whichever team's row Covers lists first in
its HTML wins that key, with no error or warning either way.

Per the parity rules, `src/scraper.py`'s functions are NOT changed here --
doing so would alter established v15 output, which requires explicit
approval, not a unilateral fix. This adapter instead gives new callers a
*correct*, registry-routed way to go from a canonical team to a Covers slug
(or vice versa) without inheriting the SR/Covers namespace conflation.
"""
from __future__ import annotations

import re

from src.identity.contracts import TeamIdentity, UnresolvedIdentity
from src.identity.team_identity import resolve_team_identity, resolve_team_identity_by_shortening
from src.logging_config import get_logger

log = get_logger(__name__)


def _restore_covers_apostrophe_encoding(covers_slug: str) -> str:
    """
    Reverse Covers' own apostrophe encoding, using the exact same regex
    `src.scraper.build_covers_ncaab_slug_map` already uses to derive its
    "Key 3" (apostrophe-restoration variant): "-s-" -> "'s-". E.g.
    "saint-joseph-s-hawks" -> "saint-joseph's-hawks". Not a new guess -- it's
    the notebook/src code's own documented transformation, applied in
    reverse-of-forward (same substitution either direction here, since it's
    a single regex, not a round trip pair).
    """
    return re.sub(r"-s-", "'s-", covers_slug)


def _covers_input_to_words(covers_name_or_slug: str) -> str:
    """
    Normalize either shape of Covers-sourced input to space-separated words
    ready for token-shortening: a raw Covers URL slug (all lowercase,
    hyphen-separated, e.g. "saint-joseph-s-hawks") or an already-human
    display name (e.g. "Saint Joseph's Hawks", possibly with a real
    apostrophe already). Slug-shaped input is detected by containing only
    ``[a-z0-9-]`` characters.
    """
    s = covers_name_or_slug.strip()
    if re.fullmatch(r"[a-z0-9-]+", s):
        s = _restore_covers_apostrophe_encoding(s)
        s = s.replace("-", " ")
    return s


def resolve_covers_team_identity(
    covers_name_or_slug: str, *, source: str = "covers"
) -> TeamIdentity | UnresolvedIdentity:
    """
    Resolve Covers-sourced input (a raw Covers URL slug like
    "saint-joseph-s-hawks", or a display name like "Saint Joseph's Hawks")
    to a canonical :class:`TeamIdentity`.

    Unlike ``covers_slug_map.get(sr_slug)`` in ``src/scraper.py``, this does
    not assume SR-slug and Covers-slug namespaces are interchangeable: it
    reconstructs Covers' own apostrophe encoding, then routes the resulting
    words through the same token-shortening + canonical-registry resolution
    used by the ESPN adapter (Covers team-list entries have the same
    "<school> <mascot>" shape as ESPN's).
    """
    if not covers_name_or_slug:
        return UnresolvedIdentity(query=str(covers_name_or_slug), source=source, reason="not_found")

    words = _covers_input_to_words(covers_name_or_slug)
    return resolve_team_identity_by_shortening(words, source=source)


def resolve_covers_slug_via_registry(
    team_query: str,
    covers_slug_map: dict[str, str],
    *,
    source: str = "covers",
) -> str | None:
    """
    Given a canonical team query and an already-built ``covers_slug_map``
    (the output of ``src.scraper.build_covers_ncaab_slug_map`` -- this
    function does not build or fetch one itself, no network calls here),
    find that team's Covers slug.

    Design note -- why this doesn't just try more candidate *keys*: an
    earlier version of this function generated a few plausible canonical-side
    keys (``sr_slug``, ``canonical_team_slug(display_name)``, a hyphenated
    display name) and looked each up in the map. That still failed for
    exactly the same teams the original ``.get(sr_slug)`` failed for --
    Saint Joseph's, VCU, Davidson -- because none of those keys can ever
    equal what Covers actually put in the map. Covers' keys are derived from
    its own slug, which encodes the team's *mascot*
    ("saint-joseph-s-**hawks**"), and the canonical registry has no mascot
    data to reconstruct that from. No amount of guessing forward from the
    canonical side can produce a string neither side ever generated.

    So this inverts the direction instead: it takes the map's *values*
    (the actual Covers slugs collected from HTML, e.g. "vcu-rams" --
    unambiguous, one per real team) and resolves each one with
    :func:`resolve_covers_team_identity`, which already handles apostrophe
    encoding and mascot-stripping correctly via the full canonical registry
    (verified above for Saint Joseph's, VCU, Miami, Loyola). Whichever
    Covers slug resolves to the same team_id as ``team_query`` is the
    answer.

    Returns ``None`` -- not a guess -- if no Covers slug in the map
    resolves to this team. No fuzzy string matching against the map's keys
    at any point.
    """
    result = resolve_team_identity(team_query, source=source)
    if not isinstance(result, TeamIdentity):
        return None

    for covers_slug in set(covers_slug_map.values()):
        candidate = resolve_covers_team_identity(covers_slug, source=source)
        if isinstance(candidate, TeamIdentity) and candidate.team_id == result.team_id:
            log.info(
                "resolved Covers slug for %r (team_id=%d) -> %r via inverse resolution",
                team_query, result.team_id, covers_slug,
            )
            return covers_slug

    log.info(
        "no Covers slug in the supplied map resolves to %r (team_id=%d)",
        team_query, result.team_id,
    )
    return None
