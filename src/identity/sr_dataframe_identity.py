"""
Sports-Reference/DataFrame team-identity adapter (Phase 2A, adapter batch 3).

Source system: cbb_teamScrape_v15_TEST.ipynb cell 33 (`normalize_team_search_name`,
`resolve_team_name`) and cell 30 (`fetch_all_schools`), all unshadowed
(`defined_in_notebook_count == 1` each per docs/function_manifest.csv).

MAJOR FINDING (changes how this adapter must be built): `TEAM_NAME_CORRECTIONS`
and `MANUAL_SLUG_OVERRIDES`, which `normalize_team_search_name`/`resolve_team_name`
depend on, are NOT separate/competing dicts. Cells 15 and 20 show the
notebook already did its own migration:

    # cell 15
    from src.slug_utils import _DISPLAY_NAME_ALIASES
    TEAM_NAME_CORRECTIONS = _DISPLAY_NAME_ALIASES  # backward-compat alias

    # cell 20
    from src.slug_utils import _SR_OVERRIDES
    MANUAL_SLUG_OVERRIDES = _SR_OVERRIDES  # backward-compat alias

Both cells' own comments say "After full migration: delete this cell" --
i.e. `TEAM_NAME_CORRECTIONS`/`MANUAL_SLUG_OVERRIDES` are LIVE references to
the exact same `src.slug_utils` tables `src.identity.team_identity`'s
canonical registry already builds from. (Each dict's *originally-written*
cell -- 14 and 19 respectively -- is fully commented out; cells 15/20 are
the active, later redefinitions.) So `resolve_team_name`'s "Pass 1:
MANUAL_SLUG_OVERRIDES" is already canonical-registry-backed today, at
notebook runtime.

What is a genuinely separate, third data source: `all_schools_df` (from
`fetch_all_schools()`), a live scrape of Sports-Reference's own schools
index page -- ~519 rows spanning every D1 (and some pre-D1/historical)
program SR has ever tracked, cached locally at
`cache/sportsref_cbb_schools.parquet`. Its `team_slug` column is SR's own
verified, ground-truth slug -- distinct from (and occasionally more
accurate than) `src.slug_utils.TEAM_REGISTRY`'s `sr_slug` field, which is
built from `_SR_OVERRIDES`/naive display-name slugification and is NOT
independently verified against SR itself.

DATAFRAME FINDINGS (from cross-referencing all 365 registry teams against
the cached fixture -- see docs/migration_state.md for the full table):
- 334/365 (91.5%) of the registry's `sr_slug` values are confirmed correct
  (they exist as a real `team_slug` in the fixture).
- 31/365 (8.5%) are wrong or unverifiable in this fixture. Pattern: SR
  sometimes uses an ABBREVIATION as its own official name/slug (UAB ->
  "uab", NC State -> "nc-state", TCU -> "tcu", UCF -> "ucf") where the
  registry assumed a spelled-out form ("alabama-birmingham",
  "north-carolina-state", "texas-christian", "central-florida"). A few are
  historical renames the fixture's snapshot doesn't reflect the same way
  (Utah Tech's registry slug is the pre-2022-rebrand "dixie-state"; the
  fixture already has "utah-tech").
- The fixture itself has a confirmed scraping bug: 23 rows are the
  paginated HTML table's repeated header row, captured as if it were real
  team data (`team_name="School"`, `team_slug="school"`). This module
  filters those out rather than risk them satisfying a lookup.

None of this is "fixed" in `src.slug_utils` here -- per the parity rules,
correcting `TEAM_REGISTRY[...].sr_slug` for 31 teams would change behavior
for every existing caller of `get_sr_slug()`/`fetch_team_page()` etc., which
needs explicit approval, not a unilateral change from an identity-adapter
batch. This adapter instead *additionally* surfaces the verified slug
(`sources["sports_reference_verified"]`) alongside the registry's own
(`sources["sports_reference"]`), so a caller can tell when they disagree.
"""
from __future__ import annotations

import difflib

import pandas as pd

from src.exceptions import ValidationError
from src.identity.contracts import SourceIdentity, TeamIdentity, UnresolvedIdentity
from src.identity.team_identity import resolve_team_identity
from src.logging_config import get_logger
from src import slug_utils

log = get_logger(__name__)

_REQUIRED_COLUMNS = {"team_name", "team_slug"}

#: Rows where SR's paginated HTML table repeated its own header row --
#: confirmed present in cache/sportsref_cbb_schools.parquet (23 rows).
_GARBAGE_TEAM_NAMES = {"school"}


def validate_schools_df(all_schools_df: pd.DataFrame) -> None:
    """
    Canonical-path validation (raises :class:`ValidationError`, a typed
    NapsEdge exception) -- distinct from
    :func:`resolve_team_name_v15_verbatim`'s legacy ``ValueError``/``KeyError``,
    which is preserved unchanged for compatibility.
    """
    if all_schools_df is None or all_schools_df.empty:
        raise ValidationError("all_schools_df is empty or None.")
    missing = _REQUIRED_COLUMNS - set(all_schools_df.columns)
    if missing:
        raise ValidationError(
            f"all_schools_df missing columns {missing}. "
            f"Columns: {all_schools_df.columns.tolist()}"
        )


def _clean_schools_df(all_schools_df: pd.DataFrame) -> pd.DataFrame:
    """Drop confirmed repeated-header garbage rows (see module docstring)."""
    mask = ~all_schools_df["team_name"].astype(str).str.strip().str.lower().isin(_GARBAGE_TEAM_NAMES)
    return all_schools_df[mask]


def normalize_team_search_name_v15(name: str) -> str:
    """
    Verbatim port of notebook cell 33's ``normalize_team_search_name``,
    using ``src.slug_utils._DISPLAY_NAME_ALIASES`` directly in place of the
    notebook's ``TEAM_NAME_CORRECTIONS`` -- confirmed to be the exact same
    dict at runtime (cell 15), not a guess.
    """
    if not isinstance(name, str):
        return ""

    n = name.strip()

    if n.lower().startswith("st ") and not n.lower().startswith("st. "):
        n = "St. " + n[3:].lstrip()

    if "Johns" in n and "John's" not in n:
        n = n.replace("Johns", "John's")

    key = n.lower().strip()
    return slug_utils._DISPLAY_NAME_ALIASES.get(key, n)


def resolve_team_name_v15_verbatim(name: str, all_schools_df: pd.DataFrame) -> pd.Series | None:
    """
    Compatibility wrapper: byte-faithful port of notebook cell 33's
    ``resolve_team_name``, including its silent fuzzy fallback
    (``difflib.get_close_matches(..., cutoff=0.72)``) and its original
    ``ValueError``/``KeyError`` validation. Exists so any future caller that
    needs bit-exact v15 output (fuzzy-guessing included) has it available,
    unmodified -- new code should use :func:`resolve_sr_team_identity`
    instead, which never guesses.

    ``MANUAL_SLUG_OVERRIDES`` here is ``src.slug_utils._SR_OVERRIDES``
    directly (confirmed live equivalence, cell 20) -- not a re-guess.
    """
    if all_schools_df is None or all_schools_df.empty:
        raise ValueError("all_schools_df is empty. Run fetch_all_schools() first.")

    required = {"team_name", "team_slug"}
    missing = required - set(all_schools_df.columns)
    if missing:
        raise KeyError(
            f"all_schools_df missing columns {missing}. "
            f"Columns: {all_schools_df.columns.tolist()}"
        )

    manual_slug_overrides = slug_utils._SR_OVERRIDES

    override_slug = (
        manual_slug_overrides.get(name)
        or manual_slug_overrides.get(name.strip())
        or next(
            (v for k, v in manual_slug_overrides.items() if k.lower().strip() == name.lower().strip()),
            None,
        )
    )

    if override_slug:
        slug_match = all_schools_df[
            all_schools_df["team_slug"].astype(str).str.lower() == override_slug.lower()
        ]
        if not slug_match.empty:
            return slug_match.iloc[0]
        return pd.Series({"team_name": name, "team_slug": override_slug})

    name_norm = normalize_team_search_name_v15(name)
    df_names = all_schools_df["team_name"].astype(str).str.strip()
    df_norm = df_names.map(normalize_team_search_name_v15)

    exact = all_schools_df[df_norm == name_norm]
    if not exact.empty:
        return exact.iloc[0]

    ci = all_schools_df[df_norm.str.lower() == name_norm.lower()]
    if not ci.empty:
        return ci.iloc[0]

    candidates = df_norm.tolist()
    match = difflib.get_close_matches(name_norm, candidates, n=1, cutoff=0.72)
    if match:
        return all_schools_df[df_norm == match[0]].iloc[0]

    return None


def resolve_sr_team_identity(
    query: str,
    all_schools_df: pd.DataFrame | None = None,
    *,
    source: str = "sports_reference_df",
) -> TeamIdentity | UnresolvedIdentity:
    """
    Resolve ``query`` to a canonical :class:`TeamIdentity`, with no fuzzy
    fallback anywhere in this path (unlike :func:`resolve_team_name_v15_verbatim`).

    Team-identity resolution itself is delegated entirely to
    :func:`src.identity.team_identity.resolve_team_identity` (the same
    canonical registry every other adapter uses -- display names and SR
    slugs are never treated as interchangeable inputs here, only as
    labeled fields on the result).

    If ``all_schools_df`` is supplied, additionally cross-references the
    resolved team against SR's own ground-truth schools index by an exact
    (case-insensitive, non-fuzzy) match on the team's display name or any
    of its known registry aliases. When found, attaches a
    ``sources["sports_reference_verified"]`` entry; if it disagrees with
    the registry's own ``sources["sports_reference"]`` slug, logs a
    warning naming both values (tracks which alias produced the match) --
    it does not silently prefer one over the other or mutate the registry.
    """
    result = resolve_team_identity(query, source=source)
    if not isinstance(result, TeamIdentity):
        return result

    if all_schools_df is None:
        return result

    validate_schools_df(all_schools_df)
    df = _clean_schools_df(all_schools_df)
    rec = slug_utils.TEAM_REGISTRY[result.team_id]

    names_lower = df["team_name"].astype(str).str.lower()
    candidates = [rec.display_name, *sorted(rec.aliases)]
    for candidate in candidates:
        hit = df[names_lower == candidate.lower()]
        if hit.empty:
            continue
        verified_slug = str(hit.iloc[0]["team_slug"])
        result.sources["sports_reference_verified"] = SourceIdentity(
            "sports_reference_verified", verified_slug, verified_slug
        )
        if verified_slug != rec.sr_slug:
            log.warning(
                "registry sr_slug %r disagrees with SR schools-index slug %r "
                "for %r (matched via %r)",
                rec.sr_slug, verified_slug, rec.display_name, candidate,
            )
        else:
            log.info(
                "registry sr_slug %r confirmed against SR schools index for %r (matched via %r)",
                rec.sr_slug, rec.display_name, candidate,
            )
        break

    return result


def find_sr_slug_mismatches(all_schools_df: pd.DataFrame) -> list[dict]:
    """
    Diagnostic sweep: compare every registered team's ``sr_slug`` against
    SR's ground-truth schools index, returning every disagreement. This is
    what produced the 31-team finding documented in this module's
    docstring and in docs/migration_state.md -- callable again against a
    fresher fixture to see if the count has changed.
    """
    validate_schools_df(all_schools_df)
    df = _clean_schools_df(all_schools_df)
    valid_slugs = set(df["team_slug"].astype(str))

    mismatches = []
    for team_id, rec in slug_utils.TEAM_REGISTRY.items():
        if rec.sr_slug not in valid_slugs:
            mismatches.append({
                "team_id": team_id,
                "display_name": rec.display_name,
                "registry_sr_slug": rec.sr_slug,
            })
    return mismatches
