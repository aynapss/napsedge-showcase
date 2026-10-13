"""
Process-specific startup configuration validation (Phase 9A).

Each process's own container entrypoint (`src.deployment.entrypoints`)
calls exactly one of the functions below once, before binding a port or
contacting an external service -- never at import time, never during
test collection, never inside `src.api.main.create_app()`, and never
while AST-enumerating Discord commands. None of these functions is
called from any existing module; adding this file changes no existing
behavior.

This module owns no new bounds or format rules of its own. Wherever an
existing authority already validates a variable --
`ApiKeyAuthenticator` for `NAPSEDGE_API_KEY_HASHES_JSON`,
`load_rate_limit_policy_from_env` for the rate-limit pair,
`load_rankings_runtime_config_from_env` for the Rankings pair -- this
module calls that exact authority and surfaces its own exception,
never re-implementing the check. `DISCORD_BOT_TOKEN` has no existing
authority object (`src.discord_bot.bot.main` performs an inline
presence check); this module mirrors that exact same check rather than
introducing a second, possibly-divergent one.

Every error names only the offending variable(s) -- the underlying
authorities already never include a variable's own value in their
exception messages, and this module never catches and re-wraps with
additional detail that could reintroduce one.

API and Streamlit never read `DISCORD_BOT_TOKEN`, and Discord never
reads `NAPSEDGE_API_KEY_HASHES_JSON` -- confirmed by this module's own
three separate functions each importing and calling only the
authorities its own process actually needs.
"""
from __future__ import annotations

import os

from src.auth.api_keys import ApiKeyAuthenticator
from src.auth.rate_limit import load_rate_limit_policy_from_env
from src.auth.rate_limit_backend import build_rate_limiter
from src.db.config import DATABASE_PASSWORD_FILE_ENV_VAR, get_pool_settings
from src.exceptions import (
    AuthenticationConfigurationError,
    DatabasePoolConfigurationError,
    RankingsConfigurationError,
    RateLimitConfigurationError,
    SecretFileConfigurationError,
)
from src.rankings.config import load_rankings_runtime_config_from_env
from src.secret_files import resolve_secret

#: No existing authority object owns this check (unlike the three
#: imported above) -- mirrors `src.discord_bot.bot.main`'s own inline
#: `os.environ.get("DISCORD_BOT_TOKEN", "").strip()` check exactly, so
#: there is only ever one place that decides what counts as "missing."
DISCORD_BOT_TOKEN_ENV_VAR = "DISCORD_BOT_TOKEN"


class DeploymentConfigurationError(Exception):
    """
    Raised by a process entrypoint before it binds a port or connects
    externally. Always names the offending variable(s); never a value.
    Deliberately not a `src.exceptions.NapsEdgeError` subclass -- this
    exception is raised and handled before any ASGI application exists,
    so it has no HTTP mapping to inherit from `src.api.errors`.
    """


def validate_api_environment() -> None:
    """
    Validates everything the 9 accepted API routes need to be fully
    functional: Bearer-authentication digest configuration and the
    Rankings season (plus the rate-limit pair, only if explicitly set
    to a malformed value -- both have safe defaults when absent, so
    this never makes them newly required). `/v1/health`/`/v1/ready`
    themselves need neither and are unaffected, since `create_app()`
    never calls this function.

    Collects every failing check before raising once, so an operator
    sees all missing/malformed variables together rather than fixing
    them one at a time across repeated restarts.
    """
    errors: list[str] = []
    try:
        ApiKeyAuthenticator()
    except AuthenticationConfigurationError as exc:
        errors.append(str(exc))
    try:
        load_rankings_runtime_config_from_env()
    except RankingsConfigurationError as exc:
        errors.append(str(exc))
    try:
        load_rate_limit_policy_from_env()
    except RateLimitConfigurationError as exc:
        errors.append(str(exc))
    try:
        build_rate_limiter()  # Phase 9D: backend selection and Redis URL/password-file validation (no connection)
    except RateLimitConfigurationError as exc:
        errors.append(str(exc))
    try:
        get_pool_settings()  # Phase 9B-8A: only a MALFORMED cap fails; absent means the documented default
    except DatabasePoolConfigurationError as exc:
        errors.append(str(exc))
    _check_database_password_file(errors)
    _check_log_format(errors)
    if errors:
        raise DeploymentConfigurationError("; ".join(errors))


def validate_streamlit_environment() -> None:
    """
    Validates the Rankings season the same way `validate_api_environment`
    does. Streamlit never performs Bearer authentication and never rate
    limits, so `NAPSEDGE_API_KEY_HASHES_JSON` and the rate-limit pair are
    never checked here -- confirmed by this function's own single import.
    """
    errors: list[str] = []
    try:
        load_rankings_runtime_config_from_env()
    except RankingsConfigurationError as exc:
        errors.append(str(exc))
    try:
        get_pool_settings()  # Phase 9B-8A: only a MALFORMED cap fails; absent means the documented default
    except DatabasePoolConfigurationError as exc:
        errors.append(str(exc))
    _check_database_password_file(errors)
    _check_log_format(errors)
    if errors:
        raise DeploymentConfigurationError("; ".join(errors))


def validate_discord_environment() -> None:
    """
    Validates only `DISCORD_BOT_TOKEN` -- the one variable every one of
    the 15 existing commands' own startup depends on. The 3 Phase 7
    commands that additionally call `src.services.*` directly (and
    therefore depend on `NAPSEDGE_DATABASE_URL`/`NAPSEDGE_NET_RANKINGS_SEASON`)
    are deliberately *not* required here: unlike the API/Streamlit
    surfaces, where Rankings is one of a small, fixed accepted feature
    set, the Discord bot's dominant command set (12 of 15) has no
    dependency on it at all, so failing the entire bot's startup over a
    Rankings-specific variable would be disproportionate -- a missing
    value still fails closed, per-command, at call time, exactly as it
    already does today.
    """
    errors: list[str] = []
    try:
        token = (resolve_secret(DISCORD_BOT_TOKEN_ENV_VAR) or "").strip()  # Phase 9 decision 9: the _FILE form or the variable
        if not token:
            errors.append(f"{DISCORD_BOT_TOKEN_ENV_VAR} is not set.")
    except SecretFileConfigurationError as exc:
        errors.append(str(exc))
    _check_database_password_file(errors)
    _check_log_format(errors)
    try:
        get_pool_settings()  # Phase 9B-8A: a malformed cap would otherwise fail only at the first database command
    except DatabasePoolConfigurationError as exc:
        errors.append(str(exc))
    if errors:
        raise DeploymentConfigurationError(" ".join(errors) if len(errors) == 1 else "; ".join(errors))


def _check_database_password_file(errors: list[str]) -> None:
    """Phase 9 decision 9: when NAPSEDGE_DATABASE_PASSWORD_FILE is set, fail at startup (naming the variable only) if it cannot be
    used -- rather than at the first database call. Absent means the plain NAPSEDGE_DATABASE_URL (or the dev fallback) as before."""
    if not (os.environ.get(DATABASE_PASSWORD_FILE_ENV_VAR) or "").strip():
        return
    from src.db.config import get_database_url

    try:
        get_database_url()
    except SecretFileConfigurationError as exc:
        errors.append(str(exc))


def _check_log_format(errors: list[str]) -> None:
    """Phase 9E-1: NAPSEDGE_LOG_FORMAT, when set, must be `text` or `json` (fail at startup rather than silently use text)."""
    from src.logging_config import LOG_FORMAT_ENV_VAR, LOG_FORMATS

    value = (os.environ.get(LOG_FORMAT_ENV_VAR) or "").strip().lower()
    if value and value not in LOG_FORMATS:
        errors.append(f"{LOG_FORMAT_ENV_VAR} must be one of {', '.join(LOG_FORMATS)}")
