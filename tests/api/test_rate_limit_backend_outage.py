"""
Phase 9D (decision 2: fail closed): when the shared rate-limit backend is unavailable, every protected route answers the sanitized
503 -- the request is never admitted -- and health/ready stay unaffected. A stand-in limiter raises the backend error; the real
Redis behaviour is covered in tests/redis.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.main import create_app
from src.exceptions import RateLimitBackendUnavailableError


class _DownBackend:
    def check_and_consume(self, client_id, policy):
        raise RateLimitBackendUnavailableError("the shared rate-limit backend is unavailable (ConnectionError)")


@pytest.mark.parametrize("path", ["/v1/teams/1", "/v1/rankings/net"])
def test_a_backend_outage_refuses_protected_requests_with_a_sanitized_503(path, migrated_db_url, auth_headers, rankings_configured):
    app = create_app(database_url=migrated_db_url)
    app.state.rate_limiter = _DownBackend()
    with TestClient(app) as client:
        refused = client.get(path, headers=auth_headers)
        health = client.get("/v1/health")
    assert refused.status_code == 503 and refused.json()["error"] == "service_unavailable"
    assert "ConnectionError" not in refused.text and "redis" not in refused.text.lower()
    assert health.status_code == 200
