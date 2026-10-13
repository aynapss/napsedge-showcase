import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

#: Directories directly under tests/ whose name matches a registered pytest
#: marker (see pytest.ini) get that marker auto-applied to every test inside,
#: so individual test files don't need to hand-decorate every function.
_AUTO_MARK_DIRS = {"unit", "regression", "integration", "live", "slow", "postgres"}


#: Public showcase: test cases that need the proprietary components or private data files omitted from this
#: repository (listed by node ID, one per line). They run in the private repository.
_SHOWCASE_EXCLUDED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "showcase_excluded.txt")


def pytest_collection_modifyitems(config, items):
    for item in items:
        rel_parts = os.path.relpath(str(item.fspath), REPO_ROOT).split(os.sep)
        for part in rel_parts:
            if part in _AUTO_MARK_DIRS:
                item.add_marker(getattr(pytest.mark, part))
    with open(_SHOWCASE_EXCLUDED, encoding="utf-8") as listing:
        excluded = {line.strip() for line in listing if line.strip()}
    deselected = [item for item in items if item.nodeid in excluded]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = [item for item in items if item.nodeid not in excluded]


@pytest.fixture
def isolated_root(tmp_path, monkeypatch):
    """
    Point src.paths.ROOT_DIR at a throwaway tmp_path and reload src.paths so
    the module-level constant is recomputed under it.

    Important limitation: ROOT_DIR and everything derived from it
    (TEAMDUMP_DIR, PLAYER_CARDS_DIR, etc.) are plain module-level constants,
    computed once at import time -- not lazy properties. Reloading
    src.paths updates *that module's own* names, but any other module that
    already did ``from src.paths import ROOT_DIR`` (or via
    src.config.paths, which re-exports the same names) keeps its own
    earlier-bound copy and will NOT see the override. Fully closing that
    gap would mean switching every such constant to a function call
    everywhere it's used -- out of scope for Phase 1's "no broad import
    rewrite" constraint.

    Prefer an explicit per-call argument when a function offers one (e.g.
    ``fetch_team_page(..., cache_dir=...)``) -- that pattern is immune to
    this limitation, which is why the existing scraper tests use it instead
    of this fixture. Use ``isolated_root`` only for code that reads
    ``src.paths`` (not a re-export of it) at call time.
    """
    import importlib

    monkeypatch.setenv("NAPSEDGE_ROOT_DIR", str(tmp_path))
    import src.paths as paths_module

    importlib.reload(paths_module)
    yield tmp_path
    importlib.reload(paths_module)
