"""
Controlled bulk-import design, Slice 2 (docs/controlled_bulk_import_design.md
sections 6/10/16, approved for this slice only): a pure, deterministic,
read-only dry-run manifest contract and atomic JSON writer.

This module never discovers or parses `TeamDump`, never connects to a
database, never calls any of the four import adapters, and never derives
a value from mutable global state or wall-clock time. Every fact in a
manifest -- every planned artifact, every timestamp -- is supplied
explicitly by the caller. This slice's own job is only: (1) hold that
data in immutable, validated, typed records, (2) serialize it to
canonical, deterministic JSON, and (3) write it to disk atomically. No
real import count, hash, or discovery logic lives here -- a future,
separately-approved slice is responsible for actually walking `TeamDump`
and calling the four adapters' own already-tested pure stages to produce
the `PlannedArtifact` records this module accepts as input.

Three design decisions this slice makes, none literally spelled out as an
exact wire format in `docs/controlled_bulk_import_design.md` (which
specifies the manifest at a conceptual/field level, not a byte-for-byte
schema) -- each is the smallest, safest, most directly-derived reading of
that document's own words, not a competing standard:

1. **`schema_version`** -- a simple string (`"1"`), mirroring the exact
   same versioning convention Slice 1's own four adapter contract-version
   constants already established.
2. **Source-family identifiers** (`"allstats"`, `"team_game_log"`,
   `"espn"`, `"player_game_log"`) -- short tokens naming the same four
   families the design document's own §3/§4 already enumerates by name,
   mapped 1:1 to the four Slice 1 contract-version constants.
3. **Existing-file overwrite policy** -- the design document does not
   specify one. This module refuses to replace an existing destination
   file unless the caller explicitly passes `overwrite=True` -- the same
   fail-closed default this whole migration already uses everywhere else
   (disposable-database-name refusal, natural-key conflict rejection,
   immutable-field conflict detection). Recorded here, and in this
   slice's own completion report, as a decision made under this batch's
   authority, not an invented parallel standard.

The artifact-disposition vocabulary (`ARTIFACT_STATUSES`) is lifted
verbatim from the design document's own §6 prose ("planned insert /
idempotent match / unresolved / ambiguous / conflict").
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from src.exceptions import ValidationError
from src.pipeline.allstats_import_adapter import ALLSTATS_IMPORT_CONTRACT_VERSION
from src.pipeline.espn_import_adapter import ESPN_IMPORT_CONTRACT_VERSION
from src.pipeline.game_log_import_adapter import TEAM_GAMELOG_IMPORT_CONTRACT_VERSION
from src.pipeline.player_game_log_import_adapter import PLAYER_GAMELOG_IMPORT_CONTRACT_VERSION

__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "SOURCE_FAMILY_ORDER",
    "SOURCE_FAMILY_CONTRACT_VERSIONS",
    "ARTIFACT_STATUSES",
    "PlannedArtifact",
    "SourceFamilyPlan",
    "DryRunManifest",
    "build_dry_run_manifest",
    "serialize_manifest",
    "write_dry_run_manifest",
]

#: See module docstring, decision 1.
MANIFEST_SCHEMA_VERSION = "1"

#: Fixed, design-recommended operational order
#: (docs/controlled_bulk_import_design.md section 4) -- never alphabetical.
#: The design's own recommended sequence (AllStats, team game-log, player
#: game-log, ESPN) maximizes `game_results` reconciliation completeness at
#: commit time (a data-completeness nicety, not a correctness requirement,
#: per that section) -- this writer mirrors it for manifest legibility and
#: byte-for-byte reproducibility, never re-deriving or second-guessing it.
SOURCE_FAMILY_ORDER: tuple[str, ...] = ("allstats", "team_game_log", "player_game_log", "espn")

#: One entry per source family, referencing each adapter's own Slice 1
#: contract-version constant directly -- never a copied/duplicated literal,
#: so this module can never silently drift from the adapters themselves.
SOURCE_FAMILY_CONTRACT_VERSIONS: dict[str, str] = {
    "allstats": ALLSTATS_IMPORT_CONTRACT_VERSION,
    "team_game_log": TEAM_GAMELOG_IMPORT_CONTRACT_VERSION,
    "player_game_log": PLAYER_GAMELOG_IMPORT_CONTRACT_VERSION,
    "espn": ESPN_IMPORT_CONTRACT_VERSION,
}

#: Verbatim from docs/controlled_bulk_import_design.md section 6's own prose.
ARTIFACT_STATUSES: frozenset[str] = frozenset({
    "planned_insert", "idempotent_match", "unresolved", "ambiguous", "conflict",
})

#: Conservative, explicit deny-list for the "reject secrets, credentials,
#: and database URLs" requirement. This is a safety NET, not a security
#: guarantee -- it catches the obvious, common shapes (a literal db
#: connection string, a key/value pair that looks like a credential) that
#: could otherwise slip in through the free-form `metadata` field on a
#: `PlannedArtifact`. It never inspects `.env`, never connects anywhere,
#: and never attempts a general-purpose secret-detection scan.
_SENSITIVE_URL_SCHEMES = ("postgres://", "postgresql://", "mysql://", "mongodb://", "redis://", "mssql://")
_SENSITIVE_SUBSTRINGS = (
    "password", "passwd", "secret", "api_key", "apikey", "access_key",
    "private_key", "begin rsa private key", "begin private key",
    "connection_string", "credential",
)


def _reject_sensitive_text(value: str, *, field_name: str) -> None:
    lowered = value.lower()
    if any(scheme in lowered for scheme in _SENSITIVE_URL_SCHEMES):
        raise ValidationError(f"{field_name} appears to contain a database connection URL -- refusing: {value!r}")
    if any(term in lowered for term in _SENSITIVE_SUBSTRINGS):
        raise ValidationError(f"{field_name} appears to contain a secret/credential-like value -- refusing: {value!r}")


def _reject_unsafe_relative_path(value: str, *, field_name: str) -> None:
    if not value or not value.strip():
        raise ValidationError(f"{field_name} must be a non-empty relative path")
    # Absolute-path rejection covers both POSIX ("/etc/passwd") and Windows
    # ("C:\\Users\\...", "\\\\server\\share") absolute forms -- checked via
    # PureWindowsPath (a superset of what PurePosixPath alone would catch)
    # rather than relying on the current OS's own path semantics, so this
    # check behaves identically regardless of which platform runs it.
    if PureWindowsPath(value).is_absolute() or value.startswith("/") or value.startswith("\\"):
        raise ValidationError(f"{field_name} must be a source-relative path, never absolute: {value!r}")
    segments = value.replace("\\", "/").split("/")
    if any(segment == ".." for segment in segments):
        raise ValidationError(f"{field_name} must not contain '..' parent-directory traversal: {value!r}")
    _reject_sensitive_text(value, field_name=field_name)


@dataclass(frozen=True)
class PlannedArtifact:
    """
    One caller-supplied, already-planned artifact record. Immutable
    (`frozen=True`) and self-validating at construction -- never repaired
    or coerced, only accepted or rejected. `metadata` is a plain,
    caller-owned `dict` copied (never referenced) at construction, so a
    caller mutating its own dict after building this record can never
    retroactively change an already-built manifest.
    """

    source_root_key: str
    relative_path: str
    content_hash: str
    status: str
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source_root_key or not self.source_root_key.strip():
            raise ValidationError("source_root_key must be a non-empty string")
        _reject_sensitive_text(self.source_root_key, field_name="source_root_key")
        _reject_unsafe_relative_path(self.relative_path, field_name="relative_path")
        if not self.content_hash or not self.content_hash.strip():
            raise ValidationError("content_hash must be a non-empty string")
        _reject_sensitive_text(self.content_hash, field_name="content_hash")
        if self.status not in ARTIFACT_STATUSES:
            raise ValidationError(f"status {self.status!r} is not one of the approved values: {sorted(ARTIFACT_STATUSES)}")
        if not isinstance(self.metadata, dict):
            raise ValidationError("metadata must be a dict[str, str]")
        for key, val in self.metadata.items():
            if not isinstance(key, str) or not isinstance(val, str):
                raise ValidationError(f"metadata keys and values must be strings, got {key!r}: {val!r}")
            _reject_sensitive_text(key, field_name=f"metadata key {key!r}")
            _reject_sensitive_text(val, field_name=f"metadata[{key!r}]")
        # Defensive copy -- never hold a reference to the caller's own dict.
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def identity(self) -> tuple[str, str, str]:
        """The natural-key triple this artifact would occupy in `source_artifacts` -- (source_root_key, relative_path, content_hash)."""
        return (self.source_root_key, self.relative_path, self.content_hash)


@dataclass(frozen=True)
class SourceFamilyPlan:
    """One source family's own planned artifacts for this manifest, already sorted and duplicate-checked at construction."""

    family: str
    contract_version: str
    artifacts: tuple[PlannedArtifact, ...]

    def __post_init__(self) -> None:
        if self.family not in SOURCE_FAMILY_CONTRACT_VERSIONS:
            raise ValidationError(f"unknown source family {self.family!r}; expected one of {sorted(SOURCE_FAMILY_CONTRACT_VERSIONS)}")
        expected_version = SOURCE_FAMILY_CONTRACT_VERSIONS[self.family]
        if self.contract_version != expected_version:
            raise ValidationError(
                f"contract_version {self.contract_version!r} does not match the current "
                f"{self.family!r} adapter contract version {expected_version!r} -- "
                f"refusing an incompatible source-family/contract-version combination"
            )
        if not isinstance(self.artifacts, tuple):
            raise ValidationError("artifacts must already be a tuple of PlannedArtifact (build via build_dry_run_manifest, not by hand)")
        seen: set[tuple[str, str, str]] = set()
        for artifact in self.artifacts:
            if not isinstance(artifact, PlannedArtifact):
                raise ValidationError(f"every artifact must be a PlannedArtifact, got {type(artifact).__name__}")
            if artifact.identity in seen:
                raise ValidationError(f"duplicate artifact identity within family {self.family!r}: {artifact.identity}")
            seen.add(artifact.identity)
        # Deterministic, stable ordering -- never the caller's own input order.
        sorted_artifacts = tuple(sorted(self.artifacts, key=lambda a: (a.source_root_key, a.relative_path, a.content_hash)))
        object.__setattr__(self, "artifacts", sorted_artifacts)


@dataclass(frozen=True)
class DryRunManifest:
    """The complete, immutable, already-validated manifest for one dry run."""

    generated_at: dt.datetime
    families: tuple[SourceFamilyPlan, ...]

    def __post_init__(self) -> None:
        if self.generated_at.tzinfo is None:
            raise ValidationError(f"generated_at must be timezone-aware (got a naive datetime): {self.generated_at!r}")
        if not isinstance(self.families, tuple):
            raise ValidationError("families must already be a tuple of SourceFamilyPlan (build via build_dry_run_manifest, not by hand)")
        seen_families: set[str] = set()
        for plan in self.families:
            if not isinstance(plan, SourceFamilyPlan):
                raise ValidationError(f"every family entry must be a SourceFamilyPlan, got {type(plan).__name__}")
            if plan.family in seen_families:
                raise ValidationError(f"duplicate source family in one manifest: {plan.family!r}")
            seen_families.add(plan.family)
        # Deterministic, design-recommended family order -- never caller input order,
        # never alphabetical. Any family not in SOURCE_FAMILY_ORDER would already have
        # been rejected by SourceFamilyPlan.__post_init__ above.
        sorted_families = tuple(sorted(self.families, key=lambda p: SOURCE_FAMILY_ORDER.index(p.family)))
        object.__setattr__(self, "families", sorted_families)
        object.__setattr__(self, "generated_at", self.generated_at.astimezone(dt.timezone.utc))


def build_dry_run_manifest(*, generated_at: dt.datetime, families) -> DryRunManifest:
    """
    Pure. Accepts any iterable of `SourceFamilyPlan` (never mutated) and
    returns a new, immutable, validated, deterministically-ordered
    `DryRunManifest`. This is the only supported construction path --
    never build a `DryRunManifest` by hand outside this function.
    """
    return DryRunManifest(generated_at=generated_at, families=tuple(families))


def serialize_manifest(manifest: DryRunManifest) -> str:
    """
    Pure. Canonical JSON text for `manifest`, ending in exactly one
    trailing newline. `sort_keys=True` guarantees stable key ordering
    independent of dict insertion order; `ensure_ascii=False` preserves
    real Unicode characters (e.g. an accented relative path) verbatim
    rather than escaping them, matching the "Unicode-safe relative paths"
    requirement -- the caller is responsible for writing the result with
    `encoding="utf-8"`, which `write_dry_run_manifest` below does.
    """
    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at": manifest.generated_at.isoformat().replace("+00:00", "Z"),
        "source_families": [
            {
                "family": plan.family,
                "contract_version": plan.contract_version,
                "artifacts": [
                    {
                        "source_root_key": a.source_root_key,
                        "relative_path": a.relative_path,
                        "content_hash": a.content_hash,
                        "status": a.status,
                        "metadata": dict(a.metadata),
                    }
                    for a in plan.artifacts
                ],
            }
            for plan in manifest.families
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_dry_run_manifest(manifest: DryRunManifest, destination: Path, *, overwrite: bool = False) -> Path:
    """
    Atomic write: serializes `manifest`, writes it to a temporary file in
    `destination`'s own parent directory (same filesystem, required for a
    true atomic replace), then `os.replace()`s it into place. `destination`
    either ends up fully containing the new manifest, or -- on any
    failure -- is left completely untouched (its prior contents, if any,
    survive unchanged); the temporary file is always removed on failure,
    never left behind. Refuses to replace an existing destination unless
    `overwrite=True` is passed explicitly (see module docstring, decision 3).
    """
    destination = Path(destination)
    if destination.exists() and not overwrite:
        raise ValidationError(
            f"refusing to overwrite existing manifest at {destination} -- pass overwrite=True to replace it explicitly"
        )
    if not destination.parent.is_dir():
        raise ValidationError(f"destination directory does not exist: {destination.parent}")

    payload = serialize_manifest(manifest)

    fd, tmp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, destination)
    except BaseException:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise
    return destination
