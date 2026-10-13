"""
Framework-independent, in-memory, single-process fixed-window rate
limiter (Phase 6D). No FastAPI/Starlette/Pydantic/database import
anywhere in this module -- `src/api/deps.py` is the only place that
adapts this to HTTP, exactly mirroring the `src.auth.api_keys` precedent.

**Internal-MVP scope only.** This limiter provides atomic, correct
accounting *within one process*. It does not, and is not claimed to,
provide multi-process or multi-host consistency: two separate
`RateLimiter` instances (e.g. two `create_app()` calls, or two worker
processes) never share state. Restarting the process clears every
bucket. See `src.api.deps.get_rate_limited_client`'s own docstring for
the full set of accepted internal-MVP limitations.

Configuration (`NAPSEDGE_RATE_LIMIT_REQUESTS`/
`NAPSEDGE_RATE_LIMIT_WINDOW_SECONDS`) is read fresh per call by
`load_rate_limit_policy_from_env` -- never at import time -- mirroring
`src.api.deps.get_rankings_context`'s own per-request configuration-read
pattern. An **absent** environment variable falls back to the documented
default (60 requests / 60 seconds); a **present but malformed or
out-of-range** value fails closed with `RateLimitConfigurationError`,
never silently falling back to the default and never silently disabling
the limiter.
"""
from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping

from src.exceptions import RateLimitConfigurationError, RateLimitExceededError

#: Existing project prefix (matches NAPSEDGE_API_KEY_HASHES_JSON, NAPSEDGE_NET_RANKINGS_SEASON, ...).
REQUESTS_ENV_VAR = "NAPSEDGE_RATE_LIMIT_REQUESTS"
WINDOW_SECONDS_ENV_VAR = "NAPSEDGE_RATE_LIMIT_WINDOW_SECONDS"

DEFAULT_MAX_REQUESTS = 60
DEFAULT_WINDOW_SECONDS = 60.0

#: Defensible upper bounds on env-var-driven configuration -- prevent an
#: absurdly large value from effectively disabling rate limiting while
#: still passing "is it a positive number" validation. Not exposed to a
#: client under any circumstance.
MAX_ALLOWED_REQUESTS = 100_000
MAX_ALLOWED_WINDOW_SECONDS = 86_400.0  # 24 hours


@dataclass(frozen=True)
class RateLimitPolicy:
    """
    Immutable, validated fixed-window rate-limit policy. Validation
    happens once, in `__post_init__`, so an invalid `RateLimitPolicy`
    can never be constructed at all -- there is no "policy object that
    might be invalid," only a successfully-constructed valid one or a
    raised `RateLimitConfigurationError`.
    """

    max_requests: int
    window_seconds: float

    def __post_init__(self) -> None:
        if isinstance(self.max_requests, bool) or not isinstance(self.max_requests, int) or self.max_requests <= 0:
            raise RateLimitConfigurationError(f"{REQUESTS_ENV_VAR} must be a positive integer.")
        if self.max_requests > MAX_ALLOWED_REQUESTS:
            raise RateLimitConfigurationError(f"{REQUESTS_ENV_VAR} exceeds the defensible upper bound of {MAX_ALLOWED_REQUESTS}.")
        if isinstance(self.window_seconds, bool) or not isinstance(self.window_seconds, (int, float)) or self.window_seconds <= 0:
            raise RateLimitConfigurationError(f"{WINDOW_SECONDS_ENV_VAR} must be a positive number.")
        if self.window_seconds > MAX_ALLOWED_WINDOW_SECONDS:
            raise RateLimitConfigurationError(
                f"{WINDOW_SECONDS_ENV_VAR} exceeds the defensible upper bound of {MAX_ALLOWED_WINDOW_SECONDS} seconds."
            )


def load_rate_limit_policy_from_env(env: Mapping[str, str] | None = None) -> RateLimitPolicy:
    """
    Reads `NAPSEDGE_RATE_LIMIT_REQUESTS`/`NAPSEDGE_RATE_LIMIT_WINDOW_SECONDS`
    from `env` (defaults to `os.environ`) -- never at import time, so a
    test can override either value per-call via the `env` parameter
    without touching the real process environment. An absent variable
    falls back to the documented default; a present-but-malformed value
    (not an integer/number, non-positive, or beyond the defensible upper
    bound) fails closed with `RateLimitConfigurationError` -- never
    silently substituting the default for a bad value, which would let a
    typo silently and invisibly change the effective policy.
    """
    source = env if env is not None else os.environ

    raw_requests = source.get(REQUESTS_ENV_VAR)
    if raw_requests is None or not raw_requests.strip():
        max_requests = DEFAULT_MAX_REQUESTS
    else:
        try:
            max_requests = int(raw_requests)
        except ValueError:
            raise RateLimitConfigurationError(f"{REQUESTS_ENV_VAR} is not a valid integer.")

    raw_window = source.get(WINDOW_SECONDS_ENV_VAR)
    if raw_window is None or not raw_window.strip():
        window_seconds: float = DEFAULT_WINDOW_SECONDS
    else:
        try:
            window_seconds = float(raw_window)
        except ValueError:
            raise RateLimitConfigurationError(f"{WINDOW_SECONDS_ENV_VAR} is not a valid number.")

    return RateLimitPolicy(max_requests=max_requests, window_seconds=window_seconds)


@dataclass
class _ClientWindow:
    window_start: float
    count: int


class RateLimiter:
    """
    Per-client fixed-window request counter. Application-scoped: each
    instance owns its own state dict and lock -- two separate
    `RateLimiter` instances (two `create_app()` calls, or a fresh
    instance built by a test) never share counters, and constructing one
    performs no environment read and no I/O of any kind (the policy is
    supplied per-call to `check_and_consume`, not stored at construction
    time -- this lets a configuration change take effect on the very
    next request without reconstructing the limiter).

    Thread-safe for the single-process model this Phase 6D slice is
    approved for: `check_and_consume` performs its check-then-increment
    sequence while holding `self._lock`, so concurrent callers cannot
    race past the same window's allowance. This does **not** provide
    multi-process or multi-host consistency -- a second worker process
    would have its own, entirely independent `RateLimiter`.

    **Exact window-boundary behavior**: a client's window is anchored at
    the timestamp of their first request in it. Any later request is
    compared against `window_start + window_seconds`: strictly before
    that instant, it counts against the same window; at or after that
    instant (`now - window_start >= window_seconds`), the window is
    treated as expired and a brand-new window starts at `now` with a
    fresh count of 1 -- the boundary instant itself always belongs to
    the *new* window, never counted against the old one's exhausted
    allowance.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._windows: dict[str, _ClientWindow] = {}

    def check_and_consume(self, client_id: str, policy: RateLimitPolicy) -> None:
        """
        Raises `RateLimitExceededError` if `client_id` has already
        consumed its full `policy.max_requests` allowance for the
        current window; otherwise records this request (creating a new
        window if none exists or the existing one has expired) and
        returns normally.

        Also opportunistically evicts every other client's already-expired
        window on each call -- a simple, sufficient bounded-retention
        strategy: at any moment, the state dict only ever holds entries
        for clients who made a request within the currently-open window,
        so memory never grows from clients who stop making requests.
        """
        now = self._clock()
        with self._lock:
            self._evict_expired_locked(now, policy.window_seconds)

            window = self._windows.get(client_id)
            if window is None:
                self._windows[client_id] = _ClientWindow(window_start=now, count=1)
                return

            if window.count >= policy.max_requests:
                remaining = policy.window_seconds - (now - window.window_start)
                retry_after_seconds = max(1, math.ceil(remaining))
                raise RateLimitExceededError(
                    "rate limit exceeded", client_id=client_id, retry_after_seconds=retry_after_seconds
                )

            window.count += 1

    def _evict_expired_locked(self, now: float, window_seconds: float) -> None:
        """Must be called while holding `self._lock`."""
        expired = [client_id for client_id, window in self._windows.items() if now - window.window_start >= window_seconds]
        for client_id in expired:
            del self._windows[client_id]
