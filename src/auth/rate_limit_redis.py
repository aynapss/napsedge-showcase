"""
Phase 9D (readiness section 7 and 15; decisions 2-3 approved by Anthony on 2026-10-09): a SHARED, Redis-backed fixed-window rate
limiter, so several API worker processes (or hosts) enforce ONE budget per `client_id`.

Same interface and semantics as the in-memory `src.auth.rate_limit.RateLimiter`:
- a client's window is anchored at its first request and lasts `policy.window_seconds`;
- a request is admitted and counted while the count is below `policy.max_requests`; at the limit it is rejected with
  `RateLimitExceededError` (with `retry_after_seconds`) and NOT counted;
- the check-then-increment is ONE atomic Lua script inside Redis, so concurrent workers can never race past the allowance.

Fail CLOSED (decision 2): any Redis error or timeout raises `RateLimitBackendUnavailableError`, a `RateLimitConfigurationError`
subclass that the API already maps to a sanitized 503 -- requests are refused while the backend is down, never silently
admitted. Timeouts are short (0.5 s) so an outage cannot stall the API. The Redis URL or password never appear in an error.
No `X-Forwarded-For` or other proxy header is used anywhere (decision 3): the key is the authenticated `client_id` only.
"""
from __future__ import annotations

import math

from src.auth.rate_limit import RateLimitPolicy
from src.exceptions import RateLimitBackendUnavailableError, RateLimitExceededError

KEY_PREFIX = "napsedge:ratelimit:"
SOCKET_TIMEOUT_SECONDS = 0.5

#: KEYS[1] the client's window key; ARGV[1] max requests; ARGV[2] window length in ms.
#: Returns {admitted (1/0), remaining window ms}.
_CHECK_AND_CONSUME = """
local count = tonumber(redis.call('GET', KEYS[1]) or '0')
local max = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
if count >= max then
  local ttl = redis.call('PTTL', KEYS[1])
  if ttl < 0 then
    redis.call('PEXPIRE', KEYS[1], window)
    ttl = window
  end
  return {0, ttl}
end
local new = redis.call('INCR', KEYS[1])
local ttl = redis.call('PTTL', KEYS[1])
if new == 1 or ttl < 0 then
  redis.call('PEXPIRE', KEYS[1], window)
  ttl = window
end
return {1, ttl}
"""


class RedisRateLimiter:
    """Shared fixed-window limiter. `client` is a `redis.Redis` (built by `connect`, or injected by tests)."""

    def __init__(self, client):
        self._client = client
        self._script = client.register_script(_CHECK_AND_CONSUME)

    @classmethod
    def connect(cls, url: str, *, password: str | None = None) -> "RedisRateLimiter":
        import redis

        return cls(redis.Redis.from_url(url, password=password, socket_timeout=SOCKET_TIMEOUT_SECONDS,
                                        socket_connect_timeout=SOCKET_TIMEOUT_SECONDS, retry_on_timeout=False))

    def check_and_consume(self, client_id: str, policy: RateLimitPolicy) -> None:
        import redis

        window_ms = max(1, math.ceil(policy.window_seconds * 1000))
        try:
            admitted, ttl_ms = self._script(keys=[KEY_PREFIX + client_id], args=[policy.max_requests, window_ms])
        except redis.RedisError as exc:
            raise RateLimitBackendUnavailableError(f"the shared rate-limit backend is unavailable ({type(exc).__name__})") from None
        if int(admitted) != 1:
            raise RateLimitExceededError("rate limit exceeded", client_id=client_id,
                                         retry_after_seconds=max(1, math.ceil(int(ttl_ms) / 1000)))


__all__ = ["RedisRateLimiter", "KEY_PREFIX"]
