"""
Typed, idempotent repositories for the Phase 5B2 game-log tables. Same
discipline as `src.db.repositories`/`src.db.allstats_repositories`:
idempotent for identical repeated inputs, reject conflicting immutable
fields via `IdentityConflictError`, never commit internally, never parse
a workbook, never touch the filesystem or network, never fuzzy-match,
never recompute an analytical value.

**Mutability rule**: every raw source value is immutable once a row
exists for its natural key -- the natural key already includes
`source_artifact_id`, so a differing raw value at the same key is a
genuine content conflict. The exceptions, mutable together, are
`opponent_team_id`/`game_id`/`validation_status`/`exclusion_reason` on
`TeamGameLogObservation` (an initially-unresolved row can be refined once
the opponent resolves or a game_id becomes computable, without that
being a conflict) and every field on `GameResult` except its own PK and
`team_a_id`/`team_b_id` (a result is expected to be refined as more
observations arrive -- e.g. single-sided -> reconciled once the second
team's file is imported).
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from src.db.game_log_models import GameResult, TeamGameLogObservation, TeamGameStatLine
from src.exceptions import IdentityConflictError

_STAT_LINE_FIELDS = (
    "off_rating", "def_rating", "net_rating", "pace", "fta_rate", "fg3a_rate", "ts_pct",
    "trb_pct", "ast_pct", "stl_pct", "blk_pct", "efg_pct", "tov_pct", "orb_pct", "ft_rate",
    "opp_efg_pct", "opp_tov_pct", "opp_orb_pct", "opp_ft_rate", "margin",
)

_OBSERVATION_IMMUTABLE_FIELDS = (
    "raw_opponent_name", "game_date", "venue_status", "raw_result_marker",
    "points_scored", "points_allowed", "overtime_marker", "game_type_raw", "schema_variant",
)


class TeamGameLogObservationRepository:
    def __init__(self, session: Session):
        self._session = session

    def upsert(
        self,
        *,
        team_id: int,
        season_id: int,
        source_artifact_id: int,
        source_row_position: int,
        raw_opponent_name: str,
        venue_status: str,
        schema_variant: str,
        validation_status: str,
        opponent_team_id: int | None = None,
        game_id: int | None = None,
        game_date=None,
        scheduled_timestamp=None,
        raw_result_marker: str | None = None,
        points_scored: int | None = None,
        points_allowed: int | None = None,
        overtime_marker: str | None = None,
        game_type_raw: str | None = None,
        exclusion_reason: str | None = None,
        pipeline_run_id: str | None = None,
    ) -> TeamGameLogObservation:
        existing = (
            self._session.query(TeamGameLogObservation)
            .filter(
                TeamGameLogObservation.team_id == team_id,
                TeamGameLogObservation.season_id == season_id,
                TeamGameLogObservation.source_artifact_id == source_artifact_id,
                TeamGameLogObservation.source_row_position == source_row_position,
            )
            .one_or_none()
        )
        incoming_immutable = dict(
            raw_opponent_name=raw_opponent_name, game_date=game_date, venue_status=venue_status,
            raw_result_marker=raw_result_marker, points_scored=points_scored, points_allowed=points_allowed,
            overtime_marker=overtime_marker, game_type_raw=game_type_raw, schema_variant=schema_variant,
        )
        if existing is None:
            row = TeamGameLogObservation(
                team_id=team_id, season_id=season_id, source_artifact_id=source_artifact_id,
                source_row_position=source_row_position, opponent_team_id=opponent_team_id, game_id=game_id,
                scheduled_timestamp=scheduled_timestamp, validation_status=validation_status,
                exclusion_reason=exclusion_reason, pipeline_run_id=pipeline_run_id, **incoming_immutable,
            )
            self._session.add(row)
            self._session.flush()
            return row

        for field_name, new_value in incoming_immutable.items():
            if getattr(existing, field_name) != new_value:
                raise IdentityConflictError(
                    f"team_game_log_observation (team_id={team_id}, season_id={season_id}, "
                    f"source_artifact_id={source_artifact_id}, source_row_position={source_row_position}) "
                    f"already has {field_name}={getattr(existing, field_name)!r}, got {new_value!r}"
                )
        existing.opponent_team_id = opponent_team_id
        existing.game_id = game_id
        existing.validation_status = validation_status
        existing.exclusion_reason = exclusion_reason
        existing.pipeline_run_id = pipeline_run_id
        self._session.flush()
        return existing


class TeamGameStatLineRepository:
    def __init__(self, session: Session):
        self._session = session

    def upsert(self, *, observation_id: int, **stat_values) -> TeamGameStatLine:
        unknown = set(stat_values) - set(_STAT_LINE_FIELDS)
        if unknown:
            raise ValueError(f"unknown stat field(s) for TeamGameStatLine: {sorted(unknown)}")

        existing = (
            self._session.query(TeamGameStatLine)
            .filter(TeamGameStatLine.observation_id == observation_id)
            .one_or_none()
        )
        if existing is None:
            row = TeamGameStatLine(observation_id=observation_id, **stat_values)
            self._session.add(row)
            self._session.flush()
            return row

        for field_name, new_value in stat_values.items():
            if getattr(existing, field_name) != new_value:
                raise IdentityConflictError(
                    f"team_game_stat_line (observation_id={observation_id}) already has "
                    f"{field_name}={getattr(existing, field_name)!r}, got {new_value!r}"
                )
        return existing


class GameResultRepository:
    """
    `game_id` is the PK -- an upsert here always targets an already-known
    canonical (provisional or confirmed) game. Every field except the PK
    and `team_a_id`/`team_b_id` (immutable once set -- a conflicting value
    there would mean two different physical games somehow share a
    `game_id`, an identity-layer bug, never silently absorbed here) is
    mutable, since a result is *expected* to be refined as more
    observations arrive (single-sided -> reconciled, or a later re-import
    resolves what was previously a conflict).
    """

    def __init__(self, session: Session):
        self._session = session

    def upsert(
        self,
        *,
        game_id: int,
        season_id: int,
        team_a_id: int,
        team_b_id: int,
        result_status: str,
        validation_status: str,
        source_count: int,
        team_a_score: int | None = None,
        team_b_score: int | None = None,
        winner_team_id: int | None = None,
        loser_team_id: int | None = None,
        overtime_status: str | None = None,
        team_a_observation_id: int | None = None,
        team_b_observation_id: int | None = None,
        pipeline_run_id: str | None = None,
    ) -> GameResult:
        existing = self._session.get(GameResult, game_id)
        mutable = dict(
            team_a_score=team_a_score, team_b_score=team_b_score, winner_team_id=winner_team_id,
            loser_team_id=loser_team_id, overtime_status=overtime_status, result_status=result_status,
            validation_status=validation_status, source_count=source_count,
            team_a_observation_id=team_a_observation_id, team_b_observation_id=team_b_observation_id,
            pipeline_run_id=pipeline_run_id,
        )
        if existing is None:
            row = GameResult(game_id=game_id, season_id=season_id, team_a_id=team_a_id, team_b_id=team_b_id, **mutable)
            self._session.add(row)
            self._session.flush()
            return row

        for immutable_field, new_value in (("season_id", season_id), ("team_a_id", team_a_id), ("team_b_id", team_b_id)):
            if getattr(existing, immutable_field) != new_value:
                raise IdentityConflictError(
                    f"game_result (game_id={game_id}) already has {immutable_field}={getattr(existing, immutable_field)!r}, got {new_value!r}"
                )
        for field_name, new_value in mutable.items():
            setattr(existing, field_name, new_value)
        self._session.flush()
        return existing
