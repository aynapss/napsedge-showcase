from __future__ import annotations

import json
import os
import random
import re
import time
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
from bs4 import BeautifulSoup, Comment

from src.config.paths import CBB_DIR, NOTEBOOK_PATH
from src.scraper import (
    TEAM_SLUG_TO_SR_PATCH,
    SportsReferenceRateLimited,
    fetch_team_page,
    safe_get_with_backoff,
)

try:
    from src.slug_utils import _SR_OVERRIDES as MANUAL_SLUG_OVERRIDES
except Exception:
    MANUAL_SLUG_OVERRIDES = {}


CORE_CELL_INDICES = (89, 91, 92, 93, 95, 97, 98, 113, 114, 115)


def get_team_folder(team_slug: str) -> str:
    path = CBB_DIR / str(team_slug)
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _runtime_globals() -> dict:
    return {
        "BeautifulSoup": BeautifulSoup,
        "CBB_DIR": CBB_DIR,
        "Comment": Comment,
        "MANUAL_SLUG_OVERRIDES": MANUAL_SLUG_OVERRIDES,
        "NOTEBOOK_PATH": NOTEBOOK_PATH,
        "Path": Path,
        "SportsReferenceRateLimited": SportsReferenceRateLimited,
        "StringIO": StringIO,
        "TEAM_SLUG_TO_SR_PATCH": TEAM_SLUG_TO_SR_PATCH,
        "_safe_get_with_backoff": safe_get_with_backoff,
        "fetch_team_page": fetch_team_page,
        "get_team_folder": get_team_folder,
        "np": np,
        "os": os,
        "pd": pd,
        "random": random,
        "re": re,
        "time": time,
    }


def load_core_runtime(notebook_path: Path = NOTEBOOK_PATH) -> SimpleNamespace:
    """
    Load the v15 notebook's Sports-Reference team pipeline definitions.

    This bridge intentionally executes only the definition cells needed for
    scrape_team_season(), save_tables_to_excel(), and build_all_stats().
    """
    notebook = json.loads(notebook_path.read_text(encoding="utf-8"))
    env = _runtime_globals()

    for cell_index in CORE_CELL_INDICES:
        source = "".join(notebook["cells"][cell_index].get("source", []))
        exec(compile(source, f"{notebook_path.name}:cell-{cell_index}", "exec"), env)

    return SimpleNamespace(
        scrape_team_season=env["scrape_team_season"],
        save_tables_to_excel=env["save_tables_to_excel"],
        build_all_stats=env["build_all_stats"],
        cleanup_team_folder=env["cleanup_team_folder"],
        SportsReferenceRateLimited=SportsReferenceRateLimited,
    )


def run_sports_reference_refresh(
    team_name: str,
    team_slug: str,
    season: int = 2026,
    *,
    sleep_between: float = 7.0,
    cleanup: bool = False,
) -> dict[str, pd.DataFrame]:
    runtime = load_core_runtime()

    if cleanup:
        runtime.cleanup_team_folder(team_slug, season, keep_seasons=1)

    dfs = runtime.scrape_team_season(
        team_name,
        team_slug,
        int(season),
        sleep_between=float(sleep_between),
    )
    runtime.save_tables_to_excel(team_slug, int(season), dfs)
    runtime.build_all_stats(team_slug, int(season), dfs)
    return dfs
