"""phase 5 identity-key remediation: widen game_id to BigInteger

Hand-written, not left as autogenerate produced it -- autogenerate
against a SQLite target produces an EMPTY diff for this exact change
(SQLite's dynamic type affinity means `INTEGER` and `BIGINT` reflect
identically, so Alembic's type-comparison sees no difference to report).
This migration exists because `src.identity.game_identity` now mints
`game_id` via `stable_wide_id` (a 63-bit digest, scheme v2 -- see
`docs/identity_scheme.md` for the full collision-root-cause analysis),
which no longer fits in a 32-bit `Integer` column on a real PostgreSQL
target. Widens exactly the four columns that carry a `game_id` value to
`BigInteger` (`BigInteger().with_variant(Integer(), "sqlite")`, the same
convention already used for every other wide surrogate key in this
schema): `games.game_id` (the PK this scheme actually produces), plus its
three dependents -- `data_quality_issues.game_id`,
`team_game_log_observations.game_id`, `game_results.game_id` (both PK and
FK there). No other column, table, or constraint changes. No Phase 5A,
5B1, or 5B2 table is otherwise touched.

`op.batch_alter_table` is used for every column so this migration is
portable to SQLite (which requires a table-recreate dance for any
ALTER COLUMN) while still compiling to a plain, single, lossless
`ALTER TABLE ... ALTER COLUMN ... TYPE BIGINT` on PostgreSQL (INTEGER ->
BIGINT is always a safe, non-truncating widen there).

Downgrade is explicitly NOT unconditional: BIGINT -> INTEGER is a
narrowing operation, and a real scheme-v2 `game_id` (up to 63 bits) can
easily exceed the 32-bit signed `Integer` range that Phase 5A/5B1/5B2
originally shipped with. `downgrade()` queries every one of the four
columns first and refuses (raises `RuntimeError`, changes nothing) if any
already-persisted value would overflow a 32-bit signed `Integer` on
narrowing -- per this batch's own instruction, "if safe downgrade is
impossible after data conversion, stop and report the migration decision
rather than implementing a destructive downgrade." A disposable database
that has never received a scheme-v2 `game_id` (e.g. still sitting at a
pre-Phase-5B2 state, or only ever loaded with legacy 6-digit ids) can
still downgrade cleanly.

Revision ID: 259058c576b5
Revises: 4fed1844dcda
Create Date: 2026-09-08 00:28:42.726270

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '259058c576b5'
down_revision: Union[str, None] = '4fed1844dcda'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_BIG = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
_INT32_MAX = 2_147_483_647

#: (table, column, existing_nullable) for every column that carries a
#: `game_id` value -- widened together, narrowed together.
_GAME_ID_COLUMNS = (
    ("games", "game_id", False),
    ("data_quality_issues", "game_id", True),
    ("team_game_log_observations", "game_id", True),
    ("game_results", "game_id", False),
)


def upgrade() -> None:
    for table_name, column_name, existing_nullable in _GAME_ID_COLUMNS:
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column(
                column_name,
                existing_type=sa.Integer(),
                type_=_BIG,
                existing_nullable=existing_nullable,
            )


def downgrade() -> None:
    conn = op.get_bind()
    overflowing = []
    for table_name, column_name, _existing_nullable in _GAME_ID_COLUMNS:
        result = conn.execute(
            sa.text(f"SELECT COUNT(*) FROM {table_name} WHERE {column_name} > :max_val OR {column_name} < :min_val"),
            {"max_val": _INT32_MAX, "min_val": -_INT32_MAX - 1},
        )
        count = result.scalar()
        if count:
            overflowing.append(f"{table_name}.{column_name} ({count} row(s))")

    if overflowing:
        raise RuntimeError(
            "Cannot downgrade 259058c576b5: the following column(s) contain a value "
            "outside the 32-bit signed INTEGER range (a real scheme-v2 game_id, per "
            "docs/identity_scheme.md) that would be silently truncated or rejected by "
            "narrowing back to INTEGER: " + ", ".join(overflowing) + ". "
            "Downgrading here would destroy real identity information. Resolve this "
            "manually (e.g. remove or re-key the affected rows in a disposable "
            "database) before downgrading, rather than forcing a lossy narrow."
        )

    for table_name, column_name, existing_nullable in _GAME_ID_COLUMNS:
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column(
                column_name,
                existing_type=_BIG,
                type_=sa.Integer(),
                existing_nullable=existing_nullable,
            )
