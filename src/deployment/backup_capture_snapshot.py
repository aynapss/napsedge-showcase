"""
Phase 9B-6C: the snapshot-owner session and its baseline reads (docs/backup_artifact_contract_proposal.md
sections 6.2, 13.4 and 13.11).

`backup_capture_snapshot(lock, database_url, designation, *, expected_alembic_head, artifact_id)` is a context manager
used INSIDE an active `backup_capture_lock` context. It opens a separate, owned physical session S, starts ONE
`REPEATABLE READ, READ ONLY` transaction on it, refuses unsupported or mismatched sources, exports the transaction's
snapshot, reads the established baseline through S's own Connection, and yields a `SnapshotHandle` while that
transaction stays open, so a consumer can import the snapshot for the whole body. On exit S is ended and physically
closed, and its absence is verified, before control returns to the enclosing lock context. That makes S's end
precede L's release whenever the two contexts are nested as intended.

Resources this helper creates and owns, and the only ones it ever cleans up: one `NullPool` engine (application name
`napsedge_backup_snapshot`, built by the lock module's own `_build_engine`, so a second physical connect is refused and
a lost backend is never replaced) with its single physical connection S, and, after exit, the lock module's own
bounded cleanup verifier. It never touches L's connection except through `lock.confirm_held()`, and it never touches a
caller's engine, pool, connection or transaction. S is not L: its backend PID must differ from L's.

Order (every step before the yield must succeed or nothing usable is yielded):
1. Inputs: an active `BackupLockSession`, a `DatabaseTargetDesignation`, the expected head, a UUID4 `artifact_id`, and a
   URL passing the lock module's own verify-full TLS check (reused, not restated).
2. `lock.confirm_held()`: L, asked on itself, still holds the key.
3. Connect S. Its isolation (`REPEATABLE READ`) and read-only mode are set on the connection BEFORE its transaction
   begins; the transaction is begun explicitly (no autobegin), and its first statements are `SET LOCAL lock_timeout` (a
   bound on S only) and a read of the server's own `transaction_isolation`, `transaction_read_only`, PID,
   `backend_start` and `now()`. Anything other than `repeatable read` / `on` is refused.
4. `observe_capture_preflight(S, v1_policy(...))` on S's own Connection (target verification included). The source is
   refused unless `accepted`.
5. Identity: L's and S's facts must agree. Each session read its own facts: the cluster `system_identifier`, the
   database OID and the verified database name. A missing `system_identifier` on either session refuses (fail
   closed). The preflight's `accepted` flag keeps its 13.9 meaning and is not changed by this.
6. `lock.confirm_held()`, then `pg_export_snapshot()` and `pg_current_snapshot()` from S's transaction.
7. The baseline: every read on S's exact Connection, inside the same transaction (`now()` is re-read and must be
   unchanged). These are the established reads of `DatabaseBaseline` (tests/postgres/pg_restore_compare.py), with the
   same SQL, order and shapes, plus `sequence_details` (C3-C5's direct reads). They are held as the production
   `BaselineDocument` and validated by `parse_baseline` on its own canonical bytes. **Sequence values are NOT frozen by
   the snapshot:** they are read live (6.5), and only `monotonic_v1` at restore verification interprets them. Nothing
   here calls `nextval` or `setval`, or repairs anything.
8. `lock.confirm_held()`, then yield.
9. After a body that returned normally: `lock.confirm_held()`, and S must still be the same backend with the same
   snapshot descriptor. A lost L or S is raised (`BackupLockNotHeld` from the lock module, or `SnapshotSessionLost`)
   and is never reacquired or reconnected.

Lock checks are point observations at those boundaries. They do NOT prove that L held the lock continuously in
between, and no monitoring thread is used.

Exit and failures: S's transaction is ended (a rollback on a normal exit; on any exception S is discarded with
`invalidate()` instead), S is closed and its engine disposed, and every step is attempted even if an earlier one
raised. A fresh validated session then confirms with a deadline that S's (PID, `backend_start`) is gone; disposal or
garbage collection is never taken as evidence. A primary exception propagates as the same object, with only one
bounded note if cleanup is unconfirmed. Without one, unconfirmed cleanup raises `SnapshotCleanupError`. A new
`KeyboardInterrupt` or `SystemExit` raised during cleanup propagates, as in the lock module. Every handle accessor
raises `SnapshotHandleClosed` after exit. Messages written by this module are fixed text plus type or field names: no
URL, credential or driver message. Server errors raised by the reads themselves (for example a lock timeout or a
permission error) propagate unchanged as the primary failure.

Phase 9B-6D addition (narrow). `SnapshotHandle.confirm_active()` makes the exit check available to a consumer while the
context is open: `lock.confirm_held()`, then, on S itself, the same PID, snapshot descriptor and transaction start. It
raises `SnapshotHandleClosed` after exit, and `BackupLockNotHeld` or `SnapshotSessionLost` on a loss. The handle also
retains, privately, the exact validated URL and designation S used, so a consumer (the dump) can use the same source and
nothing else. No public accessor exposes them. These checks are point observations, like the lock checks.

What a handle does NOT establish: continuous catalog stability (the fresh-endpoint observation, decision 6, is not
adopted here), complete provenance, archive consistency, a successful capture, or recovery readiness.
"""
from __future__ import annotations

import datetime as _dt
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator

from sqlalchemy import text
from sqlalchemy.engine import URL, Connection, Engine

from src.deployment.backup_artifact import (
    AclEntry,
    BaselineDocument,
    ConstraintEntry,
    FingerprintEntry,
    IndexEntry,
    SequenceDetail,
    SequenceEntry,
    canonical_json_bytes,
    parse_baseline,
)
from src.deployment.backup_capture_lock import (
    NO_SESSION_CREATED,
    NOT_VERIFIED,
    VERIFIED_CLOSED,
    BackupLockSession,
    _build_engine,
    _require_null_pool,
    _type_name,
    _validated_url,
    _verify_cleanup,
)
from src.deployment.backup_capture_preflight import _SEQUENCE_INVENTORY_SQL, CapturePreflight, observe_capture_preflight, v1_policy
from src.exceptions import NapsEdgeError, ValidationError
from src.pipeline.bulk_import_safety import DatabaseTargetDesignation

__all__ = [
    "SNAPSHOT_APPLICATION_NAME",
    "SnapshotSetupError",
    "SnapshotRefusedError",
    "SnapshotSessionLost",
    "SnapshotCleanupError",
    "SnapshotHandleClosed",
    "SnapshotFacts",
    "SnapshotCleanupReport",
    "SnapshotHandle",
    "backup_capture_snapshot",
]

SNAPSHOT_APPLICATION_NAME = "napsedge_backup_snapshot"

_LOCK_TIMEOUT_MS = 5000  # S only; a queued exclusive request makes S's reads fail within this bound instead of waiting
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_SAFE_IDENT = re.compile(r"[a-z_][a-z0-9_]*")

_TRANSACTION_SQL = (
    "SELECT current_setting('transaction_isolation'), current_setting('transaction_read_only'), pid, backend_start, now() "
    "FROM pg_stat_activity WHERE pid = pg_backend_pid()"
)
_EXPORT_SQL = "SELECT pg_export_snapshot()"
_DESCRIPTOR_SQL = "SELECT pg_current_snapshot()::text"
_ALIVE_SQL = "SELECT pg_backend_pid(), pg_current_snapshot()::text, now()"
_NOW_SQL = "SELECT now()"

# The established baseline reads (tests/postgres/pg_restore_compare.py: capture_baseline and its helpers), same SQL and order.
_ACL_SQL = (
    "SELECT c.relname, c.relkind::text, c.relowner::regrole::text, coalesce(c.relacl::text, '') "
    "FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'S') ORDER BY 1"
)
_PUBLIC_TABLES_SQL = "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1"
_FINGERPRINT_SQL = "SELECT count(*), coalesce(md5(string_agg(t::text, '|' ORDER BY t::text)), 'empty') FROM public.\"{table}\" t"
_CONSTRAINT_SQL = (
    "SELECT conrelid::regclass::text, conname, contype::text, convalidated, pg_get_constraintdef(oid) "
    "FROM pg_constraint WHERE connamespace = 'public'::regnamespace ORDER BY 1, 2"
)
_INDEX_SQL = "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' ORDER BY 1, 2"
_SEQUENCES_SQL = "SELECT sequencename, last_value FROM pg_sequences WHERE schemaname = 'public' ORDER BY 1"
_VERSION_SQL = "SELECT version_num FROM alembic_version"
_MAX_ALIAS_SQL = "SELECT max(id) FROM team_aliases"
# C3-C5's direct sequence reads (tests/postgres/test_postgres_backup_sequence_consistency.py read_sequence_details); the inventory is the preflight's.
_SEQUENCE_STATE_SQL = 'SELECT last_value, is_called FROM public."{name}"'
_COLUMN_MAX_SQL = 'SELECT max("{column}") FROM public."{table}"'


class SnapshotSetupError(NapsEdgeError):
    """S could not be opened. Carries the exception TYPE only."""


class SnapshotRefusedError(NapsEdgeError):
    """The source, its identity, or S's transaction is not what the contract requires; nothing usable was yielded. `preflight` holds the facts when available."""

    def __init__(self, message: str, preflight: CapturePreflight | None = None) -> None:
        super().__init__(message)
        self.preflight = preflight


class SnapshotSessionLost(NapsEdgeError):
    """S is no longer the same backend holding the same snapshot. Fixed text plus a type name only."""


class SnapshotCleanupError(NapsEdgeError):
    """Cleanup of S could not be confirmed and no other failure was propagating. `report` holds the facts."""

    def __init__(self, message: str, report: "SnapshotCleanupReport") -> None:
        super().__init__(message)
        self.report = report


class SnapshotHandleClosed(NapsEdgeError):
    """A snapshot handle was used after its context exited."""


@dataclass(frozen=True)
class SnapshotFacts:
    """Read on S itself (the transaction facts) and on L itself (`lock_backend_pid`, through the lock session)."""

    backend_pid: int
    backend_start: _dt.datetime
    transaction_start: _dt.datetime
    isolation: str
    read_only: str
    system_identifier: str
    database_oid: int
    database_name: str
    server_version_num: int
    lock_backend_pid: int


@dataclass(frozen=True)
class SnapshotCleanupReport:
    backend_pid: int | None
    transaction_ended: bool
    connection_discarded: bool
    connection_closed: bool
    engine_disposed: bool
    verification: str
    failures: tuple[str, ...]

    @property
    def confirmed(self) -> bool:
        return not self.failures and self.verification in (VERIFIED_CLOSED, NO_SESSION_CREATED)


class SnapshotHandle:
    """The live snapshot. Every accessor raises `SnapshotHandleClosed` once the context has exited; `cleanup` is set then."""

    __slots__ = ("_open", "_snapshot_id", "_descriptor", "_baseline", "_preflight", "_facts", "_confirm", "_source", "cleanup")

    def __init__(self, snapshot_id: str, descriptor: str, baseline: BaselineDocument, preflight: CapturePreflight, facts: SnapshotFacts, *,
                 confirm: Callable[[], None] | None = None, source: tuple[URL, DatabaseTargetDesignation] | None = None) -> None:
        self._open = True
        self._snapshot_id, self._descriptor, self._baseline, self._preflight, self._facts = snapshot_id, descriptor, baseline, preflight, facts
        self._confirm, self._source = confirm, source
        self.cleanup: SnapshotCleanupReport | None = None

    def confirm_active(self) -> None:
        """L still holds the lock and S is still the same backend, snapshot and transaction, right now. A point observation."""
        self._get(None)
        if self._confirm is None:
            raise SnapshotHandleClosed("the snapshot handle has no active session to confirm")
        self._confirm()

    def _validated_source(self) -> tuple[URL, DatabaseTargetDesignation]:
        """For the dump consumer only: the exact validated URL and designation S used. Rejected after exit."""
        if self._source is None:
            raise SnapshotHandleClosed("the snapshot handle carries no validated source")
        return self._get(self._source)

    def _get(self, value):
        if not self._open:
            raise SnapshotHandleClosed("the snapshot handle was used after its context exited")
        return value

    @property
    def snapshot_id(self) -> str:
        """The exported snapshot identifier; importable only while the context is open."""
        return self._get(self._snapshot_id)

    @property
    def visibility_descriptor(self) -> str:
        return self._get(self._descriptor)

    @property
    def baseline(self) -> BaselineDocument:
        return self._get(self._baseline)

    @property
    def preflight(self) -> CapturePreflight:
        return self._get(self._preflight)

    @property
    def facts(self) -> SnapshotFacts:
        return self._get(self._facts)


def _rows(connection: Connection, sql: str) -> list[tuple]:
    return [tuple(row) for row in connection.execute(text(sql)).fetchall()]


def _ident(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_IDENT.fullmatch(value):
        raise SnapshotRefusedError("an object name outside the supported identifier form was found")
    return value


def _read_baseline(connection: Connection, artifact_id: str) -> BaselineDocument:
    """The established baseline, every read on `connection` (the caller's open transaction), as the production `BaselineDocument`."""
    acls = _rows(connection, _ACL_SQL)
    fingerprints = {
        table: tuple(_rows(connection, _FINGERPRINT_SQL.format(table=_ident(table)))[0])
        for table in (row[0] for row in _rows(connection, _PUBLIC_TABLES_SQL))
    }
    constraints = _rows(connection, _CONSTRAINT_SQL)
    indexes = _rows(connection, _INDEX_SQL)
    sequences = _rows(connection, _SEQUENCES_SQL)
    version = connection.execute(text(_VERSION_SQL)).scalar()
    max_alias_id = connection.execute(text(_MAX_ALIAS_SQL)).scalar()
    details = []
    for name, table, column, data_type, increment, minimum, maximum, cache, cycle in _rows(connection, _SEQUENCE_INVENTORY_SQL):
        last_value, is_called = connection.execute(text(_SEQUENCE_STATE_SQL.format(name=_ident(name)))).one()
        owned_by, column_max = None, None
        if table is not None and column is not None:
            owned_by = (_ident(table), _ident(column))
            column_max = connection.execute(text(_COLUMN_MAX_SQL.format(column=column, table=table))).scalar()
        details.append(SequenceDetail(name=name, owned_by=owned_by, data_type=data_type, increment=increment, minimum=minimum, maximum=maximum,
                                      cache=cache, cycle=cycle, last_value=last_value, is_called=is_called, column_max=column_max))
    document = BaselineDocument(
        artifact_id=artifact_id,
        captured_utc=_dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        fingerprints=tuple(FingerprintEntry(table, rows, md5) for table, (rows, md5) in sorted(fingerprints.items())),
        constraints=tuple(ConstraintEntry(*row) for row in constraints),
        indexes=tuple(IndexEntry(*row) for row in indexes),
        acls=tuple(AclEntry(*row) for row in acls),
        sequences=tuple(SequenceEntry(*row) for row in sequences),
        version=version,
        max_alias_id=max_alias_id,
        sequence_details=tuple(details),
    )
    if parse_baseline(canonical_json_bytes(document.to_document())) != document:  # the contract's own strict validation, applied to what was read
        raise SnapshotRefusedError("the baseline read does not round-trip through the baseline contract")
    return document


def _require_same_source(lock: BackupLockSession, preflight: CapturePreflight) -> None:
    if lock.system_identifier is None or preflight.system_identifier is None:
        raise SnapshotRefusedError("the cluster system_identifier is not available on both the lock and the snapshot session")
    for field, on_lock, on_snapshot in (
        ("system_identifier", lock.system_identifier, preflight.system_identifier),
        ("database_oid", lock.database_oid, preflight.database_oid),
        ("database_name", lock.verified_target.database_name, preflight.verified_target.database_name),
    ):
        if on_lock is None or on_lock != on_snapshot:
            raise SnapshotRefusedError(f"the snapshot session and the lock session address different sources ({field})")


@contextmanager
def backup_capture_snapshot(lock: BackupLockSession, database_url: URL | str, designation: DatabaseTargetDesignation, *,
                            expected_alembic_head: str, artifact_id: str,
                            _engine_factory: Callable[[URL, str], Engine] | None = None) -> Iterator[SnapshotHandle]:
    """See the module docstring. `_engine_factory` is a test seam; whatever it returns must be a NullPool engine."""
    if not isinstance(lock, BackupLockSession):
        raise ValidationError("lock must be the BackupLockSession of an active backup_capture_lock context")
    if not isinstance(designation, DatabaseTargetDesignation):
        raise ValidationError("designation must be a DatabaseTargetDesignation")
    if not isinstance(artifact_id, str) or not _UUID4.fullmatch(artifact_id):
        raise ValidationError("artifact_id must be a lower-case UUID4")
    policy = v1_policy(designation, expected_alembic_head)
    url = _validated_url(database_url)
    lock.confirm_held()
    factory = _engine_factory or _build_engine
    engine = factory(url, SNAPSHOT_APPLICATION_NAME)
    try:
        _require_null_pool(engine)
    except BaseException:
        engine.dispose()
        raise

    connection: Connection | None = None
    pid: int | None = None
    started = None
    transaction_open = False
    handle: SnapshotHandle | None = None
    primary: BaseException | None = None
    try:
        try:
            connection = engine.connect()
        except Exception as exc:  # noqa: BLE001 -- the driver's message can carry host and user names
            raise SnapshotSetupError(f"opening the snapshot session failed ({_type_name(exc)})") from None
        connection.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
        connection.begin()
        transaction_open = True
        connection.execute(text(f"SET LOCAL lock_timeout = {_LOCK_TIMEOUT_MS}"))
        isolation, read_only, pid, started, transaction_start = connection.execute(text(_TRANSACTION_SQL)).one()
        if isolation != "repeatable read" or read_only != "on":
            raise SnapshotRefusedError("the snapshot session is not in a REPEATABLE READ, READ ONLY transaction")
        if pid == lock.backend_pid:
            raise SnapshotRefusedError("the snapshot session must be a different backend from the lock session")

        preflight = observe_capture_preflight(connection, policy)
        if not preflight.accepted:
            raise SnapshotRefusedError("the source is not supported (the capture preflight was not accepted)", preflight)
        _require_same_source(lock, preflight)

        lock.confirm_held()
        snapshot_id = connection.execute(text(_EXPORT_SQL)).scalar_one()
        descriptor = connection.execute(text(_DESCRIPTOR_SQL)).scalar_one()
        baseline = _read_baseline(connection, artifact_id)
        if connection.execute(text(_NOW_SQL)).scalar_one() != transaction_start:
            raise SnapshotRefusedError("the baseline reads did not stay in the snapshot transaction")
        lock.confirm_held()

        facts = SnapshotFacts(
            backend_pid=pid, backend_start=started, transaction_start=transaction_start, isolation=isolation, read_only=read_only,
            system_identifier=preflight.system_identifier, database_oid=preflight.database_oid,
            database_name=preflight.verified_target.database_name, server_version_num=preflight.server_version_num,
            lock_backend_pid=lock.backend_pid,
        )
        def confirm() -> None:
            lock.confirm_held()
            try:
                alive_pid, alive_descriptor, alive_start = connection.execute(text(_ALIVE_SQL)).one()
            except Exception as exc:  # noqa: BLE001 -- S's loss is reported, never repaired or replaced
                raise SnapshotSessionLost(f"the snapshot session could not be confirmed ({_type_name(exc)})") from None
            if (alive_pid, alive_descriptor, alive_start) != (pid, descriptor, transaction_start):
                raise SnapshotSessionLost("the snapshot session is no longer the same backend, snapshot and transaction")

        handle = SnapshotHandle(snapshot_id, descriptor, baseline, preflight, facts, confirm=confirm, source=(url, designation))
        yield handle

        confirm()  # the same check as confirm_active(), before a successful exit
    except BaseException as exc:
        primary = exc
        raise
    finally:
        if handle is not None:
            handle._open = False
            handle._confirm = handle._source = None  # nothing reachable through the handle after exit
        failures: list[str] = []
        interrupt: BaseException | None = None
        ended = discarded = closed = disposed = False

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
            if primary is None and transaction_open:
                ended = attempt("rollback", connection.rollback)  # ends S's read-only transaction; nothing to keep
            elif primary is not None:
                discarded = attempt("discard", connection.invalidate)
                ended = discarded  # the DBAPI connection, and with it the transaction, is closed without reuse
            closed = attempt("close", connection.close)
        disposed = attempt("dispose", engine.dispose)

        if connection is None:
            verification = NO_SESSION_CREATED
        elif pid is None or interrupt is not None:
            verification = NOT_VERIFIED
        else:
            try:
                verification, _, verify_failures = _verify_cleanup(factory, url, designation, pid, started)
                failures += verify_failures
            except BaseException as exc:
                verification = NOT_VERIFIED
                failures.append(f"verify:{_type_name(exc)}")
                interrupt = interrupt or exc

        report = SnapshotCleanupReport(backend_pid=pid, transaction_ended=ended, connection_discarded=discarded, connection_closed=closed,
                                       engine_disposed=disposed, verification=verification, failures=tuple(failures))
        if handle is not None:
            handle.cleanup = report
        if interrupt is not None:
            raise interrupt
        if not report.confirmed:
            message = f"snapshot session cleanup was not confirmed: verification={verification}; failed steps={','.join(failures) or 'none'}"
            if primary is not None:
                primary.add_note(message)
            else:
                raise SnapshotCleanupError(message, report)
