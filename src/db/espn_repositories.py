"""
Typed, idempotent repositories for the Phase 5B3 ESPN tables. Same
discipline as `src.db.repositories`/`src.db.game_log_repositories`:
idempotent for identical repeated inputs, reject conflicting immutable
fields via `IdentityConflictError`, never commit internally, never parse
a file, never touch the filesystem or network, never fuzzy-match.

**Mutability rule**: every raw source value is immutable once a row
exists for its natural key. The exceptions, mutable together, are
`team_id`/`team_resolution_method`/`player_id`/`player_match_method`/
`validation_status`/`exclusion_reason` on `EspnSourceObservation` (an
initially-unresolved row can be refined once a reviewed alias/roster link
resolves it, without that being a conflict).
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from src.db.espn_models import EspnBenchmarkSnapshot, EspnBenchmarkValue, EspnSourceObservation
from src.exceptions import IdentityConflictError

_OBSERVATION_STAT_FIELDS = (
    "rk", "pos", "gp", "min_per_game", "pts", "fgm", "fga", "fg_pct",
    "three_pm", "three_pa", "three_pct", "ftm", "fta", "ft_pct",
    "reb", "ast", "stl", "blk", "turnovers",
)

_OBSERVATION_IMMUTABLE_FIELDS = ("raw_player_name", "raw_team_code", "espn_athlete_id") + _OBSERVATION_STAT_FIELDS


class EspnSourceObservationRepository:
    def __init__(self, session: Session):
        self._session = session

    def upsert(
        self,
        *,
        source_artifact_id: int,
        source_row_position: int,
        season_id: int,
        raw_player_name: str,
        raw_team_code: str,
        validation_status: str,
        espn_athlete_id: str | None = None,
        team_id: int | None = None,
        team_resolution_method: str | None = None,
        player_id: str | None = None,
        player_match_method: str | None = None,
        exclusion_reason: str | None = None,
        pipeline_run_id: str | None = None,
        **stat_values,
    ) -> EspnSourceObservation:
        unknown = set(stat_values) - set(_OBSERVATION_STAT_FIELDS)
        if unknown:
            raise ValueError(f"unknown stat field(s) for EspnSourceObservation: {sorted(unknown)}")

        existing = (
            self._session.query(EspnSourceObservation)
            .filter(
                EspnSourceObservation.source_artifact_id == source_artifact_id,
                EspnSourceObservation.source_row_position == source_row_position,
            )
            .one_or_none()
        )
        incoming_immutable = dict(
            raw_player_name=raw_player_name, raw_team_code=raw_team_code, espn_athlete_id=espn_athlete_id,
            **{field: stat_values.get(field) for field in _OBSERVATION_STAT_FIELDS},
        )

        if existing is None:
            row = EspnSourceObservation(
                source_artifact_id=source_artifact_id, source_row_position=source_row_position,
                season_id=season_id, team_id=team_id, team_resolution_method=team_resolution_method,
                player_id=player_id, player_match_method=player_match_method,
                validation_status=validation_status, exclusion_reason=exclusion_reason,
                pipeline_run_id=pipeline_run_id, **incoming_immutable,
            )
            self._session.add(row)
            self._session.flush()
            return row

        for field_name, new_value in incoming_immutable.items():
            if getattr(existing, field_name) != new_value:
                raise IdentityConflictError(
                    f"espn_source_observation (source_artifact_id={source_artifact_id}, "
                    f"source_row_position={source_row_position}) already has {field_name}="
                    f"{getattr(existing, field_name)!r}, got {new_value!r}"
                )
        existing.team_id = team_id
        existing.team_resolution_method = team_resolution_method
        existing.player_id = player_id
        existing.player_match_method = player_match_method
        existing.validation_status = validation_status
        existing.exclusion_reason = exclusion_reason
        existing.pipeline_run_id = pipeline_run_id
        self._session.flush()
        return existing


class EspnBenchmarkSnapshotRepository:
    def __init__(self, session: Session):
        self._session = session

    def upsert(
        self,
        *,
        source_artifact_id: int,
        transformation_version: str,
        validation_status: str,
        season_id: int | None = None,
        pipeline_run_id: str | None = None,
    ) -> EspnBenchmarkSnapshot:
        existing = (
            self._session.query(EspnBenchmarkSnapshot)
            .filter(EspnBenchmarkSnapshot.source_artifact_id == source_artifact_id)
            .one_or_none()
        )
        if existing is None:
            row = EspnBenchmarkSnapshot(
                source_artifact_id=source_artifact_id, transformation_version=transformation_version,
                validation_status=validation_status, season_id=season_id, pipeline_run_id=pipeline_run_id,
            )
            self._session.add(row)
            self._session.flush()
            return row

        if existing.transformation_version != transformation_version:
            raise IdentityConflictError(
                f"espn_benchmark_snapshot (source_artifact_id={source_artifact_id}) already has "
                f"transformation_version={existing.transformation_version!r}, got {transformation_version!r}"
            )
        existing.validation_status = validation_status
        existing.season_id = season_id
        existing.pipeline_run_id = pipeline_run_id
        self._session.flush()
        return existing


class EspnBenchmarkValueRepository:
    def __init__(self, session: Session):
        self._session = session

    def upsert(self, *, snapshot_id: int, metric_name: str, ncaa_mean: float, ncaa_std: float) -> EspnBenchmarkValue:
        existing = (
            self._session.query(EspnBenchmarkValue)
            .filter(EspnBenchmarkValue.snapshot_id == snapshot_id, EspnBenchmarkValue.metric_name == metric_name)
            .one_or_none()
        )
        if existing is None:
            row = EspnBenchmarkValue(snapshot_id=snapshot_id, metric_name=metric_name, ncaa_mean=ncaa_mean, ncaa_std=ncaa_std)
            self._session.add(row)
            self._session.flush()
            return row

        if existing.ncaa_mean != ncaa_mean or existing.ncaa_std != ncaa_std:
            raise IdentityConflictError(
                f"espn_benchmark_value (snapshot_id={snapshot_id}, metric_name={metric_name!r}) already has "
                f"(ncaa_mean={existing.ncaa_mean!r}, ncaa_std={existing.ncaa_std!r}), got "
                f"(ncaa_mean={ncaa_mean!r}, ncaa_std={ncaa_std!r})"
            )
        return existing
