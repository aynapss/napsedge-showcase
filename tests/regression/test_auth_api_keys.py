"""
Phase 6, Slice 3B: unit tests for `src.auth.api_keys` -- the
framework-independent Bearer API-key verifier. No FastAPI/Starlette/
Pydantic/database import is exercised anywhere in this file; every test
constructs an `ApiKeyAuthenticator` directly with an injected digest
mapping (never touching a real environment variable) except where a test
is specifically about environment-variable loading.
"""
from __future__ import annotations

import hashlib
import inspect
import json

import pytest

from src.auth import api_keys as api_keys_module
from src.auth.api_keys import ApiKeyAuthenticator, AuthenticatedClient
from src.exceptions import AuthenticationConfigurationError, AuthenticationError


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


TOKEN_A = "token-for-client-a-" + "x" * 20
TOKEN_B = "token-for-client-b-" + "y" * 20


def test_valid_token_returns_the_correct_named_client():
    auth = ApiKeyAuthenticator({"discord-test": _digest(TOKEN_A)})
    result = auth.verify(TOKEN_A)
    assert result == AuthenticatedClient(client_id="discord-test")


def test_multiple_configured_clients_each_work():
    auth = ApiKeyAuthenticator({"discord-test": _digest(TOKEN_A), "streamlit-test": _digest(TOKEN_B)})
    assert auth.verify(TOKEN_A) == AuthenticatedClient(client_id="discord-test")
    assert auth.verify(TOKEN_B) == AuthenticatedClient(client_id="streamlit-test")


def test_invalid_token_fails():
    auth = ApiKeyAuthenticator({"discord-test": _digest(TOKEN_A)})
    with pytest.raises(AuthenticationError):
        auth.verify("not-the-right-token")


def test_missing_token_fails():
    auth = ApiKeyAuthenticator({"discord-test": _digest(TOKEN_A)})
    with pytest.raises(AuthenticationError):
        auth.verify("")


def test_malformed_configuration_json_fails_closed():
    with pytest.raises(AuthenticationConfigurationError):
        _authenticator_from_raw("{not valid json")


def _authenticator_from_raw(raw: str) -> ApiKeyAuthenticator:
    """Exercises the real env-var loading path without touching the real environment."""
    digests = api_keys_module._load_client_digests(raw)
    return ApiKeyAuthenticator(digests)


def test_empty_configuration_string_fails_closed():
    with pytest.raises(AuthenticationConfigurationError):
        _authenticator_from_raw("")


def test_none_configuration_fails_closed():
    with pytest.raises(AuthenticationConfigurationError):
        api_keys_module._load_client_digests(None)


def test_empty_json_object_configuration_fails_closed():
    with pytest.raises(AuthenticationConfigurationError):
        _authenticator_from_raw("{}")


def test_non_object_json_configuration_fails_closed():
    with pytest.raises(AuthenticationConfigurationError):
        _authenticator_from_raw(json.dumps(["not", "an", "object"]))


def test_invalid_digest_length_fails_closed():
    with pytest.raises(AuthenticationConfigurationError):
        _authenticator_from_raw(json.dumps({"discord-test": "too-short"}))


def test_invalid_hexadecimal_digest_fails_closed():
    not_hex = "g" * 64  # right length, not valid hex
    with pytest.raises(AuthenticationConfigurationError):
        _authenticator_from_raw(json.dumps({"discord-test": not_hex}))


def test_duplicate_client_identifier_in_raw_json_is_rejected():
    """Two identical top-level JSON keys -- json.loads would normally silently keep only the last; this must fail closed instead."""
    raw = '{"discord-test": "%s", "discord-test": "%s"}' % (_digest(TOKEN_A), _digest(TOKEN_B))
    with pytest.raises(AuthenticationConfigurationError):
        _authenticator_from_raw(raw)


def test_empty_client_identifier_is_rejected():
    with pytest.raises(AuthenticationConfigurationError):
        _authenticator_from_raw(json.dumps({"": _digest(TOKEN_A)}))


def test_duplicate_digest_across_different_clients_is_rejected():
    """Two different client_ids must not share one digest -- each client should be independently revocable."""
    shared_digest = _digest(TOKEN_A)
    with pytest.raises(AuthenticationConfigurationError):
        _authenticator_from_raw(json.dumps({"discord-test": shared_digest, "streamlit-test": shared_digest}))


def test_verification_uses_secrets_compare_digest_not_ordinary_equality(monkeypatch):
    calls = []
    import secrets as secrets_module

    real_compare_digest = secrets_module.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real_compare_digest(a, b)

    monkeypatch.setattr(api_keys_module.secrets, "compare_digest", spy)
    auth = ApiKeyAuthenticator({"discord-test": _digest(TOKEN_A)})
    auth.verify(TOKEN_A)
    assert len(calls) >= 1, "verify() must call secrets.compare_digest at least once"


def test_source_does_not_use_plain_equality_for_digest_comparison():
    """Static guard: the module must call secrets.compare_digest, never '==' on a digest/token."""
    source = inspect.getsource(api_keys_module)
    assert "secrets.compare_digest" in source


def test_raw_token_never_appears_in_a_raised_exception_message():
    secret_token = "super-secret-token-should-never-leak"
    auth = ApiKeyAuthenticator({"discord-test": _digest(TOKEN_A)})
    with pytest.raises(AuthenticationError) as exc_info:
        auth.verify(secret_token)
    assert secret_token not in str(exc_info.value)


def test_configuration_error_never_exposes_the_raw_configuration_contents():
    raw = json.dumps({"discord-test": "not-a-real-digest-value"})
    with pytest.raises(AuthenticationConfigurationError) as exc_info:
        _authenticator_from_raw(raw)
    assert "not-a-real-digest-value" not in str(exc_info.value)


def test_authentication_imports_no_fastapi_starlette_pydantic_or_database_module():
    """AST-level check (not a substring scan) so mentioning these names in prose/docstrings is not a false positive."""
    import ast

    tree = ast.parse(inspect.getsource(api_keys_module))
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)

    forbidden_prefixes = ("fastapi", "starlette", "pydantic", "sqlalchemy", "src.db")
    for name in imported_names:
        assert not name.startswith(forbidden_prefixes), f"unexpected import {name!r} in src/auth/api_keys.py"


def test_repeated_authentication_is_deterministic():
    auth = ApiKeyAuthenticator({"discord-test": _digest(TOKEN_A)})
    results = [auth.verify(TOKEN_A) for _ in range(5)]
    assert all(r == AuthenticatedClient(client_id="discord-test") for r in results)


def test_authenticated_client_is_frozen_and_equality_comparable():
    a = AuthenticatedClient(client_id="x")
    b = AuthenticatedClient(client_id="x")
    assert a == b
    with pytest.raises(Exception):
        a.client_id = "y"  # type: ignore[misc]


def test_constructing_authenticator_from_real_environment_variable(monkeypatch):
    """The one test that actually exercises os.environ.get -- via monkeypatch, never the real ambient environment."""
    monkeypatch.setenv(api_keys_module.ENV_VAR, json.dumps({"discord-test": _digest(TOKEN_A)}))
    auth = ApiKeyAuthenticator()
    assert auth.verify(TOKEN_A) == AuthenticatedClient(client_id="discord-test")


def test_constructing_authenticator_with_unset_environment_variable_fails_closed(monkeypatch):
    monkeypatch.delenv(api_keys_module.ENV_VAR, raising=False)
    with pytest.raises(AuthenticationConfigurationError):
        ApiKeyAuthenticator()
