"""
Controlled bulk-import design, Slice 4 (docs/controlled_bulk_import_design.md
section 12, approved for this slice only): deterministic unit tests for
`src/pipeline/bulk_import_safety.py` that need no real database connection
at all. Real-PostgreSQL behavior (actual verification against a live
server, real advisory-lock contention/release) lives in
`tests/postgres/test_postgres_bulk_import_safety.py`.
"""
from __future__ import annotations

import ast
import inspect

import pytest
from sqlalchemy import create_engine

from src.exceptions import DatabaseTargetVerificationError, ValidationError
from src.pipeline.bulk_import_safety import (
    ACKNOWLEDGEMENT_TEMPLATE,
    ADMINISTRATIVE_DATABASE_NAMES,
    LOCK_NAMESPACE,
    DatabaseTargetDesignation,
    VerifiedDatabaseTarget,
    build_expected_acknowledgement,
    compute_advisory_lock_key,
    verify_database_target,
)

# ---------------------------------------------------------------------------
# Acknowledgement construction and exact matching
# ---------------------------------------------------------------------------


def test_build_expected_acknowledgement_uses_the_documented_template():
    ack = build_expected_acknowledgement("napsedge_pg_test_abc123")
    assert ack == ACKNOWLEDGEMENT_TEMPLATE.format(database_name="napsedge_pg_test_abc123")
    assert "napsedge_pg_test_abc123" in ack


def test_designation_accepts_the_exact_matching_acknowledgement():
    name = "napsedge_pg_test_abc123"
    designation = DatabaseTargetDesignation(expected_database_name=name, acknowledgement=build_expected_acknowledgement(name))
    assert designation.expected_database_name == name


def test_designation_rejects_a_mismatched_acknowledgement():
    with pytest.raises(ValidationError):
        DatabaseTargetDesignation(expected_database_name="napsedge_pg_test_abc123", acknowledgement="I CONFIRM BULK IMPORT TARGET: some_other_db")


def test_designation_rejects_case_variant_of_correct_acknowledgement():
    """Case-sensitive exact match, per instruction -- a byte-for-byte-correct phrase in the wrong case must still fail."""
    name = "napsedge_pg_test_abc123"
    ack = build_expected_acknowledgement(name).lower()
    with pytest.raises(ValidationError):
        DatabaseTargetDesignation(expected_database_name=name, acknowledgement=ack)


def test_designation_rejects_empty_acknowledgement():
    with pytest.raises(ValidationError):
        DatabaseTargetDesignation(expected_database_name="napsedge_pg_test_abc123", acknowledgement="")


def test_designation_rejects_acknowledgement_for_a_different_database_name():
    """An acknowledgement that is correctly *formatted* but names the wrong database must still be rejected."""
    with pytest.raises(ValidationError):
        DatabaseTargetDesignation(expected_database_name="napsedge_pg_test_abc123", acknowledgement=build_expected_acknowledgement("napsedge_pg_test_xyz999"))


# ---------------------------------------------------------------------------
# Empty / malformed target names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_name", ["", "   ", None, 123])
def test_empty_or_malformed_target_names_rejected(bad_name):
    with pytest.raises(ValidationError):
        DatabaseTargetDesignation(expected_database_name=bad_name, acknowledgement=build_expected_acknowledgement(str(bad_name)))


# ---------------------------------------------------------------------------
# Administrative-database refusal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("admin_name", sorted(ADMINISTRATIVE_DATABASE_NAMES) + ["POSTGRES", "Template0", "TEMPLATE1"])
def test_administrative_database_names_always_rejected(admin_name):
    with pytest.raises(ValidationError):
        DatabaseTargetDesignation(expected_database_name=admin_name, acknowledgement=build_expected_acknowledgement(admin_name))


def test_administrative_database_set_is_exactly_the_documented_three():
    assert ADMINISTRATIVE_DATABASE_NAMES == frozenset({"postgres", "template0", "template1"})


# ---------------------------------------------------------------------------
# Sanitized verified-target representation
# ---------------------------------------------------------------------------


def test_verified_database_target_carries_only_non_secret_fields():
    target = VerifiedDatabaseTarget(database_name="napsedge_pg_test_abc123", server_version="PostgreSQL 16.15 on x86_64...")
    field_names = {f for f in target.__dataclass_fields__}
    assert field_names == {"database_name", "server_version"}
    for forbidden in ("password", "user", "host", "port", "url", "dsn", "credential", "acknowledgement"):
        assert forbidden not in field_names


def test_verify_database_target_rejects_non_dataclass_designation():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.connect() as conn:
        with pytest.raises(ValidationError):
            verify_database_target(conn, "not-a-designation")


def test_verify_database_target_rejects_sqlite_connection():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    designation = DatabaseTargetDesignation(expected_database_name="napsedge_pg_test_abc123", acknowledgement=build_expected_acknowledgement("napsedge_pg_test_abc123"))
    with engine.connect() as conn:
        with pytest.raises(DatabaseTargetVerificationError):
            verify_database_target(conn, designation)


def test_verify_database_target_rejects_closed_connection():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    designation = DatabaseTargetDesignation(expected_database_name="napsedge_pg_test_abc123", acknowledgement=build_expected_acknowledgement("napsedge_pg_test_abc123"))
    conn = engine.connect()
    conn.close()
    with pytest.raises(DatabaseTargetVerificationError):
        verify_database_target(conn, designation)


# ---------------------------------------------------------------------------
# Stable lock-key derivation
# ---------------------------------------------------------------------------


def test_lock_key_is_deterministic_across_repeated_calls():
    target = VerifiedDatabaseTarget(database_name="napsedge_pg_test_abc123", server_version="PostgreSQL 16.15")
    keys = {compute_advisory_lock_key(target) for _ in range(10)}
    assert len(keys) == 1


def test_lock_key_signed_bigint_range_compliance():
    for name in ("napsedge_pg_test_abc123", "some_other_db", "x", "z" * 200):
        target = VerifiedDatabaseTarget(database_name=name, server_version="PostgreSQL 16.15")
        key = compute_advisory_lock_key(target)
        assert isinstance(key, int)
        assert 0 <= key < 2**63  # fits a signed 64-bit PostgreSQL bigint exactly, never negative, never overflowing


def test_different_database_identities_produce_different_keys():
    key_a = compute_advisory_lock_key(VerifiedDatabaseTarget(database_name="napsedge_pg_test_abc123", server_version="x"))
    key_b = compute_advisory_lock_key(VerifiedDatabaseTarget(database_name="napsedge_pg_test_xyz999", server_version="x"))
    assert key_a != key_b


def test_lock_key_rejects_a_bare_string_not_a_verified_target():
    with pytest.raises(ValidationError):
        compute_advisory_lock_key("napsedge_pg_test_abc123")


def test_lock_namespace_is_fixed_and_distinct_from_game_identity_namespace():
    assert LOCK_NAMESPACE == "napsedge_bulk_import_advisory_lock"
    assert "game" not in LOCK_NAMESPACE


# ---------------------------------------------------------------------------
# No Python hash(), no engine/session/connection construction, no secrets
# ---------------------------------------------------------------------------


def test_module_never_uses_pythons_randomized_hash():
    import src.pipeline.bulk_import_safety as mod

    tree = ast.parse(inspect.getsource(mod))
    called_names = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "hash" not in called_names


def test_module_never_constructs_its_own_engine_session_or_connection():
    import src.pipeline.bulk_import_safety as mod

    tree = ast.parse(inspect.getsource(mod))
    called_names = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not ({"create_engine", "sessionmaker", "Session", "build_engine", "build_session_factory", "connect"} & called_names)


def test_module_never_reads_environment_or_dotenv():
    import src.pipeline.bulk_import_safety as mod

    tree = ast.parse(inspect.getsource(mod))
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
    assert "os" not in imported_names
    assert "src.db.config" not in imported_names


def test_module_never_commits_rolls_back_or_writes_rows():
    import src.pipeline.bulk_import_safety as mod

    tree = ast.parse(inspect.getsource(mod))
    called_attrs = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not ({"commit", "rollback", "flush", "add", "delete", "merge"} & called_attrs)


def test_never_exposes_credentials_urls_or_the_acknowledgement_in_the_verified_target():
    """The VerifiedDatabaseTarget object itself must never carry the acknowledgement phrase or any connection-string-shaped value."""
    target = VerifiedDatabaseTarget(database_name="napsedge_pg_test_abc123", server_version="PostgreSQL 16.15 on x86_64-pc-linux-musl")
    rendered = repr(target)
    assert "CONFIRM" not in rendered
    assert "://" not in rendered
    assert "password" not in rendered.lower()


# ---------------------------------------------------------------------------
# Input immutability
# ---------------------------------------------------------------------------


def test_designation_is_frozen():
    designation = DatabaseTargetDesignation(expected_database_name="napsedge_pg_test_abc123", acknowledgement=build_expected_acknowledgement("napsedge_pg_test_abc123"))
    with pytest.raises(Exception):
        designation.expected_database_name = "something-else"  # type: ignore[misc]


def test_verified_target_is_frozen():
    target = VerifiedDatabaseTarget(database_name="napsedge_pg_test_abc123", server_version="PostgreSQL 16.15")
    with pytest.raises(Exception):
        target.database_name = "something-else"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Exception preservation of `bulk_import_advisory_lock` (Phase 9B-5C4 correction)
#
# Acceptance criteria, recorded BEFORE the correction:
#   A1  normal body + successful release succeeds; unlock is attempted exactly once.
#   A2  primary failure + successful release: the primary propagates untouched (same object, no note).
#   A3  primary failure + a RAISED release error: the primary propagates (same object, type, explicit cause, existing notes, traceback);
#       a bounded, sanitized cleanup note names the release failure; no arbitrary message, SQL parameter, URL or secret is recorded.
#   A4  primary failure + a FALSE unlock result: the primary propagates with a cleanup note.
#   A5  with no primary failure an unsuccessful release stays a visible failure (a raised error propagates; a false result raises AdvisoryLockUnavailableError).
#   A6  acquisition refusal never runs the body and never attempts unlock; unlock is never retried.
#   A7  caller ownership is untouched: no commit, rollback, close, invalidation or disposal; signature and exports unchanged; nested acquisition stays balanced.
#   A8  a primary KeyboardInterrupt/SystemExit with an ordinary unlock failure is preserved; a NEW interrupt raised during cleanup is not swallowed.
# Preserving the primary failure is NOT a claim that the lock was released: an unconfirmed release is recorded as such.
# ---------------------------------------------------------------------------
import contextlib  # noqa: E402
import traceback  # noqa: E402

from src.exceptions import AdvisoryLockUnavailableError  # noqa: E402
from src.pipeline.bulk_import_safety import bulk_import_advisory_lock  # noqa: E402

RELEASE_SECRET = "postgresql://backup:RELEASE-SECRET@db.internal:5432/napsedge?sslmode=verify-full"
TARGET = VerifiedDatabaseTarget(database_name="napsedge_pg_test_lockfix", server_version="PostgreSQL 16.15")
KEY = compute_advisory_lock_key(TARGET)


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value


class DriverFailure(Exception):
    """Stands in for a driver or SQLAlchemy error whose MESSAGE carries sensitive text."""


class FakeConnection:
    """Records every statement. Acquisition and unlock results are configurable; any caller-owned operation raises, so a violation of ownership cannot pass quietly."""

    def __init__(self, *, acquire=True, unlock=True, unlock_error=None):
        self.acquire, self.unlock, self.unlock_error = acquire, unlock, unlock_error
        self.statements: list[tuple[str, dict]] = []

    def execute(self, statement, parameters=None):
        sql = str(statement)
        self.statements.append((sql, dict(parameters or {})))
        if "pg_try_advisory_lock" in sql:
            return _Result(self.acquire)
        if "pg_advisory_unlock" in sql:
            if self.unlock_error is not None:
                raise self.unlock_error
            return _Result(self.unlock)
        raise AssertionError(f"unexpected statement {sql!r}")

    def _forbidden(self, name):
        raise AssertionError(f"the lock manager called the caller-owned operation {name}()")

    commit = lambda self: self._forbidden("commit")  # noqa: E731
    rollback = lambda self: self._forbidden("rollback")  # noqa: E731
    close = lambda self: self._forbidden("close")  # noqa: E731
    invalidate = lambda self: self._forbidden("invalidate")  # noqa: E731
    dispose = lambda self: self._forbidden("dispose")  # noqa: E731

    @property
    def unlocks(self) -> int:
        return sum(1 for sql, _ in self.statements if "pg_advisory_unlock" in sql)


NOTE_PREFIX = "the advisory lock release also failed ("


def _run(connection, body):
    with bulk_import_advisory_lock(connection, TARGET):
        body()


def test_a1_normal_body_and_successful_release_attempt_unlock_exactly_once():
    connection = FakeConnection()
    ran = []
    _run(connection, lambda: ran.append(1))
    assert ran == [1] and connection.unlocks == 1
    assert [("pg_try_advisory_lock" in sql) for sql, _ in connection.statements] == [True, False]
    assert all(parameters == {"key": KEY} for _, parameters in connection.statements)


def test_a2_a_primary_failure_with_a_successful_release_is_preserved_untouched():
    connection, primary = FakeConnection(), RuntimeError("primary")

    def body():
        raise primary

    with pytest.raises(RuntimeError) as excinfo:
        _run(connection, body)
    assert excinfo.value is primary and getattr(primary, "__notes__", []) == [] and connection.unlocks == 1


def test_a3_a_primary_failure_with_a_raised_release_error_is_preserved_with_its_cause_notes_and_traceback_and_cleanup_is_recorded():
    connection = FakeConnection(unlock_error=DriverFailure(RELEASE_SECRET))
    cause = ValueError("the original cause")
    primary = RuntimeError("primary failure")
    primary.add_note("an existing note")

    def body_that_fails():
        raise primary from cause

    with pytest.raises(RuntimeError) as excinfo:
        _run(connection, body_that_fails)
    caught = excinfo.value
    assert caught is primary and type(caught) is RuntimeError and str(caught) == "primary failure"
    assert caught.__cause__ is cause, "an explicit cause is never erased"
    assert caught.__notes__[0] == "an existing note" and len(caught.__notes__) == 2, "existing notes are kept and exactly one is added"
    assert "body_that_fails" in [frame.name for frame in traceback.extract_tb(caught.__traceback__)], "the traceback still reaches the protected body"
    assert connection.unlocks == 1, "unlock was attempted exactly once and not retried"


def test_a3_the_cleanup_note_is_bounded_and_names_only_the_exception_type():
    connection = FakeConnection(unlock_error=DriverFailure(RELEASE_SECRET))
    primary = RuntimeError("primary")

    def body():
        raise primary

    with pytest.raises(RuntimeError):
        _run(connection, body)
    (note,) = primary.__notes__
    assert note.startswith(NOTE_PREFIX) and "DriverFailure" in note and len(note) <= 200
    for sensitive in ("RELEASE-SECRET", "postgresql://", "db.internal", "sslmode", str(KEY), "napsedge_pg_test_lockfix", "pg_advisory_unlock"):
        assert sensitive not in note, sensitive


def test_a4_a_primary_failure_with_a_false_unlock_result_is_preserved_and_cleanup_is_recorded():
    connection, primary = FakeConnection(unlock=False), RuntimeError("primary")

    def body():
        raise primary

    with pytest.raises(RuntimeError) as excinfo:
        _run(connection, body)
    assert excinfo.value is primary and connection.unlocks == 1
    (note,) = primary.__notes__
    assert "did not hold the lock" in note and len(note) <= 200 and str(KEY) not in note


def test_a5_release_only_failures_stay_visible():
    failure = DriverFailure(RELEASE_SECRET)
    connection = FakeConnection(unlock_error=failure)
    with pytest.raises(DriverFailure) as raised:
        _run(connection, lambda: None)
    assert raised.value is failure and connection.unlocks == 1
    not_held = FakeConnection(unlock=False)
    with pytest.raises(AdvisoryLockUnavailableError, match="was not held by this session"):
        _run(not_held, lambda: None)
    assert not_held.unlocks == 1


def test_a6_a_refused_acquisition_never_runs_the_body_and_never_attempts_unlock():
    connection, ran = FakeConnection(acquire=False), []
    with pytest.raises(AdvisoryLockUnavailableError, match="another connection already holds"):
        _run(connection, lambda: ran.append(1))
    assert ran == [] and connection.unlocks == 0 and len(connection.statements) == 1


@pytest.mark.parametrize("scenario", ["raised_error", "false_result"])
def test_a6_unlock_is_never_retried_after_a_failure(scenario):
    connection = FakeConnection(unlock_error=DriverFailure("x")) if scenario == "raised_error" else FakeConnection(unlock=False)
    with pytest.raises(RuntimeError):
        _run(connection, lambda: (_ for _ in ()).throw(RuntimeError("primary")))
    assert connection.unlocks == 1 and len(connection.statements) == 2


def test_a7_the_callers_connection_is_never_committed_rolled_back_closed_invalidated_or_disposed_on_any_path():
    for connection, body_fails in ((FakeConnection(), False), (FakeConnection(), True), (FakeConnection(unlock_error=DriverFailure("x")), True),
                                   (FakeConnection(unlock_error=DriverFailure("x")), False), (FakeConnection(unlock=False), True), (FakeConnection(acquire=False), False)):
        def body(fails=body_fails):
            if fails:
                raise RuntimeError("primary")

        with contextlib.suppress(RuntimeError, DriverFailure, AdvisoryLockUnavailableError):
            _run(connection, body)  # an AssertionError from a forbidden caller-owned call is NOT suppressed, so it fails the test
        assert all("commit" not in sql.lower() and "rollback" not in sql.lower() for sql, _ in connection.statements)


def test_a7_nested_acquisition_on_one_connection_stays_balanced():
    connection = FakeConnection()
    with bulk_import_advisory_lock(connection, TARGET):
        with bulk_import_advisory_lock(connection, TARGET):
            pass
    assert [("lock" if "pg_try_advisory_lock" in sql else "unlock") for sql, _ in connection.statements] == ["lock", "lock", "unlock", "unlock"]


def test_a7_signature_exports_and_unlock_call_site_are_unchanged():
    import src.pipeline.bulk_import_safety as mod

    assert list(inspect.signature(bulk_import_advisory_lock).parameters) == ["connection", "verified_target"]
    assert mod.__all__ == ["ADMINISTRATIVE_DATABASE_NAMES", "ACKNOWLEDGEMENT_TEMPLATE", "LOCK_NAMESPACE", "build_expected_acknowledgement", "DatabaseTargetDesignation",
                           "VerifiedDatabaseTarget", "verify_database_target", "compute_advisory_lock_key", "bulk_import_advisory_lock"]
    tree = ast.parse(inspect.getsource(mod))
    texts = [n.args[0].value for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "text" and n.args and isinstance(n.args[0], ast.Constant)]
    assert sum("pg_advisory_unlock" in t for t in texts) == 1, "exactly one unlock call site, so no second release algorithm exists"
    assert sum("pg_try_advisory_lock" in t for t in texts) == 1
    raises = [n for n in ast.walk(tree) if isinstance(n, ast.Raise)]
    assert not any(isinstance(n.cause, ast.Constant) and n.cause.value is None for n in raises), "no `raise ... from None`: an existing cause is never erased"


@pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit])
def test_a8_a_primary_interrupt_with_an_ordinary_unlock_failure_is_preserved(interrupt_type):
    connection, primary = FakeConnection(unlock_error=DriverFailure(RELEASE_SECRET)), interrupt_type()

    def body():
        raise primary

    with pytest.raises(interrupt_type) as excinfo:
        _run(connection, body)
    assert excinfo.value is primary and len(primary.__notes__) == 1 and "DriverFailure" in primary.__notes__[0] and "RELEASE-SECRET" not in primary.__notes__[0]
    assert connection.unlocks == 1


@pytest.mark.parametrize("new_interrupt", [KeyboardInterrupt, SystemExit])
def test_a8_a_new_interrupt_raised_during_cleanup_is_not_swallowed(new_interrupt):
    interrupt = new_interrupt()
    connection, primary = FakeConnection(unlock_error=interrupt), RuntimeError("primary")

    def body():
        raise primary

    with pytest.raises(new_interrupt) as excinfo:
        _run(connection, body)
    assert excinfo.value is interrupt and connection.unlocks == 1
    assert excinfo.value.__context__ is primary, "the primary failure remains reachable as the context of the new interrupt"
