"""
Sports-Reference/AllStats player-identity adapter (Phase 2B, batch 1).

Bounded scope per this batch's instructions: resolves Sports-Reference
roster records into canonical player identities, and resolves AllStats
records against the correct team+season roster. Does not touch ESPN,
injury, box-score, postgame, OVR, or radar player matching -- those
adapters' dependencies/call sites are inventoried in
docs/migration_state.md but not implemented here.

Source functions (see src.identity.player_identity's module docstring for
the full inventory): `harmonize_player_names` (cell 93) and
`extract_player_slugs_from_roster` (cell 91), both from
cbb_teamScrape_v15_TEST.ipynb. Neither is modified in place; both are
ported here as byte-faithful compatibility wrappers, with a new
canonical-registry-routed path alongside.
"""
from __future__ import annotations

import re

import pandas as pd

from src.exceptions import ValidationError
from src.identity.contracts import PlayerIdentity, TeamIdentity, UnresolvedIdentity
from src.identity.player_identity import (
    build_player_identity,
    canonical_player_key_v15,
    find_duplicate_player_keys,
    match_stat_row_to_roster,
    normalize_player_name_v15,
)
from src.identity.team_identity import resolve_team_identity
from src.logging_config import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Legacy compatibility wrappers -- byte-faithful to v15
# ---------------------------------------------------------------------------

#: Verbatim from notebook cell 93's `harmonize_player_names`. A small,
#: hardcoded, reviewed patch table: (team_slug, suffix-stripped key) ->
#: corrected display name, for known cases where the raw scraped name is
#: missing a suffix. This is exactly the "reviewed aliases" concept this
#: batch's contracts note calls for -- kept here as adapter-local data
#: (like every other source-specific alias table in this migration),
#: not promoted into a general table.
_REVIEWED_PLAYER_SUFFIX_PATCHES: dict[tuple[str, str], str] = {
    ("st-bonaventure", "daryl simmons"): "Daryl Simmons II",
    ("kansas", "melvin council"): "Melvin Council Jr.",
}


def harmonize_player_names_v15(team_slug: str, dfs: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """
    Verbatim port of notebook cell 93's `harmonize_player_names`: applies
    display normalization, computes the ``PlayerKey`` matching column, and
    applies the reviewed suffix patches above. Preserves the exact
    ``dict[str, pd.DataFrame]`` in/out contract.
    """
    for label, df in dfs.items():
        if df.empty or "Player" not in df.columns:
            continue

        df["Player"] = df["Player"].astype(str).map(normalize_player_name_v15)
        df["PlayerKey"] = df["Player"].map(canonical_player_key_v15)

        for (patch_team, patch_key), corrected_name in _REVIEWED_PLAYER_SUFFIX_PATCHES.items():
            if team_slug == patch_team:
                df.loc[df["PlayerKey"] == patch_key, "Player"] = corrected_name

        dfs[label] = df

    return dfs


def extract_player_slugs_from_roster_v15(soup) -> dict[str, str]:
    """
    Verbatim port of notebook cell 91's `extract_player_slugs_from_roster`.
    Extracts real Sports-Reference player slugs from
    ``<a href="/cbb/players/{slug}.html">`` anchors in the roster table
    (including SR's HTML-comment-hidden table variant). Returns
    ``{normalized_display_name: sr_player_slug}``.

    This is the one place in the whole v15 pipeline where an authoritative
    per-player source ID is actually available -- but per this module's
    parent docstring, the notebook never persists this map into AllStats.
    Callers of :func:`resolve_roster_player_identities` can pass this
    map's output in to attach real source IDs when scraping fresh HTML;
    when working from an already-saved AllStats.xlsx there is nothing to
    pass, and identities fall back to the team+season+name-scoped id.
    """
    from bs4 import BeautifulSoup, Comment

    slug_map: dict[str, str] = {}
    table = soup.find("table", id="roster")
    if table is None:
        for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
            if "roster" in comment:
                sub = BeautifulSoup(comment, "lxml")
                table = sub.find("table", id="roster")
                if table:
                    break
    if table is None:
        return slug_map

    for a in table.find_all("a", href=True):
        if "/cbb/players/" not in a["href"]:
            continue
        href = a["href"]
        player_name = normalize_player_name_v15(a.get_text(strip=True))
        parts = [p for p in href.strip("/").split("/") if p]
        try:
            players_idx = parts.index("players")
            slug = parts[players_idx + 1].replace(".html", "")
            if player_name and slug:
                slug_map[player_name] = slug
        except (ValueError, IndexError):
            continue
    return slug_map


# ---------------------------------------------------------------------------
# New canonical path -- routes through Phase 2A's canonical team registry,
# no fuzzy matching, structured ambiguous/unresolved, duplicate detection
# ---------------------------------------------------------------------------


def resolve_roster_player_identities(
    roster_df: pd.DataFrame,
    team_query: str,
    season: int,
    *,
    slug_map: dict[str, str] | None = None,
    source: str = "sports_reference",
) -> tuple[list[PlayerIdentity], list[dict], TeamIdentity | UnresolvedIdentity]:
    """
    Build a :class:`PlayerIdentity` for every row of a Sports-Reference
    roster DataFrame, scoped to the canonical team resolved from
    ``team_query`` (routed through Phase 2A's `resolve_team_identity` --
    requirement #3, "uses canonical team identities from Phase 2A") and
    ``season``.

    Requires a ``"Player"`` column; raises :class:`ValidationError`
    otherwise (canonical-path validation, distinct from the legacy
    wrapper's silent no-op on a missing column).

    Returns ``(identities, duplicates, team_result)``:

    - ``identities``: one :class:`PlayerIdentity` per roster row. If
      ``slug_map`` is supplied (e.g. from
      :func:`extract_player_slugs_from_roster_v15` on freshly-scraped
      HTML) and it has an entry for a row's normalized name, that row's
      identity carries a real ``sources["sports_reference"]``; otherwise
      it falls back to the team+season+name-scoped id.
    - ``duplicates``: output of :func:`find_duplicate_player_keys` --
      non-empty means two or more rows share a suffix-stripped key
      ("detects duplicate normalized names within the same roster",
      requirement #5). Every identity is still returned (this function
      does not silently drop or merge them -- see
      :func:`match_stat_row_to_roster` for the safe-merge story), but a
      non-empty ``duplicates`` result means those specific players cannot
      be safely disambiguated by name alone within this roster.
    - ``team_result``: the resolved :class:`TeamIdentity`, or the
      :class:`UnresolvedIdentity` if ``team_query`` itself didn't resolve
      -- in which case ``identities``/``duplicates`` are both empty
      (requirement #6, "rejects cross-team name-only matches": if the team
      itself is unresolved/ambiguous, no player identities are
      constructed under a guessed team_id).
    """
    team_result = resolve_team_identity(team_query, source=source)
    if not isinstance(team_result, TeamIdentity):
        return [], [], team_result

    if "Player" not in roster_df.columns:
        raise ValidationError(f"roster_df missing 'Player' column. Columns: {roster_df.columns.tolist()}")

    names = roster_df["Player"].astype(str).tolist()
    duplicates = find_duplicate_player_keys(names)

    identities: list[PlayerIdentity] = []
    for raw_name in names:
        normalized = normalize_player_name_v15(raw_name)
        sr_slug = (slug_map or {}).get(normalized)
        identities.append(
            build_player_identity(
                raw_name,
                team_result.team_id,
                season,
                source=source,
                source_player_id=sr_slug,
            )
        )

    if duplicates:
        log.warning(
            "roster for %r (team_id=%d, season=%d) has %d colliding PlayerKey group(s): %s",
            team_query, team_result.team_id, season, len(duplicates), duplicates,
        )

    return identities, duplicates, team_result


def resolve_allstats_player_row(
    player_name: str,
    roster_identities: list[PlayerIdentity],
) -> PlayerIdentity | UnresolvedIdentity:
    """
    Resolve one AllStats/stat-table row's player name against a roster
    already scoped to one team+season (the output of
    :func:`resolve_roster_player_identities`'s ``identities``).

    Thin, purpose-named wrapper over
    :func:`src.identity.player_identity.match_stat_row_to_roster` -- exists
    so this adapter's public surface reads as "resolve an AllStats record,"
    matching requirement #2, without duplicating the matching logic itself.
    Rejects cross-team matches by construction: ``roster_identities`` is
    never a cross-team pool, only ever one already-resolved team's roster.

    MOJIBAKE-COMPARISON FIX (confirmed defect, this batch): a roster
    identity's own ``display_name`` is always already
    ``normalize_player_name_v15``-repaired (that repair happens inside
    :func:`build_player_identity`, which every roster identity is built
    through). `match_stat_row_to_roster` itself computes its comparison
    key straight from whatever string it is given -- it does not repair
    anything on either side. Calling it with a still-raw stat-table name
    therefore compares an un-repaired key against an already-repaired one
    -- confirmed, by direct reproduction against the real
    `alabama-birmingham` fixture, to be the exact cause of all 60
    previously-unresolved stat rows across this batch's full 304-team dry
    run (11 distinct real international players, each carrying a genuine
    UTF-8-decoded-as-Latin-1 mojibake artifact in Sports-Reference's own
    raw HTML -- e.g. "JoÃ£o" for "João"). Fixed **here**, narrowly, by
    repairing `player_name` with the same established
    `normalize_player_name_v15` authority before delegating -- reusing it,
    never duplicating its replacement table, and never touching
    `match_stat_row_to_roster` itself, which 5 other identity modules
    (boxscore/ESPN/injury/OVR-radar/postgame) also call directly and
    which must keep its own byte-identical behavior for all of them (this
    batch's own instruction: "target only the documented mojibake-related
    AllStats matching gap"). Repairing here is safe and symmetric: the
    roster side's own key input (`p.display_name`) is already repaired,
    and repairing a name that needs no repair is a confirmed no-op (a
    plain ASCII name, or an already-correct accented name, produces an
    identical canonical key whether or not this repair step runs first --
    proven by test). This can only ever change a `not_found` result into
    a `matched` or a genuine `ambiguous` one; it cannot change any
    currently-`matched` row's own outcome, since the roster-side key set
    this compares against is completely unaffected by this change.
    """
    repaired_name = normalize_player_name_v15(player_name)
    return match_stat_row_to_roster(repaired_name, roster_identities, source="allstats_match")
