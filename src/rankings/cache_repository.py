"""
Framework-independent NET-rankings cache repository (Phase 6, Slice 5A).

Owns exactly one responsibility: where the authoritative local rankings
data comes from and how it is read. Every parsing, schema-validation,
duplicate-detection, and team-identity-resolution decision is delegated
entirely to the existing, unmodified `src.ingestion.net_ranking_ingestion`
(which itself reuses `src.identity.net_ranking_identity`, which reuses the
canonical `src.identity.team_identity.resolve_team_identity`) -- this
module introduces no new normalizer, no new crosswalk, no fuzzy matching,
and no live network path of its own.

**Confirmed read-only, cache-only, no network path**: `load_and_ingest_net_rankings_csv`
calls only `pandas.read_csv` on a local file plus the existing pure
identity-resolution chain -- no `requests`/`httpx`/`urllib` import, no
Selenium, no live CBS/NCAA URL, anywhere in that call chain (verified by
AST inspection; see `tests/regression/test_rankings_cache_repository.py`
for the corresponding regression test that proves this with outbound
network functions monkeypatched to raise).
"""
from __future__ import annotations

from pathlib import Path

from src.exceptions import SourceUnavailableError
from src.ingestion.net_ranking_ingestion import NetRankingIngestionResult, load_and_ingest_net_rankings_csv
from src.paths import ROOT_DIR

#: The one authoritative local cache this repository reads -- a static,
#: pre-compiled CSV fixture (`team_slug,net_rank` columns; 365 real rows
#: as of this batch). Never written, refreshed, or mutated by this module.
DEFAULT_CACHE_PATH = ROOT_DIR / "Reference Py Files" / "net_rankings_2026.csv"


class NetRankingCacheRepository:
    """
    Read-only. Constructing an instance never reads the cache file --
    `load()` does, fresh, on every call. No result is cached in a mutable
    module-level global, so two independently constructed instances
    (e.g. one per test) never share or leak state.
    """

    def __init__(self, cache_path: str | Path = DEFAULT_CACHE_PATH):
        self._cache_path = Path(cache_path)

    def load(self, *, season: int) -> NetRankingIngestionResult:
        """
        Reads and ingests the configured cache path. Raises
        `SourceUnavailableError` if the configured path does not exist --
        never a silently empty result -- since `src.exceptions.SourceUnavailableError`'s
        own docstring already names NET rankings as a source this
        exception covers when "no cached fallback was available".
        `season` is a caller-asserted label (the cache file itself carries
        no season/freshness column) -- passed straight through to the
        existing `load_and_ingest_net_rankings_csv`, never derived from
        file modification time or fabricated.
        """
        if not self._cache_path.exists():
            raise SourceUnavailableError(
                "NET rankings cache is not available at the configured path -- no cached fallback found."
            )
        return load_and_ingest_net_rankings_csv(str(self._cache_path), season=season)
