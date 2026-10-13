"""
Diagnostic service logic for `/v1/health` and `/v1/ready` (Phase 6B).

Neither function here touches HTTP concepts (status codes, Pydantic,
FastAPI) -- that translation belongs to `src/api/main.py`. Neither
function issues raw SQL: `check_readiness` reads the current Alembic
revision through Alembic's own `MigrationContext`/`ScriptDirectory` APIs,
the same authority `alembic upgrade`/`alembic check` already use, so the
expected head is never hardcoded here and never drifts from
`alembic/versions/`.
"""
from __future__ import annotations

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

from src.paths import ROOT_DIR
from src.services.contracts import HealthResult, ReadinessResult


def check_health() -> HealthResult:
    """Process liveness only -- no I/O, so this can never itself fail or block."""
    return HealthResult(ok=True)


def _alembic_config() -> Config:
    cfg = Config(str(ROOT_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT_DIR / "alembic"))
    return cfg


def check_readiness(engine: Engine) -> ReadinessResult:
    """
    Read-only readiness probe: opens one connection, reads the database's
    current Alembic revision(s), and compares them against this repo's own
    expected head(s). The connection is only ever read from and is closed
    (never committed) regardless of outcome -- no application transaction
    is begun, committed, or rolled back.
    """
    try:
        with engine.connect() as conn:
            current_heads = set(MigrationContext.configure(conn).get_current_heads())
    except Exception:
        return ReadinessResult(ready=False, detail="database unavailable")

    if not current_heads:
        return ReadinessResult(ready=False, detail="no migration state found")

    expected_heads = set(ScriptDirectory.from_config(_alembic_config()).get_heads())
    if current_heads != expected_heads:
        return ReadinessResult(ready=False, detail="migration state does not match expected head")

    return ReadinessResult(ready=True, detail=None)
