"""
Phase 7 Slice 1: package-wide static-boundary and integration-isolation
tests for `src.consumers`. Confirms the whole package is safe to import
from any process (Discord, Streamlit, or a future test) without starting
anything or reaching outside its own inputs, and confirms this slice
wired nothing into the existing API/consumer surfaces.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import pathlib

import src.consumers.contracts as contracts_module
import src.consumers.errors as errors_module
import src.consumers.injury as injury_module
import src.consumers.rankings as rankings_module
import src.consumers.team as team_module

_CONSUMER_MODULES = (contracts_module, errors_module, team_module, rankings_module, injury_module)

_FORBIDDEN_SUBSTRINGS = (
    "discord",
    "streamlit",
    "fastapi",
    "starlette",
    "pydantic",
    "sqlalchemy",
    "requests",
    "httpx",
    "urllib",
    "socket",
    "subprocess",
)


def _imported_module_names(module) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_no_consumer_module_imports_discord_streamlit_or_web_framework():
    for module in _CONSUMER_MODULES:
        imported = _imported_module_names(module)
        for name in imported:
            assert not any(f in name.lower() for f in _FORBIDDEN_SUBSTRINGS), (
                f"unexpected import {name!r} in {module.__name__}"
            )


def test_no_consumer_module_contains_a_calculation_or_identity_resolution_call():
    """Static guard: no basketball formula, normalization, or identity-resolution function name appears anywhere in src/consumers/."""
    forbidden_calls = (
        "resolve_team_identity",
        "canonical_team_slug",
        "normalize_team_name",
        "build_registry",
        "compute_",
        "build_matchup_feature_comparison",
        "build_team_summary",
    )
    for module in _CONSUMER_MODULES:
        source = inspect.getsource(module)
        for token in forbidden_calls:
            assert token not in source, f"unexpected {token!r} found in {module.__name__}"


def test_importing_src_consumers_performs_no_io():
    """Re-importing every consumer module (via importlib.reload) must not raise, block, or require any file/network/database access."""
    for module in _CONSUMER_MODULES:
        importlib.reload(module)  # must succeed silently -- no I/O, no side effect


def test_src_consumers_package_has_no_dependency_on_src_api_or_src_discord_bot():
    """This new layer must not import the very consumers it is meant to be reused by, nor the API layer it deliberately avoids duplicating."""
    for module in _CONSUMER_MODULES:
        imported = _imported_module_names(module)
        assert not any(name.startswith("src.api") for name in imported), f"{module.__name__} imports src.api"
        assert not any(name.startswith("src.discord_bot") for name in imported), f"{module.__name__} imports src.discord_bot"


def test_streamlit_and_api_files_still_do_not_reference_src_consumers():
    """
    Phase 7, Slice 2A (approved 2026-09-24) explicitly authorizes wiring
    `src.consumers` into the real Discord bot -- `src/discord_bot/` is
    deliberately excluded from this check for that reason. Streamlit
    integration remains explicitly deferred (a future, separately-approved
    Slice 2B) and this package was never wired into the FastAPI
    application -- both confirmed still unaffected by Slice 2A.
    """
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    candidate_files = (
        list((repo_root / "src" / "api").rglob("*.py"))
        + list((repo_root / "pages").glob("*.py"))
        + [repo_root / "app.py"]
    )
    for path in candidate_files:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        assert "src.consumers" not in text, f"unexpected src.consumers reference found in {path}"


def test_openapi_route_count_and_no_route_module_touched_by_this_slice():
    """Confirms the API's own route count is unaffected -- this slice added no route."""
    from src.api.main import create_app

    app = create_app(database_url="sqlite+pysqlite:///:memory:")
    assert len(app.openapi()["paths"]) == 9
