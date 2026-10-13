"""
Phase 9B-6A: deterministic unit tests for `src/deployment/backup_capture_preflight.py` that need no database.
A fake connection answers each of the module's own SQL constants; real-PostgreSQL behavior (the backup role, the
catalog queries, the queued-lock case) lives in `tests/postgres/test_postgres_backup_capture_preflight.py`.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import re
from types import SimpleNamespace

import pytest

import src.deployment.backup_capture_preflight as preflight_module
from src.deployment.backup_capture_preflight import (
    PUBLIC_GRANTEE,
    V1_OBJECT_OWNER_ROLE,
    V1_PERMITTED_GRANTEE_ROLES,
    V1_SUPPORTED_SERVER_MAJOR,
    CaptureSourcePolicy,
    OwnershipFinding,
    SequenceApplicability,
    UnsupportedObject,
    observe_capture_preflight,
    v1_policy,
)
from src.exceptions import DatabaseTargetVerificationError, ValidationError
from src.pipeline.bulk_import_safety import DatabaseTargetDesignation, build_expected_acknowledgement

DB = "napsedge_pg_test_preflight"
HEAD = "5b02bea0eb85"
M, R, B = "napsedge_migrator", "napsedge_runtime_ro", "napsedge_backup_ro"
BIGINT_MAX = 2**63 - 1


def designation(name: str = DB) -> DatabaseTargetDesignation:
    return DatabaseTargetDesignation(expected_database_name=name, acknowledgement=build_expected_acknowledgement(name))


def policy() -> CaptureSourcePolicy:
    return v1_policy(designation(), HEAD)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def one(self):
        assert len(self._rows) == 1
        return self._rows[0]

    def fetchall(self):
        return list(self._rows)

    def scalar_one(self):
        return self.one()[0]


class FakeConnection:
    """Answers the module's own statements; records every statement and any transaction-control call (none is allowed)."""

    def __init__(self, **overrides):
        self.closed = False
        self.engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        self.statements: list[str] = []
        self.control_calls: list[str] = []
        self.answers = {
            "SELECT current_database(), version()": [(DB, "PostgreSQL 16.4 on x86_64")],
            preflight_module._DATABASE_SQL: [(16384, M, 160004)],
            preflight_module._SYSTEM_IDENTIFIER_PRIVILEGE_SQL: [(True,)],
            preflight_module._SYSTEM_IDENTIFIER_SQL: [("7412345678901234567",)],
            preflight_module._ALEMBIC_TABLE_PRESENT_SQL: [(True,)],
            preflight_module._ALEMBIC_HEADS_SQL: [(HEAD,)],
            preflight_module._RELATIONS_SQL: [("alembic_version", "r", M), ("teams", "r", M), ("teams_pkey", "i", M), ("team_aliases_id_seq", "S", M)],
            preflight_module._RELATION_GRANTEES_SQL: [("teams", "r", M, B), ("teams", "r", M, M), ("teams", "r", M, R)],
            preflight_module._COLUMN_GRANTEES_SQL: [],
            preflight_module._OTHER_OBJECTS_SQL: [],
            preflight_module._SEQUENCE_INVENTORY_SQL: [("team_aliases_id_seq", "team_aliases", "id", "bigint", 1, 1, BIGINT_MAX, 1, False)],
        }
        self.answers.update(overrides)

    def execute(self, statement):
        sql = str(statement)
        self.statements.append(sql)
        if sql not in self.answers:
            raise AssertionError(f"unexpected statement: {sql[:80]}")
        return _Result(self.answers[sql])

    def __getattr__(self, name):
        if name in {"commit", "rollback", "close", "invalidate", "begin", "begin_nested", "detach"}:
            def record(*_a, **_k):
                self.control_calls.append(name)
            return record
        raise AttributeError(name)


def observe(**overrides):
    connection = FakeConnection(**overrides)
    return observe_capture_preflight(connection, policy()), connection


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------
def test_v1_policy_carries_exactly_the_approved_values():
    p = policy()
    assert (p.supported_server_major, p.object_owner_role, p.permitted_grantee_roles) == (16, M, frozenset({M, R, B}))
    assert (V1_SUPPORTED_SERVER_MAJOR, V1_OBJECT_OWNER_ROLE, V1_PERMITTED_GRANTEE_ROLES) == (16, M, frozenset({M, R, B}))


@pytest.mark.parametrize("change", [
    {"designation": "not a designation"},
    {"expected_alembic_head": ""},
    {"supported_server_major": 0},
    {"supported_server_major": True},
    {"object_owner_role": ""},
    {"permitted_grantee_roles": {M, R, B}},
    {"permitted_grantee_roles": frozenset({M, R, B, PUBLIC_GRANTEE})},
    {"permitted_grantee_roles": frozenset({R, B})},
])
def test_policy_refuses_malformed_values(change):
    with pytest.raises(ValidationError):
        dataclasses.replace(policy(), **change)


def test_observe_refuses_a_non_policy():
    with pytest.raises(ValidationError):
        observe_capture_preflight(FakeConnection(), "policy")


# ---------------------------------------------------------------------------
# acceptance and each refusal
# ---------------------------------------------------------------------------
def test_a_conforming_source_is_accepted_with_its_facts():
    result, _ = observe()
    assert result.accepted
    assert result.verified_target.database_name == DB
    assert (result.database_oid, result.database_owner, result.server_version_num, result.server_major) == (16384, M, 160004, 16)
    assert result.alembic_heads == (HEAD,) and result.head_matches
    assert result.system_identifier_readable and result.system_identifier == "7412345678901234567"
    assert result.ownership_findings == () and result.unsupported_objects == ()
    assert result.sequences == (SequenceApplicability("team_aliases_id_seq", ("team_aliases", "id"), "bigint", 1, 1, BIGINT_MAX, 1, False),)


def test_a_database_name_mismatch_raises_the_existing_verification_error():
    with pytest.raises(DatabaseTargetVerificationError):
        observe(**{"SELECT current_database(), version()": [("napsedge_pg_test_other", "PostgreSQL 16.4")]})


def test_a_non_16_major_is_not_accepted():
    result, _ = observe(**{preflight_module._DATABASE_SQL: [(16384, M, 170002)]})
    assert result.server_major == 17 and not result.server_version_supported and not result.accepted


@pytest.mark.parametrize("heads", [[("ffffffffffff",)], [], [(HEAD,), ("ffffffffffff",)]])
def test_a_head_that_is_not_exactly_the_expected_one_is_not_accepted(heads):
    result, _ = observe(**{preflight_module._ALEMBIC_HEADS_SQL: heads})
    assert not result.head_matches and not result.accepted


def test_a_missing_alembic_table_reports_no_head_and_never_reads_the_table():
    result, connection = observe(**{preflight_module._ALEMBIC_TABLE_PRESENT_SQL: [(False,)]})
    assert result.alembic_heads == () and not result.accepted
    assert preflight_module._ALEMBIC_HEADS_SQL not in connection.statements


def test_a_database_owned_by_another_role_is_not_accepted():
    result, _ = observe(**{preflight_module._DATABASE_SQL: [(16384, "someone_else", 160004)]})
    assert not result.database_owner_supported and not result.accepted


def test_a_relation_owned_by_another_role_is_reported_and_not_accepted():
    result, _ = observe(**{preflight_module._RELATIONS_SQL: [("seasons", "r", "zz_owner")]})
    assert result.ownership_findings == (OwnershipFinding("seasons", "r", "zz_owner"),) and not result.accepted


@pytest.mark.parametrize("grantee", ["zz_reader", PUBLIC_GRANTEE])
def test_a_relation_grant_outside_the_permitted_roles_is_reported_and_not_accepted(grantee):
    result, _ = observe(**{preflight_module._RELATION_GRANTEES_SQL: [("teams", "r", M, M), ("teams", "r", M, grantee)]})
    assert result.ownership_findings == (OwnershipFinding("teams", "r", M, grantee=grantee),) and not result.accepted


def test_a_column_grant_outside_the_permitted_roles_is_reported_and_not_accepted():
    result, _ = observe(**{preflight_module._COLUMN_GRANTEES_SQL: [("teams", "r", M, "slug", "zz_reader"), ("teams", "r", M, "slug", R)]})
    assert result.ownership_findings == (OwnershipFinding("teams", "r", M, grantee="zz_reader", column="slug"),) and not result.accepted


def test_unsupported_relation_kinds_and_other_objects_are_reported_and_not_accepted():
    result, _ = observe(**{
        preflight_module._RELATIONS_SQL: [("teams", "r", M), ("team_view", "v", M)],
        preflight_module._OTHER_OBJECTS_SQL: [("function", "f()"), ("trigger", "teams.t"), ("extension", "x"), ("type", "mood")],
    })
    assert result.unsupported_objects == (
        UnsupportedObject("relation:v", "team_view"), UnsupportedObject("function", "f()"), UnsupportedObject("trigger", "teams.t"),
        UnsupportedObject("extension", "x"), UnsupportedObject("type", "mood"),
    )
    assert not result.accepted


@pytest.mark.parametrize("row, applicable, validated", [
    (("s", "t", "id", "bigint", 1, 1, BIGINT_MAX, 1, True), False, False),        # CYCLE: R0 fails
    (("s", "t", "id", "bigint", -1, -BIGINT_MAX, -1, 1, False), False, False),    # negative increment: R0 fails
    (("s", None, None, "bigint", 1, 1, BIGINT_MAX, 1, False), False, True),       # not owned: R0 fails
    (("s", "t", "id", "bigint", 1, 1, BIGINT_MAX, 20, False), True, False),       # cache above 1: R0 holds, not the validated configuration
    (("s", "t", "id", "integer", 1, 1, 2**31 - 1, 1, False), True, False),        # another type
    (("s", "t", "id", "bigint", 2, 1, BIGINT_MAX, 1, False), True, False),        # another increment
])
def test_sequences_outside_r0_or_the_validated_configuration_are_not_accepted(row, applicable, validated):
    result, _ = observe(**{preflight_module._SEQUENCE_INVENTORY_SQL: [row]})
    (sequence,) = result.sequences
    assert (sequence.applicable, sequence.validated_configuration) == (applicable, validated)
    assert not result.sequences_supported and not result.accepted


def test_an_unreadable_system_identifier_is_a_fact_that_is_never_queried_and_does_not_decide_acceptance():
    result, connection = observe(**{preflight_module._SYSTEM_IDENTIFIER_PRIVILEGE_SQL: [(False,)]})
    assert (result.system_identifier_readable, result.system_identifier) == (False, None)
    assert preflight_module._SYSTEM_IDENTIFIER_SQL not in connection.statements
    assert result.accepted


def test_the_function_uses_only_the_callers_connection_and_never_controls_its_transaction():
    _, connection = observe()
    assert connection.control_calls == []
    assert all(re.match(r"\s*SELECT\b", sql) for sql in connection.statements)


# ---------------------------------------------------------------------------
# static constraints on the module
# ---------------------------------------------------------------------------
_TREE = ast.parse(inspect.getsource(preflight_module))
_SQL_CONSTANTS = {name: value for name, value in vars(preflight_module).items() if name.startswith("_") and name.endswith("_SQL")}


def test_every_sql_statement_is_a_single_select_with_no_write_or_lock_or_state_read():
    assert len(_SQL_CONSTANTS) == 10
    forbidden = re.compile(r"\b(INSERT|UPDATE|DELETE|TRUNCATE|CREATE|ALTER|DROP|GRANT|REVOKE|LOCK|COPY|SET|COMMIT|ROLLBACK|nextval|setval|pg_get_indexdef|pg_export_snapshot|pg_advisory\w*|pg_try_advisory\w*|last_value|is_called)\b", re.I)
    for name, sql in _SQL_CONSTANTS.items():
        assert re.match(r"\s*SELECT\b", sql), name
        assert ";" not in sql, name
        assert not forbidden.search(sql), (name, forbidden.search(sql))


def test_only_alembic_version_is_read_as_a_table():
    tables = {sql for sql in _SQL_CONSTANTS.values() if re.search(r"\bFROM\s+public\.", sql)}
    assert tables == {preflight_module._ALEMBIC_HEADS_SQL}


def test_the_module_never_controls_a_connection_or_imports_the_test_layer_or_does_work_at_import():
    imported = {n.module for n in ast.walk(_TREE) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(_TREE) if isinstance(n, ast.Import) for a in n.names}
    assert imported == {"__future__", "dataclasses", "sqlalchemy", "sqlalchemy.engine", "src.exceptions", "src.pipeline.bulk_import_safety"}
    called = {n.func.attr for n in ast.walk(_TREE) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not called & {"commit", "rollback", "close", "invalidate", "dispose", "begin", "connect", "create_engine"}
    for node in _TREE.body:
        assert isinstance(node, (ast.Expr, ast.ImportFrom, ast.Import, ast.Assign, ast.AnnAssign, ast.FunctionDef, ast.ClassDef)), type(node)
        if isinstance(node, ast.Expr):
            assert isinstance(node.value, ast.Constant)  # the docstring only


def test_public_symbols_are_exactly_the_documented_ones():
    assert sorted(preflight_module.__all__) == sorted([
        "V1_SUPPORTED_SERVER_MAJOR", "V1_OBJECT_OWNER_ROLE", "V1_PERMITTED_GRANTEE_ROLES", "SUPPORTED_RELKINDS", "PUBLIC_GRANTEE",
        "CaptureSourcePolicy", "v1_policy", "SequenceApplicability", "OwnershipFinding", "UnsupportedObject", "CapturePreflight",
        "observe_capture_preflight",
    ])
