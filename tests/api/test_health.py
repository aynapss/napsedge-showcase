"""Phase 6B: GET /v1/health -- process liveness only, no database access."""
from __future__ import annotations

from fastapi.testclient import TestClient

from src.api.deps import get_engine
from src.api.main import create_app


def test_health_returns_200_and_the_exact_typed_schema(client: TestClient):
    response = client.get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_does_not_request_the_database_dependency(migrated_db_url):
    """Overriding get_engine to raise must not affect /v1/health -- the route must never resolve that dependency."""
    app = create_app(database_url=migrated_db_url)

    def _boom():
        raise RuntimeError("get_engine must not be called by /v1/health")

    app.dependency_overrides[get_engine] = _boom
    with TestClient(app) as client:
        response = client.get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_is_deterministic_across_repeated_requests(client: TestClient):
    responses = [client.get("/v1/health") for _ in range(5)]
    assert all(r.status_code == 200 for r in responses)
    assert all(r.json() == {"status": "ok"} for r in responses)
