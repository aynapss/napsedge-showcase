"""
Phase 7 Slice 1: tests for `src.consumers.injury.present_team_injury` --
uses only synthetic `TeamInjuryLookupResult`/`TeamInjuryReport`/
`InjuryEntry` instances, never the real local injury cache or
`src.services.injury`/`src.features.team_summary` themselves.
"""
from __future__ import annotations

import ast
import inspect

import src.consumers.injury as injury_adapter_module
from src.consumers.injury import present_team_injury
from src.services.contracts import InjuryEntry, TeamInjuryLookupResult, TeamInjuryReport


def _loaded_report(injuries=(), impactful_count=0):
    return TeamInjuryReport(
        outcome="loaded", team_slug="duke", source_path="/some/path", injuries=injuries, impactful_count=impactful_count
    )


def test_populated_report_produces_one_row_per_entry():
    entries = (
        InjuryEntry(player="A", status="out", detail="x", raw={"extra": 1}),
        InjuryEntry(player="B", status="questionable", detail=None, raw={}),
    )
    result = TeamInjuryLookupResult(outcome="found", report=_loaded_report(entries, impactful_count=1))
    presentation = present_team_injury(result)
    assert presentation.status == "ok"
    assert len(presentation.rows) == 2


def test_known_empty_report_is_a_successful_presentation():
    result = TeamInjuryLookupResult(outcome="found", report=_loaded_report((), impactful_count=0))
    presentation = present_team_injury(result)
    assert presentation.status == "ok"
    assert presentation.rows == ()
    assert presentation.summary is not None


def test_unavailable_report_is_never_conflated_with_known_empty():
    not_loaded = TeamInjuryReport(
        outcome="not_loaded", team_slug="duke", source_path=None, injuries=(), impactful_count=0, detail="missing file"
    )
    result = TeamInjuryLookupResult(outcome="found", report=not_loaded)
    presentation = present_team_injury(result)
    assert presentation.status == "unavailable"
    assert presentation.status != "ok"


def test_known_empty_and_unavailable_are_structurally_different():
    empty_presentation = present_team_injury(TeamInjuryLookupResult(outcome="found", report=_loaded_report((), 0)))
    unavailable_report = TeamInjuryReport(
        outcome="not_loaded", team_slug="duke", source_path=None, injuries=(), impactful_count=0, detail="missing"
    )
    unavailable_presentation = present_team_injury(TeamInjuryLookupResult(outcome="found", report=unavailable_report))
    assert empty_presentation.status != unavailable_presentation.status
    assert empty_presentation != unavailable_presentation


def test_impactful_count_is_preserved_unchanged():
    result = TeamInjuryLookupResult(
        outcome="found",
        report=_loaded_report((InjuryEntry(player="A", status="out", detail=None, raw={}),), impactful_count=1),
    )
    presentation = present_team_injury(result)
    count_field = next(f for f in presentation.fields if f.label == "Impactful Count")
    assert count_field.value == "1"


def test_ordering_is_preserved_exactly_as_supplied():
    entries = (
        InjuryEntry(player="Zed", status="out", detail=None, raw={}),
        InjuryEntry(player="Amy", status="out", detail=None, raw={}),
    )
    result = TeamInjuryLookupResult(outcome="found", report=_loaded_report(entries, impactful_count=2))
    presentation = present_team_injury(result)
    players_shown = [next(f.value for f in row if f.label == "Player") for row in presentation.rows]
    assert players_shown == ["Zed", "Amy"]  # not alphabetized or otherwise reordered


def test_raw_source_path_and_team_slug_fields_are_excluded():
    entries = (InjuryEntry(player="A", status="out", detail="d", raw={"secret_internal_key": "value"}),)
    result = TeamInjuryLookupResult(outcome="found", report=_loaded_report(entries, impactful_count=1))
    presentation = present_team_injury(result)
    rendered = repr(presentation)
    assert "secret_internal_key" not in rendered  # the raw dict's own contents never leak in
    assert "/some/path" not in rendered  # source_path never leaks in
    labels = {f.label for f in presentation.fields} | {f.label for row in presentation.rows for f in row}
    assert not {"Team Slug", "Source Path", "Raw"} & labels  # never surfaced as a label at all


def test_team_not_found_is_an_explicit_not_found_presentation():
    presentation = present_team_injury(TeamInjuryLookupResult(outcome="team_not_found"))
    assert presentation.status == "not_found"
    assert presentation.message is not None


def test_adapter_makes_no_cache_or_filesystem_access():
    source = inspect.getsource(injury_adapter_module)
    forbidden = ["build_injury_report(", "get_team_injury_report(", "get_injury_report_for_team(", "open(", "read_text", "CBB_DIR"]
    for token in forbidden:
        assert token not in source, f"unexpected {token!r} found in src/consumers/injury.py"


def test_module_imports_no_filesystem_database_or_framework_library():
    tree = ast.parse(inspect.getsource(injury_adapter_module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = ("sqlalchemy", "src.db", "src.features", "src.services.injury", "discord", "streamlit", "fastapi", "starlette", "pydantic")
    for name in imported:
        assert not any(f in name for f in forbidden), f"unexpected import {name!r} in src/consumers/injury.py"
