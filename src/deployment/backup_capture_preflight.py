"""
Phase 9B-6A: the read-only capture preflight (docs/backup_artifact_contract_proposal.md
section 13.7, approved 2026-10-05 for this unit only).

It observes, through ONE caller-owned `sqlalchemy.engine.Connection`, the facts the v1
operating contract (section 13.2) requires of a source before a capture may begin:
the verified database identity, the server version, the Alembic head, whether the
connected role can read `system_identifier`, object ownership and relation/column ACL
grantees, unsupported object kinds, and each sequence's R0 applicability. It returns
those facts and ONE pure `accepted` property. It defines no reason code, no `FAILED`
class and no manifest field, and it is not a capture: it takes no lock, exports no
snapshot, runs no `pg_dump`, reads no table data, no sequence state and no catalog
digest, writes nothing and retries nothing.

Ownership of the connection stays with the caller, exactly as in
`src.pipeline.bulk_import_safety`: this module never creates an engine or connection
and never commits, rolls back, closes, invalidates or disposes anything. Statement and
lock timeouts are the caller's (`SET LOCAL` in the caller's transaction); none is
chosen here.

Locking. Apart from `verify_database_target`'s `SELECT current_database(), version()`,
every statement reads system catalogs or a privilege function, except ONE: the Alembic
head is read from `public.alembic_version`, which takes ACCESS SHARE on that one table
(an exclusive request queued on `alembic_version` itself would make it wait, bounded by
the caller's `lock_timeout`). No index definition is rendered (`pg_get_indexdef` is the
read that queued behind an exclusive request in 12.8) and no sequence relation is read.

`system_identifier`: whether the connected role may execute `pg_control_system()` is
asked with `has_function_privilege` first, so a denial is reported as a fact and never
aborts the caller's transaction. Its readability is deliberately NOT part of `accepted`:
it is a capability the later capture needs, not a property of the source, and filling
the manifest field without it needs a privilege decision that has not been made.

Not checked here (stated, not claimed): schema- and database-level ACLs, default
privileges, row-level security policies, role attributes and memberships, and any object
outside schema `public`.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from src.exceptions import ValidationError
from src.pipeline.bulk_import_safety import DatabaseTargetDesignation, VerifiedDatabaseTarget, verify_database_target

__all__ = [
    "V1_SUPPORTED_SERVER_MAJOR",
    "V1_OBJECT_OWNER_ROLE",
    "V1_PERMITTED_GRANTEE_ROLES",
    "SUPPORTED_RELKINDS",
    "PUBLIC_GRANTEE",
    "CaptureSourcePolicy",
    "v1_policy",
    "SequenceApplicability",
    "OwnershipFinding",
    "UnsupportedObject",
    "CapturePreflight",
    "observe_capture_preflight",
]

#: The values approved for v1 (specification 13.2 and 13.8).
V1_SUPPORTED_SERVER_MAJOR = 16
V1_OBJECT_OWNER_ROLE = "napsedge_migrator"
V1_PERMITTED_GRANTEE_ROLES: frozenset[str] = frozenset({"napsedge_migrator", "napsedge_runtime_ro", "napsedge_backup_ro"})

#: Relation kinds the baseline and the validated restore cover: ordinary tables, sequences and their indexes.
SUPPORTED_RELKINDS: frozenset[str] = frozenset({"r", "S", "i"})

#: How an ACL entry granted to PUBLIC (grantee OID 0) is reported. `public` cannot be a role name, so this never collides.
PUBLIC_GRANTEE = "PUBLIC"

# The only sequence configuration validated (specification 6.5; C3-C5): BIGSERIAL defaults.
_VALIDATED_SEQUENCE_TYPE = "bigint"
_VALIDATED_SEQUENCE_INCREMENT = 1
_VALIDATED_SEQUENCE_CACHE = 1
_VALIDATED_SEQUENCE_MINIMUM = 1
_VALIDATED_SEQUENCE_MAXIMUM = 2**63 - 1

_DATABASE_SQL = (
    "SELECT d.oid, pg_get_userbyid(d.datdba), current_setting('server_version_num')::int "
    "FROM pg_database d WHERE d.datname = current_database()"
)
_SYSTEM_IDENTIFIER_PRIVILEGE_SQL = "SELECT has_function_privilege('pg_catalog.pg_control_system()', 'EXECUTE')"
_SYSTEM_IDENTIFIER_SQL = "SELECT system_identifier::text FROM pg_catalog.pg_control_system()"
_ALEMBIC_TABLE_PRESENT_SQL = "SELECT to_regclass('public.alembic_version') IS NOT NULL"
_ALEMBIC_HEADS_SQL = "SELECT version_num FROM public.alembic_version ORDER BY version_num"
_RELATIONS_SQL = (
    "SELECT c.relname, c.relkind::text, pg_get_userbyid(c.relowner) "
    "FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace ORDER BY c.relname"
)
_RELATION_GRANTEES_SQL = (
    "SELECT DISTINCT c.relname, c.relkind::text, pg_get_userbyid(c.relowner), "
    f"CASE WHEN a.grantee = 0 THEN '{PUBLIC_GRANTEE}' ELSE pg_get_userbyid(a.grantee) END "
    "FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a "
    "WHERE c.relnamespace = 'public'::regnamespace ORDER BY 1, 4"
)
_COLUMN_GRANTEES_SQL = (
    "SELECT DISTINCT c.relname, c.relkind::text, pg_get_userbyid(c.relowner), at.attname, "
    f"CASE WHEN a.grantee = 0 THEN '{PUBLIC_GRANTEE}' ELSE pg_get_userbyid(a.grantee) END "
    "FROM pg_class c JOIN pg_attribute at ON at.attrelid = c.oid AND at.attnum > 0 AND NOT at.attisdropped "
    "CROSS JOIN LATERAL aclexplode(at.attacl) a "
    "WHERE c.relnamespace = 'public'::regnamespace ORDER BY 1, 4, 5"
)
_OTHER_OBJECTS_SQL = (
    "SELECT 'function', p.oid::regprocedure::text FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace "
    "UNION ALL SELECT 'trigger', c.relname || '.' || t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
    "WHERE c.relnamespace = 'public'::regnamespace AND NOT t.tgisinternal "
    "UNION ALL SELECT 'extension', e.extname FROM pg_extension e WHERE e.extnamespace = 'public'::regnamespace "
    "UNION ALL SELECT 'type', ty.typname FROM pg_type ty WHERE ty.typnamespace = 'public'::regnamespace AND ty.typtype IN ('e', 'd', 'r', 'm') "
    "ORDER BY 1, 2"
)
# Identical, character for character, to the test layer's sequence inventory (tests/postgres/test_postgres_backup_sequence_consistency.py
# `_INVENTORY_SQL`, which C3-C5 validated); a PostgreSQL test asserts the equality so the two cannot drift. Catalog reads only.
_SEQUENCE_INVENTORY_SQL = """
SELECT s.relname, t.relname, a.attname, q.seqtypid::regtype::text, q.seqincrement, q.seqmin, q.seqmax, q.seqcache, q.seqcycle
FROM pg_class s
JOIN pg_sequence q ON q.seqrelid = s.oid
LEFT JOIN pg_depend d ON d.classid = 'pg_class'::regclass AND d.objid = s.oid AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')
LEFT JOIN pg_class t ON t.oid = d.refobjid
LEFT JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
WHERE s.relkind = 'S' AND s.relnamespace = 'public'::regnamespace
ORDER BY s.relname
"""


@dataclass(frozen=True)
class CaptureSourcePolicy:
    """What a v1 capture source must look like. Self-validating; use `v1_policy` for the approved values."""

    designation: DatabaseTargetDesignation
    expected_alembic_head: str
    supported_server_major: int
    object_owner_role: str
    permitted_grantee_roles: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.designation, DatabaseTargetDesignation):
            raise ValidationError("designation must be a DatabaseTargetDesignation")
        if not isinstance(self.expected_alembic_head, str) or not self.expected_alembic_head:
            raise ValidationError("expected_alembic_head must be a non-empty string")
        if type(self.supported_server_major) is not int or self.supported_server_major <= 0:
            raise ValidationError("supported_server_major must be a positive integer")
        if not isinstance(self.object_owner_role, str) or not self.object_owner_role:
            raise ValidationError("object_owner_role must be a non-empty string")
        if not isinstance(self.permitted_grantee_roles, frozenset) or not all(isinstance(r, str) and r for r in self.permitted_grantee_roles):
            raise ValidationError("permitted_grantee_roles must be a frozenset of non-empty role names")
        if PUBLIC_GRANTEE in self.permitted_grantee_roles:
            raise ValidationError("PUBLIC can never be a permitted grantee")
        if self.object_owner_role not in self.permitted_grantee_roles:
            raise ValidationError("the object owner role must itself be a permitted grantee (an owner holds ACL entries on its own objects)")


def v1_policy(designation: DatabaseTargetDesignation, expected_alembic_head: str) -> CaptureSourcePolicy:
    """The approved v1 policy for one designated database and the repository's expected head."""
    return CaptureSourcePolicy(
        designation=designation,
        expected_alembic_head=expected_alembic_head,
        supported_server_major=V1_SUPPORTED_SERVER_MAJOR,
        object_owner_role=V1_OBJECT_OWNER_ROLE,
        permitted_grantee_roles=V1_PERMITTED_GRANTEE_ROLES,
    )


@dataclass(frozen=True)
class SequenceApplicability:
    """One sequence's parameters, from the catalog only (never its state)."""

    name: str
    owned_by: tuple[str, str] | None
    data_type: str
    increment: int
    minimum: int
    maximum: int
    cache: int
    cycle: bool

    @property
    def applicable(self) -> bool:
        """R0 of `monotonic_v1`, exactly as specification 6.5 states it: `increment > 0`, no `cycle`, `owned_by` non-null."""
        return self.increment > 0 and not self.cycle and self.owned_by is not None

    @property
    def validated_configuration(self) -> bool:
        """The only configuration C3-C5 exercised: BIGSERIAL defaults (bigint, increment 1, cache 1, 1..2**63-1, no cycle)."""
        return (
            self.data_type == _VALIDATED_SEQUENCE_TYPE
            and self.increment == _VALIDATED_SEQUENCE_INCREMENT
            and self.cache == _VALIDATED_SEQUENCE_CACHE
            and self.minimum == _VALIDATED_SEQUENCE_MINIMUM
            and self.maximum == _VALIDATED_SEQUENCE_MAXIMUM
            and not self.cycle
        )


@dataclass(frozen=True)
class OwnershipFinding:
    """
    A relation (or one of its columns) outside the ownership policy. `grantee is None` means the OWNER is outside the
    policy; otherwise `grantee` is an ACL grantee outside it (`PUBLIC` for a grant to PUBLIC), on the column `column`
    when that is set.
    """

    relname: str
    relkind: str
    owner: str
    grantee: str | None = None
    column: str | None = None


@dataclass(frozen=True)
class UnsupportedObject:
    """An object in schema `public` that the baseline and the validated restore do not cover: `kind` is `relation:<relkind>`, `function`, `trigger`, `extension` or `type`."""

    kind: str
    name: str


@dataclass(frozen=True)
class CapturePreflight:
    """The observed facts and the policy they were judged against. `accepted` is pure; nothing here was changed in the database."""

    policy: CaptureSourcePolicy
    verified_target: VerifiedDatabaseTarget
    database_oid: int
    database_owner: str
    server_version_num: int
    alembic_heads: tuple[str, ...]
    system_identifier_readable: bool
    system_identifier: str | None
    ownership_findings: tuple[OwnershipFinding, ...]
    unsupported_objects: tuple[UnsupportedObject, ...]
    sequences: tuple[SequenceApplicability, ...]

    @property
    def server_major(self) -> int:
        return self.server_version_num // 10000

    @property
    def server_version_supported(self) -> bool:
        return self.server_major == self.policy.supported_server_major

    @property
    def head_matches(self) -> bool:
        return self.alembic_heads == (self.policy.expected_alembic_head,)

    @property
    def database_owner_supported(self) -> bool:
        return self.database_owner == self.policy.object_owner_role

    @property
    def sequences_supported(self) -> bool:
        return all(s.applicable and s.validated_configuration for s in self.sequences)

    @property
    def accepted(self) -> bool:
        """True only when every source requirement of specification 13.2 holds. `system_identifier` readability is reported separately (module docstring)."""
        return (
            self.server_version_supported
            and self.head_matches
            and self.database_owner_supported
            and not self.ownership_findings
            and not self.unsupported_objects
            and self.sequences_supported
        )


def _rows(connection: Connection, sql: str) -> list[tuple]:
    return [tuple(row) for row in connection.execute(text(sql)).fetchall()]


def _scalar(connection: Connection, sql: str):
    return connection.execute(text(sql)).scalar_one()


def observe_capture_preflight(connection: Connection, policy: CaptureSourcePolicy) -> CapturePreflight:
    """
    Observes the source through `connection` (inside whatever transaction the caller opened on it) and returns the
    facts. Raises `DatabaseTargetVerificationError` (from the existing `verify_database_target`) when the connection is
    not the designated database; every other outcome is a returned fact, judged by `CapturePreflight.accepted`.
    """
    if not isinstance(policy, CaptureSourcePolicy):
        raise ValidationError("policy must be a CaptureSourcePolicy")
    verified = verify_database_target(connection, policy.designation)

    database_oid, database_owner, server_version_num = _rows(connection, _DATABASE_SQL)[0]

    readable = bool(_scalar(connection, _SYSTEM_IDENTIFIER_PRIVILEGE_SQL))
    system_identifier = str(_scalar(connection, _SYSTEM_IDENTIFIER_SQL)) if readable else None

    heads: tuple[str, ...] = ()
    if _scalar(connection, _ALEMBIC_TABLE_PRESENT_SQL):
        heads = tuple(row[0] for row in _rows(connection, _ALEMBIC_HEADS_SQL))

    findings: list[OwnershipFinding] = []
    unsupported: list[UnsupportedObject] = []
    for relname, relkind, owner in _rows(connection, _RELATIONS_SQL):
        if owner != policy.object_owner_role:
            findings.append(OwnershipFinding(relname=relname, relkind=relkind, owner=owner))
        if relkind not in SUPPORTED_RELKINDS:
            unsupported.append(UnsupportedObject(kind=f"relation:{relkind}", name=relname))
    for relname, relkind, owner, grantee in _rows(connection, _RELATION_GRANTEES_SQL):
        if grantee not in policy.permitted_grantee_roles:
            findings.append(OwnershipFinding(relname=relname, relkind=relkind, owner=owner, grantee=grantee))
    for relname, relkind, owner, column, grantee in _rows(connection, _COLUMN_GRANTEES_SQL):
        if grantee not in policy.permitted_grantee_roles:
            findings.append(OwnershipFinding(relname=relname, relkind=relkind, owner=owner, grantee=grantee, column=column))
    for kind, name in _rows(connection, _OTHER_OBJECTS_SQL):
        unsupported.append(UnsupportedObject(kind=kind, name=name))

    sequences = tuple(
        SequenceApplicability(
            name=name,
            owned_by=(table, column) if table is not None and column is not None else None,
            data_type=data_type, increment=increment, minimum=minimum, maximum=maximum, cache=cache, cycle=bool(cycle),
        )
        for name, table, column, data_type, increment, minimum, maximum, cache, cycle in _rows(connection, _SEQUENCE_INVENTORY_SQL)
    )

    return CapturePreflight(
        policy=policy,
        verified_target=verified,
        database_oid=int(database_oid),
        database_owner=database_owner,
        server_version_num=int(server_version_num),
        alembic_heads=heads,
        system_identifier_readable=readable,
        system_identifier=system_identifier,
        ownership_findings=tuple(findings),
        unsupported_objects=tuple(unsupported),
        sequences=sequences,
    )
