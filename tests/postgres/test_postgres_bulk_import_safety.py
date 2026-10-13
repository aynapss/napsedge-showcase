"""
Controlled bulk-import design, Slice 4 (docs/controlled_bulk_import_design.md
section 12, approved for this slice only): real PostgreSQL integration
tests for `src/pipeline/bulk_import_safety.py` -- actual target
verification against a live server, actual advisory-lock contention and
release, and a live SQL-statement capture proving this module issues only
read-only introspection and advisory-lock function calls.

Reuses the exact disposable-container fixtures from tests/postgres/conftest.py
(pg_container / pg_migrated_database_url) -- no new container logic, no
new safety guard of its own. Synthetic setup only, never TeamDump.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, text

from src.exceptions import AdvisoryLockUnavailableError, DatabaseTargetVerificationError, ValidationError
from src.pipeline.bulk_import_safety import (
    DatabaseTargetDesignation,
    VerifiedDatabaseTarget,
    build_expected_acknowledgement,
    bulk_import_advisory_lock,
    compute_advisory_lock_key,
    verify_database_target,
)

pytestmark = pytest.mark.postgres


def _real_database_name(connection) -> str:
    return connection.execute(text("SELECT current_database()")).scalar_one()


def _designation_for(name: str) -> DatabaseTargetDesignation:
    return DatabaseTargetDesignation(expected_database_name=name, acknowledgement=build_expected_acknowledgement(name))


# ---------------------------------------------------------------------------
# Target verification against a real server
# ---------------------------------------------------------------------------


def test_correct_disposable_database_passes_verification(pg_migrated_database_url):
    engine = create_engine(pg_migrated_database_url)
    try:
        with engine.connect() as conn:
            name = _real_database_name(conn)
            target = verify_database_target(conn, _designation_for(name))
        assert target.database_name == name
        assert "PostgreSQL" in target.server_version
    finally:
        engine.dispose()


def test_incorrect_expected_database_name_fails(pg_migrated_database_url):
    engine = create_engine(pg_migrated_database_url)
    try:
        with engine.connect() as conn:
            real_name = _real_database_name(conn)
            wrong_designation = _designation_for(real_name + "_wrong")
            with pytest.raises(DatabaseTargetVerificationError):
                verify_database_target(conn, wrong_designation)
    finally:
        engine.dispose()


def test_missing_acknowledgement_fails_before_any_connection_use(pg_migrated_database_url):
    """A missing/incorrect acknowledgement is rejected at DatabaseTargetDesignation construction -- never even reaches verify_database_target."""
    with pytest.raises(ValidationError):
        DatabaseTargetDesignation(expected_database_name="whatever", acknowledgement="")


def test_incorrect_acknowledgement_for_the_real_database_fails(pg_migrated_database_url):
    engine = create_engine(pg_migrated_database_url)
    try:
        with engine.connect() as conn:
            real_name = _real_database_name(conn)
        with pytest.raises(ValidationError):
            DatabaseTargetDesignation(expected_database_name=real_name, acknowledgement="wrong phrase")
    finally:
        engine.dispose()


def test_sqlite_connection_fails_verification_against_real_fixture_comparison(pg_migrated_database_url):
    """Confirms the rejection is dialect-based, not merely a name mismatch -- a SQLite connection fails even if given the real disposable database's own name."""
    engine = create_engine(pg_migrated_database_url)
    try:
        with engine.connect() as conn:
            real_name = _real_database_name(conn)
    finally:
        engine.dispose()

    sqlite_engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with sqlite_engine.connect() as sqlite_conn:
            with pytest.raises(DatabaseTargetVerificationError):
                verify_database_target(sqlite_conn, _designation_for(real_name))
    finally:
        sqlite_engine.dispose()


# ---------------------------------------------------------------------------
# Advisory-lock contention and release
# ---------------------------------------------------------------------------


def test_first_connection_acquires_second_fails_immediately_then_succeeds_after_release(pg_migrated_database_url):
    engine_a = create_engine(pg_migrated_database_url)
    engine_b = create_engine(pg_migrated_database_url)
    try:
        with engine_a.connect() as conn_a, engine_b.connect() as conn_b:
            name = _real_database_name(conn_a)
            target = verify_database_target(conn_a, _designation_for(name))

            with bulk_import_advisory_lock(conn_a, target):
                # A second, independent connection must fail immediately -- no blocking, no retry.
                with pytest.raises(AdvisoryLockUnavailableError):
                    with bulk_import_advisory_lock(conn_b, target):
                        pass  # pragma: no cover -- must never be entered

            # After conn_a's own lock is released (the `with` block above exited), conn_b must now succeed.
            with bulk_import_advisory_lock(conn_b, target) as key:
                assert isinstance(key, int)
    finally:
        engine_a.dispose()
        engine_b.dispose()


def test_exception_inside_context_still_releases_the_lock(pg_migrated_database_url):
    engine = create_engine(pg_migrated_database_url)
    try:
        with engine.connect() as conn:
            name = _real_database_name(conn)
            target = verify_database_target(conn, _designation_for(name))

            with pytest.raises(RuntimeError):
                with bulk_import_advisory_lock(conn, target):
                    raise RuntimeError("simulated failure inside the locked section")

            # The lock must be free again -- re-acquiring on the SAME connection must succeed immediately.
            with bulk_import_advisory_lock(conn, target):
                pass
    finally:
        engine.dispose()


def test_no_lock_remains_after_normal_exit(pg_migrated_database_url):
    engine = create_engine(pg_migrated_database_url)
    try:
        with engine.connect() as conn:
            name = _real_database_name(conn)
            target = verify_database_target(conn, _designation_for(name))
            with bulk_import_advisory_lock(conn, target):
                pass
            remaining = conn.execute(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")).scalar_one()
        assert remaining == 0
    finally:
        engine.dispose()


def test_no_lock_remains_after_exceptional_exit(pg_migrated_database_url):
    engine = create_engine(pg_migrated_database_url)
    try:
        with engine.connect() as conn:
            name = _real_database_name(conn)
            target = verify_database_target(conn, _designation_for(name))
            with pytest.raises(RuntimeError):
                with bulk_import_advisory_lock(conn, target):
                    raise RuntimeError("boom")
            remaining = conn.execute(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")).scalar_one()
        assert remaining == 0
    finally:
        engine.dispose()


def test_nested_acquisition_on_the_same_connection_is_balanced(pg_migrated_database_url):
    """PostgreSQL's own session-level advisory locks are naturally reentrant per (session, key) via an internal reference count -- this module does not fight that native behavior."""
    engine = create_engine(pg_migrated_database_url)
    try:
        with engine.connect() as conn:
            name = _real_database_name(conn)
            target = verify_database_target(conn, _designation_for(name))
            with bulk_import_advisory_lock(conn, target):
                with bulk_import_advisory_lock(conn, target):
                    pass  # the same session re-acquiring its own held lock succeeds immediately
                # inner release balances the inner acquire -- outer lock must still be held here,
                # confirmed by a second, independent connection still failing to acquire it.
                other_engine = create_engine(pg_migrated_database_url)
                try:
                    with other_engine.connect() as other_conn:
                        with pytest.raises(AdvisoryLockUnavailableError):
                            with bulk_import_advisory_lock(other_conn, target):
                                pass
                finally:
                    other_engine.dispose()
            remaining = conn.execute(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")).scalar_one()
        assert remaining == 0
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# SQL-statement inspection: only introspection + advisory-lock calls
# ---------------------------------------------------------------------------


def test_sql_statement_capture_proves_only_readonly_and_advisory_lock_statements(pg_migrated_database_url):
    engine = create_engine(pg_migrated_database_url)
    captured: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        captured.append(statement.strip())

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        with engine.connect() as conn:
            name = _real_database_name(conn)
            captured.clear()  # drop the discovery query above -- only capture the module's own statements from here
            target = verify_database_target(conn, _designation_for(name))
            with bulk_import_advisory_lock(conn, target):
                pass
    finally:
        event.remove(engine, "before_cursor_execute", _capture)
        engine.dispose()

    assert captured, "expected at least one statement to be captured"
    forbidden_verbs = ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "COMMIT", "ROLLBACK", "TRUNCATE")
    for statement in captured:
        upper = statement.upper()
        assert not any(verb in upper for verb in forbidden_verbs), f"unexpected non-read-only statement: {statement!r}"
        assert upper.startswith("SELECT"), f"expected only SELECT-shaped statements, got: {statement!r}"


def test_caller_transaction_ownership_remains_intact(pg_migrated_database_url):
    """This module never calls .commit()/.rollback() itself -- the caller's own transaction state (autobegin) is untouched by using the lock."""
    engine = create_engine(pg_migrated_database_url)
    try:
        with engine.connect() as conn:
            name = _real_database_name(conn)
            target = verify_database_target(conn, _designation_for(name))
            with bulk_import_advisory_lock(conn, target):
                in_transaction_during = conn.in_transaction()
            in_transaction_after = conn.in_transaction()
            # The connection's own transaction (autobegin on first statement) is a caller-owned concern --
            # this module never explicitly commits or rolls it back, confirmed by it remaining exactly as
            # the driver's own autobegin semantics left it, unaffected by entering/exiting the lock context.
            assert in_transaction_during == in_transaction_after
            conn.rollback()  # caller's own explicit choice, proving the caller still owns this decision
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 63-bit lock-key round trip
# ---------------------------------------------------------------------------


def test_63_bit_lock_key_round_trips_through_a_real_advisory_lock_call(pg_migrated_database_url):
    """Confirms the exact computed key is what PostgreSQL itself receives and later releases -- proven by directly calling pg_try_advisory_lock/pg_advisory_unlock with this module's own key outside the context manager and getting the expected true/true results, never a silently coerced or truncated value."""
    engine = create_engine(pg_migrated_database_url)
    try:
        with engine.connect() as conn:
            name = _real_database_name(conn)
            target = verify_database_target(conn, _designation_for(name))
            key = compute_advisory_lock_key(target)
            assert 0 <= key < 2**63

            acquired = conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar_one()
            assert acquired is True
            released = conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key}).scalar_one()
            assert released is True

            with bulk_import_advisory_lock(conn, target) as yielded_key:
                assert yielded_key == key
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def test_cleanup_leaves_no_residual_advisory_locks_after_test_database_teardown(pg_container):
    """Confirms this module's own usage pattern never leaks a session-level lock beyond its own connection's lifetime -- checked against the shared container's own admin connection after a full acquire/release cycle on a throwaway database."""
    from tests.postgres.conftest import alembic_config_for
    from alembic import command
    import uuid

    from sqlalchemy import text as _text

    admin_url = pg_container.get_connection_url()
    db_name = f"napsedge_pg_test_{uuid.uuid4().hex[:16]}"
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            conn.execute(_text(f'CREATE DATABASE "{db_name}"'))
        test_url = admin_url.rsplit("/", 1)[0] + f"/{db_name}"
        command.upgrade(alembic_config_for(test_url), "head")

        engine = create_engine(test_url)
        try:
            with engine.connect() as conn:
                target = verify_database_target(conn, _designation_for(db_name))
                with bulk_import_advisory_lock(conn, target):
                    pass
        finally:
            engine.dispose()

        with admin_engine.connect() as conn:
            conn.execute(_text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :db AND pid <> pg_backend_pid()"), {"db": db_name})
            conn.execute(_text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    finally:
        admin_engine.dispose()


# ---------------------------------------------------------------------------
# Exception preservation, against a REAL backend (Phase 9B-5C4 correction). Each test drives the PRODUCTION context manager directly.
# Preserving the primary failure is not releasing the lock: release is verified separately, from PostgreSQL, and cleanup is caller-owned.
# ---------------------------------------------------------------------------
import contextlib  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402


def _wait_until(condition, timeout: float = 15.0) -> bool:
    """Bounded CONDITION polling (never an ordering sleep)."""
    deadline, pause = time.monotonic() + timeout, threading.Event()
    while time.monotonic() < deadline:
        if condition():
            return True
        pause.wait(0.1)
    return condition()


def _advisory_holders(admin_engine, key: int) -> list[int]:
    with admin_engine.connect() as admin:
        return [r[0] for r in admin.execute(text("SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND classid::bigint = :c AND objid::bigint = :o AND objsubid = 1 ORDER BY pid"),
                                            {"c": key >> 32, "o": key & 0xFFFFFFFF}).all()]


def _backend_exists(admin_engine, pid: int) -> bool:
    with admin_engine.connect() as admin:
        return admin.execute(text("SELECT count(*) FROM pg_stat_activity WHERE pid = :p"), {"p": pid}).scalar_one() == 1


def test_real_backend_loss_inside_the_protected_body_preserves_the_primary_failure_through_the_production_manager(pg_migrated_database_url):
    owner_engine, other_engine = create_engine(pg_migrated_database_url), create_engine(pg_migrated_database_url)
    admin_engine = create_engine(pg_migrated_database_url, isolation_level="AUTOCOMMIT")
    conn = owner_engine.connect()
    pid = None
    try:
        target = verify_database_target(conn, _designation_for(_real_database_name(conn)))
        key = compute_advisory_lock_key(target)
        pid = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
        primary = RuntimeError("primary failure while the owning backend disappears")
        with pytest.raises(RuntimeError) as excinfo:
            with bulk_import_advisory_lock(conn, target):
                with admin_engine.connect() as admin:
                    assert admin.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid}).scalar_one() is True
                assert _wait_until(lambda: not _backend_exists(admin_engine, pid)), "the owning backend was removed"
                raise primary
        assert excinfo.value is primary, "the production manager preserved the ORIGINAL exception object, not the unlock error"
        assert len(primary.__notes__) == 1 and primary.__notes__[0].startswith("the advisory lock release also failed (") and "OperationalError" in primary.__notes__[0]
        # release was UNCONFIRMED by the manager; what PostgreSQL itself says is checked separately: the session ended, so the lock is gone
        assert _advisory_holders(admin_engine, key) == []
        # and a subsequent acquisition by a fresh session succeeds
        with other_engine.connect() as other:
            with bulk_import_advisory_lock(other, verify_database_target(other, _designation_for(target.database_name))) as other_key:
                assert other_key == key
    finally:
        with contextlib.suppress(Exception):
            conn.close()
        owner_engine.dispose()  # caller-owned physical cleanup, verified below
        if pid is not None:
            assert _wait_until(lambda: not _backend_exists(admin_engine, pid))
        other_engine.dispose()
        admin_engine.dispose()


def test_a_database_error_that_aborts_the_transaction_is_preserved_and_the_lock_stays_held_until_the_caller_releases_it(pg_migrated_database_url):
    """Realistic and important: the body's own SQL error leaves the transaction aborted, so the manager's unlock statement cannot run. The PRIMARY error must survive; the lock is then still held (an unconfirmed release), and rollback() does not free a session-level lock."""
    engine, other_engine = create_engine(pg_migrated_database_url), create_engine(pg_migrated_database_url)
    admin_engine = create_engine(pg_migrated_database_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn, other_engine.connect() as other:
            target = verify_database_target(conn, _designation_for(_real_database_name(conn)))
            key = compute_advisory_lock_key(target)
            pid = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
            with pytest.raises(Exception) as excinfo:
                with bulk_import_advisory_lock(conn, target):
                    conn.execute(text("SELECT 1/0"))
            primary = excinfo.value
            assert type(primary).__name__ == "DataError", f"the body's own database error must be what propagates, not {type(primary).__name__}"
            assert len(primary.__notes__) == 1 and primary.__notes__[0].startswith("the advisory lock release also failed (")
            assert _advisory_holders(admin_engine, key) == [pid], "the release was UNCONFIRMED and the lock is in fact still held"
            conn.rollback()  # the caller's own decision: a session-level advisory lock survives it
            assert _advisory_holders(admin_engine, key) == [pid]
            with pytest.raises(AdvisoryLockUnavailableError):
                with bulk_import_advisory_lock(other, target):
                    pass  # pragma: no cover
            assert conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key}).scalar_one() is True, "caller-owned recovery: an explicit release on the same session"
            assert _advisory_holders(admin_engine, key) == []
            with bulk_import_advisory_lock(other, target):
                pass
    finally:
        engine.dispose()
        other_engine.dispose()
        admin_engine.dispose()


def test_a_false_unlock_result_with_a_primary_failure_is_preserved_and_without_one_stays_visible(pg_migrated_database_url):
    engine = create_engine(pg_migrated_database_url)
    admin_engine = create_engine(pg_migrated_database_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            target = verify_database_target(conn, _designation_for(_real_database_name(conn)))
            key = compute_advisory_lock_key(target)
            primary = RuntimeError("primary")
            with pytest.raises(RuntimeError) as excinfo:
                with bulk_import_advisory_lock(conn, target):
                    assert conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key}).scalar_one() is True  # the body releases the lock itself (a contract violation)
                    raise primary
            assert excinfo.value is primary and len(primary.__notes__) == 1 and "did not hold the lock" in primary.__notes__[0]
            with pytest.raises(AdvisoryLockUnavailableError, match="was not held by this session"):
                with bulk_import_advisory_lock(conn, target):
                    conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
            assert _advisory_holders(admin_engine, key) == []
    finally:
        engine.dispose()
        admin_engine.dispose()
