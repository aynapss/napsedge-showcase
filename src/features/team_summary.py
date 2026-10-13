from __future__ import annotations

import math
import json
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from src.config.paths import CBB_DIR
from src.ingestion.load_data import load_team_dataset
from src.ingestion.team_directory import resolve_team_input


def _num(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(df[column], errors="coerce").dropna()


def _mean(df: pd.DataFrame, *columns: str) -> float | None:
    for column in columns:
        values = _num(df, column)
        if not values.empty:
            return round(float(values.mean()), 3)
    return None


def _series_or_none(df: pd.DataFrame, *columns: str) -> pd.Series | None:
    for column in columns:
        values = _num(df, column)
        if not values.empty:
            return values
    return None


def _weighted_mean(df: pd.DataFrame, value_col: str, weight_col: str = "MPG") -> float | None:
    if value_col not in df.columns:
        return None
    values = pd.to_numeric(df[value_col], errors="coerce")
    if weight_col in df.columns:
        weights = pd.to_numeric(df[weight_col], errors="coerce").fillna(0.0).clip(lower=0.0)
    elif "MP_PerGame" in df.columns:
        weights = pd.to_numeric(df["MP_PerGame"], errors="coerce").fillna(0.0).clip(lower=0.0)
    else:
        weights = pd.Series(1.0, index=df.index)

    mask = values.notna() & weights.notna() & (weights > 0)
    if not mask.any():
        return None
    return round(float((values[mask] * weights[mask]).sum() / weights[mask].sum()), 3)


def _sum_numeric(df: pd.DataFrame, *columns: str) -> float | None:
    for column in columns:
        values = _num(df, column)
        if not values.empty:
            return round(float(values.sum()), 3)
    return None


def _top_players(all_stats: pd.DataFrame, n: int = 5) -> list[dict[str, Any]]:
    if all_stats.empty:
        return []

    name_col = "Player" if "Player" in all_stats.columns else all_stats.columns[0]
    value_col = next(
        (
            column
            for column in ["PlayerValueIndex", "OVR", "O_Impact", "PPG"]
            if column in all_stats.columns
        ),
        None,
    )
    if value_col is None:
        return []

    df = all_stats[[name_col, value_col]].copy()
    df[value_col] = pd.to_numeric(df[value_col], errors="coerce")
    df = df.dropna(subset=[value_col]).sort_values(value_col, ascending=False).head(n)
    return [
        {"player": str(row[name_col]), value_col: round(float(row[value_col]), 3)}
        for _, row in df.iterrows()
    ]


def _record_from_game_log(game_log: pd.DataFrame) -> dict[str, int]:
    if game_log.empty:
        return {"wins": 0, "losses": 0}
    df = game_log.copy()
    if "date" in df.columns:
        df = df[df["date"].notna()]
    if "opponent" in df.columns:
        df = df[df["opponent"].notna()]

    wins = losses = 0
    if "result" in df.columns:
        result = df["result"].astype(str).str.strip().str.upper()
        wins = int(result.str.startswith("W").sum())
        losses = int(result.str.startswith("L").sum())
    elif {"pts_scored", "pts_allowed"}.issubset(df.columns):
        scored = pd.to_numeric(df["pts_scored"], errors="coerce")
        allowed = pd.to_numeric(df["pts_allowed"], errors="coerce")
        valid = scored.notna() & allowed.notna() & (scored < 200) & (allowed < 200)
        wins = int((scored[valid] > allowed[valid]).sum())
        losses = int((scored[valid] < allowed[valid]).sum())
    return {"wins": wins, "losses": losses}


def _player_filename_stem(player_name: str) -> str:
    import re

    text = str(player_name).lower().strip()
    text = text.replace('"', "").replace("'", "").replace(".", "")
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def _player_trends(team_slug: str, season: int, top_players: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trends = []
    team_dir = CBB_DIR / team_slug
    for player in top_players:
        name = player.get("player")
        if not name:
            continue
        path = team_dir / f"{_player_filename_stem(name)}_{season}_gamelog.csv"
        if not path.exists():
            continue
        try:
            gl = pd.read_csv(path)
        except Exception:
            continue
        pts = pd.to_numeric(gl.get("pts", pd.Series(dtype=float)), errors="coerce").dropna()
        mp = pd.to_numeric(gl.get("mp", pd.Series(dtype=float)), errors="coerce").dropna()
        if pts.empty:
            continue
        season_pts = float(pts.mean())
        last5_pts = float(pts.tail(5).mean())
        season_mp = float(mp.mean()) if not mp.empty else None
        last5_mp = float(mp.tail(5).mean()) if not mp.empty else None
        trends.append(
            {
                "player": name,
                "season_ppg": round(season_pts, 2),
                "last5_ppg": round(last5_pts, 2),
                "ppg_delta": round(last5_pts - season_pts, 2),
                "season_mpg": round(season_mp, 2) if season_mp is not None else None,
                "last5_mpg": round(last5_mp, 2) if last5_mp is not None else None,
                "streak": _streak_label(last5_pts - season_pts),
            }
        )
    return sorted(trends, key=lambda row: abs(row["ppg_delta"]), reverse=True)[:5]


def _streak_label(delta: float) -> str:
    if delta >= 4:
        return "hot streak"
    if delta >= 1.5:
        return "warming up"
    if delta <= -4:
        return "cold streak"
    if delta <= -1.5:
        return "cooling off"
    return "steady"


def build_injury_report(team_slug: str, *, search_paths: Sequence[str | Path] | None = None) -> dict[str, Any]:
    """
    Public authority for a team's local-cache injury report (Phase 6,
    Slice 6A extraction of the original `_injury_report`; see that
    function's own docstring below).

    `search_paths`, when given, replaces the default two-candidate lookup
    below with an explicit, caller-supplied list -- lets a caller (a test,
    or a future service) avoid depending on the real `TeamDump/` directory
    or a mutable module-level global, without changing the default
    behavior for every existing caller that omits it (`_injury_report`,
    `build_team_summary`), which still resolves the identical two
    candidate paths this function has always used.

    Byte-for-byte identical calculation/formatting logic to the original
    private function -- no new field, no new filtering, no new inference.
    """
    if search_paths is None:
        candidate_paths: list[Path] = [
            CBB_DIR / team_slug / f"{team_slug}_injuries.json",
            CBB_DIR.parent / "injury_cache" / f"{team_slug}_injuries.json",
        ]
    else:
        candidate_paths = [Path(p) for p in search_paths]

    for path in candidate_paths:
        if not path.exists():
            continue
        try:
            injuries = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(injuries, list):
            continue
        return {
            "status": "loaded",
            "source": str(path),
            "injuries": injuries,
            "impactful_count": len(
                [
                    item
                    for item in injuries
                    if str(item.get("status", "")).lower() in {"out", "doubtful", "questionable"}
                ]
            ),
        }
    return {
        "status": "not_loaded",
        "source": None,
        "injuries": [],
        "impactful_count": 0,
        "message": "No confirmed local injury report loaded.",
    }


def _injury_report(team_slug: str) -> dict[str, Any]:
    """
    Kept as the existing, unmodified call site for `build_team_summary`
    below -- delegates entirely to the new public `build_injury_report`,
    using its same default two-candidate lookup. Not removed, so no
    caller of this module needs to change.
    """
    return build_injury_report(team_slug)


def build_team_summary(team_slug: str, season: int = 2026) -> dict[str, Any]:
    directory_entry = None
    try:
        directory_entry = resolve_team_input(team_slug)
        team_slug = directory_entry.team_slug
    except Exception:
        pass

    data = load_team_dataset(team_slug, season)
    all_stats = data["AllStats"]
    game_log = data["GameLog"]
    top_players = _top_players(all_stats)

    summary: dict[str, Any] = {
        "team_slug": team_slug,
        "display_name": directory_entry.display_name if directory_entry else team_slug.replace("-", " ").title(),
        "conference": directory_entry.conference if directory_entry else None,
        "conference_abbr": directory_entry.conference_abbr if directory_entry else None,
        "logo_path": directory_entry.logo_path if directory_entry else None,
        "season": int(season),
        "files_loaded": {
            key: not df.empty
            for key, df in data.items()
        },
        "player_count": int(len(all_stats)) if not all_stats.empty else 0,
        "games_logged": int(len(game_log)) if not game_log.empty else 0,
        "record": _record_from_game_log(game_log),
        "top_players": top_players,
        "player_trends": _player_trends(team_slug, int(season), top_players),
        "injury_report": _injury_report(team_slug),
    }

    if not all_stats.empty:
        summary["team_averages"] = {
            "ppg": _mean(all_stats, "PPG", "PTS_PerGame"),
            "mpg": _mean(all_stats, "MPG", "MP_PerGame"),
            "off_impact": _mean(all_stats, "O_Impact"),
            "def_impact": _mean(all_stats, "D_Impact"),
            "player_value_index": _mean(all_stats, "PlayerValueIndex"),
        }
        summary["rating_profile"] = {
            "off_rating": _weighted_mean(all_stats, "OFF RTG"),
            "def_rating": _weighted_mean(all_stats, "DEF RTG"),
            "net_rating": _weighted_mean(all_stats, "NET RTG"),
            "true_shooting": _weighted_mean(all_stats, "TS%"),
            "effective_fg": _weighted_mean(all_stats, "EFG%"),
            "turnover_pct": _weighted_mean(all_stats, "TO%"),
            "assist_pct": _weighted_mean(all_stats, "AST%"),
            "off_reb_pct": _weighted_mean(all_stats, "OFF REB%"),
            "def_reb_pct": _weighted_mean(all_stats, "DEF REB%"),
            "block_pct": _weighted_mean(all_stats, "BLK%"),
            "steal_pct": _weighted_mean(all_stats, "STL%"),
            "three_point_tendency": _weighted_mean(all_stats, "3FG Tendency"),
            "ft_rate": _weighted_mean(all_stats, "FT RATE"),
        }
        summary["rotation_profile"] = {
            "rotation_players_8mpg": int(
                (pd.to_numeric(all_stats.get("MPG", all_stats.get("MP_PerGame", 0)), errors="coerce").fillna(0) >= 8).sum()
            ),
            "top5_minutes_share": _top_minutes_share(all_stats, 5),
            "top8_minutes_share": _top_minutes_share(all_stats, 8),
            "total_points_per_game": _sum_numeric(all_stats, "PPG", "PTS_PerGame"),
        }

    if not game_log.empty:
        ordered_log = game_log.copy()
        if "date" in ordered_log.columns:
            ordered_log = ordered_log[ordered_log["date"].notna()]
        if "opponent" in ordered_log.columns:
            ordered_log = ordered_log[ordered_log["opponent"].notna()]
        if "pts_scored" in ordered_log.columns:
            ordered_log = ordered_log[
                pd.to_numeric(ordered_log["pts_scored"], errors="coerce").fillna(999) < 200
            ]
        if "pts_allowed" in ordered_log.columns:
            ordered_log = ordered_log[
                pd.to_numeric(ordered_log["pts_allowed"], errors="coerce").fillna(999) < 200
            ]

        if "date" in ordered_log.columns:
            ordered_log["_date_sort"] = pd.to_datetime(
                ordered_log["date"],
                errors="coerce",
            )
            ordered_log = ordered_log.sort_values("_date_sort")

        points_for = _series_or_none(ordered_log, "pts_scored", "PTS", "pts")
        points_against = _series_or_none(ordered_log, "pts_allowed", "Opp PTS", "opp_pts")
        if points_for is None:
            points_for = pd.Series(dtype=float)
        if points_against is None:
            points_against = pd.Series(dtype=float)

        margin_series = _series_or_none(ordered_log, "margin")
        if margin_series is None and not points_for.empty and not points_against.empty:
            margin_series = points_for.reset_index(drop=True) - points_against.reset_index(drop=True)
        net_rtg = _series_or_none(ordered_log, "net_rtg", "NET RTG")

        recent_margins = (
            margin_series.tail(5)
            if margin_series is not None
            else pd.Series(dtype=float)
        )
        recent_net = net_rtg.tail(5) if net_rtg is not None else pd.Series(dtype=float)
        summary["game_log_summary"] = {
            "avg_points_for": round(float(points_for.mean()), 3) if not points_for.empty else None,
            "avg_points_against": round(float(points_against.mean()), 3) if not points_against.empty else None,
            "avg_margin": (
                round(float(margin_series.mean()), 3)
                if margin_series is not None and not margin_series.empty
                else None
            ),
            "recent_avg_margin": (
                round(float(recent_margins.mean()), 3)
                if not recent_margins.empty and not math.isnan(float(recent_margins.mean()))
                else None
            ),
            "avg_net_rtg": (
                round(float(net_rtg.mean()), 3)
                if net_rtg is not None and not net_rtg.empty
                else None
            ),
            "recent_avg_net_rtg": (
                round(float(recent_net.mean()), 3)
                if not recent_net.empty and not math.isnan(float(recent_net.mean()))
                else None
            ),
        }

    return summary


def _top_minutes_share(all_stats: pd.DataFrame, n: int) -> float | None:
    minutes_col = "MPG" if "MPG" in all_stats.columns else "MP_PerGame"
    if minutes_col not in all_stats.columns:
        return None
    minutes = pd.to_numeric(all_stats[minutes_col], errors="coerce").fillna(0.0).clip(lower=0.0)
    total = float(minutes.sum())
    if total <= 0:
        return None
    return round(float(minutes.sort_values(ascending=False).head(n).sum() / total), 3)
