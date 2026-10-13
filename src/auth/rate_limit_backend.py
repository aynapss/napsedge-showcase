"""
Phase 9D (decisions 2-3, approved 2026-10-09): choosing the API's rate limiter from configuration. Kept OUT of
`src.auth.rate_limit` on purpose: that module (the in-memory limiter) is guarded to import nothing network-capable
(tests/regression/test_rate_limit.py), while choosing the shared Redis backend needs URL parsing and the Redis client.
"""
from __future__ import annotations

import os
from typing import Mapping

from src.auth.rate_limit import RateLimiter
from src.exceptions import RateLimitConfigurationError

BACKEND_ENV_VAR = "NAPSEDGE_RATE_LIMIT_BACKEND"
REDIS_URL_ENV_VAR = "NAPSEDGE_RATE_LIMIT_REDIS_URL"
REDIS_PASSWORD_FILE_ENV_VAR = "NAPSEDGE_RATE_LIMIT_REDIS_PASSWORD_FILE"
BACKENDS = ("memory", "redis")


def build_rate_limiter(env: Mapping[str, str] | None = None):
    """
    The limiter selected by NAPSEDGE_RATE_LIMIT_BACKEND: `memory` (the default; the single-process `src.auth.rate_limit.RateLimiter`) or
    `redis` (the shared `src.auth.rate_limit_redis.RedisRateLimiter`, required before running more than one API worker). For
    `redis`, NAPSEDGE_RATE_LIMIT_REDIS_URL must be a `redis://` or `rediss://` URL WITHOUT a password; a password, if the server
    needs one, comes from NAPSEDGE_RATE_LIMIT_REDIS_PASSWORD_FILE (a credential file, `src.secret_files`). Anything malformed is
    refused with `RateLimitConfigurationError` (fail closed; values are never echoed). No connection is opened here.
    """
    source = env if env is not None else os.environ
    backend = (source.get(BACKEND_ENV_VAR) or "memory").strip().lower()
    if backend not in BACKENDS:
        raise RateLimitConfigurationError(f"{BACKEND_ENV_VAR} must be one of {', '.join(BACKENDS)}.")
    if backend == "memory":
        return RateLimiter()
    url = (source.get(REDIS_URL_ENV_VAR) or "").strip()
    if not url.startswith(("redis://", "rediss://")):
        raise RateLimitConfigurationError(f"{REDIS_URL_ENV_VAR} must be a redis:// or rediss:// URL when {BACKEND_ENV_VAR}=redis.")
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        has_password = parts.password is not None
    except ValueError:
        raise RateLimitConfigurationError(f"{REDIS_URL_ENV_VAR} is not a valid URL.") from None
    if has_password:
        raise RateLimitConfigurationError(f"{REDIS_URL_ENV_VAR} must not carry a password; use {REDIS_PASSWORD_FILE_ENV_VAR}.")
    password = None
    password_path = (source.get(REDIS_PASSWORD_FILE_ENV_VAR) or "").strip()
    if password_path:
        from src.exceptions import SecretFileConfigurationError
        from src.secret_files import read_secret_file

        try:
            password = read_secret_file(REDIS_PASSWORD_FILE_ENV_VAR, password_path)
        except SecretFileConfigurationError as exc:
            raise RateLimitConfigurationError(str(exc)) from None
    from src.auth.rate_limit_redis import RedisRateLimiter

    try:
        return RedisRateLimiter.connect(url, password=password)
    except ValueError:  # the client's own URL parsing (e.g. a non-numeric port)
        raise RateLimitConfigurationError(f"{REDIS_URL_ENV_VAR} is not a valid URL.") from None
