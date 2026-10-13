"""
Phase 9B-6I: the guarded capture coordinator (docs/backup_artifact_contract_proposal.md sections 13.17 and 13.18).

`run_guarded_capture(request, *, client, ops=None) -> PublishedCapture | SkippedOverlap` composes ONLY the accepted
modules, in the accepted order:

1. validate the request (nothing is created);
2. acquire L (`backup_capture_lock`). A genuine overlap (`AdvisoryLockUnavailableError` from the lock's own entry)
   returns `SkippedOverlap`: no reservation and no scratch directory exist;
3. `reserve_artifact` (the capture start time becomes `created_utc`), then compare L's own source identity with the
   request's expected source;
4. the fresh `before` observation (`observe_endpoint`), BEFORE S starts;
5. S (`backup_capture_snapshot`): the accepted baseline and the exported snapshot;
6. the dump (`backup_capture_dump`, through the caller's `client`; transport is the client's responsibility);
7. the fresh `after` observation while S and L are still active;
8. `require_unchanged`, and the `/2` record derived from that checked result (`catalog_observation_record`);
9. while the dump handle is valid, the exact archive bytes are copied into a private scratch directory this module
   creates; their SHA-256 and size are recorded, and the size must equal the dump's own `archive_bytes`;
10. the dump, snapshot and lock contexts exit, each completing its own cleanup verification (a failure raises);
11. the preserved copy is re-hashed and must match step 9;
12. `reservation.finalize(..., expected_archive_sha256=..., expected_archive_bytes=...)` publishes the `/2` artifact; the
    writer re-reads the archive bytes it staged and refuses (`WriteFailed`, `checksum_mismatch`, recorded once by the writer)
    any difference from the step-9 digest and size BEFORE writing `COMPLETE` (13.19). Nothing is finalized before step 10
    succeeded, and no expired handle is used: every fact needed later is copied out while its context is open. The digest
    comparison after publication remains as an additional check. This covers the bytes as staged and verified; it does not
    protect against arbitrary external mutation of the store afterwards.

Outcomes, by publication state:
* before the reservation: the original error propagates; no artifact failure record exists (overlap is not an error);
* reserved, before `finalize`: ONE `FAILED` record, mapped by `capture_failure_reason` (`internal_error` when it has no
  class; `checksum_mismatch` for a preserved archive that changed), then the SAME exception propagates. A failure of the
  recording never replaces it (the writer's own policy); a new interrupt follows the established cleanup policy;
* `finalize` failing: the writer owns the outcome (it records its own `FAILED` or reports a post-publication outcome);
  the consumed reservation is never recorded twice;
* at or after publication: `PostPublicationError` propagates unchanged ("yes" or "unknown"); nothing is recorded,
  retried or deleted. If the publication succeeded but this module's own scratch cleanup failed, or the published
  archive digest differs from the preserved one, `PublishedCaptureIncomplete` is raised: it carries the published
  artifact, says `published="yes"`, and is neither a failed partial nor a clean success.

Ownership. This module creates and removes exactly one scratch directory (a fresh, exclusively created, private
`napsedge_capture_<random>` in the system temporary directory) and the one file in it. No request field names a path it
deletes. Connections, sessions, processes, passfiles and work directories belong to the lock, snapshot, observer and
dump modules, which verify their own cleanup. The reservation's partial belongs to the writer and is retained on failure.

Manifest facts, and where each comes from (nothing is re-read from the live source after the contexts close):
source identity and `server_version_num` from S's facts; `backup_role` from the verified URL's login role;
`pg_dump_version` and the TOC fields from the `DumpArchive`; `schema.alembic_head` from the baseline's `version`;
`snapshot_descriptor` from the handle; `snapshot_unchanged_at_end` is true only because the snapshot context's own
normal exit re-confirmed S's backend and descriptor; `sequence_rule` `monotonic_v1` and `ddl_exclusion` `none` are the
recorded decisions (section 11); `foreign_write_sessions_*` are null: NOT sampled (13.18). `repository` and `tooling` are
caller-supplied job facts, validated by the contract's patterns.

What a `PublishedCapture` does NOT establish: catalog stability (the observation means only "no difference observed at
these two endpoints"), continuous exclusion (lock checks are point observations), frozen sequences, a successful restore,
or recovery readiness. Messages and notes written here are fixed text plus type names and errno values only.
"""
from __future__ import annotations

import datetime as _dt
import errno
import hashlib
import os
import tempfile
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.engine import URL

from src.deployment.backup_artifact import (
    _GIT_REVISION,
    _IDENTIFIER,
    _REVISION,
    _TOOL_NAME,
    _TOOL_VERSION,
    ArchiveInfo,
    ArtifactFormatError,
    CatalogObservationRecord,
    ConsistencyClaimV2,
    RepositoryState,
    SchemaState,
    SourceInfo,
    ToolingInfo,
    _str,
    parse_artifact_directory_name,
)
from src.deployment.backup_artifact_writer import (
    ArtifactMetadata,
    ArtifactReservation,
    DurabilityMode,
    FileSystemOps,
    WrittenArtifact,
    _close_after_failure,
    reserve_artifact,
)
from src.deployment.backup_capture_artifact import capture_failure_reason, catalog_observation_record
from src.deployment.backup_capture_catalog import SourceIdentity, observe_endpoint, require_unchanged
from src.deployment.backup_capture_dump import backup_capture_dump
from src.deployment.backup_capture_lock import _type_name, _validated_url, backup_capture_lock
from src.deployment.backup_capture_snapshot import backup_capture_snapshot
from src.exceptions import AdvisoryLockUnavailableError, DatabaseTargetVerificationError, NapsEdgeError, ValidationError
from src.pipeline.bulk_import_safety import DatabaseTargetDesignation

__all__ = [
    "CaptureRequest",
    "PublishedCapture",
    "SkippedOverlap",
    "StagedArchiveMismatch",
    "PublishedCaptureIncomplete",
    "run_guarded_capture",
]

SCRATCH_PREFIX = "napsedge_capture_"
_SCRATCH_FILE = "archive.copy"
_CHUNK = 1 << 20
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_CLIENT_OPERATIONS = ("pg_dump", "pg_restore", "make_private_dir", "join", "write_private_file", "file_state", "read_bounded", "remove",
                      "remove_tree", "exists", "localize", "start")


@dataclass(frozen=True)
class CaptureRequest:
    """Everything the caller decides. No field names a path this module deletes; `store` is handed to the writer only."""

    store: str
    database_url: URL | str
    designation: DatabaseTargetDesignation
    expected_source: SourceIdentity
    expected_alembic_head: str
    logical_name: str
    artifact_id: str
    repository: RepositoryState
    tooling: ToolingInfo
    durability: DurabilityMode = DurabilityMode.REQUIRED


@dataclass(frozen=True)
class SkippedOverlap:
    """Another job held the lock. Nothing was reserved, staged or written. Not a failure."""

    artifact_id: str
    outcome: str = "SKIPPED:overlap"


@dataclass(frozen=True)
class PublishedCapture:
    """A published /2 artifact (claim 1 plus the contract's structural check) and the facts its capture recorded."""

    written: WrittenArtifact
    archive_sha256: str
    archive_bytes: int
    observation: CatalogObservationRecord
    cleanup: dict
    establishes: tuple[str, ...] = ("files_complete", "staged_structure_validated", "no_catalog_difference_observed_at_two_endpoints")
    does_not_establish: tuple[str, ...] = ("catalog_stability", "continuous_exclusion", "frozen_sequences", "restore_verified", "recovery_readiness")


class StagedArchiveMismatch(NapsEdgeError):
    """The preserved archive copy is not what the dump produced, or changed before finalization. Recorded as `checksum_mismatch`."""


class PublishedCaptureIncomplete(NapsEdgeError):
    """
    The artifact WAS published (`published == "yes"`, `written` is it), but this module's own follow-up did not complete:
    `problem` is "scratch_cleanup_failed" (the private scratch copy could not be removed; `scratch_path` names it) or
    "archive_digest_mismatch" (the published archive differs from the preserved bytes). Never a failed partial: no FAILED
    record is written and nothing published is deleted. Verify the artifact with the public verifier before use.
    """

    def __init__(self, problem: str, written: WrittenArtifact, *, scratch_path: str | None = None) -> None:
        super().__init__(f"the capture was published but {problem.replace('_', ' ')}; nothing was deleted; verify the artifact before use")
        self.problem, self.written, self.scratch_path, self.published = problem, written, scratch_path, "yes"


def _describe(exc: BaseException) -> str:
    text = _type_name(exc)
    if isinstance(exc, OSError) and exc.errno is not None:
        text += f" (errno {exc.errno})"
    return text


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime(_TIMESTAMP_FORMAT)


def _validate(request: CaptureRequest, client: Any) -> tuple[URL, str]:
    """Narrow, explicit checks that need no resource. Raises ValidationError; creates nothing."""
    if not isinstance(request, CaptureRequest):
        raise ValidationError("request must be a CaptureRequest")
    if not isinstance(request.store, (str, os.PathLike)):
        raise ValidationError("store must be a path")
    if not isinstance(request.designation, DatabaseTargetDesignation) or not isinstance(request.expected_source, SourceIdentity):
        raise ValidationError("a DatabaseTargetDesignation and the expected SourceIdentity are required")
    if not isinstance(request.durability, DurabilityMode):
        raise ValidationError("durability must be a DurabilityMode")
    if not isinstance(request.repository, RepositoryState) or not isinstance(request.tooling, ToolingInfo):
        raise ValidationError("repository and tooling must be RepositoryState and ToolingInfo")
    for name in _CLIENT_OPERATIONS:
        if not hasattr(client, name):
            raise ValidationError("client must provide the ClientHost operations")
    try:
        for value, pattern, where in ((request.expected_alembic_head, _REVISION, "expected_alembic_head"),
                                      (request.repository.git_revision, _GIT_REVISION, "repository.git_revision"),
                                      (request.tooling.name, _TOOL_NAME, "tooling.name"), (request.tooling.version, _TOOL_VERSION, "tooling.version")):
            _str(value, where, pattern=pattern)
        if not isinstance(request.repository.dirty, bool):
            raise ArtifactFormatError("FIELD_TYPE", "repository.dirty")
        if not isinstance(request.logical_name, str) or not isinstance(request.artifact_id, str):
            raise ArtifactFormatError("FIELD_TYPE", "logical_name/artifact_id")
        parsed = parse_artifact_directory_name(f"napsedge-{request.logical_name}-20000101T000000Z-{request.artifact_id}")
        if (parsed.logical_name, parsed.artifact_id) != (request.logical_name, request.artifact_id):
            raise ArtifactFormatError("NAME_INVALID", "logical_name/artifact_id")
    except ArtifactFormatError as exc:
        raise ValidationError(f"the capture request violates the artifact contract ({exc.code})") from None
    url = _validated_url(request.database_url)  # the lock module's own verify-full TLS check
    try:
        _str(url.username, "backup_role", pattern=_IDENTIFIER)
    except ArtifactFormatError:
        raise ValidationError("the database URL must name the backup role as a plain identifier") from None
    return url, url.username


class _Scratch:
    """The ONE private directory and file this module owns. Created exclusively; removed by exactly one attempt per entry."""

    def __init__(self, ops: FileSystemOps) -> None:
        self.ops = ops
        self.directory: str | None = None
        self.file: str | None = None

    def preserve(self, source: str, expected_bytes: int) -> tuple[str, int]:
        """Copies the dump's archive, hashing as it goes; returns (sha256, size). Raises StagedArchiveMismatch on a size difference."""
        directory = os.path.join(tempfile.gettempdir(), f"{SCRATCH_PREFIX}{uuid.uuid4().hex}")
        self.ops.mkdir_private(directory)  # exclusive: never an existing directory
        self.directory = directory
        target = os.path.join(directory, _SCRATCH_FILE)
        digest, size = hashlib.sha256(), 0
        source_fd = self.ops.open_source(source)
        try:
            target_fd = self.ops.open_exclusive(target)
            self.file = target
            try:
                while True:
                    chunk = self.ops.read(source_fd, _CHUNK)
                    if not chunk:
                        break
                    view = memoryview(chunk)
                    while view:
                        written = self.ops.write(target_fd, bytes(view))
                        if written <= 0:
                            raise OSError(errno.EIO, "a write made no progress")
                        view = view[written:]
                    digest.update(chunk)
                    size += len(chunk)
                self.ops.fsync_file(target_fd)
            except BaseException as primary:
                _close_after_failure(self.ops, target_fd, primary, "the scratch archive copy")  # one attempt; never replaces `primary`
                raise
            self.ops.close(target_fd)
        except BaseException as primary:
            _close_after_failure(self.ops, source_fd, primary, "the dump archive")
            raise
        self.ops.close(source_fd)
        if size != expected_bytes:
            raise StagedArchiveMismatch("the preserved archive's size differs from the dump's own archive size")
        return digest.hexdigest(), size

    def measure(self) -> tuple[str, int]:
        digest, size = hashlib.sha256(), 0
        fd = self.ops.open_source(self.file)
        try:
            while True:
                chunk = self.ops.read(fd, _CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
        except BaseException as primary:
            _close_after_failure(self.ops, fd, primary, "the scratch archive copy")
            raise
        self.ops.close(fd)
        return digest.hexdigest(), size

    def remove(self) -> list[str]:
        """One attempt per owned entry; returns sanitized failures (never raises an ordinary error; an interrupt propagates)."""
        failures = []
        for action, path in (("unlink", self.file), ("rmdir", self.directory)):
            if path is None:
                continue
            try:
                getattr(self.ops, action)(path)
            except FileNotFoundError:
                pass
            except Exception as exc:  # noqa: BLE001 -- reported, never replacing a primary failure
                failures.append(f"scratch {action}: {_describe(exc)}")
        return failures


def _note_scratch(primary: BaseException, failures: list[str]) -> None:
    if failures:
        primary.add_note(f"capture scratch cleanup also failed: {'; '.join(failures)}; the private scratch directory may remain")


def run_guarded_capture(request: CaptureRequest, *, client: Any, ops: FileSystemOps | None = None) -> PublishedCapture | SkippedOverlap:
    """See the module docstring. Raises the original capture error, a writer error, or `PublishedCaptureIncomplete`."""
    url, backup_role = _validate(request, client)
    ops = ops or FileSystemOps()
    scratch = _Scratch(ops)
    reservation: ArtifactReservation | None = None
    lock_entered = False
    facts: dict = {}
    try:
        with backup_capture_lock(url, request.designation) as lock:
            lock_entered = True
            facts["created_utc"] = _utc_now()
            reservation = reserve_artifact(request.store, artifact_id=request.artifact_id, logical_name=request.logical_name,
                                           created_utc=facts["created_utc"], durability=request.durability, ops=ops)
            observed_on_lock = SourceIdentity(lock.system_identifier, lock.database_oid, lock.verified_target.database_name)
            if observed_on_lock != request.expected_source:
                raise DatabaseTargetVerificationError("the locked database is not the expected source (system_identifier, database_oid or name differ)")
            before = observe_endpoint(url, request.designation, label="before", expected_source=request.expected_source)
            ops.checkpoint("before_snapshot", "")
            with backup_capture_snapshot(lock, url, request.designation, expected_alembic_head=request.expected_alembic_head,
                                         artifact_id=request.artifact_id) as handle:
                with backup_capture_dump(handle, client=client) as archive:
                    after = observe_endpoint(url, request.designation, label="after", expected_source=request.expected_source)
                    record = catalog_observation_record(require_unchanged(before, after, request.expected_source))
                    sha, size = scratch.preserve(archive.archive_path, archive.archive_bytes)
                    snapshot_facts = handle.facts
                    facts.update(baseline=handle.baseline, descriptor=handle.visibility_descriptor, record=record, sha=sha, size=size,
                                 toc_entries=archive.toc_entries, toc_sha256=archive.toc_sha256, pg_dump_version=archive.pg_dump_version,
                                 system_identifier=snapshot_facts.system_identifier, database_oid=snapshot_facts.database_oid,
                                 database_name=snapshot_facts.database_name, server_version_num=snapshot_facts.server_version_num)
                    ops.checkpoint("staged", scratch.file)
                facts["dump_cleanup"] = archive.cleanup
            facts["snapshot_cleanup"] = handle.cleanup
        facts["lock_cleanup"] = lock.cleanup
        ops.checkpoint("contexts_closed", scratch.file)
        if scratch.measure() != (facts["sha"], facts["size"]):
            raise StagedArchiveMismatch("the preserved archive changed between the dump and finalization")
        facts["finished_utc"] = _utc_now()
        metadata = _metadata(request, backup_role, facts)
    except AdvisoryLockUnavailableError as exc:
        if not lock_entered and reservation is None:
            _note_scratch(exc, scratch.remove())
            return SkippedOverlap(artifact_id=request.artifact_id)
        _record(reservation, exc, scratch)
        raise
    except BaseException as exc:
        _record(reservation, exc, scratch)
        raise

    # -- finalization: the writer owns every outcome from here (its own FAILED record, or a post-publication report) --------
    try:
        ops.checkpoint("before_finalize", scratch.file)
        # 9B-6I follow-up (13.19): the writer checks the bytes it STAGED against these, before COMPLETE and publication
        written = reservation.finalize(scratch.file, metadata, facts["baseline"], expected_archive_sha256=facts["sha"], expected_archive_bytes=facts["size"])
    except BaseException as exc:
        _note_scratch(exc, scratch.remove())
        raise
    failures = scratch.remove()
    if written.archive_sha256 != facts["sha"] or written.archive_bytes != facts["size"]:
        error = PublishedCaptureIncomplete("archive_digest_mismatch", written, scratch_path=scratch.directory if failures else None)
        _note_scratch(error, failures)
        raise error
    if failures:
        error = PublishedCaptureIncomplete("scratch_cleanup_failed", written, scratch_path=scratch.directory)
        error.add_note(f"capture scratch cleanup failed: {'; '.join(failures)}")
        raise error
    return PublishedCapture(written=written, archive_sha256=facts["sha"], archive_bytes=facts["size"], observation=facts["record"],
                            cleanup={k: facts[k] for k in ("lock_cleanup", "snapshot_cleanup", "dump_cleanup")})


def _record(reservation: ArtifactReservation | None, exc: BaseException, scratch: _Scratch) -> None:
    """A failure while reserved: ONE mapped FAILED record (never replacing `exc`), then the owned scratch is removed."""
    if reservation is not None and reservation.state == "reserved":
        reason = "checksum_mismatch" if isinstance(exc, StagedArchiveMismatch) else (capture_failure_reason(exc) or "internal_error")
        reservation.record_failure(reason, exc)
    _note_scratch(exc, scratch.remove())


def _metadata(request: CaptureRequest, backup_role: str, facts: dict) -> ArtifactMetadata:
    baseline = facts["baseline"]
    head = baseline.version
    return ArtifactMetadata(
        artifact_id=request.artifact_id, created_utc=facts["created_utc"], finished_utc=facts["finished_utc"],
        source=SourceInfo(logical_name=request.logical_name, system_identifier=facts["system_identifier"], database_oid=facts["database_oid"],
                          database_name=facts["database_name"], server_version_num=facts["server_version_num"],
                          pg_dump_version=facts["pg_dump_version"], backup_role=backup_role),
        schema=SchemaState(alembic_head=head, expected_head=request.expected_alembic_head, heads_match=head == request.expected_alembic_head),
        repository=request.repository, tooling=request.tooling,
        consistency=ConsistencyClaimV2(mode="exported_snapshot", snapshot_descriptor=facts["descriptor"], snapshot_unchanged_at_end=True,
                                       sequence_rule="monotonic_v1", ddl_exclusion="none", catalog_observation=facts["record"],
                                       foreign_write_sessions_start=None, foreign_write_sessions_end=None),
        archive=ArchiveInfo(format="custom", toc_entries=facts["toc_entries"], toc_sha256=facts["toc_sha256"]),
    )
