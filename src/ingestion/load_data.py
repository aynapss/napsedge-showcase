from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from src.config.paths import CBB_DIR, NOTEBOOK_PATH


@dataclass(frozen=True)
class TeamSeasonFiles:
    team_slug: str
    season: int
    team_dir: Path
    roster_path: Path
    advanced_path: Path
    per_game_path: Path
    totals_path: Path
    all_stats_path: Path
    game_log_path: Path


def simple_slug(value: str) -> str:
    text = str(value).lower().strip()
    text = text.replace("&", "and")
    text = re.sub(r"[()\"']", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def team_files(team_slug: str, season: int = 2026) -> TeamSeasonFiles:
    slug = simple_slug(team_slug)
    team_dir = CBB_DIR / slug
    return TeamSeasonFiles(
        team_slug=slug,
        season=int(season),
        team_dir=team_dir,
        roster_path=team_dir / f"Roster_{season}.xlsx",
        advanced_path=team_dir / f"Advanced_{season}.xlsx",
        per_game_path=team_dir / f"PerGame_{season}.xlsx",
        totals_path=team_dir / f"Totals_{season}.xlsx",
        all_stats_path=team_dir / f"{slug}_{season}_AllStats.xlsx",
        game_log_path=team_dir / f"{slug}_{season}_game_log.csv",
    )


def list_team_slugs() -> list[str]:
    if not CBB_DIR.exists():
        return []
    return sorted(path.name for path in CBB_DIR.iterdir() if path.is_dir())


def load_conferences(notebook_path: Path = NOTEBOOK_PATH) -> dict[str, list[str]]:
    if not notebook_path.exists():
        return {}

    notebook = json.loads(notebook_path.read_text(encoding="utf-8"))
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


def load_excel(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_excel(path)


def load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def load_team_dataset(team_slug: str, season: int = 2026) -> dict[str, pd.DataFrame]:
    files = team_files(team_slug, season)
    return {
        "Roster": load_excel(files.roster_path),
        "Advanced": load_excel(files.advanced_path),
        "PerGame": load_excel(files.per_game_path),
        "Totals": load_excel(files.totals_path),
        "AllStats": load_excel(files.all_stats_path),
        "GameLog": load_csv(files.game_log_path),
    }
