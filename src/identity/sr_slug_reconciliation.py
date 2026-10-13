"""
Sports-Reference slug reconciliation service (Phase 2, Gate 1).

BACKGROUND

Phase 2B batch 6's Phase 2A targeted re-review found 32 real, currently-
active D1 programs where `cache/sportsref_cbb_schools.parquet`
(`all_schools_df`) and the canonical team registry disagreed on the SR
slug string (e.g. Texas A&M: "texas-aandm" vs "texas-am"). That batch
explicitly declined to guess which was correct and recorded the conflict
for approval instead of resolving it.

ROOT CAUSE, FOUND BY STATIC CODE READING (NO LIVE REQUESTS)

`fetch_all_schools()` (notebook cell 30) has a **cache self-repair path**
that runs on the ordinary, everyday "cache already exists" branch:

    if "team_slug" in df.columns and df["team_slug"].isna().any():
        miss = df["team_slug"].isna()
        df.loc[miss, "team_slug"] = df.loc[miss, "team_name"].map(_fallback_sr_slug_guess)
        df.to_parquet(SCHOOLS_CACHE_FILE, index=False)   # <-- persists the guess, unvalidated

This calls `_fallback_sr_slug_guess()` -- a naive slugify (lowercase,
"&"->"and", strip punctuation, join with hyphens) -- **without** the
`_is_valid_sr_slug()` live-HTTP check that gates every *other* call site
of the same guess function (`_fetch_and_parse`'s own "final fallback,"
cell 30, explicitly comments "This is only used if validation confirms it
exists"). This repair path has no such comment and no such check, and
silently writes the unvalidated guess back into the cache file that is
supposed to hold verified SR slugs.

**Confirmed, not inferred:** re-running `_fallback_sr_slug_guess()`
(reimplemented verbatim below in `_fallback_sr_slug_guess_v15` for
inspection/testing) against all 32 disputed display names reproduces the
cached `all_schools_df` slug **exactly, for all 32 of 32** -- conclusive
local-evidence proof (no live scraping needed) that every one of these 32
values is a product of this unvalidated repair path, not a real
href-derived slug. This satisfies the approved policy's rule 3
("generated or normalized slugs are not authoritative evidence") for
every dispute without requiring a live fetch.

TWO DISPUTES ARE NOT REALLY SLUG DISPUTES AT ALL

While investigating, two of the 32 query strings ("Colorado College
Tigers", "Washington (MO) Bears") turned out to be real, separate D3
programs that the canonical registry's own team-name shortening/
derivation heuristic incorrectly collapses onto an unrelated D1 team_id
("Colorado" / "Washington"). This is a pre-existing **canonical-side
query-resolution defect**, not a dispute about either team_id's actual SR
slug (both remain correct and unaffected) -- flagged via
`canonical_query_collision_warning` in the reconciliation record and
recorded separately in the consolidated unresolved ledger as
"Requires user approval." It does not block `get_explicit_sr_slug` for
either real team_id.

WHAT THIS MODULE PROVIDES

- `get_reconciliation_record(team_id)` / `list_unresolved_reconciliations()`
  -- read access to `docs/sr_slug_reconciliation.json`, the single
  machine-readable reconciliation record (no competing registry created).
- `get_explicit_sr_slug(team_id, *, fallback_slug=None)` -- the one
  function a caller should use when it specifically needs "the current
  Sports-Reference URL slug," as distinct from a canonical internal slug
  or a pipeline-specific (Torvik/Covers/odds) slug convention. Checks the
  reconciliation record first; falls back to the registry's own SR source
  alias otherwise.
- `_fallback_sr_slug_guess_v15` -- verbatim compat port of the notebook's
  own guess function, preserved for the same reason every other `_v15`
  wrapper in this migration is: byte-faithful reference/testing, never
  called from a canonical resolution path.

No fuzzy matching of any kind is used anywhere in this module.
`resolve_slug_from_name_v15` (the postgame batch's own compat wrapper for
`_resolve_slug_from_name`'s unrestricted difflib fallback) remains
untouched in `src/identity/postgame_team_identity.py` -- preserved only
as legacy compatibility behavior, per this batch's instruction.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from src import slug_utils
from src.identity.contracts import TeamIdentity, UnresolvedIdentity
from src.identity.team_identity import _team_identity_from_id, ensure_team_registry, resolve_team_identity
from src.logging_config import get_logger

log = get_logger(__name__)

_RECONCILIATION_PATH = Path(__file__).resolve().parents[2] / "docs" / "sr_slug_reconciliation.json"


def _fallback_sr_slug_guess_v15(team_name: str) -> str:
    """
    Verbatim port of notebook cell 28's `_fallback_sr_slug_guess`.
    Preserved for reference/testing only -- this is the exact function
    whose unvalidated output, via the cache self-repair path documented in
    this module's docstring, produced all 32 disputed values this
    reconciliation record resolves.
    """
    s = (team_name or "").lower().strip()
    s = s.replace("&", "and")
    s = s.replace("st.", "st")
    s = s.replace("st ", "st ")
    s = s.replace("'", "")
    s = s.replace(".", "")
    s = s.replace("(", "").replace(")", "")
    s = "-".join(s.split())
    return s


@lru_cache(maxsize=1)
def _load_reconciliation() -> dict:
    with open(_RECONCILIATION_PATH, encoding="utf-8") as f:
        return json.load(f)


def reconciliation_disputes() -> list[dict]:
    """All 32 recorded disputes, verbatim from the JSON record."""
    return list(_load_reconciliation()["disputes"])


def get_reconciliation_record(team_id: int) -> dict | None:
    """The reconciliation entry for one team_id, or None if it was never disputed."""
    for rec in reconciliation_disputes():
        if rec["team_id"] == team_id:
            return rec
    return None


def list_unresolved_reconciliations() -> list[dict]:
    """
    Disputes whose `resolution_status` starts with "unresolved" -- per
    this batch's policy, none currently block `get_explicit_sr_slug`
    (the two query-collision cases are flagged separately, not left
    "unresolved" against their own, unaffected team_id).
    """
    return [r for r in reconciliation_disputes() if r["resolution_status"].startswith("unresolved")]


def get_explicit_sr_slug(team_id: int, *, fallback_slug: str | None = None) -> str | None:
    """
    The current, reconciled Sports-Reference slug for `team_id`.

    This is the explicit accessor a caller must use when it specifically
    needs "the real SR URL slug" -- not a canonical internal slug
    (`src.slug_utils.canonical_team_slug`), not a pipeline-specific slug
    (Torvik/Covers/odds conventions), and not `all_schools_df`'s own
    `team_slug` column, which this batch's Gate 1 investigation showed can
    silently contain an unvalidated generated artifact for a real, active
    D1 team. Checks this reconciliation record first (a reviewed override
    for any of the 32 known disputes); falls back to `fallback_slug`
    (normally the caller's own `TeamIdentity.sources["sports_reference"].raw_value`)
    when the team was never disputed.

    Returns ``None`` for a team whose reconciliation entry is still
    genuinely `unresolved` (there are currently none such -- see
    `list_unresolved_reconciliations`) rather than silently falling back,
    unless `fallback_slug` is supplied.
    """
    record = get_reconciliation_record(team_id)
    if record is not None:
        if record["resolution_status"].startswith("unresolved"):
            return fallback_slug
        return record["selected_current_sr_slug"]
    return fallback_slug


# ---------------------------------------------------------------------------
# Query-side reconciliation (Phase 4A pre-batch, Gate 1) -- resolves a
# verified-but-not-registered slug form (e.g. "william-mary") to the same
# team_id the registry's own exact alias ("William & Mary") already
# resolves, WITHOUT editing src/slug_utils.py's alias tables at all.
# ---------------------------------------------------------------------------


def _normalized_forms(value: str) -> set[str]:
    """Both the hyphenated and space-converted lowercase forms of `value`."""
    v = value.strip().lower()
    return {v, v.replace("-", " "), v.replace(" ", "-")}


@lru_cache(maxsize=1)
def _reconciled_query_to_team_id() -> dict[str, int]:
    """
    Reverse index built from the reconciliation record: every normalized
    form of each dispute's `selected_current_sr_slug` and its
    `legacy_aliases` -> that dispute's `team_id`. Built once, lazily, from
    the same 32-entry record `get_explicit_sr_slug` already reads --
    no new data source, no live lookup.
    """
    index: dict[str, int] = {}
    for record in reconciliation_disputes():
        if record["resolution_status"].startswith("unresolved"):
            continue  # never wire an unresolved dispute into query resolution

        team_id = record["team_id"]
        if record.get("evidence_classification") == "canonical_query_collision_fixed":
            # This dispute's "legacy_aliases" entry IS the confirmed
            # false-collision query string (e.g. "colorado-college-tigers",
            # a real, different D3 program) that team_shortening_guard.py
            # was built to reject, not a benign alternate spelling of THIS
            # team_id. Indexing it here would silently reintroduce the
            # exact collision that guard exists to prevent -- confirmed by
            # a real test failure during development of this function, not
            # assumed. `canonical_query_collision_warning` was already
            # flipped to False on these two records when the guard fix
            # landed (it tracked "still needs attention," not "unsafe to
            # index"), so `evidence_classification` is the correct, stable
            # signal here instead. Only the dispute's own
            # `selected_current_sr_slug` (the real team's own slug) is
            # safe to index for these.
            candidates = [record["selected_current_sr_slug"]]
        else:
            candidates = [record["selected_current_sr_slug"], *record.get("legacy_aliases", [])]

        for candidate in candidates:
            if not candidate:
                continue
            for form in _normalized_forms(candidate):
                index.setdefault(form, team_id)
    return index


def resolve_team_identity_with_reconciliation(
    query: str, *, source: str = "unknown"
) -> TeamIdentity | UnresolvedIdentity:
    """
    Wraps `resolve_team_identity` (`team_identity.py`, untouched) with an
    additive lookup against the SR-slug reconciliation record for the 32
    previously-disputed teams. Resolving "william-mary" (the verified
    current SR slug) or "william-and-mary" (a retained legacy alias) now
    returns the same `TeamIdentity` as "William & Mary" (the registry's
    own exact alias) -- same `team_id`, same `display_name` -- without
    modifying `src/slug_utils.py`'s alias tables in any way.

    Only ever intervenes on a `not_found` result -- an `ambiguous` result
    from `resolve_team_identity` is returned exactly as-is, never
    overridden, so this cannot introduce a new alias collision. Scoped
    strictly to the reconciliation record's own 32 entries -- not a
    general-purpose fuzzy fallback.
    """
    result = resolve_team_identity(query, source=source)
    if isinstance(result, TeamIdentity) or (isinstance(result, UnresolvedIdentity) and result.reason != "not_found"):
        return result

    for form in _normalized_forms(query):
        team_id = _reconciled_query_to_team_id().get(form)
        if team_id is not None:
            ensure_team_registry()
            if team_id in slug_utils.TEAM_REGISTRY:
                log.info("reconciliation-wired query %r -> team_id=%d", query, team_id)
                return _team_identity_from_id(team_id)

    return result
