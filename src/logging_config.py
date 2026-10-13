"""
Shared structured logging for production `src/` modules.

Notebook-facing functions keep their existing ``print()`` calls -- those are
part of the interactive notebook experience and changing them would alter
notebook-visible output, which Phase 1 must not do. This module is for new
and refactored `src/` code that needs proper log levels, e.g. Phase 2
identity resolution reporting collisions/ambiguity at WARNING.

Usage:
    from src.logging_config import get_logger
    log = get_logger(__name__)
    log.warning("ambiguous team %r resolved to %d candidates", query, len(candidates))

Phase 9E-1 (decision 8, approved 2026-10-09):
- `NAPSEDGE_LOG_FORMAT=json` writes one JSON object per line (`ts` in UTC, `level`, `logger`, `message`, `request_id` when the
  line belongs to an API request, `exception` when present) for journald on the server. The default `text` format is unchanged.
- A redaction filter on the root handler replaces known secret values with `[REDACTED]` in every message and traceback, in both
  formats: the Discord token, the password inside NAPSEDGE_DATABASE_URL, and every secret read through `src.secret_files`
  (registered with `register_secret`). Values shorter than 6 characters are never registered (too likely to collide with text).
  This is a safety net, not permission to log secrets: code must still never pass a secret to a log call.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit

_CONFIGURED = False
LOG_FORMAT_ENV_VAR = "NAPSEDGE_LOG_FORMAT"
LOG_FORMATS = ("text", "json")
TEXT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
TEXT_DATEFMT = "%Y-%m-%d %H:%M:%S"
REDACTED = "[REDACTED]"
_MIN_SECRET_LENGTH = 6

#: the API request this log line belongs to (set by `src.api.request_context`), or None outside a request
REQUEST_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("napsedge_request_id", default=None)
_REGISTERED_SECRETS: set[str] = set()


def register_secret(value: str | None) -> None:
    """Add a secret value to the redaction set (`src.secret_files` registers everything it reads)."""
    if value and len(value.strip()) >= _MIN_SECRET_LENGTH:
        _REGISTERED_SECRETS.add(value.strip())


def _environment_secrets() -> set[str]:
    found = set()
    token = (os.environ.get("DISCORD_BOT_TOKEN") or "").strip()
    if len(token) >= _MIN_SECRET_LENGTH:
        found.add(token)
    url = os.environ.get("NAPSEDGE_DATABASE_URL") or ""
    try:
        password = urlsplit(url).password if url else None
    except ValueError:
        password = None
    if password and len(password) >= _MIN_SECRET_LENGTH:
        found.add(password)
    return found


def redact(text: str) -> str:
    """`text` with every known secret value replaced by [REDACTED] (longest first)."""
    for secret in sorted(_REGISTERED_SECRETS | _environment_secrets(), key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, REDACTED)
    return text


class RedactingFilter(logging.Filter):
    """Scrubs the formatted message and any traceback of known secret values before a handler writes them."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line; never raises on odd values (they are rendered with `str`)."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {"ts": datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                 "level": record.levelname, "logger": record.name, "message": record.getMessage()}
        request_id = REQUEST_ID.get()
        if request_id:
            entry["request_id"] = request_id
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            entry["exception"] = record.exc_text
        return json.dumps(entry, ensure_ascii=True, default=str)


def log_format() -> str:
    """The configured format: `text` (default) or `json`; anything else is refused by startup validation and treated as text here."""
    value = (os.environ.get(LOG_FORMAT_ENV_VAR) or "text").strip().lower()
    return value if value in LOG_FORMATS else "text"


def build_handler(stream=None) -> logging.Handler:
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(JsonFormatter() if log_format() == "json" else logging.Formatter(TEXT_FORMAT, datefmt=TEXT_DATEFMT))
    handler.addFilter(RedactingFilter())
    return handler


def _configure_root() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    level_name = os.environ.get("NAPSEDGE_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(level=level, handlers=[build_handler()])
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a module-level logger, configuring the root handler on first use."""
    _configure_root()
    return logging.getLogger(name)
