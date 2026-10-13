"""
Application secrets from FILES (Phase 9 decision 9, approved by Anthony on 2026-10-09).

On the server, each secret is a systemd encrypted credential that is mounted READ-ONLY into its container as a file, so it never
appears in the process environment, `docker inspect`, a compose file or a log. A process reads it through a `<NAME>_FILE`
variable holding the file's absolute path. The plain `<NAME>` variable keeps working for local development and tests.

Supported here:
  NAPSEDGE_API_KEY_HASHES_JSON_FILE   (instead of NAPSEDGE_API_KEY_HASHES_JSON)   -- the API
  DISCORD_BOT_TOKEN_FILE              (instead of DISCORD_BOT_TOKEN)              -- the Discord bot
  NAPSEDGE_DATABASE_PASSWORD_FILE     (with a NAPSEDGE_DATABASE_URL that carries NO password) -- every database process
    (handled by `src.db.config.get_database_url`)

File rules (the same as the backup job's password file): an absolute path; a regular file (symbolic links are followed: credential
mounts use them); on POSIX not writable by group or others; at most 64 KiB; UTF-8 without NUL; one trailing newline (LF or CRLF)
removed; non-empty. Setting both `<NAME>` and `<NAME>_FILE` is refused (exactly one source). Every refusal names the VARIABLE only,
never the path's content.
"""
from __future__ import annotations

import os
import stat
import sys

from src.exceptions import SecretFileConfigurationError
from src.logging_config import register_secret

MAX_SECRET_FILE_BYTES = 64 * 1024


def read_secret_file(variable: str, path: str) -> str:
    """The content of one secret file under the module's rules (`variable` names the setting in any error)."""
    if not os.path.isabs(path):
        raise SecretFileConfigurationError(f"{variable} must be an absolute path")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise SecretFileConfigurationError(f"{variable} cannot be opened ({type(exc).__name__})") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SecretFileConfigurationError(f"{variable} is not a regular file")
        if sys.platform != "win32" and info.st_mode & 0o022:
            raise SecretFileConfigurationError(f"{variable} is writable by group or others")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, MAX_SECRET_FILE_BYTES + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_SECRET_FILE_BYTES:
                raise SecretFileConfigurationError(f"{variable} is larger than {MAX_SECRET_FILE_BYTES} bytes")
    except OSError as exc:
        raise SecretFileConfigurationError(f"{variable} cannot be read ({type(exc).__name__})") from None
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    if b"\x00" in raw:
        raise SecretFileConfigurationError(f"{variable} contains a NUL byte")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SecretFileConfigurationError(f"{variable} is not UTF-8") from None
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    if not text.strip():
        raise SecretFileConfigurationError(f"{variable} is empty")
    register_secret(text)  # Phase 9E-1: never written to a log line, even by mistake
    return text


def resolve_secret(name: str, environ: dict | None = None) -> str | None:
    """`<name>` from its file (`<name>_FILE`) or from the plain variable; None when neither is set. Both set is refused."""
    env = os.environ if environ is None else environ
    file_variable = f"{name}_FILE"
    path = (env.get(file_variable) or "").strip()
    value = env.get(name)
    if path and value and value.strip():
        raise SecretFileConfigurationError(f"set only one of {name} and {file_variable}")
    if path:
        return read_secret_file(file_variable, path)
    return value


__all__ = ["MAX_SECRET_FILE_BYTES", "read_secret_file", "resolve_secret"]
