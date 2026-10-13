import ast
import json
import re

import pandas as pd
import streamlit as st

from src.paths import CBB_DIR, NOTEBOOK_PATH


@st.cache_data
def list_teams() -> list[str]:
    """
    Return a sorted list of team folder names from TeamDump/CBB.

    Example:
    [
        "akron",
        "arizona",
        "st-bonaventure"
    ]
    """
    if not CBB_DIR.exists():
        return []

    teams = []

    for folder in CBB_DIR.iterdir():
        if folder.is_dir():
            teams.append(folder.name)

    return sorted(teams)


@st.cache_data
def load_conferences() -> dict[str, list[str]]:
    """
    Read the notebook's conferences dictionary.

    This keeps the Streamlit app aligned with the pipeline while the project is
    still being refactored out of the notebook.
    """
    if not NOTEBOOK_PATH.exists():
        return {}

    notebook = json.loads(NOTEBOOK_PATH.read_text(encoding="utf-8"))

    for cell in notebook.get("cells", []):
        source = "".join(cell.get("source", []))

        if "conferences = {" not in source:
            continue

        tree = ast.parse(source)

        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue

            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "conferences":
                    return ast.literal_eval(node.value)

    return {}


def _simple_slug(value: str) -> str:
    """
    Convert a display team name into a folder-like slug.

    Example:
    "St. Bonaventure" -> "st-bonaventure"
    """
    text = str(value).lower().strip()
    text = text.replace("&", "and")
    text = re.sub(r"[()\"']", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def _team_folder_candidates(team_name: str) -> list[str]:
    """
    Build possible TeamDump/CBB folder names for one conference team.
    """
    base = _simple_slug(team_name)
    manual_aliases = {
        "loyola-chicago": "loyola-il",
    }
    candidates = [
        base,
        manual_aliases.get(base, ""),
        base.replace("saint-", "st-"),
        base.replace("st-", "saint-"),
        base.replace("-state", "-st"),
        base.replace("-st", "-state"),
    ]

    deduped = []
    for item in candidates:
        if item and item not in deduped:
            deduped.append(item)

    return deduped


@st.cache_data
def list_conferences() -> list[str]:
    """
    Return conference names from the notebook conference dictionary.
    """
    return sorted(load_conferences().keys())


@st.cache_data
def list_teams_for_conference(conference: str) -> list[dict[str, str]]:
    """
    Return teams for one conference, mapped to available TeamDump/CBB folders.

    Each row contains:
    - display_name: the human-readable name from the notebook
    - team_slug: the folder name used by local files
    """
    conferences = load_conferences()
    existing_folders = set(list_teams())
    rows = []

    for display_name in conferences.get(conference, []):
        matched_slug = ""

        for candidate in _team_folder_candidates(display_name):
            if candidate in existing_folders:
                matched_slug = candidate
                break

        rows.append(
            {
                "display_name": display_name,
                "team_slug": matched_slug,
            }
        )

    return rows


@st.cache_data
def load_team_game_log(team_slug: str, season: int) -> pd.DataFrame:
    """
    Load one team's season game log.

    Example path:
    TeamDump/CBB/st-bonaventure/st-bonaventure_2026_game_log.csv
    """
    path = CBB_DIR / team_slug / f"{team_slug}_{season}_game_log.csv"

    if not path.exists():
        return pd.DataFrame()

    return pd.read_csv(path)


@st.cache_data
def load_team_all_stats(team_slug: str, season: int) -> pd.DataFrame:
    """
    Load one team's all-stats Excel file.

    Example path:
    TeamDump/CBB/st-bonaventure/st-bonaventure_2026_AllStats.xlsx
    """
    path = CBB_DIR / team_slug / f"{team_slug}_{season}_AllStats.xlsx"

    if not path.exists():
        return pd.DataFrame()

    return pd.read_excel(path)


@st.cache_data
def load_team_roster(team_slug: str, season: int) -> pd.DataFrame:
    """
    Load one team's roster file.

    Example path:
    TeamDump/CBB/st-bonaventure/Roster_2026.xlsx
    """
    path = CBB_DIR / team_slug / f"Roster_{season}.xlsx"

    if not path.exists():
        return pd.DataFrame()

    return pd.read_excel(path)


@st.cache_data
def load_team_advanced(team_slug: str, season: int) -> pd.DataFrame:
    """
    Load one team's advanced stats file.

    Example path:
    TeamDump/CBB/st-bonaventure/Advanced_2026.xlsx
    """
    path = CBB_DIR / team_slug / f"Advanced_{season}.xlsx"

    if not path.exists():
        return pd.DataFrame()

    return pd.read_excel(path)
