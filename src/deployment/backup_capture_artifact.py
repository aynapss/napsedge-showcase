"""
Phase 9B-6H: the narrow bridge from capture results to the artifact contract (docs/backup_artifact_contract_proposal.md
section 13.16). Two pure functions, no I/O, no SQL, no subprocess, no artifact writing:

* `catalog_observation_record(result)` derives manifest /2's `consistency.catalog_observation` from a
  `NoDifferenceObserved` -- the only success result of `require_unchanged`. It re-runs `require_unchanged` on the result's
  own endpoints (so an incomplete, mismatched or differing pair is refused here too) and copies ONLY facts the observer
  obtained: the method, the categories, the source identity, and per endpoint the PID, `backend_start`, transaction
  start, isolation, read-only setting, cleanup verification and the digests. The record means "no difference observed at
  these two endpoints": not catalog stability, writer exclusion or archive consistency. Each endpoint is a READ COMMITTED
  transaction, not one atomic catalog snapshot, and a change and its reversal between the endpoints is not detected.

* `capture_failure_reason(exc)` maps a capture-phase exception to one of the EXISTING FAILED reason classes, by exception
  TYPE only (never by message, so no message, SQL parameter, URL or credential is consulted or carried). An overlap
  (`AdvisoryLockUnavailableError`) is not a capture failure: it returns None, because the established outcome is
  `SKIPPED:overlap` with nothing created, and the documented coordinator order acquires the lock before reserving.
  Observation-incomplete and cleanup-unconfirmed failures are `internal_error`, never `catalog_changed`.

The future guarded coordinator (not implemented) is the only intended caller.
"""
from __future__ import annotations

import datetime as _dt

from src.deployment.backup_artifact import (
    CATALOG_OBSERVATION_CATEGORIES,
    CATALOG_OBSERVATION_METHOD,
    CATALOG_OBSERVATION_RESULT,
    CatalogDigests,
    CatalogEndpointRecord,
    CatalogObservationRecord,
    ObservedSource,
)
from src.deployment.backup_capture_catalog import CATEGORIES, CatalogChanged, CatalogEndpoint, NoDifferenceObserved, require_unchanged
from src.deployment.backup_capture_dump import DumpFailed, DumpPreconditionError, DumpTimeout
from src.deployment.backup_capture_snapshot import SnapshotRefusedError, SnapshotSessionLost, SnapshotSetupError
from src.exceptions import AdvisoryLockUnavailableError, DatabaseTargetVerificationError, ValidationError

__all__ = ["CAPTURE_FAILURE_CLASSES", "capture_failure_reason", "catalog_observation_record"]

#: (exception types, FAILED reason class), first match wins. Order matters: DumpTimeout is a DumpFailed.
_MAPPING: tuple[tuple[tuple[type[BaseException], ...], str], ...] = (
    ((CatalogChanged,), "catalog_changed"),
    ((DumpTimeout,), "timeout"),
    ((DumpFailed, DumpPreconditionError), "dump_failed"),
    ((SnapshotSetupError, SnapshotSessionLost, SnapshotRefusedError), "snapshot_unavailable"),
    ((DatabaseTargetVerificationError,), "source_identity_mismatch"),
)
#: Every class this mapping can return (all are existing FAILED reason classes; a test checks that).
CAPTURE_FAILURE_CLASSES = frozenset({"interrupted", "internal_error", *(reason for _, reason in _MAPPING)})
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def capture_failure_reason(exc: BaseException) -> str | None:
    """The FAILED reason class for a capture-phase failure, or None for an overlap (not a capture failure). Type-based only."""
    if not isinstance(exc, BaseException):
        raise TypeError("an exception is required")
    if not isinstance(exc, Exception):
        return "interrupted"  # KeyboardInterrupt, SystemExit and other non-Exception interrupts
    if isinstance(exc, AdvisoryLockUnavailableError):
        return None
    for types, reason in _MAPPING:
        if isinstance(exc, types):
            return reason
    return "internal_error"  # observation incomplete, cleanup unconfirmed, a lock no longer confirmed, or anything unexpected


def _utc_text(value: object, what: str) -> str:
    if not isinstance(value, _dt.datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"the {what} must be a timezone-aware datetime")
    return value.astimezone(_dt.timezone.utc).strftime(_TIMESTAMP_FORMAT)


def _endpoint(endpoint: CatalogEndpoint) -> CatalogEndpointRecord:
    digests = endpoint.observation.digests
    return CatalogEndpointRecord(
        label=endpoint.label, backend_pid=endpoint.backend_pid,
        backend_start=_utc_text(endpoint.backend_start, f"{endpoint.label} backend_start"),
        transaction_start=_utc_text(endpoint.transaction_start, f"{endpoint.label} transaction_start"),
        isolation=endpoint.isolation, read_only=endpoint.read_only, cleanup_verification=endpoint.cleanup_verification,
        digests=CatalogDigests(constraints=digests["constraints"], indexes=digests["indexes"], acls=digests["acls"], combined=digests["combined"]),
    )


def catalog_observation_record(result: NoDifferenceObserved) -> CatalogObservationRecord:
    """Manifest /2's catalog observation, from a NoDifferenceObserved only. Refuses anything else; never builds a partial record."""
    if type(result) is not NoDifferenceObserved:
        raise ValidationError("a NoDifferenceObserved result is required; a boolean or any other value is not evidence of the observation")
    if tuple(CATEGORIES) != CATALOG_OBSERVATION_CATEGORIES:
        raise ValidationError("the catalog observer's categories differ from the artifact contract's")
    checked = require_unchanged(result.before, result.after, result.before.source)  # raises CatalogObservationFailed / CatalogChanged
    if result.digests != checked.digests:
        raise ValidationError("the result's digests are not the digests of its endpoints")
    source = result.before.source
    record = CatalogObservationRecord(
        method=CATALOG_OBSERVATION_METHOD, result=CATALOG_OBSERVATION_RESULT, categories=CATALOG_OBSERVATION_CATEGORIES,
        source=ObservedSource(system_identifier=source.system_identifier, database_oid=source.database_oid, database_name=source.database_name),
        endpoints=(_endpoint(result.before), _endpoint(result.after)),
    )
    return CatalogObservationRecord.from_document(record.to_document())  # the contract's own validation decides
