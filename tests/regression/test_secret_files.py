"""
Phase 9 decision 9 (approved 2026-10-09): application secrets from read-only credential FILES (`src.secret_files`), used by the
API key authenticator, the Discord bot, the startup validation and the database URL. Every refusal names the variable, never the
content.
"""
from __future__ import annotations

import os
import sys

import pytest

from src.deployment.env_validation import DeploymentConfigurationError
from src.exceptions import AuthenticationConfigurationError, SecretFileConfigurationError
from src.secret_files import MAX_SECRET_FILE_BYTES, read_secret_file, resolve_secret

SECRET = "s3cr3t-Value_never-in-errors"
DIGEST = "a" * 64


def secret_file(tmp_path, content: bytes, name: str = "secret") -> str:
    path = tmp_path / name
    path.write_bytes(content)
    if sys.platform != "win32":
        os.chmod(path, 0o400)
    return str(path)


@pytest.mark.parametrize("content, expected", [(SECRET.encode(), SECRET), (SECRET.encode() + b"\n", SECRET), (SECRET.encode() + b"\r\n", SECRET),
                                               (b"two\nlines\n", "two\nlines")])
def test_a_file_is_read_with_one_trailing_newline_removed(tmp_path, content, expected):
    assert read_secret_file("X_FILE", secret_file(tmp_path, content)) == expected


@pytest.mark.parametrize("case", ["relative", "missing", "directory", "empty", "whitespace", "nul", "not_utf8", "too_large"])
def test_unusable_files_are_refused_naming_the_variable_only(tmp_path, case):
    path = {"relative": "relative/secret", "missing": str(tmp_path / "absent"), "directory": str(tmp_path)}.get(case)
    if path is None:
        content = {"empty": b"", "whitespace": b"  \n", "nul": b"ab\x00cd" + SECRET.encode(), "not_utf8": b"\xff\xfe" + SECRET.encode(),
                   "too_large": b"x" * (MAX_SECRET_FILE_BYTES + 1)}[case]
        path = secret_file(tmp_path, content)
    with pytest.raises(SecretFileConfigurationError) as exc:
        read_secret_file("NAPSEDGE_X_FILE", path)
    assert "NAPSEDGE_X_FILE" in str(exc.value) and SECRET not in str(exc.value) and "x" * 50 not in str(exc.value)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_a_group_or_world_writable_file_is_refused(tmp_path):
    path = secret_file(tmp_path, SECRET.encode())
    os.chmod(path, 0o664)
    with pytest.raises(SecretFileConfigurationError, match="writable by group or others"):
        read_secret_file("X_FILE", path)


def test_resolution_prefers_exactly_one_source(tmp_path):
    path = secret_file(tmp_path, SECRET.encode())
    assert resolve_secret("X", {"X_FILE": path}) == SECRET
    assert resolve_secret("X", {"X": "plain"}) == "plain"
    assert resolve_secret("X", {}) is None
    with pytest.raises(SecretFileConfigurationError, match="set only one of X and X_FILE"):
        resolve_secret("X", {"X": "plain", "X_FILE": path})


def test_the_api_key_hashes_can_come_from_a_file_and_file_errors_are_authentication_configuration_errors(tmp_path, monkeypatch):
    from src.auth.api_keys import ApiKeyAuthenticator

    monkeypatch.delenv("NAPSEDGE_API_KEY_HASHES_JSON", raising=False)
    monkeypatch.setenv("NAPSEDGE_API_KEY_HASHES_JSON_FILE", secret_file(tmp_path, ('{"client-a": "%s"}' % DIGEST).encode()))
    assert ApiKeyAuthenticator()._client_digests == {"client-a": DIGEST}
    monkeypatch.setenv("NAPSEDGE_API_KEY_HASHES_JSON_FILE", str(tmp_path / "absent"))
    with pytest.raises(AuthenticationConfigurationError, match="NAPSEDGE_API_KEY_HASHES_JSON_FILE cannot be opened"):
        ApiKeyAuthenticator()


def test_the_database_password_file_is_inserted_into_a_password_free_url(tmp_path, monkeypatch):
    from sqlalchemy.engine import make_url

    from src.db.config import get_database_url

    monkeypatch.setenv("NAPSEDGE_DATABASE_URL", "postgresql+psycopg://napsedge_runtime_ro@db.example:25060/napsedge?sslmode=verify-full")
    monkeypatch.setenv("NAPSEDGE_DATABASE_PASSWORD_FILE", secret_file(tmp_path, b"p@ss:w/rd%\n"))
    url = make_url(get_database_url())
    assert url.password == "p@ss:w/rd%" and url.username == "napsedge_runtime_ro" and url.host == "db.example" and url.query["sslmode"] == "verify-full"


@pytest.mark.parametrize("case", ["url_has_password", "no_url", "bad_file"])
def test_unsafe_database_secret_combinations_are_refused(tmp_path, monkeypatch, case):
    from src.db.config import get_database_url

    monkeypatch.setenv("NAPSEDGE_DATABASE_PASSWORD_FILE", secret_file(tmp_path, SECRET.encode()) if case != "bad_file" else "relative")
    if case == "url_has_password":
        monkeypatch.setenv("NAPSEDGE_DATABASE_URL", "postgresql+psycopg://u:inline@db.example/napsedge")
    elif case == "no_url":
        monkeypatch.delenv("NAPSEDGE_DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("NAPSEDGE_DATABASE_URL", "postgresql+psycopg://u@db.example/napsedge")
    with pytest.raises(SecretFileConfigurationError) as exc:
        get_database_url()
    assert SECRET not in str(exc.value) and "inline" not in str(exc.value)


def test_without_the_password_file_the_url_is_passed_through_unchanged(monkeypatch):
    from src.db.config import get_database_url

    monkeypatch.delenv("NAPSEDGE_DATABASE_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("NAPSEDGE_DATABASE_URL", "postgresql+psycopg://u:p@db.example/napsedge")
    assert get_database_url() == "postgresql+psycopg://u:p@db.example/napsedge"


def test_discord_startup_validation_accepts_the_token_file_and_refuses_an_unusable_one(tmp_path, monkeypatch):
    from src.deployment.env_validation import validate_discord_environment

    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("NAPSEDGE_DATABASE_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("DISCORD_BOT_TOKEN_FILE", secret_file(tmp_path, SECRET.encode()))
    validate_discord_environment()
    monkeypatch.setenv("DISCORD_BOT_TOKEN_FILE", str(tmp_path / "absent"))
    with pytest.raises(DeploymentConfigurationError) as exc:
        validate_discord_environment()
    assert "DISCORD_BOT_TOKEN_FILE cannot be opened" in str(exc.value) and SECRET not in str(exc.value)


def test_startup_validation_reports_an_unusable_database_password_file(tmp_path, monkeypatch):
    from src.deployment.env_validation import validate_discord_environment

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token-for-the-test")
    monkeypatch.delenv("DISCORD_BOT_TOKEN_FILE", raising=False)
    monkeypatch.setenv("NAPSEDGE_DATABASE_URL", "postgresql+psycopg://u@db.example/napsedge")
    monkeypatch.setenv("NAPSEDGE_DATABASE_PASSWORD_FILE", str(tmp_path / "absent"))
    with pytest.raises(DeploymentConfigurationError, match="NAPSEDGE_DATABASE_PASSWORD_FILE cannot be opened"):
        validate_discord_environment()
