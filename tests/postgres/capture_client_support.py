"""
Phase 9B-6G: test-only selection of the client transport for the capture tests (dump, catalog, round trip).

`NAPSEDGE_TEST_CLIENT` is a TEST-ONLY environment setting; production code never reads it.
* unset or `container` (the historical default, unchanged): `ContainerClientHost` runs the PostgreSQL client tools inside
  the disposable cluster's container through `docker exec` (9B-6D/E/F).
* `local`: the production `LocalClientHost` class itself, instantiated as is, is what `backup_capture_dump` receives.
  Its real PostgreSQL 16 executables run through its own `shell=False` subprocess path, in the Linux runner the
  tests run in. Nothing relocates the network path, certificate path or credentials:
  `launched_cluster` builds ONE canonical URL for the runner (host `localhost` for verify-full, `hostaddr` = the cluster
  container's bridge address, port 5432, the runner-local CA file), and L, S, the observers and `pg_dump` all receive it.

`NativeTestClient` is the TEST-SIDE companion in local mode. It does NOT implement or override the production
`ClientHost` behavior and is never passed to production code (`production_client` returns the real `LocalClientHost`).
It gives the existing test assertions their inspection points:
* local shell commands (`_run`);
* the process evidence of what `LocalClientHost` actually launched, from a recording `subprocess.Popen` subclass that
  records the argv, environment, PID and return code and otherwise behaves exactly like `Popen`;
* local `/proc` scans;
* test-instrumentation processes (the restore, offline extraction) started with the same pattern `LocalClientHost`
  uses: `shell=False` and a new session, killed by process group.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from src.deployment.backup_capture_dump import LocalClientHost, _conninfo_value

SELECTION_VARIABLE = "NAPSEDGE_TEST_CLIENT"
NATIVE_TOOL_DIR = "/usr/lib/postgresql/16/bin"
_REAL_POPEN = subprocess.Popen


def native_mode() -> bool:
    value = os.environ.get(SELECTION_VARIABLE, "container")
    if value not in ("container", "local"):
        raise RuntimeError(f"{SELECTION_VARIABLE} must be 'container' or 'local'")
    return value == "local"


class RecordedProcess:
    """One process that LocalClientHost (or test instrumentation) actually launched, as the OS saw it."""

    def __init__(self, popen: subprocess.Popen, argv: list[str], env: dict | None) -> None:
        self.popen, self.argv, self.env = popen, argv, env or {}
        self.container_pid = popen.pid  # the local PID (the attribute name is shared with the container transport's evidence)
        self._killed: bool | None = None

    @property
    def killed(self) -> bool | None:
        """Instrumentation processes: whether THIS helper sent SIGKILL. Production-launched ones: the OS-reported end by SIGKILL (-9)."""
        if self._killed is not None:
            return self._killed
        return None if self.popen.returncode is None else self.popen.returncode == NativeTestClient.killed_returncode

    @property
    def gone_after_kill(self) -> bool | None:
        if self.popen.returncode is None:
            return None
        return not os.path.exists(f"/proc/{self.popen.pid}")

    def wait(self, timeout: float) -> int | None:
        try:
            return self.popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def stop(self, timeout: float) -> tuple[bool, int | None]:
        """Test instrumentation only (restore, extraction): the same process-group kill pattern LocalClientHost uses."""
        if self.popen.poll() is None:
            os.killpg(self.popen.pid, 9)  # SIGKILL; local mode runs only on Linux
            self._killed = True
        self.popen.wait(timeout=timeout)
        return (not os.path.exists(f"/proc/{self.popen.pid}")), self.popen.returncode


class NativeTestClient:
    transport = "local"
    killed_returncode = -9  # a process ended by SIGKILL (9), as subprocess reports it on Linux (docker exec reports 137); a literal so this imports on Windows

    def __init__(self, cluster, *, pg_dump=None, pg_restore=None) -> None:
        self.cluster = cluster
        self.local = LocalClientHost(pg_dump=tuple(pg_dump or (f"{NATIVE_TOOL_DIR}/pg_dump",)),
                                     pg_restore=tuple(pg_restore or (f"{NATIVE_TOOL_DIR}/pg_restore",)))
        self.pg_dump, self.pg_restore = self.local.pg_dump, self.local.pg_restore
        self._recorded: list[RecordedProcess] = []
        self._instrumentation: list[RecordedProcess] = []
        self.localized = None  # no client-side relocation exists in local mode
        client = self

        class RecordingPopen(_REAL_POPEN):
            def __init__(self, args, *a, **kw):
                argv = [str(x) for x in args] if isinstance(args, (list, tuple)) else [str(args)]
                tool = argv and (argv[0] in client.pg_dump[:1] or argv[0] in client.pg_restore[:1])
                if tool:
                    client._attempts.append(argv)
                super().__init__(args, *a, **kw)
                if tool:
                    client._recorded.append(RecordedProcess(self, argv, kw.get("env")))

        self._attempts: list[list[str]] = []
        self.popen_class = RecordingPopen

    # -- the production object, and the inspection points ---------------------------------------------------------------
    @property
    def production(self) -> LocalClientHost:
        return self.local

    @property
    def processes(self) -> list[RecordedProcess]:
        return [*self._recorded, *self._instrumentation]

    @property
    def calls(self) -> list[str]:
        return [f"start:{os.path.basename(argv[0])}" for argv in self._attempts]

    @property
    def files(self) -> list[str]:
        return [p.env["PGPASSFILE"] for p in self._recorded if "PGPASSFILE" in p.env]

    @property
    def dirs(self) -> list[str]:
        found = []
        for p in self._recorded:
            for a in p.argv:
                if a.startswith("--file=") and os.path.basename(os.path.dirname(a[7:])).startswith("napsedge_dump_"):
                    found.append(os.path.dirname(a[7:]))
        return sorted(set(found))

    def dump_attempts(self) -> list[list[str]]:
        return [argv for argv in self._attempts if any(a.startswith("--snapshot=") for a in argv)]

    # -- test-side helpers with the container transport's signatures ---------------------------------------------------
    def _run(self, argv, *, stdin: bytes | None = None, timeout: float = 30) -> subprocess.CompletedProcess:
        return subprocess.run([str(a) for a in argv], input=stdin, capture_output=True, timeout=timeout)

    def join(self, directory: str, name: str) -> str:
        return self.local.join(directory, name)

    def file_state(self, path: str):
        return self.local.file_state(path)

    def read_bounded(self, path: str, limit: int) -> bytes:
        return self.local.read_bounded(path, limit) if os.path.exists(path) else b""

    def exists(self, path: str) -> bool:
        return self.local.exists(path)

    def start(self, argv, env, stdout_path: str, stderr_path: str) -> RecordedProcess:
        """Test instrumentation (restore, offline extraction): not the production path."""
        environment = {k: v for k, v in os.environ.items() if not k.upper().startswith("PG")}
        environment.update(env)
        with open(stdout_path, "xb") as out, open(stderr_path, "xb") as err:
            popen = _REAL_POPEN([str(a) for a in argv], stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=environment,
                                shell=False, start_new_session=True)
        process = RecordedProcess(popen, list(argv), env)
        self._instrumentation.append(process)
        return process

    def client_processes(self) -> list[str]:
        found = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if cmdline and os.path.basename(cmdline[0].decode(errors="replace")) in ("pg_dump", "pg_restore"):
                found.append(f"{entry.name} {b' '.join(cmdline).decode(errors='replace').strip()}")
        return found


def make_test_client(cluster, **kw):
    if native_mode():
        return NativeTestClient(cluster, **kw)
    from tests.postgres.test_postgres_backup_capture_dump import ContainerClientHost

    return ContainerClientHost(cluster, **kw)


def production_client(client):
    """What production code receives: the real LocalClientHost in local mode; the container host otherwise."""
    return client.production if isinstance(client, NativeTestClient) else client


def private_scratch_dir(client) -> str:
    """A fresh 0700 directory where the client's files live (container /tmp, or the runner's /tmp)."""
    if isinstance(client, NativeTestClient):
        return tempfile.mkdtemp(prefix="napsedge_roundtrip_")
    path = f"/tmp/napsedge_roundtrip_{uuid.uuid4().hex[:16]}"
    assert client._run(["sh", "-c", 'umask 077 && mkdir -- "$1"', "sh", path]).returncode == 0
    return path


def write_role_pgpass(case, role: str, password: str) -> str:
    """A private pgpass for test instrumentation (the restore): the existing in-container helper, or a local 0600 file."""
    if isinstance(case.client, NativeTestClient):
        path = os.path.join(case.container_dir, f"pgpass_{uuid.uuid4().hex[:8]}")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, f"*:*:*:{role}:{password}\n".encode())
        finally:
            os.close(fd)
        return path
    from tests.postgres.test_postgres_tls_backup_restore import write_pgpass

    path = write_pgpass(case.cluster, [(role, password)])
    case.ledger.container_files.append(path)
    return path


def remove_role_pgpass(case, path: str) -> None:
    if isinstance(case.client, NativeTestClient):
        if os.path.exists(path):
            os.remove(path)
        return
    from tests.postgres.test_postgres_tls_backup_restore import remove_files

    remove_files(case.cluster, path)


def restore_conninfo(case, user: str, database: str) -> str:
    """The restore's libpq target: the container-internal conninfo, or the runner's canonical URL fields (no relocation)."""
    if isinstance(case.client, NativeTestClient):
        url = case.cluster.admin_url
        parts = {"host": url.host, "hostaddr": url.query["hostaddr"], "port": str(url.port), "dbname": database, "user": user,
                 "sslmode": url.query["sslmode"], "sslrootcert": url.query["sslrootcert"], "connect_timeout": "15"}
        return " ".join(f"{k}={_conninfo_value(v)}" for k, v in parts.items())  # the production libpq quoting rule
    from tests.postgres.test_postgres_tls_backup_restore import conninfo

    return conninfo(case.cluster, user, database)


def canonical_source(cluster) -> dict:
    """The canonical source configuration, without credentials (evidence)."""
    url = cluster.admin_url
    return {"host": url.host, "hostaddr": url.query.get("hostaddr"), "port": url.port, "sslmode": url.query.get("sslmode"),
            "sslrootcert": url.query.get("sslrootcert"), "transport": "local" if native_mode() else "container"}


def parse_conninfo(argv: list[str]) -> dict:
    """The libpq keywords of a recorded `--dbname=` argument (values single-quoted by the production quoting rule)."""
    import re

    dbname = next(a[len("--dbname="):] for a in argv if a.startswith("--dbname="))
    pairs = re.findall(r"(\w+)='((?:[^'\\]|\\.)*)'", dbname)
    return {key: re.sub(r"\\(.)", r"\1", value) for key, value in pairs}  # undo the production rule: backslash escapes ' and \


def localized_evidence(client) -> dict:
    """What the client did to the target: the container transport's recorded relocation, or, locally, the exact recorded invocation."""
    if isinstance(client, NativeTestClient):
        attempts = client.dump_attempts()
        values = parse_conninfo(attempts[-1]) if attempts else {}
        return {"relocation": "none", **{k: values.get(k) for k in ("host", "hostaddr", "port", "dbname", "user", "sslmode", "sslrootcert")}}
    t = client.localized
    return {"relocation": "network path and CA file (test-layer container transport)", "host": t.host, "hostaddr": t.hostaddr, "port": t.port,
            "dbname": t.dbname, "user": t.user, "sslmode": t.sslmode}


def install(monkeypatch, client):
    """Local mode: the recording Popen subclass is what `subprocess.Popen` resolves to for the duration of one test."""
    if isinstance(client, NativeTestClient):
        monkeypatch.setattr(subprocess, "Popen", client.popen_class)
    return client
