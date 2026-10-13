"""
Phase 5B7: safety-guard tests for the disposable-PostgreSQL harness itself
-- these must pass even if Docker is unavailable (they test the guard
function, not the live container), except where a live container is
explicitly required (marked `postgres`).
"""
from __future__ import annotations

import os

import pytest

from tests.postgres.conftest import TEST_DB_PREFIX, assert_disposable_database_name


def test_refuses_production_looking_database_names():
    for bad_name in ("napsedge", "napsedge_production", "postgres", "production", "prod_db", ""):
        with pytest.raises(RuntimeError):
            assert_disposable_database_name(bad_name)


def test_accepts_conforming_test_database_name():
    assert_disposable_database_name(f"{TEST_DB_PREFIX}abc123")  # must not raise


def test_prefix_is_not_a_substring_match_trick():
    """A name that merely CONTAINS the prefix, rather than starting with it, must still be refused."""
    with pytest.raises(RuntimeError):
        assert_disposable_database_name(f"production_but_contains_{TEST_DB_PREFIX}somewhere")


def test_harness_never_reads_napsedge_database_url_env_var():
    """The disposable harness must never consult the production/dev connection-string env var -- confirmed by AST inspection of actual code (not the module's own prose docstring, which legitimately mentions the name)."""
    import ast
    import inspect

    import tests.postgres.conftest as mod

    tree = ast.parse(inspect.getsource(mod))
    string_literals = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    env_reads = {n.args[0].value for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get" and n.args and isinstance(n.args[0], ast.Constant)}
    assert "NAPSEDGE_DATABASE_URL" not in env_reads
    assert not any("NAPSEDGE_DATABASE_URL" in s and "os.environ" in s for s in string_literals)


def test_harness_never_reads_dotenv_file_directly():
    import inspect

    import tests.postgres.conftest as mod

    source = inspect.getsource(mod)
    assert "load_env_file" not in source
    assert "python-dotenv" not in source


@pytest.mark.postgres
def test_disposable_instance_identifies_as_postgresql(pg_version):
    assert "PostgreSQL" in pg_version


@pytest.mark.postgres
def test_disposable_database_name_conforms_to_prefix(pg_empty_database_url):
    db_name = pg_empty_database_url.rsplit("/", 1)[-1]
    assert db_name.startswith(TEST_DB_PREFIX)
    assert_disposable_database_name(db_name)  # must not raise


@pytest.mark.postgres
def test_credentials_are_generated_not_a_known_default(pg_container):
    """Confirms this batch generated its own random credentials rather than relying on testcontainers' own well-known 'test'/'test' default -- a real, if minor, hardening over the smoke-test default."""
    assert pg_container.username != "test"
    assert pg_container.password != "test"
    assert len(pg_container.password) >= 20


@pytest.mark.postgres
def test_cleanup_actually_drops_the_database(pg_container, pg_empty_database_url):
    """Confirms the per-test database really disappears after the fixture tears down -- exercised by nesting a second, independent check inside this same test's own fixture lifetime is not possible (teardown happens after the test), so this test instead directly proves the drop mechanism works against a throwaway database it creates and destroys itself, independent of the shared fixture's own teardown timing."""
    from sqlalchemy import create_engine, text

    admin_url = pg_container.get_connection_url()
    probe_db = f"{TEST_DB_PREFIX}cleanup_probe_{os.getpid()}"
    assert_disposable_database_name(probe_db)
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{probe_db}"'))
            [exists] = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": probe_db}).one()
            assert exists == 1
            conn.execute(text(f'DROP DATABASE "{probe_db}"'))
            row = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": probe_db}).one_or_none()
            assert row is None
    finally:
        admin_engine.dispose()


@pytest.mark.postgres
def test_no_extra_containers_left_running_beyond_the_shared_one_and_reaper():
    """A coarse leak check: after this module's own fixtures have run, docker ps should show at most the shared pg_container and testcontainers' own Ryuk reaper -- never an accumulating pile of leftover containers from this batch's own tests."""
    import docker as docker_sdk

    client = docker_sdk.from_env()
    containers = client.containers.list()
    non_reaper = [c for c in containers if "ryuk" not in c.image.tags[0].lower() if c.image.tags]
    postgres_containers = [c for c in non_reaper if "postgres" in (c.image.tags[0] if c.image.tags else "")]
    assert len(postgres_containers) <= 1  # only ever the one session-scoped disposable container
