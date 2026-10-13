"""
Shared typed exceptions for `src/` production modules.

These are additive: existing exceptions (``src.scraper.SportsReferenceRateLimited``,
``src.slug_utils.TeamLookupError``/``SourceSlugError``) are untouched and keep
working exactly as before. New code should raise from this module instead of
bare ``Exception``/``ValueError`` where one of these categories fits.

Note on identity resolution specifically: an *unknown or ambiguous* team/player
is an expected outcome, not an error -- callers get a structured
``UnresolvedIdentity`` result (see ``src.identity.contracts``) so they can
decide how to handle it. ``IdentityResolutionError`` here is reserved for
programmer error or a corrupted registry, not "didn't find a match".
"""
from __future__ import annotations


class NapsEdgeError(Exception):
    """Base class for all NapsEdge-specific exceptions."""


class ConfigurationError(NapsEdgeError):
    """Required configuration (env var, path, API key) is missing or invalid."""


class IngestionError(NapsEdgeError):
    """A scrape/ingest step failed in a way that isn't already a more specific error."""


class IdentityResolutionError(NapsEdgeError):
    """
    The identity-resolution subsystem itself is broken (e.g. registry never
    built, corrupted alias table) -- not raised for a normal "no match found".
    """


class ValidationError(NapsEdgeError):
    """Input or intermediate data failed a required shape/type/range check."""


class SourceUnavailableError(NapsEdgeError):
    """
    An external data source (Sports-Reference, Covers, ESPN, odds API, NET
    rankings) could not be reached and no cached fallback was available.
    """


class IdentityConflictError(NapsEdgeError):
    """
    A repository upsert (Phase 5A, `src.db.repositories`) was given the
    same canonical ID as an existing row but a different value for one of
    that row's *immutable* identity fields (e.g. a team's `sr_slug`, a
    game's structural team/season/date/neutral fields, a sportsbook's
    `canonical_key`). Raised instead of silently overwriting -- an
    immutable-field mismatch means either caller error or a genuine,
    surfaced identity collision, never a routine update.
    """


class DatabaseTargetVerificationError(NapsEdgeError):
    """
    Controlled bulk-import design, Slice 4
    (`src.pipeline.bulk_import_safety`): a caller-supplied database
    connection failed the mandatory "prove you know what you're pointing
    at" check before any bulk-import work may proceed -- a non-PostgreSQL
    connection, a closed/invalid connection, a connected database whose
    real name does not match the caller's own declared target, or an
    administrative database (`postgres`/`template0`/`template1`). Never
    raised for a routine "no match found" identity-resolution outcome --
    this is specifically about *which database a connection is pointed
    at*, not application data.
    """


class AdvisoryLockUnavailableError(NapsEdgeError):
    """
    Controlled bulk-import design, Slice 4
    (`src.pipeline.bulk_import_safety`): a PostgreSQL session-level
    advisory lock could not be acquired immediately (`pg_try_advisory_lock`
    returned `false` -- another connection already holds it), or an
    internal consistency error was detected releasing a lock this
    session's own code believed it held. Never retried automatically --
    per this batch's own instruction, a caller that wants a retry loop
    must build one explicitly outside this module.
    """


class AuthenticationError(NapsEdgeError):
    """
    Phase 6, Slice 3B (`src.auth.api_keys`): a presented Bearer credential
    was missing, malformed, or did not match any configured client digest.
    Every raise site uses the same fixed, generic message -- never the
    raw presented token, never which configured client (if any) it came
    close to matching -- so a client cannot distinguish "no such client"
    from "wrong token" from "no credential at all".
    """


class AuthenticationConfigurationError(ConfigurationError):
    """
    Phase 6, Slice 3B (`src.auth.api_keys`): the server's own Bearer
    authentication configuration (`NAPSEDGE_API_KEY_HASHES_JSON`) is
    unset, not valid JSON, or contains no usable client/digest pairs --
    distinct from `AuthenticationError` (a *client's* credential was
    invalid): this means the server cannot authenticate anyone at all
    until reconfigured. A subclass of `ConfigurationError` semantically,
    but mapped to its own HTTP 503 (temporarily unavailable) rather than
    the generic `ConfigurationError`'s 500, since callers should treat
    this as retryable once the server is reconfigured. Never exposes the
    contents of the malformed configuration value.
    """


class RankingsCacheIntegrityError(NapsEdgeError):
    """
    Phase 6, Slice 5A (`src.services.rankings`): the local NET-rankings
    cache contains two or more rows that are exact identical duplicates
    for the same resolved canonical team (same team_id, same rank) with
    no existing, documented deduplication rule to safely collapse them.
    Distinct from a genuine rank conflict (the same team appearing twice
    with *different* ranks -- e.g. the real cached fixture's own
    `"louisiana-state"` rows, ranks 77 and 245 -- which is reported as a
    typed `outcome="conflicting"` result, not raised as an exception,
    since that condition is fully and truthfully representable without
    guessing). This exception exists only for the narrower case an
    existing exception cannot represent: an unresolvable identical-row
    cache-integrity defect.
    """


class RankingsConfigurationError(ConfigurationError):
    """
    Phase 6, Slice 5B (`src.api.deps`): the server's own Rankings
    configuration (`NAPSEDGE_NET_RANKINGS_SEASON`, and/or an explicitly
    overridden `NAPSEDGE_NET_RANKINGS_CACHE_PATH`) is unset or malformed
    -- e.g. the season is missing or not a positive integer. A subclass
    of `ConfigurationError` semantically, but mapped to its own HTTP 503
    (temporarily unavailable) rather than the generic `ConfigurationError`'s
    500, mirroring `AuthenticationConfigurationError`'s own precedent.
    Never exposes the configured cache path or the malformed value itself.
    """


class InjuryDataIntegrityError(NapsEdgeError):
    """
    Phase 6, Slice 6B (`src.services.injury`): the local Injury cache
    authority (`src.features.team_summary.build_injury_report`) hit a
    collection element that is not itself a dict -- a genuine, pre-existing
    data-quality gap in the underlying local JSON cache, confirmed and
    deliberately preserved (not fixed) during Slice 6A's extraction. The
    authority itself represents this as an uncaught `AttributeError`
    rather than a typed result; this exception exists ONLY to translate
    that into an HTTP-safe outcome at the narrow API-capable service call
    site (`src.services.injury.get_injury_report_for_team`) -- the
    extracted authority and the plain `get_team_injury_report`/
    `build_injury_report`/`_injury_report` functions are never modified
    and continue to raise the original `AttributeError` directly for any
    caller outside this one translation boundary. Mapped to a sanitized
    HTTP 503 -- never exposes the malformed element, the cache path, or
    the original exception text.
    """


class SecretFileConfigurationError(ConfigurationError):
    """
    Phase 9 decision 9 (`src.secret_files`): a secret configured through a
    `<NAME>_FILE` variable (or `NAPSEDGE_DATABASE_PASSWORD_FILE`) cannot be
    used -- relative path, missing or unreadable file, not a regular file,
    writable by group or others, too large, not UTF-8, empty, or both the
    plain variable and its `_FILE` form set. Names the variable only,
    never the file's content.
    """


class RateLimitConfigurationError(ConfigurationError):
    """
    Phase 6D (`src.auth.rate_limit`): the server's own rate-limit
    configuration (`NAPSEDGE_RATE_LIMIT_REQUESTS`/
    `NAPSEDGE_RATE_LIMIT_WINDOW_SECONDS`) is present but malformed -- not
    a positive integer/number, or outside the defensible upper bound
    (`MAX_ALLOWED_REQUESTS`/`MAX_ALLOWED_WINDOW_SECONDS`). A subclass of
    `ConfigurationError` semantically, but mapped to its own HTTP 503
    (temporarily unavailable) rather than the generic `ConfigurationError`'s
    500, mirroring `AuthenticationConfigurationError`/
    `RankingsConfigurationError`'s own precedent -- never a 429, since a
    misconfigured limiter is a server-side problem, not a client having
    exceeded a real limit. Never exposes the malformed configuration value
    itself. Deliberately never raised for an *absent* environment
    variable -- an absent value falls back to the documented default
    (60 requests / 60 seconds) instead, so rate limiting can never be
    silently disabled by omission.
    """


class RateLimitBackendUnavailableError(RateLimitConfigurationError):
    """
    Phase 9D (decision 2, fail closed): the shared rate-limit backend
    (Redis) did not answer or returned an error. Requests are refused with
    the same sanitized 503 as a misconfigured limiter -- never silently
    admitted. Names the error type only, never the URL or a password.
    """


class DatabasePoolConfigurationError(ConfigurationError):
    """
    Phase 9B-8A (`src.db.config.get_pool_settings`): a connection-cap
    variable (`NAPSEDGE_DB_POOL_SIZE`/`NAPSEDGE_DB_MAX_OVERFLOW`/
    `NAPSEDGE_DB_POOL_TIMEOUT_SECONDS`) is present but malformed -- not a
    plain decimal integer, or outside its documented bounds. Mapped to its
    own HTTP 503, following `RateLimitConfigurationError`'s precedent: a
    misconfigured pool is a server-side problem. Names the variable only,
    never its value. Never raised for an *absent* variable -- an absent
    value falls back to the documented default, so the cap can never be
    silently removed by omission.
    """


class RateLimitExceededError(NapsEdgeError):
    """
    Phase 6D (`src.auth.rate_limit.RateLimiter`): a client has already
    consumed its full fixed-window request allowance. Carries
    `retry_after_seconds` (an integer, always >= 1) so the API layer can
    set a correct `Retry-After` header, and `client_id` for server-side
    logging only -- neither the client_id, the configured threshold, nor
    any internal timestamp is ever included in the client-facing response
    body, which carries only a stable `"rate_limited"` error code and a
    generic message.
    """

    def __init__(self, message: str, *, client_id: str, retry_after_seconds: int):
        super().__init__(message)
        self.client_id = client_id
        self.retry_after_seconds = retry_after_seconds
