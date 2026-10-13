"""
Phase 9B-5C4: disposable characterization of overlap (C9) and quiescence (C10), specification section 7 cases C9 and C10 ONLY.

Scenario-level acceptance criteria (recorded BEFORE implementation)
------------------------------------------------------------------
C9  overlap and lock lifecycle
  9.1  Two distinct physical backup-role sessions (two Engines, two backend PIDs) against a verified disposable database; the PIDs are recorded.
  9.2  The EXISTING target verification, key derivation and `bulk_import_advisory_lock` are reused unchanged (no key logic, namespace or production code here).
  9.3  While the first session holds the lock the second refuses IMMEDIATELY with the recorded job outcome `SKIPPED:overlap` (not a pytest skip).
  9.4  The refused run, through the real capture flow, never reaches snapshot export, baseline capture, `pg_dump`, restore or finalization (flow record, the refused
       session's own SQL statements, its connection count, and the absence of any command, destination database or artifact marker).
  9.5  After normal release a fresh session acquires.
  9.6  Exception-path release and physical-session loss are exercised; residual locks are read from PostgreSQL (`pg_locks`), subsequent acquisition is verified, and a
       primary failure is preserved BY THE PRODUCTION CONTEXT MANAGER ITSELF (corrected after this unit's first delivery, which had observed the production manager letting a failed
       release replace the primary and had compensated in the harness; the harness now consumes the shared behavior and carries no repair policy of its own).
  9.7  A session-level lock surviving `rollback()`, and `Connection.close()` returning a live backend to the pool, are demonstrated; physical-session cleanup is
       VERIFIED from PostgreSQL after `Engine.dispose()`, never assumed.
  9.8  Whether the reused key also excludes controlled imports is RECORDED in both directions, and kept distinct from backup-overlap protection; no policy is selected.
  9.9  The backup role's existing grants suffice: nothing is granted to it (no monitoring, no write).
C10 quiescence observations and their limits (the quiescent procedure: advisory lock held, endpoint counts, baseline, a plain dump with NO snapshot, restore, strict equality)
  10.A No-writer control: strict data AND sequence equality, lock held across the window.
  10.B A short-lived NON-COOPERATING writer commits a sequence-generated insert after the baseline and physically disconnects before the end sample. Commit and sequence
       advancement are proven independently of thread completion; both endpoint counts are zero (the write is MISSED); the strict comparator shows the data and sequence
       differences. Zero counts and equal endpoints never assert continuous quiescence.
  10.C A writer retained through the end sample is COUNTED, and its committed change is separately proven.
  10.D A connected write-capable session that writes nothing is counted: a positive count establishes presence, not a write.
  The count is defined exactly (see `ForeignWriterSampler`), observed by the backup role in a FRESH transaction per sample, compared with independent administrative evidence, and
  restricted or NULL activity fields are never treated as proof of inactivity. Characterization success is kept separate from safety-requirement success: the safety
  requirements ("zero endpoint counts imply nothing wrote"; "a positive count implies a write") are STRICT xfail tests.

Nothing here validates production capture, selects a lock policy, or establishes database consistency for an artifact. No production code, grant, schema or dependency changes.
"""
from __future__ import annotations

import contextlib
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

from src.exceptions import AdvisoryLockUnavailableError
from src.pipeline.bulk_import_safety import (
    RELEASE_FAILED_NOTE,
    DatabaseTargetDesignation,
    build_expected_acknowledgement,
    bulk_import_advisory_lock,
    compute_advisory_lock_key,
    verify_database_target,
)
from tests.postgres.pg_restore_compare import all_fingerprints, capture_baseline, compare_to_baseline, keyed_diff, rows, scalar
from tests.postgres.test_postgres_backup_catalog_consistency import backup_table_privileges, wait_until
from tests.postgres.test_postgres_backup_sequence_consistency import read_sequence_details
from tests.postgres.test_postgres_backup_snapshot_consistency import (
    BARRIER_TIMEOUT_S,
    CaptureStepFailed,
    CaseLedger,
    Harness,
    harness,  # noqa: F401  (module-scoped fixture: ONE disposable cluster for this module)
    owned_case,
    prepare_case_workspace,
    restore_into_fresh_database,
    run_pg_dump,
)
from tests.postgres.test_postgres_roles_and_restore import MIGRATOR_ROLE, CommandResult
from tests.postgres.test_postgres_tls_backup_restore import BACKUP_ROLE, isolated_database_environment  # noqa: F401  (autouse module fixture)

pytestmark = pytest.mark.postgres

VALIDATED_CASES = ("C9", "C10")
SKIPPED_OVERLAP = "SKIPPED:overlap"  # a recorded JOB outcome of the harness; never a pytest skip
PROBE_ALIAS = "Quiescence Probe Alias"  # the synthetic row the non-cooperating writer inserts (its id comes from the sequence)
SESSION_WAIT_S = 15


def designation_for(database: str) -> DatabaseTargetDesignation:
    return DatabaseTargetDesignation(expected_database_name=database, acknowledgement=build_expected_acknowledgement(database))


# ==========================================================================
# physical sessions, and what PostgreSQL says about them (read by the admin session of the cluster's ADMIN database)
# ==========================================================================
class LockSession:
    """
    One physical session: its OWN Engine (and so its own pool), one Connection, its backend pid, and a record of every statement it sent and of the
    connections its Engine opened. `role` is "backup" (the existing backup role) or "migrator" (the controlled import's role).
    """

    def __init__(self, cluster, database: str, ledger: CaseLedger, label: str, role: str = "backup") -> None:
        url = cluster.backup_url(database) if role == "backup" else cluster.migrator_url(database)
        self.label, self.role = label, role
        self.engine = create_engine(url, pool_size=1, max_overflow=0, pool_timeout=15, connect_args={"connect_timeout": 15, "application_name": f"napsedge_c9_{label}"})
        self.statements: list[str] = []
        self.connects: list[int] = []
        event.listen(self.engine, "before_cursor_execute", lambda conn, cursor, statement, parameters, context, executemany: self.statements.append(" ".join(statement.split())))
        event.listen(self.engine, "connect", lambda dbapi_connection, record: self.connects.append(1))
        ledger.engines.append(self.engine)
        self.connection: Connection = self.engine.connect()
        ledger.connections.append(self.connection)
        self.pid: int = scalar(self.connection, "SELECT pg_backend_pid()")

    def end(self) -> None:
        """Physical end of the session: `Connection.close()` returns the live backend to the pool, and only `Engine.dispose()` then closes it (dispose alone never closes a checked-out connection)."""
        with contextlib.suppress(Exception):
            self.connection.close()
        self.engine.dispose()


def advisory_lock_rows(admin: Engine, key: int, database: str) -> list[tuple]:
    """(pid, granted) for every advisory lock on `key` in `database`, from pg_locks (a bigint key is stored as classid = high 32 bits, objid = low 32 bits, objsubid = 1)."""
    return rows(admin, "SELECT l.pid, l.granted FROM pg_locks l WHERE l.locktype = 'advisory' AND l.classid::bigint = :c AND l.objid::bigint = :o AND l.objsubid = 1 "
                       "AND l.database = (SELECT oid FROM pg_database WHERE datname = :d) ORDER BY l.pid", c=key >> 32, o=key & 0xFFFFFFFF, d=database)


def pids_alive(admin: Engine, pids) -> list[int]:
    found = rows(admin, "SELECT pid FROM pg_stat_activity WHERE pid = ANY(CAST(:p AS int[])) ORDER BY pid", p=list(pids))
    return [r[0] for r in found]


def confirm_gone(admin: Engine, pids, timeout: float = SESSION_WAIT_S) -> bool:
    """Bounded CONDITION polling: true once none of `pids` exists any more in pg_stat_activity (termination and disconnection are asynchronous)."""
    return wait_until(lambda: not pids_alive(admin, pids), timeout)


def terminate_and_confirm(admin: Engine, pid: int) -> None:
    if not scalar(admin, "SELECT pg_terminate_backend(:p)", p=pid) or not confirm_gone(admin, [pid]):
        raise AssertionError(f"backend {pid} was not removed")


# ==========================================================================
# the harness guard: verify, acquire non-blocking, run, release; refusal is the job outcome SKIPPED:overlap
# ==========================================================================
def run_guarded(session: LockSession, designation: DatabaseTargetDesignation, body: Callable[[int], None]) -> str:
    """
    The harness path of a capture attempt. The target is verified, the EXISTING advisory lock is taken without blocking, and `body(key)` runs only if it was acquired.
    A refusal is the recorded outcome `SKIPPED:overlap` and `body` is never called. Exception preservation is NOT done here: `bulk_import_advisory_lock` itself guarantees that a
    failed release never replaces a primary failure from `body`, so this guard only classifies the refusal (a refusal raised after `body` started is not an overlap and propagates).
    """
    target = verify_database_target(session.connection, designation)
    entered = False
    try:
        with bulk_import_advisory_lock(session.connection, target) as key:
            entered = True
            body(key)
        return "ACQUIRED"
    except AdvisoryLockUnavailableError:
        if not entered:
            return SKIPPED_OVERLAP
        raise


# ==========================================================================
# C10: the foreign-writer count, defined exactly, and the foreign sessions it is about
# ==========================================================================
class ForeignWriterSampler:
    """
    The candidate endpoint count `foreign_write_sessions`, observed AS THE BACKUP ROLE. It counts exactly: sessions (any `backend_type`) connected to the CASE database, other than
    the observer itself, whose role is one of the declared write-capable roles (`roles`: the migrator and the cluster's superuser). It does NOT count the backup role, the read-only
    runtime role, any other role, any other database, and nothing the backup role cannot see. Each sample is a FRESH transaction, because `pg_stat_activity` is cached for the
    duration of a transaction. Only identity columns are used (`datname`, `usename`, `pid`): for other roles' sessions the backup role sees `backend_type` and `state` as NULL and
    `query` as '<insufficient privilege>' -- OBSERVED: filtering on `backend_type` silently counted zero -- and such fields are never evidence of inactivity (the sample records them).
    """

    def __init__(self, engine: Engine, database: str, roles: tuple[str, ...]) -> None:
        self.engine, self.database, self.roles = engine, database, roles

    def sample_with(self, conn: Connection) -> dict:
        found = rows(conn, "SELECT pid, usename, state, query, backend_type FROM pg_stat_activity WHERE datname = :d AND pid <> pg_backend_pid() "
                           "AND usename = ANY(CAST(:r AS text[])) ORDER BY pid", d=self.database, r=list(self.roles))
        return {"count": len(found), "pids": tuple(r[0] for r in found), "roles": tuple(sorted({r[1] for r in found})),
                "restricted_fields_null": (all(r[2] is None and r[4] is None and r[3] == "<insufficient privilege>" for r in found) if found else None)}

    def sample(self) -> dict:
        with self.engine.connect() as conn:  # a FRESH transaction per sample
            return self.sample_with(conn)


def admin_sample(admin: Engine, database: str, roles: tuple[str, ...]) -> dict:
    """Independent administrative evidence for the same instant: the same selection plus the fields only an administrator sees. Never a dependency of the candidate count."""
    found = rows(admin, "SELECT pid, usename, state, backend_xid IS NOT NULL, xact_start IS NOT NULL, application_name FROM pg_stat_activity WHERE datname = :d "
                        "AND usename = ANY(CAST(:r AS text[])) ORDER BY pid", d=database, r=list(roles))
    return {"pids": tuple(r[0] for r in found), "states": tuple(r[2] for r in found), "has_xid": tuple(r[3] for r in found), "in_transaction": tuple(r[4] for r in found),
            "applications": tuple(r[5] for r in found)}


class ForeignSession:
    """A session that is NOT the backup role and does NOT take the advisory lock (a non-cooperating writer, as an Alembic run or an operator session would be)."""

    def __init__(self, cluster, database: str, ledger: CaseLedger, label: str) -> None:
        self.engine = create_engine(cluster.migrator_url(database), pool_size=1, max_overflow=0, pool_timeout=15,
                                    connect_args={"connect_timeout": 15, "application_name": f"napsedge_c10_{label}"})
        ledger.engines.append(self.engine)
        self.connection: Connection | None = self.engine.connect()
        ledger.connections.append(self.connection)
        self.pid: int = scalar(self.connection, "SELECT pg_backend_pid()")
        self.connection.commit()
        self.written_id: int | None = None
        self.committed = False

    def write(self) -> int:
        """One sequence-generated insert, committed. The id comes from the sequence's default, never from the caller."""
        conn = self.connection
        conn.execute(text("SET statement_timeout = '15s'"))
        self.written_id = conn.execute(text("INSERT INTO team_aliases (team_id, source, raw_value, normalized_value) VALUES (910002, 'odds', :r, :n) RETURNING id"),
                                       {"r": PROBE_ALIAS, "n": PROBE_ALIAS.lower()}).scalar_one()
        conn.commit()
        self.committed = True
        return self.written_id

    def disconnect(self) -> None:
        """A PHYSICAL disconnect: the connection is closed and the Engine disposed, which closes the pooled backend itself."""
        if self.connection is not None:
            with contextlib.suppress(Exception):
                self.connection.close()
            self.connection = None
        self.engine.dispose()


def fresh_state(cluster, database: str, ledger: CaseLedger) -> dict:
    """Independent proof, from a NEW backup-role transaction: the probe row (if any) and the sequence state; never taken from a writer thread or session."""
    engine = create_engine(cluster.backup_url(database), pool_size=1, max_overflow=0, pool_timeout=15, connect_args={"connect_timeout": 15, "application_name": "napsedge_c10_fresh_reader"})
    ledger.engines.append(engine)
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT id FROM team_aliases WHERE raw_value = :r"), {"r": PROBE_ALIAS}).all()
            last_value, is_called = conn.execute(text("SELECT last_value, is_called FROM public.team_aliases_id_seq")).one()
        return {"probe_ids": [r[0] for r in row], "sequence": (last_value, is_called)}
    finally:
        engine.dispose()


# ==========================================================================
# C9: the lock lifecycle (one disposable database; every experiment states what PostgreSQL itself reports)
# ==========================================================================
@dataclass
class LifecycleRecord:
    e: dict = field(default_factory=dict)
    verification: dict = field(default_factory=dict)


def _acquire_once(session: LockSession, designation, admin: Engine, key: int, database: str) -> dict:
    """A full guarded acquisition by `session`; reports the outcome and the lock rows seen from PostgreSQL while it was held and after it was released."""
    during: list = []
    outcome = run_guarded(session, designation, lambda k: during.extend(advisory_lock_rows(admin, key, database)))
    return {"outcome": outcome, "pid": session.pid, "rows_during": during, "rows_after": advisory_lock_rows(admin, key, database)}


def run_lifecycle_case(harness_: Harness, ledger: CaseLedger) -> LifecycleRecord:
    cluster = harness_.cluster
    rec = LifecycleRecord()
    e = rec.e
    workspace = prepare_case_workspace(cluster, ledger)
    source = workspace.source_database
    admin = cluster.admin_engine()  # the cluster's ADMIN database: instrumentation that is never a session of the case database
    admin_case = cluster.admin_engine(source)
    ledger.engines += [admin, admin_case]
    designation = designation_for(source)
    sessions: list[LockSession] = []
    sampling_engine = create_engine(cluster.backup_url(source), pool_size=1, max_overflow=0, pool_timeout=15, connect_args={"connect_timeout": 15, "application_name": "napsedge_c9_sampler"})
    ledger.engines.append(sampling_engine)  # observation of OTHER sessions uses its own engine: a session's single pooled connection stays that session's

    def session(label: str, role: str = "backup") -> LockSession:
        made = LockSession(cluster, source, ledger, label, role)
        sessions.append(made)
        return made

    # ---- 9.1 / 9.2: two distinct physical sessions, the existing verification and key derivation --------------------------------------------------
    first, second = session("first"), session("second")
    target = verify_database_target(first.connection, designation)
    key = compute_advisory_lock_key(target)
    e["sessions"] = {"pid_first": first.pid, "pid_second": second.pid, "distinct": first.pid != second.pid, "users": (scalar(first.connection, "SELECT current_user"), scalar(second.connection, "SELECT current_user")),
                     "separate_engines": first.engine is not second.engine}
    e["key"] = {"database": target.database_name, "key": key, "classid": key >> 32, "objid": key & 0xFFFFFFFF, "same_key_from_the_second_session": compute_advisory_lock_key(verify_database_target(second.connection, designation)) == key}

    # ---- 9.3: refusal while held ----------------------------------------------------------------------------------------------------------------
    entered: list[int] = []
    with bulk_import_advisory_lock(first.connection, target):
        held_rows = advisory_lock_rows(admin, key, source)
        started = time.monotonic()
        outcome = run_guarded(second, designation, lambda k: entered.append(k))
        elapsed = round(time.monotonic() - started, 3)
    e["refusal"] = {"outcome": outcome, "elapsed_s": elapsed, "body_entered": bool(entered), "rows_while_held": held_rows, "second_statements": list(second.statements)}
    # ---- 9.5: after normal release a FRESH session acquires --------------------------------------------------------------------------------------
    e["after_release"] = {"rows_right_after_release": advisory_lock_rows(admin, key, source), "fresh_session": _acquire_once(session("third"), designation, admin, key, source)}

    # ---- 9.6: exception-path release, with the primary preserved ---------------------------------------------------------------------------------
    primary, fourth = RuntimeError("primary failure"), session("fourth")
    caught = None
    try:
        run_guarded(fourth, designation, lambda k: (_ for _ in ()).throw(primary))
    except BaseException as exc:  # noqa: BLE001
        caught = exc
    e["exception_path"] = {"propagated_is_primary": caught is primary, "notes": list(getattr(caught, "__notes__", [])), "rows_after": advisory_lock_rows(admin, key, source),
                           "reacquire": _acquire_once(session("fifth"), designation, admin, key, source)}

    # ---- 9.6: physical session loss while holding the lock ---------------------------------------------------------------------------------------
    sixth = session("sixth")
    manager = bulk_import_advisory_lock(sixth.connection, verify_database_target(sixth.connection, designation))
    manager.__enter__()
    rows_before_loss = advisory_lock_rows(admin, key, source)
    terminate_and_confirm(admin, sixth.pid)
    rows_after_loss = advisory_lock_rows(admin, key, source)
    exit_error = None
    try:
        manager.__exit__(None, None, None)
    except BaseException as exc:  # noqa: BLE001
        exit_error = type(exc).__name__
    e["session_loss"] = {"rows_before": rows_before_loss, "rows_after": rows_after_loss, "backend_gone": not pids_alive(admin, [sixth.pid]), "release_after_loss_raised": exit_error,
                         "reacquire": _acquire_once(session("seventh"), designation, admin, key, source)}

    # ---- 9.6: a primary failure when the session is lost AND the release then fails: the PRODUCTION manager preserves the primary (directly, and through the guard) ----
    eighth, ninth = session("eighth"), session("ninth")
    primary_h = RuntimeError("primary after session loss")
    caught_h = None

    def lose_session_then_fail(k: int) -> None:
        terminate_and_confirm(admin, eighth.pid)
        raise primary_h

    try:
        run_guarded(eighth, designation, lose_session_then_fail)
    except BaseException as exc:  # noqa: BLE001
        caught_h = exc
    primary_p, observed_p = RuntimeError("primary inside the production context manager"), None
    try:
        with bulk_import_advisory_lock(ninth.connection, verify_database_target(ninth.connection, designation)):
            terminate_and_confirm(admin, ninth.pid)
            raise primary_p
    except BaseException as exc:  # noqa: BLE001
        observed_p = exc
    e["primary_with_lost_session"] = {"guard_propagated_is_primary": caught_h is primary_h, "guard_notes": list(getattr(caught_h, "__notes__", [])),
                                      "production_manager_propagated_type": type(observed_p).__name__, "production_manager_propagated_is_primary": observed_p is primary_p,
                                      "production_manager_notes": list(getattr(observed_p, "__notes__", [])), "production_manager_cause_is_none": getattr(observed_p, "__cause__", 0) is None,
                                      "rows_after": advisory_lock_rows(admin, key, source), "reacquire": _acquire_once(session("tenth"), designation, admin, key, source)}

    # ---- 9.7: a session-level lock survives rollback; Connection.close() returns a LIVE backend to the pool; only dispose() ends the physical session ----------
    leaker = session("leaker")
    acquired = scalar(leaker.connection, "SELECT pg_try_advisory_lock(:k)", k=key)  # a caller that takes the lock and then forgets to release it
    rows_acquired = advisory_lock_rows(admin, key, source)
    leaker.connection.rollback()
    rows_after_rollback = advisory_lock_rows(admin, key, source)
    leaker.connection.close()
    checked_out, alive_after_close = leaker.engine.pool.checkedout(), pids_alive(admin, [leaker.pid])
    rows_after_close = advisory_lock_rows(admin, key, source)
    refused_while_pooled = _acquire_once(session("eleventh"), designation, admin, key, source)
    leaker.engine.dispose()
    gone_after_dispose = confirm_gone(admin, [leaker.pid])
    checked_out_holder = session("checked_out_holder")
    scalar(checked_out_holder.connection, "SELECT pg_try_advisory_lock(:k)", k=key)
    checked_out_holder.engine.dispose()  # the connection is still checked out: dispose() does not close it
    alive_after_dispose_alone = pids_alive(admin, [checked_out_holder.pid]) == [checked_out_holder.pid]
    rows_after_dispose_alone = advisory_lock_rows(admin, key, source)
    refused_behind_checked_out = _acquire_once(session("thirteenth"), designation, admin, key, source)["outcome"]
    checked_out_holder.end()
    gone_after_close_and_dispose = confirm_gone(admin, [checked_out_holder.pid])
    e["pool_and_rollback"] = {"acquired": acquired, "rows_acquired": rows_acquired, "rows_after_rollback": rows_after_rollback, "pool_checked_out_after_close": checked_out,
                              "backend_alive_after_close": alive_after_close == [leaker.pid], "rows_after_close": rows_after_close, "second_session_outcome_while_pooled": refused_while_pooled["outcome"],
                              "backend_gone_after_dispose": gone_after_dispose, "rows_after_dispose": advisory_lock_rows(admin, key, source),
                              "checked_out": {"backend_alive_after_dispose_alone": alive_after_dispose_alone, "rows_after_dispose_alone": rows_after_dispose_alone,
                                              "second_session_outcome": refused_behind_checked_out, "gone_after_close_and_dispose": gone_after_close_and_dispose, "pid": checked_out_holder.pid},
                              "reacquire_after_dispose": _acquire_once(session("twelfth"), designation, admin, key, source), "leaker_pid": leaker.pid}

    # ---- 9.8: does the reused key also exclude controlled imports? (recorded in both directions; no policy) -------------------------------------------------
    backup_holder, import_session, backup_attempt = session("backup_holder"), session("import_role", "migrator"), session("backup_attempt")
    import_target = verify_database_target(import_session.connection, designation)
    with bulk_import_advisory_lock(backup_holder.connection, target):
        import_refused = run_guarded(import_session, designation, lambda k: None)
    with bulk_import_advisory_lock(import_session.connection, import_target):
        backup_refused = run_guarded(backup_attempt, designation, lambda k: None)
    e["import_exclusion"] = {"same_key": compute_advisory_lock_key(import_target) == key, "import_role": MIGRATOR_ROLE, "import_refused_while_backup_holds": import_refused,
                             "backup_refused_while_import_holds": backup_refused, "rows_after": advisory_lock_rows(admin, key, source)}

    # ---- 9.9: the existing grants suffice; nothing is added ------------------------------------------------------------------------------------------
    privileges = backup_table_privileges(admin_case)
    visible_to_backup = ForeignWriterSampler(sampling_engine, source, (MIGRATOR_ROLE,))
    probe = ForeignSession(cluster, source, ledger, "grants_probe")
    seen = visible_to_backup.sample()
    probe.disconnect()
    e["grants"] = {"select_only": all(p == ("SELECT",) for p in privileges.values()), "tables": len(privileges),
                   "can_execute_advisory_functions": [scalar(admin_case, "SELECT has_function_privilege(:r, :f, 'EXECUTE')", r=BACKUP_ROLE, f=f) for f in ("pg_try_advisory_lock(bigint)", "pg_advisory_unlock(bigint)")],
                   "member_of_monitoring_roles": [scalar(admin_case, "SELECT pg_has_role(:r, :m, 'MEMBER')", r=BACKUP_ROLE, m=m) for m in ("pg_monitor", "pg_read_all_stats", "pg_write_all_data")],
                   "foreign_session_fields_null_for_backup_role": seen["restricted_fields_null"], "foreign_session_seen_by_identity": seen["count"]}

    # ---- C10 support: pg_stat_activity is cached per transaction; a fresh transaction (or an explicit clear) is what observes a new session -----------------
    sampler = ForeignWriterSampler(sampling_engine, source, (MIGRATOR_ROLE,))
    staleness_engine = create_engine(cluster.backup_url(source), pool_size=1, max_overflow=0, pool_timeout=15, connect_args={"connect_timeout": 15, "application_name": "napsedge_c10_long_transaction"})
    ledger.engines.append(staleness_engine)
    held = staleness_engine.connect()  # one long-lived transaction, as a snapshot owner's would be
    stale_before = sampler.sample_with(held)["count"]
    late = ForeignSession(cluster, source, ledger, "sampling_probe")
    stale_after_connect = sampler.sample_with(held)["count"]
    held.execute(text("SELECT pg_stat_clear_snapshot()"))
    after_clear = sampler.sample_with(held)["count"]
    held.rollback()
    held.close()
    fresh_count = sampler.sample()["count"]
    late.disconnect()
    e["sampling"] = {"in_one_transaction_before": stale_before, "in_the_same_transaction_after_a_session_connected": stale_after_connect,
                     "after_pg_stat_clear_snapshot": after_clear, "fresh_transaction": fresh_count, "session_pid": late.pid}

    # ---- 9.7: physical-session cleanup is VERIFIED from PostgreSQL after dispose, never assumed -------------------------------------------------------------
    for made in sessions:
        made.end()
    pids = [made.pid for made in sessions]
    e["physical_cleanup"] = {"sessions_opened": len(pids), "gone_after_dispose": confirm_gone(admin, pids), "rows_after": advisory_lock_rows(admin, key, source)}
    return rec


# ==========================================================================
# the capture flow: C9's real-flow refusal and C10's four quiescence scenarios (the quiescent procedure; NO exported snapshot anywhere)
# ==========================================================================
@dataclass(frozen=True)
class FlowScenario:
    name: str
    case: str
    holder_first: bool = False  # C9: another backup-role session holds the lock when the capture is attempted
    foreign: str = "none"  # "none" | "short_lived_writer" | "retained_writer" | "idle_connected_session"


FLOW_SCENARIOS = {
    "c9_refused_while_held_then_completes": FlowScenario("c9_refused_while_held_then_completes", "C9", holder_first=True),
    "c10_a_no_writer_control": FlowScenario("c10_a_no_writer_control", "C10"),
    "c10_b_short_lived_noncooperating_writer": FlowScenario("c10_b_short_lived_noncooperating_writer", "C10", foreign="short_lived_writer"),
    "c10_c_writer_retained_through_the_end_sample": FlowScenario("c10_c_writer_retained_through_the_end_sample", "C10", foreign="retained_writer"),
    "c10_d_connected_write_capable_session_that_writes_nothing": FlowScenario("c10_d_connected_write_capable_session_that_writes_nothing", "C10", foreign="idle_connected_session"),
}


@dataclass
class FlowRecord:
    scenario: str
    case: str
    actions: list = field(default_factory=list)
    commands: list = field(default_factory=list)
    source_database: str = ""
    key: int = 0
    holder: dict = field(default_factory=dict)
    refused: dict = field(default_factory=dict)
    capture_outcome: str = ""
    lock_pid: int = 0
    lock_rows: dict = field(default_factory=dict)
    samples: dict = field(default_factory=dict)
    admin_samples: dict = field(default_factory=dict)
    foreign: dict = field(default_factory=dict)
    proofs: dict = field(default_factory=dict)
    baseline_details: tuple = ()
    restored_details: tuple = ()
    comparison: object = None
    sequence_diff: dict = field(default_factory=dict)
    dump: CommandResult | None = None
    destination_database: str | None = None
    finalization_invoked: bool = False
    databases_before: list = field(default_factory=list)
    databases_after: list = field(default_factory=list)
    physical_cleanup: dict = field(default_factory=dict)
    artifact_listing: list = field(default_factory=list)
    verification: dict = field(default_factory=dict)


def _case_databases(admin: Engine) -> list[str]:
    return [r[0] for r in rows(admin, "SELECT datname FROM pg_database WHERE datname LIKE 'napsedge_pg_test_%' ORDER BY 1")]


def run_flow_case(harness_: Harness, scenario: FlowScenario, ledger: CaseLedger) -> FlowRecord:
    cluster = harness_.cluster
    rec = FlowRecord(scenario.name, scenario.case)
    log = rec.actions.append
    workspace = prepare_case_workspace(cluster, ledger)
    source = rec.source_database = workspace.source_database
    log("source_provisioned")
    admin = cluster.admin_engine()  # the ADMIN database: instrumentation sessions never connect to the case database, so they cannot distort the endpoint counts
    ledger.engines.append(admin)
    designation = designation_for(source)
    roles = (MIGRATOR_ROLE, cluster.admin_url.username)  # the declared write-capable roles of the count
    sampler_engine = create_engine(cluster.backup_url(source), pool_size=1, max_overflow=0, pool_timeout=15, connect_args={"connect_timeout": 15, "application_name": "napsedge_c10_sampler"})
    ledger.engines.append(sampler_engine)
    sampler = ForeignWriterSampler(sampler_engine, source, roles)
    sessions: list[LockSession] = []
    foreign_sessions: list[ForeignSession] = []
    rec.databases_before = _case_databases(admin)

    def lock_session(label: str) -> LockSession:
        made = LockSession(cluster, source, ledger, label)
        sessions.append(made)
        return made

    def take(label: str) -> None:
        rec.samples[label] = sampler.sample()
        rec.admin_samples[label] = admin_sample(admin, source, roles)
        rec.lock_rows[label] = advisory_lock_rows(admin, rec.key, source)

    def body(key: int) -> None:
        """The real capture body of the QUIESCENT procedure: counts, baseline, (foreign actions), a plain dump, counts, restore, strict comparison."""
        rec.key = key
        rec.lock_pid = capture.pid
        log("lock_acquired")
        if scenario.foreign == "idle_connected_session":
            idle = ForeignSession(cluster, source, ledger, "idle_write_capable")
            foreign_sessions.append(idle)
            rec.foreign = {"pid": idle.pid, "role": MIGRATOR_ROLE, "wrote": False}
            log("foreign_session_connected_before_the_start_sample")
        take("start")
        log("start_sample")
        baseline_engine = create_engine(cluster.backup_url(source), pool_size=1, max_overflow=0, pool_timeout=15, connect_args={"connect_timeout": 15, "application_name": "napsedge_c10_baseline"})
        ledger.engines.append(baseline_engine)
        baseline = capture_baseline(baseline_engine)  # plain reads: NO exported snapshot, so a difference cannot be hidden by one
        rec.baseline_details = read_sequence_details(baseline_engine)
        log("baseline_read_without_a_snapshot")
        before_write = fresh_state(cluster, source, ledger)

        if scenario.foreign in {"short_lived_writer", "retained_writer"}:
            writer = ForeignSession(cluster, source, ledger, "noncooperating_writer")  # it never takes the advisory lock
            foreign_sessions.append(writer)
            written_id = writer.write()
            rec.foreign = {"pid": writer.pid, "role": MIGRATOR_ROLE, "wrote": True, "written_id": written_id, "took_the_lock": False,
                           "lock_rows_at_the_write": advisory_lock_rows(admin, key, source)}
            log("foreign_write_committed_while_the_lock_was_held")
            rec.proofs = {"before_write": before_write, "after_write": fresh_state(cluster, source, ledger)}  # independent of the writer: new backup-role transactions
            take("mid")
            log("mid_sample")
            if scenario.foreign == "short_lived_writer":
                writer.disconnect()
                rec.foreign["gone_confirmed_by_admin"] = confirm_gone(admin, [writer.pid])
                log("foreign_session_physically_disconnected")
                take("after_disconnect")
        rec.dump = run_pg_dump(cluster, source, workspace.archive_path, workspace.backup_pgpass, snapshot=None, commands=rec.commands)  # a PLAIN dump: no --snapshot
        log("plain_dump_exited")
        if rec.dump.returncode != 0:
            raise CaptureStepFailed(f"pg_dump failed unexpectedly: {rec.dump.diagnostics()}")
        take("end")
        log("end_sample")
        restored = restore_into_fresh_database(cluster, ledger, workspace.archive_path, workspace.migrator_pgpass, rec.commands, log)
        rec.destination_database = restored.name
        rec.comparison = compare_to_baseline(baseline, restored.admin_engine)  # the unchanged strict comparator
        rec.restored_details = read_sequence_details(restored.admin_engine)
        rec.sequence_diff = keyed_diff(rec.comparison.sequence_source, rec.comparison.sequence_restored, key_columns=1)
        log("restored_and_compared")
        rec.finalization_invoked = True  # the stand-in: reached only through a completed, acquired capture
        log("finalization_stand_in")

    # ---- C9: a first session holds the lock; the capture attempt must be REFUSED and reach nothing ------------------------------------------------------
    if scenario.holder_first:
        holder = lock_session("holder")
        holder_target = verify_database_target(holder.connection, designation)
        with bulk_import_advisory_lock(holder.connection, holder_target) as held_key:
            rec.key = held_key
            refused = lock_session("refused")
            capture = refused
            rec.holder = {"pid": holder.pid, "rows": advisory_lock_rows(admin, held_key, source)}
            started = time.monotonic()
            outcome = run_guarded(refused, designation, body)
            rec.refused = {"outcome": outcome, "elapsed_s": round(time.monotonic() - started, 3), "pid": refused.pid, "statements": list(refused.statements),
                           "connections_opened": len(refused.connects), "actions_after_refusal": list(rec.actions), "commands": len(rec.commands),
                           "databases_unchanged": _case_databases(admin) == rec.databases_before}
        rec.holder["rows_after_release"] = advisory_lock_rows(admin, held_key, source)
        log("holder_released")

    capture = lock_session("capture")
    rec.capture_outcome = run_guarded(capture, designation, body)
    rec.lock_rows["after_release"] = advisory_lock_rows(admin, rec.key, source)
    log("lock_released")

    # ---- the physical sessions this flow opened are ended and VERIFIED gone ----------------------------------------------------------------------------
    for made in sessions:
        made.end()
    for other in foreign_sessions:
        other.disconnect()
    pids = [m.pid for m in sessions] + [f.pid for f in foreign_sessions]
    rec.physical_cleanup = {"sessions": len(pids), "gone_after_dispose": confirm_gone(admin, pids), "advisory_rows_after": advisory_lock_rows(admin, rec.key, source)}
    rec.databases_after = _case_databases(admin)
    return rec


def _run_lifecycle(harness_: Harness) -> LifecycleRecord:
    with owned_case(harness_.cluster) as ledger:
        record = run_lifecycle_case(harness_, ledger)
    record.verification = ledger.verification
    return record


def _run_flow(harness_: Harness, name: str) -> FlowRecord:
    with owned_case(harness_.cluster) as ledger:
        record = run_flow_case(harness_, FLOW_SCENARIOS[name], ledger)
        record.artifact_listing = sorted(p.name for p in Path(next(iter(ledger.directories))).iterdir())
    record.verification = ledger.verification
    return record


class Registry:
    """Runs each scenario at most once per module, inside its own cleanup ledger; a failure is cached and re-raised so a scenario is never run twice."""

    def __init__(self, harness_: Harness) -> None:
        self.harness, self._results = harness_, {}

    def _get(self, key, run: Callable):
        if key not in self._results:
            try:
                self._results[key] = ("ok", run())
            except BaseException as exc:  # noqa: BLE001
                self._results[key] = ("error", exc)
        status, value = self._results[key]
        if status == "error":
            raise value
        return value

    def lifecycle(self) -> LifecycleRecord:
        return self._get(("lifecycle",), lambda: _run_lifecycle(self.harness))

    def flow(self, name: str) -> FlowRecord:
        return self._get(("flow", name), lambda: _run_flow(self.harness, name))


@pytest.fixture(scope="module")
def runs(harness):
    return Registry(harness)


# ==========================================================================
# scope and static guarantees (no cluster needed)
# ==========================================================================
ALLOWED_REFUSED_STATEMENTS = ["SELECT pg_backend_pid()", "SELECT current_database(), version()", "SELECT pg_try_advisory_lock(%(key)s)"]
FORBIDDEN_FRAGMENTS = ("pg_export_snapshot", "ISOLATION LEVEL", "SET TRANSACTION SNAPSHOT", "FROM public", "pg_dump", "CREATE DATABASE", "INSERT", "UPDATE", "DELETE")
XFAIL_QUIESCENCE = "SAFETY REQUIREMENT NOT MET: endpoint session counts cannot establish quiescence or a write"


def test_only_c9_and_c10_are_in_scope_and_nothing_else_is_claimed():
    assert VALIDATED_CASES == ("C9", "C10") and {s.case for s in FLOW_SCENARIOS.values()} == {"C9", "C10"}
    assert SKIPPED_OVERLAP == "SKIPPED:overlap" and SKIPPED_OVERLAP != "skipped", "a recorded job outcome, not a pytest skip"


def test_this_module_reuses_the_existing_key_logic_and_adds_no_lock_namespace_grant_or_production_change():
    source = Path(__file__).read_text(encoding="utf-8")
    # the forbidden words are built by concatenation so that this very test does not contain them
    for forbidden in ("stable_" "wide_id", "LOCK_" "NAMESPACE", "hash" "lib", "GRA" "NT ", "REVO" "KE ", "CREATE " "ROLE", "pg_advisory_" "lock(", "pg_advisory_xact_" "lock"):
        assert forbidden not in source, forbidden


# ==========================================================================
# C9 -- overlap and the lock lifecycle (lifecycle case: one disposable database)
# ==========================================================================
def test_c9_two_distinct_physical_backup_role_sessions_against_a_verified_database(runs):
    s = runs.lifecycle().e["sessions"]
    assert s["distinct"] and s["pid_first"] != s["pid_second"] and s["separate_engines"]
    assert s["users"] == (BACKUP_ROLE, BACKUP_ROLE)


def test_c9_the_existing_verification_and_key_derivation_are_reused_and_decoded_from_pg_locks(runs):
    k = runs.lifecycle().e["key"]
    assert k["same_key_from_the_second_session"] and k["database"].startswith("napsedge_pg_test_")
    assert k["classid"] == k["key"] >> 32 and k["objid"] == k["key"] & 0xFFFFFFFF and 0 <= k["key"] < 2**63


def test_c9_the_second_session_refuses_immediately_with_the_recorded_outcome_and_never_runs_its_body(runs):
    r, s = runs.lifecycle().e["refusal"], runs.lifecycle().e["sessions"]
    assert r["outcome"] == SKIPPED_OVERLAP and r["body_entered"] is False and r["elapsed_s"] < 2, "refused at once, never retried or polled"
    assert r["rows_while_held"] == [(s["pid_first"], True)], "PostgreSQL shows the lock held by the first session alone"
    assert s["pid_second"] not in [pid for pid, _ in r["rows_while_held"]]
    statements = r["second_statements"]
    assert statements[-1] == "SELECT pg_try_advisory_lock(%(key)s)" and not any(f in " ".join(statements) for f in FORBIDDEN_FRAGMENTS)


def test_c9_after_normal_release_a_fresh_session_acquires(runs):
    a = runs.lifecycle().e["after_release"]
    assert a["rows_right_after_release"] == []
    assert a["fresh_session"]["outcome"] == "ACQUIRED" and a["fresh_session"]["rows_during"] == [(a["fresh_session"]["pid"], True)] and a["fresh_session"]["rows_after"] == []


def test_c9_the_exception_path_releases_the_lock_and_preserves_the_primary_failure(runs):
    x = runs.lifecycle().e["exception_path"]
    assert x["propagated_is_primary"] is True and x["notes"] == [] and x["rows_after"] == []
    assert x["reacquire"]["outcome"] == "ACQUIRED"


def test_c9_physical_session_loss_removes_the_lock_and_a_fresh_session_acquires(runs):
    s = runs.lifecycle().e["session_loss"]
    assert len(s["rows_before"]) == 1 and s["rows_before"][0][1] is True and s["backend_gone"] is True and s["rows_after"] == []
    assert s["release_after_loss_raised"] == "OperationalError", "OBSERVED: releasing on a lost session raises; nothing was left held"
    assert s["reacquire"]["outcome"] == "ACQUIRED"


def test_c9_a_failed_release_never_replaces_the_primary_failure_in_the_production_manager_itself(runs):
    """
    The documented contract, required DIRECTLY from the corrected production manager. (History: this experiment first observed the production manager propagating
    `OperationalError` from its unlock instead of the primary failure, which the harness then compensated for; that defect is retained in the specification and the
    pre-change evidence, and fixed in `bulk_import_advisory_lock`.)
    """
    p = runs.lifecycle().e["primary_with_lost_session"]
    expected_note = RELEASE_FAILED_NOTE.format(error_type="OperationalError")
    assert p["production_manager_propagated_is_primary"] is True and p["production_manager_propagated_type"] == "RuntimeError"
    assert p["production_manager_notes"] == [expected_note] and p["production_manager_cause_is_none"] is True
    assert p["guard_propagated_is_primary"] is True and p["guard_notes"] == [expected_note], "the harness guard adds nothing of its own: it shows the production manager's single note"
    assert p["rows_after"] == [] and p["reacquire"]["outcome"] == "ACQUIRED", "the lost session's lock is gone (verified from PostgreSQL), and a fresh session acquires"


def test_c9_the_harness_guard_carries_no_exception_repair_policy_of_its_own():
    import inspect

    source = inspect.getsource(run_guarded)
    assert "add_note" not in source and "raise primary" not in source and "BaseException" not in source and "__context__" not in source


def test_c9_a_session_level_lock_survives_rollback_and_connection_close_returns_a_live_backend_to_the_pool(runs):
    pr = runs.lifecycle().e["pool_and_rollback"]
    pid = pr["leaker_pid"]
    assert pr["acquired"] is True and pr["rows_acquired"] == [(pid, True)]
    assert pr["rows_after_rollback"] == [(pid, True)], "rollback does not release a session-level advisory lock"
    assert pr["pool_checked_out_after_close"] == 0 and pr["backend_alive_after_close"] is True and pr["rows_after_close"] == [(pid, True)], "close() put the LIVE backend back in the pool, lock still held"
    assert pr["second_session_outcome_while_pooled"] == SKIPPED_OVERLAP
    assert pr["backend_gone_after_dispose"] is True and pr["rows_after_dispose"] == [] and pr["reacquire_after_dispose"]["outcome"] == "ACQUIRED"


def test_c9_dispose_alone_does_not_end_a_session_whose_connection_is_still_checked_out(runs):
    co = runs.lifecycle().e["pool_and_rollback"]["checked_out"]
    assert co["backend_alive_after_dispose_alone"] is True and co["rows_after_dispose_alone"] == [(co["pid"], True)] and co["second_session_outcome"] == SKIPPED_OVERLAP
    assert co["gone_after_close_and_dispose"] is True, "closing the connection and then disposing the pool is what ends the physical session"


def test_c9_physical_session_cleanup_was_verified_from_postgres_not_assumed(runs):
    c = runs.lifecycle().e["physical_cleanup"]
    assert c["sessions_opened"] >= 15 and c["gone_after_dispose"] is True and c["rows_after"] == []


def test_c9_the_reused_key_excludes_controlled_imports_in_both_directions_which_is_recorded_not_chosen(runs):
    i = runs.lifecycle().e["import_exclusion"]
    assert i["same_key"] is True and i["import_role"] == MIGRATOR_ROLE
    assert i["import_refused_while_backup_holds"] == SKIPPED_OVERLAP and i["backup_refused_while_import_holds"] == SKIPPED_OVERLAP and i["rows_after"] == []
    # backup-overlap protection (two backups) and import exclusion are different facts; this unit records both and selects no production policy


def test_c9_the_backup_roles_existing_grants_suffice_and_nothing_was_added(runs):
    g = runs.lifecycle().e["grants"]
    assert g["select_only"] is True and g["tables"] == 24 and g["can_execute_advisory_functions"] == [True, True]
    assert g["member_of_monitoring_roles"] == [False, False, False], "no pg_monitor, pg_read_all_stats or pg_write_all_data"


# ---- the real flow: the refused attempt reaches nothing, then the same flow completes ----------------------------------------------------------------
def test_c9_flow_the_refused_attempt_is_skipped_overlap_and_reaches_no_downstream_step(runs):
    r = runs.flow("c9_refused_while_held_then_completes")
    assert r.holder["rows"] == [(r.holder["pid"], True)] and r.refused["pid"] != r.holder["pid"]
    assert r.refused["outcome"] == SKIPPED_OVERLAP and r.refused["elapsed_s"] < 2
    assert r.refused["actions_after_refusal"] == ["source_provisioned"], "no snapshot export, baseline, dump, restore or finalization"
    assert r.refused["commands"] == 0 and r.refused["connections_opened"] == 1 and r.refused["databases_unchanged"] is True
    assert r.refused["statements"] == ALLOWED_REFUSED_STATEMENTS, "the refused session sent only its identity check and the non-blocking lock attempt"


def test_c9_flow_after_the_holder_releases_the_same_flow_acquires_and_completes(runs):
    r = runs.flow("c9_refused_while_held_then_completes")
    assert r.holder["rows_after_release"] == [] and r.capture_outcome == "ACQUIRED" and r.finalization_invoked is True
    assert r.lock_rows["start"] == r.lock_rows["end"] == [(r.lock_pid, True)] and r.lock_rows["after_release"] == []
    assert r.comparison.fingerprint_diffs == {} and r.sequence_diff == {} and r.artifact_listing == []


# ==========================================================================
# C10 -- quiescence observations and their limits
# ==========================================================================
def _no_snapshot(r) -> bool:
    return not any(a.startswith("--snapshot") for a in r.dump.argv) and "snapshot" not in " ".join(r.actions).replace("without_a_snapshot", "")


QUIESCENT = ["c10_a_no_writer_control", "c10_b_short_lived_noncooperating_writer", "c10_c_writer_retained_through_the_end_sample", "c10_d_connected_write_capable_session_that_writes_nothing"]


@pytest.mark.parametrize("name", QUIESCENT)
def test_c10_the_quiescent_procedure_holds_the_lock_across_the_window_and_uses_no_exported_snapshot(name, runs):
    r = runs.flow(name)
    assert r.capture_outcome == "ACQUIRED" and _no_snapshot(r)
    held = [(r.lock_pid, True)]
    stages = [k for k in r.lock_rows if k != "after_release"]
    assert {"start", "end"} <= set(stages) and all(r.lock_rows[k] == held for k in stages) and r.lock_rows["after_release"] == []
    order = [a for a in r.actions if a in {"lock_acquired", "start_sample", "baseline_read_without_a_snapshot", "plain_dump_exited", "end_sample", "restored_and_compared", "lock_released"}]
    assert order == ["lock_acquired", "start_sample", "baseline_read_without_a_snapshot", "plain_dump_exited", "end_sample", "restored_and_compared", "lock_released"]


@pytest.mark.parametrize("name", QUIESCENT)
def test_c10_the_admin_instrumentation_never_connects_to_the_case_database_so_it_cannot_distort_the_counts(name, runs):
    r = runs.flow(name)
    for stage, observed in r.admin_samples.items():
        assert "napsedge_c10_sampler" not in observed["applications"] and "napsedge_c10_baseline" not in observed["applications"], stage
        assert all(app.startswith(("napsedge_c10_noncooperating_writer", "napsedge_c10_idle_write_capable")) for app in observed["applications"]), (stage, observed["applications"])
    assert r.admin_samples["start"]["pids"] == r.samples["start"]["pids"] or name.endswith("writes_nothing")


def test_c10_a_no_writer_control_has_strict_data_and_sequence_equality(runs):
    r = runs.flow("c10_a_no_writer_control")
    assert r.samples["start"]["count"] == r.samples["end"]["count"] == 0 and r.admin_samples["start"]["pids"] == r.admin_samples["end"]["pids"] == ()
    assert r.comparison.fingerprint_diffs == {} and r.sequence_diff == {} and r.comparison.sequence_source == r.comparison.sequence_restored
    assert r.restored_details == r.baseline_details, "every one of the 13 sequences equal: last_value and is_called"


def test_c10_b_a_short_lived_noncooperating_writer_committed_while_the_lock_was_held_and_was_proven_independently(runs):
    r = runs.flow("c10_b_short_lived_noncooperating_writer")
    f = r.foreign
    assert f["wrote"] and f["took_the_lock"] is False and f["lock_rows_at_the_write"] == [(r.lock_pid, True)], "the write genuinely executed although the lock was held"
    assert f["pid"] != r.lock_pid and f["role"] == MIGRATOR_ROLE
    before, after = r.proofs["before_write"], r.proofs["after_write"]
    assert before["probe_ids"] == [] and after["probe_ids"] == [f["written_id"]], "a NEW backup-role transaction sees the committed row, independent of the writer"
    assert after["sequence"] == (before["sequence"][0] + 1, True) and f["written_id"] == after["sequence"][0], "the id came from the sequence, which advanced by one"


def test_c10_b_the_endpoint_counts_are_zero_so_the_intervening_write_was_missed_although_the_count_can_see_it_when_present(runs):
    r = runs.flow("c10_b_short_lived_noncooperating_writer")
    f = r.foreign
    assert r.samples["start"]["count"] == 0 and r.samples["end"]["count"] == 0 and r.admin_samples["start"]["pids"] == () and r.admin_samples["end"]["pids"] == ()
    assert r.samples["mid"]["count"] == 1 and r.samples["mid"]["pids"] == (f["pid"],) and r.admin_samples["mid"]["pids"] == (f["pid"],), "present in the middle: the count works, the endpoints just did not coincide with it"
    assert r.admin_samples["mid"]["states"] == ("idle",) and r.admin_samples["mid"]["has_xid"] == (False,), "administrative view: connected, idle, no open write"
    assert f["gone_confirmed_by_admin"] is True and r.samples["after_disconnect"]["count"] == 0


def test_c10_b_the_strict_comparator_shows_the_expected_data_and_sequence_differences(runs):
    r = runs.flow("c10_b_short_lived_noncooperating_writer")
    diffs = r.comparison.fingerprint_diffs
    assert set(diffs) == {"team_aliases"} and diffs["team_aliases"][0][0] + 1 == diffs["team_aliases"][1][0], "one more row in the dump than in the baseline"
    before = {d.name: d for d in r.baseline_details}["team_aliases_id_seq"]
    assert set(r.sequence_diff) == {("team_aliases_id_seq",)} and r.sequence_diff[("team_aliases_id_seq",)] == (("team_aliases_id_seq", before.last_value), ("team_aliases_id_seq", before.last_value + 1))
    assert [d for d in r.restored_details if d not in r.baseline_details][0].name == "team_aliases_id_seq" and len([d for d in r.restored_details if d not in r.baseline_details]) == 1


def test_c10_c_a_writer_retained_through_the_end_sample_is_counted_and_its_change_is_separately_proven(runs):
    r = runs.flow("c10_c_writer_retained_through_the_end_sample")
    f = r.foreign
    assert r.samples["start"]["count"] == 0 and r.samples["mid"]["pids"] == (f["pid"],) and r.samples["end"]["pids"] == (f["pid"],) and r.samples["end"]["count"] == 1
    assert r.admin_samples["end"]["pids"] == (f["pid"],) and r.admin_samples["end"]["states"] == ("idle",)
    assert r.proofs["before_write"]["probe_ids"] == [] and r.proofs["after_write"]["probe_ids"] == [f["written_id"]] and f["took_the_lock"] is False
    assert set(r.comparison.fingerprint_diffs) == {"team_aliases"} and set(r.sequence_diff) == {("team_aliases_id_seq",)}


def test_c10_d_a_connected_write_capable_session_that_writes_nothing_is_counted_but_nothing_changed(runs):
    r = runs.flow("c10_d_connected_write_capable_session_that_writes_nothing")
    f = r.foreign
    assert f["wrote"] is False and r.samples["start"]["pids"] == r.samples["end"]["pids"] == (f["pid"],) and r.samples["start"]["count"] == r.samples["end"]["count"] == 1
    assert r.admin_samples["end"]["states"] == ("idle",) and r.admin_samples["end"]["has_xid"] == (False,) and r.admin_samples["end"]["in_transaction"] == (False,)
    assert r.comparison.fingerprint_diffs == {} and r.sequence_diff == {} and r.restored_details == r.baseline_details, "a positive count established presence, not a write"


@pytest.mark.parametrize("name", QUIESCENT[1:])
def test_c10_the_backup_role_sees_other_roles_sessions_only_through_restricted_fields_which_are_not_proof_of_inactivity(name, runs):
    r = runs.flow(name)
    seen = [s for s in r.samples.values() if s["count"]]
    assert seen and all(s["restricted_fields_null"] is True for s in seen), "backend_type and state are NULL and query is '<insufficient privilege>' for another role's session"
    assert all(s["roles"] == (MIGRATOR_ROLE,) for s in seen)


def test_c10_endpoint_counts_alone_cannot_tell_the_no_writer_control_from_the_short_lived_writer(runs):
    a, b = runs.flow("c10_a_no_writer_control"), runs.flow("c10_b_short_lived_noncooperating_writer")
    assert (a.samples["start"]["count"], a.samples["end"]["count"]) == (b.samples["start"]["count"], b.samples["end"]["count"]) == (0, 0)
    assert a.comparison.fingerprint_diffs == {} and b.comparison.fingerprint_diffs != {}, "identical endpoint evidence, different data"


def test_c10_pg_stat_activity_is_cached_per_transaction_so_samples_must_come_from_fresh_transactions(runs):
    s = runs.lifecycle().e["sampling"]
    assert s["in_the_same_transaction_after_a_session_connected"] == s["in_one_transaction_before"], "STALE: the same open transaction cannot see the session that connected"
    assert s["after_pg_stat_clear_snapshot"] == s["fresh_transaction"] == s["in_one_transaction_before"] + 1, "clearing the snapshot, or a fresh transaction, observes it"


# ---- the SAFETY requirements: not met by endpoint counts (strict xfail, so a change in behavior fails loudly) ------------------------------------------
@pytest.mark.parametrize("name", [pytest.param("c10_b_short_lived_noncooperating_writer", marks=pytest.mark.xfail(strict=True, reason=XFAIL_QUIESCENCE))])
def test_c10_safety_requirement_zero_endpoint_counts_imply_that_nothing_wrote_during_the_window(name, runs):
    r = runs.flow(name)
    assert r.samples["start"]["count"] == 0 and r.samples["end"]["count"] == 0
    assert r.comparison.fingerprint_diffs == {} and r.sequence_diff == {}


@pytest.mark.parametrize("name", [pytest.param("c10_d_connected_write_capable_session_that_writes_nothing", marks=pytest.mark.xfail(strict=True, reason=XFAIL_QUIESCENCE))])
def test_c10_safety_requirement_a_positive_endpoint_count_implies_that_a_write_happened(name, runs):
    r = runs.flow(name)
    assert r.samples["end"]["count"] > 0
    assert r.comparison.fingerprint_diffs != {} or r.sequence_diff != {}


# ==========================================================================
# cleanup (normal and deliberate failure paths), credentials, recorded evidence
# ==========================================================================
def _clean(v: dict) -> None:
    assert v["problems"] == [] and v["databases_remaining"] == [] and v["sessions_in_dropped_databases"] == 0
    assert v["leftover_dump_or_owner_sessions"] == 0 and v["container_files_remaining"] == [] and v["threads_alive"] == [] and v["directories_remaining"] == []


@pytest.mark.parametrize("name", list(FLOW_SCENARIOS))
def test_cleanup_was_verified_for_each_flow_scenario_and_its_physical_sessions_are_gone(name, runs):
    r = runs.flow(name)
    _clean(r.verification)
    assert r.physical_cleanup["gone_after_dispose"] is True and r.physical_cleanup["advisory_rows_after"] == [] and r.databases_after != r.databases_before


def test_cleanup_was_verified_for_the_lifecycle_case(runs):
    _clean(runs.lifecycle().verification)


def test_cleanup_after_a_deliberate_failure_leaves_no_session_no_lock_and_no_database(harness):
    cluster = harness.cluster
    admin = cluster.admin_engine()
    created: dict = {}
    try:
        with pytest.raises(RuntimeError, match="deliberate failure"):
            with owned_case(cluster) as ledger:
                workspace = prepare_case_workspace(cluster, ledger)
                created["database"] = workspace.source_database
                holder = LockSession(cluster, workspace.source_database, ledger, "failure_holder")
                writer = ForeignSession(cluster, workspace.source_database, ledger, "failure_writer")
                target = verify_database_target(holder.connection, designation_for(workspace.source_database))
                created["key"] = compute_advisory_lock_key(target)
                assert scalar(holder.connection, "SELECT pg_try_advisory_lock(:k)", k=created["key"]) is True  # taken and then abandoned by the failure
                created["pids"] = [holder.pid, writer.pid]
                raise RuntimeError("deliberate failure")
        assert confirm_gone(admin, created["pids"]), "the ledger ended the abandoned physical sessions"
        assert advisory_lock_rows(admin, created["key"], created["database"]) == []
        assert rows(admin, "SELECT datname FROM pg_database WHERE datname = :d", d=created["database"]) == []
    finally:
        admin.dispose()


@pytest.mark.parametrize("name", list(FLOW_SCENARIOS))
def test_no_credential_appears_in_any_recorded_command_or_output(name, runs, harness):
    r = runs.flow(name)
    assert r.commands
    for command in r.commands:
        blob = " ".join(command.argv) + command.stdout + command.stderr
        assert not any(secret and secret in blob for secret in harness.cluster.secrets_to_scrub()) and "PGPASSWORD" not in blob and "password=" not in blob.lower()


@pytest.mark.parametrize("name", list(FLOW_SCENARIOS))
def test_observed_flow_facts_are_recorded_in_the_junit_evidence(name, request, runs):
    """PIDs, lock ownership and release, outcomes, committed changes, sample counts, comparator differences and cleanup; no credential."""
    r = runs.flow(name)
    p = request.node.user_properties
    p += [("scenario", f"{r.scenario} ({r.case})"), ("capture_outcome", r.capture_outcome), ("lock_pid", str(r.lock_pid)), ("lock_key", str(r.key)), ("actions", ",".join(r.actions)),
          ("finalization_invoked", str(r.finalization_invoked)), ("cleanup_problems", str(len(r.verification["problems"]))), ("physical_cleanup", str(r.physical_cleanup)),
          ("fingerprint_diffs", str({t: v for t, v in r.comparison.fingerprint_diffs.items()})), ("strict_sequence_diff", str(r.sequence_diff)),
          ("sequences_equal_after_restore", str(r.restored_details == r.baseline_details)), ("dump_has_snapshot_argument", str(any(a.startswith("--snapshot") for a in r.dump.argv)))]
    for stage, rows_ in r.lock_rows.items():
        p.append((f"lock_rows:{stage}", str(rows_)))
    for stage, sample in r.samples.items():
        p.append((f"sample:{stage}", f"count={sample['count']} pids={sample['pids']} restricted_fields_null={sample['restricted_fields_null']} roles={sample['roles']}"))
    for stage, sample in r.admin_samples.items():
        p.append((f"admin_sample:{stage}", f"pids={sample['pids']} states={sample['states']} has_xid={sample['has_xid']} in_transaction={sample['in_transaction']}"))
    if r.foreign:
        p.append(("foreign", str(r.foreign)))
    if r.proofs:
        p.append(("independent_proofs", str(r.proofs)))
    if r.refused:
        p.append(("refused_attempt", str({k: v for k, v in r.refused.items() if k != "actions_after_refusal"}) + f" actions_after_refusal={r.refused['actions_after_refusal']}"))
        p.append(("holder", str(r.holder)))
    assert not r.verification["problems"]


def test_observed_lifecycle_facts_are_recorded_in_the_junit_evidence(request, runs):
    rec = runs.lifecycle()
    p = request.node.user_properties
    for name, value in rec.e.items():
        p.append((f"lifecycle:{name}", str(value)))
    p.append(("cleanup_problems", str(len(rec.verification["problems"]))))
    assert not rec.verification["problems"]
