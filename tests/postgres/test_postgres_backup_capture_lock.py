"""
Phase 9B-6B: the dedicated backup lock session (`src/deployment/backup_capture_lock.py`) against one disposable,
synthetic PostgreSQL 16 cluster (the existing 9B harness: verified TLS, SCRAM, the three bootstrap roles, the
unchanged 9B-2 backup-role grants; each scenario on its own database, so its lock key is its own).

Every claim about lock ownership or session absence is read from the SERVER (`pg_locks`, `pg_stat_activity`, by the
cluster administrator), never inferred from engine disposal, thread completion or a closed Python object. Where a
scenario needs to act on the lock session itself (an aborted transaction, an early unlock), it does so through the
module's documented test seam (`_engine_factory`), which wraps the module's own `_build_engine`: that is TEST
orchestration and is labeled as such in the evidence. Only disposable test-owned backends are ever terminated, by the
test's administrator, never by the module. Each test records its observations with `record_property("evidence", ...)`
so they can be exported structurally from JUnit.
"""
from __future__ import annotations

import json
import time
import uuid

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.pool import NullPool

from src.deployment import backup_capture_lock as lock_module
from src.deployment.backup_capture_lock import (
    LOCK_APPLICATION_NAME,
    NOT_VERIFIED,
    VERIFIED_CLOSED,
    VERIFIER_APPLICATION_NAME,
    BackupLockCleanupError,
    BackupLockSetupError,
    backup_capture_lock,
)
from src.exceptions import AdvisoryLockUnavailableError, DatabaseTargetVerificationError, ValidationError
from src.pipeline.bulk_import_safety import (
    RELEASE_FAILED_NOTE,
    DatabaseTargetDesignation,
    build_expected_acknowledgement,
    compute_advisory_lock_key,
    verify_database_target,
)
from tests.postgres.conftest import TEST_DB_PREFIX
from tests.postgres.test_postgres_backup_overlap_quiescence import advisory_lock_rows, confirm_gone, pids_alive, terminate_and_confirm
from tests.postgres.test_postgres_backup_snapshot_consistency import Harness, owned_case
from tests.postgres.test_postgres_fresh_cluster_recovery import bootstrap_sql, launched_cluster, owned_resources, psql_as_admin
from tests.postgres.test_postgres_roles_and_restore import _create_database_owned_by_migrator
from tests.postgres.test_postgres_tls_backup_restore import (
    BACKUP_ROLE,
    isolated_database_environment,  # noqa: F401  (autouse module fixture: removes inherited PG* / NAPSEDGE_DATABASE_* variables)
    provision_database,
    remove_files,
    write_pgpass,
)

pytestmark = pytest.mark.postgres

SESSION_WAIT_S = 10.0


def designation_for(database: str) -> DatabaseTargetDesignation:
    return DatabaseTargetDesignation(expected_database_name=database, acknowledgement=build_expected_acknowledgement(database))


def record(record_property, scenario: str, **evidence) -> dict:
    evidence = {"scenario": scenario, **evidence}
    record_property("evidence", json.dumps(evidence, default=str, sort_keys=True))
    return evidence


class Capture:
    """Test seam: builds the module's OWN engines and records each physical connection's backend PID from the driver."""

    def __init__(self, *, fail_verifier: bool = False):
        self.fail_verifier = fail_verifier
        self.engines: dict = {}
        self.connections: dict = {}
        self.backend_pids: dict = {LOCK_APPLICATION_NAME: [], VERIFIER_APPLICATION_NAME: []}
        self.on_invalidate = None

    def factory(self, url, application_name):
        if self.fail_verifier and application_name == VERIFIER_APPLICATION_NAME:
            raise RuntimeError("injected verifier failure")
        engine = lock_module._build_engine(url, application_name)
        self.engines[application_name] = engine
        event.listen(engine, "connect", lambda dbapi, record_: self.backend_pids[application_name].append(dbapi.info.backend_pid))
        event.listen(engine, "engine_connect", lambda conn: self.connections.setdefault(application_name, conn))
        if application_name == LOCK_APPLICATION_NAME:
            event.listen(engine, "invalidate", lambda dbapi, record_, exc: self.on_invalidate and self.on_invalidate())
        return engine

    @property
    def lock_pid(self) -> int:
        (pid,) = self.backend_pids[LOCK_APPLICATION_NAME]
        return pid


class Admin:
    def __init__(self, cluster, database):
        self.engine = cluster.admin_engine(database)
        self.database = database

    def holders(self, key: int) -> list[tuple]:
        return [tuple(r) for r in advisory_lock_rows(self.engine, key, self.database)]

    def sessions(self, application_name: str) -> list[int]:
        with self.engine.connect() as conn:
            return [r[0] for r in conn.execute(text("SELECT pid FROM pg_stat_activity WHERE application_name = :a AND datname = :d"),
                                               {"a": application_name, "d": self.database})]

    def session_row(self, pid: int):
        with self.engine.connect() as conn:
            return conn.execute(text("SELECT usename, application_name, state FROM pg_stat_activity WHERE pid = :p"), {"p": pid}).first()

    def gone(self, pids) -> bool:
        return confirm_gone(self.engine, list(pids), timeout=SESSION_WAIT_S)

    def dispose(self):
        self.engine.dispose()


@pytest.fixture(scope="module")
def harness(docker_available):
    with owned_resources() as resources, launched_cluster(resources) as (cluster, container_id):
        boot_database = f"{TEST_DB_PREFIX}boot_{uuid.uuid4().hex[:8]}"
        pgpass = write_pgpass(cluster, [(cluster.admin_url.username, cluster.admin_password)])
        try:
            boot = psql_as_admin(cluster, pgpass, bootstrap_sql(cluster, boot_database))
            assert boot.returncode == 0, boot.diagnostics()
            yield Harness(cluster, container_id, boot_database)
        finally:
            remove_files(cluster, pgpass)


@pytest.fixture()
def case(harness):
    """One database per scenario (its own key); an admin observer; after the scenario: no lock or verifier session remains in it."""
    cluster = harness.cluster
    with owned_case(cluster) as ledger:
        database = _create_database_owned_by_migrator(cluster)
        ledger.databases.append(database)
        provision_database(cluster, database, seed=False)
        admin = Admin(cluster, database)
        ledger.engines.append(admin.engine)
        key = compute_advisory_lock_key(_verified(cluster, database))
        yield cluster, database, admin, key, ledger
        assert admin.sessions(LOCK_APPLICATION_NAME) == [] and admin.sessions(VERIFIER_APPLICATION_NAME) == []
        assert admin.holders(key) == []


def _verified(cluster, database):
    engine = create_engine(cluster.backup_url(database), poolclass=NullPool)
    try:
        with engine.connect() as conn:
            return verify_database_target(conn, designation_for(database))
    finally:
        engine.dispose()


class Probe:
    """A DISTINCT physical backup-role session (its own NullPool engine) that can take and release the key itself, and is ended and verified."""

    def __init__(self, cluster, database, key):
        self.engine = create_engine(cluster.backup_url(database), poolclass=NullPool, connect_args={"application_name": "napsedge_lock_probe"})
        self.connection = self.engine.connect()
        self.connection.execution_options(isolation_level="AUTOCOMMIT")
        self.pid = self.connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
        self.key = key

    def try_lock(self) -> bool:
        return self.connection.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": self.key}).scalar_one()

    def unlock(self) -> bool:
        return self.connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": self.key}).scalar_one()

    def end(self):
        self.connection.close()
        self.engine.dispose()


def no_secret(cluster, *texts: str) -> None:
    joined = "\n".join(texts)
    for secret in cluster.secrets_to_scrub():
        assert secret not in joined


# ---------------------------------------------------------------------------
# A. normal operation
# ---------------------------------------------------------------------------
def test_a_normal_lock_held_during_body_backend_ends_afterwards_and_a_distinct_session_can_acquire(case, record_property):
    cluster, database, admin, key, _ = case
    capture = Capture()
    during: dict = {}
    escaped = None
    try:
        with backup_capture_lock(cluster.backup_url(database), designation_for(database), _engine_factory=capture.factory) as session:
            during["holders"] = admin.holders(key)
            during["session"] = tuple(admin.session_row(session.backend_pid))
    except Exception as exc:  # recorded, then asserted AFTER the server-side evidence
        escaped = exc
    pid = session.backend_pid
    gone = admin.gone([pid])
    assert gone, "the original lock backend is still present on the server"
    holders_after = admin.holders(key)
    probe = Probe(cluster, database, key)
    try:
        probe_acquired, probe_released = probe.try_lock(), probe.unlock()
    finally:
        probe.end()
    probe_gone = admin.gone([probe.pid])
    evidence = record(record_property, "A_normal", acquired=True, body_entered=True, release="confirmed", backend_pid=pid,
                      holders_during=during["holders"], session_during=during["session"], backend_gone_after=gone,
                      holders_after=holders_after, report=session.cleanup, probe_pid=probe.pid, probe_acquired=probe_acquired,
                      probe_released=probe_released, probe_gone=probe_gone, lock_physical_connects=len(capture.backend_pids[LOCK_APPLICATION_NAME]))
    assert escaped is None, type(escaped).__name__
    assert pid == capture.lock_pid and evidence["lock_physical_connects"] == 1
    assert during["holders"] == [(pid, True)]
    assert during["session"] == (BACKUP_ROLE, LOCK_APPLICATION_NAME, "idle")  # the post-acquisition rollback left no open transaction
    assert gone and holders_after == []
    assert session.cleanup.confirmed and session.cleanup.verification == VERIFIED_CLOSED and not session.cleanup.connection_discarded
    assert session.cleanup.other_holder_present is False
    assert probe.pid != pid and probe_acquired and probe_released and probe_gone


# ---------------------------------------------------------------------------
# B. primary body failure
# ---------------------------------------------------------------------------
def test_b_a_primary_body_failure_is_preserved_and_physical_cleanup_is_verified(case, record_property):
    cluster, database, admin, key, _ = case
    primary = RuntimeError("synthetic body failure")
    primary.add_note("caller note")
    seen: dict = {}
    with pytest.raises(BaseException) as info:
        with backup_capture_lock(cluster.backup_url(database), designation_for(database)) as session:
            seen["holders"] = admin.holders(key)
            seen["session"] = session
            raise primary
    pid = seen["session"].backend_pid
    gone, holders_after = admin.gone([pid]), admin.holders(key)
    assert type(info.value) is RuntimeError, type(info.value).__name__
    record(record_property, "B_primary_failure", acquired=True, body_entered=True, release="confirmed", backend_pid=pid,
           holders_during=seen["holders"], backend_gone_after=gone, holders_after=holders_after, report=seen["session"].cleanup,
           exception=type(info.value).__name__, notes=info.value.__notes__)
    assert info.value is primary and info.value.__notes__ == ["caller note"]
    assert seen["holders"] == [(pid, True)] and gone and holders_after == []
    assert seen["session"].cleanup.connection_discarded and seen["session"].cleanup.confirmed


# ---------------------------------------------------------------------------
# C. a real aborted transaction: the session lock survives rollback; only physical closure ends it
# ---------------------------------------------------------------------------
def test_c_an_aborted_transaction_leaves_the_lock_held_until_the_wrapper_physically_closes_the_session(case, record_property):
    cluster, database, admin, key, _ = case
    capture = Capture()
    seen: dict = {}
    capture.on_invalidate = lambda: seen.setdefault("holders_at_discard", admin.holders(key))
    with pytest.raises(BaseException) as info:
        with backup_capture_lock(cluster.backup_url(database), designation_for(database), _engine_factory=capture.factory) as session:
            seen["session"] = session
            lock_connection = capture.connections[LOCK_APPLICATION_NAME]  # TEST orchestration through the seam
            with pytest.raises(DBAPIError):
                lock_connection.execute(text("SELECT 1/0"))
            lock_connection.rollback()
            seen["holders_after_rollback"] = admin.holders(key)
            lock_connection.execute(text("SELECT 1/0"))  # aborts the transaction again; this error is the primary
    pid = seen["session"].backend_pid
    gone, holders_after = admin.gone([pid]), admin.holders(key)
    assert isinstance(info.value, DBAPIError), type(info.value).__name__
    record(record_property, "C_aborted_transaction", acquired=True, body_entered=True, release="unconfirmed (unlock raised in an aborted transaction)",
           backend_pid=pid, holders_after_rollback=seen["holders_after_rollback"], holders_at_discard=seen["holders_at_discard"],
           backend_gone_after=gone, holders_after=holders_after, report=seen["session"].cleanup,
           exception=type(info.value).__name__, sqlstate=getattr(info.value.orig, "sqlstate", None), notes=info.value.__notes__)
    assert getattr(info.value.orig, "sqlstate", None) == "22012"  # the body's own division_by_zero, preserved
    assert seen["holders_after_rollback"] == [(pid, True)]  # REAL PostgreSQL: the session-level lock survives rollback
    assert info.value.__notes__ == [RELEASE_FAILED_NOTE.format(error_type="InternalError")]  # the shared manager's note, unchanged
    assert seen["holders_at_discard"] == [(pid, True)]  # still held after the failed unlock, until physical closure
    assert gone and holders_after == []
    assert seen["session"].cleanup.connection_discarded and seen["session"].cleanup.confirmed


# ---------------------------------------------------------------------------
# D. real backend loss
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("body_fails", [True, False], ids=["with_primary", "success_path"])
def test_d_backend_loss_preserves_the_primary_or_surfaces_the_release_error_and_leaves_no_ownership(case, record_property, body_fails):
    cluster, database, admin, key, _ = case
    capture = Capture()
    primary = RuntimeError("synthetic failure after backend loss")
    seen: dict = {}
    expected = RuntimeError if body_fails else OperationalError
    with pytest.raises(BaseException) as info:
        with backup_capture_lock(cluster.backup_url(database), designation_for(database), _engine_factory=capture.factory) as session:
            seen["session"] = session
            terminate_and_confirm(admin.engine, session.backend_pid)  # the disposable test-owned backend only
            seen["holders_after_termination"] = admin.holders(key)
            if body_fails:
                raise primary
    assert type(info.value) is expected, type(info.value).__name__
    pid = seen["session"].backend_pid
    holders_after = admin.holders(key)
    record(record_property, f"D_backend_loss_{'with_primary' if body_fails else 'success_path'}", acquired=True, body_entered=True,
           release="unconfirmed (backend gone)", backend_pid=pid, holders_after_termination=seen["holders_after_termination"],
           backend_gone_after=not pids_alive(admin.engine, [pid]), holders_after=holders_after, report=seen["session"].cleanup,
           exception=type(info.value).__name__, notes=getattr(info.value, "__notes__", []),
           lock_physical_connects=len(capture.backend_pids[LOCK_APPLICATION_NAME]))
    if body_fails:
        assert info.value is primary and info.value.__notes__ == [RELEASE_FAILED_NOTE.format(error_type="OperationalError")]
    else:
        assert getattr(info.value, "__notes__", []) == []  # the shared manager's release error, propagated unchanged
    assert seen["holders_after_termination"] == [] and holders_after == []
    assert len(capture.backend_pids[LOCK_APPLICATION_NAME]) == 1  # never silently reconnected
    assert seen["session"].cleanup.connection_discarded and seen["session"].cleanup.confirmed


# ---------------------------------------------------------------------------
# E. acquisition refusal
# ---------------------------------------------------------------------------
def test_e_refusal_is_immediate_the_body_never_runs_and_the_first_owner_is_unaffected(case, record_property):
    cluster, database, admin, key, _ = case
    probe = Probe(cluster, database, key)
    try:
        assert probe.try_lock()
        capture = Capture()
        entered = []
        started = time.monotonic()
        with pytest.raises(BaseException) as info:
            with backup_capture_lock(cluster.backup_url(database), designation_for(database), _engine_factory=capture.factory):
                entered.append(True)
        elapsed = time.monotonic() - started
        lock_pid = capture.lock_pid
        lock_gone = admin.gone([lock_pid])
        holders_while_probe = admin.holders(key)
        probe_alive = probe.connection.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        released = probe.unlock()
        probe.end()
    probe_gone = admin.gone([probe.pid])
    assert type(info.value) is AdvisoryLockUnavailableError, type(info.value).__name__
    record(record_property, "E_refusal", acquired=False, body_entered=bool(entered), release="not attempted (refused)", backend_pid=lock_pid,
           first_owner_pid=probe.pid, holders_after_refusal=holders_while_probe, backend_gone_after=lock_gone, elapsed_s=round(elapsed, 3),
           first_owner_still_usable=probe_alive, first_owner_released=released, first_owner_gone=probe_gone, notes=getattr(info.value, "__notes__", []))
    assert entered == [] and lock_pid != probe.pid
    assert holders_while_probe == [(probe.pid, True)] and probe_alive and released and probe_gone
    assert lock_gone and getattr(info.value, "__notes__", []) == []
    assert elapsed < 5.0  # includes connect, verification and the bounded cleanup verification; the refusal itself never waits


# ---------------------------------------------------------------------------
# F. wrong target, setup failure, and an unverified-TLS URL
# ---------------------------------------------------------------------------
def test_f_a_wrong_target_never_acquires_or_runs_the_body_and_its_session_is_closed(case, record_property):
    cluster, database, admin, key, _ = case
    capture = Capture()
    entered = []
    with pytest.raises(BaseException) as info:
        with backup_capture_lock(cluster.backup_url(database), designation_for(f"{TEST_DB_PREFIX}not_this_one"), _engine_factory=capture.factory):
            entered.append(True)
    lock_pid = capture.lock_pid
    gone, holders_after = admin.gone([lock_pid]), admin.holders(key)
    assert type(info.value) is DatabaseTargetVerificationError, type(info.value).__name__
    record(record_property, "F_wrong_target", acquired=False, body_entered=bool(entered), release="not attempted", backend_pid=lock_pid,
           backend_gone_after=gone, holders_after=holders_after, notes=info.value.__notes__)
    assert entered == [] and gone and holders_after == []
    # the verifier validates against the same (wrong) designation, so it cannot verify: reported honestly, never as success
    assert info.value.__notes__ == ["backup lock session cleanup was not confirmed: verification=not_verified; failed steps=verify:DatabaseTargetVerificationError"]
    no_secret(cluster, str(info.value), *info.value.__notes__)


def test_f_a_connection_failure_is_sanitized_and_creates_no_session(case, record_property):
    cluster, database, admin, key, _ = case
    bad_url = cluster.backup_url(database).set(password="definitely-not-the-password")
    entered = []
    with pytest.raises(BaseException) as info:
        with backup_capture_lock(bad_url, designation_for(database)):
            entered.append(True)
    rendered = str(info.value)
    assert type(info.value) is BackupLockSetupError, type(info.value).__name__
    record(record_property, "F_setup_failure", acquired=False, body_entered=bool(entered), release="not attempted", message=rendered,
           chained=info.value.__cause__ is not None or not info.value.__suppress_context__, lock_sessions_after=admin.sessions(LOCK_APPLICATION_NAME))
    assert entered == [] and rendered == "opening the dedicated backup lock session failed (OperationalError)"
    assert info.value.__cause__ is None and info.value.__suppress_context__
    assert "definitely-not-the-password" not in rendered
    no_secret(cluster, rendered)


def test_f_a_url_without_verify_full_is_refused_before_any_connection(case, record_property):
    cluster, database, admin, key, _ = case
    url = cluster.backup_url(database)
    weak = url.set(query={k: v for k, v in url.query.items() if k != "sslmode"} | {"sslmode": "require"})
    with pytest.raises(BaseException) as info:
        with backup_capture_lock(weak, designation_for(database)):
            pytest.fail("the body must not run")
    assert type(info.value) is ValidationError, type(info.value).__name__
    record(record_property, "F_unverified_tls", acquired=False, body_entered=False, message=str(info.value),
           lock_sessions_after=admin.sessions(LOCK_APPLICATION_NAME))
    no_secret(cluster, str(info.value))


# ---------------------------------------------------------------------------
# G. a real false unlock result
# ---------------------------------------------------------------------------
def test_g_a_false_unlock_result_keeps_the_shared_behavior_and_the_session_is_discarded(case, record_property):
    cluster, database, admin, key, _ = case
    capture = Capture()
    seen: dict = {}
    with pytest.raises(BaseException) as info:
        with backup_capture_lock(cluster.backup_url(database), designation_for(database), _engine_factory=capture.factory) as session:
            seen["session"] = session
            lock_connection = capture.connections[LOCK_APPLICATION_NAME]  # TEST orchestration through the seam
            seen["early_unlock"] = lock_connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key}).scalar_one()
            lock_connection.rollback()
            seen["holders_after_early_unlock"] = admin.holders(key)
    pid = seen["session"].backend_pid
    gone, holders_after = admin.gone([pid]), admin.holders(key)
    assert type(info.value) is AdvisoryLockUnavailableError, type(info.value).__name__
    record(record_property, "G_false_unlock", acquired=True, body_entered=True, release="false (not held)", backend_pid=pid,
           holders_after_early_unlock=seen["holders_after_early_unlock"], backend_gone_after=gone, holders_after=holders_after,
           report=seen["session"].cleanup, exception=type(info.value).__name__)
    assert seen["early_unlock"] is True and seen["holders_after_early_unlock"] == []
    assert "was not held by this session" in str(info.value)
    assert gone and holders_after == [] and seen["session"].cleanup.connection_discarded and seen["session"].cleanup.confirmed


# ---------------------------------------------------------------------------
# H. a verification failure is never reported as success
# ---------------------------------------------------------------------------
def test_h_an_unavailable_verifier_raises_without_a_primary_although_the_server_shows_the_session_closed(case, record_property):
    cluster, database, admin, key, _ = case
    capture = Capture(fail_verifier=True)
    seen: dict = {}
    with pytest.raises(BaseException) as info:
        with backup_capture_lock(cluster.backup_url(database), designation_for(database), _engine_factory=capture.factory) as session:
            seen["session"] = session
    pid = seen["session"].backend_pid
    gone, holders_after = admin.gone([pid]), admin.holders(key)
    assert type(info.value) is BackupLockCleanupError, type(info.value).__name__
    record(record_property, "H_verifier_unavailable", acquired=True, body_entered=True, release="confirmed by the manager", backend_pid=pid,
           backend_gone_after=gone, holders_after=holders_after, report=info.value.report, message=str(info.value))
    assert info.value.report.verification == NOT_VERIFIED and info.value.report.failures == ("verify:RuntimeError",)
    assert info.value.report.connection_closed and info.value.report.engine_disposed
    assert gone and holders_after == []  # the administrator sees it closed; the module still refuses to claim what it could not verify


# ---------------------------------------------------------------------------
# I. interrupts and credential-safe diagnostics
# ---------------------------------------------------------------------------
def test_i_a_primary_keyboard_interrupt_is_preserved_and_the_session_is_closed(case, record_property):
    cluster, database, admin, key, _ = case
    primary = KeyboardInterrupt()
    seen: dict = {}
    with pytest.raises(BaseException) as info:
        with backup_capture_lock(cluster.backup_url(database), designation_for(database)) as session:
            seen["session"] = session
            raise primary
    pid = seen["session"].backend_pid
    gone, holders_after = admin.gone([pid]), admin.holders(key)
    report = seen["session"].cleanup
    assert type(info.value) is KeyboardInterrupt, type(info.value).__name__
    record(record_property, "I_interrupt", acquired=True, body_entered=True, release="confirmed", backend_pid=pid, backend_gone_after=gone,
           holders_after=holders_after, report=report, exception=type(info.value).__name__)
    assert info.value is primary and getattr(info.value, "__notes__", []) == []
    assert gone and holders_after == [] and report.connection_discarded and report.confirmed
    no_secret(cluster, repr(report))


# ---------------------------------------------------------------------------
# J. repeated invocations
# ---------------------------------------------------------------------------
def test_j_repeated_invocations_use_distinct_physical_sessions_and_accumulate_no_locks(case, record_property):
    cluster, database, admin, key, _ = case
    pids, holders_during = [], []
    for _ in range(3):
        with backup_capture_lock(cluster.backup_url(database), designation_for(database)) as session:
            holders_during.append(admin.holders(key))
        pids.append(session.backend_pid)
        assert session.cleanup.confirmed
    gone, holders_after = admin.gone(pids), admin.holders(key)
    with admin.engine.connect() as conn:
        advisory_rows = conn.execute(text("SELECT count(*) FROM pg_locks l JOIN pg_database d ON d.oid = l.database "
                                          "WHERE l.locktype = 'advisory' AND d.datname = :d"), {"d": database}).scalar()
    record(record_property, "J_repeated", acquired=True, body_entered=True, release="confirmed", backend_pids=pids, holders_during=holders_during,
           backend_gone_after=gone, holders_after=holders_after, advisory_rows_after=advisory_rows)
    assert len(set(pids)) == 3 and holders_during == [[(pid, True)] for pid in pids]
    assert gone and holders_after == [] and advisory_rows == 0


# ---------------------------------------------------------------------------
# K. a separate caller-owned connection and its transaction are untouched
# ---------------------------------------------------------------------------
def test_k_a_callers_own_connection_and_transaction_are_untouched(case, record_property):
    cluster, database, admin, key, ledger = case
    caller_engine = create_engine(cluster.backup_url(database))  # an ordinary pooled engine: the caller's, not the module's
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
    with backup_capture_lock(cluster.backup_url(database), designation_for(database)) as session:
        pass
    after = caller.execute(text("SELECT now()")).scalar_one()
    setting = caller.execute(text("SELECT current_setting('lock_timeout')")).scalar_one()
    state = admin.session_row(caller_pid)
    record(record_property, "K_caller_untouched", acquired=True, body_entered=True, release="confirmed", backend_pid=session.backend_pid,
           caller_pid=caller_pid, same_transaction=before == after, caller_setting=setting, caller_state=state, caller_events=events,
           report=session.cleanup)
    assert caller_pid != session.backend_pid
    assert before == after and setting == "3s" and caller.in_transaction() and events == []
    assert state[0] == BACKUP_ROLE and state[1] != LOCK_APPLICATION_NAME and state[2] == "idle in transaction"
    caller.rollback()


# ---------------------------------------------------------------------------
# Phase 9B-6C additions: L's own identity facts and confirm_held()
# ---------------------------------------------------------------------------
def test_l_reads_its_own_identity_and_confirm_held_observes_ownership_until_l_is_lost(case, record_property):
    cluster, database, admin, key, _ = case
    from src.deployment.backup_capture_lock import BackupLockNotHeld

    with admin.engine.connect() as conn:
        admin_sysid = conn.execute(text("SELECT system_identifier::text FROM pg_control_system()")).scalar_one()
        admin_oid = conn.execute(text("SELECT oid FROM pg_database WHERE datname = current_database()")).scalar_one()
    seen: dict = {}
    primary = RuntimeError("synthetic failure after L was lost")
    with pytest.raises(BaseException) as info:
        with backup_capture_lock(cluster.backup_url(database), designation_for(database)) as session:
            seen["session"] = session
            seen["identity"] = (session.system_identifier, session.database_oid)
            session.confirm_held()
            seen["state_after_confirm"] = tuple(admin.session_row(session.backend_pid))
            terminate_and_confirm(admin.engine, session.backend_pid)
            with pytest.raises(BaseException) as lost:
                session.confirm_held()
            seen["lost"] = (type(lost.value).__name__, str(lost.value))
            raise primary
    assert type(info.value) is RuntimeError and info.value is primary
    with pytest.raises(BackupLockNotHeld) as after_exit:
        seen["session"].confirm_held()
    gone, holders = admin.gone([seen["session"].backend_pid]), admin.holders(key)
    record(record_property, "L_identity_confirm_held", acquired=True, body_entered=True, backend_pid=seen["session"].backend_pid,
           l_identity=seen["identity"], admin_identity=(admin_sysid, admin_oid), state_after_confirm=seen["state_after_confirm"],
           confirm_after_loss=seen["lost"], confirm_after_exit=str(after_exit.value), backend_gone_after=gone, holders_after=holders,
           notes=info.value.__notes__)
    assert seen["identity"] == (admin_sysid, admin_oid)
    assert seen["state_after_confirm"][2] == "idle"  # confirm_held ended its own transaction
    assert seen["lost"] == ("BackupLockNotHeld", "the backup lock session could not confirm it holds the lock (OperationalError)")
    assert str(after_exit.value) == "the backup lock context is not active"
    assert gone and holders == []
