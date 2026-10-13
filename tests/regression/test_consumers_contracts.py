"""
Phase 7 Slice 1: tests for the shared, framework-independent presentation
contracts in `src.consumers.contracts` -- no Discord, no Streamlit, no
FastAPI/Starlette/Pydantic, no database/network/filesystem access, and no
module-level mutable state anywhere in the module under test.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect

import src.consumers.contracts as contracts_module
from src.consumers.contracts import ConsumerPresentation, PresentationField


def test_consumer_presentation_is_frozen():
    """Immutability: mutating an existing instance must raise, not silently succeed."""
    presentation = ConsumerPresentation(status="ok", title="Duke")
    try:
        presentation.title = "UNC"  # type: ignore[misc]
        assert False, "expected FrozenInstanceError"
    except dataclasses.FrozenInstanceError:
        pass


def test_presentation_field_is_frozen():
    field = PresentationField("Team", "Duke")
    try:
        field.value = "UNC"  # type: ignore[misc]
        assert False, "expected FrozenInstanceError"
    except dataclasses.FrozenInstanceError:
        pass


def test_deterministic_equality_between_separately_constructed_instances():
    a = ConsumerPresentation(status="ok", title="Duke", fields=(PresentationField("Slug", "duke"),))
    b = ConsumerPresentation(status="ok", title="Duke", fields=(PresentationField("Slug", "duke"),))
    assert a == b
    assert a is not b


def test_repeated_construction_from_identical_inputs_is_equal():
    def _build():
        return ConsumerPresentation(status="conflict", title="x", rows=((PresentationField("a", "1"),),))

    assert _build() == _build()


def test_module_imports_no_framework_or_persistence_library():
    """Static guard: no discord/streamlit/fastapi/starlette/pydantic/sqlalchemy import anywhere in this module."""
    tree = ast.parse(inspect.getsource(contracts_module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = ("discord", "streamlit", "fastapi", "starlette", "pydantic", "sqlalchemy")
    for name in imported:
        assert not any(f in name for f in forbidden), f"unexpected import {name!r} in src/consumers/contracts.py"


def test_module_imports_no_network_filesystem_or_subprocess_capable_library():
    tree = ast.parse(inspect.getsource(contracts_module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = ("requests", "httpx", "urllib", "socket", "subprocess", "os")
    for name in imported:
        assert not any(f in name for f in forbidden), f"unexpected import {name!r} in src/consumers/contracts.py"


def test_module_has_no_mutable_global_state():
    """Every module-level name besides dunders/typing constructs/dataclasses is a class -- no module-level dict/list holding shared state."""
    for name, value in vars(contracts_module).items():
        if name.startswith("_") or name in ("annotations",):
            continue
        if name in ("Literal", "dataclass"):
            continue
        assert isinstance(value, type) or name == "PresentationStatus" or name == "PresentationRow", (
            f"unexpected non-type, non-typing module-level name {name!r} -- possible mutable global state"
        )
