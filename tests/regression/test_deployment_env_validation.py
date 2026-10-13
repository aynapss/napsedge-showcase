"""
Phase 9A: `src.deployment.env_validation` -- process-specific startup
configuration validation. No container, network, or Discord connection
is ever involved in this file.
"""
from __future__ import annotations

import pytest

from src.deployment.env_validation import (
    DISCORD_BOT_TOKEN_ENV_VAR,
    DeploymentConfigurationError,
    validate_api_environment,
    validate_discord_environment,
    validate_streamlit_environment,
)

_VALID_API_KEY_HASHES_JSON = (
    '{"test-client": "' + "0" * 64 + '"}'
)


def _clear_all(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "NAPSEDGE_API_KEY_HASHES_JSON",
        "NAPSEDGE_RATE_LIMIT_REQUESTS",
        "NAPSEDGE_RATE_LIMIT_WINDOW_SECONDS",
        "NAPSEDGE_NET_RANKINGS_SEASON",
        "NAPSEDGE_NET_RANKINGS_CACHE_PATH",
        DISCORD_BOT_TOKEN_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)


class TestValidateApiEnvironment:
    def test_passes_when_everything_required_is_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_all(monkeypatch)
        monkeypatch.setenv("NAPSEDGE_API_KEY_HASHES_JSON", _VALID_API_KEY_HASHES_JSON)
        monkeypatch.setenv("NAPSEDGE_NET_RANKINGS_SEASON", "2026")
        validate_api_environment()  # must not raise

    def test_fails_when_api_key_hashes_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_all(monkeypatch)
        monkeypatch.setenv("NAPSEDGE_NET_RANKINGS_SEASON", "2026")
        with pytest.raises(DeploymentConfigurationError) as exc_info:
            validate_api_environment()
        assert "NAPSEDGE_API_KEY_HASHES_JSON" in str(exc_info.value)

    def test_fails_when_api_key_hashes_blank(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_all(monkeypatch)
        monkeypatch.setenv("NAPSEDGE_API_KEY_HASHES_JSON", "   ")
        monkeypatch.setenv("NAPSEDGE_NET_RANKINGS_SEASON", "2026")
        with pytest.raises(DeploymentConfigurationError) as exc_info:
            validate_api_environment()
        assert "NAPSEDGE_API_KEY_HASHES_JSON" in str(exc_info.value)

    def test_fails_when_rankings_season_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_all(monkeypatch)
        monkeypatch.setenv("NAPSEDGE_API_KEY_HASHES_JSON", _VALID_API_KEY_HASHES_JSON)
        with pytest.raises(DeploymentConfigurationError) as exc_info:
            validate_api_environment()
        assert "NAPSEDGE_NET_RANKINGS_SEASON" in str(exc_info.value)

    def test_reports_multiple_missing_variables_together(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_all(monkeypatch)
        with pytest.raises(DeploymentConfigurationError) as exc_info:
            validate_api_environment()
        message = str(exc_info.value)
        assert "NAPSEDGE_API_KEY_HASHES_JSON" in message
        assert "NAPSEDGE_NET_RANKINGS_SEASON" in message

    def test_fails_when_rate_limit_requests_malformed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Rate-limit vars remain optional (safe default); this proves a *present but malformed* value is still caught before the port binds."""
        _clear_all(monkeypatch)
        monkeypatch.setenv("NAPSEDGE_API_KEY_HASHES_JSON", _VALID_API_KEY_HASHES_JSON)
        monkeypatch.setenv("NAPSEDGE_NET_RANKINGS_SEASON", "2026")
        monkeypatch.setenv("NAPSEDGE_RATE_LIMIT_REQUESTS", "not-a-number")
        with pytest.raises(DeploymentConfigurationError) as exc_info:
            validate_api_environment()
        assert "NAPSEDGE_RATE_LIMIT_REQUESTS" in str(exc_info.value)

    def test_rate_limit_absence_is_not_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Confirms rate-limit validation never becomes newly-required -- the established lazy/default behavior is preserved."""
        _clear_all(monkeypatch)
        monkeypatch.setenv("NAPSEDGE_API_KEY_HASHES_JSON", _VALID_API_KEY_HASHES_JSON)
        monkeypatch.setenv("NAPSEDGE_NET_RANKINGS_SEASON", "2026")
        validate_api_environment()  # must not raise despite both rate-limit vars being absent

    def test_error_never_contains_the_configured_digest_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_all(monkeypatch)
        monkeypatch.setenv("NAPSEDGE_API_KEY_HASHES_JSON", "not valid json at all")
        monkeypatch.setenv("NAPSEDGE_NET_RANKINGS_SEASON", "2026")
        with pytest.raises(DeploymentConfigurationError) as exc_info:
            validate_api_environment()
        assert "not valid json at all" not in str(exc_info.value)


class TestValidateStreamlitEnvironment:
    def test_passes_when_rankings_season_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_all(monkeypatch)
        monkeypatch.setenv("NAPSEDGE_NET_RANKINGS_SEASON", "2026")
        validate_streamlit_environment()  # must not raise

    def test_fails_when_rankings_season_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_all(monkeypatch)
        with pytest.raises(DeploymentConfigurationError) as exc_info:
            validate_streamlit_environment()
        assert "NAPSEDGE_NET_RANKINGS_SEASON" in str(exc_info.value)

    def test_never_requires_api_key_hashes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Streamlit never performs Bearer authentication -- confirms this validator never even inspects that variable."""
        _clear_all(monkeypatch)
        monkeypatch.setenv("NAPSEDGE_NET_RANKINGS_SEASON", "2026")
        # Deliberately leave NAPSEDGE_API_KEY_HASHES_JSON entirely unset.
        validate_streamlit_environment()  # must not raise

    def test_never_requires_discord_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_all(monkeypatch)
        monkeypatch.setenv("NAPSEDGE_NET_RANKINGS_SEASON", "2026")
        validate_streamlit_environment()  # must not raise with DISCORD_BOT_TOKEN unset


class TestValidateDiscordEnvironment:
    def test_passes_when_token_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_all(monkeypatch)
        monkeypatch.setenv(DISCORD_BOT_TOKEN_ENV_VAR, "synthetic-test-token")
        validate_discord_environment()  # must not raise

    def test_fails_when_token_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_all(monkeypatch)
        with pytest.raises(DeploymentConfigurationError) as exc_info:
            validate_discord_environment()
        assert DISCORD_BOT_TOKEN_ENV_VAR in str(exc_info.value)

    def test_fails_when_token_blank(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_all(monkeypatch)
        monkeypatch.setenv(DISCORD_BOT_TOKEN_ENV_VAR, "   ")
        with pytest.raises(DeploymentConfigurationError) as exc_info:
            validate_discord_environment()
        assert DISCORD_BOT_TOKEN_ENV_VAR in str(exc_info.value)

    def test_never_requires_database_url_or_rankings_season(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The 12 legacy commands have no dependency on either; failing bot startup over them would be disproportionate (see module docstring)."""
        _clear_all(monkeypatch)
        monkeypatch.setenv(DISCORD_BOT_TOKEN_ENV_VAR, "synthetic-test-token")
        validate_discord_environment()  # must not raise with both unset

    def test_error_never_contains_the_configured_token_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_all(monkeypatch)
        monkeypatch.setenv(DISCORD_BOT_TOKEN_ENV_VAR, "")
        with pytest.raises(DeploymentConfigurationError) as exc_info:
            validate_discord_environment()
        assert "synthetic" not in str(exc_info.value)
