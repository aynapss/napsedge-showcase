"""
Phase 9B-6C: deterministic unit tests for `src/deployment/backup_capture_snapshot.py` with fake engines and connections.
Everything here is INJECTED behavior: ordering, refusals before any handle exists, lock checks at the boundaries,
cleanup and exception preservation, and the baseline reader's exact SQL compared with the test layer's
`capture_baseline`. Real PostgreSQL behavior (transaction settings, snapshot import, commit visibility, backend
absence) lives in `tests/postgres/test_postgres_backup_capture_snapshot.py`.
"""
from __future__ import annotations

import ast
import datetime as dt
import inspect
import traceback
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool, QueuePool

import src.deployment.backup_capture_lock as lock_module
import src.deployment.backup_capture_preflight as preflight_module
import src.deployment.backup_capture_snapshot as snap
from src.deployment.backup_artifact import BaselineDocument
from src.deployment.backup_capture_lock import VERIFIER_APPLICATION_NAME, BackupLockNotHeld, BackupLockSession
from src.deployment.backup_capture_snapshot import (
    SNAPSHOT_APPLICATION_NAME,
    SnapshotCleanupError,
    SnapshotHandleClosed,
    SnapshotRefusedError,
    SnapshotSessionLost,
    SnapshotSetupError,
    backup_capture_snapshot,
)
from src.exceptions import DatabaseTargetVerificationError, ValidationError
from src.pipeline.bulk_import_safety import DatabaseTargetDesignation, VerifiedDatabaseTarget, build_expected_acknowledgement
from tests.postgres import pg_restore_compare

DB = "napsedge_pg_test_snapshot"
HEAD = "5b02bea0eb85"
SECRET = "Pl4nted-S3cret-zz9"
HOST = "db-secret-host.internal"
URL = f"postgresql+psycopg://napsedge_backup_ro:{SECRET}@{HOST}:5432/{DB}?sslmode=verify-full&sslrootcert=/ca.pem"
ARTIFACT_ID = "0b3c2f6e-1d2a-4c5b-9e8f-7a6b5c4d3e2f"
SYSID = "7412345678901234567"
T0 = dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.timezone.utc)
START = dt.datetime(2026, 10, 5, 11, 59, tzinfo=dt.timezone.utc)
M, R, B = "napsedge_migrator", "napsedge_runtime_ro", "napsedge_backup_ro"
BIGINT_MAX = 2**63 - 1
SNAP_ID = "00000004-00000002-1"
DESCRIPTOR = "100:100:"
VERIFY = "SELECT current_database(), version()"
FINGERPRINT_TEAMS = snap._FINGERPRINT_SQL.format(table="teams")
STATE = snap._SEQUENCE_STATE_SQL.format(name="team_aliases_id_seq")
COLMAX = snap._COLUMN_MAX_SQL.format(column="id", table="team_aliases")
SET_LOCAL = f"SET LOCAL lock_timeout = {snap._LOCK_TIMEOUT_MS}"
INVENTORY_ROW = ("team_aliases_id_seq", "team_aliases", "id", "bigint", 1, 1, BIGINT_MAX, 1, False)


def designation(name: str = DB) -> DatabaseTargetDesignation:
    return DatabaseTargetDesignation(expected_database_name=name, acknowledgement=build_expected_acknowledgement(name))


def snapshot_answers(**overrides):
    p = preflight_module
    answers = {
        SET_LOCAL: [],
        snap._TRANSACTION_SQL: [("repeatable read", "on", 5151, START, T0)],
        VERIFY: [(DB, "PostgreSQL 16.4")],
        p._DATABASE_SQL: [(16384, M, 160004)],
        p._SYSTEM_IDENTIFIER_PRIVILEGE_SQL: [(True,)],
        p._SYSTEM_IDENTIFIER_SQL: [(SYSID,)],
        p._ALEMBIC_TABLE_PRESENT_SQL: [(True,)],
        p._ALEMBIC_HEADS_SQL: [(HEAD,)],
        p._RELATIONS_SQL: [("teams", "r", M), ("team_aliases", "r", M), ("team_aliases_id_seq", "S", M)],
        p._RELATION_GRANTEES_SQL: [("teams", "r", M, M), ("teams", "r", M, B)],
        p._COLUMN_GRANTEES_SQL: [],
        p._OTHER_OBJECTS_SQL: [],
        p._SEQUENCE_INVENTORY_SQL: [INVENTORY_ROW],
        snap._EXPORT_SQL: [(SNAP_ID,)],
        snap._DESCRIPTOR_SQL: [(DESCRIPTOR,)],
        snap._ACL_SQL: [("team_aliases", "r", M, ""), ("team_aliases_id_seq", "S", M, ""), ("teams", "r", M, "")],
        snap._PUBLIC_TABLES_SQL: [("team_aliases",), ("teams",)],
        snap._FINGERPRINT_SQL.format(table="team_aliases"): [(3, "0123456789abcdef0123456789abcdef")],
        FINGERPRINT_TEAMS: [(0, "empty")],
        snap._CONSTRAINT_SQL: [("teams", "teams_pkey", "p", True, "PRIMARY KEY (team_id)")],
        snap._INDEX_SQL: [("teams", "teams_pkey", "CREATE UNIQUE INDEX teams_pkey ON public.teams USING btree (team_id)")],
        snap._SEQUENCES_SQL: [("team_aliases_id_seq", 3)],
        snap._VERSION_SQL: [(HEAD,)],
        snap._MAX_ALIAS_SQL: [(3,)],
        STATE: [(3, True)],
        COLMAX: [(3,)],
        snap._NOW_SQL: [(T0,)],
        snap._ALIVE_SQL: [(5151, DESCRIPTOR, T0)],
    }
    answers.update(overrides)
    return answers


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def one(self):
        assert len(self._rows) == 1
        return self._rows[0]

    def scalar_one(self):
        return self.one()[0]

    def scalar(self):
        return self._rows[0][0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConnection:
    def __init__(self, name, log, answers, fail=None):
        self.name, self.log, self.answers, self.fail = name, log, answers, fail or {}
        self.closed = False
        self.engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        self.options: dict = {}

    def _step(self, step):
        self.log.append((self.name, step))
        if step in self.fail:
            raise self.fail[step]

    def execution_options(self, **options):
        self.log.append((self.name, f"options:{sorted(options.items())}"))
        self.options.update(options)
        return self

    def begin(self):
        self._step("begin")

    def execute(self, statement, params=None):
        sql = str(statement)
        self.log.append((self.name, sql))
        answer = self.answers[sql]
        if callable(answer):
            answer = answer()
        if isinstance(answer, BaseException):
            raise answer
        return _Result(answer)

    def rollback(self):
        self._step("rollback")

    def invalidate(self):
        self._step("invalidate")

    def close(self):
        self.closed = True
        self._step("close")


class FakeEngine:
    def __init__(self, name, log, connection, *, connect_error=None, pooled=False):
        self.name, self.log, self.connection, self.connect_error = name, log, connection, connect_error
        self.pool = QueuePool(lambda: None) if pooled else NullPool(lambda: None)

    def connect(self):
        self.log.append((self.name, "connect"))
        if self.connect_error is not None:
            raise self.connect_error
        return self.connection

    def dispose(self):
        self.log.append((self.name, "dispose"))


class World:
    def __init__(self, *, answers=None, fail=None, engine_kw=None, verifier=None, lock_kw=None, confirm_results=None):
        self.log: list = []
        self.conn = FakeConnection("S", self.log, snapshot_answers(**(answers or {})), fail)
        verifier_answers = {VERIFY: [(DB, "PostgreSQL 16.4")], lock_module._ORIGINAL_PRESENT_SQL: [(0,)], lock_module._KEY_HOLDERS_SQL: [(4242,)]}
        verifier_answers.update(verifier or {})
        self.verifier = FakeConnection("V", self.log, verifier_answers)
        self.engines = {SNAPSHOT_APPLICATION_NAME: FakeEngine("snapshot_engine", self.log, self.conn, **(engine_kw or {})),
                        VERIFIER_APPLICATION_NAME: FakeEngine("verifier_engine", self.log, self.verifier)}
        self.requested: list = []
        self.confirm_results = list(confirm_results or [])
        self.confirms = 0
        kw = {"system_identifier": SYSID, "database_oid": 16384}
        kw.update(lock_kw or {})
        self.lock = BackupLockSession(VerifiedDatabaseTarget(DB, "PostgreSQL 16.4"), 123, 4242, START, confirm=self._confirm, **kw)

    def _confirm(self):
        self.log.append(("L", "confirm_held"))
        self.confirms += 1
        if self.confirm_results:
            outcome = self.confirm_results.pop(0)
            if outcome is not None:
                raise outcome

    def factory(self, url, application_name):
        self.requested.append(application_name)
        return self.engines[application_name]

    def steps(self, name):
        return [step for who, step in self.log if who == name]


def run(world, body=lambda handle: None, *, name=DB):
    with backup_capture_snapshot(world.lock, URL, designation(name), expected_alembic_head=HEAD, artifact_id=ARTIFACT_ID,
                                 _engine_factory=world.factory) as handle:
        body(handle)
    return handle


def run_expecting(exc_type, world, body=lambda handle: None, *, name=DB, **kw):
    entered: list = []
    with pytest.raises(BaseException) as info:
        with backup_capture_snapshot(kw.pop("lock", world.lock), kw.pop("url", URL), designation(name), expected_alembic_head=kw.pop("head", HEAD),
                                     artifact_id=kw.pop("artifact_id", ARTIFACT_ID), _engine_factory=world.factory) as handle:
            entered.append(handle)
            body(handle)
    assert type(info.value) is exc_type, f"expected {exc_type.__name__}, got {type(info.value).__name__}: {info.value}"
    return info, entered


def no_secret(text: str) -> None:
    assert SECRET not in text and HOST not in text and "sslrootcert=" not in text


@pytest.fixture(autouse=True)
def fast_verification(monkeypatch):
    monkeypatch.setattr(lock_module, "_VERIFY_DEADLINE_S", 0.2)
    monkeypatch.setattr(lock_module, "_VERIFY_POLL_S", 0.01)


# ---------------------------------------------------------------------------
# normal path
# ---------------------------------------------------------------------------
def test_normal_order_isolation_before_the_transaction_lock_checks_at_each_boundary_and_confirmed_cleanup():
    world = World()
    seen = []
    handle = run(world, lambda h: seen.append((h.snapshot_id, h.visibility_descriptor, h.facts, h.baseline)))
    s = world.steps("S")
    assert s[0] == "options:[('isolation_level', 'REPEATABLE READ'), ('postgresql_readonly', True)]" and s[1] == "begin"
    assert s[2] == SET_LOCAL and s[3] == snap._TRANSACTION_SQL
    assert s.index(snap._EXPORT_SQL) > s.index(preflight_module._SEQUENCE_INVENTORY_SQL)  # preflight before export
    assert s.index(snap._ACL_SQL) > s.index(snap._DESCRIPTOR_SQL)  # baseline after export
    assert s[-3:] == [snap._ALIVE_SQL, "rollback", "close"]
    assert "invalidate" not in s and "commit" not in s
    order = [step for who, step in world.log if (who, step) in {("L", "confirm_held"), ("S", snap._EXPORT_SQL), ("S", snap._NOW_SQL), ("S", snap._ALIVE_SQL)}]
    assert order == ["confirm_held", "confirm_held", snap._EXPORT_SQL, snap._NOW_SQL, "confirm_held", "confirm_held", snap._ALIVE_SQL]
    snapshot_id, descriptor, facts, baseline = seen[0]
    assert (snapshot_id, descriptor) == (SNAP_ID, DESCRIPTOR)
    assert (facts.backend_pid, facts.lock_backend_pid, facts.isolation, facts.read_only, facts.system_identifier, facts.database_oid) == (5151, 4242, "repeatable read", "on", SYSID, 16384)
    assert isinstance(baseline, BaselineDocument) and baseline.artifact_id == ARTIFACT_ID
    assert [f.table for f in baseline.fingerprints] == ["team_aliases", "teams"] and baseline.fingerprints[1].md5 == "empty"
    assert baseline.sequence_details[0].last_value == 3 and baseline.sequence_details[0].column_max == 3
    assert handle.cleanup.confirmed and handle.cleanup.transaction_ended and not handle.cleanup.connection_discarded
    assert world.steps("snapshot_engine") == ["connect", "dispose"] and world.requested == [SNAPSHOT_APPLICATION_NAME, VERIFIER_APPLICATION_NAME]


@pytest.mark.parametrize("accessor", ["snapshot_id", "visibility_descriptor", "baseline", "preflight", "facts"])
def test_every_handle_accessor_is_rejected_after_exit(accessor):
    handle = run(World())
    with pytest.raises(SnapshotHandleClosed):
        getattr(handle, accessor)
    assert handle.cleanup.confirmed


def test_no_sequence_is_advanced_set_or_repaired_and_nothing_writes():
    source = inspect.getsource(snap)
    strings = " ".join(n.value for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Constant) and isinstance(n.value, str) and not n.value.startswith("\nPhase"))
    for forbidden in ("nextval", "setval", "pg_advisory", "pg_terminate_backend", "INSERT", "UPDATE ", "DELETE", "ALTER ", "CREATE ", "GRANT", "COMMIT"):
        assert forbidden not in strings, forbidden
    world = World()
    run(world)
    assert all(step.lstrip().startswith(("SELECT", "SET LOCAL", "options:")) or step in ("begin", "rollback", "close") for step in world.steps("S"))


# ---------------------------------------------------------------------------
# the baseline reader equals the established test-layer reads, statement for statement
# ---------------------------------------------------------------------------
class _Recorder:
    def __init__(self, answers, log):
        self.answers, self.log = answers, log

    def execute(self, statement, params=None):
        self.log.append(str(statement))
        return _Result(self.answers[str(statement)])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecordingEngine:
    """Not a Connection, so the test-layer helpers take their Engine path and call connect() per read; each read is recorded."""

    def __init__(self, answers):
        self.answers, self.log = answers, []

    def connect(self):
        return _Recorder(self.answers, self.log)


def test_the_production_baseline_reads_are_the_test_layers_capture_baseline_reads_in_the_same_order():
    engine = _RecordingEngine(snapshot_answers())
    test_layer = pg_restore_compare.capture_baseline(engine)
    world = World()
    captured = []
    run(world, lambda h: captured.append(h.baseline))
    s = world.steps("S")
    first = s.index(snap._ACL_SQL)
    production = s[first: s.index(preflight_module._SEQUENCE_INVENTORY_SQL, first)]
    assert production == engine.log  # identical statements, identical order
    baseline = captured[0]
    assert {f.table: (f.rows, f.md5) for f in baseline.fingerprints} == test_layer.fingerprints
    assert [(a.relation, a.relkind, a.owner, a.acl) for a in baseline.acls] == test_layer.acls
    assert [(c.table, c.name, c.contype, c.validated, c.definition) for c in baseline.constraints] == test_layer.constraints
    assert [(i.table, i.name, i.definition) for i in baseline.indexes] == test_layer.indexes
    assert [(q.name, q.last_value) for q in baseline.sequences] == test_layer.sequences
    assert (baseline.version, baseline.max_alias_id) == (test_layer.version, test_layer.max_alias_id)


# ---------------------------------------------------------------------------
# refusals: nothing usable is yielded
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("facts", [("read committed", "on"), ("repeatable read", "off"), ("serializable", "on")])
def test_a_transaction_that_is_not_repeatable_read_read_only_is_refused_before_any_other_read(facts):
    world = World(answers={snap._TRANSACTION_SQL: [(*facts, 5151, START, T0)]})
    info, entered = run_expecting(SnapshotRefusedError, world)
    assert entered == [] and VERIFY not in world.steps("S") and snap._EXPORT_SQL not in world.steps("S")
    assert world.steps("S")[-2:] == ["invalidate", "close"]


def test_the_same_backend_as_the_lock_session_is_refused():
    world = World(answers={snap._TRANSACTION_SQL: [("repeatable read", "on", 4242, START, T0)]})
    info, entered = run_expecting(SnapshotRefusedError, world)
    assert entered == [] and "different backend" in str(info.value)


def test_an_unsupported_source_is_refused_with_its_preflight_facts_and_never_exported():
    world = World(answers={preflight_module._RELATION_GRANTEES_SQL: [("teams", "r", M, "PUBLIC")]})
    info, entered = run_expecting(SnapshotRefusedError, world)
    assert entered == [] and snap._EXPORT_SQL not in world.steps("S")
    assert info.value.preflight is not None and not info.value.preflight.accepted


def test_a_wrong_target_never_exports():
    world = World(answers={VERIFY: [("napsedge_pg_test_other", "PostgreSQL 16.4")]})
    info, entered = run_expecting(DatabaseTargetVerificationError, world)
    assert entered == [] and snap._EXPORT_SQL not in world.steps("S")


@pytest.mark.parametrize("answers, lock_kw, field", [
    ({}, {"system_identifier": None}, "not available"),
    ({preflight_module._SYSTEM_IDENTIFIER_PRIVILEGE_SQL: [(False,)]}, {}, "not available"),
    ({}, {"system_identifier": "1111111111111111111"}, "(system_identifier)"),
    ({}, {"database_oid": 99999}, "(database_oid)"),
    ({}, {"database_oid": None}, "(database_oid)"),
])
def test_missing_or_mismatched_source_identity_fails_closed_without_changing_preflight_acceptance(answers, lock_kw, field):
    world = World(answers=answers, lock_kw=lock_kw)
    info, entered = run_expecting(SnapshotRefusedError, world)
    assert entered == [] and field in str(info.value) and snap._EXPORT_SQL not in world.steps("S")


def test_a_database_name_mismatch_between_l_and_s_is_refused():
    world = World()
    world.lock = BackupLockSession(VerifiedDatabaseTarget("napsedge_pg_test_other", "x"), 123, 4242, START, system_identifier=SYSID,
                                   database_oid=16384, confirm=world._confirm)
    info, entered = run_expecting(SnapshotRefusedError, world)
    assert "(database_name)" in str(info.value) and entered == []


# ---------------------------------------------------------------------------
# lock boundaries
# ---------------------------------------------------------------------------
def test_a_lock_that_is_not_held_before_creation_creates_nothing():
    world = World(confirm_results=[BackupLockNotHeld("x")])
    run_expecting(BackupLockNotHeld, world)
    assert world.requested == [] and world.log == [("L", "confirm_held")]


@pytest.mark.parametrize("lost_at, exported", [(1, False), (2, True)], ids=["before_export", "before_yield"])
def test_a_lock_lost_at_a_boundary_before_the_yield_never_yields_and_discards_s(lost_at, exported):
    results = [None] * lost_at + [BackupLockNotHeld("lost")]
    world = World(confirm_results=results)
    info, entered = run_expecting(BackupLockNotHeld, world)
    assert entered == [] and (snap._EXPORT_SQL in world.steps("S")) is exported
    assert world.steps("S")[-2:] == ["invalidate", "close"] and world.steps("snapshot_engine")[-1] == "dispose"


def test_a_lock_lost_at_exit_is_raised_never_reacquired_and_s_is_discarded():
    lost = BackupLockNotHeld("lost")
    world = World(confirm_results=[None, None, None, lost])
    info, entered = run_expecting(BackupLockNotHeld, world)
    assert info.value is lost and len(entered) == 1 and snap._ALIVE_SQL not in world.steps("S")
    assert world.steps("S")[-2:] == ["invalidate", "close"] and entered[0].cleanup.confirmed


# ---------------------------------------------------------------------------
# S lost, baseline failure, body failure, transaction drift
# ---------------------------------------------------------------------------
class DriverFailure(Exception):
    pass


@pytest.mark.parametrize("alive", [DriverFailure(f"closed {SECRET} {HOST}"), [(9999, DESCRIPTOR, T0)], [(5151, "999:999:", T0)]],
                         ids=["lost", "other_backend", "other_snapshot"])
def test_s_lost_or_changed_at_exit_is_a_sanitized_session_lost(alive):
    world = World(answers={snap._ALIVE_SQL: alive})
    info, _ = run_expecting(SnapshotSessionLost, world)
    no_secret("".join(traceback.format_exception(info.value)))
    assert info.value.__cause__ is None
    assert world.steps("S")[-2:] == ["invalidate", "close"] and world.steps("snapshot_engine").count("connect") == 1


def test_a_baseline_read_failure_is_the_preserved_primary_and_nothing_is_yielded():
    failure = DriverFailure("permission denied for table teams")
    world = World(answers={FINGERPRINT_TEAMS: failure})
    info, entered = run_expecting(DriverFailure, world)
    assert info.value is failure and entered == [] and getattr(info.value, "__notes__", []) == []
    assert world.steps("S")[-2:] == ["invalidate", "close"]


def test_a_body_failure_is_the_same_object_with_cause_notes_and_traceback_and_s_is_discarded():
    world = World()
    cause, primary = ValueError("cause"), RuntimeError("consumer failed")
    primary.__cause__ = cause
    primary.add_note("caller note")

    def body(handle):
        raise primary

    info, entered = run_expecting(RuntimeError, world, body)
    assert info.value is primary and info.value.__cause__ is cause and info.value.__notes__ == ["caller note"]
    assert any(frame.name == "body" for frame in traceback.extract_tb(info.value.__traceback__))
    assert snap._ALIVE_SQL not in world.steps("S") and world.steps("S")[-2:] == ["invalidate", "close"]
    assert entered[0].cleanup.confirmed and entered[0].cleanup.connection_discarded


def test_baseline_reads_that_left_the_transaction_are_refused():
    world = World(answers={snap._NOW_SQL: [(T0 + dt.timedelta(seconds=1),)]})
    info, entered = run_expecting(SnapshotRefusedError, world)
    assert entered == [] and "did not stay in the snapshot transaction" in str(info.value)


# ---------------------------------------------------------------------------
# inputs, setup, cleanup and interrupts
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kw", [
    {"artifact_id": "not-a-uuid"}, {"artifact_id": ARTIFACT_ID.upper()}, {"lock": "not a lock"}, {"head": ""},
    {"url": URL.replace("verify-full", "require")},
])
def test_invalid_inputs_are_refused_before_anything_is_created(kw):
    world = World()
    info, _ = run_expecting(ValidationError, world, **kw)
    assert world.requested == [] and world.confirms == 0
    no_secret(str(info.value))


def test_a_pooled_engine_is_refused_and_disposed():
    world = World(engine_kw={"pooled": True})
    run_expecting(ValidationError, world)
    assert world.steps("snapshot_engine") == ["dispose"]


def test_a_connection_failure_is_a_sanitized_setup_error():
    world = World(engine_kw={"connect_error": DriverFailure(f"password authentication failed {SECRET}@{HOST}")})
    info, _ = run_expecting(SnapshotSetupError, world)
    assert str(info.value) == "opening the snapshot session failed (DriverFailure)" and info.value.__cause__ is None
    no_secret("".join(traceback.format_exception(info.value)))


def test_a_cleanup_failure_without_a_primary_is_visible_and_the_remaining_steps_run():
    world = World(fail={"close": DriverFailure(f"close {SECRET}")})
    info, _ = run_expecting(SnapshotCleanupError, world)
    assert info.value.report.failures == ("close:DriverFailure",) and info.value.report.engine_disposed
    assert world.requested[-1] == VERIFIER_APPLICATION_NAME
    no_secret(str(info.value))


def test_with_a_primary_a_cleanup_failure_adds_one_bounded_note():
    world = World(fail={"invalidate": DriverFailure(f"x {SECRET}")})
    primary = RuntimeError("consumer failed")

    def body(handle):
        raise primary

    info, _ = run_expecting(RuntimeError, world, body)
    assert info.value is primary and info.value.__notes__ == [
        "snapshot session cleanup was not confirmed: verification=closed_and_released; failed steps=discard:DriverFailure"]


def test_an_unverifiable_cleanup_is_never_success():
    world = World(verifier={lock_module._ORIGINAL_PRESENT_SQL: [(1,)]})
    info, _ = run_expecting(SnapshotCleanupError, world)
    assert info.value.report.verification == "original_session_present"


@pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit])
def test_a_primary_interrupt_is_preserved_when_ordinary_cleanup_fails(interrupt_type):
    world = World(fail={"close": DriverFailure("close")})
    primary = interrupt_type()

    def body(handle):
        raise primary

    info, _ = run_expecting(interrupt_type, world, body)
    assert info.value is primary and "close:DriverFailure" in info.value.__notes__[0]


def test_a_new_interrupt_during_cleanup_propagates_and_the_remaining_steps_run():
    new_interrupt = KeyboardInterrupt()
    world = World(fail={"rollback": new_interrupt})
    info, entered = run_expecting(KeyboardInterrupt, world)
    assert info.value is new_interrupt and world.steps("S")[-1] == "close" and world.steps("snapshot_engine")[-1] == "dispose"
    assert entered[0].cleanup.verification == "not_verified"


# ---------------------------------------------------------------------------
# static constraints
# ---------------------------------------------------------------------------
_TREE = ast.parse(inspect.getsource(snap))


def test_the_module_reuses_the_existing_contracts_and_imports_no_test_code():
    imported = {n.module for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(_TREE) if isinstance(n, ast.Import) for a in n.names}
    assert imported == {"__future__", "datetime", "re", "contextlib", "dataclasses", "typing", "sqlalchemy", "sqlalchemy.engine",
                        "src.deployment.backup_artifact", "src.deployment.backup_capture_lock", "src.deployment.backup_capture_preflight",
                        "src.exceptions", "src.pipeline.bulk_import_safety"}
    names = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)}
    assert {"_validated_url", "_build_engine", "_verify_cleanup", "observe_capture_preflight", "BaselineDocument", "parse_baseline",
            "_SEQUENCE_INVENTORY_SQL"} <= names
    called = {n.func.attr for n in ast.walk(_TREE) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "commit" not in called and "create_engine" not in called
    assert not any(isinstance(n, ast.Attribute) and n.attr == "__notes__" for n in ast.walk(_TREE))


def test_public_symbols_are_exactly_the_documented_ones():
    assert sorted(snap.__all__) == sorted(["SNAPSHOT_APPLICATION_NAME", "SnapshotSetupError", "SnapshotRefusedError", "SnapshotSessionLost",
                                           "SnapshotCleanupError", "SnapshotHandleClosed", "SnapshotFacts", "SnapshotCleanupReport", "SnapshotHandle",
                                           "backup_capture_snapshot"])


# ---------------------------------------------------------------------------
# Phase 9B-6D addition: confirm_active() and the private validated source
# ---------------------------------------------------------------------------
def test_confirm_active_checks_l_then_s_and_the_validated_source_is_the_exact_url_and_designation():
    world = World()
    seen = []

    def body(handle):
        before = len(world.log)
        handle.confirm_active()
        seen.append([step for who, step in world.log[before:]])
        url, designation_ = handle._validated_source()
        seen.append((url, designation_.expected_database_name))

    run(world, body)
    assert seen[0] == ["confirm_held", snap._ALIVE_SQL]
    assert seen[1] == (make_url(URL), DB)  # the very URL S validated, compared as a parsed URL


@pytest.mark.parametrize("failure", ["lock", "session"])
def test_confirm_active_raises_on_a_lost_l_or_a_changed_s(failure):
    if failure == "lock":
        world = World(confirm_results=[None, None, None, BackupLockNotHeld("lost")])
        expected = BackupLockNotHeld
    else:
        world = World(answers={snap._ALIVE_SQL: [(9999, DESCRIPTOR, T0)]})
        expected = SnapshotSessionLost
    info, _ = run_expecting(expected, world, lambda handle: handle.confirm_active())
    assert world.steps("S")[-2:] == ["invalidate", "close"]


def test_confirm_active_and_the_validated_source_are_rejected_after_exit():
    handle = run(World())
    with pytest.raises(SnapshotHandleClosed):
        handle.confirm_active()
    with pytest.raises(SnapshotHandleClosed):
        handle._validated_source()
