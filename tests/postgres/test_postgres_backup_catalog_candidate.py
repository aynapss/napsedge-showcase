"""
Phase 9B-5C5: disposable validation of the fresh catalog-observer CANDIDATE (specification sections 6.4, 12.5 and 12.8).
It does NOT adopt a production capture design and does NOT authorize production capture; nothing here establishes artifact consistency.

The candidate under test
------------------------
Each endpoint of a capture (before the snapshot export, and after `pg_dump` exits) is observed by a SEPARATE, FRESH observer transaction. The observation is the existing
`observe_catalog` (the baseline's own constraint, index and ACL/ownership reads and their digests, imported from the C6 module, not redefined), and the existing
`require_catalog_unchanged` guard decides `catalog_changed`.

* Observer isolation and lifetime (documented here, recorded per endpoint): one pooled backup-role connection (pool of one, `application_name` set), READ COMMITTED (the server
  default, as in C6), each endpoint a NEW transaction that begins at its first statement and ends with a rollback when its `with` block exits. Its first statements set
  `lock_timeout` and `statement_timeout` (bounded SQL). The pool reuses one backend between endpoints: the TRANSACTIONS are separate, the SESSION is not, and the transaction start
  time (`now()`) is recorded before and after the three reads to prove one transaction per endpoint and distinct transactions between endpoints.
* READ COMMITTED means each of the three reads takes its own snapshot. An Engine does not supply one transaction across several reads (calling the observation through an Engine uses
  one checkout per read; a Connection is used as it is): both are recorded, never assumed. How a long-lived READ COMMITTED or REPEATABLE READ observer behaves when a change commits
  between its reads is MEASURED for an index creation, a role rename and a column rename; no blanket claim is made about which catalog functions are snapshot-bound or current.
* Every baseline read stays on the snapshot owner's Connection and the owner stays open until `pg_dump` exits; the observers never touch it.

Scenario ordering and the barrier (test orchestration, NOT a production lock policy)
------------------------------------------------------------------------------------
For changes DURING a real `pg_dump`: the owner has read its baseline (so it holds ACCESS SHARE on every table); a migrator session requests ACCESS EXCLUSIVE on one owned table and is
CONFIRMED queued behind the owner (`pg_locks`, `pg_blocking_pids`); `pg_dump` then starts, enters its snapshot transaction and its `LOCK TABLE` request parks behind the queued
exclusive request (C7's finding), which is CONFIRMED from the server (the dump backend's active statement, its transaction start, its `Lock` wait, its ungranted AccessShareLock and
`pg_blocking_pids`). Only then is the catalog operation applied and proven committed (or blocked); the barrier is cancelled within a bounded deadline, the dump resumes and exits, and
the owner stays open throughout. Ordering rests on these server observations and sequence numbers, never on elapsed time, a larger fixture or a mocked subprocess.

Refusals and diagnostics
------------------------
`catalog_changed` ends the flow before restore and finalization and no artifact exists. A restore after a refusal exists ONLY as a separately labeled DIAGNOSTIC (its action names are
`diagnostic:*`), never a capture.

Cases exercised here are named S1-S5 (this unit's required scenarios); the C6/C7 and C9/C10 results are unchanged and C6 stays failed as written.
"""
from __future__ import annotations

import contextlib
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from tests.postgres.pg_restore_compare import (
    DatabaseBaseline,
    acl_snapshot,
    all_fingerprints,
    capture_baseline,
    compare_to_baseline,
    constraint_snapshot,
    index_snapshot,
    rows,
    scalar,
)
from tests.postgres.test_postgres_backup_catalog_consistency import (  # the existing catalog definitions, digests, guard and C7 barrier pieces: imported, never redefined
    CatalogObservation,
    Change,
    SessionOutcome,
    _fresh_engine,
    blocking_pids,
    changed_categories,
    granted_table_locks,
    observe_catalog,
    raw_differences,
    require_catalog_unchanged,
    start_session_thread,
    wait_until,
    waiting_modes,
)
from tests.postgres.test_postgres_backup_snapshot_consistency import (
    BARRIER_TIMEOUT_S,
    DUMP_TIMEOUT_S,
    CaptureStepFailed,
    CaseLedger,
    Harness,
    SnapshotOwner,
    backend_state,
    harness,  # noqa: F401  (module-scoped fixture: ONE disposable cluster for this module)
    owned_case,
    prepare_case_workspace,
    restore_into_fresh_database,
    run_pg_dump,
)
from tests.postgres.test_postgres_fresh_cluster_recovery import PreconditionFailed
from tests.postgres.test_postgres_roles_and_restore import MIGRATOR_ROLE, RUNTIME_ROLE
from tests.postgres.test_postgres_tls_backup_restore import (  # noqa: F401
    BACKUP_ROLE,
    _container_logs,
    _exec,
    authenticated_sessions,
    isolated_database_environment,  # autouse module fixture
)

pytestmark = pytest.mark.postgres

VALIDATED_SCENARIOS = ("S1", "S2", "S3", "S4", "S5")
OBSERVER_APP = "napsedge_candidate_observer"
BARRIER_APP = "napsedge_candidate_barrier"
ROLE_PREFIX = "napsedge_candidate_probe_"
CANDIDATE = "candidate_fresh_committed_transactions"  # the label C6 gave this candidate
CHECK_NAME = "ck_conferences_candidate_probe"
FK_NAME = "fk_team_aliases_team_candidate_probe"
INDEX_NAME = "ix_team_aliases_normalized_candidate_probe"
BARRIER_TABLE = "conferences"  # the table the barrier request is queued on; no operation under test touches it
BARRIER_LOCK_TIMEOUT = "60s"  # bounds the barrier session if the orchestration itself fails; the barrier is cancelled long before
PARK_DEADLINE_S = 20
BARRIER_QUEUE_DEADLINE_S = 10
BARRIER_CANCEL_DEADLINE_S = 10
CHANGE_LOCK_TIMEOUT = "5s"
CHANGE_STATEMENT_TIMEOUT = "20s"
OBSERVER_LOCK_TIMEOUT = "10s"
OBSERVER_STATEMENT_TIMEOUT = "20s"
SAFE_ROLE = re.compile(rf"{ROLE_PREFIX}[0-9a-f]{{10}}_[a-z]")


# ==========================================================================
# ordered events: every scenario records WHAT happened in WHICH order (sequence numbers; the timestamps are monotonic and informational only)
# ==========================================================================
@dataclass
class Events:
    items: list = field(default_factory=list)

    def add(self, name: str, **detail) -> int:
        self.items.append({"seq": len(self.items) + 1, "t": round(time.monotonic(), 3), "event": name, **detail})
        return len(self.items)

    @property
    def names(self) -> list[str]:
        return [e["event"] for e in self.items]

    def seq(self, name: str) -> int:
        found = [e["seq"] for e in self.items if e["event"] == name]
        if len(found) != 1:
            raise AssertionError(f"event {name!r} occurred {len(found)} times")
        return found[0]

    def in_order(self, *names: str) -> bool:
        return [self.seq(n) for n in names] == sorted(self.seq(n) for n in names)


# ==========================================================================
# the observer: a separate, fresh transaction per endpoint, on the EXISTING observation
# ==========================================================================
@dataclass(frozen=True)
class Endpoint:
    label: str
    observation: CatalogObservation
    pid: int
    isolation: str
    txn_start_first: str  # now() = the start of the observer transaction, read before the three reads
    txn_start_last: str  # ... and after them

    @property
    def one_transaction(self) -> bool:
        return self.txn_start_first == self.txn_start_last


def observe_endpoint(engine: Engine, label: str, *, isolation: str | None = None) -> Endpoint:
    """One endpoint: a NEW transaction on the observer connection; `observe_catalog` (unchanged) on that connection; rolled back when the block exits."""
    with engine.connect() as conn:
        if isolation is not None:
            conn = conn.execution_options(isolation_level=isolation)  # must precede the first statement
        conn.execute(text(f"SET LOCAL lock_timeout = '{OBSERVER_LOCK_TIMEOUT}'"))
        conn.execute(text(f"SET LOCAL statement_timeout = '{OBSERVER_STATEMENT_TIMEOUT}'"))
        pid, level, first = conn.execute(text("SELECT pg_backend_pid(), current_setting('transaction_isolation'), now()::text")).one()
        observation = observe_catalog(conn)
        last = scalar(conn, "SELECT now()::text")
    return Endpoint(label, observation, pid, level, first, last)


def lockfree_probe(engine: Engine) -> dict:
    """
    PROOF reads for the barrier scenarios, NOT the candidate's observation. While an ACCESS EXCLUSIVE request is queued, the EXISTING index read (the catalog view that renders each index
    with `pg_get_indexdef`) takes ACCESS SHARE on the indexed tables and queues behind it (measured by the lock-behavior probe); the existing constraint and ACL reads do not. This probe
    therefore uses those two existing reads plus a list of index relation names, all lock-free, in one fresh transaction.
    """
    with engine.connect() as conn:
        conn.execute(text(f"SET LOCAL lock_timeout = '{OBSERVER_LOCK_TIMEOUT}'"))
        conn.execute(text(f"SET LOCAL statement_timeout = '{OBSERVER_STATEMENT_TIMEOUT}'"))
        pid = scalar(conn, "SELECT pg_backend_pid()")
        return {"pid": pid, "constraints": tuple(constraint_snapshot(conn)), "acls": tuple(acl_snapshot(conn)),
                "indexes": tuple(r[0] for r in rows(conn, "SELECT c.relname FROM pg_class c WHERE c.relkind = 'i' AND c.relnamespace = 'public'::regnamespace ORDER BY 1"))}


def probe_changed_categories(before: dict, after: dict) -> list[str]:
    return [category for category in ("constraints", "indexes", "acls") if before[category] != after[category]]


# ==========================================================================
# disposable roles: created by the test, registered the moment they exist (before creation), dropped AFTER the case databases, verified absent
# ==========================================================================
@dataclass
class RoleLedger:
    cluster: object
    names: list = field(default_factory=list)
    verification: dict = field(default_factory=dict)

    def reserve(self, suffix: str) -> str:
        name = f"{ROLE_PREFIX}{uuid.uuid4().hex[:10]}_{suffix}"
        assert SAFE_ROLE.fullmatch(name) and name.startswith(ROLE_PREFIX), name
        assert name not in (BACKUP_ROLE, RUNTIME_ROLE, MIGRATOR_ROLE)
        self.names.append(name)  # registered BEFORE it exists: cleanup is DROP ... IF EXISTS
        return name

    def create(self, suffix: str) -> str:
        name = self.reserve(suffix)
        admin = self.cluster.admin_engine()
        try:
            with admin.connect() as conn:
                conn.execute(text(f"CREATE ROLE {name} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"))
        finally:
            admin.dispose()
        return name


@contextlib.contextmanager
def disposable_roles(cluster):
    ledger = RoleLedger(cluster)
    primary: BaseException | None = None
    try:
        yield ledger
    except BaseException as exc:
        primary = exc
        raise
    finally:
        problems: list[str] = []
        admin = cluster.admin_engine()
        try:
            for name in ledger.names:
                assert name.startswith(ROLE_PREFIX), name  # only roles this test created are ever dropped
                try:
                    with admin.connect() as conn:
                        conn.execute(text(f"DROP ROLE IF EXISTS {name}"))
                except Exception as exc:  # noqa: BLE001 -- reported, never masks the primary failure
                    problems.append(f"drop role: {type(exc).__name__}")
            try:
                remaining = [r[0] for r in rows(admin, "SELECT rolname FROM pg_roles WHERE rolname = ANY(CAST(:n AS text[]))", n=list(ledger.names))]
            except Exception as exc:  # noqa: BLE001
                remaining = None
                problems.append(f"could not verify role cleanup: {type(exc).__name__}")
        finally:
            admin.dispose()
        ledger.verification = {"roles_registered": len(ledger.names), "roles_remaining": remaining, "problems": problems}
        if remaining:
            problems.append(f"roles remaining: {len(remaining)}")
        if problems:
            message = "cleanup of disposable roles failed or could not be verified: " + "; ".join(problems)
            if primary is not None:
                primary.add_note(message)
            else:
                raise AssertionError(message)


# ==========================================================================
# the changes. Each step is its own transaction with bounded waits; the change session's PID and transaction start are recorded.
# (Same semantics as the C6 `apply_change`, plus the PID: that helper does not expose its session, so this thin variant exists. SQLSTATE 55P03 = blocked.)
# ==========================================================================
def build_changes(roles: tuple[str, str] | None = None) -> dict[str, Change]:
    changes = {
        "constraint_check": Change("constraint_check", "migrator", (f"ALTER TABLE public.conferences ADD CONSTRAINT {CHECK_NAME} CHECK (conference_id > 0)",), "constraints"),  # ACCESS EXCLUSIVE
        "constraint_fk": Change("constraint_fk", "migrator", (f"ALTER TABLE public.team_aliases ADD CONSTRAINT {FK_NAME} FOREIGN KEY (team_id) REFERENCES public.teams (team_id)",), "constraints"),  # SHARE ROW EXCLUSIVE
        "index": Change("index", "migrator", (f"CREATE INDEX {INDEX_NAME} ON public.team_aliases (normalized_value)",), "indexes"),
        "acl": Change("acl", "migrator", (f"GRANT INSERT ON public.teams TO {RUNTIME_ROLE}",), "acls"),
        "acl_and_reversal": Change("acl_and_reversal", "migrator", (f"GRANT INSERT ON public.teams TO {RUNTIME_ROLE}", f"REVOKE INSERT ON public.teams FROM {RUNTIME_ROLE}"), None),
    }
    if roles is not None:
        before, after = roles
        assert before.startswith(ROLE_PREFIX) and after.startswith(ROLE_PREFIX)  # a role the test created, never an application, backup or migrator role
        changes["role_rename"] = Change("role_rename", "admin", (f"ALTER ROLE {before} RENAME TO {after}",), "acls")
    return changes


def run_change(cluster, database: str, change: Change, ledger: CaseLedger, after_step: Callable[[], object] | None = None) -> dict:
    if change.role == "migrator":
        engine = create_engine(cluster.migrator_url(database), pool_size=1, max_overflow=0, pool_timeout=15, connect_args={"connect_timeout": 15, "application_name": f"napsedge_candidate_change_{change.name}"[:60]})
    else:
        engine = cluster.admin_engine(database, autocommit=False)
    ledger.engines.append(engine)
    result = {"change": change.name, "role": change.role, "steps": len(change.steps), "committed_steps": 0, "sqlstate": None, "error": None, "pid": None,
              "transaction_starts": [], "elapsed_s": 0.0, "after_each_step": []}
    started = time.monotonic()
    for statement in change.steps:
        try:
            with engine.begin() as conn:
                conn.execute(text(f"SET LOCAL lock_timeout = '{CHANGE_LOCK_TIMEOUT}'"))
                conn.execute(text(f"SET LOCAL statement_timeout = '{CHANGE_STATEMENT_TIMEOUT}'"))
                pid, begun = conn.execute(text("SELECT pg_backend_pid(), now()::text")).one()
                result["pid"] = result["pid"] or pid
                result["transaction_starts"].append(begun)
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


def apply_role_setup(cluster, database: str, kind: str, role: str, ledger: CaseLedger) -> str:
    """State created BEFORE the capture so the first endpoint contains it: an ACL entry for the disposable role, or a table owned by it. Nothing is granted to the backup role."""
    statement = {"acl_entry": f"GRANT SELECT ON public.teams TO {role}", "table_owner": f"ALTER TABLE public.seasons OWNER TO {role}"}[kind]
    admin = cluster.admin_engine(database)
    ledger.engines.append(admin)
    with admin.connect() as conn:
        conn.execute(text(statement))
    return statement


# ==========================================================================
# the scenarios
# ==========================================================================
@dataclass(frozen=True)
class CandidateScenario:
    name: str
    required: str  # S1..S5 of this unit
    change: str | None = None
    window: str = "none"  # none | after_export (the owner holds no table lock yet) | after_reads (the baseline reads hold ACCESS SHARE) | during_dump (barrier-parked dump)
    role_setup: str | None = None  # None | acl_entry | table_owner


CANDIDATE_SCENARIOS = {
    "s1_control": CandidateScenario("s1_control", "S1"),
    "s2a_constraint_before_locks": CandidateScenario("s2a_constraint_before_locks", "S2", "constraint_check", "after_export"),
    "s2b_constraint_after_locks_blocked": CandidateScenario("s2b_constraint_after_locks_blocked", "S2", "constraint_check", "after_reads"),
    "s2c_constraint_after_locks_compatible": CandidateScenario("s2c_constraint_after_locks_compatible", "S2", "constraint_fk", "after_reads"),
    "s3a_acl_during_dump": CandidateScenario("s3a_acl_during_dump", "S3", "acl", "during_dump"),
    "s3b_index_during_dump": CandidateScenario("s3b_index_during_dump", "S3", "index", "during_dump"),
    "s3c_constraint_during_dump": CandidateScenario("s3c_constraint_during_dump", "S3", "constraint_fk", "during_dump"),
    "s4a_role_rename_during_dump": CandidateScenario("s4a_role_rename_during_dump", "S4", "role_rename", "during_dump", "acl_entry"),
    "s4b_owner_role_control": CandidateScenario("s4b_owner_role_control", "S4", None, "none", "table_owner"),
    "s4b_owner_role_rename_after_export": CandidateScenario("s4b_owner_role_rename_after_export", "S4", "role_rename", "after_export", "table_owner"),
    "s5_acl_change_and_reversal_during_dump": CandidateScenario("s5_acl_change_and_reversal_during_dump", "S5", "acl_and_reversal", "during_dump"),
}
SYNCHRONOUS_DUMP = [n for n, s in CANDIDATE_SCENARIOS.items() if s.window != "during_dump"]
DURING_DUMP = [n for n, s in CANDIDATE_SCENARIOS.items() if s.window == "during_dump"]
EXPECTED_CATEGORY = {"constraint_check": "constraints", "constraint_fk": "constraints", "index": "indexes", "acl": "acls", "role_rename": "acls"}
EXPECTED_KEY = {"constraint_check": ("conferences", CHECK_NAME), "constraint_fk": ("team_aliases", FK_NAME), "index": ("team_aliases", INDEX_NAME), "acl": ("teams",)}


@dataclass
class CandidateRecord:
    scenario: str
    required: str
    window: str = "none"
    actions: list = field(default_factory=list)
    events: Events = field(default_factory=Events)
    commands: list = field(default_factory=list)
    source_database: str = ""
    pids: dict = field(default_factory=dict)
    role_names: dict = field(default_factory=dict)
    role_setup: str | None = None
    change: dict | None = None
    pre_change: Endpoint | dict | None = None  # an Endpoint, or a lock-free probe dict inside a barrier scenario
    post_change: Endpoint | dict | None = None
    visibility_method: str = ""
    fresh_visible_categories: list = field(default_factory=list)
    first: Endpoint | None = None
    second: Endpoint | None = None
    baseline: DatabaseBaseline | None = None
    baseline_catalog: CatalogObservation | None = None
    owner_after_in_transaction: CatalogObservation | None = None
    owner_connects_after_baseline: int = 0
    owner_locked_tables_after_baseline: list = field(default_factory=list)
    owner_xmin: str | None = None
    exported_id: str = ""
    descriptor_at_export: str = ""
    descriptor_after_dump: str = ""
    owner_state_at_dump_exit: str | None = None
    barrier: dict = field(default_factory=dict)
    dump_parked: dict = field(default_factory=dict)
    dump_pid_from_log: list = field(default_factory=list)
    dump: object = None
    detection: dict = field(default_factory=dict)
    differences: dict = field(default_factory=dict)
    outcome: str = ""
    finalization_invoked: bool = False
    destination_database: str | None = None
    restore_failure: str | None = None
    harm: dict = field(default_factory=dict)
    archive_mentions: dict = field(default_factory=dict)
    diagnostic: dict = field(default_factory=dict)
    diagnostic_actions: list = field(default_factory=list)
    artifact_listing: list = field(default_factory=list)
    workspace: object = None  # kept only for the separately labeled diagnostic path
    verification: dict = field(default_factory=dict)
    roles_verification: dict = field(default_factory=dict)


def dump_backend_row(server: Engine, database: str) -> dict | None:
    found = rows(server, "SELECT pid, usename, state, wait_event_type, wait_event, left(query, 60), xact_start::text, backend_xmin::text FROM pg_stat_activity "
                         "WHERE application_name = 'pg_dump' AND datname = :d", d=database)
    return dict(zip(("pid", "usename", "state", "wait_event_type", "wait_event", "query", "xact_start", "backend_xmin"), found[0])) if found else None


def dump_is_parked(server: Engine, database: str, barrier_pid: int) -> dict | None:
    """The server's evidence that pg_dump is INSIDE its transaction and waiting for the lock the barrier holds back; None when any element is missing."""
    row = dump_backend_row(server, database)
    if row is None or row["xact_start"] is None or row["wait_event_type"] != "Lock" or not str(row["query"]).upper().startswith("LOCK TABLE"):
        return None
    if waiting_modes(server, row["pid"], BARRIER_TABLE) != ["AccessShareLock"] or barrier_pid not in blocking_pids(server, row["pid"]):
        return None
    return row


def run_dump_with_operation(cluster, source: str, workspace, owner: SnapshotOwner, server: Engine, ledger: CaseLedger, record: CandidateRecord, log, operation: Callable[[], None]) -> None:
    """
    TEST ORCHESTRATION (not a production lock policy): park `pg_dump` behind a queued exclusive request, run `operation` while it is demonstrably inside its snapshot transaction, then
    cancel the barrier within a bounded deadline and let the dump finish, with the snapshot owner open throughout.
    """
    barrier, barrier_known, barrier_done = SessionOutcome(), threading.Event(), threading.Event()
    dump_box: dict = {}
    dump_done = threading.Event()
    barrier_cancelled = False

    def cancel_barrier() -> bool:
        return bool(scalar(server, "SELECT pg_cancel_backend(:p)", p=barrier.pid)) if barrier.pid is not None else False

    try:
        start_session_thread(BARRIER_APP, ledger, cluster.migrator_url(source), f"LOCK TABLE public.{BARRIER_TABLE} IN ACCESS EXCLUSIVE MODE", barrier, barrier_known, barrier_done,
                             lock_timeout=BARRIER_LOCK_TIMEOUT, kind="ddl")
        if not barrier_known.wait(BARRIER_TIMEOUT_S) or barrier.pid is None:
            raise PreconditionFailed("the barrier session did not start")
        record.pids["barrier"] = barrier.pid
        if not wait_until(lambda: waiting_modes(server, barrier.pid, BARRIER_TABLE) == ["AccessExclusiveLock"] and owner.pid in blocking_pids(server, barrier.pid), BARRIER_QUEUE_DEADLINE_S):
            raise PreconditionFailed("the barrier's exclusive request never appeared as a waiting lock blocked by the snapshot owner")
        record.barrier["queued"] = {"waiting_modes": waiting_modes(server, barrier.pid, BARRIER_TABLE), "blocked_by": blocking_pids(server, barrier.pid), "owner_pid": owner.pid}
        log("barrier_confirmed_queued_behind_the_owner", pid=barrier.pid)

        def run_dump() -> None:
            try:
                dump_box["result"] = run_pg_dump(cluster, source, workspace.archive_path, workspace.backup_pgpass, snapshot=owner.exported_id, commands=record.commands)
            except BaseException as exc:  # noqa: BLE001 -- reported to the main thread
                dump_box["error"] = exc
            finally:
                dump_done.set()

        thread = threading.Thread(target=run_dump, name="candidate-dump", daemon=True)
        ledger.threads.append(thread)
        log("dump_started")
        thread.start()
        if not wait_until(lambda: dump_is_parked(server, source, barrier.pid) is not None or dump_done.is_set(), PARK_DEADLINE_S):
            raise PreconditionFailed("pg_dump was never observed parked behind the barrier")
        parked = dump_is_parked(server, source, barrier.pid)
        if parked is None:
            raise PreconditionFailed("pg_dump finished or failed before it could be observed inside its transaction waiting on the barrier")
        record.owner_xmin = scalar(server, "SELECT backend_xmin::text FROM pg_stat_activity WHERE pid = :p", p=owner.pid)
        record.dump_parked = {**parked, "owner_pid": owner.pid, "owner_backend_xmin": record.owner_xmin, "blocked_by": blocking_pids(server, parked["pid"]), "barrier_pid": barrier.pid}
        record.pids["dump"] = parked["pid"]
        log("dump_confirmed_inside_its_snapshot_transaction_and_parked", pid=parked["pid"])

        operation()  # the catalog/ACL operation: proven committed (or blocked) by the caller's own evidence

        again = dump_is_parked(server, source, barrier.pid)
        record.dump_parked["still_parked_after_the_operation"] = again is not None and again["pid"] == parked["pid"]
        log("dump_still_parked_after_the_operation" if record.dump_parked["still_parked_after_the_operation"] else "dump_NOT_parked_after_the_operation")

        barrier_cancelled = cancel_barrier()
        if not barrier_done.wait(BARRIER_CANCEL_DEADLINE_S):
            raise PreconditionFailed("the barrier session did not end within its deadline after cancellation")
        record.barrier.update({"cancel_accepted": barrier_cancelled, "sqlstate": barrier.sqlstate, "error": barrier.error, "committed": barrier.committed, "elapsed_s": barrier.elapsed,
                               "still_present": waiting_modes(server, barrier.pid, BARRIER_TABLE) != [] or bool(rows(server, "SELECT 1 FROM pg_stat_activity WHERE pid = :p", p=barrier.pid))})
        log("barrier_cancelled_and_removed", pid=barrier.pid, sqlstate=barrier.sqlstate)
        if not dump_done.wait(DUMP_TIMEOUT_S):
            raise PreconditionFailed("pg_dump did not exit within its deadline after the barrier was removed")
        if "error" in dump_box:
            raise dump_box["error"]
        record.dump = dump_box["result"]
        log("dump_exited", returncode=record.dump.returncode)
    finally:
        if not barrier_done.is_set() or not barrier_cancelled:
            with contextlib.suppress(Exception):
                cancel_barrier()  # a failed orchestration must never leave the exclusive request (and so a parked dump) behind


def archive_mentions(cluster, archive_path: str, tokens: list[str]) -> dict:
    """A READ of the finished archive (schema only): the lines that mention each token. Analysis, not a restore."""
    result = _exec(cluster, ["pg_restore", "--schema-only", "--file=-", archive_path], timeout=60)
    if result.returncode != 0:
        return {"error": result.diagnostics()[:200]}
    mentions = {token: [line.strip()[:160] for line in result.stdout.splitlines() if token in line][:5] for token in tokens}
    mentions["GRANT INSERT ON public.teams"] = [line.strip()[:160] for line in result.stdout.splitlines() if "GRANT INSERT" in line and "teams" in line][:5]
    return mentions


def run_candidate_case(harness_: Harness, scenario: CandidateScenario, ledger: CaseLedger, roles: RoleLedger) -> CandidateRecord:
    cluster = harness_.cluster
    record = CandidateRecord(scenario.name, scenario.required, scenario.window, role_setup=scenario.role_setup)
    events = record.events

    def log(name: str, **detail) -> None:
        record.actions.append(name)
        events.add(name, **detail)

    workspace = prepare_case_workspace(cluster, ledger)
    source = record.source_database = workspace.source_database
    log("source_provisioned")
    server = cluster.admin_engine(source)  # the server's own evidence (read-only use)
    ledger.engines.append(server)
    observer = _fresh_engine(cluster, source, ledger, OBSERVER_APP)

    pair = None
    if scenario.role_setup:
        pair = (roles.create("a"), roles.reserve("b"))
        record.role_names = {"before_rename": pair[0], "after_rename": pair[1]}
        apply_role_setup(cluster, source, scenario.role_setup, pair[0], ledger)
        log("role_setup_committed", kind=scenario.role_setup)
    change = build_changes(pair)[scenario.change] if scenario.change else None

    def apply_and_prove() -> None:
        """Applies the change in a separate session and PROVES from fresh observers whether it committed and is visible (blocked or rolled back does not count)."""
        lock_free = scenario.window == "during_dump"  # the existing index read queues behind the barrier: visibility is then proven with lock-free reads (see `lockfree_probe`)
        record.visibility_method = "lock_free_probe" if lock_free else "full_observation"
        observe = (lambda label: lockfree_probe(observer)) if lock_free else (lambda label: observe_endpoint(observer, label))
        categories = probe_changed_categories if lock_free else (lambda before, after: changed_categories(before.observation, after.observation))
        record.pre_change = observe("pre_change")
        record.change = run_change(cluster, source, change, ledger, after_step=lambda: categories(record.pre_change, observe("after_step")))
        record.pids["change"] = record.change["pid"]
        log("change_attempted", status="committed" if record.change["committed"] else "blocked" if record.change["blocked"] else "failed", sqlstate=record.change["sqlstate"], pid=record.change["pid"])
        record.post_change = observe("post_change")
        record.fresh_visible_categories = categories(record.pre_change, record.post_change)
        if record.change["committed"]:
            log("change_visible_to_a_fresh_observer" if record.fresh_visible_categories or change.category is None else "change_committed_but_not_visible", categories=record.fresh_visible_categories)

    # ---- first endpoint (a fresh observer transaction), BEFORE the export ---------------------------------------------------------------------
    owner = SnapshotOwner(cluster, source, ledger)
    record.pids["owner"] = owner.pid
    log("owner_open", pid=owner.pid)
    record.first = observe_endpoint(observer, "first_endpoint")
    record.pids["observer"] = record.first.pid
    log("first_endpoint_observed", pid=record.first.pid, isolation=record.first.isolation)

    # ---- the data snapshot: exported by the owner; ALL baseline reads stay on the owner's connection -----------------------------------------------
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
    record.owner_locked_tables_after_baseline = sorted(granted_table_locks(server, owner.pid))
    if change is not None and scenario.window == "after_reads":
        apply_and_prove()

    # ---- the dump, with the owner open until it exits ------------------------------------------------------------------------------------------
    if scenario.window == "during_dump":
        run_dump_with_operation(cluster, source, workspace, owner, server, ledger, record, log, apply_and_prove)
    else:
        log("dump_started")
        record.dump = run_pg_dump(cluster, source, workspace.archive_path, workspace.backup_pgpass, snapshot=owner.exported_id, commands=record.commands)
        log("dump_exited", returncode=record.dump.returncode)
    if record.dump.returncode != 0:
        record.outcome = "dump_failed"
        owner.end_transaction()
        raise CaptureStepFailed(f"pg_dump failed unexpectedly: {record.dump.diagnostics()}")
    record.dump_pid_from_log = sorted({int(s["pid"]) for s in authenticated_sessions(_container_logs(cluster.container), app="pg_dump", user=BACKUP_ROLE, database=source)})

    # ---- the second endpoint (a fresh observer transaction); the owner is still open ---------------------------------------------------------------
    record.owner_state_at_dump_exit = backend_state(cluster, owner.pid)
    record.owner_after_in_transaction = observe_catalog(owner.connection)  # contrast only: what the owner's own repeated read sees
    record.second = observe_endpoint(observer, "second_endpoint")
    record.descriptor_after_dump = owner.visibility_descriptor()
    log("second_endpoint_observed", pid=record.second.pid)
    owner.end_transaction()
    log("owner_ended")
    before, after = record.first.observation, record.second.observation
    record.detection = {"changed": changed_categories(before, after) != [], "categories": changed_categories(before, after)}
    record.differences = raw_differences(before, after)
    record.archive_mentions = archive_mentions(cluster, workspace.archive_path, [INDEX_NAME, CHECK_NAME, FK_NAME, *(pair or ())])

    # ---- the guard in the real flow: a refusal ends the flow before restore and finalization ----------------------------------------------------------
    try:
        require_catalog_unchanged(before, after, CANDIDATE)
    except PreconditionFailed:
        record.outcome = "catalog_changed"
        log("catalog_changed_guard_refused")
        record.workspace = workspace  # kept for the separately labeled diagnostic path; the flow itself ends here
        return record
    log("catalog_guard_passed")

    try:
        restored = restore_into_fresh_database(cluster, ledger, workspace.archive_path, workspace.migrator_pgpass, record.commands, log)
    except CaptureStepFailed as failure:
        record.restore_failure = str(failure)[:300]
        record.outcome = "restore_failed"
        log("restore_failed")
        return record
    record.destination_database = restored.name
    comparison = compare_to_baseline(record.baseline, restored.admin_engine)
    record.harm = {"fingerprints_equal": not comparison.fingerprint_diffs, "indexes_equal": not comparison.index_diff, "constraints_equal": not comparison.constraint_genuine_diff,
                   "acls_equal": comparison.acls_equal, "owners_equal": comparison.owners_equal, "fingerprints_checked": len(all_fingerprints(restored.admin_engine))}
    log("compared")
    record.finalization_invoked = True  # the stand-in for finalization: reached only when every guard passed
    record.outcome = "finalization_permitted"
    log("finalization_permitted")
    return record


def run_diagnostic_restore(harness_: Harness, ledger: CaseLedger, record: CandidateRecord) -> None:
    """
    DIAGNOSTIC ONLY (never a capture, never finalization): after a refusal, restore the dump into a fresh disposable database to learn what the archive would have done, under its own
    `diagnostic:*` action names, with the existing restore recipe unchanged and the strict comparator against the owner's baseline.
    """
    cluster, workspace = harness_.cluster, record.workspace
    note = record.diagnostic_actions.append
    record.diagnostic = {"label": "DIAGNOSTIC: forensic restore after a catalog_changed refusal; not a capture", "commands": []}
    try:
        restored = restore_into_fresh_database(cluster, ledger, workspace.archive_path, workspace.migrator_pgpass, record.diagnostic["commands"], lambda name: note(f"diagnostic:{name}"))
    except CaptureStepFailed as failure:
        record.diagnostic["restore_failure"] = str(failure)[:400]
        note("diagnostic:restore_failed")
        return
    comparison = compare_to_baseline(record.baseline, restored.admin_engine)
    record.diagnostic["harm"] = {"fingerprints_equal": not comparison.fingerprint_diffs, "indexes_equal": not comparison.index_diff, "constraints_equal": not comparison.constraint_genuine_diff,
                                 "acls_equal": comparison.acls_equal, "owners_equal": comparison.owners_equal}
    record.diagnostic["index_diff_keys"] = sorted(str(k) for k in comparison.index_diff)
    record.diagnostic["constraint_diff_keys"] = sorted(str(k) for k in comparison.constraint_genuine_diff)
    note("diagnostic:compared")


def _run_candidate(harness_: Harness, name: str) -> CandidateRecord:
    scenario = CANDIDATE_SCENARIOS[name]
    with disposable_roles(harness_.cluster) as roles:  # exits AFTER the case ledger: roles are dropped once their databases are gone
        with owned_case(harness_.cluster) as ledger:
            record = run_candidate_case(harness_, scenario, ledger, roles)
            record.artifact_listing = sorted(p.name for p in Path(next(iter(ledger.directories))).iterdir())  # BEFORE any diagnostic: the flow itself left nothing
            if record.outcome == "catalog_changed":
                run_diagnostic_restore(harness_, ledger, record)
        record.verification = ledger.verification  # set when the ledger exits: its cleanup has then run and been verified
    record.roles_verification = roles.verification  # set when the role ledger exits (after the databases are gone)
    return record


# ==========================================================================
# probes (no dump): observer semantics, and DROP ROLE
# ==========================================================================
@dataclass
class SemanticsRecord:
    actions: list = field(default_factory=list)
    separate: dict = field(default_factory=dict)
    checkouts: dict = field(default_factory=dict)
    matrix: list = field(default_factory=list)
    verification: dict = field(default_factory=dict)
    roles_verification: dict = field(default_factory=dict)


ISOLATIONS = ("READ COMMITTED", "REPEATABLE READ")
ISOLATION_TAG = {"READ COMMITTED": "rc", "REPEATABLE READ": "rr"}
SEMANTICS_CHANGES = ("index", "role_rename", "column_rename")
COLUMN_RENAME = "ALTER TABLE public.players RENAME COLUMN season_id TO season_id_renamed"


def run_semantics_case(harness_: Harness, ledger: CaseLedger, roles: RoleLedger) -> SemanticsRecord:
    cluster = harness_.cluster
    record = SemanticsRecord()
    workspace = prepare_case_workspace(cluster, ledger)
    source = workspace.source_database
    record.actions.append("source_provisioned")
    pair = (roles.create("a"), roles.reserve("b"))
    apply_role_setup(cluster, source, "acl_entry", pair[0], ledger)
    observer = _fresh_engine(cluster, source, ledger, OBSERVER_APP)
    changes = build_changes(pair)
    changes["column_rename"] = Change("column_rename", "admin", (COLUMN_RENAME,), "constraints")  # a rename changes definition TEXT without adding or removing a constraint or index row

    # ---- A: separate transactions per endpoint; what an Engine does across several reads --------------------------------------------------------
    e1, e2 = observe_endpoint(observer, "e1"), observe_endpoint(observer, "e2")
    record.separate = {"pids": [e1.pid, e2.pid], "same_backend_reused": e1.pid == e2.pid, "each_one_transaction": [e1.one_transaction, e2.one_transaction],
                       "distinct_transactions": e1.txn_start_first != e2.txn_start_first, "isolation": [e1.isolation, e2.isolation]}
    counter = {"n": 0}
    event.listen(observer, "checkout", lambda *a: counter.__setitem__("n", counter["n"] + 1))
    before = counter["n"]
    observe_catalog(observer)  # through the ENGINE: every helper opens its own connection (and so its own transaction)
    through_engine = counter["n"] - before
    before = counter["n"]
    with observer.connect() as conn:
        observe_catalog(conn)  # through ONE Connection
    record.checkouts = {"observe_catalog_through_an_engine": through_engine, "observe_catalog_through_one_connection": counter["n"] - before}
    record.actions.append("separate_transactions_observed")

    # ---- B: a long-lived observer (READ COMMITTED and REPEATABLE READ) re-reads after a committed change; a fresh observer is the reference -----------
    for key in SEMANTICS_CHANGES:
        change = changes[key]
        long_lived = {}
        for isolation in ISOLATIONS:
            engine = _fresh_engine(cluster, source, ledger, f"{OBSERVER_APP}_{ISOLATION_TAG[isolation]}")
            conn = engine.connect()
            conn = conn.execution_options(isolation_level=isolation)
            conn.execute(text(f"SET LOCAL lock_timeout = '{OBSERVER_LOCK_TIMEOUT}'"))
            conn.execute(text(f"SET LOCAL statement_timeout = '{OBSERVER_STATEMENT_TIMEOUT}'"))
            started = scalar(conn, "SELECT now()::text")
            first = observe_catalog(conn)
            long_lived[isolation] = (conn, started, first, scalar(conn, "SELECT pg_backend_pid()"))
        reference = observe_endpoint(observer, "reference_before")
        applied = run_change(cluster, source, change, ledger)
        fresh = observe_endpoint(observer, "reference_after")
        for isolation, (conn, started, first, pid) in long_lived.items():
            second = observe_catalog(conn)
            same_transaction = scalar(conn, "SELECT now()::text") == started
            conn.rollback()
            conn.close()
            record.matrix.append({"change": key, "isolation": isolation, "committed": applied["committed"], "sqlstate": applied["sqlstate"], "pid": pid, "same_transaction": same_transaction,
                                  "long_lived_saw": changed_categories(first, second), "fresh_observer_saw": changed_categories(reference.observation, fresh.observation),
                                  "differences": {c: d for c, d in raw_differences(first, second).items() if d}})
        record.actions.append(f"semantics_observed:{key}")
    return record


@dataclass
class LockBehaviorRecord:
    actions: list = field(default_factory=list)
    barrier: dict = field(default_factory=dict)
    reads: dict = field(default_factory=dict)
    full_observation: dict = field(default_factory=dict)
    recovered: dict = field(default_factory=dict)
    verification: dict = field(default_factory=dict)
    roles_verification: dict = field(default_factory=dict)


CATEGORY_READS = {"constraints": constraint_snapshot, "acls": acl_snapshot, "indexes": index_snapshot}  # the EXISTING reads, one per category
PROBE_READ_LOCK_TIMEOUT = "4s"


def timed_category_read(server: Engine, observer: Engine, ledger: CaseLedger, fn: Callable, barrier_pid: int | None) -> dict:
    """One existing catalog read in its own fresh transaction on a thread, with the server's view of what it waits on while it runs (bounded by lock_timeout)."""
    box: dict = {}

    def work() -> None:
        started = time.monotonic()
        try:
            with observer.connect() as conn:
                conn.execute(text(f"SET LOCAL lock_timeout = '{PROBE_READ_LOCK_TIMEOUT}'"))
                conn.execute(text(f"SET LOCAL statement_timeout = '{OBSERVER_STATEMENT_TIMEOUT}'"))
                box["pid"] = scalar(conn, "SELECT pg_backend_pid()")
                box["rows"] = len(fn(conn))
                box["outcome"] = "rows"
        except DBAPIError as exc:
            box["outcome"], box["sqlstate"] = "error", getattr(exc.orig, "sqlstate", None)
        finally:
            box["elapsed_s"] = round(time.monotonic() - started, 2)
            box["finished"] = True

    thread = threading.Thread(target=work, name="candidate-category-read", daemon=True)
    ledger.threads.append(thread)
    thread.start()

    def waiting() -> list:
        return rows(server, "SELECT c.relname, l.mode FROM pg_locks l JOIN pg_class c ON c.oid = l.relation WHERE l.pid = :p AND NOT l.granted", p=box["pid"]) if box.get("pid") else []

    wait_until(lambda: box.get("finished", False) or bool(waiting()), 8)
    seen = waiting()
    result = {"waiting": seen, "blocked_by": blocking_pids(server, box["pid"]) if seen else [], "barrier_pid": barrier_pid}
    thread.join(timeout=20)
    result.update({k: box.get(k) for k in ("outcome", "rows", "sqlstate", "elapsed_s", "pid")})
    return result


def run_lock_behavior_case(harness_: Harness, ledger: CaseLedger, roles: RoleLedger) -> LockBehaviorRecord:
    """Does the candidate's own observation queue behind a pending exclusive request, and what does it do when it does? (No dump: only the owner and the barrier.)"""
    cluster = harness_.cluster
    record = LockBehaviorRecord()
    workspace = prepare_case_workspace(cluster, ledger)
    source = workspace.source_database
    server = cluster.admin_engine(source)
    ledger.engines.append(server)
    observer = _fresh_engine(cluster, source, ledger, OBSERVER_APP)
    owner = SnapshotOwner(cluster, source, ledger)
    owner.begin_and_export()
    capture_baseline(owner.connection)  # ACCESS SHARE on every table, held until the owner ends
    barrier, barrier_known, barrier_done = SessionOutcome(), threading.Event(), threading.Event()
    start_session_thread(BARRIER_APP, ledger, cluster.migrator_url(source), f"LOCK TABLE public.{BARRIER_TABLE} IN ACCESS EXCLUSIVE MODE", barrier, barrier_known, barrier_done,
                         lock_timeout=BARRIER_LOCK_TIMEOUT, kind="ddl")
    try:
        if not barrier_known.wait(BARRIER_TIMEOUT_S) or barrier.pid is None:
            raise PreconditionFailed("the barrier session did not start")
        if not wait_until(lambda: waiting_modes(server, barrier.pid, BARRIER_TABLE) == ["AccessExclusiveLock"] and owner.pid in blocking_pids(server, barrier.pid), BARRIER_QUEUE_DEADLINE_S):
            raise PreconditionFailed("the barrier's exclusive request never appeared as a waiting lock blocked by the snapshot owner")
        record.barrier = {"pid": barrier.pid, "owner_pid": owner.pid, "waiting_modes": waiting_modes(server, barrier.pid, BARRIER_TABLE), "blocked_by": blocking_pids(server, barrier.pid)}
        record.actions.append("barrier_confirmed_queued_behind_the_owner")
        for category, fn in CATEGORY_READS.items():
            record.reads[category] = timed_category_read(server, observer, ledger, fn, barrier.pid)
            record.actions.append(f"read_while_queued:{category}")
        started = time.monotonic()
        try:
            observe_endpoint(observer, "blocked_full_observation")
            record.full_observation = {"outcome": "returned_an_observation"}
        except DBAPIError as exc:
            record.full_observation = {"outcome": "error", "sqlstate": getattr(exc.orig, "sqlstate", None), "elapsed_s": round(time.monotonic() - started, 2)}
        record.actions.append("full_observation_attempted_while_queued")
    finally:
        with contextlib.suppress(Exception):
            scalar(server, "SELECT pg_cancel_backend(:p)", p=barrier.pid) if barrier.pid is not None else None
        barrier_done.wait(BARRIER_CANCEL_DEADLINE_S)
    record.actions.append("barrier_cancelled")
    record.recovered = {"indexes": timed_category_read(server, observer, ledger, index_snapshot, None), "full_observation_returned": observe_endpoint(observer, "recovered").one_transaction,
                        "barrier_sqlstate": barrier.sqlstate, "barrier_committed": barrier.committed}
    owner.end_transaction()
    record.actions.append("owner_ended")
    return record


@dataclass
class DropRoleRecord:
    actions: list = field(default_factory=list)
    rejected: dict = field(default_factory=dict)
    committed_drop: dict = field(default_factory=dict)
    observation_unchanged: dict = field(default_factory=dict)
    verification: dict = field(default_factory=dict)
    roles_verification: dict = field(default_factory=dict)


def _drop_role(cluster, role: str) -> dict:
    assert role.startswith(ROLE_PREFIX) and SAFE_ROLE.fullmatch(role)  # only a role this test created
    admin = cluster.admin_engine()
    outcome = {"role": "<disposable>", "sqlstate": None, "error": None}
    try:
        try:
            with admin.connect() as conn:
                conn.execute(text(f"DROP ROLE {role}"))
        except DBAPIError as exc:
            outcome["sqlstate"], outcome["error"] = getattr(exc.orig, "sqlstate", None), type(exc.orig).__name__
        outcome["role_exists_after"] = scalar(admin, "SELECT count(*) FROM pg_roles WHERE rolname = :r", r=role)
    finally:
        admin.dispose()
    outcome["committed"] = outcome["sqlstate"] is None
    return outcome


def run_drop_role_case(harness_: Harness, ledger: CaseLedger, roles: RoleLedger) -> DropRoleRecord:
    cluster = harness_.cluster
    record = DropRoleRecord()
    workspace = prepare_case_workspace(cluster, ledger)
    source = workspace.source_database
    owning, unreferenced = roles.create("a"), roles.create("b")
    apply_role_setup(cluster, source, "table_owner", owning, ledger)  # the first disposable role now OWNS a table; the second is referenced by nothing
    observer = _fresh_engine(cluster, source, ledger, OBSERVER_APP)
    baseline = observe_endpoint(observer, "before")
    record.rejected = _drop_role(cluster, owning)
    record.observation_unchanged["after_rejection"] = changed_categories(baseline.observation, observe_endpoint(observer, "after_rejection").observation) == []
    record.actions.append("drop_attempted_on_a_role_that_owns_a_table")
    record.committed_drop = _drop_role(cluster, unreferenced)
    record.observation_unchanged["after_committed_drop"] = changed_categories(baseline.observation, observe_endpoint(observer, "after_drop").observation) == []
    record.actions.append("drop_committed_on_an_unreferenced_role")
    return record


def _run_probe(harness_: Harness, runner: Callable):
    with disposable_roles(harness_.cluster) as roles:
        with owned_case(harness_.cluster) as ledger:
            record = runner(harness_, ledger, roles)
        record.verification = ledger.verification
    record.roles_verification = roles.verification
    return record


class Registry:
    """Runs each scenario at most once per module inside its own cleanup ledger; a failure is cached and re-raised so a scenario is never run twice."""

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

    def case(self, name: str) -> CandidateRecord:
        return self._get(("case", name), lambda: _run_candidate(self.harness, name))

    def semantics(self) -> SemanticsRecord:
        return self._get(("semantics",), lambda: _run_probe(self.harness, run_semantics_case))

    def drop_role(self) -> DropRoleRecord:
        return self._get(("drop_role",), lambda: _run_probe(self.harness, run_drop_role_case))

    def lock_behavior(self) -> LockBehaviorRecord:
        return self._get(("lock_behavior",), lambda: _run_probe(self.harness, run_lock_behavior_case))


@pytest.fixture(scope="module")
def runs(harness):
    return Registry(harness)


def key_of(name: str) -> tuple:
    return EXPECTED_KEY[CANDIDATE_SCENARIOS[name].change]


def category_of(name: str) -> str:
    return EXPECTED_CATEGORY[CANDIDATE_SCENARIOS[name].change]


REFUSED = ["s2a_constraint_before_locks", "s2c_constraint_after_locks_compatible", "s3a_acl_during_dump", "s3b_index_during_dump", "s3c_constraint_during_dump", "s4a_role_rename_during_dump",
           "s4b_owner_role_rename_after_export"]
PERMITTED = ["s1_control", "s2b_constraint_after_locks_blocked", "s4b_owner_role_control", "s5_acl_change_and_reversal_during_dump"]
S3_OPERATIONS = ["s3a_acl_during_dump", "s3b_index_during_dump", "s3c_constraint_during_dump"]
ROLE_RENAMES = ["s4a_role_rename_during_dump", "s4b_owner_role_rename_after_export"]
SOURCE = Path(__file__).read_text(encoding="utf-8")  # for the static tests


# ==========================================================================
# scope and the pure pieces (no cluster needed)
# ==========================================================================
def test_the_scenarios_are_exactly_s1_to_s5_and_nothing_else_is_claimed():
    assert VALIDATED_SCENARIOS == ("S1", "S2", "S3", "S4", "S5")
    assert {s.required for s in CANDIDATE_SCENARIOS.values()} == set(VALIDATED_SCENARIOS)
    assert set(REFUSED) | set(PERMITTED) == set(CANDIDATE_SCENARIOS) and not set(REFUSED) & set(PERMITTED)


def test_events_are_ordered_by_sequence_number_and_an_unknown_or_repeated_event_is_refused():
    events = Events()
    events.add("a")
    events.add("b", pid=1)
    assert events.names == ["a", "b"] and events.in_order("a", "b") and not events.in_order("b", "a")
    events.add("a")
    with pytest.raises(AssertionError):
        events.seq("a")
    with pytest.raises(AssertionError):
        events.seq("missing")


def test_the_candidate_reuses_the_existing_catalog_definitions_and_defines_no_second_baseline_representation():
    import tests.postgres.test_postgres_backup_catalog_consistency as c6

    assert observe_catalog is c6.observe_catalog and require_catalog_unchanged is c6.require_catalog_unchanged and raw_differences is c6.raw_differences
    code = "\n".join(line for line in SOURCE.splitlines() if not line.lstrip().startswith("#"))
    forbidden = ("pg_get_" "constraintdef", "pg_" "indexes", "FROM pg_" "constraint", "rel" "acl", "class " "Baseline", "Baseline" "(", "def " "observe_catalog", "def " "constraint_snapshot", "def " "acl_snapshot", "def " "index_snapshot")
    assert [token for token in forbidden if token in code] == [], "this module only imports the existing catalog reads and baseline"


def test_the_lock_free_proof_probe_never_feeds_the_candidate_guard():
    import inspect

    src = inspect.getsource(run_candidate_case)
    assert 'record.first = observe_endpoint(observer, "first_endpoint")' in src and 'record.second = observe_endpoint(observer, "second_endpoint")' in src, "the endpoints are full observations"
    assert src.count("lockfree_probe(") == 1, "the lock-free probe appears only inside the visibility proof"
    assert "before, after = record.first.observation, record.second.observation" in src and "require_catalog_unchanged(before, after, CANDIDATE)" in src


def test_every_observer_transaction_sets_bounded_waits_before_it_reads():
    import inspect

    source = inspect.getsource(observe_endpoint)
    assert "lock_timeout" in source and "statement_timeout" in source and source.index("lock_timeout") < source.index("observe_catalog(conn)")


def test_role_names_are_generated_with_the_test_prefix_and_never_an_application_backup_or_migrator_role():
    ledger = RoleLedger(cluster=None)
    names = [ledger.reserve("a"), ledger.reserve("b")]
    assert all(SAFE_ROLE.fullmatch(n) and n.startswith(ROLE_PREFIX) for n in names) and ledger.names == names and len(set(names)) == 2
    assert not {BACKUP_ROLE, RUNTIME_ROLE, MIGRATOR_ROLE} & set(names)
    with pytest.raises(AssertionError):
        build_changes((BACKUP_ROLE, ROLE_PREFIX + "0123456789_b"))
    with pytest.raises(AssertionError):
        _drop_role(None, MIGRATOR_ROLE)


_ROLE_STATEMENT = re.compile(r"(ALTER ROLE|DROP ROLE)([^\n]*)")


def test_the_only_role_statements_in_this_module_target_roles_the_test_created():
    statements = [m for m in _ROLE_STATEMENT.finditer(SOURCE) if SOURCE[max(0, m.start() - 2):m.start()] == 'f"']
    assert len(statements) == 3, "the rename, the cleanup drop and the probe drop: each an f-string that interpolates a prefix-checked, test-created role name"
    for match in statements:
        tail = match.group(2)
        assert "{name}" in tail or "{before}" in tail or "{role}" in tail, tail
        assert not any(role in tail for role in ("BACKUP_" "ROLE", "RUNTIME_" "ROLE", "MIGRATOR_" "ROLE")), tail


def test_no_grant_to_the_backup_role_and_no_termination_of_any_session_exists_in_the_scenarios():
    code = "\n".join(line for line in SOURCE.splitlines() if not line.lstrip().startswith("#"))
    assert "TO {BACKUP_" "ROLE}" not in code and "TO napsedge_" "backup_ro" not in code
    assert "pg_terminate_" "backend" not in code, "the barrier is CANCELLED; nothing is terminated"


# ==========================================================================
# S1 -- the no-change control
# ==========================================================================
def test_s1_the_control_flow_completes_with_the_candidate_guard_and_strict_equality_with_the_baseline(runs):
    r = runs.case("s1_control")
    assert r.change is None and r.detection == {"changed": False, "categories": []} and all(not d for d in r.differences.values())
    assert r.outcome == "finalization_permitted" and r.finalization_invoked and "catalog_guard_passed" in r.actions and r.dump.returncode == 0
    assert r.harm["fingerprints_equal"] and r.harm["indexes_equal"] and r.harm["constraints_equal"] and r.harm["acls_equal"] and r.harm["owners_equal"] and r.harm["fingerprints_checked"] == 24
    assert r.artifact_listing == [], "finalization is a stand-in: nothing was written"


def test_s1_the_endpoint_digests_of_the_candidate_equal_the_owners_own_baseline_catalog_reads(runs):
    r = runs.case("s1_control")
    assert r.first.observation.digests == r.second.observation.digests == r.baseline_catalog.digests, "one catalog definition, three observers, equal"


@pytest.mark.parametrize("name", list(CANDIDATE_SCENARIOS))
def test_each_endpoint_is_its_own_fresh_transaction_with_recorded_isolation_and_distinct_backends(name, runs):
    r = runs.case(name)
    endpoints = [r.first, r.second]
    assert all(e.one_transaction for e in endpoints), "the three reads of an endpoint share ONE transaction (now() is the transaction start)"
    assert r.first.txn_start_first != r.second.txn_start_first, "the two endpoints are separate transactions"
    assert {e.isolation for e in endpoints} == {"read committed"}
    assert len({r.pids["owner"], r.pids["observer"]}) == 2 and r.pids["observer"] == r.second.pid, "the observer is a different backend from the snapshot owner (the pool reuses ONE observer backend)"


@pytest.mark.parametrize("name", list(CANDIDATE_SCENARIOS))
def test_the_baseline_reads_stayed_on_the_owner_connection_and_the_owner_lived_through_the_dump(name, runs):
    r = runs.case(name)
    assert r.owner_connects_after_baseline == 0 and len(r.owner_locked_tables_after_baseline) == 24, "the server shows the OWNER backend holding ACCESS SHARE on every table"
    assert r.dump.returncode == 0 and r.owner_state_at_dump_exit == "idle in transaction", "the owner was open and idle in its transaction when pg_dump exited"
    assert r.descriptor_at_export == r.descriptor_after_dump, "its snapshot never changed"
    order = [a for a in r.actions if a in {"snapshot_exported", "baseline_data_read_through_owner", "dump_started", "dump_exited", "second_endpoint_observed", "owner_ended"}]
    assert order == ["snapshot_exported", "baseline_data_read_through_owner", "dump_started", "dump_exited", "second_endpoint_observed", "owner_ended"]
    assert r.dump_pid_from_log and (r.dump_pid_from_log == [r.dump_parked["pid"]] if r.dump_parked else True), "the dump's backend PID (TLS and SCRAM verified in the server log)"


# ==========================================================================
# S2 -- constraint changes
# ==========================================================================
def test_s2a_a_constraint_added_before_the_owners_table_locks_commits_and_is_visible_with_no_error(runs):
    r = runs.case("s2a_constraint_before_locks")
    assert r.change["committed"] and r.change["sqlstate"] is None and r.change["blocked"] is False and r.change["committed_steps"] == 1
    assert r.fresh_visible_categories == ["constraints"] and "change_visible_to_a_fresh_observer" in r.actions
    assert r.owner_locked_tables_after_baseline and r.events.in_order("snapshot_exported", "change_attempted", "baseline_data_read_through_owner"), "committed BEFORE the baseline reads took the table locks"
    assert r.change["pid"] not in (r.pids["owner"], r.pids["observer"])


def test_s2a_the_candidate_detects_the_constraint_and_refuses_the_flow(runs):
    r = runs.case("s2a_constraint_before_locks")
    assert r.detection == {"changed": True, "categories": ["constraints"]}
    key = ("conferences", CHECK_NAME)
    assert key in r.differences["constraints"] and r.differences["constraints"][key][0] is None and r.differences["constraints"][key][1][2] == "c"
    assert not r.differences["indexes"] and not r.differences["acls"]
    assert r.first.observation.digests["constraints"] != r.second.observation.digests["constraints"] and r.first.observation.digests["indexes"] == r.second.observation.digests["indexes"]


def test_s2b_the_same_change_attempted_after_the_locks_exist_is_blocked_and_never_committed(runs):
    r = runs.case("s2b_constraint_after_locks_blocked")
    assert r.change["committed"] is False and r.change["blocked"] is True and r.change["sqlstate"] == "55P03" and r.change["error"] == "LockNotAvailable" and r.change["committed_steps"] == 0
    assert 4.0 <= r.change["elapsed_s"] <= 15.0, "it waited for its bounded lock timeout and no longer"
    assert r.fresh_visible_categories == [] and "change_visible_to_a_fresh_observer" not in r.actions, "a fresh observer independently sees nothing"
    assert r.events.in_order("baseline_data_read_through_owner", "change_attempted"), "attempted AFTER the owner's baseline reads held ACCESS SHARE"


def test_s2b_a_blocked_change_is_not_detection_and_the_flow_completes_with_nothing_to_detect(runs):
    r = runs.case("s2b_constraint_after_locks_blocked")
    assert r.detection == {"changed": False, "categories": []} and r.outcome == "finalization_permitted"
    assert r.harm["constraints_equal"] and r.harm["fingerprints_equal"], "nothing changed, so baseline and restore agree"
    assert not (r.change["blocked"] and r.detection["changed"]), "a blocked change was never reported as a detected one"


def test_s2c_a_lock_compatible_constraint_commits_after_the_locks_exist_and_the_candidate_refuses(runs):
    r = runs.case("s2c_constraint_after_locks_compatible")
    assert r.change["committed"] and r.change["sqlstate"] is None and r.fresh_visible_categories == ["constraints"]
    assert r.events.in_order("baseline_data_read_through_owner", "change_attempted"), "committed although the owner already held ACCESS SHARE on every table"
    assert r.detection == {"changed": True, "categories": ["constraints"]} and key_of("s2c_constraint_after_locks_compatible") in r.differences["constraints"]
    assert r.differences["constraints"][key_of("s2c_constraint_after_locks_compatible")][1][2] == "f"


@pytest.mark.parametrize("name", ["s2a_constraint_before_locks", "s2b_constraint_after_locks_blocked", "s2c_constraint_after_locks_compatible"])
def test_s2_no_constraint_was_dropped_and_no_grant_to_the_backup_role_was_made(name, runs):
    r = runs.case(name)
    removed = [k for k, (before, after) in r.differences["constraints"].items() if after is None]
    assert removed == [] and not r.differences["acls"], "additive changes only; the backup role's ACL observation is untouched"


# ==========================================================================
# S3 -- committed changes during a real pg_dump
# ==========================================================================
@pytest.mark.parametrize("name", DURING_DUMP)
def test_s3_the_dump_was_demonstrably_inside_its_snapshot_transaction_and_parked_before_the_operation(name, runs):
    r = runs.case(name)
    parked = r.dump_parked
    assert parked["xact_start"] is not None and parked["wait_event_type"] == "Lock" and parked["query"].upper().startswith("LOCK TABLE") and parked["usename"] == BACKUP_ROLE
    assert parked["barrier_pid"] in parked["blocked_by"] and parked["pid"] not in (r.pids["owner"], r.pids["observer"], r.pids["barrier"])
    assert r.barrier["queued"]["waiting_modes"] == ["AccessExclusiveLock"] and r.barrier["queued"]["blocked_by"] == [r.pids["owner"]]
    assert parked["backend_xmin"] is not None and parked["backend_xmin"] == parked["owner_backend_xmin"], "the dump backend holds the xmin of the OWNER's exported snapshot: it has imported it"


@pytest.mark.parametrize("name", DURING_DUMP)
def test_observed_pg_dump_requests_its_table_locks_in_one_batched_statement_and_parks_inside_it(name, runs):
    """OBSERVED (PostgreSQL 16 client tools): the dump's active statement is ONE `LOCK TABLE a, b, c ...` naming many tables, so it is parked partway through its table locks."""
    parked = runs.case(name).dump_parked
    assert parked["query"].upper().startswith("LOCK TABLE PUBLIC.") and parked["query"].count(",") >= 1 and parked["wait_event"] == "relation"


@pytest.mark.parametrize("name", DURING_DUMP)
def test_s3_the_operation_committed_after_the_dump_parked_and_before_it_exited_with_the_owner_open(name, runs):
    r = runs.case(name)
    assert r.events.in_order("owner_open", "first_endpoint_observed", "snapshot_exported", "baseline_data_read_through_owner", "barrier_confirmed_queued_behind_the_owner", "dump_started",
                             "dump_confirmed_inside_its_snapshot_transaction_and_parked", "change_attempted", "dump_still_parked_after_the_operation", "barrier_cancelled_and_removed",
                             "dump_exited", "second_endpoint_observed", "owner_ended")
    assert r.dump_parked["still_parked_after_the_operation"] is True, "the dump had not moved when the operation finished"
    assert r.dump.returncode == 0 and r.owner_state_at_dump_exit == "idle in transaction" and r.descriptor_at_export == r.descriptor_after_dump


@pytest.mark.parametrize("name", DURING_DUMP)
def test_s3_the_barrier_was_cancelled_within_its_deadline_and_left_nothing_behind(name, runs):
    b = runs.case(name).barrier
    assert b["cancel_accepted"] is True and b["sqlstate"] == "57014" and b["error"] == "QueryCanceled" and b["committed"] is False and b["still_present"] is False
    assert b["elapsed_s"] < 30, "the barrier lived only as long as the orchestration needed"


@pytest.mark.parametrize("name", S3_OPERATIONS)
def test_s3_each_committed_operation_is_visible_to_a_fresh_observer_and_the_candidate_refuses(name, runs):
    r = runs.case(name)
    assert r.change["committed"] and r.change["sqlstate"] is None and r.fresh_visible_categories == [category_of(name)]
    assert r.detection == {"changed": True, "categories": [category_of(name)]} and key_of(name) in r.differences[category_of(name)]
    assert r.outcome == "catalog_changed" and r.pids["change"] not in (r.pids["dump"], r.pids["barrier"], r.pids["owner"])


CHANGE_TOKEN = {"s2a_constraint_before_locks": CHECK_NAME, "s2c_constraint_after_locks_compatible": FK_NAME, "s3a_acl_during_dump": "GRANT INSERT ON public.teams",
                "s3b_index_during_dump": INDEX_NAME, "s3c_constraint_during_dump": FK_NAME}


@pytest.mark.parametrize("name", list(CHANGE_TOKEN))
def test_observed_the_archive_did_not_contain_any_committed_change_and_matched_the_baseline(name, runs):
    """
    OBSERVED, for these operations in these windows (PostgreSQL 16 tools, small synthetic schema), not a general claim: the committed change was ABSENT from the archive and a diagnostic
    restore equals the owner's baseline on every established output. The candidate's refusal was therefore CONSERVATIVE here: it reports a live catalog difference between its endpoints,
    which is not the same thing as an archive that disagrees with its baseline.
    """
    r = runs.case(name)
    assert r.archive_mentions[CHANGE_TOKEN[name]] == [], "the archive does not carry the change"
    assert r.diagnostic["harm"] == {"fingerprints_equal": True, "indexes_equal": True, "constraints_equal": True, "acls_equal": True, "owners_equal": True}
    assert r.outcome == "catalog_changed", "refused all the same"


# ==========================================================================
# S4 -- role changes (disposable, test-owned roles only)
# ==========================================================================
@pytest.mark.parametrize("name", ROLE_RENAMES)
def test_s4_a_committed_rename_of_a_disposable_role_changes_the_acl_observation_and_is_refused(name, runs):
    r = runs.case(name)
    before, after = r.role_names["before_rename"], r.role_names["after_rename"]
    assert r.change["committed"] and r.change["sqlstate"] is None and r.change["role"] == "admin" and r.fresh_visible_categories == ["acls"]
    assert r.detection == {"changed": True, "categories": ["acls"]} and r.outcome == "catalog_changed"
    changed = [pair for pair in r.differences["acls"].values()]
    assert changed and all(before in str(b) and after in str(a) for b, a in changed), "the observation names the role: the old name before, the new name after"
    assert not r.differences["constraints"] and not r.differences["indexes"]


def test_s4_the_owner_control_without_a_rename_has_equal_endpoints_and_the_unchanged_restore_recipe_cannot_restore_a_non_migrator_owner(runs):
    """OBSERVED about the EXISTING restore recipe (migrator, no --no-owner), independent of any rename: a table owned by another role cannot be restored."""
    r = runs.case("s4b_owner_role_control")
    assert r.change is None and r.detection == {"changed": False, "categories": []} and "catalog_guard_passed" in r.actions
    assert r.outcome == "restore_failed" and r.finalization_invoked is False and 'must be able to SET ROLE' in r.restore_failure
    assert any(f"OWNER TO {r.role_names['before_rename']}" in line for line in r.archive_mentions[r.role_names["before_rename"]])


def test_observed_a_rename_during_the_dump_leaves_the_old_name_in_the_archive_and_the_restore_fails_loudly(runs):
    """OBSERVED: the archive's GRANT names the role as it was when the dump read the ACL (before the rename); the restore then fails because that name no longer exists. The candidate refused first."""
    r = runs.case("s4a_role_rename_during_dump")
    before, after = r.role_names["before_rename"], r.role_names["after_rename"]
    assert r.archive_mentions[before] == [f"GRANT SELECT ON TABLE public.teams TO {before};"] and r.archive_mentions[after] == []
    assert "restore_failure" in r.diagnostic and f'role "{before}" does not exist' in r.diagnostic["restore_failure"] and "harm" not in r.diagnostic
    assert r.outcome == "catalog_changed" and r.finalization_invoked is False


def test_observed_a_rename_before_the_dump_mixes_old_and_new_role_names_inside_one_archive(runs):
    """OBSERVED: with the rename committed after the export and before the dump's reads, the archive carries BOTH names (OWNER TO the old one, SET SESSION AUTHORIZATION the new one) and cannot be restored."""
    r = runs.case("s4b_owner_role_rename_after_export")
    before, after = r.role_names["before_rename"], r.role_names["after_rename"]
    assert any(f"OWNER TO {before}" in line for line in r.archive_mentions[before]) and r.archive_mentions[after] == [f"SET SESSION AUTHORIZATION {after};"]
    assert f'role "{before}" does not exist' in r.diagnostic["restore_failure"]



def test_s4_drop_role_a_dependency_rejection_is_distinguished_from_a_committed_drop(runs):
    r = runs.drop_role()
    assert r.rejected["committed"] is False and r.rejected["sqlstate"] == "2BP01" and r.rejected["error"] == "DependentObjectsStillExist" and r.rejected["role_exists_after"] == 1
    assert r.committed_drop["committed"] is True and r.committed_drop["sqlstate"] is None and r.committed_drop["role_exists_after"] == 0
    assert r.observation_unchanged == {"after_rejection": True, "after_committed_drop": True}, "neither changed the public-schema observation: a rejected drop is not a change, and an unreferenced role is outside the observation"


# ==========================================================================
# S5 -- change and reversal (the retained limitation)
# ==========================================================================
def test_s5_a_fresh_observer_saw_the_change_between_its_steps_but_the_endpoints_are_equal(runs):
    r = runs.case("s5_acl_change_and_reversal_during_dump")
    assert r.change["committed"] and r.change["committed_steps"] == 2 and r.change["after_each_step"] == [["acls"], []]
    assert r.detection == {"changed": False, "categories": []} and not any(r.differences.values()), "equal endpoint observations although a committed change existed in between"
    assert r.events.in_order("dump_confirmed_inside_its_snapshot_transaction_and_parked", "change_attempted", "barrier_cancelled_and_removed")


def test_s5_the_candidate_guard_permitted_the_flow_so_the_limitation_is_real_not_assumed(runs):
    r = runs.case("s5_acl_change_and_reversal_during_dump")
    assert r.outcome == "finalization_permitted" and r.finalization_invoked, "retained: equal endpoints cannot prove that no change occurred in between"
    assert len(set(r.change["transaction_starts"])) == 2, "two separate committed transactions"


@pytest.mark.xfail(strict=True, reason="REQUIREMENT NOT MET: equal endpoint observations do not prove that no catalog change occurred in between; the candidate cannot detect a change and its reversal")
def test_s5_safety_requirement_a_change_and_its_reversal_during_the_dump_is_detected_and_refused(runs):
    r = runs.case("s5_acl_change_and_reversal_during_dump")
    assert r.outcome == "catalog_changed" and r.finalization_invoked is False


# ==========================================================================
# every refusal: the normal path never ran; the forensic restore is a separately labeled diagnostic
# ==========================================================================
@pytest.mark.parametrize("name", REFUSED)
def test_a_refusal_ends_the_flow_before_restore_and_finalization_and_leaves_no_artifact(name, runs):
    r = runs.case(name)
    assert r.outcome == "catalog_changed" and r.actions[-1] == "catalog_changed_guard_refused" and r.actions[-2] == "owner_ended"
    assert r.finalization_invoked is False and r.destination_database is None and r.restore_failure is None and r.harm == {} and r.artifact_listing == []
    assert not {"destination_verified_empty", "archive_validated", "restored", "finalization_permitted", "compared", "catalog_guard_passed"} & set(r.actions)


@pytest.mark.parametrize("name", REFUSED)
def test_the_forensic_restore_after_a_refusal_is_labeled_diagnostic_and_never_a_capture(name, runs):
    r = runs.case(name)
    assert r.diagnostic["label"].startswith("DIAGNOSTIC") and r.diagnostic_actions, "it ran, under its own label"
    assert all(a.startswith("diagnostic:") for a in r.diagnostic_actions)
    assert r.outcome == "catalog_changed" and r.finalization_invoked is False and r.artifact_listing == [], "the diagnostic never turned the refusal into a success"


@pytest.mark.parametrize("name", PERMITTED)
def test_a_permitted_flow_reached_restore_and_the_finalization_stand_in_only_after_the_guard_passed(name, runs):
    r = runs.case(name)
    if r.outcome == "restore_failed":
        assert name == "s4b_owner_role_control" and "catalog_guard_passed" in r.actions and r.finalization_invoked is False
        return
    assert r.actions.index("catalog_guard_passed") < r.actions.index("restored") < r.actions.index("compared") < r.actions.index("finalization_permitted")


# ==========================================================================
# multi-query observation behavior: MEASURED
# ==========================================================================
def test_the_existing_observation_through_an_engine_uses_one_checkout_per_read_and_through_a_connection_only_one(runs):
    c = runs.semantics().checkouts
    assert c == {"observe_catalog_through_an_engine": 3, "observe_catalog_through_one_connection": 1}, "an Engine does not supply one transaction across several reads"


def test_separate_endpoint_transactions_reuse_one_pooled_backend_but_are_distinct_transactions(runs):
    s = runs.semantics().separate
    assert s["same_backend_reused"] is True and s["distinct_transactions"] is True and s["each_one_transaction"] == [True, True] and s["isolation"] == ["read committed", "read committed"]


def test_every_long_lived_observer_stayed_in_one_transaction_and_the_change_committed(runs):
    for cell in runs.semantics().matrix:
        assert cell["same_transaction"] is True and cell["committed"] is True and cell["sqlstate"] is None, cell


def test_a_fresh_observer_sees_the_category_each_change_alters(runs):
    seen = {(c["change"]): c["fresh_observer_saw"] for c in runs.semantics().matrix}
    assert seen["index"] == ["indexes"] and seen["role_rename"] == ["acls"]
    assert "constraints" in seen["column_rename"] and "indexes" in seen["column_rename"], "a column rename alters the rendered definition text of the dependent constraint and index"


def test_a_long_lived_read_committed_observer_sees_every_committed_change_on_its_next_read(runs):
    for cell in (c for c in runs.semantics().matrix if c["isolation"] == "READ COMMITTED"):
        assert cell["long_lived_saw"] == cell["fresh_observer_saw"], cell


def test_a_long_lived_repeatable_read_observer_does_not_see_a_new_index_row_on_its_next_read(runs):
    cell = next(c for c in runs.semantics().matrix if c["isolation"] == "REPEATABLE READ" and c["change"] == "index")
    assert "indexes" not in cell["long_lived_saw"], "rows read from the catalog follow the transaction's snapshot"


def test_observed_text_rendered_by_catalog_functions_is_current_even_inside_a_repeatable_read_transaction_for_the_two_renames_measured(runs):
    """OBSERVED, for exactly these two operations in this version: a REPEATABLE READ observer's second read shows a role rename and a column rename (definition and role-name TEXT), while it did not show a new index row."""
    matrix = {(c["change"], c["isolation"]): c for c in runs.semantics().matrix}
    for change in ("role_rename", "column_rename"):
        cell = matrix[(change, "REPEATABLE READ")]
        assert cell["long_lived_saw"] == cell["fresh_observer_saw"] != [], cell
    assert matrix[("index", "REPEATABLE READ")]["long_lived_saw"] == [], "rows follow the snapshot; rendered text does not: one REPEATABLE READ observer is a mixture, not a single point in time"


@pytest.mark.xfail(strict=True, reason="REQUIREMENT NOT MET: the candidate's observation does not complete while an exclusive request is queued; its index read queues behind it and the bounded observation fails (it fails closed, but the capture cannot proceed)")
def test_requirement_the_candidates_observation_completes_while_an_exclusive_request_is_queued(runs):
    assert runs.lock_behavior().full_observation["outcome"] == "returned_an_observation"


# ==========================================================================
# the candidate's own observation versus a queued exclusive request: MEASURED
# ==========================================================================
def test_the_constraint_and_acl_reads_do_not_wait_for_a_queued_exclusive_request(runs):
    lb = runs.lock_behavior()
    assert lb.barrier["waiting_modes"] == ["AccessExclusiveLock"] and lb.barrier["blocked_by"] == [lb.barrier["owner_pid"]]
    for category in ("constraints", "acls"):
        read = lb.reads[category]
        assert read["outcome"] == "rows" and read["rows"] > 0 and read["waiting"] == [] and read["elapsed_s"] < 3, (category, read)


def test_the_existing_index_read_queues_behind_a_queued_exclusive_request_like_any_ordinary_reader(runs):
    lb = runs.lock_behavior()
    read = lb.reads["indexes"]
    assert read["outcome"] == "error" and read["sqlstate"] == "55P03", "it waited for its bounded lock timeout and was refused by it"
    assert read["waiting"] == [(BARRIER_TABLE, "AccessShareLock")] and read["blocked_by"] == [lb.barrier["pid"]], "it waits for ACCESS SHARE on the barrier table behind the QUEUED request, not behind the owner"
    assert 3.5 <= read["elapsed_s"] <= 12, "bounded by the test's lock_timeout"


def test_a_blocked_observation_fails_closed_it_raises_and_never_returns_an_observation_for_the_guard(runs):
    full = runs.lock_behavior().full_observation
    assert full["outcome"] == "error" and full["sqlstate"] == "55P03" and 8 <= full["elapsed_s"] <= 20, "no CatalogObservation exists for require_catalog_unchanged to compare"


def test_the_observation_recovers_once_the_queued_request_is_removed_and_the_owner_is_untouched(runs):
    lb = runs.lock_behavior()
    assert lb.recovered["indexes"]["outcome"] == "rows" and lb.recovered["full_observation_returned"] is True
    assert lb.recovered["barrier_sqlstate"] == "57014" and lb.recovered["barrier_committed"] is False


# ==========================================================================
# cleanup, credentials, recorded evidence
# ==========================================================================
def _clean(v: dict) -> bool:
    return (v["problems"] == [] and v["databases_remaining"] == [] and v["sessions_in_dropped_databases"] == 0 and v["leftover_dump_or_owner_sessions"] == 0
            and v["container_files_remaining"] == [] and v["threads_alive"] == [] and v["directories_remaining"] == [])


@pytest.mark.parametrize("name", list(CANDIDATE_SCENARIOS))
def test_cleanup_was_verified_for_each_scenario_including_its_disposable_roles(name, runs):
    r = runs.case(name)
    assert _clean(r.verification), r.verification
    assert r.roles_verification["problems"] == [] and r.roles_verification["roles_remaining"] == []


@pytest.mark.parametrize("which", ["semantics", "drop_role", "lock_behavior"])
def test_cleanup_was_verified_for_each_probe(which, runs):
    r = {"semantics": runs.semantics, "drop_role": runs.drop_role, "lock_behavior": runs.lock_behavior}[which]()
    assert _clean(r.verification) and r.roles_verification["problems"] == [] and r.roles_verification["roles_remaining"] == []
    assert r.roles_verification["roles_registered"] >= (0 if which == "lock_behavior" else 2)


def test_no_case_database_or_probe_role_outlives_the_scenarios(harness, runs):
    runs.case("s1_control")
    admin = harness.cluster.admin_engine()
    try:
        databases = rows(admin, "SELECT datname FROM pg_database WHERE datname LIKE 'napsedge_pg_test_%' AND datname NOT LIKE 'napsedge_pg_test_admin_%' AND datname NOT LIKE 'napsedge_pg_test_boot_%'")
        leftover_roles = rows(admin, "SELECT rolname FROM pg_roles WHERE rolname LIKE :p", p=ROLE_PREFIX + "%")
        barrier_sessions = rows(admin, "SELECT pid FROM pg_stat_activity WHERE application_name = :a", a=BARRIER_APP)
    finally:
        admin.dispose()
    assert databases == [] and leftover_roles == [] and barrier_sessions == []


@pytest.mark.parametrize("name", list(CANDIDATE_SCENARIOS))
def test_no_credential_appears_in_any_recorded_command_or_output(name, runs, harness):
    r = runs.case(name)
    secrets_ = harness.cluster.secrets_to_scrub()
    commands = [*r.commands, *r.diagnostic.get("commands", [])]
    assert commands
    for command in commands:
        blob = " ".join(command.argv) + command.stdout + command.stderr
        assert not any(secret and secret in blob for secret in secrets_) and "PGPASSWORD" not in blob and "password=" not in blob.lower()


@pytest.mark.parametrize("name", list(CANDIDATE_SCENARIOS))
def test_observed_candidate_facts_are_recorded_in_the_junit_evidence(name, request, runs):
    """Operation order, backend PIDs, transaction boundaries, commit status, fresh visibility, digests and raw differences, pg_dump outcome, owner lifetime, outcome, diagnostics and cleanup."""
    r = runs.case(name)
    s = CANDIDATE_SCENARIOS[name]
    props = request.node.user_properties
    props += [("scenario", f"{r.scenario} ({r.required})"), ("window", s.window), ("role_setup", str(s.role_setup)), ("change", str({k: v for k, v in (r.change or {}).items() if k != "after_each_step"}) if r.change else "None"),
              ("change_after_each_step", str((r.change or {}).get("after_each_step"))), ("fresh_visible_categories", ",".join(r.fresh_visible_categories) or "none"),
              ("outcome", r.outcome), ("finalization_invoked", str(r.finalization_invoked)), ("pids", str(r.pids)), ("dump_pid_from_server_log", str(r.dump_pid_from_log)),
              ("events", ",".join(f"{e['seq']}:{e['event']}" for e in r.events.items)), ("diagnostic_actions", ",".join(r.diagnostic_actions) or "none"),
              ("dump", f"returncode={r.dump.returncode} owner_state_at_dump_exit={r.owner_state_at_dump_exit} descriptor_unchanged={r.descriptor_at_export == r.descriptor_after_dump}"),
              ("owner", f"pid={r.pids['owner']} connects_after_baseline={r.owner_connects_after_baseline} tables_locked={len(r.owner_locked_tables_after_baseline)}"),
              ("harm", str(r.harm) if r.harm else "not reached"), ("restore_failure", r.restore_failure or "none"), ("barrier", str(r.barrier) if r.barrier else "none"),
              ("dump_parked", str(r.dump_parked) if r.dump_parked else "none"), ("archive_mentions", str(r.archive_mentions)),
              ("diagnostic", str({k: v for k, v in r.diagnostic.items() if k != "commands"}) if r.diagnostic else "none"),
              ("role_names", "recorded" if r.role_names else "none"), ("cleanup_problems", str(len(r.verification["problems"]) + len(r.roles_verification["problems"])))]
    props.append(("visibility_method", r.visibility_method or "none"))
    for endpoint in (r.first, r.second, r.pre_change, r.post_change):
        if isinstance(endpoint, Endpoint):
            props.append((f"endpoint:{endpoint.label}", f"pid={endpoint.pid} isolation={endpoint.isolation} one_transaction={endpoint.one_transaction} txn_start={endpoint.txn_start_first}"))
        elif isinstance(endpoint, dict):
            props.append((f"probe_pid:{'pre' if endpoint is r.pre_change else 'post'}_change", str(endpoint["pid"])))
    props.append(("detect:candidate", f"changed={r.detection['changed']} categories={','.join(r.detection['categories']) or 'none'}"))
    for label, observation in (("first", r.first.observation), ("second", r.second.observation), ("owner_baseline", r.baseline_catalog), ("owner_after_in_transaction", r.owner_after_in_transaction)):
        props.append((f"digest:{label}", " ".join(f"{c}={observation.digests[c]}" for c in ("constraints", "indexes", "acls", "combined"))))
    for category, diff in r.differences.items():
        props.append((f"diff:candidate:{category}", (str(diff)[:700] if diff else "none")))
    assert not r.verification["problems"] and not r.roles_verification["problems"]


def test_observed_probe_facts_are_recorded_in_the_junit_evidence(request, runs):
    s, d, lb = runs.semantics(), runs.drop_role(), runs.lock_behavior()
    props = request.node.user_properties
    props += [("lock_behavior_barrier", str(lb.barrier)), ("lock_behavior_full_observation", str(lb.full_observation)), ("lock_behavior_recovered", str(lb.recovered)),
              ("lock_behavior_actions", ",".join(lb.actions)), ("separate_transactions", str(s.separate)), ("checkouts", str(s.checkouts)), ("drop_rejected", str(d.rejected)), ("drop_committed", str(d.committed_drop)),
              ("drop_observation_unchanged", str(d.observation_unchanged)), ("probe_actions", ",".join(s.actions + d.actions)),
              ("cleanup_problems", str(len(s.verification["problems"]) + len(d.verification["problems"]) + len(s.roles_verification["problems"]) + len(d.roles_verification["problems"])))]
    for cell in s.matrix:
        props.append((f"semantics:{cell['change']}:{cell['isolation']}", str(cell)))
    for category, read in lb.reads.items():
        props.append((f"lock_behavior_read:{category}", str(read)))
    assert not (s.verification["problems"] or d.verification["problems"] or lb.verification["problems"] or s.roles_verification["problems"] or d.roles_verification["problems"] or lb.roles_verification["problems"])
