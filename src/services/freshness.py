"""
Phase 9E-3 (readiness section 10, "cache age/freshness"): how old the locally cached data is, for the OPERATOR.

`data_freshness()` reports, without any file path or content:
- `rankings`: the NET rankings cache (NAPSEDGE_NET_RANKINGS_CACHE_PATH, else the default cache) -- present or not, its last
  modification time (UTC) and age in hours;
- `injuries`: the per-team injury cache files (`<CBB>/<team>/<team>_injuries.json` and `<CBB>/../injury_cache/*_injuries.json`,
  the two locations the injury authority reads) -- how many exist, and the oldest and newest modification times and ages;
- `stale`: every dataset that is missing or whose (newest) age exceeds NAPSEDGE_DATA_MAX_AGE_HOURS (default 48, bounds 1-720);
- `status`: `ok` or `stale`.

Exposed as the operator command `python -m src.deployment.entrypoints data-freshness` (exit 0 ok, 10 stale, 64 a malformed
setting) -- deliberately NOT as an API route: the API's approved route set is unchanged. Modification time is the file system's
record of when the cache was last written; it is not a claim about when the upstream source last changed.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

FRESHNESS_FORMAT = "napsedge.data-freshness/1"
MAX_AGE_ENV_VAR = "NAPSEDGE_DATA_MAX_AGE_HOURS"
MAX_AGE_BOUNDS = (1, 720, 48)


class FreshnessConfigurationError(ValueError):
    """NAPSEDGE_DATA_MAX_AGE_HOURS is not a whole number within its bounds (the value itself is never echoed)."""


def max_age_hours(environ: dict | None = None) -> int:
    env = os.environ if environ is None else environ
    raw = (env.get(MAX_AGE_ENV_VAR) or "").strip()
    low, high, default = MAX_AGE_BOUNDS
    if not raw:
        return default
    if not raw.isdigit() or not low <= int(raw) <= high:
        raise FreshnessConfigurationError(f"{MAX_AGE_ENV_VAR} must be a whole number of hours from {low} to {high}")
    return int(raw)


def _stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_hours(ts: float, now: datetime) -> float:
    return round(max(0.0, now.timestamp() - ts) / 3600.0, 2)


def _default_rankings_path() -> Path:
    from src.rankings.cache_repository import DEFAULT_CACHE_PATH
    from src.rankings.config import RANKINGS_CACHE_PATH_ENV_VAR

    return Path(os.environ.get(RANKINGS_CACHE_PATH_ENV_VAR) or str(DEFAULT_CACHE_PATH))


def _default_injury_files() -> list[Path]:
    from src.paths import CBB_DIR

    found = list(CBB_DIR.glob("*/*_injuries.json")) if CBB_DIR.is_dir() else []
    cache = CBB_DIR.parent / "injury_cache"
    found += list(cache.glob("*_injuries.json")) if cache.is_dir() else []
    return found


def data_freshness(*, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), rankings_path: Path | None = None,
                   injury_files: Iterable[Path] | None = None, environ: dict | None = None) -> dict:
    current = now()
    limit = max_age_hours(environ)
    stale: list[str] = []
    path = rankings_path if rankings_path is not None else _default_rankings_path()
    if path.is_file():
        mtime = path.stat().st_mtime
        rankings = {"present": True, "as_of": _stamp(mtime), "age_hours": _age_hours(mtime, current)}
        if rankings["age_hours"] > limit:
            stale.append("rankings")
    else:
        rankings = {"present": False}
        stale.append("rankings")
    times = sorted(p.stat().st_mtime for p in (injury_files if injury_files is not None else _default_injury_files()) if p.is_file())
    if times:
        injuries = {"files": len(times), "oldest_as_of": _stamp(times[0]), "oldest_age_hours": _age_hours(times[0], current),
                    "newest_as_of": _stamp(times[-1]), "newest_age_hours": _age_hours(times[-1], current)}
        if injuries["newest_age_hours"] > limit:
            stale.append("injuries")
    else:
        injuries = {"files": 0}
        stale.append("injuries")
    return {"format": FRESHNESS_FORMAT, "checked_utc": current.strftime("%Y-%m-%dT%H:%M:%SZ"), "max_age_hours": limit,
            "rankings": rankings, "injuries": injuries, "stale": stale, "status": "stale" if stale else "ok",
            "note": "file modification times of the local caches; not a claim about the upstream source"}


__all__ = ["FRESHNESS_FORMAT", "MAX_AGE_ENV_VAR", "FreshnessConfigurationError", "data_freshness", "max_age_hours"]
