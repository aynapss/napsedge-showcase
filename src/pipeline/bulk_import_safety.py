"""
Controlled bulk-import design, Slice 4 (docs/controlled_bulk_import_design.md
section 12, approved for this slice only): the database-target safety
guard and fail-fast PostgreSQL advisory-lock module.

This module never creates an engine, connection, session, or database
URL. Every function here takes an already-open, caller-managed
`sqlalchemy.engine.Connection` -- connection and transaction ownership
(open/close/commit/rollback) stay entirely with the caller. Nothing in
this module reads `.env`/`NAPSEDGE_DATABASE_URL` or any other
environment variable; a target is always supplied explicitly, in-memory,
by the caller.

**This slice does not authorize production use.** It is a validation
primitive -- a future, separately-approved slice decides whether/how it
is wired into a real import runner. This slice's own automated tests use
only the established disposable PostgreSQL fixture
(`tests/postgres/conftest.py`), never a production or shared database.

Two independent, composable pieces:

1. **Target verification** (`DatabaseTargetDesignation`,
   `verify_database_target`) -- the caller must supply an immutable
   designation naming the exact database they intend to target *and* an
   exact, case-sensitive acknowledgement phrase deterministically derived
   from that same name (see `build_expected_acknowledgement` -- the
   documented format is
   ``"I CONFIRM BULK IMPORT TARGET: {database_name}"``, never a bare
   boolean flag, so a caller cannot "confirm" without the database's own
   real name appearing in what they typed). `verify_database_target`
   then queries the *connected* server for its own actual database name
   and version and confirms an exact match, rejecting a non-PostgreSQL
   connection, a closed/invalid connection, a name mismatch, or an
   administrative database (`postgres`/`template0`/`template1`) -- never
   trusting the caller's own claim alone. Success returns a frozen
   `VerifiedDatabaseTarget` containing only non-secret server identity
   (the database name and the `SELECT version()` string) -- never a
   credential, a full connection URL, a query parameter, or the
   acknowledgement phrase itself.
2. **Advisory locking** (`bulk_import_advisory_lock`) -- a context
   manager that requires an already-`VerifiedDatabaseTarget`, computes a
   stable, deterministic 63-bit lock key (via the existing, unmodified
   `src.identity.stable_id.stable_wide_id` -- reused, not duplicated,
   never Python's randomized `hash()`) from a fixed NapsEdge namespace
   plus the verified database's own real name, and issues a single,
   non-blocking `pg_try_advisory_lock` on the caller's own connection.
   Failure to acquire raises immediately -- no retry, no polling, no
   lock-stealing. The lock is released via `pg_advisory_unlock` in a
   `finally` block on every exit path (normal or exceptional), and the
   unlock's own boolean result is checked -- an unexpected `false` (this
   session did not hold the lock) raises its own error. The release is
   attempted exactly once after a successful acquisition (never retried,
   never after a refused acquisition).

   **Exception preservation.** An exception already propagating from the
   caller's own protected block is NEVER replaced by a problem releasing
   the lock: whether the unlock statement raises an ordinary exception or
   reports `false`, the ORIGINAL exception object propagates with its type,
   explicit cause, existing notes and traceback intact, and a bounded,
   sanitized note is attached saying that the release failed or was not
   confirmed (the exception TYPE only: never an exception message, a SQL
   parameter, a URL or a credential). Without a primary failure an
   unsuccessful release stays a visible failure. A primary
   KeyboardInterrupt or SystemExit is preserved the same way; a NEW
   interrupt raised during the release is never swallowed. Preserving the
   primary failure is NOT a claim that the lock was released: when the
   release raised or reported false, the lock may remain held until its
   session ends or the caller releases it (for example after a SQL error
   in the protected block aborted the transaction, the unlock statement
   itself cannot run), and what PostgreSQL holds is for the caller to
   verify. This module still never commits, rolls back, closes,
   invalidates or disposes anything: all of that stays with the caller.

PostgreSQL's own advisory-lock semantics are session-scoped and
naturally reentrant per session+key: a nested
`bulk_import_advisory_lock(same_connection, same_target)` on the exact
same open connection succeeds immediately (the same session re-acquiring
its own held lock, per PostgreSQL's own internal reference count) and
correctly balances on unlock -- this module does not fight or re-implement
that native behavior, and does not add any lock-stealing or timeout logic
of its own.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from src.exceptions import AdvisoryLockUnavailableError, DatabaseTargetVerificationError, ValidationError
from src.identity.stable_id import stable_wide_id

__all__ = [
    "ADMINISTRATIVE_DATABASE_NAMES",
    "ACKNOWLEDGEMENT_TEMPLATE",
    "LOCK_NAMESPACE",
    "build_expected_acknowledgement",
    "DatabaseTargetDesignation",
    "VerifiedDatabaseTarget",
    "verify_database_target",
    "compute_advisory_lock_key",
    "bulk_import_advisory_lock",
]

#: Never a valid bulk-import target, regardless of caller intent --
#: compared case-insensitively so a quoted case-variant cannot slip past
#: this guard.
ADMINISTRATIVE_DATABASE_NAMES: frozenset[str] = frozenset({"postgres", "template0", "template1"})

#: The one, documented acknowledgement format -- deterministic, and
#: embeds the caller's own claimed target database name so an
#: acknowledgement cannot be copy-pasted across two different databases
#: without the caller noticing the name is wrong. Use
#: `build_expected_acknowledgement` to construct a matching value; never
#: hand-format this template directly.
ACKNOWLEDGEMENT_TEMPLATE = "I CONFIRM BULK IMPORT TARGET: {database_name}"

#: Fixed NapsEdge namespace for this module's own advisory-lock key
#: derivation -- see `compute_advisory_lock_key`. Never reused for any
#: other purpose; a distinct namespace string here guarantees this
#: module's own lock keys can never collide with any other stable-id
#: consumer's own key space (e.g. `game`/`game_source` identity), since
#: `stable_wide_id` bakes the namespace into the hashed input itself.
LOCK_NAMESPACE = "napsedge_bulk_import_advisory_lock"


#: Cleanup notes attached to a PRIMARY failure when releasing the lock also failed. Fixed text plus the exception TYPE name only (bounded): never a message,
#: SQL parameter, URL or secret.
RELEASE_FAILED_NOTE = "the advisory lock release also failed ({error_type}); the lock may remain held until its session ends"
RELEASE_NOT_HELD_NOTE = "the advisory lock release reported that this session did not hold the lock"
_MAX_ERROR_TYPE_NAME = 80


def build_expected_acknowledgement(database_name: str) -> str:
    """The one correct acknowledgement phrase for `database_name`, per `ACKNOWLEDGEMENT_TEMPLATE`. Pure string formatting -- no I/O, no randomness."""
    return ACKNOWLEDGEMENT_TEMPLATE.format(database_name=database_name)


@dataclass(frozen=True)
class DatabaseTargetDesignation:
    """
    An immutable, caller-declared bulk-import target. Self-validating at
    construction: a non-empty, non-administrative `expected_database_name`
    and an `acknowledgement` that exactly (case-sensitively) matches
    `build_expected_acknowledgement(expected_database_name)` are both
    required before this object can even be constructed -- a designation
    object existing at all is itself proof the caller supplied a
    correctly-formatted acknowledgement for this exact name.
    """

    expected_database_name: str
    acknowledgement: str

    def __post_init__(self) -> None:
        name = self.expected_database_name
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("expected_database_name must be a non-empty string")
        if name.strip().lower() in ADMINISTRATIVE_DATABASE_NAMES:
            raise ValidationError(f"refusing to target the administrative database {name!r}")
        if not isinstance(self.acknowledgement, str) or not self.acknowledgement:
            raise ValidationError("acknowledgement must be a non-empty string")
        expected = build_expected_acknowledgement(name)
        if self.acknowledgement != expected:
            raise ValidationError(
                "acknowledgement does not exactly match the required phrase for this database name "
                "(see build_expected_acknowledgement) -- refusing a mismatched or malformed confirmation"
            )


@dataclass(frozen=True)
class VerifiedDatabaseTarget:
    """
    The sanitized, non-secret result of a successful `verify_database_target`
    call. Contains only the connected server's own real database name and
    its `SELECT version()` string -- never a credential, a connection URL,
    a query parameter, or the acknowledgement phrase that produced it.
    """

    database_name: str
    server_version: str


def verify_database_target(connection: Connection, designation: DatabaseTargetDesignation) -> VerifiedDatabaseTarget:
    """
    Queries `connection`'s own connected PostgreSQL server for its real
    database identity and confirms it exactly matches `designation`.
    Raises `DatabaseTargetVerificationError` for a non-PostgreSQL
    connection, a closed/invalid connection, or a name mismatch. Never
    trusts `designation.expected_database_name` alone -- the connection's
    own server-reported identity is always the source of truth.
    """
    if not isinstance(designation, DatabaseTargetDesignation):
        raise ValidationError("designation must be a DatabaseTargetDesignation produced by its own constructor")
    if connection is None or connection.closed:
        raise DatabaseTargetVerificationError("refusing to verify a closed or invalid connection")

    dialect_name = connection.engine.dialect.name
    if dialect_name != "postgresql":
        raise DatabaseTargetVerificationError(
            f"refusing a non-PostgreSQL connection (dialect={dialect_name!r}) -- this guard only ever verifies real PostgreSQL targets"
        )

    try:
        actual_name, server_version = connection.execute(text("SELECT current_database(), version()")).one()
    except Exception as exc:  # pragma: no cover -- only on a genuinely broken/mid-failure connection
        raise DatabaseTargetVerificationError(f"failed to query the connected server's own identity: {exc.__class__.__name__}") from exc

    if actual_name != designation.expected_database_name:
        raise DatabaseTargetVerificationError(
            f"connected database {actual_name!r} does not match the expected target {designation.expected_database_name!r}"
        )

    return VerifiedDatabaseTarget(database_name=actual_name, server_version=server_version)


def compute_advisory_lock_key(verified_target: VerifiedDatabaseTarget) -> int:
    """
    A stable, deterministic, signed-bigint-safe (0 to 2**63 - 1) advisory-
    lock key for `verified_target`'s own real database name, reusing the
    existing, unmodified `stable_wide_id` (SHA-256-based, never Python's
    randomized `hash()`). Requires an already-verified target -- never
    accepts a bare database-name string, so a lock key can never be
    computed for a target that was never actually confirmed against a
    live connection.
    """
    if not isinstance(verified_target, VerifiedDatabaseTarget):
        raise ValidationError("verified_target must be a VerifiedDatabaseTarget produced by verify_database_target")
    return stable_wide_id(namespace=LOCK_NAMESPACE, key=verified_target.database_name)


def _unlock(connection: Connection, lock_key: int) -> bool:
    """The ONE release call site: a single `pg_advisory_unlock`, never retried."""
    return connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key}).scalar_one()


def _release_after_failure(connection: Connection, lock_key: int, primary: BaseException) -> None:
    """
    Releases the lock exactly once while `primary` is propagating, WITHOUT ever replacing it. An ordinary exception from the unlock, or a `false` result, is recorded as a
    bounded, sanitized note on `primary` (the release is then unconfirmed). Only `Exception` is absorbed: a new KeyboardInterrupt or SystemExit raised during the release
    is not swallowed and propagates, with `primary` as its context.
    """
    try:
        released = _unlock(connection, lock_key)
    except Exception as release_error:  # noqa: BLE001 -- must not replace the primary failure; only the TYPE name is recorded
        primary.add_note(RELEASE_FAILED_NOTE.format(error_type=type(release_error).__name__[:_MAX_ERROR_TYPE_NAME]))
    else:
        if not released:
            primary.add_note(RELEASE_NOT_HELD_NOTE)


@contextmanager
def bulk_import_advisory_lock(connection: Connection, verified_target: VerifiedDatabaseTarget):
    """
    Non-blocking PostgreSQL session-level advisory lock, held on
    `connection` for the lifetime of this `with` block. Raises
    `AdvisoryLockUnavailableError` immediately if another connection
    already holds the same key -- never retried, never polled, and never
    followed by an unlock. Yields the computed lock key. After a successful
    acquisition the release is attempted exactly once, on every exit path.
    A failure to release raises its own error ONLY when the protected block
    itself succeeded; when an exception is already propagating from the
    caller's own code, that ORIGINAL exception (object, type, cause, notes,
    traceback) is preserved and a bounded, sanitized note records the
    unconfirmed release (see the module docstring). Never commits, rolls
    back, closes, invalidates or disposes anything.
    """
    lock_key = compute_advisory_lock_key(verified_target)
    acquired = connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key}).scalar_one()
    if not acquired:
        raise AdvisoryLockUnavailableError(
            f"another connection already holds the bulk-import advisory lock for database "
            f"{verified_target.database_name!r} (key={lock_key}) -- refusing to start a second "
            f"concurrent import; this is never retried automatically"
        )
    try:
        yield lock_key
    except BaseException as primary:
        _release_after_failure(connection, lock_key, primary)
        raise  # the original exception object, with its traceback, cause and notes
    released = _unlock(connection, lock_key)  # reached only when the protected block succeeded: a release failure is then the visible failure
    if not released:
        raise AdvisoryLockUnavailableError(
            f"pg_advisory_unlock reported the lock for database {verified_target.database_name!r} "
            f"(key={lock_key}) was not held by this session -- internal consistency error, never expected"
        )
