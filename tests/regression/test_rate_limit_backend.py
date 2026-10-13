"""
Phase 9D: selecting and validating the rate-limit backend (`src.auth.rate_limit_backend.build_rate_limiter`). No Redis server is needed
here; the shared-budget behaviour against a real Redis is in tests/redis (marker `redis`).
"""
from __future__ import annotations

import pytest

from src.auth.rate_limit import RateLimiter
from src.auth.rate_limit_backend import build_rate_limiter
from src.exceptions import RateLimitConfigurationError


def test_memory_is_the_default_and_explicit_choice():
    assert isinstance(build_rate_limiter({}), RateLimiter)
    assert isinstance(build_rate_limiter({"NAPSEDGE_RATE_LIMIT_BACKEND": "memory"}), RateLimiter)


@pytest.mark.parametrize("env, fragment", [
    ({"NAPSEDGE_RATE_LIMIT_BACKEND": "postgres"}, "must be one of memory, redis"),
    ({"NAPSEDGE_RATE_LIMIT_BACKEND": "redis"}, "must be a redis:// or rediss:// URL"),
    ({"NAPSEDGE_RATE_LIMIT_BACKEND": "redis", "NAPSEDGE_RATE_LIMIT_REDIS_URL": "http://cache:6379/0"}, "must be a redis:// or rediss:// URL"),
    ({"NAPSEDGE_RATE_LIMIT_BACKEND": "redis", "NAPSEDGE_RATE_LIMIT_REDIS_URL": "redis://:hunter2-inline@cache:6379/0"}, "must not carry a password"),
    ({"NAPSEDGE_RATE_LIMIT_BACKEND": "redis", "NAPSEDGE_RATE_LIMIT_REDIS_URL": "redis://cache:6379/0",
      "NAPSEDGE_RATE_LIMIT_REDIS_PASSWORD_FILE": "relative/path"}, "NAPSEDGE_RATE_LIMIT_REDIS_PASSWORD_FILE must be an absolute path"),
])
def test_malformed_backend_settings_fail_closed_without_echoing_values(env, fragment):
    with pytest.raises(RateLimitConfigurationError) as exc:
        build_rate_limiter(env)
    assert fragment in str(exc.value) and "hunter2-inline" not in str(exc.value)


def test_a_valid_redis_setting_builds_the_shared_limiter_without_connecting():
    pytest.importorskip("redis")
    from src.auth.rate_limit_redis import RedisRateLimiter

    limiter = build_rate_limiter({"NAPSEDGE_RATE_LIMIT_BACKEND": "redis", "NAPSEDGE_RATE_LIMIT_REDIS_URL": "redis://127.0.0.1:1/0"})
    assert isinstance(limiter, RedisRateLimiter)


def test_a_redis_url_the_client_cannot_parse_fails_closed_as_a_configuration_error():
    pytest.importorskip("redis")
    with pytest.raises(RateLimitConfigurationError) as exc:
        build_rate_limiter({"NAPSEDGE_RATE_LIMIT_BACKEND": "redis", "NAPSEDGE_RATE_LIMIT_REDIS_URL": "redis://cache:port-oops/0"})
    assert "NAPSEDGE_RATE_LIMIT_REDIS_URL" in str(exc.value) and "port-oops" not in str(exc.value)


def test_api_startup_validation_reports_a_malformed_backend(monkeypatch):
    from src.deployment.env_validation import DeploymentConfigurationError, validate_api_environment

    monkeypatch.setenv("NAPSEDGE_API_KEY_HASHES_JSON", '{"client-a": "%s"}' % ("a" * 64))
    monkeypatch.setenv("NAPSEDGE_NET_RANKINGS_SEASON", "2026")
    monkeypatch.setenv("NAPSEDGE_RATE_LIMIT_BACKEND", "redis")
    monkeypatch.delenv("NAPSEDGE_RATE_LIMIT_REDIS_URL", raising=False)
    with pytest.raises(DeploymentConfigurationError, match="NAPSEDGE_RATE_LIMIT_REDIS_URL must be a redis"):
        validate_api_environment()
