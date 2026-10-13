"""
Phase 9B-6C: the snapshot-owner session (`src/deployment/backup_capture_snapshot.py`) inside the dedicated lock
session, against one disposable, synthetic PostgreSQL 16 cluster (the existing 9B harness: verified TLS, SCRAM, the
three bootstrap roles, the unchanged 9B-2 backup-role grants; each scenario on its own seeded database).

Claims about sessions, transactions, locks and commits are read from the SERVER (the administrator's
`pg_stat_activity`, `pg_locks`, fresh committed reads), never from engine disposal or thread completion. The
"consumer" that imports S's exported snapshot is TEST INSTRUMENTATION (it stands where a later `pg_dump --snapshot`
would), not a production dump. Where a scenario reads through S itself, it does so through the module's documented
test seam and only with SELECTs. Every test records `record_property("evidence", ...)` for structural export.
"""
from __future__ import annotations

import re
import threading
import time
import uuid

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from src.deployment import backup_capture_lock as lock_module
from src.deployment.backup_capture_lock import LOCK_APPLICATION_NAME, VERIFIER_APPLICATION_NAME, BackupLockNotHeld, backup_capture_lock
from src.deployment.backup_capture_snapshot import (
    SNAPSHOT_APPLICATION_NAME,
    SnapshotHandleClosed,
    SnapshotRefusedError,
    SnapshotSessionLost,
    backup_capture_snapshot,
)
from src.exceptions import DatabaseTargetVerificationError
from src.pipeline.bulk_import_safety import compute_advisory_lock_key
from tests.postgres.baseline_document import baseline_to_document
from tests.postgres.conftest import TEST_DB_PREFIX
from tests.postgres.pg_restore_compare import capture_baseline
from tests.postgres.test_postgres_backup_capture_lock import Admin, _verified, designation_for, harness, record  # noqa: F401  (harness: module fixture)
from tests.postgres.test_postgres_backup_overlap_quiescence import terminate_and_confirm
from tests.postgres.test_postgres_backup_sequence_consistency import read_sequence_details
from tests.postgres.test_postgres_backup_snapshot_consistency import CHANGED_TEAM_ID, CHANGED_TEAM_SLUG, owned_case
from tests.postgres.test_postgres_roles_and_restore import ACCEPTED_ALEMBIC_HEAD, MIGRATOR_ROLE, _create_database_owned_by_migrator
from tests.postgres.test_postgres_tls_backup_restore import (
    BACKUP_ROLE,
    isolated_database_environment,  # noqa: F401  (autouse module fixture)
    provision_database,
)

pytestmark = pytest.mark.postgres

EXPORTED_ID = re.compile(r"[0-9A-F]{8}-[0-9A-F]{8}-[0-9]+")
INVALID_SNAPSHOT = "22023"
LOCK_NOT_AVAILABLE = "55P03"
QUEUE_WAIT_S = 30
OWNED_APPS = (LOCK_APPLICATION_NAME, VERIFIER_APPLICATION_NAME, SNAPSHOT_APPLICATION_NAME)


def new_database(cluster, ledger) -> str:
    database = _create_database_owned_by_migrator(cluster)
    ledger.databases.append(database)
    provision_database(cluster, database, seed=True)
    return database


@pytest.fixture()
def case(harness):
    cluster = harness.cluster
    with owned_case(cluster) as ledger:
        database = new_database(cluster, ledger)
        admin = Admin(cluster, database)
        ledger.engines.append(admin.engine)
        key = compute_advisory_lock_key(_verified(cluster, database))
        yield cluster, database, admin, key, ledger
        for app in OWNED_APPS:
            assert admin.sessions(app) == [], app
        assert admin.holders(key) == []


class Seam:
    """Builds the modules' OWN engines (`_build_engine`) and records PIDs, Connections, BEGINs and statements, plus one optional statement hook."""

    def __init__(self, on_statement=None):
        self.pids: dict = {}
        self.connections: dict = {}
        self.begins: dict = {}
        self.statement_connections: dict = {}
        self.on_statement = on_statement

    def factory(self, url, application_name):
        engine = lock_module._build_engine(url, application_name)
        self.pids.setdefault(application_name, [])
        self.begins.setdefault(application_name, 0)
        self.statement_connections.setdefault(application_name, set())
        event.listen(engine, "connect", lambda dbapi, rec: self.pids[application_name].append(dbapi.info.backend_pid))
        event.listen(engine, "engine_connect", lambda conn: self.connections.setdefault(application_name, conn))
        event.listen(engine, "begin", lambda conn: self.begins.__setitem__(application_name, self.begins[application_name] + 1))

        def before(conn, cursor, statement, parameters, context, executemany):
            self.statement_connections[application_name].add((id(conn), id(conn.connection.dbapi_connection)))
            if self.on_statement is not None:
                self.on_statement(application_name, statement)

        event.listen(engine, "before_cursor_execute", before)
        return engine


class Consumer:
    """TEST INSTRUMENTATION: a distinct backup-role session that imports an exported snapshot into its own REPEATABLE READ, READ ONLY transaction."""

    def __init__(self, cluster, database):
        self.engine = create_engine(cluster.backup_url(database), poolclass=NullPool, connect_args={"application_name": "napsedge_snapshot_consumer"})
        self.connection = self.engine.connect()
        self.connection.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
        self.connection.begin()

    def import_snapshot(self, snapshot_id: str) -> None:
        assert EXPORTED_ID.fullmatch(snapshot_id), snapshot_id
        self.connection.execute(text(f"SET TRANSACTION SNAPSHOT '{snapshot_id}'"))

    def pid(self) -> int:
        return self.connection.execute(text("SELECT pg_backend_pid()")).scalar_one()

    def end(self) -> None:
        try:
            self.connection.rollback()
            self.connection.close()
        finally:
            self.engine.dispose()


def run_capture(cluster, database, body, *, lock_seam=None, snap_seam=None, snap_database=None, snap_name=None):
    url = cluster.backup_url(database)
    with backup_capture_lock(url, designation_for(database), _engine_factory=lock_seam.factory if lock_seam else None) as lock:
        target = snap_database or database
        with backup_capture_snapshot(lock, cluster.backup_url(target), designation_for(snap_name or target), expected_alembic_head=ACCEPTED_ALEMBIC_HEAD,
                                     artifact_id=str(uuid.uuid4()), _engine_factory=snap_seam.factory if snap_seam else None) as handle:
            body(lock, handle)
    return lock, handle


def admin_scalar(admin: Admin, sql: str, **params):
    with admin.engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def admin_row(admin: Admin, sql: str, **params):
    with admin.engine.connect() as conn:
        return conn.execute(text(sql), params).first()


# ---------------------------------------------------------------------------
# A, B, C, F: identities, transaction settings, snapshot import and equivalence, one Connection and one transaction
# ---------------------------------------------------------------------------
def test_a_b_c_f_distinct_sessions_matching_identity_server_transaction_settings_import_and_baseline_equivalence(case, record_property):
    cluster, database, admin, key, _ = case
    snap_seam = Seam()
    seen: dict = {}
    admin_sysid = admin_scalar(admin, "SELECT system_identifier::text FROM pg_control_system()")
    admin_oid = admin_scalar(admin, "SELECT oid FROM pg_database WHERE datname = current_database()")

    def body(lock, handle):
        facts = handle.facts
        seen["facts"], seen["lock_pid"], seen["lock_identity"] = facts, lock.backend_pid, (lock.system_identifier, lock.database_oid)
        seen["snapshot_id"], seen["descriptor"], seen["baseline"] = handle.snapshot_id, handle.visibility_descriptor, handle.baseline
        seen["s_server"] = tuple(admin_row(admin, "SELECT usename, application_name, state, xact_start, backend_xmin::text FROM pg_stat_activity WHERE pid = :p",
                                           p=facts.backend_pid))
        seen["holders"] = admin.holders(key)
        consumer = Consumer(cluster, database)
        seen["import_error"] = None
        try:
            consumer.import_snapshot(handle.snapshot_id)  # must be the transaction's first query
            seen["consumer_pid"] = consumer.pid()
            seen["consumer_baseline"] = capture_baseline(consumer.connection)
            seen["consumer_sequences"] = read_sequence_details(consumer.connection)
        except DBAPIError as exc:  # recorded, then asserted: an unimportable snapshot is a behavioral failure, not an error
            seen["import_error"] = getattr(exc.orig, "sqlstate", None) or type(exc).__name__
        finally:
            consumer.end()

    escaped = None
    lock = handle = None
    try:
        lock, handle = run_capture(cluster, database, body, snap_seam=snap_seam)
    except Exception as exc:  # recorded, then asserted AFTER the server-side evidence
        escaped = exc
    owned = snap_seam.pids.get(SNAPSHOT_APPLICATION_NAME, []) + ([seen["lock_pid"]] if "lock_pid" in seen else [])
    gone = admin.gone(owned)
    assert gone, "an owned snapshot or lock backend is still present on the server"
    assert seen.get("import_error") is None, f"the exported snapshot could not be imported while S was open: {seen.get('import_error')}"
    facts, baseline = seen["facts"], seen["baseline"]
    expected = baseline_to_document(seen["consumer_baseline"], artifact_id=baseline.artifact_id, captured_utc=baseline.captured_utc,
                                    sequence_details=seen["consumer_sequences"])
    xmin = seen["descriptor"].split(":")[0]
    record(record_property, "A_B_C_F_identity_transaction_import", acquired=True, body_entered=True,
           lock_pid=seen["lock_pid"], snapshot_pid=facts.backend_pid, consumer_pid=seen["consumer_pid"],
           lock_identity=seen["lock_identity"], snapshot_identity=(facts.system_identifier, facts.database_oid, facts.database_name),
           admin_identity=(admin_sysid, admin_oid), isolation=facts.isolation, read_only=facts.read_only, transaction_start=facts.transaction_start,
           snapshot_server=seen["s_server"], descriptor=seen["descriptor"], snapshot_id=seen["snapshot_id"], holders_during=seen["holders"],
           baseline_equals_imported_consumer=expected == baseline, tables=len(baseline.fingerprints), sequences=len(baseline.sequence_details),
           snapshot_physical_connects=len(snap_seam.pids[SNAPSHOT_APPLICATION_NAME]), snapshot_begins=snap_seam.begins[SNAPSHOT_APPLICATION_NAME],
           snapshot_statement_connections=len(snap_seam.statement_connections[SNAPSHOT_APPLICATION_NAME]),
           snapshot_cleanup=handle and handle.cleanup, lock_cleanup=lock and lock.cleanup, sessions_gone_after=gone, escaped=type(escaped).__name__ if escaped else None)
    # A: distinct physical sessions, matching identity read on each session itself, equal to the administrator's
    assert seen["lock_pid"] != facts.backend_pid != seen["consumer_pid"] and facts.lock_backend_pid == seen["lock_pid"]
    assert seen["lock_identity"] == (facts.system_identifier, facts.database_oid) == (admin_sysid, admin_oid) and facts.database_name == database
    assert seen["holders"] == [(seen["lock_pid"], True)]
    # B: the server reports S's transaction as REPEATABLE READ, READ ONLY; the administrator sees S idle in that transaction holding the exported xmin
    assert (facts.isolation, facts.read_only) == ("repeatable read", "on")
    assert seen["s_server"][:3] == (BACKUP_ROLE, SNAPSHOT_APPLICATION_NAME, "idle in transaction")
    assert seen["s_server"][3] == facts.transaction_start and seen["s_server"][4] == xmin
    # C: an independent consumer imported the snapshot while S was open and reproduced the established baseline exactly
    assert EXPORTED_ID.fullmatch(seen["snapshot_id"]) and expected == baseline
    assert len(baseline.fingerprints) == 24 and len(baseline.sequence_details) == 13 and baseline.version == ACCEPTED_ALEMBIC_HEAD
    # F: one physical connection, one BEGIN, every statement on one Connection/DBAPI connection, one transaction start throughout
    assert len(snap_seam.pids[SNAPSHOT_APPLICATION_NAME]) == 1 and snap_seam.begins[SNAPSHOT_APPLICATION_NAME] == 1
    assert len(snap_seam.statement_connections[SNAPSHOT_APPLICATION_NAME]) == 1
    assert escaped is None, type(escaped).__name__
    assert handle.cleanup.confirmed and lock.cleanup.confirmed


# ---------------------------------------------------------------------------
# D, E: a committed write is visible to a fresh reader but not to S or its consumer; the old snapshot dies with S
# ---------------------------------------------------------------------------
def test_d_e_a_committed_write_is_absent_from_s_and_its_consumer_and_the_snapshot_cannot_be_imported_after_exit(case, record_property):
    cluster, database, admin, key, ledger = case
    snap_seam = Seam()
    seen: dict = {}
    writer = create_engine(cluster.migrator_url(database))
    ledger.engines.append(writer)
    teams_before = admin_scalar(admin, "SELECT count(*) FROM teams")

    def body(lock, handle):
        seen["snapshot_id"] = handle.snapshot_id
        seen["baseline_teams"] = {f.table: f.rows for f in handle.baseline.fingerprints}["teams"]
        with writer.begin() as conn:  # the commit happens when this block exits
            inserted = conn.execute(text("INSERT INTO teams (team_id, display_name, sr_slug) VALUES (:i, 'Snapshot Change Team', :s)"),
                                    {"i": CHANGED_TEAM_ID, "s": CHANGED_TEAM_SLUG}).rowcount
        seen["inserted"] = inserted
        seen["fresh_reader_rows"] = admin_scalar(admin, "SELECT count(*) FROM teams WHERE team_id = :i", i=CHANGED_TEAM_ID)  # independent proof of commit
        seen["fresh_reader_teams"] = admin_scalar(admin, "SELECT count(*) FROM teams")
        snapshot_connection = snap_seam.connections[SNAPSHOT_APPLICATION_NAME]  # TEST seam: a SELECT inside S's own transaction
        seen["s_rows"] = snapshot_connection.execute(text("SELECT count(*) FROM teams WHERE team_id = :i"), {"i": CHANGED_TEAM_ID}).scalar_one()
        consumer = Consumer(cluster, database)
        try:
            consumer.import_snapshot(handle.snapshot_id)
            seen["consumer_rows"] = consumer.connection.execute(text("SELECT count(*) FROM teams WHERE team_id = :i"), {"i": CHANGED_TEAM_ID}).scalar_one()
            seen["consumer_teams"] = consumer.connection.execute(text("SELECT count(*) FROM teams")).scalar_one()
        finally:
            consumer.end()

    escaped = None
    lock = handle = None
    try:
        lock, handle = run_capture(cluster, database, body, snap_seam=snap_seam)
    except Exception as exc:  # recorded; the visibility assertions below run first
        escaped = exc
    late = Consumer(cluster, database)
    try:
        with pytest.raises(DBAPIError) as dead:
            late.import_snapshot(seen["snapshot_id"])
        late_sqlstate = getattr(dead.value.orig, "sqlstate", None)
    finally:
        late.end()
    record(record_property, "D_E_commit_visibility_and_dead_snapshot", acquired=True, body_entered=True, teams_before=teams_before,
           baseline_teams=seen["baseline_teams"], inserted=seen["inserted"], fresh_reader_rows=seen["fresh_reader_rows"],
           fresh_reader_teams=seen["fresh_reader_teams"], s_rows=seen["s_rows"], consumer_rows=seen["consumer_rows"], consumer_teams=seen["consumer_teams"],
           late_import_sqlstate=late_sqlstate, snapshot_cleanup=handle and handle.cleanup, escaped=type(escaped).__name__ if escaped else None)
    assert seen["inserted"] == 1 and seen["fresh_reader_rows"] == 1 and seen["fresh_reader_teams"] == teams_before + 1  # committed, seen fresh
    assert seen["s_rows"] == 0 and seen["consumer_rows"] == 0 and seen["consumer_teams"] == teams_before == seen["baseline_teams"]
    assert late_sqlstate == INVALID_SNAPSHOT  # S has ended: its snapshot can no longer be imported
    assert escaped is None, type(escaped).__name__
    with pytest.raises(SnapshotHandleClosed):
        handle.snapshot_id


# ---------------------------------------------------------------------------
# G: unsupported source, wrong target, identity mismatch never yield a usable handle; L is unaffected
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("variant", ["unsupported_public_grant", "wrong_target", "identity_mismatch"])
def test_g_refusals_never_yield_and_leave_no_snapshot_session(case, record_property, variant):
    cluster, database, admin, key, ledger = case
    snap_seam = Seam()
    kw: dict = {}
    if variant == "unsupported_public_grant":
        engine = create_engine(cluster.migrator_url(database))
        ledger.engines.append(engine)
        with engine.begin() as conn:
            conn.execute(text("GRANT SELECT ON public.teams TO PUBLIC"))  # disposable database only
        expected = SnapshotRefusedError
    elif variant == "wrong_target":
        kw["snap_name"] = f"{TEST_DB_PREFIX}not_this_one"
        expected = DatabaseTargetVerificationError
    else:
        kw["snap_database"] = new_database(cluster, ledger)  # same cluster, a different database
        expected = SnapshotRefusedError
    entered, during = [], {}
    with pytest.raises(BaseException) as info:
        with backup_capture_lock(cluster.backup_url(database), designation_for(database)) as lock:
            try:
                target = kw.get("snap_database", database)
                with backup_capture_snapshot(lock, cluster.backup_url(target), designation_for(kw.get("snap_name", target)),
                                             expected_alembic_head=ACCEPTED_ALEMBIC_HEAD, artifact_id=str(uuid.uuid4()), _engine_factory=snap_seam.factory) as handle:
                    entered.append(handle)
            finally:
                during["holders_after_refusal"] = admin.holders(key)
                during["lock_pid"] = lock.backend_pid
                during["snapshot_sessions"] = [p for p in snap_seam.pids.get(SNAPSHOT_APPLICATION_NAME, []) if not admin.gone([p])]
    assert type(info.value) is expected, type(info.value).__name__
    record(record_property, f"G_{variant}", acquired=True, body_entered=bool(entered), exception=type(info.value).__name__, message=str(info.value),
           notes=getattr(info.value, "__notes__", []), snapshot_pids=snap_seam.pids.get(SNAPSHOT_APPLICATION_NAME), **during)
    assert entered == [] and during["snapshot_sessions"] == []
    assert during["holders_after_refusal"] == [(during["lock_pid"], True)]  # L still held the lock after S was refused and cleaned
    if variant == "identity_mismatch":
        assert str(info.value) == "the snapshot session and the lock session address different sources (database_oid)"
    if variant == "unsupported_public_grant":
        assert info.value.preflight is not None and not info.value.preflight.accepted


# ---------------------------------------------------------------------------
# H: baseline failure, consumer-body failure, S backend loss, L backend loss
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("variant", ["baseline_failure", "body_failure", "s_backend_loss", "l_backend_loss"])
def test_h_failures_preserve_the_right_exception_and_clean_up_both_owned_sessions(case, record_property, variant):
    cluster, database, admin, key, ledger = case
    snap_seam, lock_seam = Seam(), Seam()
    primary = RuntimeError("synthetic consumer failure")
    seen: dict = {}
    if variant == "baseline_failure":
        engine = create_engine(cluster.migrator_url(database))
        ledger.engines.append(engine)
        with engine.begin() as conn:  # owned by the migrator, no grant: the preflight accepts it, the fingerprint read is denied
            conn.execute(text("CREATE TABLE public.zz_unreadable (id integer)"))

    def body(lock, handle):
        seen["entered"] = True
        if variant == "body_failure":
            raise primary
        if variant == "s_backend_loss":
            terminate_and_confirm(admin.engine, handle.facts.backend_pid)
        if variant == "l_backend_loss":
            terminate_and_confirm(admin.engine, lock.backend_pid)

    with pytest.raises(BaseException) as info:
        run_capture(cluster, database, body, lock_seam=lock_seam, snap_seam=snap_seam)
    expected = {"baseline_failure": DBAPIError, "body_failure": RuntimeError, "s_backend_loss": SnapshotSessionLost, "l_backend_loss": BackupLockNotHeld}[variant]
    assert isinstance(info.value, expected), type(info.value).__name__
    pids = snap_seam.pids[SNAPSHOT_APPLICATION_NAME] + lock_seam.pids[LOCK_APPLICATION_NAME]
    gone, holders = admin.gone(pids), admin.holders(key)
    record(record_property, f"H_{variant}", acquired=True, body_entered=bool(seen.get("entered")), exception=type(info.value).__name__,
           sqlstate=getattr(getattr(info.value, "orig", None), "sqlstate", None), message=str(info.value)[:160], notes=getattr(info.value, "__notes__", []),
           snapshot_pids=snap_seam.pids[SNAPSHOT_APPLICATION_NAME], lock_pids=lock_seam.pids[LOCK_APPLICATION_NAME], sessions_gone_after=gone, holders_after=holders)
    if variant == "body_failure":
        assert info.value is primary
    if variant == "baseline_failure":
        assert info.value.orig.sqlstate == "42501" and not seen
    if variant == "l_backend_loss":
        assert any("advisory lock release also failed" in note for note in info.value.__notes__)
    assert len(snap_seam.pids[SNAPSHOT_APPLICATION_NAME]) == 1 and len(lock_seam.pids[LOCK_APPLICATION_NAME]) == 1  # never reconnected
    assert gone and holders == []


# ---------------------------------------------------------------------------
# I: queued DDL on alembic_version gives a bounded refusal
# ---------------------------------------------------------------------------
def test_i_a_queued_exclusive_request_on_alembic_version_is_a_bounded_refusal(case, record_property):
    cluster, database, admin, key, ledger = case
    holder_engine, requester_engine = create_engine(cluster.migrator_url(database)), create_engine(cluster.migrator_url(database))
    ledger.engines += [holder_engine, requester_engine]
    holder = holder_engine.connect()
    ledger.connections.append(holder)
    holder.begin()
    holder.execute(text("LOCK TABLE public.alembic_version IN ACCESS SHARE MODE"))
    outcome: dict = {}

    def request_exclusive():
        with requester_engine.connect() as conn:
            outcome["pid"] = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
            conn.rollback()
            conn.begin()
            conn.execute(text(f"SET LOCAL lock_timeout = '{QUEUE_WAIT_S}s'"))
            try:
                conn.execute(text("ALTER TABLE public.alembic_version ADD COLUMN zz_probe integer"))
                outcome["result"] = "acquired"
            except DBAPIError as exc:
                outcome["result"] = getattr(exc.orig, "sqlstate", None)
            conn.rollback()

    thread = threading.Thread(target=request_exclusive, name="alembic-version-exclusive")
    ledger.threads.append(thread)
    thread.start()
    queued_sql = "SELECT count(*) FROM pg_locks WHERE relation = 'public.alembic_version'::regclass AND mode = 'AccessExclusiveLock' AND NOT granted"
    deadline = time.monotonic() + QUEUE_WAIT_S
    while time.monotonic() < deadline and not admin_scalar(admin, queued_sql):
        time.sleep(0.05)
    queued = bool(admin_scalar(admin, queued_sql))
    snap_seam = Seam()
    entered = []
    started = time.monotonic()
    try:
        with pytest.raises(BaseException) as info:
            run_capture(cluster, database, lambda lock, handle: entered.append(True), snap_seam=snap_seam)
        elapsed = time.monotonic() - started
        still_queued = bool(admin_scalar(admin, queued_sql))
    finally:
        if "pid" in outcome:
            admin_scalar(admin, "SELECT pg_cancel_backend(:p)", p=outcome["pid"])
        thread.join(timeout=QUEUE_WAIT_S + 5)
        holder.rollback()
    column_added = admin_scalar(admin, "SELECT count(*) FROM pg_attribute WHERE attrelid = 'public.alembic_version'::regclass AND attname = 'zz_probe'")
    gone = admin.gone(snap_seam.pids[SNAPSHOT_APPLICATION_NAME])
    sqlstate = getattr(getattr(info.value, "orig", None), "sqlstate", None)
    record(record_property, "I_queued_alembic_version", acquired=True, body_entered=bool(entered), queued=queued, still_queued=still_queued,
           exception=type(info.value).__name__, sqlstate=sqlstate, elapsed_s=round(elapsed, 2), requester=outcome.get("result"),
           column_added=column_added, snapshot_gone=gone)
    assert queued and still_queued and entered == []
    assert isinstance(info.value, DBAPIError) and sqlstate == LOCK_NOT_AVAILABLE
    assert elapsed < 15.0  # S's own 5 s lock_timeout plus setup and cleanup verification; never the requester's 30 s
    assert outcome.get("result") == "57014" and column_added == 0 and gone


# ---------------------------------------------------------------------------
# J: S ends before L is released, on normal and failure exits
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("fails", [False, True], ids=["normal", "failure"])
def test_j_s_has_ended_on_the_server_before_l_issues_its_unlock(case, record_property, fails):
    cluster, database, admin, key, _ = case
    observed: dict = {}
    snap_seam = Seam()

    def at_lock_statement(application_name, statement):
        if application_name == LOCK_APPLICATION_NAME and "pg_advisory_unlock" in statement:
            s_pids = snap_seam.pids.get(SNAPSHOT_APPLICATION_NAME, [])
            observed["snapshot_alive_at_unlock"] = [p for p in s_pids if admin_scalar(admin, "SELECT count(*) FROM pg_stat_activity WHERE pid = :p", p=p)]
            observed["holders_at_unlock"] = admin.holders(key)

    lock_seam = Seam(on_statement=at_lock_statement)
    primary = RuntimeError("synthetic failure")

    def body(lock, handle):
        if fails:
            raise primary

    if fails:
        with pytest.raises(BaseException) as info:
            run_capture(cluster, database, body, lock_seam=lock_seam, snap_seam=snap_seam)
        assert info.value is primary
    else:
        run_capture(cluster, database, body, lock_seam=lock_seam, snap_seam=snap_seam)
    pids = snap_seam.pids[SNAPSHOT_APPLICATION_NAME] + lock_seam.pids[LOCK_APPLICATION_NAME]
    gone, holders = admin.gone(pids), admin.holders(key)
    record(record_property, f"J_ordering_{'failure' if fails else 'normal'}", acquired=True, body_entered=True, snapshot_pids=snap_seam.pids[SNAPSHOT_APPLICATION_NAME],
           lock_pids=lock_seam.pids[LOCK_APPLICATION_NAME], **observed, sessions_gone_after=gone, holders_after=holders)
    assert observed["snapshot_alive_at_unlock"] == []  # S already absent on the server when L sends its unlock
    assert observed["holders_at_unlock"] == [(lock_seam.pids[LOCK_APPLICATION_NAME][0], True)]
    assert gone and holders == []


# ---------------------------------------------------------------------------
# K: a separate caller-owned connection and its transaction are untouched
# ---------------------------------------------------------------------------
def test_k_a_callers_own_connection_and_transaction_are_untouched(case, record_property):
    cluster, database, admin, key, ledger = case
    caller_engine = create_engine(cluster.backup_url(database))
    ledger.engines.append(caller_engine)
    events: list = []
    for kind in ("begin", "commit", "rollback"):
        event.listen(caller_engine, kind, lambda conn, kind=kind: events.append(kind))
    caller = caller_engine.connect()
    ledger.connections.append(caller)
    caller.begin()
    events.clear()
    caller.execute(text("SET LOCAL lock_timeout = '3s'"))
    caller_pid, before = caller.execute(text("SELECT pg_backend_pid(), now()")).one()
    lock, handle = run_capture(cluster, database, lambda lock, handle: None)
    after = caller.execute(text("SELECT now()")).scalar_one()
    setting = caller.execute(text("SELECT current_setting('lock_timeout')")).scalar_one()
    state = admin.session_row(caller_pid)
    record(record_property, "K_caller_untouched", acquired=True, body_entered=True, caller_pid=caller_pid, same_transaction=before == after,
           caller_setting=setting, caller_state=state, caller_events=events, snapshot_cleanup=handle.cleanup, lock_cleanup=lock.cleanup)
    assert before == after and setting == "3s" and caller.in_transaction() and events == []
    assert state[0] == BACKUP_ROLE and state[2] == "idle in transaction"
    assert handle.cleanup.confirmed and lock.cleanup.confirmed
    caller.rollback()
