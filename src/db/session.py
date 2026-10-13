"""
Session and transaction management (Phase 5A).

Explicit, not implicit: every write goes through `unit_of_work()`, which
commits once at the end of the `with` block and rolls back the *entire*
block on any exception -- there is no autocommit path and no commit call
inside any repository method (Phase 5A instruction: "do not commit inside
low-level model constructors" / "roll back the complete unit of work on
failure").
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.db.config import DatabasePoolSettings, get_database_url, get_pool_settings


def build_engine(url: str | None = None, *, echo: bool = False, pool: DatabasePoolSettings | None = None) -> Engine:
    """
    Build a new engine for the given (or configured) URL. Never called
    implicitly at import time -- the caller decides when a connection is
    actually needed.

    SQLite does not enforce `FOREIGN KEY` constraints unless
    `PRAGMA foreign_keys=ON` is set per connection (it is off by default)
    -- without this, this batch's own isolated-SQLite tests for foreign-key
    behavior would silently pass without the database ever having enforced
    anything, which is exactly the "do not claim PostgreSQL integration
    parity from SQLite-only execution" trap in the other direction. Every
    SQLite connection made through this factory enables it; this has no
    effect on PostgreSQL, which always enforces foreign keys.

    Phase 9B-8A: every non-SQLite engine is capped -- at most
    `pool_size + max_overflow` connections, waiting at most
    `pool_timeout_seconds` for a free one (then SQLAlchemy's pool
    `TimeoutError`, which the API maps to a sanitized 503). `pool` overrides
    the environment (`src.db.config.get_pool_settings`); SQLite engines keep
    SQLAlchemy's own SQLite pooling, unchanged.
    """
    resolved_url = url if url is not None else get_database_url()
    connect_args = {"check_same_thread": False} if resolved_url.startswith("sqlite") else {}
    caps: dict = {}
    if not resolved_url.startswith("sqlite"):
        settings = pool if pool is not None else get_pool_settings()
        caps = {"pool_size": settings.pool_size, "max_overflow": settings.max_overflow, "pool_timeout": settings.pool_timeout_seconds}
    engine = create_engine(resolved_url, echo=echo, future=True, connect_args=connect_args, **caps)

    if resolved_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)


@contextmanager
def unit_of_work(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """
    One explicit transaction. Commits once, only if the block completes
    without raising. Any exception (including a repository's own
    structured integrity error) rolls back everything the block did before
    the exception propagates -- no partial write survives.
    """
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
