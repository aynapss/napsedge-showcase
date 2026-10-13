"""
Phase 9B-5C2: disposable validation of sequence behavior under the shared-snapshot
capture. Specification section 7 cases C3, C4 and C5 ONLY (section 6.5 is the rule).

What is (and is not) established
--------------------------------
* C3  A sequence advanced by another session between the owner's baseline reads and
      `pg_dump` is NOT frozen by the snapshot: the restored next value is strictly larger
      than the baseline's (N_r > N_b), while the table data still equals the baseline
      exactly. R0, R1, R2 and R3 are evaluated from observed values, the unchanged strict
      comparator reports the sequence difference (it is not suppressed or reinterpreted),
      a no-advancement control and a live uncalled sequence (`is_called = false`) are
      exercised, and a rolled-back insert shows that `nextval` survives a rollback (a gap,
      not corruption).
* C4  A valid row whose explicit id lies above its owning sequence's next value leaves
      table data in parity and R1 true but makes R2 FAIL on the restored column maximum.
      The unsafe condition is reported, not repaired. It does not claim that the very next
      insert must collide; it shows the restored generator is not safely beyond the ids.
* C5  A sequence altered to CYCLE, and one altered to a negative increment, are rejected
      by R0 in the harness path BEFORE any dump; no monotonic acceptance is computed for
      them; an ordinary positive, non-cycling, owned sequence is the control.

These cases say nothing about catalog consistency, DDL, locks, overlap or quiescent mode
(C6, C7, C9 and C10 remain unvalidated), nothing about `equal_v1`, and nothing about
restore verification (claim 4). The rule functions here are TEST-LAYER verification code:
no production sequence verifier exists, and nothing in this module may move a sequence
except an explicit setup or writer action (a static test enforces that).

Reuse
-----
The disposable cluster, snapshot owner, shared-snapshot dump, restore into a verified-empty
database, credentials and cleanup ledger all come from the C1 module's harness (imported,
not copied). The one representation of a sequence's state is the production contract's
`SequenceDetail`; the strict comparator `compare_to_baseline` is used unchanged.

Connection ownership
--------------------
Every baseline read, data and sequences, goes through the snapshot owner's connection, as
the backup role with its existing grants. Sequence state is read directly
(`SELECT last_value, is_called FROM public.<seq>`), never inferred from the
`pg_sequences` view, whose `last_value` is NULL for a not-yet-called sequence and is kept
separately, exactly as the strict comparator holds it.
"""
from __future__ import annotations

import ast
import inspect
import re
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from src.deployment.backup_artifact import SequenceDetail, parse_baseline
from tests.postgres.baseline_document import baseline_to_document
from tests.postgres.pg_restore_compare import SAFE_IDENT, DatabaseBaseline, all_fingerprints, capture_baseline, compare_to_baseline, keyed_diff, rows, scalar
from tests.postgres.test_postgres_backup_snapshot_consistency import (
    BARRIER_TIMEOUT_S,
    CaptureStepFailed,
    CaseLedger,
    Harness,
    SnapshotOwner,
    WriterOutcome,
    _dump_sessions,
    backend_state,
    harness,  # noqa: F401  (module-scoped fixture: ONE disposable cluster for this module)
    owned_case,
    prepare_case_workspace,
    restore_into_fresh_database,
    run_pg_dump,
    start_writer,
)
from tests.postgres.test_postgres_fresh_cluster_recovery import PreconditionFailed
from tests.postgres.test_postgres_roles_and_restore import CommandResult
from tests.postgres.test_postgres_tls_backup_restore import BACKUP_ROLE, isolated_database_environment  # noqa: F401  (autouse module fixture)

pytestmark = pytest.mark.postgres

VALIDATED_CASES = ("C3", "C4", "C5")
UNVALIDATED_CASES = ("C6", "C7", "C9", "C10")

# ---- the documented schema (specification 6.5, verified there from the offline migration SQL) ------------------------------------
DOCUMENTED_TABLES = (
    "team_aliases", "player_aliases", "source_artifacts", "data_quality_issues", "player_stat_snapshots", "roster_memberships", "team_stat_snapshots",
    "team_game_log_observations", "team_game_stat_lines", "espn_benchmark_snapshots", "espn_source_observations", "espn_benchmark_values",
    "player_game_log_observations",
)
DOCUMENTED_OWNERS = {f"{table}_id_seq": (table, "id") for table in DOCUMENTED_TABLES}
DOCUMENTED_PARAMETERS = {"data_type": "bigint", "increment": 1, "minimum": 1, "maximum": 2**63 - 1, "cache": 1, "cycle": False}

CALLED_SEQUENCE = "team_aliases_id_seq"  # seeded rows have drawn from it: is_called = true
UNCALLED_SEQUENCE = "data_quality_issues_id_seq"  # the seed never touches it: is_called = false
ADVANCE_PLAN = ((CALLED_SEQUENCE, 5), (UNCALLED_SEQUENCE, 1))
MANUAL_ID = 1_000_000
HAZARD_TEAM_ID = 910002  # an existing seeded team (the seed's EXTRA_TEAM_ID)


# ==========================================================================
# test-layer verification code: READS and PURE RULES only (no nextval, setval or repair)
# ==========================================================================
_INVENTORY_SQL = """
SELECT s.relname, t.relname, a.attname, q.seqtypid::regtype::text, q.seqincrement, q.seqmin, q.seqmax, q.seqcache, q.seqcycle
FROM pg_class s
JOIN pg_sequence q ON q.seqrelid = s.oid
LEFT JOIN pg_depend d ON d.classid = 'pg_class'::regclass AND d.objid = s.oid AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')
LEFT JOIN pg_class t ON t.oid = d.refobjid
LEFT JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
WHERE s.relkind = 'S' AND s.relnamespace = 'public'::regnamespace
ORDER BY s.relname
"""


def read_sequence_details(source: Engine | Connection) -> tuple[SequenceDetail, ...]:
    """
    Discovers every public sequence with its owning table and column from the catalog, its parameters from `pg_sequence`, its state DIRECTLY
    (`last_value`, `is_called`) and the owning column's maximum, all through the caller's connection (the snapshot owner's, for a baseline). It
    never advances, sets or repairs anything. `last_value` here is the sequence's own value, not the NULL-for-uncalled `pg_sequences` view.
    """
    details = []
    for name, table, column, data_type, increment, minimum, maximum, cache, cycle in rows(source, _INVENTORY_SQL):
        assert SAFE_IDENT.fullmatch(name), name
        last_value, is_called = rows(source, f'SELECT last_value, is_called FROM public."{name}"')[0]
        owned_by, column_max = None, None
        if table is not None and column is not None:
            assert SAFE_IDENT.fullmatch(table) and SAFE_IDENT.fullmatch(column), (table, column)
            owned_by = (table, column)
            column_max = scalar(source, f'SELECT max("{column}") FROM public."{table}"')
        details.append(SequenceDetail(name=name, owned_by=owned_by, data_type=data_type, increment=increment, minimum=minimum, maximum=maximum,
                                      cache=cache, cycle=cycle, last_value=last_value, is_called=is_called, column_max=column_max))
    return tuple(details)


class NotApplicable(Exception):
    """monotonic_v1 does not apply to this sequence (R0 failed): no next value or rule outcome is ever computed for it."""


def applicability(detail: SequenceDetail) -> tuple[bool, tuple[str, ...]]:
    """R0: `increment > 0`, `cycle` false, `owned_by` non-null. Returns (applicable, the reasons it is not)."""
    reasons = []
    if detail.increment <= 0:
        reasons.append("increment_not_positive")
    if detail.cycle:
        reasons.append("cycles")
    if detail.owned_by is None:
        reasons.append("not_owned")
    return (not reasons, tuple(reasons))


def next_value(detail: SequenceDetail) -> int:
    """
    The value the sequence will hand out next, computed WITHOUT drawing anything from it: `last_value` when `is_called` is false, else `last_value + increment`.
    Defined only inside the applicability domain.
    """
    applicable, reasons = applicability(detail)
    if not applicable:
        raise NotApplicable(f"{detail.name}: {', '.join(reasons)}")
    if detail.last_value is None:
        raise ValueError(f"{detail.name}: a direct read always yields a last_value")
    return detail.last_value if not detail.is_called else detail.last_value + detail.increment


@dataclass(frozen=True)
class RuleResult:
    """R0 to R3 for one sequence, kept explicit and separate. No aggregate verdict is computed here: R3 is informational in the specification."""

    sequence: str
    applicable: bool  # R0
    reasons: tuple[str, ...]
    n_baseline: int | None
    n_restored: int | None
    column_max_restored: int | None
    r1_no_regression: bool | None
    r2_next_id_safe: bool | None
    r3_headroom: bool | None  # informational


def evaluate_rules(baseline: Sequence[SequenceDetail], restored: Sequence[SequenceDetail]) -> list[RuleResult]:
    """R1: N_r >= N_b. R2: N_r > the owning column's maximum in the RESTORED database (an empty table passes). R3: N_r <= maximum. Nothing is computed for an R0 failure."""
    restored_by_name = {detail.name: detail for detail in restored}
    results = []
    for before in baseline:
        applicable, reasons = applicability(before)
        if not applicable:
            results.append(RuleResult(before.name, False, reasons, None, None, None, None, None, None))
            continue
        after = restored_by_name[before.name]
        n_baseline, n_restored = next_value(before), next_value(after)
        column_max = after.column_max  # measured on the restored database; never derived from the baseline
        results.append(RuleResult(before.name, True, (), n_baseline, n_restored, column_max,
                                  n_restored >= n_baseline, column_max is None or n_restored > column_max, n_restored <= after.maximum))
    return results


def require_monotonic_applicable(details: Sequence[SequenceDetail]) -> None:
    """The applicability guard of the harness path: refuses, before any dump, a baseline in which any sequence fails R0."""
    rejected = {d.name: applicability(d)[1] for d in details if not applicability(d)[0]}
    if rejected:
        raise PreconditionFailed(f"monotonic_v1 is not applicable (R0): {rejected}; the dump is refused")


# ==========================================================================
# explicit setup and writer actions: the ONLY places a sequence may be moved or altered
# ==========================================================================
def _state(conn: Connection, name: str = CALLED_SEQUENCE) -> tuple[int, bool]:
    assert SAFE_IDENT.fullmatch(name)
    last_value, is_called = conn.execute(text(f'SELECT last_value, is_called FROM public."{name}"')).one()
    return last_value, is_called


def advance_action(plan: Sequence[tuple[str, int]]) -> Callable[[Connection, WriterOutcome], None]:
    """The writer's one transaction: `count` explicit nextval calls on each named owned sequence. Reports the values it drew."""

    def action(conn: Connection, outcome: WriterOutcome) -> None:
        for name, count in plan:
            assert SAFE_IDENT.fullmatch(name)
            for _ in range(count):
                outcome.results.append((name, conn.execute(text("SELECT nextval(CAST(:s AS regclass))"), {"s": f"public.{name}"}).scalar()))

    return action


def setup_rollback_demo(engine: Engine) -> dict:
    """A bounded rollback example: an INSERT draws an id and is rolled back; the table is unchanged but the sequence is not (a gap, not corruption)."""
    with engine.connect() as conn:
        rows_before = conn.execute(text("SELECT count(*) FROM team_aliases")).scalar()
        state_before = _state(conn)
        conn.execute(text("INSERT INTO team_aliases (team_id, source, raw_value, normalized_value) VALUES (:t, 'odds', 'Rolled Back Alias', 'rolled back alias')"),
                     {"t": HAZARD_TEAM_ID})
        conn.rollback()
        state_after = _state(conn)
        gap_id = state_after[0]
        gap_present = conn.execute(text("SELECT count(*) FROM team_aliases WHERE id = :i"), {"i": gap_id}).scalar()
        rows_after = conn.execute(text("SELECT count(*) FROM team_aliases")).scalar()
        conn.rollback()
    return {"state_before": state_before, "state_after": state_after, "rows_before": rows_before, "rows_after": rows_after, "gap_id": gap_id, "gap_row_present": gap_present}


def setup_manual_id(engine: Engine) -> dict:
    """A valid row with an explicit id far above the sequence's next value. The sequence is not advanced (an explicit id never evaluates the default)."""
    with engine.begin() as conn:
        state_before = _state(conn)
        conn.execute(text("INSERT INTO team_aliases (id, team_id, source, raw_value, normalized_value) VALUES (:i, :t, 'odds', 'Manual Id Alias', 'manual id alias')"),
                     {"i": MANUAL_ID, "t": HAZARD_TEAM_ID})
        state_after = _state(conn)
    return {"explicit_id": MANUAL_ID, "state_before": state_before, "state_after": state_after}


def setup_cycle(engine: Engine) -> dict:
    with engine.begin() as conn:
        conn.execute(text(f"ALTER SEQUENCE public.{CALLED_SEQUENCE} CYCLE"))
    return {"altered": CALLED_SEQUENCE, "alteration": "CYCLE"}


def setup_negative_increment(engine: Engine) -> dict:
    with engine.begin() as conn:
        conn.execute(text(f"ALTER SEQUENCE public.{CALLED_SEQUENCE} INCREMENT BY -1"))
    return {"altered": CALLED_SEQUENCE, "alteration": "INCREMENT BY -1"}


# ==========================================================================
# the scenario (C1's harness, with sequence reads, an applicability guard and rule evaluation)
# ==========================================================================
@dataclass(frozen=True)
class Scenario:
    name: str
    case: str
    setup: Callable[[Engine], dict] | None = None
    advance: tuple[tuple[str, int], ...] = ()


SCENARIOS = {
    "c3_advance": Scenario("c3_advance", "C3", advance=ADVANCE_PLAN),
    "c3_control": Scenario("c3_control", "C3"),
    "c3_rollback": Scenario("c3_rollback", "C3", setup=setup_rollback_demo),
    "c4_manual_id": Scenario("c4_manual_id", "C4", setup=setup_manual_id),
    "c5_cycle": Scenario("c5_cycle", "C5", setup=setup_cycle),
    "c5_negative_increment": Scenario("c5_negative_increment", "C5", setup=setup_negative_increment),
}


@dataclass
class SequenceRecord:
    scenario: str
    case: str
    actions: list = field(default_factory=list)
    commands: list = field(default_factory=list)
    source_database: str = ""
    destination_database: str | None = None
    setup: dict = field(default_factory=dict)
    exported_id: str = ""
    descriptor_at_export: str = ""
    descriptor_after_dump: str = ""
    owner_connects_after_reads: int = 0
    baseline: DatabaseBaseline | None = None
    details_baseline: tuple = ()
    rejection: str | None = None
    outcome: str = ""
    writer: WriterOutcome | None = None
    fresh_details_after_advance: tuple = ()
    owner_reread_details: tuple = ()
    owner_fingerprints_after_advance: dict | None = None
    dump: CommandResult | None = None
    dump_sessions_before: int = 0
    dump_sessions_after: int = 0
    owner_backend_state_at_dump_exit: str | None = None
    restored_fingerprints: dict | None = None
    comparison: object = None
    details_restored: tuple = ()
    details_restored_again: tuple = ()
    rules: list | None = None
    sequence_diff: dict | None = None
    artifact_listing: list = field(default_factory=list)
    verification: dict = field(default_factory=dict)


def _migrator_engine(harness_: Harness, database: str, ledger: CaseLedger) -> Engine:
    engine = create_engine(harness_.cluster.migrator_url(database), pool_size=1, max_overflow=0, pool_timeout=15, connect_args={"connect_timeout": 15})
    ledger.engines.append(engine)
    return engine


def run_sequence_case(harness_: Harness, scenario: Scenario, ledger: CaseLedger) -> SequenceRecord:
    cluster = harness_.cluster
    record = SequenceRecord(scenario.name, scenario.case)
    log = record.actions.append

    workspace = prepare_case_workspace(cluster, ledger)  # migrated, seeded, granted exactly as in 9B-2; nothing added for the backup role
    source = record.source_database = workspace.source_database
    log("source_provisioned")
    if scenario.setup is not None:
        record.setup = scenario.setup(_migrator_engine(harness_, source, ledger))  # explicit setup: the only mutation before capture
        log("setup_applied")

    # ---- the owner exports its snapshot; EVERY baseline read (data, then sequences) goes through its connection -------------------------
    owner = SnapshotOwner(cluster, source, ledger)
    log("owner_open")
    record.exported_id = owner.begin_and_export()
    record.descriptor_at_export = owner.descriptor
    log("snapshot_exported")
    connects_before = len(owner.connects)
    record.baseline = capture_baseline(owner.connection)  # data fingerprints, ACLs, ..., and pg_sequences.last_value kept separately
    log("baseline_data_read_through_owner")
    record.details_baseline = read_sequence_details(owner.connection)  # direct last_value / is_called, parameters, owning column, column maximum
    log("baseline_sequences_read_through_owner")
    record.owner_connects_after_reads = len(owner.connects) - connects_before
    if record.owner_connects_after_reads != 0:
        raise PreconditionFailed("a baseline read opened another connection: it did not share the owner's transaction")

    # ---- R0, in the actual harness path, BEFORE anything is dumped -------------------------------------------------------------------------
    try:
        require_monotonic_applicable(record.details_baseline)
    except PreconditionFailed as refusal:
        record.rejection, record.outcome = str(refusal), "monotonic_not_applicable"
        log("applicability_guard_refused")
        owner.end_transaction()
        return record
    log("applicability_guard_passed")

    # ---- C3: a bounded writer advances sequences AFTER the baseline sequence read and BEFORE the dump ---------------------------
    if scenario.advance:
        release, done, record.writer = threading.Event(), threading.Event(), WriterOutcome()
        start_writer(cluster, source, ledger, release, done, record.writer, action=advance_action(scenario.advance))
        release.set()
        log("writer_released")
        if not done.wait(BARRIER_TIMEOUT_S) or not record.writer.committed:
            raise PreconditionFailed(f"the sequence writer did not complete ({record.writer.error})")
        log("writer_completed")
        fresh_engine = create_engine(cluster.backup_url(source), pool_size=1, max_overflow=0, pool_timeout=15, connect_args={"connect_timeout": 15, "application_name": "napsedge_fresh_reader"})
        ledger.engines.append(fresh_engine)
        with fresh_engine.connect() as fresh:
            record.fresh_details_after_advance = read_sequence_details(fresh)
        before_by_name = {d.name: d for d in record.details_baseline}
        for name, count in scenario.advance:  # real advancement is REQUIRED before the dump may start (a writer that drew nothing cannot satisfy C3)
            moved = {d.name: d for d in record.fresh_details_after_advance}[name]
            expected = (before_by_name[name].last_value + count if before_by_name[name].is_called else before_by_name[name].last_value + count - 1)
            if (moved.last_value, moved.is_called) != (expected, True):
                raise PreconditionFailed(f"{name} did not advance by {count}: {(before_by_name[name].last_value, before_by_name[name].is_called)} -> {(moved.last_value, moved.is_called)}")
        log("advancement_confirmed")
        record.owner_reread_details = read_sequence_details(owner.connection)  # sequences are not snapshot-bound: the owner sees the advance too
        record.owner_fingerprints_after_advance = all_fingerprints(owner.connection)  # table data IS snapshot-bound
        log("owner_reread_after_advance")

    # ---- dump with the exported id; the owner stays open until the dump exits -------------------------------------------------------------
    record.dump_sessions_before = _dump_sessions(cluster, source)
    log("dump_started")
    record.dump = run_pg_dump(cluster, source, workspace.archive_path, workspace.backup_pgpass, snapshot=owner.exported_id, commands=record.commands)
    record.dump_sessions_after = _dump_sessions(cluster, source)
    log("dump_exited")
    record.owner_backend_state_at_dump_exit = backend_state(cluster, owner.pid)
    record.descriptor_after_dump = owner.visibility_descriptor()
    owner.end_transaction()
    log("owner_transaction_ended_after_dump")
    if record.dump.returncode != 0:
        raise CaptureStepFailed(f"pg_dump failed unexpectedly: {record.dump.diagnostics()}")

    # ---- restore into a fresh, verified-empty database; compare; read the restored sequences (read-only) ------------------------------------
    restored = restore_into_fresh_database(cluster, ledger, workspace.archive_path, workspace.migrator_pgpass, record.commands, log)
    record.destination_database = restored.name
    record.restored_fingerprints = all_fingerprints(restored.admin_engine)
    record.comparison = compare_to_baseline(record.baseline, restored.admin_engine)  # the unchanged strict comparator
    record.details_restored = read_sequence_details(restored.admin_engine)  # includes the owning columns' maxima on the RESTORED database
    record.details_restored_again = read_sequence_details(restored.admin_engine)  # a second read: verification must not have moved anything
    record.rules = evaluate_rules(record.details_baseline, record.details_restored)
    record.sequence_diff = keyed_diff(record.comparison.sequence_source, record.comparison.sequence_restored, key_columns=1)
    log("compared_and_rules_evaluated")
    return record


def _run(harness_: Harness, name: str) -> SequenceRecord:
    with owned_case(harness_.cluster) as ledger:
        record = run_sequence_case(harness_, SCENARIOS[name], ledger)
        directory = next(iter(ledger.directories))
        record.artifact_listing = sorted(p.name for p in Path(directory).iterdir())
    record.verification = ledger.verification
    return record


@pytest.fixture(scope="module")
def c3_advance(harness) -> SequenceRecord:
    return _run(harness, "c3_advance")


@pytest.fixture(scope="module")
def c3_control(harness) -> SequenceRecord:
    return _run(harness, "c3_control")


@pytest.fixture(scope="module")
def c3_rollback(harness) -> SequenceRecord:
    return _run(harness, "c3_rollback")


@pytest.fixture(scope="module")
def c4_manual_id(harness) -> SequenceRecord:
    return _run(harness, "c4_manual_id")


@pytest.fixture(scope="module")
def c5_cycle(harness) -> SequenceRecord:
    return _run(harness, "c5_cycle")


@pytest.fixture(scope="module")
def c5_negative_increment(harness) -> SequenceRecord:
    return _run(harness, "c5_negative_increment")


def by_name(details: Sequence[SequenceDetail]) -> dict[str, SequenceDetail]:
    return {d.name: d for d in details}


def rule(record: SequenceRecord, name: str) -> RuleResult:
    return next(r for r in record.rules if r.sequence == name)


# ==========================================================================
# scope, the pure rule logic and the static no-mutation guarantee (no cluster needed)
# ==========================================================================
def test_only_c3_c4_and_c5_are_in_scope_and_the_other_cases_stay_unvalidated():
    assert VALIDATED_CASES == ("C3", "C4", "C5") and UNVALIDATED_CASES == ("C6", "C7", "C9", "C10")
    assert {s.case for s in SCENARIOS.values()} == set(VALIDATED_CASES)


def test_the_documented_inventory_is_thirteen_bigserial_sequences_owned_by_the_id_column():
    assert len(DOCUMENTED_OWNERS) == 13 and all(column == "id" for _, column in DOCUMENTED_OWNERS.values())
    assert set(DOCUMENTED_OWNERS) >= {CALLED_SEQUENCE, UNCALLED_SEQUENCE}


def _detail(**changes) -> SequenceDetail:
    values = dict(name="s_id_seq", owned_by=("s", "id"), data_type="bigint", increment=1, minimum=1, maximum=2**63 - 1, cache=1, cycle=False, last_value=10, is_called=True, column_max=10)
    values.update(changes)
    return SequenceDetail(**values)


@pytest.mark.parametrize("last_value, is_called, increment, expected", [
    (10, True, 1, 11), (10, False, 1, 10), (1, False, 1, 1), (1, True, 1, 2), (7, True, 5, 12), (7, False, 5, 7),
], ids=["called", "uncalled", "fresh_uncalled", "first_call", "increment_five_called", "increment_five_uncalled"])
def test_the_next_value_is_last_value_when_not_called_and_last_value_plus_increment_when_called(last_value, is_called, increment, expected):
    assert next_value(_detail(last_value=last_value, is_called=is_called, increment=increment)) == expected


@pytest.mark.parametrize("changes, reasons", [
    ({"increment": -1}, ("increment_not_positive",)), ({"increment": 0}, ("increment_not_positive",)), ({"cycle": True}, ("cycles",)),
    ({"owned_by": None}, ("not_owned",)), ({"cycle": True, "increment": -3, "owned_by": None}, ("increment_not_positive", "cycles", "not_owned")),
])
def test_r0_rejects_each_unsupported_configuration_and_no_next_value_is_ever_computed_for_it(changes, reasons):
    detail = _detail(**changes)
    assert applicability(detail) == (False, reasons)
    with pytest.raises(NotApplicable):
        next_value(detail)
    result = evaluate_rules([detail], [detail])[0]
    assert (result.applicable, result.n_baseline, result.n_restored, result.r1_no_regression, result.r2_next_id_safe, result.r3_headroom) == (False, None, None, None, None, None)
    with pytest.raises(PreconditionFailed):
        require_monotonic_applicable([detail])


def test_r1_r2_and_r3_are_each_computed_from_observed_values():
    base = _detail(last_value=10, is_called=True)  # N_b = 11
    cases = {
        "ok": (_detail(last_value=14, is_called=True, column_max=10), (True, True, True)),
        "regression": (_detail(last_value=3, is_called=True, column_max=2), (False, True, True)),
        "id_above_next": (_detail(last_value=10, is_called=True, column_max=1_000_000), (True, False, True)),
        "empty_table": (_detail(last_value=1, is_called=False, column_max=None), (False, True, True)),
        "no_headroom": (_detail(last_value=2**63 - 1, is_called=False, maximum=2**63 - 2, column_max=5), (True, True, False)),
        "equal": (_detail(last_value=10, is_called=True, column_max=10), (True, True, True)),
    }
    for label, (restored, expected) in cases.items():
        result = evaluate_rules([base], [restored])[0]
        assert (result.r1_no_regression, result.r2_next_id_safe, result.r3_headroom) == expected, label
    assert not hasattr(RuleResult, "accepted"), "R3 is informational: no aggregate verdict is invented here"


def test_the_rule_for_r2_uses_the_restored_column_maximum_not_the_baseline_one():
    base = _detail(last_value=10, is_called=True, column_max=10)
    assert evaluate_rules([base], [_detail(last_value=10, is_called=True, column_max=500)])[0].r2_next_id_safe is False
    assert evaluate_rules([_detail(last_value=10, is_called=True, column_max=500)], [_detail(last_value=10, is_called=True, column_max=10)])[0].r2_next_id_safe is True


SANCTIONED_MUTATORS = {"advance_action", "setup_cycle", "setup_negative_increment"}  # the only functions allowed to name nextval, setval or ALTER SEQUENCE
_MUTATING = re.compile(r"nextval|setval|ALTER SEQUENCE|RESTART IDENTITY", re.IGNORECASE)


def test_no_verification_code_can_advance_set_or_repair_a_sequence():
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    naming = {node.name for node in tree.body if isinstance(node, ast.FunctionDef) and not node.name.startswith("test_")
              and _MUTATING.search(ast.get_source_segment(Path(__file__).read_text(encoding="utf-8"), node) or "")}
    assert naming == SANCTIONED_MUTATORS, f"unexpected functions name a sequence mutation: {sorted(naming - SANCTIONED_MUTATORS)}"
    for function in (read_sequence_details, applicability, next_value, evaluate_rules, require_monotonic_applicable):
        assert not _MUTATING.search(inspect.getsource(function)), function.__name__
    assert not _MUTATING.search(_INVENTORY_SQL)


# ==========================================================================
# inventory against the documented schema (every scenario)
# ==========================================================================
ALL_SCENARIOS = ["c3_advance", "c3_control", "c3_rollback", "c4_manual_id", "c5_cycle", "c5_negative_increment"]
ALTERED = {"c5_cycle": {"cycle": True}, "c5_negative_increment": {"increment": -1}}


@pytest.mark.parametrize("name", ALL_SCENARIOS)
def test_the_discovered_inventory_equals_the_documented_schema_and_parameters(name, request):
    record = request.getfixturevalue(name)
    details = by_name(record.details_baseline)
    assert {n: d.owned_by for n, d in details.items()} == DOCUMENTED_OWNERS
    for n, d in details.items():
        expected = {**DOCUMENTED_PARAMETERS, **(ALTERED.get(name, {}) if n == CALLED_SEQUENCE else {})}
        assert {k: getattr(d, k) for k in DOCUMENTED_PARAMETERS} == expected, n


@pytest.mark.parametrize("name", ["c3_advance", "c3_control", "c3_rollback", "c4_manual_id"])
def test_every_baseline_read_data_and_sequences_used_the_owners_connection(name, request):
    record = request.getfixturevalue(name)
    assert record.owner_connects_after_reads == 0
    order = [a for a in record.actions if a in {"snapshot_exported", "baseline_data_read_through_owner", "baseline_sequences_read_through_owner", "dump_started"}]
    assert order == ["snapshot_exported", "baseline_data_read_through_owner", "baseline_sequences_read_through_owner", "dump_started"]


# ==========================================================================
# C3 -- advancement between the baseline and the dump
# ==========================================================================
def test_c3_the_writer_advanced_the_sequences_after_the_baseline_read_and_before_the_dump_and_it_was_confirmed(c3_advance):
    order = [a for a in c3_advance.actions if a in {"baseline_sequences_read_through_owner", "writer_released", "writer_completed", "advancement_confirmed", "owner_reread_after_advance", "dump_started"}]
    assert order == ["baseline_sequences_read_through_owner", "writer_released", "writer_completed", "advancement_confirmed", "owner_reread_after_advance", "dump_started"]
    drawn = c3_advance.writer.results
    baseline = by_name(c3_advance.details_baseline)
    first_called = next_value(baseline[CALLED_SEQUENCE])
    assert [v for n, v in drawn if n == CALLED_SEQUENCE] == list(range(first_called, first_called + 5))
    assert [v for n, v in drawn if n == UNCALLED_SEQUENCE] == [1], "the first nextval on an uncalled sequence returns its last_value"
    fresh = by_name(c3_advance.fresh_details_after_advance)
    assert (fresh[CALLED_SEQUENCE].last_value, fresh[CALLED_SEQUENCE].is_called) == (baseline[CALLED_SEQUENCE].last_value + 5, True)
    assert (baseline[UNCALLED_SEQUENCE].last_value, baseline[UNCALLED_SEQUENCE].is_called) == (1, False)
    assert (fresh[UNCALLED_SEQUENCE].last_value, fresh[UNCALLED_SEQUENCE].is_called) == (1, True)


def test_c3_sequences_are_not_snapshot_bound_but_table_data_is(c3_advance):
    assert by_name(c3_advance.owner_reread_details)[CALLED_SEQUENCE].last_value == by_name(c3_advance.fresh_details_after_advance)[CALLED_SEQUENCE].last_value
    assert c3_advance.owner_fingerprints_after_advance == c3_advance.baseline.fingerprints, "the owner's table reads still see the snapshot"


def test_c3_the_dump_used_the_exported_id_with_the_owner_open_until_it_exited(c3_advance):
    assert c3_advance.dump.returncode == 0 and [a for a in c3_advance.dump.argv if a.startswith("--snapshot=")] == [f"--snapshot={c3_advance.exported_id}"]
    assert c3_advance.owner_backend_state_at_dump_exit == "idle in transaction"
    assert c3_advance.descriptor_at_export == c3_advance.descriptor_after_dump
    assert c3_advance.dump_sessions_after == c3_advance.dump_sessions_before + 1


def test_c3_table_data_equals_the_baseline_exactly_although_the_sequences_moved(c3_advance):
    assert c3_advance.comparison.fingerprint_diffs == {}
    assert c3_advance.restored_fingerprints == c3_advance.baseline.fingerprints


def test_c3_the_restored_next_value_is_strictly_larger_for_the_advanced_sequences_and_equal_for_the_rest(c3_advance):
    results = {r.sequence: r for r in c3_advance.rules}
    assert results[CALLED_SEQUENCE].n_restored - results[CALLED_SEQUENCE].n_baseline == 5 > 0
    assert (results[UNCALLED_SEQUENCE].n_baseline, results[UNCALLED_SEQUENCE].n_restored) == (1, 2), "uncalled (next = last_value = 1) became called (next = 2)"
    assert all(r.n_restored == r.n_baseline for n, r in results.items() if n not in {CALLED_SEQUENCE, UNCALLED_SEQUENCE})
    restored = by_name(c3_advance.details_restored)
    assert (restored[CALLED_SEQUENCE].is_called, restored[UNCALLED_SEQUENCE].is_called) == (True, True)


def test_c3_r0_r1_r2_and_r3_hold_for_every_sequence_from_observed_values(c3_advance):
    assert len(c3_advance.rules) == 13
    for r in c3_advance.rules:
        assert (r.applicable, r.r1_no_regression, r.r2_next_id_safe, r.r3_headroom) == (True, True, True, True), r
        assert r.column_max_restored is None or r.n_restored > r.column_max_restored


def test_c3_the_unchanged_strict_comparator_reports_exactly_the_expected_sequence_difference(c3_advance):
    diff = c3_advance.sequence_diff
    assert set(diff) == {(CALLED_SEQUENCE,), (UNCALLED_SEQUENCE,)}, "the strict comparison shows the difference and only for the advanced sequences"
    baseline = by_name(c3_advance.details_baseline)
    assert diff[(CALLED_SEQUENCE,)] == ((CALLED_SEQUENCE, baseline[CALLED_SEQUENCE].last_value), (CALLED_SEQUENCE, baseline[CALLED_SEQUENCE].last_value + 5))
    assert diff[(UNCALLED_SEQUENCE,)] == ((UNCALLED_SEQUENCE, None), (UNCALLED_SEQUENCE, 1)), "pg_sequences.last_value is NULL for an uncalled sequence and is kept as NULL"
    assert c3_advance.comparison.sequence_source == c3_advance.baseline.sequences, "the comparator reports the baseline's own pg_sequences values, untouched"


def test_c3_the_null_of_an_uncalled_sequence_is_kept_separate_from_the_direct_value(c3_advance):
    assert dict(c3_advance.baseline.sequences)[UNCALLED_SEQUENCE] is None
    assert (by_name(c3_advance.details_baseline)[UNCALLED_SEQUENCE].last_value, by_name(c3_advance.details_baseline)[UNCALLED_SEQUENCE].is_called) == (1, False)


def test_c3_control_without_advancement_has_equal_next_values_and_no_sequence_difference(c3_control):
    assert c3_control.writer is None
    assert all(r.n_restored == r.n_baseline and r.r1_no_regression and r.r2_next_id_safe and r.r3_headroom for r in c3_control.rules)
    assert c3_control.sequence_diff == {} and c3_control.comparison.sequence_source == c3_control.comparison.sequence_restored
    assert c3_control.comparison.fingerprint_diffs == {}


def test_c3_control_exercises_live_uncalled_sequences_with_is_called_false_before_and_after(c3_control):
    uncalled = [d for d in c3_control.details_baseline if not d.is_called]
    seeded = {f"{t}_id_seq" for t in ("team_aliases", "player_aliases", "source_artifacts", "player_stat_snapshots")}  # the tables the deterministic seed inserts into
    assert {d.name for d in c3_control.details_baseline if d.is_called} == seeded
    assert {d.name for d in uncalled} == set(DOCUMENTED_OWNERS) - seeded and len(uncalled) == 9 and UNCALLED_SEQUENCE in {d.name for d in uncalled}
    restored = by_name(c3_control.details_restored)
    for d in uncalled:
        assert (restored[d.name].last_value, restored[d.name].is_called) == (d.last_value, False), d.name
        assert next_value(d) == d.last_value == 1
    assert all(d.is_called for d in c3_control.details_baseline if d.name == CALLED_SEQUENCE)


def test_c3_the_baseline_sequence_block_satisfies_the_production_document_contract(c3_control):
    document = baseline_to_document(c3_control.baseline, artifact_id=str(uuid.uuid4()), captured_utc="2026-10-04T00:00:00.000000Z", sequence_details=c3_control.details_baseline)
    parsed = parse_baseline(document.to_bytes())
    assert parsed.sequence_details == c3_control.details_baseline and parsed == document


def test_c3_rollback_example_nextval_advancement_survives_a_rollback_and_the_gap_is_not_corruption(c3_rollback):
    s = c3_rollback.setup
    assert s["rows_before"] == s["rows_after"] and s["gap_row_present"] == 0, "the insert was rolled back"
    assert s["state_after"][0] == s["state_before"][0] + 1 and s["state_after"][1] is True, "but the sequence advanced and stayed advanced"
    restored = {r.sequence: r for r in c3_rollback.rules}[CALLED_SEQUENCE]
    assert restored.column_max_restored < restored.n_restored - 1, "an unused id (the gap) lies below the next value"
    assert (restored.r1_no_regression, restored.r2_next_id_safe, restored.r3_headroom) == (True, True, True)
    assert c3_rollback.comparison.fingerprint_diffs == {} and c3_rollback.sequence_diff == {}


@pytest.mark.parametrize("name", ["c3_advance", "c3_control", "c3_rollback", "c4_manual_id"])
def test_verification_never_moved_a_sequence_the_restored_state_is_what_pg_dump_wrote(name, request):
    record = request.getfixturevalue(name)
    assert record.details_restored == record.details_restored_again
    assert [(d.name, d.last_value, d.is_called) for d in record.details_restored] == [(n, d.last_value, d.is_called) for n, d in sorted(by_name(record.details_restored).items())]
    if name == "c3_control":
        assert [(d.name, d.last_value, d.is_called) for d in record.details_restored] == [(d.name, d.last_value, d.is_called) for d in record.details_baseline]


# ==========================================================================
# C4 -- the manual-id hazard
# ==========================================================================
def test_c4_the_manual_row_was_valid_and_did_not_advance_its_sequence(c4_manual_id):
    s = c4_manual_id.setup
    assert s["state_before"] == s["state_after"], "an explicit id never evaluates the default, so the sequence did not move"
    baseline = by_name(c4_manual_id.details_baseline)[CALLED_SEQUENCE]
    assert baseline.column_max == MANUAL_ID > next_value(baseline)


def test_c4_table_data_is_in_parity_and_r1_holds(c4_manual_id):
    assert c4_manual_id.comparison.fingerprint_diffs == {} and c4_manual_id.restored_fingerprints == c4_manual_id.baseline.fingerprints
    r = rule(c4_manual_id, CALLED_SEQUENCE)
    assert r.r1_no_regression is True and r.n_restored == r.n_baseline


def test_c4_r2_fails_on_the_restored_column_maximum_and_only_for_that_sequence(c4_manual_id):
    r = rule(c4_manual_id, CALLED_SEQUENCE)
    assert r.column_max_restored == MANUAL_ID, "measured on the RESTORED database"
    assert r.r2_next_id_safe is False and r.n_restored < r.column_max_restored
    assert [x.sequence for x in c4_manual_id.rules if x.r2_next_id_safe is False] == [CALLED_SEQUENCE]
    assert all(x.r1_no_regression and x.r3_headroom for x in c4_manual_id.rules)


def test_c4_the_unsafe_condition_is_reported_and_nothing_is_repaired(c4_manual_id):
    r = rule(c4_manual_id, CALLED_SEQUENCE)
    report = {"sequence": r.sequence, "next_value_restored": r.n_restored, "column_max_restored": r.column_max_restored, "unsafe": not r.r2_next_id_safe}
    assert report["unsafe"] and report["column_max_restored"] - report["next_value_restored"] > 0
    restored = by_name(c4_manual_id.details_restored)[CALLED_SEQUENCE]
    baseline = by_name(c4_manual_id.details_baseline)[CALLED_SEQUENCE]
    assert (restored.last_value, restored.is_called) == (baseline.last_value, baseline.is_called), "the restored generator is exactly as dumped: no setval, no repair"
    assert restored.column_max == MANUAL_ID, "the offending row is still there"
    assert c4_manual_id.sequence_diff == {}, "no sequence moved between the baseline and the restore"


# ==========================================================================
# C5 -- applicability (R0)
# ==========================================================================
@pytest.mark.parametrize("name, reason", [("c5_cycle", "cycles"), ("c5_negative_increment", "increment_not_positive")])
def test_c5_r0_rejects_the_altered_sequence_in_the_harness_path_before_any_dump(name, reason, request):
    record = request.getfixturevalue(name)
    altered = by_name(record.details_baseline)[CALLED_SEQUENCE]
    assert (altered.cycle, altered.increment) == ((True, 1) if reason == "cycles" else (False, -1)), "the alteration was really in place when the baseline was read"
    assert applicability(altered) == (False, (reason,))
    assert record.outcome == "monotonic_not_applicable" and reason in record.rejection and CALLED_SEQUENCE in record.rejection
    assert [applicability(d)[1] for d in record.details_baseline if d.name != CALLED_SEQUENCE] == [()] * 12, "only the altered sequence is rejected"


@pytest.mark.parametrize("name", ["c5_cycle", "c5_negative_increment"])
def test_c5_nothing_downstream_ran_and_no_monotonic_acceptance_was_computed(name, request):
    record = request.getfixturevalue(name)
    steps = [a for a in record.actions if a in {"applicability_guard_refused", "applicability_guard_passed", "dump_started", "dump_exited", "destination_verified_empty", "restored"}]
    assert steps == ["applicability_guard_refused"]
    assert record.dump is None and record.destination_database is None and record.commands == [] and record.rules is None and record.comparison is None
    assert record.artifact_listing == [], "no finalization marker"
    with pytest.raises(NotApplicable):
        next_value(by_name(record.details_baseline)[CALLED_SEQUENCE])


def test_c5_the_ordinary_positive_non_cycling_owned_sequence_is_accepted_and_the_dump_proceeds(c3_control):
    assert all(applicability(d) == (True, ()) for d in c3_control.details_baseline)
    assert "applicability_guard_passed" in c3_control.actions and c3_control.dump.returncode == 0
    assert all(r.applicable for r in c3_control.rules)


def test_c5_headroom_r3_is_reported_as_an_informational_result_without_becoming_a_policy(c3_control):
    assert all(r.r3_headroom is True for r in c3_control.rules)
    assert not any("headroom" in a for a in c3_control.actions), "no harness step reacts to R3"


# ==========================================================================
# cleanup, credentials, recorded evidence
# ==========================================================================
@pytest.mark.parametrize("name", ALL_SCENARIOS)
def test_cleanup_was_verified_for_each_scenario(name, request):
    v = request.getfixturevalue(name).verification
    assert v["problems"] == [] and v["databases_remaining"] == [] and v["sessions_in_dropped_databases"] == 0
    assert v["leftover_dump_or_owner_sessions"] == 0 and v["container_files_remaining"] == [] and v["threads_alive"] == [] and v["directories_remaining"] == []


@pytest.mark.parametrize("name", ["c3_advance", "c3_control", "c3_rollback", "c4_manual_id"])
def test_no_credential_appears_in_any_recorded_command_or_output(name, request, harness):
    record = request.getfixturevalue(name)
    secrets_ = harness.cluster.secrets_to_scrub()
    assert record.commands
    for command in record.commands:
        blob = " ".join(command.argv) + command.stdout + command.stderr
        assert not any(secret and secret in blob for secret in secrets_) and "PGPASSWORD" not in blob and "password=" not in blob.lower()


@pytest.mark.parametrize("name", ALL_SCENARIOS)
def test_observed_values_are_recorded_in_the_junit_evidence(name, request):
    """Baseline and restored sequence values, is_called, next values, restored maxima and rule outcomes, per sequence; no credential."""
    record = request.getfixturevalue(name)
    props = request.node.user_properties
    props.append(("scenario", f"{record.scenario} ({record.case})"))
    props.append(("outcome", record.outcome or "restored"))
    props.append(("actions", ",".join(record.actions)))
    props.append(("setup", str(record.setup)))
    props.append(("cleanup_problems", str(len(record.verification["problems"]))))
    if record.rejection:
        props.append(("r0_rejection", record.rejection))
    restored = by_name(record.details_restored) if record.details_restored else {}
    results = {r.sequence: r for r in (record.rules or [])}
    for d in record.details_baseline:
        line = f"baseline(last={d.last_value},called={d.is_called},incr={d.increment},cycle={d.cycle},colmax={d.column_max})"
        if d.name in restored:
            x, r = restored[d.name], results[d.name]
            line += (f" restored(last={x.last_value},called={x.is_called},colmax={x.column_max}) N_b={r.n_baseline} N_r={r.n_restored} "
                     f"R0={r.applicable} R1={r.r1_no_regression} R2={r.r2_next_id_safe} R3={r.r3_headroom}")
        props.append((f"seq:{d.name}", line))
    if record.comparison is not None:
        props.append(("strict_sequence_diff", str(record.sequence_diff)))
        props.append(("fingerprint_diff_tables", ",".join(sorted(record.comparison.fingerprint_diffs)) or "none"))
    assert not record.verification["problems"]
