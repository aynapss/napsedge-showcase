"""
Framework-independent Rankings runtime configuration (Phase 7, Slice 2A
extraction).

Before this slice, `NAPSEDGE_NET_RANKINGS_SEASON`/`NAPSEDGE_NET_RANKINGS_CACHE_PATH`
parsing existed only inside `src.api.deps.get_rankings_context`, a module
that imports `fastapi` at the top level -- unusable from the Discord bridge,
which must not import any web framework. Rather than re-implementing (and
therefore duplicating) this same environment-variable parsing a second
time, it is extracted here, into a module with no framework import of any
kind, and `src.api.deps.get_rankings_context` now delegates to it. This
changes no behavior, no environment variable name, and no exception type:
`src/api/deps.py`'s own `RankingsContext`/`get_rankings_context` keep their
exact prior signatures and semantics, confirmed by the unchanged `tests/api`
suite.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from src.exceptions import RankingsConfigurationError
from src.rankings.cache_repository import DEFAULT_CACHE_PATH, NetRankingCacheRepository

#: Unchanged from the original `src.api.deps` constants -- same names, same meaning.
RANKINGS_CACHE_PATH_ENV_VAR = "NAPSEDGE_NET_RANKINGS_CACHE_PATH"
RANKINGS_SEASON_ENV_VAR = "NAPSEDGE_NET_RANKINGS_SEASON"


@dataclass(frozen=True)
class RankingsRuntimeConfig:
    """A resolved, ready-to-use Rankings repository plus its asserted season -- never caller-supplied, mirroring `RankingsContext`'s own prior contract."""

    repository: NetRankingCacheRepository
    season: int


def load_rankings_runtime_config_from_env() -> RankingsRuntimeConfig:
    """
    Reads `NAPSEDGE_NET_RANKINGS_SEASON` (required, must be a positive
    integer) and the optional `NAPSEDGE_NET_RANKINGS_CACHE_PATH` override
    (defaults to `src.rankings.cache_repository.DEFAULT_CACHE_PATH`) fresh
    on every call -- never at import time, never cached on a module-level
    global. Fails closed with `RankingsConfigurationError` on a
    missing/malformed season -- never a fabricated default. Byte-for-byte
    the same resolution logic `src.api.deps.get_rankings_context` used
    before this extraction.
    """
    raw_season = os.environ.get(RANKINGS_SEASON_ENV_VAR)
    if raw_season is None or not raw_season.strip():
        raise RankingsConfigurationError(f"{RANKINGS_SEASON_ENV_VAR} is not set.")
    try:
        season = int(raw_season)
    except ValueError:
        raise RankingsConfigurationError(f"{RANKINGS_SEASON_ENV_VAR} is not a valid integer.")
    if season <= 0:
        raise RankingsConfigurationError(f"{RANKINGS_SEASON_ENV_VAR} must be a positive integer.")

    cache_path = os.environ.get(RANKINGS_CACHE_PATH_ENV_VAR) or str(DEFAULT_CACHE_PATH)
    return RankingsRuntimeConfig(repository=NetRankingCacheRepository(cache_path), season=season)
