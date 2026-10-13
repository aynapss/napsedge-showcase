"""
Phase 9D acceptance (readiness section 15: "two concurrently running API workers correctly share one rate-limit budget per
client_id"), against a REAL, disposable Redis. Marker `redis` (deselected by default). Redis comes from NAPSEDGE_TEST_REDIS_URL if
set, otherwise a throwaway `redis:7-alpine` container is started on a free loopback port and removed afterwards.
"""
from __future__ import annotations

import multiprocessing
import os
import socket
import subprocess
import time
import uuid

import pytest

redis = pytest.importorskip("redis")

from src.auth.rate_limit import RateLimitPolicy  # noqa: E402
from src.auth.rate_limit_redis import RedisRateLimiter  # noqa: E402
from src.exceptions import RateLimitBackendUnavailableError, RateLimitExceededError  # noqa: E402

pytestmark = pytest.mark.redis
LABEL = "org.napsedge.unit=9d"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def redis_url():
    if os.environ.get("NAPSEDGE_TEST_REDIS_URL"):
        yield os.environ["NAPSEDGE_TEST_REDIS_URL"]
        return
    port, name = _free_port(), f"napsedge-9d-redis-{uuid.uuid4().hex[:8]}"
    started = subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--label", LABEL, "-p", f"127.0.0.1:{port}:6379", "redis:7-alpine"],
                             capture_output=True, text=True)
    if started.returncode != 0:
        pytest.skip(f"Docker could not start Redis ({started.stderr.strip()[:120]})")
    try:
        url = f"redis://127.0.0.1:{port}/0"
        client = redis.Redis.from_url(url, socket_timeout=1)
        for _ in range(60):
            try:
                if client.ping():
                    break
            except redis.RedisError:
                time.sleep(0.5)
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def client_id() -> str:
    return f"client-{uuid.uuid4().hex[:8]}"


def test_admits_up_to_the_limit_then_rejects_without_counting(redis_url):
    limiter, who, policy = RedisRateLimiter.connect(redis_url), client_id(), RateLimitPolicy(max_requests=3, window_seconds=60)
    for _ in range(3):
        limiter.check_and_consume(who, policy)
    for _ in range(3):
        with pytest.raises(RateLimitExceededError) as exc:
            limiter.check_and_consume(who, policy)
        assert 1 <= exc.value.retry_after_seconds <= 60
    count = redis.Redis.from_url(redis_url).get(f"napsedge:ratelimit:{who}")
    assert int(count) == 3, "rejected requests are not counted"


def test_two_limiters_like_two_api_workers_share_one_budget(redis_url):
    worker_a, worker_b = RedisRateLimiter.connect(redis_url), RedisRateLimiter.connect(redis_url)
    who, policy = client_id(), RateLimitPolicy(max_requests=4, window_seconds=60)
    admitted = 0
    for i in range(10):
        try:
            (worker_a if i % 2 == 0 else worker_b).check_and_consume(who, policy)
            admitted += 1
        except RateLimitExceededError:
            pass
    assert admitted == 4


def _hammer(url: str, who: str, attempts: int, results) -> None:
    limiter, policy, admitted = RedisRateLimiter.connect(url), RateLimitPolicy(max_requests=25, window_seconds=60), 0
    for _ in range(attempts):
        try:
            limiter.check_and_consume(who, policy)
            admitted += 1
        except RateLimitExceededError:
            pass
    results.put(admitted)


def test_separate_processes_racing_concurrently_never_exceed_the_shared_budget(redis_url):
    who, results = client_id(), multiprocessing.get_context("spawn").Queue()
    processes = [multiprocessing.get_context("spawn").Process(target=_hammer, args=(redis_url, who, 20, results)) for _ in range(4)]
    for p in processes:
        p.start()
    for p in processes:
        p.join(60)
    totals = [results.get(timeout=10) for _ in processes]
    assert sum(totals) == 25, totals  # 80 attempts across 4 processes, exactly the 25 allowed


def test_the_window_expires_and_a_new_one_starts(redis_url):
    limiter, who, policy = RedisRateLimiter.connect(redis_url), client_id(), RateLimitPolicy(max_requests=1, window_seconds=1)
    limiter.check_and_consume(who, policy)
    with pytest.raises(RateLimitExceededError):
        limiter.check_and_consume(who, policy)
    time.sleep(1.2)
    limiter.check_and_consume(who, policy)


def test_clients_have_independent_budgets(redis_url):
    limiter, policy = RedisRateLimiter.connect(redis_url), RateLimitPolicy(max_requests=1, window_seconds=60)
    limiter.check_and_consume(client_id(), policy)
    limiter.check_and_consume(client_id(), policy)


def test_an_unreachable_backend_fails_closed_quickly_and_names_no_url():
    url = f"redis://127.0.0.1:{_free_port()}/0"  # nothing listens here
    limiter = RedisRateLimiter.connect(url)
    started = time.monotonic()
    with pytest.raises(RateLimitBackendUnavailableError) as exc:
        limiter.check_and_consume(client_id(), RateLimitPolicy(max_requests=5, window_seconds=60))
    assert time.monotonic() - started < 3 and "127.0.0.1" not in str(exc.value) and "redis://" not in str(exc.value)
