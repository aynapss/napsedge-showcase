"""
Phase 9B-6D: a real PostgreSQL 16 `pg_dump` consumer of the snapshot handle (`src/deployment/backup_capture_dump.py`),
inside the snapshot and lock contexts, against one disposable, synthetic PostgreSQL 16 cluster (the existing 9B harness:
verified TLS, SCRAM, the three bootstrap roles, the unchanged 9B-2 grants; each scenario on its own seeded database).

Where the client runs (TEST LAYER). This Windows host has no PostgreSQL client. The only PostgreSQL 16 `pg_dump` and
`pg_restore` available are those in the disposable cluster's own `postgres:16-alpine` container, which the 9B harness
already reaches with `docker exec`. `ContainerClientHost` implements the production `ClientHost` seam there:
* the owned work directory is a fresh `0700` directory in the container's `/tmp`;
* the client runs under `env -i` (no inherited variables);
* `localize` relocates ONLY the network path (127.0.0.1:5432, the server's own address inside its container) and the
  CA file, which is copied byte for byte from the URL's own `sslrootcert` into the work directory; host name (TLS
  verify-full name check), database, user and sslmode are unchanged;
* every process is started through `sh -c 'echo $$ > pidfile; exec ...'`, so the recorded container PID IS the client's
  PID, and `stop` kills THAT process inside the container (killing only the host's `docker exec` client would leave
  `pg_dump` running), then confirms it is gone from `/proc`.
The production `LocalClientHost` is exercised with real processes in the regression module; it is not exercised
against a real `pg_dump` here, because no host client exists.

Claims about backends, snapshots and commits come from the SERVER (the administrator's `pg_stat_activity` and
`pg_locks`, fresh committed reads). The in-flight scenarios are ordered by a deterministic barrier: an ACCESS EXCLUSIVE
request queued on `conferences` behind S's ACCESS SHARE makes `pg_dump`'s `LOCK TABLE` batch park, which the
administrator observes before acting, never by elapsed time. Offline `pg_restore --data-only` extraction of one known
synthetic marker (scenario C) is test instrumentation; no SQL from the archive is executed and nothing is restored.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import posixpath
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DBAPIError

from src.deployment.backup_capture_dump import (
    DUMP_APPLICATION_PREFIX,
    DUMP_VERIFIER_APPLICATION_NAME,
    DumpFailed,
    DumpPreconditionError,
    DumpTimeout,
    backup_capture_dump,
)
from src.deployment.backup_capture_lock import LOCK_APPLICATION_NAME, VERIFIER_APPLICATION_NAME, BackupLockNotHeld, backup_capture_lock
from src.deployment.backup_capture_snapshot import SNAPSHOT_APPLICATION_NAME, SnapshotHandleClosed, SnapshotSessionLost, backup_capture_snapshot
from src.pipeline.bulk_import_safety import compute_advisory_lock_key
from tests.postgres.capture_client_support import (
    NATIVE_TOOL_DIR,
    NativeTestClient,
    install,
    localized_evidence,
    make_test_client,
    native_mode,
    parse_conninfo,
    production_client,
)
from src.deployment.backup_capture_dump import LocalClientHost
from tests.postgres.test_postgres_backup_capture_lock import Admin, _verified, designation_for, harness, record  # noqa: F401  (harness: module fixture)
from tests.postgres.test_postgres_backup_capture_snapshot import Seam, admin_scalar, new_database
from tests.postgres.test_postgres_backup_snapshot_consistency import CHANGED_TEAM_ID, CHANGED_TEAM_SLUG, owned_case
from tests.postgres.test_postgres_roles_and_restore import ACCEPTED_ALEMBIC_HEAD, MIGRATOR_ROLE
from tests.postgres.test_postgres_tls_backup_restore import (
    BACKUP_ROLE,
    _container_id,
    isolated_database_environment,  # noqa: F401  (autouse module fixture)
)

pytestmark = pytest.mark.postgres

OWNED_APPS = (LOCK_APPLICATION_NAME, VERIFIER_APPLICATION_NAME, SNAPSHOT_APPLICATION_NAME, DUMP_VERIFIER_APPLICATION_NAME)
BARRIER_TABLE = "conferences"
BARRIER_WAIT_S = 60
OBSERVE_S = 60
SECRET_SHAPES = ("PASSWORD", "password=")


# ---------------------------------------------------------------------------
# the test-layer client host
# ---------------------------------------------------------------------------
class ContainerProcess:
    def __init__(self, host: "ContainerClientHost", popen: subprocess.Popen, pidfile: str, argv: list[str]) -> None:
        self.host, self.popen, self.pidfile, self.argv = host, popen, pidfile, argv
        self.container_pid: int | None = None
        self.killed = False
        self.gone_after_kill: bool | None = None

    def _read_pid(self) -> int | None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            out = self.host._run(["cat", self.pidfile]).stdout.decode().strip()
            if out.isdigit():
                return int(out)
            time.sleep(0.05)
        return None

    def wait(self, timeout: float) -> int | None:
        try:
            return self.popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def stop(self, timeout: float) -> tuple[bool, int | None]:
        self.container_pid = self._read_pid()
        if self.container_pid is not None and self.popen.poll() is None:
            self.host._run(["kill", "-KILL", str(self.container_pid)])  # the actual client process inside the container
            self.killed = True
        try:
            self.popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.popen.kill()
            self.popen.wait(timeout=timeout)
            return False, None
        alive = self.container_pid is not None and self.host._run(["sh", "-c", '[ -d "/proc/$1" ]', "sh", str(self.container_pid)]).returncode == 0
        self.gone_after_kill = not alive
        return (not alive), self.popen.returncode


class ContainerClientHost:
    """TEST LAYER implementation of the production `ClientHost` seam, inside the disposable cluster container (module docstring)."""

    transport = "container"
    killed_returncode = 137  # docker exec reports a SIGKILLed client as 128 + 9

    def dump_attempts(self) -> list[list[str]]:
        return [p.argv for p in self.processes if any(a.startswith("--snapshot=") for a in p.argv)]

    def __init__(self, cluster, *, pg_dump=("pg_dump",), pg_restore=("pg_restore",)) -> None:
        self.cluster, self.cid = cluster, _container_id(cluster)
        self.pg_dump, self.pg_restore = tuple(pg_dump), tuple(pg_restore)
        self.calls: list[str] = []
        self.processes: list[ContainerProcess] = []
        self.dirs: list[str] = []
        self.files: list[str] = []
        self.localized = None

    def _run(self, argv, *, stdin: bytes | None = None, timeout: float = 30) -> subprocess.CompletedProcess:
        docker = ["docker", "exec", *(["-i"] if stdin is not None else []), self.cid, *argv]
        return subprocess.run(docker, input=stdin, capture_output=True, timeout=timeout)

    def make_private_dir(self) -> str:
        self.calls.append("make_private_dir")
        path = f"/tmp/napsedge_dump_{uuid.uuid4().hex[:16]}"
        if self._run(["sh", "-c", 'umask 077 && mkdir -- "$1"', "sh", path]).returncode != 0:
            raise OSError("could not create the private directory")
        self.dirs.append(path)
        return path

    def join(self, directory: str, name: str) -> str:
        return posixpath.join(directory, name)

    def write_private_file(self, path: str, data: bytes) -> None:
        self.calls.append("write_private_file")
        if self._run(["sh", "-c", 'umask 077 && set -C && cat > "$1"', "sh", path], stdin=data).returncode != 0:
            raise OSError("could not create the private file exclusively")
        self.files.append(path)

    def file_state(self, path: str) -> tuple[str, int]:
        out = self._run(["sh", "-c", 'if [ -L "$1" ]; then echo other 0; elif [ -f "$1" ]; then echo regular "$(stat -c %s "$1")"; '
                                     'elif [ -e "$1" ]; then echo other 0; else echo absent 0; fi', "sh", path]).stdout.decode().split()
        return out[0], int(out[1])

    def read_bounded(self, path: str, limit: int) -> bytes:
        return self._run(["head", "-c", str(limit), path]).stdout

    def remove(self, path: str) -> None:
        self.calls.append("remove")
        if self._run(["rm", "-f", "--", path]).returncode != 0:
            raise OSError("could not remove the file")

    def remove_tree(self, path: str) -> None:
        self.calls.append("remove_tree")
        if self._run(["rm", "-rf", "--", path]).returncode != 0:
            raise OSError("could not remove the directory")

    def exists(self, path: str) -> bool:
        return self._run(["sh", "-c", '[ -e "$1" ] || [ -L "$1" ]', "sh", path]).returncode == 0

    def localize(self, target, workdir: str):
        ca_path = self.join(workdir, "ca.crt")
        self.write_private_file(ca_path, Path(target.sslrootcert).read_bytes())  # the same CA bytes the URL names
        self.localized = replace(target, hostaddr="127.0.0.1", port=5432, sslrootcert=ca_path)
        return self.localized

    def start(self, argv, env, stdout_path: str, stderr_path: str) -> ContainerProcess:
        self.calls.append(f"start:{argv[0]}")
        pidfile = stdout_path + ".pid"
        script = 'echo $$ > "$1"; out="$2"; err="$3"; shift 3; exec env -i PATH=/usr/local/bin:/usr/bin:/bin "$@" >"$out" 2>"$err"'
        full = ["docker", "exec", self.cid, "sh", "-c", script, "sh", pidfile, stdout_path, stderr_path, *[f"{k}={v}" for k, v in env.items()], *argv]
        popen = subprocess.Popen(full, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False)
        process = ContainerProcess(self, popen, pidfile, list(argv))
        self.processes.append(process)
        return process

    def client_processes(self) -> list[str]:
        out = self._run(["sh", "-c", "for d in /proc/[0-9]*; do c=$(tr '\\0' ' ' < $d/cmdline 2>/dev/null); "
                                     "case \"$c\" in pg_dump*|pg_restore*) echo \"${d#/proc/} $c\";; esac; done"]).stdout.decode()
        return [line for line in out.splitlines() if line.strip()]


class WrongPassfileHost(ContainerClientHost):
    """FAULT INJECTION at the client host only: the passfile pg_dump reads carries a wrong password (the module's own URL stays valid)."""

    def write_private_file(self, path: str, data: bytes) -> None:
        if path.endswith("/pgpass"):
            fields = data.decode().rstrip("\n").rsplit(":", 1)
            data = (fields[0] + ":not-the-backup-password\n").encode()
        super().write_private_file(path, data)


class OtherCaHost(ContainerClientHost):
    """FAULT INJECTION at the client host only: pg_dump is given the OTHER CA, so verify-full must refuse the server certificate."""

    def localize(self, target, workdir: str):
        return super().localize(replace(target, sslrootcert=self.cluster.other_ca_path), workdir)


# ---------------------------------------------------------------------------
# barrier and observer (test orchestration, NOT a production lock policy)
# ---------------------------------------------------------------------------
class ExclusiveBarrier:
    """A migrator's ACCESS EXCLUSIVE request on `conferences`, confirmed queued behind S's ACCESS SHARE; pg_dump's LOCK TABLE batch then parks behind it."""

    def __init__(self, cluster, database, admin, ledger) -> None:
        self.admin, self.outcome = admin, {}
        self.engine = create_engine(cluster.migrator_url(database))
        ledger.engines.append(self.engine)
        self.thread = threading.Thread(target=self._request, name="dump-barrier")
        ledger.threads.append(self.thread)

    def _request(self) -> None:
        with self.engine.connect() as conn:
            self.outcome["pid"] = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
            conn.rollback()
            conn.begin()
            conn.execute(text(f"SET LOCAL lock_timeout = '{BARRIER_WAIT_S}s'"))
            try:
                conn.execute(text(f"ALTER TABLE public.{BARRIER_TABLE} ADD COLUMN zz_barrier integer"))
                self.outcome["result"] = "acquired"
            except DBAPIError as exc:
                self.outcome["result"] = getattr(exc.orig, "sqlstate", None)
            conn.rollback()

    def start(self) -> None:
        self.thread.start()
        sql = (f"SELECT count(*) FROM pg_locks WHERE relation = 'public.{BARRIER_TABLE}'::regclass "
               "AND mode = 'AccessExclusiveLock' AND NOT granted")
        deadline = time.monotonic() + BARRIER_WAIT_S
        while time.monotonic() < deadline and not admin_scalar(self.admin, sql):
            time.sleep(0.05)
        assert admin_scalar(self.admin, sql), "the barrier request was never observed queued"

    def cancel(self) -> None:
        deadline = time.monotonic() + 10
        while "pid" not in self.outcome and time.monotonic() < deadline:
            time.sleep(0.02)
        if "pid" in self.outcome:
            admin_scalar(self.admin, "SELECT pg_cancel_backend(:p)", p=self.outcome["pid"])
        self.thread.join(timeout=BARRIER_WAIT_S + 5)


def dump_backend(admin: Admin) -> tuple | None:
    """The administrator's view of a pg_dump backend parked behind the barrier, or None."""
    with admin.engine.connect() as conn:  # every observation returns its connection to the pool
        row = conn.execute(text(
            "SELECT pid, datname, usename, application_name, backend_xmin::text, wait_event_type, left(query, 10), xact_start IS NOT NULL "
            "FROM pg_stat_activity WHERE application_name LIKE :p AND application_name <> :v AND wait_event_type = 'Lock'"),
            {"p": DUMP_APPLICATION_PREFIX.replace("_", r"\_") + "%", "v": DUMP_VERIFIER_APPLICATION_NAME}).first()
    return tuple(row) if row else None


class Observer:
    """Waits (bounded) for the dump backend to park behind the barrier, records server evidence, runs `action`, then releases the barrier."""

    def __init__(self, admin: Admin, barrier: ExclusiveBarrier, key: int, action=None) -> None:
        self.admin, self.barrier, self.key, self.action = admin, barrier, key, action
        self.seen: dict = {}
        self.thread = threading.Thread(target=self._run, name="dump-observer")

    def _run(self) -> None:
        try:
            deadline = time.monotonic() + OBSERVE_S
            while time.monotonic() < deadline and self.seen.get("dump") is None:
                self.seen["dump"] = dump_backend(self.admin)
                if self.seen["dump"] is None:
                    time.sleep(0.05)
            self.seen["lock_holders"] = self.admin.holders(self.key)
            self.seen["snapshot_sessions"] = self.admin.sessions(SNAPSHOT_APPLICATION_NAME)
            self.seen["lock_sessions"] = self.admin.sessions(LOCK_APPLICATION_NAME)
            if self.action is not None and self.seen["dump"] is not None:
                self.seen["action"] = self.action()
        finally:
            self.barrier.cancel()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.thread.join(timeout=OBSERVE_S + 30)
        return False


# ---------------------------------------------------------------------------
# fixtures and helpers
# ---------------------------------------------------------------------------
@pytest.fixture()
def case(harness, monkeypatch):
    cluster = harness.cluster
    with owned_case(cluster) as ledger:
        database = new_database(cluster, ledger)
        admin = Admin(cluster, database)
        ledger.engines.append(admin.engine)
        key = compute_advisory_lock_key(_verified(cluster, database))
        client = install(monkeypatch, make_test_client(cluster))  # NAPSEDGE_TEST_CLIENT selects the transport (capture_client_support)
        yield cluster, database, admin, key, ledger, client
        for app in OWNED_APPS:
            assert admin.sessions(app) == [], app
        assert dump_backends_any(admin) == []
        assert admin.holders(key) == []
        assert client.client_processes() == []
        assert [d for d in client.dirs if client.exists(d)] == []


def dump_backends_any(admin: Admin) -> list:
    with admin.engine.connect() as conn:
        return [r[0] for r in conn.execute(text("SELECT pid FROM pg_stat_activity WHERE application_name LIKE :p AND application_name <> :v"),
                                           {"p": DUMP_APPLICATION_PREFIX.replace("_", r"\_") + "%", "v": DUMP_VERIFIER_APPLICATION_NAME})]


def with_contexts(cluster, database, body, *, snap_seam=None):
    """L, then S, then the caller's body(lock, handle). Returns (lock, handle)."""
    url = cluster.backup_url(database)
    with backup_capture_lock(url, designation_for(database)) as lock:
        with backup_capture_snapshot(lock, url, designation_for(database), expected_alembic_head=ACCEPTED_ALEMBIC_HEAD, artifact_id=str(uuid.uuid4()),
                                     _engine_factory=snap_seam.factory if snap_seam else None) as handle:
            body(lock, handle)
    return lock, handle


def secrets_absent(cluster, *texts) -> bool:
    joined = "\n".join(str(t) for t in texts)
    return all(secret not in joined for secret in cluster.secrets_to_scrub())


def evidence_of(client: ContainerClientHost) -> dict:
    dump_processes = [p for p in client.processes if p.argv and p.argv[0] == client.pg_dump[0] and "--version" not in p.argv]
    process = dump_processes[-1] if dump_processes else None
    return {
        "transport": client.transport,
        "client_calls": client.calls,
        "dump_argv_sanitized": [a if not a.startswith("--dbname=") else "--dbname=<conninfo: host, hostaddr, port, dbname, user, sslmode, sslrootcert, application_name>"
                                for a in (process.argv if process else [])],
        "dump_exit": process.popen.returncode if process else None,
        "dump_killed_in_container": process.killed if process else None,
        "dump_container_pid": process.container_pid if process else None,
        "dump_gone_after_kill": process.gone_after_kill if process else None,
        "passfile_left": [f for f in client.files if client.exists(f)],
        "workdirs_left": [d for d in client.dirs if client.exists(d)],
        "client_processes_left": client.client_processes(),
    }


# ---------------------------------------------------------------------------
# A, B, C, J: a real dump of the active snapshot; server evidence in flight; a committed marker excluded; credentials removed
# ---------------------------------------------------------------------------
def test_a_b_c_j_real_dump_uses_the_active_snapshot_excludes_a_committed_marker_and_removes_credentials(case, record_property):
    cluster, database, admin, key, ledger, client = case
    snap_seam = Seam()
    seen: dict = {}
    writer = create_engine(cluster.migrator_url(database))
    ledger.engines.append(writer)
    control_slug = admin_scalar(admin, "SELECT sr_slug FROM teams ORDER BY team_id LIMIT 1")

    def body(lock, handle):
        seen["lock_pid"], seen["snapshot_pid"] = lock.backend_pid, handle.facts.backend_pid
        seen["snapshot_xmin"] = handle.visibility_descriptor.split(":")[0]
        seen["baseline_teams"] = {f.table: f.rows for f in handle.baseline.fingerprints}["teams"]
        with writer.begin() as conn:  # C: committed AFTER S's snapshot exists and BEFORE pg_dump starts
            conn.execute(text("INSERT INTO teams (team_id, display_name, sr_slug) VALUES (:i, 'Snapshot Change Team', :s)"),
                         {"i": CHANGED_TEAM_ID, "s": CHANGED_TEAM_SLUG})
        seen["fresh_reader_marker"] = admin_scalar(admin, "SELECT count(*) FROM teams WHERE team_id = :i", i=CHANGED_TEAM_ID)
        seen["s_marker"] = snap_seam.connections[SNAPSHOT_APPLICATION_NAME].execute(
            text("SELECT count(*) FROM teams WHERE team_id = :i"), {"i": CHANGED_TEAM_ID}).scalar_one()  # TEST seam: a SELECT in S
        barrier = ExclusiveBarrier(cluster, database, admin, ledger)
        barrier.start()
        with Observer(admin, barrier, key) as observer:
            with backup_capture_dump(handle, client=production_client(client)) as archive:
                seen["archive"] = {k: getattr(archive, k) for k in ("archive_bytes", "toc_entries", "toc_sha256", "pg_dump_version", "pg_restore_version",
                                                                     "application_name", "snapshot_id", "returncode", "inventory")}
                seen["passfile_during_body"] = [f for f in client.files if f.endswith("/pgpass") and client.exists(f)]
                workdir = posixpath.dirname(archive.archive_path)
                seen["toc_head"] = [line for line in client.read_bounded(client.join(workdir, "toc.list"), 1 << 20).decode().splitlines()
                                    if " ACL " in line or line.split(" ", 3)[-1].startswith(("TABLE public teams", "SEQUENCE SET public team_aliases"))]
                extract = client.start([*client.pg_restore, "--data-only", "--table=teams", f"--file={client.join(workdir, 'teams.sql')}", archive.archive_path],
                                       {}, client.join(workdir, "x.out"), client.join(workdir, "x.err"))  # OFFLINE extraction, instrumentation only
                seen["extract_exit"] = extract.wait(30)
                teams_sql = client.read_bounded(client.join(workdir, "teams.sql"), 1 << 20).decode()
                copy_rows = teams_sql.split("COPY public.teams", 1)[1].split("\n\\.", 1)[0].splitlines()[1:]
                seen["archive_teams_rows"] = len(copy_rows)
                seen["archive_has_marker"] = CHANGED_TEAM_SLUG in teams_sql
                seen["archive_has_control"] = control_slug in teams_sql
                seen["l_and_s_during_body"] = (admin.holders(key), admin.sessions(SNAPSHOT_APPLICATION_NAME))
        seen["observer"] = observer.seen
        seen["barrier"] = barrier.outcome

    lock, handle = with_contexts(cluster, database, body, snap_seam=snap_seam)
    archive_cleanup = None
    ev = evidence_of(client)
    dump = seen["observer"]["dump"]
    record(record_property, "A_B_C_J_real_dump", acquired=True, body_entered=True, lock_pid=seen["lock_pid"], snapshot_pid=seen["snapshot_pid"],
           snapshot_xmin=seen["snapshot_xmin"], dump_backend=dump, lock_holders_during_dump=seen["observer"]["lock_holders"],
           snapshot_sessions_during_dump=seen["observer"]["snapshot_sessions"], archive=seen["archive"], toc_selected=seen["toc_head"],
           fresh_reader_marker=seen["fresh_reader_marker"], s_marker=seen["s_marker"], baseline_teams=seen["baseline_teams"],
           extract_exit=seen["extract_exit"], archive_teams_rows=seen["archive_teams_rows"], archive_has_marker=seen["archive_has_marker"],
           archive_has_control=seen["archive_has_control"], passfile_during_body=seen["passfile_during_body"], barrier=seen["barrier"],
           localized=localized_evidence(client), production_client_type=type(production_client(client)).__name__,
           l_and_s_during_body=seen["l_and_s_during_body"], snapshot_cleanup=handle.cleanup, lock_cleanup=lock.cleanup, **ev)
    # C (checked first: the archive's own data decides whether the snapshot was used): committed (fresh reader) but absent from S and from the archive; a seeded control row is present
    assert seen["fresh_reader_marker"] == 1 and seen["s_marker"] == 0
    assert seen["extract_exit"] == 0 and seen["archive_has_control"] and not seen["archive_has_marker"]
    assert seen["archive_teams_rows"] == seen["baseline_teams"]
    # B: server evidence of the dump backend on the verified source, inside S's imported snapshot, with L and S open
    assert dump is not None, "the dump backend was never observed in flight"
    assert dump[1] == database and dump[2] == BACKUP_ROLE and dump[3] == seen["archive"]["application_name"]
    assert dump[4] == seen["snapshot_xmin"]  # its xmin is S's exported xmin: it imported S's snapshot (server evidence, not argv)
    assert dump[5] == "Lock" and dump[7] is True
    assert seen["observer"]["lock_holders"] == [(seen["lock_pid"], True)] and seen["observer"]["snapshot_sessions"] == [seen["snapshot_pid"]]
    # A: a real custom archive with the expected inventory
    assert seen["archive"]["returncode"] == 0 and seen["archive"]["archive_bytes"] > 0 and seen["archive"]["pg_dump_version"].startswith("pg_dump (PostgreSQL) 16.")
    assert seen["archive"]["inventory"]["TABLE"] == 24 and seen["archive"]["inventory"]["SEQUENCE SET"] == 13
    assert "--format=custom" in ev["dump_argv_sanitized"] and "--no-password" in ev["dump_argv_sanitized"]
    assert not any(a.startswith(("--no-owner", "--no-acl", "--data-only", "--schema-only", "--jobs", "-j", "--table", "--exclude")) for a in ev["dump_argv_sanitized"])
    # J: the passfile was gone while the archive was still in use; nothing owned is left; no secret anywhere
    assert seen["passfile_during_body"] == [] and ev["passfile_left"] == [] and ev["workdirs_left"] == [] and ev["client_processes_left"] == []
    assert secrets_absent(cluster, json.dumps(seen, default=str), json.dumps(ev, default=str))
    if native_mode():  # the real production class, real PostgreSQL 16 binaries, and the canonical source with no relocation
        assert type(production_client(client)) is LocalClientHost
        dump_argv = client.dump_attempts()[-1]
        assert dump_argv[0] == f"{NATIVE_TOOL_DIR}/pg_dump" and open(dump_argv[0], "rb").read(4) == b"\x7fELF"
        url, used = cluster.backup_url(database), parse_conninfo(dump_argv)
        assert (used["host"], used["hostaddr"], used["port"], used["dbname"], used["user"], used["sslmode"], used["sslrootcert"]) == (
            url.host, url.query["hostaddr"], str(url.port), url.database, url.username, url.query["sslmode"], url.query["sslrootcert"])


# ---------------------------------------------------------------------------
# D: an expired handle is rejected before anything is created
# ---------------------------------------------------------------------------
def test_d_an_expired_handle_is_rejected_before_any_process_or_file(case, record_property):
    cluster, database, admin, key, ledger, client = case
    lock, handle = with_contexts(cluster, database, lambda lock, handle: None)
    with pytest.raises(BaseException) as info:
        with backup_capture_dump(handle, client=production_client(client)):
            pytest.fail("the body must not run")
    record(record_property, "D_expired_handle", acquired=False, body_entered=False, exception=type(info.value).__name__, client_calls=client.calls)
    assert type(info.value) is SnapshotHandleClosed and client.calls == []


# ---------------------------------------------------------------------------
# E, H: real failures of the client (invalid snapshot, authentication, TLS, missing client)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("variant", ["invalid_snapshot", "authentication", "tls", "missing_client"])
def test_e_h_real_client_failures_fail_visibly_and_leave_nothing_owned(case, record_property, variant, monkeypatch):
    cluster, database, admin, key, ledger, client = case
    if native_mode() and variant in ("authentication", "tls"):
        pytest.skip("TRANSPORT-SPECIFIC fault injection (a ContainerClientHost subclass rewrites the passfile or CA); retained as container-transport "
                    "evidence only, never run or relabelled as LocalClientHost validation (9B-6G applicability table)")
    if variant == "missing_client":
        client = install(monkeypatch, NativeTestClient(cluster, pg_dump=("/nonexistent/pg_dump",)) if native_mode()
                         else ContainerClientHost(cluster, pg_dump=("pg_dump_not_installed",)))
    if variant == "authentication":
        client = WrongPassfileHost(cluster)
    if variant == "tls":
        client = OtherCaHost(cluster)
    seen: dict = {}

    def body(lock, handle):
        if variant == "invalid_snapshot":  # TEST instrumentation: a well-formed identifier no transaction exported
            handle._snapshot_id = "000000FF-000000FF-1"
        with pytest.raises(BaseException) as info:
            with backup_capture_dump(handle, client=production_client(client)):
                seen["entered"] = True
        seen["exception"] = info.value

    with_contexts(cluster, database, body)
    exc = seen["exception"]
    ev = evidence_of(client)
    record(record_property, f"E_H_{variant}", acquired=True, body_entered=bool(seen.get("entered")), exception=type(exc).__name__, message=str(exc),
           stage=getattr(exc, "stage", None), returncode=getattr(exc, "returncode", None), diagnostic=getattr(exc, "diagnostic", "")[:300],
           notes=getattr(exc, "__notes__", []), dump_backends_after=dump_backends_any(admin), **ev)
    expected = DumpPreconditionError if variant == "missing_client" else DumpFailed
    assert type(exc) is expected, type(exc).__name__
    assert "entered" not in seen and getattr(exc, "__notes__", []) == []
    assert ev["passfile_left"] == [] and ev["workdirs_left"] == [] and ev["client_processes_left"] == [] and dump_backends_any(admin) == []
    fragment = {"invalid_snapshot": "invalid snapshot identifier", "authentication": "password authentication failed",
                "tls": "certificate verify failed", "missing_client": None}[variant]
    if fragment:
        assert exc.stage == "dump" and exc.returncode == 1 and fragment in exc.diagnostic
    elif native_mode():  # a real FileNotFoundError from LocalClientHost's own subprocess start
        assert str(exc) == "the pg_dump client could not be started (FileNotFoundError)" and client.dump_attempts() == []
    else:
        assert str(exc) == "the pg_dump client did not report a PostgreSQL version" and "start:pg_dump_not_installed" in client.calls
        assert not any(c == "start:pg_dump" for c in client.calls)
    assert secrets_absent(cluster, str(exc), getattr(exc, "diagnostic", ""), json.dumps(ev, default=str))


# ---------------------------------------------------------------------------
# F: a deterministically blocked real dump reaches its deadline, is stopped and reaped, and leaves no backend
# ---------------------------------------------------------------------------
def test_f_a_blocked_dump_reaches_its_deadline_is_killed_inside_the_container_and_leaves_no_backend(case, record_property):
    cluster, database, admin, key, ledger, client = case
    seen: dict = {}

    def body(lock, handle):
        barrier = ExclusiveBarrier(cluster, database, admin, ledger)
        barrier.start()
        try:
            started = time.monotonic()
            with pytest.raises(BaseException) as info:
                with backup_capture_dump(handle, client=production_client(client), dump_timeout_s=8):
                    seen["entered"] = True
            seen["elapsed"] = time.monotonic() - started
            seen["exception"] = info.value
            seen["parked_before_kill"] = seen.get("parked")
            seen["backends_after"] = dump_backends_any(admin)
            seen["barrier_still_queued"] = bool(admin_scalar(admin, f"SELECT count(*) FROM pg_locks WHERE relation = 'public.{BARRIER_TABLE}'::regclass "
                                                                    "AND mode = 'AccessExclusiveLock' AND NOT granted"))
        finally:
            barrier.cancel()
        seen["barrier"] = barrier.outcome

    observer_seen: dict = {}

    def watch():
        deadline = time.monotonic() + OBSERVE_S
        while time.monotonic() < deadline and observer_seen.get("dump") is None:
            observer_seen["dump"] = dump_backend(admin)
            time.sleep(0.05)
        if observer_seen.get("dump") is not None and client.files and client.dirs:  # while pg_dump runs: the credential's real modes
            observer_seen["modes"] = {kind: client._run(["stat", "-c", "%a", path]).stdout.decode().strip()
                                      for kind, path in (("passfile", client.files[-1]), ("workdir", client.dirs[-1]))}

    watcher = threading.Thread(target=watch, name="dump-watch")
    ledger.threads.append(watcher)
    watcher.start()
    with_contexts(cluster, database, body)
    watcher.join(timeout=OBSERVE_S + 5)
    exc = seen["exception"]
    ev = evidence_of(client)
    column = admin_scalar(admin, f"SELECT count(*) FROM pg_attribute WHERE attrelid = 'public.{BARRIER_TABLE}'::regclass AND attname = 'zz_barrier'")
    record(record_property, "F_deadline", acquired=True, body_entered=bool(seen.get("entered")), exception=type(exc).__name__, message=str(exc),
           elapsed_s=round(seen["elapsed"], 2), parked_dump_backend=observer_seen.get("dump"), credential_modes=observer_seen.get("modes"), backends_after=seen["backends_after"],
           barrier_still_queued_at_return=seen["barrier_still_queued"], barrier=seen["barrier"], barrier_column_added=column,
           cleanup_notes=getattr(exc, "__notes__", []), **ev)
    assert type(exc) is DumpTimeout and "entered" not in seen
    assert observer_seen.get("dump") is not None  # it really was parked inside the server, not merely slow
    assert seen["barrier_still_queued"]  # the module stopped it while it was still blocked: nothing released it
    assert ev["dump_killed_in_container"] and ev["dump_gone_after_kill"] and ev["dump_exit"] == client.killed_returncode  # SIGKILL reached the real pg_dump
    assert observer_seen.get("modes") == {"passfile": "600", "workdir": "700"}, observer_seen.get("modes")
    assert seen["backends_after"] == [] and ev["client_processes_left"] == [] and ev["workdirs_left"] == [] and ev["passfile_left"] == []
    assert 8 <= seen["elapsed"] < 40 and column == 0


# ---------------------------------------------------------------------------
# G: loss of S or of L while a real dump is in flight prevents a successful result although pg_dump finishes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("lost", ["snapshot_session", "lock_session"])
def test_g_loss_of_s_or_l_during_a_real_dump_prevents_success_even_though_pg_dump_finishes(case, record_property, lost):
    cluster, database, admin, key, ledger, client = case
    seen: dict = {}

    def body(lock, handle):
        victim = handle.facts.backend_pid if lost == "snapshot_session" else lock.backend_pid
        barrier = ExclusiveBarrier(cluster, database, admin, ledger)
        barrier.start()

        def kill_victim():
            ended = admin_scalar(admin, "SELECT pg_terminate_backend(:p)", p=victim)  # the disposable test-owned backend only
            return {"victim": victim, "terminated": ended, "gone": admin.gone([victim])}

        with Observer(admin, barrier, key, action=kill_victim) as observer:
            try:  # recorded explicitly: an outer pytest.raises must not swallow an inner "did not raise"
                with backup_capture_dump(handle, client=production_client(client)):
                    seen["entered"] = True
            except BaseException as exc:
                seen["exception"] = exc
        seen["observer"] = observer.seen

    with pytest.raises(BaseException) as outer:
        with_contexts(cluster, database, body)
    exc = seen.get("exception")
    ev = evidence_of(client)
    record(record_property, f"G_loss_{lost}", acquired=True, body_entered=bool(seen.get("entered")), exception=type(exc).__name__, message=str(exc),
           outer_exception=type(outer.value).__name__, dump_backend_in_flight=seen["observer"].get("dump"), victim=seen["observer"].get("action"), **ev)
    assert "entered" not in seen, "a successful dump result was yielded although S or L had been lost"
    expected = SnapshotSessionLost if lost == "snapshot_session" else BackupLockNotHeld
    assert type(exc) is expected, type(exc).__name__
    assert seen["observer"]["dump"] is not None and seen["observer"]["action"]["gone"]
    assert ev["dump_exit"] == 0  # pg_dump itself finished successfully...
    assert "entered" not in seen  # ...and still no successful result was yielded
    assert ev["workdirs_left"] == [] and ev["passfile_left"] == [] and ev["client_processes_left"] == []


# ---------------------------------------------------------------------------
# K: caller-owned files, connections and transactions are untouched
# ---------------------------------------------------------------------------
def test_k_caller_owned_files_connections_and_transactions_are_untouched(case, record_property):
    cluster, database, admin, key, ledger, client = case
    caller_dir = Path(tempfile.mkdtemp(prefix="napsedge_caller_"))
    ledger.directories.append(caller_dir)
    sentinel = caller_dir / "caller_sentinel.txt"
    sentinel.write_text("caller data\n", encoding="utf-8")
    if native_mode():  # the client's files live in this runner: a caller-owned file beside the other one
        container_sentinel = str(caller_dir / f"client_side_sentinel_{uuid.uuid4().hex[:8]}")
    else:
        container_sentinel = f"/tmp/napsedge_caller_sentinel_{uuid.uuid4().hex[:8]}"
        ledger.container_files.append(container_sentinel)
    client._run(["sh", "-c", 'echo caller > "$1"', "sh", container_sentinel])
    before = (hashlib.sha256(sentinel.read_bytes()).hexdigest(), client._run(["cat", container_sentinel]).stdout)
    caller_engine = create_engine(cluster.backup_url(database))
    ledger.engines.append(caller_engine)
    events: list = []
    for kind in ("begin", "commit", "rollback"):
        event.listen(caller_engine, kind, lambda conn, kind=kind: events.append(kind))
    caller = caller_engine.connect()
    ledger.connections.append(caller)
    caller.begin()
    events.clear()
    caller.execute(text("SET LOCAL lock_timeout = '3s'"))
    caller_pid, started = caller.execute(text("SELECT pg_backend_pid(), now()")).one()

    def body(lock, handle):
        with backup_capture_dump(handle, client=production_client(client)) as archive:
            pass

    with_contexts(cluster, database, body)
    after = (hashlib.sha256(sentinel.read_bytes()).hexdigest(), client._run(["cat", container_sentinel]).stdout)
    same_txn = caller.execute(text("SELECT now()")).scalar_one() == started
    setting = caller.execute(text("SELECT current_setting('lock_timeout')")).scalar_one()
    state = admin.session_row(caller_pid)
    record(record_property, "K_caller_untouched", acquired=True, body_entered=True, caller_files_unchanged=before == after, same_transaction=same_txn,
           caller_setting=setting, caller_state=state, caller_events=events, caller_dir_listing=sorted(p.name for p in caller_dir.iterdir()))
    assert before == after and [n for n in sorted(p.name for p in caller_dir.iterdir()) if not n.startswith("client_side_sentinel_")] == ["caller_sentinel.txt"]
    assert same_txn and setting == "3s" and events == [] and state[2] == "idle in transaction"
    caller.rollback()
