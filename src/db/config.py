"""
Database configuration boundary (Phase 5A).

PostgreSQL is the authoritative target for this migration. Nothing in this
module -- or anywhere else built in this batch -- opens a real database
connection at import time or resolves a *production* connection string by
default. `get_database_url()` only ever resolves a connection string from
configuration; connecting is the caller's own explicit, later action
(`src.db.session`).

Resolution order:
1. `NAPSEDGE_DATABASE_URL` env var -- a real deployment sets this to a
   PostgreSQL DSN (e.g. `postgresql+psycopg://user:pass@host:5432/napsedge`).
   Read via `os.environ.get` only; never logged, never printed, never
   asserted against a literal value in any test in this batch (per the "no
   secret access" safety constraint).
   Phase 9 decision 9: on a server the URL carries NO password; the
   password is read from `NAPSEDGE_DATABASE_PASSWORD_FILE` (a read-only
   credential file, see `src.secret_files`) and inserted here, so it never
   sits in the process environment. A URL that already carries a password
   is refused when the file is also set.
2. A local-dev-only SQLite fallback under `<repo>/data/napsedge_dev.db`,
   used only when the env var is unset. Safe because it holds no
   credentials and touches no shared/production resource. Tests in this
   batch always pass their own explicit `sqlite:///:memory:` or tmp-path
   URL directly to `src.db.session` rather than relying on this fallback.

This module never reads, prints, or validates the *contents* of `.env` --
`os.environ` is populated by whatever already loaded the process
environment (this repo's own `load_env_file()` convention, or the shell),
independently of this module.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from src.exceptions import DatabasePoolConfigurationError, SecretFileConfigurationError
from src.paths import ROOT_DIR
from src.secret_files import read_secret_file

#: Phase 9 decision 9: the database password as a read-only credential file (the URL then carries no password).
DATABASE_PASSWORD_FILE_ENV_VAR = "NAPSEDGE_DATABASE_PASSWORD_FILE"

_DEV_SQLITE_PATH = ROOT_DIR / "data" / "napsedge_dev.db"


@dataclass(frozen=True)
class DatabaseConfig:
    url: str
    echo: bool = False


def get_database_url() -> str:
    """
    Resolve the configured connection string. Never connects. Never reads
    the *value* of `NAPSEDGE_DATABASE_URL` for any purpose other than
    passing it through unexamined to the engine factory (`src.db.session`).
    """
    override = os.environ.get("NAPSEDGE_DATABASE_URL")
    password_path = (os.environ.get(DATABASE_PASSWORD_FILE_ENV_VAR) or "").strip()
    if password_path:
        if not override:
            raise SecretFileConfigurationError(f"{DATABASE_PASSWORD_FILE_ENV_VAR} is set but NAPSEDGE_DATABASE_URL is not")
        from sqlalchemy.engine import make_url
        from sqlalchemy.exc import ArgumentError

        try:
            url = make_url(override)
        except ArgumentError:
            raise SecretFileConfigurationError("NAPSEDGE_DATABASE_URL is not a valid database URL") from None
        if url.password is not None:
            raise SecretFileConfigurationError(f"NAPSEDGE_DATABASE_URL must not carry a password when {DATABASE_PASSWORD_FILE_ENV_VAR} is set")
        return url.set(password=read_secret_file(DATABASE_PASSWORD_FILE_ENV_VAR, password_path)).render_as_string(hide_password=False)
    if override:
        return override
    _DEV_SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+pysqlite:///{_DEV_SQLITE_PATH.as_posix()}"


#: Phase 9B-8A connection caps. Each PostgreSQL engine built by `src.db.session.build_engine` holds at most
#: `pool_size + max_overflow` connections and waits at most `pool_timeout_seconds` for a free one. The defaults
#: (2 + 2 = 4 per process) fit the selected DigitalOcean 1 GiB plan's 22 connections with headroom: the API,
#: Streamlit and Discord processes 3 x 4 = 12, the backup job about 4, one operator session -- 17 of 22.
#: An absent variable means its default; a malformed one is refused (never silently uncapped).
POOL_SIZE_ENV_VAR = "NAPSEDGE_DB_POOL_SIZE"
MAX_OVERFLOW_ENV_VAR = "NAPSEDGE_DB_MAX_OVERFLOW"
POOL_TIMEOUT_ENV_VAR = "NAPSEDGE_DB_POOL_TIMEOUT_SECONDS"
#: variable -> (minimum, maximum, default)
POOL_BOUNDS: dict[str, tuple[int, int, int]] = {
    POOL_SIZE_ENV_VAR: (1, 10, 2),
    MAX_OVERFLOW_ENV_VAR: (0, 10, 2),
    POOL_TIMEOUT_ENV_VAR: (1, 60, 10),
}
_PLAIN_INTEGER = re.compile(r"[0-9]{1,4}")


@dataclass(frozen=True)
class DatabasePoolSettings:
    pool_size: int = POOL_BOUNDS[POOL_SIZE_ENV_VAR][2]
    max_overflow: int = POOL_BOUNDS[MAX_OVERFLOW_ENV_VAR][2]
    pool_timeout_seconds: int = POOL_BOUNDS[POOL_TIMEOUT_ENV_VAR][2]

    @property
    def max_connections(self) -> int:
        return self.pool_size + self.max_overflow


def get_pool_settings() -> DatabasePoolSettings:
    """
    Resolve the connection caps from the environment. Never connects.
    Raises `DatabasePoolConfigurationError` naming every malformed
    variable (never a value); an absent or blank variable takes its default.
    """
    values: dict[str, int] = {}
    malformed: list[str] = []
    for name, (low, high, default) in POOL_BOUNDS.items():
        raw = os.environ.get(name, "").strip()
        if not raw:
            values[name] = default
        elif _PLAIN_INTEGER.fullmatch(raw) and low <= int(raw) <= high:
            values[name] = int(raw)
        else:
            malformed.append(f"{name} must be a whole number from {low} to {high}")
    if malformed:
        raise DatabasePoolConfigurationError("; ".join(malformed))
    return DatabasePoolSettings(values[POOL_SIZE_ENV_VAR], values[MAX_OVERFLOW_ENV_VAR], values[POOL_TIMEOUT_ENV_VAR])


def get_database_config() -> DatabaseConfig:
    return DatabaseConfig(
        url=get_database_url(),
        echo=os.environ.get("NAPSEDGE_DATABASE_ECHO", "").strip().lower() in ("1", "true", "yes"),
    )
