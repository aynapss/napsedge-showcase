"""
Phase 7 Slice 1: tests for `src.consumers.errors.present_error`.
"""
from __future__ import annotations

import ast
import inspect

import src.consumers.errors as errors_module
from src.consumers.errors import present_error
from src.exceptions import (
    IdentityConflictError,
    InjuryDataIntegrityError,
    RankingsCacheIntegrityError,
    RankingsConfigurationError,
)


def test_identity_conflict_maps_to_conflict_status_with_its_own_message():
    exc = IdentityConflictError("team query 'Miami' is ambiguous between: Miami (FL), Miami (OH)")
    presentation = present_error(exc)
    assert presentation.status == "conflict"
    assert "Miami (FL)" in presentation.message


def test_rankings_configuration_error_maps_to_sanitized_unavailable():
    presentation = present_error(RankingsConfigurationError("NAPSEDGE_NET_RANKINGS_SEASON is not set."))
    assert presentation.status == "unavailable"
    assert "NAPSEDGE_NET_RANKINGS_SEASON" not in presentation.message


def test_rankings_cache_integrity_error_maps_to_sanitized_unavailable():
    presentation = present_error(RankingsCacheIntegrityError("duplicate row for team_id=1"))
    assert presentation.status == "unavailable"
    assert "team_id=1" not in presentation.message


def test_injury_data_integrity_error_maps_to_sanitized_unavailable():
    presentation = present_error(InjuryDataIntegrityError("malformed element at index 3"))
    assert presentation.status == "unavailable"
    assert "index 3" not in presentation.message


def test_unknown_exception_maps_to_generic_sanitized_error():
    presentation = present_error(RuntimeError("connection string: postgresql://user:pass@host/db"))
    assert presentation.status == "error"
    assert "postgresql" not in presentation.message
    assert "user:pass" not in presentation.message


def test_every_approved_exception_type_maps_deterministically():
    exceptions = [
        IdentityConflictError("x is ambiguous between: A, B"),
        RankingsConfigurationError("bad config"),
        RankingsCacheIntegrityError("bad cache"),
        InjuryDataIntegrityError("bad injury data"),
        RuntimeError("unknown"),
    ]
    for exc in exceptions:
        assert present_error(exc) is not None
        assert present_error(exc).status in ("conflict", "unavailable", "error")


def test_sensitive_exception_text_never_appears_for_sanitized_types():
    sensitive_messages = [
        "password=hunter2",
        "C:\\Users\\Anthony\\secret\\path.json",
        "sk_live_abcdef1234567890",
    ]
    for msg in sensitive_messages:
        presentation = present_error(RankingsConfigurationError(msg))
        assert msg not in presentation.message
        presentation = present_error(RuntimeError(msg))
        assert msg not in presentation.message


def test_repeated_mapping_of_the_same_exception_produces_identical_results():
    exc = RankingsConfigurationError("bad config")
    assert present_error(exc) == present_error(exc)


def test_module_imports_no_framework_or_api_layer():
    """Static guard: must not import src.api.* (the mapper reuses the same policy, never the code)."""
    tree = ast.parse(inspect.getsource(errors_module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = ("src.api", "discord", "streamlit", "fastapi", "starlette", "pydantic")
    for name in imported:
        assert not any(f in name for f in forbidden), f"unexpected import {name!r} in src/consumers/errors.py"
