"""
Phase 9B-7B: the live-provider compatibility probe (TEST LAYER ONLY; `tests/` never enters an image).

One fixed procedure against ONE explicitly designated, disposable PostgreSQL endpoint holding synthetic data. It is not a
capture implementation and not a framework: the capture itself is the retained backup image (`backup-capture`), and every
other step calls the accepted helpers unchanged --

* setup: the 9B-2/9B-4 bootstrap (three NOSUPERUSER roles, a migrator-owned database) and `provision_database` (database
  privileges, Alembic as the migrator, the runtime and backup grants, the deterministic synthetic seed);
* identity: the production `observe_capture_preflight` under the v1 policy, run AS THE BACKUP ROLE, which reads the
  server version, ownership, grantees, sequences and `system_identifier` itself -- nothing is invented or copied from
  another role;
* overlap: the production `backup_capture_lock` holds the real advisory lock while a second job runs;
* verify-restore: 9B-6J's `open_published` (the accepted verifier plus an independent digest re-check), 9B-6E's guarded
  `restore` (migrator, `--exit-on-error --single-transaction`) and `compare` (the strict comparator against the artifact's
  SAVED baseline, `monotonic_v1` reported separately);
* residue and cleanup: only what this probe's ledger recorded, refusing anything else.

Steps (`python -m tests.postgres.provider_probe --config <file> <step>`), each writing one sanitized JSON evidence file:

  inspect          read-only, as the provider admin: TLS, server version, admin attributes, and the absence of every
                   resource setup would create (an unexpected existing resource is REFUSED, never reused or dropped)
  setup            MUTATION (requires --confirm <source database>): roles, source database, migrations, grants, seed
  identity         read-only, as the backup role: preflight, identity, session behavior; writes the job's env file
  hold-lock        read-only, as the backup role: holds the production advisory lock for --seconds (the overlap check)
  verify-restore   MUTATION on the target only (requires --confirm <target database>): verify the published artifact,
                   create the guarded target, restore, compare
  residue          read-only: owned sessions and advisory-lock holders
  cleanup          MUTATION (requires --confirm cleanup): drops exactly the ledger's databases and roles, then proves it

Exit codes: 0 PASS; 2 BLOCKER (the endpoint refused a required capability -- never worked around); 3 REFUSED (a
precondition: configuration, an unexpected existing resource, an identity mismatch); 4 DIFFERENCE (the restore completed but
the strict comparison or the sequence policy reported a difference); 1 ERROR (anything else; type name only).

Credentials: every password is read from a file under the job's own password-file rules and kept in memory. Role
passwords reach the server only as client-computed SCRAM-SHA-256 verifiers. No password is written to a URL that is
printed, to an argument, to the env file, to evidence or to stdout; every evidence record is checked before it is written.

A pass establishes only what each check observed on that endpoint at that time. It is not provider certification, a
production deployment or a recovery-readiness claim. Null foreign-write counts mean "not sampled".
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import posixpath
import shutil
import socket
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

PASS, ERROR, BLOCKER, REFUSED, DIFFERENCE = 0, 1, 2, 3, 4
OUTCOMES = {PASS: "pass", ERROR: "error", BLOCKER: "blocker", REFUSED: "refused", DIFFERENCE: "difference"}
PROBE_ROLES = ("napsedge_migrator", "napsedge_runtime_ro", "napsedge_backup_ro")
CREDENTIAL_KEYS = ("admin", "migrator", "runtime", "backup")
TOP_KEYS = {"endpoint", "admin_user", "credential_files", "source_database", "target_database", "evidence_dir", "job"}
ENDPOINT_KEYS = {"host", "hostaddr", "port", "admin_database", "ca_file"}
JOB_KEYS = {"git_revision", "git_dirty", "tooling_version", "container_ca_file", "container_password_file", "container_store_dir"}
LEDGER = "ledger.json"
JOB_ENV = "job.env"
NATIVE_TOOL_DIR = "/usr/lib/postgresql/16/bin"


class ProbeRefused(Exception):
    """A precondition does not hold; nothing further was attempted."""


class ProbeBlocker(Exception):
    """The endpoint refused a capability the accepted contract requires. Reported, never worked around."""


# ------------------------------------------------------------------------------------------------------------- configuration
@dataclass(frozen=True)
class ProbeConfig:
    host: str
    hostaddr: str | None
    port: int
    admin_database: str
    ca_file: str
    admin_user: str
    credential_files: dict
    source_database: str
    target_database: str
    evidence_dir: Path
    job: dict


def _plain(value):
    import dataclasses

    return dataclasses.asdict(value) if dataclasses.is_dataclass(value) else repr(value)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProbeRefused(message)


def load_config(path: str | os.PathLike) -> ProbeConfig:
    """Strict: unknown or missing keys, inline secrets, relative paths and non-disposable names are refused."""
    from tests.postgres.conftest import assert_disposable_database_name

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProbeRefused(f"the configuration cannot be read ({type(exc).__name__})") from None
    return load_config_data(data)


def load_config_data(data) -> ProbeConfig:
    """The same strict validation for already-loaded data (Phase 9B-8B: the restore drill reuses it)."""
    from tests.postgres.conftest import assert_disposable_database_name

    _require(isinstance(data, dict) and set(data) == TOP_KEYS, f"configuration keys must be exactly {sorted(TOP_KEYS)}")
    endpoint, files, job = data["endpoint"], data["credential_files"], data["job"]
    _require(isinstance(endpoint, dict) and set(endpoint) == ENDPOINT_KEYS, f"endpoint keys must be exactly {sorted(ENDPOINT_KEYS)}")
    _require(isinstance(files, dict) and set(files) == set(CREDENTIAL_KEYS), f"credential_files keys must be exactly {list(CREDENTIAL_KEYS)}")
    _require(isinstance(job, dict) and set(job) == JOB_KEYS, f"job keys must be exactly {sorted(JOB_KEYS)}")
    for key, value in files.items():
        _require(isinstance(value, str) and posixpath.isabs(value), f"credential_files.{key} must be an absolute POSIX path")
    _require(len(set(files.values())) == len(files), "each role needs its own credential file")
    _require(isinstance(endpoint["host"], str) and endpoint["host"] and "@" not in endpoint["host"] and "/" not in endpoint["host"],
             "endpoint.host must be a bare host name (the name in the server certificate)")
    _require(endpoint["hostaddr"] is None or (isinstance(endpoint["hostaddr"], str) and _is_ip(endpoint["hostaddr"])),
             "endpoint.hostaddr must be null or an IP address")
    _require(isinstance(endpoint["port"], int) and 0 < endpoint["port"] < 65536, "endpoint.port must be an integer port")
    _require(isinstance(endpoint["admin_database"], str) and endpoint["admin_database"], "endpoint.admin_database is required")
    _require(isinstance(endpoint["ca_file"], str) and endpoint["ca_file"].startswith("/"), "endpoint.ca_file must be an absolute POSIX path")
    _require(isinstance(data["admin_user"], str) and data["admin_user"] and data["admin_user"] not in PROBE_ROLES,
             "admin_user must be the provider's admin role, never one of the probe's roles")
    source, target = data["source_database"], data["target_database"]
    for name in (source, target):
        _require(isinstance(name, str), "database names must be strings")
        try:
            assert_disposable_database_name(name)
        except RuntimeError:
            raise ProbeRefused("database names must start with the disposable prefix napsedge_pg_test_") from None
    _require(source != target and data["endpoint"]["admin_database"] not in (source, target), "source, target and admin databases must differ")
    _require(isinstance(data["evidence_dir"], str) and data["evidence_dir"].startswith("/"), "evidence_dir must be an absolute POSIX path")
    _require(isinstance(job["git_revision"], str) and len(job["git_revision"]) == 40 and all(c in "0123456789abcdef" for c in job["git_revision"]),
             "job.git_revision must be a 40-hex revision")
    _require(isinstance(job["git_dirty"], bool), "job.git_dirty must be true or false")
    _require(isinstance(job["tooling_version"], str) and job["tooling_version"], "job.tooling_version is required")
    for key in ("container_ca_file", "container_password_file", "container_store_dir"):
        _require(isinstance(job[key], str) and job[key].startswith("/"), f"job.{key} must be an absolute POSIX path")
    return ProbeConfig(endpoint["host"], endpoint["hostaddr"], endpoint["port"], endpoint["admin_database"], endpoint["ca_file"],
                       data["admin_user"], dict(files), source, target, Path(data["evidence_dir"]), dict(job))


def _is_ip(value: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def read_secret(config: ProbeConfig, key: str) -> str:
    """The job's own password-file rules (9B-7A), applied to every probe credential; a refusal never echoes content or path."""
    from src.deployment.backup_job import JobConfigurationError, _read_password_file

    try:
        return _read_password_file(config.credential_files[key])
    except JobConfigurationError as exc:
        reason = str(exc).replace("NAPSEDGE_BACKUP_DATABASE_PASSWORD_FILE", f"credential_files.{key}")
        raise ProbeRefused(reason) from None


def resolve_hostaddr(config: ProbeConfig) -> str:
    """Pins one address for every session (libpq still verifies `host` against the certificate)."""
    if config.hostaddr:
        return config.hostaddr
    try:
        infos = socket.getaddrinfo(config.host, config.port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ProbeBlocker(f"the endpoint host does not resolve ({type(exc).__name__})") from None
    return sorted({info[4][0] for info in infos})[0]


def probe_cluster(config: ProbeConfig, hostaddr: str):
    """A `TlsCluster` for the configured endpoint, so every accepted helper that takes a cluster runs unchanged against it."""
    from sqlalchemy.engine import URL

    from tests.postgres.test_postgres_tls_backup_restore import TlsCluster

    class EndpointCluster(TlsCluster):
        @property
        def ca_path(self) -> str:
            return config.ca_file

    admin_url = URL.create("postgresql+psycopg", username=config.admin_user, password=read_secret(config, "admin"), host=config.host,
                           port=config.port, database=config.admin_database,
                           query={"sslmode": "verify-full", "sslrootcert": config.ca_file, "hostaddr": hostaddr, "connect_timeout": "15"})
    return EndpointCluster(container=None, admin_url=admin_url, admin_password=admin_url.password,
                           migrator_password=read_secret(config, "migrator"), runtime_password=read_secret(config, "runtime"),
                           backup_password=read_secret(config, "backup"), port=config.port, material=None)


class MigratorReader:
    """The comparison reader: the restored target's OWNER (the archive restores every object owned by the migrator), so no
    privilege is granted to the provider admin. Only `admin_engine` differs; `compare` itself is unchanged."""

    def __init__(self, cluster) -> None:
        self._cluster = cluster

    def admin_engine(self, database: str | None = None, *, autocommit: bool = True):
        from sqlalchemy import create_engine

        url = self._cluster.migrator_url(database or self._cluster.admin_url.database)
        return create_engine(url, isolation_level="AUTOCOMMIT") if autocommit else create_engine(url)

    def __getattr__(self, name):
        return getattr(self._cluster, name)


# ------------------------------------------------------------------------------------------------------- evidence and ledger
def write_evidence(config: ProbeConfig, name: str, payload: dict, secrets: tuple[str, ...]) -> Path:
    text = json.dumps(payload, sort_keys=True, default=str, indent=1)
    for secret in secrets:
        if secret and secret in text:
            raise RuntimeError("an evidence record contained a credential and was NOT written")
    path = config.evidence_dir / f"{name}.json"
    path.write_text(text + "\n", encoding="utf-8")
    return path


def read_ledger(config: ProbeConfig) -> list[dict]:
    path = config.evidence_dir / LEDGER
    if not path.exists():
        return []
    entries = json.loads(path.read_text(encoding="utf-8"))
    for entry in entries:
        _require(entry.get("kind") in ("role", "database"), "the ledger holds an unknown entry")
        if entry["kind"] == "role":
            _require(entry["name"] in PROBE_ROLES, "the ledger names a role this probe never creates")
        else:
            _require(entry["name"] in (config.source_database, config.target_database), "the ledger names a database outside the configuration")
    return entries


def record_created(config: ProbeConfig, kind: str, name: str) -> None:
    """Registered the moment the resource exists, so cleanup removes exactly what this probe created."""
    entries = read_ledger(config)
    entries.append({"kind": kind, "name": name, "created_unix": round(time.time(), 3)})
    tmp = config.evidence_dir / (LEDGER + ".tmp")
    tmp.write_text(json.dumps(entries, indent=1), encoding="utf-8")
    os.replace(tmp, config.evidence_dir / LEDGER)


def _forget(config: ProbeConfig, kind: str, name: str) -> None:
    entries = [e for e in read_ledger(config) if (e["kind"], e["name"]) != (kind, name)]
    tmp = config.evidence_dir / (LEDGER + ".tmp")
    tmp.write_text(json.dumps(entries, indent=1), encoding="utf-8")
    os.replace(tmp, config.evidence_dir / LEDGER)


# -------------------------------------------------------------------------------------------------------- database access
def _engine(url):
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    return create_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")


def _scalar(engine, sql: str, **params):
    from sqlalchemy import text

    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def _rows(engine, sql: str, **params) -> list[tuple]:
    from sqlalchemy import text

    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql), params)]


def _connect_or_block(engine, who: str) -> None:
    from sqlalchemy.exc import OperationalError

    try:
        with engine.connect():
            pass
    except OperationalError as exc:
        reason = str(getattr(exc, "orig", exc)).splitlines()[0][:200]
        raise ProbeBlocker(f"{who} could not connect with verify-full: {reason}") from None


def existing_resources(admin, config: ProbeConfig) -> dict:
    return {
        "roles": [r[0] for r in _rows(admin, "SELECT rolname FROM pg_roles WHERE rolname = ANY(CAST(:r AS text[])) ORDER BY 1", r=list(PROBE_ROLES))],
        "databases": [r[0] for r in _rows(admin, "SELECT datname FROM pg_database WHERE datname = ANY(CAST(:d AS text[])) ORDER BY 1",
                                         d=[config.source_database, config.target_database])],
    }


def connection_headroom(engine) -> dict:
    """Check 7, OBSERVED ONLY: the server's limits and the client backends visible now. No threshold is judged here -- the
    application is not running and pool caps are outside this unit."""
    row = _rows(engine, "SELECT current_setting('max_connections')::int, current_setting('superuser_reserved_connections')::int, "
                        "current_setting('reserved_connections', true), (SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend')")[0]
    return {"max_connections": row[0], "superuser_reserved_connections": row[1], "reserved_connections": row[2], "client_backends_visible": row[3],
            "job_sessions_expected": "about 3-4 during a capture (decision package R11)"}


def own_backend_termination(engine) -> dict:
    """Check 6 (R6): the backup role ends ANOTHER of its own backends -- a second session this probe opened for the purpose, and
    nothing else -- with `pg_terminate_backend`, then the backend is observed gone."""
    from sqlalchemy import text

    victim = engine.connect()
    try:
        pid = victim.execute(text("SELECT pg_backend_pid()")).scalar()
        with engine.connect() as conn:
            terminated = conn.execute(text("SELECT pg_terminate_backend(:p, 5000)"), {"p": pid}).scalar()
            still_there = conn.execute(text("SELECT count(*) FROM pg_stat_activity WHERE pid = :p"), {"p": pid}).scalar()
    finally:
        with contextlib.suppress(Exception):
            victim.close()  # its backend is already gone; the client side is discarded
    return {"terminated": bool(terminated), "backend_gone": still_there == 0}


def tls_and_server(engine) -> dict:
    row = _rows(engine, "SELECT current_setting('server_version_num')::int, s.ssl, s.version, s.cipher IS NOT NULL "
                        "FROM pg_stat_ssl s WHERE s.pid = pg_backend_pid()")[0]
    return {"server_version_num": row[0], "ssl": row[1], "tls_version": row[2], "cipher_present": row[3]}


# ------------------------------------------------------------------------------------------------------------------ steps
def step_inspect(config, cluster, args) -> tuple[int, dict]:
    admin = _engine(cluster.admin_url)
    try:
        _connect_or_block(admin, "the provider admin")
        facts = tls_and_server(admin)
        role = _rows(admin, "SELECT rolsuper, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname = current_user")[0]
        facts.update(admin_role={"superuser": role[0], "createrole": role[1], "createdb": role[2]},
                     createrole_self_grant=_scalar(admin, "SELECT current_setting('createrole_self_grant', true)"),
                     existing=existing_resources(admin, config), ledger_entries=len(read_ledger(config)),
                     connection_headroom_observed=connection_headroom(admin))
    finally:
        admin.dispose()
    if facts["server_version_num"] // 10000 != 16:
        return BLOCKER, {**facts, "blocker": "the server is not PostgreSQL 16"}
    if facts["ssl"] is not True:
        return BLOCKER, {**facts, "blocker": "the session is not encrypted"}
    if not (facts["admin_role"]["createrole"] and facts["admin_role"]["createdb"]) and not facts["admin_role"]["superuser"]:
        return BLOCKER, {**facts, "blocker": "the admin role can neither create roles nor databases"}
    if facts["existing"]["roles"] or facts["existing"]["databases"] or facts["ledger_entries"]:
        return REFUSED, {**facts, "refused": "resources setup would create already exist (never reused or dropped by this probe)"}
    return PASS, facts


def _create_role(admin, name: str, password: str, *, self_grant_set: bool) -> None:
    """CREATE ROLE with a client-computed SCRAM verifier; the plaintext never reaches the server or its logs."""
    from sqlalchemy import text

    with admin.connect() as conn:
        pgconn = conn.connection.driver_connection.pgconn
        verifier = pgconn.encrypt_password(password.encode("utf-8"), name.encode("utf-8"), b"scram-sha-256").decode("ascii")
        assert verifier.startswith("SCRAM-SHA-256$") and "'" not in verifier
        if self_grant_set:
            # PostgreSQL 16: a non-superuser CREATEROLE admin receives ADMIN on the roles it creates but, by default, cannot
            # SET ROLE to them -- which CREATE DATABASE ... OWNER requires. Exactly SET (no INHERIT, so no access to the
            # migrator's objects), on the migrator only, in this session only; it disappears when the role is dropped.
            conn.execute(text("SET createrole_self_grant = 'set'"))
        try:
            # exec_driver_sql: the verifier's `:` sequences must not be parsed as bind parameters; no parameters are sent
            conn.exec_driver_sql(f"CREATE ROLE {name} LOGIN PASSWORD '{verifier}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS")
        finally:
            if self_grant_set:
                conn.execute(text("RESET createrole_self_grant"))


def _create_database(admin, name: str) -> None:
    from sqlalchemy import text

    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}" OWNER napsedge_migrator'))


def _blocker_from(exc: Exception, what: str) -> ProbeBlocker:
    orig = getattr(exc, "orig", exc)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(getattr(orig, "diag", None), "sqlstate", None)
    return ProbeBlocker(f"{what} was refused by the endpoint (SQLSTATE {sqlstate}; {str(orig).splitlines()[0][:160]})")


def step_setup(config, cluster, args) -> tuple[int, dict]:
    from sqlalchemy.exc import DBAPIError

    from tests.postgres.test_postgres_tls_backup_restore import provision_database

    _require(args.confirm == config.source_database, "setup needs --confirm <source database> (it creates roles and the source database)")
    code, facts = step_inspect(config, cluster, args)
    if code != PASS:
        return code, {"inspect": facts}
    admin = _engine(cluster.admin_url)
    done: list[str] = []
    try:
        passwords = {"napsedge_migrator": cluster.migrator_password, "napsedge_runtime_ro": cluster.runtime_password,
                     "napsedge_backup_ro": cluster.backup_password}
        for name in PROBE_ROLES:
            try:
                _create_role(admin, name, passwords[name], self_grant_set=(name == "napsedge_migrator" and not facts["admin_role"]["superuser"]))
            except DBAPIError as exc:
                raise _blocker_from(exc, f"CREATE ROLE {name}") from None
            record_created(config, "role", name)
            done.append(f"MUTATION: created role {name} (NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS)")
        try:
            _create_database(admin, config.source_database)
        except DBAPIError as exc:
            raise _blocker_from(exc, "CREATE DATABASE ... OWNER napsedge_migrator") from None
        record_created(config, "database", config.source_database)
        done.append(f"MUTATION: created database {config.source_database} owned by napsedge_migrator")
    finally:
        admin.dispose()
    try:
        seeded = provision_database(cluster, config.source_database, seed=True).seeded  # the accepted 9B-2 grants + migrations + seed
    except DBAPIError as exc:
        raise _blocker_from(exc, "provisioning the source (privileges, migrations, grants, seed)") from None
    done.append("MUTATION: database privileges, Alembic upgrade as the migrator, runtime and backup grants, synthetic seed")
    return PASS, {"mutations": done, "seeded": {"duke_team_id": seeded["duke_team_id"], "players": len(seeded["player_ids"])}, "inspect": facts}


def designation(config):
    from src.pipeline.bulk_import_safety import DatabaseTargetDesignation, build_expected_acknowledgement

    return DatabaseTargetDesignation(expected_database_name=config.source_database,
                                     acknowledgement=build_expected_acknowledgement(config.source_database))


def job_env(config: ProbeConfig, hostaddr: str, preflight) -> str:
    """The image's configuration: no password anywhere (the job reads its password file); every identity fact read by the backup role."""
    from sqlalchemy.engine import URL

    from src.pipeline.bulk_import_safety import build_expected_acknowledgement

    url = URL.create("postgresql+psycopg", username="napsedge_backup_ro", host=config.host, port=config.port, database=config.source_database,
                     query={"sslmode": "verify-full", "sslrootcert": config.job["container_ca_file"], "hostaddr": hostaddr})
    values = {
        "NAPSEDGE_BACKUP_DATABASE_URL": url.render_as_string(hide_password=False),
        "NAPSEDGE_BACKUP_DATABASE_PASSWORD_FILE": config.job["container_password_file"],
        "NAPSEDGE_BACKUP_TARGET_ACKNOWLEDGEMENT": build_expected_acknowledgement(config.source_database),
        "NAPSEDGE_BACKUP_EXPECTED_SYSTEM_IDENTIFIER": preflight.system_identifier,
        "NAPSEDGE_BACKUP_EXPECTED_DATABASE_OID": str(preflight.database_oid),
        "NAPSEDGE_BACKUP_STORE_DIR": config.job["container_store_dir"],
        "NAPSEDGE_BACKUP_LOGICAL_NAME": "napsedge",
        "NAPSEDGE_BACKUP_PG_DUMP": f"{NATIVE_TOOL_DIR}/pg_dump",
        "NAPSEDGE_BACKUP_PG_RESTORE": f"{NATIVE_TOOL_DIR}/pg_restore",
        "NAPSEDGE_BACKUP_GIT_REVISION": config.job["git_revision"],
        "NAPSEDGE_BACKUP_GIT_DIRTY": "true" if config.job["git_dirty"] else "false",
        "NAPSEDGE_BACKUP_TOOLING_VERSION": config.job["tooling_version"],
    }
    assert url.password is None, "the job's URL never carries a password"
    return "".join(f"{k}={v}\n" for k, v in values.items())


def step_identity(config, cluster, args) -> tuple[int, dict]:
    from sqlalchemy import text

    from src.deployment.backup_capture_preflight import observe_capture_preflight, v1_policy
    from tests.postgres.test_postgres_roles_and_restore import ACCEPTED_ALEMBIC_HEAD

    engine = _engine(cluster.backup_url(config.source_database))
    try:
        _connect_or_block(engine, "the backup role")
        with engine.connect() as conn:
            pids = [conn.execute(text("SELECT pg_backend_pid()")).scalar() for _ in range(3)]  # three autocommit transactions
        tls = tls_and_server(engine)
        with engine.connect() as conn:
            with conn.begin():
                preflight = observe_capture_preflight(conn, v1_policy(designation(config), ACCEPTED_ALEMBIC_HEAD))
        try:
            termination = own_backend_termination(engine)
        except Exception as exc:  # noqa: BLE001 -- a refusal is the finding; reported by type and SQLSTATE only
            orig = getattr(exc, "orig", exc)
            termination = {"terminated": False, "backend_gone": False, "error_type": type(exc).__name__, "sqlstate": getattr(orig, "sqlstate", None)}
    finally:
        engine.dispose()
    facts = {
        "role": "napsedge_backup_ro", **tls, "same_backend_across_transactions": len(set(pids)) == 1,
        "accepted": preflight.accepted, "server_version_supported": preflight.server_version_supported, "head_matches": preflight.head_matches,
        "alembic_heads": list(preflight.alembic_heads), "database_owner": preflight.database_owner,
        "database_owner_supported": preflight.database_owner_supported, "ownership_findings": [_plain(f) for f in preflight.ownership_findings],
        "unsupported_objects": [_plain(o) for o in preflight.unsupported_objects], "sequences_supported": preflight.sequences_supported,
        "sequences": len(preflight.sequences), "system_identifier_readable": preflight.system_identifier_readable,
        "system_identifier": preflight.system_identifier, "database_oid": preflight.database_oid, "database_name": config.source_database,
        "own_backend_termination": termination,
    }
    if tls["ssl"] is not True or tls["server_version_num"] // 10000 != 16:
        return BLOCKER, {**facts, "blocker": "the backup role's session is not PostgreSQL 16 over TLS"}
    if not facts["same_backend_across_transactions"]:
        return BLOCKER, {**facts, "blocker": "the endpoint is pooled: one connection changed backends between transactions (the lock "
                                             "and snapshot need direct sessions)"}
    if not preflight.system_identifier_readable:
        return BLOCKER, {**facts, "blocker": "the backup role may not execute pg_control_system(): the source identity cannot be read "
                                             "(no privilege is granted and no identity is substituted)"}
    if not (termination["terminated"] and termination["backend_gone"]):
        return BLOCKER, {**facts, "blocker": "the backup role could not end its own backend with pg_terminate_backend (R6)"}
    if not preflight.accepted:
        return BLOCKER, {**facts, "blocker": "the production preflight did not accept the source (see the findings)"}
    env_path = config.evidence_dir / JOB_ENV
    env_path.write_text(job_env(config, resolve_hostaddr(config), preflight), encoding="utf-8")
    return PASS, {**facts, "job_env": JOB_ENV}


def step_hold_lock(config, cluster, args) -> tuple[int, dict]:
    from src.deployment.backup_capture_lock import backup_capture_lock

    marker = config.evidence_dir / "hold_lock.held"
    with backup_capture_lock(cluster.backup_url(config.source_database), designation(config)) as lock:
        held = {"backend_pid": lock.backend_pid, "lock_key": str(lock.lock_key)}
        marker.write_text(json.dumps(held), encoding="utf-8")
        time.sleep(args.seconds)
    marker.unlink(missing_ok=True)
    return PASS, {**held, "held_seconds": args.seconds, "released_and_session_verified_gone": True}


def lock_key(config, cluster) -> int:
    from sqlalchemy import text

    from src.pipeline.bulk_import_safety import compute_advisory_lock_key, verify_database_target

    engine = _engine(cluster.backup_url(config.source_database))
    try:
        with engine.connect() as conn:
            verified = verify_database_target(conn, designation(config))
            conn.execute(text("SELECT 1"))
    finally:
        engine.dispose()
    return compute_advisory_lock_key(verified)


def step_residue(config, cluster, args) -> tuple[int, dict]:
    from tests.postgres.test_postgres_backup_overlap_quiescence import advisory_lock_rows

    key = lock_key(config, cluster)
    engine = _engine(cluster.backup_url(config.source_database))
    try:
        others = _rows(engine, "SELECT pid, application_name, state FROM pg_stat_activity WHERE usename = current_user AND pid <> pg_backend_pid() ORDER BY pid")
        holders = advisory_lock_rows(engine, key, config.source_database)
    finally:
        engine.dispose()
    facts = {"other_backup_role_sessions": [list(r) for r in others], "advisory_lock_holders": [list(r) for r in holders]}
    if others or holders:
        return REFUSED, {**facts, "refused": "owned sessions or lock holders remain"}
    return PASS, facts


def step_verify_restore(config, cluster, args) -> tuple[int, dict]:
    from sqlalchemy.exc import DBAPIError

    from tests.postgres.capture_client_support import NativeTestClient
    from tests.postgres.pg_restore_compare import acl_snapshot, all_fingerprints
    from tests.postgres.test_postgres_backup_capture_roundtrip import compare, restore, rule_rows
    from tests.postgres.test_postgres_backup_published_roundtrip import ArtifactRefused, open_published
    from tests.postgres.test_postgres_backup_sequence_consistency import read_sequence_details

    _require(args.confirm == config.target_database, "verify-restore needs --confirm <target database> (it creates and restores into it)")
    _require(args.artifact and posixpath.isabs(args.artifact), "--artifact must be the absolute path of the published artifact directory")
    identity_path = config.evidence_dir / "identity.json"
    _require(identity_path.exists(), "run identity first: its backup-role facts are what the artifact is checked against")
    identity = json.loads(identity_path.read_text(encoding="utf-8"))["facts"]
    try:
        published = open_published(args.artifact)
    except ArtifactRefused as exc:
        raise ProbeRefused(f"the published artifact was refused: {exc}") from None
    source = published.facts["source"]
    _require(published.facts["format"] == "napsedge.backup-artifact/2", "the artifact is not /2")
    _require((source["system_identifier"], source["database_oid"], source["database_name"])
             == (identity["system_identifier"], identity["database_oid"], identity["database_name"]),
             "the artifact's source identity differs from the identity the backup role read")
    admin = _engine(cluster.admin_url)
    try:
        _require(existing_resources(admin, config)["databases"] == [config.source_database], "the target database already exists (never reused or dropped)")
        try:
            _create_database(admin, config.target_database)
        except DBAPIError as exc:
            raise _blocker_from(exc, "CREATE DATABASE (restore target) ... OWNER napsedge_migrator") from None
        record_created(config, "database", config.target_database)
    finally:
        admin.dispose()
    client = NativeTestClient(cluster)
    scratch = tempfile.mkdtemp(prefix="napsedge_probe_")
    case = SimpleNamespace(cluster=cluster, client=client, container_dir=scratch, targets=[config.target_database], ledger=None)
    try:
        outcome = restore(case, published.captured, config.target_database)
        comparison = None
        reads = {}
        if outcome.returncode == 0:
            reader = MigratorReader(cluster)
            comparison = compare(SimpleNamespace(cluster=reader), published.captured, config.target_database)
            engine = reader.admin_engine(config.target_database)
            try:
                reads = {"fingerprints": all_fingerprints(engine), "acls": acl_snapshot(engine),
                         "sequences": [_plain(d) for d in read_sequence_details(engine)]}
            finally:
                engine.dispose()
        processes = client.client_processes()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    facts = {
        "artifact": published.facts, "target": outcome.identity, "restore_role": "napsedge_migrator", "restore_argv": outcome.argv_sanitized,
        "restore_exit": outcome.returncode, "restore_stderr": outcome.stderr[:400], "timed_out": outcome.timed_out,
        "restored_archive_sha256": outcome.archive_sha256, "listing_exit": outcome.listing_returncode, "comparison_reader": "napsedge_migrator (target owner)",
        "client_processes_after": processes, "scratch_removed": not os.path.exists(scratch), "mutations": [f"MUTATION: created database {config.target_database} owned by napsedge_migrator", "MUTATION: restored the artifact into it"],
        "restored_reads": reads, "readiness_claim": "none",
    }
    if comparison is not None:
        facts.update(strict_verdict=comparison.verdict, strict_sequences_equal=comparison.strict_sequences_equal,
                     sequence_differences=comparison.sequence_differences, monotonic_v1_rules=rule_rows(comparison),
                     monotonic_v1_policy_accepts=comparison.policy_accepts, monotonic_v1_r2_failures=comparison.r2_failures)
    if outcome.returncode != 0:
        return BLOCKER, {**facts, "blocker": "the restore into the guarded target failed"}
    if not (all(comparison.verdict.values()) and comparison.strict_sequences_equal and comparison.policy_accepts):
        return DIFFERENCE, facts
    return PASS, facts


def step_cleanup(config, cluster, args) -> tuple[int, dict]:
    from sqlalchemy import text

    _require(args.confirm == "cleanup", "cleanup needs --confirm cleanup")
    entries = read_ledger(config)
    owned = {(e["kind"], e["name"]) for e in entries}
    done, refused = [], []
    migrator = _engine(cluster.migrator_url(config.admin_database))
    try:
        for entry in [e for e in entries if e["kind"] == "database"][::-1]:
            name = entry["name"]
            others = _scalar(migrator, "SELECT count(*) FROM pg_stat_activity WHERE datname = :d AND pid <> pg_backend_pid()", d=name)
            if others:
                refused.append(f"{name}: {others} other session(s) connected; not dropped")
                continue
            with migrator.connect() as conn:
                conn.execute(text(f'DROP DATABASE "{name}"'))  # as the owner; no FORCE: nothing else is terminated
            _forget(config, "database", name)
            done.append(f"MUTATION: dropped database {name}")
    finally:
        migrator.dispose()
    admin = _engine(cluster.admin_url)
    try:
        if not refused:
            for entry in [e for e in read_ledger(config) if e["kind"] == "role"][::-1]:
                with admin.connect() as conn:
                    conn.execute(text(f"DROP ROLE {entry['name']}"))
                _forget(config, "role", entry["name"])
                done.append(f"MUTATION: dropped role {entry['name']}")
        present = existing_resources(admin, config)
    finally:
        admin.dispose()
    remaining = {"roles": [n for n in present["roles"] if ("role", n) in owned],
                 "databases": [n for n in present["databases"] if ("database", n) in owned]}
    unowned = {"roles": [n for n in present["roles"] if ("role", n) not in owned],
               "databases": [n for n in present["databases"] if ("database", n) not in owned]}
    facts = {"mutations": done, "refused": refused, "remaining": remaining, "present_but_not_created_by_this_probe_left_untouched": unowned,
             "ledger_entries_left": len(read_ledger(config))}
    if refused or remaining["roles"] or remaining["databases"]:
        return REFUSED, {**facts, "refused_reason": "cleanup could not complete; nothing outside the ledger was touched"}
    return PASS, facts


STEPS = {"inspect": step_inspect, "setup": step_setup, "identity": step_identity, "hold-lock": step_hold_lock,
         "verify-restore": step_verify_restore, "residue": step_residue, "cleanup": step_cleanup}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.postgres.provider_probe", description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", required=True)
    parser.add_argument("step", choices=sorted(STEPS))
    parser.add_argument("--confirm", default="")
    parser.add_argument("--artifact", default="")
    parser.add_argument("--seconds", type=int, default=60)
    args = parser.parse_args(argv)
    secrets: tuple[str, ...] = ()
    started = time.time()
    try:
        config = load_config(args.config)
        cluster = probe_cluster(config, resolve_hostaddr(config))
        secrets = cluster.secrets_to_scrub()
        code, facts = STEPS[args.step](config, cluster, args)
    except ProbeRefused as exc:
        code, facts = REFUSED, {"refused": str(exc)}
    except ProbeBlocker as exc:
        code, facts = BLOCKER, {"blocker": str(exc)}
    except Exception as exc:  # noqa: BLE001 -- type and code locations only; a message could carry endpoint details
        import traceback

        code, facts = ERROR, {"error_type": type(exc).__name__,
                              "at": [f"{os.path.basename(f.filename)}:{f.lineno}:{f.name}" for f in traceback.extract_tb(exc.__traceback__)][-8:]}
    record = {"step": args.step, "outcome": OUTCOMES[code], "exit": code, "seconds": round(time.time() - started, 2), "facts": facts}
    with contextlib.suppress(NameError, OSError):  # no configuration, or no writable evidence directory: stdout still reports
        write_evidence(config, args.step.replace("-", "_"), record, secrets)
    line = json.dumps({"step": args.step, "outcome": OUTCOMES[code], "exit": code, **({"reason": facts.get("blocker") or facts.get("refused")}
                                                                                        if code in (BLOCKER, REFUSED) else {})}, sort_keys=True, default=str)
    for secret in secrets:
        line = line.replace(secret, "[removed]") if secret else line
    print(line)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
