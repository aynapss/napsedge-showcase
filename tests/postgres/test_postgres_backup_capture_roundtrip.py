"""
Phase 9B-6E: disposable round trip of the integrated capture path (TEST LAYER ONLY). The real production preflight, lock,
snapshot and dump modules produce an archive. The exact archive bytes and the exact captured `BaselineDocument` are
preserved, the capture contexts are closed and proven gone, the archive is restored into a fresh disposable database, and
the result is compared with the SAVED baseline: by the unchanged strict comparator, and separately by the established
`monotonic_v1` rules with R2 blocking.

What this is NOT: a production restore service, an artifact, a verification record, or a recovery-ready status. A
passing scenario establishes only the observed round-trip result in this setup: one disposable PostgreSQL 16 cluster,
synthetic data, the 9B-2 grants, and the cluster image's own PostgreSQL 16.15 client run inside the server's container
by the 9B-6D test-layer `ContainerClientHost`, which relocates only the network path and CA-file location. The
production `LocalClientHost` route is not exercised here.

Reused, not rebuilt:
* the restore recipe of the accepted harness (`run_restore`'s options: `-d <target> --no-password --exit-on-error
  --single-transaction`, as the migrator), executed through `ContainerClientHost.start` so a timeout can kill the REAL
  in-container `pg_restore`;
* the empty-target and archive-ready guards (`require_destination_database_empty`, `require_archive_ready_for_restore`);
* the migrator-owned target recipe (`_create_database_owned_by_migrator`) and private pgpass (`write_pgpass`);
* the strict comparator (`compare_to_baseline`) and the existing pure adapter (`baseline_from_document`), with no new bridge;
* the C3-C5 sequence reads and rules (`read_sequence_details`, `evaluate_rules`) and the C4 fixture (`setup_manual_id`).

Every database, file, directory, role, process and session is registered the moment it exists and is removed and
verified removed by server and container observations.
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import posixpath
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from src.deployment.backup_artifact import canonical_json_bytes, parse_baseline
from src.deployment.backup_capture_dump import backup_capture_dump
from src.deployment.backup_capture_lock import LOCK_APPLICATION_NAME, VERIFIER_APPLICATION_NAME, backup_capture_lock
from src.deployment.backup_capture_snapshot import SNAPSHOT_APPLICATION_NAME, SnapshotHandleClosed, backup_capture_snapshot
from src.pipeline.bulk_import_safety import compute_advisory_lock_key
from tests.postgres.baseline_document import baseline_from_document
from tests.postgres.pg_restore_compare import all_fingerprints, capture_baseline, compare_to_baseline, describe_diff
from tests.postgres.capture_client_support import (
    install,
    make_test_client,
    native_mode,
    private_scratch_dir,
    production_client,
    remove_role_pgpass,
    restore_conninfo,
    write_role_pgpass,
)
from tests.postgres.test_postgres_backup_capture_dump import OWNED_APPS, ContainerClientHost, dump_backends_any  # noqa: F401
from tests.postgres.test_postgres_backup_capture_lock import Admin, _verified, designation_for, harness, record  # noqa: F401  (harness: module fixture)
from tests.postgres.test_postgres_backup_capture_preflight import disposable_role
from tests.postgres.test_postgres_backup_capture_snapshot import Seam, admin_scalar, new_database
from tests.postgres.test_postgres_backup_sequence_consistency import HAZARD_TEAM_ID, MANUAL_ID, evaluate_rules, read_sequence_details, setup_manual_id
from tests.postgres.test_postgres_backup_snapshot_consistency import owned_case, require_destination_database_empty
from tests.postgres.test_postgres_fresh_cluster_recovery import require_archive_ready_for_restore
from tests.postgres.test_postgres_roles_and_restore import ACCEPTED_ALEMBIC_HEAD, MIGRATOR_ROLE, _create_database_owned_by_migrator, _drop_database
from tests.postgres.test_postgres_tls_backup_restore import (
    CommandResult,
    conninfo,
    isolated_database_environment,  # noqa: F401  (autouse module fixture)
    remove_files,
    write_pgpass,
)

pytestmark = pytest.mark.postgres

RESTORE_APP_PREFIX = "napsedge_roundtrip_restore_"
RESTORE_TIMEOUT_S = 120
BLOCKED_RESTORE_TIMEOUT_S = 8
STDERR_LIMIT = 4096
SAFE_INSERT_TEAM = 910002  # EXTRA_TEAM_ID, a seeded team the alias row may reference


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class Case:
    cluster: object
    database: str
    admin: Admin
    key: int
    ledger: object
    client: ContainerClientHost
    host_dir: Path
    container_dir: str
    targets: list


@pytest.fixture()
def case(harness, monkeypatch):
    cluster = harness.cluster
    with owned_case(cluster) as ledger:
        database = new_database(cluster, ledger)
        admin = Admin(cluster, database)
        ledger.engines.append(admin.engine)
        key = compute_advisory_lock_key(_verified(cluster, database))
        client = install(monkeypatch, make_test_client(cluster))  # NAPSEDGE_TEST_CLIENT selects the transport (capture_client_support)
        host_dir = Path(tempfile.mkdtemp(prefix="napsedge_roundtrip_"))
        ledger.directories.append(host_dir)  # removed and verified removed by the ledger
        container_dir = private_scratch_dir(client)  # where the client's files live: the cluster container, or this Linux runner
        try:
            yield Case(cluster, database, admin, key, ledger, client, host_dir, container_dir, [])
        finally:
            client._run(["rm", "-rf", "--", container_dir])
        assert not client.exists(container_dir), "the owned container directory was not removed"
        assert client.client_processes() == [] and dump_backends_any(admin) == [] and restore_backends(cluster) == []
        for app in OWNED_APPS:
            assert admin.sessions(app) == [], app
        assert admin.holders(key) == []


def restore_backends(cluster) -> list:
    admin = cluster.admin_engine()
    try:
        with admin.connect() as conn:
            return [tuple(r) for r in conn.execute(text("SELECT pid, datname FROM pg_stat_activity WHERE application_name LIKE :p"),
                                                   {"p": RESTORE_APP_PREFIX.replace("_", r"\_") + "%"})]
    finally:
        admin.dispose()


# ---------------------------------------------------------------------------
# 1. capture through the production path, preserving the exact inputs
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class Captured:
    archive_copy: str
    archive_sha256: str
    archive_bytes: int
    toc_sha256: str
    baseline_file: Path
    baseline_sha256: str
    source_database: str
    source_oid: int
    snapshot_id: str
    pg_dump_version: str
    pg_restore_version: str
    lock_pid: int
    snapshot_pid: int
    original_archive: str
    handle: object
    after_close: dict


def capture(case: Case, *, before_dump=None) -> Captured:
    """L -> S -> dump (production code); inside the dump body the exact archive bytes and baseline are copied out; then everything closes."""
    url = case.cluster.backup_url(case.database)
    seen: dict = {}
    snap_seam = Seam()
    with backup_capture_lock(url, designation_for(case.database)) as lock:
        with backup_capture_snapshot(lock, url, designation_for(case.database), expected_alembic_head=ACCEPTED_ALEMBIC_HEAD,
                                     artifact_id=str(uuid.uuid4()), _engine_factory=snap_seam.factory) as handle:
            if before_dump is not None:
                seen["before_dump"] = before_dump(handle, snap_seam)
            with backup_capture_dump(handle, client=production_client(case.client)) as archive:
                copy = posixpath.join(case.container_dir, f"archive_{uuid.uuid4().hex[:8]}.dump")
                assert case.client._run(["sh", "-c", 'umask 077 && cp -- "$1" "$2"', "sh", archive.archive_path, copy]).returncode == 0
                original_sha = sha256_in_container(case, archive.archive_path)
                seen.update(archive_copy=copy, archive_sha256=sha256_in_container(case, copy), original_sha256=original_sha,
                            archive_bytes=archive.archive_bytes, toc_sha256=archive.toc_sha256, snapshot_id=archive.snapshot_id,
                            pg_dump_version=archive.pg_dump_version, pg_restore_version=archive.pg_restore_version,
                            original_archive=archive.archive_path, lock_pid=lock.backend_pid, snapshot_pid=handle.facts.backend_pid,
                            source_oid=handle.facts.database_oid)
                baseline_bytes = canonical_json_bytes(handle.baseline.to_document())  # the established serialization
                baseline_file = case.host_dir / f"baseline_{uuid.uuid4().hex[:8]}.json"
                with open(baseline_file, "xb") as out:
                    out.write(baseline_bytes)
                seen["baseline_file"], seen["baseline_sha256"] = baseline_file, hashlib.sha256(baseline_file.read_bytes()).hexdigest()
    # everything that captured is closed: prove it from the server and the container
    after = {
        "lock_and_snapshot_sessions": {app: case.admin.sessions(app) for app in OWNED_APPS},
        "pids_gone": case.admin.gone([seen["lock_pid"], seen["snapshot_pid"]]),
        "advisory_holders": case.admin.holders(case.key),
        "dump_backends": dump_backends_any(case.admin),
        "original_archive_exists": case.client.exists(seen["original_archive"]),
        "copy_exists": case.client.exists(seen["archive_copy"]),
    }
    with pytest.raises(SnapshotHandleClosed):
        handle.baseline  # the live handle has expired: the saved file is the only baseline from here on
    return Captured(seen["archive_copy"], seen["archive_sha256"], seen["archive_bytes"], seen["toc_sha256"], seen["baseline_file"],
                    seen["baseline_sha256"], case.database, seen["source_oid"], seen["snapshot_id"], seen["pg_dump_version"],
                    seen["pg_restore_version"], seen["lock_pid"], seen["snapshot_pid"], seen["original_archive"], handle,
                    {**after, "original_sha256": seen["original_sha256"], "before_dump": seen.get("before_dump")})


def sha256_in_container(case: Case, path: str) -> str:
    result = case.client._run(["sha256sum", "--", path])
    assert result.returncode == 0
    return result.stdout.decode().split()[0]


def saved_baseline(captured: Captured):
    """The comparison input: the SAVED bytes, re-checked against their digest and parsed by the contract's own parser."""
    data = captured.baseline_file.read_bytes()
    assert hashlib.sha256(data).hexdigest() == captured.baseline_sha256
    return baseline_from_document(parse_baseline(data))


# ---------------------------------------------------------------------------
# 2. restore target, guard and restore
# ---------------------------------------------------------------------------
class RestoreRefused(AssertionError):
    """The target guard refused before any restore process was started."""


def new_target(case: Case) -> str:
    target = _create_database_owned_by_migrator(case.cluster)
    case.ledger.databases.append(target)  # registered immediately: dropped and verified dropped by the ledger
    case.targets.append(target)
    return target


def target_identity(case: Case, target: str) -> dict:
    admin = case.cluster.admin_engine()
    try:
        with admin.connect() as conn:
            row = conn.execute(text("SELECT d.oid, pg_get_userbyid(d.datdba), (SELECT rolsuper FROM pg_roles WHERE rolname = :m) "
                                    "FROM pg_database d WHERE d.datname = :t"), {"t": target, "m": MIGRATOR_ROLE}).first()
    finally:
        admin.dispose()
    return {"name": target, "oid": row[0] if row else None, "owner": row[1] if row else None, "migrator_is_superuser": row[2] if row else None}


def require_restore_target(case: Case, target: str, captured: Captured) -> dict:
    """Refuses unless the target is a database THIS case created, owned by the migrator, empty, and not the source (by name and OID)."""
    identity = target_identity(case, target)
    if target == captured.source_database or identity["oid"] is None or identity["oid"] == captured.source_oid:
        raise RestoreRefused("the target is the source database or does not exist")
    if target not in case.targets:
        raise RestoreRefused("the target was not created by this case")
    if identity["owner"] != MIGRATOR_ROLE or identity["migrator_is_superuser"] is not False:
        raise RestoreRefused("the target is not owned by the non-superuser migrator")
    admin_target = case.cluster.admin_engine(target)
    try:
        with admin_target.connect() as conn:
            tables = conn.execute(text("SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema')")).scalar()
    finally:
        admin_target.dispose()
    require_destination_database_empty(tables)
    return identity


@dataclasses.dataclass
class RestoreOutcome:
    target: str
    identity: dict
    archive_sha256: str
    listing_returncode: int
    returncode: int | None
    stderr: str
    application_name: str
    argv_sanitized: list
    timed_out: bool
    process: object
    restore_backends_after: list


def restore(case: Case, captured: Captured, target: str, *, archive: str | None = None, timeout_s: float = RESTORE_TIMEOUT_S,
            require_archive_identity: bool = True, on_started=None) -> RestoreOutcome:
    archive = archive or captured.archive_copy
    identity = require_restore_target(case, target, captured)
    archive_sha = sha256_in_container(case, archive)
    listing = case.client._run(["pg_restore", "--list", "--", archive], timeout=60)
    listing_result = CommandResult(argv=["pg_restore", "--list"], returncode=listing.returncode, stdout=listing.stdout.decode(errors="replace"),
                                   stderr=listing.stderr.decode(errors="replace"))
    if require_archive_identity:
        require_archive_ready_for_restore(archive_sha, captured.archive_sha256, listing_result)
    pgpass = write_role_pgpass(case, MIGRATOR_ROLE, case.cluster.migrator_password)  # registered/removed by the transport's own rule
    application_name = f"{RESTORE_APP_PREFIX}{uuid.uuid4().hex[:12]}"
    dsn = f"{restore_conninfo(case, MIGRATOR_ROLE, target)} application_name={application_name}"
    argv = ["pg_restore", "-d", dsn, "--no-password", "--exit-on-error", "--single-transaction", archive]
    out, err = posixpath.join(case.container_dir, f"{application_name}.out"), posixpath.join(case.container_dir, f"{application_name}.err")
    process = case.client.start(argv, {"PGPASSFILE": pgpass}, out, err)
    timed_out = False
    try:
        if on_started is not None:
            on_started(application_name)
        returncode = process.wait(timeout_s)
        if returncode is None:
            timed_out = True
            process.stop(30)  # kills the REAL pg_restore inside the container, then reaps the docker-exec client
            returncode = process.popen.returncode
            end_restore_backend(case, target, application_name)  # owned target only: its own restore backend, by its unique name
    finally:
        if process.popen.poll() is None:
            process.stop(30)
        remove_role_pgpass(case, pgpass)
    stderr = case.client.read_bounded(err, STDERR_LIMIT).decode("utf-8", errors="replace")
    for secret in case.cluster.secrets_to_scrub():
        stderr = stderr.replace(secret, "[removed]")
    return RestoreOutcome(target, identity, archive_sha, listing.returncode, returncode, stderr, application_name,
                          [a if not a.startswith("host=") else "<conninfo: host, hostaddr, port, dbname=<target>, user=napsedge_migrator, sslmode=verify-full, sslrootcert, application_name>" for a in argv],
                          timed_out, process, restore_backends(case.cluster))


def end_restore_backend(case: Case, target: str, application_name: str) -> None:
    admin = case.cluster.admin_engine(target)
    try:
        with admin.connect() as conn:
            conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = :a AND datname = :d"),
                         {"a": application_name, "d": target})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with admin.connect() as conn:
                if not conn.execute(text("SELECT count(*) FROM pg_stat_activity WHERE application_name = :a"), {"a": application_name}).scalar():
                    return
            time.sleep(0.05)
    finally:
        admin.dispose()


# ---------------------------------------------------------------------------
# 3. independent comparison
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class Comparison:
    verdict: dict
    strict_sequences_equal: bool
    sequence_differences: list
    rules: list
    policy_accepts: bool
    r2_failures: list
    raw: object


def compare(case: Case, captured: Captured, target: str) -> Comparison:
    """The unchanged strict comparator against the SAVED baseline, through fresh transactions; monotonic_v1 evaluated separately."""
    baseline, baseline_details = saved_baseline(captured)
    admin_target = case.cluster.admin_engine(target)
    try:
        raw = compare_to_baseline(baseline, admin_target)  # each read opens its own connection and transaction
        restored_details = read_sequence_details(admin_target)
    finally:
        admin_target.dispose()
    verdict = {
        "fingerprints": raw.fingerprint_diffs == {},
        "constraints": raw.constraint_genuine_diff == {},
        "indexes": raw.index_diff == {},
        "owners": raw.owners_equal,
        "acls": raw.acls_equal,
        "version": raw.source_version == raw.restored_version,
        "constraints_validated": raw.unvalidated_constraints == [],
    }
    strict_sequences_equal = raw.sequence_source == raw.sequence_restored
    differences = [(a, b) for a, b in zip(raw.sequence_source, raw.sequence_restored) if a != b]
    rules = evaluate_rules(baseline_details, restored_details)
    policy = all(r.applicable and r.r1_no_regression and r.r2_next_id_safe for r in rules)
    return Comparison(verdict, strict_sequences_equal, differences, rules, policy, [r.sequence for r in rules if r.r2_next_id_safe is False], raw)


def rule_rows(comparison: Comparison) -> list:
    return [dataclasses.asdict(r) for r in comparison.rules]


def common_evidence(captured: Captured, outcome: RestoreOutcome) -> dict:
    return {
        "archive_sha256": captured.archive_sha256, "archive_bytes": captured.archive_bytes, "toc_sha256": captured.toc_sha256,
        "baseline_sha256": captured.baseline_sha256, "snapshot_id": captured.snapshot_id, "pg_dump_version": captured.pg_dump_version,
        "pg_restore_version": captured.pg_restore_version, "source": {"name": captured.source_database, "oid": captured.source_oid},
        "target": outcome.identity, "restore_role": MIGRATOR_ROLE, "restore_argv": outcome.argv_sanitized, "restore_exit": outcome.returncode,
        "restored_archive_sha256": outcome.archive_sha256, "listing_exit": outcome.listing_returncode, "restore_stderr": outcome.stderr[:400],
        "after_close": captured.after_close, "transport": "local" if native_mode() else "container",
    }


def assert_capture_closed(captured: Captured) -> None:
    after = captured.after_close
    assert after["pids_gone"] and after["advisory_holders"] == [] and after["dump_backends"] == []
    assert all(v == [] for v in after["lock_and_snapshot_sessions"].values())
    assert not after["original_archive_exists"] and after["copy_exists"]
    assert after["original_sha256"] == captured.archive_sha256  # the copy is byte-identical to the archive the dump produced


# ---------------------------------------------------------------------------
# A. no-change round trip
# ---------------------------------------------------------------------------
def test_a_no_change_round_trip_is_strictly_equal_and_the_sequence_policy_accepts(case, record_property):
    captured = capture(case)
    target = new_target(case)
    outcome = restore(case, captured, target)
    comparison = compare(case, captured, target) if outcome.returncode == 0 else None
    record(record_property, "A_no_change", acquired=True, body_entered=True, **common_evidence(captured, outcome),
           verdict=comparison and comparison.verdict, strict_sequences_equal=comparison and comparison.strict_sequences_equal,
           rules=comparison and rule_rows(comparison), policy_accepts=comparison and comparison.policy_accepts)
    assert_capture_closed(captured)
    assert outcome.archive_sha256 == captured.archive_sha256, "a different archive was restored"
    assert outcome.returncode == 0 and outcome.stderr == "", outcome.stderr
    assert target != captured.source_database and outcome.identity["oid"] != captured.source_oid
    assert outcome.identity["owner"] == MIGRATOR_ROLE and outcome.identity["migrator_is_superuser"] is False
    assert all(comparison.verdict.values()), (comparison.verdict, describe_diff(comparison.raw.fingerprint_diffs))
    assert comparison.strict_sequences_equal and comparison.sequence_differences == []
    assert comparison.policy_accepts and comparison.r2_failures == [] and len(comparison.rules) == 13


# ---------------------------------------------------------------------------
# B. snapshot data with a safe sequence advancement
# ---------------------------------------------------------------------------
def test_b_a_committed_sequence_generated_insert_is_excluded_the_strict_difference_is_kept_and_the_policy_accepts(case, record_property):
    writer = create_engine(case.cluster.migrator_url(case.database))
    case.ledger.engines.append(writer)

    def insert_after_snapshot(handle, snap_seam):
        before = admin_scalar(case.admin, "SELECT last_value FROM team_aliases_id_seq")
        with writer.begin() as conn:  # committed when the block exits; id from the sequence default
            new_id = conn.execute(text("INSERT INTO team_aliases (team_id, source, raw_value, normalized_value) "
                                       "VALUES (:t, 'odds', 'Roundtrip Alias', 'roundtrip alias') RETURNING id"), {"t": SAFE_INSERT_TEAM}).scalar_one()
        return {"new_id": new_id, "sequence_before": before,
                "sequence_after_fresh": admin_scalar(case.admin, "SELECT last_value FROM team_aliases_id_seq"),
                "fresh_reader_row": admin_scalar(case.admin, "SELECT count(*) FROM team_aliases WHERE id = :i", i=new_id),
                "s_row": snap_seam.connections[SNAPSHOT_APPLICATION_NAME].execute(
                    text("SELECT count(*) FROM team_aliases WHERE id = :i"), {"i": new_id}).scalar_one()}  # TEST seam: a SELECT in S

    captured = capture(case, before_dump=insert_after_snapshot)
    target = new_target(case)
    outcome = restore(case, captured, target)
    comparison = compare(case, captured, target) if outcome.returncode == 0 else None
    proof = captured.after_close["before_dump"]
    admin_target = case.cluster.admin_engine(target)
    try:
        with admin_target.connect() as conn:
            restored_row = conn.execute(text("SELECT count(*) FROM team_aliases WHERE id = :i"), {"i": proof["new_id"]}).scalar()
    finally:
        admin_target.dispose()
    record(record_property, "B_safe_advancement", acquired=True, body_entered=True, **common_evidence(captured, outcome), commit_proof=proof,
           restored_has_inserted_row=restored_row, verdict=comparison and comparison.verdict,
           strict_sequences_equal=comparison and comparison.strict_sequences_equal,
           sequence_differences=comparison and comparison.sequence_differences, rules=comparison and rule_rows(comparison),
           policy_accepts=comparison and comparison.policy_accepts)
    assert_capture_closed(captured)
    assert outcome.archive_sha256 == captured.archive_sha256, "a different archive was restored"
    assert outcome.returncode == 0, outcome.stderr
    assert proof["fresh_reader_row"] == 1 and proof["s_row"] == 0 and proof["sequence_after_fresh"] > proof["sequence_before"]
    assert restored_row == 0 and comparison.verdict["fingerprints"], describe_diff(comparison.raw.fingerprint_diffs)  # the captured snapshot, not later data
    assert all(comparison.verdict.values()), comparison.verdict
    assert not comparison.strict_sequences_equal  # the strict difference is retained, never folded into equality
    assert comparison.sequence_differences == [(("team_aliases_id_seq", proof["sequence_before"]), ("team_aliases_id_seq", proof["sequence_after_fresh"]))]
    assert comparison.policy_accepts and comparison.r2_failures == []  # a separate policy result


# ---------------------------------------------------------------------------
# C. equal but unsafe sequences (the established C4 fixture)
# ---------------------------------------------------------------------------
def test_c_equal_but_unsafe_sequences_keep_strict_equality_and_r2_rejects_the_policy(case, record_property):
    migrator = create_engine(case.cluster.migrator_url(case.database))
    case.ledger.engines.append(migrator)
    fixture = setup_manual_id(migrator)  # NEGATIVE-CONTROL SETUP on the source: an explicit id above the sequence; not a repair
    captured = capture(case)
    target = new_target(case)
    outcome = restore(case, captured, target)
    comparison = compare(case, captured, target) if outcome.returncode == 0 else None
    alias = next(r for r in comparison.rules if r.sequence == "team_aliases_id_seq")
    record(record_property, "C_equal_but_unsafe", acquired=True, body_entered=True, **common_evidence(captured, outcome), fixture=fixture,
           verdict=comparison.verdict, strict_sequences_equal=comparison.strict_sequences_equal, rules=rule_rows(comparison),
           policy_accepts=comparison.policy_accepts, r2_failures=comparison.r2_failures)
    assert_capture_closed(captured)
    assert outcome.returncode == 0 and all(comparison.verdict.values()) and comparison.strict_sequences_equal
    assert alias.r1_no_regression is True and alias.r2_next_id_safe is False and alias.column_max_restored == MANUAL_ID
    assert comparison.r2_failures == ["team_aliases_id_seq"]
    assert comparison.policy_accepts is False  # an explicit rejected policy result, although strict equality holds


# ---------------------------------------------------------------------------
# D. data tampering after restore
# ---------------------------------------------------------------------------
def test_d_a_changed_value_with_the_same_row_count_is_detected_by_the_fingerprint(case, record_property):
    captured = capture(case)
    target = new_target(case)
    outcome = restore(case, captured, target)
    migrator = create_engine(case.cluster.migrator_url(target))
    case.ledger.engines.append(migrator)
    with migrator.begin() as conn:  # target only
        conn.execute(text("UPDATE conferences SET canonical_name = canonical_name || ' (tampered)' "
                          "WHERE conference_id = (SELECT min(conference_id) FROM conferences)"))
    comparison = compare(case, captured, target)
    diffs = comparison.raw.fingerprint_diffs
    record(record_property, "D_data_tamper", acquired=True, body_entered=True, **common_evidence(captured, outcome), verdict=comparison.verdict,
           fingerprint_diffs={t: [list(a), list(b)] for t, (a, b) in diffs.items()})
    assert outcome.returncode == 0
    assert comparison.verdict["fingerprints"] is False and set(diffs) == {"conferences"}
    (rows_before, md5_before), (rows_after, md5_after) = diffs["conferences"]
    assert rows_before == rows_after and md5_before != md5_after


# ---------------------------------------------------------------------------
# E. catalog and privilege tampering (target only)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("variant", ["schema_index", "acl", "ownership"])
def test_e_catalog_acl_and_ownership_changes_are_exact_comparator_differences(case, record_property, variant):
    captured = capture(case)
    with contextlib.ExitStack() as stack:
        role = stack.enter_context(disposable_role(case.cluster)) if variant == "ownership" else None
        target = new_target(case)
        outcome = restore(case, captured, target)
        if variant == "schema_index":
            statements, engine = ["CREATE INDEX zz_roundtrip_tamper ON public.teams (display_name)"], create_engine(case.cluster.migrator_url(target))
        elif variant == "acl":
            statements, engine = ["REVOKE SELECT ON public.teams FROM napsedge_backup_ro"], create_engine(case.cluster.migrator_url(target))
        else:  # fixture privilege, target only, to a registered disposable probe role
            statements, engine = [f"ALTER TABLE public.seasons OWNER TO {role}"], case.cluster.admin_engine(target)
        case.ledger.engines.append(engine)
        with engine.begin() as conn:
            for statement in statements:
                conn.execute(text(statement))
        comparison = compare(case, captured, target)
        if role is not None:  # the probe role owns an object in the target: drop the target before the role is dropped
            engine.dispose()
            _drop_database(case.cluster, target)
            case.ledger.databases.remove(target)
    raw = comparison.raw
    record(record_property, f"E_{variant}", acquired=True, body_entered=True, **common_evidence(captured, outcome), verdict=comparison.verdict,
           index_diff={str(k): v for k, v in raw.index_diff.items()}, owners_equal=raw.owners_equal, acls_equal=raw.acls_equal)
    assert outcome.returncode == 0
    if variant == "schema_index":
        assert comparison.verdict["indexes"] is False and list(raw.index_diff) == [("teams", "zz_roundtrip_tamper")]
        assert comparison.verdict["owners"] and comparison.verdict["acls"] and comparison.verdict["fingerprints"]
    elif variant == "acl":
        assert comparison.verdict["acls"] is False and comparison.verdict["owners"] is True and comparison.verdict["indexes"]
    else:
        assert comparison.verdict["owners"] is False and comparison.verdict["acls"] is False and comparison.verdict["indexes"]


# ---------------------------------------------------------------------------
# F. failed restores: a damaged archive whose TOC is still readable, and a deterministic SQL error
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("variant", ["damaged_archive", "sql_error"])
def test_f_a_failed_restore_never_reaches_comparison_success_and_leaves_nothing(case, record_property, variant):
    captured = capture(case)
    target = new_target(case)
    archive = captured.archive_copy
    if variant == "damaged_archive":
        # same length, 64 bytes overwritten near the END, inside the last compressed table-data blocks. Observed while choosing this:
        # truncating to half made `pg_restore --list` fail, and damage at 3/4 of the file (inside the large TOC) made it crash (139).
        archive = posixpath.join(case.container_dir, "damaged.dump")
        offset = captured.archive_bytes - 300
        assert case.client._run(["sh", "-c", 'umask 077 && cp -- "$1" "$2" && head -c 64 /dev/zero | tr "\\000" "Z" | dd of="$2" bs=1 seek="$3" conv=notrunc 2>/dev/null',
                                 "sh", captured.archive_copy, archive, str(offset)]).returncode == 0
        assert case.client.file_state(archive) == ("regular", captured.archive_bytes)
    else:  # a committed, deterministic conflict that the empty-target guard (which counts tables) does not see
        migrator = create_engine(case.cluster.migrator_url(target))
        case.ledger.engines.append(migrator)
        with migrator.begin() as conn:
            conn.execute(text("CREATE TYPE public.teams AS (x integer)"))
    outcome = restore(case, captured, target, archive=archive, require_archive_identity=(variant == "sql_error"))
    comparison = comparison_error = None
    if outcome.returncode == 0:  # the gate under test: only a successful restore may be compared
        try:
            comparison = compare(case, captured, target)
        except Exception as exc:  # recorded; the restore-failure assertion below decides
            comparison_error = type(exc).__name__
    admin_target = case.cluster.admin_engine(target)
    try:
        with admin_target.connect() as conn:
            tables_after = conn.execute(text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")).scalar()
    finally:
        admin_target.dispose()
    record(record_property, f"F_{variant}", acquired=True, body_entered=True, **common_evidence(captured, outcome),
           compared=comparison is not None, comparison_error=comparison_error, tables_after=tables_after,
           client_processes_left=case.client.client_processes())
    assert outcome.returncode not in (0, None), "a failed restore was treated as a success"
    assert outcome.listing_returncode == 0  # the table of contents was still readable, and the restore still failed
    assert comparison_error is None
    assert comparison is None  # no comparison, let alone a comparison success
    assert tables_after == 0  # --single-transaction: nothing of the archive was committed
    fragment = "already exists" if variant == "sql_error" else "pg_restore: error"
    assert fragment in outcome.stderr
    assert case.client.client_processes() == [] and outcome.restore_backends_after == []


# ---------------------------------------------------------------------------
# G. a deterministically blocked restore reaches its deadline; the real pg_restore is killed and its backend ended
# ---------------------------------------------------------------------------
def test_g_a_blocked_restore_is_stopped_at_its_deadline_inside_the_container_and_leaves_no_backend(case, record_property):
    captured = capture(case)
    target = new_target(case)
    blocker_engine = create_engine(case.cluster.migrator_url(target))
    case.ledger.engines.append(blocker_engine)
    blocker = blocker_engine.connect()
    case.ledger.connections.append(blocker)
    blocker.begin()
    blocker.execute(text("CREATE TABLE public.alembic_version (version_num varchar(32))"))  # UNCOMMITTED: confined to the disposable target
    observed: dict = {}

    def watch(application_name):
        def run():
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not observed.get("backend"):
                row = case.admin.engine.connect()
                try:
                    found = row.execute(text("SELECT pid, datname, usename, wait_event_type FROM pg_stat_activity "
                                             "WHERE application_name = :a AND wait_event_type = 'Lock'"), {"a": application_name}).first()
                finally:
                    row.close()
                observed["backend"] = tuple(found) if found else None
                time.sleep(0.05)
        thread = threading.Thread(target=run, name="restore-watch")
        case.ledger.threads.append(thread)
        thread.start()

    started = time.monotonic()
    outcome = restore(case, captured, target, timeout_s=BLOCKED_RESTORE_TIMEOUT_S, on_started=watch)
    elapsed = time.monotonic() - started
    blocker.rollback()
    process = outcome.process
    record(record_property, "G_restore_deadline", acquired=True, body_entered=True, **common_evidence(captured, outcome), timed_out=outcome.timed_out,
           elapsed_s=round(elapsed, 2), blocked_backend=observed.get("backend"), killed_in_container=process.killed,
           container_pid=process.container_pid, gone_after_kill=process.gone_after_kill, restore_backends_after=outcome.restore_backends_after,
           client_processes_left=case.client.client_processes())
    assert observed.get("backend") is not None, "the restore backend was never observed blocked"
    assert observed["backend"][1] == target and observed["backend"][2] == MIGRATOR_ROLE  # restored as the non-superuser migrator
    assert outcome.timed_out and process.killed and process.gone_after_kill and outcome.returncode == case.client.killed_returncode  # the REAL pg_restore was killed
    assert outcome.restore_backends_after == [] and case.client.client_processes() == []
    assert BLOCKED_RESTORE_TIMEOUT_S <= elapsed < 60


# ---------------------------------------------------------------------------
# H. wrong-target refusal
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("variant", ["source", "unowned"])
def test_h_the_source_or_an_unowned_database_is_refused_before_any_restore(case, harness, record_property, variant):
    captured = capture(case)
    source_admin = case.cluster.admin_engine(case.database)
    case.ledger.engines.append(source_admin)
    before = all_fingerprints(source_admin)
    target = case.database if variant == "source" else harness.boot_database
    calls_before = list(case.client.calls)
    with pytest.raises(RestoreRefused) as info:
        restore(case, captured, target)
    after = all_fingerprints(source_admin)
    started = [c for c in case.client.calls[len(calls_before):] if c.startswith("start:")]
    record(record_property, f"H_wrong_target_{variant}", acquired=True, body_entered=False, refused=str(info.value), target_is_source=variant == "source",
           restore_processes_started=started, source_unchanged=before == after)
    assert started == [] and before == after
