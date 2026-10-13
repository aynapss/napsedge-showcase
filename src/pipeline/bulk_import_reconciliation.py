"""
Controlled bulk-import design, Slice 3 (docs/controlled_bulk_import_design.md
sections 8/13/16, approved for this slice only): a query-only reconciliation
reader.

This module never inserts, updates, deletes, flushes, commits, or rolls
back. It never creates or manages its own `Session`/`Engine` and never
reads `.env`/`NAPSEDGE_DATABASE_URL` -- every function here takes an
already-open, caller-owned `sqlalchemy.orm.Session` as its first argument
and issues only bounded `SELECT`/aggregate statements against it.
Transaction ownership (commit/rollback/close) stays entirely with the
caller; this module reads whatever that session's own transaction can
currently see, including the caller's own uncommitted work.

Real, confirmed schema findings this slice's own design is built on
(inspected directly against `src/db/*_models.py`, not assumed or
remembered from an earlier phase):

1. **Not every observation table carries its own `pipeline_run_id`.**
   `roster_memberships`, `player_stat_snapshots`, and `team_stat_snapshots`
   (the three AllStats tables) have no `pipeline_run_id` column at all --
   only a `source_artifact_id` FK. Every query in this module scopes a
   pipeline run uniformly via `<observation table>.source_artifact_id ==
   source_artifacts.id AND source_artifacts.pipeline_run_id == :run_id`
   (a `SourceArtifact` row's own `pipeline_run_id` is always the
   authoritative scope), never each table's own possibly-absent column
   directly -- this one join pattern works identically across all four
   families and closes the AllStats gap by construction.
2. **No family stores a labeled `ambiguous` vs. `unresolved` distinction
   for every identity dimension.** `espn_source_observations` has both
   `team_resolution_method` and `player_match_method` (so ESPN's own team
   and player dimensions distinguish resolved/ambiguous/unresolved
   fully). `player_game_log_observations` has `player_match_method` (full
   three-way split for player identity) but no equivalent column for its
   own `opponent_team_id` (only resolved-vs-not, via a plain nullable FK
   check -- confirmed by reading `player_game_log_import_adapter.py`'s
   own parse logic: an opponent's "ambiguous" and "not found" outcomes
   are never distinguished before persistence). `roster_memberships` /
   `player_stat_snapshots` (AllStats) and `team_game_log_observations`
   (opponent identity) have **no match-method column of any kind** --
   only a nullable identity FK -- so "ambiguous" is genuinely
   unrecoverable there once persisted, confirmed both by the schema and
   by the AllStats adapter's own parse logic collapsing `"stat_row_
   ambiguous"` and `"stat_row_not_found"` into the same
   `validation_status="unresolved"` before persistence (see
   `allstats_import_adapter.py`). This module reports `ambiguous` as an
   explicit `Unavailable(...)` sentinel, never a fabricated `0`, for
   every dimension the schema cannot actually distinguish.
3. **A "failed artifact" record (per the design's own section 9) can
   never be attributed to one source family via `source_artifact_id`**
   -- by construction, a failed artifact never gets a `source_artifacts`
   row at all (nothing was actually imported), so a
   `data_quality_issues` row recording that failure has
   `source_artifact_id = NULL`. This module reports failed/rolled-back
   artifact counts only at the whole-run level (via `issue_code`,
   regardless of attribution) and marks the per-family breakdown
   `Unavailable` -- never guessed from `team_id`/`player_id`/`game_id`,
   none of which reliably identifies "which of the four families."
4. **`source_artifacts.pipeline_run_id` is a mutable field an idempotent
   re-touch also updates** (confirmed in
   `SourceArtifactRepository.upsert()`'s own existing-row branch), so
   `COUNT(source_artifacts) WHERE pipeline_run_id = :run_id` counts every
   artifact *associated with* this run, not only artifacts *newly
   inserted* by it. This module names this field
   `source_artifacts_tagged_this_run` (not "new") and reports "newly
   inserted vs. idempotently re-touched" as `Unavailable` -- distinguishing
   the two would need either the future dry-run manifest (Slice 2, not
   read by this module) or a `created_at` heuristic this module
   deliberately does not invent.

No metric here is ever fabricated as zero when the schema cannot prove
it; every gap above is surfaced as an explicit, typed `Unavailable`
value with its own stated reason, never silently omitted or guessed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.db import allstats_models, espn_models, game_log_models, models, player_game_log_models
from src.exceptions import ValidationError

__all__ = [
    "Unavailable",
    "PipelineRunSummary",
    "IdentityCounts",
    "DataQualityIssueCount",
    "TableReconciliation",
    "GameResultReconciliation",
    "SourceFamilyReconciliation",
    "PipelineRunReconciliationReport",
    "SourceArtifactReconciliation",
    "SOURCE_FAMILY_SOURCE_KEYS",
    "read_pipeline_run_reconciliation",
    "read_source_family_reconciliation",
    "read_source_artifact_reconciliation",
]

#: The real, default `source_key` string each adapter registers via its
#: own `DataSourceRepository.upsert(source_key=...)` call (confirmed by
#: direct inspection of each adapter's own `persist_*_import_plan`
#: default parameter) -- reused here only to classify which family a
#: `SourceArtifact`/`data_quality_issues` row belongs to; never a second,
#: competing definition of these strings.
SOURCE_FAMILY_SOURCE_KEYS: dict[str, tuple[str, ...]] = {
    "allstats": ("sports_reference_allstats",),
    "team_game_log": ("sports_reference_game_log",),
    "espn": ("espn_player_stats", "espn_ncaa_benchmarks"),
    "player_game_log": ("sports_reference_player_game_log",),
}


@dataclass(frozen=True)
class Unavailable:
    """
    An explicit, typed marker that a requested metric cannot be derived
    from the current schema/data for this scope -- never a fabricated
    `0` or `None` standing in for "we don't know."
    """

    reason: str


@dataclass(frozen=True)
class PipelineRunSummary:
    run_id: str
    pipeline_name: str
    status: str
    started_at: object  # datetime.datetime, UTC-aware (kept loosely typed here to avoid a redundant import cycle note)
    completed_at: object | None


@dataclass(frozen=True)
class IdentityCounts:
    """Resolved/ambiguous/unresolved counts for one identity dimension (e.g. player, or opponent-team) within one table. `ambiguous` and/or `unresolved` may be `Unavailable` where the schema cannot distinguish them (see module docstring, finding 2)."""

    resolved: int
    ambiguous: int | Unavailable
    unresolved: int | Unavailable


@dataclass(frozen=True)
class DataQualityIssueCount:
    issue_code: str
    severity: str
    count: int


@dataclass(frozen=True)
class TableReconciliation:
    """One observation table's own reconciliation counts, at that table's own natural grain -- never joined against a sibling table of a different grain (see module docstring and design document section 8's own 'avoid one-to-many join multiplication' requirement)."""

    table_name: str
    persisted_rows: int
    accepted_rows: int
    rejected_rows: int
    player_identity: IdentityCounts | None
    opponent_identity: IdentityCounts | None
    team_identity: IdentityCounts | None


@dataclass(frozen=True)
class GameResultReconciliation:
    """`game_results` has a fundamentally different grain (one row per game, not per team-observation row) -- reported separately, never summed into a `TableReconciliation.persisted_rows` of a different grain."""

    persisted_rows: int
    by_result_status: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class SourceFamilyReconciliation:
    family: str
    source_artifacts_tagged_this_run: int
    newly_inserted_vs_idempotent_breakdown: Unavailable
    tables: tuple[TableReconciliation, ...]
    game_results: GameResultReconciliation | None
    espn_benchmark_values_persisted: int | None
    data_quality_issues: tuple[DataQualityIssueCount, ...]
    failed_artifacts: int | Unavailable


@dataclass(frozen=True)
class PipelineRunReconciliationReport:
    run: PipelineRunSummary
    families: tuple[SourceFamilyReconciliation, ...]
    run_level_data_quality_issues: tuple[DataQualityIssueCount, ...]
    total_persisted_rows: int
    total_accepted_rows: int
    total_rejected_rows: int
    total_failed_artifacts: int
    total_rolled_back_artifacts: int


@dataclass(frozen=True)
class SourceArtifactReconciliation:
    source_artifact_id: int
    source_key: str
    source_root_key: str
    relative_path: str
    content_hash: str | None
    validation_status: str
    collection_timestamp: object | None
    filesystem_mtime: object | None
    pipeline_run_id: str | None


def _require_pipeline_run(session: Session, run_id: str) -> models.PipelineRun:
    """Distinguishes 'this run exists and has zero rows' from 'this run_id does not exist at all' -- never the same result for both."""
    run = session.get(models.PipelineRun, run_id)
    if run is None:
        raise ValidationError(f"no such pipeline run: {run_id!r} -- refusing to report a reconciliation for a scope that does not exist")
    return run


def _pipeline_run_summary(run: models.PipelineRun) -> PipelineRunSummary:
    return PipelineRunSummary(
        run_id=run.run_id, pipeline_name=run.pipeline_name, status=run.status,
        started_at=run.started_at, completed_at=run.completed_at,
    )


def _validation_status_breakdown(session: Session, model, run_id: str) -> dict[str, int]:
    """SELECT model.validation_status, COUNT(*) scoped to this run via a join through source_artifacts -- the one uniform scoping path every family uses (module docstring finding 1)."""
    stmt = (
        select(model.validation_status, func.count())
        .join(models.SourceArtifact, model.source_artifact_id == models.SourceArtifact.id)
        .where(models.SourceArtifact.pipeline_run_id == run_id)
        .group_by(model.validation_status)
    )
    return {status: count for status, count in session.execute(stmt).all()}


def _identity_breakdown_by_column(
    session: Session, model, run_id: str, *, id_column, method_column=None, unresolved_method_values: tuple[str, ...] = ("unresolved", "team_unresolved"),
) -> IdentityCounts:
    """
    Generic resolved/ambiguous/unresolved counter for one identity
    dimension. If `method_column` is None, the schema has no match-method
    label for this dimension at all (module docstring finding 2) -- only
    `resolved` (id_column IS NOT NULL) vs. the unresolved-or-ambiguous
    total (id_column IS NULL) can be told apart, so `ambiguous` is
    reported `Unavailable` and `unresolved` reports the combined total
    with an explanatory note baked into the `Unavailable` reason only
    when it is itself unavailable (it is not, here -- the combined count
    IS knowable, just not split further).
    """
    resolved_stmt = (
        select(func.count())
        .select_from(model)
        .join(models.SourceArtifact, model.source_artifact_id == models.SourceArtifact.id)
        .where(models.SourceArtifact.pipeline_run_id == run_id, id_column.isnot(None))
    )
    resolved = session.execute(resolved_stmt).scalar_one()

    if method_column is None:
        combined_unresolved_stmt = (
            select(func.count())
            .select_from(model)
            .join(models.SourceArtifact, model.source_artifact_id == models.SourceArtifact.id)
            .where(models.SourceArtifact.pipeline_run_id == run_id, id_column.is_(None))
        )
        combined_unresolved = session.execute(combined_unresolved_stmt).scalar_one()
        return IdentityCounts(
            resolved=resolved,
            ambiguous=Unavailable("this table stores no match-method column -- ambiguous and not-found outcomes are both persisted as a null identity FK and cannot be told apart after the fact"),
            unresolved=combined_unresolved,
        )

    method_stmt = (
        select(method_column, func.count())
        .select_from(model)
        .join(models.SourceArtifact, model.source_artifact_id == models.SourceArtifact.id)
        .where(models.SourceArtifact.pipeline_run_id == run_id)
        .group_by(method_column)
    )
    by_method = {m: c for m, c in session.execute(method_stmt).all()}
    ambiguous = by_method.get("ambiguous", 0)
    unresolved = sum(by_method.get(v, 0) for v in unresolved_method_values)
    return IdentityCounts(resolved=resolved, ambiguous=ambiguous, unresolved=unresolved)


def _dq_issues_for_family(session: Session, run_id: str, source_keys: tuple[str, ...]) -> tuple[DataQualityIssueCount, ...]:
    """Only issues joinable to a source_artifact whose source_key belongs to this family -- an issue with source_artifact_id IS NULL (e.g. a whole-file failure, module docstring finding 3) is never attributable here and is excluded, on purpose, from this per-family breakdown (it is still counted at the run level)."""
    stmt = (
        select(models.DataQualityIssue.issue_code, models.DataQualityIssue.severity, func.count())
        .join(models.SourceArtifact, models.DataQualityIssue.source_artifact_id == models.SourceArtifact.id)
        .where(models.SourceArtifact.pipeline_run_id == run_id, models.SourceArtifact.source_key.in_(source_keys))
        .group_by(models.DataQualityIssue.issue_code, models.DataQualityIssue.severity)
        .order_by(models.DataQualityIssue.issue_code, models.DataQualityIssue.severity)
    )
    return tuple(DataQualityIssueCount(issue_code=code, severity=sev, count=cnt) for code, sev, cnt in session.execute(stmt).all())


def _source_artifacts_tagged(session: Session, run_id: str, source_keys: tuple[str, ...]) -> int:
    stmt = select(func.count()).select_from(models.SourceArtifact).where(
        models.SourceArtifact.pipeline_run_id == run_id, models.SourceArtifact.source_key.in_(source_keys),
    )
    return session.execute(stmt).scalar_one()


def _allstats_reconciliation(session: Session, run_id: str) -> SourceFamilyReconciliation:
    tables = []
    for model, name in (
        (allstats_models.RosterMembership, "roster_memberships"),
        (allstats_models.PlayerStatSnapshot, "player_stat_snapshots"),
        (allstats_models.TeamStatSnapshot, "team_stat_snapshots"),
    ):
        status_counts = _validation_status_breakdown(session, model, run_id)
        persisted = sum(status_counts.values())
        accepted = sum(status_counts.get(s, 0) for s in ("valid", "partial"))
        rejected = sum(status_counts.get(s, 0) for s in ("invalid", "conflicting"))
        player_identity = None
        if hasattr(model, "player_id"):
            player_identity = _identity_breakdown_by_column(session, model, run_id, id_column=model.player_id, method_column=None)
        tables.append(TableReconciliation(
            table_name=name, persisted_rows=persisted, accepted_rows=accepted, rejected_rows=rejected,
            player_identity=player_identity, opponent_identity=None, team_identity=None,
        ))
    source_keys = SOURCE_FAMILY_SOURCE_KEYS["allstats"]
    return SourceFamilyReconciliation(
        family="allstats",
        source_artifacts_tagged_this_run=_source_artifacts_tagged(session, run_id, source_keys),
        newly_inserted_vs_idempotent_breakdown=Unavailable("source_artifacts.pipeline_run_id is updated on an idempotent re-touch too -- distinguishing genuinely-new inserts requires the Slice 2 dry-run manifest, not read by this module"),
        tables=tuple(tables), game_results=None, espn_benchmark_values_persisted=None,
        data_quality_issues=_dq_issues_for_family(session, run_id, source_keys),
        failed_artifacts=Unavailable("a failed-artifact data_quality_issues row has no source_artifact_id to attribute to this family -- see run_level_data_quality_issues for the whole-run total"),
    )


def _team_game_log_reconciliation(session: Session, run_id: str) -> SourceFamilyReconciliation:
    model = game_log_models.TeamGameLogObservation
    status_counts = _validation_status_breakdown(session, model, run_id)
    persisted = sum(status_counts.values())
    accepted = sum(status_counts.get(s, 0) for s in ("valid", "partial"))
    rejected = sum(status_counts.get(s, 0) for s in ("invalid", "conflicting"))
    opponent_identity = _identity_breakdown_by_column(session, model, run_id, id_column=model.opponent_team_id, method_column=None)
    table = TableReconciliation(
        table_name="team_game_log_observations", persisted_rows=persisted, accepted_rows=accepted, rejected_rows=rejected,
        player_identity=None, opponent_identity=opponent_identity, team_identity=None,
    )

    game_result_stmt = (
        select(game_log_models.GameResult.result_status, func.count())
        .where(game_log_models.GameResult.pipeline_run_id == run_id)
        .group_by(game_log_models.GameResult.result_status)
    )
    by_status = session.execute(game_result_stmt).all()
    game_results = GameResultReconciliation(
        persisted_rows=sum(c for _, c in by_status),
        by_result_status=tuple((s, c) for s, c in by_status),
    )

    source_keys = SOURCE_FAMILY_SOURCE_KEYS["team_game_log"]
    return SourceFamilyReconciliation(
        family="team_game_log",
        source_artifacts_tagged_this_run=_source_artifacts_tagged(session, run_id, source_keys),
        newly_inserted_vs_idempotent_breakdown=Unavailable("source_artifacts.pipeline_run_id is updated on an idempotent re-touch too -- distinguishing genuinely-new inserts requires the Slice 2 dry-run manifest, not read by this module"),
        tables=(table,), game_results=game_results, espn_benchmark_values_persisted=None,
        data_quality_issues=_dq_issues_for_family(session, run_id, source_keys),
        failed_artifacts=Unavailable("a failed-artifact data_quality_issues row has no source_artifact_id to attribute to this family -- see run_level_data_quality_issues for the whole-run total"),
    )


def _espn_reconciliation(session: Session, run_id: str) -> SourceFamilyReconciliation:
    model = espn_models.EspnSourceObservation
    status_counts = _validation_status_breakdown(session, model, run_id)
    persisted = sum(status_counts.values())
    accepted = sum(status_counts.get(s, 0) for s in ("valid", "partial"))
    rejected = sum(status_counts.get(s, 0) for s in ("invalid", "conflicting"))
    team_identity = _identity_breakdown_by_column(session, model, run_id, id_column=model.team_id, method_column=model.team_resolution_method, unresolved_method_values=("unresolved",))
    player_identity = _identity_breakdown_by_column(session, model, run_id, id_column=model.player_id, method_column=model.player_match_method, unresolved_method_values=("unresolved", "team_unresolved"))
    table = TableReconciliation(
        table_name="espn_source_observations", persisted_rows=persisted, accepted_rows=accepted, rejected_rows=rejected,
        player_identity=player_identity, opponent_identity=None, team_identity=team_identity,
    )
    # espn_benchmark_values has a fundamentally different grain (one row per cohort
    # metric, no player/team identity concept at all) -- counted separately via its
    # own parent's pipeline_run_id, never folded into the per-observation table above.
    benchmark_values_stmt = (
        select(func.count())
        .select_from(espn_models.EspnBenchmarkValue)
        .join(espn_models.EspnBenchmarkSnapshot, espn_models.EspnBenchmarkValue.snapshot_id == espn_models.EspnBenchmarkSnapshot.id)
        .where(espn_models.EspnBenchmarkSnapshot.pipeline_run_id == run_id)
    )
    benchmark_values_persisted = session.execute(benchmark_values_stmt).scalar_one()

    source_keys = SOURCE_FAMILY_SOURCE_KEYS["espn"]
    return SourceFamilyReconciliation(
        family="espn",
        source_artifacts_tagged_this_run=_source_artifacts_tagged(session, run_id, source_keys),
        newly_inserted_vs_idempotent_breakdown=Unavailable("source_artifacts.pipeline_run_id is updated on an idempotent re-touch too -- distinguishing genuinely-new inserts requires the Slice 2 dry-run manifest, not read by this module"),
        tables=(table,), game_results=None, espn_benchmark_values_persisted=benchmark_values_persisted,
        data_quality_issues=_dq_issues_for_family(session, run_id, source_keys),
        failed_artifacts=Unavailable("a failed-artifact data_quality_issues row has no source_artifact_id to attribute to this family -- see run_level_data_quality_issues for the whole-run total"),
    )


def _player_game_log_reconciliation(session: Session, run_id: str) -> SourceFamilyReconciliation:
    model = player_game_log_models.PlayerGameLogObservation
    status_counts = _validation_status_breakdown(session, model, run_id)
    persisted = sum(status_counts.values())
    accepted = sum(status_counts.get(s, 0) for s in ("valid", "partial"))
    rejected = sum(status_counts.get(s, 0) for s in ("invalid", "conflicting"))
    player_identity = _identity_breakdown_by_column(session, model, run_id, id_column=model.player_id, method_column=model.player_match_method, unresolved_method_values=("unresolved", "team_unresolved"))
    opponent_identity = _identity_breakdown_by_column(session, model, run_id, id_column=model.opponent_team_id, method_column=None)
    table = TableReconciliation(
        table_name="player_game_log_observations", persisted_rows=persisted, accepted_rows=accepted, rejected_rows=rejected,
        player_identity=player_identity, opponent_identity=opponent_identity, team_identity=None,
    )
    source_keys = SOURCE_FAMILY_SOURCE_KEYS["player_game_log"]
    return SourceFamilyReconciliation(
        family="player_game_log",
        source_artifacts_tagged_this_run=_source_artifacts_tagged(session, run_id, source_keys),
        newly_inserted_vs_idempotent_breakdown=Unavailable("source_artifacts.pipeline_run_id is updated on an idempotent re-touch too -- distinguishing genuinely-new inserts requires the Slice 2 dry-run manifest, not read by this module"),
        tables=(table,), game_results=None, espn_benchmark_values_persisted=None,
        data_quality_issues=_dq_issues_for_family(session, run_id, source_keys),
        failed_artifacts=Unavailable("a failed-artifact data_quality_issues row has no source_artifact_id to attribute to this family -- see run_level_data_quality_issues for the whole-run total"),
    )


_FAMILY_READERS = {
    "allstats": _allstats_reconciliation,
    "team_game_log": _team_game_log_reconciliation,
    "espn": _espn_reconciliation,
    "player_game_log": _player_game_log_reconciliation,
}

#: Deterministic, stable family ordering for the full report -- matches
#: Slice 2's own SOURCE_FAMILY_ORDER exactly, never re-derived or
#: re-alphabetized independently.
_FAMILY_ORDER: tuple[str, ...] = ("allstats", "team_game_log", "player_game_log", "espn")


def read_source_family_reconciliation(session: Session, run_id: str, family: str) -> SourceFamilyReconciliation:
    """Single-family scope. Raises ValidationError for an unknown family or a non-existent run_id -- never silently returns an all-zero report for either."""
    if family not in _FAMILY_READERS:
        raise ValidationError(f"unknown source family {family!r}; expected one of {sorted(_FAMILY_READERS)}")
    _require_pipeline_run(session, run_id)
    return _FAMILY_READERS[family](session, run_id)


def read_pipeline_run_reconciliation(session: Session, run_id: str) -> PipelineRunReconciliationReport:
    """The complete, whole-run reconciliation report across all four families, in deterministic family order."""
    run = _require_pipeline_run(session, run_id)

    families = tuple(_FAMILY_READERS[family](session, run_id) for family in _FAMILY_ORDER)

    run_level_dq_stmt = (
        select(models.DataQualityIssue.issue_code, models.DataQualityIssue.severity, func.count())
        .where(models.DataQualityIssue.pipeline_run_id == run_id)
        .group_by(models.DataQualityIssue.issue_code, models.DataQualityIssue.severity)
        .order_by(models.DataQualityIssue.issue_code, models.DataQualityIssue.severity)
    )
    run_level_issues = tuple(
        DataQualityIssueCount(issue_code=code, severity=sev, count=cnt)
        for code, sev, cnt in session.execute(run_level_dq_stmt).all()
    )
    total_failed = sum(c.count for c in run_level_issues if c.issue_code == "bulk_import_artifact_failed")

    return PipelineRunReconciliationReport(
        run=_pipeline_run_summary(run),
        families=families,
        run_level_data_quality_issues=run_level_issues,
        total_persisted_rows=sum(t.persisted_rows for f in families for t in f.tables),
        total_accepted_rows=sum(t.accepted_rows for f in families for t in f.tables),
        total_rejected_rows=sum(t.rejected_rows for f in families for t in f.tables),
        total_failed_artifacts=total_failed,
        total_rolled_back_artifacts=total_failed,  # same event by construction -- design document section 9
    )


def read_source_artifact_reconciliation(session: Session, source_artifact_id: int) -> SourceArtifactReconciliation:
    """Single-artifact scope -- the smallest reconciliation grain the schema supports."""
    artifact = session.get(models.SourceArtifact, source_artifact_id)
    if artifact is None:
        raise ValidationError(f"no such source_artifact id: {source_artifact_id!r} -- refusing to report a reconciliation for a scope that does not exist")
    return SourceArtifactReconciliation(
        source_artifact_id=artifact.id, source_key=artifact.source_key, source_root_key=artifact.source_root_key,
        relative_path=artifact.relative_path, content_hash=artifact.content_hash, validation_status=artifact.validation_status,
        collection_timestamp=artifact.collection_timestamp, filesystem_mtime=artifact.filesystem_mtime,
        pipeline_run_id=artifact.pipeline_run_id,
    )
