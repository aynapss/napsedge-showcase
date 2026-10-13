"""
Phase 5B2 foundation ORM models: normalized team-perspective game-log
observations and reconciled canonical game results.

Kept in a separate module from `src/db/models.py` (Phase 5A) and
`src/db/allstats_models.py` (Phase 5B1) so the revision boundary stays
legible in the codebase the same way it does in Alembic history -- all
three share one `Base`/`MetaData`.

GRAIN, DOCUMENTED BEFORE ANY TABLE (per Phase 5B2 instruction)

Real local `TeamDump/CBB/<slug>/<slug>_2026_game_log.csv` files (confirmed
by reading St. Bonaventure, Michigan State, Akron/Kent State [a real
mirrored matchup, played three times in the same season], and Arizona
[an advanced-schema fixture] this batch) come in exactly two column
layouts:

- **`basic`** (128 of 144 real teams): `game_num, date, opponent,
  pts_scored, pts_allowed, ORtg, DRtg, net_rtg`. No venue, no explicit
  win/loss marker, no overtime marker. `game_num` counts UP from the
  season opener (game_num=1 is the earliest date). **Every one of these
  128 files ends with exactly one trailing row whose `date` and
  `opponent` are both blank and whose `pts_scored`/`pts_allowed` are
  season-total sums, not a real game's score** -- the confirmed defect
  from the Phase 5A local-data-inventory batch, excluded by this batch's
  parser before any observation, game, result, or stat-line row is ever
  created for it (see `src.pipeline.game_log_import_adapter`'s season-
  total classifier).
- **`advanced`** (16 of 144 real teams): 29 columns, adding explicit
  `location` (blank = home, `"@"` = away, `"N"` = neutral -- a real,
  literal Sports-Reference convention, confirmed against Arizona's real
  file), `game_type` (`"REG (Non-Conf)"`, `"REG (Conf)"`, `"CTOURN"`,
  `"ROUND-64"`, `"ROUND-32"`, ...), `result` (`"W"`/`"L"`, this team's own
  perspective), `overtimes` (blank or `"OT"`), and ~17 rate/percentage/
  possession columns (`pace`, `fta_rate`, `fg3a_rate`, `ts_pct`, ...).
  **`game_num` counts DOWN from the season's last game** (game_num=1 is
  the MOST RECENT date) -- the exact opposite convention from `basic`,
  confirmed by reading Arizona's real file (`game_num=1` -> `2026-03-22`,
  its Round-of-32 loss; `game_num=36` -> `2025-11-03`, its season opener).
  **Neither `game_num` nor row position is ever treated as a proxy for
  chronology anywhere in this batch's own code** -- only the real `date`
  column is. Confirmed **no** season-total-row defect exists in this
  schema (every real row has a real, non-blank date and opponent).

TWO DISTINCT PERSISTENCE LAYERS (per Phase 5B2 objective -- never
collapsed into one uncontrolled table)

1. **`team_game_log_observations`** (+ its 1:1 companion
   `team_game_stat_lines`) -- one row per real source CSV row (one
   team's own, immutable, as-scraped perspective on one game). Two
   different teams' observations of the *same real game* are two
   different rows here, on purpose -- this layer never merges them.
2. **`game_results`** -- one row per canonical `game_id` (reusing the
   existing Phase 5A `games`/`GameIdentity` authority -- **no second game
   identity is invented here**), holding the *reconciled* outcome derived
   from whichever of the two observation rows exist for that `game_id`.

`team_game_log_observations` is keyed by (perspective team, season,
source_artifact, source row position) -- deliberately NOT by
(team, opponent, date) alone, so a later re-scrape of the same file
(a new, immutable `source_artifacts` row per Phase 5A policy) produces
its own additive set of observation rows rather than overwriting
history, exactly mirroring `roster_memberships`'/`player_stat_snapshots`'
own Phase 5B1 precedent.

VENUE: A REAL FOURTH STATE, NEVER INFERRED

`venue_status` is `home`/`away`/`neutral`/**`unknown`** -- `unknown` is
not a placeholder, it is the honest, common case: the `basic` schema (89%
of real teams) supplies no venue evidence at all. Phase 5B2 instruction:
"do not infer venue solely from team ordering or score" -- this column is
only ever set from the `advanced` schema's own explicit `location`
column; every `basic`-schema-derived observation is `unknown`.
Consequently, every provisional `game_id` this batch computes uses
`GameIdentity`'s own neutral-site (order-independent) structural-key mode
regardless of the row's real `venue_status` -- a **key-computation
choice** (it makes the id deterministic and independent of which team's
file is processed first, per instruction), never a factual claim that
the game was actually played at a neutral site. The real, evidenced
venue fact (`home`/`away`/`neutral`/`unknown`) is preserved separately on
the observation row itself and never lost.

NO CALCULATED VALUE

Every column here is either a raw source value (score, venue marker,
rate/percentage stat as Sports-Reference itself reports it) or a
reconciliation-derived *fact* (winner/loser, whether two observations
agree) -- never a projection, probability, spread, total, EV, Kelly
size, or recommendation. `gamelog_margin_std_v15`'s own formula
(`src/transformations/game_projection_transformation.py`) is untouched
by this batch and does not read from these tables.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    CheckConstraint,
    Date,
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
from src.db.enums import (
    GAME_LOG_SCHEMA_VARIANTS,
    GAME_LOG_VENUE_STATUSES,
    GAME_RESULT_STATUSES,
    VALIDATION_STATUSES,
    sql_check_in,
)
from src.db.models import _BigIntegerPk


class TeamGameLogObservation(Base):
    """
    One row per real source CSV row -- one team's own perspective on one
    game. `opponent_team_id`/`game_id` are nullable: an unresolved
    opponent still produces a row here (raw evidence preserved,
    `validation_status="unresolved"`), because a single bad row must
    never invalidate the rest of a file (Phase 5B2 instruction, same
    principle as Phase 5B1's roster rows).
    """

    __tablename__ = "team_game_log_observations"

    id: Mapped[int] = mapped_column(_BigIntegerPk, primary_key=True, autoincrement=True)

    source_artifact_id: Mapped[int] = mapped_column(ForeignKey("source_artifacts.id"), nullable=False)
    source_row_position: Mapped[int] = mapped_column(Integer, nullable=False)
    pipeline_run_id: Mapped[str | None] = mapped_column(ForeignKey("pipeline_runs.run_id"), nullable=True)

    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.season_id"), nullable=False)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.team_id"), nullable=False)  # perspective team

    raw_opponent_name: Mapped[str] = mapped_column(String(200), nullable=False)
    opponent_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.team_id"), nullable=True)
    game_id: Mapped[int | None] = mapped_column(ForeignKey("games.game_id"), nullable=True)

    game_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)  # null only for a genuinely unparseable date -- never inferred
    #: Real tipoff time -- always null in this batch's real data (no
    #: source ever supplies one). Present so a future source that DOES
    #: supply a real scheduled time has somewhere to put it, without ever
    #: being derived from `game_date` here.
    scheduled_timestamp: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    venue_status: Mapped[str] = mapped_column(String(10), nullable=False)  # home | away | neutral | unknown
    raw_result_marker: Mapped[str | None] = mapped_column(String(5), nullable=True)  # raw "W"/"L", advanced schema only
    points_scored: Mapped[int | None] = mapped_column(Integer, nullable=True)
    points_allowed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overtime_marker: Mapped[str | None] = mapped_column(String(10), nullable=True)  # raw "OT" etc., advanced schema only
    game_type_raw: Mapped[str | None] = mapped_column(String(30), nullable=True)  # e.g. "REG (Conf)", "CTOURN" -- advanced schema only

    schema_variant: Mapped[str] = mapped_column(String(10), nullable=False)  # basic | advanced
    validation_status: Mapped[str] = mapped_column(String(20), nullable=False)
    exclusion_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("team_id", "season_id", "source_artifact_id", "source_row_position", name="uq_team_game_log_observations_natural_key"),
        CheckConstraint(sql_check_in("venue_status", GAME_LOG_VENUE_STATUSES), name="venue_status"),
        CheckConstraint(sql_check_in("schema_variant", GAME_LOG_SCHEMA_VARIANTS), name="schema_variant"),
        CheckConstraint(sql_check_in("validation_status", VALIDATION_STATUSES), name="validation_status"),
        Index("ix_team_game_log_observations_team_id_season_id", "team_id", "season_id"),
        Index("ix_team_game_log_observations_opponent_team_id", "opponent_team_id"),
        Index("ix_team_game_log_observations_game_id", "game_id"),
        Index("ix_team_game_log_observations_source_artifact_id", "source_artifact_id"),
    )


class TeamGameStatLine(Base):
    """
    1:1 companion to `TeamGameLogObservation` -- the efficiency/rate
    numbers, kept separate from the identity/linkage/score fields on the
    observation row itself (mirroring `PlayerStatSnapshot`'s own sparse-
    wide, non-EAV design from Phase 5B1). `off_rating`/`def_rating`/
    `net_rating` are populated for both schema variants; every other
    column is populated only for `advanced`-schema-derived observations.
    """

    __tablename__ = "team_game_stat_lines"

    id: Mapped[int] = mapped_column(_BigIntegerPk, primary_key=True, autoincrement=True)
    observation_id: Mapped[int] = mapped_column(ForeignKey("team_game_log_observations.id"), nullable=False)

    off_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    def_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_rating: Mapped[float | None] = mapped_column(Float, nullable=True)

    pace: Mapped[float | None] = mapped_column(Float, nullable=True)
    fta_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    fg3a_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    ts_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    trb_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    ast_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    stl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    blk_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    efg_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    tov_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    orb_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    ft_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    opp_efg_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    opp_tov_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    opp_orb_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    opp_ft_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    margin: Mapped[float | None] = mapped_column(Float, nullable=True)  # raw source column, NOT recomputed from points_scored/points_allowed

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("observation_id", name="uq_team_game_stat_lines_observation_id"),
        Index("ix_team_game_stat_lines_observation_id", "observation_id"),
    )


class GameResult(Base):
    """
    One row per canonical `game_id` (`games.game_id` -- no second game
    identity invented here). `team_a_id`/`team_b_id` mirror the joined
    `games` row's own `home_team_id`/`away_team_id` (already sorted
    min/max for the neutral-site structural-key mode this batch always
    uses -- see module docstring's "VENUE" section).

    `result_status` is the domain-specific reconciliation outcome
    (`reconciled` | `single_sided_provisional` | `conflicting`);
    `validation_status` is the standard cross-table audit vocabulary
    (kept for consistency with every other Phase 5A/5B1/5B2 table) --
    the two serve different purposes and are not redundant: a caller
    filtering broadly across many tables for `validation_status="valid"`
    should not need to know this table's own domain vocabulary too.
    """

    __tablename__ = "game_results"

    game_id: Mapped[int] = mapped_column(ForeignKey("games.game_id"), primary_key=True, autoincrement=False)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.season_id"), nullable=False)
    team_a_id: Mapped[int] = mapped_column(ForeignKey("teams.team_id"), nullable=False)
    team_b_id: Mapped[int] = mapped_column(ForeignKey("teams.team_id"), nullable=False)

    team_a_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    team_b_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    winner_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.team_id"), nullable=True)
    loser_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.team_id"), nullable=True)
    overtime_status: Mapped[str | None] = mapped_column(String(20), nullable=True)  # "regulation" | "overtime" | "unknown"

    result_status: Mapped[str] = mapped_column(String(30), nullable=False)
    validation_status: Mapped[str] = mapped_column(String(20), nullable=False)
    source_count: Mapped[int] = mapped_column(Integer, nullable=False)

    team_a_observation_id: Mapped[int | None] = mapped_column(ForeignKey("team_game_log_observations.id"), nullable=True)
    team_b_observation_id: Mapped[int | None] = mapped_column(ForeignKey("team_game_log_observations.id"), nullable=True)

    pipeline_run_id: Mapped[str | None] = mapped_column(ForeignKey("pipeline_runs.run_id"), nullable=True)
    first_imported_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=lambda: dt.datetime.now(dt.timezone.utc))

    __table_args__ = (
        CheckConstraint("team_a_id <> team_b_id", name="team_a_b_distinct"),
        CheckConstraint(sql_check_in("result_status", GAME_RESULT_STATUSES), name="result_status"),
        CheckConstraint(sql_check_in("validation_status", VALIDATION_STATUSES), name="validation_status"),
        CheckConstraint("source_count >= 1 AND source_count <= 2", name="source_count_range"),
        Index("ix_game_results_season_id", "season_id"),
        Index("ix_game_results_team_a_id", "team_a_id"),
        Index("ix_game_results_team_b_id", "team_b_id"),
    )
