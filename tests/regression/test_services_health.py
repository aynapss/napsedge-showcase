"""
Phase 6B: unit tests for `src.services.health`, the pure diagnostic logic
behind `/v1/health` and `/v1/ready`. No FastAPI/HTTP layer involved here --
`tests/api/test_health.py` and `tests/api/test_ready.py` cover the route
translation separately.

Every fixture here is a disposable, isolated SQLite database created fresh
under `tmp_path` -- never a shared file, never `TeamDump`, never a live
network connection.
"""
from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from src.services.contracts import HealthResult, ReadinessResult
from src.services.health import check_health, check_readiness


def _alembic_config(db_path) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{db_path.as_posix()}")
    return cfg


def _migrated_engine(tmp_path):
    db_path = tmp_path / "readiness_migrated.db"
    command.upgrade(_alembic_config(db_path), "head")
    return create_engine(f"sqlite+pysqlite:///{db_path.as_posix()}", connect_args={"check_same_thread": False})


# ---------------------------------------------------------------------------
# check_health
# ---------------------------------------------------------------------------


def test_check_health_always_reports_ok():
    result = check_health()
    assert result == HealthResult(ok=True)


def test_check_health_is_deterministic_across_repeated_calls():
    results = [check_health() for _ in range(5)]
    assert all(r == HealthResult(ok=True) for r in results)


# ---------------------------------------------------------------------------
# check_readiness -- healthy path
# ---------------------------------------------------------------------------


def test_check_readiness_true_for_a_database_migrated_to_head(tmp_path):
    engine = _migrated_engine(tmp_path)
    try:
        result = check_readiness(engine)
        assert result == ReadinessResult(ready=True, detail=None)
    finally:
        engine.dispose()


def test_check_readiness_is_deterministic_across_repeated_calls(tmp_path):
    engine = _migrated_engine(tmp_path)
    try:
        results = [check_readiness(engine) for _ in range(5)]
        assert all(r == ReadinessResult(ready=True, detail=None) for r in results)
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# check_readiness -- unready paths
# ---------------------------------------------------------------------------


def test_check_readiness_false_for_a_database_with_no_migration_state(tmp_path):
    """A schema-less database (e.g. a fresh SQLite file never migrated) has no `alembic_version` row -- not ready, not a crash."""
    db_path = tmp_path / "unmigrated.db"
    engine = create_engine(f"sqlite+pysqlite:///{db_path.as_posix()}", connect_args={"check_same_thread": False})
    try:
        # Touch the file so it exists but carries no schema at all.
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

        result = check_readiness(engine)
        assert result.ready is False
        assert result.detail == "no migration state found"
    finally:
        engine.dispose()


def test_check_readiness_false_for_an_unreachable_database(tmp_path):
    """A connection attempt that fails outright (e.g. the parent directory does not exist) must be reported as not ready, never raised to the caller."""
    unreachable_path = tmp_path / "does_not_exist_dir" / "impossible.db"
    engine = create_engine(f"sqlite+pysqlite:///{unreachable_path.as_posix()}", connect_args={"check_same_thread": False})
    try:
        result = check_readiness(engine)
        assert result.ready is False
        assert result.detail == "database unavailable"
    finally:
        engine.dispose()


def test_check_readiness_detail_never_contains_the_database_url(tmp_path):
    """No sensitive value (connection string, path, credentials) may leak into the sanitized detail string."""
    unreachable_path = tmp_path / "does_not_exist_dir" / "impossible.db"
    url = f"sqlite+pysqlite:///{unreachable_path.as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    try:
        result = check_readiness(engine)
        assert result.detail is not None
        assert str(unreachable_path) not in result.detail
        assert url not in result.detail
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# check_readiness -- read-only guarantee
# ---------------------------------------------------------------------------


def test_check_readiness_never_mutates_the_database(tmp_path):
    """Calling check_readiness repeatedly must never change any row anywhere -- it is a pure read."""
    engine = _migrated_engine(tmp_path)
    try:
        with engine.connect() as conn:
            before = conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()

        for _ in range(3):
            check_readiness(engine)

        with engine.connect() as conn:
            after = conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()

        assert before == after
    finally:
        engine.dispose()
