"""
Phase 9B-6I: regression tests for the guarded capture coordinator (`src/deployment/backup_capture_coordinator.py`).

The reservation, writer, verifier, bridge (`catalog_observation_record`, `capture_failure_reason`) and `require_unchanged`
are the REAL production code, writing real files. Only the four database-facing components the coordinator composes
(lock, catalog observer, snapshot, dump) are replaced by recording fakes that yield the production result types
(`BackupLockSession`, `CatalogEndpoint`, `SnapshotHandle`, `DumpArchive`) and that can fail at entry or exit exactly as
the real modules do. Outcomes are judged by the files on disk and the public verifier, not by mocked calls. The real
components run in tests/postgres/test_postgres_backup_capture_coordinator.py.
"""
from __future__ import annotations

import ast
import contextlib
import copy
import datetime as dt
import errno
import hashlib
import json
import os
import traceback
from pathlib import Path

import pytest

import src.deployment.backup_capture_coordinator as coord
from src.deployment.backup_artifact import BaselineDocument, RepositoryState, ToolingInfo, parse_manifest, verify_artifact_directory
from src.deployment.backup_artifact_writer import DurabilityMode, PostPublicationError, PreconditionError, WriteFailed
from src.deployment.backup_capture_catalog import CatalogChanged, CatalogObservation, CatalogObservationFailed, SourceIdentity
from src.deployment.backup_capture_coordinator import (
    CaptureRequest,
    PublishedCapture,
    PublishedCaptureIncomplete,
    SkippedOverlap,
    StagedArchiveMismatch,
    run_guarded_capture,
)
from src.deployment.backup_capture_dump import DumpArchive, DumpCleanupError, DumpCleanupReport, DumpFailed, DumpTimeout
from src.deployment.backup_capture_lock import BackupLockCleanupError, BackupLockNotHeld, BackupLockSession, BackupLockSetupError, LockCleanupReport
from src.deployment.backup_capture_snapshot import SnapshotCleanupError, SnapshotCleanupReport, SnapshotFacts, SnapshotHandle, SnapshotSessionLost
from src.exceptions import AdvisoryLockUnavailableError, DatabaseTargetVerificationError, ValidationError
from src.pipeline.bulk_import_safety import DatabaseTargetDesignation, VerifiedDatabaseTarget, build_expected_acknowledgement
from tests.regression.test_deployment_backup_artifact import baseline_doc
from tests.regression.test_deployment_backup_artifact_v2 import OBSERVATION, T_AFTER_BACKEND, T_BEFORE_BACKEND, endpoint, names, outcome_of
from tests.regression.test_deployment_backup_artifact_writer import Probe

REPO_ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_ID = "3f2b8c1e-6a47-4d0e-9b53-1c7a2d9e5f08"
SYSID, OID, DBNAME, HEAD = "7550123456789012345", 16384, "napsedge", "5b02bea0eb85"
SECRET = "hunter2-planted-secret"
URL_TEXT = f"postgresql+psycopg://napsedge_backup_ro:{SECRET}@localhost:5432/napsedge?sslmode=verify-full&sslrootcert=/run/ca.pem"
ARCHIVE = b"PGDMP synthetic archive bytes for the coordinator tests\n" * 512
SOURCE = SourceIdentity(SYSID, OID, DBNAME)
UTC = dt.timezone.utc


def designation():
    return DatabaseTargetDesignation(DBNAME, build_expected_acknowledgement(DBNAME))


def make_request(store: Path, **overrides) -> CaptureRequest:
    fields = dict(store=str(store), database_url=URL_TEXT, designation=designation(), expected_source=SOURCE, expected_alembic_head=HEAD,
                  logical_name="napsedge", artifact_id=ARTIFACT_ID, repository=RepositoryState("373b35452bb47f71d5f85cbca1e0624a9e4139f3", True),
                  tooling=ToolingInfo("napsedge-backup", "0.0.0-test"), durability=DurabilityMode.REQUIRED)
    fields.update(overrides)
    return CaptureRequest(**fields)


class Client:
    """Satisfies the ClientHost attribute check; the fake dump never uses it (the real dump does, in the postgres tests)."""

    pg_dump, pg_restore = ("pg_dump",), ("pg_restore",)

    def __getattr__(self, name):
        if name in coord._CLIENT_OPERATIONS:
            return lambda *a, **k: None
        raise AttributeError(name)


# --------------------------------------------------------------------------- the recording fakes
class World:
    """The fake database side. `log` is the order of events; each fake checks what must (not) exist when it runs."""

    def __init__(self, tmp: Path, store: Path) -> None:
        self.tmp, self.store, self.log = tmp, store, []
        self.lock_active = self.snapshot_active = self.dump_active = False
        self.fail: dict = {}
        self.after_observation = OBSERVATION
        self.lock_facts = (SYSID, OID, DBNAME)
        self.archive_bytes_reported = len(ARCHIVE)

    def published(self) -> bool:
        return any(not n.startswith(".partial-") for n in os.listdir(self.store))

    def raise_if(self, point):
        error = self.fail.get(point)
        if error is not None:
            raise error

    @contextlib.contextmanager
    def lock(self, url, designation_):
        self.log.append("lock_enter")
        self.raise_if("lock_enter")
        session = BackupLockSession(VerifiedDatabaseTarget(self.lock_facts[2], "PostgreSQL 16.4"), 42, 501, T_BEFORE_BACKEND,
                                    system_identifier=self.lock_facts[0], database_oid=self.lock_facts[1], confirm=lambda: None)
        self.lock_active = True
        try:
            yield session
        finally:  # the real module always ends and verifies L, on every path
            self.log.append(f"lock_exit(published={self.published()})")
            self.lock_active = False
            session.cleanup = LockCleanupReport(501, True, False, True, True, "closed_and_released", False, ())
        self.raise_if("lock_exit")  # an unconfirmed cleanup with no primary failure raises after the body returned

    def observe(self, url, designation_, *, label, expected_source):
        self.log.append(f"observe:{label}(snapshot={self.snapshot_active}, lock={self.lock_active}, reserved={self.reserved()})")
        self.raise_if(f"observe_{label}")
        if label == "before":
            return endpoint("before", 811, T_BEFORE_BACKEND, dt.datetime.now(UTC))
        start = dt.datetime.now(UTC)
        return endpoint("after", 812, start, start + dt.timedelta(milliseconds=1), observation=self.after_observation)

    def reserved(self) -> bool:
        return (self.store / f".partial-{ARTIFACT_ID}").is_dir()

    @contextlib.contextmanager
    def snapshot(self, lock, url, designation_, *, expected_alembic_head, artifact_id):
        self.log.append("snapshot_enter")
        self.raise_if("snapshot_enter")
        document = baseline_doc()
        document.update(artifact_id=artifact_id, captured_utc=dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
        facts = SnapshotFacts(601, T_BEFORE_BACKEND, T_BEFORE_BACKEND, "repeatable read", "on", SYSID, OID, DBNAME, 160004, 501)
        handle = SnapshotHandle("00000003-00000002-1", "748:748:", BaselineDocument.from_document(document), None, facts, confirm=lambda: None)
        self.snapshot_active = True
        try:
            yield handle
        finally:
            self.log.append(f"snapshot_exit(published={self.published()})")
            self.snapshot_active = False
            handle._open = False
            handle.cleanup = SnapshotCleanupReport(601, True, False, True, True, "closed_and_released", ())
        self.raise_if("snapshot_exit")

    @contextlib.contextmanager
    def dump(self, handle, *, client):
        self.log.append("dump_enter")
        handle.snapshot_id  # a closed handle raises here, as for the real dump
        self.raise_if("dump_enter")
        workdir = self.tmp / "dump_workdir"
        workdir.mkdir()
        path = workdir / "archive.dump"
        path.write_bytes(ARCHIVE)
        archive = DumpArchive(str(path), archive_bytes=self.archive_bytes_reported, toc_entries=275, toc_sha256=hashlib.sha256(b"toc").hexdigest(),
                              pg_dump_version="pg_dump (PostgreSQL) 16.15", pg_restore_version="pg_restore (PostgreSQL) 16.15",
                              application_name="napsedge_backup_dump_x", snapshot_id=handle.snapshot_id, returncode=0, inventory={})
        self.dump_active = True
        try:
            yield archive
        finally:
            self.log.append(f"dump_exit(published={self.published()})")
            self.dump_active = False
            archive._open = False
            path.unlink()
            workdir.rmdir()
            archive.cleanup = DumpCleanupReport(True, False, True, "gone", False, True, True, ())
        self.raise_if("dump_exit")


@pytest.fixture
def world(tmp_path, monkeypatch):
    store, temp = tmp_path / "store", tmp_path / "systemp"
    store.mkdir()
    temp.mkdir()
    monkeypatch.setattr(coord.tempfile, "tempdir", str(temp))
    w = World(tmp_path, store)
    w.temp = temp
    monkeypatch.setattr(coord, "backup_capture_lock", w.lock)
    monkeypatch.setattr(coord, "observe_endpoint", w.observe)
    monkeypatch.setattr(coord, "backup_capture_snapshot", w.snapshot)
    monkeypatch.setattr(coord, "backup_capture_dump", w.dump)
    return w


def capture(world, probe=None, **overrides):
    return outcome_of(lambda: run_guarded_capture(make_request(world.store, **overrides), client=Client(), ops=probe if probe is not None else Probe()))


def partial(world) -> Path:
    return world.store / f".partial-{ARTIFACT_ID}"


def failed_reason(world):
    record = partial(world) / "FAILED"
    return json.loads(record.read_bytes())["reason_class"] if record.exists() else None


def scratch_entries(world):
    return sorted(os.listdir(world.temp))


def store_bytes(world) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(world.store.rglob("*")) if p.is_file())


EXPECTED_ORDER = ["lock_enter", "observe:before(snapshot=False, lock=True, reserved=True)", "snapshot_enter", "dump_enter",
                  "observe:after(snapshot=True, lock=True, reserved=True)", "dump_exit(published=False)", "snapshot_exit(published=False)",
                  "lock_exit(published=False)"]


# =========================================================================== success
def test_a_successful_capture_follows_the_accepted_order_and_publishes_a_verified_v2_artifact(world):
    probe = Probe()
    kind, result = capture(world, probe)
    assert kind == "returned" and type(result) is PublishedCapture, result
    assert world.log == EXPECTED_ORDER, "before-observation before S; after-observation while S and L are active; publication after all exits"
    final = Path(result.written.path)
    assert names(world.store) == [final.name] and final.name.endswith(ARTIFACT_ID)
    verdict = verify_artifact_directory(final)
    assert verdict.valid, [str(r) for r in verdict.reasons]
    manifest = verdict.manifest
    assert manifest.format == "napsedge.backup-artifact/2"
    c = manifest.consistency
    assert (c.mode, c.snapshot_descriptor, c.snapshot_unchanged_at_end, c.sequence_rule, c.ddl_exclusion) == ("exported_snapshot", "748:748:", True, "monotonic_v1", "none")
    assert (c.foreign_write_sessions_start, c.foreign_write_sessions_end) == (None, None), "not sampled: null, never an invented zero"
    assert c.catalog_observation == result.observation and [e.backend_pid for e in c.catalog_observation.endpoints] == [811, 812]
    s = manifest.source
    assert (s.system_identifier, s.database_oid, s.database_name, s.server_version_num, s.pg_dump_version, s.backup_role) == (
        SYSID, OID, DBNAME, 160004, "pg_dump (PostgreSQL) 16.15", "napsedge_backup_ro")
    assert (manifest.archive.toc_entries, manifest.archive.toc_sha256) == (275, hashlib.sha256(b"toc").hexdigest())
    assert (final / "archive.dump").read_bytes() == ARCHIVE and result.archive_sha256 == hashlib.sha256(ARCHIVE).hexdigest() == result.written.archive_sha256
    assert manifest.created_utc <= verdict.baseline.captured_utc <= manifest.finished_utc
    assert scratch_entries(world) == [], "the private scratch directory was removed"
    assert ("mkdir_private", next(e[1] for e in probe.events if e[0] == "mkdir_private" and e[1].startswith("napsedge_capture_"))) in probe.events
    assert SECRET.encode() not in store_bytes(world)
    assert "restore_verified" in result.does_not_establish and "recovery_readiness" in result.does_not_establish


def test_a_snapshot_and_dump_handles_are_not_used_after_their_contexts(world):
    kind, result = capture(world)
    assert kind == "returned", result  # every accessor of a closed SnapshotHandle/DumpArchive raises; reaching here proves none was used late


# =========================================================================== overlap and pre-reservation failures
def test_b_overlap_returns_skipped_and_creates_no_reservation_or_scratch(world):
    world.fail["lock_enter"] = AdvisoryLockUnavailableError("another job holds the lock")
    kind, result = capture(world)
    assert kind == "returned" and type(result) is SkippedOverlap and result.outcome == "SKIPPED:overlap"
    assert names(world.store) == [] and scratch_entries(world) == [] and world.log == ["lock_enter"]


@pytest.mark.parametrize("override, fragment", [
    ({"artifact_id": "not-a-uuid"}, "artifact contract"), ({"logical_name": "Bad/Name"}, "artifact contract"),
    ({"expected_alembic_head": ""}, "artifact contract"), ({"repository": RepositoryState("abc", False)}, "artifact contract"),
    ({"database_url": "postgresql+psycopg://u:p@localhost/napsedge?sslmode=require"}, ""), ({"durability": "required"}, "DurabilityMode"),
    ({"expected_source": (SYSID, OID, DBNAME)}, "SourceIdentity"),
])
def test_c_invalid_requests_are_refused_before_any_resource(world, override, fragment):
    kind, error = capture(world, **override)
    assert kind == "raised" and isinstance(error, ValidationError) and fragment in str(error)
    assert world.log == [] and names(world.store) == [] and scratch_entries(world) == []
    assert SECRET not in str(error)


def test_c_a_lock_setup_failure_propagates_with_no_artifact_record(world):
    primary = BackupLockSetupError("the lock session could not be opened (OperationalError)")
    world.fail["lock_enter"] = primary
    kind, error = capture(world)
    assert kind == "raised" and error is primary and names(world.store) == [] and scratch_entries(world) == []


def test_c_a_reservation_refusal_propagates_with_no_artifact_record(world):
    (world.store / f".partial-{ARTIFACT_ID}").mkdir()
    (world.store / f".partial-{ARTIFACT_ID}" / "owner.txt").write_text("someone else")
    kind, error = capture(world)
    assert kind == "raised" and type(error) is PreconditionError and "PARTIAL_EXISTS" in str(error)
    assert names(partial(world)) == ["owner.txt"], "the existing partial is not touched and receives no FAILED record"
    assert world.log == ["lock_enter", "lock_exit(published=False)"] and scratch_entries(world) == []


# =========================================================================== reserved, before finalization: one mapped FAILED record
def _changed_observation():
    return CatalogObservation(OBSERVATION.constraints, (), OBSERVATION.acls)


FAILURES = [
    # id, (world mutation), expected exception type, expected FAILED class
    ("catalog_difference", lambda w: setattr(w, "after_observation", _changed_observation()), CatalogChanged, "catalog_changed"),
    ("observation_timeout", lambda w: w.fail.update(observe_after=CatalogObservationFailed("the catalog observation did not complete (QueryCanceled)")),
     CatalogObservationFailed, "internal_error"),
    ("before_observation_fails", lambda w: w.fail.update(observe_before=CatalogObservationFailed("the catalog observer could not connect (OperationalError)")),
     CatalogObservationFailed, "internal_error"),
    ("source_mismatch", lambda w: setattr(w, "lock_facts", ("1234", OID, DBNAME)), DatabaseTargetVerificationError, "source_identity_mismatch"),
    ("dump_failure", lambda w: w.fail.update(dump_enter=DumpFailed("pg_dump exited with status 1", stage="dump", returncode=1, diagnostic=SECRET)),
     DumpFailed, "dump_failed"),
    ("dump_deadline", lambda w: w.fail.update(dump_enter=DumpTimeout("pg_dump exceeded its deadline and was stopped", stage="dump")), DumpTimeout, "timeout"),
    ("snapshot_session_lost", lambda w: w.fail.update(dump_enter=SnapshotSessionLost("S is no longer the same backend")), SnapshotSessionLost, "snapshot_unavailable"),
    ("lock_lost_at_snapshot_exit", lambda w: w.fail.update(snapshot_exit=BackupLockNotHeld("L no longer holds the key")), BackupLockNotHeld, "internal_error"),
    ("dump_cleanup_unconfirmed", lambda w: w.fail.update(dump_exit=DumpCleanupError("dump cleanup was not confirmed", None)), DumpCleanupError, "internal_error"),
    ("snapshot_cleanup_unconfirmed", lambda w: w.fail.update(snapshot_exit=SnapshotCleanupError("snapshot cleanup was not confirmed", None)),
     SnapshotCleanupError, "internal_error"),
    ("lock_cleanup_unconfirmed", lambda w: w.fail.update(lock_exit=BackupLockCleanupError("lock cleanup was not confirmed", None)), BackupLockCleanupError, "internal_error"),
    ("dump_size_differs", lambda w: setattr(w, "archive_bytes_reported", len(ARCHIVE) + 1), StagedArchiveMismatch, "checksum_mismatch"),
    ("interrupted_dump", lambda w: w.fail.update(dump_enter=KeyboardInterrupt()), KeyboardInterrupt, "interrupted"),
]


@pytest.mark.parametrize("case, mutate, error_type, reason", FAILURES, ids=[f[0] for f in FAILURES])
def test_d_a_failure_while_reserved_records_one_mapped_failed_and_never_publishes(world, case, mutate, error_type, reason):
    mutate(world)
    planted = next(iter(world.fail.values()), None)
    kind, error = capture(world)
    assert kind == "raised" and type(error) is error_type, f"{case}: {kind} {type(error).__name__}"
    if planted is not None:
        assert error is planted, "the original exception object propagates"
    assert failed_reason(world) == reason and names(partial(world)) == ["FAILED"], "one FAILED record; no payload, no COMPLETE"
    assert names(world.store) == [partial(world).name] and verify_artifact_directory(partial(world)).codes == ("NOT_FINAL",)
    assert scratch_entries(world) == [] and SECRET.encode() not in store_bytes(world)
    assert all("published=True" not in entry for entry in world.log), "nothing was published before or during the context exits"


def test_d_exit_failures_prevent_finalization_and_the_contexts_still_close_in_order(world):
    world.fail["dump_exit"] = DumpCleanupError("dump cleanup was not confirmed", None)
    capture(world)
    assert world.log[-3:] == ["dump_exit(published=False)", "snapshot_exit(published=False)", "lock_exit(published=False)"]


# =========================================================================== staged archive bytes
def test_e_an_archive_copy_failure_is_recorded_and_the_scratch_is_removed(world):
    primary = OSError(errno.ENOSPC, SECRET)
    kind, error = capture(world, Probe(fail={("write", "archive.copy"): primary}))
    assert kind == "raised" and error is primary and failed_reason(world) == "internal_error"
    assert scratch_entries(world) == [] and names(world.store) == [partial(world).name]


@pytest.mark.parametrize("checkpoint", ["staged", "contexts_closed"])
def test_e_staged_byte_substitution_is_caught_before_finalization(world, checkpoint):
    def tamper(path):
        data = bytearray(Path(path).read_bytes())
        data[100] ^= 0xFF  # same size, different bytes
        Path(path).write_bytes(bytes(data))

    kind, error = capture(world, Probe(hooks={checkpoint: tamper}))
    assert kind == "raised" and type(error) is StagedArchiveMismatch and failed_reason(world) == "checksum_mismatch"
    assert names(world.store) == [partial(world).name] and scratch_entries(world) == []


# =========================================================================== recording failures, finalization, publication
def test_f_a_failed_recording_keeps_the_original_exception(world):
    primary = DumpFailed("pg_dump exited with status 1", stage="dump", returncode=1)
    world.fail["dump_enter"] = primary
    probe = Probe(fail={("open_exclusive", "FAILED"): OSError(errno.ENOSPC, SECRET)})
    try:
        run_guarded_capture(make_request(world.store), client=Client(), ops=probe)
    except DumpFailed as raised:
        assert raised is primary and raised.__traceback__ is not None and traceback.format_tb(raised.__traceback__)
        assert any("recording the FAILED marker" in n for n in raised.__notes__) and SECRET not in " ".join(raised.__notes__)
    else:
        pytest.fail("the capture did not fail")
    assert names(partial(world)) == ["RUNNING"] and verify_artifact_directory(partial(world)).codes == ("NOT_FINAL",) and scratch_entries(world) == []


def test_f_a_finalization_failure_is_recorded_once_by_the_writer(world):
    probe = Probe(fail={("fsync_file", "manifest.json"): OSError(errno.EIO, SECRET)})
    kind, error = capture(world, probe)
    assert kind == "raised" and type(error) is WriteFailed and error.reason_class == "durability_failed"
    assert [e for e in probe.events if e == ("open_exclusive", "FAILED")] == [("open_exclusive", "FAILED")], "exactly one FAILED record"
    assert failed_reason(world) == "durability_failed" and scratch_entries(world) == [] and names(world.store) == [partial(world).name]


@pytest.mark.parametrize("probe_kwargs, published", [({"rename_then_raise": OSError(errno.EIO, SECRET)}, "yes")])
def test_f_publication_uncertainty_is_surfaced_without_any_failed_record(world, probe_kwargs, published):
    probe = Probe(**probe_kwargs)
    kind, error = capture(world, probe)
    assert kind == "raised" and type(error) is PostPublicationError and error.published == published
    assert ("open_exclusive", "FAILED") not in probe.events and not partial(world).exists()
    final = next(p for p in world.store.iterdir())
    assert verify_artifact_directory(final).valid and scratch_entries(world) == []


def test_f_scratch_cleanup_failure_after_publication_is_neither_failure_nor_clean_success(world):
    probe = Probe(fail={("unlink", "archive.copy"): OSError(errno.EACCES, SECRET)})
    kind, error = capture(world, probe)
    assert kind == "raised" and type(error) is PublishedCaptureIncomplete
    assert (error.problem, error.published) == ("scratch_cleanup_failed", "yes") and verify_artifact_directory(error.written.path).valid
    assert ("open_exclusive", "FAILED") not in probe.events and not partial(world).exists()
    assert len(scratch_entries(world)) == 1 and error.scratch_path and SECRET not in " ".join(error.__notes__)


def test_f_an_interrupt_while_recording_does_not_replace_the_primary(world):
    primary = DumpTimeout("pg_dump exceeded its deadline and was stopped", stage="dump")
    world.fail["dump_enter"] = primary
    kind, error = capture(world, Probe(fail={("write", "FAILED"): KeyboardInterrupt()}))
    assert kind == "raised" and error is primary and any("KeyboardInterrupt" in n for n in error.__notes__)


# =========================================================================== caller-owned resources
def test_g_caller_owned_resources_are_never_touched(world):
    bystander = world.store / "napsedge-other-20261001T000000Z-9a1d3e7f-2c58-4b61-8f04-5d7e6a1b2c93"
    bystander.mkdir()
    (bystander / "COMPLETE").write_text("not ours")
    foreign_scratch = world.temp / "napsedge_capture_foreign"
    foreign_scratch.mkdir()
    (foreign_scratch / "archive.copy").write_bytes(b"not ours")
    request = make_request(world.store)
    request_before = copy.deepcopy(request)
    world.fail["dump_enter"] = DumpFailed("pg_dump exited with status 1", stage="dump", returncode=1)
    with pytest.raises(DumpFailed):
        run_guarded_capture(request, client=Client(), ops=Probe())
    world.fail.clear()
    (partial(world) / "FAILED").unlink()
    (partial(world)).rmdir()  # the test removes the deliberate failed partial; the coordinator never does
    run_guarded_capture(request, client=Client(), ops=Probe())
    assert request == request_before
    assert (bystander / "COMPLETE").read_text() == "not ours" and (foreign_scratch / "archive.copy").read_bytes() == b"not ours"
    assert scratch_entries(world) == ["napsedge_capture_foreign"]


# =========================================================================== static constraints
def test_the_coordinator_reuses_the_accepted_modules_and_never_touches_transport_or_docker():
    source = (REPO_ROOT / "src" / "deployment" / "backup_capture_coordinator.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m.startswith("tests") for m in imported)
    assert not {"subprocess", "docker", "shutil"} & imported
    for forbidden in ("NAPSEDGE_TEST_CLIENT", "docker", "os.environ", "rmtree", "record_failure(", "def main"):
        assert source.count(forbidden) == (1 if forbidden == "record_failure(" else 0), forbidden
    assert sorted(coord.__all__) == sorted(["CaptureRequest", "PublishedCapture", "SkippedOverlap", "StagedArchiveMismatch",
                                            "PublishedCaptureIncomplete", "run_guarded_capture"])


# =========================================================================== the expected-archive guard, end to end (13.19)
def _substitute_same_size(path):
    data = bytearray(Path(path).read_bytes())
    data[len(data) // 2] ^= 0xFF
    Path(path).write_bytes(bytes(data))


def test_h_the_published_archive_has_exactly_the_preserved_digest_and_size(world):
    kind, result = capture(world)
    assert kind == "returned" and type(result) is PublishedCapture
    published = Path(result.written.path, "archive.dump").read_bytes()
    assert hashlib.sha256(published).hexdigest() == result.archive_sha256 and len(published) == result.archive_bytes == len(ARCHIVE)


def test_h_a_substitution_after_the_scratch_recheck_is_rejected_by_the_writer_before_publication(world):
    """Same-size bytes swapped between the coordinator's re-check and the writer's copy: only the digest guard can see it."""
    probe = Probe(hooks={"before_finalize": _substitute_same_size})
    kind, error = capture(world, probe)
    assert kind == "raised" and type(error) is WriteFailed, f"{type(error).__name__}: a post-publication report does not satisfy prevention"
    assert (error.reason_class, error.stage, error.published, error.failed_record) == ("checksum_mismatch", "archive", "no", "recorded")
    assert error.__cause__.codes == ("FILE_DIGEST_MISMATCH",)
    assert names(world.store) == [partial(world).name], "no final artifact directory exists"
    assert names(partial(world)) == ["FAILED", "archive.dump"] and failed_reason(world) == "checksum_mismatch"
    assert [e for e in probe.events if e == ("open_exclusive", "FAILED")] == [("open_exclusive", "FAILED")], "recorded once, by the writer"
    assert scratch_entries(world) == [] and all("published=True" not in entry for entry in world.log)


def test_h_a_rejection_whose_scratch_cleanup_fails_keeps_the_writer_error(world):
    probe = Probe(hooks={"before_finalize": _substitute_same_size}, fail={("unlink", "archive.copy"): OSError(errno.EACCES, SECRET)})
    kind, error = capture(world, probe)
    assert kind == "raised" and type(error) is WriteFailed and error.reason_class == "checksum_mismatch"
    assert any("capture scratch cleanup also failed" in n for n in error.__notes__) and SECRET not in " ".join(error.__notes__)
    assert names(world.store) == [partial(world).name] and len(scratch_entries(world)) == 1


def test_h_a_rejection_whose_failed_record_fails_publishes_nothing(world):
    probe = Probe(hooks={"before_finalize": _substitute_same_size}, fail={("open_exclusive", "FAILED"): OSError(errno.ENOSPC, SECRET)})
    kind, error = capture(world, probe)
    assert kind == "raised" and type(error) is WriteFailed and error.failed_record.startswith("failed: ")
    assert names(world.store) == [partial(world).name] and names(partial(world)) == ["RUNNING", "archive.dump"] and scratch_entries(world) == []
