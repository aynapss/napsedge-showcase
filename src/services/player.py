"""
Read-only Player raw-statistics service (Phase 6, Slice 4A).

Mirrors `src.services.team`'s structure exactly: independent of FastAPI --
takes an already-open `Session` (owned and closed by the caller, never by
this module) and returns plain `@dataclass` results. Never constructs a
`select(...)`/`.query(...)` itself: all persistence access goes through
`src.db.repositories.PlayerRepository` and
`src.db.allstats_repositories.PlayerStatSnapshotRepository`. Performs no
calculation, no OVR/composite scoring, no per-game/per-40 derivation, no
name-based resolution (no authoritative, tested Player name resolver
exists in this codebase today -- `src.identity.player_identity.build_player_identity`
is explicitly documented as "a constructor, not a lookup" -- so this
service accepts only an already-known canonical `player_id`).

"Raw statistics" here means `PlayerStatSnapshot` (season-level, AllStats-
sourced) only -- the smallest single authoritative statistics table.
`PlayerGameLogObservation` (per-game box scores, a structurally different
shape: one row per game rather than one row per season/stat_table) and
`RosterMembership` (biographical/roster fields, not statistics) are
deliberately out of scope for this slice; each would need its own typed
contract and its own service operation, not a flattened, misleading
merge into this one.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from src.db.allstats_models import PlayerStatSnapshot
from src.db.allstats_repositories import PlayerStatSnapshotRepository
from src.db.repositories import PlayerRepository
from src.services.contracts import PlayerRawStatisticsResult, PlayerStatRecord

#: Every `PlayerStatSnapshot` column this service exposes -- a direct 1:1
#: mirror, nothing computed, nothing omitted, nothing added.
_STAT_FIELDS = (
    "stat_table", "season_id", "team_id", "source_artifact_id",
    "player_key", "source_player_name", "source_rank", "source_position", "awards_raw",
    "games_played", "games_started", "minutes_played",
    "fg_made", "fg_attempted", "fg_pct",
    "three_p_made", "three_p_attempted", "three_p_pct",
    "two_p_made", "two_p_attempted", "two_p_pct",
    "efg_pct", "ft_made", "ft_attempted", "ft_pct",
    "orb", "drb", "trb", "ast", "stl", "blk", "tov", "pf", "pts",
    "off_rating", "def_rating",
    "per", "ts_pct", "three_par", "ftr", "pprod",
    "orb_pct", "drb_pct", "trb_pct", "ast_pct", "stl_pct", "blk_pct", "tov_pct", "usg_pct",
    "ows", "dws", "ws", "ws_per_40", "obpm", "dbpm", "bpm",
    "validation_status",
)


def _to_stat_record(row: PlayerStatSnapshot) -> PlayerStatRecord:
    return PlayerStatRecord(**{field: getattr(row, field) for field in _STAT_FIELDS})


def get_player_raw_statistics(
    session: Session,
    player_id: str,
    *,
    season_id: int | None = None,
    stat_table: str | None = None,
) -> PlayerRawStatisticsResult:
    """
    Canonical Player lookup by its already-resolved `player_id`, plus every
    persisted `PlayerStatSnapshot` row for that player -- optionally
    narrowed to one `season_id` and/or one `stat_table`
    (`src.db.enums.ALLSTATS_STAT_TABLES`).

    Filter parameter names match the actual database columns
    (`season_id`, `stat_table`) rather than generic placeholders, per this
    batch's own instruction to use the repository's real naming
    convention -- there is no single "source" column on this table (the
    closest concept is `stat_table`, since one workbook import always has
    exactly one `source_artifact_id` per row already).

    Returns `outcome="not_found"` only when `player_id` itself has no
    database row. A known player with zero matching statistics (no rows
    at all, or none matching the given filters) is `outcome="found"` with
    an empty `stats` tuple -- never conflated with an unknown player.
    """
    player_row = PlayerRepository(session).get_by_id(player_id)
    if player_row is None:
        return PlayerRawStatisticsResult(outcome="not_found", detail="no database row for this player_id")

    rows = PlayerStatSnapshotRepository(session).list_for_player(player_id, season_id=season_id, stat_table=stat_table)
    return PlayerRawStatisticsResult(
        outcome="found",
        player_id=player_row.player_id,
        display_name=player_row.display_name,
        team_id=player_row.team_id,
        season_id=player_row.season_id,
        stats=tuple(_to_stat_record(row) for row in rows),
    )
