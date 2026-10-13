from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from src.config.paths import ROOT_DIR


ODDS_API_URL = "https://api.the-odds-api.com/v4/sports/basketball_ncaab/odds"


@dataclass(frozen=True)
class AutoMarketResult:
    status: str
    spread: float | None = None
    total: float | None = None
    open_spread: float | None = None
    close_spread: float | None = None
    source: str = "the_odds_api"
    matched_game: dict[str, Any] | None = None
    message: str = ""


def load_env_file(path: Path | None = None) -> None:
    env_path = path or ROOT_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        clean = line.strip()
        if not clean or clean.startswith("#") or "=" not in clean:
            continue
        key, value = clean.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _norm(value: str) -> str:
    text = str(value or "").lower()
    text = text.replace("&", "and")
    text = re.sub(r"\b(st|saint)\b", "saint", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(value: str) -> set[str]:
    stop = {"university", "college", "state", "men", "basketball"}
    return {token for token in _norm(value).split() if token and token not in stop}


def _match_team(query: str, candidate: str) -> bool:
    q = _tokens(query.replace("-", " "))
    c = _tokens(candidate)
    if not q or not c:
        return False
    if q <= c or c <= q:
        return True
    overlap = q & c
    return len(overlap) >= min(2, len(q), len(c))


def _extract_market(bookmakers: list[dict[str, Any]], market_key: str) -> list[dict[str, Any]]:
    rows = []
    for book in bookmakers or []:
        title = book.get("title") or book.get("key") or "book"
        for market in book.get("markets", []):
            if market.get("key") == market_key:
                rows.append({"book": title, "outcomes": market.get("outcomes", [])})
    return rows


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def _extract_spread_for_team(game: dict[str, Any], team_a_slug: str) -> float | None:
    values = []
    for market in _extract_market(game.get("bookmakers", []), "spreads"):
        for outcome in market["outcomes"]:
            point = outcome.get("point")
            if point is not None and _match_team(team_a_slug, outcome.get("name", "")):
                values.append(float(point))
    return _avg(values)


def _extract_total(game: dict[str, Any]) -> float | None:
    values = []
    for market in _extract_market(game.get("bookmakers", []), "totals"):
        for outcome in market["outcomes"]:
            point = outcome.get("point")
            if point is not None:
                values.append(float(point))
    return _avg(values)


def fetch_auto_market(
    team_a_slug: str,
    team_b_slug: str,
    *,
    regions: str = "us",
    odds_format: str = "american",
    timeout: int = 20,
) -> AutoMarketResult:
    """
    Resolve current NCAAB spread and total for a selected matchup.

    Uses The Odds API v4 odds endpoint with markets `spreads,totals`, the
    documented endpoint for popular markets such as point spreads and totals.
    """
    load_env_file()
    api_key = os.environ.get("ODDS_API_KEY", "").strip()
    if not api_key:
        return AutoMarketResult(status="missing_key", message="ODDS_API_KEY is not set.")

    params = {
        "apiKey": api_key,
        "regions": regions,
        "markets": "spreads,totals",
        "oddsFormat": odds_format,
    }
    try:
        response = requests.get(ODDS_API_URL, params=params, timeout=timeout)
        response.raise_for_status()
        games = response.json()
    except Exception as exc:
        return AutoMarketResult(status="fetch_failed", message=f"{type(exc).__name__}: {exc}")

    for game in games:
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        teams_match = (
            (_match_team(team_a_slug, home) and _match_team(team_b_slug, away))
            or (_match_team(team_a_slug, away) and _match_team(team_b_slug, home))
        )
        if not teams_match:
            continue

        spread = _extract_spread_for_team(game, team_a_slug)
        total = _extract_total(game)
        return AutoMarketResult(
            status="matched",
            spread=spread,
            total=total,
            close_spread=spread,
            matched_game={
                "id": game.get("id"),
                "home_team": home,
                "away_team": away,
                "commence_time": game.get("commence_time"),
                "bookmaker_count": len(game.get("bookmakers", [])),
            },
            message="Auto market resolved from current odds feed.",
        )

    return AutoMarketResult(
        status="no_match",
        message=f"No live odds matchup matched {team_a_slug} vs {team_b_slug}.",
    )
