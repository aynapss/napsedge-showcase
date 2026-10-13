"""
Phase 9B-6D: unit tests for `src/deployment/backup_capture_dump.py` that need no database.

Two kinds, kept apart:
* INJECTED behavior through an in-memory `ClientHost` fake: ordering, refusals, argv/environment, failure handling,
  cleanup and exception preservation, sanitization.
* REAL processes through the production `LocalClientHost`, with a small stand-in client script (this host has no
  PostgreSQL client): environment scrubbing, exclusive private files, a real deadline kill and reap, a real start
  failure, version checks and pipe-free output capture. The stand-in is not `pg_dump`; real `pg_dump` behavior is in
  `tests/postgres/test_postgres_backup_capture_dump.py`.
"""
from __future__ import annotations

import ast
import datetime as dt
import inspect
import json
import os
import sys
import textwrap
import traceback
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

import src.deployment.backup_capture_dump as dump_module
from src.deployment.backup_artifact import AclEntry, BaselineDocument, FingerprintEntry, SequenceDetail, SequenceEntry
from src.deployment.backup_capture_dump import (
    BACKEND_GONE,
    BACKEND_NOT_STARTED,
    BACKEND_NOT_VERIFIED,
    BACKEND_PRESENT,
    DUMP_VERIFIER_APPLICATION_NAME,
    DumpCleanupError,
    DumpFailed,
    DumpPreconditionError,
    DumpTimeout,
    LibpqTarget,
    LocalClientHost,
    backup_capture_dump,
)
from src.deployment.backup_capture_lock import BackupLockNotHeld
from src.deployment.backup_capture_snapshot import SnapshotFacts, SnapshotHandle, SnapshotHandleClosed, SnapshotSessionLost
from src.exceptions import ValidationError
from src.pipeline.bulk_import_safety import DatabaseTargetDesignation, build_expected_acknowledgement

DB = "napsedge_pg_test_dump"
SECRET = "Pl4nted-S3cret:zz9\\x"
HOST = "db.internal"
URL = make_url(f"postgresql+psycopg://napsedge_backup_ro:x@{HOST}:6543/{DB}?sslmode=verify-full&sslrootcert=/ca.pem&hostaddr=10.0.0.5&connect_timeout=15").set(password=SECRET)
SNAP_ID = "00000004-00000002-1"
M = "napsedge_migrator"
T0 = dt.datetime(2026, 10, 5, 12, tzinfo=dt.timezone.utc)
TOC = (
    ";\n; Archive created at 2026-10-05\n;\n"
    "215; 1259 16390 TABLE public teams napsedge_migrator\n"
    "216; 1259 16400 SEQUENCE public team_aliases_id_seq napsedge_migrator\n"
    "3400; 0 16390 TABLE DATA public teams napsedge_migrator\n"
    "3401; 0 0 SEQUENCE SET public team_aliases_id_seq napsedge_migrator\n"
    "3500; 0 0 ACL public TABLE teams napsedge_migrator\n"
)


def designation(name: str = DB) -> DatabaseTargetDesignation:
    return DatabaseTargetDesignation(expected_database_name=name, acknowledgement=build_expected_acknowledgement(name))


def baseline() -> BaselineDocument:
    return BaselineDocument(
        artifact_id="0b3c2f6e-1d2a-4c5b-9e8f-7a6b5c4d3e2f", captured_utc="2026-10-05T12:00:00.000000Z",
        fingerprints=(FingerprintEntry("teams", 3, "0123456789abcdef0123456789abcdef"),), constraints=(), indexes=(),
        acls=(AclEntry("team_aliases_id_seq", "S", M, ""), AclEntry("teams", "r", M, "{napsedge_migrator=arwdDxt/napsedge_migrator}")),
        sequences=(SequenceEntry("team_aliases_id_seq", 3),), version="5b02bea0eb85", max_alias_id=3,
        sequence_details=(SequenceDetail("team_aliases_id_seq", ("team_aliases", "id"), "bigint", 1, 1, 2**63 - 1, 1, False, 3, True, 3),),
    )


class Handle:
    """A real `SnapshotHandle` whose confirmation is scripted (each entry None = ok, or an exception to raise)."""

    def __init__(self, confirms=None, url=URL):
        self.log: list = []
        self.confirms = list(confirms or [])
        facts = SnapshotFacts(5151, T0, T0, "repeatable read", "on", "7412345678901234567", 16384, DB, 160004, 4242)
        self.handle = SnapshotHandle(SNAP_ID, "100:100:", baseline(), None, facts, confirm=self._confirm, source=(url, designation()))

    def _confirm(self):
        self.log.append("confirm_active")
        outcome = self.confirms.pop(0) if self.confirms else None
        if outcome is not None:
            raise outcome


# ---------------------------------------------------------------------------
# an in-memory ClientHost (injected behavior)
# ---------------------------------------------------------------------------
class FakeProcess:
    def __init__(self, host, returncode, *, stop_ok=True, wait_raises=None):
        self.host, self.returncode, self.stop_ok, self.wait_raises = host, returncode, stop_ok, wait_raises
        self.stopped = False

    def wait(self, timeout):
        self.host.log.append(("wait", timeout))
        if self.wait_raises is not None:
            raise self.wait_raises
        return self.returncode

    def stop(self, timeout):
        self.host.log.append(("stop", timeout))
        self.stopped = True
        return (self.stop_ok, -9 if self.stop_ok else None)


class FakeClient:
    pg_dump = ("pg_dump",)
    pg_restore = ("pg_restore",)

    def __init__(self, *, dump_rc=0, archive=b"PGDMP\x01custom", toc=TOC, list_rc=0, version="16.4", restore_version=None, fail=None,
                 hang=False, stop_ok=True, wait_raises=None, archive_kind="regular", localize=None, stderr=b""):
        self.log: list = []
        self.files: dict = {}
        self.dirs: set = set()
        self.dump_rc, self.archive, self.toc, self.list_rc, self.version = dump_rc, archive, toc, list_rc, version
        self.restore_version = restore_version or version
        self.fail = fail or {}
        self.hang, self.stop_ok, self.wait_raises, self.archive_kind, self._localize, self.stderr = hang, stop_ok, wait_raises, archive_kind, localize, stderr
        self.started: list = []

    def _maybe(self, op):
        if op in self.fail:
            raise self.fail[op]

    def make_private_dir(self):
        self.log.append("make_private_dir")
        self.dirs.add("/work")
        return "/work"

    def join(self, directory, name):
        return f"{directory}/{name}"

    def write_private_file(self, path, data):
        self.log.append(("write", path))
        if path in self.files:
            raise FileExistsError(path)
        self.files[path] = data

    def file_state(self, path):
        if path.endswith("archive.dump") and path in self.files:
            return (self.archive_kind, len(self.files[path]))
        return ("regular", len(self.files[path])) if path in self.files else ("absent", 0)

    def read_bounded(self, path, limit):
        return self.files.get(path, b"")[:limit]

    def remove(self, path):
        self.log.append(("remove", path))
        self._maybe("remove")
        self.files.pop(path, None)

    def remove_tree(self, path):
        self.log.append(("remove_tree", path))
        self._maybe("remove_tree")
        for name in [n for n in self.files if n.startswith(path + "/")]:
            del self.files[name]
        self.dirs.discard(path)

    def exists(self, path):
        return path in self.dirs or path in self.files

    def localize(self, target, workdir):
        return self._localize(target) if self._localize else target

    def start(self, argv, env, stdout_path, stderr_path):
        self.log.append(("start", argv[0], "--version" in argv))
        self.started.append((list(argv), dict(env)))
        self._maybe("start")
        if "--version" in argv:
            name = argv[0]
            self.files[stdout_path] = f"{name} (PostgreSQL) {self.version if name == 'pg_dump' else self.restore_version}\n".encode()
            return FakeProcess(self, 0)
        if argv[0] == "pg_restore":
            toc_path = next(a.split("=", 1)[1] for a in argv if a.startswith("--file="))
            if self.list_rc == 0:
                self.files[toc_path] = self.toc.encode()
            return FakeProcess(self, self.list_rc)
        self.files[stderr_path] = self.stderr
        if self.hang:
            return FakeProcess(self, None, stop_ok=self.stop_ok)
        if self.wait_raises is not None:
            return FakeProcess(self, None, wait_raises=self.wait_raises)
        if self.dump_rc == 0 and self.archive is not None:
            self.files[next(a.split("=", 1)[1] for a in argv if a.startswith("--file="))] = self.archive
        return FakeProcess(self, self.dump_rc)


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def scalar_one(self):
        return self.rows[0][0]

    def one(self):
        return self.rows[0]


class FakeVerifier:
    """The verifier session: answers target verification and the backend count; records whether the backend was ended."""

    def __init__(self, *, present=(0,), connect_error=None):
        self.present = list(present)
        self.connect_error = connect_error
        self.statements: list = []
        self.pool = NullPool(lambda: None)
        self.closed = False
        self.engine = type("E", (), {"dialect": type("D", (), {"name": "postgresql"})()})()

    def __call__(self, url, application_name):
        assert application_name == DUMP_VERIFIER_APPLICATION_NAME
        return self

    def connect(self):
        if self.connect_error:
            raise self.connect_error
        return self

    def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append((sql, dict(params or {})))
        if sql.startswith("SELECT current_database()"):
            return _Result([(DB, "PostgreSQL 16.4")])
        if sql == dump_module._BACKEND_END_SQL:
            return _Result([(1,)])
        if sql == dump_module._BACKEND_COUNT_SQL:
            return _Result([(self.present.pop(0) if len(self.present) > 1 else self.present[0],)])
        raise AssertionError(sql)

    def rollback(self):
        pass

    def close(self):
        self.closed = True

    def dispose(self):
        pass


def run(handle, client, verifier=None, body=lambda archive: None, **kw):
    verifier = verifier or FakeVerifier()
    with backup_capture_dump(handle.handle, client=client, _engine_factory=verifier, **kw) as archive:
        body(archive)
    return archive, verifier


def run_expecting(exc_type, handle, client, verifier=None, body=lambda archive: None, **kw):
    verifier = verifier or FakeVerifier()
    entered: list = []
    with pytest.raises(BaseException) as info:
        with backup_capture_dump(handle.handle, client=client, _engine_factory=verifier, **kw) as archive:
            entered.append(archive)
            body(archive)
    assert type(info.value) is exc_type, f"expected {exc_type.__name__}, got {type(info.value).__name__}: {info.value}"
    return info, entered, verifier


def no_secret(*texts):
    joined = "\n".join(str(t) for t in texts)
    assert SECRET not in joined and SECRET.split(":")[0] not in joined


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(dump_module, "_BACKEND_DEADLINE_S", 0.2)
    monkeypatch.setattr(dump_module, "_POLL_S", 0.01)


# ---------------------------------------------------------------------------
# libpq translation and credentials
# ---------------------------------------------------------------------------
def test_the_validated_url_becomes_exact_libpq_keywords_and_a_passfile_line_with_escaping():
    target, password = dump_module._libpq_target(URL)
    assert target == LibpqTarget(HOST, "10.0.0.5", 6543, DB, "napsedge_backup_ro", "verify-full", "/ca.pem", "15") and password == SECRET
    assert dump_module._conninfo(target, "napsedge_backup_dump_x") == (
        f"host='{HOST}' hostaddr='10.0.0.5' port='6543' dbname='{DB}' user='napsedge_backup_ro' sslmode='verify-full' "
        "sslrootcert='/ca.pem' application_name='napsedge_backup_dump_x' connect_timeout='15'")
    assert dump_module._conninfo_value("a'b\\c") == "'a\\'b\\\\c'"
    assert dump_module._passfile_bytes(replace(target, host="h:1\\x"), "p:w\\d") == f"h\\:1\\\\x:6543:{DB}:napsedge_backup_ro:p\\:w\\\\d\n".encode()


@pytest.mark.parametrize("change, fragment", [
    ({"query": {"sslmode": "verify-full", "sslrootcert": "/ca.pem", "sslcert": "/c.pem"}}, "sslcert"),
    ({"query": {"sslmode": "verify-full", "sslrootcert": "/ca.pem", "options": "-c x=1"}}, "options"),
    ({"query": {"sslmode": "verify-full", "sslrootcert": "/ca.pem", "target_session_attrs": "any"}}, "target_session_attrs"),
    ({"query": {"sslmode": "require", "sslrootcert": "/ca.pem"}}, "verify-full"),
    ({"query": {"sslmode": "verify-full", "sslrootcert": ("/a.pem", "/b.pem")}}, "repeated"),
    ({"query": {"sslmode": "verify-full", "sslrootcert": "/ca.pem", "connect_timeout": "15s"}}, "whole number"),
    ({"password": None}, "password source"),
    ({"database": "bad\ndb"}, "line break"),
])
def test_unsupported_or_unsafe_connection_parameters_are_refused_never_dropped(change, fragment):
    url = URL._replace(password=None) if change == {"password": None} else URL.set(**change)  # URL.set() ignores None values
    with pytest.raises(DumpPreconditionError) as info:
        dump_module._libpq_target(url)
    assert fragment in str(info.value)
    no_secret(str(info.value))


# ---------------------------------------------------------------------------
# normal path (injected)
# ---------------------------------------------------------------------------
def test_normal_order_argv_environment_facts_and_confirmed_cleanup():
    handle, client = Handle(), FakeClient()
    seen = []
    archive, verifier = run(handle, client, body=lambda a: seen.append((a.archive_path, a.archive_bytes, a.inventory, a.toc_entries, a.pg_dump_version)))
    dump_argv, dump_env = next((argv, env) for argv, env in client.started if argv[0] == "pg_dump" and "--version" not in argv)
    assert dump_argv[0] == "pg_dump" and dump_argv[2:] == ["--format=custom", f"--snapshot={SNAP_ID}", "--no-password", "--file=/work/archive.dump"]
    assert dump_argv[1].startswith("--dbname=host=") and "application_name='napsedge_backup_dump_" in dump_argv[1]
    assert dump_env == {"PGPASSFILE": "/work/pgpass"}
    assert client.started[-1][0] == ["pg_restore", "--list", "--file=/work/toc.list", "/work/archive.dump"]  # offline: no database option
    assert handle.log == ["confirm_active", "confirm_active", "confirm_active"]  # before anything, before launch, after completion
    dump_start = client.log.index(("start", "pg_dump", False))
    assert client.log.index("make_private_dir") < client.log.index(("write", "/work/pgpass")) < dump_start
    assert [e for e in client.log[:dump_start] if isinstance(e, tuple) and e[0] == "start"] == [("start", "pg_dump", True), ("start", "pg_restore", True)]
    assert ("remove", "/work/pgpass") in client.log and client.log.index(("remove", "/work/pgpass")) < client.log.index(("start", "pg_restore", False))
    assert seen == [("/work/archive.dump", 12, {"TABLE": 1, "TABLE DATA": 1, "SEQUENCE": 1, "SEQUENCE SET": 1, "ACL": 1, "OWNERS": 2}, 5,
                     "pg_dump (PostgreSQL) 16.4")]
    assert archive.cleanup.confirmed and archive.cleanup.backend == BACKEND_GONE and not archive.cleanup.process_stopped_by_module
    assert client.dirs == set() and client.files == {}
    assert not any(sql == dump_module._BACKEND_END_SQL for sql, _ in verifier.statements)  # nothing ended on a normal exit
    with pytest.raises(DumpPreconditionError):
        archive.archive_path
    no_secret(json.dumps(client.started), json.dumps([str(e) for e in client.log]))
    assert client.started and all(SECRET not in " ".join(argv) for argv, _ in client.started)


def test_the_password_reaches_only_the_passfile():
    handle, client = Handle(), FakeClient()
    writes = []
    original = client.write_private_file
    client.write_private_file = lambda path, data: (writes.append((path, data)), original(path, data))[1]
    run(handle, client)
    assert [p for p, d in writes if SECRET.encode().replace(b"\\", b"\\\\").replace(b":", b"\\:") in d] == ["/work/pgpass"]


# ---------------------------------------------------------------------------
# refusals before launch
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kw", [{"handle": "not a handle"}, {"client": object()}, {"dump_timeout_s": 0}, {"tool_timeout_s": True}])
def test_invalid_inputs_are_refused_before_anything(kw):
    handle, client = Handle(), FakeClient()
    with pytest.raises(ValidationError):
        with backup_capture_dump(kw.get("handle", handle.handle), client=kw.get("client", client), _engine_factory=FakeVerifier(),
                                 **{k: v for k, v in kw.items() if k.endswith("_s")}):
            pytest.fail("no body")
    assert client.log == [] and handle.log == []


def test_an_expired_handle_is_rejected_before_anything():
    handle, client = Handle(), FakeClient()
    handle.handle._open = False
    run_expecting(SnapshotHandleClosed, handle, client)
    assert client.log == []


@pytest.mark.parametrize("lost", [BackupLockNotHeld("lost"), SnapshotSessionLost("lost")])
def test_a_loss_found_before_anything_or_before_launch_starts_no_dump(lost):
    handle, client = Handle(confirms=[lost]), FakeClient()
    run_expecting(type(lost), handle, client)
    assert client.log == []
    handle, client = Handle(confirms=[None, lost]), FakeClient()
    run_expecting(type(lost), handle, client)
    assert not any(e[0] == "start" and e[1] == "pg_dump" and not e[2] for e in client.log if isinstance(e, tuple))
    assert client.dirs == set() and client.files == {}


@pytest.mark.parametrize("version, restore_version, fragment", [
    ("15.6", None, "major 15, not 16"), ("16.4", "17.0", "major 17, not 16"), ("x", None, "did not report a PostgreSQL version"),
])
def test_a_client_that_is_not_postgresql_16_is_refused_before_any_dump(version, restore_version, fragment):
    handle, client = Handle(), FakeClient(version=version, restore_version=restore_version)
    info, _, _ = run_expecting(DumpPreconditionError, handle, client)
    assert fragment in str(info.value)
    assert not any(isinstance(e, tuple) and e[0] == "start" and not e[2] for e in client.log) and client.dirs == set()


def test_a_client_host_that_relocates_more_than_network_path_and_ca_is_refused():
    handle, client = Handle(), FakeClient(localize=lambda t: replace(t, dbname="postgres"))
    info, _, _ = run_expecting(DumpPreconditionError, handle, client)
    assert "only the network path and the CA file" in str(info.value) and "/work/pgpass" not in client.files and client.dirs == set()


def test_a_start_failure_of_the_client_is_a_sanitized_precondition_error():
    handle, client = Handle(), FakeClient(fail={"start": FileNotFoundError(f"no such file {SECRET}")})
    info, _, _ = run_expecting(DumpPreconditionError, handle, client)
    assert str(info.value) == "the pg_dump client could not be started (FileNotFoundError)" and info.value.__cause__ is None
    no_secret("".join(traceback.format_exception(info.value)))


# ---------------------------------------------------------------------------
# failures after launch
# ---------------------------------------------------------------------------
def test_a_nonzero_exit_is_a_failure_with_a_bounded_password_free_diagnostic():
    handle, client = Handle(), FakeClient(dump_rc=1, stderr=(f"pg_dump: error: invalid snapshot identifier {SECRET} " + "x" * 5000).encode())
    info, entered, _ = run_expecting(DumpFailed, handle, client)
    assert (info.value.stage, info.value.returncode, str(info.value)) == ("dump", 1, "pg_dump exited with status 1")
    assert "invalid snapshot identifier" in info.value.diagnostic and len(info.value.diagnostic) <= 2048 + 16
    no_secret(info.value.diagnostic, str(info.value))
    assert entered == [] and client.dirs == set() and client.files == {}


@pytest.mark.parametrize("archive, kind", [(b"", "regular"), (None, "regular"), (b"PGDMP", "other")], ids=["empty", "missing", "not_regular"])
def test_an_empty_missing_or_irregular_archive_is_never_success(archive, kind):
    handle, client = Handle(), FakeClient(archive=archive, archive_kind=kind)
    info, entered, _ = run_expecting(DumpFailed, handle, client)
    assert info.value.stage == "output" and entered == [] and client.dirs == set()


def test_a_loss_after_completion_prevents_success_and_removes_the_archive():
    handle, client = Handle(confirms=[None, None, SnapshotSessionLost("lost")]), FakeClient()
    info, entered, _ = run_expecting(SnapshotSessionLost, handle, client)
    assert entered == [] and client.files == {} and client.dirs == set()


@pytest.mark.parametrize("toc, list_rc, fragment", [
    (TOC, 1, "pg_restore --list exited with status 1"),
    (TOC.replace("3400; 0 16390 TABLE DATA public teams napsedge_migrator\n", ""), 0, "TABLE DATA"),
    (TOC.replace("TABLE public teams napsedge_migrator", "TABLE public teams someone_else"), 0, "OWNERS"),
    (TOC.replace("3500; 0 0 ACL public TABLE teams napsedge_migrator\n", ""), 0, "ACL"),
    (TOC + "3402; 0 0 SEQUENCE SET public extra_seq napsedge_migrator\n", 0, "SEQUENCE SET"),
], ids=["list_failed", "missing_data", "wrong_owner", "missing_acl", "extra_sequence"])
def test_the_offline_inventory_must_match_the_baseline_exactly(toc, list_rc, fragment):
    handle, client = Handle(), FakeClient(toc=toc, list_rc=list_rc)
    info, entered, _ = run_expecting(DumpFailed, handle, client)
    assert fragment in str(info.value) and entered == [] and client.dirs == set()


def test_a_deadline_stops_and_reaps_the_client_and_ends_only_this_dumps_own_backend():
    handle, client = Handle(), FakeClient(hang=True)
    info, entered, verifier = run_expecting(DumpTimeout, handle, client, dump_timeout_s=3)
    assert ("stop", dump_module._REAP_TIMEOUT_S) in client.log and ("wait", 3) in client.log
    ended = [params for sql, params in verifier.statements if sql == dump_module._BACKEND_END_SQL]
    assert len(ended) == 1 and ended[0]["app"].startswith("napsedge_backup_dump_")
    assert "application_name = :app AND usename = current_user AND datname = current_database() AND pid <> pg_backend_pid()" in dump_module._BACKEND_END_SQL
    assert getattr(info.value, "__notes__", []) == [] and client.dirs == set() and client.files == {}


def test_an_interrupt_while_waiting_is_preserved_and_the_client_is_stopped():
    interrupt = KeyboardInterrupt()
    handle, client = Handle(), FakeClient(wait_raises=interrupt)
    info, _, _ = run_expecting(KeyboardInterrupt, handle, client)
    assert info.value is interrupt and any(e[0] == "stop" for e in client.log if isinstance(e, tuple)) and client.dirs == set()


# ---------------------------------------------------------------------------
# cleanup failures and interrupts
# ---------------------------------------------------------------------------
def test_a_cleanup_failure_with_a_primary_adds_one_note_and_never_replaces_it():
    handle, client = Handle(), FakeClient(dump_rc=1, fail={"remove_tree": OSError(f"busy {SECRET}")})
    info, _, _ = run_expecting(DumpFailed, handle, client)
    assert len(info.value.__notes__) == 1 and "workdir:OSError" in info.value.__notes__[0]
    no_secret(info.value.__notes__[0])


@pytest.mark.parametrize("client_kw, verifier_kw, fragment", [
    ({"fail": {"remove_tree": OSError("busy")}}, {}, "workdir:OSError"),
    ({}, {"present": (1,)}, "backend=present"),
    ({}, {"connect_error": OSError("down")}, "backend=not_verified"),
    ({"hang": True, "stop_ok": False}, {}, "reaped=False"),
])
def test_unconfirmed_cleanup_without_a_primary_is_never_success(client_kw, verifier_kw, fragment):
    handle, client = Handle(), FakeClient(**client_kw)
    if client_kw.get("hang"):
        info, _, _ = run_expecting(DumpTimeout, handle, client, FakeVerifier(**verifier_kw), dump_timeout_s=1)
        assert fragment in info.value.__notes__[0]
        return
    info, _, _ = run_expecting(DumpCleanupError, handle, client, FakeVerifier(**verifier_kw))
    assert fragment in str(info.value) and not info.value.report.confirmed


def test_a_new_interrupt_during_cleanup_propagates_after_the_remaining_steps():
    new = KeyboardInterrupt()
    handle, client = Handle(), FakeClient(fail={"remove_tree": new})
    info, entered, _ = run_expecting(KeyboardInterrupt, handle, client)
    assert info.value is new and entered[0].cleanup is not None and not entered[0].cleanup.workdir_removed


# ---------------------------------------------------------------------------
# REAL processes through the production LocalClientHost (stand-in client script)
# ---------------------------------------------------------------------------
STAND_IN = textwrap.dedent('''
    import json, os, sys, time
    tool, args = sys.argv[1], sys.argv[2:]
    mode = os.environ.get("NAPSEDGE_STANDIN_MODE", "ok")
    report = os.environ.get("NAPSEDGE_STANDIN_REPORT")
    if report:
        with open(report, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"tool": tool, "args": args, "pg_env": sorted(k for k in os.environ if k.upper().startswith("PG")),
                                     "pid": os.getpid()}) + "\\n")
    if "--version" in args:
        print(f"{tool} (PostgreSQL) {'15.6' if mode == 'v15' else '16.4'}")
        sys.exit(0)
    target = next(a.split("=", 1)[1] for a in args if a.startswith("--file="))
    if tool == "pg_restore":
        open(target, "w", encoding="utf-8").write(os.environ["NAPSEDGE_STANDIN_TOC"])
        sys.exit(0)
    if mode == "sleep":
        time.sleep(120)
    if mode == "fail":
        sys.stderr.write("pg_dump: error: synthetic failure\\n")
        sys.exit(1)
    passfile = open(os.environ["PGPASSFILE"], encoding="utf-8").read()
    open(target, "wb").write(b"PGDMP" + (b"x" if passfile.count(":") >= 4 else b""))
''')


@pytest.fixture()
def local(tmp_path, monkeypatch):
    script = tmp_path / "standin.py"
    script.write_text(STAND_IN, encoding="utf-8")
    report = tmp_path / "report.jsonl"
    monkeypatch.setenv("NAPSEDGE_STANDIN_REPORT", str(report))
    monkeypatch.setenv("NAPSEDGE_STANDIN_TOC", TOC)
    for name in ("PGHOST", "PGPORT", "PGSSLMODE", "PGSERVICE", "PGOPTIONS", "PGPASSWORD", "PGSSLROOTCERT", "pgdatabase"):
        monkeypatch.setenv(name, "inherited-must-not-reach-the-client")
    client = LocalClientHost(pg_dump=(sys.executable, str(script), "pg_dump"), pg_restore=(sys.executable, str(script), "pg_restore"))
    created = []
    original = client.make_private_dir
    object.__setattr__(client, "make_private_dir", lambda: (created.append(original()), created[-1])[1])
    return client, report, created


def reports(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_local_host_real_processes_succeed_with_a_scrubbed_environment_and_leave_nothing(local):
    client, report, created = local
    seen = []
    archive, _ = run(Handle(), client, body=lambda a: seen.append((Path(a.archive_path).read_bytes(), sorted(os.listdir(Path(a.archive_path).parent)))))
    assert seen[0][0] == b"PGDMPx"  # the passfile the client read had all five fields
    assert "pgpass" not in seen[0][1] and "archive.dump" in seen[0][1]  # the passfile was already gone while the archive existed
    entries = reports(report)
    assert {e["tool"] for e in entries} == {"pg_dump", "pg_restore"}
    assert all(e["pg_env"] == (["PGPASSFILE"] if e["tool"] == "pg_dump" and "--version" not in e["args"] else []) for e in entries)
    assert all(SECRET not in json.dumps(e) for e in entries)
    assert archive.cleanup.confirmed and not os.path.lexists(created[0])


def test_local_host_a_real_deadline_kills_and_reaps_the_real_process(local, monkeypatch):
    client, report, created = local
    monkeypatch.setenv("NAPSEDGE_STANDIN_MODE", "sleep")
    info, _, verifier = run_expecting(DumpTimeout, Handle(), client, dump_timeout_s=2)
    pid = [e for e in reports(report) if e["tool"] == "pg_dump" and "--version" not in e["args"]][0]["pid"]
    if os.name == "posix":
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    else:
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        code = ctypes.c_ulong()
        alive = bool(handle) and ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259  # STILL_ACTIVE
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
        assert not alive
    assert any(sql == dump_module._BACKEND_END_SQL for sql, _ in verifier.statements)
    assert not os.path.lexists(created[0])


def test_local_host_real_nonzero_exit_and_wrong_version_and_missing_executable(local, monkeypatch, tmp_path):
    client, report, created = local
    monkeypatch.setenv("NAPSEDGE_STANDIN_MODE", "fail")
    info, _, _ = run_expecting(DumpFailed, Handle(), client)
    assert info.value.returncode == 1 and "synthetic failure" in info.value.diagnostic
    monkeypatch.setenv("NAPSEDGE_STANDIN_MODE", "v15")
    info, _, _ = run_expecting(DumpPreconditionError, Handle(), client)
    assert "major 15" in str(info.value)
    missing = LocalClientHost(pg_dump=(str(tmp_path / "no_such_pg_dump.exe"),), pg_restore=(str(tmp_path / "no_such_pg_restore.exe"),))
    info, _, _ = run_expecting(DumpPreconditionError, Handle(), missing)
    assert str(info.value) == "the pg_dump client could not be started (FileNotFoundError)"
    assert not any(os.path.lexists(d) for d in created)


def test_local_host_private_files_are_exclusive_and_never_follow_or_overwrite(tmp_path):
    host = LocalClientHost()
    directory = host.make_private_dir()
    try:
        path = host.join(directory, "pgpass")
        host.write_private_file(path, b"one")
        with pytest.raises(FileExistsError):
            host.write_private_file(path, b"two")
        assert Path(path).read_bytes() == b"one" and host.file_state(path) == ("regular", 3) and host.file_state(path + ".x") == ("absent", 0)
        if os.name == "posix":
            assert oct(os.stat(directory).st_mode & 0o777) == "0o700" and oct(os.stat(path).st_mode & 0o777) == "0o600"
    finally:
        host.remove_tree(directory)
    assert not host.exists(directory)


# ---------------------------------------------------------------------------
# static constraints
# ---------------------------------------------------------------------------
_SOURCE = inspect.getsource(dump_module)
_TREE = ast.parse(_SOURCE)


def test_no_forbidden_dump_options_no_shell_and_no_unrelated_backend_signalling():
    strings = [n.value for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    code = " ".join(s for s in strings if not s.startswith("\nPhase"))
    for forbidden in ("--no-owner", "--no-acl", "--data-only", "--schema-only", "--jobs", "--exclude", "--table", "pg_advisory", "pg_cancel_backend",
                      "--dbname=postgres", "--host", "PGPASSWORD"):
        assert forbidden not in code, forbidden
    assert code.count("pg_terminate_backend") == 1  # only _BACKEND_END_SQL, scoped to this dump's application_name, role and database
    calls = [n for n in ast.walk(_TREE) if isinstance(n, ast.Call)]
    assert not any(kw.arg == "shell" and getattr(kw.value, "value", None) is True for c in calls for kw in c.keywords)
    imported = {n.module for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(_TREE) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m.startswith("tests") for m in imported) and "src.deployment.backup_artifact_writer" not in imported
    for marker in ("COMPLETE", "manifest.json", "verification.json", "FAILED"):
        assert marker not in code, marker  # nothing here publishes or records an artifact
