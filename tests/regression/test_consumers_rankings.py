"""
Phase 7 Slice 1: tests for `src.consumers.rankings.present_team_ranking`
-- uses only synthetic `RankingLookupResult`/`RankingRecord` instances,
never the real local cache or `src.services.rankings`/`src.rankings`
themselves.
"""
from __future__ import annotations

import ast
import inspect

import src.consumers.rankings as rankings_adapter_module
from src.consumers.rankings import present_team_ranking
from src.services.contracts import RankingLookupResult, RankingRecord


def test_found_ranking_produces_successful_presentation():
    result = RankingLookupResult(
        outcome="found", record=RankingRecord(team_id=1, net_rank=5, raw_team_key="duke", season=2026)
    )
    presentation = present_team_ranking(result)
    assert presentation.status == "ok"
    assert any(f.label == "NET Rank" and f.value == "5" for f in presentation.fields)


def test_not_found_produces_explicit_not_found_presentation():
    presentation = present_team_ranking(RankingLookupResult(outcome="not_found"))
    assert presentation.status == "not_found"
    assert presentation.message is not None


def test_conflicting_ranks_are_all_preserved_as_separate_rows():
    result = RankingLookupResult(
        outcome="conflicting",
        conflicting_records=(
            RankingRecord(team_id=2, net_rank=77, raw_team_key="louisiana-state", season=2026),
            RankingRecord(team_id=2, net_rank=245, raw_team_key="louisiana-state", season=2026),
        ),
    )
    presentation = present_team_ranking(result)
    assert presentation.status == "conflict"
    assert len(presentation.rows) == 2
    ranks_shown = {next(f.value for f in row if f.label == "NET Rank") for row in presentation.rows}
    assert ranks_shown == {"77", "245"}


def test_no_conflicting_rank_is_silently_selected():
    """The presentation must never carry a single top-level 'NET Rank' field for a conflicting outcome -- only rows."""
    result = RankingLookupResult(
        outcome="conflicting",
        conflicting_records=(
            RankingRecord(team_id=2, net_rank=77, raw_team_key="louisiana-state", season=2026),
            RankingRecord(team_id=2, net_rank=245, raw_team_key="louisiana-state", season=2026),
        ),
    )
    presentation = present_team_ranking(result)
    assert presentation.fields == ()
    assert len(presentation.warnings) >= 1


def test_cache_empty_produces_explicit_unavailable_presentation():
    presentation = present_team_ranking(RankingLookupResult(outcome="cache_empty"))
    assert presentation.status == "unavailable"
    assert presentation.message is not None


def test_no_fabricated_freshness_field_exists_anywhere():
    result = RankingLookupResult(
        outcome="found", record=RankingRecord(team_id=1, net_rank=5, raw_team_key="duke", season=2026)
    )
    presentation = present_team_ranking(result)
    labels = {f.label for f in presentation.fields}
    assert not any("fresh" in label.lower() or "as of" in label.lower() or "updated" in label.lower() for label in labels)
    # The truthful substitute is a static disclaimer note, never a fabricated date.
    assert presentation.notes and "snapshot" in presentation.notes[0].lower()


def test_season_is_preserved_exactly_as_supplied():
    result = RankingLookupResult(
        outcome="found", record=RankingRecord(team_id=1, net_rank=5, raw_team_key="duke", season=1999)
    )
    presentation = present_team_ranking(result)
    season_field = next(f for f in presentation.fields if f.label == "Season")
    assert season_field.value == "1999"


def test_rank_state_and_identity_state_remain_independent():
    """A found result's team_id/raw_team_key (identity-facing) and net_rank (rank-validity-facing) are separate fields, never merged into one derived value."""
    result = RankingLookupResult(
        outcome="found", record=RankingRecord(team_id=7, net_rank=12, raw_team_key="unc", season=2026)
    )
    presentation = present_team_ranking(result)
    labels = [f.label for f in presentation.fields]
    assert "Team ID" in labels and "NET Rank" in labels and "Source Key" in labels
    assert len(set(labels)) == len(labels)  # no duplicate/merged field


def test_adapter_makes_no_cache_or_identity_access():
    source = inspect.getsource(rankings_adapter_module)
    forbidden = ["NetRankingCacheRepository(", "list_rankings(", "get_team_ranking(", "open(", "read_csv", "resolve_team_identity"]
    for token in forbidden:
        assert token not in source, f"unexpected {token!r} found in src/consumers/rankings.py"


def test_module_imports_no_cache_database_or_framework_library():
    tree = ast.parse(inspect.getsource(rankings_adapter_module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = ("sqlalchemy", "src.db", "src.rankings", "src.identity", "discord", "streamlit", "fastapi", "starlette", "pydantic")
    for name in imported:
        assert not any(f in name for f in forbidden), f"unexpected import {name!r} in src/consumers/rankings.py"
