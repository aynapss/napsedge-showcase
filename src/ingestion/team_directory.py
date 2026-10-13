from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import get_close_matches
from pathlib import Path

from src.config.paths import CBB_DIR, TEAM_LOGOS_DIR
from src.ingestion.load_data import list_team_slugs, load_conferences, simple_slug


CONFERENCE_ABBR = {
    "America East": "AE",
    "American Athletic": "AAC",
    "Atlantic 10": "A-10",
    "Atlantic Coast": "ACC",
    "ACC": "ACC",
    "ASUN": "ASUN",
    "Big 12": "B12",
    "Big East": "BE",
    "Big Sky": "BSky",
    "Big South": "BSth",
    "Big Ten": "B10",
    "Big West": "BW",
    "Coastal Athletic": "CAA",
    "Conference USA": "CUSA",
    "Horizon League": "HL",
    "Ivy League": "Ivy",
    "Metro Atlantic Athletic": "MAAC",
    "Mid-American": "MAC",
    "Missouri Valley": "MVC",
    "Mountain West": "MWC",
    "Northeast": "NEC",
    "Ohio Valley": "OVC",
    "Patriot League": "PL",
    "Southeastern": "SEC",
    "Southern": "SoCon",
    "Southland": "SL",
    "Southwestern Athletic": "SWAC",
    "Summit League": "Summit",
    "Sun Belt": "SBC",
    "West Coast": "WCC",
    "Western Athletic": "WAC",
}

DISPLAY_ALIASES = {
    "st-bonaventure": "St. Bonaventure",
    "north-carolina": "North Carolina",
}

INPUT_ALIASES = {
    "unc": "north-carolina",
    "tarheels": "north-carolina",
    "sbu": "st-bonaventure",
    "bonnies": "st-bonaventure",
    "ecu": "east-carolina",
    "vcu": "virginia-commonwealth",
    "uconn": "connecticut",
    "byu": "brigham-young",
    "usc": "usc",
    "ucla": "ucla",
    "smu": "southern-methodist",
    "uab": "alabama-birmingham",
    "utep": "texas-el-paso",
    "utsa": "utsa",
    "unlv": "nevada-las-vegas",
}

FOLDER_SLUG_ALIASES = {
    "easaint-carolina-pirates": "east-carolina",
    "south-carolina-saint": "south-carolina-state",
    "st bonaventure": "st-bonaventure",
    "st. bonaventure": "st-bonaventure",
}

LOGO_FILE_ALIASES = {
    "north-carolina": "North_Carolina_Tar_Heels.png",
    "st-bonaventure": "St_Bonaventure_Bonnies.png",
    "east-carolina": "East_Carolina_Pirates.png",
    "virginia-commonwealth": "VCU_Rams.png",
    "connecticut": "UConn_Huskies.png",
}


@dataclass(frozen=True)
class TeamDirectoryEntry:
    team_slug: str
    display_name: str
    conference: str | None
    conference_abbr: str | None
    logo_path: str | None


def _logo_key(value: str) -> str:
    text = str(value or "").lower().replace("&", "and")
    return re.sub(r"[^a-z0-9]+", "", text)


def _logo_index() -> dict[str, Path]:
    if not TEAM_LOGOS_DIR.exists():
        return {}
    index: dict[str, Path] = {}
    for path in TEAM_LOGOS_DIR.glob("*.png"):
        stem = path.stem.replace("_", " ")
        parts = stem.split()
        keys = {_logo_key(stem)}
        if len(parts) > 1:
            keys.add(_logo_key(" ".join(parts[:-1])))
            keys.add(_logo_key(" ".join(parts[:2])))
        for key in keys:
            if key:
                index.setdefault(key, path)
    return index


def resolve_logo_path(team_slug: str, display_name: str | None = None) -> Path | None:
    index = _logo_index()
    if not index:
        return None
    alias_file = LOGO_FILE_ALIASES.get(team_slug)
    if alias_file and (TEAM_LOGOS_DIR / alias_file).exists():
        return TEAM_LOGOS_DIR / alias_file
    candidates = [
        display_name or "",
        DISPLAY_ALIASES.get(team_slug, ""),
        team_slug.replace("-", " "),
        team_slug,
    ]
    for candidate in candidates:
        key = _logo_key(candidate)
        if key in index:
            return index[key]
    all_keys = list(index)
    for candidate in candidates:
        matches = get_close_matches(_logo_key(candidate), all_keys, n=1, cutoff=0.72)
        if matches:
            return index[matches[0]]
    return None


def _folder_candidates(display_name: str) -> list[str]:
    base = simple_slug(display_name)

    def token_replace(value: str, old: str, new: str) -> str:
        tokens = [new if token == old else token for token in value.split("-")]
        return "-".join(tokens)

    candidates = [
        base,
        token_replace(base, "saint", "st"),
        token_replace(base, "st", "saint"),
        token_replace(base, "state", "st"),
        token_replace(base, "st", "state"),
    ]
    aliases = {
        "st-bonaventure": ["saint-bonaventure"],
        "loyola-chicago": ["loyola-il"],
        "maryland-baltimore-county": ["umbc"],
        "massachusetts-lowell": ["umass-lowell"],
    }
    candidates.extend(aliases.get(base, []))
    deduped = []
    for item in candidates:
        if item and item not in deduped:
            deduped.append(item)
    return deduped


def build_team_directory() -> list[TeamDirectoryEntry]:
    conferences = load_conferences()
    folders = set(list_team_slugs())
    canonical_folder_slugs = {slug: slug for slug in folders}
    for source_slug, canonical_slug in FOLDER_SLUG_ALIASES.items():
        if source_slug in folders and canonical_slug not in canonical_folder_slugs:
            canonical_folder_slugs[canonical_slug] = source_slug
    rows: dict[str, TeamDirectoryEntry] = {}

    for conference, teams in conferences.items():
        for display_name in teams:
            slug = next(
                (
                    canonical_folder_slugs[candidate]
                    for candidate in _folder_candidates(display_name)
                    if candidate in canonical_folder_slugs
                ),
                None,
            )
            if slug is None:
                continue
            logo = resolve_logo_path(slug, display_name)
            rows[slug] = TeamDirectoryEntry(
                team_slug=slug,
                display_name=display_name,
                conference=conference,
                conference_abbr=CONFERENCE_ABBR.get(conference, conference),
                logo_path=str(logo) if logo else None,
            )

    for slug in folders:
        canonical_slug = FOLDER_SLUG_ALIASES.get(slug, slug)
        if canonical_slug != slug and canonical_slug in rows:
            continue
        if slug in rows:
            continue
        display = DISPLAY_ALIASES.get(slug, slug.replace("-", " ").title())
        logo = resolve_logo_path(slug, display)
        rows[slug] = TeamDirectoryEntry(
            team_slug=slug,
            display_name=display,
            conference=None,
            conference_abbr=None,
            logo_path=str(logo) if logo else None,
        )

    return sorted(rows.values(), key=lambda row: row.display_name.lower())


def resolve_team_input(value: str) -> TeamDirectoryEntry:
    query = str(value or "").strip()
    if not query:
        raise ValueError("Team value is blank.")
    directory = build_team_directory()
    by_slug = {row.team_slug: row for row in directory}
    if query in by_slug:
        return by_slug[query]

    query_key = _logo_key(query)
    if query_key in INPUT_ALIASES and INPUT_ALIASES[query_key] in by_slug:
        return by_slug[INPUT_ALIASES[query_key]]

    aliases: dict[str, TeamDirectoryEntry] = {}
    for row in directory:
        values = {
            row.team_slug,
            row.team_slug.replace("-", " "),
            row.display_name,
            f"{row.display_name} {row.conference_abbr or ''}",
        }
        for item in values:
            key = _logo_key(item)
            if key not in aliases or (aliases[key].conference is None and row.conference is not None):
                aliases[key] = row

    if query_key in aliases:
        return aliases[query_key]
    matches = get_close_matches(query_key, aliases.keys(), n=1, cutoff=0.58)
    if matches:
        return aliases[matches[0]]
    raise ValueError(f"Could not resolve team input: {value}")


def autocomplete_teams(query: str, limit: int = 25) -> list[TeamDirectoryEntry]:
    query_key = _logo_key(query)
    rows = build_team_directory()
    deduped: dict[str, TeamDirectoryEntry] = {}
    for row in rows:
        dedupe_key = _logo_key(row.display_name)
        existing = deduped.get(dedupe_key)
        if existing is None or (existing.conference is None and row.conference is not None):
            deduped[dedupe_key] = row
    rows = sorted(deduped.values(), key=lambda row: row.display_name.lower())
    if not query_key:
        return rows[:limit]
    alias_slug = INPUT_ALIASES.get(query_key)
    alias_row = next((row for row in rows if row.team_slug == alias_slug), None)
    exact_rows = []
    fuzzy_rows = []
    for row in rows:
        haystack = _logo_key(f"{row.display_name} {row.team_slug} {row.conference_abbr or ''}")
        if query_key in haystack:
            exact_rows.append(row)
        else:
            matches = get_close_matches(query_key, [haystack], n=1, cutoff=0.45)
            if matches:
                fuzzy_rows.append(row)
    selected = exact_rows or fuzzy_rows
    if alias_row:
        prefix_rows = [
            row
            for row in exact_rows
            if _logo_key(row.display_name).startswith(query_key) or _logo_key(row.team_slug).startswith(query_key)
        ]
        if not prefix_rows:
            return [alias_row]
        rest = sorted((row for row in prefix_rows if row.team_slug != alias_row.team_slug), key=lambda row: row.display_name.lower())
        return ([alias_row] + rest)[:limit]
    return sorted(selected, key=lambda row: row.display_name.lower())[:limit]
