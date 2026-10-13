"""
Phase 5B4 foundation ORM model: raw, immutable player-perspective
game-log evidence.

GRAIN, DOCUMENTED BEFORE THE TABLE (per Phase 5B4 instruction)

Confirmed by direct inspection of every real local file this batch
(1,561 files under `TeamDump/CBB/<slug>/<player>_<season>_gamelog.csv`,
37,623 total rows): **exactly one real schema, no variant.** Every file
shares the identical 13-column layout: `game_num, date, opponent, result,
mp, pts, trb, ast, stl, blk, tov, fg, fga`. Unlike the Phase 5B2
team-level game log (two real schema variants, a confirmed
trailing-season-total-row defect), this source has **zero** aggregate
rows, **zero** duplicate dates, **zero** null values, and **zero**
`mp=0` rows anywhere in the real corpus -- confirmed by an exhaustive
scan, not sampled. Because there is only one real schema (no sparse,
schema-variant stat group the way `team_game_stat_lines` exists for the
advanced team-level schema), a single wide table is the evidence-
justified design here -- no companion stat-line table, no separate
participation table (see `participation_status` below).

**The file itself carries no player-name column at all** -- identity is
encoded entirely in the filename
(`frank_mitchell_2026_gamelog.csv` -> "Frank Mitchell"), confirmed by
reading every real file's own column set. `game_num` here is the
*player's own* appearance ordinal (game 1 = this player's first game
they actually appeared in), **not** the team's season-wide game number --
confirmed by cross-referencing a bench player's file (4 rows) against
their team's own 34-game season: chronology must always come from the
real `date` column, never `game_num` or row position, exactly the same
lesson already learned for the Phase 5B2 team-level schema.

`result` (e.g. `"W 69-63"`, `"L 75-78 (OT)"`) is parsed into a raw
win/loss marker, this player's own team's score, the opponent's score
(confirmed, by direct cross-reference against the real Phase 5B2
`team_game_log_observations` `points_scored`/`points_allowed` values for
the same real games, to always list this player's own team's score
first), and an optional overtime marker (`OT`/`2OT`/`3OT`, confirmed the
only three real values present in the corpus).

PARTICIPATION: A REAL, HONEST, SINGLE-VALUE STATE TODAY

`participation_status` is `"played"` for every real row today (100% of
37,623 rows have `mp >= 1`) -- this source's own contract simply never
emits a DNP/inactive/suspended row; a player's absence from a given team
game is represented by the *absence* of a row in their own file, never a
zero-minute one. The full controlled vocabulary
(`PLAYER_GAME_PARTICIPATION_STATUSES`) still includes `dnp`/
`did_not_dress`/`not_with_team`/`suspended`/`unknown`, reusing verbatim
the already-established real vocabulary
`src.identity.postgame_player_identity.SR_NON_PLAYER_ROWS` defines for a
structurally different, not-yet-persisted live box-score source -- kept
here for forward compatibility, never fabricated from this batch's own
evidence.

THIS TABLE NEVER FEEDS THE LIVE `_player_recency_factor` PATH

`src/ovr_model.py`'s own `_player_recency_factor` (a real, active
function, confirmed this batch, wired into the live
`generate_player_card` -> `build_full_player_card` path) already reads
these exact same real files directly, computing a recency-momentum
factor from their raw `pts`/`trb`/`ast` columns. **This batch does not
modify `ovr_model.py` in any way** (a protected file) and this table is
a completely separate, additive, read-only-derived snapshot of the same
raw evidence -- it is never read by, and never changes the output of,
`_player_recency_factor` or any other active calculation. See
`docs/migration_state.md`'s Phase 5B4 section for the full numerical-
parity finding.

GAME LINKAGE REUSES THE EXISTING PHASE 5B2 IDENTITY, NEVER A SECOND ONE

`game_id` is computed via the exact same
`src.identity.game_identity.build_provisional_game_identity(...,
neutral_site=True)` call the Phase 5B2 team-level adapter already uses --
same canonical team-id pair, season, and date -- so a player-game row's
computed `game_id` is guaranteed identical to whatever `game_id` the
team-level import already established (or will establish) for the same
real game. No second game-identity algorithm exists anywhere in this
module.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.enums import PLAYER_GAME_PARTICIPATION_STATUSES, VALIDATION_STATUSES, sql_check_in
from src.db.models import _BigIntegerPk


class PlayerGameLogObservation(Base):
    """
    One row per real `<player>_<season>_gamelog.csv` row -- one player's
    own, immutable, as-scraped perspective on one game they actually
    appeared in. `opponent_team_id`/`game_id`/`player_id` are nullable:
    an unresolved opponent, game, or player still produces a row here
    (raw evidence preserved), because a single unresolved row must never
    invalidate the rest of a file.
    """

    __tablename__ = "player_game_log_observations"

    id: Mapped[int] = mapped_column(_BigIntegerPk, primary_key=True, autoincrement=True)

    source_artifact_id: Mapped[int] = mapped_column(ForeignKey("source_artifacts.id"), nullable=False)
    source_row_position: Mapped[int] = mapped_column(Integer, nullable=False)
    pipeline_run_id: Mapped[str | None] = mapped_column(ForeignKey("pipeline_runs.run_id"), nullable=True)

    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.season_id"), nullable=False)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.team_id"), nullable=False)  # perspective team, from the file's own directory

    raw_player_name: Mapped[str] = mapped_column(String(200), nullable=False)  # reconstructed from the filename, immutable
    player_id: Mapped[str | None] = mapped_column(ForeignKey("players.player_id"), nullable=True)
    player_match_method: Mapped[str | None] = mapped_column(String(40), nullable=True)

    raw_opponent_name: Mapped[str] = mapped_column(String(200), nullable=False)
    opponent_team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.team_id"), nullable=True)
    game_id: Mapped[int | None] = mapped_column(ForeignKey("games.game_id"), nullable=True)

    game_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)  # null only for a genuinely unparseable date -- never inferred
    #: Real tipoff time -- always null for this source (no source ever
    #: supplies one). Never derived from `game_date`.
    scheduled_timestamp: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    raw_result_marker: Mapped[str | None] = mapped_column(String(5), nullable=True)  # raw "W"/"L"
    team_score: Mapped[int | None] = mapped_column(Integer, nullable=True)  # this player's own team's score, parsed from `result`
    opponent_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overtime_marker: Mapped[str | None] = mapped_column(String(10), nullable=True)  # raw "OT" | "2OT" | "3OT", parsed from `result`

    participation_status: Mapped[str] = mapped_column(String(20), nullable=False)

    # Raw box-score stat columns -- exactly as the real CSV reports them, no calculation.
    minutes_played: Mapped[int | None] = mapped_column(Integer, nullable=True)
    points: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rebounds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    assists: Mapped[int | None] = mapped_column(Integer, nullable=True)
    steals: Mapped[int | None] = mapped_column(Integer, nullable=True)
    blocks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    turnovers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    field_goals_made: Mapped[int | None] = mapped_column(Integer, nullable=True)
    field_goals_attempted: Mapped[int | None] = mapped_column(Integer, nullable=True)

    validation_status: Mapped[str] = mapped_column(String(20), nullable=False)
    exclusion_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("source_artifact_id", "source_row_position", name="uq_player_game_log_observations_natural_key"),
        CheckConstraint(sql_check_in("participation_status", PLAYER_GAME_PARTICIPATION_STATUSES), name="participation_status"),
        CheckConstraint(sql_check_in("validation_status", VALIDATION_STATUSES), name="validation_status"),
        Index("ix_player_game_log_observations_team_id_season_id", "team_id", "season_id"),
        Index("ix_player_game_log_observations_player_id", "player_id"),
        Index("ix_player_game_log_observations_opponent_team_id", "opponent_team_id"),
        Index("ix_player_game_log_observations_game_id", "game_id"),
        Index("ix_player_game_log_observations_source_artifact_id", "source_artifact_id"),
    )
