"""
Phase 5A foundation ORM models.

Every canonical-identity table's primary key is the externally-computed
stable ID from `src.identity.contracts`/`src.identity.stable_id` --
`team_id`, `player_id`, `conference_id`, `season_id`, `sportsbook_id`,
`model_version_id`, `game_id` -- never a database-generated surrogate.
This is deliberate and load-bearing: "never create a second team/player/
season/game/sportsbook/model-version identifier" (Phase 5A instruction)
means these columns must NOT be `autoincrement`/`SERIAL`; the value is
always supplied by the caller (already computed by the existing,
already-tested identity-resolution layer) and the database only stores
and constrains it.

Non-identity bookkeeping tables (`team_aliases`, `player_aliases`,
`source_artifacts`, `data_quality_issues`) DO use a database-generated
surrogate `id`, because an alias row, a source-file record, or a
data-quality finding has no canonical cross-source identity of its own to
preserve -- inventing one would violate the same principle in the other
direction.

No analytical calculation appears anywhere in this module (Phase 5A
instruction: "database models must not contain analytical calculations").
Every column is either an externally-supplied identity value, a raw
provenance fact, or a bookkeeping timestamp.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base
from src.db.enums import (
    ACQUISITION_TYPES,
    ISSUE_SEVERITIES,
    PIPELINE_RUN_STATUSES,
    POINT_IN_TIME_CLASSIFICATIONS,
    VALIDATION_STATUSES,
    sql_check_in,
)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


#: BigInteger on PostgreSQL (BIGSERIAL when autoincrement), but Integer on
#: SQLite -- SQLite's ROWID/autoincrement aliasing only applies to a column
#: that renders as exactly "INTEGER"; a literal BIGINT primary key silently
#: stops autoincrementing on SQLite (confirmed while writing this batch's
#: own tests). This affects only the surrogate (non-identity) PKs below --
#: every canonical-identity PK is caller-supplied and never autoincrements
#: on either dialect, so it does not need this variant.
_BigIntegerPk = BigInteger().with_variant(Integer(), "sqlite")


# ---------------------------------------------------------------------------
# Canonical identity / reference tables
# ---------------------------------------------------------------------------


class Conference(Base):
    __tablename__ = "conferences"

    conference_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    canonical_name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=_utcnow)

    teams: Mapped[list["Team"]] = relationship(back_populates="conference")

    __table_args__ = (
        UniqueConstraint("canonical_name", name="uq_conferences_canonical_name"),
    )


class Team(Base):
    __tablename__ = "teams"

    team_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    sr_slug: Mapped[str] = mapped_column(String(200), nullable=False)
    conference_id: Mapped[int | None] = mapped_column(ForeignKey("conferences.conference_id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=_utcnow)

    conference: Mapped[Conference | None] = relationship(back_populates="teams")
    aliases: Mapped[list["TeamAlias"]] = relationship(back_populates="team", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("sr_slug", name="uq_teams_sr_slug"),
    )


class TeamAlias(Base):
    """
    One source system's raw spelling for a team. Uniqueness is scoped by
    `source` (Phase 5A instruction: "alias uniqueness must be scoped by
    source and entity") -- within one source, a given raw value must always
    map to the same team; it may collide freely with an unrelated raw value
    coincidentally shared by a different source.
    """

    __tablename__ = "team_aliases"

    id: Mapped[int] = mapped_column(_BigIntegerPk, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.team_id"), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    raw_value: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    team: Mapped[Team] = relationship(back_populates="aliases")

    __table_args__ = (
        UniqueConstraint("source", "raw_value", name="uq_team_aliases_source_raw_value"),
        Index("ix_team_aliases_team_id", "team_id"),
    )


class Season(Base):
    __tablename__ = "seasons"

    season_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    start_year: Mapped[int] = mapped_column(Integer, nullable=False)
    end_year: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("end_year >= start_year", name="end_year_after_start_year"),
    )


class Player(Base):
    __tablename__ = "players"

    player_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.team_id"), nullable=False)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.season_id"), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=_utcnow)

    aliases: Mapped[list["PlayerAlias"]] = relationship(back_populates="player", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_players_team_id_season_id", "team_id", "season_id"),
    )


class PlayerAlias(Base):
    """
    Scoped by `player_id` (not globally by `source`, unlike `TeamAlias`):
    player display names are not unique per source without team+season
    context (two different real players can share a name), so a
    source-global uniqueness constraint would be actively wrong here.
    """

    __tablename__ = "player_aliases"

    id: Mapped[int] = mapped_column(_BigIntegerPk, primary_key=True, autoincrement=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.player_id"), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    raw_value: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    player: Mapped[Player] = relationship(back_populates="aliases")

    __table_args__ = (
        UniqueConstraint("player_id", "source", "raw_value", name="uq_player_aliases_player_source_raw_value"),
        Index("ix_player_aliases_player_id", "player_id"),
    )


class Game(Base):
    """
    `is_provisional` follows `src.identity.game_identity` exactly: a
    provisional `game_id` is a structural hash over
    (home_team_id, away_team_id, season_id, game_date, neutral_site); a
    confirmed one is derived from a trusted `source`/`source_game_id`
    instead. A provisional row is never mutated into a confirmed one in
    place -- a confirmed row gets its own, different `game_id` (a new row).
    Reconciling the two is `src.identity.game_identity.find_game_id_conflicts`'s
    job at the application layer, not a DB constraint (matching that
    module's own "reported, never silently merged" design).
    """

    __tablename__ = "games"

    #: Widened from `Integer` to `_BigIntegerPk` by the Phase 5 identity-
    #: key remediation migration -- `game_id` now comes from
    #: `stable_wide_id` (a 63-bit digest), which no longer fits in a
    #: 32-bit `Integer` column. See `docs/identity_scheme.md`.
    game_id: Mapped[int] = mapped_column(_BigIntegerPk, primary_key=True, autoincrement=False)
    home_team_id: Mapped[int] = mapped_column(ForeignKey("teams.team_id"), nullable=False)
    away_team_id: Mapped[int] = mapped_column(ForeignKey("teams.team_id"), nullable=False)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.season_id"), nullable=False)
    game_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    neutral_site: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_provisional: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source_game_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=_utcnow)

    __table_args__ = (
        CheckConstraint("home_team_id <> away_team_id", name="home_away_distinct"),
        Index("ix_games_season_id", "season_id"),
        Index("ix_games_home_team_id", "home_team_id"),
        Index("ix_games_away_team_id", "away_team_id"),
    )


class Sportsbook(Base):
    __tablename__ = "sportsbooks"

    sportsbook_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    canonical_key: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=_utcnow)

    __table_args__ = (
        UniqueConstraint("canonical_key", name="uq_sportsbooks_canonical_key"),
    )


class ModelVersion(Base):
    __tablename__ = "model_versions"

    model_version_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    config_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ---------------------------------------------------------------------------
# Source-provenance / pipeline-run / data-quality infrastructure
# ---------------------------------------------------------------------------


class DataSource(Base):
    """
    Small, explicitly-registered vocabulary of source systems (e.g.
    `sports_reference_allstats`, `espn_player_stats`, `torvik`) -- never
    auto-created by an importer guessing a new key.

    `acquisition_type` classifies *how* this source's evidence is
    obtained (e.g. `live_scraped_json` for Torvik's real live-scraped JSON
    response, confirmed by the user this batch) -- a fact about the
    source's own mechanism, never a per-artifact value, and never a
    substitute for anything on `source_artifacts` itself.
    `is_rescrapable` records whether a fresh fetch can currently recover
    equivalent evidence -- true for Torvik (the user confirmed AdjOE/AdjDE
    and other live metrics can be reacquired), independent of whether any
    *specific past snapshot* is still reproducible (it generally is not,
    since Torvik's own ratings recalculate as games are played -- that
    non-reproducibility is exactly why every snapshot must be preserved
    immutably in `source_artifacts` rather than treated as
    interchangeable with a fresh scrape).
    """

    __tablename__ = "data_sources"

    source_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    acquisition_type: Mapped[str] = mapped_column(String(30), nullable=False)
    is_rescrapable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(sql_check_in("acquisition_type", ACQUISITION_TYPES), name="acquisition_type"),
    )


class PipelineRun(Base):
    """
    `run_id` is an application-generated UUID4 hex string (assigned before
    the row exists, e.g. so it can appear in log lines emitted before the
    insert commits) rather than a database-generated serial.
    """

    __tablename__ = "pipeline_runs"

    run_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    pipeline_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    model_version_id: Mapped[int | None] = mapped_column(ForeignKey("model_versions.model_version_id"), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(sql_check_in("status", PIPELINE_RUN_STATUSES), name="status"),
    )


class SourceArtifact(Base):
    """
    One imported/observed snapshot of evidence from a registered
    `data_sources` row. `source_root_key` + `relative_path` are the
    portable identifier -- an absolute Windows path is never stored as (or
    derivable as necessary for) a join key; `source_root_key` names a
    *configured* source root, not the path itself.

    For a local, file-based source, that root is a directory (e.g.
    `src.paths.CBB_DIR`/`ESPN_DIR`) and `relative_path` is a path within
    it. For a live-scraped/rescrapable source with no local directory of
    its own (e.g. Torvik, per this batch's source-classification review),
    the same two columns instead name a configured *cache* root and a
    (optionally content-addressed) relative path within it -- the schema
    does not need to know or care which case applies; both are "a root
    plus a path relative to it," never an absolute path or a live URL.

    Content-hash-scoped natural-key uniqueness (see `__table_args__`
    below) is what actually makes repeated scrapes of a rescrapable
    source safe: a later scrape that returns *different* content
    (Torvik's ratings recalculating as games are played, for example)
    gets a new row -- it is never used to overwrite an earlier snapshot,
    and an identical repeat scrape (same content, same hash) is the
    ordinary idempotent-upsert no-op every other table in this batch
    already has.
    """

    __tablename__ = "source_artifacts"

    id: Mapped[int] = mapped_column(_BigIntegerPk, primary_key=True, autoincrement=True)
    source_key: Mapped[str] = mapped_column(ForeignKey("data_sources.source_key"), nullable=False)
    source_root_key: Mapped[str] = mapped_column(String(50), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(500), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(200), nullable=False)
    file_type: Mapped[str] = mapped_column(String(20), nullable=False)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: Filesystem modification time -- METADATA ONLY. Phase 5A instruction:
    #: "filesystem modification time must never be treated as collection
    #: time." Never read by any repository method as a substitute for
    #: `collection_timestamp` below.
    filesystem_mtime: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    collection_timestamp: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_timestamp: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    import_timestamp: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    season_id: Mapped[int | None] = mapped_column(ForeignKey("seasons.season_id"), nullable=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.team_id"), nullable=True)
    player_id: Mapped[str | None] = mapped_column(ForeignKey("players.player_id"), nullable=True)

    validation_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    point_in_time_classification: Mapped[str] = mapped_column(String(50), nullable=False)
    pipeline_run_id: Mapped[str | None] = mapped_column(ForeignKey("pipeline_runs.run_id"), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("source_root_key", "relative_path", "content_hash", name="uq_source_artifacts_root_path_hash"),
        CheckConstraint(sql_check_in("validation_status", VALIDATION_STATUSES), name="validation_status"),
        CheckConstraint(sql_check_in("point_in_time_classification", POINT_IN_TIME_CLASSIFICATIONS), name="pit_classification"),
        Index("ix_source_artifacts_source_key", "source_key"),
        Index("ix_source_artifacts_team_id", "team_id"),
    )


class DataQualityIssue(Base):
    __tablename__ = "data_quality_issues"

    id: Mapped[int] = mapped_column(_BigIntegerPk, primary_key=True, autoincrement=True)
    issue_code: Mapped[str] = mapped_column(String(100), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    source_artifact_id: Mapped[int | None] = mapped_column(ForeignKey("source_artifacts.id"), nullable=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.team_id"), nullable=True)
    player_id: Mapped[str | None] = mapped_column(ForeignKey("players.player_id"), nullable=True)
    game_id: Mapped[int | None] = mapped_column(ForeignKey("games.game_id"), nullable=True)
    pipeline_run_id: Mapped[str | None] = mapped_column(ForeignKey("pipeline_runs.run_id"), nullable=True)

    context: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(sql_check_in("severity", ISSUE_SEVERITIES), name="severity"),
        Index("ix_data_quality_issues_issue_code", "issue_code"),
    )
