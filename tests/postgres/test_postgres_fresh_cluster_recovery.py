"""
Phase 9B-4: loss-of-source recovery onto an independently initialized cluster.

What this adds
--------------
Phases 9B-1/9B-2 prove recovery *within one cluster*: the restore target shares
roles, certificate chain, superuser and server with the source, so a hidden
dependency on any of them cannot show up. This module removes that shelter:

* a SOURCE cluster and a DESTINATION cluster are two separately initialized
  PostgreSQL servers (different `system_identifier`, container, port, synthetic
  certificate authority, superuser and every credential);
* the archive is produced by the dedicated backup role over authenticated
  `verify-full` TLS, copied off the source, and the SOURCE CONTAINER IS REMOVED
  before the destination is touched;
* the destination starts with no NapsEdge roles and no application data (checked
  with a gate that is itself shown to fail on a populated cluster), is
  provisioned explicitly from the declared bootstrap definitions with newly
  generated credentials, and is restored atomically as the non-superuser migrator;
* nothing is read from the source after it stops: the comparison uses a baseline
  captured beforehand (held in memory only), and the seeded identifiers come from
  the seeding call.

Enforced gates versus recorded observations
-------------------------------------------
The preconditions of each stage are ENFORCED while the scenario runs: a guard
raises `PreconditionFailed` and the downstream action is never executed. They
are, in order:

1. source-to-host archive checksum equality -- before the source is removed;
2. confirmed source removal AND a failed connection to the source -- before any
   destination recovery step;
3. all three destination emptiness counts zero -- before bootstrap SQL runs;
4. destination archive checksum equality AND a successful `pg_restore --list`
   containing the expected table-data entry -- before bootstrap and before
   `run_restore`.

Everything else the tests assert afterwards (equality with the baseline, the
permission batteries, readiness, credentials, trust chain) is an OBSERVATION
about the result, not a gate that stopped anything.

The same guarded functions are used by the negative tests (a populated
destination, a checksum mismatch, an unreadable archive, an archive without the
expected entry, an unconfirmed source removal), each against real disposable
clusters, and each proves the refused action did not run. The 9B-2 rejection
matrices (wrong password, untrusted CA, host mismatch, TLS disabled, Unix
socket) are not repeated.

Ownership and cleanup
---------------------
Every container, temporary certificate directory and archive directory this
module creates is registered with an `owned_resources` ledger that removes them
and then verifies they are gone, on success AND on failure. A verification
failure never hides the original failure (it is attached as a note). The shared
`pg_container` and any named Docker volume are never used.

Everything here is synthetic: certificates, passwords and records are generated
per run, live outside the repository and in the throwaway containers, and no
real credential, password hash, globals dump or source data is involved.
"""
from __future__ import annotations

import contextlib
import os
import hashlib
import io
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest
from sqlalchemy import create_engine, text

from tests.postgres.conftest import TEST_DB_PREFIX, assert_disposable_database_name
from tests.postgres.pg_restore_compare import capture_baseline, compare_to_baseline, describe_diff, rows, scalar
from tests.postgres.test_postgres_roles_and_restore import (
    ACCEPTED_ALEMBIC_HEAD,
    MIGRATOR_ROLE,
    REQUIRED_READ_TABLES,
    RUNTIME_ROLE,
    CommandResult,
    _literal,
    alembic_check_as_migrator,
    assert_runtime_boundary,
    exercise_accepted_read_paths,
    provision_database_level_privileges,
)
from tests.postgres.test_postgres_tls_backup_restore import (
    BACKUP_ROLE,
    TLS_PROTOCOLS,
    TlsCluster,
    _app_engine_from_environment,
    _attempt,
    _container_logs,
    _exec,
    _injection_archive,
    _wait_until_ready,
    assert_backup_boundary,
    authenticated_sessions,
    conninfo,
    count_log,
    generate_tls_material,
    handwritten_dsn,
    isolated_database_environment,  # noqa: F401  (autouse module fixture: removes inherited PG* / NAPSEDGE_DATABASE_* variables)
    provision_backup_access,
    provision_database,
    run_dump,
    run_restore,
    tls_url,
    write_pgpass,
)

pytestmark = pytest.mark.postgres

ARCHIVE_NAME = "napsedge_recovery.dump"
DESTINATION_ARCHIVE_PATH = f"/tmp/{ARCHIVE_NAME}"
EXPECTED_TOC_ENTRY = "TABLE DATA public teams "
EMPTY_REPORT_KEYS = {"napsedge_roles", "application_databases", "bootstrap_database_tables"}


# --------------------------------------------------------------------------
# enforced preconditions
# --------------------------------------------------------------------------
class PreconditionFailed(AssertionError):
    """A condition a stage requires before the next one may run. Raised during execution; the downstream action is never executed."""


class RecoveryStepFailed(AssertionError):
    """A stage ran and failed (as opposed to a precondition that refused to let it start)."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PreconditionFailed(message)


def require_archive_checksums_match(source_sha: str, host_sha: str) -> None:
    require(bool(source_sha) and source_sha == host_sha, "the archive checksum differs between the source and the host copy; the source must not be removed")


def require_source_unavailable(gone: bool, connect_error: str) -> None:
    require(gone, "the source container still exists; destination recovery must not begin")
    require(bool(connect_error), "a connection to the source succeeded after its removal; destination recovery must not begin")


def require_destination_empty(report: dict) -> None:
    require(set(report) == EMPTY_REPORT_KEYS and all(count == 0 for count in report.values()),
            f"the destination is not empty ({sorted(report.items())}); bootstrap and restore are refused")


def require_archive_ready_for_restore(destination_sha: str, expected_sha: str, listing: CommandResult) -> None:
    require(bool(destination_sha) and destination_sha == expected_sha, "the archive checksum on the destination differs from the recorded one; restore is refused")
    require(listing.returncode == 0, "pg_restore --list failed on the destination archive; restore is refused")
    require(EXPECTED_TOC_ENTRY in listing.stdout, f"the archive listing lacks the expected {EXPECTED_TOC_ENTRY.strip()!r} entry; restore is refused")


# --------------------------------------------------------------------------
# ownership ledger: only what this module creates, removed and VERIFIED removed (also on failure)
# --------------------------------------------------------------------------
def _container_exists(container_id: str) -> bool:
    import docker as docker_sdk

    try:
        docker_sdk.from_env().containers.get(container_id)
        return True
    except docker_sdk.errors.NotFound:
        return False


@dataclass
class OwnedResources:
    container_ids: list = field(default_factory=list)
    directories: list = field(default_factory=list)


@contextlib.contextmanager
def owned_resources(*, exists: Callable[[str], bool] = _container_exists, remove_directory: Callable = shutil.rmtree):
    """
    Containers are stopped by the `launched_cluster` blocks nested inside; on exit
    this removes the registered directories and verifies that every registered
    container and directory is gone.

    Cleanup is exhaustive: a failure to remove or to inspect one resource never
    stops the attempt on the others. Every problem found -- a leak, a removal
    error, or an inspection error (for example an unreachable Docker daemon) --
    is collected. If another exception is already propagating, the problems are
    attached to it as notes and it is re-raised unchanged; otherwise they are
    reported together in one clearly worded AssertionError.
    """
    ledger = OwnedResources()
    primary: BaseException | None = None
    try:
        yield ledger
    except BaseException as exc:
        primary = exc
        raise
    finally:
        problems: list[str] = []
        for directory in ledger.directories:
            try:
                remove_directory(directory, ignore_errors=True)
            except Exception as exc:  # noqa: BLE001 -- must not mask the primary failure or skip the remaining resources
                problems.append(f"could not remove directory {Path(directory).name}: {type(exc).__name__}: {exc}")
        for container_id in ledger.container_ids:
            try:
                if exists(container_id):
                    problems.append(f"container {container_id[:12]} still exists")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"could not verify container {container_id[:12]}: {type(exc).__name__}: {exc}")
        for directory in ledger.directories:
            try:
                if Path(directory).exists():
                    problems.append(f"directory {Path(directory).name} still exists")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"could not verify directory {Path(directory).name}: {type(exc).__name__}: {exc}")
        if problems:
            message = "cleanup of resources created by the rehearsal failed or could not be verified: " + "; ".join(problems)
            if primary is not None:
                primary.add_note(message)
            else:
                raise AssertionError(message)


# --------------------------------------------------------------------------
# an independently initialized cluster (own container, own synthetic CA, own credentials)
# --------------------------------------------------------------------------
@contextlib.contextmanager
def launched_cluster(ledger: OwnedResources):
    """
    Starts one disposable PostgreSQL that accepts only verified-TLS + SCRAM TCP
    connections, with NO NapsEdge roles and no application database. Yields
    (cluster, container_id). The certificate directory is registered with the
    ledger the moment it exists, and the container id the moment it is known, so a
    failure at any later point of setup is still cleaned up and verified by the
    ledger (this function's own removal below is a convenience, not the guarantee).
    """
    from testcontainers.core.container import DockerContainer

    material = generate_tls_material()
    ledger.directories.append(material.workdir)
    admin_user = f"pgtest_{secrets.token_hex(4)}"
    admin_password = secrets.token_urlsafe(24)
    admin_db = f"{TEST_DB_PREFIX}admin_{secrets.token_hex(4)}"
    assert_disposable_database_name(admin_db)
    container = DockerContainer("postgres:16-alpine")
    container.with_env("POSTGRES_USER", admin_user).with_env("POSTGRES_PASSWORD", admin_password).with_env("POSTGRES_DB", admin_db)
    container.ports[5432] = ("127.0.0.1", None)
    container.with_kwargs(entrypoint=["sh", "-c", "while [ ! -f /tls/ready ]; do sleep 0.1; done; exec docker-entrypoint.sh postgres"])
    try:
        container.start()
        container_id = container.get_wrapped_container().id
        ledger.container_ids.append(container_id)
        raw = container.get_wrapped_container()
        assert raw.put_archive("/", _injection_archive(material))
        raw.reload()
        bindings = raw.ports["5432/tcp"]
        assert [b["HostIp"] for b in bindings] == ["127.0.0.1"], bindings
        port = int(bindings[0]["HostPort"])
        admin_url = tls_url(port, material.ca_cert.as_posix(), admin_user, admin_password, admin_db)
        if os.environ.get("NAPSEDGE_TEST_CLIENT") == "local":
            # TEST-ONLY (Phase 9B-6G, tests/postgres/capture_client_support.py): the tests run in a Linux runner container on the
            # same Docker bridge, so the ONE canonical URL every session uses is the cluster's bridge address on 5432. The host name
            # stays `localhost`, so verify-full still checks the certificate's SAN; the CA is this runner's own file. No client relocates it.
            networks = raw.attrs["NetworkSettings"]["Networks"]
            assert list(networks) == ["bridge"] and networks["bridge"]["IPAddress"], networks
            port = 5432
            admin_url = admin_url.set(port=port).update_query_dict({"hostaddr": networks["bridge"]["IPAddress"]})
        cluster = TlsCluster(
            container=container,
            admin_url=admin_url,
            admin_password=admin_password,
            migrator_password=secrets.token_urlsafe(24), runtime_password=secrets.token_urlsafe(24),
            backup_password=secrets.token_urlsafe(24), port=port, material=material,
        )
        _wait_until_ready(cluster, container)
        yield cluster, container_id
    finally:
        with contextlib.suppress(Exception):
            container.stop()
        shutil.rmtree(material.workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# the declared bootstrap definitions (the same ones provision BOTH clusters)
# --------------------------------------------------------------------------
_ROLE_ATTRIBUTES = "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"


def bootstrap_sql(cluster: TlsCluster, database: str) -> str:
    """
    Cluster-level bootstrap: three login roles with this cluster's own generated
    passwords, and the application database owned by the non-superuser migrator.
    Built in memory and fed to `psql` on stdin, so no password is ever an argument
    or a file; `password_encryption=scram-sha-256` makes the stored verifiers SCRAM.
    """
    assert_disposable_database_name(database)
    statements = [
        f"CREATE ROLE {role} LOGIN PASSWORD {_literal(password)} {_ROLE_ATTRIBUTES};"
        for role, password in ((MIGRATOR_ROLE, cluster.migrator_password), (RUNTIME_ROLE, cluster.runtime_password), (BACKUP_ROLE, cluster.backup_password))
    ]
    statements.append(f'CREATE DATABASE "{database}" OWNER {MIGRATOR_ROLE};')
    return "\n".join(statements) + "\n"


def psql_as_admin(cluster: TlsCluster, pgpass: str, sql: str, *, database: str | None = None, tuples_only: bool = False) -> CommandResult:
    """The exact operator command: psql, TCP + verify-full, explicit CA, protected pgpass, SQL on stdin, stop on first error."""
    admin = cluster.admin_url
    argv = ["psql", "-w", "-X", "-v", "ON_ERROR_STOP=1", "-d", conninfo(cluster, admin.username, database or admin.database)]
    if tuples_only:
        argv += ["-tA", "-F", "|"]
    argv += ["-f", "-"]
    return _exec(cluster, argv, env={"PGPASSFILE": pgpass}, stdin=sql)


_EMPTY_CHECK_SQL = """
SELECT 'napsedge_roles', count(*) FROM pg_roles WHERE rolname LIKE 'napsedge\\_%'
UNION ALL
SELECT 'application_databases', count(*) FROM pg_database WHERE NOT datistemplate AND datname NOT IN ('postgres', current_database())
UNION ALL
SELECT 'bootstrap_database_tables', count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema');
"""


def empty_destination_report(cluster: TlsCluster, pgpass: str) -> dict[str, int]:
    """The 'is this an empty recovery target?' measurement: three counts that the guard requires to be zero."""
    result = psql_as_admin(cluster, pgpass, _EMPTY_CHECK_SQL, tuples_only=True)
    assert result.returncode == 0, result.diagnostics()
    return {name: int(count) for name, count in (line.split("|") for line in result.stdout.strip().splitlines())}


# --------------------------------------------------------------------------
# container helpers
# --------------------------------------------------------------------------
def _sha256_in_container(cluster: TlsCluster, path: str) -> str:
    result = _exec(cluster, ["sha256sum", path])
    assert result.returncode == 0, result.diagnostics()
    return result.stdout.split()[0]


def _read_file_from_container(container, path: str) -> bytes:
    stream, _ = container.get_wrapped_container().get_archive(path)
    with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as tar:
        member = tar.getmembers()[0]
        return tar.extractfile(member).read()


def _put_file_into_container(container, directory: str, name: str, data: bytes) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.size, info.mode = len(data), 0o600
        tar.addfile(info, io.BytesIO(data))
    assert container.get_wrapped_container().put_archive(directory, buffer.getvalue())


def _system_identifier(cluster: TlsCluster) -> str:
    engine = cluster.admin_engine()
    try:
        return str(scalar(engine, "SELECT system_identifier FROM pg_control_system()"))
    finally:
        engine.dispose()


def _ca_fingerprint(cluster: TlsCluster) -> str:
    out = subprocess.run([shutil.which("openssl"), "x509", "-in", str(cluster.material.ca_cert), "-noout", "-fingerprint", "-sha256"],
                         capture_output=True, text=True, check=True).stdout
    return out.strip().split("=", 1)[1]


# --------------------------------------------------------------------------
# guarded stages (the same functions run the scenario and the negative tests)
# --------------------------------------------------------------------------
@dataclass
class ArchiveRecord:
    host_path: Path
    source_sha256: str
    host_sha256: str
    dump: CommandResult
    sessions: list


def take_archive_off_source(source: TlsCluster, source_db: str, archive_dir: Path, *, copy_out=_read_file_from_container, log: list | None = None) -> ArchiveRecord:
    """Dump as the backup role, copy the archive off the source, and REQUIRE source-to-host checksum equality before returning (so before any removal)."""
    pgpass = write_pgpass(source, [(BACKUP_ROLE, source.backup_password)])
    path = f"/tmp/napsedge_fresh_{uuid.uuid4().hex[:12]}.dump"
    dump = run_dump(source, conninfo(source, BACKUP_ROLE, source_db), path, pgpass)
    if dump.returncode != 0:
        raise RecoveryStepFailed(f"pg_dump failed: {dump.diagnostics()}")
    source_sha = _sha256_in_container(source, path)
    data = copy_out(source.container, path)
    host_sha = hashlib.sha256(data).hexdigest()
    require_archive_checksums_match(source_sha, host_sha)
    if log is not None:
        log.append("archive_checksums_match")
    host_path = archive_dir / ARCHIVE_NAME
    host_path.write_bytes(data)
    sessions = authenticated_sessions(_container_logs(source.container), app="pg_dump", user=BACKUP_ROLE, database=source_db)
    return ArchiveRecord(host_path=host_path, source_sha256=source_sha, host_sha256=host_sha, dump=dump, sessions=sessions)


@dataclass
class SourceLoss:
    confirmed: bool
    gone: bool
    connect_error: str


def remove_source(source: TlsCluster, source_id: str, source_db: str, *, remover=lambda container: container.stop(), exists=_container_exists, log: list | None = None) -> SourceLoss:
    """Remove the source this scenario owns, then REQUIRE confirmed removal and a failed connection before returning a confirmed SourceLoss."""
    remover(source.container)
    gone = not exists(source_id)
    probe = tls_url(source.port, source.ca_path, BACKUP_ROLE, source.backup_password, source_db).update_query_dict({"connect_timeout": "4"})
    connect_error = _attempt(source, probe) or ""
    require_source_unavailable(gone, connect_error)
    if log is not None:
        log.append("source_unavailable")
    return SourceLoss(confirmed=True, gone=gone, connect_error=connect_error)


def _default_bootstrap(cluster, pgpass, database):
    return psql_as_admin(cluster, pgpass, bootstrap_sql(cluster, database))


def _default_put_archive(cluster, data):
    _put_file_into_container(cluster.container, "/tmp", ARCHIVE_NAME, data)


def _default_sha256(cluster):
    return _sha256_in_container(cluster, DESTINATION_ARCHIVE_PATH)


def _default_list_archive(cluster):
    return _exec(cluster, ["pg_restore", "--list", DESTINATION_ARCHIVE_PATH])


def _default_restore(cluster, database, pgpass):
    return run_restore(cluster, conninfo(cluster, MIGRATOR_ROLE, database), DESTINATION_ARCHIVE_PATH, pgpass)


@dataclass(frozen=True)
class RecoveryActions:
    """The side-effecting steps of recovery. Defaults are the real ones; negative tests wrap them in a recorder to prove which steps ran."""

    empty_gate: Callable = empty_destination_report
    put_archive: Callable = _default_put_archive
    sha256: Callable = _default_sha256
    list_archive: Callable = _default_list_archive
    bootstrap: Callable = _default_bootstrap
    restore: Callable = _default_restore


@dataclass
class RecoveryRun:
    empty_gate: dict = field(default_factory=dict)
    archive_sha256: str = ""
    listing: CommandResult | None = None
    bootstrap: CommandResult | None = None
    restore: CommandResult | None = None
    guards: list = field(default_factory=list)


def recover_onto_destination(destination: TlsCluster, destination_db: str, archive: bytes, expected_sha256: str, loss: SourceLoss,
                             actions: RecoveryActions = RecoveryActions()) -> RecoveryRun:
    """
    Gate 2 (source loss), gate 3 (empty destination), gate 4 (archive ready)
    are enforced here, each BEFORE the step it protects:
    empty-check -> [gate 3] -> archive in -> checksum + listing -> [gate 4] -> bootstrap -> restore.
    """
    require(loss.confirmed, "the source loss is not confirmed; destination recovery is refused")
    run = RecoveryRun()
    admin_pgpass = write_pgpass(destination, [(destination.admin_url.username, destination.admin_password)])
    run.empty_gate = actions.empty_gate(destination, admin_pgpass)
    require_destination_empty(run.empty_gate)
    run.guards.append("destination_empty")
    actions.put_archive(destination, archive)
    run.archive_sha256 = actions.sha256(destination)
    run.listing = actions.list_archive(destination)
    require_archive_ready_for_restore(run.archive_sha256, expected_sha256, run.listing)
    run.guards.append("archive_ready_for_restore")
    run.bootstrap = actions.bootstrap(destination, admin_pgpass, destination_db)
    if run.bootstrap.returncode != 0:
        raise RecoveryStepFailed(f"bootstrap failed: {run.bootstrap.diagnostics()}")
    migrator_pgpass = write_pgpass(destination, [(MIGRATOR_ROLE, destination.migrator_password)])
    run.restore = actions.restore(destination, destination_db, migrator_pgpass)
    if run.restore.returncode != 0:
        raise RecoveryStepFailed(f"pg_restore failed: {run.restore.diagnostics()}")
    return run


def recording_actions(calls: list, **overrides) -> RecoveryActions:
    """Wraps every action so `calls` lists, in order, which ones actually executed; `overrides` replace an action's implementation."""
    real = RecoveryActions()

    def wrap(name):
        implementation = overrides.get(name, getattr(real, name))

        def call(*args, **kwargs):
            calls.append(name)
            return implementation(*args, **kwargs)

        return call

    return RecoveryActions(**{name: wrap(name) for name in ("empty_gate", "put_archive", "sha256", "list_archive", "bootstrap", "restore")})


# --------------------------------------------------------------------------
# the scenario
# --------------------------------------------------------------------------
@dataclass
class Rehearsal:
    source: TlsCluster
    destination: TlsCluster
    source_db: str
    destination_db: str
    seeded: dict
    owned_container_ids: list
    archive_dir: Path
    source_container_id: str
    archive_bytes: bytes = b""
    source_system_identifier: str = ""
    destination_system_identifier: str = ""
    source_ca_fingerprint: str = ""
    destination_ca_fingerprint: str = ""
    populated_gate: dict = field(default_factory=dict)
    archive_record: ArchiveRecord | None = None
    loss: SourceLoss | None = None
    run: RecoveryRun | None = None
    restore_sessions: list = field(default_factory=list)
    db_privileges_before_reprovision: dict = field(default_factory=dict)
    destination_archive_sha256_after: str = ""
    comparison: object = None
    timings: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    guard_log: list = field(default_factory=list)

    # convenience views used by the observation tests
    @property
    def empty_gate(self) -> dict:
        return self.run.empty_gate

    @property
    def restore(self) -> CommandResult:
        return self.run.restore


@pytest.fixture(scope="module")
def rehearsal(docker_available):
    clock = time.monotonic
    started = clock()
    timings: dict[str, float] = {}
    with owned_resources() as ledger:
        archive_dir = Path(tempfile.mkdtemp(prefix="napsedge_recovery_archive_"))
        ledger.directories.append(archive_dir)
        with launched_cluster(ledger) as (source, source_id), launched_cluster(ledger) as (destination, _destination_id):
            timings["two_cluster_startup_s"] = clock() - started
            source_db = f"{TEST_DB_PREFIX}{uuid.uuid4().hex[:16]}"
            destination_db = f"{TEST_DB_PREFIX}{uuid.uuid4().hex[:16]}"
            assert source_db != destination_db
            state = Rehearsal(source=source, destination=destination, source_db=source_db, destination_db=destination_db, seeded={},
                              owned_container_ids=ledger.container_ids, archive_dir=archive_dir, source_container_id=source_id)

            # ---- 1. source: bootstrap, migrate, grant, seed (existing helpers), distinctness evidence -----------------
            mark = clock()
            source_pgpass = write_pgpass(source, [(source.admin_url.username, source.admin_password)])
            boot = psql_as_admin(source, source_pgpass, bootstrap_sql(source, source_db))
            assert boot.returncode == 0, boot.diagnostics()
            state.seeded = provision_database(source, source_db, seed=True).seeded
            state.source_system_identifier, state.destination_system_identifier = _system_identifier(source), _system_identifier(destination)
            state.source_ca_fingerprint, state.destination_ca_fingerprint = _ca_fingerprint(source), _ca_fingerprint(destination)
            state.populated_gate = empty_destination_report(source, source_pgpass)  # control: the gate's measurement on a populated cluster
            timings["source_build_and_seed_s"] = clock() - mark

            # ---- 2. baseline captured BEFORE the source stops (held in memory only) ----------------------------------
            admin_engine = source.admin_engine(source_db)
            try:
                baseline = capture_baseline(admin_engine)
            finally:
                admin_engine.dispose()

            # ---- 3. GATE 1: dump, copy off, require checksum equality -- all before the source is removed -------------
            mark = clock()
            state.archive_record = take_archive_off_source(source, source_db, archive_dir, log=state.guard_log)
            state.archive_bytes = state.archive_record.host_path.read_bytes()
            timings["dump_and_copy_off_source_s"] = clock() - mark
            state.events.append(("archive_copied_off_source", clock()))

            # ---- 4. GATE 2: remove the source, require confirmed removal + failed connectivity -------------------------
            state.loss = remove_source(source, source_id, source_db, log=state.guard_log)
            state.events.append(("source_removed", clock()))

            # ---- 5. GATES 3 and 4, bootstrap, atomic restore (the guarded recovery path) ----------------------------------
            recovery_mark = clock()
            state.events.append(("destination_recovery_started", recovery_mark))
            state.run = recover_onto_destination(destination, destination_db, state.archive_bytes, state.archive_record.host_sha256, state.loss)
            state.guard_log.extend(state.run.guards)
            state.restore_sessions = authenticated_sessions(_container_logs(destination.container), app="pg_restore", user=MIGRATOR_ROLE, database=destination_db)

            # ---- 6. re-apply what the archive does not carry, then compare with the pre-loss baseline ----------------------
            admin = destination.admin_engine(destination_db)
            try:
                state.db_privileges_before_reprovision = {
                    "runtime_temporary": scalar(admin, "SELECT has_database_privilege(:r, :d, 'TEMPORARY')", r=RUNTIME_ROLE, d=destination_db),
                    "backup_connect_via_public": scalar(admin, "SELECT has_database_privilege(:r, :d, 'CONNECT')", r=BACKUP_ROLE, d=destination_db),
                }
            finally:
                admin.dispose()
            provision_database_level_privileges(destination, destination_db)
            provision_backup_access(destination, destination_db)
            admin = destination.admin_engine(destination_db)
            try:
                state.comparison = compare_to_baseline(baseline, admin)
            finally:
                admin.dispose()
            state.destination_archive_sha256_after = _sha256_in_container(destination, DESTINATION_ARCHIVE_PATH)
            timings["destination_recovery_s"] = clock() - recovery_mark
            state.timings = timings
            yield state


# --------------------------------------------------------------------------
# negative scenarios: each guard blocks its downstream action (real clusters, same guarded functions)
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def negative_source(docker_available):
    """A disposable, migrated source with no data; used only to prove source-side guards. Cleanup is verified at module teardown."""
    with owned_resources() as ledger, launched_cluster(ledger) as (cluster, container_id):
        db = f"{TEST_DB_PREFIX}{uuid.uuid4().hex[:16]}"
        pgpass = write_pgpass(cluster, [(cluster.admin_url.username, cluster.admin_password)])
        assert psql_as_admin(cluster, pgpass, bootstrap_sql(cluster, db)).returncode == 0
        provision_database(cluster, db, seed=False)
        yield cluster, container_id, db, ledger


@pytest.fixture(scope="module")
def empty_negative_destination(docker_available):
    """A disposable EMPTY destination. The archive negatives never let any step past the guards, so it stays empty for all of them."""
    with owned_resources() as ledger, launched_cluster(ledger) as (cluster, container_id):
        pgpass = write_pgpass(cluster, [(cluster.admin_url.username, cluster.admin_password)])
        require_destination_empty(empty_destination_report(cluster, pgpass))
        yield cluster, pgpass, container_id


def test_a_checksum_mismatch_between_source_and_host_copy_blocks_source_removal(negative_source, tmp_path):
    cluster, container_id, db, _ = negative_source
    log: list = []
    with pytest.raises(PreconditionFailed, match="checksum differs between the source and the host copy"):
        take_archive_off_source(cluster, db, tmp_path, copy_out=lambda container, path: _read_file_from_container(container, path) + b"\x00", log=log)
    assert log == [], "the checksum guard must not have passed"
    assert not (tmp_path / ARCHIVE_NAME).exists(), "no host archive may be recorded for a mismatching copy"
    assert _container_exists(container_id), "the source must not have been removed"
    assert _attempt(cluster, tls_url(cluster.port, cluster.ca_path, BACKUP_ROLE, cluster.backup_password, db)) is None, "the source must still accept connections"
    # control: the identical call with an honest copy-out passes the guard
    record = take_archive_off_source(cluster, db, tmp_path, log=log)
    assert log == ["archive_checksums_match"] and record.source_sha256 == record.host_sha256


def test_unconfirmed_source_removal_blocks_destination_recovery(negative_source):
    cluster, container_id, db, _ = negative_source
    # removal that does nothing: the container still exists
    with pytest.raises(PreconditionFailed, match="source container still exists"):
        remove_source(cluster, container_id, db, remover=lambda container: None)
    # container reported gone but the source still answers
    with pytest.raises(PreconditionFailed, match="a connection to the source succeeded"):
        remove_source(cluster, container_id, db, remover=lambda container: None, exists=lambda cid: False)
    assert _container_exists(container_id), "this negative test must not have removed anything"
    # and recovery itself refuses an unconfirmed loss before ANY action runs
    calls: list = []
    with pytest.raises(PreconditionFailed, match="source loss is not confirmed"):
        recover_onto_destination(cluster, "napsedge_pg_test_unused", b"", "", SourceLoss(confirmed=False, gone=False, connect_error=""), recording_actions(calls))
    assert calls == []


def test_a_populated_destination_blocks_bootstrap_and_restore_and_its_resources_are_still_cleaned_up(rehearsal):
    """Real populated destination (roles already present). Also proves cleanup on a SETUP failure: the cluster, its certificate directory and the ledger are all verified gone afterwards."""
    calls: list = []
    observed: dict = {}
    target_db = f"{TEST_DB_PREFIX}{uuid.uuid4().hex[:16]}"
    with pytest.raises(PreconditionFailed, match="destination is not empty") as excinfo:
        with owned_resources() as ledger:
            with launched_cluster(ledger) as (destination, container_id):
                observed["container_id"], observed["workdir"] = container_id, destination.material.workdir
                admin_pgpass = write_pgpass(destination, [(destination.admin_url.username, destination.admin_password)])
                assert psql_as_admin(destination, admin_pgpass, bootstrap_sql(destination, f"{TEST_DB_PREFIX}{uuid.uuid4().hex[:16]}")).returncode == 0
                try:
                    recover_onto_destination(destination, target_db, rehearsal.archive_bytes, rehearsal.archive_record.host_sha256, rehearsal.loss, recording_actions(calls))
                except PreconditionFailed:
                    admin = destination.admin_engine()
                    try:
                        observed["roles"] = scalar(admin, "SELECT count(*) FROM pg_roles WHERE rolname LIKE 'napsedge\\_%'")
                        observed["target_exists"] = scalar(admin, "SELECT count(*) FROM pg_database WHERE datname = :d", d=target_db)
                    finally:
                        admin.dispose()
                    observed["archive_present"] = _exec(destination, ["test", "-e", DESTINATION_ARCHIVE_PATH]).returncode == 0
                    raise
    assert "not removed" not in "".join(getattr(excinfo.value, "__notes__", [])), "cleanup left something behind"
    assert calls == ["empty_gate"], f"steps ran past the emptiness guard: {calls}"
    assert observed["roles"] == 3 and observed["target_exists"] == 0 and observed["archive_present"] is False
    assert not _container_exists(observed["container_id"]) and not observed["workdir"].exists()


@pytest.mark.parametrize("case", ["checksum_mismatch", "archive_listing_fails", "archive_lacks_expected_entry"])
def test_an_unverifiable_archive_blocks_bootstrap_and_restore(rehearsal, empty_negative_destination, case):
    destination, _pgpass, _ = empty_negative_destination
    good = rehearsal.archive_bytes
    expected = hashlib.sha256(good).hexdigest()
    overrides: dict = {}
    if case == "checksum_mismatch":
        archive, expected_sha, message = good[:-1] + bytes([good[-1] ^ 0xFF]), expected, "checksum on the destination differs"
    elif case == "archive_listing_fails":
        archive = good[:200]  # truncated, but its checksum is recorded as the expected one, so ONLY the listing can object
        expected_sha, message = hashlib.sha256(archive).hexdigest(), "pg_restore --list failed"
    else:
        archive, expected_sha, message = good, expected, "lacks the expected"
        overrides["list_archive"] = lambda cluster: CommandResult(argv=["pg_restore", "--list"], returncode=0, stdout="; a listing without the table-data entries\n", stderr="")
    calls: list = []
    with pytest.raises(PreconditionFailed, match=message):
        recover_onto_destination(destination, f"{TEST_DB_PREFIX}{uuid.uuid4().hex[:16]}", archive, expected_sha, rehearsal.loss, recording_actions(calls, **overrides))
    assert "bootstrap" not in calls and "restore" not in calls, f"a step ran past the archive guard: {calls}"
    assert calls[:3] == ["empty_gate", "put_archive", "sha256"]
    # nothing executed on the destination: it is still empty
    check_pgpass = write_pgpass(destination, [(destination.admin_url.username, destination.admin_password)])
    assert empty_destination_report(destination, check_pgpass) == {"napsedge_roles": 0, "application_databases": 0, "bootstrap_database_tables": 0}


# --------------------------------------------------------------------------
# pure tests of the guards and of the ownership ledger (no Docker)
# --------------------------------------------------------------------------
def _listing(returncode: int, stdout: str) -> CommandResult:
    return CommandResult(argv=["pg_restore", "--list"], returncode=returncode, stdout=stdout, stderr="")


@pytest.mark.parametrize(
    "guard, args, message",
    [
        (require_archive_checksums_match, ("a" * 64, "b" * 64), "checksum differs"),
        (require_archive_checksums_match, ("", ""), "checksum differs"),
        (require_source_unavailable, (False, "refused"), "still exists"),
        (require_source_unavailable, (True, ""), "succeeded after its removal"),
        (require_destination_empty, ({"napsedge_roles": 1, "application_databases": 0, "bootstrap_database_tables": 0},), "not empty"),
        (require_destination_empty, ({"napsedge_roles": 0, "application_databases": 2, "bootstrap_database_tables": 0},), "not empty"),
        (require_destination_empty, ({"napsedge_roles": 0, "application_databases": 0, "bootstrap_database_tables": 4},), "not empty"),
        (require_destination_empty, ({"napsedge_roles": 0, "application_databases": 0},), "not empty"),  # a missing count is not 'zero'
        (require_archive_ready_for_restore, ("a" * 64, "b" * 64, _listing(0, EXPECTED_TOC_ENTRY)), "checksum on the destination differs"),
        (require_archive_ready_for_restore, ("a" * 64, "a" * 64, _listing(1, EXPECTED_TOC_ENTRY)), "--list failed"),
        (require_archive_ready_for_restore, ("a" * 64, "a" * 64, _listing(0, "; nothing useful")), "lacks the expected"),
    ],
)
def test_each_guard_refuses_exactly_its_unsafe_condition(guard, args, message):
    with pytest.raises(PreconditionFailed, match=message):
        guard(*args)


def test_each_guard_accepts_the_safe_condition():
    require_archive_checksums_match("a" * 64, "a" * 64)
    require_source_unavailable(True, "connection refused")
    require_destination_empty({"napsedge_roles": 0, "application_databases": 0, "bootstrap_database_tables": 0})
    require_archive_ready_for_restore("a" * 64, "a" * 64, _listing(0, f"1; 0 0 {EXPECTED_TOC_ENTRY}teams postgres"))


def test_a_cleanup_inspection_failure_never_replaces_the_original_exception(tmp_path):
    """An inspection error (unreachable Docker daemon) is a NOTE on the original failure; every other resource is still inspected and removed."""
    first, second = "a" * 64, "b" * 64
    directory = tmp_path / "ledger_dir"
    directory.mkdir()
    inspected: list = []

    def exists(container_id):
        inspected.append(container_id)
        if container_id == first:
            raise OSError("docker daemon unreachable")
        return False

    with pytest.raises(ValueError, match="original failure") as excinfo:
        with owned_resources(exists=exists) as ledger:
            ledger.container_ids += [first, second]
            ledger.directories.append(directory)
            raise ValueError("original failure")
    notes = "\n".join(getattr(excinfo.value, "__notes__", []))
    assert f"could not verify container {first[:12]}" in notes and "OSError" in notes and "docker daemon unreachable" in notes
    assert inspected == [first, second], "an inspection error stopped the remaining inspections"
    assert not directory.exists(), "an inspection error stopped directory removal"

    # a removal error is handled the same way, and the remaining directory is still attempted
    removed: list = []
    kept = tmp_path / "kept_dir"
    other = tmp_path / "other_dir"
    kept.mkdir()
    other.mkdir()

    def remove(path, ignore_errors=True):
        removed.append(Path(path).name)
        if Path(path) == kept:
            raise PermissionError("access denied")
        shutil.rmtree(path, ignore_errors=ignore_errors)

    with pytest.raises(RuntimeError, match="boom") as excinfo:
        with owned_resources(exists=lambda cid: False, remove_directory=remove) as ledger:
            ledger.directories += [kept, other]
            raise RuntimeError("boom")
    assert removed == ["kept_dir", "other_dir"] and not other.exists()
    assert any("could not remove directory kept_dir" in note and "PermissionError" in note for note in excinfo.value.__notes__)

    # with NO primary failure the same problems are reported clearly, all together
    with pytest.raises(AssertionError, match="could not be verified") as excinfo2:
        with owned_resources(exists=exists) as ledger:
            ledger.container_ids += [first, second]
    assert f"could not verify container {first[:12]}" in str(excinfo2.value)


def test_normal_cleanup_succeeds_removes_directories_and_inspects_every_resource(tmp_path):
    dirs = [tmp_path / "one", tmp_path / "two"]
    for directory in dirs:
        directory.mkdir()
        (directory / "file").write_text("x")
    inspected: list = []

    with owned_resources(exists=lambda cid: inspected.append(cid) or False) as ledger:  # raises nothing
        ledger.container_ids += ["1" * 64, "2" * 64]
        ledger.directories += dirs

    assert inspected == ["1" * 64, "2" * 64]
    assert all(not d.exists() for d in dirs)


def test_certificate_directory_removal_is_verified_when_setup_fails(docker_available, monkeypatch):
    """
    The certificate directory is registered the moment it exists, so a failure
    later in setup (the container cannot start) still has its removal verified.
    No container is ever created: `start` is made to fail after the TLS material
    exists.
    """
    from testcontainers.core.container import DockerContainer

    def failing_start(self):
        raise RuntimeError("simulated setup failure")

    monkeypatch.setattr(DockerContainer, "start", failing_start)
    holder: dict = {}

    # (a) normal removal: the setup failure propagates alone, with the directory registered and gone
    with pytest.raises(RuntimeError, match="simulated setup failure") as excinfo:
        with owned_resources() as ledger:
            holder["ledger"] = ledger
            with launched_cluster(ledger):
                pytest.fail("setup was expected to fail before the body ran")
    registered = holder["ledger"].directories
    assert len(registered) == 1 and registered[0].name.startswith("napsedge_tls_")
    assert not registered[0].exists()
    assert getattr(excinfo.value, "__notes__", []) == []
    assert holder["ledger"].container_ids == [], "no container may exist for this setup failure"

    # (b) removal that silently fails: the ledger still VERIFIES, and says so on the original failure
    real_rmtree = shutil.rmtree
    leftovers: list = []
    try:
        with monkeypatch.context() as patch:
            patch.setattr(shutil, "rmtree", lambda *args, **kwargs: None)  # launched_cluster's own removal silently does nothing ...
            with pytest.raises(RuntimeError, match="simulated setup failure") as excinfo:
                with owned_resources(remove_directory=lambda *args, **kwargs: None) as ledger:  # ... and so does the ledger's, so only VERIFICATION can notice
                    holder["ledger"] = ledger
                    with launched_cluster(ledger):
                        pytest.fail("setup was expected to fail before the body ran")
        leftovers = list(holder["ledger"].directories)
        assert len(leftovers) == 1 and leftovers[0].exists(), "this control needs the directory to have survived"
        assert any(f"directory {leftovers[0].name} still exists" in note for note in excinfo.value.__notes__)
    finally:
        for directory in leftovers:
            real_rmtree(directory, ignore_errors=True)
    assert all(not d.exists() for d in leftovers)


def test_ledger_reports_a_leak_on_success_but_never_replaces_an_original_failure(tmp_path):
    leaked_directory = tmp_path / "ledger_dir"
    leaked_directory.mkdir()

    # success path + a leaked container -> the leak is reported
    with pytest.raises(AssertionError, match="still exists"):
        with owned_resources(exists=lambda cid: True) as ledger:
            ledger.container_ids.append("0" * 64)

    # failure path + a leaked container -> the ORIGINAL error propagates and carries the leak as a note
    with pytest.raises(ValueError, match="original failure") as excinfo:
        with owned_resources(exists=lambda cid: True) as ledger:
            ledger.container_ids.append("1" * 64)
            raise ValueError("original failure")
    assert any("still exists" in note for note in excinfo.value.__notes__)

    # directories are removed on both paths; a clean exit raises nothing
    with owned_resources(exists=lambda cid: False) as ledger:
        ledger.directories.append(leaked_directory)
    assert not leaked_directory.exists()
    leaked_directory.mkdir()
    with pytest.raises(RuntimeError):
        with owned_resources(exists=lambda cid: False) as ledger:
            ledger.directories.append(leaked_directory)
            raise RuntimeError("boom")
    assert not leaked_directory.exists()


# --------------------------------------------------------------------------
# observations about the successful recovery (not gates; they ran after the fact)
# --------------------------------------------------------------------------
def test_source_and_destination_are_distinct_independently_initialized_clusters(rehearsal):
    s, d = rehearsal.source, rehearsal.destination
    assert rehearsal.source_system_identifier and rehearsal.destination_system_identifier
    assert rehearsal.source_system_identifier != rehearsal.destination_system_identifier, "same system_identifier: not independently initialized"
    assert rehearsal.source_container_id != d.container.get_wrapped_container().id
    assert s.port != d.port
    assert rehearsal.source_ca_fingerprint != rehearsal.destination_ca_fingerprint, "the clusters share a certificate authority"
    assert s.admin_url.username != d.admin_url.username and s.admin_url.database != d.admin_url.database
    for role in ("migrator_password", "runtime_password", "backup_password", "admin_password"):
        assert getattr(s, role) != getattr(d, role), f"{role} was reused on the destination"
    assert rehearsal.source_db != rehearsal.destination_db


def test_the_enforced_gates_passed_in_order_and_before_the_steps_they_protect(rehearsal):
    assert rehearsal.guard_log == ["archive_checksums_match", "source_unavailable", "destination_empty", "archive_ready_for_restore"]
    assert rehearsal.loss.confirmed and rehearsal.loss.gone and rehearsal.loss.connect_error
    order = dict(rehearsal.events)
    assert order["archive_copied_off_source"] < order["source_removed"] < order["destination_recovery_started"]


def test_the_archive_was_produced_by_the_backup_role_over_authenticated_tls(rehearsal):
    record = rehearsal.archive_record
    assert record.dump.stderr.strip() == "", record.dump.diagnostics()
    assert record.sessions, "the source's log shows no authenticated pg_dump session"
    for session in record.sessions:
        assert session["user"] == BACKUP_ROLE and session["method_scram"] is True and session["tls_protocol"] in TLS_PROTOCOLS, session


def test_the_archive_arrived_intact_and_was_never_modified(rehearsal):
    record = rehearsal.archive_record
    assert record.source_sha256 == record.host_sha256 == rehearsal.run.archive_sha256
    assert rehearsal.destination_archive_sha256_after == rehearsal.run.archive_sha256, "restoring changed the archive"
    assert rehearsal.run.listing.returncode == 0 and EXPECTED_TOC_ENTRY in rehearsal.run.listing.stdout


def test_the_empty_destination_measurement_differs_between_a_populated_cluster_and_the_destination(rehearsal):
    assert rehearsal.populated_gate["napsedge_roles"] == 3 and rehearsal.populated_gate["application_databases"] >= 1, rehearsal.populated_gate
    assert rehearsal.empty_gate == {"napsedge_roles": 0, "application_databases": 0, "bootstrap_database_tables": 0}


def test_destination_roles_were_provisioned_from_the_declared_definitions_with_new_scram_credentials(rehearsal):
    d = rehearsal.destination
    assert rehearsal.run.bootstrap.returncode == 0 and rehearsal.empty_gate["napsedge_roles"] == 0
    admin = d.admin_engine()
    try:
        attrs = rows(admin, "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolcanlogin, left(rolpassword, 14) "
                            "FROM pg_authid WHERE rolname LIKE 'napsedge\\_%' ORDER BY 1")
        assert attrs == [(BACKUP_ROLE, False, False, False, False, False, True, "SCRAM-SHA-256$"),
                         (MIGRATOR_ROLE, False, False, False, False, False, True, "SCRAM-SHA-256$"),
                         (RUNTIME_ROLE, False, False, False, False, False, True, "SCRAM-SHA-256$")]
        assert scalar(admin, "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = :d", d=rehearsal.destination_db) == MIGRATOR_ROLE
    finally:
        admin.dispose()


def test_the_archive_does_not_carry_database_level_privileges_so_the_explicit_step_was_required(rehearsal):
    assert rehearsal.db_privileges_before_reprovision == {"runtime_temporary": True, "backup_connect_via_public": True}


def test_restore_ran_as_the_migrator_over_destination_tls_with_clean_stderr(rehearsal):
    assert rehearsal.restore.returncode == 0 and rehearsal.restore.stderr.strip() == "", rehearsal.restore.diagnostics()
    assert "--exit-on-error" in rehearsal.restore.argv and "--single-transaction" in rehearsal.restore.argv
    assert rehearsal.restore_sessions, "the destination's log shows no authenticated pg_restore session"
    for session in rehearsal.restore_sessions:
        assert session["user"] == MIGRATOR_ROLE and session["method_scram"] is True and session["tls_protocol"] in TLS_PROTOCOLS, session


def test_recovered_data_constraints_indexes_sequences_ownership_and_acls_equal_the_source_baseline(rehearsal):
    c = rehearsal.comparison
    assert c.fingerprint_diffs == {}, f"recovered data differs: {c.fingerprint_diffs}"
    assert c.constraint_genuine_diff == {}, describe_diff(c.constraint_genuine_diff)
    assert set(c.constraint_rendering_only) == set(c.constraint_diff)
    assert c.index_diff == {}, describe_diff(c.index_diff)
    assert c.unvalidated_constraints == []
    assert c.sequence_source == c.sequence_restored and any(name == "team_aliases_id_seq" and last for name, last in c.sequence_restored)
    assert c.owners_equal is True and c.acls_equal is True  # ownership and table/sequence ACLs, including the backup and runtime roles' grants
    assert c.source_version == c.restored_version == ACCEPTED_ALEMBIC_HEAD
    # guard against a vacuous equality: the recovered database really holds the known synthetic records
    admin = rehearsal.destination.admin_engine(rehearsal.destination_db)
    try:
        counts = {name: scalar(admin, f"SELECT count(*) FROM {name}") for name in ("conferences", "teams", "team_aliases", "players", "player_aliases", "source_artifacts", "player_stat_snapshots")}
    finally:
        admin.dispose()
    assert counts == {"conferences": 1, "teams": 3, "team_aliases": 3, "players": 2, "player_aliases": 2, "source_artifacts": 2, "player_stat_snapshots": 3}


def test_destination_runtime_and_backup_boundaries_hold(rehearsal):
    assert_runtime_boundary(rehearsal.destination, rehearsal.destination_db)  # reads succeed for exactly the accepted set, 27 write/DDL/escalation statements denied (42501)
    assert_backup_boundary(rehearsal.destination, rehearsal.destination_db)


def test_accepted_reads_alembic_check_and_readiness_on_the_destination(rehearsal, monkeypatch):
    from src.services.health import check_readiness

    d = rehearsal.destination
    alembic_check_as_migrator(d, rehearsal.destination_db)
    engine = _app_engine_from_environment(monkeypatch, handwritten_dsn(d, RUNTIME_ROLE, d.runtime_password, rehearsal.destination_db))
    try:
        assert check_readiness(engine).ready is True
        assert exercise_accepted_read_paths(engine, rehearsal.seeded) == REQUIRED_READ_TABLES
    finally:
        engine.dispose()
    admin = d.admin_engine(rehearsal.destination_db)
    try:
        assert scalar(admin, "SELECT version_num FROM alembic_version") == ACCEPTED_ALEMBIC_HEAD
    finally:
        admin.dispose()


def test_old_source_credentials_cannot_authenticate_to_the_destination(rehearsal):
    s, d, db = rehearsal.source, rehearsal.destination, rehearsal.destination_db
    for role, source_password, destination_password in (
        (MIGRATOR_ROLE, s.migrator_password, d.migrator_password),
        (RUNTIME_ROLE, s.runtime_password, d.runtime_password),
        (BACKUP_ROLE, s.backup_password, d.backup_password),
    ):
        assert _attempt(d, tls_url(d.port, d.ca_path, role, destination_password, db)) is None, f"control failed for {role}"
        error = _attempt(d, tls_url(d.port, d.ca_path, role, source_password, db))
        assert error is not None and "password authentication failed" in error, f"{role}: the old source password was not refused for the intended reason"
    error = _attempt(d, tls_url(d.port, d.ca_path, s.admin_url.username, s.admin_password, d.admin_url.database))
    assert error is not None and "password authentication failed" in error, "the old source superuser credentials were not refused"
    assert count_log(_container_logs(d.container), "password authentication failed") >= 4


def test_destination_uses_its_own_trusted_certificate_chain(rehearsal):
    s, d = rehearsal.source, rehearsal.destination
    db = rehearsal.destination_db
    engine = create_engine(tls_url(d.port, d.ca_path, MIGRATOR_ROLE, d.migrator_password, db))
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")).scalar() is True
    finally:
        engine.dispose()
    error = _attempt(d, tls_url(d.port, s.ca_path, MIGRATOR_ROLE, d.migrator_password, db))
    assert error is not None and "certificate verify failed" in error, "the destination's certificate chains to the source's authority"
    openssl = shutil.which("openssl")
    own = subprocess.run([openssl, "verify", "-CAfile", str(d.material.ca_cert), str(d.material.server_cert)], capture_output=True, text=True)
    foreign = subprocess.run([openssl, "verify", "-CAfile", str(s.material.ca_cert), str(d.material.server_cert)], capture_output=True, text=True)
    assert own.returncode == 0 and foreign.returncode != 0


def test_rehearsal_timings_are_recorded_and_labeled_synthetic(rehearsal, capsys):
    required = {"two_cluster_startup_s", "source_build_and_seed_s", "dump_and_copy_off_source_s", "destination_recovery_s"}
    assert required <= set(rehearsal.timings), rehearsal.timings
    with capsys.disabled():
        print("\nSYNTHETIC REHEARSAL TIMINGS (tiny synthetic data set on a developer workstation; not a production recovery guarantee): "
              + ", ".join(f"{k}={v:.1f}" for k, v in sorted(rehearsal.timings.items())))
    assert all(v > 0 for v in rehearsal.timings.values())
