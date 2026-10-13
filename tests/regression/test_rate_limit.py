"""
Phase 6D: framework-independent tests for `src.auth.rate_limit` -- no
FastAPI/Starlette/database import anywhere in this module, exactly
mirroring `src.auth.rate_limit`'s own framework-independence.

Every test that cares about window timing injects a `FakeClock` rather
than sleeping on the real wall clock -- deterministic, and fast. The one
exception is the concurrency test, which uses real `threading.Thread`s
(the only way to genuinely exercise `threading.Lock` contention) but
still injects a fake, manually-advanced clock so the *window* never
expires mid-test regardless of real wall-clock timing.
"""
from __future__ import annotations

import ast
import inspect
import threading

import pytest

import src.auth.rate_limit as rate_limit_module
from src.exceptions import RateLimitConfigurationError, RateLimitExceededError
from src.auth.rate_limit import (
    DEFAULT_MAX_REQUESTS,
    DEFAULT_WINDOW_SECONDS,
    MAX_ALLOWED_REQUESTS,
    MAX_ALLOWED_WINDOW_SECONDS,
    REQUESTS_ENV_VAR,
    WINDOW_SECONDS_ENV_VAR,
    RateLimiter,
    RateLimitPolicy,
    load_rate_limit_policy_from_env,
)


class FakeClock:
    """A manually-advanced monotonic clock -- never the real wall clock."""

    def __init__(self, start: float = 1_000.0):
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


# ---------------------------------------------------------------------------
# Allow / reject sequencing
# ---------------------------------------------------------------------------


def test_requests_below_the_limit_are_allowed():
    limiter = RateLimiter(clock=FakeClock())
    policy = RateLimitPolicy(max_requests=5, window_seconds=60)
    for _ in range(4):
        limiter.check_and_consume("client-a", policy)  # must not raise


def test_exact_final_allowed_request_succeeds():
    limiter = RateLimiter(clock=FakeClock())
    policy = RateLimitPolicy(max_requests=3, window_seconds=60)
    for _ in range(3):
        limiter.check_and_consume("client-a", policy)  # requests 1, 2, 3 -- all allowed


def test_first_request_over_the_limit_is_rejected():
    limiter = RateLimiter(clock=FakeClock())
    policy = RateLimitPolicy(max_requests=3, window_seconds=60)
    for _ in range(3):
        limiter.check_and_consume("client-a", policy)
    with pytest.raises(RateLimitExceededError):
        limiter.check_and_consume("client-a", policy)


def test_repeated_requests_over_the_limit_are_each_rejected():
    limiter = RateLimiter(clock=FakeClock())
    policy = RateLimitPolicy(max_requests=2, window_seconds=60)
    limiter.check_and_consume("client-a", policy)
    limiter.check_and_consume("client-a", policy)
    for _ in range(5):
        with pytest.raises(RateLimitExceededError):
            limiter.check_and_consume("client-a", policy)


def test_rejected_requests_do_not_increment_the_stored_count():
    """A 429 must not silently push the window further past the configured allowance."""
    limiter = RateLimiter(clock=FakeClock())
    policy = RateLimitPolicy(max_requests=2, window_seconds=60)
    limiter.check_and_consume("client-a", policy)
    limiter.check_and_consume("client-a", policy)
    for _ in range(10):
        with pytest.raises(RateLimitExceededError):
            limiter.check_and_consume("client-a", policy)
    assert limiter._windows["client-a"].count == 2


# ---------------------------------------------------------------------------
# Independent client buckets
# ---------------------------------------------------------------------------


def test_separate_client_ids_have_independent_buckets():
    limiter = RateLimiter(clock=FakeClock())
    policy = RateLimitPolicy(max_requests=1, window_seconds=60)
    limiter.check_and_consume("client-a", policy)
    limiter.check_and_consume("client-b", policy)  # must not raise -- independent bucket
    with pytest.raises(RateLimitExceededError):
        limiter.check_and_consume("client-a", policy)
    with pytest.raises(RateLimitExceededError):
        limiter.check_and_consume("client-b", policy)


# ---------------------------------------------------------------------------
# Exact window-boundary semantics
# ---------------------------------------------------------------------------


def test_window_resets_once_the_configured_duration_has_fully_elapsed():
    clock = FakeClock()
    limiter = RateLimiter(clock=clock)
    policy = RateLimitPolicy(max_requests=1, window_seconds=10)
    limiter.check_and_consume("client-a", policy)
    with pytest.raises(RateLimitExceededError):
        limiter.check_and_consume("client-a", policy)

    clock.advance(10.0)  # exactly at the boundary -- must belong to the NEW window
    limiter.check_and_consume("client-a", policy)  # must not raise


def test_window_does_not_reset_a_moment_before_the_boundary():
    clock = FakeClock()
    limiter = RateLimiter(clock=clock)
    policy = RateLimitPolicy(max_requests=1, window_seconds=10)
    limiter.check_and_consume("client-a", policy)

    clock.advance(9.999)  # strictly before the boundary -- still the OLD, exhausted window
    with pytest.raises(RateLimitExceededError):
        limiter.check_and_consume("client-a", policy)


def test_new_window_after_reset_starts_a_fresh_count_of_one():
    clock = FakeClock()
    limiter = RateLimiter(clock=clock)
    policy = RateLimitPolicy(max_requests=2, window_seconds=10)
    limiter.check_and_consume("client-a", policy)
    limiter.check_and_consume("client-a", policy)

    clock.advance(10.0)
    limiter.check_and_consume("client-a", policy)  # 1st request of the new window
    limiter.check_and_consume("client-a", policy)  # 2nd request of the new window
    with pytest.raises(RateLimitExceededError):
        limiter.check_and_consume("client-a", policy)  # 3rd request of the new window -- rejected


# ---------------------------------------------------------------------------
# Retry-After calculation
# ---------------------------------------------------------------------------


def test_retry_after_reflects_the_remaining_window_duration():
    clock = FakeClock()
    limiter = RateLimiter(clock=clock)
    policy = RateLimitPolicy(max_requests=1, window_seconds=10)
    limiter.check_and_consume("client-a", policy)

    clock.advance(4.0)  # 6 seconds remain in the window
    with pytest.raises(RateLimitExceededError) as excinfo:
        limiter.check_and_consume("client-a", policy)
    assert excinfo.value.retry_after_seconds == 6


def test_retry_after_rounds_a_subsecond_remainder_up_to_one():
    clock = FakeClock()
    limiter = RateLimiter(clock=clock)
    policy = RateLimitPolicy(max_requests=1, window_seconds=10)
    limiter.check_and_consume("client-a", policy)

    clock.advance(9.7)  # 0.3 seconds remain -- must never render as "Retry-After: 0"
    with pytest.raises(RateLimitExceededError) as excinfo:
        limiter.check_and_consume("client-a", policy)
    assert excinfo.value.retry_after_seconds == 1


def test_retry_after_is_never_less_than_one_second():
    clock = FakeClock()
    limiter = RateLimiter(clock=clock)
    policy = RateLimitPolicy(max_requests=1, window_seconds=10)
    limiter.check_and_consume("client-a", policy)

    clock.advance(9.999999)
    with pytest.raises(RateLimitExceededError) as excinfo:
        limiter.check_and_consume("client-a", policy)
    assert excinfo.value.retry_after_seconds >= 1


def test_retry_after_exception_carries_the_client_id_for_server_side_logging_only():
    limiter = RateLimiter(clock=FakeClock())
    policy = RateLimitPolicy(max_requests=1, window_seconds=60)
    limiter.check_and_consume("a-specific-client-id", policy)
    with pytest.raises(RateLimitExceededError) as excinfo:
        limiter.check_and_consume("a-specific-client-id", policy)
    assert excinfo.value.client_id == "a-specific-client-id"


# ---------------------------------------------------------------------------
# Clock injection
# ---------------------------------------------------------------------------


def test_clock_is_injected_and_never_reads_real_wall_time():
    """Constructing a RateLimiter with a fixed clock that never advances must never move a window forward on its own."""
    clock = FakeClock(start=500.0)
    limiter = RateLimiter(clock=clock)
    policy = RateLimitPolicy(max_requests=1, window_seconds=1)
    limiter.check_and_consume("client-a", policy)
    with pytest.raises(RateLimitExceededError):
        limiter.check_and_consume("client-a", policy)  # still blocked -- the fake clock never moved


def test_default_clock_is_time_monotonic():
    import time

    limiter = RateLimiter()
    assert limiter._clock is time.monotonic


# ---------------------------------------------------------------------------
# Policy validation -- malformed/zero/negative/excessive configuration
# ---------------------------------------------------------------------------


def test_zero_max_requests_is_rejected():
    with pytest.raises(RateLimitConfigurationError):
        RateLimitPolicy(max_requests=0, window_seconds=60)


def test_negative_max_requests_is_rejected():
    with pytest.raises(RateLimitConfigurationError):
        RateLimitPolicy(max_requests=-5, window_seconds=60)


def test_excessive_max_requests_is_rejected():
    with pytest.raises(RateLimitConfigurationError):
        RateLimitPolicy(max_requests=MAX_ALLOWED_REQUESTS + 1, window_seconds=60)


def test_max_requests_at_the_upper_bound_is_accepted():
    RateLimitPolicy(max_requests=MAX_ALLOWED_REQUESTS, window_seconds=60)  # must not raise


def test_zero_window_seconds_is_rejected():
    with pytest.raises(RateLimitConfigurationError):
        RateLimitPolicy(max_requests=60, window_seconds=0)


def test_negative_window_seconds_is_rejected():
    with pytest.raises(RateLimitConfigurationError):
        RateLimitPolicy(max_requests=60, window_seconds=-1)


def test_excessive_window_seconds_is_rejected():
    with pytest.raises(RateLimitConfigurationError):
        RateLimitPolicy(max_requests=60, window_seconds=MAX_ALLOWED_WINDOW_SECONDS + 1)


def test_window_seconds_at_the_upper_bound_is_accepted():
    RateLimitPolicy(max_requests=60, window_seconds=MAX_ALLOWED_WINDOW_SECONDS)  # must not raise


def test_boolean_max_requests_is_rejected():
    """bool is an int subclass in Python -- must not silently pass the positive-integer check."""
    with pytest.raises(RateLimitConfigurationError):
        RateLimitPolicy(max_requests=True, window_seconds=60)


def test_non_integer_max_requests_is_rejected():
    with pytest.raises(RateLimitConfigurationError):
        RateLimitPolicy(max_requests=1.5, window_seconds=60)


# ---------------------------------------------------------------------------
# load_rate_limit_policy_from_env -- absent/default/override/malformed
# ---------------------------------------------------------------------------


def test_absent_environment_variables_fall_back_to_documented_defaults():
    policy = load_rate_limit_policy_from_env(env={})
    assert policy.max_requests == DEFAULT_MAX_REQUESTS
    assert policy.window_seconds == DEFAULT_WINDOW_SECONDS


def test_explicit_env_mapping_overrides_without_touching_real_process_environment(monkeypatch):
    monkeypatch.delenv(REQUESTS_ENV_VAR, raising=False)
    monkeypatch.delenv(WINDOW_SECONDS_ENV_VAR, raising=False)
    policy = load_rate_limit_policy_from_env(env={REQUESTS_ENV_VAR: "10", WINDOW_SECONDS_ENV_VAR: "30"})
    assert policy.max_requests == 10
    assert policy.window_seconds == 30.0
    import os

    assert REQUESTS_ENV_VAR not in os.environ


def test_malformed_requests_env_var_raises_configuration_error():
    with pytest.raises(RateLimitConfigurationError):
        load_rate_limit_policy_from_env(env={REQUESTS_ENV_VAR: "not-a-number"})


def test_malformed_window_env_var_raises_configuration_error():
    with pytest.raises(RateLimitConfigurationError):
        load_rate_limit_policy_from_env(env={WINDOW_SECONDS_ENV_VAR: "not-a-number"})


def test_out_of_range_env_var_raises_configuration_error_not_silent_default():
    """A bad value must never be silently replaced by the default -- that would invisibly change the effective policy."""
    with pytest.raises(RateLimitConfigurationError):
        load_rate_limit_policy_from_env(env={REQUESTS_ENV_VAR: "0"})


def test_blank_env_var_falls_back_to_default_like_an_absent_one():
    policy = load_rate_limit_policy_from_env(env={REQUESTS_ENV_VAR: "   "})
    assert policy.max_requests == DEFAULT_MAX_REQUESTS


# ---------------------------------------------------------------------------
# Application-instance isolation
# ---------------------------------------------------------------------------


def test_two_rate_limiter_instances_never_share_state():
    clock = FakeClock()
    limiter_a = RateLimiter(clock=clock)
    limiter_b = RateLimiter(clock=clock)
    policy = RateLimitPolicy(max_requests=1, window_seconds=60)

    limiter_a.check_and_consume("client-a", policy)
    limiter_b.check_and_consume("client-a", policy)  # must not raise -- independent instance state


def test_repeated_construction_is_deterministic():
    policy = RateLimitPolicy(max_requests=2, window_seconds=60)

    def _run() -> list[bool]:
        clock = FakeClock()
        limiter = RateLimiter(clock=clock)
        outcomes = []
        for _ in range(4):
            try:
                limiter.check_and_consume("client-a", policy)
                outcomes.append(True)
            except RateLimitExceededError:
                outcomes.append(False)
        return outcomes

    assert _run() == _run() == [True, True, False, False]


def test_constructing_a_rate_limiter_performs_no_environment_read():
    """Policy is supplied per-call, never read at construction time -- static guard on __init__'s own source."""
    import inspect

    source = inspect.getsource(RateLimiter.__init__)
    assert "os.environ" not in source
    assert "os.getenv" not in source


# ---------------------------------------------------------------------------
# Bounded client-state retention
# ---------------------------------------------------------------------------


def test_expired_client_windows_are_evicted_from_memory():
    clock = FakeClock()
    limiter = RateLimiter(clock=clock)
    policy = RateLimitPolicy(max_requests=5, window_seconds=10)

    for i in range(50):
        limiter.check_and_consume(f"stale-client-{i}", policy)
    assert len(limiter._windows) == 50

    clock.advance(10.0)  # every prior window has now expired
    limiter.check_and_consume("fresh-client", policy)  # triggers opportunistic eviction
    assert len(limiter._windows) == 1
    assert "fresh-client" in limiter._windows


def test_a_client_with_no_recent_requests_leaves_no_permanent_trace():
    clock = FakeClock()
    limiter = RateLimiter(clock=clock)
    policy = RateLimitPolicy(max_requests=1, window_seconds=5)

    limiter.check_and_consume("one-time-client", policy)
    assert "one-time-client" in limiter._windows

    clock.advance(5.0)
    limiter.check_and_consume("another-client", policy)
    assert "one-time-client" not in limiter._windows


def test_stored_state_holds_only_client_id_window_start_and_count():
    """No token, digest, credential, or extra metadata is ever retained -- only what fixed-window accounting requires."""
    limiter = RateLimiter(clock=FakeClock())
    policy = RateLimitPolicy(max_requests=5, window_seconds=60)
    limiter.check_and_consume("client-a", policy)

    window = limiter._windows["client-a"]
    stored_fields = set(vars(window).keys())
    assert stored_fields == {"window_start", "count"}


# ---------------------------------------------------------------------------
# Thread safety / atomic accounting
# ---------------------------------------------------------------------------


def test_concurrent_requests_cannot_exceed_the_configured_allowance():
    """
    A `threading.Barrier` forces every worker thread to release at
    (as near as the OS scheduler allows) the same instant, maximizing
    contention on `check_and_consume`'s internal lock -- a much tighter
    race window than simply starting threads in a loop, so this test
    would be far more likely to catch a non-atomic check-then-increment
    bug if one were ever introduced.
    """
    clock = FakeClock()  # fixed -- the window never expires mid-test regardless of real wall-clock timing
    limiter = RateLimiter(clock=clock)
    policy = RateLimitPolicy(max_requests=50, window_seconds=3600)

    thread_count = 200
    barrier = threading.Barrier(thread_count)
    allowed = []
    rejected = []
    lock = threading.Lock()

    def _worker():
        barrier.wait()  # release every thread at once
        try:
            limiter.check_and_consume("shared-client", policy)
            with lock:
                allowed.append(1)
        except RateLimitExceededError:
            with lock:
                rejected.append(1)

    threads = [threading.Thread(target=_worker) for _ in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(allowed) == 50
    assert len(rejected) == 150
    assert limiter._windows["shared-client"].count == 50


# ---------------------------------------------------------------------------
# Framework-independence / no-network static guards
# ---------------------------------------------------------------------------


def _imported_module_names(module) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(a.name for a in node.names)
    return names


def test_rate_limit_module_imports_no_web_framework_or_database_library():
    """AST-level check: no FastAPI/Starlette/Pydantic/SQLAlchemy import anywhere in src/auth/rate_limit.py."""
    imported = _imported_module_names(rate_limit_module)
    forbidden = ("fastapi", "starlette", "pydantic", "sqlalchemy")
    for name in imported:
        assert not any(f in name for f in forbidden), f"unexpected import {name!r} in src/auth/rate_limit.py"


def test_rate_limit_module_imports_no_network_capable_library():
    """AST-level check: no requests/httpx/urllib/socket/selenium import anywhere in src/auth/rate_limit.py."""
    imported = _imported_module_names(rate_limit_module)
    forbidden = ("requests", "httpx", "urllib", "socket", "selenium")
    for name in imported:
        assert not any(f in name for f in forbidden), f"unexpected network-capable import {name!r} in src/auth/rate_limit.py"


def test_rate_limit_module_source_contains_no_network_or_database_call():
    """Direct source-substring guard, mirroring src.rankings.cache_repository's own precedent -- belt-and-suspenders against a call reached via an indirect import alias."""
    source = inspect.getsource(rate_limit_module)
    forbidden = ["requests.get(", "requests.post(", "httpx.", "urlopen(", "socket.socket(", "session.execute", "select(", "SELECT "]
    for token in forbidden:
        assert token not in source, f"unexpected {token!r} found in src/auth/rate_limit.py"


def test_concurrent_requests_across_independent_clients_stay_within_each_own_allowance():
    """Same barrier-synchronized contention proof, but for two independent client_ids racing simultaneously -- neither bucket may leak into the other's count."""
    clock = FakeClock()
    limiter = RateLimiter(clock=clock)
    policy = RateLimitPolicy(max_requests=20, window_seconds=3600)

    thread_count = 100  # 50 threads per client
    barrier = threading.Barrier(thread_count)
    results: dict[str, list[int]] = {"client-a": [], "client-b": []}
    lock = threading.Lock()

    def _worker(client_id: str):
        barrier.wait()
        try:
            limiter.check_and_consume(client_id, policy)
            with lock:
                results[client_id].append(1)
        except RateLimitExceededError:
            pass

    threads = [threading.Thread(target=_worker, args=(("client-a", "client-b")[i % 2],)) for i in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results["client-a"]) == 20
    assert len(results["client-b"]) == 20
