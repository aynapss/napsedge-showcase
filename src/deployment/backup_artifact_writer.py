"""
Backup artifact writer: the local, file-only WRITE side of the artifact contract
(Phase 9B-5B).

Scope
-----
Given a local archive file and caller-SUPPLIED baseline and metadata, build a
complete artifact directory in an owned `.partial-<artifact_id>` staging
directory and publish it with one same-filesystem directory rename. It performs
no database capture, SQL, `pg_dump`, snapshot, sequence evaluation, scheduling,
retention, external storage or subprocess work, and it imports nothing from the
test tree. It invents nothing: source identity, schema head, capture times,
consistency claims and the archive's `pg_restore --list` fields (`toc_entries`,
`toc_sha256`) are supplied by the caller and recorded as supplied; this module
cannot check the table-of-contents fields (that needs `pg_restore`). It derives
only what follows from bytes it handled itself: sizes, SHA-256 digests, and the
baseline summary counts.

Everything contractual is reused, not copied: documents are validated and
serialized by the contract's own types, validators and canonical serializer; the
completion marker is checked with the contract's parser; the manifest/baseline
relations are the verifier's own function; and the staged directory is judged
before publication by the verifier's own content check, called with the FINAL
directory name. The public verifier still refuses `.partial-*` directories.

What success establishes
------------------------
Claim 1 (files complete) plus a structural check of the staged files by the
contract's rules immediately before publication. It does NOT establish database
consistency, a successful restore, authenticity, or survival across power loss.

Finalization order (the specification's, in full)
-------------------------------------------------
create `.partial-<id>` exclusively (private) -> create `RUNNING` -> write and flush
`archive.dump` (streamed) -> write and flush `baseline.json` -> write and flush
`manifest.json` -> remove `RUNNING`, flush the staging directory -> write and flush
`COMPLETE` (binding the manifest's actual digest and size), flush the staging
directory -> validate the staged contents -> publish by rename -> flush the parent
directory.

Filesystem and platform guarantees
----------------------------------
Atomic visibility: one `rename` of a directory within a single filesystem. No
cross-filesystem copy or move fallback exists; a rename that cannot be done
atomically fails.
No replacement: Windows `rename` refuses an existing destination of any kind. On
POSIX `rename` would silently replace an EMPTY destination directory, so the
destination is first reserved with an exclusive `mkdir` (which fails if anything,
even an empty directory, exists) and the rename then replaces only that reservation.
If the process dies between the two steps an empty directory with the final name
remains; it verifies as INVALID, is never adopted or deleted by this module, and
blocks a retry of the same artifact id until an operator removes it.
Durability, stated per operation:
* file flush (`flush` + `fsync`): supported on POSIX and Windows. A failed file
  flush is always an error, in every mode.
* directory flush: supported on POSIX (`fsync` of a directory descriptor);
  UNSUPPORTED on Windows through the Python standard library. Therefore the
  default mode REQUIRED refuses to start where directory flushes are unsupported,
  before creating anything. The explicit mode BEST_EFFORT_REPORTED proceeds and
  lists every skipped directory flush as a gap in the result, so the result never
  claims durability it did not obtain. A directory flush that FAILS for a real
  reason (for example an I/O error) is an error in both modes.
* the flushes only report that the operating system accepted them; fault-injection
  tests do not and cannot prove survival of an actual power loss.
Privacy: the staging directory and files are created with modes 0700/0600 and, on
POSIX, the directory mode is checked. On Windows the mode argument is not applied;
access follows the parent directory's ACL, and the result says so.
Concurrency assumptions: the artifact store directory is written only by
cooperating writers that use this module. Exclusive creation of
`.partial-<artifact_id>` guarantees that two attempts for the same id cannot both
proceed; different ids never conflict. A process that tampers with the store while
a write is in progress (removing the reservation, swapping directories) is outside
this guarantee. A partial directory, finished or failed, is never reused, adopted
or deleted by this module.
Failure after the rename is a separate outcome: the directory may already be
published, so nothing is deleted and the caller must verify it with the public
verifier before use.
Early reservation (Phase 9B-6H, specification 13.16): `reserve_artifact` performs the
same validation and the same exclusive creation of `.partial-<artifact_id>` plus
`RUNNING` BEFORE any capture work, needing only the store, the artifact id, the
logical name and the capture start time (no source fact, baseline or archive). The
returned `ArtifactReservation` is single-shot: either `record_failure` writes the
existing FAILED record into it, or `finalize` runs exactly the one-shot writer's
remaining steps. Nothing finalizes, records or deletes implicitly; a reservation that
is simply dropped keeps `RUNNING` and stays unadoptable. `write_artifact` keeps its
signature, order, outputs and failure semantics and shares the staging and completion
code with the reservation.
Failed partials never keep a marker this run wrote (Phase 9B-6H follow-up, specification
13.17): the publication commit point is the directory rename. If anything fails before it
(including a rename that provably did not happen), the `COMPLETE` file this run created is
removed BEFORE `FAILED` is written, so a failed partial never shows `COMPLETE` beside
`FAILED`. Only that one file is removed; nothing else is deleted. If the removal fails, the
partial keeps both markers, stays non-adoptable, and the sanitized reason is attached to the
original failure. After a rename that happened, or may have happened, nothing is removed or
recorded: `PostPublicationError` reports `published` as "yes" or "unknown".
Descriptor ownership: every descriptor this module opens (the source archive, each
staged file, each directory opened for a flush) receives exactly ONE close attempt,
and an uncertain close is never retried. The source is closed as soon as its copy is
verified, before anything is published. A close that fails with no earlier failure is
itself a failure (a written file: `durability_failed` in that file's stage; the source:
`internal_error`); a close that fails while another failure is propagating NEVER
replaces it: the primary keeps its type, stage, classification and cause, and the
secondary is attached as a sanitized note and in `cleanup_failures` (exception type and
errno only, never a message). A KeyboardInterrupt or SystemExit that is propagating
stays the propagating exception; an interrupt raised by a close itself is not swallowed.
"""
from __future__ import annotations

import datetime as _dt
import enum
import errno
import hashlib
import os
import stat
import sys
from dataclasses import dataclass
from typing import Any

# `_inspect_artifact_path`, `_manifest_baseline_relations` and `_verify_contents` are package-private members of the contract module,
# deliberately shared so the writer cannot drift from the verifier's rules.
from src.deployment.backup_artifact import (
    ARCHIVE_FILE,
    BASELINE_FILE,
    DOES_NOT_ESTABLISH,
    FAILED_FILE,
    MANIFEST_FILE,
    MARKER_FILE,
    MARKER_FORMAT,
    RUNNING_FILE,
    ArchiveInfo,
    ArtifactFormatError,
    ArtifactManifest,
    BaselineDocument,
    BaselineSummary,
    ConsistencyClaim,
    ConsistencyClaimV2,
    FileEntry,
    RepositoryState,
    SchemaState,
    SourceInfo,
    ToolingInfo,
    _inspect_artifact_path,
    _is_link,
    _SHA256,
    _int,
    _manifest_baseline_relations,
    _read_file,
    _str,
    _timestamp,
    _verify_contents,
    canonical_json_bytes,
    parse_artifact_directory_name,
    parse_completion_marker,
)

RUNNING_FORMAT = "napsedge.backup-running/1"
FAILED_FORMAT = "napsedge.backup-failed/1"
#: The specification's reason classes plus the two the writer needs (a collision discovered at publication, and a failed or refused flush).
FAILED_REASON_CLASSES = frozenset({
    "dump_failed", "snapshot_unavailable", "baseline_failed", "checksum_mismatch", "source_identity_mismatch", "schema_head_mismatch",
    "catalog_changed", "timeout", "storage_full", "interrupted", "internal_error", "destination_exists", "durability_failed",
})
MAX_FAILED_RECORD_BYTES = 512
COPY_CHUNK_BYTES = 1 << 20
FILE_MODE = 0o600
DIRECTORY_MODE = 0o700
ESTABLISHES = ("files_complete", "staged_structure_validated")
DURABILITY_NOTE = "flush calls the operating system accepted; this is not proof of survival across power loss"
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_DEFAULT_PRIVACY = "not enforced on this platform: access follows the parent directory's ACL"


class DurabilityMode(enum.Enum):
    REQUIRED = "required"
    BEST_EFFORT_REPORTED = "best_effort_reported"


class DurabilityUnsupported(OSError):
    """A directory flush is not available on this platform or filesystem. Never raised for a flush that was attempted and failed."""


class ArtifactWriteError(Exception):
    """
    Base of every writer failure. `stage` names where it happened; `published` is
    "no", "yes" or "unknown"; `failed_record` says whether a FAILED record was
    written to the owned partial directory. The message carries class names and
    codes only -- never a path to a secret, a DSN, or a traceback; the underlying
    exception is the `__cause__`. `cleanup_failures` lists, as fixed-format text
    (what was being closed, the exception type and its errno, nothing else), descriptor
    closes that failed while this failure was propagating.
    """

    def __init__(self, stage: str, message: str, *, reason_class: str | None = None, partial_path: str | None = None,
                 published: str = "no", failed_record: str = "not_attempted", cleanup_failures: tuple[str, ...] = ()) -> None:
        super().__init__(f"[{stage}] {message}")
        self.stage = stage
        self.reason_class = reason_class
        self.partial_path = partial_path
        self.published = published
        self.failed_record = failed_record
        self.cleanup_failures = tuple(cleanup_failures)


class PreconditionError(ArtifactWriteError):
    """Refused before anything was created: invalid input, a collision, or an unsupported required guarantee."""


class WriteFailed(ArtifactWriteError):
    """Failed after the staging directory was created and before publication. The partial directory remains, marked FAILED where recording succeeded."""


class PostPublicationError(ArtifactWriteError):
    """
    Failed at or after the rename. The artifact directory MAY ALREADY BE PUBLISHED:
    nothing was deleted, and no claim is made that the final path is absent. Verify it
    with the public verifier before use.
    """


class ReservationStateError(RuntimeError):
    """A reservation was used after its single outcome (finalize or record_failure) was attempted. Nothing was touched."""


@dataclass(frozen=True)
class ArtifactMetadata:
    """Everything about an artifact that the caller must SUPPLY. The writer adds only sizes, digests and the baseline summary."""

    artifact_id: str
    created_utc: str
    finished_utc: str
    source: SourceInfo
    schema: SchemaState
    repository: RepositoryState
    tooling: ToolingInfo
    consistency: ConsistencyClaim | ConsistencyClaimV2  # /1 or /2; the manifest format follows from the type
    archive: ArchiveInfo


@dataclass(frozen=True)
class OwnedPartial:
    """A staging directory THIS module created. Only such a directory may receive a FAILED record."""

    path: str
    artifact_id: str


@dataclass(frozen=True)
class DurabilityReport:
    mode: str
    files_flushed: tuple[str, ...]
    directories_flushed: tuple[str, ...]
    gaps: tuple[str, ...]
    atomic_rename: bool
    note: str = DURABILITY_NOTE

    @property
    def durable(self) -> bool:
        """True only if every required flush was obtained: no gaps."""
        return not self.gaps


@dataclass(frozen=True)
class WrittenArtifact:
    path: str
    artifact_id: str
    manifest_sha256: str
    manifest_bytes: int
    archive_sha256: str
    archive_bytes: int
    baseline_sha256: str
    baseline_bytes: int
    durability: DurabilityReport
    privacy: str
    establishes: tuple[str, ...] = ESTABLISHES
    does_not_establish: tuple[str, ...] = DOES_NOT_ESTABLISH


class FileSystemOps:
    """
    Every filesystem and clock operation the writer performs, in one place. The
    defaults use the operating system directly; tests subclass this to inject
    failures at precise boundaries. `checkpoint` is a named no-op seam.
    """

    def lstat(self, path: str) -> os.stat_result:
        return os.lstat(path)

    def mkdir_private(self, path: str) -> None:
        os.mkdir(path, DIRECTORY_MODE)

    def mkdir_reservation(self, path: str) -> None:
        os.mkdir(path, DIRECTORY_MODE)

    def open_source(self, path: str) -> int:
        return os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))

    def open_exclusive(self, path: str) -> int:
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), FILE_MODE)

    def fstat(self, descriptor: int) -> os.stat_result:
        return os.fstat(descriptor)

    def read(self, descriptor: int, size: int) -> bytes:
        return os.read(descriptor, size)

    def write(self, descriptor: int, data: bytes) -> int:
        return os.write(descriptor, data)

    def fsync_file(self, descriptor: int) -> None:
        os.fsync(descriptor)

    def open_directory(self, path: str) -> int:
        return os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))

    def fsync_directory(self, path: str) -> None:
        if sys.platform == "win32":
            raise DurabilityUnsupported("directory flush is not available through the Python standard library on Windows")
        self.fsync_directory_descriptor(self.open_directory(path))

    def fsync_directory_descriptor(self, descriptor: int) -> None:
        """
        Flushes an open directory descriptor and closes it: exactly one close attempt, never retried.
        The flush outcome is primary. "Unsupported" (the filesystem cannot flush a directory) and an
        actual operation failure stay distinct, and a close that fails while either is propagating is
        attached to it as a note instead of replacing it. A close that fails after a SUCCESSFUL flush
        is an error: the caller is not told the flush succeeded.
        """
        try:
            try:
                os.fsync(descriptor)
            except OSError as exc:
                if exc.errno in {errno.EINVAL, getattr(errno, "ENOTSUP", -1), getattr(errno, "EOPNOTSUPP", -1)}:
                    raise DurabilityUnsupported("this filesystem does not support flushing a directory") from exc
                raise
        except BaseException as primary:
            _close_after_failure(self, descriptor, primary, "the directory")
            raise
        self.close(descriptor)

    def close(self, descriptor: int) -> None:
        os.close(descriptor)

    def unlink(self, path: str) -> None:
        os.unlink(path)

    def rename(self, source: str, destination: str) -> None:
        os.rename(source, destination)

    def rmdir(self, path: str) -> None:
        os.rmdir(path)

    def now(self) -> _dt.datetime:
        return _dt.datetime.now(_dt.timezone.utc)

    def pid(self) -> int:
        return os.getpid()

    def checkpoint(self, name: str, path: str) -> None:
        """Called at named boundaries ("before_validation", "before_publication"); does nothing."""


class _Run:
    """Mutable record of one write attempt: where it is, and what was actually obtained."""

    def __init__(self, mode: DurabilityMode) -> None:
        self.mode = mode
        self.stage = "plan"
        self.files_flushed: list[str] = []
        self.directories_flushed: list[str] = []
        self.gaps: list[str] = []


def _describe(exc: BaseException) -> str:
    text = type(exc).__name__
    if isinstance(exc, OSError) and exc.errno is not None:
        text += f" (errno {exc.errno})"
    if isinstance(exc, ArtifactFormatError):
        text += f" {exc.code}"
    return text


_CLEANUP_PREFIX = "cleanup: closing "
_MARKER_CLEANUP_PREFIX = "cleanup: removing "


def _close_after_failure(ops: FileSystemOps, descriptor: int, primary: BaseException, what: str) -> None:
    """
    The ONE close attempt for a descriptor whose operation is already failing with `primary`. It never
    raises an ordinary error and never retries: a failed close is attached to `primary` as a sanitized note
    (what was being closed, the exception type and errno; never a message) and, for a writer error, in
    `cleanup_failures`. Only an interrupt raised by the close itself escapes, because swallowing it would hide a user's interrupt.
    """
    try:
        ops.close(descriptor)
    except Exception as close_error:  # noqa: BLE001 -- must never replace `primary`
        note = f"{_CLEANUP_PREFIX}{what} also failed: {_describe(close_error)}"
        primary.add_note(note)
        if isinstance(primary, ArtifactWriteError):
            primary.cleanup_failures = (*primary.cleanup_failures, note)


def _cleanup_notes(exc: BaseException) -> tuple[str, ...]:
    """The sanitized cleanup notes this module attached to `exc` (and only those)."""
    return tuple(note for note in getattr(exc, "__notes__", ())
                 if isinstance(note, str) and note.startswith((_CLEANUP_PREFIX, _MARKER_CLEANUP_PREFIX)))


def _remove_owned_marker(ops: FileSystemOps, staging: str, primary: BaseException) -> None:
    """
    Before publication, on the failure path: ONE attempt to remove the `COMPLETE` file THIS run created in its own
    staging directory, so the failed partial does not keep a misleading completion marker. It never raises an ordinary
    error and never retries: a failed removal is attached to `primary` as a sanitized note (type and errno only) and, for
    a writer error, in `cleanup_failures`; the partial then keeps COMPLETE beside FAILED, which stays non-adoptable
    (`NOT_FINAL`, or `CONFLICTING_MARKERS` if renamed by hand). As for closes, only an interrupt raised by the removal
    itself escapes. The removal is not followed by a directory flush.
    """
    try:
        ops.unlink(os.path.join(staging, MARKER_FILE))
    except Exception as cleanup_error:  # noqa: BLE001 -- must never replace `primary`
        note = (f"{_MARKER_CLEANUP_PREFIX}the completion marker of the failed partial also failed: {_describe(cleanup_error)}; "
                f"the partial keeps COMPLETE beside FAILED and stays non-adoptable")
        primary.add_note(note)
        if isinstance(primary, ArtifactWriteError):
            primary.cleanup_failures = (*primary.cleanup_failures, note)


def _utc_text(moment: _dt.datetime) -> str:
    return moment.astimezone(_dt.timezone.utc).strftime(_TIMESTAMP_FORMAT)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _compose_manifest(metadata: ArtifactMetadata, baseline: BaselineDocument, archive_entry: FileEntry, baseline_entry: FileEntry) -> ArtifactManifest:
    """Builds the manifest and validates it by round-tripping through the contract's own parser-side validation."""
    manifest = ArtifactManifest(
        artifact_id=metadata.artifact_id, created_utc=metadata.created_utc, finished_utc=metadata.finished_utc, source=metadata.source,
        schema=metadata.schema, repository=metadata.repository, tooling=metadata.tooling, consistency=metadata.consistency,
        archive=metadata.archive,
        baseline=BaselineSummary(table_count=len(baseline.fingerprints), row_total=sum(f.rows for f in baseline.fingerprints)),
        files=(archive_entry, baseline_entry),
    )
    return ArtifactManifest.from_document(manifest.to_document())


def _final_directory_name(manifest: ArtifactManifest) -> str:
    stamp = _dt.datetime.strptime(manifest.created_utc, _TIMESTAMP_FORMAT).strftime("%Y%m%dT%H%M%SZ")
    name = f"napsedge-{manifest.source.logical_name}-{stamp}-{manifest.artifact_id}"
    parse_artifact_directory_name(name)  # the contract's own grammar decides whether this is a valid name
    return name


def _marker_bytes(artifact_id: str, manifest_sha256: str, manifest_size: int) -> bytes:
    text = f"{MARKER_FORMAT}\nartifact_id={artifact_id}\nmanifest_sha256={manifest_sha256}\nmanifest_bytes={manifest_size}\n".encode("ascii")
    parsed = parse_completion_marker(text)  # the contract's parser must accept exactly what is about to be written
    if (parsed.artifact_id, parsed.manifest_sha256, parsed.manifest_bytes) != (artifact_id, manifest_sha256, manifest_size):
        raise ArtifactFormatError("MARKER_MALFORMED", "the marker did not round-trip")
    return text


def _write_all(ops: FileSystemOps, descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = ops.write(descriptor, bytes(view[:COPY_CHUNK_BYTES]))
        if written <= 0:
            raise OSError(errno.EIO, "a write made no progress")
        view = view[written:]


def _create_file(run: _Run, ops: FileSystemOps, path: str, name: str, payload: bytes | None = None, *, stream: Any = None,
                 on_created: Any = None) -> tuple[int, str]:
    """
    Creates `path` exclusively, writes `payload` or streams `stream(write)`, flushes the
    FILE, and closes it. Returns (bytes written, sha256 of the bytes handed to write).
    The descriptor gets exactly one close attempt: after a failure it is made without ever
    replacing that failure; after success a failing close is itself a (durability) failure.
    `on_created()` is called as soon as the exclusive creation succeeded: from then on the file is this run's.
    """
    descriptor = ops.open_exclusive(path)
    if on_created is not None:
        on_created()
    try:
        digest = hashlib.sha256()
        total = 0
        if payload is not None:
            _write_all(ops, descriptor, payload)
            digest.update(payload)
            total = len(payload)
        else:
            total, digest = stream(lambda chunk: _write_all(ops, descriptor, chunk))
        try:
            ops.fsync_file(descriptor)
        except OSError as exc:
            raise _FlushFailed(f"flushing {name} failed: {_describe(exc)}") from exc
        run.files_flushed.append(name)
    except BaseException as primary:
        _close_after_failure(ops, descriptor, primary, name)
        raise
    try:
        ops.close(descriptor)
    except OSError as exc:  # not retried: the descriptor's state is unknown, and the file is not known to be complete
        raise _FlushFailed(f"closing {name} failed: {_describe(exc)}") from exc
    return total, digest.hexdigest()


def _flush_directory(run: _Run, ops: FileSystemOps, path: str, label: str) -> None:
    try:
        ops.fsync_directory(path)
    except DurabilityUnsupported as exc:
        if run.mode is DurabilityMode.REQUIRED:
            raise
        extra = "".join(f"; {note}" for note in _cleanup_notes(exc))
        run.gaps.append(f"{label}: directory flush unsupported ({type(exc).__name__}); explicitly accepted by BEST_EFFORT_REPORTED{extra}")
    else:
        run.directories_flushed.append(label)


def _classify(exc: BaseException, stage: str) -> str:
    if not isinstance(exc, Exception):
        return "interrupted"
    cause = exc.__cause__ if isinstance(exc, _FlushFailed) else exc
    if isinstance(cause, OSError) and cause.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)}:
        return "storage_full"
    if isinstance(exc, _FlushFailed):
        return "durability_failed"
    if stage == "publication" and isinstance(exc, FileExistsError):
        return "destination_exists"
    if isinstance(exc, DurabilityUnsupported) or stage.startswith("flush"):
        return "durability_failed"
    if isinstance(exc, _ValidationFailed):
        return "checksum_mismatch" if exc.integrity else "internal_error"
    if isinstance(exc, _SourceChanged):
        return "checksum_mismatch"
    return "internal_error"


class _FlushFailed(Exception):
    """A FILE flush, or the close of a written file, failed. Always an error, in every durability mode."""


class _SourceCloseFailed(Exception):
    """Closing the (read-only) source archive failed after its copy had been checked: an error, never success."""


class _ValidationFailed(Exception):
    def __init__(self, codes: tuple[str, ...]) -> None:
        super().__init__("the staged contents failed contract validation: " + ", ".join(codes))
        self.codes = codes
        self.integrity = any(code in {"FILE_DIGEST_MISMATCH", "FILE_SIZE_MISMATCH", "MARKER_DIGEST_MISMATCH", "MARKER_SIZE_MISMATCH"} for code in codes)


class _SourceChanged(Exception):
    pass


def write_failed_record(owned: OwnedPartial, reason_class: str, *, ops: FileSystemOps | None = None) -> None:
    """
    Writes the bounded FAILED record into a partial directory THIS module created.

    The record is fixed-shape canonical JSON -- format, artifact id, UTC time and an
    enumerated reason class -- so it cannot carry a credential, DSN, host or traceback,
    and it is created exclusively (an existing FAILED record is never overwritten). It
    supersedes the RUNNING marker, which is then removed on a best-effort basis. A
    directory carrying FAILED stays invalid and is never adopted: the public verifier
    refuses `.partial-*` outright, and even a directory renamed by hand to its final
    name is `CONFLICTING_MARKERS` if COMPLETE is present and `ARTIFACT_FAILED` if not.
    """
    ops = ops or FileSystemOps()
    if reason_class not in FAILED_REASON_CLASSES:
        raise ValueError("reason_class is not one of the enumerated failure classes")
    if os.path.basename(owned.path) != f".partial-{owned.artifact_id}":
        raise ValueError("not a staging directory of this artifact id")
    record = canonical_json_bytes({"format": FAILED_FORMAT, "artifact_id": owned.artifact_id, "utc": _utc_text(ops.now()), "reason_class": reason_class})
    if len(record) > MAX_FAILED_RECORD_BYTES:
        raise ValueError("the FAILED record exceeded its size bound")
    scratch = _Run(DurabilityMode.BEST_EFFORT_REPORTED)
    _create_file(scratch, ops, os.path.join(owned.path, FAILED_FILE), FAILED_FILE, record)
    try:
        ops.unlink(os.path.join(owned.path, RUNNING_FILE))
    except OSError:
        pass  # FAILED already exists and supersedes RUNNING; a leftover RUNNING is harmless in a directory that is never published


def _plan(parent_directory: Any, archive_path: Any, metadata: ArtifactMetadata, baseline: BaselineDocument, ops: FileSystemOps, mode: DurabilityMode):
    """Everything that can be decided without creating anything in the store. Raises PreconditionError."""
    parent = _usable_store(parent_directory)
    archive_text, source_stat, baseline_document, baseline_bytes, final_name = _plan_contents(archive_path, metadata, baseline, ops)
    final_path, staging_path = _check_targets(ops, parent, final_name, metadata.artifact_id, mode)
    return parent, archive_text, source_stat, baseline_document, baseline_bytes, final_name, final_path, staging_path


def _usable_store(parent_directory: Any) -> str:
    parent, _, failure = _inspect_artifact_path(parent_directory)
    if failure is not None:
        raise PreconditionError("store", f"the artifact store directory is not usable: {failure.code} {failure.detail}")
    return parent


def _plan_contents(archive_path: Any, metadata: ArtifactMetadata, baseline: BaselineDocument, ops: FileSystemOps):
    """The archive, baseline and metadata checks, creating nothing. Raises PreconditionError."""
    def refuse(message: str, stage: str = "plan") -> PreconditionError:
        return PreconditionError(stage, message)

    archive_text = os.fspath(archive_path)
    if not isinstance(archive_text, str):
        raise TypeError("the archive path must be a str or a path-like object that yields a str")
    try:
        source_stat = ops.lstat(archive_text)
    except OSError as exc:
        raise refuse(f"the archive cannot be inspected: {_describe(exc)}", "archive") from exc
    if _is_link(source_stat) or not stat.S_ISREG(source_stat.st_mode):
        raise refuse("the archive must be a regular file and not a link", "archive")
    try:
        baseline_document = BaselineDocument.from_document(baseline.to_document())
        baseline_bytes = baseline_document.to_bytes()
        provisional = _compose_manifest(metadata, baseline_document, FileEntry(ARCHIVE_FILE, source_stat.st_size, "0" * 64),
                                        FileEntry(BASELINE_FILE, len(baseline_bytes), _sha256(baseline_bytes)))
        final_name = _final_directory_name(provisional)
    except ArtifactFormatError as exc:
        raise refuse(f"the supplied metadata violates the contract: {exc.code} {exc.detail}", "metadata") from exc
    relations = _manifest_baseline_relations(provisional, baseline_document)
    if relations:
        raise refuse("the supplied manifest metadata and baseline disagree: " + ", ".join(r.code for r in relations), "metadata")
    return archive_text, source_stat, baseline_document, baseline_bytes, final_name


def _check_targets(ops: FileSystemOps, parent: str, final_name: str, artifact_id: str, mode: DurabilityMode) -> tuple[str, str]:
    """The destination and the partial must not exist; under REQUIRED, directory flushes must be obtainable. Creates nothing."""
    def refuse(message: str, stage: str) -> PreconditionError:
        return PreconditionError(stage, message)

    final_path = os.path.join(parent, final_name)
    staging_path = os.path.join(parent, f".partial-{artifact_id}")
    for label, candidate in (("destination", final_path), ("partial", staging_path)):
        try:
            ops.lstat(candidate)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise refuse(f"the {label} path cannot be inspected: {_describe(exc)}", label) from exc
        raise refuse(f"{'DESTINATION_EXISTS' if label == 'destination' else 'PARTIAL_EXISTS'}: an entry already exists at the {label} path; "
                     f"it is never replaced, adopted or deleted", label)
    if mode is DurabilityMode.REQUIRED:
        try:
            ops.fsync_directory(parent)
        except DurabilityUnsupported as exc:
            raise refuse("directory flushes are unsupported here and DurabilityMode.REQUIRED was requested; nothing was created. "
                         "Use BEST_EFFORT_REPORTED to proceed with the gap reported", "durability") from exc
        except OSError as exc:
            raise refuse(f"the durability probe on the store directory failed: {_describe(exc)}", "durability") from exc
    return final_path, staging_path


def _after_rename_failure(ops: FileSystemOps, staging: str, final: str) -> str:
    """Classifies an exception raised by the rename: "yes" (published), "no" (not), or "unknown"."""
    try:
        staging_exists = _exists(ops, staging)
        final_exists = _exists(ops, final)
        # "yes" only if the final name holds a completion marker: on POSIX the final name may be just this run's EMPTY
        # reservation (9B-6H follow-up, 13.17), which is not evidence that the staged directory arrived there
        final_has_marker = final_exists and _exists(ops, os.path.join(final, MARKER_FILE))
    except OSError:
        return "unknown"
    if staging_exists:
        return "no"  # a directory rename removes the source name: if staging is still there, nothing was published
    if final_has_marker:
        return "yes"
    return "unknown"


def _exists(ops: FileSystemOps, path: str) -> bool:
    try:
        ops.lstat(path)
        return True
    except FileNotFoundError:
        return False


def write_artifact(parent_directory: Any, archive_path: Any, metadata: ArtifactMetadata, baseline: BaselineDocument, *,
                   durability: DurabilityMode = DurabilityMode.REQUIRED, ops: FileSystemOps | None = None) -> WrittenArtifact:
    """
    Writes one artifact into `parent_directory` and returns a `WrittenArtifact`, or raises
    an `ArtifactWriteError` subclass; it never reports success it did not achieve.

    Outcomes: `PreconditionError` -- refused, nothing created; `WriteFailed` -- the owned
    partial directory exists (never deleted, FAILED recorded where possible), not
    published; `PostPublicationError` -- the artifact may already be published. Repeating
    a call: after success the destination exists (refused, untouched); after a failure the
    partial exists (refused, never adopted: choose a new artifact id or have an operator
    deal with the directory); after a post-publication failure the destination exists.
    """
    ops = ops or FileSystemOps()
    run = _Run(durability)
    parent, archive_text, source_stat, baseline_doc, baseline_bytes, final_name, final_path, staging = _plan(
        parent_directory, archive_path, metadata, baseline, ops, durability)
    source = _open_source(ops, archive_text)
    try:
        opened = ops.fstat(source.fd)
        if not stat.S_ISREG(opened.st_mode):
            raise PreconditionError("archive", "the archive is not a regular file")
        _create_staging(ops, staging)
        owned = OwnedPartial(staging, metadata.artifact_id)
        return _complete(run, ops, owned, source, opened, parent, final_name, final_path, metadata, baseline_doc, baseline_bytes,
                         privacy=_DEFAULT_PRIVACY, begin=True)
    except BaseException as exc:
        source.release(exc)  # a no-op once the source was closed or released; covers failures before the staging directory exists
        raise


class _Source:
    """The read-only input archive descriptor: exactly one close attempt, never retried."""

    def __init__(self, ops: FileSystemOps, fd: int) -> None:
        self.ops, self.fd, self.open = ops, fd, True

    def release(self, primary: BaseException) -> None:
        """Failure path: the single close attempt on the source, unless it was already attempted."""
        if self.open:
            self.open = False
            _close_after_failure(self.ops, self.fd, primary, "the source archive")

    def close_checked(self) -> None:
        """Success path: the one close attempt is made now, before anything is published, and is never retried."""
        self.open = False
        try:
            self.ops.close(self.fd)
        except OSError as exc:
            raise _SourceCloseFailed("closing the source archive failed") from exc


def _open_source(ops: FileSystemOps, archive_text: str) -> _Source:
    try:
        return _Source(ops, ops.open_source(archive_text))
    except OSError as exc:
        raise PreconditionError("archive", f"the archive could not be opened: {_describe(exc)}") from exc


def _create_staging(ops: FileSystemOps, staging: str) -> None:
    """The EXCLUSIVE creation of the private staging directory: the moment ownership begins. Raises PreconditionError (nothing created) on failure."""
    try:
        ops.mkdir_private(staging)
    except FileExistsError as exc:
        raise PreconditionError("partial", "PARTIAL_EXISTS: a staging directory appeared; it is never reused", ) from exc
    except OSError as exc:
        raise PreconditionError("staging", f"the staging directory could not be created: {_describe(exc)}") from exc


def _begin_staging(run: _Run, ops: FileSystemOps, staging: str, artifact_id: str) -> str:
    """Inside the owned staging directory: the POSIX privacy check, then RUNNING. Returns the privacy statement."""
    run.stage = "staging"
    privacy = _DEFAULT_PRIVACY
    if sys.platform != "win32":
        if stat.S_IMODE(ops.lstat(staging).st_mode) & 0o077:
            raise OSError(errno.EPERM, "the staging directory is accessible to group or others")
        privacy = "directory mode 0700 verified; files created 0600"
    running_payload = canonical_json_bytes({"format": RUNNING_FORMAT, "artifact_id": artifact_id, "pid": ops.pid(), "utc": _utc_text(ops.now())})
    _create_file(run, ops, os.path.join(staging, RUNNING_FILE), RUNNING_FILE, running_payload)
    return privacy


def _complete(run: _Run, ops: FileSystemOps, owned: OwnedPartial, source: _Source, opened: os.stat_result, parent: str, final_name: str,
              final_path: str, metadata: ArtifactMetadata, baseline_doc: BaselineDocument, baseline_bytes: bytes, *, privacy: str,
              begin: bool, expected_archive: tuple[str, int] | None = None) -> WrittenArtifact:
    """
    Everything after ownership began, for the one-shot writer (`begin=True`: privacy check and RUNNING first) and for a
    reservation (`begin=False`: they were done at reservation time). A failure before the rename records FAILED and raises
    `WriteFailed` (or the interrupt itself); at or after the rename, `PostPublicationError`.
    """
    staging = owned.path
    reserved = False
    marker_owned = False  # True once THIS run created COMPLETE; until the rename commits, a failure removes it

    def owns_marker() -> None:
        nonlocal marker_owned
        marker_owned = True

    try:
        # -- staging directory and RUNNING (one-shot only) -----------------------------------------------------------
        if begin:
            privacy = _begin_staging(run, ops, staging, metadata.artifact_id)

        # -- archive (streamed), baseline, manifest ----------------------------------------------------------
        run.stage = "archive"

        def copy_archive(write):
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = ops.read(source.fd, COPY_CHUNK_BYTES)
                if not chunk:
                    return total, digest
                digest.update(chunk)
                total += len(chunk)
                write(chunk)

        archive_size, archive_sha = _create_file(run, ops, os.path.join(staging, ARCHIVE_FILE), ARCHIVE_FILE, stream=copy_archive)
        after = ops.fstat(source.fd)
        if archive_size != opened.st_size or (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
            raise _SourceChanged("the archive changed while it was being copied")
        source.close_checked()
        if expected_archive is not None:
            # 9B-6I follow-up (13.19): the STAGED archive.dump, re-read from disk by the verifier's own streaming reader, must be
            # exactly the caller's expected bytes -- before the baseline, manifest and COMPLETE exist, so a mismatch is an ordinary
            # pre-publication failure (`checksum_mismatch`) and can never be published.
            staged_size, staged_sha, _ = _read_file(os.path.join(staging, ARCHIVE_FILE), keep_limit=None)
            expected_sha, expected_size = expected_archive
            codes = tuple(code for code, wrong in (("FILE_SIZE_MISMATCH", staged_size != expected_size or staged_size != archive_size),
                                                    ("FILE_DIGEST_MISMATCH", staged_sha != expected_sha or staged_sha != archive_sha)) if wrong)
            if codes:
                raise _ValidationFailed(codes)

        run.stage = "baseline"
        baseline_size, baseline_sha = _create_file(run, ops, os.path.join(staging, BASELINE_FILE), BASELINE_FILE, baseline_bytes)

        run.stage = "manifest"
        manifest = _compose_manifest(metadata, baseline_doc, FileEntry(ARCHIVE_FILE, archive_size, archive_sha),
                                     FileEntry(BASELINE_FILE, baseline_size, baseline_sha))
        manifest_bytes = manifest.to_bytes()
        manifest_size, manifest_sha = _create_file(run, ops, os.path.join(staging, MANIFEST_FILE), MANIFEST_FILE, manifest_bytes)

        # -- RUNNING removed BEFORE COMPLETE, then COMPLETE last ----------------------------------------------------
        run.stage = "running_removal"
        ops.unlink(os.path.join(staging, RUNNING_FILE))
        run.stage = "flush_after_running_removal"
        _flush_directory(run, ops, staging, "staging after RUNNING removal")
        run.stage = "marker"
        _create_file(run, ops, os.path.join(staging, MARKER_FILE), MARKER_FILE, _marker_bytes(metadata.artifact_id, manifest_sha, manifest_size),
                     on_created=owns_marker)
        run.stage = "flush_after_marker"
        _flush_directory(run, ops, staging, "staging after COMPLETE")

        # -- validation by the contract's own logic, with the FINAL name -------------------------------------------
        run.stage = "validation"
        ops.checkpoint("before_validation", staging)
        verdict = _verify_contents(staging, final_name, None)
        if not verdict.valid:
            raise _ValidationFailed(verdict.codes)
        if verdict.manifest != manifest:
            raise _ValidationFailed(("MANIFEST_NOT_AS_WRITTEN",))

        # -- publication --------------------------------------------------------------------------------------------------
        run.stage = "publication"
        ops.checkpoint("before_publication", staging)
        if sys.platform != "win32":
            ops.mkdir_reservation(final_path)  # exclusive: fails if ANYTHING, even an empty directory, exists
            reserved = True
        try:
            ops.rename(staging, final_path)
        except BaseException as exc:
            outcome = _after_rename_failure(ops, staging, final_path)
            if outcome == "no" and reserved:
                try:
                    ops.rmdir(final_path)  # only our own, still-empty reservation
                except OSError:
                    pass
            if outcome == "no":
                raise
            error = PostPublicationError("publication", f"the rename raised {_describe(exc)} and the directory may already be published: nothing was deleted; "
                                                        f"verify it with the public verifier before use", published=outcome, partial_path=staging)
            raise error from exc
    except PostPublicationError:
        raise  # the rename happened or may have happened: nothing is removed and no FAILED record is written
    except BaseException as exc:
        source.release(exc)
        if marker_owned:
            _remove_owned_marker(ops, staging, exc)  # before FAILED: a failed partial never shows COMPLETE beside FAILED
        failure = _failure(exc, run, owned, ops)
        if failure is exc:
            raise
        raise failure from exc
    # -- after the rename: the artifact IS published; nothing below may delete it ----------------------------------------
    run.stage = "flush_parent"
    try:
        _flush_directory(run, ops, parent, "parent after publication")
    except BaseException as exc:
        raise PostPublicationError("flush_parent", f"the directory was renamed into place (published) but flushing its parent failed: {_describe(exc)}; "
                                                   f"nothing was deleted; verify it with the public verifier before use",
                                   published="yes", reason_class="durability_failed", cleanup_failures=_cleanup_notes(exc)) from exc
    return WrittenArtifact(
        path=final_path, artifact_id=metadata.artifact_id, manifest_sha256=manifest_sha, manifest_bytes=manifest_size,
        archive_sha256=archive_sha, archive_bytes=archive_size, baseline_sha256=baseline_sha, baseline_bytes=baseline_size,
        durability=DurabilityReport(mode=run.mode.value, files_flushed=tuple(run.files_flushed), directories_flushed=tuple(run.directories_flushed),
                                    gaps=tuple(run.gaps), atomic_rename=True),
        privacy=privacy,
    )


# ---------------------------------------------------------------------------
# early reservation (Phase 9B-6H, specification 13.16)
# ---------------------------------------------------------------------------
class _FinalizeRefused(Exception):
    """A finalize precondition failed AFTER ownership began: recorded as FAILED (internal_error), raised as WriteFailed. `__cause__` holds the PreconditionError."""


class ArtifactReservation:
    """
    An owned `.partial-<artifact_id>` directory holding only `RUNNING`, created by `reserve_artifact` before capture work.

    Single-shot. Exactly one of these may be attempted, and only while `state` is "reserved":
    * `record_failure(reason_class, primary)` -- writes the existing bounded FAILED record (state "failed");
    * `finalize(archive_path, metadata, baseline)` -- the one-shot writer's remaining steps (state "finalized", "failed" or
      "publication_uncertain").
    Any later call raises `ReservationStateError` and touches nothing. Nothing happens implicitly: there is no context
    manager, no automatic finalization or failure recording, and no deletion. A reservation that is dropped keeps
    `RUNNING`, is invalid to the public verifier (`NOT_FINAL`), and is never adopted; failed partials are retained.
    """

    def __init__(self, *, owned: OwnedPartial, parent: str, final_name: str, final_path: str, logical_name: str, created_utc: str,
                 run: _Run, ops: FileSystemOps, privacy: str) -> None:
        self._owned, self._parent, self._run, self._ops = owned, parent, run, ops
        self.final_name, self.final_path, self.logical_name, self.created_utc, self.privacy = final_name, final_path, logical_name, created_utc, privacy
        self.state = "reserved"
        self.failed_record: str | None = None

    @property
    def path(self) -> str:
        return self._owned.path

    @property
    def artifact_id(self) -> str:
        return self._owned.artifact_id

    def _claim(self, action: str) -> None:
        if self.state != "reserved":
            raise ReservationStateError(f"{action} refused: the reservation is {self.state}, and each reservation allows one outcome")

    def record_failure(self, reason_class: str, primary: BaseException | None = None) -> str:
        """
        Writes the FAILED record (an enumerated class, nothing else) into this owned partial. Returns "recorded" or
        "failed: <ExceptionType>"; it never raises because the recording failed, so the caller's primary failure stays the
        failure. On a recording failure a bounded, sanitized note is added to `primary` (its type, cause, notes and traceback
        are otherwise untouched). Never durable where the filesystem prevents it: a failed recording leaves RUNNING, still unadoptable.
        """
        if reason_class not in FAILED_REASON_CLASSES:
            raise ValueError("reason_class is not one of the enumerated failure classes")
        if primary is not None and not isinstance(primary, BaseException):
            raise TypeError("primary must be the exception being recorded, or None")
        self._claim("record_failure")
        self.state = "failed"
        self.failed_record = _record_failed(self._owned, reason_class, self._ops)
        if self.failed_record != "recorded" and primary is not None:
            primary.add_note(f"recording the FAILED marker ({reason_class}) in the owned partial directory also failed ({self.failed_record}); "
                             f"this exception remains the primary failure")
        return self.failed_record

    def finalize(self, archive_path: Any, metadata: ArtifactMetadata, baseline: BaselineDocument, *,
                 expected_archive_sha256: str | None = None, expected_archive_bytes: int | None = None) -> WrittenArtifact:
        """
        The explicit, successful-path operation: the metadata must name this reservation's artifact id, logical name and
        creation time; then exactly the one-shot writer's steps from the archive copy to the parent flush. A refusal or
        failure before the rename records FAILED and raises `WriteFailed`; at or after it, `PostPublicationError`.

        Optional, supplied together or not at all: the archive's expected SHA-256 (64 lower-case hex) and byte count (>= 1).
        The STAGED archive's bytes are then checked against them right after the copy, before anything else is written; a
        difference is `WriteFailed` (`checksum_mismatch`) and nothing is published. Malformed expectations are refused like
        any other finalize precondition. Without them, behavior is unchanged.
        """
        self._claim("finalize")
        self.state = "finalizing"
        run, ops, owned = self._run, self._ops, self._owned
        run.stage = "finalize_plan"
        source: _Source | None = None
        try:
            try:
                if not isinstance(metadata, ArtifactMetadata) or not isinstance(metadata.source, SourceInfo):
                    raise PreconditionError("metadata", "ArtifactMetadata is required")
                if (metadata.artifact_id, metadata.source.logical_name, metadata.created_utc) != (self.artifact_id, self.logical_name, self.created_utc):
                    raise PreconditionError("metadata", "the metadata does not name this reservation's artifact id, logical name and creation time")
                archive_text, _, baseline_doc, baseline_bytes, final_name = _plan_contents(archive_path, metadata, baseline, ops)
                if final_name != self.final_name:
                    raise PreconditionError("metadata", "the metadata does not produce this reservation's final name")
                expected_archive = _expected_archive(expected_archive_sha256, expected_archive_bytes)
                source = _open_source(ops, archive_text)
                opened = ops.fstat(source.fd)
                if not stat.S_ISREG(opened.st_mode):
                    raise PreconditionError("archive", "the archive is not a regular file")
            except PreconditionError as refused:
                raise _FinalizeRefused(f"finalize refused at {refused.stage}") from refused
        except BaseException as exc:
            if source is not None:
                source.release(exc)
            self.state = "failed"
            failure = _failure(exc, run, owned, ops)
            self.failed_record = getattr(failure, "failed_record", None)
            if failure is exc:
                raise
            raise failure from exc
        try:
            written = _complete(run, ops, owned, source, opened, self._parent, self.final_name, self.final_path, metadata, baseline_doc,
                                baseline_bytes, privacy=self.privacy, begin=False, expected_archive=expected_archive)
        except PostPublicationError:
            self.state = "publication_uncertain"
            raise
        except BaseException as exc:
            self.state = "failed"
            self.failed_record = getattr(exc, "failed_record", None)
            raise
        self.state = "finalized"
        return written


def _expected_archive(sha256: Any, size: Any) -> tuple[str, int] | None:
    """Both absent (no check) or both well-formed; anything else is a PreconditionError (recorded by finalize like any refusal)."""
    if sha256 is None and size is None:
        return None
    if sha256 is None or size is None:
        raise PreconditionError("archive", "the expected archive digest and size must be supplied together")
    try:  # the contract's own validators: a digest is 64 lower-case hex, a size an exact integer (never a boolean), here >= 1
        return _str(sha256, "expected_archive_sha256", pattern=_SHA256), _int(size, "expected_archive_bytes", minimum=1)
    except ArtifactFormatError as exc:
        raise PreconditionError("archive", f"the expected archive digest or size is malformed: {exc.code}") from exc


def reserve_artifact(parent_directory: Any, *, artifact_id: str, logical_name: str, created_utc: str,
                     durability: DurabilityMode = DurabilityMode.REQUIRED, ops: FileSystemOps | None = None) -> ArtifactReservation:
    """
    Reserves an artifact BEFORE capture work: validates the store, the artifact id, the logical name and the capture start
    time (`created_utc`, which becomes the manifest's and fixes the final directory name), refuses an existing destination
    or partial, probes durability under REQUIRED -- all before creating anything (`PreconditionError`, nothing created) --
    then creates `.partial-<artifact_id>` exclusively and privately and writes `RUNNING`. A failure after the exclusive
    creation records FAILED in the owned partial and raises `WriteFailed`, as the one-shot writer does. No source fact,
    baseline or archive is needed or recorded. Of two competing reservations for one artifact id, exactly one creates the
    directory; the other gets `PreconditionError` (PARTIAL_EXISTS) and touches nothing.
    """
    ops = ops or FileSystemOps()
    run = _Run(durability)
    parent = _usable_store(parent_directory)
    for label, value in (("artifact_id", artifact_id), ("logical_name", logical_name), ("created_utc", created_utc)):
        if not isinstance(value, str):
            raise PreconditionError("reservation", f"{label} must be a string")
    try:
        _timestamp(created_utc, "created_utc")
        final_name = f"napsedge-{logical_name}-{_dt.datetime.strptime(created_utc, _TIMESTAMP_FORMAT).strftime('%Y%m%dT%H%M%SZ')}-{artifact_id}"
        parsed = parse_artifact_directory_name(final_name)  # the contract's grammar: logical name pattern, UUID4
    except ArtifactFormatError as exc:
        raise PreconditionError("reservation", f"the artifact id, logical name or creation time violates the contract: {exc.code}") from exc
    if (parsed.logical_name, parsed.artifact_id) != (logical_name, artifact_id):
        raise PreconditionError("reservation", "the artifact id or logical name violates the contract")
    final_path, staging = _check_targets(ops, parent, final_name, artifact_id, durability)
    _create_staging(ops, staging)
    owned = OwnedPartial(staging, artifact_id)
    try:
        privacy = _begin_staging(run, ops, staging, artifact_id)
    except BaseException as exc:
        failure = _failure(exc, run, owned, ops)
        if failure is exc:
            raise
        raise failure from exc
    return ArtifactReservation(owned=owned, parent=parent, final_name=final_name, final_path=final_path, logical_name=logical_name,
                               created_utc=created_utc, run=run, ops=ops, privacy=privacy)


def _record_failed(owned: OwnedPartial, reason: str, ops: FileSystemOps) -> str:
    """One attempt to write FAILED: "recorded", or "failed: <ExceptionType>". It never raises, so it can never replace a primary failure."""
    try:
        write_failed_record(owned, reason, ops=ops)
        return "recorded"
    except BaseException as record_error:  # noqa: BLE001 -- must never replace the original failure
        return f"failed: {type(record_error).__name__}"


def _failure(exc: BaseException, run: _Run, owned: OwnedPartial, ops: FileSystemOps) -> BaseException:
    """
    Records FAILED in the owned partial directory (never letting that hide `exc`) and returns
    the exception to raise: a `WriteFailed` for ordinary errors, or `exc` itself, annotated, for
    interrupts. A failure of the recording is attached as a note; the original failure survives.
    """
    reason = _classify(exc, run.stage)
    recorded = _record_failed(owned, reason, ops)
    if not isinstance(exc, Exception):
        exc.add_note(f"a FAILED record ({reason}) was {recorded} in the owned partial directory")
        return exc
    if isinstance(exc, ArtifactWriteError):
        exc.failed_record = recorded
        return exc
    error = WriteFailed(run.stage, f"{_describe(exc)}; the partial directory was kept (never deleted or adopted); FAILED record {recorded}",
                        reason_class=reason, partial_path=owned.path, published="no", failed_record=recorded, cleanup_failures=_cleanup_notes(exc))
    if recorded != "recorded":
        error.add_note(f"recording the FAILED marker also failed ({recorded}); the original failure above is the cause")
    return error


__all__ = [
    "COPY_CHUNK_BYTES", "FAILED_FORMAT", "FAILED_REASON_CLASSES", "RUNNING_FORMAT", "ArtifactMetadata", "ArtifactReservation", "ArtifactWriteError",
    "DurabilityMode", "DurabilityReport", "DurabilityUnsupported", "FileSystemOps", "OwnedPartial", "PostPublicationError", "PreconditionError",
    "ReservationStateError", "WriteFailed", "WrittenArtifact", "reserve_artifact", "write_artifact", "write_failed_record",
]
