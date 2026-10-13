"""
Logo/asset canonical ingestion (Phase 3, batch 5, Gate 2).

ACTIVE V15 DEFINITIONS AND CALL SITES CONFIRMED

`fetch_espn_teams` (cell 84), `normalize_name` (cell 77 -- already ported
verbatim as `normalize_espn_name` in `src/identity/espn_identity.py`,
Phase 2A), `build_slug_authority` (cell 79), `build_resolved_slug_map`
(cell 80), `resolve_team_slug` (cell 82), `slug_to_filename` (cell 83),
`download_all_team_logos` (cell 85) -- all six unshadowed
(`defined_in_notebook_count == 1`), forming one linear chain:
`download_all_team_logos` calls `build_resolved_slug_map` (which calls
`build_slug_authority`), `fetch_espn_teams`, and per-team
`resolve_team_slug`/`slug_to_filename`. This is the exact cluster
`src/identity/espn_identity.py`'s own docstring already identifies and
covers at the team-identity level (`resolve_espn_team_identity`,
`resolve_espn_team_slug`) -- reused directly here, not re-implemented.

**No other active logo source was found.** The manifest rows for all six
functions above are the complete inventory for this cluster; no second
logo-fetching function, alternate CDN, or non-ESPN image source appears
anywhere in the targeted search. Logo downloading in v15 is exclusively
ESPN-backed.

CONFIRMED SOURCE-ID FINDING: `download_all_team_logos` DISCARDS THE REAL
ESPN TEAM ID AFTER USING IT

Read in full (cell 85): `team_id = match.group(1)` (extracted from the
team-list page's own `<a href="...id/{id}/...">`) is used **only** to
build the logo CDN URL (`https://a.espncdn.com/i/teamlogos/ncaa/500/{team_id}.png`)
-- it is never saved, returned, or attached to anything. The saved file
is just `{logo_key}.png` (a slug-derived filename via `TEAM_LOGO_BY_SLUG.get(slug, slug)`),
with no ESPN id anywhere in the output. This is the exact "extracted-but-
discarded source id" shape already confirmed for SR player slugs (batch
1), ESPN athlete ids (batch 2), and Covers raw player names (batch 3) --
now for ESPN numeric team ids used in logo downloads. This module's
`LogoAssetRecord.source_team_id` closes that gap additively, without
touching `download_all_team_logos` itself.

Also confirmed while reading cell 85: `unmapped.append(espn_name)` inside
`download_all_team_logos` references a bare `unmapped` name with no local
initialization in the shown cell body -- almost certainly relies on a
module-level `unmapped = []` defined elsewhere, or is a latent bug. Not
investigated further (out of this batch's ingestion-correctness scope);
noted for completeness, not fixed.

DESIGN

Mirrors the established ingestion-module pattern: team metadata retrieval
(`fetch_espn_teams`, untouched, not called here -- this module takes
already-fetched team entries as input) is separate from raw-URL evidence
capture, identity resolution (`resolve_espn_team_identity`, Phase 2A,
reused unmodified), binary retrieval (via an injected fetch function --
"no live downloads," per instruction), response validation, filename
construction (`slug_to_filename`, ported verbatim as `slug_to_filename_v15`
since no `src/` copy existed), checksum generation, and manifest
construction. Actual disk persistence is optional and off by default
(`output_dir=None` writes nothing) -- this batch produces an additive
manifest, not a Phase 5/8 storage system. No filename is ever allowed to
resolve outside the caller-supplied `output_dir`.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.identity.contracts import TeamIdentity, UnresolvedIdentity
from src.identity.espn_identity import resolve_espn_team_identity
from src.logging_config import get_logger

log = get_logger(__name__)

PARSER_VERSION = "logo_asset_ingestion.v1"
LOGO_CDN_URL_TEMPLATE = "https://a.espncdn.com/i/teamlogos/ncaa/500/{team_id}.png"


# ---------------------------------------------------------------------------
# Legacy compatibility wrapper -- byte-faithful to v15
# ---------------------------------------------------------------------------


def slug_to_filename_v15(slug: str) -> str:
    """Verbatim port of notebook cell 83's `slug_to_filename`."""
    return (
        slug.replace(" ", "_")
        .replace("(", "")
        .replace(")", "")
        .replace("'", "")
        .replace(".", "")
    )


def build_espn_logo_url_v15(espn_team_id: str) -> str:
    """Verbatim port of `download_all_team_logos`'s (cell 85) own URL construction."""
    return LOGO_CDN_URL_TEMPLATE.format(team_id=espn_team_id)


# ---------------------------------------------------------------------------
# Canonical result contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LogoFetchResponse:
    """
    Minimal duck-typed response shape this module expects from an injected
    fetch function -- deliberately small (not a real `requests.Response`)
    so tests can construct one without any HTTP library at all.
    """

    status_code: int
    content: bytes
    content_type: str | None = None


@dataclass(frozen=True)
class LogoRawEvidence:
    """Raw source evidence, preserved before any cleaning/transformation."""

    source_name: str
    source_endpoint: str
    fixture_provenance: str  # "mocked_team_list" | "mocked_binary" | a caller-supplied label
    collected_at_utc: str
    parser_version: str


@dataclass(frozen=True)
class LogoAssetRecord:
    """One team's raw logo evidence plus canonical identity attachment."""

    source_team_id: str | None
    raw_team_name: str
    team_identity: TeamIdentity | UnresolvedIdentity
    raw_logo_url: str | None
    source_endpoint: str
    media_type: str | None
    byte_length: int | None
    checksum_sha256: str | None
    canonical_filename: str | None
    legacy_filename: str | None
    retrieval_status: str  # see RETRIEVAL_STATUSES below
    output_path: str | None  # set only when output_dir was supplied and a write occurred


RETRIEVAL_STATUSES = {
    "downloaded", "skipped_existing", "failed_http", "failed_empty_content",
    "failed_invalid_media_type", "unresolved_team", "ambiguous_team", "missing_url",
}


@dataclass
class LogoIngestionResult:
    """
    The canonical logo/asset ingestion result -- an additive manifest.
    Carries no legacy dict/print-summary shape; `download_all_team_logos`'s
    existing callers are unaffected.
    """

    evidence: LogoRawEvidence
    records: list[LogoAssetRecord] = field(default_factory=list)
    duplicate_urls: dict[str, int] = field(default_factory=dict)
    duplicate_checksums: dict[str, list[str]] = field(default_factory=dict)
    filename_collisions: dict[str, list[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Safety: filenames and output paths
# ---------------------------------------------------------------------------


_UNSAFE_FILENAME_RX = re.compile(r"[\\/]|\.\.")


def is_safe_filename(filename: str) -> bool:
    """Rejects any filename containing a path separator or a ".." traversal segment."""
    return bool(filename) and not _UNSAFE_FILENAME_RX.search(filename)


def safe_output_path(output_dir: str, filename: str) -> Path | None:
    """
    Resolves `filename` under `output_dir` and confirms the result is
    still contained within it -- returns `None` (never a path) if the
    filename is unsafe or would escape the directory, so a caller can
    never be tricked into writing outside the configured location.
    """
    if not is_safe_filename(filename):
        return None
    base = Path(output_dir).resolve()
    candidate = (base / filename).resolve()
    if base not in candidate.parents and candidate != base:
        return None
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    return candidate


# ---------------------------------------------------------------------------
# Team-entry parsing -- extracts what download_all_team_logos discards
# ---------------------------------------------------------------------------


def parse_espn_team_entry(raw_team_name: str, href: str | None) -> tuple[str | None, str | None]:
    """
    Extracts the real ESPN numeric team id from a team-list `<a href>`
    (the same pattern `download_all_team_logos` already parses via
    `re.search(r"/id/(\\d+)", link["href"])`, reused verbatim) and builds
    the deterministic logo URL from it -- returns `(team_id, logo_url)`,
    both `None` when no id is present (never fabricated).
    """
    if not href:
        return None, None
    match = re.search(r"/id/(\d+)", href)
    if not match:
        return None, None
    team_id = match.group(1)
    return team_id, build_espn_logo_url_v15(team_id)


# ---------------------------------------------------------------------------
# Canonical identity + record construction per team
# ---------------------------------------------------------------------------


def resolve_logo_asset_record(
    raw_team_name: str,
    href: str | None,
    *,
    fetch_fn: Callable[[str], LogoFetchResponse] | None = None,
    legacy_filename_overrides: dict[str, str] | None = None,
    output_dir: str | None = None,
    source: str = "espn_logo",
) -> LogoAssetRecord:
    """
    Resolves one ESPN team-list entry to a canonical logo asset record.

    Team resolution reuses `resolve_espn_team_identity` (Phase 2A) --
    never infers a team from the filename alone. When `fetch_fn` is
    supplied, retrieves the logo binary (never a live request in this
    module itself -- the caller's injected function is responsible for
    that, and tests always supply a mocked one) and validates its status/
    content/media type. `legacy_filename_overrides` mirrors v15's
    `TEAM_LOGO_BY_SLUG` (a caller-suppliable slug -> legacy filename map,
    since this migration has no local evidence of that dict's real
    contents) -- when absent or not overridden, `legacy_filename` is
    `None` (not fabricated as "same as canonical").
    """
    team_id, logo_url = parse_espn_team_entry(raw_team_name, href)

    team_result = resolve_espn_team_identity(raw_team_name, source=source)

    if not isinstance(team_result, TeamIdentity):
        status = "ambiguous_team" if isinstance(team_result, UnresolvedIdentity) and team_result.reason == "ambiguous" else "unresolved_team"
        return LogoAssetRecord(
            source_team_id=team_id, raw_team_name=raw_team_name, team_identity=team_result,
            raw_logo_url=logo_url, source_endpoint=logo_url or "", media_type=None, byte_length=None,
            checksum_sha256=None, canonical_filename=None, legacy_filename=None,
            retrieval_status=status, output_path=None,
        )

    if not logo_url:
        return LogoAssetRecord(
            source_team_id=team_id, raw_team_name=raw_team_name, team_identity=team_result,
            raw_logo_url=None, source_endpoint="", media_type=None, byte_length=None,
            checksum_sha256=None, canonical_filename=None, legacy_filename=None,
            retrieval_status="missing_url", output_path=None,
        )

    sr_source = team_result.sources.get("sports_reference")
    canonical_slug = sr_source.raw_value if sr_source else str(team_result.team_id)
    canonical_filename = slug_to_filename_v15(canonical_slug).lower() + ".png"
    legacy_filename = (legacy_filename_overrides or {}).get(canonical_slug)

    if fetch_fn is None:
        return LogoAssetRecord(
            source_team_id=team_id, raw_team_name=raw_team_name, team_identity=team_result,
            raw_logo_url=logo_url, source_endpoint=logo_url, media_type=None, byte_length=None,
            checksum_sha256=None, canonical_filename=canonical_filename, legacy_filename=legacy_filename,
            retrieval_status="missing_url", output_path=None,
        )

    response = fetch_fn(logo_url)

    if response.status_code != 200:
        return LogoAssetRecord(
            source_team_id=team_id, raw_team_name=raw_team_name, team_identity=team_result,
            raw_logo_url=logo_url, source_endpoint=logo_url, media_type=response.content_type,
            byte_length=None, checksum_sha256=None, canonical_filename=canonical_filename,
            legacy_filename=legacy_filename, retrieval_status="failed_http", output_path=None,
        )

    if not response.content:
        return LogoAssetRecord(
            source_team_id=team_id, raw_team_name=raw_team_name, team_identity=team_result,
            raw_logo_url=logo_url, source_endpoint=logo_url, media_type=response.content_type,
            byte_length=0, checksum_sha256=None, canonical_filename=canonical_filename,
            legacy_filename=legacy_filename, retrieval_status="failed_empty_content", output_path=None,
        )

    if not (response.content_type or "").startswith("image/"):
        return LogoAssetRecord(
            source_team_id=team_id, raw_team_name=raw_team_name, team_identity=team_result,
            raw_logo_url=logo_url, source_endpoint=logo_url, media_type=response.content_type,
            byte_length=len(response.content), checksum_sha256=None, canonical_filename=canonical_filename,
            legacy_filename=legacy_filename, retrieval_status="failed_invalid_media_type", output_path=None,
        )

    checksum = hashlib.sha256(response.content).hexdigest()
    output_path: str | None = None
    status = "downloaded"

    if output_dir is not None:
        target = safe_output_path(output_dir, canonical_filename)
        if target is None:
            status = "failed_invalid_media_type"  # unsafe filename -- treated as a failure, never written
        elif target.exists():
            status = "skipped_existing"
            output_path = str(target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as fh:
                fh.write(response.content)
            output_path = str(target)

    return LogoAssetRecord(
        source_team_id=team_id, raw_team_name=raw_team_name, team_identity=team_result,
        raw_logo_url=logo_url, source_endpoint=logo_url, media_type=response.content_type,
        byte_length=len(response.content), checksum_sha256=checksum, canonical_filename=canonical_filename,
        legacy_filename=legacy_filename, retrieval_status=status, output_path=output_path,
    )


# ---------------------------------------------------------------------------
# Conflict detection -- never resolved silently
# ---------------------------------------------------------------------------


def find_duplicate_urls(records: list[LogoAssetRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in records:
        if r.raw_logo_url:
            counts[r.raw_logo_url] = counts.get(r.raw_logo_url, 0) + 1
    return {url: n for url, n in counts.items() if n > 1}


def find_duplicate_checksums(records: list[LogoAssetRecord]) -> dict[str, list[str]]:
    by_checksum: dict[str, list[str]] = {}
    for r in records:
        if r.checksum_sha256:
            by_checksum.setdefault(r.checksum_sha256, []).append(r.raw_team_name)
    return {c: names for c, names in by_checksum.items() if len(names) > 1}


def find_filename_collisions(records: list[LogoAssetRecord]) -> dict[str, list[str]]:
    """Two different teams whose canonical filenames would collide -- a real write hazard, never silently overwritten."""
    by_filename: dict[str, list[str]] = {}
    for r in records:
        if r.canonical_filename:
            by_filename.setdefault(r.canonical_filename, []).append(r.raw_team_name)
    return {f: names for f, names in by_filename.items() if len(set(names)) > 1}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_logo_asset_ingestion(
    team_entries: list[tuple[str, str | None]],
    *,
    fetch_fn: Callable[[str], LogoFetchResponse] | None = None,
    legacy_filename_overrides: dict[str, str] | None = None,
    output_dir: str | None = None,
    source_endpoint: str = "https://www.espn.com/mens-college-basketball/teams",
    fixture_provenance: str = "mocked_team_list",
    source: str = "espn_logo",
) -> LogoIngestionResult:
    """
    Orchestrates canonical logo/asset ingestion from already-retrieved
    team-list entries -- `[(raw_team_name, href_or_None), ...]`, the exact
    shape `download_all_team_logos`'s own loop consumes (`team.find("h2").text`,
    `team.find("a", href=True)["href"]`). Never calls `fetch_espn_teams`
    itself, never performs a live binary download -- `fetch_fn` is always
    caller-injected. Deterministic and idempotent: a repeated call against
    the same inputs and a `fetch_fn` returning the same bytes produces
    byte-identical records and checksums (verified by test); a second call
    with `output_dir` set reports `"skipped_existing"` rather than
    re-downloading or overwriting.
    """
    evidence = LogoRawEvidence(
        source_name=source,
        source_endpoint=source_endpoint,
        fixture_provenance=fixture_provenance,
        collected_at_utc=datetime.now(timezone.utc).isoformat(),
        parser_version=PARSER_VERSION,
    )

    records = [
        resolve_logo_asset_record(
            name, href, fetch_fn=fetch_fn, legacy_filename_overrides=legacy_filename_overrides,
            output_dir=output_dir, source=source,
        )
        for name, href in team_entries
    ]

    return LogoIngestionResult(
        evidence=evidence,
        records=records,
        duplicate_urls=find_duplicate_urls(records),
        duplicate_checksums=find_duplicate_checksums(records),
        filename_collisions=find_filename_collisions(records),
    )
