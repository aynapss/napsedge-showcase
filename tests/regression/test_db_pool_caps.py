"""
Phase 9B-8A: the database connection caps (`src.db.config.get_pool_settings`, `src.db.session.build_engine`), their
startup validation in each process, the API's mapping of pool exhaustion, and the deployment files that carry the
variables. No database is contacted: SQLAlchemy engines are lazy. Server-side behavior is covered by
`tests/postgres/test_postgres_pool_caps.py`.
"""
from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.exc import TimeoutError as SqlAlchemyPoolTimeout
from sqlalchemy.pool import QueuePool

from src.api.main import create_app
from src.db import config as db_config
from src.db.config import (
    MAX_OVERFLOW_ENV_VAR,
    POOL_BOUNDS,
    POOL_SIZE_ENV_VAR,
    POOL_TIMEOUT_ENV_VAR,
    DatabasePoolSettings,
    get_pool_settings,
)
from src.db.session import build_engine
from src.deployment.env_validation import (
    DeploymentConfigurationError,
    validate_api_environment,
    validate_discord_environment,
    validate_streamlit_environment,
)
from src.exceptions import DatabasePoolConfigurationError

POOL_VARS = (POOL_SIZE_ENV_VAR, MAX_OVERFLOW_ENV_VAR, POOL_TIMEOUT_ENV_VAR)
PG_URL = "postgresql+psycopg://napsedge_runtime_ro@db.example.invalid:5432/napsedge"
PLANTED = "9999planted"


@pytest.fixture(autouse=True)
def clean_pool_env(monkeypatch):
    for name in POOL_VARS:
        monkeypatch.delenv(name, raising=False)


def test_a_absent_or_blank_variables_mean_the_documented_defaults(monkeypatch):
    assert get_pool_settings() == DatabasePoolSettings(2, 2, 10) and get_pool_settings().max_connections == 4
    for name in POOL_VARS:
        monkeypatch.setenv(name, "   ")
    assert get_pool_settings() == DatabasePoolSettings(2, 2, 10)


@pytest.mark.parametrize("name", POOL_VARS)
def test_b_every_bound_is_inclusive(monkeypatch, name):
    low, high, _ = POOL_BOUNDS[name]
    for value in (low, high):
        monkeypatch.setenv(name, f" {value} ")
        assert getattr(get_pool_settings(), {POOL_SIZE_ENV_VAR: "pool_size", MAX_OVERFLOW_ENV_VAR: "max_overflow",
                                              POOL_TIMEOUT_ENV_VAR: "pool_timeout_seconds"}[name]) == value


MALFORMED = {
    POOL_SIZE_ENV_VAR: ["0", "11", "-1", "+3", "3.5", "1e2", "two", "²", "00002", PLANTED],
    MAX_OVERFLOW_ENV_VAR: ["11", "-1", "0x2", "١", PLANTED],
    POOL_TIMEOUT_ENV_VAR: ["0", "61", "10s", "inf", PLANTED],
}


@pytest.mark.parametrize("name, value", [(n, v) for n, values in MALFORMED.items() for v in values])
def test_c_a_malformed_variable_is_refused_by_name_never_by_value(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(DatabasePoolConfigurationError) as excinfo:
        get_pool_settings()
    low, high, _ = POOL_BOUNDS[name]
    assert str(excinfo.value) == f"{name} must be a whole number from {low} to {high}", "the exact message: the variable, never the value"


def test_d_every_malformed_variable_is_reported_together(monkeypatch):
    for name in POOL_VARS:
        monkeypatch.setenv(name, PLANTED)
    message = str(pytest.raises(DatabasePoolConfigurationError, get_pool_settings).value)
    assert all(name in message for name in POOL_VARS) and PLANTED not in message


def test_e_a_postgresql_engine_is_capped_by_the_environment_without_connecting(monkeypatch):
    monkeypatch.setenv(POOL_SIZE_ENV_VAR, "3")
    monkeypatch.setenv(MAX_OVERFLOW_ENV_VAR, "1")
    monkeypatch.setenv(POOL_TIMEOUT_ENV_VAR, "7")
    engine = build_engine(PG_URL)
    try:
        assert isinstance(engine.pool, QueuePool)
        assert (engine.pool.size(), engine.pool._max_overflow, engine.pool._timeout) == (3, 1, 7)
        assert engine.pool.checkedout() == 0, "building the engine connected nothing"
    finally:
        engine.dispose()


def test_f_defaults_apply_when_unset_and_an_explicit_setting_overrides_the_environment(monkeypatch):
    engine = build_engine(PG_URL)
    assert (engine.pool.size(), engine.pool._max_overflow, engine.pool._timeout) == (2, 2, 10)
    engine.dispose()
    monkeypatch.setenv(POOL_SIZE_ENV_VAR, PLANTED)  # would be refused if read
    engine = build_engine(PG_URL, pool=DatabasePoolSettings(1, 0, 1))
    assert (engine.pool.size(), engine.pool._max_overflow, engine.pool._timeout) == (1, 0, 1)
    engine.dispose()


def test_g_a_malformed_cap_stops_a_postgresql_engine_but_sqlite_is_untouched(monkeypatch, tmp_path):
    monkeypatch.setenv(MAX_OVERFLOW_ENV_VAR, PLANTED)
    with pytest.raises(DatabasePoolConfigurationError):
        build_engine(PG_URL)
    memory = build_engine("sqlite:///:memory:")
    file_engine = build_engine(f"sqlite:///{(tmp_path / 'x.db').as_posix()}")
    try:
        assert type(memory.pool).__name__ == "SingletonThreadPool"
        assert (file_engine.pool.size(), file_engine.pool._max_overflow) == (5, 10), "SQLAlchemy's own SQLite pooling is unchanged"
    finally:
        memory.dispose()
        file_engine.dispose()


def _valid_process_env(monkeypatch):
    monkeypatch.setenv("NAPSEDGE_API_KEY_HASHES_JSON", '{"client": "' + "a" * 64 + '"}')
    monkeypatch.setenv("NAPSEDGE_NET_RANKINGS_SEASON", "2026")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "planted-token-not-real")
    monkeypatch.delenv("NAPSEDGE_RATE_LIMIT_REQUESTS", raising=False)
    monkeypatch.delenv("NAPSEDGE_RATE_LIMIT_WINDOW_SECONDS", raising=False)


@pytest.mark.parametrize("validate", [validate_api_environment, validate_streamlit_environment, validate_discord_environment])
def test_h_each_process_validates_the_caps_at_startup(monkeypatch, validate):
    _valid_process_env(monkeypatch)
    validate()  # absent caps: the defaults, no error
    monkeypatch.setenv(POOL_TIMEOUT_ENV_VAR, PLANTED)
    with pytest.raises(DeploymentConfigurationError) as excinfo:
        validate()
    assert POOL_TIMEOUT_ENV_VAR in str(excinfo.value) and PLANTED not in str(excinfo.value)


def test_i_discord_reports_a_missing_token_and_a_malformed_cap_together(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    with pytest.raises(DeploymentConfigurationError) as excinfo:
        validate_discord_environment()
    assert str(excinfo.value) == "DISCORD_BOT_TOKEN is not set.", "the single-error message is unchanged"
    monkeypatch.setenv(POOL_SIZE_ENV_VAR, PLANTED)
    message = str(pytest.raises(DeploymentConfigurationError, validate_discord_environment).value)
    assert "DISCORD_BOT_TOKEN" in message and POOL_SIZE_ENV_VAR in message and PLANTED not in message


@pytest.mark.parametrize("raised", [SqlAlchemyPoolTimeout("QueuePool limit of size 2 overflow 2 reached " + PLANTED),
                                    DatabasePoolConfigurationError(f"{POOL_SIZE_ENV_VAR} must be ... {PLANTED}")])
def test_j_pool_exhaustion_and_pool_misconfiguration_are_a_sanitized_503(tmp_path, raised):
    app = create_app(database_url=f"sqlite:///{(tmp_path / 'api.db').as_posix()}")

    @app.get("/v1/_pool_probe")
    def _probe():
        raise raised

    response = TestClient(app, raise_server_exceptions=False).get("/v1/_pool_probe")
    assert response.status_code == 503
    assert response.json() == {"error": "service_unavailable", "message": "a required external dependency is unavailable"}
    assert PLANTED not in response.text and "QueuePool" not in response.text


def test_k_compose_passes_the_caps_to_exactly_the_three_database_processes():
    with open("compose.yaml", encoding="utf-8") as handle:
        services = yaml.safe_load(handle)["services"]
    for service in ("api", "streamlit", "discord"):
        for name in POOL_VARS:
            assert services[service]["environment"][name] == "${" + name + ":-}", (service, name)
    assert not any(name in services["local-db"].get("environment", {}) for name in POOL_VARS)


def test_l_the_env_example_documents_the_caps_with_their_defaults_and_budget():
    with open(".env.example", encoding="utf-8") as handle:
        text = handle.read()
    for name in POOL_VARS:
        assert f"\n{name}={POOL_BOUNDS[name][2]}\n" in text, name
    assert "22 connections" in text and "17 of 22" in text


def test_m_the_budget_arithmetic_holds_for_the_defaults():
    per_process = DatabasePoolSettings().max_connections
    backup_job, operator, plan = 4, 1, 22
    assert 3 * per_process + backup_job + operator == 17 <= plan
    assert db_config.POOL_BOUNDS[POOL_SIZE_ENV_VAR][1] + db_config.POOL_BOUNDS[MAX_OVERFLOW_ENV_VAR][1] == 20, "upper bounds are a hard ceiling, not a budget"
