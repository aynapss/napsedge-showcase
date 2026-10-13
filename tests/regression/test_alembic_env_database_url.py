"""
Phase 9B-3: `alembic/env.py` must accept a raw `NAPSEDGE_DATABASE_URL` that
contains percent-encoding.

The defect (found in Phase 9B-2): `env.py` handed the resolved URL to
`Config.set_main_option`, which runs values through ConfigParser interpolation
where `%` is syntax, so a valid URL with a percent-encoded password character
or certificate path failed with `ValueError: invalid interpolation syntax`
before any connection was attempted.

These tests run the REAL `alembic/env.py` through `alembic.command` with the
URL in the environment exactly as an operator would supply it. They never use
the test-only `escape_for_alembic` helper (which exists for programmatic
`Config` callers and is independently necessary there). No database and no
network are used: the online path is stopped at `engine_from_config`, where the
exact configuration the driver would receive is captured; the offline path runs
to completion.

All secrets are synthetic and generated per test. Assertion helpers below never
include a URL or password in a failure message.
"""
from __future__ import annotations

import io
import secrets
import shutil
import traceback
from pathlib import Path
from urllib.parse import quote

import pytest
import sqlalchemy
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_VAR = "NAPSEDGE_DATABASE_URL"


class _Reached(Exception):
    """Raised by the capturing stand-in for `engine_from_config` to stop after env.py resolved its configuration."""


@pytest.fixture()
def captured_engine_configuration(monkeypatch):
    """Replaces `sqlalchemy.engine_from_config` (looked up by env.py each time it is executed) with a recorder."""
    seen: list[dict] = []

    def recorder(configuration, prefix="sqlalchemy.", **kwargs):
        seen.append(dict(configuration))
        raise _Reached

    monkeypatch.setattr(sqlalchemy, "engine_from_config", recorder)
    return seen


def _config(script_location: Path | None = None, url: str | None = None) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(script_location or REPO_ROOT / "alembic"))
    if url is not None:
        cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _run_online(cfg: Config) -> None:
    with pytest.raises(_Reached):
        command.upgrade(cfg, "head")


def _same(actual: str | None, expected: str, label: str) -> None:
    """Equality without echoing either value (they can carry credentials)."""
    if actual != expected:
        diverge = next((i for i, (a, b) in enumerate(zip(actual or "", expected)) if a != b), min(len(actual or ""), len(expected)))
        pytest.fail(f"{label}: value differs from the original (lengths {len(actual or '')} vs {len(expected)}, first difference at index {diverge})")


# ---------------------------------------------------------------- synthetic URLs
def _cases() -> dict[str, tuple[str, dict]]:
    """label -> (raw URL exactly as an operator would type it, expectations decoded by SQLAlchemy)."""
    token = secrets.token_hex(6)
    space_path = "/tmp/my certs/ca.crt"
    win_space_path = "C:/Users/someone/my certs/ca.crt"
    return {
        "no_percent_escapes": (
            f"postgresql+psycopg://svc:{token}@localhost:5432/napsedge?sslmode=verify-full&sslrootcert=/etc/ssl/ca.crt",
            {"password": token, "sslrootcert": "/etc/ssl/ca.crt"},
        ),
        "encoded_at_and_percent_in_password": (
            f"postgresql+psycopg://svc:p%40ss%25{token}@localhost:5432/napsedge?sslmode=verify-full&sslrootcert=/etc/ssl/ca.crt",
            {"password": f"p@ss%{token}", "sslrootcert": "/etc/ssl/ca.crt"},
        ),
        "encoded_certificate_path_with_space": (
            f"postgresql+psycopg://svc:{token}@localhost:5432/napsedge?sslmode=verify-full&sslrootcert={quote(space_path, safe='/')}",
            {"password": token, "sslrootcert": space_path},
        ),
        "encoded_windows_certificate_path_with_space": (
            f"postgresql+psycopg://svc:{token}@localhost:5432/napsedge?sslmode=verify-full&sslrootcert={quote(win_space_path, safe='/')}",
            {"password": token, "sslrootcert": win_space_path},
        ),
        "everything_at_once": (
            f"postgresql+psycopg://svc:p%40ss%25%2F{token}@localhost:5432/napsedge?sslmode=verify-full&hostaddr=127.0.0.1"
            f"&sslrootcert={quote(win_space_path, safe='/')}",
            {"password": f"p@ss%/{token}", "sslrootcert": win_space_path, "hostaddr": "127.0.0.1"},
        ),
        "literal_double_percent_in_password": (  # a password that itself contains the two characters %% (encoded as %25%25)
            f"postgresql+psycopg://svc:a%25%25{token}@localhost:5432/napsedge?sslmode=disable",
            {"password": f"a%%{token}"},
        ),
    }


_CASE_LABELS = sorted(_cases())


# ---------------------------------------------------------------- the defect, online path
@pytest.mark.parametrize("label", _CASE_LABELS)
def test_driver_receives_the_raw_environment_url_exactly(monkeypatch, captured_engine_configuration, label):
    raw, expected = _cases()[label]
    monkeypatch.setenv(ENV_VAR, raw)
    cfg = _config()

    _run_online(cfg)

    assert len(captured_engine_configuration) == 1
    received = captured_engine_configuration[0]["sqlalchemy.url"]
    _same(received, raw, f"{label}: URL handed to the engine factory")
    parsed = make_url(received)
    assert (parsed.host, parsed.port, parsed.database, parsed.username) == ("localhost", 5432, "napsedge", "svc")
    _same(parsed.password, expected["password"], f"{label}: decoded password")
    for key in ("sslrootcert", "hostaddr"):
        if key in expected:
            _same(parsed.query.get(key), expected[key], f"{label}: decoded {key}")
    # retrieving the configuration returns the original text exactly (the escape is invisible to readers)
    _same(cfg.get_main_option("sqlalchemy.url"), raw, f"{label}: Config.get_main_option")


@pytest.mark.parametrize("label", _CASE_LABELS)
def test_offline_mode_resolves_the_same_raw_environment_url(monkeypatch, label):
    raw, _ = _cases()[label]
    monkeypatch.setenv(ENV_VAR, raw)
    cfg = _config()
    cfg.output_buffer = io.StringIO()

    command.upgrade(cfg, "head", sql=True)  # offline: renders SQL, never connects

    assert "CREATE TABLE" in cfg.output_buffer.getvalue()
    _same(cfg.get_main_option("sqlalchemy.url"), raw, f"{label}: Config.get_main_option after an offline run")


@pytest.mark.parametrize("label", ["encoded_at_and_percent_in_password", "everything_at_once", "literal_double_percent_in_password"])
def test_repeated_executions_never_accumulate_escapes(monkeypatch, captured_engine_configuration, label):
    raw, _ = _cases()[label]
    monkeypatch.setenv(ENV_VAR, raw)
    cfg = _config()  # the SAME Config object is reused, as a long-lived caller (or the test fixtures) would

    for round_number in range(4):
        _run_online(cfg)
        _same(captured_engine_configuration[round_number]["sqlalchemy.url"], raw, f"{label}: engine URL on run {round_number + 1}")
        _same(cfg.get_main_option("sqlalchemy.url"), raw, f"{label}: stored URL after run {round_number + 1}")
    # a second, fresh Config in the same process gives the same answer
    _run_online(_config())
    _same(captured_engine_configuration[-1]["sqlalchemy.url"], raw, f"{label}: fresh Config")


# ---------------------------------------------------------------- unchanged behaviour
def test_environment_url_still_overrides_a_programmatic_config_url(monkeypatch, captured_engine_configuration):
    raw, _ = _cases()["encoded_at_and_percent_in_password"]
    monkeypatch.setenv(ENV_VAR, raw)

    _run_online(_config(url="sqlite+pysqlite:///programmatic.db"))

    _same(captured_engine_configuration[0]["sqlalchemy.url"], raw, "precedence: environment over programmatic")


def test_programmatic_url_is_kept_unchanged_when_the_environment_variable_is_absent(monkeypatch, captured_engine_configuration, tmp_path):
    """The caller-side contract is untouched: a caller who escapes `%` as `%%` (as the existing fixtures must) gets the original URL back, decoded exactly once."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    original = f"sqlite+pysqlite:///{(tmp_path / 'programmatic.db').as_posix()}?note=a%40b"
    cfg = _config(url=original.replace("%", "%%"))

    _run_online(cfg)

    _same(captured_engine_configuration[0]["sqlalchemy.url"], original, "programmatic URL")


def test_programmatic_percent_free_url_is_unchanged_when_the_environment_variable_is_absent(monkeypatch, captured_engine_configuration, tmp_path):
    monkeypatch.delenv(ENV_VAR, raising=False)
    original = f"sqlite+pysqlite:///{(tmp_path / 'programmatic.db').as_posix()}"

    _run_online(_config(url=original))

    _same(captured_engine_configuration[0]["sqlalchemy.url"], original, "programmatic URL")


def test_development_sqlite_fallback_still_applies_and_survives_a_percent_in_its_path(monkeypatch, captured_engine_configuration, tmp_path):
    """No environment variable and no programmatic URL: env.py falls back to `get_database_url()`'s sqlite path. A `%` in that path is the same boundary."""
    import src.db.config as database_config

    fallback = tmp_path / "pct%dir" / "napsedge_dev.db"
    monkeypatch.setattr(database_config, "_DEV_SQLITE_PATH", fallback)
    monkeypatch.delenv(ENV_VAR, raising=False)
    cfg = _config()
    assert not cfg.get_main_option("sqlalchemy.url"), "alembic.ini must leave the URL unset for this test to mean anything"

    _run_online(cfg)

    _same(captured_engine_configuration[0]["sqlalchemy.url"], f"sqlite+pysqlite:///{fallback.as_posix()}", "fallback URL")


# ---------------------------------------------------------------- sensitivity: the regression fails without the fix
def test_without_the_escape_the_original_interpolation_error_returns(monkeypatch, captured_engine_configuration, tmp_path):
    """
    Permanent proof that the tests above are sensitive to the fix: a copy of the
    Alembic directory whose env.py has the escape removed fails with the original
    ConfigParser interpolation error, before the engine is ever reached. If
    env.py is refactored so the marker below no longer exists, this test fails
    loudly rather than silently proving nothing.
    """
    marker = '.replace("%", "%%")'
    source = (REPO_ROOT / "alembic" / "env.py").read_bytes().decode("utf-8")
    assert source.count(marker) == 1, "env.py no longer contains the single expected escape; update this control to match"
    mutated_dir = tmp_path / "alembic_without_escape"
    shutil.copytree(REPO_ROOT / "alembic", mutated_dir, ignore=shutil.ignore_patterns("__pycache__"))
    (mutated_dir / "env.py").write_bytes(source.replace(marker, "").encode("utf-8"))

    raw, _ = _cases()["encoded_at_and_percent_in_password"]
    monkeypatch.setenv(ENV_VAR, raw)
    with pytest.raises(ValueError, match="interpolation") as excinfo:
        command.upgrade(_config(script_location=mutated_dir), "head")

    assert captured_engine_configuration == [], "the engine must not have been reached"
    assert any(frame.name == "<module>" and "env.py" in frame.filename for frame in traceback.extract_tb(excinfo.tb)), "the error must originate in env.py"

    # and the unmodified env.py succeeds on the identical input (so the failure is caused by the missing escape alone)
    _run_online(_config())
