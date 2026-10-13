"""
Typed, idempotent repositories for the Phase 5A foundation tables.

Every `upsert_*` method:
- accepts canonical typed inputs (the IDs are always the already-computed
  values from `src.identity.*` -- no repository here invents a second id
  scheme for anything it touches);
- is idempotent for identical repeated inputs (calling it twice with the
  same values is a no-op the second time, not a duplicate row or an
  error);
- rejects a conflicting *immutable* identity field with a structured
  `IdentityConflictError` rather than silently overwriting it;
- updates only the fields documented as mutable for that table;
- never fuzzy-matches anything (every lookup here is an exact primary-key
  or exact-unique-constraint lookup);
- never recomputes an analytical value (margins, probabilities, ratings,
  etc. -- none of that belongs in this module, see `src.db.models`'s own
  docstring);
- never touches the filesystem or the network.

None of these methods commits. The caller wraps one or more repository
calls in `src.db.session.unit_of_work(...)`, which commits once at the end
of the block or rolls back the whole block on any exception -- including
an `IdentityConflictError` raised partway through.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from src.db.enums import is_valid_pipeline_run_transition
from src.db.models import (
    Conference,
    DataQualityIssue,
    DataSource,
    Game,
    ModelVersion,
    PipelineRun,
    Player,
    PlayerAlias,
    Season,
    SourceArtifact,
    Sportsbook,
    Team,
    TeamAlias,
)
from src.exceptions import IdentityConflictError, ValidationError


def _require_utc(value: dt.datetime | None, *, field_name: str) -> dt.datetime | None:
    """Reject a naive datetime explicitly rather than silently assuming a timezone (Phase 5A: 'use timezone-aware UTC timestamps')."""
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValidationError(f"{field_name} must be timezone-aware (got a naive datetime): {value!r}")
    return value.astimezone(dt.timezone.utc)


def _coerce_date(value: dt.date | str) -> dt.date:
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    if isinstance(value, str):
        return dt.date.fromisoformat(value)
    raise ValidationError(f"game_date must be a date or an ISO-8601 string, got {value!r}")


class ConferenceRepository:
    def __init__(self, session: Session):
        self._session = session

    def upsert(self, *, conference_id: int, canonical_name: str) -> Conference:
        existing = self._session.get(Conference, conference_id)
        if existing is None:
            row = Conference(conference_id=conference_id, canonical_name=canonical_name)
            self._session.add(row)
            self._session.flush()
            return row
        if existing.canonical_name != canonical_name:
            raise IdentityConflictError(
                f"conference_id {conference_id} already exists with canonical_name={existing.canonical_name!r}, got {canonical_name!r}"
            )
        return existing


class TeamRepository:
    def __init__(self, session: Session):
        self._session = session

    def get_by_id(self, team_id: int) -> Team | None:
        """Read-only primary-key lookup (Phase 6, Slice 3) -- returns None rather than raising when no row exists; never mutates."""
        return self._session.get(Team, team_id)

    def upsert(self, *, team_id: int, display_name: str, sr_slug: str, conference_id: int | None = None) -> Team:
        existing = self._session.get(Team, team_id)
        if existing is None:
            row = Team(team_id=team_id, display_name=display_name, sr_slug=sr_slug, conference_id=conference_id)
            self._session.add(row)
            self._session.flush()
            return row
        if existing.sr_slug != sr_slug:
            raise IdentityConflictError(
                f"team_id {team_id} already exists with sr_slug={existing.sr_slug!r}, got {sr_slug!r}"
            )
        existing.display_name = display_name
        existing.conference_id = conference_id
        self._session.flush()
        return existing


class TeamAliasRepository:
    def __init__(self, session: Session):
        self._session = session

    def upsert(self, *, team_id: int, source: str, raw_value: str, normalized_value: str) -> TeamAlias:
        existing = (
            self._session.query(TeamAlias)
            .filter(TeamAlias.source == source, TeamAlias.raw_value == raw_value)
            .one_or_none()
        )
        if existing is None:
            row = TeamAlias(team_id=team_id, source=source, raw_value=raw_value, normalized_value=normalized_value)
            self._session.add(row)
            self._session.flush()
            return row
        if existing.team_id != team_id:
            raise IdentityConflictError(
                f"alias (source={source!r}, raw_value={raw_value!r}) already maps to team_id {existing.team_id}, got {team_id}"
            )
        existing.normalized_value = normalized_value
        self._session.flush()
        return existing


class SeasonRepository:
    def __init__(self, session: Session):
        self._session = session

    def upsert(self, *, season_id: int, start_year: int, end_year: int, label: str) -> Season:
        existing = self._session.get(Season, season_id)
        if existing is None:
            row = Season(season_id=season_id, start_year=start_year, end_year=end_year, label=label)
            self._session.add(row)
            self._session.flush()
            return row
        if (existing.start_year, existing.end_year, existing.label) != (start_year, end_year, label):
            raise IdentityConflictError(
                f"season_id {season_id} already exists as ({existing.start_year}, {existing.end_year}, {existing.label!r}), "
                f"got ({start_year}, {end_year}, {label!r})"
            )
        return existing


class PlayerRepository:
    def __init__(self, session: Session):
        self._session = session

    def get_by_id(self, player_id: str) -> Player | None:
        """Read-only primary-key lookup (Phase 6, Slice 4A) -- returns None rather than raising when no row exists; never mutates."""
        return self._session.get(Player, player_id)

    def upsert(self, *, player_id: str, display_name: str, team_id: int, season_id: int) -> Player:
        existing = self._session.get(Player, player_id)
        if existing is None:
            row = Player(player_id=player_id, display_name=display_name, team_id=team_id, season_id=season_id)
            self._session.add(row)
            self._session.flush()
            return row
        if (existing.team_id, existing.season_id) != (team_id, season_id):
            raise IdentityConflictError(
                f"player_id {player_id!r} already exists for (team_id={existing.team_id}, season_id={existing.season_id}), "
                f"got (team_id={team_id}, season_id={season_id})"
            )
        existing.display_name = display_name
        self._session.flush()
        return existing


class PlayerAliasRepository:
    def __init__(self, session: Session):
        self._session = session

    def upsert(self, *, player_id: str, source: str, raw_value: str, normalized_value: str) -> PlayerAlias:
        existing = (
            self._session.query(PlayerAlias)
            .filter(PlayerAlias.player_id == player_id, PlayerAlias.source == source, PlayerAlias.raw_value == raw_value)
            .one_or_none()
        )
        if existing is None:
            row = PlayerAlias(player_id=player_id, source=source, raw_value=raw_value, normalized_value=normalized_value)
            self._session.add(row)
            self._session.flush()
            return row
        existing.normalized_value = normalized_value
        self._session.flush()
        return existing


class GameRepository:
    def __init__(self, session: Session):
        self._session = session

    def upsert(
        self,
        *,
        game_id: int,
        home_team_id: int,
        away_team_id: int,
        season_id: int,
        game_date: dt.date | str,
        neutral_site: bool,
        is_provisional: bool,
        source: str | None = None,
        source_game_id: str | None = None,
    ) -> Game:
        if home_team_id == away_team_id:
            raise ValidationError(f"game_id {game_id}: home_team_id and away_team_id must differ, both were {home_team_id}")
        parsed_date = _coerce_date(game_date)
        existing = self._session.get(Game, game_id)
        if existing is None:
            row = Game(
                game_id=game_id, home_team_id=home_team_id, away_team_id=away_team_id, season_id=season_id,
                game_date=parsed_date, neutral_site=neutral_site, is_provisional=is_provisional,
                source=source, source_game_id=source_game_id,
            )
            self._session.add(row)
            self._session.flush()
            return row

        incoming = {
            "home_team_id": home_team_id, "away_team_id": away_team_id, "season_id": season_id,
            "game_date": parsed_date, "neutral_site": neutral_site, "is_provisional": is_provisional,
            "source": source, "source_game_id": source_game_id,
        }
        for field_name, new_value in incoming.items():
            if getattr(existing, field_name) != new_value:
                raise IdentityConflictError(
                    f"game_id {game_id} already exists with {field_name}={getattr(existing, field_name)!r}, got {new_value!r}"
                )
        return existing


class SportsbookRepository:
    def __init__(self, session: Session):
        self._session = session

    def upsert(self, *, sportsbook_id: int, canonical_key: str, display_name: str) -> Sportsbook:
        existing = self._session.get(Sportsbook, sportsbook_id)
        if existing is None:
            row = Sportsbook(sportsbook_id=sportsbook_id, canonical_key=canonical_key, display_name=display_name)
            self._session.add(row)
            self._session.flush()
            return row
        if existing.canonical_key != canonical_key:
            raise IdentityConflictError(
                f"sportsbook_id {sportsbook_id} already exists with canonical_key={existing.canonical_key!r}, got {canonical_key!r}"
            )
        existing.display_name = display_name
        self._session.flush()
        return existing


class ModelVersionRepository:
    def __init__(self, session: Session):
        self._session = session

    def upsert(self, *, model_version_id: int, label: str, config_fingerprint: str | None = None) -> ModelVersion:
        existing = self._session.get(ModelVersion, model_version_id)
        if existing is None:
            row = ModelVersion(model_version_id=model_version_id, label=label, config_fingerprint=config_fingerprint)
            self._session.add(row)
            self._session.flush()
            return row
        if (existing.label, existing.config_fingerprint) != (label, config_fingerprint):
            raise IdentityConflictError(
                f"model_version_id {model_version_id} already exists as (label={existing.label!r}, "
                f"config_fingerprint={existing.config_fingerprint!r}), got (label={label!r}, config_fingerprint={config_fingerprint!r})"
            )
        return existing


class DataSourceRepository:
    """
    `acquisition_type` and `is_rescrapable` are both treated as mutable
    here (like `display_name`/`description`): they describe the source's
    current real-world mechanism/status, which can legitimately change
    (e.g. a source that shuts down is no longer rescrapable) without that
    being an identity conflict -- `source_key` is the only field that
    identifies *which* source this is.
    """

    def __init__(self, session: Session):
        self._session = session

    def upsert(
        self,
        *,
        source_key: str,
        display_name: str,
        acquisition_type: str,
        is_rescrapable: bool,
        description: str | None = None,
    ) -> DataSource:
        existing = self._session.get(DataSource, source_key)
        if existing is None:
            row = DataSource(
                source_key=source_key, display_name=display_name, description=description,
                acquisition_type=acquisition_type, is_rescrapable=is_rescrapable,
            )
            self._session.add(row)
            self._session.flush()
            return row
        existing.display_name = display_name
        existing.description = description
        existing.acquisition_type = acquisition_type
        existing.is_rescrapable = is_rescrapable
        self._session.flush()
        return existing


class PipelineRunRepository:
    def __init__(self, session: Session):
        self._session = session

    def start(
        self,
        *,
        run_id: str,
        pipeline_name: str,
        started_at: dt.datetime,
        status: str = "running",
        model_version_id: int | None = None,
        notes: str | None = None,
    ) -> PipelineRun:
        started_at = _require_utc(started_at, field_name="started_at")
        existing = self._session.get(PipelineRun, run_id)
        if existing is not None:
            raise IdentityConflictError(f"pipeline run_id {run_id!r} already exists -- run_id must be freshly generated per run")
        row = PipelineRun(
            run_id=run_id, pipeline_name=pipeline_name, status=status, started_at=started_at,
            model_version_id=model_version_id, notes=notes,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def transition(self, *, run_id: str, new_status: str, completed_at: dt.datetime | None = None) -> PipelineRun:
        row = self._session.get(PipelineRun, run_id)
        if row is None:
            raise ValidationError(f"pipeline run_id {run_id!r} does not exist")
        if not is_valid_pipeline_run_transition(row.status, new_status):
            raise ValidationError(f"pipeline run_id {run_id!r}: invalid transition {row.status!r} -> {new_status!r}")
        row.status = new_status
        if completed_at is not None:
            row.completed_at = _require_utc(completed_at, field_name="completed_at")
        self._session.flush()
        return row


class SourceArtifactRepository:
    """
    Natural key: (`source_root_key`, `relative_path`, `content_hash`).
    Immutable once inserted for that key: `source_key`, `file_type`
    (they describe what the file objectively is). Mutable: everything
    else that can legitimately be refined by a later pass over the same
    file (re-classification, newly-resolved canonical links, a later
    pipeline run's re-validation).
    """

    def __init__(self, session: Session):
        self._session = session

    def upsert(
        self,
        *,
        source_key: str,
        source_root_key: str,
        relative_path: str,
        original_filename: str,
        file_type: str,
        point_in_time_classification: str,
        content_hash: str | None = None,
        file_size_bytes: int | None = None,
        filesystem_mtime: dt.datetime | None = None,
        collection_timestamp: dt.datetime | None = None,
        effective_timestamp: dt.datetime | None = None,
        season_id: int | None = None,
        team_id: int | None = None,
        player_id: str | None = None,
        validation_status: str = "pending",
        pipeline_run_id: str | None = None,
    ) -> SourceArtifact:
        filesystem_mtime = _require_utc(filesystem_mtime, field_name="filesystem_mtime")
        collection_timestamp = _require_utc(collection_timestamp, field_name="collection_timestamp")
        effective_timestamp = _require_utc(effective_timestamp, field_name="effective_timestamp")

        existing = (
            self._session.query(SourceArtifact)
            .filter(
                SourceArtifact.source_root_key == source_root_key,
                SourceArtifact.relative_path == relative_path,
                SourceArtifact.content_hash == content_hash,
            )
            .one_or_none()
        )
        if existing is None:
            row = SourceArtifact(
                source_key=source_key, source_root_key=source_root_key, relative_path=relative_path,
                original_filename=original_filename, file_type=file_type, file_size_bytes=file_size_bytes,
                content_hash=content_hash, filesystem_mtime=filesystem_mtime,
                collection_timestamp=collection_timestamp, effective_timestamp=effective_timestamp,
                season_id=season_id, team_id=team_id, player_id=player_id,
                validation_status=validation_status, point_in_time_classification=point_in_time_classification,
                pipeline_run_id=pipeline_run_id,
            )
            self._session.add(row)
            self._session.flush()
            return row

        if existing.source_key != source_key:
            raise IdentityConflictError(
                f"source_artifact ({source_root_key!r}, {relative_path!r}, {content_hash!r}) already exists with "
                f"source_key={existing.source_key!r}, got {source_key!r}"
            )
        if existing.file_type != file_type:
            raise IdentityConflictError(
                f"source_artifact ({source_root_key!r}, {relative_path!r}, {content_hash!r}) already exists with "
                f"file_type={existing.file_type!r}, got {file_type!r}"
            )

        existing.original_filename = original_filename
        existing.file_size_bytes = file_size_bytes
        existing.filesystem_mtime = filesystem_mtime
        existing.collection_timestamp = collection_timestamp
        existing.effective_timestamp = effective_timestamp
        existing.season_id = season_id
        existing.team_id = team_id
        existing.player_id = player_id
        existing.validation_status = validation_status
        existing.point_in_time_classification = point_in_time_classification
        existing.pipeline_run_id = pipeline_run_id
        self._session.flush()
        return existing


class DataQualityIssueRepository:
    """Append-only: every call inserts a new row. Never updates or dedupes an existing finding -- Phase 5A instruction: 'preserve audit history' / 'data quality issue append behavior'."""

    def __init__(self, session: Session):
        self._session = session

    def append(
        self,
        *,
        issue_code: str,
        severity: str,
        description: str,
        source_artifact_id: int | None = None,
        team_id: int | None = None,
        player_id: str | None = None,
        game_id: int | None = None,
        pipeline_run_id: str | None = None,
        context: str | None = None,
    ) -> DataQualityIssue:
        row = DataQualityIssue(
            issue_code=issue_code, severity=severity, description=description,
            source_artifact_id=source_artifact_id, team_id=team_id, player_id=player_id,
            game_id=game_id, pipeline_run_id=pipeline_run_id, context=context,
        )
        self._session.add(row)
        self._session.flush()
        return row
