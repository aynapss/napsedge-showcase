"""
Phase 5B3 foundation ORM models: raw ESPN player-stat evidence and
derived NCAA-cohort benchmark reference snapshots.

GRAIN, DOCUMENTED BEFORE ANY TABLE (per Phase 5B3 instruction)

Three real local files exist under `TeamDump/ESPN` (confirmed by reading
all three this batch): `espn_full_player_stats.csv` (3,121 rows, 21
columns, RAW ESPN player evidence -- one row per player's season-to-date
aggregate stat line, keyed by a short team abbreviation code like `"BYU"`
-- no id column, no season column, no timestamp column, confirmed
against the real fixture), `espn_full_player_stats_enriched.csv` (same
3,121 players, 130 columns -- confirmed, by exact float-equality diff in
an earlier batch, to be a 100%-reproducible DERIVED output of the
already-migrated `src.transformations.player_benchmarks_transformation.
add_espn_benchmarks` applied to the raw file, never independent
evidence), and `espn_ncaa_benchmarks.xlsx` (21 rows, `Metric`/`NCAA_Mean`/
`NCAA_Std` -- the cohort-level statistics `add_espn_benchmarks` computes
from the same raw file, also derived, never independent).

WHAT IS PERSISTED AND WHY (raw vs. derived boundary, kept structurally
separate per instruction: "do not store calculated benchmarks as if they
were raw ESPN observations")

1. **`espn_source_observations`** -- one row per real raw CSV row (the
   first file only). Genuine, non-reproducible, externally-collected
   evidence -- exactly the kind of thing this migration's `source_artifacts`
   provenance model exists to preserve.
2. **`espn_benchmark_snapshots`** (+ its `espn_benchmark_values` line
   items) -- the *cohort-level* `NCAA_Mean`/`NCAA_Std` reference constants
   (the third file). Small (21 rows), well-bounded, genuinely useful as
   an auditable record of "which cohort baseline was in effect for a
   given pipeline run" -- unlike the 130-column *per-player* enriched
   file, which is deliberately **not** given its own table this batch: it
   is 100%-deterministically re-derivable from `espn_source_observations`
   via the already-tested `add_espn_benchmarks` function, so persisting
   it as well would (a) violate "do not store calculated benchmarks as if
   they were raw observations" in the opposite direction if ever
   conflated with the raw table, and (b) duplicate a value this batch's
   own active pipeline has no confirmed consumer for today ("only persist
   layers actually required by the active pipeline and justified by the
   files"). See `docs/database_schema.md`'s Phase 5B3 section for the
   full architecture-decision writeup.

TEAM-ABBREVIATION RESOLUTION IS ADAPTER-LOCAL, NEVER A SCHEMA CONCERN

`espn_source_observations.team_id` is nullable and populated by
`src.pipeline.espn_import_adapter` via
`src.identity.espn_team_abbreviation_aliases.resolve_espn_team_abbreviation`
(baseline exact match, then this batch's own reviewed crosswalk) -- the
schema itself has no opinion on how that resolution happened;
`team_resolution_method` records it per row for audit.

NO CALCULATED VALUE ORIGINATES HERE

Every column on `espn_source_observations` is a raw source value exactly
as ESPN's own page reports it; every column on `espn_benchmark_values` is
a value already computed by the already-migrated, already-tested
`add_espn_benchmarks` (re-derived and diffed byte-for-byte against the
real cached file in an earlier batch, not recomputed by this module).
Neither table computes anything new.
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
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.enums import (
    ESPN_BENCHMARK_METRIC_NAMES,
    ESPN_PLAYER_MATCH_METHODS,
    ESPN_TEAM_RESOLUTION_METHODS,
    VALIDATION_STATUSES,
    sql_check_in,
)
from src.db.models import _BigIntegerPk


class EspnSourceObservation(Base):
    """
    One row per real `espn_full_player_stats.csv` row -- a player's raw,
    season-to-date aggregate stat line as ESPN itself reports it. No
    season column exists in the real source, so `season_id` must be
    supplied explicitly by the caller (matching
    `run_espn_player_ingestion_from_csv`'s own already-established
    contract) -- never inferred.
    """

    __tablename__ = "espn_source_observations"

    id: Mapped[int] = mapped_column(_BigIntegerPk, primary_key=True, autoincrement=True)

    source_artifact_id: Mapped[int] = mapped_column(ForeignKey("source_artifacts.id"), nullable=False)
    source_row_position: Mapped[int] = mapped_column(Integer, nullable=False)
    pipeline_run_id: Mapped[str | None] = mapped_column(ForeignKey("pipeline_runs.run_id"), nullable=True)

    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.season_id"), nullable=False)

    raw_player_name: Mapped[str] = mapped_column(String(200), nullable=False)
    raw_team_code: Mapped[str] = mapped_column(String(20), nullable=False)
    espn_athlete_id: Mapped[str | None] = mapped_column(String(50), nullable=True)

    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.team_id"), nullable=True)
    team_resolution_method: Mapped[str | None] = mapped_column(String(30), nullable=True)
    player_id: Mapped[str | None] = mapped_column(ForeignKey("players.player_id"), nullable=True)
    player_match_method: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # Raw stat columns -- exactly as the real CSV reports them, no calculation.
    rk: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pos: Mapped[str | None] = mapped_column(String(10), nullable=True)
    gp: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_per_game: Mapped[float | None] = mapped_column(Float, nullable=True)
    pts: Mapped[float | None] = mapped_column(Float, nullable=True)
    fgm: Mapped[float | None] = mapped_column(Float, nullable=True)
    fga: Mapped[float | None] = mapped_column(Float, nullable=True)
    fg_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    three_pm: Mapped[float | None] = mapped_column(Float, nullable=True)
    three_pa: Mapped[float | None] = mapped_column(Float, nullable=True)
    three_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    ftm: Mapped[float | None] = mapped_column(Float, nullable=True)
    fta: Mapped[float | None] = mapped_column(Float, nullable=True)
    ft_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    reb: Mapped[float | None] = mapped_column(Float, nullable=True)
    ast: Mapped[float | None] = mapped_column(Float, nullable=True)
    stl: Mapped[float | None] = mapped_column(Float, nullable=True)
    blk: Mapped[float | None] = mapped_column(Float, nullable=True)
    turnovers: Mapped[float | None] = mapped_column(Float, nullable=True)  # source column "TO" -- not a Python keyword

    validation_status: Mapped[str] = mapped_column(String(20), nullable=False)
    exclusion_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("source_artifact_id", "source_row_position", name="uq_espn_source_observations_natural_key"),
        CheckConstraint(sql_check_in("validation_status", VALIDATION_STATUSES), name="validation_status"),
        CheckConstraint(
            sql_check_in("team_resolution_method", ESPN_TEAM_RESOLUTION_METHODS) + " OR team_resolution_method IS NULL",
            name="team_resolution_method",
        ),
        CheckConstraint(
            sql_check_in("player_match_method", ESPN_PLAYER_MATCH_METHODS) + " OR player_match_method IS NULL",
            name="player_match_method",
        ),
        Index("ix_espn_source_observations_team_id", "team_id"),
        Index("ix_espn_source_observations_player_id", "player_id"),
        Index("ix_espn_source_observations_season_id", "season_id"),
        Index("ix_espn_source_observations_source_artifact_id", "source_artifact_id"),
    )


class EspnBenchmarkSnapshot(Base):
    """
    One row per real `espn_ncaa_benchmarks.xlsx` artifact -- metadata for
    one cohort-benchmark computation run. Not team/player/season-scoped
    (`season_id` nullable, honestly reflecting the real file's own lack of
    any date/season scoping, per `docs/local_data_inventory.md` section
    9's `current_snapshot_only` classification).
    """

    __tablename__ = "espn_benchmark_snapshots"

    id: Mapped[int] = mapped_column(_BigIntegerPk, primary_key=True, autoincrement=True)
    source_artifact_id: Mapped[int] = mapped_column(ForeignKey("source_artifacts.id"), nullable=False)
    pipeline_run_id: Mapped[str | None] = mapped_column(ForeignKey("pipeline_runs.run_id"), nullable=True)
    season_id: Mapped[int | None] = mapped_column(ForeignKey("seasons.season_id"), nullable=True)

    #: Which version of the (already-migrated, already-tested)
    #: transformation produced these values -- distinct from
    #: `model_versions`/`model_version_id` (an *analytical model*
    #: configuration identity), never conflated with it.
    transformation_version: Mapped[str] = mapped_column(String(50), nullable=False)

    validation_status: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("source_artifact_id", name="uq_espn_benchmark_snapshots_source_artifact_id"),
        CheckConstraint(sql_check_in("validation_status", VALIDATION_STATUSES), name="validation_status"),
        Index("ix_espn_benchmark_snapshots_season_id", "season_id"),
    )


class EspnBenchmarkValue(Base):
    """
    One `Metric`/`NCAA_Mean`/`NCAA_Std` row within one snapshot -- exactly
    as the real workbook reports it, no recomputation. `metric_name` is a
    controlled vocabulary (`ESPN_BENCHMARK_METRIC_NAMES`, the exact 21
    real values confirmed in the source file), not an arbitrary free-text
    column -- this is the "constrained benchmark-definition" contract
    substituting for a separate lookup table, since the real vocabulary
    is small, fixed, and confirmed stable (never dynamic per real
    evidence).
    """

    __tablename__ = "espn_benchmark_values"

    id: Mapped[int] = mapped_column(_BigIntegerPk, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("espn_benchmark_snapshots.id"), nullable=False)
    metric_name: Mapped[str] = mapped_column(String(50), nullable=False)
    ncaa_mean: Mapped[float] = mapped_column(Float, nullable=False)
    ncaa_std: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("snapshot_id", "metric_name", name="uq_espn_benchmark_values_natural_key"),
        CheckConstraint(sql_check_in("metric_name", ESPN_BENCHMARK_METRIC_NAMES), name="metric_name"),
        Index("ix_espn_benchmark_values_snapshot_id", "snapshot_id"),
    )
