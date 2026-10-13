"""
Phase 9B-6B: the dedicated backup lock session (docs/backup_artifact_contract_proposal.md
sections 13.3 and 13.10).

`backup_capture_lock(database_url, designation)` is a context manager that holds the
EXISTING advisory lock (`bulk_import_advisory_lock`: same namespace, same key derivation,
same non-blocking acquisition, same single release attempt) on ONE dedicated, unpooled
physical PostgreSQL session for the duration of a protected body, and then makes sure that
session has physically ended.

Resources this helper creates and owns -- and the only ones it ever cleans up:

* the LOCK engine: a new `NullPool` engine built from the caller's URL (never the application
  engine or any pool), whose single physical connection is the lock session L. A second physical
  connect on that engine is refused (`BackupLockSessionLost`), so L can never be replaced
  silently by a new backend after a loss;
* after exit, one short-lived VERIFIER engine and connection (also `NullPool`), validated against
  the same target, used only to check that L's backend is gone.

It never touches a caller's engine, pool, connection or transaction.

The lock session is not the snapshot owner. L runs only its identity query, target verification,
the lock and unlock statements, and the rollbacks of its own transactions. It is not yielded to
the body, and it is never the future snapshot-owning session (specification 6.2), which will be a
separate connection.

Order: the URL is checked first (driver `postgresql+psycopg`, `sslmode=verify-full`, an
explicit `sslrootcert`, a host and a database: specification section 8; there was no production
TLS validator to reuse). Then L connects; its PID and `backend_start` are recorded;
`verify_database_target` checks the target; L's autobegun transaction is rolled back; and
`bulk_import_advisory_lock` acquires the lock (a refusal raises its
`AdvisoryLockUnavailableError` and the body never runs). L's transaction is rolled back
again after acquisition. That rollback ends the transaction only; it does NOT release the
session-level lock. The body then runs with a `BackupLockSession` (target, key, PID,
`backend_start`; never the connection).

Exit: the shared manager attempts its single unlock exactly as before; this module adds no
unlock of its own, no retry and no `pg_advisory_unlock_all`. Then, using only the
resources above:
1. if ANY exception left the block (the body's, or the manager's release failure), L is
   discarded with `Connection.invalidate()`, so its DBAPI connection is closed and never
   rolled back or reused;
2. L is closed (`NullPool`: a physical close);
3. the lock engine is disposed;
4. a fresh verifier session, validated against the same designation, polls with a deadline
   for L's (PID, `backend_start`) in `pg_stat_activity` and lists the holders of the key in
   `pg_locks`.

Every step is attempted even if an earlier one raised. The outcome is a `LockCleanupReport`
whose `verification` is exactly one of `closed_and_released`, `original_session_present`,
`not_verified` or `no_session_created`. Another session holding the key is reported
separately (`other_holder_present`) and is never mistaken for L. Engine disposal, garbage
collection or a closed Python object are never taken as evidence. Cleanup policy never
depends on exception notes.

Errors: a primary exception (from the body or the shared manager) always propagates as the
same object, with its type, cause, notes and traceback. If cleanup is not confirmed, one
bounded, sanitized note is attached. Without a primary exception, unconfirmed cleanup raises
`BackupLockCleanupError`, which carries the report. Only `Exception` is absorbed during
cleanup: a new `KeyboardInterrupt` or `SystemExit` raised there propagates after the remaining
steps were attempted. A failure to connect raises `BackupLockSetupError` naming only the
exception type. No URL, credential, host or driver message is ever placed in an error or
note written by this module. The shared manager's own exceptions pass through unchanged.

Phase 9B-6C additions (compatible). At setup L also records, from ITS OWN session, the
database OID and -- if `has_function_privilege` allows it -- the cluster `system_identifier`
(the preflight's own statements, reused). The value is None when not readable; this module does
not refuse on it, the snapshot unit does. `BackupLockSession.confirm_held()` asks L itself, in a
fresh transaction, whether its backend still holds the key; it raises `BackupLockNotHeld`
(sanitized) when not, when L's backend is gone, or after the context has exited. It is a point
observation at the moment it is called, never a proof of continuous ownership between calls.

Scope: the shared key also excludes controlled imports (C9). That does not show that every
import, migration or administrative session cooperates. This module grants nothing,
terminates nothing, and is not a capture.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import URL, Connection, Engine, make_url
from sqlalchemy.pool import NullPool

from src.deployment.backup_capture_preflight import _DATABASE_SQL, _SYSTEM_IDENTIFIER_PRIVILEGE_SQL, _SYSTEM_IDENTIFIER_SQL
from src.exceptions import NapsEdgeError, ValidationError
from src.pipeline.bulk_import_safety import (
    DatabaseTargetDesignation,
    VerifiedDatabaseTarget,
    bulk_import_advisory_lock,
    compute_advisory_lock_key,
    verify_database_target,
)

__all__ = [
    "LOCK_APPLICATION_NAME",
    "VERIFIER_APPLICATION_NAME",
    "VERIFIED_CLOSED",
    "ORIGINAL_SESSION_PRESENT",
    "NOT_VERIFIED",
    "NO_SESSION_CREATED",
    "BackupLockSetupError",
    "BackupLockSessionLost",
    "BackupLockCleanupError",
    "BackupLockNotHeld",
    "LockCleanupReport",
    "BackupLockSession",
    "backup_capture_lock",
]

LOCK_APPLICATION_NAME = "napsedge_backup_lock"
VERIFIER_APPLICATION_NAME = "napsedge_backup_lock_verify"

#: Verification outcomes (a report value, not an artifact reason code).
VERIFIED_CLOSED = "closed_and_released"
ORIGINAL_SESSION_PRESENT = "original_session_present"
NOT_VERIFIED = "not_verified"
NO_SESSION_CREATED = "no_session_created"

_CONNECT_TIMEOUT_S = 15
_STATEMENT_TIMEOUT_MS = 15000
_VERIFY_DEADLINE_S = 10.0
_VERIFY_POLL_S = 0.05
_MAX_TYPE_NAME = 80

_IDENTITY_SQL = "SELECT pid, backend_start FROM pg_stat_activity WHERE pid = pg_backend_pid()"
_ORIGINAL_PRESENT_SQL = "SELECT count(*) FROM pg_stat_activity WHERE pid = :pid AND backend_start = :started"
_KEY_HOLDERS_SQL = (
    "SELECT l.pid FROM pg_locks l WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 1 "
    "AND l.classid::bigint = :hi AND l.objid::bigint = :lo "
    "AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database()) ORDER BY l.pid"
)

# Asked on L itself: its own PID, and whether that backend holds the key in this database (a point observation).
_HELD_SQL = (
    "SELECT pg_backend_pid(), count(*) FROM pg_locks l WHERE l.pid = pg_backend_pid() AND l.locktype = 'advisory' AND l.granted "
    "AND l.objsubid = 1 AND l.classid::bigint = :hi AND l.objid::bigint = :lo "
    "AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())"
)

_CLEANUP_UNCONFIRMED_NOTE = "backup lock session cleanup was not confirmed: verification={verification}; failed steps={failures}"


class BackupLockSetupError(NapsEdgeError):
    """The dedicated lock session could not be opened. Carries the exception TYPE only, never a URL, credential or driver message."""


class BackupLockSessionLost(NapsEdgeError):
    """The lock engine was asked for a second physical connection: the original lock session is gone and is never replaced silently."""


class BackupLockNotHeld(NapsEdgeError):
    """`confirm_held()` could not observe that L still holds the lock (lost, not held, or the context has exited). Fixed text plus a type name only."""


class BackupLockCleanupError(NapsEdgeError):
    """Cleanup of the owned lock session could not be confirmed and no other failure was propagating. `report` holds the facts."""

    def __init__(self, message: str, report: "LockCleanupReport") -> None:
        super().__init__(message)
        self.report = report


@dataclass(frozen=True)
class LockCleanupReport:
    """What cleanup did and what a fresh, validated session observed afterwards. `failures` holds `step:ExceptionType` entries only."""

    backend_pid: int | None
    lock_acquired: bool
    connection_discarded: bool
    connection_closed: bool
    engine_disposed: bool
    verification: str
    other_holder_present: bool | None
    failures: tuple[str, ...]

    @property
    def confirmed(self) -> bool:
        return not self.failures and self.verification in (VERIFIED_CLOSED, NO_SESSION_CREATED)


class BackupLockSession:
    """
    What the protected body receives. It deliberately exposes no connection. `system_identifier` and `database_oid`
    were read on L itself (`system_identifier` is None when L may not read it). `cleanup` is set when the block exits.
    """

    __slots__ = ("verified_target", "lock_key", "backend_pid", "backend_start", "system_identifier", "database_oid", "cleanup", "_confirm")

    def __init__(self, verified_target: VerifiedDatabaseTarget, lock_key: int, backend_pid: int, backend_start, *,
                 system_identifier: str | None = None, database_oid: int | None = None,
                 confirm: Callable[[], None] | None = None) -> None:
        self.verified_target = verified_target
        self.lock_key = lock_key
        self.backend_pid = backend_pid
        self.backend_start = backend_start
        self.system_identifier = system_identifier
        self.database_oid = database_oid
        self.cleanup: LockCleanupReport | None = None
        self._confirm = confirm

    def confirm_held(self) -> None:
        """Raises `BackupLockNotHeld` unless L, right now, still holds the key. A point observation; never a continuous guarantee."""
        if self._confirm is None:
            raise BackupLockNotHeld("the backup lock context is not active")
        self._confirm()


def _type_name(exc: BaseException) -> str:
    return type(exc).__name__[:_MAX_TYPE_NAME]


def _validated_url(database_url: URL | str) -> URL:
    """Specification section 8: psycopg over verified TLS with an explicit CA. Messages never include the URL or any part of it."""
    if isinstance(database_url, str):
        try:
            url = make_url(database_url)
        except Exception:  # noqa: BLE001 -- the parser's message may echo the URL
            raise ValidationError("database_url is not a valid database URL") from None
    elif isinstance(database_url, URL):
        url = database_url
    else:
        raise ValidationError("database_url must be a sqlalchemy URL or a URL string")
    if url.drivername != "postgresql+psycopg":
        raise ValidationError("the backup lock session requires the postgresql+psycopg driver")
    if url.query.get("sslmode") != "verify-full" or not url.query.get("sslrootcert"):
        raise ValidationError("the backup lock session requires sslmode=verify-full with an explicit sslrootcert")
    if not url.host or not url.database:
        raise ValidationError("the backup lock session requires an explicit host and database")
    return url


def _build_engine(url: URL, application_name: str) -> Engine:
    """A new, unpooled engine whose first physical connection is its only one: a second connect is refused before it reaches the network."""
    engine = create_engine(
        url,
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={
            "connect_timeout": _CONNECT_TIMEOUT_S,
            "application_name": application_name,
            "options": f"-c statement_timeout={_STATEMENT_TIMEOUT_MS}",
        },
    )
    connects = [0]

    @event.listens_for(engine, "do_connect")
    def _one_physical_connection(dialect, conn_rec, cargs, cparams):
        connects[0] += 1
        if connects[0] > 1:
            raise BackupLockSessionLost("the dedicated backup lock session was lost and is never replaced by a new connection")

    return engine


def _require_null_pool(engine: Engine) -> None:
    if type(engine.pool) is not NullPool:
        raise ValidationError("the backup lock session must use its own NullPool engine, never a pooled one")


def _verify_cleanup(engine_factory: Callable[[URL, str], Engine], url: URL, designation: DatabaseTargetDesignation,
                    pid: int, started) -> tuple[str, bool | None, list[str]]:
    """A fresh, validated, unpooled session: bounded polling until L's (pid, backend_start) is absent; never takes a lock itself."""
    failures: list[str] = []
    engine: Engine | None = None
    connection: Connection | None = None
    try:
        engine = engine_factory(url, VERIFIER_APPLICATION_NAME)
        _require_null_pool(engine)
        connection = engine.connect()
        verified = verify_database_target(connection, designation)
        key = compute_advisory_lock_key(verified)
        params = {"hi": key >> 32, "lo": key & 0xFFFFFFFF}
        deadline = time.monotonic() + _VERIFY_DEADLINE_S
        while True:
            present = connection.execute(text(_ORIGINAL_PRESENT_SQL), {"pid": pid, "started": started}).scalar_one()
            holders = [row[0] for row in connection.execute(text(_KEY_HOLDERS_SQL), params).fetchall()]
            connection.rollback()  # pg_stat_activity is cached per transaction: every poll reads in a new one
            if not present:
                return VERIFIED_CLOSED, bool(holders), failures
            if time.monotonic() >= deadline:
                return ORIGINAL_SESSION_PRESENT, None, failures
            time.sleep(_VERIFY_POLL_S)
    except Exception as exc:  # noqa: BLE001 -- inability to verify is a reported outcome, never success
        failures.append(f"verify:{_type_name(exc)}")
        return NOT_VERIFIED, None, failures
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception as exc:  # noqa: BLE001
                failures.append(f"verify_close:{_type_name(exc)}")
        if engine is not None:
            try:
                engine.dispose()
            except Exception as exc:  # noqa: BLE001
                failures.append(f"verify_dispose:{_type_name(exc)}")


@contextmanager
def backup_capture_lock(database_url: URL | str, designation: DatabaseTargetDesignation, *,
                        _engine_factory: Callable[[URL, str], Engine] | None = None) -> Iterator[BackupLockSession]:
    """
    Holds the existing advisory lock on a dedicated, unpooled session around the body, then ends that session and
    verifies it is gone (module docstring). `_engine_factory` is a test seam; whatever it returns must be a NullPool
    engine or nothing is attempted.
    """
    if not isinstance(designation, DatabaseTargetDesignation):
        raise ValidationError("designation must be a DatabaseTargetDesignation")
    url = _validated_url(database_url)
    factory = _engine_factory or _build_engine
    engine = factory(url, LOCK_APPLICATION_NAME)
    try:
        _require_null_pool(engine)
    except BaseException:
        engine.dispose()
        raise

    connection: Connection | None = None
    pid: int | None = None
    started = None
    acquired = False
    session: BackupLockSession | None = None
    primary: BaseException | None = None
    try:
        try:
            connection = engine.connect()
        except Exception as exc:  # noqa: BLE001 -- the driver's message can carry host and user names
            raise BackupLockSetupError(f"opening the dedicated backup lock session failed ({_type_name(exc)})") from None
        pid, started = connection.execute(text(_IDENTITY_SQL)).one()
        verified = verify_database_target(connection, designation)
        database_oid = int(connection.execute(text(_DATABASE_SQL)).one()[0])
        readable = bool(connection.execute(text(_SYSTEM_IDENTIFIER_PRIVILEGE_SQL)).scalar_one())
        system_identifier = str(connection.execute(text(_SYSTEM_IDENTIFIER_SQL)).scalar_one()) if readable else None
        connection.rollback()  # ends L's own autobegun verification transaction
        with bulk_import_advisory_lock(connection, verified) as key:
            acquired = True
            connection.rollback()  # ends the acquisition's transaction; the SESSION-level lock is NOT released by this

            def confirm() -> None:
                try:
                    current_pid, held = connection.execute(text(_HELD_SQL), {"hi": key >> 32, "lo": key & 0xFFFFFFFF}).one()
                    connection.rollback()  # L stays idle between observations
                except Exception as exc:  # noqa: BLE001 -- a lost or unusable L is reported, never repaired or replaced
                    raise BackupLockNotHeld(f"the backup lock session could not confirm it holds the lock ({_type_name(exc)})") from None
                if current_pid != pid or held != 1:
                    raise BackupLockNotHeld("the backup lock session no longer holds the lock")

            session = BackupLockSession(verified, key, pid, started, system_identifier=system_identifier, database_oid=database_oid, confirm=confirm)
            yield session
    except BaseException as exc:
        primary = exc
        raise
    finally:
        failures: list[str] = []
        interrupt: BaseException | None = None
        discarded = closed = disposed = False

        def attempt(step: str, action: Callable[[], None]) -> bool:
            nonlocal interrupt
            try:
                action()
                return True
            except Exception as exc:  # noqa: BLE001 -- a failed step never skips the remaining ones
                failures.append(f"{step}:{_type_name(exc)}")
            except BaseException as exc:  # a NEW interrupt: remaining steps still run, then it propagates
                failures.append(f"{step}:{_type_name(exc)}")
                interrupt = interrupt or exc
            return False

        if connection is not None:
            if primary is not None:
                discarded = attempt("discard", connection.invalidate)
            closed = attempt("close", connection.close)
        disposed = attempt("dispose", engine.dispose)

        other_holder: bool | None = None
        if connection is None:
            verification = NO_SESSION_CREATED
        elif pid is None or interrupt is not None:
            verification = NOT_VERIFIED
        else:
            try:
                verification, other_holder, verify_failures = _verify_cleanup(factory, url, designation, pid, started)
                failures += verify_failures
            except BaseException as exc:  # an interrupt during verification: reported, then propagated
                verification = NOT_VERIFIED
                failures.append(f"verify:{_type_name(exc)}")
                interrupt = interrupt or exc

        report = LockCleanupReport(
            backend_pid=pid, lock_acquired=acquired, connection_discarded=discarded, connection_closed=closed,
            engine_disposed=disposed, verification=verification, other_holder_present=other_holder, failures=tuple(failures),
        )
        if session is not None:
            session._confirm = None  # confirm_held() is rejected once the context has exited
            session.cleanup = report
        if interrupt is not None:
            raise interrupt  # never swallowed; a primary failure stays reachable as its context
        if not report.confirmed:
            message = _CLEANUP_UNCONFIRMED_NOTE.format(verification=verification, failures=",".join(failures) or "none")
            if primary is not None:
                primary.add_note(message)
            else:
                raise BackupLockCleanupError(message, report)
