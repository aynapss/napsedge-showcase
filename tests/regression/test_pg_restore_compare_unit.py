"""
Phase 9B-2: pure unit tests (no Docker, no database) for the shared recovery
comparator in `tests/postgres/pg_restore_compare.py`.

The comparator exists to decide whether a restored PostgreSQL database still
has the same CHECK constraints as its source. PostgreSQL re-renders one
specific family of them after `pg_restore` (a `varchar IN (...)` list); the
comparator must accept exactly that rewrite and must still flag every change
that alters meaning. The previous Phase 9B-1 normalization stripped all
parentheses and so also accepted a change of boolean grouping --
`(A AND B) OR C` vs `A AND (B OR C)` -- as "rendering only". These tests pin
the stricter behavior, and each detection case has a control showing the same
shape IS accepted when only the known rendering differs, so no case can pass
vacuously.
"""
from __future__ import annotations

import pytest

from tests.postgres.pg_restore_compare import (
    canonicalize_restored_check,
    escape_for_alembic,
    rows,
    scalar,
    scrub_database_environment,
    split_constraint_diff,
)

# Source form (as Alembic-created constraints are stored) and the restored form
# of the same expression, both captured from real PostgreSQL 16 output.
SRC_SEVERITY = (
    "CHECK (((severity)::text = ANY ((ARRAY['info'::character varying, 'warning'::character varying, "
    "'error'::character varying, 'critical'::character varying])::text[])))"
)
DST_SEVERITY = (
    "CHECK (((severity)::text = ANY (ARRAY[('info'::character varying)::text, ('warning'::character varying)::text, "
    "('error'::character varying)::text, ('critical'::character varying)::text])))"
)


def _pair(src_def: str, dst_def: str, *, contype: str = "c", validated: bool = True, dst_validated: bool | None = None):
    src = ("t", "ck_t", contype, validated, src_def)
    dst = ("t", "ck_t", contype, validated if dst_validated is None else dst_validated, dst_def)
    return {("t", "ck_t"): (src, dst)}


def _verdict(src_def: str, dst_def: str, **kwargs) -> str:
    rendering_only, genuine = split_constraint_diff(_pair(src_def, dst_def, **kwargs))
    assert bool(rendering_only) != bool(genuine), "every difference must land in exactly one bucket"
    return "rendering_only" if rendering_only else "genuine"


# ---------------------------------------------------------------- the permitted rewrite
def test_known_restored_rendering_is_rewritten_to_the_source_form_exactly():
    assert canonicalize_restored_check(DST_SEVERITY) == SRC_SEVERITY


def test_source_form_is_never_modified():
    assert canonicalize_restored_check(SRC_SEVERITY) == SRC_SEVERITY


def test_non_check_text_is_never_modified():
    text = "FOREIGN KEY (team_id) REFERENCES teams(team_id) ON DELETE CASCADE"
    assert canonicalize_restored_check(text) == text


def test_known_rendering_difference_is_classified_rendering_only():
    assert _verdict(SRC_SEVERITY, DST_SEVERITY) == "rendering_only"


def test_known_rendering_inside_a_multi_clause_constraint_is_classified_rendering_only():
    src = ("CHECK ((((severity)::text = ANY ((ARRAY['info'::character varying, 'warning'::character varying])::text[])) "
           "OR (team_resolution_method IS NULL)))")
    dst = ("CHECK ((((severity)::text = ANY (ARRAY[('info'::character varying)::text, ('warning'::character varying)::text])) "
           "OR (team_resolution_method IS NULL)))")
    assert _verdict(src, dst) == "rendering_only"


def test_literals_containing_quotes_survive_the_rewrite():
    src = "CHECK (((v)::text = ANY ((ARRAY['it''s'::character varying, 'b'::character varying])::text[])))"
    dst = "CHECK (((v)::text = ANY (ARRAY[('it''s'::character varying)::text, ('b'::character varying)::text])))"
    assert canonicalize_restored_check(dst) == src
    assert _verdict(src, dst) == "rendering_only"


# ---------------------------------------------------------------- everything else is detected
@pytest.mark.parametrize(
    "label, mutate",
    [
        ("extra allowed value", lambda d: d.replace("('critical'::character varying)::text]", "('critical'::character varying)::text, ('x'::character varying)::text]")),
        ("allowed value removed", lambda d: d.replace(", ('critical'::character varying)::text", "")),
        ("allowed value changed", lambda d: d.replace("'critical'", "'fatal'")),
        ("different constrained column", lambda d: d.replace("(severity)::text", "(status)::text")),
        ("operator flipped to <> ALL", lambda d: d.replace("= ANY", "<> ALL")),
        ("literal order changed", lambda d: d.replace("('info'::character varying)::text, ('warning'::character varying)::text", "('warning'::character varying)::text, ('info'::character varying)::text")),
        ("duplicate literal added", lambda d: d.replace("('info'::character varying)::text,", "('info'::character varying)::text, ('info'::character varying)::text,")),
        ("column cast changed to numeric", lambda d: d.replace("(severity)::text", "(severity)::numeric")),
        ("extra OR clause appended", lambda d: d.replace("critical'::character varying)::text])))", "critical'::character varying)::text])) OR (note IS NULL)))")),
        ("NOT added", lambda d: d.replace("CHECK (((severity)", "CHECK ((NOT ((severity)").replace("])))", "]))))")),
    ],
)
def test_meaning_changing_differences_are_flagged_as_genuine(label, mutate):
    mutated = mutate(DST_SEVERITY)
    assert mutated != DST_SEVERITY, f"{label}: the mutation did not change the text (test bug)"
    assert _verdict(SRC_SEVERITY, mutated) == "genuine", label


def test_boolean_grouping_change_is_flagged_even_though_only_parentheses_move():
    """The demonstrated 9B-1 gap: (A AND B) OR C vs A AND (B OR C) share every token and differ only in grouping."""
    src_grouped_left = (
        "CHECK ((((severity)::text = ANY ((ARRAY['info'::character varying])::text[])) AND (a IS NULL)) OR (b IS NULL))"
    )
    dst_same_grouping = (
        "CHECK ((((severity)::text = ANY (ARRAY[('info'::character varying)::text])) AND (a IS NULL)) OR (b IS NULL))"
    )
    dst_regrouped = (
        "CHECK (((severity)::text = ANY (ARRAY[('info'::character varying)::text])) AND ((a IS NULL) OR (b IS NULL)))"
    )
    # control: identical grouping, only the known rendering differs -> accepted
    assert _verdict(src_grouped_left, dst_same_grouping) == "rendering_only"
    # the same tokens regrouped -> must be flagged
    assert _verdict(src_grouped_left, dst_regrouped) == "genuine"


def test_changed_literal_type_semantics_are_flagged():
    src = SRC_SEVERITY
    dst = DST_SEVERITY.replace("('info'::character varying)::text", "('info'::text)::text")
    assert _verdict(src, dst) == "genuine"


# ---------------------------------------------------------------- metadata and presence
def test_validation_state_change_is_flagged_even_when_the_text_is_the_known_rendering():
    assert _verdict(SRC_SEVERITY, DST_SEVERITY, dst_validated=False) == "genuine"


def test_non_check_constraint_with_the_same_text_pattern_is_never_treated_as_rendering_only():
    assert _verdict(SRC_SEVERITY, DST_SEVERITY, contype="f") == "genuine"


def test_constraint_present_on_only_one_side_is_genuine():
    src_row = ("t", "ck_t", "c", True, SRC_SEVERITY)
    assert split_constraint_diff({("t", "ck_t"): (src_row, None)}) == ({}, {("t", "ck_t"): (src_row, None)})
    assert split_constraint_diff({("t", "ck_t"): (None, src_row)}) == ({}, {("t", "ck_t"): (None, src_row)})


def test_renamed_or_retabled_constraint_is_genuine():
    src = ("t", "ck_t", "c", True, SRC_SEVERITY)
    assert split_constraint_diff({("t", "ck_t"): (src, ("t", "ck_other", "c", True, DST_SEVERITY))})[0] == {}
    assert split_constraint_diff({("t", "ck_t"): (src, ("other", "ck_t", "c", True, DST_SEVERITY))})[0] == {}


# ---------------------------------------------------------------- environment isolation
def test_scrub_removes_every_libpq_and_napsedge_database_variable_and_returns_only_names():
    decoy = "decoy-value-that-must-never-be-returned"
    env = {
        "PGPASSWORD": decoy, "PGSSLMODE": "disable", "PGHOST": decoy, "PGHOSTADDR": decoy, "PGSSLROOTCERT": decoy,
        "PGPASSFILE": decoy, "PGSERVICE": decoy, "PGOPTIONS": decoy, "PGUSER": decoy, "PGDATABASE": decoy,
        "NAPSEDGE_DATABASE_URL": f"postgresql://u:{decoy}@h/db", "NAPSEDGE_DATABASE_ECHO": "1",
        "PATH": "/usr/bin", "PYTHONPATH": "/x", "NAPSEDGE_NET_RANKINGS_SEASON": "2026", "DISCORD_BOT_TOKEN": decoy,
    }
    cleaned, removed = scrub_database_environment(env)
    assert removed == sorted(
        ["PGPASSWORD", "PGSSLMODE", "PGHOST", "PGHOSTADDR", "PGSSLROOTCERT", "PGPASSFILE", "PGSERVICE", "PGOPTIONS",
         "PGUSER", "PGDATABASE", "NAPSEDGE_DATABASE_URL", "NAPSEDGE_DATABASE_ECHO"]
    )
    assert set(cleaned) == {"PATH", "PYTHONPATH", "NAPSEDGE_NET_RANKINGS_SEASON", "DISCORD_BOT_TOKEN"}
    assert all(decoy not in name for name in removed), "a value leaked into the returned names"


def test_scrub_does_not_mutate_its_input_and_handles_an_empty_environment():
    env = {"PGPASSWORD": "x", "PATH": "/p"}
    snapshot = dict(env)
    scrub_database_environment(env)
    assert env == snapshot
    assert scrub_database_environment({}) == ({}, [])


def test_scrub_leaves_unrelated_variables_whose_names_merely_resemble_the_prefix():
    cleaned, removed = scrub_database_environment({"PYGPATH": "1", "XPGHOST": "1", "PG": "1"})
    assert removed == ["PG"] and set(cleaned) == {"PYGPATH", "XPGHOST"}


# ---------------------------------------------------------------- alembic url escaping
def test_escape_for_alembic_round_trips_through_configparser_interpolation():
    from alembic.config import Config

    url = "postgresql+psycopg://u:p%40ss%2Fword@localhost:5432/db?sslmode=verify-full&sslrootcert=C%3A%5Ctmp%5Cca.crt"
    cfg = Config()
    cfg.set_main_option("sqlalchemy.url", escape_for_alembic(url))
    assert cfg.get_main_option("sqlalchemy.url") == url


def test_unescaped_percent_url_would_have_been_rejected_by_alembic_config():
    """Control: proves the escape is necessary rather than decorative."""
    from alembic.config import Config

    with pytest.raises(ValueError):
        Config().set_main_option("sqlalchemy.url", "postgresql://u:p%40ss@h/db")


def test_escape_for_alembic_leaves_percent_free_urls_untouched():
    assert escape_for_alembic("postgresql://u:pw@h/db") == "postgresql://u:pw@h/db"


# ---------------------------------------------------------------- connection ownership (Phase 9B-5C1)
# An Engine opens a new connection per read; a Connection is used as is. SQLite stands in for PostgreSQL here because only the
# ownership rule is under test: a TEMP table is private to the connection that created it, so seeing it proves "the same connection".
def _counting_engine():
    from sqlalchemy import create_engine, event
    from sqlalchemy.pool import NullPool

    engine = create_engine("sqlite://", poolclass=NullPool)  # NullPool: every engine.connect() is a brand-new DBAPI connection
    opened = []
    event.listen(engine, "connect", lambda dbapi_connection, record: opened.append(dbapi_connection))
    return engine, opened


def test_reads_through_an_engine_each_use_a_separate_connection_so_they_cannot_share_a_transaction():
    from sqlalchemy import text

    engine, opened = _counting_engine()
    with engine.connect() as owner:
        owner.execute(text("CREATE TEMP TABLE only_here (x integer)"))
        before = len(opened)
        with pytest.raises(Exception, match="only_here"):
            rows(engine, "SELECT count(*) FROM only_here")  # a different connection: it cannot see the owner's temp table
        assert len(opened) == before + 1, "one new connection was opened for the single read"
        assert scalar(engine, "SELECT 1") == 1 and len(opened) == before + 2


def test_reads_through_a_connection_all_use_that_one_connection_and_never_close_or_commit_it():
    from sqlalchemy import text

    engine, opened = _counting_engine()
    with engine.connect() as owner:
        owner.execute(text("CREATE TEMP TABLE only_here (x integer)"))
        owner.execute(text("INSERT INTO only_here VALUES (1), (2)"))
        before = len(opened)
        assert scalar(owner, "SELECT count(*) FROM only_here") == 2
        assert rows(owner, "SELECT x FROM only_here ORDER BY x") == [(1,), (2,)]
        assert len(opened) == before, "no connection was opened: every read used the caller's connection"
        assert not owner.closed and owner.in_transaction(), "the helpers neither closed the connection nor ended the caller's transaction"
        owner.rollback()  # the caller still owns the transaction, so it can still end it
        assert scalar(owner, "SELECT count(*) FROM only_here") == 0
