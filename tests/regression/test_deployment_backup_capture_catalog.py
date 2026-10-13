"""
Phase 9B-6F: deterministic unit tests for `src/deployment/backup_capture_catalog.py` with fake engines and connections
(INJECTED behavior). Real PostgreSQL behavior -- fresh endpoints around a real dump, commit evidence, queued-DDL refusal,
the change-and-reversal bypass -- is in `tests/postgres/test_postgres_backup_capture_catalog.py`.
"""
from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import inspect
import traceback
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

import src.deployment.backup_capture_catalog as cat
import src.deployment.backup_capture_lock as lock_module
import src.deployment.backup_capture_preflight as preflight_module
import src.deployment.backup_capture_snapshot as snap_module
from src.deployment.backup_capture_catalog import (
    CATEGORIES,
    OBSERVER_APPLICATION_NAME,
    CatalogChanged,
    CatalogEndpoint,
    CatalogObservation,
    CatalogObservationFailed,
    NoDifferenceObserved,
    SourceIdentity,
    catalog_changed,
    changed_categories,
    observe_endpoint,
    raw_differences,
    require_unchanged,
)
from src.exceptions import ValidationError
from src.pipeline.bulk_import_safety import DatabaseTargetDesignation, build_expected_acknowledgement
from tests.postgres.pg_restore_compare import keyed_diff

DB = "napsedge_pg_test_catalog"
SECRET = "Pl4nted-S3cret-zz9"
HOST = "db-secret-host.internal"
URL = f"postgresql+psycopg://napsedge_backup_ro:{SECRET}@{HOST}:5432/{DB}?sslmode=verify-full&sslrootcert=/ca.pem"
SYSID = "7412345678901234567"
SOURCE = SourceIdentity(SYSID, 16384, DB)
T0 = dt.datetime(2026, 10, 5, 12, tzinfo=dt.timezone.utc)
T1 = T0 + dt.timedelta(seconds=30)
START = T0 - dt.timedelta(seconds=1)
VERIFY = "SELECT current_database(), version()"
CONSTRAINTS = [("teams", "teams_pkey", "p", True, "PRIMARY KEY (team_id)")]
INDEXES = [("teams", "teams_pkey", "CREATE UNIQUE INDEX teams_pkey ON public.teams USING btree (team_id)")]
ACLS = [("conferences", "r", "napsedge_migrator", "{napsedge_migrator=arwdDxt/napsedge_migrator}"), ("teams", "r", "napsedge_migrator", "")]


def designation(name: str = DB) -> DatabaseTargetDesignation:
    return DatabaseTargetDesignation(expected_database_name=name, acknowledgement=build_expected_acknowledgement(name))


def obs(**changes) -> CatalogObservation:
    base = {"constraints": tuple(CONSTRAINTS), "indexes": tuple(INDEXES), "acls": tuple(ACLS)}
    base.update(changes)
    return CatalogObservation(**base)


def endpoint(label="before", observation=None, pid=101, start=T0, **changes) -> CatalogEndpoint:
    values = dict(label=label, observation=observation or obs(), backend_pid=pid, backend_start=START, transaction_start=start,
                  isolation="read committed", read_only="on", source=SOURCE, cleanup_verification=lock_module.VERIFIED_CLOSED)
    values.update(changes)
    return CatalogEndpoint(**values)


# ---------------------------------------------------------------------------
# a fake observer session and the lock module's verifier
# ---------------------------------------------------------------------------
class _Result:
    def __init__(self, rows):
        self.rows = rows

    def one(self):
        assert len(self.rows) == 1
        return self.rows[0]

    def scalar_one(self):
        return self.one()[0]

    def scalar(self):
        return self.rows[0][0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class FakeConnection:
    def __init__(self, name, log, answers, fail=None):
        self.name, self.log, self.answers, self.fail = name, log, answers, fail or {}
        self.closed = False
        self.engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def _step(self, step):
        self.log.append((self.name, step))
        if step in self.fail:
            raise self.fail[step]

    def execution_options(self, **options):
        self.log.append((self.name, f"options:{sorted(options.items())}"))
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
    def __init__(self, name, log, connection, connect_error=None):
        self.name, self.log, self.connection, self.connect_error = name, log, connection, connect_error
        self.pool = NullPool(lambda: None)

    def connect(self):
        self.log.append((self.name, "connect"))
        if self.connect_error is not None:
            raise self.connect_error
        return self.connection

    def dispose(self):
        self.log.append((self.name, "dispose"))


class DriverFailure(Exception):
    pass


def observer_answers(**overrides):
    answers = {
        f"SET LOCAL lock_timeout = {cat._LOCK_TIMEOUT_MS}": [],
        cat._TRANSACTION_SQL: [("read committed", "on", 101, START, T0)],
        VERIFY: [(DB, "PostgreSQL 16.4")],
        preflight_module._DATABASE_SQL: [(16384, "napsedge_migrator", 160004)],
        preflight_module._SYSTEM_IDENTIFIER_PRIVILEGE_SQL: [(True,)],
        preflight_module._SYSTEM_IDENTIFIER_SQL: [(SYSID,)],
        snap_module._CONSTRAINT_SQL: CONSTRAINTS,
        snap_module._INDEX_SQL: INDEXES,
        snap_module._ACL_SQL: ACLS,
        cat._NOW_SQL: [(T0,)],
    }
    answers.update(overrides)
    return answers


class World:
    def __init__(self, *, answers=None, fail=None, connect_error=None, verifier=None, verifier_error=None):
        self.log: list = []
        self.conn = FakeConnection("O", self.log, observer_answers(**(answers or {})), fail)
        verifier_answers = {VERIFY: [(DB, "PostgreSQL 16.4")], lock_module._ORIGINAL_PRESENT_SQL: [(0,)], lock_module._KEY_HOLDERS_SQL: []}
        verifier_answers.update(verifier or {})
        self.verifier = FakeConnection("V", self.log, verifier_answers)
        self.engines = {OBSERVER_APPLICATION_NAME: FakeEngine("observer_engine", self.log, self.conn, connect_error),
                        lock_module.VERIFIER_APPLICATION_NAME: FakeEngine("verifier_engine", self.log, self.verifier)}
        self.verifier_error = verifier_error

    def factory(self, url, application_name):
        if application_name == lock_module.VERIFIER_APPLICATION_NAME and self.verifier_error is not None:
            raise self.verifier_error
        return self.engines[application_name]

    def steps(self, name):
        return [step for who, step in self.log if who == name]


def observe(world, label="before", expected=SOURCE):
    return observe_endpoint(URL, designation(), label=label, expected_source=expected, _engine_factory=world.factory)


def refused(world, fragment, **kw):
    with pytest.raises(BaseException) as info:
        observe(world, **kw)
    assert type(info.value) is CatalogObservationFailed, type(info.value).__name__
    assert fragment in str(info.value), str(info.value)
    assert SECRET not in "".join(traceback.format_exception(info.value)) and HOST not in str(info.value)
    return info.value


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(lock_module, "_VERIFY_DEADLINE_S", 0.2)
    monkeypatch.setattr(lock_module, "_VERIFY_POLL_S", 0.01)


# ---------------------------------------------------------------------------
# the moved canonical definitions
# ---------------------------------------------------------------------------
def test_digests_are_the_established_per_category_sha256_of_the_compact_json_rows_and_a_combined_digest():
    import hashlib
    import json

    o = obs()
    expected = {c: hashlib.sha256(json.dumps([list(r) for r in getattr(o, c)], default=str, separators=(",", ":")).encode()).hexdigest() for c in CATEGORIES}
    expected["combined"] = hashlib.sha256("|".join(expected[c] for c in CATEGORIES).encode("ascii")).hexdigest()
    assert o.digests == expected


def test_changed_categories_and_catalog_changed_name_exactly_the_changed_category():
    for category, changed in (("constraints", ()), ("indexes", ()), ("acls", (("conferences", "r", "zz", ""),))):
        other = obs(**{category: changed})
        assert changed_categories(obs(), other) == [category] and catalog_changed(obs(), other)
    assert not catalog_changed(obs(), obs())


@pytest.mark.parametrize("left, right, keys", [
    (CONSTRAINTS, CONSTRAINTS + [("teams", "zz_chk", "c", True, "CHECK (team_id > 0)")], 2),
    (INDEXES, [], 2),
    (ACLS, [ACLS[0][:3] + ("{}",), ACLS[1]], 1),
    (ACLS, [("zz", "r", "x", "")], 1),
])
def test_raw_differences_equal_the_test_layer_keyed_diff_drift_guard(left, right, keys):
    category = {2: "constraints" if left is CONSTRAINTS else "indexes", 1: "acls"}[keys]
    before, after = obs(**{category: tuple(left)}), obs(**{category: tuple(right)})
    assert raw_differences(before, after)[category] == keyed_diff(list(left), list(right), key_columns=keys)


def test_the_c6_test_module_now_uses_these_definitions_rather_than_its_own():
    import tests.postgres.test_postgres_backup_catalog_consistency as c6

    for name in ("CatalogObservation", "changed_categories", "raw_differences", "catalog_changed", "CATEGORIES"):
        assert getattr(c6, name) is getattr(cat, name), name
    source = inspect.getsource(c6)
    assert "def _digest" not in source and "class CatalogObservation" not in source and "def raw_differences" not in source


# ---------------------------------------------------------------------------
# observe_endpoint (injected)
# ---------------------------------------------------------------------------
def test_a_complete_endpoint_is_one_fresh_read_committed_read_only_transaction_with_its_own_identity():
    world = World()
    result = observe(world)
    o = world.steps("O")
    assert o[0] == "options:[('isolation_level', 'READ COMMITTED'), ('postgresql_readonly', True)]" and o[1] == "begin"
    assert o[2] == f"SET LOCAL lock_timeout = {cat._LOCK_TIMEOUT_MS}" and o[3] == cat._TRANSACTION_SQL
    assert o.index(snap_module._CONSTRAINT_SQL) < o.index(snap_module._INDEX_SQL) < o.index(snap_module._ACL_SQL) < o.index(cat._NOW_SQL)
    assert o[-2:] == ["rollback", "close"] and "invalidate" not in o and world.steps("observer_engine") == ["connect", "dispose"]
    assert sum(1 for s in o if s in (snap_module._CONSTRAINT_SQL, snap_module._INDEX_SQL, snap_module._ACL_SQL)) == 3  # never retried
    assert result.complete and result.source == SOURCE and result.backend_pid == 101 and result.observation == obs()
    assert result.cleanup_verification == lock_module.VERIFIED_CLOSED and world.steps("V")[0] == VERIFY


@pytest.mark.parametrize("answers, fragment", [
    ({preflight_module._DATABASE_SQL: [(99999, "napsedge_migrator", 160004)]}, "different source (database_oid)"),
    ({preflight_module._SYSTEM_IDENTIFIER_SQL: [("1111111111111111111",)]}, "different source (system_identifier)"),
    ({preflight_module._SYSTEM_IDENTIFIER_PRIVILEGE_SQL: [(False,)]}, "could not read the cluster system_identifier"),
    ({cat._TRANSACTION_SQL: [("repeatable read", "on", 101, START, T0)]}, "not in a READ COMMITTED, READ ONLY transaction"),
    ({cat._TRANSACTION_SQL: [("read committed", "off", 101, START, T0)]}, "not in a READ COMMITTED, READ ONLY transaction"),
    ({cat._NOW_SQL: [(T1,)]}, "did not stay in one transaction"),
    ({VERIFY: [("napsedge_pg_test_other", "PostgreSQL 16.4")]}, "did not complete (DatabaseTargetVerificationError)"),
    ({snap_module._INDEX_SQL: DriverFailure("canceling statement due to lock timeout")}, "did not complete (DriverFailure)"),
])
def test_identity_isolation_transaction_and_read_failures_refuse_and_never_return_an_endpoint(answers, fragment):
    world = World(answers=answers)
    refused(world, fragment)
    assert world.steps("O")[-1] == "close" and world.steps("observer_engine")[-1] == "dispose"


def test_a_different_expected_name_refuses_naming_the_field():
    refused(World(), "different source (database_name)", expected=SourceIdentity(SYSID, 16384, "napsedge_pg_test_other"))


def test_a_connection_failure_is_sanitized_and_nothing_is_left():
    world = World(connect_error=DriverFailure(f"password authentication failed {SECRET}@{HOST}"))
    exc = refused(world, "could not connect (DriverFailure)")
    assert exc.__cause__ is None and world.steps("observer_engine") == ["connect", "dispose"]


def test_a_read_failure_is_discarded_and_the_primary_keeps_its_cause_free_message():
    world = World(answers={snap_module._ACL_SQL: DriverFailure(f"lost {SECRET}")})
    exc = refused(world, "did not complete (DriverFailure)")
    assert world.steps("O")[-2:] == ["invalidate", "close"] and getattr(exc, "__notes__", []) == []


@pytest.mark.parametrize("kw, fragment", [
    ({"verifier": {lock_module._ORIGINAL_PRESENT_SQL: [(1,)]}}, "verification=original_session_present"),
    ({"verifier_error": DriverFailure("down")}, "verification=not_verified"),
    ({"fail": {"close": DriverFailure("close")}}, "close:DriverFailure"),
])
def test_an_unconfirmed_cleanup_refuses_even_when_the_observation_itself_completed(kw, fragment):
    refused(World(**kw), fragment)


def test_with_a_primary_failure_an_unconfirmed_cleanup_is_one_note_and_the_primary_is_kept():
    world = World(answers={cat._NOW_SQL: [(T1,)]}, fail={"close": DriverFailure("close")})
    exc = refused(world, "did not stay in one transaction")
    assert len(exc.__notes__) == 1 and "close:DriverFailure" in exc.__notes__[0]


def test_inputs_are_validated_before_anything_is_created():
    world = World()
    for kw in ({"label": "middle"}, {"expected": ("not", "a", "source")}):
        with pytest.raises(ValidationError):
            observe(world, **kw)
    assert world.log == []


# ---------------------------------------------------------------------------
# require_unchanged
# ---------------------------------------------------------------------------
def test_equal_complete_endpoints_give_only_a_no_difference_observed_result():
    result = require_unchanged(endpoint("before"), endpoint("after", pid=202, start=T1), SOURCE)
    assert isinstance(result, NoDifferenceObserved) and result.meaning == "no difference observed at these endpoints"
    assert result.digests == obs().digests


def test_a_difference_refuses_with_the_categories_and_raw_differences():
    after = endpoint("after", observation=obs(acls=(ACLS[0][:3] + ("{napsedge_migrator=arwdDxt/napsedge_migrator,napsedge_runtime_ro=a/napsedge_migrator}",), ACLS[1])),
                     pid=202, start=T1)
    with pytest.raises(CatalogChanged) as info:
        require_unchanged(endpoint("before"), after, SOURCE)
    assert info.value.categories == ["acls"] and list(info.value.differences["acls"]) == [("conferences",)]
    assert info.value.differences["constraints"] == {} and info.value.differences["indexes"] == {}


@pytest.mark.parametrize("before, after, fragment", [
    (None, endpoint("after", pid=202, start=T1), "before catalog observation is missing"),
    (endpoint("before"), None, "after catalog observation is missing"),
    (endpoint("after"), endpoint("before", pid=202, start=T1), "incomplete or malformed"),
    (endpoint("before", cleanup_verification="not_verified"), endpoint("after", pid=202, start=T1), "incomplete or malformed"),
    (endpoint("before", isolation="repeatable read"), endpoint("after", pid=202, start=T1), "incomplete or malformed"),
    (endpoint("before", source=SourceIdentity(None, 16384, DB)), endpoint("after", pid=202, start=T1), "incomplete or malformed"),
    (endpoint("before", observation=CatalogObservation(None, (), ())), endpoint("after", pid=202, start=T1), "incomplete or malformed"),
    (endpoint("before", source=SourceIdentity(SYSID, 1, DB)), endpoint("after", pid=202, start=T1), "different source"),
    (endpoint("before"), endpoint("after"), "not distinct transactions"),
    (endpoint("before", start=T1), endpoint("after", pid=202, start=T0), "does not follow"),
    ("not an endpoint", endpoint("after", pid=202, start=T1), "missing"),
])
def test_missing_malformed_incomplete_or_misordered_records_refuse_explicitly_and_never_compare(before, after, fragment):
    with pytest.raises(BaseException) as info:
        require_unchanged(before, after, SOURCE)
    assert type(info.value) is CatalogObservationFailed and fragment in str(info.value)


# ---------------------------------------------------------------------------
# static constraints
# ---------------------------------------------------------------------------
_SOURCE_TEXT = inspect.getsource(cat)
_TREE = ast.parse(_SOURCE_TEXT)


def test_the_catalog_sql_is_the_snapshot_modules_own_constants_and_nothing_writes():
    assert cat._CONSTRAINT_SQL is snap_module._CONSTRAINT_SQL and cat._INDEX_SQL is snap_module._INDEX_SQL and cat._ACL_SQL is snap_module._ACL_SQL
    strings = " ".join(n.value for n in ast.walk(_TREE) if isinstance(n, ast.Constant) and isinstance(n.value, str) and not n.value.startswith("\nPhase"))
    for forbidden in ("INSERT", "UPDATE ", "DELETE", "ALTER ", "CREATE ", "GRANT", "REVOKE", "pg_export_snapshot", "pg_advisory", "pg_terminate_backend",
                      "catalog_bracket_equal", "COMPLETE", "manifest"):
        assert forbidden not in strings, forbidden
    imported = {n.module for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom)}
    assert not any(m.startswith("tests") for m in imported) and "src.deployment.backup_artifact_writer" not in imported
    loops = [n for n in ast.walk(_TREE) if isinstance(n, (ast.For, ast.While))]
    assert all("attempt" not in ast.unparse(n) or "attempt(" in ast.unparse(n) for n in loops)  # no retry loop around an observation


def test_public_symbols_are_exactly_the_documented_ones():
    assert sorted(cat.__all__) == sorted(["CATEGORIES", "OBSERVER_APPLICATION_NAME", "CatalogObservation", "changed_categories", "raw_differences",
                                          "catalog_changed", "SourceIdentity", "CatalogEndpoint", "NoDifferenceObserved", "CatalogRefusal",
                                          "CatalogObservationFailed", "CatalogChanged", "observe_endpoint", "require_unchanged"])
