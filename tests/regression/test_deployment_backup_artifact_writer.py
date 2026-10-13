"""
Phase 9B-5B: tests for the local write side of the artifact contract
(`src/deployment/backup_artifact_writer.py`).

All inputs are synthetic and every digest is genuinely computed. Failures are
injected through `FileSystemOps`, the writer's single seam for filesystem and
clock operations, at the write, flush, marker, validation and publication
boundaries. Fault injection shows what the writer does when an operation fails;
it is NOT evidence that data survives an actual power loss, and no test here
claims that.

Every outcome is checked against the PUBLIC, unchanged verifier from
`backup_artifact.py`, never against the writer's own opinion of itself. Mutating
code lives only in the writer, so the read side's import and no-write tests stay
meaningful (and are still run alongside these).
"""
from __future__ import annotations

import ast
import collections
import dataclasses
import datetime as dt
import errno
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tracemalloc
from pathlib import Path

import pytest

import src.deployment.backup_artifact_writer as w
from src.deployment.backup_artifact import (
    ArtifactManifest,
    BaselineDocument,
    SchemaState,
    verify_artifact_directory,
)
from src.deployment.backup_artifact_writer import (
    ArtifactMetadata,
    DurabilityMode,
    DurabilityUnsupported,
    FileSystemOps,
    OwnedPartial,
    PostPublicationError,
    PreconditionError,
    WriteFailed,
    write_artifact,
    write_failed_record,
)
from tests.regression.test_deployment_backup_artifact import (
    ARTIFACT_ID,
    CAPTURED,
    DIR_NAME,
    FINISHED,
    LOGICAL,
    OTHER_ID,
    baseline_doc,
    make_directory_link,
    make_symlink,
    manifest_doc,
    oracle,
    sha256,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WRITER_PATH = REPO_ROOT / "src" / "deployment" / "backup_artifact_writer.py"
FIXED_NOW = dt.datetime(2026, 10, 2, 19, 0, 0, 123456, tzinfo=dt.timezone.utc)
FIXED_NOW_TEXT = "2026-10-02T19:00:00.123456Z"
ARCHIVE_BYTES = (b"PGDMP synthetic archive: the writer never parses a dump\n" * 2000) + bytes(range(256)) * 40
MODE = DurabilityMode.REQUIRED
EXPECTED_FILES = {"archive.dump", "baseline.json", "manifest.json", "COMPLETE"}


# --------------------------------------------------------------------------- inputs
@dataclasses.dataclass
class Inputs:
    root: Path
    store: Path
    archive: Path
    metadata: ArtifactMetadata
    baseline: BaselineDocument
    final_name: str
    final: Path
    partial: Path
    bystander: Path


def build_inputs(tmp_path: Path, *, artifact_id: str = ARTIFACT_ID, archive_bytes: bytes = ARCHIVE_BYTES, root_name: str = "case") -> Inputs:
    root = tmp_path / root_name
    store = root / "store"
    store.mkdir(parents=True)
    (root / "input").mkdir()
    archive = root / "input" / "dump.bin"
    archive.write_bytes(archive_bytes)
    bystander = root / "bystander"
    bystander.mkdir()
    (bystander / "keep.txt").write_text("unrelated")
    bdoc = baseline_doc()
    bdoc["artifact_id"] = artifact_id
    mdoc = manifest_doc(archive_bytes, bdoc=bdoc)
    mdoc["artifact_id"] = artifact_id
    manifest = ArtifactManifest.from_document(mdoc)
    metadata = ArtifactMetadata(artifact_id=artifact_id, created_utc=manifest.created_utc, finished_utc=manifest.finished_utc, source=manifest.source,
                                schema=manifest.schema, repository=manifest.repository, tooling=manifest.tooling, consistency=manifest.consistency,
                                archive=manifest.archive)
    final_name = f"napsedge-{LOGICAL}-20261002T183000Z-{artifact_id}"
    return Inputs(root, store, archive, metadata, BaselineDocument.from_document(bdoc), final_name, store / final_name, store / f".partial-{artifact_id}", bystander)


def run(inputs: Inputs, **kwargs):
    kwargs.setdefault("durability", MODE)
    return write_artifact(inputs.store, inputs.archive, inputs.metadata, inputs.baseline, **kwargs)


def names(directory: Path) -> list[str]:
    return sorted(entry.name for entry in os.scandir(directory))


def file_sha(path: Path) -> str:
    return sha256(path.read_bytes())


def read_failed_record(partial: Path) -> dict:
    return json.loads((partial / "FAILED").read_bytes())


# --------------------------------------------------------------------------- the fault-injecting seam
class Probe(FileSystemOps):
    """
    Records every mutating operation in order and can fail or alter chosen ones.
    `fail[(method, label)]` is an exception instance (raised on every hit) or
    `(n, exception)` (raised on the n-th hit only). Labels are file base names,
    `<source>` for the input archive, and `staging` / `parent` for directory flushes.
    `directory_flush` is "fake_ok" (records only, so the logic is exercised on every
    platform), "unsupported", "real" (POSIX only), or "descriptor": the PRODUCTION
    `fsync_directory_descriptor` runs on every platform against a descriptor of the
    `scratch` regular file, with the flush itself raising `dir_flush_errors[label]`.

    Descriptor ownership is tracked: `opened` and `closed` list the labels in order,
    `bad_closes` any close of a descriptor that is not currently open (the real
    descriptor is then left alone). `close` really releases the descriptor FIRST and then
    raises the injected error, as a failing `close(2)` does.
    """

    def __init__(self, *, fail=None, directory_flush: str = "fake_ok", hooks=None, corrupt_write=None, rename_then_raise=None,
                 scratch=None, dir_flush_errors=None) -> None:
        self.events: list[tuple[str, str]] = []
        self.fail = dict(fail or {})
        self.hits: collections.Counter = collections.Counter()
        self.names: dict[int, str] = {}
        self.directory_flush = directory_flush
        self.hooks = dict(hooks or {})
        self.corrupt_write = corrupt_write
        self.rename_then_raise = rename_then_raise
        self.scratch = scratch
        self.dir_flush_errors = dict(dir_flush_errors or {})
        self.live: dict[int, str] = {}
        self.opened: list[str] = []
        self.closed: list[str] = []
        self.bad_closes: list[int] = []

    def _register(self, descriptor: int, label: str) -> int:
        self.names[descriptor] = label
        self.live[descriptor] = label
        self.opened.append(label)
        return descriptor

    def assert_every_descriptor_closed_exactly_once(self) -> None:
        assert self.bad_closes == [], "a descriptor that was not open was closed (a retry or a double close)"
        assert self.live == {}, f"descriptors left open: {sorted(self.live.values())}"
        assert collections.Counter(self.opened) == collections.Counter(self.closed), "each owned descriptor needs exactly one close attempt"

    def _gate(self, method: str, label: str, *, record: bool = True) -> None:
        if record:
            self.events.append((method, label))
        self.hits[(method, label)] += 1
        spec = self.fail.get((method, label))
        if spec is None:
            return
        nth, error = spec if isinstance(spec, tuple) else (None, spec)
        if nth is None or self.hits[(method, label)] == nth:
            raise error

    def lstat(self, path):
        self._gate("lstat", os.path.basename(os.fspath(path)), record=False)
        return super().lstat(path)

    def mkdir_private(self, path):
        self._gate("mkdir_private", os.path.basename(path))
        super().mkdir_private(path)

    def mkdir_reservation(self, path):
        self._gate("mkdir_reservation", os.path.basename(path))
        super().mkdir_reservation(path)

    def open_source(self, path):
        self._gate("open_source", os.path.basename(path))
        return self._register(super().open_source(path), "<source>")

    def open_exclusive(self, path):
        label = os.path.basename(path)
        self._gate("open_exclusive", label)
        return self._register(super().open_exclusive(path), label)

    def open_directory(self, path):
        return self._register(super().open_directory(path), "staging" if os.path.basename(path).startswith(".partial-") else "parent")

    def close(self, descriptor):
        label = self.live.pop(descriptor, None)
        if label is None:
            self.bad_closes.append(descriptor)
            raise OSError(errno.EBADF, "not an open owned descriptor")
        self.closed.append(label)
        super().close(descriptor)
        self._gate("close", label, record=False)

    def read(self, descriptor, size):
        label = self.names.get(descriptor, "?")
        self._gate("read", label, record=False)
        data = super().read(descriptor, size)
        hook = self.hooks.get("after_read")
        if hook is not None:
            hook(self.hits[("read", label)])
        return data

    def write(self, descriptor, data):
        label = self.names.get(descriptor, "?")
        self._gate("write", label)
        if self.corrupt_write is not None and self.corrupt_write == (label, self.hits[("write", label)]):
            data = bytes([data[0] ^ 0xFF]) + data[1:]
        return super().write(descriptor, data)

    def fsync_file(self, descriptor):
        self._gate("fsync_file", self.names.get(descriptor, "?"))
        super().fsync_file(descriptor)

    def fsync_directory(self, path):
        label = "staging" if os.path.basename(path).startswith(".partial-") else "parent"
        self._gate("fsync_directory", label)
        if self.directory_flush == "unsupported":
            raise DurabilityUnsupported("simulated: directory flushes are unsupported")
        if self.directory_flush == "real":
            super().fsync_directory(path)
        elif self.directory_flush == "descriptor":
            descriptor = self._register(os.open(self.scratch, os.O_RDWR | getattr(os, "O_BINARY", 0)), label)
            spec = self.dir_flush_errors.get(label)  # (n, exception): the flush raises on the n-th directory flush with that label
            real_fsync, injected = os.fsync, (spec[1] if spec and self.hits[("fsync_directory", label)] == spec[0] else None)

            def gated(target):
                if target == descriptor and injected is not None:
                    raise injected
                return real_fsync(target)

            os.fsync = gated
            try:
                self.fsync_directory_descriptor(descriptor)
            finally:
                os.fsync = real_fsync

    def unlink(self, path):
        self._gate("unlink", os.path.basename(path))
        super().unlink(path)

    def rename(self, source, destination):
        self._gate("rename", os.path.basename(source))
        super().rename(source, destination)
        if self.rename_then_raise is not None:
            raise self.rename_then_raise

    def rmdir(self, path):
        self._gate("rmdir", os.path.basename(path))
        super().rmdir(path)

    def now(self):
        return FIXED_NOW

    def pid(self):
        return 4242

    def checkpoint(self, name, path):
        self.events.append(("checkpoint", name))
        hook = self.hooks.get(name)
        if hook is not None:
            hook(path)


def tree_snapshot(path: Path) -> dict:
    """Relative path -> kind, and for files size, mtime and content hash. Directory mtimes are excluded on purpose."""
    result: dict = {}
    for root, directories, files in os.walk(path, followlinks=False):
        for name in sorted(directories + files):
            full = os.path.join(root, name)
            relative = os.path.relpath(full, path)
            info = os.lstat(full)
            if stat.S_ISDIR(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 0x400):
                result[relative] = ("dir",)
            elif stat.S_ISREG(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 0x400):
                result[relative] = ("file", info.st_size, info.st_mtime_ns, sha256(Path(full).read_bytes()))
            else:
                result[relative] = ("other", stat.S_IFMT(info.st_mode))
    return result


MUTATING = {"mkdir_private", "mkdir_reservation", "open_exclusive", "write", "fsync_file", "unlink", "rename", "rmdir"}


def assert_untouched(inputs: Inputs, archive_sha: str, bystander_before: dict) -> None:
    assert file_sha(inputs.archive) == archive_sha, "the input archive changed"
    assert tree_snapshot(inputs.bystander) == bystander_before, "an unrelated directory changed"


# =========================================================================== 1. success, judged by the unchanged public verifier
def test_a_successful_write_passes_the_unchanged_public_verifier_and_leaves_exactly_the_four_files(tmp_path):
    inputs = build_inputs(tmp_path)
    archive_sha, bystander_before = file_sha(inputs.archive), tree_snapshot(inputs.bystander)
    result = run(inputs, ops=Probe())
    assert result.path == str(inputs.final) and inputs.final.is_dir()
    assert names(inputs.final) == sorted(EXPECTED_FILES), "exactly four files, no RUNNING and no FAILED"
    assert names(inputs.store) == [inputs.final_name], "no partial directory is left behind by a success"
    verdict = verify_artifact_directory(inputs.final)
    assert verdict.valid and verdict.status == "STRUCTURALLY_VALID", [str(r) for r in verdict.reasons]
    assert_untouched(inputs, archive_sha, bystander_before)


def test_success_claims_file_completion_and_structure_only(tmp_path):
    result = run(build_inputs(tmp_path), ops=Probe())
    assert result.establishes == ("files_complete", "staged_structure_validated")
    assert {"database_consistency", "restore_verified", "authenticity"} <= set(result.does_not_establish)
    assert not {"database_consistency", "restore_verified", "authenticity"} & set(result.establishes)
    assert result.durability.atomic_rename is True


def test_supplied_metadata_is_recorded_exactly_and_nothing_is_invented(tmp_path):
    inputs = build_inputs(tmp_path)
    verdict = verify_artifact_directory(run(inputs, ops=Probe()).path)
    m, supplied = verdict.manifest, inputs.metadata
    assert (m.artifact_id, m.created_utc, m.finished_utc) == (supplied.artifact_id, supplied.created_utc, supplied.finished_utc)
    assert m.source == supplied.source and m.schema == supplied.schema and m.repository == supplied.repository
    assert m.tooling == supplied.tooling and m.consistency == supplied.consistency and m.archive == supplied.archive
    assert verdict.baseline == inputs.baseline
    assert m.baseline.table_count == len(inputs.baseline.fingerprints) and m.baseline.row_total == sum(f.rows for f in inputs.baseline.fingerprints)
    assert m.consistency.snapshot_descriptor == "1042:1042:", "capture evidence is the caller's, copied verbatim"


def test_sizes_and_digests_are_those_of_the_bytes_actually_written(tmp_path):
    inputs = build_inputs(tmp_path)
    result = run(inputs, ops=Probe())
    on_disk = {name: (inputs.final / name).read_bytes() for name in EXPECTED_FILES}
    manifest = verify_artifact_directory(inputs.final).manifest
    assert on_disk["archive.dump"] == ARCHIVE_BYTES
    assert (manifest.files[0].bytes, manifest.files[0].sha256) == (len(ARCHIVE_BYTES), sha256(ARCHIVE_BYTES)) == (result.archive_bytes, result.archive_sha256)
    assert (manifest.files[1].bytes, manifest.files[1].sha256) == (len(on_disk["baseline.json"]), sha256(on_disk["baseline.json"]))
    assert (result.manifest_bytes, result.manifest_sha256) == (len(on_disk["manifest.json"]), sha256(on_disk["manifest.json"]))
    assert on_disk["COMPLETE"] == (f"napsedge.backup-complete/1\nartifact_id={ARTIFACT_ID}\nmanifest_sha256={result.manifest_sha256}\nmanifest_bytes={result.manifest_bytes}\n").encode()


def test_documents_are_the_contracts_canonical_serialization(tmp_path):
    inputs = build_inputs(tmp_path)
    run(inputs, ops=Probe())
    for name in ("manifest.json", "baseline.json"):
        raw = (inputs.final / name).read_bytes()
        assert raw == oracle(json.loads(raw)), f"{name} is not canonical"


def test_flushed_files_are_reported_in_write_order(tmp_path):
    result = run(build_inputs(tmp_path), ops=Probe())
    assert result.durability.files_flushed == ("RUNNING", "archive.dump", "baseline.json", "manifest.json", "COMPLETE")


def test_the_documented_finalization_order_is_followed_exactly(tmp_path):
    inputs = build_inputs(tmp_path)
    probe = Probe()
    run(inputs, ops=probe)
    events = [event for event in probe.events if event[0] != "checkpoint"]
    first: dict = {}
    for i, event in enumerate(events):
        first.setdefault(event, i)
    # In REQUIRED mode the first event is a read-only probe that the store directory can be flushed; it creates nothing.
    assert events[0] == ("fsync_directory", "parent") and events[1] == ("open_source", "dump.bin") and events[2] == ("mkdir_private", f".partial-{ARTIFACT_ID}"), (
        "after the read-only probe and the read-only open of the input, the private staging directory is the first thing created")
    assert not any(method in {"open_exclusive", "mkdir_reservation", "rename", "unlink"} for method, _ in events[:3])
    order = [("mkdir_private", f".partial-{ARTIFACT_ID}"), ("open_exclusive", "RUNNING"), ("open_exclusive", "archive.dump"), ("fsync_file", "archive.dump"),
             ("open_exclusive", "baseline.json"), ("fsync_file", "baseline.json"), ("open_exclusive", "manifest.json"), ("fsync_file", "manifest.json"),
             ("unlink", "RUNNING"), ("fsync_directory", "staging"), ("open_exclusive", "COMPLETE"), ("fsync_file", "COMPLETE"),
             ("rename", f".partial-{ARTIFACT_ID}")]
    positions = [first[event] for event in order]
    assert positions == sorted(positions), f"events out of the documented order: {order}"
    last_parent_flush = max(i for i, event in enumerate(events) if event == ("fsync_directory", "parent"))
    assert last_parent_flush > first[("rename", f".partial-{ARTIFACT_ID}")], "the parent directory is flushed after the rename"
    creations = [label for method, label in events if method == "open_exclusive"]
    assert creations == ["RUNNING", "archive.dump", "baseline.json", "manifest.json", "COMPLETE"], "COMPLETE is the last file created"
    assert events.index(("unlink", "RUNNING")) < events.index(("open_exclusive", "COMPLETE")), "RUNNING is removed before COMPLETE is written"
    assert events.index(("rename", f".partial-{ARTIFACT_ID}")) > events.index(("fsync_file", "COMPLETE"))


def test_staged_contents_are_valid_by_the_contracts_rules_while_the_public_verifier_still_refuses_the_partial(tmp_path):
    inputs = build_inputs(tmp_path)
    seen: dict = {}

    def inspect(path):
        seen["listing"] = names(Path(path))
        seen["public_verdict_on_partial"] = [r.code for r in verify_artifact_directory(path).reasons]

    run(inputs, ops=Probe(hooks={"before_validation": inspect}))
    assert seen["listing"] == sorted(EXPECTED_FILES), "at validation time RUNNING is already gone and COMPLETE is present"
    assert seen["public_verdict_on_partial"] == ["NOT_FINAL"], "the public verifier's refusal of .partial directories is unchanged"


def test_the_archive_is_streamed_not_loaded(tmp_path, monkeypatch):
    big = b"\x5a" * (1 << 20)
    inputs = build_inputs(tmp_path, archive_bytes=big * 16)
    tracemalloc.start()
    try:
        result = run(inputs, ops=Probe())
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.archive_bytes == 16 * (1 << 20)
    assert peak < 8 * (1 << 20), f"peak traced memory {peak}: the 16 MiB archive appears to have been loaded"


def test_chunked_copying_with_a_small_chunk_size_is_byte_exact(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "COPY_CHUNK_BYTES", 1000)
    inputs = build_inputs(tmp_path)
    probe = Probe()
    run(inputs, ops=probe)
    assert (inputs.final / "archive.dump").read_bytes() == ARCHIVE_BYTES
    assert probe.hits[("write", "archive.dump")] >= len(ARCHIVE_BYTES) // 1000


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission modes are not applied on Windows: privacy coverage NOT exercised there")
def test_posix_staging_and_files_are_private(tmp_path):
    inputs = build_inputs(tmp_path)
    result = run(inputs)
    assert stat.S_IMODE(os.stat(inputs.final).st_mode) & 0o077 == 0
    assert all(stat.S_IMODE(os.stat(inputs.final / n).st_mode) & 0o077 == 0 for n in EXPECTED_FILES)
    assert "0700 verified" in result.privacy


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only check")
def test_a_staging_directory_that_is_not_private_is_refused_on_posix(tmp_path):
    inputs = build_inputs(tmp_path)

    class Permissive(Probe):
        def lstat(self, path):
            result = super().lstat(path)
            if os.path.basename(os.fspath(path)).startswith(".partial-"):
                values = list(result)
                values[0] = stat.S_IFDIR | 0o777
                return os.stat_result(values)
            return result

    with pytest.raises(WriteFailed) as excinfo:
        run(inputs, ops=Permissive())
    assert excinfo.value.stage == "staging" and excinfo.value.published == "no"
    assert not inputs.final.exists()


def test_privacy_is_reported_honestly_on_windows(tmp_path):
    if sys.platform != "win32":
        pytest.skip("Windows-only statement")
    assert "not enforced" in run(build_inputs(tmp_path), ops=Probe()).privacy


# =========================================================================== 2. refusals before anything is created
def metadata_with(inputs: Inputs, **changes) -> ArtifactMetadata:
    return dataclasses.replace(inputs.metadata, **changes)


PRECONDITION_CASES = {
    "logical_name_not_valid": lambda i: ("metadata", dict(metadata=metadata_with(i, source=dataclasses.replace(i.metadata.source, logical_name="Not Valid")))),
    "created_utc_not_a_real_instant": lambda i: ("metadata", dict(metadata=metadata_with(i, created_utc="2026-02-30T18:30:00.000000Z"))),
    "finished_before_created": lambda i: ("metadata", dict(metadata=metadata_with(i, created_utc=FINISHED, finished_utc="2026-10-02T18:30:00.000000Z"))),
    "heads_match_contradicts_the_heads": lambda i: ("metadata", dict(metadata=metadata_with(i, schema=SchemaState("5b02bea0eb85", "5b02bea0eb85", False)))),
    "artifact_id_not_a_uuid4": lambda i: ("metadata", dict(metadata=metadata_with(i, artifact_id="not-a-uuid"))),
    "baseline_names_another_artifact": lambda i: ("metadata", dict(baseline=dataclasses.replace(i.baseline, artifact_id=OTHER_ID))),
    "baseline_version_differs_from_the_schema_head": lambda i: ("metadata", dict(baseline=dataclasses.replace(i.baseline, version="ffffffffffff"))),
    "baseline_version_null_while_the_manifest_names_a_head": lambda i: ("metadata", dict(baseline=dataclasses.replace(i.baseline, version=None))),
    "baseline_captured_outside_the_capture_window": lambda i: ("metadata", dict(baseline=dataclasses.replace(i.baseline, captured_utc="2026-10-02T17:00:00.000000Z"))),
    "baseline_has_a_duplicate_logical_key": lambda i: ("metadata", dict(baseline=dataclasses.replace(i.baseline, fingerprints=i.baseline.fingerprints + (i.baseline.fingerprints[0],)))),
}


@pytest.mark.parametrize("case", sorted(PRECONDITION_CASES))
def test_invalid_metadata_or_baseline_is_refused_before_anything_is_created(case, tmp_path):
    inputs = build_inputs(tmp_path)
    archive_sha, bystander_before, store_before = file_sha(inputs.archive), tree_snapshot(inputs.bystander), names(inputs.store)
    stage, overrides = PRECONDITION_CASES[case](inputs)
    kwargs = {"durability": MODE, "ops": Probe()}
    with pytest.raises(PreconditionError) as excinfo:
        write_artifact(inputs.store, inputs.archive, overrides.get("metadata", inputs.metadata), overrides.get("baseline", inputs.baseline), **kwargs)
    assert excinfo.value.stage == stage and excinfo.value.published == "no" and excinfo.value.partial_path is None
    assert names(inputs.store) == store_before == [], "something was created in the store"
    assert_untouched(inputs, archive_sha, bystander_before)
    assert not any(method in {"mkdir_private", "open_exclusive", "rename"} for method, _ in kwargs["ops"].events), "a filesystem mutation was attempted"


def test_an_empty_archive_is_refused_by_the_contract_before_anything_is_created(tmp_path):
    inputs = build_inputs(tmp_path)
    inputs.archive.write_bytes(b"")
    with pytest.raises(PreconditionError) as excinfo:
        run(inputs, ops=Probe())
    assert excinfo.value.stage == "metadata" and "FIELD_VALUE" in str(excinfo.value)
    assert names(inputs.store) == []


@pytest.mark.parametrize("shape", ["missing", "a_directory"])
def test_an_unusable_archive_path_is_refused_before_anything_is_created(shape, tmp_path):
    inputs = build_inputs(tmp_path)
    if shape == "missing":
        inputs.archive.unlink()
    else:
        inputs.archive.unlink()
        inputs.archive.mkdir()
    with pytest.raises(PreconditionError) as excinfo:
        run(inputs, ops=Probe())
    assert excinfo.value.stage == "archive" and names(inputs.store) == []


def test_an_archive_that_is_a_link_is_refused(tmp_path):
    inputs = build_inputs(tmp_path)
    real = inputs.root / "input" / "real.bin"
    real.write_bytes(ARCHIVE_BYTES)
    inputs.archive.unlink()
    make_symlink(inputs.archive, real, directory=False)
    with pytest.raises(PreconditionError) as excinfo:
        run(inputs, ops=Probe())
    assert excinfo.value.stage == "archive" and names(inputs.store) == []


def test_an_unusable_store_directory_is_refused(tmp_path):
    inputs = build_inputs(tmp_path)
    for store, expected in ((inputs.root / "missing_store", "NOT_A_DIRECTORY"), (inputs.archive, "NOT_A_DIRECTORY")):
        with pytest.raises(PreconditionError) as excinfo:
            write_artifact(store, inputs.archive, inputs.metadata, inputs.baseline, durability=MODE, ops=Probe())
        assert excinfo.value.stage == "store" and expected in str(excinfo.value)
    with pytest.raises(TypeError):
        write_artifact(b"/bytes/store", inputs.archive, inputs.metadata, inputs.baseline, ops=Probe())


def test_a_store_beneath_a_linked_ancestor_is_refused(tmp_path):
    inputs = build_inputs(tmp_path)
    link = inputs.root / "linked_root"
    make_directory_link(link, inputs.root)
    with pytest.raises(PreconditionError) as excinfo:
        write_artifact(link / "store", inputs.archive, inputs.metadata, inputs.baseline, durability=MODE, ops=Probe())
    assert excinfo.value.stage == "store" and "SYMLINK" in str(excinfo.value)
    assert names(inputs.store) == []


# =========================================================================== 3. collisions: nothing existing is replaced, adopted or deleted
def make_destination(inputs: Inputs, shape: str) -> None:
    if shape == "empty_directory":
        inputs.final.mkdir()
    elif shape == "non_empty_directory":
        inputs.final.mkdir()
        (inputs.final / "something.txt").write_text("existing")
    elif shape == "regular_file":
        inputs.final.write_text("a file in the way")
    elif shape == "link_to_a_directory":
        target = inputs.root / "elsewhere"
        target.mkdir()
        make_directory_link(inputs.final, target)
    elif shape == "a_previously_published_artifact":
        other = build_inputs(inputs.root.parent, root_name="published_elsewhere")
        run(other, ops=Probe())
        os.rename(other.final, inputs.final)


@pytest.mark.parametrize("shape", ["empty_directory", "non_empty_directory", "regular_file", "link_to_a_directory", "a_previously_published_artifact"])
def test_an_existing_destination_is_never_replaced_including_an_empty_directory(shape, tmp_path):
    inputs = build_inputs(tmp_path)
    make_destination(inputs, shape)
    archive_sha, before = file_sha(inputs.archive), tree_snapshot(inputs.store)
    probe = Probe()
    with pytest.raises(PreconditionError) as excinfo:
        run(inputs, ops=probe)
    assert "DESTINATION_EXISTS" in str(excinfo.value) and excinfo.value.published == "no"
    assert not any(method in MUTATING for method, _ in probe.events), "a mutating operation was attempted although the destination exists"
    assert tree_snapshot(inputs.store) == before, "the existing destination (or the store) was altered"
    assert not inputs.partial.exists(), "no staging directory may be created when the destination already exists"
    assert file_sha(inputs.archive) == archive_sha


@pytest.mark.parametrize("shape", ["empty", "with_files", "failed_earlier"])
def test_an_existing_partial_directory_is_never_reused_adopted_or_deleted(shape, tmp_path):
    inputs = build_inputs(tmp_path)
    inputs.partial.mkdir()
    if shape != "empty":
        (inputs.partial / "archive.dump").write_bytes(b"half written")
    if shape == "failed_earlier":
        (inputs.partial / "FAILED").write_text('{"reason_class":"timeout"}')
    before = tree_snapshot(inputs.partial)
    probe = Probe()
    with pytest.raises(PreconditionError) as excinfo:
        run(inputs, ops=probe)
    assert "PARTIAL_EXISTS" in str(excinfo.value) and excinfo.value.published == "no"
    assert not any(method in MUTATING for method, _ in probe.events), "a mutating operation was attempted although a partial directory exists"
    assert tree_snapshot(inputs.partial) == before and names(inputs.store) == [inputs.partial.name]


@pytest.mark.parametrize("appears", ["empty_directory", "non_empty_directory"])
def test_a_destination_that_appears_during_the_write_is_not_replaced(appears, tmp_path):
    inputs = build_inputs(tmp_path)
    probe = Probe(hooks={"before_publication": lambda path: (inputs.final.mkdir(), (inputs.final / "x.txt").write_text("late") if appears == "non_empty_directory" else None)})
    with pytest.raises(WriteFailed) as excinfo:
        run(inputs, ops=probe)
    assert excinfo.value.stage == "publication" and excinfo.value.published == "no"
    assert inputs.final.is_dir() and names(inputs.final) == (["x.txt"] if appears == "non_empty_directory" else []), "the pre-existing destination was replaced or altered"
    # 9B-6H follow-up (13.17): the marker this run wrote is removed before FAILED is recorded; the rest of the partial is kept
    assert inputs.partial.is_dir() and names(inputs.partial) == ["FAILED", "archive.dump", "baseline.json", "manifest.json"],         "the finished partial is kept (not deleted, not published) without its completion marker"


# =========================================================================== 4. injected failures: never success, and the owned partial is handled safely
ENOSPC = OSError(errno.ENOSPC, "No space left on device")
EIO = OSError(errno.EIO, "Input/output error")
FAULTS = [
    ("archive_write_no_space_on_the_second_chunk", {("write", "archive.dump"): (2, ENOSPC)}, "archive", "storage_full"),
    ("archive_write_io_error", {("write", "archive.dump"): (1, EIO)}, "archive", "internal_error"),
    ("source_read_io_error", {("read", "<source>"): (2, EIO)}, "archive", "internal_error"),
    ("archive_flush_fails", {("fsync_file", "archive.dump"): EIO}, "archive", "durability_failed"),
    ("archive_flush_no_space", {("fsync_file", "archive.dump"): ENOSPC}, "archive", "storage_full"),
    ("baseline_write_fails", {("write", "baseline.json"): EIO}, "baseline", "internal_error"),
    ("baseline_flush_fails", {("fsync_file", "baseline.json"): EIO}, "baseline", "durability_failed"),
    ("manifest_write_fails", {("write", "manifest.json"): EIO}, "manifest", "internal_error"),
    ("manifest_flush_fails", {("fsync_file", "manifest.json"): EIO}, "manifest", "durability_failed"),
    ("running_create_fails", {("open_exclusive", "RUNNING"): OSError(errno.EACCES, "denied")}, "staging", "internal_error"),
    ("running_flush_fails", {("fsync_file", "RUNNING"): EIO}, "staging", "durability_failed"),
    ("running_removal_fails", {("unlink", "RUNNING"): OSError(errno.EACCES, "denied")}, "running_removal", "internal_error"),
    ("staging_directory_flush_after_running_removal_fails", {("fsync_directory", "staging"): (1, EIO)}, "flush_after_running_removal", "durability_failed"),
    ("marker_create_fails", {("open_exclusive", "COMPLETE"): OSError(errno.EACCES, "denied")}, "marker", "internal_error"),
    ("marker_write_fails", {("write", "COMPLETE"): EIO}, "marker", "internal_error"),
    ("marker_flush_fails", {("fsync_file", "COMPLETE"): EIO}, "marker", "durability_failed"),
    ("staging_directory_flush_after_marker_fails", {("fsync_directory", "staging"): (2, EIO)}, "flush_after_marker", "durability_failed"),
    ("rename_fails_as_a_cross_filesystem_move_would", {("rename", f".partial-{ARTIFACT_ID}"): OSError(errno.EXDEV, "Invalid cross-device link")}, "publication", "internal_error"),
]


@pytest.mark.parametrize("name, fail, stage, reason", FAULTS, ids=[f[0] for f in FAULTS])
def test_an_injected_failure_never_reports_success_and_leaves_a_failed_unadopted_partial(name, fail, stage, reason, tmp_path, monkeypatch):
    monkeypatch.setattr(w, "COPY_CHUNK_BYTES", 4096)  # several writes per file, so "the Nth write" exists
    inputs = build_inputs(tmp_path)
    archive_sha, bystander_before = file_sha(inputs.archive), tree_snapshot(inputs.bystander)
    probe = Probe(fail=fail)
    with pytest.raises(WriteFailed) as excinfo:
        run(inputs, ops=probe)
    error = excinfo.value
    assert (error.stage, error.reason_class, error.published, error.failed_record) == (stage, reason, "no", "recorded")
    assert error.partial_path == str(inputs.partial) and isinstance(error.__cause__, BaseException)
    assert names(inputs.store) == [inputs.partial.name], "only the owned partial remains: no final directory, nothing else, nothing deleted"
    assert [r.code for r in verify_artifact_directory(inputs.final).reasons] == ["NOT_A_DIRECTORY"]
    assert [r.code for r in verify_artifact_directory(inputs.partial).reasons] == ["NOT_FINAL"]
    record = read_failed_record(inputs.partial)
    assert record["reason_class"] == reason and set(record) == {"format", "artifact_id", "utc", "reason_class"}
    if name == "running_removal_fails":
        # the injected fault applies to EVERY removal of RUNNING, including the FAILED recorder's own best-effort one: both markers remain,
        # the directory is still unpublished and invalid, and the record is still written
        assert (inputs.partial / "RUNNING").exists() and (inputs.partial / "FAILED").exists()
    else:
        assert (inputs.partial / "RUNNING").exists() is False, "FAILED supersedes RUNNING"
    assert_untouched(inputs, archive_sha, bystander_before)
    if name == "rename_fails_as_a_cross_filesystem_move_would":
        assert [e for e in probe.events if e[0] == "rename"] == [("rename", f".partial-{ARTIFACT_ID}")], "exactly one rename attempt: no copy or move fallback"
        assert not inputs.final.exists(), "the reservation (POSIX) was cleaned up and nothing was published"


def test_a_failed_partial_that_reached_complete_is_not_valid_even_if_renamed_by_hand(tmp_path):
    """9B-6H follow-up (13.17): the marker written by this run is removed before FAILED, so the hand-renamed directory has no COMPLETE."""
    inputs = build_inputs(tmp_path)
    with pytest.raises(WriteFailed):
        run(inputs, ops=Probe(fail={("rename", f".partial-{ARTIFACT_ID}"): OSError(errno.EXDEV, "cross-device")}))
    assert not (inputs.partial / "COMPLETE").exists() and (inputs.partial / "FAILED").exists()
    os.rename(inputs.partial, inputs.final)
    verdict = verify_artifact_directory(inputs.final)
    assert verdict.status == "INVALID" and {"ARTIFACT_FAILED", "MISSING_FILE"} <= set(r.code for r in verdict.reasons)


def test_a_failed_partial_whose_marker_could_not_be_removed_keeps_both_markers_and_is_not_valid(tmp_path):
    """If the marker removal itself fails, COMPLETE stays beside FAILED: still never adoptable (the pre-follow-up state)."""
    inputs = build_inputs(tmp_path)
    with pytest.raises(WriteFailed) as excinfo:
        run(inputs, ops=Probe(fail={("rename", f".partial-{ARTIFACT_ID}"): OSError(errno.EXDEV, "cross-device"),
                                    ("unlink", "COMPLETE"): OSError(errno.EACCES, "denied")}))
    assert any("completion marker" in note for note in excinfo.value.cleanup_failures)
    assert (inputs.partial / "COMPLETE").exists() and (inputs.partial / "FAILED").exists()
    os.rename(inputs.partial, inputs.final)
    verdict = verify_artifact_directory(inputs.final)
    assert verdict.status == "INVALID" and "CONFLICTING_MARKERS" in [r.code for r in verdict.reasons]


def test_a_failed_partial_that_never_reached_complete_is_not_valid_even_if_renamed_by_hand(tmp_path):
    inputs = build_inputs(tmp_path)
    with pytest.raises(WriteFailed):
        run(inputs, ops=Probe(fail={("fsync_file", "baseline.json"): EIO}))
    os.rename(inputs.partial, inputs.final)
    verdict = verify_artifact_directory(inputs.final)
    assert verdict.status == "INVALID" and "ARTIFACT_FAILED" in [r.code for r in verdict.reasons]


# ---- validation boundary
def test_staged_corruption_is_caught_by_validation_before_publication(tmp_path):
    inputs = build_inputs(tmp_path)

    def corrupt(path):
        target = Path(path) / "archive.dump"
        data = target.read_bytes()
        target.write_bytes(data[:100] + bytes([data[100] ^ 0x01]) + data[101:])

    with pytest.raises(WriteFailed) as excinfo:
        run(inputs, ops=Probe(hooks={"before_validation": corrupt}))
    assert (excinfo.value.stage, excinfo.value.reason_class, excinfo.value.published) == ("validation", "checksum_mismatch", "no")
    assert "FILE_DIGEST_MISMATCH" in str(excinfo.value.__cause__)
    assert not inputs.final.exists() and (inputs.partial / "FAILED").exists()


def test_an_extra_staged_file_is_caught_by_validation(tmp_path):
    inputs = build_inputs(tmp_path)
    with pytest.raises(WriteFailed) as excinfo:
        run(inputs, ops=Probe(hooks={"before_validation": lambda path: (Path(path) / "stray.txt").write_text("x")}))
    assert (excinfo.value.stage, excinfo.value.reason_class) == ("validation", "internal_error") and "UNLISTED_FILE" in str(excinfo.value.__cause__)
    assert not inputs.final.exists()


def test_bytes_corrupted_on_their_way_to_disk_are_caught_because_validation_reads_the_written_file_back(tmp_path):
    inputs = build_inputs(tmp_path)
    with pytest.raises(WriteFailed) as excinfo:
        run(inputs, ops=Probe(corrupt_write=("archive.dump", 1)))
    assert excinfo.value.reason_class == "checksum_mismatch" and "FILE_DIGEST_MISMATCH" in str(excinfo.value.__cause__)
    assert not inputs.final.exists()


def test_an_archive_that_changes_while_it_is_copied_is_not_published(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "COPY_CHUNK_BYTES", 4096)
    inputs = build_inputs(tmp_path)

    def grow(read_number):
        if read_number == 1:
            with open(inputs.archive, "ab") as handle:
                handle.write(b"appended by another process")

    with pytest.raises(WriteFailed) as excinfo:
        run(inputs, ops=Probe(hooks={"after_read": grow}))
    assert (excinfo.value.stage, excinfo.value.reason_class) == ("archive", "checksum_mismatch")
    assert not inputs.final.exists() and inputs.partial.is_dir()


# ---- after the rename
def test_a_failure_flushing_the_parent_after_the_rename_reports_a_published_artifact_and_deletes_nothing(tmp_path):
    inputs = build_inputs(tmp_path)
    flushes = 2  # the REQUIRED-mode probe of the store directory is the first "parent" flush; the post-publication flush is the second
    with pytest.raises(PostPublicationError) as excinfo:
        run(inputs, ops=Probe(fail={("fsync_directory", "parent"): (flushes, EIO)}))
    error = excinfo.value
    assert (error.stage, error.published, error.reason_class, error.failed_record) == ("flush_parent", "yes", "durability_failed", "not_attempted")
    assert "nothing was deleted" in str(error)
    assert inputs.final.is_dir() and names(inputs.store) == [inputs.final_name], "the published directory stays; there is no partial and no FAILED record"
    assert verify_artifact_directory(inputs.final).valid, "the artifact is intact; only the parent's flush failed"


def test_a_rename_that_raises_after_taking_effect_is_reported_as_possibly_published_and_nothing_is_deleted(tmp_path):
    inputs = build_inputs(tmp_path)
    with pytest.raises(PostPublicationError) as excinfo:
        run(inputs, ops=Probe(rename_then_raise=OSError(errno.EIO, "reported after the fact")))
    assert excinfo.value.stage == "publication" and excinfo.value.published == "yes"
    assert "may already be published" in str(excinfo.value) and "nothing was deleted" in str(excinfo.value)
    assert verify_artifact_directory(inputs.final).valid and names(inputs.store) == [inputs.final_name]


def test_an_unclassifiable_rename_outcome_is_reported_as_unknown_not_as_absent(tmp_path):
    inputs = build_inputs(tmp_path)
    state = {"renamed": False}

    class Murky(Probe):
        def rename(self, source, destination):
            super().rename(source, destination)
            state["renamed"] = True
            raise OSError(errno.EIO, "raised after the fact")

        def lstat(self, path):
            if state["renamed"]:
                raise OSError(errno.EIO, "cannot inspect")
            return super().lstat(path)

    with pytest.raises(PostPublicationError) as excinfo:
        run(inputs, ops=Murky())
    assert excinfo.value.published == "unknown" and "may already be published" in str(excinfo.value)
    assert inputs.final.is_dir(), "nothing was deleted"


def test_a_repeated_attempt_after_success_is_refused_and_leaves_the_published_artifact_untouched(tmp_path):
    inputs = build_inputs(tmp_path)
    run(inputs, ops=Probe())
    before = tree_snapshot(inputs.final)
    with pytest.raises(PreconditionError) as excinfo:
        run(inputs, ops=Probe())
    assert "DESTINATION_EXISTS" in str(excinfo.value) and tree_snapshot(inputs.final) == before


def test_a_repeated_attempt_after_a_failure_is_refused_while_a_new_artifact_id_still_succeeds(tmp_path):
    inputs = build_inputs(tmp_path)
    with pytest.raises(WriteFailed):
        run(inputs, ops=Probe(fail={("fsync_file", "baseline.json"): EIO}))
    partial_before = tree_snapshot(inputs.partial)
    with pytest.raises(PreconditionError) as excinfo:
        run(inputs, ops=Probe())
    assert "PARTIAL_EXISTS" in str(excinfo.value) and tree_snapshot(inputs.partial) == partial_before, "the failed partial was neither adopted nor touched"
    fresh = build_inputs(tmp_path, artifact_id=OTHER_ID, root_name="second")
    fresh_in_same_store = dataclasses.replace(fresh, store=inputs.store, final=inputs.store / fresh.final_name, partial=inputs.store / fresh.partial.name)
    result = run(fresh_in_same_store, ops=Probe())
    assert verify_artifact_directory(result.path).valid and inputs.partial.is_dir()
    assert sorted(names(inputs.store)) == sorted([inputs.partial.name, fresh.final_name])


# =========================================================================== 5. the FAILED record
def test_the_failed_record_is_fixed_shape_bounded_and_free_of_credentials_and_stack_traces(tmp_path):
    inputs = build_inputs(tmp_path)
    leaky = OSError(errno.EIO, "postgresql://backup:SUPERSECRET@db.internal/napsedge Traceback (most recent call last): password=hunter2")
    with pytest.raises(WriteFailed) as excinfo:
        run(inputs, ops=Probe(fail={("write", "baseline.json"): leaky}))
    raw = (inputs.partial / "FAILED").read_bytes()
    record = json.loads(raw)
    assert raw == oracle(record) and len(raw) <= w.MAX_FAILED_RECORD_BYTES
    assert record == {"format": "napsedge.backup-failed/1", "artifact_id": ARTIFACT_ID, "utc": FIXED_NOW_TEXT, "reason_class": "internal_error"}
    for secret in (b"SUPERSECRET", b"postgresql://", b"Traceback", b"hunter2", b"backup:"):
        assert secret not in raw
    message = str(excinfo.value)
    assert "SUPERSECRET" not in message and "hunter2" not in message and "Traceback" not in message, "the raised message carries class names only"
    assert "SUPERSECRET" in str(excinfo.value.__cause__), "the original failure is preserved as the cause"


def test_every_documented_failure_class_is_a_valid_record(tmp_path):
    for index, reason in enumerate(sorted(w.FAILED_REASON_CLASSES)):
        parent = tmp_path / f"c{index}"
        partial = parent / f".partial-{ARTIFACT_ID}"
        partial.mkdir(parents=True)
        write_failed_record(OwnedPartial(str(partial), ARTIFACT_ID), reason, ops=Probe())
        assert read_failed_record(partial)["reason_class"] == reason


def test_the_failed_writer_refuses_bad_input_and_never_overwrites(tmp_path):
    partial = tmp_path / f".partial-{ARTIFACT_ID}"
    partial.mkdir()
    with pytest.raises(ValueError):
        write_failed_record(OwnedPartial(str(partial), ARTIFACT_ID), "something free-form: postgresql://u:p@h/db", ops=Probe())
    with pytest.raises(ValueError):
        write_failed_record(OwnedPartial(str(tmp_path / "not-a-partial"), ARTIFACT_ID), "timeout", ops=Probe())
    assert names(partial) == [], "a refused request wrote nothing"
    write_failed_record(OwnedPartial(str(partial), ARTIFACT_ID), "timeout", ops=Probe())
    first = (partial / "FAILED").read_bytes()
    with pytest.raises(FileExistsError):
        write_failed_record(OwnedPartial(str(partial), ARTIFACT_ID), "internal_error", ops=Probe())
    assert (partial / "FAILED").read_bytes() == first, "an existing FAILED record was overwritten"


def test_a_failure_to_record_failed_never_hides_the_original_failure(tmp_path):
    inputs = build_inputs(tmp_path)
    with pytest.raises(WriteFailed) as excinfo:
        run(inputs, ops=Probe(fail={("fsync_file", "baseline.json"): EIO, ("open_exclusive", "FAILED"): OSError(errno.EACCES, "cannot record")}))
    error = excinfo.value
    assert error.stage == "baseline" and error.reason_class == "durability_failed", "the ORIGINAL failure is what is reported"
    assert error.failed_record.startswith("failed:") and any("recording the FAILED marker also failed" in note for note in error.__notes__)
    assert isinstance(error.__cause__, Exception) and "flushing baseline.json failed" in str(error.__cause__)
    assert not (inputs.partial / "FAILED").exists() and not inputs.final.exists()
    assert [r.code for r in verify_artifact_directory(inputs.partial).reasons] == ["NOT_FINAL"], "still invalid without a FAILED record"


def test_an_interrupt_is_recorded_as_interrupted_and_propagates_unwrapped(tmp_path):
    inputs = build_inputs(tmp_path)
    with pytest.raises(KeyboardInterrupt) as excinfo:
        run(inputs, ops=Probe(fail={("write", "baseline.json"): KeyboardInterrupt()}))
    assert read_failed_record(inputs.partial)["reason_class"] == "interrupted"
    assert any("FAILED record (interrupted) was recorded" in note for note in excinfo.value.__notes__)
    assert not inputs.final.exists()


# =========================================================================== 6. durability, stated and reported honestly
def test_a_complete_flush_report_has_no_gaps_when_every_directory_flush_is_obtained(tmp_path):
    report = run(build_inputs(tmp_path), ops=Probe(directory_flush="fake_ok")).durability
    assert report.directories_flushed == ("staging after RUNNING removal", "staging after COMPLETE", "parent after publication")
    assert report.gaps == () and report.durable is True and report.mode == "required"
    assert "not proof of survival across power loss" in report.note


def test_required_durability_is_refused_before_anything_is_created_when_directory_flushes_are_unsupported(tmp_path):
    inputs = build_inputs(tmp_path)
    with pytest.raises(PreconditionError) as excinfo:
        run(inputs, ops=Probe(directory_flush="unsupported"), durability=DurabilityMode.REQUIRED)
    assert excinfo.value.stage == "durability" and "BEST_EFFORT_REPORTED" in str(excinfo.value)
    assert names(inputs.store) == []


def test_best_effort_proceeds_but_reports_every_skipped_directory_flush_as_a_gap(tmp_path):
    inputs = build_inputs(tmp_path)
    result = run(inputs, ops=Probe(directory_flush="unsupported"), durability=DurabilityMode.BEST_EFFORT_REPORTED)
    report = result.durability
    assert report.durable is False and report.directories_flushed == () and len(report.gaps) == 3
    assert all("unsupported" in gap for gap in report.gaps) and report.mode == "best_effort_reported"
    assert any("staging after RUNNING removal" in g for g in report.gaps) and any("parent after publication" in g for g in report.gaps)
    assert report.files_flushed == ("RUNNING", "archive.dump", "baseline.json", "manifest.json", "COMPLETE"), "file flushes are still obtained and reported"
    assert verify_artifact_directory(inputs.final).valid


@pytest.mark.parametrize("mode", list(DurabilityMode))
def test_a_file_flush_failure_is_an_error_in_every_mode(mode, tmp_path):
    inputs = build_inputs(tmp_path)
    with pytest.raises(WriteFailed) as excinfo:
        run(inputs, ops=Probe(directory_flush="fake_ok" if mode is DurabilityMode.REQUIRED else "unsupported", fail={("fsync_file", "manifest.json"): EIO}), durability=mode)
    assert excinfo.value.reason_class == "durability_failed" and not inputs.final.exists()


def test_a_directory_flush_that_fails_for_a_real_reason_is_an_error_even_in_best_effort(tmp_path):
    inputs = build_inputs(tmp_path)
    with pytest.raises(WriteFailed) as excinfo:
        run(inputs, ops=Probe(fail={("fsync_directory", "staging"): (1, EIO)}), durability=DurabilityMode.BEST_EFFORT_REPORTED)
    assert excinfo.value.stage == "flush_after_running_removal" and excinfo.value.reason_class == "durability_failed" and not inputs.final.exists()


def test_real_platform_behaviour_matches_the_documented_support(tmp_path):
    """The REAL operations, no injection: POSIX obtains directory flushes; Windows cannot, so the default refuses and the explicit mode reports gaps."""
    inputs = build_inputs(tmp_path)
    if sys.platform == "win32":
        with pytest.raises(PreconditionError) as excinfo:
            run(inputs, durability=DurabilityMode.REQUIRED)
        assert excinfo.value.stage == "durability" and names(inputs.store) == []
        result = run(inputs, durability=DurabilityMode.BEST_EFFORT_REPORTED)
        assert result.durability.durable is False and len(result.durability.gaps) == 3
    else:
        result = run(inputs, durability=DurabilityMode.REQUIRED)
        assert result.durability.durable is True and len(result.durability.directories_flushed) == 3
    assert verify_artifact_directory(inputs.final).valid and names(inputs.final) == sorted(EXPECTED_FILES)


def test_a_failing_real_fsync_is_an_error_through_the_default_operations(tmp_path, monkeypatch):
    """Injection at the seam proves the writer's handling; this proves the DEFAULT operations do not swallow what the operating system reports."""
    inputs = build_inputs(tmp_path)
    real_fsync = os.fsync
    attempts = []

    def failing_for_files_only(descriptor):
        if stat.S_ISREG(os.fstat(descriptor).st_mode):  # directory flushes (POSIX) stay real, so ONLY the file-flush path can produce the failure
            attempts.append(descriptor)
            raise EIO
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", failing_for_files_only)
    with pytest.raises(WriteFailed) as excinfo:
        run(inputs, durability=DurabilityMode.BEST_EFFORT_REPORTED)
    assert attempts, "the default operations did call the operating system's flush"
    assert excinfo.value.stage == "staging" and excinfo.value.reason_class == "durability_failed" and not inputs.final.exists()
    assert (inputs.partial / "FAILED").exists() and verify_artifact_directory(inputs.partial).valid is False


def test_staged_contents_that_differ_from_the_manifest_that_was_written_are_not_published(tmp_path, monkeypatch):
    """Defence in depth: the digest binding normally catches this first, so the check is exercised by making the shared verifier disagree."""
    inputs = build_inputs(tmp_path)
    real = w._verify_contents

    def disagreeing(root, name, expected):
        verdict = real(root, name, expected)
        return dataclasses.replace(verdict, manifest=dataclasses.replace(verdict.manifest, created_utc="2000-01-01T00:00:00.000000Z"))

    monkeypatch.setattr(w, "_verify_contents", disagreeing)
    with pytest.raises(WriteFailed) as excinfo:
        run(inputs, durability=DurabilityMode.BEST_EFFORT_REPORTED)
    assert excinfo.value.stage == "validation" and excinfo.value.__cause__.codes == ("MANIFEST_NOT_AS_WRITTEN",)
    assert not inputs.final.exists() and (inputs.partial / "FAILED").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX publication path: the exclusive reservation is what prevents replacing an empty directory")
def test_posix_publication_reserves_the_destination_exclusively_before_the_rename(tmp_path):
    inputs = build_inputs(tmp_path)
    probe = Probe()
    run(inputs, ops=probe)
    order = [e[0] for e in probe.events if e[0] in {"mkdir_reservation", "rename"}]
    assert order == ["mkdir_reservation", "rename"]


def test_directory_flush_classification_of_the_real_operation(tmp_path, monkeypatch):
    ops = FileSystemOps()
    if sys.platform == "win32":
        with pytest.raises(DurabilityUnsupported):
            ops.fsync_directory(str(tmp_path))
        return
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda descriptor: (_ for _ in ()).throw(OSError(errno.EINVAL, "not supported here")))
    with pytest.raises(DurabilityUnsupported):
        ops.fsync_directory(str(tmp_path))
    monkeypatch.setattr(os, "fsync", lambda descriptor: (_ for _ in ()).throw(OSError(errno.EIO, "real failure")))
    with pytest.raises(OSError) as excinfo:
        ops.fsync_directory(str(tmp_path))
    assert not isinstance(excinfo.value, DurabilityUnsupported), "a real I/O error must not be mistaken for 'unsupported'"
    monkeypatch.setattr(os, "fsync", real_fsync)
    ops.fsync_directory(str(tmp_path))


# =========================================================================== 6b. descriptor closure: a failing close never replaces, hides or invents an outcome
# Every owned descriptor gets exactly ONE close attempt and an uncertain close is never retried. A close failure with no earlier failure is a
# structured writer outcome; with an earlier failure it is attached as sanitized secondary information and the primary survives unchanged.
CLOSE_TEXT = "postgresql://backup:CLOSE-SECRET@db.internal/napsedge Traceback (most recent call last)"
CLOSE_STAGES = [("RUNNING", "staging"), ("archive.dump", "archive"), ("baseline.json", "baseline"), ("manifest.json", "manifest"), ("COMPLETE", "marker")]


def fresh(code: int, text: str = "injected") -> OSError:
    """A new instance every time: a primary exception receives notes, so instances must not be shared between tests."""
    return OSError(code, text)


def cleanup_error(code: int = errno.EIO) -> OSError:
    return fresh(code, CLOSE_TEXT)


def note_for(what: str, code: int = errno.EIO) -> str:
    return f"cleanup: closing {what} also failed: OSError (errno {code})"


def assert_sanitized(*carriers, partial: Path | None = None) -> None:
    """No credential, DSN, host, traceback text or arbitrary exception message in the error, its notes, its cleanup record or the FAILED record."""
    texts = []
    for carrier in carriers:
        texts += [str(carrier), *getattr(carrier, "__notes__", ()), *getattr(carrier, "cleanup_failures", ())]
    if partial is not None and (partial / "FAILED").exists():
        texts.append((partial / "FAILED").read_text())
    for text in texts:
        assert "CLOSE-SECRET" not in text and "db.internal" not in text and "Traceback" not in text, text


def assert_failed_unpublished(inputs: Inputs) -> None:
    assert names(inputs.store) == [inputs.partial.name] and not inputs.final.exists()
    assert [r.code for r in verify_artifact_directory(inputs.partial).reasons] == ["NOT_FINAL"]


def final_bytes(directory: Path) -> dict:
    return {name: (directory / name).read_bytes() for name in names(directory)}


def test_a_source_close_failure_with_no_earlier_failure_is_a_structured_failure_not_success(tmp_path):
    inputs = build_inputs(tmp_path)
    archive_sha, bystander_before = file_sha(inputs.archive), tree_snapshot(inputs.bystander)
    probe = Probe(fail={("close", "<source>"): cleanup_error()})
    with pytest.raises(WriteFailed) as excinfo:
        run(inputs, ops=probe)
    error = excinfo.value
    assert (error.stage, error.reason_class, error.published, error.failed_record) == ("archive", "internal_error", "no", "recorded")
    assert isinstance(error.__cause__.__cause__, OSError) and error.__cause__.__cause__.errno == errno.EIO, "the close failure is the recorded cause"
    assert_failed_unpublished(inputs)
    assert read_failed_record(inputs.partial)["reason_class"] == "internal_error"
    assert probe.closed.count("<source>") == 1
    probe.assert_every_descriptor_closed_exactly_once()
    assert_sanitized(error, partial=inputs.partial)
    assert_untouched(inputs, archive_sha, bystander_before)


@pytest.mark.parametrize("label, stage", CLOSE_STAGES, ids=[c[0] for c in CLOSE_STAGES])
def test_a_close_failure_on_a_written_file_with_no_earlier_failure_is_a_durability_failure_in_its_own_stage(label, stage, tmp_path):
    inputs = build_inputs(tmp_path)
    probe = Probe(fail={("close", label): cleanup_error()})
    with pytest.raises(WriteFailed) as excinfo:
        run(inputs, ops=probe)
    error = excinfo.value
    assert (error.stage, error.reason_class, error.published, error.failed_record) == (stage, "durability_failed", "no", "recorded")
    assert isinstance(error.__cause__.__cause__, OSError) and error.__cause__.__cause__.errno == errno.EIO
    assert_failed_unpublished(inputs)
    assert probe.closed.count(label) == 1
    probe.assert_every_descriptor_closed_exactly_once()
    assert_sanitized(error, partial=inputs.partial)


PRIMARY_PLUS_FILE_CLOSE = [
    ("archive_flush_eio_then_close_enospc", ("fsync_file", "archive.dump"), errno.EIO, errno.ENOSPC, "archive", "durability_failed", True),
    ("archive_write_enospc_then_close_eio", ("write", "archive.dump"), errno.ENOSPC, errno.EIO, "archive", "storage_full", False),
    ("manifest_flush_eio_then_close_enospc", ("fsync_file", "manifest.json"), errno.EIO, errno.ENOSPC, "manifest", "durability_failed", True),
    ("baseline_write_eio_then_close_enospc", ("write", "baseline.json"), errno.EIO, errno.ENOSPC, "baseline", "internal_error", False),
]


@pytest.mark.parametrize("name, primary_gate, primary_code, close_code, stage, reason, wrapped", PRIMARY_PLUS_FILE_CLOSE, ids=[c[0] for c in PRIMARY_PLUS_FILE_CLOSE])
def test_a_primary_failure_keeps_its_stage_class_and_cause_when_the_files_close_also_fails(name, primary_gate, primary_code, close_code, stage, reason, wrapped, tmp_path):
    inputs = build_inputs(tmp_path)
    primary, closing = fresh(primary_code), cleanup_error(close_code)
    label = primary_gate[1]
    probe = Probe(fail={primary_gate: primary, ("close", label): closing})
    with pytest.raises(WriteFailed) as excinfo:
        run(inputs, ops=probe)
    error = excinfo.value
    assert (error.stage, error.reason_class, error.published, error.failed_record) == (stage, reason, "no", "recorded")
    assert (error.__cause__.__cause__ if wrapped else error.__cause__) is primary, "the causal chain still leads to the PRIMARY failure"
    assert error.cleanup_failures == (note_for(label, close_code),)
    assert note_for(label, close_code) in error.__cause__.__notes__
    assert_failed_unpublished(inputs)
    assert read_failed_record(inputs.partial)["reason_class"] == reason
    probe.assert_every_descriptor_closed_exactly_once()
    assert probe.closed.count(label) == 1 and probe.closed.count("<source>") == 1
    assert_sanitized(error, partial=inputs.partial)


def test_a_primary_failure_survives_when_both_the_files_close_and_the_failed_records_close_fail(tmp_path):
    inputs = build_inputs(tmp_path)
    primary = fresh(errno.EIO)
    probe = Probe(fail={("fsync_file", "archive.dump"): primary, ("close", "archive.dump"): cleanup_error(), ("close", "FAILED"): cleanup_error()})
    with pytest.raises(WriteFailed) as excinfo:
        run(inputs, ops=probe)
    error = excinfo.value
    assert (error.stage, error.reason_class, error.published) == ("archive", "durability_failed", "no")
    assert error.__cause__.__cause__ is primary and error.failed_record.startswith("failed:"), "FAILED stays best effort and cannot replace the primary"
    assert error.cleanup_failures == (note_for("archive.dump"),)
    assert [r.code for r in verify_artifact_directory(inputs.partial).reasons] == ["NOT_FINAL"] and not inputs.final.exists()
    probe.assert_every_descriptor_closed_exactly_once()
    assert_sanitized(error)


SOURCE_OPEN_FAILURES = [
    ("archive_write_no_space", {("write", "archive.dump"): (2, errno.ENOSPC)}, "archive", "storage_full"),
    ("source_read_error", {("read", "<source>"): (2, errno.EIO)}, "archive", "internal_error"),
]


@pytest.mark.parametrize("name, fail, stage, reason", SOURCE_OPEN_FAILURES, ids=[c[0] for c in SOURCE_OPEN_FAILURES])
def test_an_existing_pre_publication_failure_survives_a_failing_source_close(name, fail, stage, reason, tmp_path, monkeypatch):
    monkeypatch.setattr(w, "COPY_CHUNK_BYTES", 4096)
    inputs = build_inputs(tmp_path)
    archive_sha, bystander_before = file_sha(inputs.archive), tree_snapshot(inputs.bystander)
    primaries = {key: (value[0], fresh(value[1])) for key, value in fail.items()}
    probe = Probe(fail={**primaries, ("close", "<source>"): cleanup_error()})
    with pytest.raises(WriteFailed) as excinfo:
        run(inputs, ops=probe)
    error = excinfo.value
    assert (error.stage, error.reason_class, error.published, error.failed_record) == (stage, reason, "no", "recorded")
    assert error.__cause__ is next(iter(primaries.values()))[1]
    assert error.cleanup_failures == (note_for("the source archive"),)
    assert_failed_unpublished(inputs)
    assert probe.closed.count("<source>") == 1
    probe.assert_every_descriptor_closed_exactly_once()
    assert_sanitized(error, partial=inputs.partial)
    assert_untouched(inputs, archive_sha, bystander_before)


def test_a_changed_source_is_still_reported_as_a_checksum_mismatch_when_its_close_also_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "COPY_CHUNK_BYTES", 4096)
    inputs = build_inputs(tmp_path)

    def grow(read_number):
        if read_number == 1:
            with open(inputs.archive, "ab") as handle:
                handle.write(b"appended by another process")

    probe = Probe(hooks={"after_read": grow}, fail={("close", "<source>"): cleanup_error()})
    with pytest.raises(WriteFailed) as excinfo:
        run(inputs, ops=probe)
    assert (excinfo.value.stage, excinfo.value.reason_class) == ("archive", "checksum_mismatch")
    assert excinfo.value.cleanup_failures == (note_for("the source archive"),)
    assert_failed_unpublished(inputs)
    probe.assert_every_descriptor_closed_exactly_once()


def test_a_precondition_failure_after_the_source_is_open_keeps_its_type_and_still_closes_the_source_once(tmp_path):
    inputs = build_inputs(tmp_path)
    probe = Probe(fail={("mkdir_private", inputs.partial.name): fresh(errno.EACCES), ("close", "<source>"): cleanup_error()})
    with pytest.raises(PreconditionError) as excinfo:
        run(inputs, ops=probe)
    error = excinfo.value
    assert (error.stage, error.published) == ("staging", "no") and error.cleanup_failures == (note_for("the source archive"),)
    assert names(inputs.store) == [], "nothing was created"
    assert probe.closed == ["<source>"]
    probe.assert_every_descriptor_closed_exactly_once()
    assert_sanitized(error)


@pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("closing", ["archive.dump", "<source>"])
def test_an_interrupt_propagates_unwrapped_and_recorded_when_cleanup_also_fails(interrupt_type, closing, tmp_path):
    inputs = build_inputs(tmp_path)
    interrupt = interrupt_type()
    probe = Probe(fail={("write", "archive.dump"): interrupt, ("close", closing): cleanup_error()})
    with pytest.raises(interrupt_type) as excinfo:
        run(inputs, ops=probe)
    assert excinfo.value is interrupt, "the interrupt itself propagates, not a wrapper and not the close error"
    assert note_for("the source archive" if closing == "<source>" else closing) in interrupt.__notes__
    assert read_failed_record(inputs.partial)["reason_class"] == "interrupted"
    assert_failed_unpublished(inputs)
    probe.assert_every_descriptor_closed_exactly_once()
    assert_sanitized(interrupt, partial=inputs.partial)


def test_an_interrupt_raised_by_a_close_during_cleanup_is_not_swallowed(tmp_path):
    inputs = build_inputs(tmp_path)
    probe = Probe(fail={("write", "archive.dump"): fresh(errno.EIO), ("close", "archive.dump"): KeyboardInterrupt()})
    with pytest.raises(KeyboardInterrupt):
        run(inputs, ops=probe)
    assert read_failed_record(inputs.partial)["reason_class"] == "interrupted" and not inputs.final.exists()
    probe.assert_every_descriptor_closed_exactly_once()


# ---- directory flushes and their descriptors (the production fsync_directory_descriptor, on every platform, over a regular file)
def directory_flush_probe(tmp_path: Path, **kwargs) -> Probe:
    scratch = tmp_path / "scratch-for-directory-flushes.bin"
    scratch.write_bytes(b"x")
    return Probe(directory_flush="descriptor", scratch=str(scratch), **kwargs)


@pytest.mark.parametrize("flush_fails", [False, True], ids=["flush_ok_then_close_fails", "flush_fails_then_close_fails"])
def test_a_post_publication_failure_keeps_its_publication_status_and_the_published_artifact_when_the_directory_close_fails(flush_fails, tmp_path):
    inputs = build_inputs(tmp_path)
    control = build_inputs(tmp_path, root_name="control")
    run(control, ops=Probe())
    flush_error, close_failure = fresh(errno.EIO), cleanup_error(errno.ENOSPC)
    # parent flush number 1 is the REQUIRED-mode probe of the store; number 2 is the one after publication
    probe = directory_flush_probe(tmp_path, dir_flush_errors={"parent": (2, flush_error)} if flush_fails else {}, fail={("close", "parent"): (2, close_failure)})
    with pytest.raises(PostPublicationError) as excinfo:
        run(inputs, ops=probe)
    error = excinfo.value
    assert (error.stage, error.published, error.reason_class, error.failed_record) == ("flush_parent", "yes", "durability_failed", "not_attempted")
    if flush_fails:
        assert error.__cause__ is flush_error and error.cleanup_failures == (note_for("the directory", errno.ENOSPC),)
    else:
        assert error.__cause__ is close_failure and error.cleanup_failures == ()
    assert inputs.final.is_dir() and names(inputs.store) == [inputs.final_name]
    assert verify_artifact_directory(inputs.final).valid
    assert final_bytes(inputs.final) == final_bytes(control.final), "the published contents were not rewritten"
    probe.assert_every_descriptor_closed_exactly_once()
    assert_sanitized(error)


def test_unsupported_directory_flushing_stays_unsupported_and_is_reported_as_a_gap_when_the_directory_close_also_fails(tmp_path):
    inputs = build_inputs(tmp_path)
    probe = directory_flush_probe(tmp_path, dir_flush_errors={"staging": (1, fresh(errno.EINVAL))}, fail={("close", "staging"): (1, cleanup_error())})
    result = run(inputs, ops=probe, durability=DurabilityMode.BEST_EFFORT_REPORTED)
    gap = next(g for g in result.durability.gaps if "staging after RUNNING removal" in g)
    assert "unsupported" in gap and note_for("the directory") in gap, "the unsupported flush is a gap, and the failed close is reported with it"
    assert result.durability.durable is False and len(result.durability.gaps) == 1
    assert verify_artifact_directory(inputs.final).valid
    probe.assert_every_descriptor_closed_exactly_once()
    assert_sanitized(*result.durability.gaps)


def test_the_required_mode_probe_still_reports_unsupported_when_its_directory_close_also_fails(tmp_path):
    inputs = build_inputs(tmp_path)
    probe = directory_flush_probe(tmp_path, dir_flush_errors={"parent": (1, fresh(errno.EINVAL))}, fail={("close", "parent"): (1, cleanup_error())})
    with pytest.raises(PreconditionError) as excinfo:
        run(inputs, ops=probe, durability=DurabilityMode.REQUIRED)
    assert excinfo.value.stage == "durability" and "unsupported" in str(excinfo.value) and "probe" not in str(excinfo.value)
    assert names(inputs.store) == []
    probe.assert_every_descriptor_closed_exactly_once()


DIRECTORY_COMBINATIONS = [
    ("flush_ok_close_ok", None, None, None),
    ("flush_ok_close_fails", None, errno.EIO, "close"),
    ("unsupported_close_ok", errno.EINVAL, None, "unsupported"),
    ("unsupported_close_fails", errno.EINVAL, errno.EIO, "unsupported"),
    ("real_failure_close_fails", errno.EIO, errno.ENOSPC, "failure"),
]


@pytest.mark.parametrize("name, flush_code, close_code, expected", DIRECTORY_COMBINATIONS, ids=[c[0] for c in DIRECTORY_COMBINATIONS])
def test_every_directory_flush_and_close_combination_has_one_close_attempt_and_the_right_primary(name, flush_code, close_code, expected, tmp_path, monkeypatch):
    scratch = tmp_path / "scratch.bin"
    scratch.write_bytes(b"x")
    closing = cleanup_error(close_code) if close_code else None
    probe = Probe(fail={("close", "parent"): closing} if closing else {})
    descriptor = probe._register(os.open(scratch, os.O_RDWR | getattr(os, "O_BINARY", 0)), "parent")
    real_fsync, flush_error = os.fsync, (fresh(flush_code) if flush_code else None)
    monkeypatch.setattr(os, "fsync", lambda target: (_ for _ in ()).throw(flush_error) if (target == descriptor and flush_error) else real_fsync(target))
    if expected is None:
        probe.fsync_directory_descriptor(descriptor)
    elif expected == "close":
        with pytest.raises(OSError) as excinfo:
            probe.fsync_directory_descriptor(descriptor)
        assert excinfo.value is closing and not isinstance(excinfo.value, DurabilityUnsupported), "a failed close after a successful flush is an error"
    elif expected == "unsupported":
        with pytest.raises(DurabilityUnsupported) as excinfo:
            probe.fsync_directory_descriptor(descriptor)
        assert excinfo.value.__cause__ is flush_error
        assert list(getattr(excinfo.value, "__notes__", [])) == ([note_for("the directory", close_code)] if close_code else [])
    else:
        with pytest.raises(OSError) as excinfo:
            probe.fsync_directory_descriptor(descriptor)
        assert excinfo.value is flush_error and not isinstance(excinfo.value, DurabilityUnsupported), "a real failure must not be mistaken for 'unsupported'"
        assert list(excinfo.value.__notes__) == [note_for("the directory", close_code)]
    probe.assert_every_descriptor_closed_exactly_once()
    assert probe.closed == ["parent"]


@pytest.mark.skipif(sys.platform == "win32", reason="the real directory descriptor path exists only where directory flushes are supported")
@pytest.mark.parametrize("flush_code, expect_unsupported", [(errno.EINVAL, True), (errno.EIO, False)], ids=["unsupported", "real_failure"])
def test_the_real_directory_flush_does_not_let_a_failing_close_replace_its_primary_outcome(flush_code, expect_unsupported, tmp_path, monkeypatch):
    real_close, calls = os.close, []

    def failing_close(descriptor):
        real_close(descriptor)  # the descriptor really is released, as after a failing close(2)
        calls.append(descriptor)
        raise OSError(errno.ENOSPC, "injected close failure")

    monkeypatch.setattr(os, "fsync", lambda descriptor: (_ for _ in ()).throw(fresh(flush_code)))
    monkeypatch.setattr(os, "close", failing_close)
    with pytest.raises(OSError) as excinfo:
        FileSystemOps().fsync_directory(str(tmp_path))
    monkeypatch.undo()
    assert isinstance(excinfo.value, DurabilityUnsupported) is expect_unsupported
    if not expect_unsupported:
        assert excinfo.value.errno == errno.EIO, "the close failure (ENOSPC) replaced the primary flush failure"
    assert len(calls) == 1, "exactly one close attempt"


# =========================================================================== 7. the module's own constraints
def writer_tree() -> ast.Module:
    return ast.parse(WRITER_PATH.read_text(encoding="utf-8"))


def test_the_writer_imports_only_the_standard_library_and_the_contract_module():
    imported = set()
    internal = set()
    for node in ast.walk(writer_tree()):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            (internal if (node.module or "").startswith("src.") else imported).add(node.module or "")
    assert internal == {"src.deployment.backup_artifact"}, internal
    forbidden = {"tests", "shutil", "subprocess", "socket", "sqlalchemy", "psycopg", "psycopg2", "tempfile", "ctypes", "threading", "urllib", "http", "requests",
                 "multiprocessing", "asyncio", "re", "json", "alembic", "pydantic"}
    assert imported.isdisjoint(forbidden), sorted(imported & forbidden)
    assert imported <= {"datetime", "enum", "errno", "hashlib", "os", "stat", "sys", "dataclasses", "typing", "__future__"}, sorted(imported)


def test_the_writer_has_no_copy_move_fallback_no_replacing_rename_and_no_recursive_delete():
    attributes = {node.attr for node in ast.walk(writer_tree()) if isinstance(node, ast.Attribute)}
    assert attributes.isdisjoint({"replace", "rmtree", "removedirs", "copy", "copyfile", "copytree", "move", "system", "popen", "Popen", "run",
                                  "symlink", "link", "chmod", "truncate", "walk"}), sorted(attributes)
    source = WRITER_PATH.read_text(encoding="utf-8")
    assert source.count("os.rename(") == 1 and "os.replace" not in source and "shutil" not in source
    assert source.count("os.rmdir(") == 1 and source.count("os.unlink(") == 1, "the only deletions are the writer's own RUNNING marker and its own empty reservation"


def test_the_writer_restates_no_schema_or_normalization_rules():
    """No regular expressions, no JSON module and no field patterns: every rule comes from the contract module."""
    source = WRITER_PATH.read_text(encoding="utf-8")
    assert "import re" not in source and "import json" not in source and "fullmatch" not in source
    assert "_verify_contents(" in source and "_manifest_baseline_relations(" in source and "canonical_json_bytes(" in source


def test_importing_the_writer_performs_no_network_process_or_write_activity():
    script = (
        "import sys, json, hashlib, os, re, stat, datetime, dataclasses, typing, enum, errno\n"
        "import src.deployment.backup_artifact\n"
        "events = []\n"
        "def hook(event, args):\n"
        "    if event.startswith('socket.') or event in ('subprocess.Popen', 'os.system', 'os.mkdir', 'os.remove', 'os.rename', 'os.rmdir', 'os.symlink', 'os.chmod'):\n"
        "        events.append(event)\n"
        "    elif event == 'open':\n"
        "        mode = str(args[1]) if len(args) > 1 and args[1] is not None else 'r'\n"
        "        path = str(args[0])\n"
        "        if any(flag in mode for flag in 'wax+') or not path.endswith(('.py', '.pyc')):\n"
        "            events.append('open:' + mode + ':' + os.path.basename(path))\n"
        "sys.addaudithook(hook)\n"
        "import src.deployment.backup_artifact_writer\n"
        "print(json.dumps(events))\n"
    )
    completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
                               env={**{k: v for k, v in os.environ.items() if not k.startswith(("PG", "NAPSEDGE_DATABASE"))}, "PYTHONDONTWRITEBYTECODE": "1"})
    assert completed.returncode == 0, completed.stderr[-400:]
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == [], completed.stdout


def test_the_read_side_module_is_unchanged_in_kind_it_still_contains_no_write_operations():
    reader = (REPO_ROOT / "src" / "deployment" / "backup_artifact.py").read_text(encoding="utf-8")
    attributes = {node.attr for node in ast.walk(ast.parse(reader)) if isinstance(node, ast.Attribute)}
    assert attributes.isdisjoint({"rename", "replace", "remove", "unlink", "rmdir", "mkdir", "makedirs", "write", "fsync", "Popen"}), sorted(attributes)
    assert "_verify_contents" in reader and ".partial-" in reader, "the public verifier still refuses .partial directories"
