"""
Typed, idempotent repository for the Phase 5B4 player-game-log table.
Same discipline as every earlier Phase 5 repository: idempotent for
identical repeated inputs, reject conflicting immutable fields via
`IdentityConflictError`, never commit internally, never parse a file,
never touch the filesystem or network, never fuzzy-match.

**Mutability rule**: every raw source value is immutable once a row
exists for its natural key. The exceptions, mutable together, are
`player_id`/`player_match_method`/`opponent_team_id`/`game_id`/
`validation_status`/`exclusion_reason` (an initially-unresolved row can
be refined once a player/opponent/game resolves, without that being a
conflict).
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from src.db.player_game_log_models import PlayerGameLogObservation
from src.exceptions import IdentityConflictError

_OBSERVATION_STAT_FIELDS = (
    "minutes_played", "points", "rebounds", "assists", "steals", "blocks",
    "turnovers", "field_goals_made", "field_goals_attempted",
)

_OBSERVATION_IMMUTABLE_FIELDS = (
    "raw_player_name", "raw_opponent_name", "game_date", "raw_result_marker",
    "team_score", "opponent_score", "overtime_marker",
) + _OBSERVATION_STAT_FIELDS


class PlayerGameLogObservationRepository:
    def __init__(self, session: Session):
        self._session = session

    def upsert(
        self,
        *,
        source_artifact_id: int,
        source_row_position: int,
        season_id: int,
        team_id: int,
        raw_player_name: str,
        raw_opponent_name: str,
        participation_status: str,
        validation_status: str,
        game_date=None,
        raw_result_marker: str | None = None,
        team_score: int | None = None,
        opponent_score: int | None = None,
        overtime_marker: str | None = None,
        player_id: str | None = None,
        player_match_method: str | None = None,
        opponent_team_id: int | None = None,
        game_id: int | None = None,
        exclusion_reason: str | None = None,
        pipeline_run_id: str | None = None,
        **stat_values,
    ) -> PlayerGameLogObservation:
        unknown = set(stat_values) - set(_OBSERVATION_STAT_FIELDS)
        if unknown:
            raise ValueError(f"unknown stat field(s) for PlayerGameLogObservation: {sorted(unknown)}")

        existing = (
            self._session.query(PlayerGameLogObservation)
            .filter(
                PlayerGameLogObservation.source_artifact_id == source_artifact_id,
                PlayerGameLogObservation.source_row_position == source_row_position,
            )
            .one_or_none()
        )
        incoming_immutable = dict(
            raw_player_name=raw_player_name, raw_opponent_name=raw_opponent_name, game_date=game_date,
            raw_result_marker=raw_result_marker, team_score=team_score, opponent_score=opponent_score,
            overtime_marker=overtime_marker,
            **{field: stat_values.get(field) for field in _OBSERVATION_STAT_FIELDS},
        )

        if existing is None:
            row = PlayerGameLogObservation(
                source_artifact_id=source_artifact_id, source_row_position=source_row_position,
                season_id=season_id, team_id=team_id, player_id=player_id, player_match_method=player_match_method,
                opponent_team_id=opponent_team_id, game_id=game_id, participation_status=participation_status,
                validation_status=validation_status, exclusion_reason=exclusion_reason,
                pipeline_run_id=pipeline_run_id, **incoming_immutable,
            )
            self._session.add(row)
            self._session.flush()
            return row

        for field_name, new_value in incoming_immutable.items():
            if getattr(existing, field_name) != new_value:
                raise IdentityConflictError(
                    f"player_game_log_observation (source_artifact_id={source_artifact_id}, "
                    f"source_row_position={source_row_position}) already has {field_name}="
                    f"{getattr(existing, field_name)!r}, got {new_value!r}"
                )
        existing.player_id = player_id
        existing.player_match_method = player_match_method
        existing.opponent_team_id = opponent_team_id
        existing.game_id = game_id
        existing.participation_status = participation_status
        existing.validation_status = validation_status
        existing.exclusion_reason = exclusion_reason
        existing.pipeline_run_id = pipeline_run_id
        self._session.flush()
        return existing
