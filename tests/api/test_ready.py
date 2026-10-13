"""Phase 6B: GET /v1/ready -- read-only database/schema readiness probe."""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from src.api.deps import get_engine
from src.api.main import create_app


def test_ready_returns_200_with_a_healthy_migrated_database(migrated_db_url):
    app = create_app(database_url=migrated_db_url)

    with TestClient(app) as client:
        response = client.get("/v1/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_ready_returns_503_for_an_unreachable_database(unreachable_db_url):
    app = create_app(database_url=unreachable_db_url)

    with TestClient(app) as client:
        response = client.get("/v1/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "not_ready"
    assert "message" in body
    # No sensitive value leaked into the response body.
    assert unreachable_db_url not in str(body)


def test_ready_returns_503_for_a_database_with_no_migration_state(tmp_path):
    """A real, reachable database that was never migrated -- distinct failure mode from an unreachable one, same sanitized 503 contract."""
    db_path = tmp_path / "never_migrated.db"
    # Create the file (a real, connectable database) without running Alembic.
    create_engine(f"sqlite+pysqlite:///{db_path.as_posix()}").connect().close()

    app = create_app(database_url=f"sqlite+pysqlite:///{db_path.as_posix()}")

    with TestClient(app) as client:
        response = client.get("/v1/ready")

    assert response.status_code == 503
    assert response.json()["error"] == "not_ready"


def test_ready_performs_no_write_to_the_database(migrated_db_url):
    app = create_app(database_url=migrated_db_url)

    engine = create_engine(migrated_db_url, connect_args={"check_same_thread": False})
    try:
        with engine.connect() as conn:
            before = conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()

        with TestClient(app) as client:
            for _ in range(3):
                client.get("/v1/ready")

        with engine.connect() as conn:
            after = conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()

        assert before == after
    finally:
        engine.dispose()


def test_ready_dependency_override_replaces_the_injected_engine(migrated_db_url, unreachable_db_url):
    """Confirms app.dependency_overrides works for get_engine -- a real engine can be swapped for a failing one without rebuilding the app."""
    app = create_app(database_url=migrated_db_url)
    bad_engine = create_engine(unreachable_db_url)
    app.dependency_overrides[get_engine] = lambda: bad_engine

    with TestClient(app) as client:
        response = client.get("/v1/ready")

    assert response.status_code == 503


def test_ready_is_deterministic_across_repeated_requests(client):
    responses = [client.get("/v1/ready") for _ in range(5)]
    assert all(r.status_code == 200 for r in responses)
    assert all(r.json() == {"status": "ready"} for r in responses)
