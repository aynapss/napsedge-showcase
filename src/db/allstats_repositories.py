"""
Typed, idempotent repositories for the Phase 5B1 AllStats source-evidence
tables. Same discipline as `src.db.repositories` (Phase 5A): idempotent
for identical repeated inputs, reject conflicting immutable fields via
`IdentityConflictError`, never commit internally, never parse Excel,
never touch the filesystem or network, never fuzzy-match, never
recompute an analytical value.

**Mutability rule for all three repositories below**: every raw source
value (jersey number, height, weight, every stat column, ...) is
**immutable** once a row exists for a given natural key -- the natural
key already includes `source_artifact_id`, so a raw value differing while
everything else in the key matches means the same physical source row is
being re-imported with different content, which is a genuine conflict to
surface, not a routine update. The two exceptions are `player_id` and
`validation_status`, which are **mutable together** -- an initially-
unresolved row (`player_id=None`, `validation_status="unresolved"`) can
legitimately be refined once identity resolution improves (e.g. a
reviewed alias is added in a later batch) without that being a conflict,
matching "approved mutable provenance updates must follow Phase 5A
repository policy."
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from src.db.allstats_models import PlayerStatSnapshot, RosterMembership, TeamStatSnapshot
from src.exceptions import IdentityConflictError

#: Every raw-value field name shared by PlayerStatSnapshot/TeamStatSnapshot
#: (everything except identity/key/mutable-status fields) -- used to
#: detect a genuine content conflict on repeat upsert of the same natural key.
_STAT_VALUE_FIELDS = (
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
)


class RosterMembershipRepository:
    def __init__(self, session: Session):
        self._session = session

    def upsert(
        self,
        *,
        team_id: int,
        season_id: int,
        source_artifact_id: int,
        player_key: str,
        source_player_name: str,
        validation_status: str,
        player_id: str | None = None,
        jersey_number: int | None = None,
        class_year: str | None = None,
        position: str | None = None,
        height: str | None = None,
        weight: float | None = None,
        hometown: str | None = None,
        high_school: str | None = None,
        rsci_top_100: str | None = None,
        summary: str | None = None,
    ) -> RosterMembership:
        existing = (
            self._session.query(RosterMembership)
            .filter(
                RosterMembership.player_key == player_key,
                RosterMembership.team_id == team_id,
                RosterMembership.season_id == season_id,
                RosterMembership.source_artifact_id == source_artifact_id,
            )
            .one_or_none()
        )
        incoming_immutable = dict(
            source_player_name=source_player_name, jersey_number=jersey_number, class_year=class_year,
            position=position, height=height, weight=weight, hometown=hometown, high_school=high_school,
            rsci_top_100=rsci_top_100, summary=summary,
        )
        if existing is None:
            row = RosterMembership(
                team_id=team_id, season_id=season_id, source_artifact_id=source_artifact_id,
                player_key=player_key, player_id=player_id, validation_status=validation_status,
                **incoming_immutable,
            )
            self._session.add(row)
            self._session.flush()
            return row

        for field_name, new_value in incoming_immutable.items():
            if getattr(existing, field_name) != new_value:
                raise IdentityConflictError(
                    f"roster_membership ({player_key!r}, team_id={team_id}, season_id={season_id}, "
                    f"source_artifact_id={source_artifact_id}) already has {field_name}={getattr(existing, field_name)!r}, got {new_value!r}"
                )
        existing.player_id = player_id
        existing.validation_status = validation_status
        self._session.flush()
        return existing


class PlayerStatSnapshotRepository:
    def __init__(self, session: Session):
        self._session = session

    def list_for_player(
        self, player_id: str, *, season_id: int | None = None, stat_table: str | None = None
    ) -> list[PlayerStatSnapshot]:
        """
        Read-only (Phase 6, Slice 4A): every persisted stat-snapshot row
        for one resolved player_id, optionally narrowed to one season
        and/or one stat_table. Always bounded by the player_id filter --
        never an unbounded cross-player scan. Deterministically ordered
        (season_id, stat_table, source_artifact_id, id) so a caller gets
        the same sequence on every call regardless of insertion order.
        Never mutates.
        """
        query = self._session.query(PlayerStatSnapshot).filter(PlayerStatSnapshot.player_id == player_id)
        if season_id is not None:
            query = query.filter(PlayerStatSnapshot.season_id == season_id)
        if stat_table is not None:
            query = query.filter(PlayerStatSnapshot.stat_table == stat_table)
        return query.order_by(
            PlayerStatSnapshot.season_id,
            PlayerStatSnapshot.stat_table,
            PlayerStatSnapshot.source_artifact_id,
            PlayerStatSnapshot.id,
        ).all()

    def upsert(
        self,
        *,
        team_id: int,
        season_id: int,
        source_artifact_id: int,
        stat_table: str,
        player_key: str,
        source_player_name: str,
        validation_status: str,
        player_id: str | None = None,
        source_rank: float | None = None,
        source_position: str | None = None,
        awards_raw: str | None = None,
        **stat_values,
    ) -> PlayerStatSnapshot:
        unknown = set(stat_values) - set(_STAT_VALUE_FIELDS)
        if unknown:
            raise ValueError(f"unknown stat field(s) for PlayerStatSnapshot: {sorted(unknown)}")

        existing = (
            self._session.query(PlayerStatSnapshot)
            .filter(
                PlayerStatSnapshot.player_key == player_key,
                PlayerStatSnapshot.team_id == team_id,
                PlayerStatSnapshot.season_id == season_id,
                PlayerStatSnapshot.stat_table == stat_table,
                PlayerStatSnapshot.source_artifact_id == source_artifact_id,
            )
            .one_or_none()
        )
        incoming_immutable = dict(
            source_player_name=source_player_name, source_rank=source_rank,
            source_position=source_position, awards_raw=awards_raw, **stat_values,
        )
        if existing is None:
            row = PlayerStatSnapshot(
                team_id=team_id, season_id=season_id, source_artifact_id=source_artifact_id,
                stat_table=stat_table, player_key=player_key, player_id=player_id,
                validation_status=validation_status, **incoming_immutable,
            )
            self._session.add(row)
            self._session.flush()
            return row

        for field_name, new_value in incoming_immutable.items():
            if getattr(existing, field_name) != new_value:
                raise IdentityConflictError(
                    f"player_stat_snapshot ({player_key!r}, team_id={team_id}, season_id={season_id}, "
                    f"stat_table={stat_table!r}, source_artifact_id={source_artifact_id}) already has "
                    f"{field_name}={getattr(existing, field_name)!r}, got {new_value!r}"
                )
        existing.player_id = player_id
        existing.validation_status = validation_status
        self._session.flush()
        return existing


class TeamStatSnapshotRepository:
    def __init__(self, session: Session):
        self._session = session

    def upsert(
        self,
        *,
        team_id: int,
        season_id: int,
        source_artifact_id: int,
        stat_table: str,
        source_row_label: str,
        validation_status: str,
        **stat_values,
    ) -> TeamStatSnapshot:
        unknown = set(stat_values) - set(_STAT_VALUE_FIELDS)
        if unknown:
            raise ValueError(f"unknown stat field(s) for TeamStatSnapshot: {sorted(unknown)}")

        existing = (
            self._session.query(TeamStatSnapshot)
            .filter(
                TeamStatSnapshot.team_id == team_id,
                TeamStatSnapshot.season_id == season_id,
                TeamStatSnapshot.stat_table == stat_table,
                TeamStatSnapshot.source_artifact_id == source_artifact_id,
            )
            .one_or_none()
        )
        incoming_immutable = dict(source_row_label=source_row_label, **stat_values)
        if existing is None:
            row = TeamStatSnapshot(
                team_id=team_id, season_id=season_id, source_artifact_id=source_artifact_id,
                stat_table=stat_table, validation_status=validation_status, **incoming_immutable,
            )
            self._session.add(row)
            self._session.flush()
            return row

        for field_name, new_value in incoming_immutable.items():
            if getattr(existing, field_name) != new_value:
                raise IdentityConflictError(
                    f"team_stat_snapshot (team_id={team_id}, season_id={season_id}, stat_table={stat_table!r}, "
                    f"source_artifact_id={source_artifact_id}) already has {field_name}={getattr(existing, field_name)!r}, got {new_value!r}"
                )
        existing.validation_status = validation_status
        self._session.flush()
        return existing
