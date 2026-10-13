"""
Controlled bulk-import design, Slice 1 (docs/controlled_bulk_import_design.md
sections 6/16, approved for this slice only): tests for the four adapter
contract-version constants added this slice. These constants are static,
unused-at-runtime markers reserved for the future (not-yet-approved)
dry-run manifest writer -- this slice adds no parsing, validation,
identity, persistence, or manifest behavior at all, so these tests only
prove the constants exist, hold the exact approved value/type, are
immutable-by-construction, and were added with zero import-time side
effect (no network, filesystem, or database access).
"""
from __future__ import annotations

import ast
import importlib
import inspect

import pytest

EXPECTED_CONTRACT_VERSIONS = {
    "src.pipeline.allstats_import_adapter": ("ALLSTATS_IMPORT_CONTRACT_VERSION", "1"),
    "src.pipeline.game_log_import_adapter": ("TEAM_GAMELOG_IMPORT_CONTRACT_VERSION", "1"),
    "src.pipeline.espn_import_adapter": ("ESPN_IMPORT_CONTRACT_VERSION", "1"),
    "src.pipeline.player_game_log_import_adapter": ("PLAYER_GAMELOG_IMPORT_CONTRACT_VERSION", "1"),
}


@pytest.mark.parametrize("module_path,expected", EXPECTED_CONTRACT_VERSIONS.items(), ids=list(EXPECTED_CONTRACT_VERSIONS))
def test_adapter_exposes_its_contract_version_constant(module_path, expected):
    constant_name, expected_value = expected
    module = importlib.import_module(module_path)
    assert hasattr(module, constant_name), f"{module_path} does not expose {constant_name}"
    value = getattr(module, constant_name)
    assert value == expected_value
    assert isinstance(value, str)  # a plain string is immutable by construction -- no mutable container


@pytest.mark.parametrize("module_path,expected", EXPECTED_CONTRACT_VERSIONS.items(), ids=list(EXPECTED_CONTRACT_VERSIONS))
def test_contract_version_constant_assigned_exactly_once(module_path, expected):
    """AST-level guard: the constant must be a single, module-level, top-of-file
    assignment -- never reassigned elsewhere, never computed from mutable state,
    never a class/instance attribute that could be mutated at runtime."""
    constant_name, _ = expected
    module = importlib.import_module(module_path)
    tree = ast.parse(inspect.getsource(module))
    assignments = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == constant_name for t in node.targets)
    ]
    assert len(assignments) == 1, f"{constant_name} must be assigned exactly once in {module_path}, found {len(assignments)}"
    (assignment,) = assignments
    assert isinstance(assignment.value, ast.Constant) and isinstance(assignment.value.value, str), (
        f"{constant_name} must be a plain string literal, not a computed or mutable value"
    )


@pytest.mark.parametrize("module_path", EXPECTED_CONTRACT_VERSIONS)
def test_contract_version_not_added_to_public_all_export(module_path):
    """Matches this codebase's own established precedent (espn_import_adapter.TRANSFORMATION_VERSION,
    already excluded from __all__ despite being a real, importable, test-used constant) -- the new
    Slice 1 constants follow the same convention, never added to __all__."""
    module = importlib.import_module(module_path)
    constant_name, _ = EXPECTED_CONTRACT_VERSIONS[module_path]
    all_list = getattr(module, "__all__", [])
    assert constant_name not in all_list


def test_contract_version_assignment_is_a_bare_literal_with_no_side_effect():
    """Each new constant's own assignment (already proven, in
    test_contract_version_constant_assigned_exactly_once, to be a single top-level
    `ast.Constant` string) has no child call/attribute/subscript nodes at all -- a bare
    string literal cannot perform network, filesystem, or database access by construction.
    Combined with each adapter's own pre-existing 'never imports network libraries' AST
    guard (in that adapter's own test file), this confirms the addition introduces no
    import-time side effect of any kind, without needing to reload any module (reloading
    a module containing dataclasses mid-suite would risk breaking isinstance checks in
    other, already-imported test files that hold references to the original classes)."""
    for module_path, (constant_name, _) in EXPECTED_CONTRACT_VERSIONS.items():
        module = importlib.import_module(module_path)
        tree = ast.parse(inspect.getsource(module))
        (assignment,) = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == constant_name for t in node.targets)
        ]
        # A bare ast.Constant node has no child nodes of its own -- walking it yields
        # only itself, proving there is no nested Call/Attribute/Subscript anywhere in
        # this specific assignment's own value expression.
        assert list(ast.walk(assignment.value)) == [assignment.value]


def test_importing_each_adapter_module_standalone_triggers_no_network_access():
    """Spawns a clean subprocess (never touching this test session's own already-imported
    module state) that imports each adapter module fresh with real TCP socket connections
    patched to raise -- proves the module (including this slice's own new constant) can be
    imported with zero network access, end to end, not just by inspecting its source."""
    import subprocess
    import sys

    for module_path in EXPECTED_CONTRACT_VERSIONS:
        script = (
            "import socket\n"
            "def _boom(*a, **k):\n"
            "    raise AssertionError('no network access expected from importing an adapter module')\n"
            "socket.socket.connect = _boom\n"
            f"import {module_path}\n"
            "print('OK')\n"
        )
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, f"{module_path} failed to import cleanly: {result.stderr}"
        assert "OK" in result.stdout


def test_all_four_contract_versions_are_distinct_constant_names():
    """No accidental name collision across modules -- each adapter's own constant name is unique, so a future manifest writer importing all four into one namespace never shadows one with another."""
    names = [name for name, _ in EXPECTED_CONTRACT_VERSIONS.values()]
    assert len(names) == len(set(names)) == 4


def test_all_four_contract_versions_currently_share_the_same_initial_value():
    """Documents the deliberate Slice 1 choice: every adapter starts at the same baseline value
    ("1") since none has had its own contract changed since this slice's own baseline was set --
    they are expected to diverge independently as each adapter's own contract evolves later."""
    values = {value for _, value in EXPECTED_CONTRACT_VERSIONS.values()}
    assert values == {"1"}
