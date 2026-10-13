"""
Phase 9B-6I: the guarded capture coordinator against a disposable PostgreSQL 16 cluster (synthetic data, the 9B-2 grants).

`run_guarded_capture` runs UNCHANGED through the real preflight, lock, catalog observer, snapshot, dump, bridge, reservation
and writer. The client is the production `LocalClientHost` with real PostgreSQL 16 tools: these tests need
`NAPSEDGE_TEST_CLIENT=local` (the accepted Linux runner, 13.15). Under the default container transport they are SKIPPED,
with the reason stated: the coordinator preserves the archive through the local filesystem -- correct for the production
`LocalClientHost` -- while the test-only container transport keeps the archive inside the cluster container.

Seams are test-side only: a `FileSystemOps` subclass whose `checkpoint` hooks act on the database at the coordinator's own
named points ("before_snapshot": after the before-observation, before S; "staged": inside the dump context, after the
archive copy). The `case` fixture's teardown verifies that no owned session, lock holder, client process or dump backend
remains. Published artifacts and deliberate failed partials are expected retained outputs (removed with the case).
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from src.deployment.backup_artifact import RepositoryState, ToolingInfo, verify_artifact_directory
from src.deployment.backup_artifact_writer import FileSystemOps, WriteFailed
from src.deployment.backup_capture_catalog import OBSERVER_APPLICATION_NAME, CatalogChanged, SourceIdentity
from src.deployment.backup_capture_coordinator import CaptureRequest, PublishedCapture, SkippedOverlap, run_guarded_capture
from src.deployment.backup_capture_dump import DUMP_VERIFIER_APPLICATION_NAME, DumpPreconditionError, LocalClientHost
from src.deployment.backup_capture_lock import LOCK_APPLICATION_NAME, VERIFIER_APPLICATION_NAME, backup_capture_lock
from src.deployment.backup_capture_snapshot import SNAPSHOT_APPLICATION_NAME
from tests.postgres.capture_client_support import native_mode, production_client
from tests.postgres.test_postgres_backup_capture_lock import designation_for, harness  # noqa: F401  (harness: module fixture)
from tests.postgres.test_postgres_backup_capture_roundtrip import case  # noqa: F401  (case: fixture)
from tests.postgres.test_postgres_backup_capture_snapshot import admin_scalar
from tests.postgres.test_postgres_roles_and_restore import ACCEPTED_ALEMBIC_HEAD, RUNTIME_ROLE

pytestmark = [pytest.mark.postgres,
              pytest.mark.skipif(not native_mode(), reason="transport-specific: the coordinator preserves the archive through the LOCAL filesystem "
                                                           "(production LocalClientHost); the test-only container transport keeps it inside the "
                                                           "cluster container. Run with NAPSEDGE_TEST_CLIENT=local on the Linux runner.")]

OWNED_APPS = (LOCK_APPLICATION_NAME, VERIFIER_APPLICATION_NAME, SNAPSHOT_APPLICATION_NAME, DUMP_VERIFIER_APPLICATION_NAME, OBSERVER_APPLICATION_NAME)
GRANT = f"GRANT INSERT ON public.conferences TO {RUNTIME_ROLE}"


class Hooks(FileSystemOps):
    """The production operations, plus test-side actions at the coordinator's named checkpoints."""

    def __init__(self, **hooks) -> None:
        self.hooks, self.seen = hooks, []

    def checkpoint(self, name, path):
        self.seen.append(name)
        if name in self.hooks:
            self.hooks[name](path)


GIT_REVISION = "373b35452bb47f71d5f85cbca1e0624a9e4139f3"  # caller-supplied job fact (the runner has no git); the tree is dirty


def expected_source(case) -> SourceIdentity:
    """The test's own administrator view of the source (truth for the comparison, never given to the capture as evidence)."""
    return SourceIdentity(str(admin_scalar(case.admin, "SELECT system_identifier FROM pg_control_system()")),
                          int(admin_scalar(case.admin, "SELECT oid FROM pg_database WHERE datname = current_database()")), case.database)


@pytest.fixture()
def store(case, monkeypatch):
    root = case.host_dir / "coordinator"
    (root / "store").mkdir(parents=True)
    (root / "systemp").mkdir()
    import src.deployment.backup_capture_coordinator as coordinator

    monkeypatch.setattr(coordinator.tempfile, "tempdir", str(root / "systemp"))  # where the coordinator's private scratch lives, observable here
    return root


def request_for(case, store: Path, **overrides) -> CaptureRequest:
    fields = dict(store=str(store / "store"), database_url=case.cluster.backup_url(case.database), designation=designation_for(case.database),
                  expected_source=expected_source(case), expected_alembic_head=ACCEPTED_ALEMBIC_HEAD, logical_name="napsedge",
                  artifact_id=str(uuid.uuid4()), repository=RepositoryState(GIT_REVISION, True), tooling=ToolingInfo("napsedge-backup", "0.0.0-test"))
    fields.update(overrides)
    return CaptureRequest(**fields)


def leftovers(case, store: Path) -> dict:
    return {"owned_sessions": {app: case.admin.sessions(app) for app in OWNED_APPS}, "lock_holders": case.admin.holders(case.key),
            "client_processes": case.client.client_processes(), "scratch": sorted(os.listdir(store / "systemp"))}


def assert_nothing_left(case, store: Path) -> dict:
    found = leftovers(case, store)
    assert all(v == [] for v in found["owned_sessions"].values()) and found["lock_holders"] == [], found
    assert found["client_processes"] == [] and found["scratch"] == [], found
    return found


def evidence(record_property, scenario: str, **facts) -> None:
    record_property("evidence", json.dumps({"scenario": scenario, "transport": "local", **facts}, sort_keys=True, default=str))


def failed_reason(store: Path, request: CaptureRequest):
    record = store / "store" / f".partial-{request.artifact_id}" / "FAILED"
    return json.loads(record.read_bytes())["reason_class"] if record.exists() else None


def terminate(case, application_name: str) -> list:
    engine = case.cluster.admin_engine(case.database)
    try:
        with engine.begin() as conn:
            # the victims are fixed FIRST (a materialized CTE), so pg_terminate_backend can only ever be applied to them
            return [r[0] for r in conn.execute(text("WITH victims AS MATERIALIZED (SELECT pid FROM pg_stat_activity WHERE application_name = :a "
                                                    "AND datname = current_database()) SELECT pid, pg_terminate_backend(pid) FROM victims"),
                                               {"a": application_name}) if r[1]]
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
def test_a_a_real_capture_publishes_a_verified_v2_artifact_and_leaves_nothing_running(case, store, record_property):
    request = request_for(case, store)
    hooks = Hooks()
    result = run_guarded_capture(request, client=production_client(case.client), ops=hooks)
    assert type(result) is PublishedCapture and type(production_client(case.client)) is LocalClientHost
    verdict = verify_artifact_directory(result.written.path)
    assert verdict.valid, [str(r) for r in verdict.reasons]
    m = verdict.manifest
    assert m.format == "napsedge.backup-artifact/2" and (m.consistency.foreign_write_sessions_start, m.consistency.foreign_write_sessions_end) == (None, None)
    assert (m.source.system_identifier, m.source.database_oid, m.source.database_name) == tuple(vars(request.expected_source).values())
    assert m.source.backup_role == request.database_url.username and m.source.pg_dump_version.startswith("pg_dump (PostgreSQL) 16")
    assert m.schema.alembic_head == ACCEPTED_ALEMBIC_HEAD and m.consistency.snapshot_unchanged_at_end is True
    assert m.consistency.catalog_observation == result.observation and m.created_utc <= verdict.baseline.captured_utc <= m.finished_utc
    assert result.written.archive_sha256 == result.archive_sha256 and Path(result.written.path, "archive.dump").stat().st_size == result.archive_bytes
    assert hooks.seen == ["before_snapshot", "staged", "contexts_closed", "before_finalize", "before_validation", "before_publication"],         "the writer's own steps follow the context exits"
    published = Path(result.written.path, "archive.dump").read_bytes()
    assert hashlib.sha256(published).hexdigest() == result.archive_sha256 and len(published) == result.archive_bytes, "exactly the preserved bytes"
    assert all(report.confirmed for report in result.cleanup.values())
    left = assert_nothing_left(case, store)
    evidence(record_property, "A_success", artifact=os.path.basename(result.written.path), archive_bytes=result.archive_bytes,
             toc_entries=m.archive.toc_entries, observation_pids=[e.backend_pid for e in result.observation.endpoints], leftovers=left)


def test_b_overlap_between_real_jobs_skips_and_creates_nothing(case, store, record_property):
    request = request_for(case, store)
    with backup_capture_lock(case.cluster.backup_url(case.database), designation_for(case.database)) as other_job:
        result = run_guarded_capture(request, client=production_client(case.client))
        holders = case.admin.holders(case.key)
    assert type(result) is SkippedOverlap and result.outcome == "SKIPPED:overlap"
    assert os.listdir(store / "store") == [] and os.listdir(store / "systemp") == []
    assert [h for h in holders if other_job.backend_pid in h], "the other job held the lock throughout"
    left = assert_nothing_left(case, store)
    evidence(record_property, "B_overlap", outcome=result.outcome, leftovers=left)


def test_c_a_persistent_catalog_change_is_caught_and_recorded(case, store, record_property):
    request = request_for(case, store)

    def grant(_path):
        engine = create_engine(case.cluster.migrator_url(case.database))
        try:
            with engine.begin() as conn:
                conn.execute(text("SET LOCAL lock_timeout = '5s'"))
                conn.execute(text(GRANT))
        finally:
            engine.dispose()

    with pytest.raises(CatalogChanged) as info:
        run_guarded_capture(request, client=production_client(case.client), ops=Hooks(before_snapshot=grant))
    assert "acls" in info.value.categories and failed_reason(store, request) == "catalog_changed"
    assert os.listdir(store / "store") == [f".partial-{request.artifact_id}"]
    left = assert_nothing_left(case, store)
    evidence(record_property, "C_catalog_change", categories=info.value.categories, failed=failed_reason(store, request), leftovers=left)


def test_d_a_real_dump_failure_is_recorded_and_publishes_nothing(case, store, record_property):
    request = request_for(case, store)
    missing = LocalClientHost(pg_dump=("/nonexistent/pg_dump",), pg_restore=("/usr/lib/postgresql/16/bin/pg_restore",))
    with pytest.raises(DumpPreconditionError) as info:
        run_guarded_capture(request, client=missing)
    assert "could not be started" in str(info.value) and failed_reason(store, request) == "dump_failed"
    assert os.listdir(store / "store") == [f".partial-{request.artifact_id}"]
    left = assert_nothing_left(case, store)
    evidence(record_property, "D_dump_failure", error=type(info.value).__name__, failed=failed_reason(store, request), leftovers=left)


@pytest.mark.parametrize("victim, reason", [(SNAPSHOT_APPLICATION_NAME, "snapshot_unavailable"), (LOCK_APPLICATION_NAME, "internal_error")])
def test_e_losing_s_or_l_before_the_contexts_exit_prevents_publication(case, store, record_property, victim, reason):
    request = request_for(case, store)
    killed = {}

    def kill(_path):
        killed["pids"] = terminate(case, victim)

    with pytest.raises(BaseException) as info:
        run_guarded_capture(request, client=production_client(case.client), ops=Hooks(staged=kill))
    assert killed["pids"], f"the {victim} session existed at the staged checkpoint"
    assert failed_reason(store, request) == reason, (type(info.value).__name__, failed_reason(store, request))
    assert os.listdir(store / "store") == [f".partial-{request.artifact_id}"], "nothing was published"
    left = assert_nothing_left(case, store)
    evidence(record_property, f"E_loss_{victim}", error=type(info.value).__name__, failed=failed_reason(store, request), leftovers=left)


def test_f_a_same_size_substitution_after_the_scratch_recheck_is_rejected_before_publication(case, store, record_property):
    """The real capture, with the coordinator's preserved archive swapped for same-size bytes after its own re-check and before the
    writer copies it (the `before_finalize` checkpoint): the writer's expected-archive guard rejects the STAGED bytes."""
    request = request_for(case, store)

    def substitute(path):
        data = bytearray(Path(path).read_bytes())
        data[len(data) // 2] ^= 0xFF
        Path(path).write_bytes(bytes(data))

    with pytest.raises(WriteFailed) as info:
        run_guarded_capture(request, client=production_client(case.client), ops=Hooks(before_finalize=substitute))
    error = info.value
    assert (error.reason_class, error.stage, error.published, error.failed_record) == ("checksum_mismatch", "archive", "no", "recorded")
    assert error.__cause__.codes == ("FILE_DIGEST_MISMATCH",)
    partial_dir = store / "store" / f".partial-{request.artifact_id}"
    assert os.listdir(store / "store") == [partial_dir.name], "no final artifact directory was published"
    assert sorted(os.listdir(partial_dir)) == ["FAILED", "archive.dump"] and failed_reason(store, request) == "checksum_mismatch"
    left = assert_nothing_left(case, store)
    evidence(record_property, "F_substitution_rejected", error=type(error).__name__, codes=list(error.__cause__.codes), failed=failed_reason(store, request),
             partial=sorted(os.listdir(partial_dir)), leftovers=left)
