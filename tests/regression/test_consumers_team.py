"""
Phase 7 Slice 1: tests for `src.consumers.team.present_team_lookup` --
uses only synthetic `TeamLookupResult`/`TeamRecord` instances, never a
database session, a repository, or `src.services.team` itself.
"""
from __future__ import annotations

import ast
import inspect

import src.consumers.team as team_adapter_module
from src.consumers.team import present_team_lookup
from src.services.contracts import TeamLookupResult, TeamRecord


def test_found_team_produces_successful_presentation():
    result = TeamLookupResult(
        outcome="found",
        team=TeamRecord(team_id=42, canonical_name="Duke", canonical_slug="duke", conference_name="ACC"),
    )
    presentation = present_team_lookup(result)
    assert presentation.status == "ok"
    assert presentation.title == "Duke"
    assert presentation.message is None


def test_not_found_produces_explicit_not_found_presentation():
    presentation = present_team_lookup(TeamLookupResult(outcome="not_found"))
    assert presentation.status == "not_found"
    assert presentation.message is not None
    assert presentation.fields == ()


def test_missing_conference_remains_none_not_fabricated():
    result = TeamLookupResult(
        outcome="found",
        team=TeamRecord(team_id=1, canonical_name="Some School", canonical_slug="some-school", conference_name=None),
    )
    presentation = present_team_lookup(result)
    conference_field = next(f for f in presentation.fields if f.label == "Conference")
    assert conference_field.value is None


def test_field_ordering_is_stable_and_deterministic():
    result = TeamLookupResult(
        outcome="found",
        team=TeamRecord(team_id=1, canonical_name="Duke", canonical_slug="duke", conference_name="ACC"),
    )
    labels = [f.label for f in present_team_lookup(result).fields]
    assert labels == ["Team", "Slug", "Conference", "Team ID"]
    # Repeated calls produce the identical ordering -- no set/dict iteration involved.
    assert [f.label for f in present_team_lookup(result).fields] == labels


def test_adapter_makes_no_service_or_repository_call():
    """Static guard: no reference to get_team/resolve_team/TeamRepository/Session anywhere in this module."""
    source = inspect.getsource(team_adapter_module)
    forbidden = ["get_team(", "resolve_team(", "TeamRepository(", "Session", "select(", "SELECT "]
    for token in forbidden:
        assert token not in source, f"unexpected {token!r} found in src/consumers/team.py"


def test_no_internal_identity_field_leaks_into_the_presentation():
    """TeamRecord carries exactly 4 fields -- confirms the adapter does not smuggle in an internal identity key the contract itself never exposed."""
    result = TeamLookupResult(
        outcome="found",
        team=TeamRecord(team_id=1, canonical_name="Duke", canonical_slug="duke", conference_name="ACC"),
    )
    presentation = present_team_lookup(result)
    labels = {f.label for f in presentation.fields}
    assert labels == {"Team", "Slug", "Conference", "Team ID"}


def test_module_imports_no_database_identity_or_framework_library():
    tree = ast.parse(inspect.getsource(team_adapter_module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = ("sqlalchemy", "src.db", "src.identity", "discord", "streamlit", "fastapi", "starlette", "pydantic")
    for name in imported:
        assert not any(f in name for f in forbidden), f"unexpected import {name!r} in src/consumers/team.py"
