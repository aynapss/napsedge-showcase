"""
Framework-independent Bearer API-key verification (Phase 6, Slice 3B).

No FastAPI/Starlette/Pydantic/database import anywhere in this module --
`src/api/deps.py` is the only place that adapts this to HTTP. Credential
comparison always uses `secrets.compare_digest`, never ordinary string
equality, and the raw presented token is never returned, logged, or
retained beyond the single SHA-256 digest computed to compare it.

Configuration: one environment variable, `NAPSEDGE_API_KEY_HASHES_JSON`
(matching the existing `NAPSEDGE_*` prefix used by `src.db.config`/
`src.paths`), holding a JSON object of `{client_id: sha256_hex_digest}`.
Reading it happens only when an `ApiKeyAuthenticator` is actually
constructed -- never at import time -- and any malformed/missing/empty/
duplicate configuration fails closed by raising
`AuthenticationConfigurationError`, never by silently accepting nothing
or fabricating a default credential.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass

from src.exceptions import AuthenticationConfigurationError, AuthenticationError, SecretFileConfigurationError
from src.secret_files import resolve_secret

#: Existing project prefix (matches NAPSEDGE_DATABASE_URL, NAPSEDGE_ROOT_DIR, NAPSEDGE_LOG_LEVEL).
ENV_VAR = "NAPSEDGE_API_KEY_HASHES_JSON"

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class AuthenticatedClient:
    """The one piece of identity a successful verification yields -- no roles, no scopes, no expiry."""

    client_id: str


def _reject_duplicate_keys(pairs: list[tuple[object, object]]) -> dict:
    """`json.loads(..., object_pairs_hook=...)`: a plain dict silently keeps only the last of two repeated JSON keys -- this raises instead."""
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _load_client_digests(raw: str | None) -> dict[str, str]:
    if not raw or not raw.strip():
        raise AuthenticationConfigurationError(
            f"{ENV_VAR} is not set -- Bearer authentication cannot verify any credential until it is configured."
        )
    try:
        parsed = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise AuthenticationConfigurationError(f"{ENV_VAR} is not valid, duplicate-key-free JSON.") from exc

    if not isinstance(parsed, dict) or not parsed:
        raise AuthenticationConfigurationError(f"{ENV_VAR} must be a non-empty JSON object of client_id -> SHA-256 digest.")

    digests: dict[str, str] = {}
    seen_digests: set[str] = set()
    for client_id, digest in parsed.items():
        if not isinstance(client_id, str) or not client_id.strip():
            raise AuthenticationConfigurationError(f"{ENV_VAR} contains an empty or non-string client identifier.")
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest.lower()):
            raise AuthenticationConfigurationError(
                f"{ENV_VAR} contains a digest for {client_id!r} that is not a 64-character hexadecimal SHA-256 value."
            )
        normalized_digest = digest.lower()
        if normalized_digest in seen_digests:
            raise AuthenticationConfigurationError(
                f"{ENV_VAR} declares the same digest for more than one client identifier -- "
                "each client must be independently rotatable/revocable."
            )
        digests[client_id] = normalized_digest
        seen_digests.add(normalized_digest)
    return digests


class ApiKeyAuthenticator:
    """
    Verifies an opaque Bearer token against configured SHA-256 digests.

    Constructing an instance loads and validates configuration once (fail
    closed on anything missing/malformed/duplicate) -- `verify()` itself
    never re-reads configuration and never mutates it. Pass
    `client_digests` directly to inject configuration in tests without
    touching the real environment.
    """

    def __init__(self, client_digests: dict[str, str] | None = None, *, env_var: str = ENV_VAR):
        if client_digests is None:
            try:
                raw = resolve_secret(env_var)  # Phase 9 decision 9: `<env_var>_FILE` (a credential file) or the plain variable
            except SecretFileConfigurationError as exc:
                raise AuthenticationConfigurationError(str(exc)) from None
            client_digests = _load_client_digests(raw)
        self._client_digests = dict(client_digests)

    def verify(self, token: str) -> AuthenticatedClient:
        if not token:
            raise AuthenticationError("missing or invalid credential")
        presented_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for client_id, digest in self._client_digests.items():
            if secrets.compare_digest(presented_digest, digest):
                return AuthenticatedClient(client_id=client_id)
        raise AuthenticationError("missing or invalid credential")
