"""
Phase 9B-8A: the connection caps observed from the SERVER. A sampler counts the engine's own backends in
`pg_stat_activity` (by a unique `application_name`) while many threads compete for connections. Disposable database,
synthetic workload; nothing here is a capacity or performance claim.
"""
from __future__ import annotations

import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import TimeoutError as SqlAlchemyPoolTimeout

from src.api.main import create_app
from src.db.config import MAX_OVERFLOW_ENV_VAR, POOL_SIZE_ENV_VAR, POOL_TIMEOUT_ENV_VAR, DatabasePoolSettings
from src.db.session import build_engine

pytestmark = pytest.mark.postgres


class Sampler:
    """Polls the server for the number of backends carrying `application_name` until stopped; keeps the maximum."""

    def __init__(self, admin_url: str, application_name: str) -> None:
        self.engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        self.application_name, self.maximum, self.samples = application_name, 0, 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def count(self) -> int:
        with self.engine.connect() as conn:
            return conn.execute(text("SELECT count(*) FROM pg_stat_activity WHERE application_name = :a"), {"a": self.application_name}).scalar()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.maximum = max(self.maximum, self.count())
            self.samples += 1
            time.sleep(0.02)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=10)

    def gone(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.count() == 0:
                return True
            time.sleep(0.05)
        return False


@pytest.fixture()
def tagged(pg_migrated_database_url, pg_container):
    application_name = f"napsedge_pool_{uuid.uuid4().hex[:10]}"
    url = f"{pg_migrated_database_url}?application_name={application_name}"
    sampler = Sampler(pg_container.get_connection_url(), application_name)
    try:
        yield url, sampler
    finally:
        sampler.engine.dispose()


def hammer(engine, workers: int, rounds: int, sleep_s: float) -> dict:
    outcomes = {"ok": 0, "timeout": 0, "other": []}
    lock = threading.Lock()

    def work():
        for _ in range(rounds):
            try:
                with engine.connect() as conn:
                    conn.execute(text("SELECT pg_sleep(:s)"), {"s": sleep_s})
                key = "ok"
            except SqlAlchemyPoolTimeout:
                key = "timeout"
            except Exception as exc:  # noqa: BLE001 -- recorded and asserted empty
                with lock:
                    outcomes["other"].append(type(exc).__name__)
                continue
            with lock:
                outcomes[key] += 1

    threads = [threading.Thread(target=work) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    return outcomes


def test_a_an_explicit_cap_is_never_exceeded_on_the_server_and_excess_waits_then_times_out(tagged):
    url, sampler = tagged
    engine = build_engine(url, pool=DatabasePoolSettings(pool_size=1, max_overflow=1, pool_timeout_seconds=1))
    try:
        with sampler:
            outcomes = hammer(engine, workers=10, rounds=3, sleep_s=0.4)
    finally:
        engine.dispose()
    assert outcomes["other"] == [], outcomes
    assert sampler.maximum == 2, f"server-side maximum {sampler.maximum} (cap 1 + 1), {sampler.samples} samples"
    assert outcomes["timeout"] >= 1 and outcomes["ok"] >= 1, outcomes
    assert sampler.gone(), "disposing the engine leaves no backend"


def test_b_the_defaults_cap_a_process_at_four_backends_and_a_burst_waits_instead_of_failing(tagged, monkeypatch):
    url, sampler = tagged
    for name in (POOL_SIZE_ENV_VAR, MAX_OVERFLOW_ENV_VAR, POOL_TIMEOUT_ENV_VAR):
        monkeypatch.delenv(name, raising=False)
    engine = build_engine(url)
    try:
        with sampler:
            outcomes = hammer(engine, workers=16, rounds=2, sleep_s=0.3)
    finally:
        engine.dispose()
    assert outcomes == {"ok": 32, "timeout": 0, "other": []}, outcomes
    assert sampler.maximum == 4, f"server-side maximum {sampler.maximum} (default 2 + 2)"
    assert sampler.gone()


def test_c_a_saturated_api_answers_not_ready_and_recovers_when_a_connection_frees(tagged, monkeypatch):
    url, sampler = tagged
    monkeypatch.setenv(POOL_SIZE_ENV_VAR, "1")
    monkeypatch.setenv(MAX_OVERFLOW_ENV_VAR, "0")
    monkeypatch.setenv(POOL_TIMEOUT_ENV_VAR, "1")
    app = create_app(database_url=url)
    client = TestClient(app, raise_server_exceptions=False)
    try:
        assert client.get("/v1/ready").status_code == 200
        held = app.state.engine.connect()  # the API process's only connection
        try:
            started = time.monotonic()
            saturated = client.get("/v1/ready")
            waited = time.monotonic() - started
            assert sampler.count() == 1, "never more than the cap, even while a request waits"
        finally:
            held.close()
        assert saturated.status_code == 503 and "QueuePool" not in saturated.text, saturated.text
        assert 0.9 <= waited < 10, f"the request waited for the pool timeout ({waited:.2f}s)"
        assert client.get("/v1/ready").status_code == 200, "recovers once the connection is free"
    finally:
        app.state.engine.dispose()
    assert sampler.gone()
