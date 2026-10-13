"""
Shared comparison helpers for the PostgreSQL recovery drills (Phase 9B-1 and
9B-2). Deliberately free of any pytest, Docker, or application import so the
pure parts (the CHECK-constraint canonicalizer and the environment scrubber)
can be unit-tested in the default suite without a database.

This module is not a test module (no `test_` prefix) and contains no fixtures.
"""
from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from typing import Iterator, Mapping

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

SAFE_IDENT = re.compile(r"[a-z_][a-z0-9_]*")

#: What every read helper below accepts. The parameter keeps its historical name, `engine`, so keyword callers are unaffected.
Source = Engine | Connection


# --------------------------------------------------------------------------
# catalog reads (always over a connection the caller supplies)
# --------------------------------------------------------------------------
@contextlib.contextmanager
def _connection(source: Source) -> Iterator[Connection]:
    """
    Connection ownership of every read in this module.

    * An `Engine` opens a NEW connection (and so a new transaction) for EACH call and closes it
      afterwards. Several reads through an Engine are therefore several separate transactions: they
      do NOT share a snapshot. This is the behavior all existing callers rely on, unchanged.
    * A `Connection` is used as it is. These helpers never open, commit, roll back or close it: its
      lifetime and its transaction belong to the caller. Passing the connection of a snapshot-owning
      REPEATABLE READ transaction is how a caller makes every read in `capture_baseline` run inside
      that one snapshot.
    """
    if isinstance(source, Connection):
        yield source
    else:
        with source.connect() as conn:
            yield conn


def scalar(engine: Source, sql: str, **params):
    with _connection(engine) as conn:
        return conn.execute(text(sql), params).scalar()


def rows(engine: Source, sql: str, **params) -> list[tuple]:
    with _connection(engine) as conn:
        return [tuple(r) for r in conn.execute(text(sql), params).fetchall()]


def public_tables(engine: Source) -> list[str]:
    return [r[0] for r in rows(engine, "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")]


def table_fingerprint(engine: Source, table: str) -> tuple[int, str]:
    """(row count, md5 over every row's text form in a deterministic order) -- covers every stored value, timestamps and floats included."""
    assert SAFE_IDENT.fullmatch(table), table
    return tuple(rows(
        engine,
        f"SELECT count(*), coalesce(md5(string_agg(t::text, '|' ORDER BY t::text)), 'empty') FROM public.\"{table}\" t",
    )[0])


def all_fingerprints(engine: Source) -> dict[str, tuple[int, str]]:
    return {table: table_fingerprint(engine, table) for table in public_tables(engine)}


def acl_snapshot(engine: Source) -> list[tuple]:
    return rows(
        engine,
        "SELECT c.relname, c.relkind::text, c.relowner::regrole::text, coalesce(c.relacl::text, '') "
        "FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'S') ORDER BY 1",
    )


def constraint_snapshot(engine: Source) -> list[tuple]:
    return rows(
        engine,
        "SELECT conrelid::regclass::text, conname, contype::text, convalidated, pg_get_constraintdef(oid) "
        "FROM pg_constraint WHERE connamespace = 'public'::regnamespace ORDER BY 1, 2",
    )


def index_snapshot(engine: Source) -> list[tuple]:
    return rows(engine, "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' ORDER BY 1, 2")


def sequence_snapshot(engine: Source) -> list[tuple]:
    return rows(engine, "SELECT sequencename, last_value FROM pg_sequences WHERE schemaname = 'public' ORDER BY 1")


# --------------------------------------------------------------------------
# diffing
# --------------------------------------------------------------------------
def keyed_diff(left: list[tuple], right: list[tuple], key_columns: int = 2) -> dict:
    """Differences paired by the leading key columns, so a failure shows the source and restored definitions side by side."""
    left_map = {row[:key_columns]: row for row in left}
    right_map = {row[:key_columns]: row for row in right}
    return {
        key: (left_map.get(key), right_map.get(key))
        for key in sorted(set(left_map) | set(right_map), key=str)
        if left_map.get(key) != right_map.get(key)
    }


def describe_diff(diff: dict, limit: int = 3) -> str:
    """Compact failure message: for each differing key, the first diverging position with context from BOTH definitions."""
    parts = [f"{len(diff)} differing item(s)"]
    for key, (src, dst) in list(diff.items())[:limit]:
        a, b = (src[-1] if src else None), (dst[-1] if dst else None)
        if a is None or b is None:
            parts.append(f"{key}: present only in {'source' if b is None else 'restored'}")
            continue
        i = next((n for n, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
        parts.append(f"{key}: diverges at {i}: src=...{a[max(0, i - 40):i + 80]!r} dst=...{b[max(0, i - 40):i + 80]!r}")
    return " | ".join(parts)


# --------------------------------------------------------------------------
# the one permitted constraint-text difference
# --------------------------------------------------------------------------
_ELEMENT = r"\('(?:[^']|'')*'::character varying\)::text"
_RESTORED_IN_LIST = re.compile(rf"ANY \(ARRAY\[({_ELEMENT}(?:, {_ELEMENT})*)\]\)")
_ELEMENT_LITERAL = re.compile(r"\(('(?:[^']|'')*'::character varying)\)::text")


def canonicalize_restored_check(definition: str) -> str:
    """
    Rewrites ONE observed pg_restore rendering into the form Alembic-created
    constraints use, and nothing else.

    Observed on PostgreSQL 16: a `varchar IN (...)` CHECK created by Alembic is
    stored as
        ... = ANY ((ARRAY['a'::character varying, 'b'::character varying])::text[])
    while the same constraint re-parsed from pg_dump's text is stored as
        ... = ANY (ARRAY[('a'::character varying)::text, ('b'::character varying)::text])
    This function converts the second form into the first, byte for byte, so
    the caller can then require EXACT equality. It performs no other
    normalization: it never strips parentheses, reorders or de-duplicates
    literals, ignores casts, or touches operators, columns, or boolean
    grouping, so any change to those still produces a difference.
    """

    def rewrite(match: re.Match) -> str:
        literals = _ELEMENT_LITERAL.findall(match.group(1))
        return "ANY ((ARRAY[" + ", ".join(literals) + "])::text[])"

    return _RESTORED_IN_LIST.sub(rewrite, definition)


def split_constraint_diff(diff: dict) -> tuple[dict, dict]:
    """
    (rendering_only, genuine). A textual difference is `rendering_only` only
    when both sides exist with the same table, name, type and validation
    state, it is a CHECK, and rewriting the RESTORED text with
    `canonicalize_restored_check` makes it byte-identical to the SOURCE text.
    Everything else is `genuine`.
    """
    rendering_only: dict = {}
    genuine: dict = {}
    for key, (src, dst) in diff.items():
        if src is None or dst is None:
            genuine[key] = (src, dst)
        elif src[:4] == dst[:4] and src[2] == "c" and canonicalize_restored_check(dst[4]) == src[4]:
            rendering_only[key] = (src, dst)
        else:
            genuine[key] = (src, dst)
    return rendering_only, genuine


# --------------------------------------------------------------------------
# whole-database comparison
# --------------------------------------------------------------------------
@dataclass
class DatabaseComparison:
    fingerprint_diffs: dict = field(default_factory=dict)
    constraint_diff: dict = field(default_factory=dict)
    constraint_rendering_only: dict = field(default_factory=dict)
    constraint_genuine_diff: dict = field(default_factory=dict)
    index_diff: dict = field(default_factory=dict)
    sequence_source: list = field(default_factory=list)
    sequence_restored: list = field(default_factory=list)
    unvalidated_constraints: list = field(default_factory=list)
    owners_equal: bool = False
    acls_equal: bool = False
    source_version: str | None = None
    restored_version: str | None = None
    source_max_alias_id: int = 0


@dataclass
class DatabaseBaseline:
    """Everything `compare_databases` reads from the SOURCE, captured as plain data so the source may stop existing before the comparison (Phase 9B-4)."""

    acls: list
    fingerprints: dict
    constraints: list
    indexes: list
    sequences: list
    version: str | None
    max_alias_id: int


def capture_baseline(source: Source) -> DatabaseBaseline:
    """
    Read-only snapshot of a database through a connection the caller owns.

    Pass an Engine and every read below opens its own connection and transaction (no shared snapshot). Pass a
    Connection and EVERY read runs on that one connection, inside whatever transaction the caller opened on it
    (for a shared-snapshot capture, the snapshot-owning REPEATABLE READ, READ ONLY transaction). Nothing here
    begins, ends or closes the caller's transaction. Output shape is identical either way.
    """
    return DatabaseBaseline(
        acls=acl_snapshot(source),
        fingerprints=all_fingerprints(source),
        constraints=constraint_snapshot(source),
        indexes=index_snapshot(source),
        sequences=sequence_snapshot(source),
        version=scalar(source, "SELECT version_num FROM alembic_version"),
        max_alias_id=scalar(source, "SELECT max(id) FROM team_aliases"),
    )


def compare_to_baseline(baseline: DatabaseBaseline, restored: Source) -> DatabaseComparison:
    """Compares a previously captured source baseline with a live restored database. Read-only."""
    result = DatabaseComparison()
    dst_acls = acl_snapshot(restored)
    result.owners_equal = bool(baseline.acls) and [r[:3] for r in baseline.acls] == [r[:3] for r in dst_acls]
    result.acls_equal = baseline.acls == dst_acls

    dst_fp = all_fingerprints(restored)
    result.fingerprint_diffs = {
        t: (baseline.fingerprints.get(t), dst_fp.get(t))
        for t in sorted(set(baseline.fingerprints) | set(dst_fp))
        if baseline.fingerprints.get(t) != dst_fp.get(t)
    }
    dst_constraints = constraint_snapshot(restored)
    result.constraint_diff = keyed_diff(baseline.constraints, dst_constraints)
    result.constraint_rendering_only, result.constraint_genuine_diff = split_constraint_diff(result.constraint_diff)
    result.index_diff = keyed_diff(baseline.indexes, index_snapshot(restored))
    result.sequence_source, result.sequence_restored = baseline.sequences, sequence_snapshot(restored)
    result.unvalidated_constraints = [r for r in dst_constraints if r[3] is False]
    result.source_version = baseline.version
    result.restored_version = scalar(restored, "SELECT version_num FROM alembic_version")
    result.source_max_alias_id = baseline.max_alias_id
    return result


def compare_databases(source: Source, restored: Source) -> DatabaseComparison:
    """Compares two live databases through admin connections the caller owns. Read-only. Equivalent to capturing the source baseline and comparing it."""
    return compare_to_baseline(capture_baseline(source), restored)


# --------------------------------------------------------------------------
# Alembic URL handling
# --------------------------------------------------------------------------
def escape_for_alembic(url: str) -> str:
    """
    `Config.set_main_option` runs the value through ConfigParser interpolation,
    where a literal `%` must be written `%%`. A URL with a percent-encoded
    password or a percent-encoded query value (certificate paths) would
    otherwise fail or be silently altered.
    """
    return url.replace("%", "%%")


# --------------------------------------------------------------------------
# environment isolation
# --------------------------------------------------------------------------
_DATABASE_ENV = re.compile(r"^(PG[A-Z0-9_]*|NAPSEDGE_DATABASE_URL|NAPSEDGE_DATABASE_ECHO)$")


def scrub_database_environment(env: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
    """
    Returns (environment without any libpq `PG*` variable or NapsEdge database
    variable, sorted names that were removed). Only NAMES are ever returned or
    logged -- never a value -- because these variables can carry credentials.
    """
    removed = sorted(name for name in env if _DATABASE_ENV.match(name))
    return {name: value for name, value in env.items() if name not in removed}, removed
