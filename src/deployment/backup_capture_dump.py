"""
Phase 9B-6D: a real `pg_dump` consumer of the snapshot handle, writing only to an owned temporary directory
(docs/backup_artifact_contract_proposal.md section 13.12).

`backup_capture_dump(handle, *, client)` is a context manager used INSIDE an active `backup_capture_snapshot` context
(itself inside `backup_capture_lock`). It runs one PostgreSQL 16 `pg_dump --format=custom --snapshot=<the handle's
exported id> --no-password` against the handle's own validated source, as the same backup role, while S's transaction
stays open and L still holds the lock. It checks the result offline, and yields a `DumpArchive` whose file exists only
for the body of the context. It never publishes anything. Successful completion means only that this dump procedure
completed and its basic archive checks passed. It does NOT establish artifact consistency, data correctness,
recoverability or recovery readiness.

Where the client runs: the `ClientHost` seam. One object says how the PostgreSQL client tools are launched and where the
owned work directory lives. `LocalClientHost` (production) runs local executables with `shell=False`, in their own
process group (POSIX) or process group (Windows), with an environment from which every `PG*` variable has been removed,
so inherited libpq settings cannot override the validated source or TLS. A host may `localize` only the network path
(`hostaddr`, port) and the location of the same CA file for where the client runs; host name, database, user, sslmode
and TLS verification never change. The production host localizes nothing.

What this module creates and owns, and the only things it cleans up:
* one private work directory, made by the host before anything else;
* inside it, exactly: the passfile, the archive, the client's stdout/stderr capture files, the table-of-contents file
  and the version-check outputs;
* the client processes it starts (the version checks, `pg_dump`, `pg_restore --list`), each bounded;
* the dump's own server backend, identified by a unique `application_name` (`napsedge_backup_dump_<hex>`), the backup
  role and the database. When the client process had to be stopped, that backend, and no other, is ended with
  `pg_terminate_backend`, which needs no privilege beyond being the same role. Other backends are never signalled;
* one short-lived, validated verifier session (`NullPool`, the lock module's engine builder) that observes the backend.

Order: the handle is confirmed active (`confirm_active()`: L holds the key, and S is the same backend, snapshot and
transaction) BEFORE anything is created. The URL is translated to libpq keywords: only `sslmode` (which must be
`verify-full`), `sslrootcert`, `hostaddr` and `connect_timeout` are accepted, and anything else is refused, never
dropped. Then, in order: the work directory; both client versions (major 16 required); the passfile (escaped, created
exclusively, private); a re-confirmation of the handle; launch; a bounded wait. Then the passfile is removed, the exit
status is checked, the handle is re-confirmed after completion, the archive must be a non-empty regular file, and an
OFFLINE `pg_restore --list --file=...` (never connected to a database) must list exactly the baseline's tables, table
data, sequences, sequence values, owners and ACL-bearing relations. A readable table of contents is a structural check,
not data verification.

Exit: a still-running client is stopped and reaped (and its backend ended), the dump backend's absence is observed with
a deadline, the passfile is removed if still present, and the work directory is removed and confirmed absent. Every
step is attempted even if an earlier one failed. A primary exception propagates as the same object with at most one
bounded note. Without one, unconfirmed cleanup raises `DumpCleanupError`. A new interrupt raised during cleanup
propagates after the remaining steps. The password reaches only the passfile: never argv, the environment, messages,
notes or diagnostics. Messages and notes written by this module are fixed text plus type names, status codes or counts.
`DumpFailed.diagnostic` holds at most 2 KiB of the client's stderr with the password removed.

Sequence values in the archive are whatever `pg_dump` read when it read them; the exported data snapshot does not freeze
them (specification 6.5). Boundary checks of L and S are point observations and do not establish continuous ownership.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Callable, Iterator, Mapping, Protocol, Sequence

from sqlalchemy import text
from sqlalchemy.engine import URL, Connection, Engine

from src.deployment.backup_artifact import BaselineDocument
from src.deployment.backup_capture_lock import _build_engine, _require_null_pool, _type_name
from src.deployment.backup_capture_snapshot import SnapshotHandle
from src.exceptions import NapsEdgeError, ValidationError
from src.pipeline.bulk_import_safety import DatabaseTargetDesignation, verify_database_target

__all__ = [
    "DUMP_APPLICATION_PREFIX",
    "DUMP_VERIFIER_APPLICATION_NAME",
    "SUPPORTED_CLIENT_MAJOR",
    "BACKEND_GONE",
    "BACKEND_PRESENT",
    "BACKEND_NOT_VERIFIED",
    "BACKEND_NOT_STARTED",
    "LibpqTarget",
    "ClientProcess",
    "ClientHost",
    "LocalClientHost",
    "DumpPreconditionError",
    "DumpFailed",
    "DumpTimeout",
    "DumpCleanupError",
    "DumpCleanupReport",
    "DumpArchive",
    "backup_capture_dump",
]

DUMP_APPLICATION_PREFIX = "napsedge_backup_dump_"
DUMP_VERIFIER_APPLICATION_NAME = "napsedge_backup_dump_verify"
SUPPORTED_CLIENT_MAJOR = 16

BACKEND_GONE = "gone"
BACKEND_PRESENT = "present"
BACKEND_NOT_VERIFIED = "not_verified"
BACKEND_NOT_STARTED = "not_started"

_ALLOWED_QUERY = frozenset({"sslmode", "sslrootcert", "hostaddr", "connect_timeout"})
_EXPORTED_ID = re.compile(r"[0-9A-F]{8}-[0-9A-F]{8}-[0-9]+")
_VERSION = re.compile(r"^(pg_dump|pg_restore) \(PostgreSQL\) ([0-9]+)(\.[0-9]+)?\b")
_TOC_ENTRY = re.compile(r"^[0-9]+; [0-9]+ [0-9]+ (?P<rest>.+)$")
_DEFAULT_DUMP_TIMEOUT_S = 600.0
_DEFAULT_TOOL_TIMEOUT_S = 30.0
_REAP_TIMEOUT_S = 15.0
_BACKEND_DEADLINE_S = 10.0
_POLL_S = 0.05
_STDERR_LIMIT = 2048
_TOC_LIMIT = 4 * 1024 * 1024
_VERSION_LIMIT = 4096

_BACKEND_COUNT_SQL = (
    "SELECT count(*) FROM pg_stat_activity WHERE application_name = :app AND usename = current_user AND datname = current_database()"
)
_BACKEND_END_SQL = (
    "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
    "WHERE application_name = :app AND usename = current_user AND datname = current_database() AND pid <> pg_backend_pid()"
)


# ---------------------------------------------------------------------------
# errors and results
# ---------------------------------------------------------------------------
class DumpPreconditionError(NapsEdgeError):
    """Refused before any client process was launched (unsupported parameters, client version, handle state)."""


class DumpFailed(NapsEdgeError):
    """The dump procedure did not complete successfully. `stage` and `returncode` are facts; `diagnostic` is bounded, password-free stderr."""

    def __init__(self, message: str, *, stage: str, returncode: int | None = None, diagnostic: str = "") -> None:
        super().__init__(message)
        self.stage, self.returncode, self.diagnostic = stage, returncode, diagnostic


class DumpTimeout(DumpFailed):
    """The client exceeded its deadline and was stopped."""


class DumpCleanupError(NapsEdgeError):
    """Cleanup of owned processes, files or the dump backend could not be confirmed and no other failure was propagating."""

    def __init__(self, message: str, report: "DumpCleanupReport") -> None:
        super().__init__(message)
        self.report = report


@dataclass(frozen=True)
class DumpCleanupReport:
    process_started: bool
    process_stopped_by_module: bool
    process_reaped: bool
    backend: str
    backend_end_requested: bool
    passfile_removed: bool
    workdir_removed: bool
    failures: tuple[str, ...]

    @property
    def confirmed(self) -> bool:
        return (not self.failures and self.workdir_removed and self.passfile_removed
                and self.backend in (BACKEND_GONE, BACKEND_NOT_STARTED) and (self.process_reaped or not self.process_started))


class DumpArchive:
    """The temporary archive and its facts. `archive_path` raises `DumpPreconditionError` after exit, when the file is gone; `cleanup` is set then."""

    __slots__ = ("_open", "_path", "archive_bytes", "toc_entries", "toc_sha256", "pg_dump_version", "pg_restore_version",
                 "application_name", "snapshot_id", "returncode", "inventory", "cleanup")

    def __init__(self, path: str, **facts) -> None:
        self._open, self._path = True, path
        for name, value in facts.items():
            setattr(self, name, value)
        self.cleanup: DumpCleanupReport | None = None

    @property
    def archive_path(self) -> str:
        """The archive inside the owned work directory; valid only while the context is open, removed on exit."""
        if not self._open:
            raise DumpPreconditionError("the dump archive was used after its context exited")
        return self._path


# ---------------------------------------------------------------------------
# libpq translation and credentials
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LibpqTarget:
    host: str
    hostaddr: str | None
    port: int
    dbname: str
    user: str
    sslmode: str
    sslrootcert: str
    connect_timeout: str | None


def _libpq_target(url: URL) -> tuple[LibpqTarget, str]:
    """Exactly the validated URL, as libpq keywords. Anything libpq would receive differently, or not at all, is refused."""
    unsupported = sorted(set(url.query) - _ALLOWED_QUERY)
    if unsupported:
        raise DumpPreconditionError(f"unsupported connection parameters for pg_dump: {', '.join(unsupported)}")
    query = {}
    for key, value in url.query.items():
        if not isinstance(value, str):
            raise DumpPreconditionError(f"connection parameter {key} is repeated")
        query[key] = value
    if query.get("sslmode") != "verify-full" or not query.get("sslrootcert"):
        raise DumpPreconditionError("pg_dump requires sslmode=verify-full with an explicit sslrootcert")
    if not url.host or not url.database or not url.username or url.password is None:
        raise DumpPreconditionError("pg_dump requires an explicit host, database, user and password source")
    timeout = query.get("connect_timeout")
    if timeout is not None and not timeout.isdigit():
        raise DumpPreconditionError("connect_timeout must be a whole number of seconds")
    target = LibpqTarget(host=url.host, hostaddr=query.get("hostaddr"), port=url.port or 5432, dbname=url.database, user=url.username,
                         sslmode=query["sslmode"], sslrootcert=query["sslrootcert"], connect_timeout=timeout)
    for value in (target.host, target.hostaddr or "", target.dbname, target.user, target.sslrootcert, str(url.password)):
        if any(ch in value for ch in "\r\n\x00"):
            raise DumpPreconditionError("a connection value contains a line break or NUL")
    return target, str(url.password)


def _conninfo_value(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _conninfo(target: LibpqTarget, application_name: str) -> str:
    parts = [("host", target.host)]
    if target.hostaddr:
        parts.append(("hostaddr", target.hostaddr))
    parts += [("port", str(target.port)), ("dbname", target.dbname), ("user", target.user), ("sslmode", target.sslmode),
              ("sslrootcert", target.sslrootcert), ("application_name", application_name)]
    if target.connect_timeout:
        parts.append(("connect_timeout", target.connect_timeout))
    return " ".join(f"{key}={_conninfo_value(value)}" for key, value in parts)


def _passfile_field(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:")


def _passfile_bytes(target: LibpqTarget, password: str) -> bytes:
    fields = (target.host, str(target.port), target.dbname, target.user, password)
    return (":".join(_passfile_field(f) for f in fields) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# where the client runs
# ---------------------------------------------------------------------------
class ClientProcess(Protocol):
    def wait(self, timeout: float) -> int | None: ...

    def stop(self, timeout: float) -> tuple[bool, int | None]: ...


class ClientHost(Protocol):
    pg_dump: Sequence[str]
    pg_restore: Sequence[str]

    def make_private_dir(self) -> str: ...

    def join(self, directory: str, name: str) -> str: ...

    def write_private_file(self, path: str, data: bytes) -> None: ...

    def file_state(self, path: str) -> tuple[str, int]: ...

    def read_bounded(self, path: str, limit: int) -> bytes: ...

    def remove(self, path: str) -> None: ...

    def remove_tree(self, path: str) -> None: ...

    def exists(self, path: str) -> bool: ...

    def localize(self, target: LibpqTarget, workdir: str) -> LibpqTarget: ...

    def start(self, argv: Sequence[str], env: Mapping[str, str], stdout_path: str, stderr_path: str) -> ClientProcess: ...


class _LocalProcess:
    def __init__(self, popen: subprocess.Popen) -> None:
        self._popen = popen

    def wait(self, timeout: float) -> int | None:
        try:
            return self._popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def stop(self, timeout: float) -> tuple[bool, int | None]:
        if self._popen.poll() is None:
            if os.name == "posix":
                os.killpg(self._popen.pid, signal.SIGKILL)
            else:
                self._popen.kill()
        try:
            self._popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, None
        return True, self._popen.returncode


@dataclass(frozen=True)
class LocalClientHost:
    """Runs local PostgreSQL client executables. `pg_dump`/`pg_restore` are argv prefixes (normally one absolute path each)."""

    pg_dump: tuple[str, ...] = ("pg_dump",)
    pg_restore: tuple[str, ...] = ("pg_restore",)

    def make_private_dir(self) -> str:
        path = tempfile.mkdtemp(prefix="napsedge_dump_")  # 0700 on POSIX; on Windows it inherits the per-user temp directory's ACL
        if os.name == "posix" and stat.S_IMODE(os.lstat(path).st_mode) != 0o700:
            shutil.rmtree(path)
            raise DumpPreconditionError("the private work directory is not private")
        return path

    def join(self, directory: str, name: str) -> str:
        return os.path.join(directory, name)

    def write_private_file(self, path: str, data: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)

    def file_state(self, path: str) -> tuple[str, int]:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return "absent", 0
        if stat.S_ISREG(info.st_mode):
            return "regular", info.st_size
        return "other", 0

    def read_bounded(self, path: str, limit: int) -> bytes:
        with open(path, "rb") as handle:
            return handle.read(limit)

    def remove(self, path: str) -> None:
        if self.file_state(path)[0] != "absent":
            os.remove(path)

    def remove_tree(self, path: str) -> None:
        if os.path.lexists(path):
            shutil.rmtree(path)

    def exists(self, path: str) -> bool:
        return os.path.lexists(path)

    def localize(self, target: LibpqTarget, workdir: str) -> LibpqTarget:
        return target

    def start(self, argv: Sequence[str], env: Mapping[str, str], stdout_path: str, stderr_path: str) -> ClientProcess:
        environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("PG")}
        environment.update(env)
        group = {"start_new_session": True} if os.name == "posix" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        with open(stdout_path, "xb") as out, open(stderr_path, "xb") as err:
            popen = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=environment, shell=False, **group)
        return _LocalProcess(popen)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _sanitized(raw: bytes, password: str) -> str:
    textual = raw.decode("utf-8", errors="replace")
    return textual.replace(password, "[removed]") if password else textual


def _client_version(client: ClientHost, tool: Sequence[str], name: str, workdir: str, timeout: float) -> str:
    out, err = client.join(workdir, f"{name}.version.out"), client.join(workdir, f"{name}.version.err")
    try:
        process = client.start([*tool, "--version"], {}, out, err)
    except Exception as exc:  # noqa: BLE001 -- a missing executable is a precondition failure, reported by type only
        raise DumpPreconditionError(f"the {name} client could not be started ({_type_name(exc)})") from None
    returncode = process.wait(timeout)
    if returncode is None:
        process.stop(_REAP_TIMEOUT_S)
        raise DumpPreconditionError(f"the {name} version check exceeded its deadline")
    version = client.read_bounded(out, _VERSION_LIMIT).decode("utf-8", errors="replace").strip()
    match = _VERSION.match(version)
    if returncode != 0 or match is None or match.group(1) != name:
        raise DumpPreconditionError(f"the {name} client did not report a PostgreSQL version")
    if int(match.group(2)) != SUPPORTED_CLIENT_MAJOR:
        raise DumpPreconditionError(f"the {name} client is major {match.group(2)}, not {SUPPORTED_CLIENT_MAJOR}")
    return version.splitlines()[0]


def _toc_inventory(toc: str) -> dict[str, set]:
    found: dict[str, set] = {"TABLE": set(), "TABLE DATA": set(), "SEQUENCE": set(), "SEQUENCE SET": set(), "ACL": set(), "OWNERS": set()}
    for line in toc.splitlines():
        match = _TOC_ENTRY.match(line)
        if match is None:
            continue
        tokens = match.group("rest").split(" ")
        for kind in ("TABLE DATA", "SEQUENCE SET", "TABLE", "SEQUENCE"):
            words = kind.split(" ")
            if tokens[: len(words)] == words and len(tokens) == len(words) + 3 and tokens[len(words)] == "public":
                name, owner = tokens[len(words) + 1], tokens[len(words) + 2]
                found[kind].add(name)
                if kind in ("TABLE", "SEQUENCE"):
                    found["OWNERS"].add((name, owner))
                break
        else:
            if tokens[:2] == ["ACL", "public"] and len(tokens) >= 5 and tokens[2] in ("TABLE", "SEQUENCE"):
                found["ACL"].add(tokens[3])
    return found


def _expected_inventory(baseline: BaselineDocument) -> dict[str, set]:
    tables = {entry.table for entry in baseline.fingerprints}
    sequences = {entry.name for entry in baseline.sequence_details}
    return {
        "TABLE": tables,
        "TABLE DATA": tables,
        "SEQUENCE": sequences,
        "SEQUENCE SET": sequences,
        "ACL": {acl.relation for acl in baseline.acls if acl.acl},
        "OWNERS": {(acl.relation, acl.owner) for acl in baseline.acls},
    }


def _observe_backend(engine_factory: Callable[[URL, str], Engine], url: URL, designation: DatabaseTargetDesignation,
                     application_name: str, end_it: bool) -> tuple[str, list[str]]:
    """A fresh validated session: optionally ends THIS dump's own backend, then polls with a deadline until it is absent."""
    failures: list[str] = []
    engine: Engine | None = None
    connection: Connection | None = None
    try:
        engine = engine_factory(url, DUMP_VERIFIER_APPLICATION_NAME)
        _require_null_pool(engine)
        connection = engine.connect()
        verify_database_target(connection, designation)
        connection.rollback()
        if end_it:
            connection.execute(text(_BACKEND_END_SQL), {"app": application_name}).scalar_one()
            connection.rollback()
        deadline = time.monotonic() + _BACKEND_DEADLINE_S
        while True:
            present = connection.execute(text(_BACKEND_COUNT_SQL), {"app": application_name}).scalar_one()
            connection.rollback()  # pg_stat_activity is cached per transaction
            if not present:
                return BACKEND_GONE, failures
            if time.monotonic() >= deadline:
                return BACKEND_PRESENT, failures
            time.sleep(_POLL_S)
    except Exception as exc:  # noqa: BLE001 -- inability to observe is reported, never success
        failures.append(f"backend_verify:{_type_name(exc)}")
        return BACKEND_NOT_VERIFIED, failures
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception as exc:  # noqa: BLE001
                failures.append(f"backend_verify_close:{_type_name(exc)}")
        if engine is not None:
            try:
                engine.dispose()
            except Exception as exc:  # noqa: BLE001
                failures.append(f"backend_verify_dispose:{_type_name(exc)}")


# ---------------------------------------------------------------------------
# the context manager
# ---------------------------------------------------------------------------
@contextmanager
def backup_capture_dump(handle: SnapshotHandle, *, client: ClientHost, dump_timeout_s: float = _DEFAULT_DUMP_TIMEOUT_S,
                        tool_timeout_s: float = _DEFAULT_TOOL_TIMEOUT_S,
                        _engine_factory: Callable[[URL, str], Engine] | None = None) -> Iterator[DumpArchive]:
    """See the module docstring. `_engine_factory` is a test seam for the verifier session; it must return a NullPool engine."""
    if not isinstance(handle, SnapshotHandle):
        raise ValidationError("handle must be the SnapshotHandle of an active backup_capture_snapshot context")
    for name in ("pg_dump", "pg_restore", "make_private_dir", "join", "write_private_file", "file_state", "read_bounded", "remove",
                 "remove_tree", "exists", "localize", "start"):
        if not hasattr(client, name):
            raise ValidationError("client must provide the ClientHost operations")
    for value in (dump_timeout_s, tool_timeout_s):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
            raise ValidationError("timeouts must be positive numbers of seconds")

    handle.confirm_active()  # an expired handle, a lost L or a lost S: nothing is created
    url, designation = handle._validated_source()
    target, password = _libpq_target(url)
    snapshot_id = handle.snapshot_id
    if not _EXPORTED_ID.fullmatch(snapshot_id):
        raise DumpPreconditionError("the handle's snapshot identifier is not an exported snapshot identifier")
    baseline = handle.baseline
    application_name = f"{DUMP_APPLICATION_PREFIX}{uuid.uuid4().hex[:16]}"
    factory = _engine_factory or _build_engine

    workdir: str | None = None
    passfile: str | None = None
    process: ClientProcess | None = None
    returncode: int | None = None
    archive: DumpArchive | None = None
    primary: BaseException | None = None
    try:
        workdir = client.make_private_dir()
        dump_version = _client_version(client, client.pg_dump, "pg_dump", workdir, tool_timeout_s)
        restore_version = _client_version(client, client.pg_restore, "pg_restore", workdir, tool_timeout_s)
        localized = client.localize(target, workdir)
        if replace(localized, hostaddr=target.hostaddr, port=target.port, sslrootcert=target.sslrootcert) != target:
            raise DumpPreconditionError("the client host may relocate only the network path and the CA file")
        passfile = client.join(workdir, "pgpass")
        client.write_private_file(passfile, _passfile_bytes(localized, password))
        archive_path = client.join(workdir, "archive.dump")
        if client.file_state(archive_path)[0] != "absent":
            raise DumpPreconditionError("the archive path already exists in the private work directory")
        stderr_path = client.join(workdir, "pg_dump.stderr")
        argv = [*client.pg_dump, f"--dbname={_conninfo(localized, application_name)}", "--format=custom", f"--snapshot={snapshot_id}",
                "--no-password", f"--file={archive_path}"]

        handle.confirm_active()
        process = client.start(argv, {"PGPASSFILE": passfile}, client.join(workdir, "pg_dump.stdout"), stderr_path)
        returncode = process.wait(dump_timeout_s)
        if returncode is None:
            raise DumpTimeout(f"pg_dump exceeded its {dump_timeout_s:g} s deadline and was stopped", stage="dump")
        client.remove(passfile)
        passfile = None
        if returncode != 0:
            raise DumpFailed(f"pg_dump exited with status {returncode}", stage="dump", returncode=returncode,
                             diagnostic=_sanitized(client.read_bounded(stderr_path, _STDERR_LIMIT), password))

        handle.confirm_active()  # S and L still valid after completion; a loss here means no successful result
        kind, size = client.file_state(archive_path)
        if kind != "regular" or size == 0:
            raise DumpFailed("pg_dump produced no non-empty regular archive file", stage="output", returncode=returncode)

        toc_path = client.join(workdir, "toc.list")
        lister = client.start([*client.pg_restore, "--list", f"--file={toc_path}", archive_path], {},
                              client.join(workdir, "pg_restore.stdout"), client.join(workdir, "pg_restore.stderr"))
        list_status = lister.wait(tool_timeout_s)
        if list_status is None:
            lister.stop(_REAP_TIMEOUT_S)
            raise DumpTimeout("pg_restore --list exceeded its deadline and was stopped", stage="toc")
        if list_status != 0:
            raise DumpFailed(f"pg_restore --list exited with status {list_status}", stage="toc", returncode=list_status,
                             diagnostic=_sanitized(client.read_bounded(client.join(workdir, "pg_restore.stderr"), _STDERR_LIMIT), password))
        toc_bytes = client.read_bounded(toc_path, _TOC_LIMIT + 1)
        if len(toc_bytes) > _TOC_LIMIT:
            raise DumpFailed("the archive table of contents exceeds its size bound", stage="toc")
        toc = toc_bytes.decode("utf-8", errors="replace")
        found, expected = _toc_inventory(toc), _expected_inventory(baseline)
        mismatched = sorted(kind for kind in expected if found[kind] != expected[kind])
        if mismatched:
            raise DumpFailed(f"the archive inventory does not match the baseline ({', '.join(mismatched)})", stage="inventory")

        archive = DumpArchive(
            archive_path, archive_bytes=size, toc_entries=sum(1 for line in toc.splitlines() if _TOC_ENTRY.match(line)),
            toc_sha256=hashlib.sha256(toc_bytes).hexdigest(), pg_dump_version=dump_version, pg_restore_version=restore_version,
            application_name=application_name, snapshot_id=snapshot_id, returncode=returncode,
            inventory={kind: len(names) for kind, names in found.items()},
        )
        yield archive
    except BaseException as exc:
        primary = exc
        raise
    finally:
        if archive is not None:
            archive._open = False
        failures: list[str] = []
        interrupt: BaseException | None = None

        def attempt(step: str, action: Callable[[], object]):
            nonlocal interrupt
            try:
                return True, action()
            except Exception as exc:  # noqa: BLE001 -- a failed step never skips the remaining ones
                failures.append(f"{step}:{_type_name(exc)}")
            except BaseException as exc:  # a NEW interrupt: remaining steps still run, then it propagates
                failures.append(f"{step}:{_type_name(exc)}")
                interrupt = interrupt or exc
            return False, None

        stopped_by_module = False
        reaped = process is None
        if process is not None:
            if returncode is None:  # deadline, interrupt or failure while the client was running
                stopped_by_module = True
                ok, outcome = attempt("stop", lambda: process.stop(_REAP_TIMEOUT_S))
                reaped = bool(ok and outcome and outcome[0])
            else:
                reaped = True
        backend = BACKEND_NOT_STARTED
        if process is not None:
            if interrupt is None:
                ok, outcome = attempt("backend", lambda: _observe_backend(factory, url, designation, application_name, stopped_by_module))
                if ok and outcome:
                    backend, observe_failures = outcome
                    failures += observe_failures
                else:
                    backend = BACKEND_NOT_VERIFIED
            else:
                backend = BACKEND_NOT_VERIFIED
        passfile_removed = passfile is None
        if passfile is not None:
            passfile_removed, _ = attempt("passfile", lambda: client.remove(passfile))
        workdir_removed = workdir is None
        if workdir is not None:
            attempt("workdir", lambda: client.remove_tree(workdir))
            ok, present = attempt("workdir_check", lambda: client.exists(workdir))
            workdir_removed = bool(ok and not present)
            passfile_removed = passfile_removed or workdir_removed

        report = DumpCleanupReport(process_started=process is not None, process_stopped_by_module=stopped_by_module, process_reaped=reaped,
                                   backend=backend, backend_end_requested=stopped_by_module and backend != BACKEND_NOT_VERIFIED,
                                   passfile_removed=passfile_removed, workdir_removed=workdir_removed, failures=tuple(failures))
        if archive is not None:
            archive.cleanup = report
        if interrupt is not None:
            raise interrupt
        if not report.confirmed:
            message = (f"dump cleanup was not confirmed: backend={backend}; reaped={reaped}; workdir_removed={workdir_removed}; "
                       f"failed steps={','.join(failures) or 'none'}")
            if primary is not None:
                primary.add_note(message)
            else:
                raise DumpCleanupError(message, report)
