"""
Meaningful-token shortening guard (Phase 3 pre-batch identity correction 1).

CONFIRMED ROOT CAUSE

Two related lossy paths in team resolution can silently discard a
structurally meaningful word and land on the wrong, unrelated program:

1. `resolve_team_identity`'s step 3 ("canonical slug derivation") calls
   `slug_utils.canonical_team_slug()`, which -- confirmed by reading its
   source -- unconditionally strips both real mascot words (safe: a
   mascot nickname never distinguishes *which* real program a query
   means) and several structural identity words via a flat, non-word-
   boundary-aware replacements dict: ``"college"``, ``"university"``,
   ``"the"``, plus (via a separate regex) the **entire contents of any
   parenthetical group** -- e.g. ``"(MO)"`` -- regardless of whether that
   parenthetical is a meaningful state/regional disambiguator.
2. `resolve_team_identity_by_shortening`'s trailing-token-dropping loop
   has the identical risk one level up: it drops whole trailing words
   (mascot or not) and accepts the first candidate that resolves *at
   all*, with no concept of "was the dropped word meaningful."

Two real collisions traced directly to these (Gate 1, Phase 2):
``"Colorado College Tigers"`` -> drops ``"Tigers"`` (mascot, safe) then
``"College"`` (NOT safe) -> collides with the real `Colorado` (Big 12)
program. ``"Washington (MO) Bears"`` -> drops the entire ``"(MO)"``
parenthetical (NOT safe) then ``"Bears"`` (mascot, safe) -> collides with
the real `Washington` (Huskies) program.

DESIGN: A SMALL, TARGETED "REQUIRES-PROOF" LIST -- NOT A MASCOT WHITELIST

An earlier version of this guard tried to whitelist "known-safe mascot
words" (mirroring `canonical_team_slug`'s own short, non-exhaustive
mascot list). That approach broke real teams whose mascots simply aren't
in that list (e.g. "Duke Blue Devils" -> dropping "Blue"/"Devils" was
wrongly treated as unproven, since neither word was in the copied
whitelist) -- confirmed by testing against real D1 names, not assumed.

Mascots are effectively unbounded in vocabulary (365+ D1 programs, many
with unique nicknames), so a whitelist can never be complete. The
defect categories this correction actually needs to guard are narrow and
enumerable, per the approved requirements: **"College"**, **parenthetical
location qualifiers**, and **state/regional disambiguators**. So instead
of trying to recognize every safe mascot, this module recognizes the
*few* specific shapes of token that are almost always meaningful:

- the literal word "college", "university", or "state" (case-insensitive)
- any token that appeared inside a parenthetical group in the original
  text (a location/regional qualifier -- e.g. "(MO)", "(FL)")
- a short (2-4 letter) ALL-CAPS token in the original text (the common
  written form of a state/regional abbreviation, e.g. "MO", "IL")

Any *other* dropped/removed word (ordinary-case, not in this short list)
is treated as safe decoration, exactly matching v15's own intent for
mascot-stripping -- this is what keeps "Duke Blue Devils" and every other
ordinary "<team> <mascot>" query working exactly as before.

A token flagged this way still doesn't block the match if it's *echoed*
in the matched team's own known display name/aliases (e.g. "Boston
College" legitimately contains "college" itself, so losing it there is
provably safe -- the target's own identity still carries it).

If a flagged token is lost and not echoed anywhere in the matched team's
own known forms, the match is unproven -- the caller treats this as
`not_found` rather than a false positive. No `src/slug_utils.py` edit was
made or is required -- `canonical_team_slug()` remains byte-identical,
preserved for every existing legacy/compatibility caller. This guard
gates only the two lossy paths described above; exact alias and SR-
override matches (steps 1-2 of `resolve_team_identity`) are curated,
deterministic data and are not gated here.
"""
from __future__ import annotations

import re

from src import slug_utils

#: The few word-shapes this migration has evidence are structurally
#: meaningful enough to require proof before discarding -- not an attempt
#: at an exhaustive "meaningful word" dictionary.
_MEANINGFUL_DISAMBIGUATOR_WORDS: frozenset[str] = frozenset({"college", "university", "state"})

_TOKEN_RX = re.compile(r"[a-z]+")


def _tokenize(text: str) -> set[str]:
    return set(_TOKEN_RX.findall(text.lower()))


def _is_meaningful_dropped_token(raw_token: str) -> bool:
    """
    Classifies one dropped token (in its *original* case/punctuation, as
    it appeared in the source text) as requiring proof before it can be
    silently discarded. See module docstring for the three recognized
    shapes; everything else (ordinary-case mascot/descriptor words) is
    treated as safe decoration.
    """
    if "(" in raw_token or ")" in raw_token:
        return True

    alpha_only = re.sub(r"[^A-Za-z]", "", raw_token)
    if not alpha_only:
        return False

    if alpha_only.lower() in _MEANINGFUL_DISAMBIGUATOR_WORDS:
        return True

    if 2 <= len(alpha_only) <= 4 and alpha_only.isupper():
        return True

    return False


def _known_forms_tokens(team_id: int) -> set[str] | None:
    """All tokens across a team's registered display name and every alias pointing at it."""
    rec = slug_utils.TEAM_REGISTRY.get(team_id)
    if rec is None:
        return None
    tokens = _tokenize(rec.display_name)
    for alias, tid in slug_utils._ALIAS_TO_ID.items():
        if tid == team_id:
            tokens |= _tokenize(alias)
    return tokens


def find_unproven_shortened_tokens(query: str, matched_team_id: int) -> set[str]:
    """
    Returns the set of (lowercased) tokens `canonical_team_slug()` removed
    from `query` that are flagged as requiring proof (see module
    docstring) and are not echoed anywhere in `matched_team_id`'s own
    registered display name/aliases. A non-empty result means accepting
    this match would silently discard a token the query asserted
    mattered. An empty result means the match is provably safe.
    """
    canon = slug_utils.canonical_team_slug(query)
    canon_tokens = _tokenize(canon.replace("-", " "))

    meaningful_removed: set[str] = set()
    for raw in query.split():
        alpha_only = re.sub(r"[^A-Za-z]", "", raw)
        lower = alpha_only.lower()
        if not lower or lower in canon_tokens:
            continue  # not removed by canonicalization
        if _is_meaningful_dropped_token(raw):
            meaningful_removed.add(lower)

    if not meaningful_removed:
        return set()

    known_forms = _known_forms_tokens(matched_team_id)
    if known_forms is None:
        return meaningful_removed  # can't verify against a real registry record -- treat as unproven

    return meaningful_removed - known_forms


def is_shortened_match_proven_safe(query: str, matched_team_id: int) -> bool:
    """True when no meaningful, unproven token would be silently discarded by accepting this match."""
    return not find_unproven_shortened_tokens(query, matched_team_id)


def find_unproven_dropped_tokens(dropped_tokens: list[str], matched_team_id: int) -> set[str]:
    """
    Same classification as `find_unproven_shortened_tokens`, applied to an
    explicit list of *raw* (original-case) tokens a caller cut off the end
    of a query -- used by `resolve_team_identity_by_shortening`'s own
    trailing-token-dropping loop, which has the identical risk one level
    up from step 3 (see module docstring).
    """
    meaningful: set[str] = set()
    for raw in dropped_tokens:
        if not raw:
            continue
        alpha_only = re.sub(r"[^A-Za-z]", "", raw)
        lower = alpha_only.lower()
        if lower and _is_meaningful_dropped_token(raw):
            meaningful.add(lower)

    if not meaningful:
        return set()

    known_forms = _known_forms_tokens(matched_team_id)
    if known_forms is None:
        return meaningful

    return meaningful - known_forms
