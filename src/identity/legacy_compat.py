"""
Compatibility wrappers that reproduce exact v15 notebook behavior, verbatim,
for callers that need bit-exact notebook parity rather than the canonical
identity layer's improved (but *different*) behavior.

Why this file exists: docs/notebook_src_coverage.csv documents that
``src.slug_utils.normalize_team_name`` matches notebook cell 10, but the
notebook's own execution order makes cell 43's redefinition the one that is
ACTUALLY active at runtime -- and cell 43 differs from cell 10 (it does not
strip curly right-single-quotes). Neither ``src.slug_utils.normalize_team_name``
nor this file's function was changed to match the other; per the 2026-08-27
apostrophe decision, curly/straight-apostrophe folding was added only to
``src.identity.team_identity.resolve_team_identity`` (the new canonical
service), and this module preserves the exact notebook-active behavior for
any caller that specifically needs it (e.g. regenerating an artifact that
must byte-match current notebook output).
"""
from __future__ import annotations

import re


def normalize_team_name_v15_active(name: str) -> str:
    """
    Verbatim reproduction of ``normalize_team_name`` as defined in
    ``cbb_teamScrape_v15_TEST.ipynb`` cell 43 -- the definition that wins at
    notebook runtime (it's defined after, and therefore overrides, the
    cell-10 definition that ``src.slug_utils.normalize_team_name`` matches).

    Deliberately does NOT strip curly right-single-quotes (unlike both
    ``src.slug_utils.normalize_team_name`` and
    ``src.identity.team_identity.resolve_team_identity``). Do not
    "improve" this function -- its entire purpose is to stay byte-identical
    to the notebook cell it mirrors. If cell 43 changes, update this
    docstring's cell reference and re-verify via source diff, don't guess.
    """
    if not name:
        return ""

    name = name.lower()

    # Remove parentheticals: (FL), (OH), (NC)
    name = re.sub(r"\(.*?\)", "", name)

    replacements = {
        "&": "and",
        "st.": "saint",
        "st ": "saint ",
        "'": "",
        ".": "",
        "-": " ",
        "_": " ",
    }

    for k, v in replacements.items():
        name = name.replace(k, v)

    name = re.sub(r"\s+", " ", name).strip()

    return name
