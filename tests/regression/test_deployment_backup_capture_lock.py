"""
Phase 9B-6B: deterministic unit tests for `src/deployment/backup_capture_lock.py` with fake engines and connections.
Everything here is INJECTED behavior (ordering, failure handling, exception preservation, sanitization). Real
PostgreSQL behavior -- server-side lock ownership, backend absence, a lock surviving rollback -- is tested only in
`tests/postgres/test_postgres_backup_capture_lock.py`.
"""
from __future__ import annotations

import ast
import datetime as dt
import inspect
import traceback
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool, QueuePool

import src.deployment.backup_capture_lock as lock_module
import src.deployment.backup_capture_preflight as preflight_module
from src.deployment.backup_capture_lock import (
    LOCK_APPLICATION_NAME,
    NO_SESSION_CREATED,
    NOT_VERIFIED,
    ORIGINAL_SESSION_PRESENT,
    VERIFIED_CLOSED,
    VERIFIER_APPLICATION_NAME,
    BackupLockCleanupError,
    BackupLockNotHeld,
    BackupLockSessionLost,
    BackupLockSetupError,
    backup_capture_lock,
)
from src.exceptions import AdvisoryLockUnavailableError, DatabaseTargetVerificationError, ValidationError
from src.pipeline.bulk_import_safety import (
    RELEASE_FAILED_NOTE,
    RELEASE_NOT_HELD_NOTE,
    DatabaseTargetDesignation,
    build_expected_acknowledgement,
)

DB = "napsedge_pg_test_lock"
SECRET = "Pl4nted-S3cret-zz9"
HOST = "db-secret-host.internal"
URL = f"postgresql+psycopg://napsedge_backup_ro:{SECRET}@{HOST}:5432/{DB}?sslmode=verify-full&sslrootcert=/ca.pem"
START = dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.timezone.utc)
VERIFY = "SELECT current_database(), version()"
TRY_LOCK = "SELECT pg_try_advisory_lock(:key)"
UNLOCK = "SELECT pg_advisory_unlock(:key)"
DB_FACTS = preflight_module._DATABASE_SQL
SYSID_PRIV = preflight_module._SYSTEM_IDENTIFIER_PRIVILEGE_SQL
SYSID = preflight_module._SYSTEM_IDENTIFIER_SQL
HELD = lock_module._HELD_SQL


def designation(name: str = DB) -> DatabaseTargetDesignation:
    return DatabaseTargetDesignation(expected_database_name=name, acknowledgement=build_expected_acknowledgement(name))


class DriverFailure(Exception):
    pass


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def one(self):
        assert len(self._rows) == 1
        return self._rows[0]

    def scalar_one(self):
        return self.one()[0]

    def fetchall(self):
        return list(self._rows)


class FakeConnection:
    def __init__(self, name, log, answers, *, fail=None):
        self.name, self.log, self.answers, self.fail = name, log, answers, fail or {}
        self.closed = False
        self.engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def _maybe_fail(self, step):
        if step in self.fail:
            raise self.fail[step]

    def execute(self, statement, params=None):
        sql = str(statement)
        self.log.append((self.name, "execute", sql))
        answer = self.answers[sql]
        if callable(answer):
            answer = answer()
        if isinstance(answer, BaseException):
            raise answer
        return _Result(answer)

    def rollback(self):
        self.log.append((self.name, "rollback"))
        self._maybe_fail("rollback")

    def invalidate(self):
        self.log.append((self.name, "invalidate"))
        self._maybe_fail("invalidate")

    def close(self):
        self.log.append((self.name, "close"))
        self.closed = True
        self._maybe_fail("close")

    def commit(self):  # pragma: no cover -- must never be called
        self.log.append((self.name, "commit"))


class FakeEngine:
    def __init__(self, name, log, connection=None, *, connect_error=None, dispose_error=None, pooled=False):
        self.name, self.log, self.connection = name, log, connection
        self.connect_error, self.dispose_error = connect_error, dispose_error
        self.pool = QueuePool(lambda: None) if pooled else NullPool(lambda: None)

    def connect(self):
        self.log.append((self.name, "connect"))
        if self.connect_error is not None:
            raise self.connect_error
        return self.connection

    def dispose(self):
        self.log.append((self.name, "dispose"))
        if self.dispose_error is not None:
            raise self.dispose_error


def lock_answers(**overrides):
    answers = {
        lock_module._IDENTITY_SQL: [(4242, START)],
        VERIFY: [(DB, "PostgreSQL 16.4")],
        DB_FACTS: [(16384, "napsedge_migrator", 160004)],
        SYSID_PRIV: [(True,)],
        SYSID: [("7412345678901234567",)],
        TRY_LOCK: [(True,)],
        UNLOCK: [(True,)],
        HELD: [(4242, 1)],
    }
    answers.update(overrides)
    return answers


def verifier_answers(**overrides):
    answers = {VERIFY: [(DB, "PostgreSQL 16.4")], lock_module._ORIGINAL_PRESENT_SQL: [(0,)], lock_module._KEY_HOLDERS_SQL: []}
    answers.update(overrides)
    return answers


class World:
    """A factory handing out the lock engine first, then the verifier engine; everything is logged in one ordered list."""

    def __init__(self, *, lock=None, verifier=None, lock_fail=None, verifier_fail=None, lock_engine_kw=None, verifier_engine_kw=None):
        self.log: list = []
        self.lock_conn = FakeConnection("L", self.log, lock_answers(**(lock or {})), fail=lock_fail)
        self.verifier_conn = FakeConnection("V", self.log, verifier_answers(**(verifier or {})), fail=verifier_fail)
        self.engines = {
            LOCK_APPLICATION_NAME: FakeEngine("lock_engine", self.log, self.lock_conn, **(lock_engine_kw or {})),
            VERIFIER_APPLICATION_NAME: FakeEngine("verifier_engine", self.log, self.verifier_conn, **(verifier_engine_kw or {})),
        }
        self.requested: list = []

    def factory(self, url, application_name):
        self.requested.append(application_name)
        return self.engines[application_name]

    def steps(self, name):
        return [entry[1] if entry[1] != "execute" else entry[2] for entry in self.log if entry[0] == name]


def run(world, body=lambda session: None, *, name=DB):
    entered = []

    def wrapped(session):
        entered.append(session)
        return body(session)

    with backup_capture_lock(URL, designation(name), _engine_factory=world.factory) as session:
        wrapped(session)
    return session, entered


def run_expecting(exc_type, world, body=lambda session: None, *, name=DB):
    """Catches ANY escaping exception and asserts its exact type, so a wrong exception is an assertion failure, not an error."""
    entered: list = []
    with pytest.raises(BaseException) as info:
        with backup_capture_lock(URL, designation(name), _engine_factory=world.factory) as session:
            entered.append(session)
            body(session)
    assert type(info.value) is exc_type, f"expected {exc_type.__name__}, got {type(info.value).__name__}"
    return info, entered


def no_secret(text: str) -> None:
    assert SECRET not in text and HOST not in text and "@" not in text and "sslrootcert=" not in text


@pytest.fixture(autouse=True)
def fast_verification(monkeypatch):
    monkeypatch.setattr(lock_module, "_VERIFY_DEADLINE_S", 0.2)
    monkeypatch.setattr(lock_module, "_VERIFY_POLL_S", 0.01)


# ---------------------------------------------------------------------------
# inputs are validated before anything is created
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("url", [
    f"postgresql+psycopg2://u:{SECRET}@{HOST}/{DB}?sslmode=verify-full&sslrootcert=/ca.pem",
    f"postgresql+psycopg://u:{SECRET}@{HOST}/{DB}",
    f"postgresql+psycopg://u:{SECRET}@{HOST}/{DB}?sslmode=require&sslrootcert=/ca.pem",
    f"postgresql+psycopg://u:{SECRET}@{HOST}/{DB}?sslmode=verify-full",
    f"postgresql+psycopg://u:{SECRET}@/{DB}?sslmode=verify-full&sslrootcert=/ca.pem",
    f"postgresql+psycopg://u:{SECRET}@{HOST}/?sslmode=verify-full&sslrootcert=/ca.pem",
    f"not a url {SECRET} {HOST}",
    12345,
])
def test_a_url_without_verified_tls_or_target_is_refused_before_anything_is_created_and_never_echoed(url):
    world = World()
    with pytest.raises(ValidationError) as info:
        with backup_capture_lock(url, designation(), _engine_factory=world.factory):
            pytest.fail("the body must not run")
    no_secret(str(info.value))
    assert world.requested == [] and world.log == []


def test_a_url_object_is_accepted():
    world = World()
    with backup_capture_lock(make_url(URL), designation(), _engine_factory=world.factory) as session:
        assert session.backend_pid == 4242


def test_a_non_designation_is_refused_before_anything_is_created():
    world = World()
    with pytest.raises(ValidationError):
        with backup_capture_lock(URL, "napsedge_pg_test_lock", _engine_factory=world.factory):
            pytest.fail("the body must not run")
    assert world.requested == []


def test_a_pooled_engine_is_refused_disposed_and_never_connected():
    world = World(lock_engine_kw={"pooled": True})
    with pytest.raises(ValidationError):
        with backup_capture_lock(URL, designation(), _engine_factory=world.factory):
            pytest.fail("the body must not run")
    assert world.log == [("lock_engine", "dispose")]


# ---------------------------------------------------------------------------
# A. normal operation (injected)
# ---------------------------------------------------------------------------
def test_normal_operation_order_session_contents_and_confirmed_cleanup():
    world = World()
    session, entered = run(world)
    assert entered == [session]
    assert (session.backend_pid, session.backend_start, session.verified_target.database_name) == (4242, START, DB)
    assert not hasattr(session, "connection") and not hasattr(session, "__dict__")
    assert world.steps("lock_engine") == ["connect", "dispose"]
    assert world.steps("L") == [lock_module._IDENTITY_SQL, VERIFY, DB_FACTS, SYSID_PRIV, SYSID, "rollback", TRY_LOCK, "rollback", UNLOCK, "close"]
    assert (session.system_identifier, session.database_oid) == ("7412345678901234567", 16384)
    assert world.steps("V")[0] == VERIFY and world.steps("V")[-1] == "close"
    assert world.requested == [LOCK_APPLICATION_NAME, VERIFIER_APPLICATION_NAME]
    report = session.cleanup
    assert report.confirmed and report.verification == VERIFIED_CLOSED and report.lock_acquired
    assert (report.connection_discarded, report.connection_closed, report.engine_disposed, report.other_holder_present) == (False, True, True, False)
    assert "commit" not in [entry[1] for entry in world.log]


def test_the_body_runs_after_the_post_acquisition_rollback_and_before_the_single_unlock():
    world = World()
    marks = []
    run(world, lambda session: marks.append(len(world.log)))
    executed = [entry[2] if entry[1] == "execute" else entry[1] for entry in world.log if entry[0] == "L"]
    assert executed.count(TRY_LOCK) == 1 and executed.count(UNLOCK) == 1
    body_at = marks[0]
    assert world.log[body_at - 1] == ("L", "rollback")
    assert world.log[body_at] == ("L", "execute", UNLOCK)


# ---------------------------------------------------------------------------
# B. a primary body failure is preserved; the owned session is discarded
# ---------------------------------------------------------------------------
def test_a_primary_body_failure_is_the_same_object_with_its_cause_notes_and_traceback_and_the_session_is_discarded():
    world = World()
    cause = ValueError("cause")
    primary = RuntimeError("body failed")
    primary.__cause__ = cause
    primary.add_note("caller note")

    def body(session):
        raise primary

    info, entered = run_expecting(RuntimeError, world, body)
    assert info.value is primary and info.value.__cause__ is cause and info.value.__notes__ == ["caller note"]
    assert any(frame.name == "body" for frame in traceback.extract_tb(info.value.__traceback__))
    assert world.steps("L")[-3:] == [UNLOCK, "invalidate", "close"]
    assert entered[0].cleanup.connection_discarded and entered[0].cleanup.confirmed


# ---------------------------------------------------------------------------
# E. refusal never runs the body and never unlocks
# ---------------------------------------------------------------------------
def test_acquisition_refusal_never_enters_the_body_never_unlocks_and_still_cleans_up():
    world = World(lock={TRY_LOCK: [(False,)]}, verifier={lock_module._KEY_HOLDERS_SQL: [(77,)]})
    info, entered = run_expecting(AdvisoryLockUnavailableError, world)
    assert entered == []
    assert UNLOCK not in world.steps("L")
    assert world.steps("L")[-2:] == ["invalidate", "close"]
    assert world.steps("lock_engine")[-1] == "dispose"
    assert getattr(info.value, "__notes__", []) == []  # cleanup confirmed: another holder is reported, never mistaken for a leak


# ---------------------------------------------------------------------------
# F. wrong target and setup failure
# ---------------------------------------------------------------------------
def test_a_wrong_target_never_acquires_or_enters_the_body_and_cleans_up():
    world = World(lock={VERIFY: [("napsedge_pg_test_other", "PostgreSQL 16.4")]})
    info, entered = run_expecting(DatabaseTargetVerificationError, world)
    assert entered == [] and TRY_LOCK not in world.steps("L")
    assert world.steps("L")[-2:] == ["invalidate", "close"] and world.steps("lock_engine")[-1] == "dispose"


def test_a_connection_failure_is_a_sanitized_setup_error_with_no_chained_driver_message():
    world = World(lock_engine_kw={"connect_error": OperationalError("connect", {}, DriverFailure(f"password authentication failed for {SECRET}@{HOST}"))})
    info, entered = run_expecting(BackupLockSetupError, world)
    assert entered == [] and str(info.value) == "opening the dedicated backup lock session failed (OperationalError)"
    assert info.value.__cause__ is None and info.value.__suppress_context__
    no_secret("".join(traceback.format_exception(info.value)))
    assert world.steps("lock_engine") == ["connect", "dispose"] and world.requested == [LOCK_APPLICATION_NAME]


def test_a_lost_identity_query_is_preserved_and_cleanup_is_reported_as_not_verified():
    lost = DriverFailure("server closed the connection")
    world = World(lock={lock_module._IDENTITY_SQL: lost})
    info, entered = run_expecting(DriverFailure, world)
    assert info.value is lost and entered == []
    assert info.value.__notes__ == ["backup lock session cleanup was not confirmed: verification=not_verified; failed steps=none"]
    assert world.requested == [LOCK_APPLICATION_NAME]


# ---------------------------------------------------------------------------
# G. release failures: the shared manager's behavior is unchanged; the session is discarded
# ---------------------------------------------------------------------------
def test_a_raised_unlock_after_a_successful_body_propagates_unchanged_and_the_session_is_discarded():
    release_error = DriverFailure("unlock failed")
    world = World(lock={UNLOCK: release_error})
    info, _ = run_expecting(DriverFailure, world)
    assert info.value is release_error
    assert world.steps("L")[-2:] == ["invalidate", "close"]


def test_a_false_unlock_after_a_successful_body_is_the_managers_error_and_the_session_is_discarded():
    world = World(lock={UNLOCK: [(False,)]})
    info, _ = run_expecting(AdvisoryLockUnavailableError, world)
    assert "was not held by this session" in str(info.value)
    assert world.steps("L")[-2:] == ["invalidate", "close"]


@pytest.mark.parametrize("unlock, note", [
    (DriverFailure("unlock failed"), RELEASE_FAILED_NOTE.format(error_type="DriverFailure")),
    ([(False,)], RELEASE_NOT_HELD_NOTE),
])
def test_with_a_primary_failure_the_managers_release_note_is_kept_and_nothing_else_is_added_when_cleanup_is_confirmed(unlock, note):
    world = World(lock={UNLOCK: unlock})
    primary = RuntimeError("body failed")

    def body(session):
        raise primary

    info, _ = run_expecting(RuntimeError, world, body)
    assert info.value is primary and info.value.__notes__ == [note]
    assert world.steps("L")[-2:] == ["invalidate", "close"]


# ---------------------------------------------------------------------------
# H. cleanup and verification failures
# ---------------------------------------------------------------------------
def test_a_close_failure_without_a_primary_is_visible_and_the_remaining_steps_still_run():
    world = World(lock_fail={"close": DriverFailure(f"close {SECRET}")})
    info, entered = run_expecting(BackupLockCleanupError, world)
    report = info.value.report
    assert report.failures == ("close:DriverFailure",) and not report.confirmed
    assert report.engine_disposed and report.verification == VERIFIED_CLOSED
    assert world.steps("lock_engine")[-1] == "dispose" and world.steps("V")
    no_secret(str(info.value))
    assert entered[0].cleanup is report


def test_with_a_primary_failure_a_discard_failure_adds_one_sanitized_note_and_close_and_dispose_still_run():
    world = World(lock_fail={"invalidate": DriverFailure(f"invalidate {SECRET} {HOST}")})
    primary = RuntimeError("body failed")

    def body(session):
        raise primary

    info, _ = run_expecting(RuntimeError, world, body)
    assert info.value is primary
    assert info.value.__notes__ == ["backup lock session cleanup was not confirmed: verification=closed_and_released; failed steps=discard:DriverFailure"]
    assert world.steps("L")[-2:] == ["invalidate", "close"] and world.steps("lock_engine")[-1] == "dispose"
    no_secret("".join(info.value.__notes__))


def test_an_original_session_still_present_at_the_deadline_is_never_reported_as_success():
    world = World(verifier={lock_module._ORIGINAL_PRESENT_SQL: [(1,)]})
    info, _ = run_expecting(BackupLockCleanupError, world)
    assert info.value.report.verification == ORIGINAL_SESSION_PRESENT and not info.value.report.confirmed
    polls = world.steps("V").count(lock_module._ORIGINAL_PRESENT_SQL)
    assert polls > 1  # bounded polling ran more than once before the deadline


def test_another_holder_of_the_key_is_reported_and_not_mistaken_for_the_original_session():
    world = World(verifier={lock_module._KEY_HOLDERS_SQL: [(9001,)]})
    session, _ = run(world)
    assert session.cleanup.confirmed and session.cleanup.other_holder_present is True


@pytest.mark.parametrize("verifier_kw, failure", [
    ({"verifier_engine_kw": {"connect_error": DriverFailure(f"no route {HOST}")}}, "verify:DriverFailure"),
    ({"verifier": {VERIFY: [("napsedge_pg_test_other", "PostgreSQL 16.4")]}}, "verify:DatabaseTargetVerificationError"),
    ({"verifier_engine_kw": {"pooled": True}}, "verify:ValidationError"),
])
def test_inability_to_verify_is_visible_without_a_primary_and_a_note_with_one(verifier_kw, failure):
    world = World(**verifier_kw)
    info, _ = run_expecting(BackupLockCleanupError, world)
    assert info.value.report.verification == NOT_VERIFIED and failure in info.value.report.failures
    no_secret(str(info.value))

    world = World(**verifier_kw)
    primary = RuntimeError("body failed")

    def body(session):
        raise primary

    info, _ = run_expecting(RuntimeError, world, body)
    assert info.value is primary and len(info.value.__notes__) == 1 and "verification=not_verified" in info.value.__notes__[0]


# ---------------------------------------------------------------------------
# I. interrupts
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit])
def test_a_primary_interrupt_is_preserved_when_ordinary_cleanup_fails(interrupt_type):
    world = World(lock_fail={"close": DriverFailure("close failed")})
    primary = interrupt_type()

    def body(session):
        raise primary

    info, _ = run_expecting(interrupt_type, world, body)
    assert info.value is primary and "close:DriverFailure" in info.value.__notes__[0]
    assert world.steps("lock_engine")[-1] == "dispose"


def test_a_new_interrupt_during_cleanup_is_never_swallowed_and_the_remaining_steps_still_run():
    new_interrupt = KeyboardInterrupt()
    world = World(lock_fail={"close": new_interrupt})
    info, entered = run_expecting(KeyboardInterrupt, world)
    assert info.value is new_interrupt
    assert world.steps("lock_engine")[-1] == "dispose"
    assert world.requested == [LOCK_APPLICATION_NAME]  # verification is not attempted while an interrupt is pending
    assert entered[0].cleanup.verification == NOT_VERIFIED


def test_a_new_interrupt_during_cleanup_keeps_the_primary_reachable_as_its_context():
    new_interrupt = KeyboardInterrupt()
    world = World(lock_fail={"invalidate": new_interrupt})
    primary = RuntimeError("body failed")

    def body(session):
        raise primary

    info, _ = run_expecting(KeyboardInterrupt, world, body)
    assert info.value is new_interrupt and info.value.__context__ is primary
    assert world.steps("L")[-1] == "close" and world.steps("lock_engine")[-1] == "dispose"


# ---------------------------------------------------------------------------
# the default engine: unpooled, and never reconnects
# ---------------------------------------------------------------------------
def test_the_default_engine_is_unpooled_hides_parameters_and_refuses_a_second_physical_connect():
    url = make_url("postgresql+psycopg://u:x@127.0.0.1:9/db?sslmode=verify-full&sslrootcert=/nonexistent-ca.pem&connect_timeout=2")
    engine = lock_module._build_engine(url, LOCK_APPLICATION_NAME)
    try:
        assert type(engine.pool) is NullPool and engine.hide_parameters
        with pytest.raises(OperationalError):
            engine.connect()  # the first physical attempt is allowed (and fails: nothing listens there)
        with pytest.raises(BaseException) as second:
            engine.connect()  # a second physical connect is refused before it reaches the network
        assert type(second.value) is BackupLockSessionLost, type(second.value).__name__
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# static constraints
# ---------------------------------------------------------------------------
_SOURCE = inspect.getsource(lock_module)
_TREE = ast.parse(_SOURCE)
_DOCSTRINGS = {id(node.body[0].value) for node in ast.walk(_TREE)
               if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body and isinstance(node.body[0], ast.Expr)
               and isinstance(node.body[0].value, ast.Constant)}
#: every string literal the module can actually execute or emit (docstrings excluded: they describe what is NOT done)
_CODE_STRINGS = " ".join(n.value for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in _DOCSTRINGS)


def test_the_module_reuses_the_shared_manager_and_adds_no_unlock_of_its_own():
    for forbidden in ("pg_advisory_unlock", "pg_advisory_lock", "pg_try_advisory_lock", "pg_terminate_backend", "pg_cancel_backend", "GRANT", "SET ROLE"):
        assert forbidden not in _CODE_STRINGS, forbidden
    names = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)}
    assert {"bulk_import_advisory_lock", "verify_database_target", "compute_advisory_lock_key", "NullPool"} <= names


def test_the_module_never_commits_and_never_reads_notes_and_imports_no_application_engine():
    called = {n.func.attr for n in ast.walk(_TREE) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "commit" not in called and "begin" not in called
    assert not any(isinstance(n, ast.Attribute) and n.attr == "__notes__" for n in ast.walk(_TREE))
    imported = {n.module for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(_TREE) if isinstance(n, ast.Import) for a in n.names}
    assert imported == {"__future__", "time", "contextlib", "dataclasses", "typing", "sqlalchemy", "sqlalchemy.engine", "sqlalchemy.pool",
                        "src.deployment.backup_capture_preflight", "src.exceptions", "src.pipeline.bulk_import_safety"}
    for node in _TREE.body:
        assert isinstance(node, (ast.Expr, ast.ImportFrom, ast.Import, ast.Assign, ast.AnnAssign, ast.FunctionDef, ast.ClassDef)), type(node)


def test_public_symbols_are_exactly_the_documented_ones():
    assert sorted(lock_module.__all__) == sorted([
        "LOCK_APPLICATION_NAME", "VERIFIER_APPLICATION_NAME", "VERIFIED_CLOSED", "ORIGINAL_SESSION_PRESENT", "NOT_VERIFIED",
        "NO_SESSION_CREATED", "BackupLockSetupError", "BackupLockSessionLost", "BackupLockCleanupError", "BackupLockNotHeld", "LockCleanupReport",
        "BackupLockSession", "backup_capture_lock",
    ])
    assert NO_SESSION_CREATED == "no_session_created"


# ---------------------------------------------------------------------------
# Phase 9B-6C additions: identity facts read on L itself, and confirm_held()
# ---------------------------------------------------------------------------
def test_an_unreadable_system_identifier_is_recorded_as_none_and_never_queried():
    world = World(lock={SYSID_PRIV: [(False,)]})
    session, _ = run(world)
    assert session.system_identifier is None and session.database_oid == 16384
    assert SYSID not in world.steps("L") and session.cleanup.confirmed


def test_confirm_held_asks_l_itself_then_ends_that_transaction():
    world = World()
    results = []
    session, _ = run(world, lambda s: results.append(s.confirm_held()))
    assert results == [None]
    steps = world.steps("L")
    assert steps[steps.index(HELD) + 1] == "rollback"


@pytest.mark.parametrize("held_rows", [[(4242, 0)], [(9999, 1)]], ids=["not_held", "different_backend"])
def test_confirm_held_raises_when_l_does_not_hold_the_key(held_rows):
    world = World(lock={HELD: held_rows})
    info, _ = run_expecting(BackupLockNotHeld, world, lambda s: s.confirm_held())
    assert str(info.value) == "the backup lock session no longer holds the lock"


def test_confirm_held_on_a_lost_l_is_sanitized_and_never_reconnects():
    world = World(lock={HELD: DriverFailure(f"server closed the connection {SECRET} {HOST}")})
    info, _ = run_expecting(BackupLockNotHeld, world, lambda s: s.confirm_held())
    assert str(info.value) == "the backup lock session could not confirm it holds the lock (DriverFailure)"
    assert info.value.__cause__ is None and info.value.__suppress_context__
    assert world.steps("lock_engine").count("connect") == 1
    no_secret("".join(traceback.format_exception(info.value)))


def test_confirm_held_is_rejected_after_the_context_exits():
    world = World()
    session, _ = run(world)
    with pytest.raises(BackupLockNotHeld, match="not active"):
        session.confirm_held()
    assert HELD not in world.steps("L")
