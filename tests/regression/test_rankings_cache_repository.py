"""
Phase 6, Slice 5A: unit tests for `src.rankings.cache_repository` and
`src.services.rankings` -- the read-only, local-cache-only NET-rankings
service. Every test uses a temporary synthetic CSV file except the small
number of dedicated real-cache parity tests, clearly marked as such.
Never modifies the real `Reference Py Files/net_rankings_2026.csv` file.
"""
from __future__ import annotations

import ast
import inspect

import pytest

from src.exceptions import RankingsCacheIntegrityError, SourceUnavailableError, ValidationError
from src.rankings import cache_repository as cache_repository_module
from src.rankings.cache_repository import DEFAULT_CACHE_PATH, NetRankingCacheRepository
from src.services import rankings as rankings_service_module
from src.services.contracts import RankingLookupResult, RankingRecord
from src.services.rankings import get_team_ranking, list_rankings


def _write_csv(tmp_path, rows: list[tuple[str, object]], *, filename: str = "rankings.csv"):
    path = tmp_path / filename
    lines = ["team_slug,net_rank"] + [f"{slug},{rank}" for slug, rank in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


DUKE_TEAM_ID = 848850  # real, stable canonical team_id (SHA-256 of Duke's SR slug) -- resolved via the existing identity registry, never guessed


# ---------------------------------------------------------------------------
# Cache repository -- basic read behavior
# ---------------------------------------------------------------------------


def test_valid_single_row_cache(tmp_path):
    path = _write_csv(tmp_path, [("duke", 1)])
    repo = NetRankingCacheRepository(path)
    result = repo.load(season=2026)
    assert len(result.records) == 1
    assert result.records[0].raw_team_key == "duke"
    assert result.records[0].validated_rank == 1


def test_valid_multiple_row_cache(tmp_path):
    path = _write_csv(tmp_path, [("duke", 1), ("michigan", 2), ("arizona", 3)])
    repo = NetRankingCacheRepository(path)
    result = repo.load(season=2026)
    assert len(result.records) == 3


def test_missing_cache_file_raises_source_unavailable_error(tmp_path):
    repo = NetRankingCacheRepository(tmp_path / "does_not_exist.csv")
    with pytest.raises(SourceUnavailableError):
        repo.load(season=2026)


def test_empty_cache_file_with_headers_only(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("team_slug,net_rank\n", encoding="utf-8")
    repo = NetRankingCacheRepository(path)
    result = repo.load(season=2026)
    assert result.records == []


def test_missing_required_headers_raises_validation_error(tmp_path):
    path = tmp_path / "bad_headers.csv"
    path.write_text("school,rank\nduke,1\n", encoding="utf-8")
    repo = NetRankingCacheRepository(path)
    with pytest.raises(ValidationError):
        repo.load(season=2026)


def test_malformed_rank_is_recorded_as_invalid_not_fabricated(tmp_path):
    path = _write_csv(tmp_path, [("duke", "not-a-number")])
    repo = NetRankingCacheRepository(path)
    result = repo.load(season=2026)
    assert result.records[0].validated_rank is None
    assert result.records[0].resolution_status == "invalid_rank"


def test_non_positive_rank_is_recorded_as_invalid(tmp_path):
    path = _write_csv(tmp_path, [("duke", 0), ("michigan", -5)])
    repo = NetRankingCacheRepository(path)
    result = repo.load(season=2026)
    assert all(r.validated_rank is None for r in result.records)


def test_repository_never_mutates_the_cache_file(tmp_path):
    path = _write_csv(tmp_path, [("duke", 1)])
    before_bytes = path.read_bytes()
    before_mtime = path.stat().st_mtime
    repo = NetRankingCacheRepository(path)
    repo.load(season=2026)
    repo.load(season=2026)
    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime == before_mtime


def test_deterministic_repeated_reads(tmp_path):
    path = _write_csv(tmp_path, [("duke", 1), ("michigan", 2)])
    repo = NetRankingCacheRepository(path)
    first = repo.load(season=2026)
    second = repo.load(season=2026)
    assert [(r.raw_team_key, r.validated_rank) for r in first.records] == [(r.raw_team_key, r.validated_rank) for r in second.records]


def test_deterministic_ordering_preserves_source_row_order(tmp_path):
    path = _write_csv(tmp_path, [("michigan", 2), ("duke", 1), ("arizona", 3)])
    repo = NetRankingCacheRepository(path)
    result = repo.load(season=2026)
    assert [r.raw_team_key for r in result.records] == ["michigan", "duke", "arizona"]


# ---------------------------------------------------------------------------
# No module-import-time file read; no network
# ---------------------------------------------------------------------------


def test_no_cache_file_read_at_module_import(tmp_path, monkeypatch):
    """Importing the module must never touch DEFAULT_CACHE_PATH -- only constructing+calling a repository does."""
    import importlib

    calls = []
    original_exists = cache_repository_module.Path.exists

    def spy_exists(self):
        calls.append(self)
        return original_exists(self)

    monkeypatch.setattr(cache_repository_module.Path, "exists", spy_exists)
    importlib.reload(cache_repository_module)
    assert calls == [], "reloading the module must not check the cache path's existence"


def test_default_cache_path_points_at_the_real_repo_location():
    assert DEFAULT_CACHE_PATH.name == "net_rankings_2026.csv"
    assert DEFAULT_CACHE_PATH.parent.name == "Reference Py Files"


def test_cache_repository_module_imports_no_network_capable_function():
    """AST-level check: no requests/httpx/urllib/selenium import, no build_net_rankings_from_cbs, anywhere in this module."""
    tree = ast.parse(inspect.getsource(cache_repository_module))
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
            imported_names.update(a.name for a in node.names)

    forbidden = ("requests", "httpx", "urllib", "selenium", "build_net_rankings_from_cbs", "build_net_rankings_from_ncaa_v15")
    for name in imported_names:
        assert not any(f in name for f in forbidden), f"unexpected network-capable import {name!r}"


def test_rankings_service_module_imports_no_fastapi_pydantic_or_network_function():
    tree = ast.parse(inspect.getsource(rankings_service_module))
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
            imported_names.update(a.name for a in node.names)

    forbidden = ("fastapi", "starlette", "pydantic", "requests", "httpx", "urllib", "selenium")
    for name in imported_names:
        assert not any(f in name for f in forbidden), f"unexpected import {name!r} in src/services/rankings.py"


def test_rankings_service_succeeds_with_outbound_network_functions_disabled(tmp_path, monkeypatch):
    """Dynamic proof, not just AST: disable every plausible outbound network function and confirm the service still works."""
    import socket

    def _network_disabled(*args, **kwargs):
        raise AssertionError("network access attempted during a Rankings service call")

    monkeypatch.setattr(socket, "socket", _network_disabled)
    try:
        import requests

        monkeypatch.setattr(requests, "get", _network_disabled, raising=False)
        monkeypatch.setattr(requests, "post", _network_disabled, raising=False)
    except ImportError:
        pass

    path = _write_csv(tmp_path, [("duke", 1)])
    repo = NetRankingCacheRepository(path)
    result = get_team_ranking(repo, DUKE_TEAM_ID, season=2026)
    assert result.outcome == "found"


# ---------------------------------------------------------------------------
# get_team_ranking -- outcome classification
# ---------------------------------------------------------------------------


def test_successful_team_lookup(tmp_path):
    path = _write_csv(tmp_path, [("duke", 1)])
    repo = NetRankingCacheRepository(path)
    result = get_team_ranking(repo, DUKE_TEAM_ID, season=2026)
    assert result == RankingLookupResult(
        outcome="found",
        record=RankingRecord(team_id=DUKE_TEAM_ID, net_rank=1, raw_team_key="duke", season=2026),
    )


def test_team_absent_from_a_valid_cache(tmp_path):
    path = _write_csv(tmp_path, [("duke", 1)])
    repo = NetRankingCacheRepository(path)
    result = get_team_ranking(repo, 1, season=2026)  # a team_id guaranteed absent from this synthetic cache
    assert result.outcome == "not_found"


def test_empty_cache_returns_cache_empty_not_not_found(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("team_slug,net_rank\n", encoding="utf-8")
    repo = NetRankingCacheRepository(path)
    result = get_team_ranking(repo, DUKE_TEAM_ID, season=2026)
    assert result.outcome == "cache_empty"
    assert result.record is None


def test_exact_duplicate_raises_cache_integrity_error(tmp_path):
    """Two rows, same raw key, same rank -- no existing dedup rule exists, so this must not be silently collapsed."""
    path = _write_csv(tmp_path, [("duke", 1), ("duke", 1)])
    repo = NetRankingCacheRepository(path)
    with pytest.raises(RankingsCacheIntegrityError):
        get_team_ranking(repo, DUKE_TEAM_ID, season=2026)


def test_conflicting_duplicate_returns_conflicting_outcome(tmp_path):
    """Two rows, same raw key, DIFFERENT ranks -- a real, representable condition, never silently resolved to one value."""
    path = _write_csv(tmp_path, [("duke", 1), ("duke", 50)])
    repo = NetRankingCacheRepository(path)
    result = get_team_ranking(repo, DUKE_TEAM_ID, season=2026)
    assert result.outcome == "conflicting"
    assert result.record is None
    assert "1" in result.detail and "50" in result.detail


def test_conflicting_outcome_preserves_every_contributing_record_structurally(tmp_path):
    """Phase 6, Slice 5B: conflicting_records must carry each raw row's own (raw_team_key, net_rank) pair -- not just a formatted string -- so an API layer can present every value without re-deriving them."""
    path = _write_csv(tmp_path, [("duke", 1), ("duke", 50)])
    repo = NetRankingCacheRepository(path)
    result = get_team_ranking(repo, DUKE_TEAM_ID, season=2026)
    assert len(result.conflicting_records) == 2
    ranks = {r.net_rank for r in result.conflicting_records}
    assert ranks == {1, 50}
    assert all(r.raw_team_key == "duke" for r in result.conflicting_records)
    assert all(r.team_id == DUKE_TEAM_ID for r in result.conflicting_records)


def test_found_outcome_has_empty_conflicting_records(tmp_path):
    path = _write_csv(tmp_path, [("duke", 1)])
    repo = NetRankingCacheRepository(path)
    result = get_team_ranking(repo, DUKE_TEAM_ID, season=2026)
    assert result.conflicting_records == ()


def test_unresolved_team_identity_elsewhere_does_not_block_a_valid_lookup(tmp_path):
    path = _write_csv(tmp_path, [("duke", 1), ("zzznotarealteam99", 5)])
    repo = NetRankingCacheRepository(path)
    result = get_team_ranking(repo, DUKE_TEAM_ID, season=2026)
    assert result.outcome == "found"
    assert result.record.net_rank == 1


def test_ambiguous_team_identity_elsewhere_does_not_block_a_valid_lookup(tmp_path):
    """"Miami" is a genuinely ambiguous raw key (Miami FL / Miami OH) -- its presence elsewhere in the cache must not affect Duke's own lookup."""
    path = _write_csv(tmp_path, [("duke", 1), ("Miami", 5)])
    repo = NetRankingCacheRepository(path)
    result = get_team_ranking(repo, DUKE_TEAM_ID, season=2026)
    assert result.outcome == "found"


def test_invalid_rank_row_elsewhere_does_not_block_a_valid_lookup(tmp_path):
    path = _write_csv(tmp_path, [("duke", 1), ("michigan", "garbage")])
    repo = NetRankingCacheRepository(path)
    result = get_team_ranking(repo, DUKE_TEAM_ID, season=2026)
    assert result.outcome == "found"


# ---------------------------------------------------------------------------
# Field/value fidelity -- no fabrication
# ---------------------------------------------------------------------------


def test_season_is_preserved_exactly_as_the_caller_asserted(tmp_path):
    path = _write_csv(tmp_path, [("duke", 1)])
    repo = NetRankingCacheRepository(path)
    result = get_team_ranking(repo, DUKE_TEAM_ID, season=2019)
    assert result.record.season == 2019


def test_raw_team_key_source_field_is_preserved(tmp_path):
    path = _write_csv(tmp_path, [("duke", 1)])
    repo = NetRankingCacheRepository(path)
    result = get_team_ranking(repo, DUKE_TEAM_ID, season=2026)
    assert result.record.raw_team_key == "duke"


def test_no_fabricated_default_rank_for_malformed_input(tmp_path):
    path = _write_csv(tmp_path, [("duke", "abc")])
    repo = NetRankingCacheRepository(path)
    result = get_team_ranking(repo, DUKE_TEAM_ID, season=2026)
    assert result.outcome == "not_found"  # the malformed row never resolves to a usable rank, so it cannot "find" the team
    assert result.record is None


def test_ranking_record_has_no_fabricated_as_of_or_current_flag():
    """Structural guard: RankingRecord must not carry an as_of/is_current field the cache cannot truthfully support."""
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(RankingRecord)}
    assert "as_of" not in field_names
    assert "is_current" not in field_names
    assert "freshness" not in field_names


def test_existing_team_identity_authority_is_reused_not_duplicated():
    """Static guard: src/services/rankings.py must not define its own normalize/crosswalk function."""
    source = inspect.getsource(rankings_service_module)
    forbidden = ["def normalize_team", "def resolve_team", "TEAM_NAME_CORRECTIONS", "get_close_matches", "difflib"]
    for token in forbidden:
        assert token not in source, f"unexpected {token!r} found in src/services/rankings.py"


def test_no_first_match_or_fuzzy_identity_fallback_in_the_service():
    source = inspect.getsource(rankings_service_module)
    assert "get_close_matches" not in source
    assert ".lower()" not in source  # no ad hoc local normalization at all


# ---------------------------------------------------------------------------
# Real-cache parity (read-only, small, representative) -- does not assert
# stale docstring counts; asserts the mechanism for two named, currently-
# confirmed real rows.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_real_cache_duke_resolves_found_with_rank_one():
    repo = NetRankingCacheRepository()  # the real DEFAULT_CACHE_PATH -- read-only
    result = get_team_ranking(repo, DUKE_TEAM_ID, season=2026)
    assert result.outcome == "found"
    assert result.record.net_rank == 1
    assert result.record.raw_team_key == "duke"


@pytest.mark.integration
def test_real_cache_louisiana_state_is_a_confirmed_real_conflicting_row():
    """Real, currently-confirmed data-quality defect in the cached fixture itself (not fabricated for this test): "louisiana-state" appears twice with ranks 77 and 245."""
    from src.identity.net_ranking_identity import resolve_net_team_identity

    lsu_identity = resolve_net_team_identity("louisiana-state")
    assert hasattr(lsu_identity, "team_id")  # must resolve cleanly at the identity layer

    repo = NetRankingCacheRepository()
    result = get_team_ranking(repo, lsu_identity.team_id, season=2026)
    assert result.outcome == "conflicting"
    assert "77" in result.detail and "245" in result.detail
