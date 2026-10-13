"""
Test-layer adapter between `DatabaseBaseline` and the production baseline
document (`src.deployment.backup_artifact.BaselineDocument`). Phase 9B-5A.

Why it lives here. The production module defines only the wire shape and its
validation; it must not import the test tree and must not contain capture or
comparison code. `DatabaseBaseline` (tests/postgres/pg_restore_compare.py)
stays the single definition of what a baseline contains and how it is compared.
This module is the only place that knows both, and it exists so tests can prove
the two never drift apart.

Shapes, from inspecting real captures (not assumed):
* `fingerprints` is a DICT `{table: (row_count, md5)}`. The md5 is 32 hex digits,
  or the literal string `"empty"` for a table with no rows.
* `acls`, `constraints`, `indexes`, `sequences` are lists of TUPLES in the
  capture's own `ORDER BY` order. `sequences` last values, `max_alias_id` and
  `version` can be `None`.
* `acls` strings can be `""`, `constraints` carry a real bool for `validated`.

The mapping is explicit and reversible:
* `fingerprints` dict <-> a list of `[table, rows, md5]` sorted by table name
  (a dict has no order, so the wire form fixes one); the reverse rebuilds the
  dict with `(rows, md5)` tuples.
* every other list of tuples <-> a list of arrays in the same order, tuples
  restored on the way back.
* nulls are preserved as nulls; nothing is normalized.
"""
from __future__ import annotations

import dataclasses

from src.deployment.backup_artifact import (
    AclEntry,
    BaselineDocument,
    ConstraintEntry,
    FingerprintEntry,
    IndexEntry,
    SequenceDetail,
    SequenceEntry,
)
from tests.postgres.pg_restore_compare import DatabaseBaseline

#: Fields of the document that are NOT `DatabaseBaseline` fields: the header and the new sequence block.
DOCUMENT_ONLY_FIELDS = frozenset({"artifact_id", "captured_utc", "sequence_details"})


def baseline_to_document(
    baseline: DatabaseBaseline, *, artifact_id: str, captured_utc: str, sequence_details: tuple[SequenceDetail, ...] = ()
) -> BaselineDocument:
    """`sequence_details` is not part of `DatabaseBaseline`; the caller supplies it."""
    for table, value in baseline.fingerprints.items():
        if not (isinstance(value, tuple) and len(value) == 2):
            raise TypeError(f"fingerprints[{table!r}] is not a (rows, md5) tuple")
    return BaselineDocument(
        artifact_id=artifact_id,
        captured_utc=captured_utc,
        fingerprints=tuple(FingerprintEntry(table, rows, md5) for table, (rows, md5) in sorted(baseline.fingerprints.items())),
        constraints=tuple(ConstraintEntry(*row) for row in baseline.constraints),
        indexes=tuple(IndexEntry(*row) for row in baseline.indexes),
        acls=tuple(AclEntry(*row) for row in baseline.acls),
        sequences=tuple(SequenceEntry(*row) for row in baseline.sequences),
        version=baseline.version,
        max_alias_id=baseline.max_alias_id,
        sequence_details=tuple(sequence_details),
    )


def baseline_from_document(document: BaselineDocument) -> tuple[DatabaseBaseline, tuple[SequenceDetail, ...]]:
    """The exact inverse of `baseline_to_document` (plus the sequence block, returned separately)."""
    fingerprints = {entry.table: (entry.rows, entry.md5) for entry in document.fingerprints}
    if len(fingerprints) != len(document.fingerprints):
        raise ValueError("duplicate table in fingerprints")
    baseline = DatabaseBaseline(
        acls=[(a.relation, a.relkind, a.owner, a.acl) for a in document.acls],
        fingerprints=fingerprints,
        constraints=[(c.table, c.name, c.contype, c.validated, c.definition) for c in document.constraints],
        indexes=[(i.table, i.name, i.definition) for i in document.indexes],
        sequences=[(s.name, s.last_value) for s in document.sequences],
        version=document.version,
        max_alias_id=document.max_alias_id,
    )
    return baseline, document.sequence_details


def schema_field_drift() -> dict[str, set[str]]:
    """Field-name differences between `DatabaseBaseline` and the production document (both empty sets = no drift)."""
    database_fields = {f.name for f in dataclasses.fields(DatabaseBaseline)}
    document_fields = {f.name for f in dataclasses.fields(BaselineDocument)} - DOCUMENT_ONLY_FIELDS
    return {"only_in_database_baseline": database_fields - document_fields, "only_in_document": document_fields - database_fields}
