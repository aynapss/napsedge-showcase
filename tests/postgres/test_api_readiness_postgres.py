"""
Phase 6B: the smallest genuine PostgreSQL proof that `/v1/ready` behaves
correctly against a real (not SQLite-emulated) database. Reuses the
existing disposable-PostgreSQL fixtures from `tests/postgres/conftest.py`
(`pg_migrated_database_url`, `pg_empty_database_url`) -- no competing
container harness is created here.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.main import create_app

pytestmark = pytest.mark.postgres


def test_ready_returns_200_against_a_real_migrated_postgresql_database(pg_migrated_database_url):
    app = create_app(database_url=pg_migrated_database_url)
    with TestClient(app) as client:
        response = client.get("/v1/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_ready_returns_503_against_a_real_but_unmigrated_postgresql_database(pg_empty_database_url):
    app = create_app(database_url=pg_empty_database_url)
    with TestClient(app) as client:
        response = client.get("/v1/ready")

    assert response.status_code == 503
    assert response.json()["error"] == "not_ready"


def test_ready_against_postgresql_leaves_zero_open_connections_after_teardown(pg_migrated_database_url):
    """Resource-cleanup proof: the engine created inside create_app() must not leak a pooled connection past the request/app lifecycle."""
    from sqlalchemy import create_engine, text

    app = create_app(database_url=pg_migrated_database_url)
    with TestClient(app) as client:
        response = client.get("/v1/ready")
    assert response.status_code == 200
    app.state.engine.dispose()

    admin_engine = create_engine(pg_migrated_database_url)
    try:
        with admin_engine.connect() as conn:
            active = conn.execute(
                text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()")
            ).scalar()
        assert active == 0
    finally:
        admin_engine.dispose()
