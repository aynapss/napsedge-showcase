"""
Model-slug team-identity adapter (Phase 2A, adapter batch 6 -- final).

INVESTIGATION SUMMARY

"Model slug" turns out not to be a genuinely separate, actively-used
namespace in the way the earlier five adapters' source systems were.

- Notebook cell 366's ``team_name_to_slug`` -- the function whose own
  docstring claims *"Canonical MODEL slug. Single source of truth."* --
  has **zero confirmed call sites anywhere in the notebook**. It is dead
  code. (Verified faithfully ported below anyway, since the task names it
  explicitly, and because a caller could exist outside the parts of the
  notebook this migration has inspected.)
- The real, actively-used "model-facing" team identifier is the same
  pipeline `team_slug` used throughout Sports-Reference scraping/caching
  (`TeamDump/CBB/<slug>/`), consumed by `src/ovr_model.py` (read-only
  reference for this batch, not modified) via `_resolve_team_slug()` /
  `get_team_folder()`, and produced upstream by the notebook's
  `resolve_team_profile()` / `build_team_profile_from_allstats()` (cells
  364/360) -- both of which *require* the caller to already supply
  `team_slug` in the profile dict; `resolve_team_profile`'s only slug-
  *computing* code path (string input -> `canonical_team_slug(team_name)`)
  is followed immediately by an unconditional `raise TypeError`, i.e. it's
  dead-ended, half-finished scaffolding, not a working code path.
- `ovr_model.py`'s `_resolve_team_slug()` already imports
  `src.slug_utils._SR_OVERRIDES` directly as its own `MANUAL_SLUG_OVERRIDES`
  (top of that file) -- so the "model slug" system is *already*, natively,
  wired to the same canonical override table every other adapter in this
  migration uses. It is an alias/consumer, not an independent system
  (objective #2's answer: yes, it aliases the canonical/SR override table).

WHAT MODEL SLUGS ARE ACTUALLY USED FOR (objective #5)

Exclusively **filesystem folder and file names** -- `get_team_folder()`
returns `CBB_DIR / slug`; downstream code reads `*_AllStats.xlsx` /
`*_game_log.csv` from that folder. No serialized model weights, no
pickled/JSON prediction cache, no persisted benchmark artifact anywhere in
`src/ovr_model.py` is keyed by team slug (checked: its three
`@lru_cache`-decorated functions are process-local and either take no
team-specific key at all, or (`_load_ncaa_bench`) glob *all* team folders
rather than looking one up by slug). So changing what a "model slug"
resolves to cannot corrupt a trained model or a saved prediction -- the
real risk (objective #6) is purely **pointing at the wrong, or a
nonexistent, folder on disk**.

A REAL, CONFIRMED IN-INGESTION DEFECT (found this session, NOT fixed)

`TeamDump/CBB/` (459 real folders) contains at least 9 folders corrupted
by what looks like a naive, word-boundary-free `"st"` -> `"saint"`
substring replace somewhere upstream in the scraping/caching pipeline:
`alabama-saint-hornets` (should be `alabama-state...`), `alcorn-saint-braves`,
`arkansas-saint-red-wolves`, `cal-baptisaint-lancers` (from "Baptist"),
`easaint-carolina-pirates` (from "East" -- "st" inside "Ea**st**"),
`delaware-saint-hornets`, `florida-saint-seminoles`, `indiana-saint-sycamores`,
`jacksonville-saint-gamecocks`, and more. **`alabama-state` also exists as
a separate, correctly-named folder for the same team** -- i.e. this is a
real duplicate-folder-key case, not hypothetical. This is an ingestion-time
defect (something scraped/cached the data under a corrupted name), not an
identity-resolution defect, and per this batch's explicit instruction it
is recorded here and in docs/migration_state.md's ledger as a **deferred
Phase 3 ingestion defect** -- not renamed, not fixed, not worked around
with fuzzy matching.

A second, independent duplicate-folder case (also found, also not fixed):
`TeamDump/CBB/` has **three separate folders for St. Bonaventure** --
`st bonaventure` (space), `st-bonaventure` (the clean canonical form), and
`st. bonaventure` (period) -- from evidently-repeated scrapes under
different raw-name variants. Because Windows' filesystem is
case/punctuation-loose for `Path.exists()`, `resolve_team_slug_v15_compat`'s
*first* candidate (the raw, unslugified query string) can match whichever
of these three the OS happens to resolve to, which is why that legacy
function is left exactly as order-dependent as the original --
:func:`resolve_model_team_folder` avoids this by always trying the
canonical `sr_slug` first, which deterministically picks `st-bonaventure`
regardless of what raw string the caller passed in.
"""
from __future__ import annotations

import re

from src.identity.contracts import TeamIdentity, UnresolvedIdentity
from src.identity.team_identity import resolve_team_identity
from src.logging_config import get_logger
from src.paths import CBB_DIR
from src import slug_utils

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Legacy compatibility wrappers -- byte-faithful to v15 / existing src
# ---------------------------------------------------------------------------


def team_name_to_slug_v15(name: str) -> str:
    """
    Verbatim port of notebook cell 366's ``team_name_to_slug``. Dead code
    in the notebook (no confirmed callers) -- ported for completeness/
    reference since the task names it explicitly, not because anything
    depends on it.
    """
    s = name.lower()
    s = re.sub(r"\([^)]*\)", "", s)
    s = s.replace("&", "and")
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s


def _simple_slug_v15(value: str) -> str:
    """Verbatim port of src/ovr_model.py's `_simple_slug` (read-only reference, not modified)."""
    text = str(value).lower().strip()
    text = text.replace("&", "and")
    text = re.sub(r"[()\"']", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def resolve_team_slug_v15_compat(team_value: str, *, cbb_dir=None) -> str:
    """
    Verbatim port of src/ovr_model.py's `_resolve_team_slug` (that file is
    a read-only reference for this batch, not modified). Same candidate
    order, same ``MANUAL_SLUG_OVERRIDES`` source
    (``src.slug_utils._SR_OVERRIDES``, already a direct import in
    ovr_model.py -- confirmed, not re-guessed here), same unconditional
    ``_simple_slug`` fallback even when no candidate exists on disk --
    preserved for exact parity. ``cbb_dir`` defaults to
    ``src.paths.CBB_DIR``; injectable for tests so this doesn't require
    the real ``TeamDump/CBB/`` tree to exist.
    """
    base_dir = CBB_DIR if cbb_dir is None else cbb_dir
    manual_slug_overrides = slug_utils._SR_OVERRIDES

    raw = str(team_value).strip()
    candidates = [
        raw,
        _simple_slug_v15(raw),
        _simple_slug_v15(raw).replace("saint-", "st-"),
        _simple_slug_v15(raw).replace("st-", "saint-"),
        manual_slug_overrides.get(raw, ""),
        manual_slug_overrides.get(_simple_slug_v15(raw), ""),
    ]

    for candidate in candidates:
        if candidate and (base_dir / candidate).exists():
            return candidate

    return _simple_slug_v15(raw)


# ---------------------------------------------------------------------------
# New canonical path -- no fuzzy matching, structured ambiguous/unresolved
# ---------------------------------------------------------------------------


def resolve_model_team_identity(
    query: str, *, source: str = "model"
) -> TeamIdentity | UnresolvedIdentity:
    """
    Resolve a model-facing team string/slug to a canonical
    :class:`TeamIdentity`.

    Plain ``resolve_team_identity`` only (hyphenated form tried first, then
    space-converted) -- deliberately **not**
    ``resolve_team_identity_by_shortening``, for the same reason the
    NET-ranking adapter avoids it: model/profile slugs are already-clean
    school-name slugs with no mascot suffix to strip, and this codebase has
    real state-university prefix collisions (Ohio/Ohio State,
    Iowa/Iowa State) where trailing-token-dropping would silently resolve
    to the wrong real team.
    """
    if not query:
        return UnresolvedIdentity(query=str(query), source=source, reason="not_found")

    hyphenated = query.strip()
    result = resolve_team_identity(hyphenated, source=source)
    if isinstance(result, TeamIdentity) or result.reason == "ambiguous":
        return result

    return resolve_team_identity(hyphenated.replace("-", " "), source=source)


def resolve_model_team_folder(
    query: str, *, cbb_dir=None, source: str = "model"
) -> tuple[TeamIdentity, str] | UnresolvedIdentity:
    """
    Resolve ``query`` to a canonical team AND find its actual on-disk
    ``TeamDump/CBB/`` folder slug, tracking which candidate matched.

    Unlike :func:`resolve_team_slug_v15_compat` (which always returns
    *some* slug string, even one that doesn't exist on disk -- a silent,
    if non-fuzzy, guess), this function returns
    :class:`UnresolvedIdentity` when the team resolves canonically but no
    known slug/alias for it exists as a real folder. That is the honest
    answer: this migration found real teams (the "st"->"saint" corruption
    cases documented in this module's docstring) whose *only* on-disk
    folder is a misspelled name no clean candidate list should be expected
    to guess -- and guessing via fuzzy matching would risk pulling a
    *different* team's data, which is worse than reporting "not found."

    Candidate order: canonical ``sr_slug``, then every registered alias
    (sorted for determinism), checked against ``cbb_dir`` (defaults to
    ``src.paths.CBB_DIR``; injectable for tests).
    """
    identity = resolve_model_team_identity(query, source=source)
    if not isinstance(identity, TeamIdentity):
        return identity

    base_dir = CBB_DIR if cbb_dir is None else cbb_dir
    rec = slug_utils.TEAM_REGISTRY[identity.team_id]

    candidates = [("sr_slug", rec.sr_slug)] + [
        ("alias", alias) for alias in sorted(rec.aliases)
    ]

    for candidate_type, candidate in candidates:
        if candidate and (base_dir / candidate).exists():
            log.info(
                "resolved model folder for %r (team_id=%d) -> %r via %s",
                query, identity.team_id, candidate, candidate_type,
            )
            return identity, candidate

    log.info(
        "team %r (team_id=%d) resolved canonically but no known slug/alias "
        "exists as a folder under %s",
        query, identity.team_id, base_dir,
    )
    return UnresolvedIdentity(query=query, source=source, reason="not_found")
