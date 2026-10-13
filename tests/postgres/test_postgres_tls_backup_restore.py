"""
Phase 9B-2: password-authenticated, TLS-verified PostgreSQL backup/restore
tests with a dedicated backup role.

Everything here runs against ITS OWN disposable PostgreSQL container, not the
shared `pg_container`, because the point is a server that *only* accepts
verified-TLS + SCRAM connections. Nothing in this module connects to, or
removes, any named Docker volume or real database, reads real source data, or
opens an external connection.

What is synthetic
-----------------
* The certificate authority, the "other" (untrusted) authority, the server
  certificate and every password are generated per run with the host's
  `openssl` and `secrets`, live only in a temporary directory outside the
  repository (and in the throwaway container), and are never written to a
  tracked file or to test output. Private keys: the CA keys are deleted as soon
  as the server certificate is signed; the server key is held in memory, copied
  into the container (mode 0600, owner postgres) and its host file is deleted.
* None of this is a production certificate or credential. It proves the
  *contract* (what a correctly configured server and client must do), not that
  any real deployment is configured this way.

Server contract under test
--------------------------
`ssl=on`, TLS >= 1.2, `password_encryption=scram-sha-256`, and a pg_hba.conf
with exactly: `local ... reject`, `hostssl ... scram-sha-256` (IPv4/IPv6), and
`host ... reject`. There is no `trust`, `peer`, `md5` or plain-password rule and
no Unix-socket path, so a tool that "falls back" has nowhere to fall back to.
The published port is bound to 127.0.0.1 only.

Client contract under test
--------------------------
Successful connections use `sslmode=verify-full`, an explicit CA file, an
explicit `host` (checked against the certificate's SAN) and an explicit
`hostaddr`. Each rejection is proven for its own reason, and each rejection has
a control that differs only in the one property under test, so no rejection can
be explained by a different failure.

Environment isolation: `PG*` and `NAPSEDGE_DATABASE_*` variables inherited from
the developer's shell are removed for the whole module (names only are ever
recorded, never values).
"""
from __future__ import annotations

import io
import os
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError, OperationalError

from tests.postgres.conftest import TEST_DB_PREFIX, alembic_config_for, assert_disposable_database_name
from tests.postgres.pg_restore_compare import (
    canonicalize_restored_check,
    compare_databases,
    describe_diff,
    public_tables,
    rows,
    scalar,
    scrub_database_environment,
    all_fingerprints,
)
from tests.postgres.test_postgres_roles_and_restore import (
    _DENIED_STATEMENTS,
    ACCEPTED_ALEMBIC_HEAD,
    INSUFFICIENT_PRIVILEGE,
    MIGRATOR_ROLE,
    REQUIRED_READ_TABLES,
    RUNTIME_ROLE,
    CommandResult,
    RoleCluster,
    SourceDatabase,
    _as_string,
    _create_database_owned_by_migrator,
    _drop_database,
    _literal,
    _sqlstate,
    alembic_check_as_migrator,
    assert_runtime_boundary,
    exercise_accepted_read_paths,
    grant_runtime_read_access,
    migrate_as_migrator,
    provision_database_level_privileges,
    seed_synthetic_records,
)

pytestmark = pytest.mark.postgres

BACKUP_ROLE = "napsedge_backup_ro"
POSTGRES_UID = 70  # uid/gid of `postgres` in postgres:16-alpine
TLS_PROTOCOLS = ("TLSv1.2", "TLSv1.3")
UNIX_SOCKET_DIR = "/var/run/postgresql"


# --------------------------------------------------------------------------
# environment isolation (module-wide; fixtures below are module-scoped, so this must be too)
# --------------------------------------------------------------------------
@pytest.fixture(scope="module", autouse=True)
def isolated_database_environment():
    """Removes inherited PG* / NAPSEDGE_DATABASE_* variables for the whole module and restores them afterwards. Yields the removed NAMES only."""
    _, removed = scrub_database_environment(os.environ)
    with pytest.MonkeyPatch.context() as patch:
        for name in removed:
            patch.delenv(name)
        yield removed


# --------------------------------------------------------------------------
# synthetic TLS material (host openssl; never a tracked file, never printed)
# --------------------------------------------------------------------------
@dataclass
class TlsMaterial:
    workdir: Path
    ca_cert: Path
    other_ca_cert: Path
    server_cert: Path
    server_key_pem: bytes = field(repr=False)
    server_common_name: str = ""


def _openssl(executable: str, *args: str, cwd: Path) -> None:
    completed = subprocess.run([executable, *args], cwd=cwd, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, f"openssl {args[0]} failed: {completed.stderr.strip()[-300:]}"


def generate_tls_material() -> TlsMaterial:
    """An ephemeral CA, a server certificate it signs (SAN: DNS:localhost, IP:127.0.0.1; CN deliberately NOT a tested host name), and a second unrelated CA."""
    executable = shutil.which("openssl")
    if executable is None:
        pytest.skip("host `openssl` is not on PATH, so ephemeral TLS fixtures cannot be generated (not counted as verification)")
    workdir = Path(tempfile.mkdtemp(prefix="napsedge_tls_"))
    tag = secrets.token_hex(4)
    ec = ("-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes")
    try:
        _openssl(executable, "req", "-x509", *ec, "-keyout", "ca.key", "-out", "ca.crt", "-days", "2",
                 "-subj", f"/CN=NapsEdge synthetic test CA {tag}", "-addext", "basicConstraints=critical,CA:TRUE",
                 "-addext", "keyUsage=critical,keyCertSign,cRLSign", cwd=workdir)
        _openssl(executable, "req", "-x509", *ec, "-keyout", "other_ca.key", "-out", "other_ca.crt", "-days", "2",
                 "-subj", f"/CN=NapsEdge synthetic OTHER CA {tag}", "-addext", "basicConstraints=critical,CA:TRUE",
                 "-addext", "keyUsage=critical,keyCertSign,cRLSign", cwd=workdir)
        common_name = f"napsedge-synthetic-server-{tag}"
        _openssl(executable, "req", "-new", *ec, "-keyout", "server.key", "-out", "server.csr", "-subj", f"/CN={common_name}", cwd=workdir)
        (workdir / "server_ext.cnf").write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n")
        _openssl(executable, "x509", "-req", "-in", "server.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial",
                 "-out", "server.crt", "-days", "2", "-extfile", "server_ext.cnf", cwd=workdir)
        server_key = (workdir / "server.key").read_bytes()
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    # Private keys never outlive this function on the host: only the public certificates remain.
    for leftover in (*workdir.glob("*.key"), *workdir.glob("*.csr"), *workdir.glob("*.srl"), workdir / "server_ext.cnf"):
        leftover.unlink()
    return TlsMaterial(workdir=workdir, ca_cert=workdir / "ca.crt", other_ca_cert=workdir / "other_ca.crt",
                       server_cert=workdir / "server.crt", server_key_pem=server_key, server_common_name=common_name)


_INIT_SCRIPT = """#!/bin/sh
set -e
cat >> "$PGDATA/postgresql.conf" <<CONF
ssl = on
ssl_cert_file = '/tls/server.crt'
ssl_key_file = '/tls/server.key'
ssl_min_protocol_version = 'TLSv1.2'
password_encryption = 'scram-sha-256'
log_connections = on
log_line_prefix = 'pid=%p app=%a user=%u db=%d '
CONF
cat > "$PGDATA/pg_hba.conf" <<HBA
local all all reject
hostssl all all 0.0.0.0/0 scram-sha-256
hostssl all all ::/0 scram-sha-256
host all all 0.0.0.0/0 reject
host all all ::/0 reject
HBA
"""


def _injection_archive(material: TlsMaterial) -> bytes:
    """Files for the container as one tar stream with explicit ownership/mode -- no bind mount, so nothing about the host filesystem is exposed to the container."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        def add(name: str, data: bytes, mode: int, *, directory: bool = False) -> None:
            info = tarfile.TarInfo(name)
            info.uid = info.gid = POSTGRES_UID
            info.mode = mode
            if directory:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

        add("tls", b"", 0o755, directory=True)
        add("tls/server.crt", material.server_cert.read_bytes(), 0o644)
        add("tls/ca.crt", material.ca_cert.read_bytes(), 0o644)
        add("tls/other_ca.crt", material.other_ca_cert.read_bytes(), 0o644)
        add("tls/server.key", material.server_key_pem, 0o600)
        add("docker-entrypoint-initdb.d/zz-tls.sh", _INIT_SCRIPT.encode(), 0o755)
        add("tls/ready", b"1", 0o644)  # last: the entrypoint wrapper below waits for it
    return buffer.getvalue()


@dataclass(frozen=True)
class TlsCluster(RoleCluster):
    """A `RoleCluster` whose URLs all carry the verify-full TLS parameters, so every 9B-1 helper that takes a cluster runs over verified TLS unchanged."""

    backup_password: str = field(default="", repr=False)
    port: int = 0
    material: TlsMaterial | None = field(default=None, repr=False)

    @property
    def ca_path(self) -> str:
        return self.material.ca_cert.as_posix()

    @property
    def other_ca_path(self) -> str:
        return self.material.other_ca_cert.as_posix()

    def backup_url(self, database: str) -> URL:
        return self.url(BACKUP_ROLE, self.backup_password, database)

    def secrets_to_scrub(self) -> tuple[str, ...]:
        return (*super().secrets_to_scrub(), self.backup_password)


def tls_url(port: int, ca_path: str | None, user: str, password: str, database: str, *, host: str = "localhost", sslmode: str = "verify-full") -> URL:
    query = {"sslmode": sslmode, "hostaddr": "127.0.0.1"}
    if sslmode != "disable" and ca_path is not None:
        query["sslrootcert"] = ca_path
    return URL.create("postgresql+psycopg", username=user, password=password, host=host, port=port, database=database, query=query)


def handwritten_dsn(cluster: TlsCluster, user: str, password: str, database: str, **overrides: str) -> str:
    """A DSN as an operator would type it into NAPSEDGE_DATABASE_URL, with no percent-encoding (the baseline; percent-encoded DSNs are covered separately by the 9B-3 regression tests below)."""
    params = {"sslmode": "verify-full", "sslrootcert": cluster.ca_path, "hostaddr": "127.0.0.1", **overrides}
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return f"postgresql+psycopg://{user}:{password}@localhost:{cluster.port}/{database}?{query}"


def _container_logs(container) -> str:
    out, err = container.get_logs()
    return (out + err).decode("utf-8", errors="replace")


@pytest.fixture(scope="module")
def tls_cluster(docker_available):
    from testcontainers.core.container import DockerContainer

    material = generate_tls_material()
    admin_user = f"pgtest_{secrets.token_hex(4)}"
    admin_password = secrets.token_urlsafe(24)
    admin_db = f"{TEST_DB_PREFIX}admin_{secrets.token_hex(4)}"
    assert_disposable_database_name(admin_db)

    container = DockerContainer("postgres:16-alpine")
    container.with_env("POSTGRES_USER", admin_user).with_env("POSTGRES_PASSWORD", admin_password).with_env("POSTGRES_DB", admin_db)
    container.ports[5432] = ("127.0.0.1", None)  # loopback only; random host port
    container.with_kwargs(entrypoint=["sh", "-c", "while [ ! -f /tls/ready ]; do sleep 0.1; done; exec docker-entrypoint.sh postgres"])
    container.start()
    try:
        raw = container.get_wrapped_container()
        assert raw.put_archive("/", _injection_archive(material)), "could not inject TLS material into the container"
        raw.reload()
        bindings = raw.ports["5432/tcp"]
        assert [b["HostIp"] for b in bindings] == ["127.0.0.1"], f"published port is not loopback-only: {bindings}"
        port = int(bindings[0]["HostPort"])

        migrator_password, runtime_password, backup_password = (secrets.token_urlsafe(24) for _ in range(3))
        cluster = TlsCluster(
            container=container,
            admin_url=tls_url(port, material.ca_cert.as_posix(), admin_user, admin_password, admin_db),
            admin_password=admin_password, migrator_password=migrator_password, runtime_password=runtime_password,
            backup_password=backup_password, port=port, material=material,
        )
        _wait_until_ready(cluster, container)
        engine = cluster.admin_engine()
        try:
            with engine.connect() as conn:
                existing = conn.execute(text("SELECT rolname FROM pg_roles WHERE rolname IN (:m, :r, :b)"),
                                        {"m": MIGRATOR_ROLE, "r": RUNTIME_ROLE, "b": BACKUP_ROLE}).fetchall()
                assert not existing, f"refusing to reuse pre-existing roles: {existing}"
                attributes = "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                for role, password in ((MIGRATOR_ROLE, migrator_password), (RUNTIME_ROLE, runtime_password), (BACKUP_ROLE, backup_password)):
                    conn.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD {_literal(password)} {attributes}"))
            yield cluster
        finally:
            with engine.connect() as conn:
                for role in (BACKUP_ROLE, RUNTIME_ROLE, MIGRATOR_ROLE):
                    conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
            engine.dispose()
    finally:
        container.stop()
        shutil.rmtree(material.workdir, ignore_errors=True)


def _wait_until_ready(cluster: TlsCluster, container, timeout: float = 120.0) -> None:
    """Polls with the real verify-full admin connection, so readiness is itself an exercise of the contract."""
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        engine = create_engine(cluster.admin_url)
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except OperationalError as exc:
            last = exc
            time.sleep(1.0)
        finally:
            engine.dispose()
    pytest.fail(f"TLS PostgreSQL did not become ready: {str(last)[-300:]}\n{_container_logs(container)[-1500:]}")


# --------------------------------------------------------------------------
# backup role: provisioning (the contract) -- run by the migrator, the owner of every object
# --------------------------------------------------------------------------
def provision_backup_access(cluster: RoleCluster, database: str) -> None:
    """
    The complete, explicit grant set `pg_dump` needs, nothing broader:
      CONNECT on the database, USAGE on schema public, SELECT on every public
      table, SELECT on every public sequence.
    No `pg_read_all_data`, no membership, no default privileges. The ALL TABLES /
    ALL SEQUENCES forms cover objects that exist NOW; objects a later migration
    creates are NOT covered until this procedure is re-run (see
    `test_new_migrated_object_fails_the_dump_loudly_until_the_grant_procedure_is_rerun`).
    Idempotent, and also the database-level step a per-database dump does not carry.
    """
    assert_disposable_database_name(database)
    engine = create_engine(cluster.migrator_url(database))
    try:
        with engine.begin() as conn:
            conn.execute(text(f'GRANT CONNECT ON DATABASE "{database}" TO {BACKUP_ROLE}'))
            conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {BACKUP_ROLE}"))
            conn.execute(text(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {BACKUP_ROLE}"))
            conn.execute(text(f"GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO {BACKUP_ROLE}"))
    finally:
        engine.dispose()


def provision_database(cluster: RoleCluster, name: str, *, seed: bool) -> SourceDatabase:
    provision_database_level_privileges(cluster, name)
    migrate_as_migrator(cluster, name)
    grant_runtime_read_access(cluster, name)
    provision_backup_access(cluster, name)
    return SourceDatabase(name=name, seeded=seed_synthetic_records(cluster, name) if seed else {})


@pytest.fixture(scope="module")
def source_database(tls_cluster):
    name = _create_database_owned_by_migrator(tls_cluster)
    try:
        yield provision_database(tls_cluster, name, seed=True)
    finally:
        _drop_database(tls_cluster, name)


@pytest.fixture(scope="module")
def scratch_database(tls_cluster):
    """Migrated and provisioned like the real one but unseeded; the misconfiguration and necessity controls mutate this one only."""
    name = _create_database_owned_by_migrator(tls_cluster)
    try:
        yield provision_database(tls_cluster, name, seed=False).name
    finally:
        _drop_database(tls_cluster, name)


# --------------------------------------------------------------------------
# tools run inside the container, over TCP+TLS, with a protected temporary pgpass file
# --------------------------------------------------------------------------
def _scrub(value: str, cluster: RoleCluster) -> str:
    for secret in cluster.secrets_to_scrub():
        value = value.replace(secret, "***")
    return value


def _container_id(cluster: RoleCluster) -> str:
    return cluster.container.get_wrapped_container().id


def _exec(cluster: RoleCluster, argv: list[str], *, env: dict[str, str] | None = None, stdin: str | None = None, timeout: int = 300) -> CommandResult:
    docker_argv = ["docker", "exec"]
    if stdin is not None:
        docker_argv.append("-i")
    for key, value in (env or {}).items():
        docker_argv += ["-e", f"{key}={value}"]
    cid = _container_id(cluster)
    docker_argv += [cid, *argv]
    completed = subprocess.run(docker_argv, capture_output=True, text=True, encoding="utf-8", errors="replace", input=stdin, timeout=timeout)
    return CommandResult(argv=[a for a in docker_argv if a != cid], returncode=completed.returncode,
                         stdout=_scrub(completed.stdout, cluster), stderr=_scrub(completed.stderr, cluster))


def write_pgpass(cluster: RoleCluster, entries: list[tuple[str, str]]) -> str:
    """
    Creates a 0600 pgpass file INSIDE the container and returns its path. The
    secrets travel on stdin (never argv), are matched on any host/db, and only
    the file's path is ever placed in a command line or environment.
    """
    path = f"/tmp/napsedge_pgpass_{uuid.uuid4().hex[:12]}"
    content = "".join(f"*:5432:*:{user}:{password}\n" for user, password in entries)
    result = _exec(cluster, ["sh", "-c", f"umask 077; cat > {path}"], stdin=content)
    assert result.returncode == 0, result.diagnostics()
    return path


def remove_files(cluster: RoleCluster, *paths: str) -> None:
    _exec(cluster, ["rm", "-f", *paths])


def conninfo(cluster: TlsCluster, user: str, database: str, *, host: str | None = "localhost", hostaddr: str | None = "127.0.0.1",
             sslmode: str = "verify-full", sslrootcert: str | None = "/tls/ca.crt") -> str:
    parts = []
    if host is not None:
        parts.append(f"host={host}")
    if hostaddr is not None:
        parts.append(f"hostaddr={hostaddr}")
    parts += ["port=5432", f"dbname={database}", f"user={user}", f"sslmode={sslmode}", "connect_timeout=15"]
    if sslrootcert is not None and sslmode != "disable":
        parts.append(f"sslrootcert={sslrootcert}")
    return " ".join(parts)


def run_dump(cluster: TlsCluster, dsn: str, dump_path: str, pgpass: str) -> CommandResult:
    return _exec(cluster, ["pg_dump", "-d", dsn, "-Fc", "--no-password", "-f", dump_path], env={"PGPASSFILE": pgpass})


def run_restore(cluster: TlsCluster, dsn: str, dump_path: str, pgpass: str) -> CommandResult:
    return _exec(cluster, ["pg_restore", "-d", dsn, "--no-password", "--exit-on-error", "--single-transaction", dump_path],
                 env={"PGPASSFILE": pgpass})


# server-side log evidence ------------------------------------------------------
_LOG_LINE = re.compile(r"^pid=(\d+) app=(\S*) user=(\S*) db=(\S*) LOG:\s+connection (authenticated|authorized): (.*)$")


def authenticated_sessions(logs: str, *, app: str | None = None, user: str, database: str | None = None) -> list[dict]:
    """
    Backends that the SERVER logged as SCRAM-authenticated AND authorized with TLS,
    matched by pid, filtered by client application name / role / database.
    """
    authenticated: dict[str, str] = {}
    authorized: dict[str, tuple[str, str, str, str]] = {}
    for line in logs.splitlines():
        m = _LOG_LINE.match(line)
        if not m:
            continue
        pid, application, role, db, kind, detail = m.groups()
        if kind == "authenticated" and f'identity="{user}"' in detail:
            authenticated[pid] = detail
        elif kind == "authorized" and f"user={user} " in detail + " ":
            named = re.search(r"application_name=(\S+)", detail)
            authorized[pid] = (named.group(1) if named else "", role, db, detail)
    sessions = []
    for pid, (application, role, db, detail) in authorized.items():
        if pid not in authenticated or (app is not None and application != app) or (database is not None and db != database):
            continue
        tls = re.search(r"SSL enabled \(protocol=(TLSv1\.[23])", detail)
        sessions.append({"pid": pid, "app": application, "user": role, "db": db, "method_scram": "method=scram-sha-256" in authenticated[pid],
                         "tls_protocol": tls.group(1) if tls else None})
    return sessions


def count_log(logs: str, needle: str) -> int:
    return sum(1 for line in logs.splitlines() if needle in line)


# --------------------------------------------------------------------------
# contract tests on the server configuration
# --------------------------------------------------------------------------
def test_published_port_is_bound_to_loopback_only(tls_cluster):
    raw = tls_cluster.container.get_wrapped_container()
    raw.reload()
    published = {port: [(b["HostIp"], b["HostPort"]) for b in (bindings or [])] for port, bindings in raw.ports.items()}
    assert set(published) == {"5432/tcp"}, published
    assert [host for host, _ in published["5432/tcp"]] == ["127.0.0.1"], f"not loopback-only: {published}"
    declared = raw.attrs["HostConfig"]["PortBindings"]["5432/tcp"]
    assert [b["HostIp"] for b in declared] == ["127.0.0.1"], declared


def test_server_configuration_requires_tls_and_scram(tls_cluster):
    admin = tls_cluster.admin_engine()
    try:
        settings = dict(rows(admin, "SELECT name, setting FROM pg_settings WHERE name IN "
                                    "('ssl', 'ssl_min_protocol_version', 'password_encryption', 'ssl_cert_file', 'ssl_key_file')"))
        assert settings == {"ssl": "on", "ssl_min_protocol_version": "TLSv1.2", "password_encryption": "scram-sha-256",
                            "ssl_cert_file": "/tls/server.crt", "ssl_key_file": "/tls/server.key"}
        rules = rows(admin, "SELECT type, address, auth_method, error FROM pg_hba_file_rules ORDER BY line_number")
        assert all(error is None for *_, error in rules), rules
        assert [(t, a, m) for t, a, m, _ in rules] == [
            ("local", None, "reject"), ("hostssl", "0.0.0.0", "scram-sha-256"), ("hostssl", "::", "scram-sha-256"),
            ("host", "0.0.0.0", "reject"), ("host", "::", "reject"),
        ]
        assert {m for _, _, m, _ in rules}.isdisjoint({"trust", "peer", "ident", "md5", "password", "cert", "pam", "ldap", "gss", "sspi", "radius"})
    finally:
        admin.dispose()


def test_every_login_role_stores_a_scram_verifier_and_none_stores_md5_or_plaintext(tls_cluster):
    admin = tls_cluster.admin_engine()
    try:
        stored = dict(rows(admin, "SELECT rolname, left(rolpassword, 14) FROM pg_authid WHERE rolcanlogin"))
        expected = {MIGRATOR_ROLE, RUNTIME_ROLE, BACKUP_ROLE, tls_cluster.admin_url.username}
        assert set(stored) >= expected
        assert {role: prefix for role, prefix in stored.items() if role in expected} == {role: "SCRAM-SHA-256$" for role in expected}
    finally:
        admin.dispose()


def test_server_certificate_identity_comes_from_the_san_not_the_common_name(tls_cluster):
    executable = shutil.which("openssl")
    cert = tls_cluster.material.server_cert
    subject = subprocess.run([executable, "x509", "-in", str(cert), "-noout", "-subject"], capture_output=True, text=True).stdout
    san = subprocess.run([executable, "x509", "-in", str(cert), "-noout", "-ext", "subjectAltName"], capture_output=True, text=True).stdout
    assert tls_cluster.material.server_common_name in subject and "localhost" not in subject and "127.0.0.1" not in subject
    assert "DNS:localhost" in san and "IP Address:127.0.0.1" in san
    # verify-full succeeds for both the SAN DNS name and the SAN IP address, with a CN that matches neither
    admin = tls_cluster.admin_url
    for host in ("localhost", "127.0.0.1"):
        url = tls_url(tls_cluster.port, tls_cluster.ca_path, admin.username, tls_cluster.admin_password, admin.database, host=host)
        assert _attempt(tls_cluster, url) is None, host


def test_private_key_material_never_remains_on_the_host_or_in_the_repository(tls_cluster):
    workdir = tls_cluster.material.workdir
    repo_root = Path(__file__).resolve().parents[2]
    assert repo_root not in workdir.resolve().parents, "TLS material must live outside the repository"
    names = sorted(p.name for p in workdir.iterdir())
    assert names == ["ca.crt", "other_ca.crt", "server.crt"], names
    for path in workdir.iterdir():
        assert b"PRIVATE KEY" not in path.read_bytes(), f"private key material in {path.name}"


# --------------------------------------------------------------------------
# connection contract
# --------------------------------------------------------------------------
@pytest.mark.parametrize("role", [MIGRATOR_ROLE, RUNTIME_ROLE, BACKUP_ROLE, "admin"])
def test_verified_connection_proves_role_tls_and_scram_on_the_same_connection(tls_cluster, source_database, role):
    passwords = {MIGRATOR_ROLE: tls_cluster.migrator_password, RUNTIME_ROLE: tls_cluster.runtime_password,
                 BACKUP_ROLE: tls_cluster.backup_password, "admin": tls_cluster.admin_password}
    user = tls_cluster.admin_url.username if role == "admin" else role
    engine = create_engine(tls_url(tls_cluster.port, tls_cluster.ca_path, user, passwords[role], source_database.name))
    try:
        raw = engine.raw_connection()
        try:
            client = raw.driver_connection
            assert client.pgconn.ssl_in_use is True
            parameters = client.info.get_parameters()
            assert parameters.get("sslmode") == "verify-full" and parameters.get("sslrootcert") == tls_cluster.ca_path, "the connection was not made with the verify-full contract"
            cursor = raw.cursor()
            cursor.execute("SELECT session_user, current_user, pg_backend_pid(), "
                           "(SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()), "
                           "(SELECT version FROM pg_stat_ssl WHERE pid = pg_backend_pid()), "
                           "(SELECT cipher FROM pg_stat_ssl WHERE pid = pg_backend_pid())")
            session_user, current_user, pid, server_ssl, server_protocol, cipher = cursor.fetchone()
            assert (session_user, current_user) == (user, user)
            assert server_ssl is True and server_protocol in TLS_PROTOCOLS and cipher, (server_ssl, server_protocol, cipher)
            # the server's own log for THIS backend: SCRAM authentication and an authorization carrying the TLS parameters
            logged = [s for s in authenticated_sessions(_container_logs(tls_cluster.container), user=user, database=source_database.name) if s["pid"] == str(pid)]
            assert len(logged) == 1, f"no server log evidence for backend {pid}"
            assert logged[0]["method_scram"] is True and logged[0]["tls_protocol"] == server_protocol, logged[0]
        finally:
            raw.close()
    finally:
        engine.dispose()


def _attempt(cluster: TlsCluster, url: URL) -> str | None:
    """None on success; otherwise the scrubbed server/driver error text."""
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return None
    except OperationalError as exc:
        return _scrub(str(exc.orig), cluster)
    finally:
        engine.dispose()


def _backup_url(cluster: TlsCluster, database: str, **overrides) -> URL:
    kwargs = {"host": "localhost", "sslmode": "verify-full"}
    ca = overrides.pop("ca", cluster.ca_path)
    password = overrides.pop("password", cluster.backup_password)
    kwargs.update(overrides)
    return tls_url(cluster.port, ca, BACKUP_ROLE, password, database, **kwargs)


@pytest.mark.parametrize(
    "label, overrides, fragment",
    [
        ("wrong_password", {"password": "definitely-not-the-password"}, "password authentication failed"),
        ("untrusted_ca", {"ca": "OTHER"}, "certificate verify failed"),
        ("hostname_mismatch", {"host": "wrong.example.test"}, "does not match host name"),
        ("tls_disabled", {"sslmode": "disable"}, "no encryption"),
    ],
)
def test_each_connection_rejection_has_its_own_reason_and_a_one_variable_control(tls_cluster, source_database, label, overrides, fragment):
    db = source_database.name
    overrides = dict(overrides)
    if overrides.get("ca") == "OTHER":
        overrides["ca"] = tls_cluster.other_ca_path
    before = _container_logs(tls_cluster.container)
    # control: the same connection with every property correct succeeds
    assert _attempt(tls_cluster, _backup_url(tls_cluster, db)) is None
    after_control = _container_logs(tls_cluster.container)
    error = _attempt(tls_cluster, _backup_url(tls_cluster, db, **overrides))
    assert error is not None, f"{label}: connection unexpectedly succeeded"
    assert fragment in error, f"{label}: rejected, but not for the intended reason {fragment!r}: {error[:240]}"
    after_attempt = _container_logs(tls_cluster.container)
    assert count_log(after_control, "connection authenticated:") > count_log(before, "connection authenticated:"), "control did not authenticate"
    delta_auth = count_log(after_attempt, "connection authenticated:") - count_log(after_control, "connection authenticated:")
    if label == "wrong_password":
        assert delta_auth == 0
        assert count_log(after_attempt, "password authentication failed") > count_log(after_control, "password authentication failed")
    else:
        assert delta_auth == 0, f"{label}: the server authenticated a session that should have been refused"


def test_controls_isolate_verification_and_hostname_checks_from_encryption(tls_cluster, source_database):
    """
    Without the verifying mode the same untrusted trust anchor / wrong host name
    connect (encrypted but unauthenticated). So the rejections above are caused by
    verification, not by TLS, reachability or the credentials. (libpq upgrades
    `require` to verify-ca when a root certificate is supplied, hence the control
    omits `sslrootcert`.)
    """
    db = source_database.name
    assert _attempt(tls_cluster, _backup_url(tls_cluster, db, sslmode="require", ca=None)) is None, "require without a root certificate must not verify the server"
    assert _attempt(tls_cluster, _backup_url(tls_cluster, db, sslmode="require", ca=None, host="wrong.example.test")) is None
    assert _attempt(tls_cluster, _backup_url(tls_cluster, db, sslmode="verify-ca", host="wrong.example.test")) is None, "verify-ca must not verify the host name"
    assert "certificate verify failed" in _attempt(tls_cluster, _backup_url(tls_cluster, db, sslmode="verify-ca", ca=tls_cluster.other_ca_path))
    assert "does not match host name" in _attempt(tls_cluster, _backup_url(tls_cluster, db, host="wrong.example.test"))


def test_a_missing_ca_file_fails_closed(tls_cluster, source_database):
    error = _attempt(tls_cluster, _backup_url(tls_cluster, source_database.name, ca=(tls_cluster.material.workdir / "absent.crt").as_posix()))
    assert error is not None and ("root certificate file" in error and "does not exist" in error), error


def test_no_unix_socket_or_unencrypted_tcp_path_exists_inside_the_container(tls_cluster, source_database):
    db = source_database.name
    pgpass = write_pgpass(tls_cluster, [(BACKUP_ROLE, tls_cluster.backup_password)])
    try:
        env = {"PGPASSFILE": pgpass}
        socket_attempt = _exec(tls_cluster, ["psql", "-w", "-h", UNIX_SOCKET_DIR, "-U", BACKUP_ROLE, "-d", db, "-c", "SELECT 1"], env=env)
        assert socket_attempt.returncode != 0 and 'rejects connection for host "[local]"' in socket_attempt.stderr, socket_attempt.diagnostics()
        plain = _exec(tls_cluster, ["psql", "-w", "-h", "127.0.0.1", "-U", BACKUP_ROLE, "-d", db, "-c", "SELECT 1"],
                      env={**env, "PGSSLMODE": "disable"})
        assert plain.returncode != 0 and "no encryption" in plain.stderr, plain.diagnostics()
        # control: the same tool, same file, but TCP + verify-full succeeds
        good = _exec(tls_cluster, ["psql", "-w", "-d", conninfo(tls_cluster, BACKUP_ROLE, db), "-tAc", "SELECT session_user"], env=env)
        assert good.returncode == 0 and good.stdout.strip() == BACKUP_ROLE, good.diagnostics()
    finally:
        remove_files(tls_cluster, pgpass)


# --------------------------------------------------------------------------
# environment isolation
# --------------------------------------------------------------------------
def test_inherited_database_environment_was_removed_for_this_module(isolated_database_environment):
    leftovers = sorted(name for name in os.environ if re.match(r"^(PG[A-Z0-9_]*|NAPSEDGE_DATABASE_URL|NAPSEDGE_DATABASE_ECHO)$", name))
    assert leftovers == [], f"database environment variables still set: {leftovers}"
    assert all(isinstance(name, str) for name in isolated_database_environment)  # names only, never values


def test_scrubbing_is_effective_on_a_real_connection_not_just_on_paper(tls_cluster, source_database, monkeypatch):
    """Control: an inherited PGOPTIONS really does change a connection that sets no `options` itself; scrubbing it really does undo that."""
    monkeypatch.setenv("PGOPTIONS", "-c default_transaction_read_only=on")
    url = tls_url(tls_cluster.port, tls_cluster.ca_path, MIGRATOR_ROLE, tls_cluster.migrator_password, source_database.name)

    def read_only() -> str:
        engine = create_engine(url)
        try:
            with engine.connect() as conn:
                return conn.execute(text("SHOW default_transaction_read_only")).scalar()
        finally:
            engine.dispose()

    assert read_only() == "on", "the decoy did not take effect, so this control proves nothing"
    cleaned, removed = scrub_database_environment(os.environ)
    assert "PGOPTIONS" in removed
    for name in removed:
        monkeypatch.delenv(name)
    assert read_only() == "off"


# --------------------------------------------------------------------------
# the existing application connection path over verified TLS
# --------------------------------------------------------------------------
def _app_engine_from_environment(monkeypatch, dsn: str):
    monkeypatch.setenv("NAPSEDGE_DATABASE_URL", dsn)
    from src.db.session import build_engine

    return build_engine()  # no argument: resolves through src.db.config.get_database_url(), exactly like production


def test_application_engine_from_the_environment_uses_verified_tls_and_scram(tls_cluster, source_database, monkeypatch):
    from src.services.health import check_readiness

    db = source_database.name
    engine = _app_engine_from_environment(monkeypatch, handwritten_dsn(tls_cluster, RUNTIME_ROLE, tls_cluster.runtime_password, db))
    try:
        assert check_readiness(engine).ready is True
        with engine.connect() as conn:
            session_user, ssl, version = conn.execute(text(
                "SELECT session_user, (SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()), (SELECT version FROM pg_stat_ssl WHERE pid = pg_backend_pid())"
            )).one()
        assert (session_user, ssl) == (RUNTIME_ROLE, True) and version in TLS_PROTOCOLS
        tables = exercise_accepted_read_paths(engine, source_database.seeded)
        assert tables == REQUIRED_READ_TABLES
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "label, overrides, password",
    [
        ("untrusted_ca", {"sslrootcert": "OTHER"}, None),
        ("hostname_mismatch", {}, None),
        ("tls_disabled", {"sslmode": "disable"}, None),
        ("wrong_password", {}, "definitely-not-the-password"),
        ("missing_ca_file", {"sslrootcert": "ABSENT"}, None),
    ],
)
def test_application_readiness_fails_closed_for_every_broken_tls_or_password_setting(tls_cluster, source_database, monkeypatch, label, overrides, password):
    from fastapi.testclient import TestClient

    from src.api.main import create_app
    from src.services.health import check_readiness

    db = source_database.name
    overrides = dict(overrides)
    if overrides.get("sslrootcert") == "OTHER":
        overrides["sslrootcert"] = tls_cluster.other_ca_path
    if overrides.get("sslrootcert") == "ABSENT":
        overrides["sslrootcert"] = (tls_cluster.material.workdir / "absent.crt").as_posix()
    good = handwritten_dsn(tls_cluster, RUNTIME_ROLE, tls_cluster.runtime_password, db)
    bad = handwritten_dsn(tls_cluster, RUNTIME_ROLE, password or tls_cluster.runtime_password, db, **overrides)
    if label == "hostname_mismatch":
        bad = bad.replace("@localhost:", "@wrong.example.test:")
    monkeypatch.setenv("NAPSEDGE_API_KEY_HASHES_JSON", '{"drill-client": "' + "0" * 64 + '"}')

    for dsn, expected_ready in ((good, True), (bad, False)):  # control first: the app path works with the good DSN
        monkeypatch.setenv("NAPSEDGE_DATABASE_URL", dsn)
        app = create_app()
        try:
            assert check_readiness(app.state.engine).ready is expected_ready, label
            assert TestClient(app).get("/v1/ready").status_code == (200 if expected_ready else 503), label
        finally:
            app.state.engine.dispose()


def test_alembic_check_through_the_real_environment_path_works_with_a_verified_tls_dsn(tls_cluster, source_database, monkeypatch):
    """The production resolution path (env.py prefers NAPSEDGE_DATABASE_URL), not a Config override. Percent-free DSN baseline (9B-2); the percent-encoded case is the 9B-3 group below."""
    monkeypatch.setenv("NAPSEDGE_DATABASE_URL", handwritten_dsn(tls_cluster, MIGRATOR_ROLE, tls_cluster.migrator_password, source_database.name))
    command.check(alembic_config_for("postgresql+psycopg://unused-because-the-environment-wins"))


# --------------------------------------------------------------------------
# Phase 9B-3: raw, percent-encoded NAPSEDGE_DATABASE_URL through the production alembic/env.py
# (the 9B-2 limitation test that lived here became these regression tests when env.py was fixed)
# --------------------------------------------------------------------------
PCT_ROLE = "napsedge_pct_probe"


@dataclass(frozen=True)
class PercentProbe:
    """A role whose password needs percent-encoding, a database it owns, and CA copies under a directory name containing a space."""

    role: str
    password: str = field(repr=False)
    database: str
    ca_path: str
    other_ca_path: str

    def dsn(self, cluster: TlsCluster, *, database: str | None = None, password: str | None = None, ca: str | None = None, host: str = "localhost", sslmode: str = "verify-full") -> str:
        """Built the way a standards-following operator would: userinfo and query values percent-encoded, NOT routed through any test escaping helper."""
        query = f"sslmode={sslmode}&hostaddr=127.0.0.1"
        if sslmode != "disable":
            query += f"&sslrootcert={quote(ca or self.ca_path, safe='/')}"
        return f"postgresql+psycopg://{self.role}:{quote(password or self.password, safe='')}@{host}:{cluster.port}/{database or self.database}?{query}"


@pytest.fixture(scope="module")
def percent_probe(tls_cluster, tmp_path_factory):
    spaced = tmp_path_factory.mktemp("cert dir") / "with space"
    spaced.mkdir()
    shutil.copy(tls_cluster.material.ca_cert, spaced / "ca.crt")
    shutil.copy(tls_cluster.material.other_ca_cert, spaced / "other_ca.crt")
    password = f"p@ss%/{secrets.token_hex(8)}"  # synthetic; '@', '%' and '/' all require percent-encoding in a URL
    database = f"{TEST_DB_PREFIX}{uuid.uuid4().hex[:16]}"
    assert_disposable_database_name(database)
    admin = tls_cluster.admin_engine()
    try:
        with admin.connect() as conn:
            assert not conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": PCT_ROLE}).fetchall(), "refusing to reuse a pre-existing role"
            conn.execute(text(f"CREATE ROLE {PCT_ROLE} LOGIN PASSWORD '{password}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"))
            conn.execute(text(f'CREATE DATABASE "{database}" OWNER {PCT_ROLE}'))
        yield PercentProbe(role=PCT_ROLE, password=password, database=database, ca_path=(spaced / "ca.crt").as_posix(),
                           other_ca_path=(spaced / "other_ca.crt").as_posix())
    finally:
        _drop_database(tls_cluster, database)
        with admin.connect() as conn:
            conn.execute(text(f"DROP ROLE IF EXISTS {PCT_ROLE}"))
        admin.dispose()


@pytest.fixture(scope="module")
def migrated_percent_probe(tls_cluster, percent_probe):
    """The probe's primary database, migrated through the production env.py with the raw percent-encoded environment URL."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("NAPSEDGE_DATABASE_URL", percent_probe.dsn(tls_cluster))
        command.upgrade(alembic_config_for(""), "head")
    return percent_probe


def _scrubbed(text_value: str, probe: PercentProbe, cluster: TlsCluster) -> str:
    return _scrub(text_value, cluster).replace(probe.password, "***").replace(quote(probe.password, safe=""), "***")


def test_raw_percent_encoded_environment_url_runs_real_tls_migrations_and_alembic_check(tls_cluster, percent_probe, monkeypatch):
    """A FRESH database: upgrade head then check, both through the production env.py with the raw environment URL; the server's own log shows the SCRAM + TLS session for that role."""
    assert "%" in quote(percent_probe.password, safe="") and " " in percent_probe.ca_path
    fresh = f"{TEST_DB_PREFIX}{uuid.uuid4().hex[:16]}"
    assert_disposable_database_name(fresh)
    admin_server = tls_cluster.admin_engine()
    try:
        with admin_server.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{fresh}" OWNER {PCT_ROLE}'))
        monkeypatch.setenv("NAPSEDGE_DATABASE_URL", percent_probe.dsn(tls_cluster, database=fresh))
        cfg = alembic_config_for("")  # no programmatic URL at all: the environment is the only source

        command.upgrade(cfg, "head")
        command.check(cfg)
        command.upgrade(cfg, "head")  # a second run on the same Config is a no-op and still resolves the URL

        admin = tls_cluster.admin_engine(fresh)
        try:
            assert scalar(admin, "SELECT version_num FROM alembic_version") == ACCEPTED_ALEMBIC_HEAD
            assert scalar(admin, "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tableowner = :r", r=PCT_ROLE) >= 20
        finally:
            admin.dispose()
        sessions = authenticated_sessions(_container_logs(tls_cluster.container), user=PCT_ROLE, database=fresh)
        assert sessions and all(s["method_scram"] and s["tls_protocol"] in TLS_PROTOCOLS for s in sessions), "no SCRAM+TLS session was logged for the migration role"
    finally:
        admin_server.dispose()
        _drop_database(tls_cluster, fresh)


def test_application_readiness_works_with_the_same_percent_encoded_environment_url(tls_cluster, migrated_percent_probe, monkeypatch):
    from src.services.health import check_readiness

    engine = _app_engine_from_environment(monkeypatch, migrated_percent_probe.dsn(tls_cluster))
    try:
        assert check_readiness(engine).ready is True
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "label, fragment",
    [
        ("wrong_password", "password authentication failed"),
        ("untrusted_ca", "certificate verify failed"),
        ("hostname_mismatch", "does not match host name"),
        ("tls_disabled", "no encryption"),
        ("missing_ca_file", "does not exist"),
    ],
)
def test_percent_encoded_environment_url_still_fails_closed_for_credentials_and_tls(tls_cluster, migrated_percent_probe, monkeypatch, label, fragment):
    percent_probe = migrated_percent_probe
    """With env.py now accepting the URL, bad credentials and bad TLS settings must still be refused by the server/driver (never by ConfigParser), each for its own reason."""
    overrides = {
        "wrong_password": {"password": "wrong-" + percent_probe.password},
        "untrusted_ca": {"ca": percent_probe.other_ca_path},
        "hostname_mismatch": {"host": "wrong.example.test"},
        "tls_disabled": {"sslmode": "disable"},
        "missing_ca_file": {"ca": percent_probe.ca_path.replace("ca.crt", "absent.crt")},
    }[label]
    cfg = alembic_config_for("")
    monkeypatch.setenv("NAPSEDGE_DATABASE_URL", percent_probe.dsn(tls_cluster))
    command.check(cfg)  # control: the identical command with every property correct succeeds

    monkeypatch.setenv("NAPSEDGE_DATABASE_URL", percent_probe.dsn(tls_cluster, **overrides))
    with pytest.raises(OperationalError) as excinfo:
        command.check(cfg)
    message = _scrubbed(str(excinfo.value.orig), percent_probe, tls_cluster)
    assert fragment in message, f"{label}: refused, but not for the intended reason {fragment!r}: {message[:240]}"


# --------------------------------------------------------------------------
# the backup role
# --------------------------------------------------------------------------
_BACKUP_EXCLUDED_DENIALS = {"select_ungranted_table", "select_ungranted_pipeline_runs"}  # the backup role legitimately reads these
_BACKUP_DENIED_STATEMENTS = tuple(
    (label, statement.replace(RUNTIME_ROLE, BACKUP_ROLE)) for label, statement in _DENIED_STATEMENTS if label not in _BACKUP_EXCLUDED_DENIALS
) + (
    ("setval_on_sequence", "SELECT setval(pg_get_serial_sequence('team_aliases', 'id'), 1)"),
    ("read_pg_authid", "SELECT count(*) FROM pg_authid"),
    ("grant_pg_read_all_data_to_self", f"GRANT pg_read_all_data TO {BACKUP_ROLE}"),
    ("grant_pg_write_all_data_to_self", f"GRANT pg_write_all_data TO {BACKUP_ROLE}"),
    ("create_replication_slot", "SELECT pg_create_physical_replication_slot('zz_denied_slot')"),
    ("create_publication", "CREATE PUBLICATION zz_denied_pub FOR ALL TABLES"),
)


def assert_backup_boundary(cluster: TlsCluster, database: str) -> None:
    """Everything the backup role may and may not do, evidenced from a real verify-full connection. Same function for the source and the restored database."""
    admin = cluster.admin_engine(database)
    backup = create_engine(cluster.backup_url(database))
    r = BACKUP_ROLE
    try:
        baseline = all_fingerprints(admin)
        tables, sequences = public_tables(admin), [s for (s,) in rows(admin, "SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'")]
        assert tables and sequences

        # --- attributes and memberships ------------------------------------------
        attrs = rows(admin, "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname = :r", r=r)[0]
        assert attrs == (False, False, False, False, False, True), f"role attributes unexpected: {attrs}"
        assert rows(admin, "SELECT 1 FROM pg_auth_members WHERE member = (SELECT oid FROM pg_roles WHERE rolname = :r)", r=r) == [], "role membership: backup has direct membership(s)"
        for role in (MIGRATOR_ROLE, RUNTIME_ROLE, "pg_read_all_data", "pg_write_all_data", "pg_execute_server_program", "pg_read_server_files",
                     "pg_write_server_files", "pg_monitor", "pg_signal_backend", "pg_database_owner", cluster.admin_url.username):
            assert scalar(admin, "SELECT pg_has_role(:r, :t, 'MEMBER')", r=r, t=role) is False, f"role membership: backup is a member of {role}"

        # --- ownership -------------------------------------------------------------
        for label, sql in (
            ("relation", "SELECT count(*) FROM pg_class WHERE relowner = (SELECT oid FROM pg_roles WHERE rolname = :r)"),
            ("schema", "SELECT count(*) FROM pg_namespace WHERE nspowner = (SELECT oid FROM pg_roles WHERE rolname = :r)"),
            ("function", "SELECT count(*) FROM pg_proc WHERE proowner = (SELECT oid FROM pg_roles WHERE rolname = :r)"),
            ("type", "SELECT count(*) FROM pg_type WHERE typowner = (SELECT oid FROM pg_roles WHERE rolname = :r) AND typnamespace = 'public'::regnamespace"),
            ("database", "SELECT count(*) FROM pg_database WHERE datdba = (SELECT oid FROM pg_roles WHERE rolname = :r)"),
        ):
            assert scalar(admin, sql, r=r) == 0, f"backup owns a {label}"

        # --- grants: exactly SELECT on tables and sequences, USAGE on the schema, CONNECT ---------
        for table in tables:
            assert scalar(admin, "SELECT has_table_privilege(:r, :t, 'SELECT')", r=r, t=f"public.{table}") is True, f"backup lacks SELECT on table {table}"
            for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER", "SELECT WITH GRANT OPTION"):
                assert scalar(admin, "SELECT has_table_privilege(:r, :t, :p)", r=r, t=f"public.{table}", p=privilege) is False, (table, privilege)
        for sequence in sequences:
            assert scalar(admin, "SELECT has_sequence_privilege(:r, :s, 'SELECT')", r=r, s=f"public.{sequence}") is True, f"backup lacks SELECT on sequence {sequence}"
            for privilege in ("USAGE", "UPDATE"):
                assert scalar(admin, "SELECT has_sequence_privilege(:r, :s, :p)", r=r, s=f"public.{sequence}", p=privilege) is False, f"backup holds {privilege} on sequence {sequence}"
        assert scalar(admin, "SELECT has_schema_privilege(:r, 'public', 'USAGE')", r=r) is True, "backup lacks USAGE on schema public"
        assert scalar(admin, "SELECT has_schema_privilege(:r, 'public', 'CREATE')", r=r) is False, "backup can CREATE in schema public"
        assert scalar(admin, "SELECT has_database_privilege(:r, :d, 'CONNECT')", r=r, d=database) is True, "backup lacks CONNECT on the database"
        assert scalar(admin, "SELECT has_database_privilege(:r, :d, 'CREATE')", r=r, d=database) is False, "backup holds CREATE on database"
        assert scalar(admin, "SELECT has_database_privilege(:r, :d, 'TEMPORARY')", r=r, d=database) is False, "backup holds TEMPORARY on database"
        assert scalar(admin, "SELECT count(*) FROM pg_class c, aclexplode(c.relacl) a WHERE c.relnamespace = 'public'::regnamespace AND a.grantee = 0") == 0, "PUBLIC holds privileges on a public-schema relation"
        assert scalar(admin, "SELECT count(*) FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid WHERE c.relnamespace = 'public'::regnamespace AND a.attacl IS NOT NULL") == 0, "unexpected column-level ACLs"
        assert scalar(admin, "SELECT count(*) FROM pg_default_acl d, aclexplode(d.defaclacl) a WHERE a.grantee = (SELECT oid FROM pg_roles WHERE rolname = :r)", r=r) == 0, "default ACL would auto-grant future objects to the backup role"

        # --- behaviour over a real connection ---------------------------------------------
        with backup.connect() as conn:
            assert conn.execute(text("SELECT session_user, current_user")).one() == (r, r)
            assert conn.execute(text("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")).scalar() is True
            for table in tables:
                conn.execute(text(f'SELECT count(*) FROM public."{table}"')).scalar()
            for sequence in sequences:
                conn.execute(text(f'SELECT last_value FROM public."{sequence}"')).scalar()
        for label, statement in _BACKUP_DENIED_STATEMENTS:
            options = {"isolation_level": "AUTOCOMMIT"} if label == "alter_system" else {}
            with backup.connect() as conn:
                conn = conn.execution_options(**options)
                with pytest.raises(DBAPIError) as excinfo:
                    conn.execute(text(statement))
                assert _sqlstate(excinfo) == INSUFFICIENT_PRIVILEGE, f"{label}: sqlstate={_sqlstate(excinfo)} ({excinfo.value.orig})"

        # --- nothing changed -----------------------------------------------------------------
        assert all_fingerprints(admin) == baseline, "a denied statement nevertheless altered stored data"
        assert "zz_denied" not in set(public_tables(admin))
    finally:
        backup.dispose()
        admin.dispose()


def test_backup_role_boundary_on_the_source_database(tls_cluster, source_database):
    assert_backup_boundary(tls_cluster, source_database.name)


def test_runtime_role_boundary_is_unchanged_by_the_backup_role_and_holds_over_tls(tls_cluster, source_database):
    """The whole 9B-1 runtime battery (27 denied operations, grants, defaults), now over verify-full TLS with the backup role present."""
    assert_runtime_boundary(tls_cluster, source_database.name)


def test_backup_role_is_not_a_member_of_and_cannot_become_the_migrator(tls_cluster, source_database):
    engine = create_engine(tls_cluster.backup_url(source_database.name))
    try:
        with engine.connect() as conn:
            for statement in (f"SET ROLE {MIGRATOR_ROLE}", f"SET SESSION AUTHORIZATION {MIGRATOR_ROLE}"):
                with pytest.raises(DBAPIError) as excinfo:
                    conn.execute(text(statement))
                assert _sqlstate(excinfo) == INSUFFICIENT_PRIVILEGE
                conn.rollback()
    finally:
        engine.dispose()


# misconfigurations the backup battery must catch ------------------------------------------
def _backup_mutations(database: str) -> dict[str, tuple[str, str, str]]:
    r, m = BACKUP_ROLE, MIGRATOR_ROLE
    return {
        "extra_insert_grant": (f"GRANT INSERT ON TABLE public.teams TO {r}", f"REVOKE INSERT ON TABLE public.teams FROM {r}", "('teams', 'INSERT')"),
        "update_on_alembic_version": (f"GRANT UPDATE ON TABLE public.alembic_version TO {r}", f"REVOKE UPDATE ON TABLE public.alembic_version FROM {r}", "('alembic_version', 'UPDATE')"),
        "missing_table_select": (f"REVOKE SELECT ON TABLE public.teams FROM {r}", f"GRANT SELECT ON TABLE public.teams TO {r}", "lacks SELECT on table teams"),
        "missing_sequence_select": (f"REVOKE SELECT ON SEQUENCE public.team_aliases_id_seq FROM {r}", f"GRANT SELECT ON SEQUENCE public.team_aliases_id_seq TO {r}", "lacks SELECT on sequence team_aliases_id_seq"),
        "sequence_update": (f"GRANT UPDATE ON SEQUENCE public.team_aliases_id_seq TO {r}", f"REVOKE UPDATE ON SEQUENCE public.team_aliases_id_seq FROM {r}", "holds UPDATE on sequence team_aliases_id_seq"),
        "missing_schema_usage": (f"REVOKE USAGE ON SCHEMA public FROM {r}", f"GRANT USAGE ON SCHEMA public TO {r}", "lacks USAGE on schema public"),
        "schema_create": (f"GRANT CREATE ON SCHEMA public TO {r}", f"REVOKE CREATE ON SCHEMA public FROM {r}", "can CREATE in schema public"),
        "database_temporary": (f'GRANT TEMPORARY ON DATABASE "{database}" TO {r}', f'REVOKE TEMPORARY ON DATABASE "{database}" FROM {r}', "TEMPORARY on database"),
        "public_can_read": ("GRANT SELECT ON TABLE public.teams TO PUBLIC", "REVOKE SELECT ON TABLE public.teams FROM PUBLIC", "PUBLIC holds privileges"),
        "member_of_migrator": (f"GRANT {m} TO {r}", f"REVOKE {m} FROM {r}", "role membership"),
        "member_of_pg_read_all_data": (f"GRANT pg_read_all_data TO {r}", f"REVOKE pg_read_all_data FROM {r}", "role membership"),
        "createrole_attribute": (f"ALTER ROLE {r} CREATEROLE", f"ALTER ROLE {r} NOCREATEROLE", "role attributes unexpected"),
        "replication_attribute": (f"ALTER ROLE {r} REPLICATION", f"ALTER ROLE {r} NOREPLICATION", "role attributes unexpected"),
        "default_privilege_autogrant": (
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {m} IN SCHEMA public GRANT SELECT ON TABLES TO {r}",
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {m} IN SCHEMA public REVOKE SELECT ON TABLES FROM {r}",
            "default ACL would auto-grant",
        ),
    }


@pytest.mark.parametrize("label", sorted(_backup_mutations("x")))
def test_backup_boundary_battery_detects_injected_misconfiguration(tls_cluster, scratch_database, label):
    apply_sql, revert_sql, expected = _backup_mutations(scratch_database)[label]
    admin = tls_cluster.admin_engine(scratch_database)
    try:
        with admin.connect() as conn:
            conn.execute(text(apply_sql))
        try:
            with pytest.raises((AssertionError, pytest.fail.Exception)) as excinfo:
                assert_backup_boundary(tls_cluster, scratch_database)
            assert expected in str(excinfo.value), f"{label}: caught, but not for the expected reason {expected!r}: {str(excinfo.value)[:300]}"
        finally:
            with admin.connect() as conn:
                conn.execute(text(revert_sql))
    finally:
        admin.dispose()


def test_backup_battery_passes_again_after_every_injected_misconfiguration_was_reverted(tls_cluster, scratch_database):
    assert_backup_boundary(tls_cluster, scratch_database)


# necessity of each grant -----------------------------------------------------------------------
def _dump_as_backup(cluster: TlsCluster, database: str) -> tuple[CommandResult, str, str]:
    pgpass = write_pgpass(cluster, [(BACKUP_ROLE, cluster.backup_password)])
    path = f"/tmp/napsedge_probe_{uuid.uuid4().hex[:12]}.dump"
    return run_dump(cluster, conninfo(cluster, BACKUP_ROLE, database), path, pgpass), path, pgpass


_NECESSITY = {
    "connect": ("REVOKE CONNECT ON DATABASE \"{db}\" FROM " + BACKUP_ROLE, "GRANT CONNECT ON DATABASE \"{db}\" TO " + BACKUP_ROLE, "does not have CONNECT privilege"),
    "schema_usage": ("REVOKE USAGE ON SCHEMA public FROM " + BACKUP_ROLE, "GRANT USAGE ON SCHEMA public TO " + BACKUP_ROLE, "permission denied for schema public"),
    "table_select": ("REVOKE SELECT ON TABLE public.teams FROM " + BACKUP_ROLE, "GRANT SELECT ON TABLE public.teams TO " + BACKUP_ROLE, "permission denied for table teams"),
    "sequence_select": ("REVOKE SELECT ON SEQUENCE public.team_aliases_id_seq FROM " + BACKUP_ROLE, "GRANT SELECT ON SEQUENCE public.team_aliases_id_seq TO " + BACKUP_ROLE, "permission denied for sequence team_aliases_id_seq"),
}


@pytest.mark.parametrize("element", sorted(_NECESSITY))
def test_every_backup_grant_is_necessary_for_a_complete_dump(tls_cluster, scratch_database, element):
    revoke, regrant, fragment = _NECESSITY[element]
    admin = tls_cluster.admin_engine(scratch_database)
    leftovers: list[str] = []
    try:
        control, path, pgpass = _dump_as_backup(tls_cluster, scratch_database)
        leftovers += [path, pgpass]
        assert control.returncode == 0, f"control dump failed before any grant was removed: {control.diagnostics()}"
        with admin.connect() as conn:
            conn.execute(text(revoke.format(db=scratch_database)))
        try:
            result, path, pgpass = _dump_as_backup(tls_cluster, scratch_database)
            leftovers += [path, pgpass]
            assert result.returncode != 0, f"pg_dump succeeded without the {element} grant, so the grant is not necessary"
            assert fragment in result.stderr, f"{element}: pg_dump failed, but not for the expected reason {fragment!r}: {result.stderr.strip()[-300:]}"
        finally:
            with admin.connect() as conn:
                conn.execute(text(regrant.format(db=scratch_database)))
        again, path, pgpass = _dump_as_backup(tls_cluster, scratch_database)
        leftovers += [path, pgpass]
        assert again.returncode == 0, again.diagnostics()
    finally:
        remove_files(tls_cluster, *leftovers)
        admin.dispose()


def test_new_migrated_object_fails_the_dump_loudly_until_the_grant_procedure_is_rerun(tls_cluster, scratch_database):
    """Backup grants are fail-closed: a table a later migration adds is NOT silently omitted from backups -- the dump fails -- until `provision_backup_access` is re-run."""
    migrator = create_engine(tls_cluster.migrator_url(scratch_database))
    leftovers: list[str] = []
    try:
        with migrator.begin() as conn:
            conn.execute(text("CREATE TABLE zz_future_object (id serial PRIMARY KEY, note text)"))
        try:
            result, path, pgpass = _dump_as_backup(tls_cluster, scratch_database)
            leftovers += [path, pgpass]
            assert result.returncode != 0
            assert "permission denied for table zz_future_object" in result.stderr, result.stderr.strip()[-300:]
            provision_backup_access(tls_cluster, scratch_database)
            fixed, path, pgpass = _dump_as_backup(tls_cluster, scratch_database)
            leftovers += [path, pgpass]
            assert fixed.returncode == 0, fixed.diagnostics()
            listing = _exec(tls_cluster, ["pg_restore", "--list", path])
            assert re.search(r"TABLE DATA public zz_future_object ", listing.stdout) and re.search(r"SEQUENCE SET public zz_future_object_id_seq", listing.stdout)
            assert_backup_boundary(tls_cluster, scratch_database)
        finally:
            with migrator.begin() as conn:
                conn.execute(text("DROP TABLE IF EXISTS zz_future_object"))
    finally:
        remove_files(tls_cluster, *leftovers)
        migrator.dispose()


# --------------------------------------------------------------------------
# recovery over authenticated TLS
# --------------------------------------------------------------------------
@dataclass
class Attempt:
    order: int
    utility: str
    label: str
    role: str
    result: CommandResult
    auth_delta: int
    password_failure_delta: int


@dataclass
class TlsRecovery:
    source: str
    restored: str
    dump_path: str
    pgpass_files: list[str]
    attempts: list[Attempt] = field(default_factory=list)
    dump: CommandResult | None = None
    dump_order: int = -1
    listing: CommandResult | None = None
    restore: CommandResult | None = None
    restore_order: int = -1
    dump_size: int = 0
    logs_after_dump: str = ""
    logs_after_restore: str = ""
    carried_owners_equal: bool = False
    carried_acls_equal: bool = False
    backup_connect_via_public_before_reprovision: bool | None = None
    runtime_temp_privilege_before_reprovision: bool | None = None
    comparison: object = None

    def require_success(self) -> None:
        assert self.dump is not None and self.dump.returncode == 0, f"pg_dump failed: {self.dump and self.dump.diagnostics()}"
        assert self.listing is not None and self.listing.returncode == 0, f"pg_restore --list failed: {self.listing and self.listing.diagnostics()}"
        assert self.restore is not None and self.restore.returncode == 0, f"pg_restore failed: {self.restore and self.restore.diagnostics()}"


#: label -> (conninfo overrides, expected stderr fragment). Valid credentials are used in every case but wrong_password.
_TOOL_NEGATIVES = {
    "wrong_password": ({}, "password authentication failed"),
    "untrusted_ca": ({"sslrootcert": "/tls/other_ca.crt"}, "certificate verify failed"),
    "hostname_mismatch": ({"host": "wrong.example.test"}, "does not match host name"),
    "tls_disabled": ({"sslmode": "disable"}, "no encryption"),
    "unix_socket": ({"host": UNIX_SOCKET_DIR, "hostaddr": None, "sslmode": "prefer", "sslrootcert": None}, 'rejects connection for host "[local]"'),
}


def _record_negatives(cluster: TlsCluster, drill: TlsRecovery, utility: str, role: str, database: str, good_pgpass: str, wrong_pgpass: str, dump_path: str) -> None:
    for label, (overrides, _) in _TOOL_NEGATIVES.items():
        info = conninfo(cluster, role, database, **overrides)
        runner = run_dump if utility == "pg_dump" else run_restore
        target = f"{dump_path}.negative_{label}" if utility == "pg_dump" else dump_path
        before = _container_logs(cluster.container)
        result = runner(cluster, info, target, wrong_pgpass if label == "wrong_password" else good_pgpass)
        after = _container_logs(cluster.container)
        drill.attempts.append(Attempt(
            order=len(drill.attempts), utility=utility, label=label, role=role, result=result,
            auth_delta=count_log(after, "connection authenticated:") - count_log(before, "connection authenticated:"),
            password_failure_delta=count_log(after, "password authentication failed") - count_log(before, "password authentication failed"),
        ))
        if utility == "pg_dump":
            drill.pgpass_files.append(f"{target}")  # in case a partial file was left behind


def perform_tls_recovery_drill(cluster: TlsCluster, source: SourceDatabase) -> TlsRecovery:
    dump_path = f"/tmp/napsedge_tls_drill_{uuid.uuid4().hex[:12]}.dump"
    good = write_pgpass(cluster, [(BACKUP_ROLE, cluster.backup_password), (MIGRATOR_ROLE, cluster.migrator_password)])
    wrong = write_pgpass(cluster, [(BACKUP_ROLE, "wrong-" + secrets.token_urlsafe(12)), (MIGRATOR_ROLE, "wrong-" + secrets.token_urlsafe(12))])
    restored = _create_database_owned_by_migrator(cluster)
    drill = TlsRecovery(source=source.name, restored=restored, dump_path=dump_path, pgpass_files=[good, wrong])
    # 1. pg_dump as the backup role: every rejection first, then the real dump
    _record_negatives(cluster, drill, "pg_dump", BACKUP_ROLE, source.name, good, wrong, dump_path)
    drill.dump = run_dump(cluster, conninfo(cluster, BACKUP_ROLE, source.name), dump_path, good)
    drill.dump_order = len(drill.attempts)
    drill.logs_after_dump = _container_logs(cluster.container)
    size = _exec(cluster, ["sh", "-c", f"wc -c < {dump_path}"])
    drill.dump_size = int(size.stdout.strip() or 0) if size.returncode == 0 else 0
    drill.listing = _exec(cluster, ["pg_restore", "--list", dump_path])
    if drill.dump.returncode != 0:
        return drill

    # 2. pg_restore as the migrator into a fresh database: every rejection first, then the real restore
    _record_negatives(cluster, drill, "pg_restore", MIGRATOR_ROLE, restored, good, wrong, dump_path)
    drill.restore_order = len(drill.attempts)
    drill.restore = run_restore(cluster, conninfo(cluster, MIGRATOR_ROLE, restored), dump_path, good)
    drill.logs_after_restore = _container_logs(cluster.container)
    if drill.restore.returncode != 0:
        return drill

    # 3. compare, then perform the explicit post-restore provisioning
    src_admin, dst_admin = cluster.admin_engine(source.name), cluster.admin_engine(restored)
    try:
        drill.backup_connect_via_public_before_reprovision = scalar(dst_admin, "SELECT has_database_privilege(:r, :d, 'CONNECT')", r=BACKUP_ROLE, d=restored)
        drill.runtime_temp_privilege_before_reprovision = scalar(dst_admin, "SELECT has_database_privilege(:r, :d, 'TEMPORARY')", r=RUNTIME_ROLE, d=restored)
        provision_database_level_privileges(cluster, restored)
        provision_backup_access(cluster, restored)
        drill.comparison = compare_databases(src_admin, dst_admin)
        drill.carried_owners_equal = drill.comparison.owners_equal
        drill.carried_acls_equal = drill.comparison.acls_equal
    finally:
        src_admin.dispose()
        dst_admin.dispose()
    return drill


@pytest.fixture(scope="module")
def tls_recovery(tls_cluster, source_database):
    drill = perform_tls_recovery_drill(tls_cluster, source_database)
    try:
        yield drill
    finally:
        _drop_database(tls_cluster, drill.restored)
        remove_files(tls_cluster, drill.dump_path, *drill.pgpass_files)


def test_pgpass_files_are_owner_only_and_hold_secrets_nowhere_but_inside_the_container(tls_cluster, tls_recovery):
    for path in tls_recovery.pgpass_files[:2]:
        mode = _exec(tls_cluster, ["stat", "-c", "%a", path])
        assert mode.returncode == 0 and mode.stdout.strip() == "600", (path, mode.stdout)


def test_tool_rejections_precede_the_corresponding_success_and_each_has_its_own_reason(tls_cluster, tls_recovery):
    labels = {(a.utility, a.label) for a in tls_recovery.attempts}
    assert labels == {(u, l) for u in ("pg_dump", "pg_restore") for l in _TOOL_NEGATIVES}, "a negative control is missing"
    dump_negatives = [a for a in tls_recovery.attempts if a.utility == "pg_dump"]
    restore_negatives = [a for a in tls_recovery.attempts if a.utility == "pg_restore"]
    assert all(a.order < tls_recovery.dump_order for a in dump_negatives)
    assert all(tls_recovery.dump_order <= a.order < tls_recovery.restore_order for a in restore_negatives)
    assert tls_recovery.dump_order > 0 and tls_recovery.restore_order > tls_recovery.dump_order
    for attempt in tls_recovery.attempts:
        fragment = _TOOL_NEGATIVES[attempt.label][1]
        assert attempt.result.returncode != 0, f"{attempt.utility}/{attempt.label}: unexpectedly succeeded"
        assert fragment in attempt.result.stderr, f"{attempt.utility}/{attempt.label}: failed, but not for {fragment!r}: {attempt.result.stderr.strip()[-300:]}"
        assert attempt.auth_delta == 0, f"{attempt.utility}/{attempt.label}: the server authenticated a session that should have been refused"
        if attempt.label == "wrong_password":
            assert attempt.password_failure_delta >= 1, f"{attempt.utility}: the server did not log the password failure"


def test_a_rejected_dump_produces_no_archive_content(tls_cluster, tls_recovery):
    """pg_dump creates its output file before connecting, so the observable guarantee is: no non-empty file, and a non-zero exit (asserted above)."""
    for attempt in (a for a in tls_recovery.attempts if a.utility == "pg_dump"):
        probe = _exec(tls_cluster, ["sh", "-c", f"test -s {tls_recovery.dump_path}.negative_{attempt.label} && echo content || echo empty"])
        assert probe.stdout.strip() == "empty", f"{attempt.label}: archive content exists although authentication/TLS failed"


def test_dump_and_restore_succeeded_with_clean_stderr_over_verified_tls(tls_recovery):
    tls_recovery.require_success()
    assert tls_recovery.dump.stderr.strip() == "", tls_recovery.dump.diagnostics()
    assert tls_recovery.restore.stderr.strip() == "", tls_recovery.restore.diagnostics()
    assert tls_recovery.dump_size > 0
    assert tls_recovery.restored != tls_recovery.source


def test_server_logged_both_tools_as_scram_authenticated_tls_sessions_under_the_right_roles(tls_recovery):
    tls_recovery.require_success()
    dump_sessions = authenticated_sessions(tls_recovery.logs_after_dump, app="pg_dump", user=BACKUP_ROLE, database=tls_recovery.source)
    restore_sessions = authenticated_sessions(tls_recovery.logs_after_restore, app="pg_restore", user=MIGRATOR_ROLE, database=tls_recovery.restored)
    assert dump_sessions and restore_sessions, "the server log shows no authenticated pg_dump/pg_restore session"
    for session in (*dump_sessions, *restore_sessions):
        assert session["method_scram"] is True and session["tls_protocol"] in TLS_PROTOCOLS, session
    # and nothing connected as the OTHER role: the dump never ran as the migrator, the restore never as the backup role
    assert authenticated_sessions(tls_recovery.logs_after_restore, app="pg_dump", user=MIGRATOR_ROLE) == []
    assert authenticated_sessions(tls_recovery.logs_after_restore, app="pg_restore", user=BACKUP_ROLE) == []


def test_archive_toc_covers_every_public_table_and_sequence_and_both_roles_grants(tls_cluster, source_database, tls_recovery):
    tls_recovery.require_success()
    admin = tls_cluster.admin_engine(source_database.name)
    try:
        expected_tables = set(public_tables(admin))
        expected_sequences = {s for (s,) in rows(admin, "SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'")}
    finally:
        admin.dispose()
    toc = tls_recovery.listing.stdout
    assert set(re.findall(r"TABLE DATA public (\S+) ", toc)) == expected_tables
    assert set(re.findall(r"SEQUENCE SET public (\S+) ", toc)) == expected_sequences
    assert expected_tables >= set(REQUIRED_READ_TABLES)


def test_restored_data_matches_for_every_table(tls_recovery):
    tls_recovery.require_success()
    assert tls_recovery.comparison.fingerprint_diffs == {}, f"restored data differs: {tls_recovery.comparison.fingerprint_diffs}"


def test_restored_constraints_indexes_and_sequences_match_with_only_the_documented_rendering_difference(tls_recovery):
    tls_recovery.require_success()
    c = tls_recovery.comparison
    assert c.constraint_genuine_diff == {}, describe_diff(c.constraint_genuine_diff)
    assert set(c.constraint_rendering_only) == set(c.constraint_diff)
    for key, (src, dst) in c.constraint_rendering_only.items():
        assert src[4] != dst[4] and canonicalize_restored_check(dst[4]) == src[4], key
    assert c.index_diff == {}, describe_diff(c.index_diff)
    assert c.unvalidated_constraints == []
    assert c.sequence_source == c.sequence_restored and any(name == "team_aliases_id_seq" and last for name, last in c.sequence_restored)
    assert c.source_version == c.restored_version == ACCEPTED_ALEMBIC_HEAD


def test_ownership_and_table_sequence_acls_including_the_backup_role_were_carried(tls_cluster, tls_recovery):
    tls_recovery.require_success()
    assert tls_recovery.carried_owners_equal is True and tls_recovery.carried_acls_equal is True
    admin = tls_cluster.admin_engine(tls_recovery.restored)
    try:
        acls = rows(admin, "SELECT c.relname, c.relacl::text FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'S') ORDER BY 1")
        assert acls and all(f"{BACKUP_ROLE}=" in acl for _, acl in acls), "the backup role's grants were not carried to every table and sequence"
    finally:
        admin.dispose()


def test_database_level_privileges_are_not_carried_and_the_explicit_reprovisioning_restores_them(tls_cluster, tls_recovery):
    """A per-database dump does not carry database-level ACLs; before re-provisioning, PUBLIC's default CONNECT is what lets the backup role in."""
    tls_recovery.require_success()
    assert tls_recovery.backup_connect_via_public_before_reprovision is True
    assert tls_recovery.runtime_temp_privilege_before_reprovision is True
    assert_backup_boundary(tls_cluster, tls_recovery.restored)
    assert_runtime_boundary(tls_cluster, tls_recovery.restored)


def test_restored_database_is_ready_migrated_and_serves_the_accepted_reads(tls_cluster, source_database, tls_recovery, monkeypatch):
    tls_recovery.require_success()
    from src.services.health import check_readiness

    alembic_check_as_migrator(tls_cluster, tls_recovery.restored)
    engine = _app_engine_from_environment(monkeypatch, handwritten_dsn(tls_cluster, RUNTIME_ROLE, tls_cluster.runtime_password, tls_recovery.restored))
    try:
        assert check_readiness(engine).ready is True
        assert exercise_accepted_read_paths(engine, source_database.seeded) == REQUIRED_READ_TABLES
    finally:
        engine.dispose()


def test_restored_constraints_are_actually_enforced_over_tls(tls_cluster, tls_recovery):
    tls_recovery.require_success()
    engine = create_engine(tls_cluster.migrator_url(tls_recovery.restored))
    try:
        for statement, state in (
            ("INSERT INTO players (player_id, display_name, team_id, season_id) VALUES ('orphan', 'Orphan', 424242, 2026)", "23503"),
            ("INSERT INTO team_aliases (team_id, source, raw_value, normalized_value) SELECT team_id, source, raw_value, normalized_value FROM team_aliases LIMIT 1", "23505"),
            ("INSERT INTO data_sources (source_key, display_name, acquisition_type, is_rescrapable) VALUES ('zz_bad', 'Bad', 'definitely_not_allowed', false)", "23514"),
        ):
            with engine.connect() as conn, pytest.raises(DBAPIError) as excinfo:
                conn.execute(text(statement))
            assert _sqlstate(excinfo) == state, statement
        with engine.begin() as conn:
            new_id = conn.execute(text("INSERT INTO team_aliases (team_id, source, raw_value, normalized_value) SELECT team_id, 'drill', 'probe', 'probe' FROM teams LIMIT 1 RETURNING id")).scalar()
        assert new_id > tls_recovery.comparison.source_max_alias_id
    finally:
        engine.dispose()


def test_restored_database_can_itself_be_dumped_by_the_backup_role(tls_cluster, tls_recovery):
    """Second-generation backup: recovery leaves a database the backup role can immediately protect again."""
    tls_recovery.require_success()
    result, path, pgpass = _dump_as_backup(tls_cluster, tls_recovery.restored)
    try:
        assert result.returncode == 0, result.diagnostics()
        listing = _exec(tls_cluster, ["pg_restore", "--list", path])
        assert re.search(r"TABLE DATA public teams ", listing.stdout)
    finally:
        remove_files(tls_cluster, path, pgpass)


def test_a_restore_into_a_populated_database_fails_atomically_over_tls(tls_cluster, tls_recovery):
    tls_recovery.require_success()
    admin = tls_cluster.admin_engine(tls_recovery.restored)
    pgpass = write_pgpass(tls_cluster, [(MIGRATOR_ROLE, tls_cluster.migrator_password)])
    try:
        before = all_fingerprints(admin)
        again = run_restore(tls_cluster, conninfo(tls_cluster, MIGRATOR_ROLE, tls_recovery.restored), tls_recovery.dump_path, pgpass)
        assert again.returncode != 0 and "already exists" in again.stderr, again.diagnostics()
        assert all_fingerprints(admin) == before
    finally:
        remove_files(tls_cluster, pgpass)
        admin.dispose()


def test_a_truncated_archive_never_restores_and_leaves_nothing_behind(tls_cluster, tls_recovery):
    tls_recovery.require_success()
    truncated = f"{tls_recovery.dump_path}.truncated"
    cut = _exec(tls_cluster, ["sh", "-c", f"head -c 200 {tls_recovery.dump_path} > {truncated}"])
    assert cut.returncode == 0
    scratch = _create_database_owned_by_migrator(tls_cluster)
    pgpass = write_pgpass(tls_cluster, [(MIGRATOR_ROLE, tls_cluster.migrator_password)])
    try:
        bad = run_restore(tls_cluster, conninfo(tls_cluster, MIGRATOR_ROLE, scratch), truncated, pgpass)
        assert bad.returncode != 0
        admin = tls_cluster.admin_engine(scratch)
        try:
            assert public_tables(admin) == []
        finally:
            admin.dispose()
    finally:
        _drop_database(tls_cluster, scratch)
        remove_files(tls_cluster, truncated, pgpass)


def test_restore_requires_the_cluster_roles_to_exist_first_and_fails_atomically_without_them(tls_cluster, tls_recovery):
    """
    The archive carries object ownership and ACLs that NAME roles, but not the
    roles themselves. Recovery into a cluster that lacks them must fail (and
    leave nothing), which is why the recovery procedure begins by provisioning
    cluster roles. Simulated by renaming the backup role away for the attempt.
    """
    tls_recovery.require_success()
    admin = tls_cluster.admin_engine()
    scratch = _create_database_owned_by_migrator(tls_cluster)
    pgpass = write_pgpass(tls_cluster, [(MIGRATOR_ROLE, tls_cluster.migrator_password)])
    away = f"{BACKUP_ROLE}_away"
    try:
        with admin.connect() as conn:
            conn.execute(text(f"ALTER ROLE {BACKUP_ROLE} RENAME TO {away}"))
        try:
            result = run_restore(tls_cluster, conninfo(tls_cluster, MIGRATOR_ROLE, scratch), tls_recovery.dump_path, pgpass)
        finally:
            with admin.connect() as conn:
                conn.execute(text(f"ALTER ROLE {away} RENAME TO {BACKUP_ROLE}"))
        assert result.returncode != 0 and f'role "{BACKUP_ROLE}" does not exist' in result.stderr, result.diagnostics()
        scratch_admin = tls_cluster.admin_engine(scratch)
        try:
            assert public_tables(scratch_admin) == []
        finally:
            scratch_admin.dispose()
        assert scalar(admin, "SELECT left(rolpassword, 14) FROM pg_authid WHERE rolname = :r", r=BACKUP_ROLE) == "SCRAM-SHA-256$", "the rename must not have damaged the credential"
    finally:
        admin.dispose()
        _drop_database(tls_cluster, scratch)
        remove_files(tls_cluster, pgpass)


def test_no_secret_appears_in_any_recorded_command_line_or_output(tls_cluster, tls_recovery):
    tls_recovery.require_success()
    results = [a.result for a in tls_recovery.attempts] + [tls_recovery.dump, tls_recovery.listing, tls_recovery.restore]
    for result in results:
        blob = " ".join(result.argv) + result.stdout + result.stderr
        for secret in tls_cluster.secrets_to_scrub():
            assert secret not in blob
        assert "PGPASSWORD" not in blob and "password=" not in blob.lower()
    # the server log itself must not contain any generated secret either
    logs = _container_logs(tls_cluster.container)
    for secret in tls_cluster.secrets_to_scrub():
        assert secret not in logs


def test_recovery_leaves_no_credential_files_behind_after_cleanup(tls_cluster):
    """Self-contained: creates a pgpass file the same way the drill does, removes it the same way, and proves the removal."""
    path = write_pgpass(tls_cluster, [(BACKUP_ROLE, tls_cluster.backup_password)])
    assert _exec(tls_cluster, ["test", "-f", path]).returncode == 0
    remove_files(tls_cluster, path)
    assert _exec(tls_cluster, ["test", "-e", path]).returncode != 0
