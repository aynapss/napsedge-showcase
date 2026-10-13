"""
Phase 5B1 foundation ORM models: local CBB AllStats source evidence.

Kept in a separate module from `src/db/models.py` (the Phase 5A
foundation) so the Phase 5A/5B1 boundary stays legible in the codebase
the same way it does in the Alembic revision history -- importing
`src.db.models` alone still gives every Phase 5A table; a caller that
also needs Phase 5B1 imports this module too. Both share one
`Base`/`MetaData` (`src.db.base.Base`), so Alembic autogenerate sees all
16 tables together regardless of which module defines which.

GRAIN, DOCUMENTED BEFORE ANY TABLE (per Phase 5B1 instruction)

Real Sports-Reference AllStats workbooks (confirmed by reading
`TeamDump/CBB/st-bonaventure/*_2026.xlsx` and 20 other teams' workbooks
plus `TeamDump/CBB/albany-ny/*_2026.xlsx` as a structurally-different
sample, this batch) contain SIX sheets/files per team-season:
`Roster`, `PerGame`, `Totals`, `Per40`, `Per100`, `Advanced`.

- `Roster`: one row per rostered player. Biographical/roster fields only
  (no box-score stats). Never contains a team-aggregate row. -> maps to
  `roster_memberships`, one row per (player, team, season, source
  artifact).
- `PerGame`/`Totals`/`Per40`/`Per100`/`Advanced`: one row per player who
  accumulated at least the minimum evidence Sports-Reference requires for
  that specific derived table (row *counts differ across the five tables
  for the same team* -- a low-minutes player who appears in `Roster` and
  `PerGame` may be entirely absent from `Per40`/`Per100`; this is real,
  expected Sports-Reference behavior, not a data defect). `PerGame`,
  `Totals`, `Per40`, and `Advanced` additionally contain **exactly one**
  trailing row per table where `Player == "Team Totals"` -- a genuine
  source-level team aggregate, confirmed present in `PerGame`/`Totals`/
  `Advanced` and confirmed ABSENT from `Per40`/`Per100` (Sports-Reference
  does not publish a pace-adjusted team-total row). -> the player rows
  map to `player_stat_snapshots` (one row per player per stat table per
  source artifact); the "Team Totals" rows map to `team_stat_snapshots`
  (one row per team per stat table per source artifact) -- **never**
  coerced into a `player_stat_snapshots` row (Phase 5B1 instruction: "do
  not coerce season-total or header rows into players").

Every one of the five stat tables shares one join key, `PlayerKey`
(present in the great majority of real workbooks; confirmed this batch
that a real workbook -- `albany-ny` -- can genuinely omit it from every
one of its six files; the import adapter recomputes it via the same
already-tested `canonical_player_key_v15` `assemble_legacy_allstats_dataframe`
itself already falls back to in that exact case, never inventing a new
key scheme).

Percentage columns are preserved exactly as the source represents them,
not rescaled for consistency (rescaling would be a calculation): `FG%`/
`3P%`/`2P%`/`eFG%`/`FT%`/`TS%` are 0-1 fractions in the real data;
`ORB%`/`DRB%`/`TRB%`/`AST%`/`STL%`/`BLK%`/`TOV%`/`USG%` are 0-100
percents. Both are stored as plain nullable floats -- the scale
difference is a source-contract fact to document (here, and in
`docs/database_schema.md`), not a discrepancy to fix.

NOT AN EAV TABLE

`player_stat_snapshots`/`team_stat_snapshots` are wide, sparse tables --
every column is a real, named, individually-typed Sports-Reference
statistic (confirmed against the real workbooks, not invented), never a
generic `(stat_name, stat_value)` pair. A `PerGame`/`Totals`/`Per40`/
`Per100` row populates the box-score column group and leaves the
Advanced-only column group null; an `Advanced` row does the reverse. This
is an ordinary wide-sparse fact table (the same pattern as, e.g., an
`orders` table with both a `shipping_address` and a `digital_download_url`
column, only one of which applies per row), not an EAV table.

NO CALCULATED VALUE

Every column here is either a raw Sports-Reference value or a raw
biographical/roster fact. `run_allstats_transformation`'s own additive,
*calculated* columns (`O_Impact`/`D_Impact`/`Gravity`/`PlayerValueIndex`,
and the ~60 legacy-alias columns `apply_previous_workflow_metrics_v15`
adds) are deliberately NOT persisted anywhere in this module -- Phase 5B1
instruction: "do not store calculated OVR, classifications, team
strength, matchup values, betting values, or recommendations in these
source-evidence tables." A future Phase 5B batch that wants those values
queryable gets them by recomputing from these raw rows through the
already-tested transformation, never by reading a stored calculated
column here.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.enums import ALLSTATS_STAT_TABLES, VALIDATION_STATUSES, sql_check_in
from src.db.models import _BigIntegerPk


class RosterMembership(Base):
    """
    One row per (player, team, season, source_artifact) -- i.e. one row
    per real `Roster` table row of one imported workbook. Natural key
    intentionally includes `source_artifact_id` (not just player/team/
    season): a later re-scrape of the same team-season roster produces a
    NEW immutable `source_artifacts` row (Phase 5A policy, unchanged
    here), and this table preserves one membership row per artifact
    rather than upserting roster facts in place -- consistent with this
    whole migration's "never silently overwrite a prior snapshot" stance
    (see the Phase 5A Torvik-classification addendum for the same
    reasoning applied to a different source).

    `player_id` is nullable: an unresolved or ambiguous roster player
    still gets a row here (`validation_status` records why), because a
    file-level identity failure (team/season) is the only thing that
    should suppress import of an ENTIRE workbook -- one bad row must not
    invalidate the rest (Phase 5B1 instruction).

    Does NOT model "roster status" (active/inactive/redshirt/etc.) -- the
    real `Roster` table carries no such column; inventing one would
    violate "only include fields genuinely supported by the source
    contract." A future source that does carry real status evidence (an
    injury feed, for example) can add it as its own table.

    Explicitly does not assume a player belongs to only one team across
    seasons, or even within one season -- no unique constraint on
    `(player_id, season_id)` alone exists here.
    """

    __tablename__ = "roster_memberships"

    id: Mapped[int] = mapped_column(_BigIntegerPk, primary_key=True, autoincrement=True)

    player_id: Mapped[str | None] = mapped_column(ForeignKey("players.player_id"), nullable=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.team_id"), nullable=False)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.season_id"), nullable=False)
    source_artifact_id: Mapped[int] = mapped_column(ForeignKey("source_artifacts.id"), nullable=False)

    player_key: Mapped[str] = mapped_column(String(200), nullable=False)
    source_player_name: Mapped[str] = mapped_column(String(200), nullable=False)

    jersey_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    class_year: Mapped[str | None] = mapped_column(String(10), nullable=True)
    position: Mapped[str | None] = mapped_column(String(10), nullable=True)
    height: Mapped[str | None] = mapped_column(String(10), nullable=True)  # raw "6-8"-style string, never parsed to inches
    weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    hometown: Mapped[str | None] = mapped_column(String(200), nullable=True)
    high_school: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Raw text, NOT numeric -- confirmed this batch (real Michigan State
    #: fixture) that this column is not always a clean number, e.g.
    #: `"34 (2023)"` (rank + recruiting class year in parens). Storing as
    #: a float would silently fail or require parsing out the rank, which
    #: is a calculation this table does not make.
    rsci_top_100: Mapped[str | None] = mapped_column(String(30), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)  # raw SR summary string, e.g. "16.1 Pts, 9.8 Reb, 3.0 Ast" -- not recomputed here

    validation_status: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("player_key", "team_id", "season_id", "source_artifact_id", name="uq_roster_memberships_natural_key"),
        CheckConstraint(sql_check_in("validation_status", VALIDATION_STATUSES), name="validation_status"),
        Index("ix_roster_memberships_player_id", "player_id"),
        Index("ix_roster_memberships_team_id_season_id", "team_id", "season_id"),
        Index("ix_roster_memberships_source_artifact_id", "source_artifact_id"),
    )


class PlayerStatSnapshot(Base):
    """
    One row per (player, team, season, stat_table, source_artifact) --
    i.e. one real player row from one of the five AllStats stat tables of
    one imported workbook. See module docstring for the full grain
    rationale and the box-score-vs-advanced sparse-column design.

    `player_id` nullable for the same reason as `RosterMembership`.
    """

    __tablename__ = "player_stat_snapshots"

    id: Mapped[int] = mapped_column(_BigIntegerPk, primary_key=True, autoincrement=True)

    player_id: Mapped[str | None] = mapped_column(ForeignKey("players.player_id"), nullable=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.team_id"), nullable=False)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.season_id"), nullable=False)
    source_artifact_id: Mapped[int] = mapped_column(ForeignKey("source_artifacts.id"), nullable=False)

    stat_table: Mapped[str] = mapped_column(String(20), nullable=False)
    player_key: Mapped[str] = mapped_column(String(200), nullable=False)
    source_player_name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_rank: Mapped[float | None] = mapped_column(Float, nullable=True)  # "Rk" -- float since some tables carry it as float64, others int64
    source_position: Mapped[str | None] = mapped_column(String(10), nullable=True)  # "Pos" as listed in THIS stat table
    awards_raw: Mapped[str | None] = mapped_column(String(200), nullable=True)  # "Awards" -- raw text, never parsed/interpreted

    games_played: Mapped[int | None] = mapped_column(Integer, nullable=True)
    games_started: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minutes_played: Mapped[float | None] = mapped_column(Float, nullable=True)
    fg_made: Mapped[float | None] = mapped_column(Float, nullable=True)
    fg_attempted: Mapped[float | None] = mapped_column(Float, nullable=True)
    fg_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    three_p_made: Mapped[float | None] = mapped_column(Float, nullable=True)
    three_p_attempted: Mapped[float | None] = mapped_column(Float, nullable=True)
    three_p_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    two_p_made: Mapped[float | None] = mapped_column(Float, nullable=True)
    two_p_attempted: Mapped[float | None] = mapped_column(Float, nullable=True)
    two_p_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    efg_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    ft_made: Mapped[float | None] = mapped_column(Float, nullable=True)
    ft_attempted: Mapped[float | None] = mapped_column(Float, nullable=True)
    ft_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    orb: Mapped[float | None] = mapped_column(Float, nullable=True)
    drb: Mapped[float | None] = mapped_column(Float, nullable=True)
    trb: Mapped[float | None] = mapped_column(Float, nullable=True)
    ast: Mapped[float | None] = mapped_column(Float, nullable=True)
    stl: Mapped[float | None] = mapped_column(Float, nullable=True)
    blk: Mapped[float | None] = mapped_column(Float, nullable=True)
    tov: Mapped[float | None] = mapped_column(Float, nullable=True)
    pf: Mapped[float | None] = mapped_column(Float, nullable=True)
    pts: Mapped[float | None] = mapped_column(Float, nullable=True)
    off_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    def_rating: Mapped[float | None] = mapped_column(Float, nullable=True)

    per: Mapped[float | None] = mapped_column(Float, nullable=True)
    ts_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    three_par: Mapped[float | None] = mapped_column(Float, nullable=True)
    ftr: Mapped[float | None] = mapped_column(Float, nullable=True)
    pprod: Mapped[float | None] = mapped_column(Float, nullable=True)
    orb_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    drb_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    trb_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    ast_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    stl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    blk_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    tov_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    usg_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    ows: Mapped[float | None] = mapped_column(Float, nullable=True)
    dws: Mapped[float | None] = mapped_column(Float, nullable=True)
    ws: Mapped[float | None] = mapped_column(Float, nullable=True)
    ws_per_40: Mapped[float | None] = mapped_column(Float, nullable=True)
    obpm: Mapped[float | None] = mapped_column(Float, nullable=True)
    dbpm: Mapped[float | None] = mapped_column(Float, nullable=True)
    bpm: Mapped[float | None] = mapped_column(Float, nullable=True)

    validation_status: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("player_key", "team_id", "season_id", "stat_table", "source_artifact_id", name="uq_player_stat_snapshots_natural_key"),
        CheckConstraint(sql_check_in("stat_table", ALLSTATS_STAT_TABLES), name="stat_table"),
        CheckConstraint(sql_check_in("validation_status", VALIDATION_STATUSES), name="validation_status"),
        Index("ix_player_stat_snapshots_player_id", "player_id"),
        Index("ix_player_stat_snapshots_team_id_season_id", "team_id", "season_id"),
        Index("ix_player_stat_snapshots_source_artifact_id", "source_artifact_id"),
    )


class TeamStatSnapshot(Base):
    """
    One row per (team, season, stat_table, source_artifact) -- the
    confirmed real "Team Totals" aggregate row Sports-Reference appends to
    the `PerGame`/`Totals`/`Advanced` tables (never `Per40`/`Per100`,
    confirmed this batch). Built only because this evidence genuinely
    exists in the source (Phase 5B1 instruction: "only if justified by
    actual source-level team evidence") -- never synthesized by summing
    player rows here (that would be a calculation, not raw evidence).

    Shares the same box-score/advanced sparse-column design as
    `PlayerStatSnapshot`, minus every player-identity field.
    """

    __tablename__ = "team_stat_snapshots"

    id: Mapped[int] = mapped_column(_BigIntegerPk, primary_key=True, autoincrement=True)

    team_id: Mapped[int] = mapped_column(ForeignKey("teams.team_id"), nullable=False)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.season_id"), nullable=False)
    source_artifact_id: Mapped[int] = mapped_column(ForeignKey("source_artifacts.id"), nullable=False)

    stat_table: Mapped[str] = mapped_column(String(20), nullable=False)
    source_row_label: Mapped[str] = mapped_column(String(50), nullable=False)  # raw "Player" value, e.g. "Team Totals"

    games_played: Mapped[int | None] = mapped_column(Integer, nullable=True)
    games_started: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minutes_played: Mapped[float | None] = mapped_column(Float, nullable=True)
    fg_made: Mapped[float | None] = mapped_column(Float, nullable=True)
    fg_attempted: Mapped[float | None] = mapped_column(Float, nullable=True)
    fg_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    three_p_made: Mapped[float | None] = mapped_column(Float, nullable=True)
    three_p_attempted: Mapped[float | None] = mapped_column(Float, nullable=True)
    three_p_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    two_p_made: Mapped[float | None] = mapped_column(Float, nullable=True)
    two_p_attempted: Mapped[float | None] = mapped_column(Float, nullable=True)
    two_p_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    efg_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    ft_made: Mapped[float | None] = mapped_column(Float, nullable=True)
    ft_attempted: Mapped[float | None] = mapped_column(Float, nullable=True)
    ft_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    orb: Mapped[float | None] = mapped_column(Float, nullable=True)
    drb: Mapped[float | None] = mapped_column(Float, nullable=True)
    trb: Mapped[float | None] = mapped_column(Float, nullable=True)
    ast: Mapped[float | None] = mapped_column(Float, nullable=True)
    stl: Mapped[float | None] = mapped_column(Float, nullable=True)
    blk: Mapped[float | None] = mapped_column(Float, nullable=True)
    tov: Mapped[float | None] = mapped_column(Float, nullable=True)
    pf: Mapped[float | None] = mapped_column(Float, nullable=True)
    pts: Mapped[float | None] = mapped_column(Float, nullable=True)
    off_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    def_rating: Mapped[float | None] = mapped_column(Float, nullable=True)

    per: Mapped[float | None] = mapped_column(Float, nullable=True)
    ts_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    three_par: Mapped[float | None] = mapped_column(Float, nullable=True)
    ftr: Mapped[float | None] = mapped_column(Float, nullable=True)
    pprod: Mapped[float | None] = mapped_column(Float, nullable=True)
    orb_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    drb_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    trb_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    ast_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    stl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    blk_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    tov_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    usg_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    ows: Mapped[float | None] = mapped_column(Float, nullable=True)
    dws: Mapped[float | None] = mapped_column(Float, nullable=True)
    ws: Mapped[float | None] = mapped_column(Float, nullable=True)
    ws_per_40: Mapped[float | None] = mapped_column(Float, nullable=True)
    obpm: Mapped[float | None] = mapped_column(Float, nullable=True)
    dbpm: Mapped[float | None] = mapped_column(Float, nullable=True)
    bpm: Mapped[float | None] = mapped_column(Float, nullable=True)

    validation_status: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("team_id", "season_id", "stat_table", "source_artifact_id", name="uq_team_stat_snapshots_natural_key"),
        CheckConstraint(sql_check_in("stat_table", ALLSTATS_STAT_TABLES), name="stat_table"),
        CheckConstraint(sql_check_in("validation_status", VALIDATION_STATUSES), name="validation_status"),
        Index("ix_team_stat_snapshots_team_id_season_id", "team_id", "season_id"),
        Index("ix_team_stat_snapshots_source_artifact_id", "source_artifact_id"),
    )
