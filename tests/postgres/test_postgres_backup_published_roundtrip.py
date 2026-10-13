"""
Phase 9B-6J: round trip of a PUBLISHED /2 artifact (TEST LAYER ONLY; no production restore service).

`run_guarded_capture` (the production coordinator) publishes a `/2` artifact through the real lock, observers, snapshot,
`LocalClientHost` dump and writer. After it returns, the capture's sessions, locks, processes and scratch are proven gone.
The artifact is then REOPENED from disk through the accepted public verifier; the archive and the canonical baseline are
taken from it, their recorded sizes and SHA-256 digests re-checked independently, and the baseline parsed by the contract
parser and the existing test-layer adapter. Nothing is taken from a live capture handle, and no expected fact is
reconstructed from the source database: the restore guard's source name and OID come from the artifact's manifest.

The accepted 9B-6E round-trip helpers are reused unchanged: `restore` (the guarded, migrator-owned, empty, non-source
target; `pg_restore -d ... --no-password --exit-on-error --single-transaction`, no owner or ACL stripping; a deadline that
kills the real `pg_restore` and ends its backend by its unique application name) and `compare` (the unchanged strict
comparator against the SAVED baseline, and `monotonic_v1` R0-R3 reported separately, R2 blocking). The artifact itself is
never modified: corruption scenarios change a COPY.

What a passing scenario demonstrates: in this setup (one disposable PostgreSQL 16 cluster, synthetic data, the 9B-2 grants,
real PostgreSQL 16.15 client tools on the accepted Linux runner) the published artifact verified, restored into a guarded
disposable database, and compared as reported. A successful restore, strict equality or policy acceptance is NOT a
recovery-readiness claim. Null foreign-write counts mean "not sampled": they do not establish quiescence.

Runs only with `NAPSEDGE_TEST_CLIENT=local` (the coordinator preserves its archive through the local filesystem).
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import shutil
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

import src.deployment.backup_capture_coordinator as coordinator
from src.deployment.backup_artifact import parse_baseline, verify_artifact_directory
from src.deployment.backup_capture_coordinator import PublishedCapture, run_guarded_capture
from tests.postgres.baseline_document import baseline_from_document
from tests.postgres.capture_client_support import native_mode, production_client
from tests.postgres.pg_restore_compare import all_fingerprints, describe_diff
from tests.postgres.test_postgres_backup_capture_coordinator import Hooks, assert_nothing_left, request_for, store  # noqa: F401  (store: fixture)
from tests.postgres.test_postgres_backup_capture_lock import harness, record  # noqa: F401  (harness: module fixture)
from tests.postgres.test_postgres_backup_capture_roundtrip import (  # noqa: F401  (case: fixture)
    SAFE_INSERT_TEAM,
    Captured,
    RestoreRefused,
    case,
    compare,
    new_target,
    restore,
    rule_rows,
)
from tests.postgres.test_postgres_backup_capture_snapshot import admin_scalar
from tests.postgres.test_postgres_backup_sequence_consistency import MANUAL_ID, setup_manual_id
from tests.postgres.test_postgres_fresh_cluster_recovery import PreconditionFailed
from tests.postgres.test_postgres_roles_and_restore import MIGRATOR_ROLE

pytestmark = [pytest.mark.postgres,
              pytest.mark.skipif(not native_mode(), reason="transport-specific: the coordinator preserves the archive through the LOCAL filesystem "
                                                           "(production LocalClientHost). Run with NAPSEDGE_TEST_CLIENT=local on the Linux runner.")]

BLOCKED_RESTORE_TIMEOUT_S = 8


class ArtifactRefused(AssertionError):
    """The published artifact did not verify, or a re-checked digest or size differs: no restore may start."""


@dataclasses.dataclass
class Published:
    path: Path
    manifest: object
    captured: Captured  # the accepted helpers' input record, filled ONLY from the verified artifact
    facts: dict


def file_digest(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def artifact_digests(path: Path) -> dict:
    return {p.name: file_digest(p) for p in sorted(path.iterdir())}


def open_published(path) -> Published:
    """The accepted reader/verifier first; then an independent re-check of the archive and baseline against the manifest."""
    path = Path(path)
    verdict = verify_artifact_directory(path)
    if not verdict.valid:
        raise ArtifactRefused("the published artifact did not verify: " + ", ".join(verdict.codes))
    manifest = verdict.manifest
    archive_entry, baseline_entry = manifest.files
    archive_sha, archive_bytes = file_digest(path / archive_entry.path)
    baseline_raw = (path / baseline_entry.path).read_bytes()
    baseline_sha, baseline_bytes = hashlib.sha256(baseline_raw).hexdigest(), len(baseline_raw)
    if (archive_sha, archive_bytes) != (archive_entry.sha256, archive_entry.bytes) or (baseline_sha, baseline_bytes) != (baseline_entry.sha256, baseline_entry.bytes):
        raise ArtifactRefused("a re-checked digest or size differs from the manifest")
    document = parse_baseline(baseline_raw)  # the contract parser
    if document != verdict.baseline:
        raise ArtifactRefused("the baseline parsed twice differently")
    baseline_from_document(document)  # the existing adapter accepts it (compare() re-parses the same saved bytes)
    captured = Captured(archive_copy=str(path / archive_entry.path), archive_sha256=archive_sha, archive_bytes=archive_bytes,
                        toc_sha256=manifest.archive.toc_sha256, baseline_file=path / baseline_entry.path, baseline_sha256=baseline_sha,
                        source_database=manifest.source.database_name, source_oid=manifest.source.database_oid, snapshot_id=None,
                        pg_dump_version=manifest.source.pg_dump_version, pg_restore_version=None, lock_pid=None, snapshot_pid=None,
                        original_archive=str(path / archive_entry.path), handle=None, after_close={})
    facts = {"artifact": path.name, "format": manifest.format, "archive_sha256": archive_sha, "archive_bytes": archive_bytes,
             "baseline_sha256": baseline_sha, "baseline_bytes": baseline_bytes, "toc_entries": manifest.archive.toc_entries,
             "source": {"system_identifier": manifest.source.system_identifier, "database_oid": manifest.source.database_oid,
                        "database_name": manifest.source.database_name},
             "foreign_write_sessions": [manifest.consistency.foreign_write_sessions_start, manifest.consistency.foreign_write_sessions_end],
             "foreign_write_sessions_meaning": "not sampled (null); not evidence of quiescence"}
    return Published(path, manifest, captured, facts)


def publish(case, store, *, ops=None) -> tuple[Published, dict]:
    """The production coordinator; then the capture is proven closed BEFORE the artifact is reopened."""
    result = run_guarded_capture(request_for(case, store), client=production_client(case.client), ops=ops)
    assert type(result) is PublishedCapture
    closed = assert_nothing_left(case, store)  # source-session inspection: cleanup evidence only, never comparison input
    published = open_published(result.written.path)
    assert published.facts["format"] == "napsedge.backup-artifact/2"
    return published, closed


def compare_published(case, published: Published, target: str):
    return compare(case, published.captured, target)  # the SAVED baseline from the artifact; never the live source


def evidence(record_property, scenario: str, published: Published | None, outcome=None, comparison=None, **extra) -> None:
    fields = {"acquired": True, "body_entered": True, "transport": "local"}
    if published is not None:
        fields["artifact"] = published.facts
    if outcome is not None:
        fields.update(target=outcome.identity, restore_role=MIGRATOR_ROLE, restore_argv=outcome.argv_sanitized, restore_exit=outcome.returncode,
                      restored_archive_sha256=outcome.archive_sha256, listing_exit=outcome.listing_returncode, restore_stderr=outcome.stderr[:400],
                      timed_out=outcome.timed_out)
    if comparison is not None:
        fields.update(strict_verdict=comparison.verdict, strict_sequences_equal=comparison.strict_sequences_equal,
                      sequence_differences=comparison.sequence_differences, monotonic_v1_rules=rule_rows(comparison),
                      monotonic_v1_r2_failures=comparison.r2_failures, monotonic_v1_policy_accepts=comparison.policy_accepts,
                      readiness_claim="none (a restore, strict equality or policy acceptance is not recovery readiness)")
    record(record_property, scenario, **fields, **extra)


def assert_artifact_preserved(published: Published, before: dict) -> None:
    assert artifact_digests(published.path) == before, "the published artifact was modified"


# ---------------------------------------------------------------------------
# A. unchanged source
# ---------------------------------------------------------------------------
def test_a_an_unchanged_source_round_trips_strictly_equal_and_every_sequence_satisfies_the_policy(case, store, record_property):
    published, closed = publish(case, store)
    before = artifact_digests(published.path)
    target = new_target(case)
    outcome = restore(case, published.captured, target)
    assert outcome.archive_sha256 == published.facts["archive_sha256"], "a different archive was restored"
    comparison = compare_published(case, published, target) if outcome.returncode == 0 else None
    evidence(record_property, "A_unchanged", published, outcome, comparison, capture_cleanup=closed)
    assert outcome.returncode == 0 and outcome.stderr == "", outcome.stderr
    assert outcome.identity["owner"] == MIGRATOR_ROLE and outcome.identity["migrator_is_superuser"] is False
    assert target != published.manifest.source.database_name and outcome.identity["oid"] != published.manifest.source.database_oid
    assert all(comparison.verdict.values()), (comparison.verdict, describe_diff(comparison.raw.fingerprint_diffs))
    assert comparison.strict_sequences_equal and comparison.sequence_differences == []
    assert comparison.policy_accepts and comparison.r2_failures == [] and len(comparison.rules) == 13
    assert all(r.applicable and r.r1_no_regression and r.r2_next_id_safe for r in comparison.rules)
    assert_artifact_preserved(published, before)


# ---------------------------------------------------------------------------
# B. a sequence-generated write after the snapshot, before the dump reads sequence state
# ---------------------------------------------------------------------------
def test_b_a_write_after_the_snapshot_is_excluded_and_the_strict_sequence_difference_is_retained(case, store, record_property, monkeypatch):
    writer = create_engine(case.cluster.migrator_url(case.database))
    case.ledger.engines.append(writer)
    proof: dict = {}
    real_dump = coordinator.backup_capture_dump

    @contextlib.contextmanager
    def dump_after_a_committed_insert(handle, *, client):
        # Deterministic barrier (the 9B-6E `before_dump` ordering): this runs inside S's context -- the snapshot is exported and
        # the baseline read -- and BEFORE pg_dump starts, so the dump reads the advanced sequence but imports the older snapshot.
        proof["sequence_before"] = admin_scalar(case.admin, "SELECT last_value FROM team_aliases_id_seq")
        with writer.begin() as conn:
            proof["new_id"] = conn.execute(text("INSERT INTO team_aliases (team_id, source, raw_value, normalized_value) "
                                                "VALUES (:t, 'odds', 'Published Alias', 'published alias') RETURNING id"), {"t": SAFE_INSERT_TEAM}).scalar_one()
        proof["sequence_after_fresh"] = admin_scalar(case.admin, "SELECT last_value FROM team_aliases_id_seq")
        proof["fresh_reader_row"] = admin_scalar(case.admin, "SELECT count(*) FROM team_aliases WHERE id = :i", i=proof["new_id"])
        with real_dump(handle, client=client) as archive:
            yield archive

    monkeypatch.setattr(coordinator, "backup_capture_dump", dump_after_a_committed_insert)  # TEST seam around the REAL dump
    published, closed = publish(case, store)
    monkeypatch.setattr(coordinator, "backup_capture_dump", real_dump)  # only this seam is removed; the fixtures' own patches stay
    target = new_target(case)
    outcome = restore(case, published.captured, target)
    assert outcome.archive_sha256 == published.facts["archive_sha256"], "a different archive was restored"
    comparison = compare_published(case, published, target) if outcome.returncode == 0 else None
    admin_target = case.cluster.admin_engine(target)
    try:
        with admin_target.connect() as conn:
            restored_row = conn.execute(text("SELECT count(*) FROM team_aliases WHERE id = :i"), {"i": proof["new_id"]}).scalar()
    finally:
        admin_target.dispose()
    evidence(record_property, "B_write_after_snapshot", published, outcome, comparison, commit_proof=proof, restored_has_inserted_row=restored_row,
             capture_cleanup=closed)
    assert outcome.returncode == 0, outcome.stderr
    assert proof["fresh_reader_row"] == 1 and proof["sequence_after_fresh"] > proof["sequence_before"]
    assert restored_row == 0 and comparison.verdict["fingerprints"], describe_diff(comparison.raw.fingerprint_diffs)  # the saved baseline, not later data
    assert all(comparison.verdict.values()), comparison.verdict
    assert not comparison.strict_sequences_equal, "the strict sequence difference must be retained, never folded into equality"
    assert comparison.sequence_differences == [(("team_aliases_id_seq", proof["sequence_before"]), ("team_aliases_id_seq", proof["sequence_after_fresh"]))]
    assert comparison.policy_accepts and comparison.r2_failures == [], "a separate policy result: the advancement is safe"


# ---------------------------------------------------------------------------
# C. equal but unsafe
# ---------------------------------------------------------------------------
def test_c_strict_equality_does_not_conceal_a_failed_r2_and_the_policy_rejects(case, store, record_property):
    migrator = create_engine(case.cluster.migrator_url(case.database))
    case.ledger.engines.append(migrator)
    fixture = setup_manual_id(migrator)  # NEGATIVE-CONTROL SETUP on the source: an explicit id above the sequence (the C4 fixture)
    published, closed = publish(case, store)
    target = new_target(case)
    outcome = restore(case, published.captured, target)
    comparison = compare_published(case, published, target) if outcome.returncode == 0 else None
    evidence(record_property, "C_equal_but_unsafe", published, outcome, comparison, fixture=fixture, capture_cleanup=closed)
    alias = next(r for r in comparison.rules if r.sequence == "team_aliases_id_seq")
    assert outcome.returncode == 0 and all(comparison.verdict.values()) and comparison.strict_sequences_equal
    assert alias.r1_no_regression is True and alias.r2_next_id_safe is False and alias.column_max_restored == MANUAL_ID
    assert comparison.r2_failures == ["team_aliases_id_seq"]
    assert comparison.policy_accepts is False, "a failed R2 must reject the policy although strict equality holds"


# ---------------------------------------------------------------------------
# D. a corrupted COPY of the artifact is refused before any restore process starts
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("member", ["archive.dump", "baseline.json"])
def test_d_a_corrupted_artifact_copy_is_refused_before_any_restore(case, store, record_property, member):
    published, closed = publish(case, store)
    before = artifact_digests(published.path)
    copy_root = store / "tampered"
    copy_root.mkdir()
    tampered = copy_root / published.path.name  # the same final name, so only the content can make it invalid
    shutil.copytree(published.path, tampered)
    data = bytearray((tampered / member).read_bytes())
    if member == "archive.dump":
        position = len(data) - 300  # inside the last compressed table-data block (the accepted damaged-archive position)
        data[position] ^= 0x01
    else:
        position = data.index(b'"captured_utc":"') + len(b'"captured_utc":"')  # the year's first digit: still valid JSON
        data[position] = ord("3") if data[position] != ord("3") else ord("4")
    (tampered / member).write_bytes(bytes(data))  # same size; the manifest and COMPLETE are NOT updated
    starts_before = [c for c in case.client.calls if c.startswith("start:")]
    targets_before = list(case.targets)
    with pytest.raises(ArtifactRefused) as info:
        open_published(tampered)
    starts_after = [c for c in case.client.calls if c.startswith("start:")]
    evidence(record_property, f"D_corrupt_{member.split('.')[0]}", published, refused=str(info.value), restore_processes_started=starts_after[len(starts_before):],
             targets_created=case.targets[len(targets_before):], capture_cleanup=closed)
    assert "FILE_DIGEST_MISMATCH" in str(info.value), str(info.value)
    assert starts_after == starts_before and case.targets == targets_before, "no target and no restore process before verification"
    assert_artifact_preserved(published, before)


# ---------------------------------------------------------------------------
# E. wrong targets are refused without changing them
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("variant", ["source", "unowned", "non_empty"])
def test_e_a_wrong_target_is_refused_before_restore_and_left_unchanged(case, store, harness, record_property, variant):
    published, closed = publish(case, store)
    if variant == "source":
        target = case.database
    elif variant == "unowned":
        target = harness.boot_database  # exists, but this case did not create it
    else:
        target = new_target(case)  # owned and migrator-owned, then made non-empty by a committed table (the guard's own condition)
        engine = create_engine(case.cluster.migrator_url(target))
        case.ledger.engines.append(engine)
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE public.occupied (id integer)"))
    target_admin = case.cluster.admin_engine(target)
    case.ledger.engines.append(target_admin)
    fingerprints_before = all_fingerprints(target_admin)
    starts_before = [c for c in case.client.calls if c.startswith("start:")]
    with pytest.raises((RestoreRefused, PreconditionFailed)) as info:
        restore(case, published.captured, target)
    starts_after = [c for c in case.client.calls if c.startswith("start:")]
    fingerprints_after = all_fingerprints(target_admin)
    evidence(record_property, f"E_{variant}", published, refused=f"{type(info.value).__name__}: {info.value}", target_unchanged=fingerprints_before == fingerprints_after,
             restore_processes_started=starts_after[len(starts_before):], capture_cleanup=closed)
    assert starts_after == starts_before, "a restore process was started"
    assert fingerprints_before == fingerprints_after, "the refused target changed"


# ---------------------------------------------------------------------------
# F. restore failure and deadline
# ---------------------------------------------------------------------------
def test_f_a_failed_restore_never_reaches_a_comparison(case, store, record_property):
    published, closed = publish(case, store)
    before = artifact_digests(published.path)
    target = new_target(case)
    migrator = create_engine(case.cluster.migrator_url(target))
    case.ledger.engines.append(migrator)
    with migrator.begin() as conn:
        # Injected fault (accepted 9B-6E technique): a committed TYPE named like a table the archive creates. The empty-target guard,
        # which counts TABLES, still passes unchanged; the restore then fails deterministically with "already exists".
        conn.execute(text("CREATE TYPE public.teams AS (x integer)"))
    outcome = restore(case, published.captured, target)
    comparison = comparison_error = None
    if outcome.returncode == 0:  # the gate under test: only a successful restore may be compared
        try:
            comparison = compare_published(case, published, target)
        except Exception as exc:  # contained and recorded, so the gate assertion below judges the outcome (the 9B-6E structure)
            comparison_error = type(exc).__name__
    admin_target = case.cluster.admin_engine(target)
    try:
        with admin_target.connect() as conn:
            tables_after = conn.execute(text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")).scalar()
    finally:
        admin_target.dispose()
    evidence(record_property, "F_restore_failure", published, outcome, comparison, compared=comparison is not None, comparison_error=comparison_error,
             tables_after=tables_after, client_processes_left=case.client.client_processes(), capture_cleanup=closed)
    assert outcome.returncode not in (0, None), "a failed restore was treated as a success"
    assert comparison_error is None
    assert comparison is None, "a failed restore must not produce a comparison"
    assert tables_after == 0 and "already exists" in outcome.stderr  # --single-transaction: nothing committed
    assert case.client.client_processes() == [] and outcome.restore_backends_after == []
    assert_artifact_preserved(published, before)


def test_f_a_blocked_restore_is_stopped_at_its_deadline_with_its_backend(case, store, record_property):
    published, closed = publish(case, store)
    target = new_target(case)
    blocker_engine = create_engine(case.cluster.migrator_url(target))
    case.ledger.engines.append(blocker_engine)
    blocker = blocker_engine.connect()
    case.ledger.connections.append(blocker)
    blocker.begin()
    # Injected fault (accepted 9B-6E technique): an UNCOMMITTED table in the disposable target. The guard sees no committed table and
    # passes; the restore's CREATE of the same name then waits on the lock until the deadline.
    blocker.execute(text("CREATE TABLE public.alembic_version (version_num varchar(32))"))
    observed: dict = {}

    def watch(application_name):
        def run():
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not observed.get("backend"):
                conn = case.admin.engine.connect()
                try:
                    found = conn.execute(text("SELECT pid, datname, usename FROM pg_stat_activity WHERE application_name = :a AND wait_event_type = 'Lock'"),
                                         {"a": application_name}).first()
                finally:
                    conn.close()
                observed["backend"] = tuple(found) if found else None
                time.sleep(0.05)
        thread = threading.Thread(target=run, name="published-restore-watch")
        case.ledger.threads.append(thread)
        thread.start()

    started = time.monotonic()
    outcome = restore(case, published.captured, target, timeout_s=BLOCKED_RESTORE_TIMEOUT_S, on_started=watch)
    elapsed = time.monotonic() - started
    blocker.rollback()
    comparison = compare_published(case, published, target) if outcome.returncode == 0 else None
    process = outcome.process
    evidence(record_property, "F_deadline", published, outcome, comparison, elapsed_s=round(elapsed, 2), blocked_backend=observed.get("backend"),
             killed=process.killed, pid=process.container_pid, gone_after_kill=process.gone_after_kill, restore_backends_after=outcome.restore_backends_after,
             client_processes_left=case.client.client_processes(), capture_cleanup=closed)
    assert observed.get("backend") is not None and observed["backend"][1] == target and observed["backend"][2] == MIGRATOR_ROLE
    assert outcome.timed_out and process.killed and process.gone_after_kill and outcome.returncode == case.client.killed_returncode, \
        "the REAL pg_restore process must be killed at the deadline"
    assert outcome.restore_backends_after == [] and case.client.client_processes() == [], "its target backend and every client process are gone"
    assert comparison is None and BLOCKED_RESTORE_TIMEOUT_S <= elapsed < 60
