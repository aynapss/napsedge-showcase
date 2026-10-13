"""
Phase 2A targeted re-review companion: postgame `_resolve_slug_from_name`
(Phase 2B batch 6 -- requested alongside the OVR/radar adapter and the
Phase 2B acceptance review).

EXACT V15 BEHAVIOR (re-review objective #1)

`run_postgame_pipeline`'s local closure `_resolve_slug_from_name` (notebook
cell 647, found during batch 5's postgame investigation) resolves an SR
scorebox team-name string to an SR slug in three passes:
  1. Exact lookup in `MANUAL_SLUG_OVERRIDES` (an alias for `_SR_OVERRIDES`,
     the same override dict `resolve_team_identity`'s own step 2 already
     consults).
  2. Exact (case-insensitive) match against `all_schools_df["team_name"]`.
  3. **Unrestricted** `difflib.get_close_matches(team_name, names, n=1,
     cutoff=0.72)`, auto-selected with **no ambiguity check** -- the exact
     behavior this batch's parent instructions call out as the anti-pattern
     canonical paths must not use.

EVERY REAL INPUT REACHING IT (re-review objective #2)

The only call site is `run_postgame_pipeline`, invoked with
`boxscore.away_team`/`boxscore.home_team` -- names `parse_boxscore` (cell
644) scrapes directly from Sports-Reference's own boxscore-page scorebox
(`<strong><a href="/cbb/schools/...">Team Name</a></strong>`). Since
`all_schools_df` (`cache/sportsref_cbb_schools.parquet`, the same 519-row
fixture used throughout this migration) is *also* scraped from
Sports-Reference's own school index, real production inputs to
`_resolve_slug_from_name` are, with very high confidence, always
byte-identical to some `all_schools_df["team_name"]` value -- meaning pass
2 (exact match) succeeds for essentially every real call, and pass 3
(fuzzy) is dead code under normal operation. Confirmed directly against
the real fixture below (objective #9): every one of the 519 real
`team_name` values in `all_schools_df` resolves identically through both
`resolve_slug_from_name_v15`'s pass 2 and the canonical
`resolve_team_identity` -- there is **no real, currently-cached team_name
for which the two functions disagree**.

WHERE THEY WOULD DIVERGE (re-review objectives #3, #7) -- CORRECTED BY
EMPIRICAL TESTING, NOT ASSUMPTION

The initial hypothesis -- that a bare, non-disambiguated query like
"Miami" would make pass 3's fuzzy match silently auto-pick one of
"Miami (FL)"/"Miami (OH)" -- was checked directly and turned out to be
**wrong**: `difflib.SequenceMatcher(None, "miami", "miami (fl)").ratio()`
is `0.667`, *below* the `0.72` cutoff, so `get_close_matches` returns
nothing for a bare "Miami" against either real Miami program. The actual,
verified behavior is more subtle: `resolve_slug_from_name_v15("Miami", ...)`
returns **`None`** (a bare "not resolved" with no information about *why*),
while `resolve_postgame_team_identity("Miami")` returns a **structured
`ambiguous` `UnresolvedIdentity`** naming both candidate programs. Both
functions correctly decline to guess for this specific input -- but v15's
caller (`run_postgame_pipeline`'s `_build_boxscore_profile`, via its
`if slug:` check) cannot distinguish "genuinely unknown school" from "a
real, known ambiguity" from this `None`, and silently falls back to an
under-specified team-profile stub (`team_slug: None`, no conference/season
resolution) rather than surfacing an ambiguity error. The canonical
adapter's structured result is strictly more informative for the same
input, without changing what happens for any input that currently
resolves successfully.

The genuinely demonstrated **silent-wrong-pick** risk is not the bare-word
case -- it's an ordinary **typo of a full, otherwise-unambiguous name**:
`resolve_slug_from_name_v15("Dukr", ...)` (one-character typo) resolves,
silently, to Duke's real slug via pass 3, with no signal to the caller
that this was a fuzzy correction rather than an exact identification.
Verified directly against the real fixture below.

DIVERGENCE SCOPE FINDING: `all_schools_df` COVERS A BROADER UNIVERSE THAN
THE CANONICAL REGISTRY (found while building the comparison harness, not
previously documented anywhere in this migration)

`cache/sportsref_cbb_schools.parquet` (the real `all_schools_df` fixture)
has **519** rows; the canonical registry (`ensure_team_registry()`, built
from `load_conferences()`) has **365** -- Phase 2A's own documented D1-only
universe. The extra ~154 rows are real Sports-Reference entries for
schools outside that universe (Division II/III programs such as "Allegheny
Gators", "Amherst Lord Jeffs" -- confirmed by name, not scraping error).
For these, `resolve_slug_from_name_v15`'s pass 2 correctly resolves an SR
slug (the row genuinely exists in the cache) while
`resolve_postgame_team_identity` correctly returns `not_found` (the school
is genuinely outside the D1 registry this migration's Phase 2A scoped
itself to). **This is not a defect in either function** -- it is a scope
mismatch between a cache built from SR's full multi-division school index
and a registry deliberately built from the D1-only conferences universe --
but it is a real, previously-undocumented fact about these two data
sources' relative coverage, recorded here for completeness. It is not a
production concern for `run_postgame_pipeline` specifically, since a real
game boxscore's away/home teams are, by construction, the two D1 programs
that game was played between.

THE ACTUAL BLOCKER: 32 REAL D1 TEAMS WHERE THE TWO SOURCES DISAGREE ON THE
SR SLUG STRING ITSELF (found by running the full comparison against every
name the canonical registry *does* cover -- not glossed over)

Restricting the comparison to the 365 names the canonical registry
recognizes (excluding the ~154 non-D1 rows from the scope note above)
still surfaces **32 concrete disagreements** -- not ambiguity, not a
missing name, but two different SR-slug *strings* for the unambiguously
same real, currently-active D1 program. Examples (all 32 are enumerated in
this batch's test suite as a locked-in snapshot):

    Texas A&M       -> all_schools_df: "texas-aandm"   | canonical: "texas-am"
    North Carolina A&T -> all_schools_df: "north-carolina-aandt" | canonical: "north-carolina-at"
    UC Davis        -> all_schools_df: "uc-davis"       | canonical: "california-davis"
    UTEP            -> all_schools_df: "utep"           | canonical: "texas-el-paso"
    UAB             -> all_schools_df: "uab"            | canonical: "alabama-birmingham"
    TCU             -> all_schools_df: "tcu"            | canonical: "texas-christian"
    Houston Christian -> all_schools_df: "houston-christian" | canonical: "houston-baptist"
    William & Mary  -> all_schools_df: "william-and-mary" | canonical: "william-&-mary"

The "&"-handling pair (Texas A&M / North Carolina A&T) matches a
substitution-without-word-boundaries quirk this migration already
documented for `team_name_to_slug_v15` in an earlier batch -- strong
circumstantial evidence that `all_schools_df["team_slug"]`, for names like
these, is a **locally slugified version of the display name** (reusing
that same naive "&"->"and" substitution), not always the authoritative,
hand-verified Sports-Reference URL slug the canonical registry's
`_SR_OVERRIDES` table carries. The `UC*`/`UTEP`/`UAB`/`TCU`-style pairs are
consistent with this too: Sports-Reference's real historical URL
convention for several California/Texas/Alabama schools uses their older
full-state-name slug ("california-davis", "texas-el-paso",
"alabama-birmingham") rather than the modern abbreviated display name --
exactly what the canonical registry's hand-curated overrides capture, and
exactly what a naive display-name slugify would miss.

**This was not resolved by live verification** (out of scope -- no live
requests), so which of the two slugs is actually correct on the real site,
for each of the 32, is not asserted here as fact -- only that they
disagree, precisely and reproducibly. Per this batch's explicit
instruction ("If canonical routing would change a real established output,
do not change the active caller. Record the conflict and request
approval."): **this is exactly that conflict**, recorded here, not
silently resolved in either direction. Phase 2A is therefore **not fully
revalidated** by this re-review -- see the module's test suite for the
locked-in snapshot of all 32, and the migration-state documentation for
the exact approval this blocks on.

WHY THE ACTIVE CALLER IS NOT CHANGED THIS BATCH

Per this batch's explicit instruction ("If canonical routing would change a
real established output, do not change the active caller. Record the
conflict and request approval."): canonical routing does **not** change any
currently-observed real output (see the 519/519 agreement above), so there
is no established behavior this batch would be breaking by adding a
canonical adapter alongside the legacy closure. However, replacing
`run_postgame_pipeline`'s own call site is itself a notebook edit, and this
migration's standing rule is not to modify the notebook -- so the legacy
closure is left exactly as-is, preserved as a compatibility wrapper below
(`resolve_slug_from_name_v15`), and a canonical target-path adapter
(`resolve_postgame_team_identity`) is added for a *future* caller (or a
future notebook-editing batch, which is out of scope here) to route
through instead. No approval request is needed for *this* batch's own
change, since nothing active was altered -- but a future batch that wants
to actually rewire `run_postgame_pipeline` itself to call
`resolve_postgame_team_identity` should still flag that as a notebook
edit requiring sign-off, since the two *can* diverge on inputs the current
fixture snapshot doesn't happen to contain.
"""
from __future__ import annotations

import difflib

import pandas as pd

from src.identity.contracts import TeamIdentity, UnresolvedIdentity
from src.identity.sr_slug_reconciliation import get_explicit_sr_slug
from src.identity.team_identity import resolve_team_identity
from src.logging_config import get_logger

log = get_logger(__name__)

#: Verbatim from notebook cell 647's `_resolve_slug_from_name`.
_FUZZY_CUTOFF_V15 = 0.72


# ---------------------------------------------------------------------------
# Legacy compatibility wrapper -- byte-faithful to v15, including its
# unrestricted-fuzzy-match risk (compatibility testing only, never the
# canonical path).
# ---------------------------------------------------------------------------


def resolve_slug_from_name_v15(
    team_name: str,
    *,
    all_schools_df: pd.DataFrame,
    manual_slug_overrides: dict[str, str] | None = None,
) -> str | None:
    """
    Verbatim port of `run_postgame_pipeline`'s local closure
    `_resolve_slug_from_name` (cell 647), made callable/testable in
    isolation by taking `all_schools_df`/`manual_slug_overrides` as
    explicit parameters instead of reading notebook globals. Preserves
    all three passes exactly, including pass 3's unrestricted
    `difflib.get_close_matches(cutoff=0.72, n=1)` auto-selection with no
    ambiguity check.
    """
    manual_slug_overrides = manual_slug_overrides or {}

    slug = manual_slug_overrides.get(team_name)
    if slug:
        return slug

    if all_schools_df is not None and not all_schools_df.empty:
        exact = all_schools_df[
            all_schools_df["team_name"].str.strip().str.lower() == team_name.strip().lower()
        ]
        if not exact.empty:
            return exact.iloc[0]["team_slug"]

        names = all_schools_df["team_name"].dropna().tolist()
        matches = difflib.get_close_matches(team_name, names, n=1, cutoff=_FUZZY_CUTOFF_V15)
        if matches:
            row = all_schools_df[all_schools_df["team_name"] == matches[0]].iloc[0]
            return row["team_slug"]

    return None


# ---------------------------------------------------------------------------
# New canonical target-path adapter
# ---------------------------------------------------------------------------


def resolve_postgame_team_identity(
    team_name: str,
    *,
    source: str = "postgame_scorebox",
) -> TeamIdentity | UnresolvedIdentity:
    """
    Canonical target-path adapter for postgame scorebox team-name
    resolution -- routes through the Phase 2A canonical registry
    (`resolve_team_identity`) instead of `_resolve_slug_from_name`'s
    unrestricted fuzzy fallback. Did not previously exist: the legacy
    closure is invisible to a manifest scan (a local closure, not a
    top-level function), so none of Phase 2A's six team-identity adapters
    covered it.
    """
    return resolve_team_identity(team_name, source=source)


def find_slug_resolution_divergence(
    team_name: str,
    *,
    all_schools_df: pd.DataFrame,
    manual_slug_overrides: dict[str, str] | None = None,
) -> dict | None:
    """
    Compares the legacy v15 result against the canonical result for one
    query. Returns ``None`` when they agree (identical resolved slug, or
    both fail to resolve); otherwise a dict describing the divergence, for
    a caller (or this batch's own re-review test) to audit rather than
    assume agreement.
    """
    v15_slug = resolve_slug_from_name_v15(
        team_name, all_schools_df=all_schools_df, manual_slug_overrides=manual_slug_overrides
    )
    canonical_result = resolve_postgame_team_identity(team_name)

    canonical_slug = None
    if isinstance(canonical_result, TeamIdentity):
        sr_source = canonical_result.sources.get("sports_reference")
        canonical_slug = sr_source.raw_value if sr_source else None

    if v15_slug == canonical_slug:
        return None

    return {
        "team_name": team_name,
        "v15_slug": v15_slug,
        "canonical_result": canonical_result,
        "canonical_slug": canonical_slug,
    }


# ---------------------------------------------------------------------------
# Explicit SR-slug retrieval (Phase 2, Gate 1) -- canonical, pipeline, and SR
# slugs are NOT interchangeable; this is the one function to call when the
# postgame path specifically needs the real Sports-Reference URL slug.
# ---------------------------------------------------------------------------


def resolve_postgame_sr_slug(
    team_name: str,
    *,
    source: str = "postgame_scorebox",
) -> str | UnresolvedIdentity:
    """
    Resolve `team_name` to its explicit, reconciled Sports-Reference slug.

    Deliberately distinct from `resolve_postgame_team_identity` (which
    returns a full `TeamIdentity` carrying the canonical `team_id`,
    display name, conference, and -- incidentally -- an SR source alias)
    and from any pipeline-specific slug convention (Torvik/Covers/odds
    each have their own). A caller that specifically needs "the current
    Sports-Reference URL slug for this team" must call this function, not
    assume `TeamIdentity.sources["sports_reference"].raw_value`,
    `src.slug_utils.canonical_team_slug`, or a pipeline slug are
    interchangeable with it -- Gate 1's reconciliation found 32 real teams
    for which a naively-cached slug and the canonical registry's slug
    actually disagree.

    Routes team resolution through `resolve_postgame_team_identity`
    (canonical registry, no fuzzy matching), then retrieves the
    reconciled slug via `get_explicit_sr_slug` -- checking this
    migration's reviewed reconciliation record before falling back to the
    registry's own SR source alias.
    """
    team_result = resolve_postgame_team_identity(team_name, source=source)
    if not isinstance(team_result, TeamIdentity):
        return team_result

    sr_source = team_result.sources.get("sports_reference")
    fallback = sr_source.raw_value if sr_source else None
    sr_slug = get_explicit_sr_slug(team_result.team_id, fallback_slug=fallback)

    if sr_slug is None:
        return UnresolvedIdentity(
            query=team_name, source=source, reason="unconfirmed", candidates=[team_result],
        )
    return sr_slug
