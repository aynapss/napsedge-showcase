"""
Phase 9B-6F: the bounded fresh catalog observer and its refusal check (docs/backup_artifact_contract_proposal.md
sections 6.4, 12.5, 12.8 and 13.14).

Adopted behavior, and nothing more:
* the capture is refused when the tested catalog observations differ between two endpoints;
* it is refused when a required observation, identity verification or cleanup cannot complete;
* equal observations mean only "no difference observed at these endpoints". They do NOT establish continuous catalog
  stability, exclusion of writers, or artifact consistency. A change and its exact reversal between the endpoints is
  NOT detected (12.8 S5), and a write that changes no observed catalog row is never visible here.

Canonical definitions. The observation is the baseline's own three catalog reads (constraints, indexes, and ACL/ownership:
the SAME SQL constants `backup_capture_snapshot` reads the baseline with, which 9B-6C drift-tested statement for
statement against the test layer's `capture_baseline`), digested per category. `CatalogObservation`, its digests,
`changed_categories`, `raw_differences` and `catalog_changed` were MOVED here from the C6 test module
(tests/postgres/test_postgres_backup_catalog_consistency.py), which now imports them. This module is their one
authoritative implementation. Coverage is not broadened: no role attributes, memberships or other object types.

The endpoint (`observe_endpoint`):
* uses a separate, owned physical session (`NullPool`, the lock module's engine builder, so it never reconnects
  silently), never S's exported snapshot and never L's transaction;
* opens one READ COMMITTED, READ ONLY transaction, set on the connection before it begins and checked from the server.
  READ COMMITTED is the isolation the 12.8 candidate was validated with. Each of the three reads therefore takes its
  own statement snapshot, and catalog functions such as `pg_get_indexdef` render current state (12.8): the endpoint is
  NOT an atomic catalog snapshot, and a change landing between its reads can tear it;
* verifies the target and reads its own source identity (`system_identifier`, database OID and name); a missing or
  different identity refuses;
* is bounded: connect timeout, `SET LOCAL lock_timeout`, `statement_timeout`, and a total deadline;
* records PID, `backend_start`, transaction start (re-read after the reads and required unchanged), identity, the raw
  observation and its digests;
* is never retried: a failed observation refuses rather than moving the endpoint later;
* always ends its session, and its absence is confirmed by the lock module's bounded verifier. An unconfirmed
  cleanup refuses.

`require_unchanged(before, after, expected_source)` requires two complete endpoints, labelled `before` and `after`, from
distinct transactions on the expected source. It raises `CatalogChanged` (with the changed categories and raw
differences) on any difference, and `CatalogObservationFailed` for anything incomplete or unverifiable. Its only success
result is a `NoDifferenceObserved`. Nothing here writes an artifact field, reason code or readiness state.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import time
from dataclasses import dataclass, fields
from typing import Callable

from sqlalchemy import text
from sqlalchemy.engine import URL, Connection, Engine

from src.deployment.backup_capture_lock import VERIFIED_CLOSED, _build_engine, _require_null_pool, _type_name, _validated_url, _verify_cleanup
from src.deployment.backup_capture_preflight import _DATABASE_SQL, _SYSTEM_IDENTIFIER_PRIVILEGE_SQL, _SYSTEM_IDENTIFIER_SQL
from src.deployment.backup_capture_snapshot import _ACL_SQL, _CONSTRAINT_SQL, _INDEX_SQL
from src.exceptions import NapsEdgeError, ValidationError
from src.pipeline.bulk_import_safety import DatabaseTargetDesignation, verify_database_target

__all__ = [
    "CATEGORIES",
    "OBSERVER_APPLICATION_NAME",
    "CatalogObservation",
    "changed_categories",
    "raw_differences",
    "catalog_changed",
    "SourceIdentity",
    "CatalogEndpoint",
    "NoDifferenceObserved",
    "CatalogRefusal",
    "CatalogObservationFailed",
    "CatalogChanged",
    "observe_endpoint",
    "require_unchanged",
]

CATEGORIES = ("constraints", "indexes", "acls")  # "acls" is the ACL AND ownership observation (relowner and relacl)
OBSERVER_APPLICATION_NAME = "napsedge_backup_catalog_observer"
_LABELS = ("before", "after")
_LOCK_TIMEOUT_MS = 5000
_TOTAL_DEADLINE_S = 60.0
_TRANSACTION_SQL = (
    "SELECT current_setting('transaction_isolation'), current_setting('transaction_read_only'), pid, backend_start, now() "
    "FROM pg_stat_activity WHERE pid = pg_backend_pid()"
)
_NOW_SQL = "SELECT now()"


# ---------------------------------------------------------------------------
# the canonical pure definitions (moved from the C6 test module; unchanged in behavior)
# ---------------------------------------------------------------------------
def _digest(observed: list) -> str:
    return hashlib.sha256(json.dumps(observed, default=str, separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CatalogObservation:
    """The baseline's own constraint, index and ACL/ownership reads and their digests."""

    constraints: tuple
    indexes: tuple
    acls: tuple

    @property
    def digests(self) -> dict:
        parts = {"constraints": _digest(list(self.constraints)), "indexes": _digest(list(self.indexes)), "acls": _digest(list(self.acls))}
        parts["combined"] = hashlib.sha256("|".join(parts[c] for c in CATEGORIES).encode("ascii")).hexdigest()
        return parts


def changed_categories(before: CatalogObservation, after: CatalogObservation) -> list[str]:
    return [c for c in CATEGORIES if before.digests[c] != after.digests[c]]


def _keyed_differences(left: list[tuple], right: list[tuple], key_columns: int) -> dict:
    """Pairs rows by their leading key columns and keeps the pairs that differ. Kept identical to the test layer's comparator helper by a drift test."""
    left_map = {row[:key_columns]: row for row in left}
    right_map = {row[:key_columns]: row for row in right}
    return {key: (left_map.get(key), right_map.get(key)) for key in sorted(set(left_map) | set(right_map), key=str)
            if left_map.get(key) != right_map.get(key)}


def raw_differences(before: CatalogObservation, after: CatalogObservation) -> dict:
    """Per category, the keyed raw differences (constraints and indexes keyed by table and name, ACL/ownership by relation)."""
    keys = {"constraints": 2, "indexes": 2, "acls": 1}
    return {c: _keyed_differences(list(getattr(before, c)), list(getattr(after, c)), keys[c]) for c in CATEGORIES}


def catalog_changed(before: CatalogObservation, after: CatalogObservation) -> bool:
    return before.digests["combined"] != after.digests["combined"]


# ---------------------------------------------------------------------------
# endpoints, refusals and the comparison result
# ---------------------------------------------------------------------------
class CatalogRefusal(NapsEdgeError):
    """The capture must not proceed on the normal path."""


class CatalogObservationFailed(CatalogRefusal):
    """A required observation, identity verification, record check or cleanup could not complete. Fixed text plus type names only."""


class CatalogChanged(CatalogRefusal):
    """The two endpoints differ. `categories` and `differences` hold the facts."""

    def __init__(self, categories: list[str], differences: dict) -> None:
        super().__init__(f"catalog_changed: the observed catalog differs between the endpoints ({', '.join(categories)})")
        self.categories, self.differences = categories, differences


@dataclass(frozen=True)
class SourceIdentity:
    system_identifier: str
    database_oid: int
    database_name: str


@dataclass(frozen=True)
class CatalogEndpoint:
    label: str
    observation: CatalogObservation
    backend_pid: int
    backend_start: _dt.datetime
    transaction_start: _dt.datetime
    isolation: str
    read_only: str
    source: SourceIdentity
    cleanup_verification: str

    @property
    def complete(self) -> bool:
        return (self.label in _LABELS and isinstance(self.observation, CatalogObservation)
                and all(isinstance(getattr(self.observation, c), tuple) for c in CATEGORIES)
                and isinstance(self.source, SourceIdentity) and all(getattr(self.source, f.name) not in (None, "") for f in fields(SourceIdentity))
                and self.backend_pid is not None and self.backend_start is not None and self.transaction_start is not None
                and (self.isolation, self.read_only) == ("read committed", "on") and self.cleanup_verification == VERIFIED_CLOSED)


@dataclass(frozen=True)
class NoDifferenceObserved:
    """The only success result: equal observations at these two endpoints. Not stability, exclusion or consistency."""

    before: CatalogEndpoint
    after: CatalogEndpoint
    digests: dict
    meaning: str = "no difference observed at these endpoints"


def observe_endpoint(database_url: URL | str, designation: DatabaseTargetDesignation, *, label: str, expected_source: SourceIdentity,
                     _engine_factory: Callable[[URL, str], Engine] | None = None) -> CatalogEndpoint:
    """One fresh, owned, bounded observation (module docstring). Raises `CatalogObservationFailed`; never returns a partial endpoint."""
    if label not in _LABELS:
        raise ValidationError("label must be 'before' or 'after'")
    if not isinstance(designation, DatabaseTargetDesignation) or not isinstance(expected_source, SourceIdentity):
        raise ValidationError("a DatabaseTargetDesignation and the expected SourceIdentity are required")
    url = _validated_url(database_url)
    factory = _engine_factory or _build_engine
    started_at = time.monotonic()
    engine = factory(url, OBSERVER_APPLICATION_NAME)
    connection: Connection | None = None
    pid = backend_start = None
    result: dict = {}
    primary: BaseException | None = None
    try:
        _require_null_pool(engine)
        try:
            connection = engine.connect()
        except Exception as exc:  # noqa: BLE001 -- the driver's message can carry host and user names
            raise CatalogObservationFailed(f"the catalog observer could not connect ({_type_name(exc)})") from None
        try:
            connection.execution_options(isolation_level="READ COMMITTED", postgresql_readonly=True)
            connection.begin()
            connection.execute(text(f"SET LOCAL lock_timeout = {_LOCK_TIMEOUT_MS}"))
            isolation, read_only, pid, backend_start, transaction_start = connection.execute(text(_TRANSACTION_SQL)).one()
            verified = verify_database_target(connection, designation)
            database_oid = int(connection.execute(text(_DATABASE_SQL)).one()[0])
            readable = bool(connection.execute(text(_SYSTEM_IDENTIFIER_PRIVILEGE_SQL)).scalar_one())
            system_identifier = str(connection.execute(text(_SYSTEM_IDENTIFIER_SQL)).scalar_one()) if readable else None
            observation = CatalogObservation(*(tuple(tuple(row) for row in connection.execute(text(sql)).fetchall())
                                               for sql in (_CONSTRAINT_SQL, _INDEX_SQL, _ACL_SQL)))
            same_transaction = connection.execute(text(_NOW_SQL)).scalar_one() == transaction_start
        except CatalogRefusal:
            raise
        except Exception as exc:  # noqa: BLE001 -- a lock timeout, a lost session or any read failure refuses; it is never retried
            raise CatalogObservationFailed(f"the catalog observation did not complete ({_type_name(exc)})") from None
        if (isolation, read_only) != ("read committed", "on"):
            raise CatalogObservationFailed("the catalog observer is not in a READ COMMITTED, READ ONLY transaction")
        if not same_transaction:
            raise CatalogObservationFailed("the catalog observation did not stay in one transaction")
        source = SourceIdentity(system_identifier, database_oid, verified.database_name)
        if system_identifier is None:
            raise CatalogObservationFailed("the catalog observer could not read the cluster system_identifier")
        if source != expected_source:
            mismatched = [f.name for f in fields(SourceIdentity) if getattr(source, f.name) != getattr(expected_source, f.name)]
            raise CatalogObservationFailed(f"the catalog observer is connected to a different source ({', '.join(mismatched)})")
        if time.monotonic() - started_at > _TOTAL_DEADLINE_S:
            raise CatalogObservationFailed("the catalog observation exceeded its total deadline")
        result = dict(observation=observation, transaction_start=transaction_start, isolation=isolation, read_only=read_only, source=source)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        failures: list[str] = []
        interrupt: BaseException | None = None

        def attempt(step: str, action: Callable[[], object]) -> None:
            nonlocal interrupt
            try:
                action()
            except Exception as exc:  # noqa: BLE001 -- a failed step never skips the remaining ones
                failures.append(f"{step}:{_type_name(exc)}")
            except BaseException as exc:  # a NEW interrupt: remaining steps still run, then it propagates
                failures.append(f"{step}:{_type_name(exc)}")
                interrupt = interrupt or exc

        if connection is not None:
            attempt("discard" if primary is not None else "rollback", connection.invalidate if primary is not None else connection.rollback)
            attempt("close", connection.close)
        attempt("dispose", engine.dispose)
        verification = "no_session_created"
        if connection is not None:
            if pid is None or interrupt is not None:
                verification = "not_verified"
            else:
                try:
                    verification, _, verify_failures = _verify_cleanup(factory, url, designation, pid, backend_start)
                    failures += verify_failures
                except BaseException as exc:
                    verification = "not_verified"
                    failures.append(f"verify:{_type_name(exc)}")
                    interrupt = interrupt or exc
        if interrupt is not None:
            raise interrupt
        confirmed = not failures and verification in (VERIFIED_CLOSED, "no_session_created")
        if not confirmed:
            message = f"catalog observer cleanup was not confirmed: verification={verification}; failed steps={','.join(failures) or 'none'}"
            if primary is not None:
                primary.add_note(message)
            else:
                raise CatalogObservationFailed(message)
    return CatalogEndpoint(label=label, backend_pid=pid, backend_start=backend_start, cleanup_verification=verification, **result)


def require_unchanged(before: CatalogEndpoint | None, after: CatalogEndpoint | None, expected_source: SourceIdentity) -> NoDifferenceObserved:
    """The refusal check. Equal, complete, verified endpoints from distinct transactions give `NoDifferenceObserved`; everything else refuses."""
    for label, endpoint in (("before", before), ("after", after)):
        if not isinstance(endpoint, CatalogEndpoint):
            raise CatalogObservationFailed(f"the {label} catalog observation is missing")
        if endpoint.label != label or not endpoint.complete:
            raise CatalogObservationFailed(f"the {label} catalog observation is incomplete or malformed")
        if endpoint.source != expected_source:
            raise CatalogObservationFailed(f"the {label} catalog observation is from a different source")
    if (before.backend_pid, before.backend_start, before.transaction_start) == (after.backend_pid, after.backend_start, after.transaction_start):
        raise CatalogObservationFailed("the two catalog observations are not distinct transactions")
    if after.transaction_start < before.transaction_start:
        raise CatalogObservationFailed("the after observation does not follow the before observation")
    if catalog_changed(before.observation, after.observation):
        raise CatalogChanged(changed_categories(before.observation, after.observation), raw_differences(before.observation, after.observation))
    return NoDifferenceObserved(before, after, before.observation.digests)
