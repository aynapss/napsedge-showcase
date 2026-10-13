"""
Phase 9B-5A: tests for the backup artifact file contract and its read-only
verifier (`src/deployment/backup_artifact.py`).

Everything is synthetic and needs no database, Docker, subprocess output or
network. Artifact directories are built on disk with genuinely computed SHA-256
digests and sizes; a builder in THIS file (not the production module, which has
no writer) writes them, and it serializes with an independent oracle
(`json.dumps` with the documented parameters) so the production serializer is
checked against something other than itself.

Expected outcomes are stated explicitly for every case. Where a case documents a
LIMIT of the verifier (it accepts something that is not evidence of consistency,
restorability or authenticity) the test says so and asserts the verdict's own
`does_not_establish` field, rather than pretending the check is stronger.
"""
from __future__ import annotations

import ast
import copy
import dataclasses
import hashlib
import json
import os
import stat
import subprocess
import sys
import tracemalloc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest

import src.deployment.backup_artifact as ba
from src.deployment.backup_artifact import (
    ArtifactFormatError,
    BaselineDocument,
    ExpectedIdentity,
    SequenceDetail,
    canonical_json_bytes,
    parse_artifact_directory_name,
    parse_baseline,
    parse_completion_marker,
    parse_manifest,
    parse_strict_json,
    verify_artifact_directory,
)
from tests.postgres.baseline_document import baseline_from_document, baseline_to_document, schema_field_drift
from tests.postgres.pg_restore_compare import DatabaseBaseline

REPO_ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_ID = "3f2b8c1e-6a47-4d0e-9b53-1c7a2d9e5f08"
OTHER_ID = "9a1d3e7f-2c58-4b61-8f04-5d7e6a1b2c93"
CREATED = "2026-10-02T18:30:00.000000Z"
CAPTURED = "2026-10-02T18:30:00.210344Z"
FINISHED = "2026-10-02T18:30:03.482911Z"
LOGICAL = "napsedge"
DIR_NAME = f"napsedge-{LOGICAL}-20261002T183000Z-{ARTIFACT_ID}"
ARCHIVE = b"PGDMP synthetic archive bytes (the verifier never parses a dump)\n" * 64


# --------------------------------------------------------------------------- independent oracle and builders
def oracle(document) -> bytes:
    """The documented canonical form, written independently of the production serializer."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def baseline_doc() -> dict:
    """Realistic shapes (see tests/postgres/baseline_document.py): an empty table, nulls, a called and a not-called sequence."""
    return {
        "format": ba.BASELINE_FORMAT,
        "artifact_id": ARTIFACT_ID,
        "captured_utc": CAPTURED,
        "fingerprints": [["alembic_version", 1, "0d9f4c1a7b6e2f8d3a5c9e1b4f7a2d60"], ["team_aliases", 3, "5be0c3a91d2e47f6a8b0c1d2e3f40516"], ["teams", 0, "empty"]],
        "constraints": [["team_aliases", "pk_team_aliases", "p", True, "PRIMARY KEY (id)"], ["teams", "pk_teams", "p", True, "PRIMARY KEY (team_id)"]],
        "indexes": [["teams", "pk_teams", "CREATE UNIQUE INDEX pk_teams ON public.teams USING btree (team_id)"]],
        "acls": [["teams", "r", "napsedge_migrator", "napsedge_migrator=arwdDxt/napsedge_migrator"], ["team_aliases_id_seq", "S", "napsedge_migrator", ""]],
        "sequences": [["team_aliases_id_seq", 3], ["teams_unused_seq", None]],
        "version": "5b02bea0eb85",
        "max_alias_id": 3,
        "sequence_details": [
            {"name": "team_aliases_id_seq", "owned_by": ["team_aliases", "id"], "data_type": "bigint", "increment": 1, "minimum": 1,
             "maximum": 9223372036854775807, "cache": 1, "cycle": False, "last_value": 3, "is_called": True, "column_max": 3},
            {"name": "teams_unused_seq", "owned_by": None, "data_type": "bigint", "increment": 1, "minimum": 1,
             "maximum": 9223372036854775807, "cache": 1, "cycle": False, "last_value": None, "is_called": False, "column_max": None},
        ],
    }


def manifest_doc(archive: bytes = ARCHIVE, baseline_bytes: bytes | None = None, *, bdoc: dict | None = None) -> dict:
    bdoc = bdoc if bdoc is not None else baseline_doc()
    baseline_bytes = baseline_bytes if baseline_bytes is not None else oracle(bdoc)
    return {
        "format": ba.MANIFEST_FORMAT,
        "artifact_id": ARTIFACT_ID,
        "created_utc": CREATED,
        "finished_utc": FINISHED,
        "source": {"logical_name": LOGICAL, "system_identifier": "7550123456789012345", "database_oid": 16384, "database_name": "napsedge",
                   "server_version_num": 160004, "pg_dump_version": "pg_dump (PostgreSQL) 16.4", "backup_role": "napsedge_backup_ro"},
        "schema": {"alembic_head": "5b02bea0eb85", "expected_head": "5b02bea0eb85", "heads_match": True},
        "repository": {"git_revision": "373b35452bb47f71d5f85cbca1e0624a9e4139f3", "dirty": False},
        "tooling": {"name": "napsedge-backup", "version": "0.0.0-test"},
        "consistency": {"mode": "exported_snapshot", "snapshot_descriptor": "1042:1042:", "snapshot_unchanged_at_end": True,
                        "sequence_rule": "monotonic_v1", "ddl_exclusion": "none", "catalog_bracket_equal": True,
                        "foreign_write_sessions_start": 0, "foreign_write_sessions_end": 0},
        "archive": {"format": "custom", "toc_entries": 187, "toc_sha256": sha256(b"toc")},
        "baseline": {"table_count": len(bdoc["fingerprints"]), "row_total": sum(row[1] for row in bdoc["fingerprints"])},
        "files": [{"path": "archive.dump", "bytes": len(archive), "sha256": sha256(archive)},
                  {"path": "baseline.json", "bytes": len(baseline_bytes), "sha256": sha256(baseline_bytes)}],
    }


def marker_text(manifest_bytes: bytes, artifact_id: str = ARTIFACT_ID) -> bytes:
    return f"{ba.MARKER_FORMAT}\nartifact_id={artifact_id}\nmanifest_sha256={sha256(manifest_bytes)}\nmanifest_bytes={len(manifest_bytes)}\n".encode()


@dataclass
class Parts:
    """Everything one artifact directory contains, as editable pieces. `render()` computes marker bytes from the manifest bytes actually written."""

    archive: bytes = ARCHIVE
    bdoc: dict = field(default_factory=baseline_doc)
    mdoc: dict | None = None
    baseline_bytes: bytes | None = None   # raw override
    manifest_bytes: bytes | None = None   # raw override
    marker: bytes | None = None           # raw override
    name: str = DIR_NAME

    def __post_init__(self) -> None:
        if self.mdoc is None:
            self.mdoc = manifest_doc(self.archive, bdoc=self.bdoc)

    def render(self) -> dict[str, bytes]:
        baseline_bytes = self.baseline_bytes if self.baseline_bytes is not None else oracle(self.bdoc)
        manifest_bytes = self.manifest_bytes if self.manifest_bytes is not None else oracle(self.mdoc)
        return {"archive.dump": self.archive, "baseline.json": baseline_bytes, "manifest.json": manifest_bytes,
                "COMPLETE": self.marker if self.marker is not None else marker_text(manifest_bytes)}


def materialize(parent: Path, parts: Parts, *, directory_name: str | None = None) -> Path:
    directory = parent / (directory_name or parts.name)
    directory.mkdir()
    for file_name, content in parts.render().items():
        (directory / file_name).write_bytes(content)
    return directory


def good(parent: Path, **kwargs) -> Path:
    return materialize(parent, Parts(**kwargs))


def edit_manifest(parts: Parts, edit: Callable[[dict], None]) -> Parts:
    edit(parts.mdoc)
    return parts


def edit_baseline(parts: Parts, edit: Callable[[dict], None]) -> Parts:
    """Edit the baseline and bring the manifest's inventory and summary in line, so no digest/summary error masks the intended one."""
    edit(parts.bdoc)
    baseline_bytes = oracle(parts.bdoc)
    parts.mdoc["files"][1] = {"path": "baseline.json", "bytes": len(baseline_bytes), "sha256": sha256(baseline_bytes)}
    parts.mdoc["baseline"] = {"table_count": len(parts.bdoc["fingerprints"]) if isinstance(parts.bdoc.get("fingerprints"), list) else 0,
                              "row_total": sum(r[1] for r in parts.bdoc["fingerprints"]) if isinstance(parts.bdoc.get("fingerprints"), list) else 0}
    return parts


def codes(verdict) -> list[str]:
    return [reason.code for reason in verdict.reasons]


def describe(verdict) -> str:
    return "; ".join(str(r) for r in verdict.reasons) or verdict.status


def expect(verdict, *expected: str, detail: str | None = None) -> None:
    """Exactly these codes (as a multiset-free list, in order), optionally with a detail fragment on the first."""
    assert verdict.status == "INVALID", f"expected INVALID, got {verdict.status}"
    assert codes(verdict) == list(expected), f"expected {list(expected)}, got {describe(verdict)}"
    assert verdict.manifest is None and verdict.baseline is None, "an invalid verdict must not expose unauthenticated documents"
    if detail is not None:
        assert detail in verdict.reasons[0].detail, f"detail {detail!r} not in {verdict.reasons[0].detail!r}"


# =========================================================================== 1. strict JSON
LONG_NESTING = "[" * 20 + "]" * 20
JSON_CASES = [
    ("duplicate_top_level_key", b'{"a":1,"a":2}', "JSON_DUPLICATE_KEY"),
    ("duplicate_nested_key", b'{"a":{"b":1,"b":2}}', "JSON_DUPLICATE_KEY"),
    ("nan", b'{"a":NaN}', "JSON_NON_FINITE"),
    ("infinity", b'{"a":Infinity}', "JSON_NON_FINITE"),
    ("negative_infinity", b'{"a":-Infinity}', "JSON_NON_FINITE"),
    ("float_with_fraction", b'{"a":1.0}', "JSON_FLOAT"),
    ("float_with_exponent", b'{"a":1e3}', "JSON_FLOAT"),
    ("negative_zero_float", b'{"a":-0.0}', "JSON_FLOAT"),
    ("exponent_overflow", b'{"a":1E400}', "JSON_FLOAT"),
    ("integer_above_int64", b'{"a":9223372036854775808}', "INTEGER_OUT_OF_RANGE"),
    ("integer_below_int64", b'{"a":-9223372036854775809}', "INTEGER_OUT_OF_RANGE"),
    ("byte_order_mark", b'\xef\xbb\xbf{"a":1}', "INVALID_ENCODING"),
    ("invalid_utf8", b'{"a":"\xff\xfe"}', "INVALID_ENCODING"),
    ("empty_input", b"", "JSON_MALFORMED"),
    ("trailing_garbage", b'{"a":1} x', "JSON_MALFORMED"),
    ("truncated", b'{"a":1', "JSON_MALFORMED"),
    ("single_quotes", b"{'a':1}", "JSON_MALFORMED"),
    ("deep_nesting", LONG_NESTING.encode(), "JSON_TOO_DEEP"),
    ("lone_surrogate_escape", b'{"a":"\\ud800"}', "JSON_INVALID_STRING"),
    ("lone_surrogate_in_key", b'{"\\udc00":1}', "JSON_INVALID_STRING"),
    ("huge_integer_literal", b'{"a":' + b"9" * 5000 + b"}", "JSON_MALFORMED"),
]


@pytest.mark.parametrize("name, data, code", JSON_CASES, ids=[c[0] for c in JSON_CASES])
def test_strict_json_rejects_each_violation_with_its_documented_code(name, data, code):
    with pytest.raises(ArtifactFormatError) as excinfo:
        parse_strict_json(data, max_bytes=1 << 20)
    assert excinfo.value.code == code, f"{name}: expected {code}, got {excinfo.value.code}"


def test_strict_json_accepts_the_allowed_value_types_exactly():
    document = {"object": {"n": 1}, "array": [1, "two", True, False, None], "string": "café ☃", "int": -9223372036854775808,
                "max": 9223372036854775807, "bool": True, "null": None, "emoji": "\U0001F600"}
    parsed = parse_strict_json(json.dumps(document).encode(), max_bytes=1 << 20)
    assert parsed == document
    assert type(parsed["bool"]) is bool and type(parsed["int"]) is int


def test_size_limit_is_applied_before_decoding_or_parsing():
    oversized_and_invalid = b"\xff" * 100  # would fail INVALID_ENCODING if it were decoded
    with pytest.raises(ArtifactFormatError) as excinfo:
        parse_strict_json(oversized_and_invalid, max_bytes=50)
    assert excinfo.value.code == "FILE_TOO_LARGE"
    assert parse_strict_json(b"{}", max_bytes=2) == {}


SERIALIZATION_CASES = [
    ("float", {"a": 1.5}, "JSON_FLOAT"),
    ("nan", {"a": float("nan")}, "JSON_FLOAT"),
    ("tuple", {"a": (1, 2)}, "JSON_UNSUPPORTED_TYPE"),
    ("set", {"a": {1}}, "JSON_UNSUPPORTED_TYPE"),
    ("bytes", {"a": b"x"}, "JSON_UNSUPPORTED_TYPE"),
    ("non_string_key", {1: "x"}, "JSON_UNSUPPORTED_TYPE"),
    ("int_above_int64", {"a": 2**63}, "INTEGER_OUT_OF_RANGE"),
    ("lone_surrogate", {"a": "\ud800"}, "JSON_INVALID_STRING"),
    ("too_deep", [[[[[[[[[[1]]]]]]]]]], "JSON_TOO_DEEP"),
]


@pytest.mark.parametrize("name, document, code", SERIALIZATION_CASES, ids=[c[0] for c in SERIALIZATION_CASES])
def test_serialization_refuses_what_parsing_refuses(name, document, code):
    with pytest.raises(ArtifactFormatError) as excinfo:
        canonical_json_bytes(document)
    assert excinfo.value.code == code, name


def test_canonical_serialization_is_deterministic_and_matches_the_independent_oracle():
    for document in (baseline_doc(), manifest_doc(), {"b": 1, "a": {"y": [True, None, "x"], "x": "café"}}, {"z": "☃\n\t\"\\", "a": ""}):
        produced = canonical_json_bytes(document)
        assert produced == oracle(document)
        assert produced.endswith(b"\n") and not produced.endswith(b"\n\n")
        assert produced == canonical_json_bytes(copy.deepcopy(document))
    assert canonical_json_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}\n'
    assert canonical_json_bytes({"k": "café"}) == '{"k":"café"}\n'.encode("utf-8"), "non-ASCII is written raw, not \\u-escaped"
    assert canonical_json_bytes({"k": True}) == b'{"k":true}\n' and canonical_json_bytes({"k": 1}) == b'{"k":1}\n', "booleans and integers stay distinct"


def test_canonical_round_trip_is_stable_and_preserves_types():
    document = baseline_doc()
    raw = canonical_json_bytes(document)
    again = parse_strict_json(raw, max_bytes=1 << 20)
    assert again == document and canonical_json_bytes(again) == raw
    assert type(again["sequence_details"][0]["cycle"]) is bool and again["sequences"][1][1] is None


NON_CANONICAL_VARIANTS = {
    "pretty_printed": lambda d: json.dumps(d, sort_keys=True, indent=2).encode() + b"\n",
    "spaces_after_separators": lambda d: json.dumps(d, sort_keys=True).encode() + b"\n",
    "unsorted_keys": lambda d: json.dumps(d, separators=(",", ":"), sort_keys=False).encode() + b"\n" if list(d) != sorted(d) else None,
    "missing_trailing_newline": lambda d: oracle(d)[:-1],
    "double_trailing_newline": lambda d: oracle(d) + b"\n",
    "ascii_escaped_unicode": lambda d: json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n" if any(ord(c) > 127 for c in json.dumps(d, ensure_ascii=False)) else None,
}


@pytest.mark.parametrize("variant", sorted(NON_CANONICAL_VARIANTS))
def test_valid_but_non_canonical_documents_are_rejected(variant):
    document = manifest_doc()
    document["tooling"]["version"] = "0.0.0-test"
    document["source"]["pg_dump_version"] = "pg_dump (PostgreSQL) 16.4 café"
    raw = NON_CANONICAL_VARIANTS[variant](document)
    if raw is None:
        pytest.skip(f"{variant} does not differ for this document")
    assert raw != oracle(document)
    with pytest.raises(ArtifactFormatError) as excinfo:
        parse_manifest(raw)
    assert excinfo.value.code == "NOT_CANONICAL", variant


def test_top_level_value_must_be_an_object():
    for raw in (b"[1]\n", b'"x"\n', b"1\n", b"null\n"):
        with pytest.raises(ArtifactFormatError) as excinfo:
            parse_manifest(raw)
        assert excinfo.value.code == "FIELD_TYPE"


# =========================================================================== 2. timestamps
BAD_TIMESTAMPS = [
    ("february_30", "2026-02-30T00:00:00.000000Z"), ("month_13", "2026-13-01T00:00:00.000000Z"), ("day_32", "2026-01-32T00:00:00.000000Z"),
    ("hour_24", "2026-10-02T24:00:00.000000Z"), ("minute_60", "2026-10-02T10:60:00.000000Z"), ("second_60", "2026-10-02T10:00:60.000000Z"),
    ("year_zero", "0000-10-02T10:00:00.000000Z"), ("non_leap_feb_29", "2025-02-29T00:00:00.000000Z"),
    ("no_z", "2026-10-02T18:30:00.000000"), ("offset", "2026-10-02T18:30:00.000000+00:00"), ("lowercase_z", "2026-10-02T18:30:00.000000z"),
    ("five_fraction_digits", "2026-10-02T18:30:00.00000Z"), ("seven_fraction_digits", "2026-10-02T18:30:00.0000000Z"),
    ("no_fraction", "2026-10-02T18:30:00Z"), ("space_separator", "2026-10-02 18:30:00.000000Z"),
    ("leading_space", " 2026-10-02T18:30:00.000000Z"), ("trailing_newline", "2026-10-02T18:30:00.000000Z\n"), ("empty", ""),
    ("unicode_digits", "2026-10-02T18:30:00.00000١Z"),
]


@pytest.mark.parametrize("name, value", BAD_TIMESTAMPS, ids=[c[0] for c in BAD_TIMESTAMPS])
@pytest.mark.parametrize("where", ["created_utc", "finished_utc"])
def test_invalid_timestamps_are_rejected_with_the_timestamp_code(name, value, where):
    document = manifest_doc()
    document[where] = value
    with pytest.raises(ArtifactFormatError) as excinfo:
        parse_manifest(oracle(document))
    assert excinfo.value.code == "TIMESTAMP_INVALID", f"{name}: got {excinfo.value.code}"


def test_a_valid_leap_day_and_boundary_instants_are_accepted():
    for value in ("2024-02-29T23:59:59.999999Z", "2026-12-31T00:00:00.000000Z", "2026-01-01T00:00:00.000000Z"):
        document = manifest_doc()
        document["created_utc"] = document["finished_utc"] = value
        assert parse_manifest(oracle(document)).created_utc == value


def test_timestamp_in_the_future_of_finished_is_a_value_error():
    document = manifest_doc()
    document["created_utc"], document["finished_utc"] = FINISHED, CREATED
    with pytest.raises(ArtifactFormatError) as excinfo:
        parse_manifest(oracle(document))
    assert excinfo.value.code == "FIELD_VALUE" and "finished_utc" in excinfo.value.detail


# =========================================================================== 3. completion marker
VALID_MARKER = marker_text(b"manifest")
MARKER_CASES = [
    ("missing_final_newline", VALID_MARKER[:-1], "MARKER_MALFORMED"),
    ("crlf", VALID_MARKER.replace(b"\n", b"\r\n"), "MARKER_MALFORMED"),
    ("extra_line", VALID_MARKER + b"extra=1\n", "MARKER_MALFORMED"),
    ("missing_line", b"\n".join(VALID_MARKER.split(b"\n")[:3]) + b"\n", "MARKER_MALFORMED"),
    ("lines_reordered", b"\n".join([VALID_MARKER.split(b"\n")[0], VALID_MARKER.split(b"\n")[2], VALID_MARKER.split(b"\n")[1], VALID_MARKER.split(b"\n")[3]]) + b"\n", "MARKER_MALFORMED"),
    ("empty", b"", "MARKER_MALFORMED"),
    ("only_newline", b"\n", "MARKER_MALFORMED"),
    ("non_ascii", VALID_MARKER + "café".encode("utf-8"), "MARKER_MALFORMED"),
    ("uppercase_digest", VALID_MARKER.replace(sha256(b"manifest").encode(), sha256(b"manifest").upper().encode()), "MARKER_MALFORMED"),
    ("short_digest", f"{ba.MARKER_FORMAT}\nartifact_id={ARTIFACT_ID}\nmanifest_sha256=abcd\nmanifest_bytes=8\n".encode(), "MARKER_MALFORMED"),
    ("bad_uuid", f"{ba.MARKER_FORMAT}\nartifact_id=not-a-uuid\nmanifest_sha256={sha256(b'm')}\nmanifest_bytes=8\n".encode(), "MARKER_MALFORMED"),
    ("uuid_version_1", f"{ba.MARKER_FORMAT}\nartifact_id=3f2b8c1e-6a47-1d0e-9b53-1c7a2d9e5f08\nmanifest_sha256={sha256(b'm')}\nmanifest_bytes=8\n".encode(), "MARKER_MALFORMED"),
    ("size_with_leading_zero", f"{ba.MARKER_FORMAT}\nartifact_id={ARTIFACT_ID}\nmanifest_sha256={sha256(b'm')}\nmanifest_bytes=08\n".encode(), "MARKER_MALFORMED"),
    ("size_zero", f"{ba.MARKER_FORMAT}\nartifact_id={ARTIFACT_ID}\nmanifest_sha256={sha256(b'm')}\nmanifest_bytes=0\n".encode(), "MARKER_MALFORMED"),
    ("size_negative", f"{ba.MARKER_FORMAT}\nartifact_id={ARTIFACT_ID}\nmanifest_sha256={sha256(b'm')}\nmanifest_bytes=-8\n".encode(), "MARKER_MALFORMED"),
    ("size_not_a_number", f"{ba.MARKER_FORMAT}\nartifact_id={ARTIFACT_ID}\nmanifest_sha256={sha256(b'm')}\nmanifest_bytes=eight\n".encode(), "MARKER_MALFORMED"),
    ("size_beyond_int64", f"{ba.MARKER_FORMAT}\nartifact_id={ARTIFACT_ID}\nmanifest_sha256={sha256(b'm')}\nmanifest_bytes=9223372036854775808\n".encode(), "MARKER_MALFORMED"),
    ("trailing_space", VALID_MARKER.replace(b"manifest_bytes=8\n", b"manifest_bytes=8 \n"), "MARKER_MALFORMED"),
    ("too_long", VALID_MARKER + b"x" * 600, "MARKER_MALFORMED"),
    ("wrong_field_name", VALID_MARKER.replace(b"manifest_sha256", b"manifest_sha1"), "MARKER_MALFORMED"),
    ("unsupported_version", VALID_MARKER.replace(b"complete/1", b"complete/2"), "MARKER_UNSUPPORTED_VERSION"),
    ("unsupported_version_zero", VALID_MARKER.replace(b"complete/1", b"complete/0"), "MARKER_UNSUPPORTED_VERSION"),
    ("different_family", VALID_MARKER.replace(b"napsedge.backup-complete/1", b"somebody.else/1"), "MARKER_MALFORMED"),
]


@pytest.mark.parametrize("name, data, code", MARKER_CASES, ids=[c[0] for c in MARKER_CASES])
def test_malformed_markers_are_rejected_with_their_code(name, data, code):
    with pytest.raises(ArtifactFormatError) as excinfo:
        parse_completion_marker(data)
    assert excinfo.value.code == code, f"{name}: got {excinfo.value.code}"


def test_a_well_formed_marker_parses_exactly():
    manifest = b'{"manifest":"bytes"}\n'
    marker = parse_completion_marker(marker_text(manifest))
    assert (marker.artifact_id, marker.manifest_sha256, marker.manifest_bytes) == (ARTIFACT_ID, sha256(manifest), len(manifest))


# =========================================================================== 4. manifest schema
def paths_of(document, prefix=()):
    """Every object-key path and array-element path in a document, for exhaustive missing/unknown/type probes."""
    if isinstance(document, dict):
        for key, value in document.items():
            yield prefix + (key,)
            yield from paths_of(value, prefix + (key,))
    elif isinstance(document, list):
        for index, value in enumerate(document):
            yield from paths_of(value, prefix + (index,))


def at(document, path):
    for step in path[:-1]:
        document = document[step]
    return document


def object_paths(document, prefix=()):
    if isinstance(document, dict):
        yield prefix
        for key, value in document.items():
            yield from object_paths(value, prefix + (key,))
    elif isinstance(document, list):
        for index, value in enumerate(document):
            yield from object_paths(value, prefix + (index,))


def parse_modified(parse, document):
    return parse(oracle(document))


def test_a_valid_manifest_round_trips_to_identical_bytes():
    raw = oracle(manifest_doc())
    manifest = parse_manifest(raw)
    assert manifest.to_bytes() == raw
    assert manifest.files[0].path == "archive.dump" and manifest.files[1].path == "baseline.json"


@pytest.mark.parametrize("path", sorted(set(paths_of(manifest_doc())), key=str), ids=lambda p: ".".join(map(str, p)))
def test_every_manifest_field_is_required(path):
    document = manifest_doc()
    if isinstance(path[-1], int):
        pytest.skip("array elements are covered by the inventory tests")
    del at(document, path)[path[-1]]
    with pytest.raises(ArtifactFormatError) as excinfo:
        parse_modified(parse_manifest, document)
    assert excinfo.value.code == "FIELD_MISSING", f"removing {path}: got {excinfo.value.code}"


@pytest.mark.parametrize("path", [p for p in object_paths(manifest_doc())], ids=lambda p: ".".join(map(str, p)) or "root")
def test_unknown_manifest_fields_are_rejected_at_every_object_level(path):
    document = manifest_doc()
    target = document
    for step in path:
        target = target[step]
    target["zz_unknown"] = 1
    with pytest.raises(ArtifactFormatError) as excinfo:
        parse_modified(parse_manifest, document)
    assert excinfo.value.code == "FIELD_UNKNOWN", f"extra key at {path}: got {excinfo.value.code}"


MANIFEST_TYPE_CASES = [
    (("source", "database_oid"), True, "FIELD_TYPE"), (("source", "database_oid"), False, "FIELD_TYPE"), (("source", "database_oid"), "16384", "FIELD_TYPE"),
    (("source", "server_version_num"), True, "FIELD_TYPE"), (("source", "logical_name"), 5, "FIELD_TYPE"), (("source", "system_identifier"), None, "FIELD_TYPE"),
    (("source", "system_identifier"), 7550123456789012345, "FIELD_TYPE"), (("source",), [], "FIELD_TYPE"), (("source",), None, "FIELD_TYPE"),
    (("schema", "heads_match"), 1, "FIELD_TYPE"), (("schema", "heads_match"), "true", "FIELD_TYPE"), (("schema", "heads_match"), None, "FIELD_TYPE"),
    (("repository", "dirty"), None, "FIELD_TYPE"), (("repository", "dirty"), 0, "FIELD_TYPE"),
    (("consistency", "foreign_write_sessions_start"), True, "FIELD_TYPE"), (("consistency", "foreign_write_sessions_end"), False, "FIELD_TYPE"),
    (("consistency", "catalog_bracket_equal"), "yes", "FIELD_TYPE"), (("consistency", "snapshot_unchanged_at_end"), 1, "FIELD_TYPE"),
    (("archive", "toc_entries"), True, "FIELD_TYPE"), (("baseline", "table_count"), False, "FIELD_TYPE"), (("baseline", "row_total"), True, "FIELD_TYPE"),
    (("baseline", "row_total"), None, "FIELD_TYPE"), (("files",), {}, "FIELD_TYPE"), (("files", 0, "bytes"), True, "FIELD_TYPE"),
    (("files", 1, "bytes"), "6120", "FIELD_TYPE"), (("files", 0, "path"), 7, "FIELD_TYPE"), (("files", 0), "archive.dump", "FIELD_TYPE"),
    (("artifact_id",), None, "FIELD_TYPE"), (("created_utc",), 20261002, "FIELD_TYPE"), (("format",), None, "FIELD_VALUE"),
]


@pytest.mark.parametrize("path, value, code", MANIFEST_TYPE_CASES, ids=[f"{'.'.join(map(str, c[0]))}={c[1]!r}" for c in MANIFEST_TYPE_CASES])
def test_manifest_type_violations_are_field_type_errors(path, value, code):
    document = manifest_doc()
    at(document, path)[path[-1]] = value
    with pytest.raises(ArtifactFormatError) as excinfo:
        parse_modified(parse_manifest, document)
    assert excinfo.value.code == code, f"{path}={value!r}: got {excinfo.value.code}"


def test_a_float_in_an_integer_field_is_rejected_at_the_json_level():
    raw = oracle(manifest_doc()).replace(b'"database_oid":16384', b'"database_oid":16384.0')
    with pytest.raises(ArtifactFormatError) as excinfo:
        parse_manifest(raw)
    assert excinfo.value.code == "JSON_FLOAT"


MANIFEST_VALUE_CASES = [
    (("artifact_id",), "3f2b8c1e-6a47-1d0e-9b53-1c7a2d9e5f08"),                 # uuid version 1
    (("artifact_id",), "3F2B8C1E-6A47-4D0E-9B53-1C7A2D9E5F08"),                 # upper case
    (("artifact_id",), "3f2b8c1e-6a47-4d0e-7b53-1c7a2d9e5f08"),                 # bad variant
    (("source", "logical_name"), "Upper"), (("source", "logical_name"), "a" * 41), (("source", "logical_name"), "has-dash"), (("source", "logical_name"), ""),
    (("source", "system_identifier"), "12ab"), (("source", "system_identifier"), ""), (("source", "system_identifier"), "1" * 21),
    (("source", "database_oid"), -1), (("source", "server_version_num"), 0), (("source", "database_name"), ""), (("source", "database_name"), "x" * 64),
    (("source", "database_name"), "has space"), (("source", "backup_role"), "bad role"), (("source", "pg_dump_version"), ""),
    (("schema", "alembic_head"), ""), (("schema", "alembic_head"), "not valid!"), (("schema", "heads_match"), False),
    (("repository", "git_revision"), "373b35452bb47f71d5f85cbca1e0624a9e4139f"), (("repository", "git_revision"), "373B35452BB47F71D5F85CBCA1E0624A9E4139F3"),
    (("tooling", "name"), "Has Space"), (("tooling", "version"), ""), (("tooling", "version"), "has space"),
    (("consistency", "mode"), "snapshot"), (("consistency", "sequence_rule"), "monotonic_v2"), (("consistency", "ddl_exclusion"), "partial"),
    (("consistency", "snapshot_descriptor"), ""), (("consistency", "snapshot_descriptor"), "not a snapshot"),
    (("consistency", "foreign_write_sessions_start"), -1), (("consistency", "foreign_write_sessions_end"), -1),
    (("archive", "format"), "tar"), (("archive", "toc_entries"), -1), (("archive", "toc_sha256"), "ABC"), (("archive", "toc_sha256"), "g" * 64),
    (("baseline", "table_count"), -1), (("baseline", "row_total"), -1),
    (("files", 0, "bytes"), 0), (("files", 0, "bytes"), -5), (("files", 1, "bytes"), -1), (("files", 0, "sha256"), "ABCDEF"), (("files", 1, "sha256"), "0" * 63),
]


@pytest.mark.parametrize("path, value", MANIFEST_VALUE_CASES, ids=[f"{'.'.join(map(str, c[0]))}={c[1]!r}"[:70] for c in MANIFEST_VALUE_CASES])
def test_manifest_value_violations_are_field_value_errors(path, value):
    document = manifest_doc()
    at(document, path)[path[-1]] = value
    with pytest.raises(ArtifactFormatError) as excinfo:
        parse_modified(parse_manifest, document)
    assert excinfo.value.code == "FIELD_VALUE", f"{path}={value!r}: got {excinfo.value.code}"


def test_quiescent_mode_must_record_no_snapshot_and_snapshot_mode_must_record_one():
    document = manifest_doc()
    document["consistency"].update(mode="quiescent", snapshot_descriptor="", snapshot_unchanged_at_end=False, sequence_rule="equal_v1")
    assert parse_manifest(oracle(document)).consistency.mode == "quiescent"
    for descriptor, unchanged in (("1042:1042:", False), ("", True)):
        bad = copy.deepcopy(document)
        bad["consistency"].update(snapshot_descriptor=descriptor, snapshot_unchanged_at_end=unchanged)
        with pytest.raises(ArtifactFormatError) as excinfo:
            parse_manifest(oracle(bad))
        assert excinfo.value.code == "FIELD_VALUE"


@pytest.mark.parametrize("files, code", [
    ([], "INVENTORY_INVALID"),
    ([{"path": "archive.dump", "bytes": 1, "sha256": "0" * 64}], "INVENTORY_INVALID"),
    ([{"path": "archive.dump", "bytes": 1, "sha256": "0" * 64}] * 3, "INVENTORY_INVALID"),
    ([{"path": "baseline.json", "bytes": 1, "sha256": "0" * 64}, {"path": "archive.dump", "bytes": 1, "sha256": "0" * 64}], "INVENTORY_INVALID"),
    ([{"path": "archive.dump", "bytes": 1, "sha256": "0" * 64}, {"path": "archive.dump", "bytes": 1, "sha256": "0" * 64}], "INVENTORY_INVALID"),
    ([{"path": "archive.dump", "bytes": 1, "sha256": "0" * 64}, {"path": "other.bin", "bytes": 1, "sha256": "0" * 64}], "INVENTORY_INVALID"),
    ([{"path": "archive.dump", "bytes": 1, "sha256": "0" * 64}, {"path": "manifest.json", "bytes": 1, "sha256": "0" * 64}], "INVENTORY_INVALID"),
    ([{"path": "../archive.dump", "bytes": 1, "sha256": "0" * 64}, {"path": "baseline.json", "bytes": 1, "sha256": "0" * 64}], "UNSAFE_PATH"),
    ([{"path": "sub/archive.dump", "bytes": 1, "sha256": "0" * 64}, {"path": "baseline.json", "bytes": 1, "sha256": "0" * 64}], "UNSAFE_PATH"),
    ([{"path": "/etc/passwd", "bytes": 1, "sha256": "0" * 64}, {"path": "baseline.json", "bytes": 1, "sha256": "0" * 64}], "UNSAFE_PATH"),
    ([{"path": "archive.dump", "bytes": 1, "sha256": "0" * 64}, {"path": "..\\baseline.json", "bytes": 1, "sha256": "0" * 64}], "UNSAFE_PATH"),
    ([{"path": "C:archive.dump", "bytes": 1, "sha256": "0" * 64}, {"path": "baseline.json", "bytes": 1, "sha256": "0" * 64}], "UNSAFE_PATH"),
    ([{"path": ".hidden", "bytes": 1, "sha256": "0" * 64}, {"path": "baseline.json", "bytes": 1, "sha256": "0" * 64}], "UNSAFE_PATH"),
    ([{"path": "archive.dump\u0000", "bytes": 1, "sha256": "0" * 64}, {"path": "baseline.json", "bytes": 1, "sha256": "0" * 64}], "UNSAFE_PATH"),
    ([{"path": "", "bytes": 1, "sha256": "0" * 64}, {"path": "baseline.json", "bytes": 1, "sha256": "0" * 64}], "UNSAFE_PATH"),
    ([{"path": "ARCHIVE.DUMP", "bytes": 1, "sha256": "0" * 64}, {"path": "baseline.json", "bytes": 1, "sha256": "0" * 64}], "INVENTORY_INVALID"),
])
def test_inventory_must_be_exactly_the_two_plain_names_in_order(files, code):
    document = manifest_doc()
    document["files"] = files
    with pytest.raises(ArtifactFormatError) as excinfo:
        parse_modified(parse_manifest, document)
    assert excinfo.value.code == code


@pytest.mark.parametrize("found, code", [("napsedge.backup-artifact/3", "UNSUPPORTED_VERSION"), ("napsedge.backup-artifact/0", "UNSUPPORTED_VERSION"),
                                          ("napsedge.backup-artifact/1.1", "UNSUPPORTED_VERSION"), ("napsedge.backup-artifact/", "UNSUPPORTED_VERSION"),
                                          ("napsedge.backup-baseline/1", "FIELD_VALUE"), ("something/1", "FIELD_VALUE"), ("", "FIELD_VALUE")])
def test_unsupported_manifest_versions_fail_closed_before_any_other_field_is_judged(found, code):
    document = {"format": found, "unrelated": "an unknown version's other fields are never interpreted"}
    with pytest.raises(ArtifactFormatError) as excinfo:
        parse_modified(parse_manifest, document)
    assert excinfo.value.code == code


# =========================================================================== 5. baseline schema
def test_a_valid_baseline_round_trips_to_identical_bytes_and_keeps_documented_nulls():
    raw = oracle(baseline_doc())
    document = parse_baseline(raw)
    assert document.to_bytes() == raw
    assert document.version == "5b02bea0eb85" and document.max_alias_id == 3
    assert document.sequences[1].last_value is None and document.sequence_details[1].last_value is None
    assert document.sequence_details[1].owned_by is None and document.sequence_details[1].column_max is None


@pytest.mark.parametrize("field_name, value", [("version", None), ("max_alias_id", None)])
def test_documented_null_baseline_fields_are_accepted(field_name, value):
    document = baseline_doc()
    document[field_name] = value
    assert getattr(parse_baseline(oracle(document)), field_name) is None


BASELINE_NULL_REJECTIONS = [
    ("artifact_id", None), ("captured_utc", None), ("fingerprints", None), ("constraints", None), ("indexes", None), ("acls", None),
    ("sequences", None), ("sequence_details", None), ("format", None),
]


@pytest.mark.parametrize("field_name, value", BASELINE_NULL_REJECTIONS)
def test_null_is_rejected_for_every_undocumented_baseline_field(field_name, value):
    document = baseline_doc()
    document[field_name] = value
    with pytest.raises(ArtifactFormatError) as excinfo:
        parse_baseline(oracle(document))
    assert excinfo.value.code in {"FIELD_TYPE", "FIELD_VALUE"}, f"{field_name}: {excinfo.value.code}"


NESTED_NULL_REJECTIONS = [
    (("fingerprints", 0, 0), None), (("fingerprints", 0, 1), None), (("fingerprints", 0, 2), None),
    (("constraints", 0, 3), None), (("constraints", 0, 4), None), (("indexes", 0, 2), None), (("acls", 0, 3), None), (("acls", 0, 2), None),
    (("sequences", 0, 0), None), (("sequence_details", 0, "cycle"), None), (("sequence_details", 0, "is_called"), None),
    (("sequence_details", 0, "increment"), None), (("sequence_details", 0, "name"), None), (("sequence_details", 0, "data_type"), None),
]


@pytest.mark.parametrize("path, value", NESTED_NULL_REJECTIONS, ids=[".".join(map(str, c[0])) for c in NESTED_NULL_REJECTIONS])
def test_null_is_rejected_inside_baseline_entries_except_where_documented(path, value):
    document = baseline_doc()
    at(document, path)[path[-1]] = value
    with pytest.raises(ArtifactFormatError) as excinfo:
        parse_baseline(oracle(document))
    assert excinfo.value.code in {"FIELD_TYPE", "FIELD_VALUE"}, f"{path}: {excinfo.value.code}"


@pytest.mark.parametrize("path, value", [
    (("fingerprints", 0, 1), True), (("fingerprints", 1, 1), False), (("sequences", 0, 1), True), (("sequences", 0, 1), "3"),
    (("sequence_details", 0, "increment"), True), (("sequence_details", 0, "cache"), True), (("sequence_details", 0, "last_value"), True),
    (("sequence_details", 0, "column_max"), False), (("sequence_details", 0, "minimum"), True), (("max_alias_id",), True),
    (("constraints", 0, 3), 1), (("constraints", 0, 3), "true"), (("sequence_details", 0, "cycle"), 0), (("sequence_details", 0, "is_called"), "yes"),
    (("version",), 5), (("fingerprints", 0, 0), 5), (("acls", 0, 1), 1),
])
def test_booleans_and_wrong_types_are_rejected_where_integers_strings_or_booleans_are_required(path, value):
    document = baseline_doc()
    at(document, path)[path[-1]] = value
    with pytest.raises(ArtifactFormatError) as excinfo:
        parse_baseline(oracle(document))
    assert excinfo.value.code == "FIELD_TYPE", f"{path}={value!r}: got {excinfo.value.code}"


BASELINE_VALUE_CASES = [
    ("empty_md5_with_rows", lambda d: d["fingerprints"].__setitem__(1, ["team_aliases", 3, "empty"]), "FIELD_VALUE"),
    ("hex_md5_with_zero_rows", lambda d: d["fingerprints"].__setitem__(2, ["teams", 0, "0d9f4c1a7b6e2f8d3a5c9e1b4f7a2d60"]), "FIELD_VALUE"),
    ("uppercase_md5", lambda d: d["fingerprints"].__setitem__(0, ["alembic_version", 1, "0D9F4C1A7B6E2F8D3A5C9E1B4F7A2D60"]), "FIELD_VALUE"),
    ("short_md5", lambda d: d["fingerprints"].__setitem__(0, ["alembic_version", 1, "0d9f"]), "FIELD_VALUE"),
    ("negative_rows", lambda d: d["fingerprints"].__setitem__(0, ["alembic_version", -1, "0d9f4c1a7b6e2f8d3a5c9e1b4f7a2d60"]), "FIELD_VALUE"),
    ("fingerprint_wrong_arity", lambda d: d["fingerprints"].__setitem__(0, ["alembic_version", 1]), "FIELD_VALUE"),
    ("constraint_wrong_arity", lambda d: d["constraints"].__setitem__(0, ["t", "n", "p", True]), "FIELD_VALUE"),
    ("constraint_two_char_type", lambda d: d["constraints"].__setitem__(0, ["t", "n", "pk", True, "def"]), "FIELD_VALUE"),
    ("constraint_empty_definition", lambda d: d["constraints"].__setitem__(0, ["t", "n", "p", True, ""]), "FIELD_VALUE"),
    ("index_wrong_arity", lambda d: d["indexes"].__setitem__(0, ["t", "i"]), "FIELD_VALUE"),
    ("acl_two_char_relkind", lambda d: d["acls"].__setitem__(0, ["teams", "rr", "owner", "x"]), "FIELD_VALUE"),
    ("acl_empty_owner", lambda d: d["acls"].__setitem__(0, ["teams", "r", "", "x"]), "FIELD_VALUE"),
    ("sequence_wrong_arity", lambda d: d["sequences"].__setitem__(0, ["s", 1, 2]), "FIELD_VALUE"),
    ("detail_bad_data_type", lambda d: d["sequence_details"][0].__setitem__("data_type", "numeric"), "FIELD_VALUE"),
    ("detail_zero_cache", lambda d: d["sequence_details"][0].__setitem__("cache", 0), "FIELD_VALUE"),
    ("detail_owned_by_wrong_arity", lambda d: d["sequence_details"][0].__setitem__("owned_by", ["only_table"]), "FIELD_VALUE"),
    ("version_not_a_revision", lambda d: d.__setitem__("version", "has space"), "FIELD_VALUE"),
    ("captured_not_a_timestamp", lambda d: d.__setitem__("captured_utc", "2026-02-30T00:00:00.000000Z"), "TIMESTAMP_INVALID"),
    ("artifact_id_v1", lambda d: d.__setitem__("artifact_id", "3f2b8c1e-6a47-1d0e-9b53-1c7a2d9e5f08"), "FIELD_VALUE"),
    ("duplicate_fingerprint_table", lambda d: d["fingerprints"].append(["teams", 0, "empty"]), "BASELINE_DUPLICATE_KEY"),
    ("duplicate_constraint_key", lambda d: d["constraints"].append(["teams", "pk_teams", "p", True, "other definition"]), "BASELINE_DUPLICATE_KEY"),
    ("duplicate_index_key", lambda d: d["indexes"].append(["teams", "pk_teams", "other definition"]), "BASELINE_DUPLICATE_KEY"),
    ("duplicate_acl_relation", lambda d: d["acls"].append(["teams", "r", "owner", "other"]), "BASELINE_DUPLICATE_KEY"),
    ("duplicate_sequence_name", lambda d: d["sequences"].append(["team_aliases_id_seq", 9]), "BASELINE_DUPLICATE_KEY"),
    ("duplicate_sequence_detail", lambda d: d["sequence_details"].append(copy.deepcopy(d["sequence_details"][0])), "BASELINE_DUPLICATE_KEY"),
    ("fingerprints_unsorted", lambda d: d["fingerprints"].reverse(), "BASELINE_ORDER"),
    ("details_missing_a_sequence", lambda d: d["sequence_details"].pop(), "BASELINE_SEQUENCE_MISMATCH"),
    ("details_name_a_stranger", lambda d: d["sequence_details"][1].__setitem__("name", "stranger_seq"), "BASELINE_SEQUENCE_MISMATCH"),
]


@pytest.mark.parametrize("name, mutate, code", BASELINE_VALUE_CASES, ids=[c[0] for c in BASELINE_VALUE_CASES])
def test_baseline_value_duplicate_and_order_violations_have_their_codes(name, mutate, code):
    document = baseline_doc()
    mutate(document)
    with pytest.raises(ArtifactFormatError) as excinfo:
        parse_baseline(oracle(document))
    assert excinfo.value.code == code, f"{name}: got {excinfo.value.code}"


def test_a_distinct_constraint_with_the_same_name_on_another_table_is_not_a_duplicate():
    document = baseline_doc()
    document["constraints"].append(["other_table", "pk_teams", "p", True, "PRIMARY KEY (id)"])
    assert len(parse_baseline(oracle(document)).constraints) == 3


@pytest.mark.parametrize("path", [p for p in object_paths(baseline_doc())], ids=lambda p: ".".join(map(str, p)) or "root")
def test_unknown_baseline_fields_are_rejected_at_every_object_level(path):
    document = baseline_doc()
    target = document
    for step in path:
        target = target[step]
    if isinstance(target, dict):
        target["zz_unknown"] = 1
        expected = "FIELD_UNKNOWN"
    else:
        pytest.skip("only objects can carry an unknown key")
    with pytest.raises(ArtifactFormatError) as excinfo:
        parse_baseline(oracle(document))
    assert excinfo.value.code == expected


@pytest.mark.parametrize("key", sorted(baseline_doc()))
def test_every_baseline_field_is_required(key):
    document = baseline_doc()
    del document[key]
    with pytest.raises(ArtifactFormatError) as excinfo:
        parse_baseline(oracle(document))
    assert excinfo.value.code == "FIELD_MISSING"


def test_unsupported_baseline_version_fails_closed():
    document = baseline_doc()
    document["format"] = "napsedge.backup-baseline/2"
    with pytest.raises(ArtifactFormatError) as excinfo:
        parse_baseline(oracle(document))
    assert excinfo.value.code == "UNSUPPORTED_VERSION"


# =========================================================================== 6. directory name
@pytest.mark.parametrize("name", [
    "napsedge-napsedge-20261002T183000Z-" + OTHER_ID, "napsedge-a_1-20261002T183000Z-" + ARTIFACT_ID, "napsedge-" + "a" * 40 + "-20261002T183000Z-" + ARTIFACT_ID])
def test_valid_directory_names_parse(name):
    assert parse_artifact_directory_name(name).artifact_id in {ARTIFACT_ID, OTHER_ID}


@pytest.mark.parametrize("name", [
    "", "napsedge", "backup-napsedge-20261002T183000Z-" + ARTIFACT_ID, "napsedge-Upper-20261002T183000Z-" + ARTIFACT_ID,
    "napsedge-" + "a" * 41 + "-20261002T183000Z-" + ARTIFACT_ID, "napsedge-has-dash-20261002T183000Z-" + ARTIFACT_ID,
    "napsedge-x-20261302T183000Z-" + ARTIFACT_ID, "napsedge-x-20260230T183000Z-" + ARTIFACT_ID, "napsedge-x-20261002T246000Z-" + ARTIFACT_ID,
    "napsedge-x-20261002T183000-" + ARTIFACT_ID, "napsedge-x-20261002T183000Z-" + ARTIFACT_ID.upper(),
    "napsedge-x-20261002T183000Z-3f2b8c1e-6a47-1d0e-9b53-1c7a2d9e5f08", "napsedge-x-20261002T183000Z-" + ARTIFACT_ID + "-extra",
    ".partial-" + ARTIFACT_ID, "napsedge-x-20261002T183000Z-" + ARTIFACT_ID + " ",
])
def test_invalid_directory_names_are_rejected(name):
    with pytest.raises(ArtifactFormatError) as excinfo:
        parse_artifact_directory_name(name)
    assert excinfo.value.code == "NAME_INVALID"


# =========================================================================== 7. the verifier on directories
def test_a_valid_artifact_is_structurally_valid_and_the_verdict_states_what_that_does_not_mean(tmp_path):
    directory = good(tmp_path)
    verdict = verify_artifact_directory(directory)
    assert verdict.valid and verdict.status == "STRUCTURALLY_VALID" and verdict.reasons == ()
    assert verdict.establishes == ("structural_integrity",)
    assert {"database_consistency", "restore_verified", "authenticity"} <= set(verdict.does_not_establish)
    assert verdict.manifest.artifact_id == ARTIFACT_ID and verdict.baseline.artifact_id == ARTIFACT_ID
    assert verdict.manifest.source.logical_name == LOGICAL
    verify_artifact_directory(str(directory))  # a plain string path works too


def test_expected_identity_matches_are_accepted(tmp_path):
    directory = good(tmp_path)
    expected = ExpectedIdentity(system_identifier="7550123456789012345", database_name="napsedge", alembic_head="5b02bea0eb85")
    assert verify_artifact_directory(directory, expected).valid


def sequence_detail_value(parts: Parts, **changes) -> Parts:
    return edit_baseline(parts, lambda d: d["sequence_details"][0].update(changes))


def build_case_missing(name):
    def build(tmp):
        directory = good(tmp)
        (directory / name).unlink()
        return directory
    return build


def build_case_extra(tmp):
    directory = good(tmp)
    (directory / "notes.txt").write_text("x")
    return directory


def build_case_extra_directory(tmp):
    directory = good(tmp)
    (directory / "subdir").mkdir()
    return directory


def build_case_dir_named_like_a_file(tmp):
    directory = good(tmp)
    (directory / "archive.dump").unlink()
    (directory / "archive.dump").mkdir()
    return directory


def build_case_marker(tmp, content: bytes):
    parts = Parts()
    parts.marker = content
    return materialize(tmp, parts)


def build_case_with_extra_marker(tmp, marker_name: str, *, complete: bool = True):
    directory = good(tmp)
    (directory / marker_name).write_text("{}\n")
    if not complete:
        (directory / "COMPLETE").unlink()
    return directory


def build_case_tamper(tmp, file_name: str, how: str):
    directory = good(tmp)
    path = directory / file_name
    data = path.read_bytes()
    path.write_bytes({"flip": data[:10] + bytes([data[10] ^ 0x01]) + data[11:], "truncate": data[:-1], "append": data + b"x", "empty": b""}[how])
    return directory


def build_case_rebound_manifest_edit(tmp, edit: Callable[[dict], None]):
    parts = Parts()
    edit(parts.mdoc)
    return materialize(tmp, parts)


def build_case_baseline_edit(tmp, edit: Callable[[dict], None]):
    return materialize(tmp, edit_baseline(Parts(), edit))


def build_case_path_is_a_file(tmp):
    path = tmp / DIR_NAME
    path.write_text("x")
    return path


def build_case_name(tmp, name):
    return materialize(tmp, Parts(), directory_name=name)


def build_case_marker_for_other_artifact(tmp):
    parts = Parts()
    parts.marker = marker_text(oracle(parts.mdoc), artifact_id=OTHER_ID)
    return materialize(tmp, parts)


def build_case_pretty_manifest(tmp):
    parts = Parts()
    parts.manifest_bytes = json.dumps(parts.mdoc, indent=2, sort_keys=True).encode() + b"\n"
    return materialize(tmp, parts)


def build_case_manifest_raw(tmp, raw: bytes):
    parts = Parts()
    parts.manifest_bytes = raw
    return materialize(tmp, parts)


def build_case_baseline_raw(tmp, raw: bytes):
    parts = Parts()
    parts.baseline_bytes = raw
    parts.mdoc["files"][1] = {"path": "baseline.json", "bytes": len(raw), "sha256": sha256(raw)}
    return materialize(tmp, parts)


def build_case_partial(tmp, *, with_marker: bool):
    parts = Parts()
    directory = materialize(tmp, parts, directory_name=f".partial-{ARTIFACT_ID}")
    if not with_marker:
        (directory / "COMPLETE").unlink()
        (directory / "manifest.json").unlink()
        (directory / "RUNNING").write_text("{}\n")
    return directory


@dataclass(frozen=True)
class Case:
    id: str
    build: Callable[[Path], Path]
    expected: tuple[str, ...]
    detail: str | None = None


def _mdoc_edit(fn):
    return lambda tmp: build_case_rebound_manifest_edit(tmp, fn)


def _bdoc_edit(fn):
    return lambda tmp: build_case_baseline_edit(tmp, fn)


DIRECTORY_CASES: list[Case] = [
    # -- the directory itself ------------------------------------------------------------------
    Case("path_does_not_exist", lambda tmp: tmp / "absent", ("NOT_A_DIRECTORY",)),
    Case("path_is_a_file", build_case_path_is_a_file, ("NOT_A_DIRECTORY",)),
    Case("name_invalid", lambda tmp: build_case_name(tmp, "my-backup"), ("NAME_INVALID",), "my-backup"),
    Case("name_has_invalid_calendar_time", lambda tmp: build_case_name(tmp, f"napsedge-{LOGICAL}-20261302T183000Z-{ARTIFACT_ID}"), ("NAME_INVALID",)),
    # -- interrupted finalization --------------------------------------------------------------------
    Case("partial_directory_with_running_marker_and_no_manifest", lambda tmp: build_case_partial(tmp, with_marker=False), ("NOT_FINAL",)),
    Case("partial_directory_with_complete_marker_before_rename", lambda tmp: build_case_partial(tmp, with_marker=True), ("NOT_FINAL",)),
    Case("final_name_without_complete_marker", build_case_missing("COMPLETE"), ("MISSING_FILE",), "COMPLETE"),
    Case("final_name_without_manifest_or_marker", lambda tmp: (lambda d: ((d / "manifest.json").unlink(), (d / "COMPLETE").unlink(), d)[-1])(good(tmp)), ("MISSING_FILE", "MISSING_FILE")),
    Case("running_marker_left_beside_complete", lambda tmp: build_case_with_extra_marker(tmp, "RUNNING"), ("CONFLICTING_MARKERS",), "RUNNING"),
    Case("failed_marker_beside_complete", lambda tmp: build_case_with_extra_marker(tmp, "FAILED"), ("CONFLICTING_MARKERS",), "FAILED"),
    Case("failed_without_complete", lambda tmp: build_case_with_extra_marker(tmp, "FAILED", complete=False), ("MISSING_FILE", "ARTIFACT_FAILED")),
    Case("running_without_complete", lambda tmp: build_case_with_extra_marker(tmp, "RUNNING", complete=False), ("MISSING_FILE", "ARTIFACT_RUNNING")),
    # -- the exact file set -----------------------------------------------------------------------------
    Case("archive_missing", build_case_missing("archive.dump"), ("MISSING_FILE",), "archive.dump"),
    Case("baseline_missing", build_case_missing("baseline.json"), ("MISSING_FILE",), "baseline.json"),
    Case("manifest_missing", build_case_missing("manifest.json"), ("MISSING_FILE",), "manifest.json"),
    Case("unlisted_file", build_case_extra, ("UNLISTED_FILE",), "notes.txt"),
    Case("unlisted_directory", build_case_extra_directory, ("UNLISTED_FILE",), "subdir"),
    Case("required_name_is_a_directory", build_case_dir_named_like_a_file, ("NOT_A_REGULAR_FILE",), "archive.dump"),
    # -- the marker -----------------------------------------------------------------------------------------
    Case("marker_malformed", lambda tmp: build_case_marker(tmp, b"COMPLETE\n"), ("MARKER_MALFORMED",)),
    Case("marker_unsupported_version", lambda tmp: build_case_marker(tmp, VALID_MARKER.replace(b"complete/1", b"complete/9")), ("MARKER_UNSUPPORTED_VERSION",)),
    Case("marker_binds_a_different_digest", lambda tmp: build_case_marker(tmp, marker_text(b"some other manifest")), ("MARKER_SIZE_MISMATCH", "MARKER_DIGEST_MISMATCH")),
    Case("manifest_bytes_flipped_after_marker_was_written", lambda tmp: build_case_tamper(tmp, "manifest.json", "flip"), ("MARKER_DIGEST_MISMATCH",)),
    Case("manifest_truncated_after_marker_was_written", lambda tmp: build_case_tamper(tmp, "manifest.json", "truncate"), ("MARKER_SIZE_MISMATCH", "MARKER_DIGEST_MISMATCH")),
    Case("manifest_extended_after_marker_was_written", lambda tmp: build_case_tamper(tmp, "manifest.json", "append"), ("MARKER_SIZE_MISMATCH", "MARKER_DIGEST_MISMATCH")),
    Case("marker_for_a_different_artifact", build_case_marker_for_other_artifact, ("MARKER_ARTIFACT_MISMATCH",)),
    # -- the manifest document -------------------------------------------------------------------------------
    Case("manifest_not_canonical", build_case_pretty_manifest, ("NOT_CANONICAL",)),
    Case("manifest_duplicate_key", lambda tmp: build_case_manifest_raw(tmp, oracle(Parts().mdoc).replace(b'{"archive"', b'{"archive":{},"archive"', 1)), ("JSON_DUPLICATE_KEY",)),
    Case("manifest_not_json", lambda tmp: build_case_manifest_raw(tmp, b"not json\n"), ("JSON_MALFORMED",)),
    Case("manifest_float", lambda tmp: build_case_manifest_raw(tmp, oracle(Parts().mdoc).replace(b'"database_oid":16384', b'"database_oid":16384.5')), ("JSON_FLOAT",)),
    Case("manifest_nan", lambda tmp: build_case_manifest_raw(tmp, b'{"format":NaN}\n'), ("JSON_NON_FINITE",)),
    Case("manifest_bom", lambda tmp: build_case_manifest_raw(tmp, b"\xef\xbb\xbf" + oracle(Parts().mdoc)), ("INVALID_ENCODING",)),
    Case("manifest_integer_too_large", lambda tmp: build_case_manifest_raw(tmp, oracle(Parts().mdoc).replace(b'"database_oid":16384', b'"database_oid":99999999999999999999')), ("INTEGER_OUT_OF_RANGE",)),
    Case("manifest_unsupported_version", _mdoc_edit(lambda m: m.update(format="napsedge.backup-artifact/3")), ("UNSUPPORTED_VERSION",)),  # /2 is supported since 9B-6H (13.16); /3 is not
    Case("manifest_missing_field", _mdoc_edit(lambda m: m.pop("tooling")), ("FIELD_MISSING",), "manifest.tooling"),
    Case("manifest_unknown_field", _mdoc_edit(lambda m: m.update(extra=1)), ("FIELD_UNKNOWN",)),
    Case("manifest_boolean_in_integer_field", _mdoc_edit(lambda m: m["source"].update(database_oid=True)), ("FIELD_TYPE",), "database_oid"),
    Case("manifest_bad_value", _mdoc_edit(lambda m: m["schema"].update(heads_match=False)), ("FIELD_VALUE",), "heads_match"),
    Case("manifest_invalid_calendar_timestamp", _mdoc_edit(lambda m: m.update(created_utc="2026-02-30T18:30:00.000000Z")), ("TIMESTAMP_INVALID",)),
    Case("manifest_inventory_missing_an_entry", _mdoc_edit(lambda m: m["files"].pop()), ("INVENTORY_INVALID",)),
    Case("manifest_inventory_unsafe_path", _mdoc_edit(lambda m: m["files"][0].update(path="../archive.dump")), ("UNSAFE_PATH",)),
    # -- names versus manifest ---------------------------------------------------------------------------------
    Case("directory_artifact_id_differs", lambda tmp: build_case_name(tmp, f"napsedge-{LOGICAL}-20261002T183000Z-{OTHER_ID}"), ("NAME_MISMATCH",), "artifact_id"),
    Case("directory_logical_name_differs", lambda tmp: build_case_name(tmp, f"napsedge-other-20261002T183000Z-{ARTIFACT_ID}"), ("NAME_MISMATCH",), "logical_name"),
    Case("directory_time_differs", lambda tmp: build_case_name(tmp, f"napsedge-{LOGICAL}-20261002T183001Z-{ARTIFACT_ID}"), ("NAME_MISMATCH",), "created_utc"),
    # -- inventory sizes and digests ---------------------------------------------------------------------------
    Case("archive_one_bit_flipped", lambda tmp: build_case_tamper(tmp, "archive.dump", "flip"), ("FILE_DIGEST_MISMATCH",), "archive.dump"),
    Case("archive_truncated", lambda tmp: build_case_tamper(tmp, "archive.dump", "truncate"), ("FILE_SIZE_MISMATCH",), "archive.dump"),
    Case("archive_extended", lambda tmp: build_case_tamper(tmp, "archive.dump", "append"), ("FILE_SIZE_MISMATCH",), "archive.dump"),
    Case("archive_emptied", lambda tmp: build_case_tamper(tmp, "archive.dump", "empty"), ("FILE_SIZE_MISMATCH",), "archive.dump"),
    Case("baseline_one_bit_flipped", lambda tmp: build_case_tamper(tmp, "baseline.json", "flip"), ("FILE_DIGEST_MISMATCH",), "baseline.json"),
    Case("baseline_truncated", lambda tmp: build_case_tamper(tmp, "baseline.json", "truncate"), ("FILE_SIZE_MISMATCH",), "baseline.json"),
    Case("archive_and_baseline_both_tampered", lambda tmp: (lambda d: (build_case_tamper_in_place(d, "archive.dump", "flip"), build_case_tamper_in_place(d, "baseline.json", "flip"), d)[-1])(good(tmp)), ("FILE_DIGEST_MISMATCH", "FILE_DIGEST_MISMATCH")),
    # -- the baseline document ----------------------------------------------------------------------------------
    Case("baseline_not_json", lambda tmp: build_case_baseline_raw(tmp, b"not json"), ("JSON_MALFORMED",), "baseline.json"),
    Case("baseline_not_canonical", lambda tmp: build_case_baseline_raw(tmp, json.dumps(baseline_doc(), indent=1, sort_keys=True).encode() + b"\n"), ("NOT_CANONICAL",)),
    Case("baseline_duplicate_key", lambda tmp: build_case_baseline_raw(tmp, oracle(baseline_doc()).replace(b'{"acls"', b'{"acls":[],"acls"', 1)), ("JSON_DUPLICATE_KEY",)),
    Case("baseline_float", lambda tmp: build_case_baseline_raw(tmp, oracle(baseline_doc()).replace(b'"max_alias_id":3', b'"max_alias_id":3.0')), ("JSON_FLOAT",)),
    Case("baseline_unsupported_version", _bdoc_edit(lambda d: d.update(format="napsedge.backup-baseline/9")), ("UNSUPPORTED_VERSION",)),
    Case("baseline_duplicate_logical_key", _bdoc_edit(lambda d: d["fingerprints"].append(["teams", 0, "empty"])), ("BASELINE_DUPLICATE_KEY",)),
    Case("baseline_boolean_in_integer_field", _bdoc_edit(lambda d: d["fingerprints"].__setitem__(0, ["alembic_version", True, "0d9f4c1a7b6e2f8d3a5c9e1b4f7a2d60"])), ("FIELD_TYPE",)),
    Case("baseline_for_another_artifact", _bdoc_edit(lambda d: d.update(artifact_id=OTHER_ID)), ("BASELINE_ARTIFACT_MISMATCH",)),
    Case("baseline_summary_table_count_differs", lambda tmp: build_case_rebound_manifest_edit(tmp, lambda m: m["baseline"].update(table_count=99)), ("BASELINE_SUMMARY_MISMATCH",)),
    Case("baseline_summary_row_total_differs", lambda tmp: build_case_rebound_manifest_edit(tmp, lambda m: m["baseline"].update(row_total=1)), ("BASELINE_SUMMARY_MISMATCH",)),
    Case("baseline_captured_before_the_capture_started", _bdoc_edit(lambda d: d.update(captured_utc="2026-10-02T18:29:59.999999Z")), ("BASELINE_TIME_OUTSIDE_CAPTURE",)),
    Case("baseline_captured_after_the_capture_finished", _bdoc_edit(lambda d: d.update(captured_utc="2026-10-02T18:30:03.482912Z")), ("BASELINE_TIME_OUTSIDE_CAPTURE",)),
    Case("baseline_sequence_details_mismatch", _bdoc_edit(lambda d: d["sequence_details"].pop()), ("BASELINE_SEQUENCE_MISMATCH",)),
    Case("baseline_version_differs_from_the_manifest_schema_head", _bdoc_edit(lambda d: d.update(version="ffffffffffff")), ("BASELINE_SCHEMA_VERSION_MISMATCH",), "ffffffffffff"),
    Case("baseline_version_is_null_but_the_manifest_names_a_head", _bdoc_edit(lambda d: d.update(version=None)), ("BASELINE_SCHEMA_VERSION_MISMATCH",), "None"),
    Case("baseline_version_differs_only_in_letter_case", _bdoc_edit(lambda d: d.update(version="5B02BEA0EB85")), ("BASELINE_SCHEMA_VERSION_MISMATCH",), "5B02BEA0EB85"),
    # -- several problems at once --------------------------------------------------------------------------------
    Case("multiple_independent_problems_are_all_reported", lambda tmp: (lambda d: (build_case_tamper_in_place(d, "archive.dump", "truncate"), (d / "extra.bin").write_bytes(b"x"), d)[-1])(good(tmp)), ("UNLISTED_FILE", "FILE_SIZE_MISMATCH")),
]


def build_case_tamper_in_place(directory: Path, file_name: str, how: str) -> None:
    path = directory / file_name
    data = path.read_bytes()
    path.write_bytes({"flip": data[:10] + bytes([data[10] ^ 0x01]) + data[11:], "truncate": data[:-1], "append": data + b"x", "empty": b""}[how])


@pytest.mark.parametrize("case", DIRECTORY_CASES, ids=[c.id for c in DIRECTORY_CASES])
def test_directory_defects_produce_exactly_their_documented_reason_codes(case, tmp_path):
    directory = case.build(tmp_path)
    expect(verify_artifact_directory(directory), *case.expected, detail=case.detail)


def test_interrupted_finalization_never_looks_valid_at_any_stage(tmp_path):
    """Every stage of the specification's finalization order, as a directory a crash could leave behind."""
    parts = Parts()
    stages = {}
    # stage A: files written in .partial, RUNNING present, no manifest or marker yet
    a = tmp_path / "a"; a.mkdir()
    stages["A_files_only"] = materialize(a, parts, directory_name=f".partial-{ARTIFACT_ID}")
    for gone in ("manifest.json", "COMPLETE"):
        (stages["A_files_only"] / gone).unlink()
    (stages["A_files_only"] / "RUNNING").write_text("{}\n")
    # stage B: manifest written, RUNNING still present, no marker
    b = tmp_path / "b"; b.mkdir()
    stages["B_manifest_written"] = materialize(b, parts, directory_name=f".partial-{ARTIFACT_ID}")
    (stages["B_manifest_written"] / "COMPLETE").unlink()
    (stages["B_manifest_written"] / "RUNNING").write_text("{}\n")
    # stage C: RUNNING removed, marker not yet written
    c = tmp_path / "c"; c.mkdir()
    stages["C_running_removed"] = materialize(c, parts, directory_name=f".partial-{ARTIFACT_ID}")
    (stages["C_running_removed"] / "COMPLETE").unlink()
    # stage D: marker written, directory not yet renamed
    d = tmp_path / "d"; d.mkdir()
    stages["D_marker_written_not_renamed"] = materialize(d, parts, directory_name=f".partial-{ARTIFACT_ID}")
    for stage, directory in stages.items():
        verdict = verify_artifact_directory(directory)
        assert not verdict.valid and codes(verdict) == ["NOT_FINAL"], f"{stage}: {describe(verdict)}"
    # stage E: renamed -- the only state that verifies
    e = tmp_path / "e"; e.mkdir()
    final = materialize(e, parts)
    assert verify_artifact_directory(final).valid


def test_a_defective_writer_that_publishes_with_running_still_present_is_caught(tmp_path):
    directory = good(tmp_path)
    (directory / "RUNNING").write_text("{}\n")
    expect(verify_artifact_directory(directory), "CONFLICTING_MARKERS")


# --------------------------------------------------------------------------- expectations about identity
def test_identity_expectations_are_checked_only_when_supplied(tmp_path):
    directory = good(tmp_path)
    assert verify_artifact_directory(directory, ExpectedIdentity()).valid
    expect(verify_artifact_directory(directory, ExpectedIdentity(system_identifier="1")), "IDENTITY_MISMATCH", detail="system_identifier")
    expect(verify_artifact_directory(directory, ExpectedIdentity(database_name="other")), "IDENTITY_MISMATCH", detail="database_name")
    expect(verify_artifact_directory(directory, ExpectedIdentity(alembic_head="ffffffffffff")), "SCHEMA_HEAD_MISMATCH")
    verdict = verify_artifact_directory(directory, ExpectedIdentity(system_identifier="1", database_name="other", alembic_head="ffffffffffff"))
    assert codes(verdict) == ["IDENTITY_MISMATCH", "IDENTITY_MISMATCH", "SCHEMA_HEAD_MISMATCH"]


# --------------------------------------------------------------------------- what the verifier must NOT conclude
def test_a_garbage_archive_with_correct_digests_is_structurally_valid_and_the_verdict_says_it_proves_no_restore(tmp_path):
    """LIMIT, asserted on purpose: matching hashes are not evidence that the archive is a real dump or restores."""
    directory = good(tmp_path, archive=b"this is not a pg_dump archive at all\n")
    verdict = verify_artifact_directory(directory)
    assert verdict.valid
    assert "restore_verified" in verdict.does_not_establish and "restore_verified" not in verdict.establishes


def test_manifest_consistency_flags_are_claims_the_verifier_does_not_weigh(tmp_path):
    """LIMIT: a manifest asserting a failed catalog bracket or a changed snapshot is just as structurally valid as one asserting success."""
    for index, flags in enumerate(({"catalog_bracket_equal": True, "snapshot_unchanged_at_end": True},
                                   {"catalog_bracket_equal": False, "snapshot_unchanged_at_end": False})):
        parts = Parts()
        parts.mdoc["consistency"].update(flags)
        parent = tmp_path / f"case_{index}"
        parent.mkdir()
        verdict = verify_artifact_directory(materialize(parent, parts))
        assert verdict.valid, describe(verdict)
        assert "database_consistency" in verdict.does_not_establish and "database_consistency" not in verdict.establishes


def test_a_rewritten_manifest_with_a_recomputed_marker_still_verifies_so_the_marker_is_not_authenticity(tmp_path):
    """LIMIT: COMPLETE binds against accident, not against someone who can rewrite every file."""
    parts = Parts()
    parts.mdoc["tooling"]["version"] = "tampered-but-consistent"
    verdict = verify_artifact_directory(materialize(tmp_path, parts))
    assert verdict.valid and verdict.manifest.tooling.version == "tampered-but-consistent"
    assert "authenticity" in verdict.does_not_establish


# --------------------------------------------------------------------------- resource handling
def test_files_are_hashed_by_streaming_not_loaded_into_memory(tmp_path):
    chunk = b"\x5a" * (1 << 20)
    archive_path_size = 16
    archive = chunk * archive_path_size
    parts = Parts(archive=archive)
    directory = materialize(tmp_path, parts)
    del archive, parts
    tracemalloc.start()
    try:
        verdict = verify_artifact_directory(directory)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert verdict.valid
    assert peak < 6 * (1 << 20), f"peak traced memory {peak} bytes: the 16 MiB archive appears to have been loaded"


def test_size_limits_are_applied_before_any_json_is_loaded(tmp_path, monkeypatch):
    parsed: list[str] = []
    monkeypatch.setattr(ba, "parse_manifest", lambda data: parsed.append("manifest") or pytest.fail("manifest was parsed"))
    monkeypatch.setattr(ba, "MAX_MANIFEST_BYTES", 100)
    directory = good(tmp_path)
    verdict = verify_artifact_directory(directory)
    assert codes(verdict) == ["FILE_TOO_LARGE"] and verdict.reasons[0].detail == "manifest.json" and parsed == []

    monkeypatch.undo()
    monkeypatch.setattr(ba, "parse_baseline", lambda data: pytest.fail("baseline was parsed"))
    monkeypatch.setattr(ba, "MAX_BASELINE_BYTES", 100)
    other = tmp_path / "second"
    other.mkdir()
    verdict = verify_artifact_directory(good(other))
    assert codes(verdict) == ["FILE_TOO_LARGE"] and verdict.reasons[0].detail == "baseline.json"


def test_the_marker_size_limit_is_applied_before_reading(tmp_path):
    directory = good(tmp_path)
    (directory / "COMPLETE").write_bytes(b"x" * 100_000)
    expect(verify_artifact_directory(directory), "MARKER_MALFORMED")


def test_an_unreadable_required_file_is_reported_not_raised(tmp_path, monkeypatch):
    directory = good(tmp_path)
    real_open = os.open

    def deny(path, flags, *args, **kwargs):
        if str(path).endswith("archive.dump"):
            raise PermissionError("denied")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(ba.os, "open", deny)
    expect(verify_artifact_directory(directory), "UNREADABLE_FILE", detail="archive.dump")


def snapshot(directory: Path) -> dict:
    result = {"root_mtime_ns": os.stat(directory).st_mtime_ns}
    for entry in sorted(os.scandir(directory), key=lambda e: e.name):
        st = entry.stat(follow_symlinks=False)
        result[entry.name] = (st.st_size, st.st_mtime_ns, sha256(Path(entry.path).read_bytes()) if entry.is_file(follow_symlinks=False) else "dir")
    return result


@pytest.mark.parametrize("case", [c for c in DIRECTORY_CASES if c.id not in {"path_does_not_exist", "path_is_a_file"}][::4], ids=lambda c: c.id)
def test_the_verifier_never_modifies_its_input_valid_or_invalid(case, tmp_path):
    directory = case.build(tmp_path)
    before = snapshot(directory)
    verify_artifact_directory(directory)
    verify_artifact_directory(directory)
    assert snapshot(directory) == before, "verification changed the directory"


def test_a_valid_artifact_can_be_verified_when_every_file_is_read_only(tmp_path):
    directory = good(tmp_path)
    paths = [directory / n for n in ba.ARTIFACT_FILES]
    try:
        for p in paths:
            os.chmod(p, stat.S_IREAD)
        before = snapshot(directory)
        assert verify_artifact_directory(directory).valid
        assert snapshot(directory) == before
    finally:
        for p in paths:
            os.chmod(p, stat.S_IREAD | stat.S_IWRITE)


# --------------------------------------------------------------------------- symlinks and reparse points
def make_symlink(link: Path, target: Path, *, directory: bool) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError, AttributeError) as exc:
        pytest.skip(f"cannot create symbolic links here ({type(exc).__name__}): symlink coverage NOT exercised on this platform/user")


@pytest.mark.parametrize("name", list(ba.ARTIFACT_FILES))
def test_a_symlink_in_place_of_a_required_file_is_rejected(name, tmp_path):
    directory = good(tmp_path)
    target = tmp_path / "elsewhere.bin"
    target.write_bytes((directory / name).read_bytes())
    (directory / name).unlink()
    make_symlink(directory / name, target, directory=False)
    verdict = verify_artifact_directory(directory)
    assert verdict.status == "INVALID" and codes(verdict)[0] == "SYMLINK" and verdict.reasons[0].detail == name, describe(verdict)


def test_a_symlink_as_the_artifact_directory_itself_is_rejected(tmp_path):
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    real = good(real_parent)
    link = tmp_path / DIR_NAME
    make_symlink(link, real, directory=True)
    expect(verify_artifact_directory(link), "SYMLINK")


def test_an_extra_symlink_entry_is_reported_as_a_symlink_not_followed(tmp_path):
    directory = good(tmp_path)
    secret = tmp_path / "outside.txt"
    secret.write_text("outside")
    make_symlink(directory / "link.txt", secret, directory=False)
    expect(verify_artifact_directory(directory), "SYMLINK", detail="link.txt")


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions exist only on Windows: reparse-point coverage NOT exercised on this platform")
def test_a_directory_junction_is_rejected_as_a_reparse_point(tmp_path):
    directory = good(tmp_path)
    target = tmp_path / "junction_target"
    target.mkdir()
    completed = subprocess.run(["cmd", "/c", "mklink", "/J", str(directory / "junction"), str(target)], capture_output=True, text=True)
    if completed.returncode != 0:
        pytest.skip("cannot create a junction here: reparse-point coverage NOT exercised")
    verdict = verify_artifact_directory(directory)
    assert verdict.status == "INVALID" and "SYMLINK" in codes(verdict) and any("junction" in r.detail for r in verdict.reasons), describe(verdict)


def test_link_and_reparse_point_predicate_logic_without_creating_real_links():
    """
    LOGIC-ONLY coverage of the predicate behind every SYMLINK reason. It is NOT a
    substitute for the real-symlink tests above, which are skipped on platforms or
    users that cannot create symlinks; it proves the predicate's rules on synthetic stat results.
    """
    from types import SimpleNamespace

    is_link = ba._is_link
    assert is_link(SimpleNamespace(st_mode=stat.S_IFLNK | 0o777))
    assert is_link(SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_file_attributes=0x400))
    assert is_link(SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_file_attributes=0x400 | 0x10))
    assert not is_link(SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_file_attributes=0x20))
    assert not is_link(SimpleNamespace(st_mode=stat.S_IFDIR | 0o755))
    assert not is_link(SimpleNamespace(st_mode=stat.S_IFREG | 0o644))


# --------------------------------------------------------------------------- the artifact path: links anywhere on it (Phase 9B-5A correction)
def make_directory_link(link: Path, target: Path) -> None:
    """
    A REAL directory link: a symbolic link on POSIX, an NTFS junction on Windows
    (the platform's unprivileged directory link). If neither can be created the test
    is skipped with the reason stated, never silently passed.
    """
    if sys.platform == "win32":
        completed = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
        if completed.returncode != 0:
            pytest.skip("cannot create a junction here: directory-link coverage NOT exercised")
        return
    make_symlink(link, target, directory=True)


@dataclass
class LinkLayout:
    work: Path
    real_parent: Path      # work/real            (a real directory)
    real: Path             # work/real/<NAME>     (a valid artifact)
    links: Path            # work/links           (a real directory)
    root_link: Path        # work/links/<NAME>    -> real          (a link NAMED like an artifact)
    parent_link: Path      # work/linked_parent   -> real_parent   (a linked ancestor)
    deep_link: Path        # work/deep_link       -> work/deep     (a linked grandparent; the artifact is deep/x/<NAME>)


def link_layout(tmp_path: Path) -> LinkLayout:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    real = materialize(real_parent, Parts())
    links = tmp_path / "links"
    links.mkdir()
    deep = tmp_path / "deep"
    (deep / "x").mkdir(parents=True)
    materialize(deep / "x", Parts())
    layout = LinkLayout(tmp_path, real_parent, real, links, links / DIR_NAME, tmp_path / "linked_parent", tmp_path / "deep_link")
    make_directory_link(layout.root_link, real)
    make_directory_link(layout.parent_link, real_parent)
    make_directory_link(layout.deep_link, deep)
    return layout


@pytest.fixture()
def content_access(monkeypatch):
    """Records every directory listing and artifact-content read the verifier performs, while still performing them."""
    calls: list[str] = []
    real_read, real_scandir = ba._read_file, ba.os.scandir

    def read(*args, **kwargs):
        calls.append("read")
        return real_read(*args, **kwargs)

    def scandir(*args, **kwargs):
        calls.append("scandir")
        return real_scandir(*args, **kwargs)

    monkeypatch.setattr(ba, "_read_file", read)
    monkeypatch.setattr(ba.os, "scandir", scandir)
    return calls


SEP = os.sep
ROOT_SUFFIXES = sorted({"", SEP, "/", SEP + ".", "/.", SEP * 2, "//", "/./", "/././", SEP + "." + SEP + ".", "/./."})


def reject_before_reading(path: str, content_access: list, *, code: str, role: str) -> None:
    content_access.clear()
    verdict = verify_artifact_directory(path)
    assert verdict.status == "INVALID" and codes(verdict) == [code], f"{path!r}: {describe(verdict)}"
    assert role in verdict.reasons[0].detail, verdict.reasons[0].detail
    assert content_access == [], f"{path!r}: the verifier listed or read content before rejecting the path: {content_access}"


@pytest.mark.parametrize("suffix", ROOT_SUFFIXES, ids=repr)
def test_a_link_named_like_an_artifact_is_rejected_with_every_trailing_variant_before_any_content_read(tmp_path, content_access, suffix):
    layout = link_layout(tmp_path)
    content_access.clear()
    control = verify_artifact_directory(str(layout.real) + suffix)  # the same spelling on a REAL directory is fine ...
    assert control.valid, describe(control)
    assert "read" in content_access and "scandir" in content_access, "the spy did not observe the control's reads, so it proves nothing"
    reject_before_reading(str(layout.root_link) + suffix, content_access, code="SYMLINK", role="artifact directory")  # ... and the link is not


@pytest.mark.parametrize("shape", ["dot_before", "dotdot_via_real_directory", "forward_slashes", "dot_and_trailing"])
def test_dot_and_dotdot_components_cannot_hide_a_link_named_like_an_artifact(tmp_path, content_access, shape):
    layout = link_layout(tmp_path)
    links = str(layout.links)
    path = {
        "dot_before": links + SEP + "." + SEP + DIR_NAME,
        "dotdot_via_real_directory": links + SEP + ".." + SEP + "links" + SEP + DIR_NAME,
        "forward_slashes": str(layout.root_link).replace(SEP, "/") + "/",
        "dot_and_trailing": links + SEP + "." + SEP + DIR_NAME + SEP + "." + SEP,
    }[shape]
    reject_before_reading(path, content_access, code="SYMLINK", role="artifact directory")


@pytest.mark.parametrize("shape", ["plain", "trailing_separator", "trailing_dot", "forward_slashes", "dot_component", "relative_to_cwd"])
def test_an_artifact_beneath_a_linked_parent_is_rejected_before_any_content_read(tmp_path, content_access, monkeypatch, shape):
    layout = link_layout(tmp_path)
    parent = str(layout.parent_link)
    path = {
        "plain": parent + SEP + DIR_NAME,
        "trailing_separator": parent + SEP + DIR_NAME + SEP,
        "trailing_dot": parent + SEP + DIR_NAME + SEP + ".",
        "forward_slashes": (parent + SEP + DIR_NAME).replace(SEP, "/"),
        "dot_component": parent + SEP + "." + SEP + DIR_NAME,
        "relative_to_cwd": "linked_parent" + SEP + DIR_NAME,
    }[shape]
    if shape == "relative_to_cwd":
        monkeypatch.chdir(layout.work)
    reject_before_reading(path, content_access, code="SYMLINK", role="ancestor")


def test_an_artifact_beneath_a_linked_grandparent_is_rejected_before_any_content_read(tmp_path, content_access):
    layout = link_layout(tmp_path)
    path = str(layout.deep_link) + SEP + "x" + SEP + DIR_NAME
    reject_before_reading(path, content_access, code="SYMLINK", role="ancestor")
    content_access.clear()
    assert verify_artifact_directory(str(layout.work / "deep" / "x" / DIR_NAME)).valid, "control: the same artifact by its real path"


def test_dotdot_is_not_collapsed_through_a_link_before_the_link_is_validated(tmp_path, content_access):
    """`linked_parent/../real/<NAME>` would collapse lexically to a valid path; the link in it must still be rejected."""
    layout = link_layout(tmp_path)
    through_link = str(layout.parent_link) + SEP + ".." + SEP + "real" + SEP + DIR_NAME
    reject_before_reading(through_link, content_access, code="SYMLINK", role="ancestor")
    content_access.clear()
    assert verify_artifact_directory(str(layout.work) + SEP + "real" + SEP + ".." + SEP + "real" + SEP + DIR_NAME).valid, "control: the same shape through real directories"


def test_ordinary_spellings_of_a_real_artifact_path_remain_valid(tmp_path, monkeypatch):
    layout = link_layout(tmp_path)
    (layout.real_parent / "sub").mkdir()
    base = str(layout.real)
    for spelling in (base, base + SEP, base + "/", base + SEP + ".", str(layout.real_parent) + SEP + "." + SEP + DIR_NAME,
                     str(layout.real_parent) + SEP + "sub" + SEP + ".." + SEP + DIR_NAME, base + SEP + SEP, Path(base)):
        verdict = verify_artifact_directory(spelling)
        assert verdict.valid, f"{spelling!r}: {describe(verdict)}"
    monkeypatch.chdir(layout.real_parent)
    for relative in (DIR_NAME, "." + SEP + DIR_NAME + SEP, "sub" + SEP + ".." + SEP + DIR_NAME):
        assert verify_artifact_directory(relative).valid, relative
    monkeypatch.chdir(layout.real)
    assert verify_artifact_directory(".").valid
    assert verify_artifact_directory(".." + SEP + DIR_NAME).valid


def test_missing_non_directory_and_malformed_paths_are_rejected_before_any_content_read(tmp_path, content_access):
    layout = link_layout(tmp_path)
    a_file = tmp_path / "a_file"
    a_file.write_text("x")
    reject_before_reading(str(tmp_path / "missing" / DIR_NAME), content_access, code="NOT_A_DIRECTORY", role="missing")
    reject_before_reading(str(a_file / DIR_NAME), content_access, code="NOT_A_DIRECTORY", role="a_file")
    reject_before_reading(str(layout.real) + "\x00", content_access, code="NOT_A_DIRECTORY", role="NUL")
    reject_before_reading(str(tmp_path / "does_not_exist"), content_access, code="NOT_A_DIRECTORY", role="does_not_exist")
    if sys.platform == "win32":
        reject_before_reading("C:relative", content_access, code="NOT_A_DIRECTORY", role="drive-relative")
    with pytest.raises(TypeError):
        verify_artifact_directory(b"/bytes/path")  # a bytes path is a programming error, not an artifact defect


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only fact: Windows does not resolve a junction for `lstat(link + separator)` the way POSIX resolves a symlink")
def test_the_posix_semantics_that_make_a_naive_lstat_unsafe_are_real(tmp_path):
    """Documents WHY the walk exists: on POSIX a trailing separator makes lstat describe the link's target. Skipped on Windows, where this was not observed."""
    layout = link_layout(tmp_path)
    assert ba._is_link(os.lstat(str(layout.root_link))) is True
    assert ba._is_link(os.lstat(str(layout.root_link) + "/")) is False, "lstat with a trailing slash resolved the link"
    assert ba._is_link(os.lstat(str(layout.root_link) + "/.")) is False


# --------------------------------------------------------------------------- schema provenance across documents (Phase 9B-5A correction)
def assert_bindings_intact(directory: Path) -> None:
    """Independent proof that digests, sizes and the marker are all correct, so only the cross-document check can fail."""
    manifest_bytes = (directory / "manifest.json").read_bytes()
    marker = parse_completion_marker((directory / "COMPLETE").read_bytes())
    assert marker.manifest_sha256 == sha256(manifest_bytes) and marker.manifest_bytes == len(manifest_bytes)
    for entry in parse_manifest(manifest_bytes).files:
        data = (directory / entry.path).read_bytes()
        assert entry.bytes == len(data) and entry.sha256 == sha256(data), entry.path


@pytest.mark.parametrize("version", ["ffffffffffff", None, "5B02BEA0EB85", "0e8d5e6e4e89"])
def test_a_baseline_version_that_differs_from_the_manifest_head_is_rejected_although_every_digest_and_the_marker_are_correct(tmp_path, version):
    parts = edit_baseline(Parts(), lambda d: d.update(version=version))
    directory = materialize(tmp_path, parts)
    assert_bindings_intact(directory)
    assert parse_manifest((directory / "manifest.json").read_bytes()).schema.alembic_head == "5b02bea0eb85"
    standalone = parse_baseline((directory / "baseline.json").read_bytes())  # a standalone baseline may carry any version, including null
    assert standalone.version == version
    expect(verify_artifact_directory(directory), "BASELINE_SCHEMA_VERSION_MISMATCH")


def test_matching_schema_versions_pass_whatever_the_head_is(tmp_path):
    for head in ("5b02bea0eb85", "aaaaaaaaaaaa"):
        parts = Parts()
        parts.mdoc["schema"].update(alembic_head=head, heads_match=(head == parts.mdoc["schema"]["expected_head"]))
        edit_baseline(parts, lambda d, head=head: d.update(version=head))
        parent = tmp_path / f"case_{head}"
        parent.mkdir()
        directory = materialize(parent, parts)
        assert_bindings_intact(directory)
        verdict = verify_artifact_directory(directory)
        assert verdict.valid, describe(verdict)
        assert verdict.baseline.version == verdict.manifest.schema.alembic_head == head


def test_a_head_that_differs_from_the_expected_head_is_recorded_not_rejected_as_a_structural_defect(tmp_path):
    """LIMIT, asserted on purpose: internally consistent metadata is not database consistency, and `expected_head` is not compared to the actual head."""
    parts = Parts()
    parts.mdoc["schema"].update(alembic_head="aaaaaaaaaaaa", expected_head="5b02bea0eb85", heads_match=False)
    edit_baseline(parts, lambda d: d.update(version="aaaaaaaaaaaa"))
    verdict = verify_artifact_directory(materialize(tmp_path, parts))
    assert verdict.valid, describe(verdict)
    assert verdict.manifest.schema.heads_match is False
    assert "database_consistency" in verdict.does_not_establish and "restore_verified" in verdict.does_not_establish


def test_standalone_baseline_parsing_and_the_adapter_round_trip_still_accept_a_null_version():
    document = baseline_doc()
    document["version"] = None
    assert parse_baseline(oracle(document)).version is None
    original = dataclasses.replace(realistic_database_baseline(), version=None)
    wire = baseline_to_document(original, artifact_id=ARTIFACT_ID, captured_utc=CAPTURED, sequence_details=realistic_sequence_details()).to_bytes()
    assert baseline_from_document(parse_baseline(wire))[0] == original


# --------------------------------------------------------------------------- the module's own constraints
MODULE_PATH = REPO_ROOT / "src" / "deployment" / "backup_artifact.py"


def module_ast() -> ast.Module:
    return ast.parse(MODULE_PATH.read_text(encoding="utf-8"))


def test_production_module_imports_nothing_from_tests_and_nothing_that_does_io_beyond_the_standard_library():
    imported = set()
    for node in ast.walk(module_ast()):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    forbidden = {"tests", "subprocess", "socket", "sqlalchemy", "psycopg", "psycopg2", "urllib", "http", "requests", "shutil", "tempfile",
                 "multiprocessing", "asyncio", "sched", "threading", "ssl", "ftplib", "smtplib", "alembic", "pydantic", "src"}
    assert imported.isdisjoint(forbidden), sorted(imported & forbidden)
    assert imported <= {"datetime", "hashlib", "json", "os", "re", "stat", "dataclasses", "typing", "__future__"}, sorted(imported)


def test_production_module_contains_no_write_rename_delete_or_process_operations():
    forbidden_calls = {"rename", "replace", "remove", "unlink", "rmdir", "mkdir", "makedirs", "symlink", "chmod", "utime", "write", "writelines",
                       "truncate", "fsync", "system", "popen", "Popen", "run", "check_output", "copy", "copyfile", "move"}
    found = {node.attr for node in ast.walk(module_ast()) if isinstance(node, ast.Attribute)} & forbidden_calls
    assert found == set(), f"write-side or process operations found: {sorted(found)}"
    source = MODULE_PATH.read_text(encoding="utf-8")
    for flag in ("O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND"):
        assert flag not in source, f"{flag} would make the verifier able to write"


def test_importing_the_module_performs_no_network_process_or_write_activity():
    script = (
        "import sys, json, hashlib, os, re, stat, datetime, dataclasses, typing\n"
        "events = []\n"
        "def hook(event, args):\n"
        "    if event.startswith('socket.') or event in ('subprocess.Popen', 'os.system', 'os.mkdir', 'os.remove', 'os.rename', 'os.rmdir', 'shutil.copyfile', 'os.symlink', 'os.chmod'):\n"
        "        events.append(event)\n"
        "    elif event == 'open':\n"
        "        mode = str(args[1]) if len(args) > 1 and args[1] is not None else 'r'\n"
        "        path = str(args[0])\n"
        "        if any(flag in mode for flag in 'wax+') or not path.endswith(('.py', '.pyc')):\n"
        "            events.append('open:' + mode + ':' + os.path.basename(path))\n"
        "sys.addaudithook(hook)\n"
        "import src.deployment.backup_artifact\n"
        "print(json.dumps(events))\n"
    )
    completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
                               env={**{k: v for k, v in os.environ.items() if not k.startswith(("PG", "NAPSEDGE_DATABASE"))}, "PYTHONDONTWRITEBYTECODE": "1"})
    assert completed.returncode == 0, completed.stderr[-400:]
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == [], completed.stdout


def test_every_documented_reason_code_is_exercised_by_a_table_in_this_file():
    exercised = {c for case in DIRECTORY_CASES for c in case.expected}
    exercised |= {c for _, _, c in JSON_CASES} | {c for _, _, c in SERIALIZATION_CASES} | {c for _, _, c in MARKER_CASES}
    exercised |= {c for _, _, c in BASELINE_VALUE_CASES} | {c for _, _, c in MANIFEST_TYPE_CASES}
    # codes asserted by dedicated tests rather than a table: the oversize files, an unreadable file, symlinks, and the caller's expectations
    exercised |= {"FILE_TOO_LARGE", "UNREADABLE_FILE", "SYMLINK", "IDENTITY_MISMATCH", "SCHEMA_HEAD_MISMATCH"}
    assert exercised == set(ba.REASON_CODES), {"undocumented": exercised - set(ba.REASON_CODES), "never_exercised": set(ba.REASON_CODES) - exercised}


# =========================================================================== 8. baseline compatibility (test-layer adapter)
def realistic_database_baseline() -> DatabaseBaseline:
    """Values with the exact Python shapes real captures return (inspected against a live PostgreSQL in Phase 9B-5A)."""
    return DatabaseBaseline(
        acls=[("team_aliases", "r", "napsedge_migrator", "napsedge_migrator=arwdDxt/napsedge_migrator,napsedge_backup_ro=r/napsedge_migrator"),
              ("team_aliases_id_seq", "S", "napsedge_migrator", ""), ("teams", "r", "napsedge_migrator", "")],
        fingerprints={"teams": (0, "empty"), "alembic_version": (1, "0d9f4c1a7b6e2f8d3a5c9e1b4f7a2d60"), "team_aliases": (3, "5be0c3a91d2e47f6a8b0c1d2e3f40516")},
        constraints=[("team_aliases", "pk_team_aliases", "p", True, "PRIMARY KEY (id)"),
                     ("teams", "ck_teams_status", "c", True, "CHECK (((status)::text = ANY ((ARRAY['a'::character varying, 'b'::character varying])::text[])))"),
                     ("teams", "fk_teams_conference", "f", False, "FOREIGN KEY (conference_id) REFERENCES conferences(conference_id) NOT VALID")],
        indexes=[("teams", "pk_teams", "CREATE UNIQUE INDEX pk_teams ON public.teams USING btree (team_id)")],
        sequences=[("team_aliases_id_seq", 3), ("source_artifacts_id_seq", None)],
        version="5b02bea0eb85",
        max_alias_id=3,
    )


def realistic_sequence_details() -> tuple[SequenceDetail, ...]:
    big = 9223372036854775807
    return (SequenceDetail("team_aliases_id_seq", ("team_aliases", "id"), "bigint", 1, 1, big, 1, False, 3, True, 3),
            SequenceDetail("source_artifacts_id_seq", ("source_artifacts", "id"), "bigint", 1, 1, big, 1, False, None, False, None))


def test_the_adapter_round_trip_reproduces_an_identical_database_baseline_through_the_production_schema():
    original = realistic_database_baseline()
    document = baseline_to_document(original, artifact_id=ARTIFACT_ID, captured_utc=CAPTURED, sequence_details=realistic_sequence_details())
    wire = document.to_bytes()
    assert wire == oracle(json.loads(wire)), "the production serialization is canonical"
    restored, details = baseline_from_document(parse_baseline(wire))
    assert restored == original
    assert details == realistic_sequence_details()
    # types survive: tuples stay tuples, the fingerprints dict stays a dict of tuples, nulls stay null
    assert isinstance(restored.fingerprints, dict) and all(isinstance(v, tuple) for v in restored.fingerprints.values())
    assert all(isinstance(row, tuple) for rows in (restored.acls, restored.constraints, restored.indexes, restored.sequences) for row in rows)
    assert restored.sequences[1] == ("source_artifacts_id_seq", None) and restored.fingerprints["teams"] == (0, "empty")
    assert restored.constraints[2][3] is False and restored.acls[1][3] == ""


def test_fingerprints_wire_form_is_a_sorted_list_and_the_mapping_is_reversible_for_any_insertion_order():
    original = realistic_database_baseline()
    document = baseline_to_document(original, artifact_id=ARTIFACT_ID, captured_utc=CAPTURED, sequence_details=realistic_sequence_details())
    wire = json.loads(document.to_bytes())
    assert wire["fingerprints"] == [["alembic_version", 1, "0d9f4c1a7b6e2f8d3a5c9e1b4f7a2d60"], ["team_aliases", 3, "5be0c3a91d2e47f6a8b0c1d2e3f40516"], ["teams", 0, "empty"]]
    reordered = dataclasses.replace(original, fingerprints=dict(reversed(list(original.fingerprints.items()))))
    assert baseline_to_document(reordered, artifact_id=ARTIFACT_ID, captured_utc=CAPTURED, sequence_details=realistic_sequence_details()).to_bytes() == document.to_bytes()
    assert baseline_from_document(parse_baseline(document.to_bytes()))[0].fingerprints == reordered.fingerprints


def test_the_adapter_refuses_a_fingerprint_that_is_not_a_rows_md5_tuple():
    broken = dataclasses.replace(realistic_database_baseline(), fingerprints={"teams": [0, "empty"]})
    with pytest.raises(TypeError):
        baseline_to_document(broken, artifact_id=ARTIFACT_ID, captured_utc=CAPTURED)


def test_every_comparison_field_of_database_baseline_has_a_document_field_and_nothing_else_drifted():
    assert schema_field_drift() == {"only_in_database_baseline": set(), "only_in_document": set()}
    assert {f.name for f in dataclasses.fields(DatabaseBaseline)} == {"acls", "fingerprints", "constraints", "indexes", "sequences", "version", "max_alias_id"}
    document_keys = set(baseline_doc())
    assert document_keys == {"format", "artifact_id", "captured_utc", "sequence_details"} | {f.name for f in dataclasses.fields(DatabaseBaseline)}


def test_nulls_in_every_nullable_database_baseline_field_survive_the_round_trip():
    original = dataclasses.replace(realistic_database_baseline(), version=None, max_alias_id=None, sequences=[("a_seq", None), ("b_seq", None)])
    details = tuple(dataclasses.replace(d, name=n) for d, n in zip(realistic_sequence_details(), ("a_seq", "b_seq")))
    wire = baseline_to_document(original, artifact_id=ARTIFACT_ID, captured_utc=CAPTURED, sequence_details=details).to_bytes()
    restored, _ = baseline_from_document(parse_baseline(wire))
    assert restored == original and restored.version is None and restored.max_alias_id is None


def test_the_baseline_document_bytes_equal_between_construction_paths():
    """The typed document, the independent oracle and the parsed-then-reserialized bytes all agree."""
    document = BaselineDocument.from_document(baseline_doc())
    assert document.to_bytes() == oracle(baseline_doc()) == parse_baseline(oracle(baseline_doc())).to_bytes()
