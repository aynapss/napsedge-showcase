"""
Phase 9B-6F: the production fresh catalog observer and refusal check (`src/deployment/backup_capture_catalog.py`)
exercised around the real capture path, against one disposable, synthetic PostgreSQL 16 cluster (the existing 9B
harness, the 9B-2 grants, the cluster image's PostgreSQL 16.15 client through the 9B-6D test-layer `ContainerClientHost`).

The guarded flow (`guarded_capture`) is TEST-LAYER orchestration, not a production coordinator. It calls the production
modules in this order:
  1. `backup_capture_lock` (L), then `observe_endpoint(label="before")`, BEFORE S's snapshot transaction begins;
  2. `backup_capture_snapshot` (S; baseline), then `backup_capture_dump` (the real `pg_dump`);
  3. inside the dump body, i.e. after `pg_dump` has finished and while S and L are still open:
     `observe_endpoint(label="after")`, then `require_unchanged(before, after, <L's own source identity>)`;
  4. only if that check passes: the NORMAL path (copy the scratch archive and baseline out; in scenario A, restore and compare).
A refusal propagates out of the dump, snapshot and lock contexts, which clean up under their own contracts. The existing
entry points `backup_capture_lock`, `backup_capture_snapshot` and `backup_capture_dump` are UNCHANGED and do not call the
observer themselves: the standalone dump module is not a guarded capture coordinator.

Claims about commits, sessions and blocking come from the SERVER (administrator reads in fresh transactions). Ordering
during the dump uses the 9B-6D deterministic barrier (an ACCESS EXCLUSIVE request queued on `conferences` behind S's ACCESS
SHARE, which parks pg_dump's LOCK TABLE batch). Only previously validated operations are used: `GRANT`/`REVOKE INSERT ON
conferences` to the application role (12.5, 12.8) and the queued exclusive request (C7, 12.8). A blocked or rolled-back
change is never counted as detection.
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import posixpath
import time
import uuid

import pytest
from sqlalchemy import create_engine, event, text

from src.deployment.backup_artifact import canonical_json_bytes
from src.deployment.backup_capture_catalog import (
    OBSERVER_APPLICATION_NAME,
    CatalogChanged,
    CatalogObservation,
    CatalogObservationFailed,
    NoDifferenceObserved,
    SourceIdentity,
    observe_endpoint,
    require_unchanged,
)
from src.deployment.backup_capture_dump import backup_capture_dump
from src.deployment.backup_capture_lock import VERIFIER_APPLICATION_NAME, backup_capture_lock
from src.deployment.backup_capture_snapshot import SNAPSHOT_APPLICATION_NAME, backup_capture_snapshot
from tests.postgres.capture_client_support import production_client
from tests.postgres.test_postgres_backup_capture_dump import ExclusiveBarrier, Observer
from tests.postgres.test_postgres_backup_capture_lock import designation_for, harness, record  # noqa: F401  (harness: module fixture)
from tests.postgres.test_postgres_backup_capture_roundtrip import Captured, case, compare, new_target, restore, sha256_in_container  # noqa: F401  (case: fixture)
from tests.postgres.test_postgres_backup_capture_snapshot import Seam, admin_scalar, new_database
from tests.postgres.test_postgres_roles_and_restore import ACCEPTED_ALEMBIC_HEAD, RUNTIME_ROLE

pytestmark = pytest.mark.postgres

ACL_PROBE = f"SELECT coalesce(relacl::text, '') FROM pg_class WHERE oid = 'public.conferences'::regclass"
GRANT = f"GRANT INSERT ON public.conferences TO {RUNTIME_ROLE}"
REVOKE = f"REVOKE INSERT ON public.conferences FROM {RUNTIME_ROLE}"
INSERT_GRANTED = f"{RUNTIME_ROLE}=a"


def committed(case, statement: str) -> dict:
    """Runs one validated catalog statement as the migrator in its own committed transaction; proves the commit from a FRESH administrator read."""
    engine = create_engine(case.cluster.migrator_url(case.database))
    try:
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout = '5s'"))
            conn.execute(text(statement))
    finally:
        engine.dispose()
    acl = admin_scalar(case.admin, ACL_PROBE)
    return {"statement": statement, "fresh_reader_acl": acl, "insert_granted_visible": INSERT_GRANTED in acl, "committed_at": admin_scalar(case.admin, "SELECT now()")}


def endpoint_facts(endpoint) -> dict | None:
    if endpoint is None:
        return None
    return {"label": endpoint.label, "pid": endpoint.backend_pid, "backend_start": endpoint.backend_start, "transaction_start": endpoint.transaction_start,
            "isolation": endpoint.isolation, "read_only": endpoint.read_only, "source": dataclasses.asdict(endpoint.source),
            "digests": endpoint.observation.digests, "cleanup": endpoint.cleanup_verification}


def guarded_capture(case, rec: dict, *, during_dump=None, in_snapshot=None, before_after_observation=None, observer_factory=None):
    """The test-layer guarded flow (module docstring). Everything it learns goes into `rec`, also when it raises."""
    url, d = case.cluster.backup_url(case.database), designation_for(case.database)
    rec.update(order=[], normal_path_entered=False, transport=case.client.transport)
    snap_seam = Seam()
    with backup_capture_lock(url, d) as lock:
        expected = SourceIdentity(lock.system_identifier, lock.database_oid, lock.verified_target.database_name)  # read on L itself
        rec.update(lock_pid=lock.backend_pid, expected=dataclasses.asdict(expected))
        rec["before"] = observe_endpoint(url, d, label="before", expected_source=expected, _engine_factory=observer_factory)
        rec["order"].append("before_observed")
        with backup_capture_snapshot(lock, url, d, expected_alembic_head=ACCEPTED_ALEMBIC_HEAD, artifact_id=str(uuid.uuid4()),
                                     _engine_factory=snap_seam.factory) as handle:
            rec["order"].append("snapshot_open")
            rec.update(snapshot_pid=handle.facts.backend_pid, snapshot_transaction_start=handle.facts.transaction_start)
            if in_snapshot is not None:
                rec["in_snapshot"] = in_snapshot(handle, snap_seam)
            with contextlib.ExitStack() as stack:
                if during_dump is not None:
                    barrier = ExclusiveBarrier(case.cluster, case.database, case.admin, case.ledger)
                    barrier.start()
                    rec["dump_observer"] = stack.enter_context(Observer(case.admin, barrier, case.key, action=during_dump)).seen
                with backup_capture_dump(handle, client=production_client(case.client)) as archive:
                    rec["order"].append("dump_finished")
                    rec["dump"] = {"application_name": archive.application_name, "returncode": archive.returncode, "archive_bytes": archive.archive_bytes}
                    blocker = before_after_observation(case) if before_after_observation is not None else contextlib.nullcontext()
                    with blocker as blocked:
                        rec["blocked_by"] = blocked
                        started = time.monotonic()
                        try:
                            rec["after"] = observe_endpoint(url, d, label="after", expected_source=expected, _engine_factory=observer_factory)
                        finally:
                            rec["after_elapsed_s"] = round(time.monotonic() - started, 2)
                    rec["order"].append("after_observed")
                    rec["sessions_during_check"] = {"lock": case.admin.holders(case.key), "snapshot": case.admin.sessions(SNAPSHOT_APPLICATION_NAME)}
                    rec["result"] = require_unchanged(rec["before"], rec["after"], expected)  # the refusal check, before the normal path
                    rec["order"].append("check_passed")
                    rec["normal_path_entered"] = True  # ---- the NORMAL path: only reachable when the check passed ----
                    copy = posixpath.join(case.container_dir, f"archive_{uuid.uuid4().hex[:8]}.dump")
                    assert case.client._run(["sh", "-c", 'umask 077 && cp -- "$1" "$2"', "sh", archive.archive_path, copy]).returncode == 0
                    baseline_file = case.host_dir / f"baseline_{uuid.uuid4().hex[:8]}.json"
                    with open(baseline_file, "xb") as out:
                        out.write(canonical_json_bytes(handle.baseline.to_document()))
                    rec["captured"] = Captured(copy, sha256_in_container(case, copy), archive.archive_bytes, archive.toc_sha256, baseline_file,
                                               hashlib.sha256(baseline_file.read_bytes()).hexdigest(), case.database, handle.facts.database_oid,
                                               archive.snapshot_id, archive.pg_dump_version, archive.pg_restore_version, lock.backend_pid,
                                               handle.facts.backend_pid, archive.archive_path, handle, {})
    return rec


def run_refused(case, **kw) -> tuple[dict, BaseException]:
    rec: dict = {}
    with pytest.raises(BaseException) as info:
        guarded_capture(case, rec, **kw)
    return rec, info.value


def scratch_archives(case) -> list[str]:
    out = case.client._run(["sh", "-c", 'ls -1 -- "$1"', "sh", case.container_dir]).stdout.decode().split()
    return [name for name in out if name.startswith("archive_")]


def owned_sessions(case) -> dict:
    return {app: case.admin.sessions(app) for app in (OBSERVER_APPLICATION_NAME, VERIFIER_APPLICATION_NAME, SNAPSHOT_APPLICATION_NAME)}


def base_evidence(rec: dict) -> dict:
    return {"transport": rec.get("transport"), "order": rec.get("order"), "lock_pid": rec.get("lock_pid"), "snapshot_pid": rec.get("snapshot_pid"),
            "snapshot_transaction_start": rec.get("snapshot_transaction_start"), "expected_source": rec.get("expected"),
            "before": endpoint_facts(rec.get("before")), "after": endpoint_facts(rec.get("after")), "dump": rec.get("dump"),
            "sessions_during_check": rec.get("sessions_during_check"), "normal_path_entered": rec.get("normal_path_entered")}


# ---------------------------------------------------------------------------
# A. no-change control: observations match and the guarded scratch flow proceeds
# ---------------------------------------------------------------------------
def test_a_no_change_the_observations_match_and_the_normal_path_proceeds_to_a_restore(case, record_property):
    rec: dict = {}
    escaped = None
    try:
        guarded_capture(case, rec)
    except Exception as exc:  # recorded; the server-side session assertion below runs first
        escaped = exc
    assert all(v == [] for v in owned_sessions(case).values()), owned_sessions(case)
    assert escaped is None, f"{type(escaped).__name__}: {escaped}"
    result = rec["result"]
    target = new_target(case)
    outcome = restore(case, rec["captured"], target)
    comparison = compare(case, rec["captured"], target) if outcome.returncode == 0 else None
    sessions = owned_sessions(case)
    record(record_property, "A_no_change", acquired=True, body_entered=True, **base_evidence(rec), result_meaning=result.meaning,
           restore_exit=outcome.returncode, verdict=comparison and comparison.verdict, sessions_after=sessions)
    assert isinstance(result, NoDifferenceObserved) and result.meaning == "no difference observed at these endpoints"
    assert rec["order"] == ["before_observed", "snapshot_open", "dump_finished", "after_observed", "check_passed"]
    b, a = rec["before"], rec["after"]
    assert len({b.backend_pid, a.backend_pid, rec["snapshot_pid"], rec["lock_pid"]}) == 4  # four distinct sessions
    assert b.transaction_start < rec["snapshot_transaction_start"] < a.transaction_start  # before precedes S; after follows the dump
    assert b.source == a.source == SourceIdentity(**rec["expected"]) and b.complete and a.complete
    assert rec["sessions_during_check"]["lock"] == [(rec["lock_pid"], True)] and rec["sessions_during_check"]["snapshot"] == [rec["snapshot_pid"]]
    assert rec["normal_path_entered"] and outcome.returncode == 0 and all(comparison.verdict.values())
    assert all(v == [] for v in sessions.values())


# ---------------------------------------------------------------------------
# B. a persistent committed ACL change during the real dump is detected; the normal path is not entered
# ---------------------------------------------------------------------------
def test_b_a_committed_acl_change_during_the_dump_refuses_with_the_raw_difference_and_no_normal_path(case, record_property):
    rec, exc = run_refused(case, during_dump=lambda: committed(case, GRANT))
    proof = rec["dump_observer"].get("action")
    sessions = owned_sessions(case)
    record(record_property, "B_committed_change", acquired=True, body_entered=True, **base_evidence(rec), dump_parked=rec["dump_observer"].get("dump"),
           commit_proof=proof, exception=type(exc).__name__, categories=getattr(exc, "categories", None),
           differences={c: {str(k): v for k, v in d.items()} for c, d in getattr(exc, "differences", {}).items()},
           scratch_archives=scratch_archives(case), sessions_after=sessions)
    assert rec["dump_observer"].get("dump") is not None  # the change happened while the real pg_dump was in flight
    assert proof["insert_granted_visible"]  # committed, proven by a fresh read
    assert type(exc) is CatalogChanged and exc.categories == ["acls"] and list(exc.differences["acls"]) == [("conferences",)]
    before_row, after_row = exc.differences["acls"][("conferences",)]
    assert INSERT_GRANTED not in before_row[3] and INSERT_GRANTED in after_row[3]
    assert rec["order"] == ["before_observed", "snapshot_open", "dump_finished", "after_observed"]  # refused before "check_passed"
    assert rec["normal_path_entered"] is False and scratch_archives(case) == [] and "captured" not in rec
    assert all(v == [] for v in sessions.values())


# ---------------------------------------------------------------------------
# C. a blocked observation times out and refuses; it never becomes an empty or equal digest
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def queued_exclusive_request(case):
    """A migrator's ACCESS EXCLUSIVE request on conferences, confirmed queued behind S's ACCESS SHARE (C7/12.8); cancelled on exit."""
    barrier = ExclusiveBarrier(case.cluster, case.database, case.admin, case.ledger)
    barrier.start()
    try:
        yield {"queued_on": "conferences", "requester_pid": barrier.outcome.get("pid")}
    finally:
        barrier.cancel()


def test_c_an_observation_blocked_by_a_queued_exclusive_request_times_out_and_refuses(case, record_property):
    rec, exc = run_refused(case, before_after_observation=queued_exclusive_request)
    sessions = owned_sessions(case)
    record(record_property, "C_observation_blocked", acquired=True, body_entered=True, **base_evidence(rec), blocked_by=rec.get("blocked_by"),
           after_elapsed_s=rec.get("after_elapsed_s"), exception=type(exc).__name__, message=str(exc), notes=getattr(exc, "__notes__", []),
           scratch_archives=scratch_archives(case), sessions_after=sessions)
    assert type(exc) is CatalogObservationFailed and str(exc) == "the catalog observation did not complete (OperationalError)"
    assert "after" not in rec and "result" not in rec  # no endpoint, no digest, no comparison
    assert 4.5 <= rec["after_elapsed_s"] < 30  # the observer's own 5 s lock_timeout, not an indefinite wait
    assert rec["normal_path_entered"] is False and scratch_archives(case) == [] and all(v == [] for v in sessions.values())


# ---------------------------------------------------------------------------
# D. change and reversal: the known equal-endpoint bypass (CHARACTERIZATION, not a safety validation)
# ---------------------------------------------------------------------------
def test_d_characterization_a_change_and_its_reversal_between_the_endpoints_is_not_detected(case, record_property):
    rec: dict = {}
    guarded_capture(case, rec, during_dump=lambda: {"grant": committed(case, GRANT), "revoke": committed(case, REVOKE)})
    proof = rec["dump_observer"].get("action")
    record(record_property, "D_change_and_reversal_bypass", acquired=True, body_entered=True, **base_evidence(rec), commit_proof=proof,
           result_meaning=rec["result"].meaning, characterization="KNOWN BYPASS: equal endpoints, the guarded flow proceeds; not a safety validation")
    assert proof["grant"]["insert_granted_visible"] and not proof["revoke"]["insert_granted_visible"]  # both committed, each proven by a fresh read
    assert proof["grant"]["committed_at"] <= proof["revoke"]["committed_at"]
    assert rec["before"].transaction_start < proof["grant"]["committed_at"] and proof["revoke"]["committed_at"] < rec["after"].transaction_start
    assert isinstance(rec["result"], NoDifferenceObserved)  # the bypass, retained: the check cannot see a change and its reversal
    assert rec["normal_path_entered"]


# ---------------------------------------------------------------------------
# E. freshness: the endpoint sees a committed change that the long-lived snapshot owner does not
# ---------------------------------------------------------------------------
def test_e_the_fresh_endpoint_sees_a_committed_change_that_the_snapshot_owner_does_not(case, record_property):
    def change_while_s_is_open(handle, snap_seam):
        proof = committed(case, GRANT)
        proof["s_acl"] = snap_seam.connections[SNAPSHOT_APPLICATION_NAME].execute(text(ACL_PROBE)).scalar_one()  # TEST seam: a SELECT inside S
        proof["s_now"] = snap_seam.connections[SNAPSHOT_APPLICATION_NAME].execute(text("SELECT now()")).scalar_one()
        return proof

    rec, exc = run_refused(case, in_snapshot=change_while_s_is_open)
    proof = rec["in_snapshot"]
    record(record_property, "E_freshness", acquired=True, body_entered=True, **base_evidence(rec), commit_proof=proof, exception=type(exc).__name__,
           categories=getattr(exc, "categories", None))
    assert proof["insert_granted_visible"] and INSERT_GRANTED not in proof["s_acl"]  # committed and fresh-visible; S does not see it
    assert proof["s_now"] == rec["snapshot_transaction_start"]  # S is still in its one original transaction
    assert type(exc) is CatalogChanged and exc.categories == ["acls"]  # the fresh after-endpoint saw it
    assert len({rec["before"].backend_pid, rec["after"].backend_pid, rec["snapshot_pid"], rec["lock_pid"]}) == 4
    assert rec["before"].transaction_start < rec["snapshot_transaction_start"] < proof["committed_at"] < rec["after"].transaction_start


# ---------------------------------------------------------------------------
# F. identity and missing evidence
# ---------------------------------------------------------------------------
def test_f_a_different_source_missing_and_malformed_records_refuse_explicitly(case, record_property):
    other = new_database(case.cluster, case.ledger)
    url, d = case.cluster.backup_url(case.database), designation_for(case.database)
    refusals = {}
    with backup_capture_lock(url, d) as lock:
        expected = SourceIdentity(lock.system_identifier, lock.database_oid, lock.verified_target.database_name)
        before = observe_endpoint(url, d, label="before", expected_source=expected)
        after = observe_endpoint(url, d, label="after", expected_source=expected)
        with pytest.raises(BaseException) as different:
            observe_endpoint(case.cluster.backup_url(other), designation_for(other), label="after", expected_source=expected)
        refusals["different_source"] = (type(different.value).__name__, str(different.value))
        for name, args in {
            "missing_before": (None, after), "missing_after": (before, None),
            "malformed_observation": (before, dataclasses.replace(after, observation=CatalogObservation(None, (), ()))),
            "incomplete_cleanup": (before, dataclasses.replace(after, cleanup_verification="not_verified")),
            "missing_identity": (before, dataclasses.replace(after, source=SourceIdentity(None, after.source.database_oid, after.source.database_name))),
            "swapped": (after, before),
        }.items():
            with pytest.raises(BaseException) as info:
                require_unchanged(*args, expected)
            refusals[name] = (type(info.value).__name__, str(info.value))
        control = require_unchanged(before, after, expected)
    sessions = owned_sessions(case)
    record(record_property, "F_identity_and_missing", acquired=True, body_entered=True, refusals=refusals, control=control.meaning,
           before=endpoint_facts(before), after=endpoint_facts(after), sessions_after=sessions)
    assert refusals["different_source"] == ("CatalogObservationFailed", "the catalog observer is connected to a different source (database_oid, database_name)")
    assert all(kind == "CatalogObservationFailed" for kind, _ in refusals.values())
    assert "missing" in refusals["missing_before"][1] and "missing" in refusals["missing_after"][1]
    assert all("incomplete or malformed" in refusals[n][1] for n in ("malformed_observation", "incomplete_cleanup", "missing_identity", "swapped"))
    assert isinstance(control, NoDifferenceObserved) and all(v == [] for v in sessions.values())


# ---------------------------------------------------------------------------
# G. failure cleanup: connection, query, comparison and cleanup failures; caller-owned sessions untouched
# ---------------------------------------------------------------------------
def test_g_failures_keep_their_primary_error_and_leave_no_observer_session_and_the_callers_session_is_untouched(case, record_property):
    caller_engine = create_engine(case.cluster.backup_url(case.database))
    case.ledger.engines.append(caller_engine)
    events: list = []
    for kind in ("begin", "commit", "rollback"):
        event.listen(caller_engine, kind, lambda conn, kind=kind: events.append(kind))
    caller = caller_engine.connect()
    case.ledger.connections.append(caller)
    caller.begin()
    events.clear()
    caller.execute(text("SET LOCAL lock_timeout = '3s'"))
    caller_pid, caller_start = caller.execute(text("SELECT pg_backend_pid(), now()")).one()

    url, d = case.cluster.backup_url(case.database), designation_for(case.database)
    outcomes = {}
    with backup_capture_lock(url, d) as lock:
        expected = SourceIdentity(lock.system_identifier, lock.database_oid, lock.verified_target.database_name)
        with pytest.raises(BaseException) as conn_fail:  # connection failure
            observe_endpoint(url.set(password="not-the-backup-password"), d, label="before", expected_source=expected)
        outcomes["connection"] = (type(conn_fail.value).__name__, str(conn_fail.value), owned_sessions(case)[OBSERVER_APPLICATION_NAME])

        observer_pids: list = []

        def failing_verifier(u, app):  # cleanup failure: the observation completes, its cleanup cannot be confirmed
            from src.deployment.backup_capture_lock import _build_engine
            if app == VERIFIER_APPLICATION_NAME:
                raise RuntimeError("injected verifier failure")
            engine = _build_engine(u, app)
            event.listen(engine, "connect", lambda dbapi, r: observer_pids.append(dbapi.info.backend_pid))
            return engine

        with pytest.raises(BaseException) as cleanup_fail:
            observe_endpoint(url, d, label="before", expected_source=expected, _engine_factory=failing_verifier)
        outcomes["cleanup"] = (type(cleanup_fail.value).__name__, str(cleanup_fail.value), case.admin.gone(observer_pids))
    rec, query_exc = run_refused(case, before_after_observation=queued_exclusive_request)  # query failure (lock timeout)
    outcomes["query"] = (type(query_exc).__name__, str(query_exc))
    rec_b, comparison_exc = run_refused(case, during_dump=lambda: committed(case, REVOKE if INSERT_GRANTED in admin_scalar(case.admin, ACL_PROBE) else GRANT))
    outcomes["comparison"] = (type(comparison_exc).__name__, str(comparison_exc), rec_b.get("normal_path_entered"))

    same_txn = caller.execute(text("SELECT now()")).scalar_one() == caller_start
    setting = caller.execute(text("SELECT current_setting('lock_timeout')")).scalar_one()
    state = case.admin.session_row(caller_pid)
    sessions = owned_sessions(case)
    record(record_property, "G_failure_cleanup", acquired=True, body_entered=True, outcomes=outcomes, caller_same_transaction=same_txn,
           caller_setting=setting, caller_state=state, caller_events=events, sessions_after=sessions)
    assert outcomes["connection"][:2] == ("CatalogObservationFailed", "the catalog observer could not connect (OperationalError)")
    assert outcomes["connection"][2] == [] and "not-the-backup-password" not in outcomes["connection"][1]
    assert outcomes["cleanup"][0] == "CatalogObservationFailed" and "verification=not_verified" in outcomes["cleanup"][1]
    assert outcomes["cleanup"][2]  # the server shows the observer gone, but the module still refused to claim it
    assert outcomes["query"] == ("CatalogObservationFailed", "the catalog observation did not complete (OperationalError)")
    assert outcomes["comparison"][0] == "CatalogChanged" and outcomes["comparison"][2] is False
    assert same_txn and setting == "3s" and events == [] and state[2] == "idle in transaction"
    assert all(v == [] for v in sessions.values())
    caller.rollback()
