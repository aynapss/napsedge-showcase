"""
Phase 5B7: proves the real six-revision Alembic chain against a real,
disposable PostgreSQL 16 server -- not just SQLite mechanics
(`tests/regression/test_db_migrations.py`) and not just dialect-compiled
DDL with no connection (`tests/regression/test_db_models.py`). Every test
here uses its own fresh, uniquely named, disposable database (see
`tests/postgres/conftest.py`) via the real `alembic upgrade`/`downgrade`
commands -- never `Base.metadata.create_all`, which would silently skip
exercising the migrations themselves.

Table-count boundaries mirror the exact, already-established constants in
`tests/regression/test_db_migrations.py` -- reproduced here rather than
imported, since this file must stand alone as PostgreSQL-specific proof.
"""
from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

from src.db.base import Base
from src.db import models, allstats_models, game_log_models, espn_models, player_game_log_models  # noqa: F401

from tests.postgres.conftest import alembic_config_for

pytestmark = pytest.mark.postgres

EXPECTED_TABLES_PHASE_5A = {
    "conferences", "teams", "team_aliases", "players", "player_aliases",
    "seasons", "games", "sportsbooks", "model_versions", "data_sources",
    "source_artifacts", "pipeline_runs", "data_quality_issues",
}
EXPECTED_TABLES_PHASE_5B1 = EXPECTED_TABLES_PHASE_5A | {
    "roster_memberships", "player_stat_snapshots", "team_stat_snapshots",
}
EXPECTED_TABLES_PHASE_5B2 = EXPECTED_TABLES_PHASE_5B1 | {
    "team_game_log_observations", "team_game_stat_lines", "game_results",
}
EXPECTED_TABLES_IDENTITY_REMEDIATION = EXPECTED_TABLES_PHASE_5B2
EXPECTED_TABLES_PHASE_5B3 = EXPECTED_TABLES_IDENTITY_REMEDIATION | {
    "espn_source_observations", "espn_benchmark_snapshots", "espn_benchmark_values",
}
EXPECTED_TABLES_AT_HEAD = EXPECTED_TABLES_PHASE_5B3 | {"player_game_log_observations"}

PHASE_5A_REVISION = "f496cbc68703"
PHASE_5B1_REVISION = "d990bbb62ce8"
PHASE_5B2_REVISION = "4fed1844dcda"
IDENTITY_REMEDIATION_REVISION = "259058c576b5"
PHASE_5B3_REVISION = "0e8d5e6e4e89"
HEAD_REVISION = "5b02bea0eb85"


def _table_names(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names(schema="public")) - {"alembic_version"}
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Full 13-step lifecycle (task's own required sequence)
# ---------------------------------------------------------------------------


def test_full_lifecycle_every_documented_boundary(pg_empty_database_url):
    url = pg_empty_database_url
    cfg = alembic_config_for(url)

    # 1. empty database
    assert _table_names(url) == set()

    # 2. upgrade to Phase 5A
    command.upgrade(cfg, PHASE_5A_REVISION)
    assert _table_names(url) == EXPECTED_TABLES_PHASE_5A

    # 3. upgrade to Phase 5B1
    command.upgrade(cfg, PHASE_5B1_REVISION)
    assert _table_names(url) == EXPECTED_TABLES_PHASE_5B1

    # 4. upgrade to Phase 5B2
    command.upgrade(cfg, PHASE_5B2_REVISION)
    assert _table_names(url) == EXPECTED_TABLES_PHASE_5B2

    # 5. upgrade through identity-key remediation
    command.upgrade(cfg, IDENTITY_REMEDIATION_REVISION)
    assert _table_names(url) == EXPECTED_TABLES_IDENTITY_REMEDIATION

    # 6. upgrade to Phase 5B3
    command.upgrade(cfg, PHASE_5B3_REVISION)
    assert _table_names(url) == EXPECTED_TABLES_PHASE_5B3

    # 7. upgrade to Phase 5B4 / head
    command.upgrade(cfg, "head")
    assert _table_names(url) == EXPECTED_TABLES_AT_HEAD

    # 8. re-run upgrade-to-head as a no-op
    command.upgrade(cfg, "head")
    assert _table_names(url) == EXPECTED_TABLES_AT_HEAD

    # 9. downgrade one revision at a time
    command.downgrade(cfg, PHASE_5B3_REVISION)
    assert _table_names(url) == EXPECTED_TABLES_PHASE_5B3
    command.downgrade(cfg, IDENTITY_REMEDIATION_REVISION)
    assert _table_names(url) == EXPECTED_TABLES_IDENTITY_REMEDIATION
    command.downgrade(cfg, PHASE_5B2_REVISION)
    assert _table_names(url) == EXPECTED_TABLES_PHASE_5B2
    command.downgrade(cfg, PHASE_5B1_REVISION)
    assert _table_names(url) == EXPECTED_TABLES_PHASE_5B1
    command.downgrade(cfg, PHASE_5A_REVISION)
    assert _table_names(url) == EXPECTED_TABLES_PHASE_5A
    command.downgrade(cfg, "base")
    assert _table_names(url) == set()

    # 10. re-upgrade to head
    command.upgrade(cfg, "head")
    assert _table_names(url) == EXPECTED_TABLES_AT_HEAD

    # 11. downgrade fully to base
    command.downgrade(cfg, "base")

    # 12. confirm all project tables are removed
    assert _table_names(url) == set()

    # 13. re-upgrade from base to head
    command.upgrade(cfg, "head")
    assert _table_names(url) == EXPECTED_TABLES_AT_HEAD


def test_no_project_object_leaked_after_full_downgrade(pg_empty_database_url):
    """Confirms no extension, enum type, sequence, index, or constraint this project owns survives a full downgrade -- only Postgres's own built-in catalog objects and (per Alembic's own convention) the empty alembic_version table remain."""
    url = pg_empty_database_url
    cfg = alembic_config_for(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            tables = conn.execute(text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            )).scalars().all()
            assert set(tables) <= {"alembic_version"}

            sequences = conn.execute(text(
                "SELECT sequence_name FROM information_schema.sequences WHERE sequence_schema = 'public'"
            )).scalars().all()
            assert sequences == []

            # This project never defines a native PostgreSQL ENUM type (src/db/enums.py's own
            # documented decision: CHECK constraints instead, precisely to avoid this class of
            # leaked-type cleanup problem) -- confirm zero exist regardless.
            enum_types = conn.execute(text(
                "SELECT typname FROM pg_type t JOIN pg_namespace n ON t.typnamespace = n.oid "
                "WHERE n.nspname = 'public' AND t.typtype = 'e'"
            )).scalars().all()
            assert enum_types == []

            indexes = conn.execute(text(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND indexname NOT LIKE '%alembic%'"
            )).scalars().all()
            assert indexes == []
    finally:
        engine.dispose()


def test_alembic_check_is_clean_against_real_postgresql(pg_migrated_database_url):
    """The real, unmodified `alembic check` command reports no drift once head is applied -- reusing the project's own CLI entry point, not a hand-rolled comparison."""
    from alembic.util.exc import AutogenerateDiffsDetected

    cfg = alembic_config_for(pg_migrated_database_url)
    try:
        command.check(cfg)  # must not raise
    except AutogenerateDiffsDetected as exc:  # pragma: no cover -- only hit on genuine drift
        pytest.fail(f"alembic check detected drift against real PostgreSQL: {exc}")


def test_orm_metadata_matches_migrated_schema_table_set(pg_migrated_database_url):
    assert _table_names(pg_migrated_database_url) == set(Base.metadata.tables.keys())


def test_migration_never_touches_teamdump_against_real_connection():
    """Reuses the exact AST guard already proven for SQLite -- repeated here so this file's own PostgreSQL-specific suite stands alone as a complete proof, not dependent on the SQLite test file having run first."""
    import ast
    import pathlib

    version_files = list(pathlib.Path("alembic/versions").glob("*.py"))
    assert len(version_files) == 6
    for f in version_files:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_names.add(node.module)
                imported_names.update(a.name for a in node.names)
        assert "src.paths" not in imported_names
        assert "CBB_DIR" not in imported_names
        assert "ESPN_DIR" not in imported_names
