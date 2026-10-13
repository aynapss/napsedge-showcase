"""
Backup artifact file contract: typed documents, strict JSON, and a read-only
verifier (Phase 9B-5A).

This module is the READ SIDE of `docs/backup_artifact_contract_proposal.md`
(revision 3). It defines what a well-formed artifact directory looks like and
answers one question about a directory on disk: is it structurally intact?

What it deliberately does not do
--------------------------------
* It writes nothing: no marker writer, no finalizer, no `FAILED` writer, and the
  verifier never repairs, renames, adopts or otherwise modifies its input.
* It performs no capture: no SQL, no database or network connection, no
  subprocess, no scheduler, no storage integration.
* It evaluates no sequence rule and compares no database state.
* It never imports from the test tree. Importing it does no filesystem, network
  or process work (only definitions are created).

What "STRUCTURALLY_VALID" means
-------------------------------
Claim 2 of the specification and **only** claim 2: the directory has exactly the
expected files, safe names, strict well-formed JSON in the supported versions,
sizes and SHA-256 digests that match, a completion marker that binds the
manifest, and documents whose identities and summaries agree with each other.
It does NOT establish that the archive and baseline describe the same database
state (claim 3) or that the archive restores (claim 4), and the verdict says so
explicitly. A completion marker is an integrity binding against accidents, not
an authenticity proof: anyone who can rewrite the files can rewrite the marker.
Matching hashes, manifest flags and `COMPLETE` are never treated as evidence of
database consistency or of a successful restore.

Manifest versions (Phase 9B-6H, specification 13.16)
----------------------------------------------------
`/1` is unchanged, including the historical meaning of `catalog_bracket_equal`
(the in-transaction bracket that failed validation, 12.5). `/2` drops that field
and requires `consistency.catalog_observation`: the fresh-endpoint procedure
observed NO DIFFERENCE between two READ COMMITTED endpoints. That is not catalog
stability, writer exclusion or consistency, and this module checks only the
record's shape and internal agreement (categories, digest formats and derivation,
endpoint completeness and order, the source matching the manifest's); it cannot
authenticate the observations or prove the procedure ran. Any other version is
`UNSUPPORTED_VERSION`, and the typed manifest's version follows from its
consistency record's type, so /1 and /2 content cannot be mixed.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from typing import Any, NoReturn, Sequence

# ---------------------------------------------------------------------------
# format identifiers, names and limits
# ---------------------------------------------------------------------------
MANIFEST_FORMAT = "napsedge.backup-artifact/1"
#: Phase 9B-6H (specification 13.16): /2 replaces /1's `consistency.catalog_bracket_equal` with a required
#: `consistency.catalog_observation` record. /1 keeps its validation branch and its historical meanings unchanged.
MANIFEST_FORMAT_V2 = "napsedge.backup-artifact/2"
MANIFEST_FORMATS = (MANIFEST_FORMAT, MANIFEST_FORMAT_V2)
BASELINE_FORMAT = "napsedge.backup-baseline/1"
MARKER_FORMAT = "napsedge.backup-complete/1"

ARCHIVE_FILE = "archive.dump"
BASELINE_FILE = "baseline.json"
MANIFEST_FILE = "manifest.json"
MARKER_FILE = "COMPLETE"
RUNNING_FILE = "RUNNING"
FAILED_FILE = "FAILED"
ARTIFACT_FILES = (ARCHIVE_FILE, BASELINE_FILE, MANIFEST_FILE, MARKER_FILE)
INVENTORY_PATHS = (ARCHIVE_FILE, BASELINE_FILE)

MAX_MANIFEST_BYTES = 1 << 20
MAX_BASELINE_BYTES = 64 << 20
MAX_MARKER_BYTES = 512
MAX_JSON_DEPTH = 8
HASH_CHUNK_BYTES = 1 << 20

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT (Windows symlinks, junctions, mount points)

#: Every reason code the verifier or the parsers can produce, with its meaning.
REASON_CODES: dict[str, str] = {
    # directory level
    "NOT_A_DIRECTORY": "the path does not exist, cannot be inspected, or is not a directory",
    "SYMLINK": "a symbolic link or reparse point where a real file or directory is required (the artifact directory, any ancestor of it, or an entry inside it)",
    "NAME_INVALID": "the directory name does not match napsedge-<logical_name>-<UTC time>-<uuid4>",
    "NOT_FINAL": "a .partial-* working directory: never a published artifact",
    "MISSING_FILE": "one of the four required files is absent",
    "NOT_A_REGULAR_FILE": "a required name exists but is not a regular file",
    "UNLISTED_FILE": "an entry that is not one of the four required files",
    "CONFLICTING_MARKERS": "COMPLETE alongside RUNNING or FAILED",
    "ARTIFACT_FAILED": "a FAILED marker and no COMPLETE",
    "ARTIFACT_RUNNING": "a RUNNING marker and no COMPLETE (interrupted or unfinished)",
    "UNREADABLE_FILE": "a required file could not be read",
    "FILE_TOO_LARGE": "a file exceeds its size limit; it was not read into memory or parsed",
    # completion marker
    "MARKER_MALFORMED": "the COMPLETE marker is not exactly the four expected LF-terminated lines",
    "MARKER_UNSUPPORTED_VERSION": "the COMPLETE marker names a format version this verifier does not support",
    "MARKER_SIZE_MISMATCH": "manifest.json's size differs from the marker's manifest_bytes",
    "MARKER_DIGEST_MISMATCH": "manifest.json's SHA-256 differs from the marker's manifest_sha256",
    "MARKER_ARTIFACT_MISMATCH": "the marker's artifact_id differs from the manifest's",
    # JSON documents (the detail names the document and field)
    "INVALID_ENCODING": "not valid UTF-8, or begins with a byte order mark",
    "JSON_MALFORMED": "not syntactically valid JSON",
    "JSON_DUPLICATE_KEY": "an object repeats a key",
    "JSON_NON_FINITE": "NaN or Infinity",
    "JSON_FLOAT": "a number with a fraction or exponent (only exact integers are allowed)",
    "JSON_TOO_DEEP": "nesting deeper than the limit",
    "JSON_INVALID_STRING": "a string that is not valid Unicode text (for example a lone surrogate)",
    "JSON_UNSUPPORTED_TYPE": "a value type the contract does not allow (serialization only)",
    "INTEGER_OUT_OF_RANGE": "an integer outside the signed 64-bit range",
    "NOT_CANONICAL": "valid content but not byte-identical to its canonical serialization",
    "UNSUPPORTED_VERSION": "a `format` value this verifier does not support",
    "FIELD_MISSING": "a required field is absent",
    "FIELD_UNKNOWN": "a field the format does not define",
    "FIELD_TYPE": "a field has the wrong JSON type (including a boolean where an integer is required)",
    "FIELD_VALUE": "a field has the right type but an invalid value, or fields contradict each other",
    "TIMESTAMP_INVALID": "not exactly YYYY-MM-DDTHH:MM:SS.ffffffZ, or not a real calendar instant",
    "BASELINE_DUPLICATE_KEY": "a logical key repeats inside a baseline list",
    "BASELINE_ORDER": "a baseline list that must be in canonical order is not",
    "BASELINE_SEQUENCE_MISMATCH": "sequence_details and sequences name different sequences",
    # cross-file and cross-document
    "NAME_MISMATCH": "the directory name disagrees with the manifest (artifact id, logical name or time)",
    "INVENTORY_INVALID": "the manifest's file inventory is not exactly the two expected entries",
    "UNSAFE_PATH": "an inventory path that is not one of the two allowed plain names",
    "FILE_SIZE_MISMATCH": "a file's size differs from the inventory",
    "FILE_DIGEST_MISMATCH": "a file's SHA-256 differs from the inventory",
    "BASELINE_ARTIFACT_MISMATCH": "baseline.json names a different artifact than the manifest",
    "BASELINE_SCHEMA_VERSION_MISMATCH": "baseline.version is null or differs from the manifest's schema.alembic_head",
    "BASELINE_SUMMARY_MISMATCH": "the manifest's baseline summary differs from the baseline document",
    "BASELINE_TIME_OUTSIDE_CAPTURE": "the baseline's capture time is outside the manifest's capture window",
    "IDENTITY_MISMATCH": "the source identity differs from the caller's expectation",
    "SCHEMA_HEAD_MISMATCH": "the schema head differs from the caller's expectation",
}

ESTABLISHES = ("structural_integrity",)
DOES_NOT_ESTABLISH = ("database_consistency", "restore_verified", "authenticity")


class ArtifactFormatError(ValueError):
    """A document, marker or serialization request violates the contract. `code` is a key of REASON_CODES."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _fail(code: str, detail: str = "") -> NoReturn:
    raise ArtifactFormatError(code, detail)


# ---------------------------------------------------------------------------
# strict JSON and canonical serialization
# ---------------------------------------------------------------------------
def _reject_float(text: str) -> NoReturn:
    _fail("JSON_FLOAT", text[:40])


def _reject_constant(text: str) -> NoReturn:
    _fail("JSON_NON_FINITE", text)


def _object_pairs(pairs: list) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            _fail("JSON_DUPLICATE_KEY", ascii(key)[:80])
        result[key] = value
    return result


def _check_text(value: str, where: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _fail("JSON_INVALID_STRING", where)


def _check_value(value: Any, depth: int = 0) -> None:
    """The one place that defines which JSON values the contract allows. Used for parsing and for serialization."""
    if depth > MAX_JSON_DEPTH:
        _fail("JSON_TOO_DEEP", f"deeper than {MAX_JSON_DEPTH}")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if not INT64_MIN <= value <= INT64_MAX:
            _fail("INTEGER_OUT_OF_RANGE", str(value)[:30])
        return
    if isinstance(value, float):
        _fail("JSON_FLOAT", repr(value)[:40])
    if isinstance(value, str):
        _check_text(value, "string value")
        return
    if isinstance(value, list):
        for item in value:
            _check_value(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _fail("JSON_UNSUPPORTED_TYPE", f"object key of type {type(key).__name__}")
            _check_text(key, "object key")
            _check_value(item, depth + 1)
        return
    _fail("JSON_UNSUPPORTED_TYPE", type(value).__name__)


def parse_strict_json(data: bytes, *, max_bytes: int) -> Any:
    """
    Parses `data` under the contract's rules or raises ArtifactFormatError:
    size bound first (before any decoding), strict UTF-8 without a BOM, valid
    syntax, no duplicate keys, no floats, no non-finite numbers, integers within
    signed 64 bits, bounded depth, and no lone surrogates.
    """
    if len(data) > max_bytes:
        _fail("FILE_TOO_LARGE", f"{len(data)} bytes exceeds {max_bytes}")
    if data.startswith(b"\xef\xbb\xbf"):
        _fail("INVALID_ENCODING", "byte order mark")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        _fail("INVALID_ENCODING", "not valid UTF-8")
    try:
        value = json.loads(text, object_pairs_hook=_object_pairs, parse_float=_reject_float, parse_constant=_reject_constant)
    except ArtifactFormatError:
        raise
    except RecursionError:
        _fail("JSON_TOO_DEEP", "parser recursion limit")
    except ValueError as exc:  # JSONDecodeError and over-long integer literals
        _fail("JSON_MALFORMED", type(exc).__name__)
    _check_value(value)
    return value


def canonical_json_bytes(document: Any) -> bytes:
    """
    The one deterministic serialization: keys sorted, separators `,` and `:`,
    raw UTF-8 (no ASCII escaping), no NaN, exactly one trailing LF. Applies the
    same value rules as parsing, so anything it produces parses back.
    """
    _check_value(document)
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"


def _parse_canonical(data: bytes, *, max_bytes: int) -> dict:
    value = parse_strict_json(data, max_bytes=max_bytes)
    if not isinstance(value, dict):
        _fail("FIELD_TYPE", "document: expected a JSON object at the top level")
    if canonical_json_bytes(value) != data:
        _fail("NOT_CANONICAL", "bytes differ from the canonical serialization")
    return value


# ---------------------------------------------------------------------------
# field validators (every violation raises ArtifactFormatError with a path in the detail)
# ---------------------------------------------------------------------------
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MD5 = re.compile(r"[0-9a-f]{32}")
_GIT_REVISION = re.compile(r"[0-9a-f]{40}")
_LOGICAL_NAME = re.compile(r"[a-z0-9_]{1,40}")
_SYSTEM_IDENTIFIER = re.compile(r"[0-9]{1,20}")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z")
_SNAPSHOT_DESCRIPTOR = re.compile(r"[0-9]+:[0-9]+:([0-9]+(,[0-9]+)*)?")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_$.\-]{1,63}")
_TOOL_NAME = re.compile(r"[a-z0-9_.\-]{1,64}")
_TOOL_VERSION = re.compile(r"[A-Za-z0-9_.+\-]{1,64}")
_REVISION = re.compile(r"[A-Za-z0-9_]{1,64}")


def _obj(value: Any, path: str, keys: Sequence[str]) -> dict:
    if not isinstance(value, dict):
        _fail("FIELD_TYPE", f"{path}: expected an object")
    for key in keys:
        if key not in value:
            _fail("FIELD_MISSING", f"{path}.{key}")
    unknown = sorted(set(value) - set(keys))
    if unknown:
        _fail("FIELD_UNKNOWN", f"{path}.{ascii(unknown[0])[:60]}")
    return value


def _list(value: Any, path: str) -> list:
    if not isinstance(value, list):
        _fail("FIELD_TYPE", f"{path}: expected an array")
    return value


def _str(value: Any, path: str, *, pattern: re.Pattern | None = None, allowed: Sequence[str] | None = None, nonempty: bool = True, max_len: int = 4096) -> str:
    if not isinstance(value, str):
        _fail("FIELD_TYPE", f"{path}: expected a string")
    if (nonempty and not value) or len(value) > max_len:
        _fail("FIELD_VALUE", f"{path}: empty or longer than {max_len}")
    if allowed is not None and value not in allowed:
        _fail("FIELD_VALUE", f"{path}: not one of {list(allowed)}")
    if pattern is not None and not pattern.fullmatch(value):
        _fail("FIELD_VALUE", f"{path}: does not match the required pattern")
    return value


def _int(value: Any, path: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail("FIELD_TYPE", f"{path}: expected an integer (booleans are not integers here)")
    if minimum is not None and value < minimum:
        _fail("FIELD_VALUE", f"{path}: below {minimum}")
    return value


def _int_or_null(value: Any, path: str) -> int | None:
    return None if value is None else _int(value, path)


def _bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        _fail("FIELD_TYPE", f"{path}: expected a boolean")
    return value


def _timestamp(value: Any, path: str) -> _dt.datetime:
    if not isinstance(value, str):
        _fail("FIELD_TYPE", f"{path}: expected a string")
    if not _TIMESTAMP.fullmatch(value):
        _fail("TIMESTAMP_INVALID", f"{path}: not YYYY-MM-DDTHH:MM:SS.ffffffZ")
    try:
        return _dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        _fail("TIMESTAMP_INVALID", f"{path}: not a real calendar instant")


def _check_format(document: Any, expected: str | tuple[str, ...], family: str, path: str) -> None:
    if not isinstance(document, dict) or "format" not in document:
        _fail("FIELD_MISSING", f"{path}.format")
    found = document["format"]
    if found in ((expected,) if isinstance(expected, str) else expected):
        return
    if isinstance(found, str) and found.startswith(family):
        _fail("UNSUPPORTED_VERSION", f"{path}.format: {ascii(found)[:60]}")
    _fail("FIELD_VALUE", f"{path}.format: not a {family}* value")


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------
_MANIFEST_FAMILY = "napsedge.backup-artifact/"
_BASELINE_FAMILY = "napsedge.backup-baseline/"
_MARKER_FAMILY = "napsedge.backup-complete/"


@dataclass(frozen=True)
class SourceInfo:
    logical_name: str
    system_identifier: str
    database_oid: int
    database_name: str
    server_version_num: int
    pg_dump_version: str
    backup_role: str


@dataclass(frozen=True)
class SchemaState:
    alembic_head: str
    expected_head: str
    heads_match: bool


@dataclass(frozen=True)
class RepositoryState:
    git_revision: str
    dirty: bool


@dataclass(frozen=True)
class ToolingInfo:
    name: str
    version: str


@dataclass(frozen=True)
class ConsistencyClaim:
    """What the capture procedure CLAIMS. Nothing in this module checks it against a database."""

    mode: str
    snapshot_descriptor: str
    snapshot_unchanged_at_end: bool
    sequence_rule: str
    ddl_exclusion: str
    catalog_bracket_equal: bool
    foreign_write_sessions_start: int
    foreign_write_sessions_end: int


# -- manifest /2: the fresh catalog observation record (specification 13.16) ----------------------------------------------
CATALOG_OBSERVATION_METHOD = "fresh_endpoints_v1"
CATALOG_OBSERVATION_RESULT = "no_difference_observed"
CATALOG_OBSERVATION_CATEGORIES = ("constraints", "indexes", "acls")  # the catalog observer's CATEGORIES (drift-tested), in its order
CATALOG_ENDPOINT_LABELS = ("before", "after")
CATALOG_ENDPOINT_ISOLATION = "read committed"
CATALOG_ENDPOINT_READ_ONLY = "on"
CATALOG_ENDPOINT_CLEANUP = "closed_and_released"  # the lock module's VERIFIED_CLOSED (drift-tested)


@dataclass(frozen=True)
class CatalogDigests:
    constraints: str
    indexes: str
    acls: str
    combined: str


@dataclass(frozen=True)
class CatalogEndpointRecord:
    """One fresh observer endpoint, as the observer obtained it. Timestamps are the SERVER's, rendered in UTC."""

    label: str
    backend_pid: int
    backend_start: str
    transaction_start: str
    isolation: str
    read_only: str
    cleanup_verification: str
    digests: CatalogDigests


@dataclass(frozen=True)
class ObservedSource:
    system_identifier: str
    database_oid: int
    database_name: str


@dataclass(frozen=True)
class CatalogObservationRecord:
    """
    Manifest /2 `consistency.catalog_observation`: the fresh-endpoint procedure found NO DIFFERENCE between these two
    endpoints. It is not catalog stability, writer exclusion or archive consistency: each endpoint is a READ COMMITTED
    transaction (not one atomic catalog snapshot), and a change and its reversal between the endpoints is not detected.
    Structural validation checks the record's shape and internal agreement only; it does not authenticate the
    observations or prove that the procedure was performed.
    """

    method: str
    result: str
    categories: tuple[str, ...]
    source: ObservedSource
    endpoints: tuple[CatalogEndpointRecord, CatalogEndpointRecord]

    @classmethod
    def from_document(cls, document: Any, path: str = "manifest.consistency.catalog_observation") -> "CatalogObservationRecord":
        o = _obj(document, path, ("method", "result", "categories", "source", "endpoints"))
        method = _str(o["method"], f"{path}.method", allowed=(CATALOG_OBSERVATION_METHOD,))
        result = _str(o["result"], f"{path}.result", allowed=(CATALOG_OBSERVATION_RESULT,))
        categories = _list(o["categories"], f"{path}.categories")
        if categories != list(CATALOG_OBSERVATION_CATEGORIES):
            _fail("FIELD_VALUE", f"{path}.categories: must be exactly {list(CATALOG_OBSERVATION_CATEGORIES)}")
        s = _obj(o["source"], f"{path}.source", ("system_identifier", "database_oid", "database_name"))
        source = ObservedSource(
            system_identifier=_str(s["system_identifier"], f"{path}.source.system_identifier", pattern=_SYSTEM_IDENTIFIER),
            database_oid=_int(s["database_oid"], f"{path}.source.database_oid", minimum=0),
            database_name=_str(s["database_name"], f"{path}.source.database_name", pattern=_IDENTIFIER),
        )
        raw = _list(o["endpoints"], f"{path}.endpoints")
        if len(raw) != len(CATALOG_ENDPOINT_LABELS):
            _fail("FIELD_VALUE", f"{path}.endpoints: expected exactly {len(CATALOG_ENDPOINT_LABELS)} endpoints, found {len(raw)}")
        endpoints = tuple(_catalog_endpoint(item, f"{path}.endpoints[{index}]", CATALOG_ENDPOINT_LABELS[index]) for index, item in enumerate(raw))
        before, after = endpoints
        if before.digests != after.digests:
            _fail("FIELD_VALUE", f"{path}.endpoints: the endpoint digests differ, which is not a no-difference result")
        if (before.backend_pid, before.backend_start, before.transaction_start) == (after.backend_pid, after.backend_start, after.transaction_start):
            _fail("FIELD_VALUE", f"{path}.endpoints: the two endpoints are not distinct transactions")
        if _timestamp(after.transaction_start, path) < _timestamp(before.transaction_start, path):
            _fail("FIELD_VALUE", f"{path}.endpoints[1].transaction_start: earlier than the before endpoint's")
        return cls(method=method, result=result, categories=tuple(categories), source=source, endpoints=(before, after))

    def to_document(self) -> dict:
        return {
            "method": self.method, "result": self.result, "categories": list(self.categories),
            "source": {"system_identifier": self.source.system_identifier, "database_oid": self.source.database_oid,
                       "database_name": self.source.database_name},
            "endpoints": [{"label": e.label, "backend_pid": e.backend_pid, "backend_start": e.backend_start, "transaction_start": e.transaction_start,
                           "isolation": e.isolation, "read_only": e.read_only, "cleanup_verification": e.cleanup_verification,
                           "digests": {"constraints": e.digests.constraints, "indexes": e.digests.indexes, "acls": e.digests.acls,
                                       "combined": e.digests.combined}} for e in self.endpoints],
        }


def _catalog_endpoint(value: Any, path: str, label: str) -> CatalogEndpointRecord:
    e = _obj(value, path, ("label", "backend_pid", "backend_start", "transaction_start", "isolation", "read_only", "cleanup_verification", "digests"))
    _str(e["label"], f"{path}.label", allowed=(label,))
    backend_start = _timestamp(e["backend_start"], f"{path}.backend_start")
    transaction_start = _timestamp(e["transaction_start"], f"{path}.transaction_start")
    if transaction_start < backend_start:
        _fail("FIELD_VALUE", f"{path}.transaction_start: earlier than backend_start")
    d = _obj(e["digests"], f"{path}.digests", (*CATALOG_OBSERVATION_CATEGORIES, "combined"))
    digests = CatalogDigests(**{key: _str(d[key], f"{path}.digests.{key}", pattern=_SHA256) for key in (*CATALOG_OBSERVATION_CATEGORIES, "combined")})
    combined = hashlib.sha256("|".join(getattr(digests, c) for c in CATALOG_OBSERVATION_CATEGORIES).encode("ascii")).hexdigest()
    if digests.combined != combined:
        _fail("FIELD_VALUE", f"{path}.digests.combined: not the digest of the category digests")
    return CatalogEndpointRecord(
        label=label,
        backend_pid=_int(e["backend_pid"], f"{path}.backend_pid", minimum=1),
        backend_start=e["backend_start"], transaction_start=e["transaction_start"],
        isolation=_str(e["isolation"], f"{path}.isolation", allowed=(CATALOG_ENDPOINT_ISOLATION,)),
        read_only=_str(e["read_only"], f"{path}.read_only", allowed=(CATALOG_ENDPOINT_READ_ONLY,)),
        cleanup_verification=_str(e["cleanup_verification"], f"{path}.cleanup_verification", allowed=(CATALOG_ENDPOINT_CLEANUP,)),
        digests=digests,
    )


@dataclass(frozen=True)
class ConsistencyClaimV2:
    """
    Manifest /2's `consistency`: /1's fields without `catalog_bracket_equal`, plus the required `catalog_observation`.
    Since Phase 9B-6I (specification 13.18) the two foreign-write-session counts may BOTH be null, meaning "not sampled
    by this capture path" -- never zero. No accepted production module samples them, and the count's role set is an open
    decision (section 11, item 8). They were, and remain, evidence only: never exclusion or quiescence. /1 is unchanged.
    """

    mode: str
    snapshot_descriptor: str
    snapshot_unchanged_at_end: bool
    sequence_rule: str
    ddl_exclusion: str
    catalog_observation: CatalogObservationRecord
    foreign_write_sessions_start: int | None
    foreign_write_sessions_end: int | None


@dataclass(frozen=True)
class ArchiveInfo:
    format: str
    toc_entries: int
    toc_sha256: str


@dataclass(frozen=True)
class BaselineSummary:
    table_count: int
    row_total: int


@dataclass(frozen=True)
class FileEntry:
    path: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class ArtifactManifest:
    artifact_id: str
    created_utc: str
    finished_utc: str
    source: SourceInfo
    schema: SchemaState
    repository: RepositoryState
    tooling: ToolingInfo
    consistency: ConsistencyClaim | ConsistencyClaimV2
    archive: ArchiveInfo
    baseline: BaselineSummary
    files: tuple[FileEntry, FileEntry]

    @property
    def format(self) -> str:
        """DERIVED from the consistency record's type, so a manifest mixing /1 and /2 content cannot be built or written."""
        if type(self.consistency) is ConsistencyClaimV2:
            return MANIFEST_FORMAT_V2
        if type(self.consistency) is ConsistencyClaim:
            return MANIFEST_FORMAT
        raise ArtifactFormatError("FIELD_TYPE", "manifest.consistency: not a ConsistencyClaim (/1) or ConsistencyClaimV2 (/2)")

    @classmethod
    def from_document(cls, document: Any) -> "ArtifactManifest":
        _check_format(document, MANIFEST_FORMATS, _MANIFEST_FAMILY, "manifest")
        version_2 = document["format"] == MANIFEST_FORMAT_V2
        top = _obj(document, "manifest", ("format", "artifact_id", "created_utc", "finished_utc", "source", "schema", "repository",
                                           "tooling", "consistency", "archive", "baseline", "files"))
        artifact_id = _str(top["artifact_id"], "manifest.artifact_id", pattern=_UUID4)
        created = _timestamp(top["created_utc"], "manifest.created_utc")
        finished = _timestamp(top["finished_utc"], "manifest.finished_utc")
        if finished < created:
            _fail("FIELD_VALUE", "manifest.finished_utc: earlier than created_utc")

        s = _obj(top["source"], "manifest.source", ("logical_name", "system_identifier", "database_oid", "database_name",
                                                     "server_version_num", "pg_dump_version", "backup_role"))
        source = SourceInfo(
            logical_name=_str(s["logical_name"], "manifest.source.logical_name", pattern=_LOGICAL_NAME),
            system_identifier=_str(s["system_identifier"], "manifest.source.system_identifier", pattern=_SYSTEM_IDENTIFIER),
            database_oid=_int(s["database_oid"], "manifest.source.database_oid", minimum=0),
            database_name=_str(s["database_name"], "manifest.source.database_name", pattern=_IDENTIFIER),
            server_version_num=_int(s["server_version_num"], "manifest.source.server_version_num", minimum=1),
            pg_dump_version=_str(s["pg_dump_version"], "manifest.source.pg_dump_version", max_len=200),
            backup_role=_str(s["backup_role"], "manifest.source.backup_role", pattern=_IDENTIFIER),
        )
        sc = _obj(top["schema"], "manifest.schema", ("alembic_head", "expected_head", "heads_match"))
        schema = SchemaState(
            alembic_head=_str(sc["alembic_head"], "manifest.schema.alembic_head", pattern=_REVISION),
            expected_head=_str(sc["expected_head"], "manifest.schema.expected_head", pattern=_REVISION),
            heads_match=_bool(sc["heads_match"], "manifest.schema.heads_match"),
        )
        if schema.heads_match != (schema.alembic_head == schema.expected_head):
            _fail("FIELD_VALUE", "manifest.schema.heads_match: contradicts the two heads")
        r = _obj(top["repository"], "manifest.repository", ("git_revision", "dirty"))
        repository = RepositoryState(
            git_revision=_str(r["git_revision"], "manifest.repository.git_revision", pattern=_GIT_REVISION),
            dirty=_bool(r["dirty"], "manifest.repository.dirty"),
        )
        t = _obj(top["tooling"], "manifest.tooling", ("name", "version"))
        tooling = ToolingInfo(
            name=_str(t["name"], "manifest.tooling.name", pattern=_TOOL_NAME),
            version=_str(t["version"], "manifest.tooling.version", pattern=_TOOL_VERSION),
        )
        # /1 and /2 differ ONLY here: /1 carries the historical `catalog_bracket_equal` (the failed in-transaction bracket,
        # 12.5; never reinterpreted), /2 requires `catalog_observation` instead and does not allow the legacy field.
        evidence_key = "catalog_observation" if version_2 else "catalog_bracket_equal"
        c = _obj(top["consistency"], "manifest.consistency", ("mode", "snapshot_descriptor", "snapshot_unchanged_at_end", "sequence_rule",
                                                             "ddl_exclusion", evidence_key, "foreign_write_sessions_start",
                                                             "foreign_write_sessions_end"))
        common = dict(
            mode=_str(c["mode"], "manifest.consistency.mode", allowed=("exported_snapshot", "quiescent")),
            snapshot_descriptor=_str(c["snapshot_descriptor"], "manifest.consistency.snapshot_descriptor", nonempty=False, max_len=2048),
            snapshot_unchanged_at_end=_bool(c["snapshot_unchanged_at_end"], "manifest.consistency.snapshot_unchanged_at_end"),
            sequence_rule=_str(c["sequence_rule"], "manifest.consistency.sequence_rule", allowed=("monotonic_v1", "equal_v1")),
            ddl_exclusion=_str(c["ddl_exclusion"], "manifest.consistency.ddl_exclusion", allowed=("none", "locks")),
        )
        if version_2:
            samples = {key: (None if c[key] is None else _int(c[key], f"manifest.consistency.{key}", minimum=0))
                       for key in ("foreign_write_sessions_start", "foreign_write_sessions_end")}
            if (samples["foreign_write_sessions_start"] is None) != (samples["foreign_write_sessions_end"] is None):
                _fail("FIELD_VALUE", "manifest.consistency.foreign_write_sessions_*: both sampled or both null (not sampled), never one of each")
            consistency = ConsistencyClaimV2(
                **common, catalog_observation=CatalogObservationRecord.from_document(c["catalog_observation"]), **samples,
            )
        else:
            consistency = ConsistencyClaim(
                **common, catalog_bracket_equal=_bool(c["catalog_bracket_equal"], "manifest.consistency.catalog_bracket_equal"),
                foreign_write_sessions_start=_int(c["foreign_write_sessions_start"], "manifest.consistency.foreign_write_sessions_start", minimum=0),
                foreign_write_sessions_end=_int(c["foreign_write_sessions_end"], "manifest.consistency.foreign_write_sessions_end", minimum=0),
            )
        if consistency.mode == "exported_snapshot":
            if not _SNAPSHOT_DESCRIPTOR.fullmatch(consistency.snapshot_descriptor):
                _fail("FIELD_VALUE", "manifest.consistency.snapshot_descriptor: not xmin:xmax:xip-list text")
        elif consistency.snapshot_descriptor != "" or consistency.snapshot_unchanged_at_end:
            _fail("FIELD_VALUE", "manifest.consistency: quiescent mode records no snapshot")
        a = _obj(top["archive"], "manifest.archive", ("format", "toc_entries", "toc_sha256"))
        archive = ArchiveInfo(
            format=_str(a["format"], "manifest.archive.format", allowed=("custom",)),
            toc_entries=_int(a["toc_entries"], "manifest.archive.toc_entries", minimum=0),
            toc_sha256=_str(a["toc_sha256"], "manifest.archive.toc_sha256", pattern=_SHA256),
        )
        b = _obj(top["baseline"], "manifest.baseline", ("table_count", "row_total"))
        baseline = BaselineSummary(
            table_count=_int(b["table_count"], "manifest.baseline.table_count", minimum=0),
            row_total=_int(b["row_total"], "manifest.baseline.row_total", minimum=0),
        )
        files = _inventory(top["files"])
        if version_2:
            observed = consistency.catalog_observation.source
            for name in ("system_identifier", "database_oid", "database_name"):
                if getattr(observed, name) != getattr(source, name):
                    _fail("FIELD_VALUE", f"manifest.consistency.catalog_observation.source.{name}: differs from manifest.source.{name}")
        return cls(artifact_id=artifact_id, created_utc=top["created_utc"], finished_utc=top["finished_utc"], source=source, schema=schema,
                   repository=repository, tooling=tooling, consistency=consistency, archive=archive, baseline=baseline, files=files)

    def to_document(self) -> dict:
        version = self.format  # first: a consistency record of neither version is refused before any field is read
        consistency = {"mode": self.consistency.mode, "snapshot_descriptor": self.consistency.snapshot_descriptor,
                       "snapshot_unchanged_at_end": self.consistency.snapshot_unchanged_at_end,
                       "sequence_rule": self.consistency.sequence_rule, "ddl_exclusion": self.consistency.ddl_exclusion,
                       "foreign_write_sessions_start": self.consistency.foreign_write_sessions_start,
                       "foreign_write_sessions_end": self.consistency.foreign_write_sessions_end}
        if version == MANIFEST_FORMAT_V2:
            consistency["catalog_observation"] = self.consistency.catalog_observation.to_document()
        else:
            consistency["catalog_bracket_equal"] = self.consistency.catalog_bracket_equal
        return {
            "format": version,
            "artifact_id": self.artifact_id,
            "created_utc": self.created_utc,
            "finished_utc": self.finished_utc,
            "source": {"logical_name": self.source.logical_name, "system_identifier": self.source.system_identifier,
                       "database_oid": self.source.database_oid, "database_name": self.source.database_name,
                       "server_version_num": self.source.server_version_num, "pg_dump_version": self.source.pg_dump_version,
                       "backup_role": self.source.backup_role},
            "schema": {"alembic_head": self.schema.alembic_head, "expected_head": self.schema.expected_head, "heads_match": self.schema.heads_match},
            "repository": {"git_revision": self.repository.git_revision, "dirty": self.repository.dirty},
            "tooling": {"name": self.tooling.name, "version": self.tooling.version},
            "consistency": consistency,
            "archive": {"format": self.archive.format, "toc_entries": self.archive.toc_entries, "toc_sha256": self.archive.toc_sha256},
            "baseline": {"table_count": self.baseline.table_count, "row_total": self.baseline.row_total},
            "files": [{"path": f.path, "bytes": f.bytes, "sha256": f.sha256} for f in self.files],
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_document())


def _inventory(value: Any) -> tuple[FileEntry, FileEntry]:
    entries = _list(value, "manifest.files")
    if len(entries) != 2:
        _fail("INVENTORY_INVALID", f"manifest.files: expected exactly 2 entries, found {len(entries)}")
    parsed: list[FileEntry] = []
    for index, raw in enumerate(entries):
        where = f"manifest.files[{index}]"
        item = _obj(raw, where, ("path", "bytes", "sha256"))
        path = item["path"]
        if not isinstance(path, str):
            _fail("FIELD_TYPE", f"{where}.path: expected a string")
        if path not in INVENTORY_PATHS:
            unsafe = (not path or "/" in path or "\\" in path or ".." in path or ":" in path or "\x00" in path or path.startswith("."))
            _fail("UNSAFE_PATH" if unsafe else "INVENTORY_INVALID", f"{where}.path: {ascii(path)[:60]}")
        parsed.append(FileEntry(path=path, bytes=_int(item["bytes"], f"{where}.bytes", minimum=0),
                                sha256=_str(item["sha256"], f"{where}.sha256", pattern=_SHA256)))
    if [f.path for f in parsed] != list(INVENTORY_PATHS):
        _fail("INVENTORY_INVALID", "manifest.files: must list archive.dump then baseline.json exactly once each")
    if parsed[0].bytes < 1:
        _fail("FIELD_VALUE", "manifest.files[0].bytes: an empty archive is not a dump")
    return parsed[0], parsed[1]


def parse_manifest(data: bytes) -> ArtifactManifest:
    """Strict, canonical-form manifest parsing. Raises ArtifactFormatError."""
    return ArtifactManifest.from_document(_parse_canonical(data, max_bytes=MAX_MANIFEST_BYTES))


# ---------------------------------------------------------------------------
# baseline document
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FingerprintEntry:
    table: str
    rows: int
    md5: str


@dataclass(frozen=True)
class ConstraintEntry:
    table: str
    name: str
    contype: str
    validated: bool
    definition: str


@dataclass(frozen=True)
class IndexEntry:
    table: str
    name: str
    definition: str


@dataclass(frozen=True)
class AclEntry:
    relation: str
    relkind: str
    owner: str
    acl: str


@dataclass(frozen=True)
class SequenceEntry:
    name: str
    last_value: int | None


@dataclass(frozen=True)
class SequenceDetail:
    name: str
    owned_by: tuple[str, str] | None
    data_type: str
    increment: int
    minimum: int
    maximum: int
    cache: int
    cycle: bool
    last_value: int | None
    is_called: bool
    column_max: int | None


@dataclass(frozen=True)
class BaselineDocument:
    """
    The existing `DatabaseBaseline` (tests/postgres/pg_restore_compare.py) field
    for field under the same names, plus `sequence_details`. This module defines
    only the wire shape and its validation; it contains no capture or comparison
    code, and values are stored exactly as captured with no normalization.
    """

    artifact_id: str
    captured_utc: str
    fingerprints: tuple[FingerprintEntry, ...]
    constraints: tuple[ConstraintEntry, ...]
    indexes: tuple[IndexEntry, ...]
    acls: tuple[AclEntry, ...]
    sequences: tuple[SequenceEntry, ...]
    version: str | None
    max_alias_id: int | None
    sequence_details: tuple[SequenceDetail, ...]

    @classmethod
    def from_document(cls, document: Any) -> "BaselineDocument":
        _check_format(document, BASELINE_FORMAT, _BASELINE_FAMILY, "baseline")
        top = _obj(document, "baseline", ("format", "artifact_id", "captured_utc", "fingerprints", "constraints", "indexes", "acls",
                                           "sequences", "version", "max_alias_id", "sequence_details"))
        artifact_id = _str(top["artifact_id"], "baseline.artifact_id", pattern=_UUID4)
        _timestamp(top["captured_utc"], "baseline.captured_utc")

        fingerprints: list[FingerprintEntry] = []
        for i, raw in enumerate(_list(top["fingerprints"], "baseline.fingerprints")):
            row = _fixed_list(raw, f"baseline.fingerprints[{i}]", 3)
            table = _str(row[0], f"baseline.fingerprints[{i}][0]")
            rows = _int(row[1], f"baseline.fingerprints[{i}][1]", minimum=0)
            md5 = _str(row[2], f"baseline.fingerprints[{i}][2]")
            if md5 == "empty":
                if rows != 0:
                    _fail("FIELD_VALUE", f"baseline.fingerprints[{i}][2]: 'empty' is only valid for a table with no rows")
            elif not _MD5.fullmatch(md5) or rows == 0:
                _fail("FIELD_VALUE", f"baseline.fingerprints[{i}][2]: expected 32 lower-case hex digits for a non-empty table, or 'empty' for none")
            fingerprints.append(FingerprintEntry(table, rows, md5))
        _unique([f.table for f in fingerprints], "baseline.fingerprints")
        if [f.table for f in fingerprints] != sorted(f.table for f in fingerprints):
            _fail("BASELINE_ORDER", "baseline.fingerprints: must be sorted by table name (the existing field is a dictionary)")

        constraints: list[ConstraintEntry] = []
        for i, raw in enumerate(_list(top["constraints"], "baseline.constraints")):
            row = _fixed_list(raw, f"baseline.constraints[{i}]", 5)
            constraints.append(ConstraintEntry(
                _str(row[0], f"baseline.constraints[{i}][0]"), _str(row[1], f"baseline.constraints[{i}][1]"),
                _str(row[2], f"baseline.constraints[{i}][2]", max_len=1), _bool(row[3], f"baseline.constraints[{i}][3]"),
                _str(row[4], f"baseline.constraints[{i}][4]")))
        _unique([(c.table, c.name) for c in constraints], "baseline.constraints")

        indexes: list[IndexEntry] = []
        for i, raw in enumerate(_list(top["indexes"], "baseline.indexes")):
            row = _fixed_list(raw, f"baseline.indexes[{i}]", 3)
            indexes.append(IndexEntry(_str(row[0], f"baseline.indexes[{i}][0]"), _str(row[1], f"baseline.indexes[{i}][1]"),
                                      _str(row[2], f"baseline.indexes[{i}][2]")))
        _unique([(x.table, x.name) for x in indexes], "baseline.indexes")

        acls: list[AclEntry] = []
        for i, raw in enumerate(_list(top["acls"], "baseline.acls")):
            row = _fixed_list(raw, f"baseline.acls[{i}]", 4)
            acls.append(AclEntry(_str(row[0], f"baseline.acls[{i}][0]"), _str(row[1], f"baseline.acls[{i}][1]", max_len=1),
                                 _str(row[2], f"baseline.acls[{i}][2]"), _str(row[3], f"baseline.acls[{i}][3]", nonempty=False)))
        _unique([a.relation for a in acls], "baseline.acls")

        sequences: list[SequenceEntry] = []
        for i, raw in enumerate(_list(top["sequences"], "baseline.sequences")):
            row = _fixed_list(raw, f"baseline.sequences[{i}]", 2)
            sequences.append(SequenceEntry(_str(row[0], f"baseline.sequences[{i}][0]"), _int_or_null(row[1], f"baseline.sequences[{i}][1]")))
        _unique([s.name for s in sequences], "baseline.sequences")

        version = top["version"]
        if version is not None:
            version = _str(version, "baseline.version", pattern=_REVISION)
        max_alias_id = _int_or_null(top["max_alias_id"], "baseline.max_alias_id")

        details: list[SequenceDetail] = []
        for i, raw in enumerate(_list(top["sequence_details"], "baseline.sequence_details")):
            where = f"baseline.sequence_details[{i}]"
            d = _obj(raw, where, ("name", "owned_by", "data_type", "increment", "minimum", "maximum", "cache", "cycle", "last_value",
                                  "is_called", "column_max"))
            owned = d["owned_by"]
            owned_by: tuple[str, str] | None = None
            if owned is not None:
                pair = _fixed_list(owned, f"{where}.owned_by", 2)
                owned_by = (_str(pair[0], f"{where}.owned_by[0]"), _str(pair[1], f"{where}.owned_by[1]"))
            details.append(SequenceDetail(
                name=_str(d["name"], f"{where}.name"), owned_by=owned_by,
                data_type=_str(d["data_type"], f"{where}.data_type", allowed=("smallint", "integer", "bigint")),
                increment=_int(d["increment"], f"{where}.increment"), minimum=_int(d["minimum"], f"{where}.minimum"),
                maximum=_int(d["maximum"], f"{where}.maximum"), cache=_int(d["cache"], f"{where}.cache", minimum=1),
                cycle=_bool(d["cycle"], f"{where}.cycle"), last_value=_int_or_null(d["last_value"], f"{where}.last_value"),
                is_called=_bool(d["is_called"], f"{where}.is_called"), column_max=_int_or_null(d["column_max"], f"{where}.column_max")))
        _unique([d.name for d in details], "baseline.sequence_details")
        if {d.name for d in details} != {s.name for s in sequences}:
            _fail("BASELINE_SEQUENCE_MISMATCH", "baseline.sequence_details names differ from baseline.sequences")
        return cls(artifact_id=artifact_id, captured_utc=top["captured_utc"], fingerprints=tuple(fingerprints), constraints=tuple(constraints),
                   indexes=tuple(indexes), acls=tuple(acls), sequences=tuple(sequences), version=version, max_alias_id=max_alias_id,
                   sequence_details=tuple(details))

    def to_document(self) -> dict:
        return {
            "format": BASELINE_FORMAT,
            "artifact_id": self.artifact_id,
            "captured_utc": self.captured_utc,
            "fingerprints": [[f.table, f.rows, f.md5] for f in self.fingerprints],
            "constraints": [[c.table, c.name, c.contype, c.validated, c.definition] for c in self.constraints],
            "indexes": [[x.table, x.name, x.definition] for x in self.indexes],
            "acls": [[a.relation, a.relkind, a.owner, a.acl] for a in self.acls],
            "sequences": [[s.name, s.last_value] for s in self.sequences],
            "version": self.version,
            "max_alias_id": self.max_alias_id,
            "sequence_details": [
                {"name": d.name, "owned_by": None if d.owned_by is None else list(d.owned_by), "data_type": d.data_type,
                 "increment": d.increment, "minimum": d.minimum, "maximum": d.maximum, "cache": d.cache, "cycle": d.cycle,
                 "last_value": d.last_value, "is_called": d.is_called, "column_max": d.column_max}
                for d in self.sequence_details],
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_document())


def _fixed_list(value: Any, path: str, length: int) -> list:
    items = _list(value, path)
    if len(items) != length:
        _fail("FIELD_VALUE", f"{path}: expected {length} elements, found {len(items)}")
    return items


def _unique(keys: Sequence[Any], path: str) -> None:
    seen: set = set()
    for key in keys:
        if key in seen:
            _fail("BASELINE_DUPLICATE_KEY", f"{path}: {ascii(key)[:80]}")
        seen.add(key)


def parse_baseline(data: bytes) -> BaselineDocument:
    """Strict, canonical-form baseline parsing. Raises ArtifactFormatError."""
    return BaselineDocument.from_document(_parse_canonical(data, max_bytes=MAX_BASELINE_BYTES))


# ---------------------------------------------------------------------------
# completion marker (parsing only)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CompletionMarker:
    artifact_id: str
    manifest_sha256: str
    manifest_bytes: int


def parse_completion_marker(data: bytes) -> CompletionMarker:
    """Parses exactly four LF-terminated ASCII lines. Raises ArtifactFormatError."""
    if len(data) > MAX_MARKER_BYTES:
        _fail("MARKER_MALFORMED", "longer than the marker size limit")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        _fail("MARKER_MALFORMED", "not ASCII")
    if not text.endswith("\n") or "\r" in text:
        _fail("MARKER_MALFORMED", "lines must be LF-terminated, with a final LF and no CR")
    lines = text[:-1].split("\n")
    if lines and lines[0].startswith(_MARKER_FAMILY) and lines[0] != MARKER_FORMAT:
        _fail("MARKER_UNSUPPORTED_VERSION", ascii(lines[0])[:60])
    if len(lines) != 4 or lines[0] != MARKER_FORMAT:
        _fail("MARKER_MALFORMED", "expected the format line and three field lines")
    if not lines[1].startswith("artifact_id=") or not _UUID4.fullmatch(lines[1][len("artifact_id="):]):
        _fail("MARKER_MALFORMED", "line 2 must be artifact_id=<uuid4>")
    if not lines[2].startswith("manifest_sha256=") or not _SHA256.fullmatch(lines[2][len("manifest_sha256="):]):
        _fail("MARKER_MALFORMED", "line 3 must be manifest_sha256=<64 lower-case hex>")
    size_text = lines[3][len("manifest_bytes="):] if lines[3].startswith("manifest_bytes=") else ""
    if not re.fullmatch(r"[1-9][0-9]{0,18}", size_text) or int(size_text) > INT64_MAX:
        _fail("MARKER_MALFORMED", "line 4 must be manifest_bytes=<positive decimal without leading zeros>")
    return CompletionMarker(artifact_id=lines[1][len("artifact_id="):], manifest_sha256=lines[2][len("manifest_sha256="):],
                            manifest_bytes=int(size_text))


# ---------------------------------------------------------------------------
# directory names
# ---------------------------------------------------------------------------
_DIRECTORY_NAME = re.compile(r"napsedge-([a-z0-9_]{1,40})-([0-9]{8}T[0-9]{6}Z)-([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})")


@dataclass(frozen=True)
class ArtifactName:
    logical_name: str
    timestamp: str  # compact UTC, e.g. 20261002T183000Z
    artifact_id: str


def parse_artifact_directory_name(name: str) -> ArtifactName:
    match = _DIRECTORY_NAME.fullmatch(name)
    if not match:
        _fail("NAME_INVALID", ascii(name)[:80])
    try:
        _dt.datetime.strptime(match.group(2), "%Y%m%dT%H%M%SZ")
    except ValueError:
        _fail("NAME_INVALID", "the timestamp in the name is not a real calendar instant")
    return ArtifactName(match.group(1), match.group(2), match.group(3))


# ---------------------------------------------------------------------------
# the verifier
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Reason:
    code: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}" if self.detail else self.code


@dataclass(frozen=True)
class ExpectedIdentity:
    """Optional caller expectations; any field left as None is not checked."""

    system_identifier: str | None = None
    database_name: str | None = None
    alembic_head: str | None = None


@dataclass(frozen=True)
class Verdict:
    """
    `status` is "STRUCTURALLY_VALID" or "INVALID". A valid verdict carries the
    parsed documents; an invalid one carries every reason found and no documents.
    `establishes` and `does_not_establish` are part of the verdict on purpose.
    """

    status: str
    reasons: tuple[Reason, ...] = ()
    manifest: ArtifactManifest | None = None
    baseline: BaselineDocument | None = None
    establishes: tuple[str, ...] = ESTABLISHES
    does_not_establish: tuple[str, ...] = DOES_NOT_ESTABLISH

    @property
    def valid(self) -> bool:
        return self.status == "STRUCTURALLY_VALID"

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(reason.code for reason in self.reasons)


def _is_link(result: os.stat_result) -> bool:
    return stat.S_ISLNK(result.st_mode) or bool(getattr(result, "st_file_attributes", 0) & _REPARSE_POINT)


def _path_anchor_and_parts(text: str) -> tuple[str, list[str]] | None:
    """
    Splits a path into (anchor, raw components) WITHOUT normalizing anything. The
    anchor is "" for a relative path. Returns None for a Windows drive-relative
    path (`C:name`), which has no meaning without a per-drive working directory.
    """
    drive, rest = os.path.splitdrive(text)
    separators = {os.sep} | ({os.altsep} if os.altsep else set())
    absolute = bool(rest) and rest[0] in separators
    if drive and not absolute:
        return None
    pattern = "[" + "".join(re.escape(s) for s in sorted(separators)) + "]"
    return (drive + os.sep if absolute else ""), re.split(pattern, rest)


def _inspect_artifact_path(path: str | os.PathLike) -> tuple[str, str, Reason | None]:
    """
    Validates the path to the artifact directory component by component and
    returns (safe_path, directory_name, failure).

    Why this exists. Handing the caller's string to `lstat` is not enough: on
    POSIX a trailing separator or a trailing `.` component makes the kernel
    resolve a symbolic link, so `lstat("link/")` describes the link's TARGET and a
    link named like an artifact would be accepted. Nothing is normalized or
    resolved before it is validated: every component is pushed and `lstat`-ed on
    its own (never with a trailing separator), and the resulting path is rebuilt
    from the validated components only. Rules:
    * empty and `.` components are dropped (they name the same directory);
    * `..` pops the previously validated component, which is only sound because
      every pushed component was already confirmed to be a real directory; a link
      that is later popped is still rejected, conservatively;
    * a relative path is anchored at the current directory and that directory's
      own components are validated too;
    * ANY component that is a symbolic link or reparse point, the artifact
      directory or any ancestor, is rejected with SYMLINK (reparse points on
      Windows are detected through the file attributes);
    * a missing component, or an ancestor that is not a directory, is NOT_A_DIRECTORY.
    Limits, stated plainly: this is a check, not a lock. A component replaced by a
    link after its `lstat` and before the later reads is not prevented, and
    `O_NOFOLLOW` (where it exists) protects only the final component of each file
    opened. An environment that legitimately keeps the artifact store behind a link
    (for example a symlinked temporary directory) is rejected as well; that needs an
    explicit, separately decided trusted-root mechanism, not silent resolution.
    """
    text = os.fspath(path)
    if not isinstance(text, str):
        raise TypeError("the artifact path must be a str or a path-like object that yields a str")
    if "\x00" in text:
        return "", "", Reason("NOT_A_DIRECTORY", "the path contains a NUL character")
    split = _path_anchor_and_parts(text)
    if split is None:
        return "", "", Reason("NOT_A_DIRECTORY", "drive-relative paths are not supported")
    anchor, parts = split
    if anchor == "":
        base = _path_anchor_and_parts(os.getcwd())
        if base is None:
            return "", "", Reason("NOT_A_DIRECTORY", "the current directory has no usable absolute form")
        anchor, base_parts = base
        parts = base_parts + parts
    significant = [i for i, part in enumerate(parts) if part not in ("", ".")]
    last = significant[-1] if significant else -1
    stack: list[str] = []
    for index, component in enumerate(parts):
        if component in ("", "."):
            continue
        if component == "..":
            if stack:
                stack.pop()
            continue
        stack.append(component)
        role = "artifact directory" if index == last else "ancestor"
        try:
            found = os.lstat(anchor + os.sep.join(stack))
        except OSError as exc:
            return "", "", Reason("NOT_A_DIRECTORY", f"{role} {ascii(component)[:80]} cannot be inspected ({type(exc).__name__})")
        if _is_link(found):
            return "", "", Reason("SYMLINK", f"{role} {ascii(component)[:80]}")
        if not stat.S_ISDIR(found.st_mode):
            return "", "", Reason("NOT_A_DIRECTORY", f"{role} {ascii(component)[:80]} is not a directory")
    if not stack:
        return "", "", Reason("NAME_INVALID", "the path names a filesystem root")
    return anchor + os.sep.join(stack), stack[-1], None


def _read_file(path: str, *, keep_limit: int | None) -> tuple[int, str, bytes | None]:
    """
    Streams a regular file once, returning (size, sha256, content). Content is
    kept only when `keep_limit` is given and the file is not larger; a file larger
    than the limit raises FILE_TOO_LARGE after at most limit+1 bytes were kept.
    The hash and the returned bytes come from the SAME read, so what is parsed is
    exactly what was hashed.
    """
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError("not a regular file")
        digest = hashlib.sha256()
        total = 0
        kept = bytearray() if keep_limit is not None else None
        while True:
            chunk = handle.read(HASH_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
            if kept is not None:
                if total > keep_limit:
                    _fail("FILE_TOO_LARGE", f"more than {keep_limit} bytes")
                kept.extend(chunk)
    return total, digest.hexdigest(), (bytes(kept) if kept is not None else None)


def _manifest_baseline_relations(manifest: ArtifactManifest, baseline: BaselineDocument) -> list[Reason]:
    """
    The cross-document relations between a manifest and its baseline, as a pure
    function of the two parsed documents (no filesystem). Shared by the verifier
    and by the writer's pre-write validation, so there is exactly one definition.
    """
    reasons: list[Reason] = []
    if baseline.artifact_id != manifest.artifact_id:
        reasons.append(Reason("BASELINE_ARTIFACT_MISMATCH", "baseline and manifest name different artifacts"))
    if baseline.version != manifest.schema.alembic_head:
        # Both documents are internally well-formed, but they describe different schema provenance. A standalone baseline may
        # carry a null version; a FINALIZED artifact whose manifest names a concrete head may not. `expected_head` is
        # deliberately not compared: a head that differs from what the repository expects is a fact the manifest records, not
        # a structural defect.
        reasons.append(Reason("BASELINE_SCHEMA_VERSION_MISMATCH",
                              f"baseline.version {ascii(baseline.version)[:40]} vs manifest schema.alembic_head {ascii(manifest.schema.alembic_head)[:40]}"))
    if len(baseline.fingerprints) != manifest.baseline.table_count or sum(f.rows for f in baseline.fingerprints) != manifest.baseline.row_total:
        reasons.append(Reason("BASELINE_SUMMARY_MISMATCH", "table_count or row_total differs from the baseline document"))
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
    captured = _dt.datetime.strptime(baseline.captured_utc, fmt)
    if not (_dt.datetime.strptime(manifest.created_utc, fmt) <= captured <= _dt.datetime.strptime(manifest.finished_utc, fmt)):
        reasons.append(Reason("BASELINE_TIME_OUTSIDE_CAPTURE", "captured_utc is outside created_utc..finished_utc"))
    return reasons


def verify_artifact_directory(path: str | os.PathLike, expected: ExpectedIdentity | None = None) -> Verdict:
    """
    Read-only structural and integrity verification of one artifact directory.

    Never creates, modifies, repairs, renames or adopts anything; rejects a
    symbolic link or reparse point as the artifact directory, as ANY ancestor of
    it, or as an entry inside it, validating each path component before using it
    (see `_inspect_artifact_path` for the exact rules and their limits); never
    opens a path taken from file content;
    applies size limits before loading any JSON; hashes files by streaming. It
    reports every problem it can find without trusting unauthenticated content:
    the manifest is parsed only if the marker binds it, and the baseline only if
    the manifest's inventory authenticates it.
    """
    root, name, failure = _inspect_artifact_path(path)
    if failure is not None:
        return Verdict(status="INVALID", reasons=(Reason(failure.code, failure.detail),))
    if name.startswith(".partial-"):
        return Verdict(status="INVALID", reasons=(Reason("NOT_FINAL", ascii(name)[:80]),))
    return _verify_contents(root, name, expected)


def _verify_contents(root: str, name: str, expected: ExpectedIdentity | None) -> Verdict:
    """
    Steps 2 to 8 of the verifier on an already path-validated directory.

    `root` is where the files are NOW; `name` is the directory name the artifact
    has, or will have once published, and is what the identity checks compare with
    the manifest. The public verifier calls this for a published directory. The
    writer calls it for its own staging directory with the FINAL name, so staged
    contents are judged by exactly the rules a consumer will apply, while the
    public entry point keeps refusing `.partial-*` directories (it never reaches
    this function for them).
    """
    reasons: list[Reason] = []

    def add(code: str, detail: str = "") -> None:
        assert code in REASON_CODES, code
        reasons.append(Reason(code, detail))

    def invalid() -> Verdict:
        return Verdict(status="INVALID", reasons=tuple(reasons))

    parsed_name: ArtifactName | None = None
    try:
        parsed_name = parse_artifact_directory_name(name)
    except ArtifactFormatError as exc:
        add(exc.code, exc.detail)

    # -- 2. the exact file set --------------------------------------------------
    try:
        entries = {entry.name: entry.stat(follow_symlinks=False) for entry in os.scandir(root)}
    except OSError as exc:
        add("NOT_A_DIRECTORY", f"cannot list the directory ({type(exc).__name__})")
        return invalid()
    usable: dict[str, str] = {}  # required name -> path of a regular, non-link file
    for required in ARTIFACT_FILES:
        entry = entries.get(required)
        if entry is None:
            add("MISSING_FILE", required)
        elif _is_link(entry):
            add("SYMLINK", required)
        elif not stat.S_ISREG(entry.st_mode):
            add("NOT_A_REGULAR_FILE", required)
        else:
            usable[required] = os.path.join(root, required)
    has_complete = MARKER_FILE in entries
    for marker_name, code in ((RUNNING_FILE, "ARTIFACT_RUNNING"), (FAILED_FILE, "ARTIFACT_FAILED")):
        if marker_name in entries:
            add("CONFLICTING_MARKERS" if has_complete else code, marker_name)
    for extra in sorted(set(entries) - set(ARTIFACT_FILES) - {RUNNING_FILE, FAILED_FILE}):
        if _is_link(entries[extra]):
            add("SYMLINK", ascii(extra)[:80])
        else:
            add("UNLISTED_FILE", ascii(extra)[:80])

    # -- 3. the completion marker -------------------------------------------------
    marker: CompletionMarker | None = None
    if MARKER_FILE in usable:
        try:
            if entries[MARKER_FILE].st_size > MAX_MARKER_BYTES:
                raise ArtifactFormatError("MARKER_MALFORMED", "longer than the marker size limit")
            _, _, marker_bytes = _read_file(usable[MARKER_FILE], keep_limit=MAX_MARKER_BYTES)
            marker = parse_completion_marker(marker_bytes or b"")
        except ArtifactFormatError as exc:
            add("MARKER_MALFORMED" if exc.code == "FILE_TOO_LARGE" else exc.code, exc.detail)
        except OSError as exc:
            add("UNREADABLE_FILE", f"{MARKER_FILE} ({type(exc).__name__})")

    # -- 4. the manifest, authenticated by the marker --------------------------------
    manifest: ArtifactManifest | None = None
    if MANIFEST_FILE in usable:
        if entries[MANIFEST_FILE].st_size > MAX_MANIFEST_BYTES:
            add("FILE_TOO_LARGE", MANIFEST_FILE)
        else:
            try:
                size, digest, manifest_bytes = _read_file(usable[MANIFEST_FILE], keep_limit=MAX_MANIFEST_BYTES)
            except ArtifactFormatError as exc:
                add(exc.code, MANIFEST_FILE)
            except OSError as exc:
                add("UNREADABLE_FILE", f"{MANIFEST_FILE} ({type(exc).__name__})")
            else:
                bound = marker is not None
                if marker is not None:
                    if size != marker.manifest_bytes:
                        add("MARKER_SIZE_MISMATCH", f"manifest has {size} bytes, marker says {marker.manifest_bytes}")
                        bound = False
                    if digest != marker.manifest_sha256:
                        add("MARKER_DIGEST_MISMATCH", MANIFEST_FILE)
                        bound = False
                if bound:
                    try:
                        manifest = parse_manifest(manifest_bytes or b"")
                    except ArtifactFormatError as exc:
                        add(exc.code, f"{MANIFEST_FILE}: {exc.detail}")

    # -- 5. identities that must agree --------------------------------------------------
    if manifest is not None:
        if marker is not None and marker.artifact_id != manifest.artifact_id:
            add("MARKER_ARTIFACT_MISMATCH", "marker and manifest name different artifacts")
        if parsed_name is not None:
            if parsed_name.artifact_id != manifest.artifact_id:
                add("NAME_MISMATCH", "artifact_id")
            if parsed_name.logical_name != manifest.source.logical_name:
                add("NAME_MISMATCH", "logical_name")
            created = _dt.datetime.strptime(manifest.created_utc, "%Y-%m-%dT%H:%M:%S.%fZ").strftime("%Y%m%dT%H%M%SZ")
            if parsed_name.timestamp != created:
                add("NAME_MISMATCH", "created_utc")

    # -- 6. the inventory: archive and baseline sizes and digests ------------------------
    baseline_bytes: bytes | None = None
    if manifest is not None:
        for entry in manifest.files:
            if entry.path not in usable:
                continue  # already reported as missing, a link or not a regular file
            file_path = usable[entry.path]
            is_baseline = entry.path == BASELINE_FILE
            if is_baseline and entries[BASELINE_FILE].st_size > MAX_BASELINE_BYTES:
                add("FILE_TOO_LARGE", BASELINE_FILE)
                continue
            try:
                size, digest, content = _read_file(file_path, keep_limit=MAX_BASELINE_BYTES if is_baseline else None)
            except ArtifactFormatError as exc:
                add(exc.code, entry.path)
                continue
            except OSError as exc:
                add("UNREADABLE_FILE", f"{entry.path} ({type(exc).__name__})")
                continue
            if size != entry.bytes:
                add("FILE_SIZE_MISMATCH", f"{entry.path}: {size} bytes, inventory says {entry.bytes}")
                continue
            if digest != entry.sha256:
                add("FILE_DIGEST_MISMATCH", entry.path)
                continue
            if is_baseline:
                baseline_bytes = content

    # -- 7. the baseline, authenticated by the manifest's inventory ---------------------------
    baseline: BaselineDocument | None = None
    if manifest is not None and baseline_bytes is not None:
        try:
            baseline = parse_baseline(baseline_bytes)
        except ArtifactFormatError as exc:
            add(exc.code, f"{BASELINE_FILE}: {exc.detail}")
        if baseline is not None:
            reasons.extend(_manifest_baseline_relations(manifest, baseline))

    # -- 8. the caller's expectations ------------------------------------------------------------
    if manifest is not None and expected is not None:
        if expected.system_identifier is not None and manifest.source.system_identifier != expected.system_identifier:
            add("IDENTITY_MISMATCH", "system_identifier")
        if expected.database_name is not None and manifest.source.database_name != expected.database_name:
            add("IDENTITY_MISMATCH", "database_name")
        if expected.alembic_head is not None and manifest.schema.alembic_head != expected.alembic_head:
            add("SCHEMA_HEAD_MISMATCH", "alembic_head")

    if reasons:
        return invalid()
    return Verdict(status="STRUCTURALLY_VALID", manifest=manifest, baseline=baseline)


__all__ = [
    "ARCHIVE_FILE", "ARTIFACT_FILES", "BASELINE_FILE", "BASELINE_FORMAT", "DOES_NOT_ESTABLISH", "ESTABLISHES", "FAILED_FILE",
    "INVENTORY_PATHS", "MANIFEST_FILE", "MANIFEST_FORMAT", "MANIFEST_FORMAT_V2", "MANIFEST_FORMATS", "MARKER_FILE", "MARKER_FORMAT", "REASON_CODES", "RUNNING_FILE",
    "AclEntry", "ArchiveInfo", "ArtifactFormatError", "ArtifactManifest", "ArtifactName", "BaselineDocument", "BaselineSummary",
    "CATALOG_ENDPOINT_CLEANUP", "CATALOG_ENDPOINT_ISOLATION", "CATALOG_ENDPOINT_LABELS", "CATALOG_ENDPOINT_READ_ONLY",
    "CATALOG_OBSERVATION_CATEGORIES", "CATALOG_OBSERVATION_METHOD", "CATALOG_OBSERVATION_RESULT", "CatalogDigests", "CatalogEndpointRecord",
    "CatalogObservationRecord", "CompletionMarker", "ConsistencyClaim", "ConsistencyClaimV2", "ConstraintEntry", "ExpectedIdentity", "FileEntry", "FingerprintEntry", "IndexEntry",
    "ObservedSource", "Reason", "RepositoryState", "SchemaState", "SequenceDetail", "SequenceEntry", "SourceInfo", "ToolingInfo", "Verdict",
    "canonical_json_bytes", "parse_artifact_directory_name", "parse_baseline", "parse_completion_marker", "parse_manifest",
    "parse_strict_json", "verify_artifact_directory",
]
