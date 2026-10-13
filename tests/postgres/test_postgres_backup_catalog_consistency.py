"""
Phase 9B-5C3: disposable validation of catalog observations and locking.
Specification section 7 cases C6 and C7 ONLY (sections 6.3 and 6.4 are the proposal under test).

What is (and is not) established
--------------------------------
* C6  The proposed CATALOG BRACKET is implemented AS WRITTEN first: the snapshot owner digests the baseline's constraint, index and ACL/ownership
      reads before the snapshot export and again after `pg_dump` exits, inside the owner's actual transaction boundaries. Isolated scenarios commit an
      index change, an ACL change and an owner change (plus a no-change control) at defined points, each proven committed and visible to a fresh
      observer; a blocked or rolled-back change never counts as detection. The raw observation differences and their digests are recorded separately
      for constraints, indexes and ACL/ownership. What the bracket detects and what it misses is OBSERVED, not assumed: where a committed change is
      missed, a characterization test retains the blind spot and a STRICT xfail test states the safety requirement that is therefore still unmet.
      A separate test-layer CANDIDATE (each endpoint observed through a fresh committed transaction, leaving the data-snapshot owner untouched) is
      evaluated and labeled as an alternative that needs later design review; its `catalog_changed` refusal is exercised in the real harness flow and
      prevents finalization. Equality at two endpoints cannot prove that nothing changed in between (a change and its reversal is shown undetected).
* C7  The backup role can take the proposed ACCESS SHARE locks with its existing grants; ordinary baseline reads versus the explicit-lock variant are
      compared from `pg_locks`; a bounded migrator operation that genuinely requests ACCESS EXCLUSIVE is shown queued behind the open owner
      (`pg_locks`, `pg_stat_activity`, `pg_blocking_pids`), times out and rolls back, and a fresh ordinary reader is shown waiting behind the QUEUED
      request and recovering once it times out, while the owner stays open; a control without a queued request shows no waiting. The availability
      cost is recorded. No production lock policy is chosen.

Nothing here validates C9 or C10, restore verification (claim 4) or production readiness; no production capture exists. Data-snapshot reads stay
on the owner connection throughout; catalog observations never replace them.

Reuse
-----
The disposable cluster, snapshot owner, shared-snapshot dump, restore into a verified-empty database, credentials and cleanup ledger come from the C1
module's harness (imported, not copied). The catalog reads are the existing `constraint_snapshot`, `index_snapshot` and `acl_snapshot` through the
caller's connection, and the existing strict comparator judges baseline-versus-restored; no catalog normalization is duplicated.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

from src.deployment.backup_capture_catalog import CATEGORIES, CatalogObservation, catalog_changed, changed_categories, raw_differences  # noqa: F401  (re-exported for the 12.8 candidate module)
from tests.postgres.pg_restore_compare import DatabaseBaseline, acl_snapshot, all_fingerprints, capture_baseline, compare_to_baseline, constraint_snapshot, index_snapshot, keyed_diff, public_tables, rows, scalar
from tests.postgres.test_postgres_backup_snapshot_consistency import (
    BARRIER_TIMEOUT_S,
    CaptureStepFailed,
    CaseLedger,
    Harness,
    SnapshotOwner,
    _dump_sessions,
    backend_state,
    harness,  # noqa: F401  (module-scoped fixture: ONE disposable cluster for this module)
    owned_case,
    prepare_case_workspace,
    restore_into_fresh_database,
    run_pg_dump,
)
from tests.postgres.test_postgres_fresh_cluster_recovery import PreconditionFailed
from tests.postgres.test_postgres_roles_and_restore import RUNTIME_ROLE, CommandResult, _literal
from tests.postgres.test_postgres_tls_backup_restore import BACKUP_ROLE, isolated_database_environment  # noqa: F401  (autouse module fixture)

pytestmark = pytest.mark.postgres

VALIDATED_CASES = ("C6", "C7")
UNVALIDATED_CASES = ("C9", "C10")

PROBE_ROLE = "napsedge_catalog_probe_owner"  # a disposable NOLOGIN role that receives a table in the owner-change scenario
INDEX_NAME = "ix_teams_display_name_catalog_probe"
DDL_LOCK_TIMEOUT = "10s"  # the migrator's bounded wait in C7; the owner-change attempts in C6 use the short one below
CHANGE_LOCK_TIMEOUT = "5s"
CHANGE_STATEMENT_TIMEOUT = "20s"


# ==========================================================================
# the observation: the existing catalog reads, digested separately per category
# ==========================================================================
# The pure definitions (CatalogObservation and its digests, changed_categories, raw_differences, catalog_changed) were MOVED
# to their one authoritative implementation, src/deployment/backup_capture_catalog.py (Phase 9B-6F), and are imported
# above. This module keeps only its own I/O helper (an Engine or a Connection, through the existing pg_restore_compare
# reads) and its harness guard.
def observe_catalog(source: Engine | Connection) -> CatalogObservation:
    return CatalogObservation(tuple(constraint_snapshot(source)), tuple(index_snapshot(source)), tuple(acl_snapshot(source)))


def require_catalog_unchanged(before: CatalogObservation, after: CatalogObservation, label: str) -> None:
    """The `catalog_changed` guard of the harness flow: a refusal means the capture must not be finalized."""
    if catalog_changed(before, after):
        raise PreconditionFailed(f"catalog_changed ({label}): {changed_categories(before, after)}; the capture is refused and must not be finalized")


# ==========================================================================
# the changes, applied in a separate session and PROVEN committed (or reported blocked)
# ==========================================================================
@dataclass(frozen=True)
class Change:
    name: str
    role: str  # "migrator" (the table owner) or "admin" (the disposable cluster's superuser, needed to hand a table to another role)
    steps: tuple  # each step is committed in its own transaction
    category: str | None  # the observation category the change is meant to alter (None: it is reversed again)


CHANGES = {
    "index": Change("index", "migrator", (f"CREATE INDEX {INDEX_NAME} ON public.teams (display_name)",), "indexes"),
    "acl": Change("acl", "migrator", (f"GRANT INSERT ON public.conferences TO {RUNTIME_ROLE}",), "acls"),
    "owner": Change("owner", "admin", (f"ALTER TABLE public.conferences OWNER TO {PROBE_ROLE}",), "acls"),
    "index_and_reversal": Change("index_and_reversal", "migrator", (f"CREATE INDEX {INDEX_NAME} ON public.teams (display_name)", f"DROP INDEX public.{INDEX_NAME}"), None),
}


def apply_change(cluster, database: str, change: Change, ledger: CaseLedger, after_step: Callable[[], object] | None = None) -> dict:
    """
    Runs each step in its own transaction with a bounded lock wait and statement time. Reports how many steps COMMITTED and, when a step did not,
    its SQLSTATE (55P03 is a lock timeout: the operation BLOCKED behind a lock and did not commit).
    """
    engine = (create_engine(cluster.migrator_url(database), pool_size=1, max_overflow=0, pool_timeout=15, connect_args={"connect_timeout": 15})
              if change.role == "migrator" else cluster.admin_engine(database, autocommit=False))
    ledger.engines.append(engine)
    result = {"change": change.name, "role": change.role, "steps": len(change.steps), "committed_steps": 0, "sqlstate": None, "error": None, "elapsed_s": 0.0, "after_each_step": []}
    started = time.monotonic()
    for statement in change.steps:
        try:
            with engine.begin() as conn:
                conn.execute(text(f"SET LOCAL lock_timeout = '{CHANGE_LOCK_TIMEOUT}'"))
                conn.execute(text(f"SET LOCAL statement_timeout = '{CHANGE_STATEMENT_TIMEOUT}'"))
                conn.execute(text(statement))
            result["committed_steps"] += 1
            if after_step is not None:
                result["after_each_step"].append(after_step())  # what a fresh committed observer sees right after this step committed
        except DBAPIError as exc:
            result["sqlstate"], result["error"] = getattr(exc.orig, "sqlstate", None), type(exc.orig).__name__
            break
    result["elapsed_s"] = round(time.monotonic() - started, 2)
    result["committed"] = result["committed_steps"] == result["steps"]
    result["blocked"] = result["sqlstate"] == "55P03"
    return result


# ==========================================================================
# the C6 flow: the proposal as written, plus the candidate, in the real harness flow
# ==========================================================================
@dataclass(frozen=True)
class CatalogScenario:
    name: str
    case: str
    change: str | None = None
    window: str = "none"  # "after_export" (the owner holds no table lock yet) or "after_reads" (the baseline reads hold ACCESS SHARE on every table)
    gate: str = "as_written"  # which endpoints decide `catalog_changed` in the flow: "as_written" (owner) or "candidate" (fresh committed transactions)
    explicit_locks: bool = False  # take ACCESS SHARE on every application table BEFORE the export (the proposal's optional layer 2)


@dataclass
class CatalogRecord:
    scenario: str
    case: str
    actions: list = field(default_factory=list)
    commands: list = field(default_factory=list)
    source_database: str = ""
    destination_database: str | None = None
    change: dict | None = None
    fresh_visible_categories: list = field(default_factory=list)
    owner_connects_after_baseline: int = 0
    owner_locked_tables_after_baseline: list = field(default_factory=list)
    before_owner: CatalogObservation | None = None
    before_fresh: CatalogObservation | None = None
    baseline_catalog: CatalogObservation | None = None
    after_in_transaction: CatalogObservation | None = None
    after_fresh: CatalogObservation | None = None
    after_owner_ended: CatalogObservation | None = None
    detection: dict = field(default_factory=dict)  # as_written / owner_after_commit / candidate -> {"changed": bool, "categories": [...]}
    differences: dict = field(default_factory=dict)  # raw per-category differences for each variant
    outcome: str = ""
    finalization_invoked: bool = False
    baseline: DatabaseBaseline | None = None
    dump: CommandResult | None = None
    exported_id: str = ""
    descriptor_at_export: str = ""
    descriptor_after_dump: str = ""
    owner_state_at_dump_exit: str | None = None
    restore_failure: str | None = None
    comparison: object = None
    restored_fingerprints: dict | None = None
    harm: dict = field(default_factory=dict)
    explicit_lock_count: int = 0
    artifact_listing: list = field(default_factory=list)
    verification: dict = field(default_factory=dict)


def _fresh_engine(cluster, database: str, ledger: CaseLedger, app: str) -> Engine:
    engine = create_engine(cluster.backup_url(database), pool_size=1, max_overflow=0, pool_timeout=15, connect_args={"connect_timeout": 15, "application_name": app})
    ledger.engines.append(engine)
    return engine


def _fresh_observation(engine: Engine) -> CatalogObservation:
    with engine.connect() as conn:  # a NEW transaction, committed-read: it sees whatever has committed
        return observe_catalog(conn)


def _begin_with_explicit_locks(owner: SnapshotOwner, tables: list[str]) -> int:
    """The proposal's layer 2: ACCESS SHARE on every application table, taken inside the owner's transaction BEFORE the snapshot is exported."""
    owner.connection.exec_driver_sql("BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    for table in tables:
        owner.connection.exec_driver_sql(f'LOCK TABLE public."{table}" IN ACCESS SHARE MODE')
    owner.exported_id = scalar(owner.connection, "SELECT pg_export_snapshot()")
    owner.descriptor = owner.visibility_descriptor()
    owner.state = {"isolation": scalar(owner.connection, "SHOW transaction_isolation"), "read_only": scalar(owner.connection, "SHOW transaction_read_only"),
                   "role": scalar(owner.connection, "SELECT current_user")}
    return len(tables)


def run_catalog_case(harness_: Harness, scenario: CatalogScenario, ledger: CaseLedger) -> CatalogRecord:
    cluster = harness_.cluster
    record = CatalogRecord(scenario.name, scenario.case)
    log = record.actions.append
    workspace = prepare_case_workspace(cluster, ledger)
    source = record.source_database = workspace.source_database
    log("source_provisioned")
    fresh_engine = _fresh_engine(cluster, source, ledger, "napsedge_catalog_fresh_observer")
    change = CHANGES[scenario.change] if scenario.change else None

    def apply_and_prove() -> None:
        """Applies the change in a separate session, then PROVES from a fresh observer whether it committed and is visible (blocked or rolled back does not count)."""
        before_change = _fresh_observation(fresh_engine)
        record.change = apply_change(cluster, source, change, ledger, after_step=lambda: changed_categories(before_change, _fresh_observation(fresh_engine)))
        log(f"change_attempted:{'committed' if record.change['committed'] else 'blocked' if record.change['blocked'] else 'failed'}")
        after_change = _fresh_observation(fresh_engine)
        record.fresh_visible_categories = changed_categories(before_change, after_change)
        if record.change["committed"]:
            log("change_visible_to_a_fresh_observer" if record.fresh_visible_categories or change.category is None else "change_committed_but_not_visible")

    # ---- first endpoint, BEFORE the export: the owner's own connection (the proposal) and a fresh committed transaction (the candidate) ----
    owner = SnapshotOwner(cluster, source, ledger)
    log("owner_open")
    record.before_owner = observe_catalog(owner.connection)  # the owner session, outside any data transaction yet
    record.before_fresh = _fresh_observation(fresh_engine)
    log("first_endpoint_observed")

    # ---- the data snapshot: exported by the owner; its baseline data reads stay on the owner connection ------------------------------------
    if scenario.explicit_locks:
        record.explicit_lock_count = _begin_with_explicit_locks(owner, public_tables(fresh_engine))
        log("explicit_access_share_locks_taken_before_export")
    else:
        owner.begin_and_export()
    record.exported_id, record.descriptor_at_export = owner.exported_id, owner.descriptor
    log("snapshot_exported")
    if change is not None and scenario.window == "after_export":
        apply_and_prove()
    connects_before = len(owner.connects)
    record.baseline = capture_baseline(owner.connection)
    record.owner_connects_after_baseline = len(owner.connects) - connects_before
    record.baseline_catalog = CatalogObservation(tuple(record.baseline.constraints), tuple(record.baseline.indexes), tuple(record.baseline.acls))
    log("baseline_data_read_through_owner")
    if record.owner_connects_after_baseline != 0:
        raise PreconditionFailed("the baseline opened another connection: it did not share the owner's transaction")
    server_view = cluster.admin_engine(source)  # the server's own evidence that the reads ran in THIS backend's transaction: its relation locks
    ledger.engines.append(server_view)
    record.owner_locked_tables_after_baseline = sorted(granted_table_locks(server_view, owner.pid))
    if change is not None and scenario.window == "after_reads":
        apply_and_prove()

    # ---- the dump, with the owner open until it exits -----------------------------------------------------------------------------------------
    log("dump_started")
    record.dump = run_pg_dump(cluster, source, workspace.archive_path, workspace.backup_pgpass, snapshot=owner.exported_id, commands=record.commands)
    log("dump_exited")
    if record.dump.returncode != 0:
        record.outcome = "dump_failed"
        owner.end_transaction()
        raise CaptureStepFailed(f"pg_dump failed unexpectedly: {record.dump.diagnostics()}")

    # ---- the second endpoint: the proposal AS WRITTEN (owner, inside its open transaction), after the owner ends, and the candidate --------------------
    record.owner_state_at_dump_exit = backend_state(cluster, owner.pid)
    record.after_in_transaction = observe_catalog(owner.connection)
    record.after_fresh = _fresh_observation(fresh_engine)  # the owner is still open: it is neither changed nor ended
    record.descriptor_after_dump = owner.visibility_descriptor()
    owner.end_transaction()
    record.after_owner_ended = observe_catalog(owner.connection)
    log("second_endpoint_observed")
    variants = {"as_written_owner_in_transaction": (record.before_owner, record.after_in_transaction),
                "owner_after_its_transaction_ended": (record.before_owner, record.after_owner_ended),
                "candidate_fresh_committed_transactions": (record.before_fresh, record.after_fresh)}
    for label, (before, after) in variants.items():
        record.detection[label] = {"changed": catalog_changed(before, after), "categories": changed_categories(before, after)}
        record.differences[label] = raw_differences(before, after)

    # ---- the guard in the real flow: a refusal ends the flow before restore and finalization --------------------------------------------------------
    endpoints = {"as_written": ("as_written_owner_in_transaction", record.before_owner, record.after_in_transaction),
                 "candidate": ("candidate_fresh_committed_transactions", record.before_fresh, record.after_fresh)}[scenario.gate]
    try:
        require_catalog_unchanged(endpoints[1], endpoints[2], endpoints[0])
    except PreconditionFailed:
        record.outcome = "catalog_changed"
        log("catalog_changed_guard_refused")
        return record
    log("catalog_guard_passed")

    # ---- restore, compare (the strict comparator, unchanged), and the finalization stub (nothing is written) -------------------------------------
    try:
        restored = restore_into_fresh_database(cluster, ledger, workspace.archive_path, workspace.migrator_pgpass, record.commands, log)
    except CaptureStepFailed as failure:
        record.restore_failure = str(failure)[:300]
        record.outcome = "restore_failed"
        log("restore_failed")
        return record
    record.destination_database = restored.name
    record.restored_fingerprints = all_fingerprints(restored.admin_engine)
    record.comparison = compare_to_baseline(record.baseline, restored.admin_engine)
    record.harm = {"fingerprints_equal": not record.comparison.fingerprint_diffs, "indexes_equal": not record.comparison.index_diff,
                   "constraints_equal": not record.comparison.constraint_genuine_diff, "acls_equal": record.comparison.acls_equal, "owners_equal": record.comparison.owners_equal}
    log("compared")
    record.finalization_invoked = True  # the stand-in for finalization: reached only when every guard passed
    record.outcome = "finalization_permitted"
    log("finalization_permitted")
    return record


def _run_catalog(harness_: Harness, name: str) -> CatalogRecord:
    with owned_case(harness_.cluster) as ledger:
        record = run_catalog_case(harness_, CATALOG_SCENARIOS[name], ledger)
        record.artifact_listing = sorted(p.name for p in Path(next(iter(ledger.directories))).iterdir())
    record.verification = ledger.verification
    return record


CATALOG_SCENARIOS = {
    "c6_control": CatalogScenario("c6_control", "C6"),
    "c6_index_after_export": CatalogScenario("c6_index_after_export", "C6", "index", "after_export"),
    "c6_acl_after_export": CatalogScenario("c6_acl_after_export", "C6", "acl", "after_export"),
    "c6_owner_after_export": CatalogScenario("c6_owner_after_export", "C6", "owner", "after_export"),
    "c6_index_after_reads": CatalogScenario("c6_index_after_reads", "C6", "index", "after_reads"),
    "c6_acl_after_reads": CatalogScenario("c6_acl_after_reads", "C6", "acl", "after_reads"),
    "c6_owner_after_reads": CatalogScenario("c6_owner_after_reads", "C6", "owner", "after_reads"),
    "c6_index_change_and_reversal": CatalogScenario("c6_index_change_and_reversal", "C6", "index_and_reversal", "after_export"),
    "c6_candidate_gate_refuses_index_change": CatalogScenario("c6_candidate_gate_refuses_index_change", "C6", "index", "after_export", gate="candidate"),
    "c6_candidate_gate_passes_control": CatalogScenario("c6_candidate_gate_passes_control", "C6", gate="candidate"),
    "c7_owner_change_after_export_with_explicit_locks": CatalogScenario("c7_owner_change_after_export_with_explicit_locks", "C7", "owner", "after_export", explicit_locks=True),
    "c7_index_change_after_export_with_explicit_locks": CatalogScenario("c7_index_change_after_export_with_explicit_locks", "C7", "index", "after_export", explicit_locks=True),
    "c7_acl_change_after_export_with_explicit_locks": CatalogScenario("c7_acl_change_after_export_with_explicit_locks", "C7", "acl", "after_export", explicit_locks=True),
}


# ==========================================================================
# C7: lock behavior and runtime-read impact
# ==========================================================================
def wait_until(condition: Callable[[], bool], timeout: float, interval: float = 0.1) -> bool:
    """Bounded CONDITION polling: re-checks until true or the deadline passes (never an ordering sleep)."""
    deadline, pause = time.monotonic() + timeout, threading.Event()
    while time.monotonic() < deadline:
        if condition():
            return True
        pause.wait(interval)
    return condition()


def relation_locks(admin: Engine, pid: int) -> list[tuple]:
    """(relation, relkind, mode, granted) for every lock of backend `pid` on a public relation, from pg_locks, read by the admin session of the SAME database."""
    return rows(admin, "SELECT c.relname, c.relkind::text, l.mode, l.granted FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
                       "WHERE l.pid = :p AND l.locktype = 'relation' AND c.relnamespace = 'public'::regnamespace ORDER BY 1, 3", p=pid)


def granted_table_locks(admin: Engine, pid: int) -> dict[str, set]:
    result: dict[str, set] = {}
    for relation, kind, mode, granted in relation_locks(admin, pid):
        if kind == "r" and granted:
            result.setdefault(relation, set()).add(mode)
    return result


def waiting_modes(admin: Engine, pid: int, relation: str) -> list[str]:
    return [r[0] for r in rows(admin, "SELECT l.mode FROM pg_locks l JOIN pg_class c ON c.oid = l.relation WHERE l.pid = :p AND NOT l.granted AND c.relname = :r", p=pid, r=relation)]


def blocking_pids(admin: Engine, pid: int) -> list[int]:
    return list(scalar(admin, "SELECT pg_blocking_pids(:p)", p=pid))


def activity(admin: Engine, pid: int) -> dict | None:
    found = rows(admin, "SELECT wait_event_type, wait_event, state FROM pg_stat_activity WHERE pid = :p", p=pid)
    return dict(zip(("wait_event_type", "wait_event", "state"), found[0])) if found else None


@dataclass
class SessionOutcome:
    """What a thread-run session reports: its backend pid, when it started and finished (monotonic), whether it succeeded, and the SQLSTATE if not."""

    pid: int | None = None
    started: float | None = None
    finished: float | None = None
    committed: bool = False
    result: object = None
    sqlstate: str | None = None
    error: str | None = None

    @property
    def elapsed(self) -> float | None:
        return None if self.started is None or self.finished is None else round(self.finished - self.started, 2)


def start_session_thread(name: str, ledger: CaseLedger, url, statement: str, outcome: SessionOutcome, pid_known: threading.Event, done: threading.Event,
                         *, lock_timeout: str, kind: str) -> threading.Thread:
    """
    One bounded session in its own daemon thread. `kind` "ddl" runs `statement` in a transaction and commits; "read" runs it as a plain query.
    The backend pid is published BEFORE the statement starts, so the main thread can watch exactly that backend.
    """
    def work() -> None:
        engine = create_engine(url, pool_size=1, max_overflow=0, pool_timeout=15, connect_args={"connect_timeout": 15, "application_name": name})
        conn = None
        try:
            conn = engine.connect()
            conn.execute(text(f"SET lock_timeout = '{lock_timeout}'"))
            conn.execute(text("SET statement_timeout = '40s'"))
            conn.commit()
            outcome.pid = conn.execute(text("SELECT pg_backend_pid()")).scalar()
            conn.commit()
            outcome.started = time.monotonic()
            pid_known.set()
            if kind == "ddl":
                conn.execute(text(statement))
                conn.commit()
                outcome.committed = True
            else:
                outcome.result = conn.execute(text(statement)).scalar()
                conn.commit()
                outcome.committed = True
        except DBAPIError as exc:
            outcome.sqlstate, outcome.error = getattr(exc.orig, "sqlstate", None), type(exc.orig).__name__
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001 -- reported through the outcome
                    pass
        except BaseException as exc:  # noqa: BLE001 -- never raised inside a thread
            outcome.error = type(exc).__name__
        finally:
            outcome.finished = time.monotonic()
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            engine.dispose()
            pid_known.set()
            done.set()

    thread = threading.Thread(target=work, name=name, daemon=True)
    ledger.threads.append(thread)
    thread.start()
    return thread


@dataclass
class LockRecord:
    scenario: str
    case: str = "C7"
    actions: list = field(default_factory=list)
    tables: list = field(default_factory=list)
    backup_role_access_share: dict = field(default_factory=dict)
    backup_privileges_before: dict = field(default_factory=dict)
    backup_privileges_after: dict = field(default_factory=dict)
    ordinary: dict = field(default_factory=dict)
    explicit: dict = field(default_factory=dict)
    verification: dict = field(default_factory=dict)


def backup_table_privileges(admin: Engine) -> dict:
    """{table: the privileges the backup role holds on it}, from has_table_privilege (admin, read-only)."""
    privileges = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
    result = {}
    for table in public_tables(admin):
        result[table] = tuple(p for p in privileges if scalar(admin, "SELECT has_table_privilege(:r, CAST(:t AS regclass), :p)", r=BACKUP_ROLE, t=f"public.{table}", p=p))
    return result


def run_lock_observation_case(harness_: Harness, ledger: CaseLedger) -> LockRecord:
    """The backup role's ACCESS SHARE capability, then the relation locks of an ordinary baseline read versus the explicit-lock variant (no dump)."""
    cluster = harness_.cluster
    record = LockRecord("c7_lock_observation")
    log = record.actions.append
    workspace = prepare_case_workspace(cluster, ledger)
    source = workspace.source_database
    log("source_provisioned")
    admin = cluster.admin_engine(source)
    ledger.engines.append(admin)
    record.tables = public_tables(admin)
    record.backup_privileges_before = backup_table_privileges(admin)
    fresh_engine = _fresh_engine(cluster, source, ledger, "napsedge_c7_backup_role_probe")

    # ---- the backup role, with its EXISTING grants, can take the proposed lock (and nothing stronger) -----------------------------------------------
    with fresh_engine.connect() as conn:
        taken = 0
        for table in record.tables:
            conn.execute(text(f'LOCK TABLE public."{table}" IN ACCESS SHARE MODE'))
            taken += 1
        mine = granted_table_locks(admin, scalar(conn, "SELECT pg_backend_pid()"))
        conn.commit()
    denied = {}
    for mode in ("ACCESS EXCLUSIVE", "ROW EXCLUSIVE"):
        with fresh_engine.connect() as conn:
            try:
                conn.execute(text(f"LOCK TABLE public.teams IN {mode} MODE"))
                denied[mode] = None
            except DBAPIError as exc:
                denied[mode] = getattr(exc.orig, "sqlstate", None)
                conn.rollback()
    record.backup_role_access_share = {"tables_locked": taken, "locked_modes": sorted({m for modes in mine.values() for m in modes}), "denied_sqlstate": denied}
    log("backup_role_access_share_capability_checked")

    # ---- ordinary baseline reads: which relation locks, and WHEN ---------------------------------------------------------------------------------------
    owner = SnapshotOwner(cluster, source, ledger)
    owner.begin_and_export()
    before = granted_table_locks(admin, owner.pid)
    baseline = capture_baseline(owner.connection)
    after = granted_table_locks(admin, owner.pid)
    owner.end_transaction()
    record.ordinary = {"locked_after_export_before_reads": before, "locked_after_reads": after, "baseline_tables": sorted(baseline.fingerprints)}
    log("ordinary_read_locks_observed")

    # ---- the proposal's explicit-lock variant: ACCESS SHARE on every table BEFORE the export -------------------------------------------------------
    explicit_owner = SnapshotOwner(cluster, source, ledger)
    _begin_with_explicit_locks(explicit_owner, record.tables)
    before_x = granted_table_locks(admin, explicit_owner.pid)
    capture_baseline(explicit_owner.connection)
    after_x = granted_table_locks(admin, explicit_owner.pid)
    explicit_owner.end_transaction()
    record.explicit = {"locked_after_lock_before_reads": before_x, "locked_after_reads": after_x}
    record.backup_privileges_after = backup_table_privileges(admin)
    log("explicit_variant_locks_observed")
    return record


@dataclass
class QueueRecord:
    scenario: str
    case: str = "C7"
    queued: bool = True
    actions: list = field(default_factory=list)
    owner_pid: int = 0
    owner_table_locks: dict = field(default_factory=dict)
    ddl: SessionOutcome | None = None
    reader: SessionOutcome | None = None
    ddl_waiting: dict = field(default_factory=dict)
    reader_waiting: dict = field(default_factory=dict)
    reader_done_while_ddl_queued: bool | None = None
    ungranted_locks_seen_by_control: int = 0
    column_present_after: int | None = None
    owner_state_at_end: str | None = None
    descriptor_unchanged: bool | None = None
    verification: dict = field(default_factory=dict)


PROBE_COLUMN = "catalog_probe_col"


def run_queue_case(harness_: Harness, ledger: CaseLedger, *, queue_exclusive_request: bool) -> QueueRecord:
    """
    The owner holds its transaction open (its baseline reads hold ACCESS SHARE on every table). When `queue_exclusive_request` is true a bounded migrator
    DDL that genuinely requests ACCESS EXCLUSIVE is started and CONFIRMED queued from pg_locks, pg_stat_activity and pg_blocking_pids; only then a fresh
    ordinary reader (the runtime role) is started and observed. The control starts the same reader with nothing queued.
    """
    cluster = harness_.cluster
    record = QueueRecord("c7_queued_exclusive_request" if queue_exclusive_request else "c7_control_no_queued_request", queued=queue_exclusive_request)
    log = record.actions.append
    workspace = prepare_case_workspace(cluster, ledger)
    source = workspace.source_database
    log("source_provisioned")
    admin = cluster.admin_engine(source)
    ledger.engines.append(admin)
    owner = SnapshotOwner(cluster, source, ledger)
    owner.begin_and_export()
    capture_baseline(owner.connection)  # ordinary reads: ACCESS SHARE on every table is now held by the owner, until its transaction ends
    record.owner_pid = owner.pid
    record.owner_table_locks = granted_table_locks(admin, owner.pid)
    log("owner_open_holding_locks")

    ddl_known, ddl_done = threading.Event(), threading.Event()
    if queue_exclusive_request:
        record.ddl = SessionOutcome()
        start_session_thread("napsedge_c7_migrator_ddl", ledger, cluster.migrator_url(source), f"ALTER TABLE public.teams ADD COLUMN {PROBE_COLUMN} integer",
                             record.ddl, ddl_known, ddl_done, lock_timeout=DDL_LOCK_TIMEOUT, kind="ddl")
        if not ddl_known.wait(BARRIER_TIMEOUT_S) or record.ddl.pid is None:
            raise PreconditionFailed("the migrator session did not start")
        log("ddl_started")
        # confirmed QUEUED, from the server's own views, not from elapsed time
        if not wait_until(lambda: waiting_modes(admin, record.ddl.pid, "teams") == ["AccessExclusiveLock"] and owner.pid in blocking_pids(admin, record.ddl.pid), 8):
            raise PreconditionFailed("the exclusive request never appeared as a waiting lock blocked by the owner")
        record.ddl_waiting = {"waiting_modes": waiting_modes(admin, record.ddl.pid, "teams"), "blocked_by": blocking_pids(admin, record.ddl.pid), "activity": activity(admin, record.ddl.pid)}
        log("exclusive_request_confirmed_queued")

    reader_known, reader_done = threading.Event(), threading.Event()
    record.reader = SessionOutcome()
    start_session_thread("napsedge_c7_runtime_reader", ledger, cluster.runtime_url(source), "SELECT count(*) FROM public.teams", record.reader, reader_known, reader_done,
                         lock_timeout="30s", kind="read")
    if not reader_known.wait(BARRIER_TIMEOUT_S) or record.reader.pid is None:
        raise PreconditionFailed("the reader session did not start")
    log("reader_started")
    if queue_exclusive_request:
        # the reader is waiting behind the QUEUED exclusive request (not behind the owner, whose ACCESS SHARE is compatible with its own)
        if not wait_until(lambda: waiting_modes(admin, record.reader.pid, "teams") == ["AccessShareLock"] and record.ddl.pid in blocking_pids(admin, record.reader.pid), 8):
            raise PreconditionFailed("the ordinary reader was never observed waiting behind the queued exclusive request")
        record.reader_waiting = {"waiting_modes": waiting_modes(admin, record.reader.pid, "teams"), "blocked_by": blocking_pids(admin, record.reader.pid),
                                 "activity": activity(admin, record.reader.pid)}
        record.reader_done_while_ddl_queued = reader_done.is_set()
        log("reader_confirmed_waiting_behind_the_queued_request")
        if not ddl_done.wait(30):
            raise PreconditionFailed("the bounded DDL did not end within its deadline")
        log("ddl_ended")
    else:
        seen = {"ungranted": 0}

        def finished() -> bool:
            seen["ungranted"] += len([1 for r in rows(admin, "SELECT 1 FROM pg_locks WHERE NOT granted")])
            return reader_done.is_set()

        wait_until(finished, 15)
        record.ungranted_locks_seen_by_control = seen["ungranted"]
    if not reader_done.wait(BARRIER_TIMEOUT_S):
        raise PreconditionFailed("the ordinary reader never finished")
    log("reader_finished")
    record.column_present_after = scalar(admin, "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'teams' AND column_name = :c", c=PROBE_COLUMN)
    record.owner_state_at_end = backend_state(cluster, owner.pid)  # the owner was open the whole time
    record.descriptor_unchanged = owner.visibility_descriptor() == owner.descriptor
    owner.end_transaction()
    log("owner_ended_after_everything")
    return record


def _run_locks(harness_: Harness) -> LockRecord:
    with owned_case(harness_.cluster) as ledger:
        record = run_lock_observation_case(harness_, ledger)
    record.verification = ledger.verification
    return record


def _run_queue(harness_: Harness, queued: bool) -> QueueRecord:
    with owned_case(harness_.cluster) as ledger:
        record = run_queue_case(harness_, ledger, queue_exclusive_request=queued)
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
            except BaseException as exc:  # noqa: BLE001 -- cached and re-raised for every test that asks
                self._results[key] = ("error", exc)
        status, value = self._results[key]
        if status == "error":
            raise value
        return value

    def catalog(self, name: str) -> CatalogRecord:
        return self._get(("catalog", name), lambda: _run_catalog(self.harness, name))

    def locks(self) -> LockRecord:
        return self._get(("locks",), lambda: _run_locks(self.harness))

    def queue(self, queued: bool) -> QueueRecord:
        return self._get(("queue", queued), lambda: _run_queue(self.harness, queued))


@pytest.fixture(scope="module")
def runs(harness, probe_role):
    return Registry(harness)


@pytest.fixture(scope="module")
def probe_role(harness):
    """A disposable NOLOGIN role (the receiver in the owner-change scenario). Dropped, and verified absent, after every case database is gone."""
    cluster = harness.cluster
    admin = cluster.admin_engine()
    try:
        with admin.connect() as conn:
            conn.execute(text(f"CREATE ROLE {PROBE_ROLE} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"))
        yield PROBE_ROLE
    finally:
        with admin.connect() as conn:
            conn.execute(text(f"DROP ROLE IF EXISTS {PROBE_ROLE}"))
            leftover = conn.execute(text("SELECT count(*) FROM pg_roles WHERE rolname = :r"), {"r": PROBE_ROLE}).scalar()
        admin.dispose()
        assert leftover == 0, "the probe role was not dropped"


# ==========================================================================
# groupings of the scenarios (the OBSERVED behavior of each operation is asserted below; none of these lists is an assumption about the bracket)
# ==========================================================================
AS_WRITTEN = "as_written_owner_in_transaction"
OWNER_ENDED = "owner_after_its_transaction_ended"
CANDIDATE = "candidate_fresh_committed_transactions"
VARIANTS = (AS_WRITTEN, OWNER_ENDED, CANDIDATE)
ENDPOINTS = ("before_owner", "before_fresh", "baseline_catalog", "after_in_transaction", "after_fresh", "after_owner_ended")

COMMITTING = [
    "c6_index_after_export", "c6_acl_after_export", "c6_owner_after_export", "c6_index_after_reads", "c6_acl_after_reads",
    "c7_index_change_after_export_with_explicit_locks", "c7_acl_change_after_export_with_explicit_locks",
]
BLOCKED = ["c6_owner_after_reads", "c7_owner_change_after_export_with_explicit_locks"]
FLOW_SCENARIOS = [s for s in CATALOG_SCENARIOS if s != "c6_candidate_gate_refuses_index_change"]
EXPECTED_CATEGORY = {"index": "indexes", "acl": "acls", "owner": "acls"}
EXPECTED_KEY = {"index": ("teams", INDEX_NAME), "acl": ("conferences",), "owner": ("conferences",)}


def category_of(name: str) -> str:
    return EXPECTED_CATEGORY[CATALOG_SCENARIOS[name].change]


def key_of(name: str) -> tuple:
    return EXPECTED_KEY[CATALOG_SCENARIOS[name].change]


XFAIL_AS_WRITTEN = pytest.mark.xfail(strict=True, reason="PROPOSAL NOT MET: the catalog bracket as written (the owner re-reading inside its snapshot transaction) does not detect a committed catalog change")


# ==========================================================================
# scope and the pure pieces (no cluster needed)
# ==========================================================================
def test_only_c6_and_c7_are_in_scope_and_c9_and_c10_stay_unvalidated():
    assert VALIDATED_CASES == ("C6", "C7") and UNVALIDATED_CASES == ("C9", "C10")
    assert {s.case for s in CATALOG_SCENARIOS.values()} <= set(VALIDATED_CASES)


def _observation(**changes) -> CatalogObservation:
    values = {"constraints": (("t", "ck", "c", True, "CHECK (x > 0)"),), "indexes": (("t", "ix", "CREATE INDEX ix ON t (x)"),), "acls": (("t", "r", "owner_a", "owner_a=arwdDxt/owner_a"),)}
    values.update(changes)
    return CatalogObservation(**values)


def test_digests_are_separate_per_category_and_change_only_for_the_category_that_changed():
    base = _observation()
    for category, changed in (("constraints", (("t", "ck", "c", True, "CHECK (x > 1)"),)), ("indexes", ()), ("acls", (("t", "r", "owner_b", "owner_b=arwdDxt/owner_b"),))):
        other = _observation(**{category: changed})
        assert changed_categories(base, other) == [category] and catalog_changed(base, other)
        assert all(base.digests[c] == other.digests[c] for c in CATEGORIES if c != category)
    assert not catalog_changed(base, _observation()) and base.digests["combined"] == _observation().digests["combined"]


def test_raw_differences_reuse_the_existing_keyed_diff_per_category():
    before, after = _observation(), _observation(indexes=(("t", "ix", "CREATE INDEX ix ON t (x)"), ("t", "ix2", "CREATE INDEX ix2 ON t (y)")))
    diff = raw_differences(before, after)
    assert set(diff["indexes"]) == {("t", "ix2")} and diff["constraints"] == {} and diff["acls"] == {}
    assert diff["indexes"][("t", "ix2")] == (None, ("t", "ix2", "CREATE INDEX ix2 ON t (y)"))


def test_the_catalog_changed_guard_refuses_a_difference_and_names_the_category():
    require_catalog_unchanged(_observation(), _observation(), "control")
    with pytest.raises(PreconditionFailed, match=r"catalog_changed \(x\): \['acls'\]"):
        require_catalog_unchanged(_observation(), _observation(acls=()), "x")


def test_condition_polling_is_bounded_and_returns_as_soon_as_the_condition_holds():
    calls = {"n": 0}

    def third_time() -> bool:
        calls["n"] += 1
        return calls["n"] >= 3

    assert wait_until(third_time, 5, 0.01) is True and calls["n"] == 3
    started = time.monotonic()
    assert wait_until(lambda: False, 0.3, 0.05) is False and time.monotonic() - started < 3


_DDL = re.compile(r"(CREATE INDEX|DROP INDEX|GRANT|REVOKE|ALTER TABLE|LOCK TABLE|ALTER SEQUENCE)")  # word boundaries: pg_locks has a column named "granted"


def test_observation_and_lock_reading_code_contains_no_ddl_or_lock_statement_and_never_grants_the_backup_role_anything():
    import inspect

    for function in (observe_catalog, changed_categories, raw_differences, catalog_changed, require_catalog_unchanged, _fresh_observation, relation_locks,
                     granted_table_locks, waiting_modes, blocking_pids, activity, backup_table_privileges, wait_until):
        source = inspect.getsource(function).upper()
        assert not _DDL.search(source), function.__name__
    assert all(BACKUP_ROLE not in step for change in CHANGES.values() for step in change.steps), "no change grants anything to the backup role"
    assert all(step.startswith(("CREATE INDEX", "DROP INDEX", "GRANT INSERT ON public.conferences TO " + RUNTIME_ROLE, "ALTER TABLE public.conferences OWNER TO")) for c in CHANGES.values() for step in c.steps)


# ==========================================================================
# C6 -- what committed, what blocked, what was visible to a fresh observer
# ==========================================================================
@pytest.mark.parametrize("name", COMMITTING)
def test_c6_each_intended_change_committed_and_was_visible_to_a_fresh_observer(name, runs):
    r = runs.catalog(name)
    assert r.change["committed"] is True and r.change["blocked"] is False and r.change["committed_steps"] == r.change["steps"] == 1
    assert r.fresh_visible_categories == [category_of(name)], "a fresh committed observer sees the change, in its own category only"
    assert r.change["after_each_step"] == [[category_of(name)]]
    assert "change_visible_to_a_fresh_observer" in r.actions


@pytest.mark.parametrize("name", BLOCKED)
def test_c6_a_blocked_change_is_reported_as_blocked_and_never_counts_as_detection(name, runs):
    r = runs.catalog(name)
    assert (r.change["committed"], r.change["blocked"], r.change["sqlstate"], r.change["committed_steps"]) == (False, True, "55P03", 0), "ACCESS EXCLUSIVE waited behind the owner's ACCESS SHARE and timed out"
    assert 4 <= r.change["elapsed_s"] <= 15, "the wait was bounded by the lock timeout"
    assert r.fresh_visible_categories == [] and all(not d["changed"] for d in r.detection.values()), "nothing changed, so nothing was detected or missed"
    assert not any(a.startswith("change_visible") for a in r.actions)


def test_c6_the_owner_change_commits_before_the_owner_holds_table_locks_and_blocks_after_them(runs):
    early, late, explicit = runs.catalog("c6_owner_after_export"), runs.catalog("c6_owner_after_reads"), runs.catalog("c7_owner_change_after_export_with_explicit_locks")
    assert early.change["committed"] and not late.change["committed"] and not explicit.change["committed"]
    assert explicit.explicit_lock_count == 24, "the explicit variant held ACCESS SHARE on every application table before the export"
    assert [a for a in explicit.actions if a in {"explicit_access_share_locks_taken_before_export", "snapshot_exported", "change_attempted:blocked"}] == [
        "explicit_access_share_locks_taken_before_export", "snapshot_exported", "change_attempted:blocked"]


# ---- the proposal AS WRITTEN: characterization of what it does (these PASS; they do not make the safety requirement pass) ----------------------
@pytest.mark.parametrize("name", COMMITTING)
def test_c6_characterization_the_bracket_as_written_misses_every_committed_change(name, runs):
    r = runs.catalog(name)
    assert r.change["committed"] and r.fresh_visible_categories == [category_of(name)], "the change really committed and was visible: this is a MISS, not a blocked change"
    assert r.detection[AS_WRITTEN] == {"changed": False, "categories": []}
    assert r.before_owner.digests == r.after_in_transaction.digests, "all three category digests are identical at the owner's two endpoints"
    assert all(diff == {} for diff in r.differences[AS_WRITTEN].values())
    assert r.outcome == "finalization_permitted" and r.finalization_invoked, "in the real flow the as-written guard let the capture through to finalization"


@pytest.mark.parametrize("name", COMMITTING)
def test_c6_the_blind_spot_is_snapshot_blindness_and_not_absence_of_the_change(name, runs):
    r = runs.catalog(name)
    expected = [category_of(name)]
    assert r.detection[OWNER_ENDED]["categories"] == expected, "the SAME owner session sees the change once its snapshot transaction has ended"
    assert r.detection[CANDIDATE]["categories"] == expected, "a fresh committed transaction sees it too"
    assert r.detection[AS_WRITTEN]["categories"] == [], "while the owner is still inside its REPEATABLE READ transaction its catalog reads are snapshot-bound and cannot see it"
    assert r.baseline_catalog.digests == r.after_in_transaction.digests, "the baseline's own reads and the owner's later reads agree with each other (both blind)"


@pytest.mark.parametrize("name", COMMITTING)
def test_c6_raw_differences_and_digests_are_recorded_separately_for_each_category(name, runs):
    r = runs.catalog(name)
    for variant in (OWNER_ENDED, CANDIDATE):
        diff = r.differences[variant]
        assert set(diff[category_of(name)]) == {key_of(name)}, (variant, diff)
        assert all(diff[c] == {} for c in CATEGORIES if c != category_of(name)), "no other category differs"
    before, after = r.before_fresh.digests, r.after_fresh.digests
    assert [c for c in CATEGORIES if before[c] != after[c]] == [category_of(name)] and before["combined"] != after["combined"]


# ---- the safety requirement itself: UNMET (strict xfail, so an unexpected change in behavior fails loudly) ---------------------------------------
@pytest.mark.parametrize("name", [pytest.param(n, marks=XFAIL_AS_WRITTEN) for n in COMMITTING])
def test_c6_safety_requirement_the_bracket_as_written_detects_a_committed_change_and_refuses_finalization(name, runs):
    r = runs.catalog(name)
    assert r.change["committed"]
    assert r.detection[AS_WRITTEN]["changed"] is True
    assert r.outcome == "catalog_changed" and not r.finalization_invoked


def test_c6_the_control_without_a_change_is_equal_at_every_endpoint_for_every_variant(runs):
    r = runs.catalog("c6_control")
    assert r.change is None and all(not d["changed"] for d in r.detection.values())
    assert r.before_owner.digests == r.after_in_transaction.digests == r.after_owner_ended.digests == r.before_fresh.digests == r.after_fresh.digests
    assert r.outcome == "finalization_permitted" and r.finalization_invoked


@pytest.mark.parametrize("name", COMMITTING)
def test_c6_observation_only_these_particular_missed_changes_produced_no_baseline_versus_archive_difference(name, runs):
    """Reported as observed, NOT as a safety claim: baseline and archive were both snapshot-bound for these changes, so the strict comparator found them equal."""
    r = runs.catalog(name)
    assert r.harm == {"fingerprints_equal": True, "indexes_equal": True, "constraints_equal": True, "acls_equal": True, "owners_equal": True}
    assert r.comparison.fingerprint_diffs == {}


def test_c6_a_change_and_its_reversal_is_invisible_to_every_endpoint_comparison_by_construction(runs):
    r = runs.catalog("c6_index_change_and_reversal")
    assert r.change["committed"] and r.change["committed_steps"] == 2
    assert r.change["after_each_step"] == [["indexes"], []], "a fresh observer saw the index appear and then disappear: both steps really committed"
    assert all(not d["changed"] for d in r.detection.values()), "equality at two endpoints cannot show that something changed in between, for ANY variant"
    assert r.outcome == "finalization_permitted"


# ---- the candidate (an ALTERNATIVE that needs later design review) ----------------------------------------------------------------------------
@pytest.mark.parametrize("name", COMMITTING)
def test_c6_candidate_fresh_committed_observation_detects_every_committed_change_without_touching_the_owner(name, runs):
    r = runs.catalog(name)
    assert r.detection[CANDIDATE] == {"changed": True, "categories": [category_of(name)]}
    assert r.owner_connects_after_baseline == 0, "the data-snapshot reads stayed on the owner connection"
    assert r.owner_state_at_dump_exit == "idle in transaction" and r.descriptor_at_export == r.descriptor_after_dump, "the owner was neither changed nor ended"


def test_c6_candidate_refusal_runs_in_the_real_flow_and_prevents_restore_and_finalization(runs):
    r = runs.catalog("c6_candidate_gate_refuses_index_change")
    assert r.change["committed"] and r.detection[CANDIDATE]["changed"] and r.outcome == "catalog_changed"
    assert r.actions[-2:] == ["second_endpoint_observed", "catalog_changed_guard_refused"]
    assert r.finalization_invoked is False and r.destination_database is None and r.restore_failure is None and r.artifact_listing == []
    assert not {"destination_verified_empty", "archive_validated", "restored", "finalization_permitted"} & set(r.actions)


def test_c6_candidate_gate_passes_the_control_and_the_flow_completes(runs):
    r = runs.catalog("c6_candidate_gate_passes_control")
    assert r.outcome == "finalization_permitted" and r.finalization_invoked and "catalog_guard_passed" in r.actions and r.harm["acls_equal"]


@pytest.mark.parametrize("name", FLOW_SCENARIOS)
def test_data_snapshot_reads_stayed_on_the_owner_connection_in_every_flow(name, runs):
    r = runs.catalog(name)
    assert r.owner_connects_after_baseline == 0
    assert len(r.owner_locked_tables_after_baseline) == 24, "the server shows the OWNER backend holding ACCESS SHARE on every table: the baseline data reads ran in its transaction"
    order = [a for a in r.actions if a in {"snapshot_exported", "baseline_data_read_through_owner", "dump_started"}]
    assert order == ["snapshot_exported", "baseline_data_read_through_owner", "dump_started"]
    assert r.dump.returncode == 0 and r.owner_state_at_dump_exit == "idle in transaction" and r.descriptor_at_export == r.descriptor_after_dump


# ==========================================================================
# C7 -- ACCESS SHARE capability, observed locks, queued exclusive requests, reader impact
# ==========================================================================
def test_c7_the_backup_role_can_take_access_share_with_its_existing_privileges_and_nothing_stronger(runs):
    r = runs.locks()
    assert r.backup_role_access_share["tables_locked"] == len(r.tables) == 24
    assert r.backup_role_access_share["locked_modes"] == ["AccessShareLock"]
    assert r.backup_role_access_share["denied_sqlstate"] == {"ACCESS EXCLUSIVE": "42501", "ROW EXCLUSIVE": "42501"}, "insufficient_privilege: the grants were not widened"
    assert r.backup_privileges_before == r.backup_privileges_after and all(p == ("SELECT",) for p in r.backup_privileges_before.values()), "the backup role holds SELECT only, before and after"


def test_c7_ordinary_baseline_reads_hold_access_share_on_every_table_but_only_after_the_reads(runs):
    r = runs.locks()
    assert r.ordinary["locked_after_export_before_reads"] == {}, "right after the export the owner holds no application-table lock"
    after = r.ordinary["locked_after_reads"]
    assert set(after) == set(r.tables) and all(modes == {"AccessShareLock"} for modes in after.values())


def test_c7_the_explicit_variant_holds_the_same_locks_but_from_before_the_export(runs):
    r = runs.locks()
    before, after = r.explicit["locked_after_lock_before_reads"], r.explicit["locked_after_reads"]
    assert set(before) == set(r.tables) and all(modes == {"AccessShareLock"} for modes in before.values())
    assert before == after == r.ordinary["locked_after_reads"], "the explicit variant changes WHEN the locks are held, not WHICH"


def test_c7_access_share_does_not_stop_index_or_acl_changes_even_when_taken_before_the_export(runs):
    for name in ("c7_index_change_after_export_with_explicit_locks", "c7_acl_change_after_export_with_explicit_locks"):
        r = runs.catalog(name)
        assert r.explicit_lock_count == 24 and r.change["committed"] and r.fresh_visible_categories == [category_of(name)], name
        assert r.detection[AS_WRITTEN]["changed"] is False and r.detection[CANDIDATE]["changed"] is True, name


def test_c7_the_exclusive_request_is_confirmed_queued_behind_the_owner_from_the_servers_own_views(runs):
    r = runs.queue(True)
    assert len(r.owner_table_locks) == 24 and all("AccessShareLock" in modes for modes in r.owner_table_locks.values())
    assert r.ddl_waiting["waiting_modes"] == ["AccessExclusiveLock"] and r.owner_pid in r.ddl_waiting["blocked_by"]
    assert (r.ddl_waiting["activity"]["wait_event_type"], r.ddl_waiting["activity"]["wait_event"]) == ("Lock", "relation")


def test_c7_the_bounded_ddl_timed_out_rolled_back_and_did_not_commit(runs):
    r = runs.queue(True)
    assert (r.ddl.committed, r.ddl.sqlstate, r.ddl.error) == (False, "55P03", "LockNotAvailable")
    assert 9.0 <= r.ddl.elapsed <= 20.0, "it waited for its bounded lock timeout and no longer"
    assert r.column_present_after == 0, "the DDL left no trace"


def test_c7_a_fresh_ordinary_reader_waits_behind_the_queued_request_not_behind_the_owner(runs):
    r = runs.queue(True)
    assert r.reader_waiting["waiting_modes"] == ["AccessShareLock"]
    assert r.ddl.pid in r.reader_waiting["blocked_by"] and r.owner_pid not in r.reader_waiting["blocked_by"], "it is blocked by the queued exclusive request; the owner's own lock is compatible with it"
    assert (r.reader_waiting["activity"]["wait_event_type"], r.reader_waiting["activity"]["wait_event"]) == ("Lock", "relation")
    assert r.reader_done_while_ddl_queued is False


def test_c7_the_reader_recovered_after_the_ddl_timed_out_while_the_owner_stayed_open(runs):
    r = runs.queue(True)
    steps = [a for a in r.actions if a in {"exclusive_request_confirmed_queued", "reader_started", "reader_confirmed_waiting_behind_the_queued_request", "ddl_ended", "reader_finished", "owner_ended_after_everything"}]
    assert steps == ["exclusive_request_confirmed_queued", "reader_started", "reader_confirmed_waiting_behind_the_queued_request", "ddl_ended", "reader_finished", "owner_ended_after_everything"]
    assert (r.reader.committed, r.reader.result, r.reader.error) == (True, 3, None), "the reader completed and returned the seeded team count"
    assert r.owner_state_at_end == "idle in transaction" and r.descriptor_unchanged is True, "the snapshot owner was open the whole time"


def test_c7_control_without_a_queued_request_the_reader_does_not_wait(runs):
    r = runs.queue(False)
    assert r.ddl is None and r.reader_waiting == {} and r.ungranted_locks_seen_by_control == 0, "no ungranted lock existed at any poll"
    assert (r.reader.committed, r.reader.result) == (True, 3) and r.reader.elapsed < 3
    assert r.owner_state_at_end == "idle in transaction" and r.column_present_after == 0


def test_c7_availability_cost_is_recorded_without_choosing_a_policy(runs):
    queued, control = runs.queue(True), runs.queue(False)
    cost = queued.reader.elapsed - control.reader.elapsed
    assert cost > 3, f"an ordinary read stalled for about the remaining lock timeout ({queued.reader.elapsed}s against {control.reader.elapsed}s)"
    assert 0 < cost <= float(DDL_LOCK_TIMEOUT.rstrip("s")) + 5, "bounded by the migrator's lock timeout"
    # no function in this module selects or sets a production lock timeout policy: the timeout used is a TEST value, recorded here
    assert DDL_LOCK_TIMEOUT == "10s"


# ==========================================================================
# cleanup, credentials, recorded evidence
# ==========================================================================
def _verified(record) -> dict:
    return record.verification


@pytest.mark.parametrize("name", list(CATALOG_SCENARIOS))
def test_cleanup_was_verified_for_each_catalog_scenario(name, runs):
    v = _verified(runs.catalog(name))
    assert v["problems"] == [] and v["databases_remaining"] == [] and v["sessions_in_dropped_databases"] == 0
    assert v["leftover_dump_or_owner_sessions"] == 0 and v["container_files_remaining"] == [] and v["threads_alive"] == [] and v["directories_remaining"] == []


@pytest.mark.parametrize("which", ["locks", "queued", "control"])
def test_cleanup_was_verified_for_each_c7_scenario(which, runs):
    v = _verified(runs.locks() if which == "locks" else runs.queue(which == "queued"))
    assert v["problems"] == [] and v["databases_remaining"] == [] and v["sessions_in_dropped_databases"] == 0
    assert v["leftover_dump_or_owner_sessions"] == 0 and v["container_files_remaining"] == [] and v["threads_alive"] == [] and v["directories_remaining"] == []


def test_the_probe_role_is_dropped_when_the_module_ends_and_no_case_database_outlives_its_case(harness, runs):
    runs.catalog("c6_control")
    admin = harness.cluster.admin_engine()
    try:
        leftovers = rows(admin, "SELECT datname FROM pg_database WHERE datname LIKE 'napsedge_pg_test_%' AND datname NOT LIKE 'napsedge_pg_test_admin_%' AND datname NOT LIKE 'napsedge_pg_test_boot_%'")
    finally:
        admin.dispose()
    assert leftovers == [], "every case database was dropped by its own ledger"


@pytest.mark.parametrize("name", FLOW_SCENARIOS)
def test_no_credential_appears_in_any_recorded_command_or_output(name, runs, harness):
    record = runs.catalog(name)
    secrets_ = harness.cluster.secrets_to_scrub()
    assert record.commands
    for command in record.commands:
        blob = " ".join(command.argv) + command.stdout + command.stderr
        assert not any(secret and secret in blob for secret in secrets_) and "PGPASSWORD" not in blob and "password=" not in blob.lower()


@pytest.mark.parametrize("name", list(CATALOG_SCENARIOS))
def test_observed_catalog_facts_are_recorded_in_the_junit_evidence(name, request, runs):
    """Operations, commit/block, detection per variant, per-endpoint digests and raw differences per category, outcome and cleanup; no credential."""
    r = runs.catalog(name)
    s = CATALOG_SCENARIOS[name]
    props = request.node.user_properties
    props += [("scenario", f"{r.scenario} ({r.case})"), ("window", s.window), ("gate", s.gate), ("explicit_locks", str(s.explicit_locks)), ("change", str(r.change)),
              ("fresh_visible_categories", ",".join(r.fresh_visible_categories) or "none"), ("outcome", r.outcome), ("finalization_invoked", str(r.finalization_invoked)),
              ("harm", str(r.harm)), ("actions", ",".join(r.actions)), ("cleanup_problems", str(len(r.verification["problems"])))]
    for variant, result in r.detection.items():
        props.append((f"detect:{variant}", f"changed={result['changed']} categories={','.join(result['categories']) or 'none'}"))
    for endpoint in ENDPOINTS:
        observation = getattr(r, endpoint)
        props.append((f"digest:{endpoint}", " ".join(f"{c}={observation.digests[c]}" for c in (*CATEGORIES, "combined"))))
    for variant, per_category in r.differences.items():
        for category, diff in per_category.items():
            props.append((f"diff:{variant}:{category}", (str(diff)[:700] if diff else "none")))
    assert not r.verification["problems"]


def test_observed_lock_facts_are_recorded_in_the_junit_evidence(request, runs):
    r, queued, control = runs.locks(), runs.queue(True), runs.queue(False)
    props = request.node.user_properties
    summarize = lambda locks: f"{len(locks)} tables; modes={','.join(sorted({m for v in locks.values() for m in v})) or 'none'}"  # noqa: E731
    props += [("backup_role_access_share", str(r.backup_role_access_share)), ("backup_privileges_unchanged", str(r.backup_privileges_before == r.backup_privileges_after)),
              ("ordinary_locks_after_export_before_reads", summarize(r.ordinary["locked_after_export_before_reads"])), ("ordinary_locks_after_reads", summarize(r.ordinary["locked_after_reads"])),
              ("explicit_locks_before_reads", summarize(r.explicit["locked_after_lock_before_reads"])), ("explicit_locks_after_reads", summarize(r.explicit["locked_after_reads"])),
              ("queue_ddl_waiting", str(queued.ddl_waiting)), ("queue_reader_waiting", str(queued.reader_waiting)),
              ("queue_ddl", f"committed={queued.ddl.committed} sqlstate={queued.ddl.sqlstate} elapsed_s={queued.ddl.elapsed}"),
              ("queue_reader", f"committed={queued.reader.committed} result={queued.reader.result} elapsed_s={queued.reader.elapsed}"),
              ("queue_column_present_after", str(queued.column_present_after)), ("queue_owner_state_at_end", f"{queued.owner_state_at_end} descriptor_unchanged={queued.descriptor_unchanged}"),
              ("control_reader", f"committed={control.reader.committed} result={control.reader.result} elapsed_s={control.reader.elapsed} ungranted_locks_seen={control.ungranted_locks_seen_by_control}"),
              ("availability_cost_s", str(round(queued.reader.elapsed - control.reader.elapsed, 2))), ("queue_actions", ",".join(queued.actions)),
              ("cleanup_problems", str(len(r.verification["problems"]) + len(queued.verification["problems"]) + len(control.verification["problems"])))]
    assert not (r.verification["problems"] or queued.verification["problems"] or control.verification["problems"])
