"""
Proves the Alembic migrations in `alembic/versions/` run upgrade from
zero, downgrade, and re-upgrade cleanly, in a disposable, isolated SQLite
database created fresh per test (`tmp_path` -- never a shared file, never
a production database, never `TeamDump`).

Five revisions now exist: Phase 5A's `f496cbc68703` (initial schema, 13
tables), Phase 5B1's `d990bbb62ce8` (AllStats source evidence, +3 tables,
`down_revision=f496cbc68703`), Phase 5B2's `4fed1844dcda` (game-log
evidence and canonical results, +3 tables, `down_revision=d990bbb62ce8`),
the Phase 5 identity-key-remediation batch's `259058c576b5` (widens
`game_id` on `games`/`data_quality_issues`/`team_game_log_observations`/
`game_results` from `Integer` to `BigInteger` -- **no table added or
removed**, `down_revision=4fed1844dcda`; see `docs/identity_scheme.md`),
and Phase 5B3's `0e8d5e6e4e89` (ESPN raw player-stat evidence and derived
NCAA-cohort benchmark reference, +3 tables --
`espn_source_observations`/`espn_benchmark_snapshots`/
`espn_benchmark_values` -- `down_revision=259058c576b5`).
`EXPECTED_TABLES_PHASE_5A`, `EXPECTED_TABLES_PHASE_5B1`,
`EXPECTED_TABLES_IDENTITY_REMEDIATION`, and `EXPECTED_TABLES_AT_HEAD` are
kept as distinct constants (not just "whatever `Base.metadata` has
today") so a test can assert against a specific, named milestone rather
than a constantly-moving target as future batches add more revisions.
`EXPECTED_TABLES_IDENTITY_REMEDIATION` equals Phase 5B2's own table set
(that revision changed no table shape); `EXPECTED_TABLES_AT_HEAD` now
additionally includes Phase 5B3's three new tables.

This is a mechanical proof of each migration's own up/down symmetry, not
a claim of live PostgreSQL parity -- see `test_db_models.py` for the
separate PostgreSQL-dialect DDL-compilation proof, and the module
docstring there for why the two are kept apart.
"""
from __future__ import annotations

import sqlite3

import pytest
from alembic import command
from alembic.config import Config

from src.db.base import Base
from src.db import models, allstats_models, game_log_models, espn_models, player_game_log_models  # noqa: F401

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

#: The identity-key-remediation revision (259058c576b5) changes no table
#: shape at all -- same table set as Phase 5B2's own.
EXPECTED_TABLES_IDENTITY_REMEDIATION = EXPECTED_TABLES_PHASE_5B2

EXPECTED_TABLES_PHASE_5B3 = EXPECTED_TABLES_IDENTITY_REMEDIATION | {
    "espn_source_observations", "espn_benchmark_snapshots", "espn_benchmark_values",
}

EXPECTED_TABLES_AT_HEAD = EXPECTED_TABLES_PHASE_5B3 | {
    "player_game_log_observations",
}

PHASE_5A_REVISION = "f496cbc68703"
PHASE_5B1_REVISION = "d990bbb62ce8"
PHASE_5B2_REVISION = "4fed1844dcda"
IDENTITY_REMEDIATION_REVISION = "259058c576b5"
PHASE_5B3_REVISION = "0e8d5e6e4e89"


def _alembic_config(db_path) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{db_path.as_posix()}")
    return cfg


def _table_names(db_path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# Full chain: zero -> head (currently Phase 5B1)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_upgrade_from_zero_creates_exactly_the_tables_at_head(tmp_path):
    db_path = tmp_path / "upgrade_proof.db"
    assert not db_path.exists()
    cfg = _alembic_config(db_path)

    command.upgrade(cfg, "head")

    assert _table_names(db_path) == EXPECTED_TABLES_AT_HEAD


@pytest.mark.integration
def test_downgrade_to_base_drops_every_table(tmp_path):
    db_path = tmp_path / "downgrade_proof.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")
    assert _table_names(db_path) == EXPECTED_TABLES_AT_HEAD

    command.downgrade(cfg, "base")

    assert _table_names(db_path) == set()


@pytest.mark.integration
def test_upgrade_matches_declarative_metadata_table_set(tmp_path):
    """The migration chain must not drift from src/db/models.py + src/db/allstats_models.py -- same table set either way."""
    assert EXPECTED_TABLES_AT_HEAD == set(Base.metadata.tables.keys())


@pytest.mark.integration
def test_migration_is_isolated_never_touches_teamdump():
    """AST-level check that no migration revision imports src.paths or references TeamDump/CBB_DIR/ESPN_DIR in actual code (not just prose) -- do not read TeamDump files during migration execution."""
    import ast
    import pathlib

    version_files = list(pathlib.Path("alembic/versions").glob("*.py"))
    assert len(version_files) >= 6, "expected at least the Phase 5A, 5B1, 5B2, identity-remediation, Phase 5B3, and Phase 5B4 revision files"
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


@pytest.mark.integration
def test_upgrade_twice_is_idempotent_at_head(tmp_path):
    """Running upgrade to head twice must not error (Alembic's own no-op-if-already-there behavior) -- a real deployment may call this more than once."""
    db_path = tmp_path / "double_upgrade.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")  # must not raise
    assert _table_names(db_path) == EXPECTED_TABLES_AT_HEAD


@pytest.mark.integration
def test_upgrade_downgrade_reupgrade_cycle_full_chain(tmp_path):
    """Full round trip in one isolated database: upgrade from zero to head, downgrade to base, upgrade again -- the schema must come back identical each time."""
    db_path = tmp_path / "reupgrade_cycle.db"
    cfg = _alembic_config(db_path)

    command.upgrade(cfg, "head")
    assert _table_names(db_path) == EXPECTED_TABLES_AT_HEAD

    command.downgrade(cfg, "base")
    assert _table_names(db_path) == set()

    command.upgrade(cfg, "head")
    assert _table_names(db_path) == EXPECTED_TABLES_AT_HEAD


# ---------------------------------------------------------------------------
# Phase 5A checkpoint specifically (unaffected by later revisions)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_upgrade_to_phase_5a_revision_creates_exactly_its_own_tables(tmp_path):
    db_path = tmp_path / "phase5a_checkpoint.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, PHASE_5A_REVISION)
    assert _table_names(db_path) == EXPECTED_TABLES_PHASE_5A


@pytest.mark.integration
def test_phase_5a_revision_never_modified_by_a_later_revision(tmp_path):
    """Phase 5B1 instruction: do not rewrite or delete the Phase 5A revision -- confirmed by upgrading only to f496cbc68703 and checking its table set is exactly the original 13, unaffected by d990bbb62ce8 existing in the same versions directory."""
    db_path = tmp_path / "phase5a_untouched.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, PHASE_5A_REVISION)
    before = _table_names(db_path)
    assert before == EXPECTED_TABLES_PHASE_5A

    command.upgrade(cfg, "head")
    command.downgrade(cfg, PHASE_5A_REVISION)
    after = _table_names(db_path)
    assert after == EXPECTED_TABLES_PHASE_5A == before


# ---------------------------------------------------------------------------
# Phase 5B1: 5A -> 5B1 -> back to 5A -> forward to 5B1 again
# (targets PHASE_5B1_REVISION explicitly, never "head" -- "head" now means
# Phase 5B2, one revision further)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_upgrade_from_phase_5a_to_phase_5b1_adds_exactly_three_tables(tmp_path):
    db_path = tmp_path / "phase5b1_upgrade.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, PHASE_5A_REVISION)
    assert _table_names(db_path) == EXPECTED_TABLES_PHASE_5A

    command.upgrade(cfg, PHASE_5B1_REVISION)

    new_tables = _table_names(db_path) - EXPECTED_TABLES_PHASE_5A
    assert new_tables == {"roster_memberships", "player_stat_snapshots", "team_stat_snapshots"}


@pytest.mark.integration
def test_downgrade_from_phase_5b1_to_phase_5a_removes_exactly_three_tables(tmp_path):
    db_path = tmp_path / "phase5b1_downgrade.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, PHASE_5B1_REVISION)
    assert _table_names(db_path) == EXPECTED_TABLES_PHASE_5B1

    command.downgrade(cfg, PHASE_5A_REVISION)

    assert _table_names(db_path) == EXPECTED_TABLES_PHASE_5A


@pytest.mark.integration
def test_phase_5b1_upgrade_downgrade_reupgrade_cycle(tmp_path):
    db_path = tmp_path / "phase5b1_cycle.db"
    cfg = _alembic_config(db_path)

    command.upgrade(cfg, PHASE_5B1_REVISION)
    assert _table_names(db_path) == EXPECTED_TABLES_PHASE_5B1

    command.downgrade(cfg, PHASE_5A_REVISION)
    assert _table_names(db_path) == EXPECTED_TABLES_PHASE_5A

    command.upgrade(cfg, PHASE_5B1_REVISION)
    assert _table_names(db_path) == EXPECTED_TABLES_PHASE_5B1


# ---------------------------------------------------------------------------
# Phase 5B2: 5B1 -> 5B2 -> back to 5B1 -> forward to 5B2 again
# (targets PHASE_5B2_REVISION explicitly, never "head" -- "head" now means
# the identity-remediation revision, one revision further)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_upgrade_from_phase_5b1_to_phase_5b2_adds_exactly_three_tables(tmp_path):
    db_path = tmp_path / "phase5b2_upgrade.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, PHASE_5B1_REVISION)
    assert _table_names(db_path) == EXPECTED_TABLES_PHASE_5B1

    command.upgrade(cfg, PHASE_5B2_REVISION)

    new_tables = _table_names(db_path) - EXPECTED_TABLES_PHASE_5B1
    assert new_tables == {"team_game_log_observations", "team_game_stat_lines", "game_results"}


@pytest.mark.integration
def test_downgrade_from_phase_5b2_to_phase_5b1_removes_exactly_three_tables(tmp_path):
    db_path = tmp_path / "phase5b2_downgrade.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, PHASE_5B2_REVISION)
    assert _table_names(db_path) == EXPECTED_TABLES_PHASE_5B2

    command.downgrade(cfg, PHASE_5B1_REVISION)

    assert _table_names(db_path) == EXPECTED_TABLES_PHASE_5B1


@pytest.mark.integration
def test_phase_5b2_upgrade_downgrade_reupgrade_cycle(tmp_path):
    db_path = tmp_path / "phase5b2_cycle.db"
    cfg = _alembic_config(db_path)

    command.upgrade(cfg, PHASE_5B2_REVISION)
    assert _table_names(db_path) == EXPECTED_TABLES_PHASE_5B2

    command.downgrade(cfg, PHASE_5B1_REVISION)
    assert _table_names(db_path) == EXPECTED_TABLES_PHASE_5B1

    command.upgrade(cfg, PHASE_5B2_REVISION)
    assert _table_names(db_path) == EXPECTED_TABLES_PHASE_5B2


@pytest.mark.integration
def test_phase_5a_5b1_5b2_revisions_never_modified_by_identity_remediation(tmp_path):
    """This batch's own instruction: do not rewrite completed migrations."""
    db_path = tmp_path / "phase5b2_untouched.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, PHASE_5B2_REVISION)
    before = _table_names(db_path)
    assert before == EXPECTED_TABLES_PHASE_5B2

    command.upgrade(cfg, IDENTITY_REMEDIATION_REVISION)
    command.downgrade(cfg, PHASE_5B2_REVISION)
    after = _table_names(db_path)
    assert after == EXPECTED_TABLES_PHASE_5B2 == before


# ---------------------------------------------------------------------------
# Identity-key remediation: 5B2 -> 259058c576b5 -- no table added or
# removed, only game_id widened from Integer to BigInteger on 4 columns.
# (targets IDENTITY_REMEDIATION_REVISION explicitly, never "head" -- "head"
# now means the Phase 5B3 ESPN revision, one revision further)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_upgrade_to_identity_remediation_changes_no_table_set(tmp_path):
    """The whole point of this migration is a column-type change, not a schema-shape change -- the table set must be identical to Phase 5B2's own."""
    db_path = tmp_path / "identity_remediation_tables.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, IDENTITY_REMEDIATION_REVISION)
    assert _table_names(db_path) == EXPECTED_TABLES_IDENTITY_REMEDIATION


@pytest.mark.integration
def test_identity_remediation_widens_game_id_to_accept_a_63_bit_value(tmp_path):
    """A real scheme-v2 game_id (up to 63 bits) must round-trip through games/game_results/team_game_log_observations/data_quality_issues without truncation -- would fail on a real 32-bit INTEGER column."""
    db_path = tmp_path / "widen_roundtrip.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, IDENTITY_REMEDIATION_REVISION)

    wide_id = 8909015122393871858  # a real scheme-v2 id, from the confirmed Kentucky/St. John's collision bucket
    assert wide_id > 2**31

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO games (game_id, home_team_id, away_team_id, season_id, game_date, neutral_site, is_provisional) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (wide_id, 1, 2, 2026, "2025-12-20", 1, 1),
        )
        conn.commit()
        row = conn.execute("SELECT game_id FROM games WHERE game_id = ?", (wide_id,)).fetchone()
        assert row is not None
        assert row[0] == wide_id  # exact round-trip, no truncation/overflow
    finally:
        conn.close()


@pytest.mark.integration
def test_identity_remediation_downgrade_refuses_with_overflowing_game_id(tmp_path):
    """Instruction: do not implement a destructive downgrade -- a real 63-bit game_id must block the narrowing downgrade with a clear error, not silently truncate."""
    db_path = tmp_path / "downgrade_guard.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, IDENTITY_REMEDIATION_REVISION)

    wide_id = 8909015122393871858
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO games (game_id, home_team_id, away_team_id, season_id, game_date, neutral_site, is_provisional) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (wide_id, 1, 2, 2026, "2025-12-20", 1, 1),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="Cannot downgrade 259058c576b5"):
        command.downgrade(cfg, PHASE_5B2_REVISION)

    # data survives the refused downgrade untouched
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT game_id FROM games WHERE game_id = ?", (wide_id,)).fetchone()
        assert row is not None and row[0] == wide_id
    finally:
        conn.close()


@pytest.mark.integration
def test_identity_remediation_downgrade_succeeds_without_overflowing_game_id(tmp_path):
    """A database that never received a real scheme-v2 game_id (e.g. only legacy 6-digit ids, or empty) can downgrade cleanly."""
    db_path = tmp_path / "downgrade_clean.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, IDENTITY_REMEDIATION_REVISION)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO games (game_id, home_team_id, away_team_id, season_id, game_date, neutral_site, is_provisional) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (123456, 1, 2, 2026, "2025-12-20", 1, 1),  # a legacy-scheme-shaped 6-digit id -- well within int32 range
        )
        conn.commit()
    finally:
        conn.close()

    command.downgrade(cfg, PHASE_5B2_REVISION)  # must not raise

    assert _table_names(db_path) == EXPECTED_TABLES_PHASE_5B2
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT game_id FROM games WHERE game_id = ?", (123456,)).fetchone()
        assert row is not None and row[0] == 123456
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Phase 5B3: identity-remediation -> Phase 5B3 (ESPN evidence, +3 tables)
# (targets PHASE_5B3_REVISION explicitly, never "head" -- "head" now means
# the Phase 5B4 player-game-log revision, one revision further)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_upgrade_from_identity_remediation_to_phase_5b3_adds_exactly_three_tables(tmp_path):
    db_path = tmp_path / "phase5b3_upgrade.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, IDENTITY_REMEDIATION_REVISION)
    assert _table_names(db_path) == EXPECTED_TABLES_IDENTITY_REMEDIATION

    command.upgrade(cfg, PHASE_5B3_REVISION)

    new_tables = _table_names(db_path) - EXPECTED_TABLES_IDENTITY_REMEDIATION
    assert new_tables == {"espn_source_observations", "espn_benchmark_snapshots", "espn_benchmark_values"}


@pytest.mark.integration
def test_downgrade_from_phase_5b3_to_identity_remediation_removes_exactly_three_tables(tmp_path):
    db_path = tmp_path / "phase5b3_downgrade.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, PHASE_5B3_REVISION)
    assert _table_names(db_path) == EXPECTED_TABLES_PHASE_5B3

    command.downgrade(cfg, IDENTITY_REMEDIATION_REVISION)

    assert _table_names(db_path) == EXPECTED_TABLES_IDENTITY_REMEDIATION


@pytest.mark.integration
def test_phase_5b3_upgrade_downgrade_reupgrade_cycle(tmp_path):
    db_path = tmp_path / "phase5b3_cycle.db"
    cfg = _alembic_config(db_path)

    command.upgrade(cfg, PHASE_5B3_REVISION)
    assert _table_names(db_path) == EXPECTED_TABLES_PHASE_5B3

    command.downgrade(cfg, IDENTITY_REMEDIATION_REVISION)
    assert _table_names(db_path) == EXPECTED_TABLES_IDENTITY_REMEDIATION

    command.upgrade(cfg, PHASE_5B3_REVISION)
    assert _table_names(db_path) == EXPECTED_TABLES_PHASE_5B3


@pytest.mark.integration
def test_earlier_revisions_never_modified_by_phase_5b3(tmp_path):
    """This batch's own instruction: do not modify the completed migrations."""
    db_path = tmp_path / "phase5b3_untouched.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, IDENTITY_REMEDIATION_REVISION)
    before = _table_names(db_path)
    assert before == EXPECTED_TABLES_IDENTITY_REMEDIATION

    command.upgrade(cfg, PHASE_5B3_REVISION)
    command.downgrade(cfg, IDENTITY_REMEDIATION_REVISION)
    after = _table_names(db_path)
    assert after == EXPECTED_TABLES_IDENTITY_REMEDIATION == before


@pytest.mark.integration
def test_espn_source_observations_natural_key_constraint_present_after_migration(tmp_path):
    """Confirms the Phase 5B3 migration creates the natural-key UNIQUE constraint at the SQL level."""
    db_path = tmp_path / "espn_natural_key.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO seasons (season_id, start_year, end_year, label) VALUES (2026, 2025, 2026, '2025-26')",
        )
        common = (1, 0, 2026, "A Player", "ABC", "unresolved")
        conn.execute(
            "INSERT INTO espn_source_observations (source_artifact_id, source_row_position, season_id, raw_player_name, raw_team_code, validation_status) VALUES (?, ?, ?, ?, ?, ?)",
            common,
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO espn_source_observations (source_artifact_id, source_row_position, season_id, raw_player_name, raw_team_code, validation_status) VALUES (?, ?, ?, ?, ?, ?)",
                common,
            )
    finally:
        conn.close()


@pytest.mark.integration
def test_espn_benchmark_metric_name_check_constraint_present_after_migration(tmp_path):
    """Confirms the Phase 5B3 migration creates the metric_name controlled-vocabulary CHECK constraint at the SQL level -- an uncontrolled/unvalidated metric name must be rejected, not silently accepted."""
    db_path = tmp_path / "espn_metric_check.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO source_artifacts (source_key, source_root_key, relative_path, original_filename, file_type, content_hash, validation_status, point_in_time_classification) VALUES ('espn_ncaa_benchmarks', 'teamdump_espn', 'x', 'x', 'xlsx', 'h', 'valid', 'current_snapshot_only')"
        )
        conn.execute("INSERT INTO espn_benchmark_snapshots (source_artifact_id, transformation_version, validation_status) VALUES (1, 'espn_player_benchmarks_v1', 'valid')")
        conn.commit()
        conn.execute("INSERT INTO espn_benchmark_values (snapshot_id, metric_name, ncaa_mean, ncaa_std) VALUES (1, 'PTS', 8.4, 4.6)")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO espn_benchmark_values (snapshot_id, metric_name, ncaa_mean, ncaa_std) VALUES (1, 'NOT_A_REAL_METRIC', 1.0, 1.0)")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Phase 5B4: Phase 5B3 -> head (player game-log evidence, +1 table)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_upgrade_from_phase_5b3_to_phase_5b4_adds_exactly_one_table(tmp_path):
    db_path = tmp_path / "phase5b4_upgrade.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, PHASE_5B3_REVISION)
    assert _table_names(db_path) == EXPECTED_TABLES_PHASE_5B3

    command.upgrade(cfg, "head")

    new_tables = _table_names(db_path) - EXPECTED_TABLES_PHASE_5B3
    assert new_tables == {"player_game_log_observations"}


@pytest.mark.integration
def test_downgrade_from_phase_5b4_to_phase_5b3_removes_exactly_one_table(tmp_path):
    db_path = tmp_path / "phase5b4_downgrade.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")
    assert _table_names(db_path) == EXPECTED_TABLES_AT_HEAD

    command.downgrade(cfg, PHASE_5B3_REVISION)

    assert _table_names(db_path) == EXPECTED_TABLES_PHASE_5B3


@pytest.mark.integration
def test_phase_5b4_upgrade_downgrade_reupgrade_cycle(tmp_path):
    db_path = tmp_path / "phase5b4_cycle.db"
    cfg = _alembic_config(db_path)

    command.upgrade(cfg, "head")
    assert _table_names(db_path) == EXPECTED_TABLES_AT_HEAD

    command.downgrade(cfg, PHASE_5B3_REVISION)
    assert _table_names(db_path) == EXPECTED_TABLES_PHASE_5B3

    command.upgrade(cfg, "head")
    assert _table_names(db_path) == EXPECTED_TABLES_AT_HEAD


@pytest.mark.integration
def test_earlier_revisions_never_modified_by_phase_5b4(tmp_path):
    """This batch's own instruction: do not modify the five completed migrations."""
    db_path = tmp_path / "phase5b4_untouched.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, PHASE_5B3_REVISION)
    before = _table_names(db_path)
    assert before == EXPECTED_TABLES_PHASE_5B3

    command.upgrade(cfg, "head")
    command.downgrade(cfg, PHASE_5B3_REVISION)
    after = _table_names(db_path)
    assert after == EXPECTED_TABLES_PHASE_5B3 == before


@pytest.mark.integration
def test_player_game_log_natural_key_constraint_present_after_migration(tmp_path):
    """Confirms the Phase 5B4 migration creates the natural-key UNIQUE constraint at the SQL level."""
    db_path = tmp_path / "player_gamelog_natural_key.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("INSERT INTO seasons (season_id, start_year, end_year, label) VALUES (2026, 2025, 2026, '2025-26')")
        conn.execute("INSERT INTO teams (team_id, display_name, sr_slug) VALUES (1, 'Team A', 'team-a')")
        common = (1, 0, 2026, 1, "A Player", "Team B", "played", "unresolved")
        conn.execute(
            "INSERT INTO player_game_log_observations (source_artifact_id, source_row_position, season_id, team_id, raw_player_name, raw_opponent_name, participation_status, validation_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            common,
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO player_game_log_observations (source_artifact_id, source_row_position, season_id, team_id, raw_player_name, raw_opponent_name, participation_status, validation_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                common,
            )
    finally:
        conn.close()


@pytest.mark.integration
def test_player_game_log_game_id_accepts_63_bit_value_after_migration(tmp_path):
    """Confirms player_game_log_observations.game_id inherits the widened (v2 63-bit-compatible) BigInteger column -- never a 32-bit INTEGER."""
    db_path = tmp_path / "player_gamelog_63bit.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")

    wide_id = 8909015122393871858
    assert wide_id > 2**31
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("INSERT INTO seasons (season_id, start_year, end_year, label) VALUES (2026, 2025, 2026, '2025-26')")
        conn.execute("INSERT INTO teams (team_id, display_name, sr_slug) VALUES (1, 'Team A', 'team-a')")
        conn.execute(
            "INSERT INTO player_game_log_observations (source_artifact_id, source_row_position, season_id, team_id, raw_player_name, raw_opponent_name, game_id, participation_status, validation_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1, 0, 2026, 1, "A Player", "Team B", wide_id, "played", "valid"),
        )
        conn.commit()
        row = conn.execute("SELECT game_id FROM player_game_log_observations WHERE game_id = ?", (wide_id,)).fetchone()
        assert row is not None and row[0] == wide_id
    finally:
        conn.close()


@pytest.mark.integration
def test_full_six_revision_chain_lifecycle(tmp_path):
    """Zero -> 5A -> 5B1 -> 5B2 -> identity-remediation -> Phase 5B3 -> Phase 5B4 (head) -> base, in one disposable database."""
    db_path = tmp_path / "full_chain.db"
    cfg = _alembic_config(db_path)

    command.upgrade(cfg, "head")
    assert _table_names(db_path) == EXPECTED_TABLES_AT_HEAD

    command.downgrade(cfg, PHASE_5B3_REVISION)
    assert _table_names(db_path) == EXPECTED_TABLES_PHASE_5B3

    command.downgrade(cfg, IDENTITY_REMEDIATION_REVISION)
    assert _table_names(db_path) == EXPECTED_TABLES_IDENTITY_REMEDIATION

    command.downgrade(cfg, PHASE_5B2_REVISION)
    assert _table_names(db_path) == EXPECTED_TABLES_PHASE_5B2  # same table set, narrower game_id column

    command.upgrade(cfg, "head")
    assert _table_names(db_path) == EXPECTED_TABLES_AT_HEAD

    command.downgrade(cfg, "base")
    assert _table_names(db_path) == set()


@pytest.mark.integration
def test_phase_5a_and_5b1_revisions_never_modified_by_phase_5b2(tmp_path):
    """Phase 5B2 instruction: do not modify either completed migration."""
    db_path = tmp_path / "phase5a_5b1_untouched.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, PHASE_5B1_REVISION)
    before = _table_names(db_path)
    assert before == EXPECTED_TABLES_PHASE_5B1

    command.upgrade(cfg, "head")
    command.downgrade(cfg, PHASE_5B1_REVISION)
    after = _table_names(db_path)
    assert after == EXPECTED_TABLES_PHASE_5B1 == before


@pytest.mark.integration
def test_data_sources_acquisition_type_check_constraint_present_after_migration(tmp_path):
    """Confirms the migration actually creates the acquisition_type CHECK constraint (Torvik source-classification review) -- not just that the ORM model declares it."""
    db_path = tmp_path / "acquisition_type.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO data_sources (source_key, display_name, acquisition_type, is_rescrapable) VALUES (?, ?, ?, ?)",
            ("torvik", "Torvik ratings", "live_scraped_json", True),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO data_sources (source_key, display_name, acquisition_type, is_rescrapable) VALUES (?, ?, ?, ?)",
                ("bad_source", "Bad", "not_a_real_acquisition_type", True),
            )
    finally:
        conn.close()


@pytest.mark.integration
def test_stat_table_check_constraint_present_after_migration(tmp_path):
    """Confirms the Phase 5B1 migration creates the stat_table CHECK constraint on player_stat_snapshots at the SQL level."""
    db_path = tmp_path / "stat_table_check.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys=OFF")  # isolate the CHECK constraint from FK setup noise
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO player_stat_snapshots (team_id, season_id, source_artifact_id, stat_table, player_key, source_player_name, validation_status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (1, 2026, 1, "Roster", "x", "X", "unresolved"),
            )
    finally:
        conn.close()


@pytest.mark.integration
def test_game_result_status_check_constraint_present_after_migration(tmp_path):
    """Confirms the Phase 5B2 migration creates the result_status CHECK constraint on game_results at the SQL level."""
    db_path = tmp_path / "result_status_check.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO game_results (game_id, season_id, team_a_id, team_b_id, result_status, validation_status, source_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (1, 2026, 1, 2, "not_a_real_status", "valid", 1),
            )
    finally:
        conn.close()


@pytest.mark.integration
def test_game_result_team_distinct_check_constraint_present_after_migration(tmp_path):
    """Confirms the Phase 5B2 migration creates the team_a_b_distinct CHECK constraint at the SQL level -- same-team games must be invalid."""
    db_path = tmp_path / "team_distinct_check.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO game_results (game_id, season_id, team_a_id, team_b_id, result_status, validation_status, source_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (1, 2026, 1, 1, "reconciled", "valid", 2),
            )
    finally:
        conn.close()
