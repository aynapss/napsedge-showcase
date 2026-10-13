"""
Alembic environment (Phase 5A).

Connection string resolution goes through `src.db.config.get_database_url()`
exactly like every other part of this package -- no second URL-resolution
path, no hardcoded credential, no implicit production connection. A caller
proving migrations in an isolated environment sets `NAPSEDGE_DATABASE_URL`
to a throwaway SQLite/PostgreSQL URL before invoking Alembic; this file
never assumes or defaults to a production database.
"""
from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from src.db.base import Base
from src.db.config import get_database_url

# Import every model module so its tables register on Base.metadata before
# `target_metadata` is read below -- a model class that's never imported
# would otherwise be silently invisible to autogenerate/create_all.
from src.db import models  # noqa: F401
from src.db import allstats_models  # noqa: F401  -- Phase 5B1
from src.db import game_log_models  # noqa: F401  -- Phase 5B2
from src.db import espn_models  # noqa: F401  -- Phase 5B3
from src.db import player_game_log_models  # noqa: F401  -- Phase 5B4

config = context.config

if config.config_file_name is not None:
    # Phase 9E-1: keep loggers that already exist (fileConfig's default disables them), so running a migration in a process
    # never silences that process's own application loggers (e.g. the API access log).
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# Resolution priority: an explicit `NAPSEDGE_DATABASE_URL` env var always
# wins (matches src.db.config.get_database_url()'s own priority); otherwise
# keep a URL a caller already set programmatically on this Config object
# (this batch's own isolated migration tests do exactly that, pointing at
# a disposable tmp_path database) rather than overwriting it; only fall
# back to get_database_url()'s dev-sqlite default when neither is present
# -- e.g. a bare `alembic upgrade head` invoked from a shell with no env
# var set and no programmatic override.
if os.environ.get("NAPSEDGE_DATABASE_URL") or not config.get_main_option("sqlalchemy.url"):
    # `Config.set_main_option` runs the value through ConfigParser interpolation,
    # where a literal `%` is syntax. A valid URL with percent-encoding (an
    # encoded `@` or `%` in a password, an encoded space in a certificate path)
    # would otherwise fail with "invalid interpolation syntax" before any
    # connection -- and that error text echoes the URL, credentials included.
    # Doubling `%` here is purely a storage escape: `get_main_option` (and so
    # `engine_from_config` / offline `url`) returns the original text exactly,
    # nothing is decoded or re-encoded, and callers that set their own URL
    # programmatically still escape their own `%` as before.
    config.set_main_option("sqlalchemy.url", get_database_url().replace("%", "%%"))


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
