"""
Phase 9B-5C1: disposable validation of shared-snapshot TABLE-DATA consistency and
snapshot-owner lifetime. Specification section 7, cases C1, C2 and C8 ONLY.

What is (and is not) established
--------------------------------
* C1  A baseline read through the snapshot-owning transaction, and a real
      `pg_dump --snapshot=<exported id>` started AFTER another session committed a
      known change, describe the same table data: the restored fingerprints equal the
      baseline exactly and the post-snapshot change is absent.
* C2  The negative control: the same ordering with a dump that does NOT import the
      snapshot produces a specific, explained fingerprint difference and contains the
      committed change, so C1 is sensitive to a dump using the wrong snapshot.
* C8  If the snapshot owner is gone before `pg_dump` attaches, `pg_dump` fails with
      PostgreSQL's invalid-snapshot error (not an authentication, connectivity or
      permission failure), the harness records `snapshot_unavailable`, and nothing
      downstream (archive validation, restore, finalization) runs.

These cases say nothing about sequences, catalog consistency, DDL, locking, overlap,
restore verification (claim 4) or operational readiness. C3-C7, C9 and C10 are NOT
validated here (`UNVALIDATED_CASES`), and no production capture code exists.

Reuse
-----
One disposable `postgres:16-alpine` cluster (TLS verify-full, SCRAM, private pgpass
files inside the container) from the 9B-4 `launched_cluster` and ownership ledger;
the 9B-2 roles and backup-role grants, unchanged (no privilege is added so that a
capture can pass); the existing fingerprint SQL and comparison semantics; the
existing archive-ready guard and `pg_restore` wrapper. Each case gets its own
migrated, seeded database in that cluster and its own fresh restore target, because
the cases mutate data. Independence of clusters is not what is tested here (the
9B-4 rehearsal covers that).

Connection ownership (the critical contract)
--------------------------------------------
`capture_baseline(Engine)` opens a separate connection, and so a separate
transaction, for EVERY read: several reads through an Engine do not share a snapshot.
`pg_restore_compare` therefore accepts an open `Connection` as well, and then runs
every read on it, never opening, committing or closing it. This module passes the
snapshot owner's connection, asserts that the owner's engine opened no further
connection during the capture, and re-reads the data through the owner after the
change commits to show that those reads really are snapshot-bound.

Determinism and bounds
----------------------
Ordering uses threading events with timeouts, never sleeps. Every connection has a
connect timeout; the owner has statement and idle-in-transaction timeouts that exceed
the dump deadline; every subprocess has a timeout; the writer is a daemon thread with a
bounded join; cleanup (databases, sessions, container files, threads) is verified by a
per-case ledger on success and on failure.
"""
from __future__ import annotations

import contextlib
import re
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection

from src.deployment.backup_artifact_writer import FAILED_REASON_CLASSES
from tests.postgres.conftest import TEST_DB_PREFIX, assert_disposable_database_name
from tests.postgres.pg_restore_compare import DatabaseBaseline, all_fingerprints, capture_baseline, compare_to_baseline, describe_diff, scalar
from tests.postgres.test_postgres_fresh_cluster_recovery import (
    EXPECTED_TOC_ENTRY,
    PreconditionFailed,
    _sha256_in_container,
    bootstrap_sql,
    launched_cluster,
    owned_resources,
    psql_as_admin,
    require,
    require_archive_ready_for_restore,
)
from tests.postgres.test_postgres_roles_and_restore import (
    CONFERENCE_ID,
    MIGRATOR_ROLE,
    CommandResult,
    _create_database_owned_by_migrator,
    _drop_database,
)
from tests.postgres.test_postgres_tls_backup_restore import (
    BACKUP_ROLE,
    TlsCluster,
    _container_logs,
    _exec,
    authenticated_sessions,
    conninfo,
    isolated_database_environment,  # noqa: F401  (autouse module fixture: removes inherited PG* / NAPSEDGE_DATABASE_* variables)
    provision_database,
    remove_files,
    run_restore,
    write_pgpass,
)

pytestmark = pytest.mark.postgres

VALIDATED_CASES = ("C1", "C2", "C8")
UNVALIDATED_CASES = ("C3", "C4", "C5", "C6", "C7", "C9", "C10")

# the known, synthetic change a separate writer commits after the snapshot exists
CHANGED_TEAM_ID = 910004
CHANGED_TEAM_SLUG = "snapshot-change-team"
CHANGED_CONFERENCE_NAME = "Drill Conference (changed after the snapshot)"
EXPECTED_CHANGED_TABLES = {"teams", "conferences"}

BARRIER_TIMEOUT_S = 30
DUMP_TIMEOUT_S = 90
OWNER_STATEMENT_TIMEOUT = "30s"
OWNER_IDLE_TIMEOUT = "150s"  # longer than the dump deadline (specification 6.2 and 8)
OWNER_APP = "napsedge_snapshot_owner"
EXPORTED_ID = re.compile(r"[0-9A-F]{8}-[0-9A-F]{8}-[0-9]+")  # pg_export_snapshot(): an identifier a later session can import
VISIBILITY_DESCRIPTOR = re.compile(r"[0-9]+:[0-9]+:[0-9,]*")  # pg_current_snapshot(): xmin:xmax:xip_list, which is NOT importable
# stderr fragments that mean a failure of authentication, connectivity or permission rather than of the snapshot
NOT_A_SNAPSHOT_PROBLEM = ("authentication failed", "no pg_hba.conf entry", "could not connect", "connection to server", "SSL error",
                          "certificate", "permission denied", "FATAL", "timeout expired", "server closed the connection", "role \"")


class CaptureStepFailed(AssertionError):
    """A step ran and failed in a way the scenario did not expect (as opposed to a precondition that refused to let the next step start)."""


# --------------------------------------------------------------------------
# failure classification (test harness only; the production reason class is snapshot_unavailable)
# --------------------------------------------------------------------------
def classify_dump_failure(result: CommandResult) -> str:
    """
    `snapshot_unavailable` only when PostgreSQL reported an invalid snapshot identifier AND nothing in the
    output points at authentication, connectivity or permission; anything else is a plain `dump_failed`.
    """
    assert result.returncode != 0, "only a failed dump is classified"
    if "invalid snapshot identifier" in result.stderr and not any(fragment in result.stderr for fragment in NOT_A_SNAPSHOT_PROBLEM):
        return "snapshot_unavailable"
    return "dump_failed"


# --------------------------------------------------------------------------
# per-case ownership ledger: databases, sessions, container files, connections, threads -- removed AND verified, also on failure
# --------------------------------------------------------------------------
@dataclass
class CaseLedger:
    cluster: TlsCluster
    databases: list = field(default_factory=list)
    container_files: list = field(default_factory=list)
    connections: list = field(default_factory=list)
    engines: list = field(default_factory=list)
    threads: list = field(default_factory=list)
    directories: list = field(default_factory=list)
    verification: dict = field(default_factory=dict)


@contextlib.contextmanager
def owned_case(cluster: TlsCluster, *, remove_container_files: Callable = remove_files):
    """
    Everything a case creates is registered the moment it exists. On exit, whether the case passed or failed, this
    joins the writer thread, closes every connection, removes the container files and drops the databases, then
    VERIFIES that no registered database, session, file, thread or directory remains. A problem found is attached as a
    note to an exception already propagating (which is re-raised unchanged); otherwise it is raised as an AssertionError.
    """
    ledger = CaseLedger(cluster)
    primary: BaseException | None = None
    try:
        yield ledger
    except BaseException as exc:
        primary = exc
        raise
    finally:
        problems: list[str] = []

        def attempt(label: str, action: Callable) -> None:
            try:
                action()
            except Exception as exc:  # noqa: BLE001 -- a failure of one cleanup step must not skip the others or mask the primary failure
                problems.append(f"{label}: {type(exc).__name__}")

        for thread in ledger.threads:
            thread.join(timeout=15)
        for connection in reversed(ledger.connections):
            attempt("close connection", connection.close)
        for engine in ledger.engines:
            attempt("dispose engine", engine.dispose)
        if ledger.container_files:
            attempt("remove container files", lambda: remove_container_files(cluster, *ledger.container_files))
        for database in ledger.databases:
            attempt(f"drop database {database[-6:]}", lambda database=database: _drop_database(cluster, database))
        for directory in ledger.directories:
            attempt("remove directory", lambda directory=directory: _remove_tree(directory))

        verification: dict = {"threads_alive": [t.name for t in ledger.threads if t.is_alive()], "directories_remaining": [str(d) for d in ledger.directories if Path(d).exists()]}
        try:
            admin = cluster.admin_engine()
            try:
                with admin.connect() as conn:
                    names = list(ledger.databases)
                    verification["databases_remaining"] = [r[0] for r in conn.execute(text("SELECT datname FROM pg_database WHERE datname = ANY(CAST(:n AS text[]))"), {"n": names})]
                    verification["sessions_in_dropped_databases"] = conn.execute(
                        text("SELECT count(*) FROM pg_stat_activity WHERE datname = ANY(CAST(:n AS text[]))"), {"n": names}).scalar()
                    verification["leftover_dump_or_owner_sessions"] = conn.execute(
                        text("SELECT count(*) FROM pg_stat_activity WHERE application_name IN ('pg_dump', 'pg_restore', :owner) AND pid <> pg_backend_pid()"),
                        {"owner": OWNER_APP}).scalar()
            finally:
                admin.dispose()
            remaining: list = []
            for path in ledger.container_files:
                if _exec(cluster, ["test", "-e", path], timeout=30).returncode == 0:
                    remaining.append(path)
            verification["container_files_remaining"] = [Path(p).name for p in remaining]
        except Exception as exc:  # noqa: BLE001
            problems.append(f"could not verify cleanup: {type(exc).__name__}")
        ledger.verification = verification
        for key in ("threads_alive", "directories_remaining", "databases_remaining", "container_files_remaining"):
            if verification.get(key):
                problems.append(f"{key}: {verification[key]}")
        for key in ("sessions_in_dropped_databases", "leftover_dump_or_owner_sessions"):
            if verification.get(key):
                problems.append(f"{key}: {verification[key]}")
        verification["problems"] = problems
        if problems:
            message = "cleanup of resources created by the case failed or could not be verified: " + "; ".join(problems)
            if primary is not None:
                primary.add_note(message)
            else:
                raise AssertionError(message)


def _remove_tree(directory) -> None:
    import shutil

    shutil.rmtree(directory, ignore_errors=True)


# --------------------------------------------------------------------------
# the snapshot owner: one connection, as the backup role, owning the snapshot for its whole life
# --------------------------------------------------------------------------
class SnapshotOwner:
    """
    One connection as `napsedge_backup_ro` over verified TLS + SCRAM, in AUTOCOMMIT mode so that the transaction is exactly the
    one begun explicitly (`BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY`) and nothing else. Every baseline read is made on
    `self.connection`. `connects` counts connections the owner's engine opened: it must stay at one.
    """

    def __init__(self, cluster: TlsCluster, database: str, ledger: CaseLedger) -> None:
        self.cluster, self.database = cluster, database
        self.engine = create_engine(cluster.backup_url(database), isolation_level="AUTOCOMMIT", pool_size=1, max_overflow=0, pool_timeout=15,
                                    connect_args={"connect_timeout": 15, "application_name": OWNER_APP})
        self.connects: list[int] = []
        event.listen(self.engine, "connect", lambda dbapi_connection, record: self.connects.append(1))
        ledger.engines.append(self.engine)
        self.connection: Connection = self.engine.connect()
        ledger.connections.append(self.connection)
        self.pid: int = scalar(self.connection, "SELECT pg_backend_pid()")
        self.connection.execute(text(f"SET statement_timeout = '{OWNER_STATEMENT_TIMEOUT}'"))
        self.connection.execute(text(f"SET idle_in_transaction_session_timeout = '{OWNER_IDLE_TIMEOUT}'"))
        self.exported_id = ""
        self.descriptor = ""
        self.state: dict = {}

    def begin_and_export(self) -> str:
        self.connection.exec_driver_sql("BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        self.exported_id = scalar(self.connection, "SELECT pg_export_snapshot()")
        self.descriptor = self.visibility_descriptor()
        self.state = {
            "isolation": scalar(self.connection, "SHOW transaction_isolation"),
            "read_only": scalar(self.connection, "SHOW transaction_read_only"),
            "role": scalar(self.connection, "SELECT current_user"),
        }
        return self.exported_id

    def visibility_descriptor(self) -> str:
        return scalar(self.connection, "SELECT pg_current_snapshot()::text")

    def end_transaction(self) -> None:
        self.connection.exec_driver_sql("ROLLBACK")


def backend_state(cluster: TlsCluster, pid: int) -> str | None:
    """The server's own view of a backend (admin, read-only): None when the backend no longer exists."""
    admin = cluster.admin_engine()
    try:
        return scalar(admin, "SELECT state FROM pg_stat_activity WHERE pid = :p", p=pid)
    finally:
        admin.dispose()


def terminate_backend_and_confirm_gone(cluster: TlsCluster, pid: int) -> None:
    """Ends a backend from the server side and REQUIRES, within a deadline, that it no longer exists (termination is asynchronous)."""
    admin = cluster.admin_engine()
    try:
        require(bool(scalar(admin, "SELECT pg_terminate_backend(:p)", p=pid)), "the server did not accept the termination request for the snapshot owner")
        gone = threading.Event()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if scalar(admin, "SELECT count(*) FROM pg_stat_activity WHERE pid = :p", p=pid) == 0:
                gone.set()
                break
            threading.Event().wait(0.1)  # bounded condition wait with a deadline, not ordering by sleep
        require(gone.is_set(), "the snapshot owner backend still exists after termination was requested")
    finally:
        admin.dispose()


# --------------------------------------------------------------------------
# the separate writer: commits a known change only after the barrier is released
# --------------------------------------------------------------------------
@dataclass
class WriterOutcome:
    committed: bool = False
    error: str | None = None
    results: list = field(default_factory=list)  # whatever a custom writer action chooses to report (for example the values nextval returned)


def start_writer(cluster: TlsCluster, database: str, ledger: CaseLedger, release: threading.Event, done: threading.Event, outcome: WriterOutcome,
                 action: Callable[[Connection, WriterOutcome], None] | None = None) -> threading.Thread:
    """
    A daemon thread that waits for `release`, then runs ONE transaction as the migrator: `action(connection, outcome)` when given, otherwise the
    C1/C2 change (an INSERT into teams and an UPDATE of conferences). `done` is always set; failures are reported through `outcome`.
    """
    def work() -> None:
        engine = None
        try:
            if not release.wait(BARRIER_TIMEOUT_S):
                outcome.error = "the barrier was never released"
                return
            engine = create_engine(cluster.migrator_url(database), pool_size=1, max_overflow=0, pool_timeout=15, connect_args={"connect_timeout": 15})
            with engine.begin() as conn:
                conn.execute(text("SET LOCAL lock_timeout = '10s'"))
                conn.execute(text("SET LOCAL statement_timeout = '15s'"))
                if action is not None:
                    action(conn, outcome)
                else:
                    inserted = conn.execute(text("INSERT INTO teams (team_id, display_name, sr_slug) VALUES (:i, 'Snapshot Change Team', :s)"),
                                            {"i": CHANGED_TEAM_ID, "s": CHANGED_TEAM_SLUG}).rowcount
                    updated = conn.execute(text("UPDATE conferences SET canonical_name = :n WHERE conference_id = :c"),
                                           {"n": CHANGED_CONFERENCE_NAME, "c": CONFERENCE_ID}).rowcount
                    if (inserted, updated) != (1, 1):
                        raise RuntimeError(f"unexpected row counts {(inserted, updated)}")
            outcome.committed = True  # reached only after the commit succeeded
        except BaseException as exc:  # noqa: BLE001 -- reported through the outcome, never raised inside a thread
            outcome.error = type(exc).__name__
        finally:
            if engine is not None:
                engine.dispose()
            done.set()

    thread = threading.Thread(target=work, name="snapshot-case-writer", daemon=True)
    ledger.threads.append(thread)
    thread.start()
    return thread


# --------------------------------------------------------------------------
# the real tools, as the backup role / migrator, inside the container, with protected pgpass files
# --------------------------------------------------------------------------
def run_pg_dump(cluster: TlsCluster, database: str, path: str, pgpass: str, *, snapshot: str | None, commands: list) -> CommandResult:
    """`pg_dump -Fc` as the backup role over verify-full TLS. `snapshot` is the EXPORTED id, never the visibility descriptor. Bounded by a timeout."""
    argv = ["pg_dump", "-d", conninfo(cluster, BACKUP_ROLE, database), "-Fc", "--no-password"]
    if snapshot is not None:
        argv.append(f"--snapshot={snapshot}")
    argv += ["-f", path]
    try:
        result = _exec(cluster, argv, env={"PGPASSFILE": pgpass}, timeout=DUMP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        admin = cluster.admin_engine()  # the client was killed on timeout; end the server side so no dump keeps running
        try:
            scalar(admin, "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE application_name = 'pg_dump'")
        finally:
            admin.dispose()
        raise CaptureStepFailed(f"pg_dump exceeded its {DUMP_TIMEOUT_S}s deadline and was ended") from None
    commands.append(result)
    return result


def require_destination_database_empty(table_count: int) -> None:
    require(table_count == 0, f"the destination database already holds {table_count} table(s); restore is refused")


# --------------------------------------------------------------------------
# the scenario
# --------------------------------------------------------------------------
@dataclass
class CaseRecord:
    case: str
    source_database: str = ""
    destination_database: str | None = None
    actions: list = field(default_factory=list)
    commands: list = field(default_factory=list)
    exported_id: str = ""
    owner_pid: int = 0
    owner_state: dict = field(default_factory=dict)
    owner_connects_after_capture: int = 0
    descriptor_at_export: str = ""
    descriptor_after_commit: str = ""
    descriptor_after_dump: str = ""
    baseline: DatabaseBaseline | None = None
    owner_reread_after_commit: dict | None = None
    fresh_fingerprints: dict | None = None
    writer: WriterOutcome | None = None
    dump: CommandResult | None = None
    dump_sessions_before: int = 0
    dump_sessions_after: int = 0
    owner_backend_state_at_dump_exit: str | None = None
    owner_backend_state_before_loss: str | None = None
    owner_backend_state_before_dump: str | None = "not recorded"
    archive_sha256: str = ""
    destination_table_count_before_restore: int | None = None
    restore: CommandResult | None = None
    restored_fingerprints: dict | None = None
    comparison: object = None
    restored_probe: dict = field(default_factory=dict)
    outcome: str = ""
    control: CommandResult | None = None
    leftover: dict = field(default_factory=dict)
    artifact_directory: Path | None = None
    artifact_listing: list = field(default_factory=list)
    verification: dict = field(default_factory=dict)


@dataclass
class Harness:
    cluster: TlsCluster
    container_id: str
    boot_database: str


@dataclass
class Workspace:
    source_database: str
    backup_pgpass: str
    migrator_pgpass: str
    archive_path: str
    artifact_directory: Path


def prepare_case_workspace(cluster: TlsCluster, ledger: CaseLedger) -> Workspace:
    """A migrated, seeded source database owned by the migrator (existing helpers), the two pgpass files, the dump path and the would-be artifact directory, all registered at once."""
    source = _create_database_owned_by_migrator(cluster)
    ledger.databases.append(source)
    provision_database(cluster, source, seed=True)  # grants the backup role exactly its 9B-2 set; nothing is added here
    backup_pgpass = write_pgpass(cluster, [(BACKUP_ROLE, cluster.backup_password)])
    migrator_pgpass = write_pgpass(cluster, [(MIGRATOR_ROLE, cluster.migrator_password)])
    archive_path = f"/tmp/napsedge_snapshot_{uuid.uuid4().hex[:12]}.dump"
    ledger.container_files += [backup_pgpass, migrator_pgpass, archive_path]
    artifact_directory = Path(tempfile.mkdtemp(prefix="napsedge_snapshot_artifact_"))
    ledger.directories.append(artifact_directory)  # where a finalization marker would go: it must stay empty
    return Workspace(source, backup_pgpass, migrator_pgpass, archive_path, artifact_directory)


@dataclass
class RestoredDatabase:
    name: str
    admin_engine: object
    archive_sha256: str
    table_count_before_restore: int
    restore: CommandResult


def restore_into_fresh_database(cluster: TlsCluster, ledger: CaseLedger, archive_path: str, migrator_pgpass: str, commands: list, log: Callable[[str], None]) -> RestoredDatabase:
    """
    The existing empty-destination and archive-validation patterns: a fresh database owned by the migrator is verified empty, the archive is
    checksummed and listed and must pass the archive-ready guard, and only then does `pg_restore` run (as the migrator, atomically).
    """
    archive_sha256 = _sha256_in_container(cluster, archive_path)
    destination = _create_database_owned_by_migrator(cluster)
    ledger.databases.append(destination)
    admin_destination = cluster.admin_engine(destination)
    ledger.engines.append(admin_destination)
    table_count = scalar(admin_destination, "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema')")
    require_destination_database_empty(table_count)
    log("destination_verified_empty")
    listing = _exec(cluster, ["pg_restore", "--list", archive_path], timeout=60)
    commands.append(listing)
    require_archive_ready_for_restore(_sha256_in_container(cluster, archive_path), archive_sha256, listing)
    log("archive_validated")
    restore = run_restore(cluster, conninfo(cluster, MIGRATOR_ROLE, destination), archive_path, migrator_pgpass)
    commands.append(restore)
    if restore.returncode != 0:
        raise CaptureStepFailed(f"pg_restore failed: {restore.diagnostics()}")
    log("restored")
    return RestoredDatabase(destination, admin_destination, archive_sha256, table_count, restore)


def _dump_sessions(cluster: TlsCluster, database: str) -> int:
    return len(authenticated_sessions(_container_logs(cluster.container), app="pg_dump", user=BACKUP_ROLE, database=database))


def run_case(harness: Harness, mode: str, ledger: CaseLedger) -> CaseRecord:
    """
    mode: "shared_snapshot" (C1), "no_snapshot" (C2) or "owner_lost" (C8). The three share every step up to the dump, so each
    case differs from the others in exactly one variable.
    """
    cluster = harness.cluster
    record = CaseRecord(case={"shared_snapshot": "C1", "no_snapshot": "C2", "owner_lost": "C8"}[mode])
    log = record.actions.append

    # ---- source: a migrated, seeded database owned by the migrator (existing helpers) -----------------------------------
    workspace = prepare_case_workspace(cluster, ledger)
    source, backup_pgpass, migrator_pgpass, archive_path = workspace.source_database, workspace.backup_pgpass, workspace.migrator_pgpass, workspace.archive_path
    record.source_database = source
    record.artifact_directory = workspace.artifact_directory
    log("source_provisioned")

    # ---- the owner exports its snapshot and reads the baseline THROUGH ITS OWN CONNECTION ---------------------------------
    owner = SnapshotOwner(cluster, source, ledger)
    record.owner_pid = owner.pid
    log("owner_open")
    record.exported_id = owner.begin_and_export()
    record.descriptor_at_export, record.owner_state = owner.descriptor, owner.state
    log("snapshot_exported")
    connects_before = len(owner.connects)
    record.baseline = capture_baseline(owner.connection)
    record.owner_connects_after_capture = len(owner.connects) - connects_before
    log("baseline_read_through_owner")
    require(record.owner_connects_after_capture == 0, "capture_baseline opened another connection: its reads did not share the owner's transaction")

    if mode == "owner_lost":
        # ---- C8: the owner ends BEFORE pg_dump imports the snapshot --------------------------------------------------------
        record.owner_backend_state_before_loss = backend_state(cluster, owner.pid)  # the server's own view: inside its open transaction
        terminate_backend_and_confirm_gone(cluster, owner.pid)
        log("owner_terminated_and_confirmed_gone")
        with contextlib.suppress(Exception):
            owner.connection.invalidate()  # its backend no longer exists; the ledger still closes it
        record.owner_backend_state_before_dump = backend_state(cluster, owner.pid)
        require(record.owner_backend_state_before_dump is None, "the snapshot owner backend still exists; pg_dump must not start")
    else:
        # ---- C1 / C2: a separate writer commits a known change after the snapshot exists and before pg_dump starts ----------
        release, done, record.writer = threading.Event(), threading.Event(), WriterOutcome()
        start_writer(cluster, source, ledger, release, done, record.writer)
        release.set()
        log("writer_released")
        require(done.wait(BARRIER_TIMEOUT_S), "the writer did not finish within its deadline")
        require(record.writer.committed, f"the intended change never committed ({record.writer.error}); the case would pass or fail vacuously")
        log("writer_committed")
        fresh_engine = create_engine(cluster.backup_url(source), pool_size=1, max_overflow=0, pool_timeout=15, connect_args={"connect_timeout": 15, "application_name": "napsedge_fresh_reader"})
        ledger.engines.append(fresh_engine)
        with fresh_engine.connect() as fresh:  # a NEW transaction, which must see the committed change
            record.fresh_fingerprints = all_fingerprints(fresh)
        changed = {t for t in record.baseline.fingerprints if record.baseline.fingerprints[t] != record.fresh_fingerprints.get(t)}
        require(changed == EXPECTED_CHANGED_TABLES, f"a fresh transaction does not show exactly the intended change (differing tables: {sorted(changed)})")
        log("change_visible_to_a_fresh_transaction")
        record.owner_reread_after_commit = all_fingerprints(owner.connection)  # the owner's reads are snapshot-bound
        record.descriptor_after_commit = owner.visibility_descriptor()
        log("owner_reread_after_commit")

    # ---- the dump ------------------------------------------------------------------------------------------------------------------------
    record.dump_sessions_before = _dump_sessions(cluster, source)
    log("dump_started")
    record.dump = run_pg_dump(cluster, source, archive_path, backup_pgpass, snapshot=None if mode == "no_snapshot" else record.exported_id, commands=record.commands)
    record.dump_sessions_after = _dump_sessions(cluster, source)
    log("dump_exited")

    if mode != "owner_lost":
        record.owner_backend_state_at_dump_exit = backend_state(cluster, owner.pid)  # the owner must still be inside its open transaction
        record.descriptor_after_dump = owner.visibility_descriptor()
        owner.end_transaction()
        log("owner_transaction_ended_after_dump")

    if record.dump.returncode != 0:
        record.outcome = classify_dump_failure(record.dump)
        log(f"outcome:{record.outcome}")
        if mode != "owner_lost":
            raise CaptureStepFailed(f"pg_dump failed unexpectedly: {record.dump.diagnostics()}")
        # ---- C8 only from here: NOTHING downstream runs; the leftover is judged by the same archive gate -------------------------------
        leftover_exists = _exec(cluster, ["test", "-e", archive_path], timeout=30).returncode == 0
        record.leftover = {"exists": leftover_exists, "refused_by_archive_gate": None}
        if leftover_exists:
            listing = _exec(cluster, ["pg_restore", "--list", archive_path], timeout=60)
            record.commands.append(listing)
            try:
                require_archive_ready_for_restore(_sha256_in_container(cluster, archive_path), _sha256_in_container(cluster, archive_path), listing)
                record.leftover["refused_by_archive_gate"] = False
            except PreconditionFailed:
                record.leftover["refused_by_archive_gate"] = True
        # ---- the one-variable control: the same command, role and flags with a snapshot whose owner is ALIVE (never restored) ------------
        control_path = f"/tmp/napsedge_snapshot_control_{uuid.uuid4().hex[:12]}.dump"
        ledger.container_files.append(control_path)
        live_owner = SnapshotOwner(cluster, source, ledger)
        live_id = live_owner.begin_and_export()
        record.control = run_pg_dump(cluster, source, control_path, backup_pgpass, snapshot=live_id, commands=record.commands)
        live_owner.end_transaction()
        log("control_dump_with_live_owner")
        return record

    # ---- restore into a fresh database: the existing empty-destination and archive-validation patterns ---------------------
    restored = restore_into_fresh_database(cluster, ledger, archive_path, migrator_pgpass, record.commands, log)
    admin_destination = restored.admin_engine
    record.archive_sha256, record.destination_database = restored.archive_sha256, restored.name
    record.destination_table_count_before_restore, record.restore = restored.table_count_before_restore, restored.restore
    record.restored_fingerprints = all_fingerprints(admin_destination)
    record.comparison = compare_to_baseline(record.baseline, admin_destination)  # the existing comparison; only fingerprints are judged below
    record.restored_probe = {
        "changed_team_rows": scalar(admin_destination, "SELECT count(*) FROM teams WHERE team_id = :i", i=CHANGED_TEAM_ID),
        "conference_name": scalar(admin_destination, "SELECT canonical_name FROM conferences WHERE conference_id = :c", c=CONFERENCE_ID),
    }
    log("compared")
    return record


def _run_in_ledger(harness: Harness, mode: str) -> CaseRecord:
    with owned_case(harness.cluster) as ledger:
        record = run_case(harness, mode, ledger)
        record.artifact_listing = sorted(p.name for p in record.artifact_directory.iterdir())
    record.verification = ledger.verification
    return record


# --------------------------------------------------------------------------
# fixtures: ONE disposable cluster; each case runs once on its own databases
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def harness(docker_available):
    with owned_resources() as ledger, launched_cluster(ledger) as (cluster, container_id):
        boot_database = f"{TEST_DB_PREFIX}boot_{uuid.uuid4().hex[:8]}"
        pgpass = write_pgpass(cluster, [(cluster.admin_url.username, cluster.admin_password)])
        try:
            boot = psql_as_admin(cluster, pgpass, bootstrap_sql(cluster, boot_database))  # the three roles with fresh SCRAM credentials; the bootstrap database stays empty
            assert boot.returncode == 0, boot.diagnostics()
            yield Harness(cluster, container_id, boot_database)
        finally:
            remove_files(cluster, pgpass)


@pytest.fixture(scope="module")
def c1(harness) -> CaseRecord:
    return _run_in_ledger(harness, "shared_snapshot")


@pytest.fixture(scope="module")
def c2(harness) -> CaseRecord:
    return _run_in_ledger(harness, "no_snapshot")


@pytest.fixture(scope="module")
def c8(harness) -> CaseRecord:
    return _run_in_ledger(harness, "owner_lost")


def snapshot_argument(argv: list[str]) -> list[str]:
    return [a for a in argv if a.startswith("--snapshot=")]


def comparable(argv: list[str]) -> list[str]:
    """The command line without the values that legitimately differ per run: the snapshot, the output path, the pgpass path and the database name."""
    return [re.sub(r"dbname=\S+", "dbname=<case database>", a) for a in argv if not a.startswith(("--snapshot=", "/tmp/", "PGPASSFILE="))]


# ==========================================================================
# scope, helpers and the classifier (no cluster needed)
# ==========================================================================
def test_only_c1_c2_and_c8_are_in_scope_and_every_other_case_stays_unvalidated():
    assert VALIDATED_CASES == ("C1", "C2", "C8")
    assert set(UNVALIDATED_CASES) == {f"C{n}" for n in (3, 4, 5, 6, 7, 9, 10)}
    assert not set(VALIDATED_CASES) & set(UNVALIDATED_CASES)


def _result(stderr: str, code: int = 1) -> CommandResult:
    return CommandResult(argv=["pg_dump"], returncode=code, stdout="", stderr=stderr)


INVALID = 'pg_dump: error: query failed: ERROR:  invalid snapshot identifier: "00000004-0000000C-1"\npg_dump: detail: Query was: SET TRANSACTION SNAPSHOT \'00000004-0000000C-1\''


@pytest.mark.parametrize("stderr, expected", [
    (INVALID, "snapshot_unavailable"),
    ('pg_dump: error: connection to server at "localhost" (127.0.0.1), port 5432 failed: FATAL:  password authentication failed for user "x"', "dump_failed"),
    ("pg_dump: error: connection to server failed: no pg_hba.conf entry for host", "dump_failed"),
    ("pg_dump: error: could not connect to server", "dump_failed"),
    ("pg_dump: error: query failed: ERROR:  permission denied for table teams", "dump_failed"),
    (INVALID + "\nERROR:  permission denied for table teams", "dump_failed"),
    (INVALID + "\nFATAL:  password authentication failed", "dump_failed"),
    ("pg_dump: error: something else entirely", "dump_failed"),
], ids=["invalid_snapshot", "authentication", "pg_hba", "connectivity", "permission", "invalid_plus_permission", "invalid_plus_authentication", "unrelated"])
def test_the_classifier_reports_snapshot_unavailable_only_for_an_invalid_snapshot_with_no_other_cause(stderr, expected):
    assert classify_dump_failure(_result(stderr)) == expected


def test_snapshot_unavailable_is_one_of_the_enumerated_failure_classes():
    assert "snapshot_unavailable" in FAILED_REASON_CLASSES


# ==========================================================================
# C1 -- shared snapshot
# ==========================================================================
def test_c1_the_exported_snapshot_id_and_the_visibility_descriptor_are_different_things(c1):
    assert EXPORTED_ID.fullmatch(c1.exported_id), c1.exported_id
    assert VISIBILITY_DESCRIPTOR.fullmatch(c1.descriptor_at_export), c1.descriptor_at_export
    assert c1.exported_id != c1.descriptor_at_export and not EXPORTED_ID.fullmatch(c1.descriptor_at_export)


def test_c1_the_owner_is_the_backup_role_in_a_repeatable_read_read_only_transaction(c1):
    assert c1.owner_state == {"isolation": "repeatable read", "read_only": "on", "role": BACKUP_ROLE}


def test_c1_every_baseline_read_used_the_owners_connection_and_the_owner_reads_are_snapshot_bound(c1):
    assert c1.owner_connects_after_capture == 0, "capture_baseline opened a connection of its own"
    assert c1.owner_reread_after_commit == c1.baseline.fingerprints, "the owner, re-reading after the commit, still sees the snapshot"
    assert c1.fresh_fingerprints != c1.baseline.fingerprints, "a fresh transaction sees the committed change, so the owner's equality is not vacuous"


def test_c1_the_writer_committed_after_the_snapshot_and_before_the_dump_and_a_fresh_transaction_sees_it(c1):
    assert c1.writer.committed and c1.writer.error is None
    order = [a for a in c1.actions if a in {"snapshot_exported", "baseline_read_through_owner", "writer_released", "writer_committed",
                                            "change_visible_to_a_fresh_transaction", "owner_reread_after_commit", "dump_started", "dump_exited"}]
    assert order == ["snapshot_exported", "baseline_read_through_owner", "writer_released", "writer_committed",
                     "change_visible_to_a_fresh_transaction", "owner_reread_after_commit", "dump_started", "dump_exited"]
    diff = {t for t in c1.baseline.fingerprints if c1.baseline.fingerprints[t] != c1.fresh_fingerprints[t]}
    assert diff == EXPECTED_CHANGED_TABLES
    assert c1.fresh_fingerprints["teams"][0] == c1.baseline.fingerprints["teams"][0] + 1
    assert c1.fresh_fingerprints["conferences"][0] == c1.baseline.fingerprints["conferences"][0], "an UPDATE: same count, different content"


def test_c1_the_dump_ran_as_the_backup_role_with_the_exported_id_over_authenticated_tls(c1):
    assert c1.dump.returncode == 0, c1.dump.diagnostics()
    assert snapshot_argument(c1.dump.argv) == [f"--snapshot={c1.exported_id}"]
    assert c1.descriptor_at_export not in " ".join(c1.dump.argv), "the visibility descriptor must never be passed to pg_dump"
    assert "-Fc" in c1.dump.argv and not any(a.startswith("-j") or a.startswith("--jobs") for a in c1.dump.argv), "single process: only one importer"
    assert c1.dump.stderr.strip() == ""
    assert c1.dump_sessions_after == c1.dump_sessions_before + 1, "the server logged exactly one new SCRAM-authenticated TLS pg_dump session as the backup role"


def test_c1_the_owner_stayed_open_until_the_dump_exited_and_its_visibility_descriptor_never_changed(c1):
    order = [a for a in c1.actions if a in {"dump_exited", "owner_transaction_ended_after_dump"}]
    assert order == ["dump_exited", "owner_transaction_ended_after_dump"]
    assert c1.owner_backend_state_at_dump_exit == "idle in transaction", "the owner was still inside its open transaction when the dump exited"
    assert c1.descriptor_at_export == c1.descriptor_after_commit == c1.descriptor_after_dump


def test_c1_the_archive_gates_ran_before_the_restore_into_an_empty_destination(c1):
    assert c1.destination_table_count_before_restore == 0
    steps = [a for a in c1.actions if a in {"dump_exited", "destination_verified_empty", "archive_validated", "restored", "compared"}]
    assert steps == ["dump_exited", "destination_verified_empty", "archive_validated", "restored", "compared"]
    assert c1.restore.returncode == 0 and c1.destination_database != c1.source_database


def test_c1_restored_table_data_equals_the_baseline_exactly(c1):
    assert c1.comparison.fingerprint_diffs == {}, describe_diff(c1.comparison.fingerprint_diffs)
    assert c1.restored_fingerprints == c1.baseline.fingerprints
    assert c1.baseline.fingerprints["teams"][0] >= 3, "the baseline holds the deterministic seed, so equality is not an empty-database coincidence"


def test_c1_the_post_snapshot_change_is_absent_from_the_restored_data(c1):
    assert c1.restored_probe["changed_team_rows"] == 0
    assert c1.restored_probe["conference_name"] != CHANGED_CONFERENCE_NAME
    assert c1.restored_fingerprints != c1.fresh_fingerprints, "the restored data is the snapshot, not the committed state"


# ==========================================================================
# C2 -- negative control
# ==========================================================================
def test_c2_the_same_ordering_ran_and_the_intended_change_committed_and_was_visible(c2):
    assert c2.writer.committed and c2.writer.error is None
    steps = [a for a in c2.actions if a in {"snapshot_exported", "baseline_read_through_owner", "writer_committed", "change_visible_to_a_fresh_transaction", "dump_started"}]
    assert steps == ["snapshot_exported", "baseline_read_through_owner", "writer_committed", "change_visible_to_a_fresh_transaction", "dump_started"]
    assert c2.owner_connects_after_capture == 0 and c2.owner_reread_after_commit == c2.baseline.fingerprints


def test_c2_the_dump_differs_from_c1_only_in_not_importing_the_snapshot(c1, c2):
    assert snapshot_argument(c2.dump.argv) == []
    assert comparable(c2.dump.argv) == comparable(c1.dump.argv)
    assert c2.dump.returncode == 0, c2.dump.diagnostics()


def test_c2_the_restored_data_differs_from_the_baseline_in_exactly_the_changed_tables(c2):
    diffs = c2.comparison.fingerprint_diffs
    assert set(diffs) == EXPECTED_CHANGED_TABLES, describe_diff(diffs)
    base, fresh = c2.baseline.fingerprints, c2.fresh_fingerprints
    assert diffs["teams"] == (base["teams"], fresh["teams"]) and diffs["teams"][1][0] == diffs["teams"][0][0] + 1, "one more team row"
    conferences = diffs["conferences"]
    assert conferences[0][0] == conferences[1][0] and conferences[0][1] != conferences[1][1], "same conference count, different content"


def test_c2_the_committed_change_is_present_after_restore(c2):
    assert c2.restored_probe["changed_team_rows"] == 1
    assert c2.restored_probe["conference_name"] == CHANGED_CONFERENCE_NAME
    assert c2.restored_fingerprints == c2.fresh_fingerprints, "without the snapshot the dump captured the post-commit state exactly"


def test_c2_proves_c1_can_fail_the_two_cases_restored_different_data(c1, c2):
    assert c1.restored_fingerprints != c2.restored_fingerprints
    counts = lambda record: {table: count for table, (count, _) in record.baseline.fingerprints.items()}  # noqa: E731
    assert counts(c1) == counts(c2), "both cases started from the same seeded shape"
    # Not asserted: equal md5s across the two databases. The seed stores server-generated `created_at` timestamps, which the fingerprint covers, so two
    # separately seeded databases differ in content. Each case is deterministic WITHIN its own database, which is all that C1 and C2 compare.


# ==========================================================================
# C8 -- owner loss before the snapshot is imported
# ==========================================================================
def test_c8_the_owner_was_definitively_gone_before_pg_dump_started(c8):
    steps = [a for a in c8.actions if a in {"snapshot_exported", "baseline_read_through_owner", "owner_terminated_and_confirmed_gone", "dump_started"}]
    assert steps == ["snapshot_exported", "baseline_read_through_owner", "owner_terminated_and_confirmed_gone", "dump_started"]
    assert c8.owner_backend_state_before_loss == "idle in transaction", "the owner really held the exported snapshot in an open transaction"
    assert c8.owner_backend_state_before_dump is None, "and the server no longer had that backend when the dump started"
    assert EXPORTED_ID.fullmatch(c8.exported_id) and snapshot_argument(c8.dump.argv) == [f"--snapshot={c8.exported_id}"]


def test_c8_pg_dump_failed_with_the_invalid_snapshot_error_and_not_an_authentication_connectivity_or_permission_error(c8):
    assert c8.dump.returncode != 0
    assert "invalid snapshot identifier" in c8.dump.stderr, c8.dump.stderr[-400:]
    for fragment in NOT_A_SNAPSHOT_PROBLEM:
        assert fragment not in c8.dump.stderr, f"unexpected {fragment!r} in the failure output"
    assert classify_dump_failure(c8.dump) == "snapshot_unavailable"


def test_c8_the_session_authenticated_and_connected_so_the_failure_came_after_authentication(c8):
    assert c8.dump_sessions_after == c8.dump_sessions_before + 1, "the server logged the failed pg_dump as a SCRAM-authenticated TLS session of the backup role"


def test_c8_the_same_command_succeeds_when_the_snapshot_owner_is_alive(c8):
    assert c8.control.returncode == 0, c8.control.diagnostics()
    assert snapshot_argument(c8.control.argv) != snapshot_argument(c8.dump.argv), "a different, live snapshot"
    assert comparable(c8.control.argv) == comparable(c8.dump.argv)


def test_c8_the_harness_outcome_is_snapshot_unavailable(c8):
    assert c8.outcome == "snapshot_unavailable"
    assert c8.outcome in FAILED_REASON_CLASSES
    assert c8.actions[-2:] == ["outcome:snapshot_unavailable", "control_dump_with_live_owner"]


def test_c8_nothing_downstream_ran_no_validation_no_restore_no_finalization(c8):
    forbidden = {"destination_verified_empty", "archive_validated", "restored", "compared", "owner_transaction_ended_after_dump"}
    assert not forbidden & set(c8.actions)
    assert c8.destination_database is None and c8.restore is None and c8.comparison is None
    assert not any("--exit-on-error" in command.argv for command in c8.commands), "no restore command was ever issued (only dumps and, at most, a --list of the leftover)"
    assert c8.artifact_listing == [] and "COMPLETE" not in c8.artifact_listing, "no finalization marker was published"


def test_c8_any_leftover_dump_is_incomplete_and_refused_by_the_archive_gate(c8):
    if c8.leftover["exists"]:
        assert c8.leftover["refused_by_archive_gate"] is True, "a leftover archive was accepted as ready for restore"
    else:
        assert c8.leftover["refused_by_archive_gate"] is None


# ==========================================================================
# cleanup, on success and on failure; no secret anywhere
# ==========================================================================
@pytest.mark.parametrize("name", ["c1", "c2", "c8"])
def test_cleanup_was_verified_for_each_case(name, request):
    record = request.getfixturevalue(name)
    verification = record.verification
    assert verification["problems"] == []
    assert verification["databases_remaining"] == [] and verification["sessions_in_dropped_databases"] == 0
    assert verification["leftover_dump_or_owner_sessions"] == 0 and verification["container_files_remaining"] == []
    assert verification["threads_alive"] == [] and verification["directories_remaining"] == []


def test_cleanup_is_complete_and_verified_when_a_case_fails_midway(harness):
    cluster = harness.cluster
    release = threading.Event()
    created: dict = {}
    with pytest.raises(RuntimeError, match="deliberate failure"):
        with owned_case(cluster) as ledger:
            created["database"] = _create_database_owned_by_migrator(cluster)
            ledger.databases.append(created["database"])
            admin = cluster.admin_engine(created["database"])
            ledger.engines.append(admin)
            connection = admin.connect()
            ledger.connections.append(connection)
            created["pid"] = scalar(connection, "SELECT pg_backend_pid()")
            path = f"/tmp/napsedge_snapshot_midway_{uuid.uuid4().hex[:8]}"
            assert _exec(cluster, ["sh", "-c", f"echo x > {path}"], timeout=30).returncode == 0
            ledger.container_files.append(path)
            thread = threading.Thread(target=release.wait, args=(BARRIER_TIMEOUT_S,), daemon=True, name="midway-waiter")
            ledger.threads.append(thread)
            thread.start()
            release.set()
            raise RuntimeError("deliberate failure")
    verification = ledger.verification
    assert verification["problems"] == []
    assert verification["databases_remaining"] == [] and verification["sessions_in_dropped_databases"] == 0
    assert verification["container_files_remaining"] == [] and verification["threads_alive"] == []
    assert backend_state(cluster, created["pid"]) is None, "the registered connection's backend is gone"


def test_a_cleanup_that_leaves_something_behind_is_reported_and_never_replaces_the_original_failure(harness):
    cluster = harness.cluster
    leak = f"/tmp/napsedge_snapshot_leak_{uuid.uuid4().hex[:8]}"
    assert _exec(cluster, ["sh", "-c", f"echo x > {leak}"], timeout=30).returncode == 0
    try:
        with pytest.raises(AssertionError, match="container_files_remaining"):
            with owned_case(cluster, remove_container_files=lambda *args: None) as ledger:  # a remover that silently does nothing
                ledger.container_files.append(leak)
        with pytest.raises(RuntimeError, match="original") as excinfo:
            with owned_case(cluster, remove_container_files=lambda *args: None) as ledger:
                ledger.container_files.append(leak)
                raise RuntimeError("original")
        assert any("container_files_remaining" in note for note in excinfo.value.__notes__), "the leak is attached to the original failure as a note"
    finally:
        remove_files(cluster, leak)


@pytest.mark.parametrize("name", ["c1", "c2", "c8"])
def test_no_credential_appears_in_any_recorded_command_or_output(name, request, harness):
    record = request.getfixturevalue(name)
    secrets_ = harness.cluster.secrets_to_scrub()
    assert record.commands, "commands were recorded"
    for command in record.commands:
        blob = " ".join(command.argv) + command.stdout + command.stderr
        assert not any(secret and secret in blob for secret in secrets_)
        assert "PGPASSWORD" not in blob and "password=" not in blob.lower()


@pytest.mark.parametrize("name", ["c1", "c2", "c8"])
def test_observed_facts_are_recorded_in_the_junit_evidence(name, request):
    """Writes what each case actually observed into the JUnit properties (scrubbed, bounded, no credential), so the retained evidence is self-explaining."""
    r = request.getfixturevalue(name)
    facts = {
        "case": r.case,
        "dump_exit_code": r.dump.returncode,
        "dump_argv_snapshot_arguments": len(snapshot_argument(r.dump.argv)),
        "dump_stderr_tail": r.dump.stderr.strip()[-300:],
        "outcome": r.outcome or "restored",
        "actions": ",".join(r.actions),
        "cleanup_problems": len(r.verification["problems"]),
    }
    if r.baseline is not None and r.case != "C8":
        facts.update({
            "baseline_row_counts": ",".join(f"{t}={c}" for t, (c, _) in sorted(r.baseline.fingerprints.items()) if t in EXPECTED_CHANGED_TABLES),
            "fresh_row_counts": ",".join(f"{t}={c}" for t, (c, _) in sorted(r.fresh_fingerprints.items()) if t in EXPECTED_CHANGED_TABLES),
            "restored_equals_baseline": r.restored_fingerprints == r.baseline.fingerprints,
            "restored_equals_fresh_committed_state": r.restored_fingerprints == r.fresh_fingerprints,
            "fingerprint_diff_tables": ",".join(sorted(r.comparison.fingerprint_diffs)),
            "descriptor_unchanged": r.descriptor_at_export == r.descriptor_after_commit == r.descriptor_after_dump,
        })
    if r.case == "C8":
        facts.update({"leftover_exists": r.leftover["exists"], "leftover_refused_by_archive_gate": r.leftover["refused_by_archive_gate"],
                      "control_dump_exit_code": r.control.returncode})
    for key, value in facts.items():
        request.node.user_properties.append((key, str(value)))  # the JUnit properties (record_property would warn under the default xunit2 family)
    assert facts["cleanup_problems"] == 0
