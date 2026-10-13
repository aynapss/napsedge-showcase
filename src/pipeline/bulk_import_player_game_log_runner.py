"""
Controlled bulk-import design, Slice 7 (docs/controlled_bulk_import_design.md
section 16 item 7, approved for this slice only): the player-game-log
single-source-family committed-import runner.

**This slice does not authorize a real import.** Every automated test for
this module runs only against synthetic, in-memory player-game-log
fixtures and the established disposable PostgreSQL container
(`tests/postgres/conftest.py`). This module never discovers, scans, or
reads `TeamDump` itself (including the per-team `Roster_<season>.xlsx`
`resolve_team_roster_for_player_gamelogs` reads), never connects to a
database on its own (every connection/session is caller-supplied), and
never invokes the player-game-log parser directly -- it calls the
existing, unmodified `persist_player_game_log_import_plan` and nothing
else touches the database.

Artifact-unit precheck (done before writing any code, per instruction):
direct inspection of `persist_player_game_log_import_plan` confirms the
design's own stated model is CORRECT for this family, exactly like Slice
6's own team game-log finding and unlike Slice 5's AllStats correction --
the function accepts exactly one `DiscoveredPlayerGameLogFile` (one CSV)
and one `content_hash: str` (singular), committing it in exactly one
`unit_of_work`. `PlayerGameLogCommitArtifact` is defined at that same
one-file granularity, no correction needed.

Two facts, confirmed by direct inspection, distinguish this family's own
artifact identity from Slices 5/6's:

1. **`plan.team_slug`/`plan.player_slug` ARE safe to cross-check against
   `discovered`'s own fields here** -- unlike AllStats/team-game-log
   (where `plan.team_slug` is a parse-time display-name `team_query`,
   legitimately different from the directory slug), `parse_player_game_log_workbook`
   takes `team_slug`/`player_slug` directly as keyword arguments and
   stores them verbatim on `ParsedPlayerGameLogFile` -- confirmed against
   every call site in the adapter's own test suite, which always passes
   the identical slug to both the parse call and the discovered-file
   fixture. `PlayerGameLogCommitArtifact.__post_init__` cross-checks both.
2. **`game_num`, present as a raw source CSV column, is never read by
   this adapter at all** -- confirmed by direct inspection of
   `parse_player_game_log_workbook`'s own body (no `row.get("game_num")`
   anywhere). The adapter's own ordinal is `source_row_position` (this
   player's Nth row in THEIR OWN file, an enumerate index) -- never a
   team-season-game number, and never assumed to relate to one. This
   runner reasons only about `source_row_position`, never `game_num`.

**Player identity is filename-encoded, one player per artifact** -- the
runner's own duplicate/ordering identity is `(team_slug, player_slug,
season)`, not just `(team_slug, season)` (a team may have many player
files for one season).

**The adapter never writes `game_results`** -- confirmed by grepping both
`player_game_log_import_adapter.py` and its own test suite for
`GameResult`: zero references. Unlike Slice 6, this runner's own
reconciliation therefore never checks a `game_results` state transition;
instead it proves, from an independent read, that `game_results` stays
completely untouched by this family's own commits (see
`_verify_no_unexpected_game_result_effect` below) -- "do not create a
GameResult ... unless the existing adapter already does so" is enforced
as a runtime invariant, not just a documentation claim. `Game` rows ARE
written (shared, upserted by PK, possibly already existing from a team
game-log or a teammate's own player file) -- this runner confirms
existence for every `game_id` its own artifact touches via an
independent read, but never tries to count "newly inserted" Games (the
same row may legitimately already exist from an unrelated prior commit,
in this run or an earlier one -- "do not assume persisted observation
count equals newly inserted Game count").

Same leaf-level conventions as Slice 6 (both remain independent,
single-family modules -- no shared orchestration framework): complete
manifest/artifact correspondence is required (both "missing planned
artifact" and "unplanned artifact" are refused), the manifest's own
`PlannedArtifact.source_root_key` must equal the literal, hardcoded value
`persist_player_game_log_import_plan` itself always writes
(`EXPECTED_SOURCE_ROOT_KEY = "teamdump_cbb"`), and a committable
artifact's own manifest `status` must be in `COMMITTABLE_MANIFEST_STATUSES`.

Nothing here fabricates a metric the schema cannot prove: opponent
ambiguity is never invented where the schema stores only a nullable FK
(same documented gap as team-game-log's own opponent identity, per the
existing Slice 3 reader); "newly inserted vs. idempotently reused"
counts are never claimed (same existing, documented `Unavailable` gap);
no roster evidence is synthesized for a team with none (e.g.
`st-thomas-(mn)`, a confirmed real, already-documented gap, left exactly
as-is).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Sequence

from sqlalchemy.engine import Connection

from src.db import game_log_models, models
from src.db.repositories import DataQualityIssueRepository, PipelineRunRepository
from src.db.session import unit_of_work
from src.exceptions import ValidationError
from src.pipeline.bulk_import_manifest import MANIFEST_SCHEMA_VERSION, DryRunManifest, SourceFamilyPlan
from src.pipeline.bulk_import_reconciliation import read_source_family_reconciliation
from src.pipeline.bulk_import_safety import (
    DatabaseTargetDesignation,
    bulk_import_advisory_lock,
    verify_database_target,
)
from src.pipeline.player_game_log_import_adapter import (
    PLAYER_GAMELOG_IMPORT_CONTRACT_VERSION,
    DiscoveredPlayerGameLogFile,
    PlayerGameLogImportPlan,
    PlayerGameLogImportResult,
    persist_player_game_log_import_plan,
)

__all__ = [
    "FAILED_ARTIFACT_ISSUE_CODE",
    "RECONCILIATION_MISMATCH_ISSUE_CODE",
    "EXPECTED_SOURCE_ROOT_KEY",
    "COMMITTABLE_MANIFEST_STATUSES",
    "PlayerGameLogCommitArtifact",
    "PlayerGameLogArtifactOutcome",
    "PlayerGameLogCommittedImportRunResult",
    "run_player_game_log_committed_import",
]

#: Verbatim from docs/controlled_bulk_import_design.md section 9 -- the
#: same fixed issue_code Slices 5/6's own runners use, intentionally
#: duplicated here (not imported from either) since every runner
#: independently derives it from the same approved design text, not from
#: each other -- no cross-runner coupling.
FAILED_ARTIFACT_ISSUE_CODE = "bulk_import_artifact_failed"

#: This runner's own issue_code for the (expected-to-be-unreachable in
#: correct operation) case where a just-committed artifact's independently
#: re-read state does not match what this family's own adapter could
#: actually have produced.
RECONCILIATION_MISMATCH_ISSUE_CODE = "bulk_import_reconciliation_mismatch"

#: The literal, hardcoded `source_root_key` value `persist_player_game_log_import_plan`
#: itself always writes (confirmed by direct inspection -- not a caller-
#: configurable parameter of that function).
EXPECTED_SOURCE_ROOT_KEY = "teamdump_cbb"

#: A dry-run manifest disposition eligible for an actual commit. An
#: `"unresolved"`/`"ambiguous"`/`"conflict"` status is a recorded data-
#: quality finding from the dry run, never something this runner commits.
COMMITTABLE_MANIFEST_STATUSES = frozenset({"planned_insert", "idempotent_match"})


@dataclass(frozen=True)
class PlayerGameLogCommitArtifact:
    """
    One caller-supplied, already-parsed-and-planned player-season
    game-log file, ready to commit. Confirmed (see module docstring) to
    be the real, unmodified adapter's own atomic transaction unit for
    this family -- no artifact-granularity correction needed.

    `team_slug`/`player_slug`/`season` are derived from `discovered`
    (never taken as separate constructor arguments) AND cross-checked
    against `plan`'s own matching fields -- safe here (unlike Slices 5/6)
    because `parse_player_game_log_workbook` stores its `team_slug`/
    `player_slug` keyword arguments verbatim, with no display-name
    substitution.
    """

    discovered: DiscoveredPlayerGameLogFile
    content_hash: str
    plan: PlayerGameLogImportPlan

    def __post_init__(self) -> None:
        if not isinstance(self.discovered, DiscoveredPlayerGameLogFile):
            raise ValidationError("discovered must be a DiscoveredPlayerGameLogFile produced by discover_player_game_log_artifacts")
        if not isinstance(self.content_hash, str) or not self.content_hash:
            raise ValidationError("content_hash must be a non-empty string")
        if not isinstance(self.plan, PlayerGameLogImportPlan):
            raise ValidationError("plan must be a PlayerGameLogImportPlan produced by build_player_game_log_import_plan")
        if self.plan.season != self.discovered.season:
            raise ValidationError(
                f"plan season {self.plan.season!r} does not match discovered file season {self.discovered.season!r} "
                f"for team_slug={self.discovered.team_slug!r}, player_slug={self.discovered.player_slug!r} -- "
                f"refusing a mismatched discovered/plan pairing"
            )
        if self.plan.team_slug != self.discovered.team_slug:
            raise ValidationError(
                f"plan team_slug {self.plan.team_slug!r} does not match discovered file team_slug {self.discovered.team_slug!r} "
                f"-- refusing a mismatched discovered/plan pairing"
            )
        if self.plan.player_slug != self.discovered.player_slug:
            raise ValidationError(
                f"plan player_slug {self.plan.player_slug!r} does not match discovered file player_slug "
                f"{self.discovered.player_slug!r} -- refusing a mismatched discovered/plan pairing"
            )

    @property
    def team_slug(self) -> str:
        return self.discovered.team_slug

    @property
    def player_slug(self) -> str:
        return self.discovered.player_slug

    @property
    def season(self) -> int:
        return self.discovered.season

    @property
    def identity(self) -> tuple[str, str, int]:
        """The `(team_slug, player_slug, season)` triple this artifact would occupy -- this runner's own duplicate/ordering key."""
        return (self.team_slug, self.player_slug, self.season)

    @property
    def touched_game_ids(self) -> frozenset[int]:
        return frozenset(spec["game_id"] for spec in self.plan.observation_specs if spec["game_id"] is not None)


@dataclass(frozen=True)
class PlayerGameLogArtifactOutcome:
    """One artifact's own outcome within a committed-import run. `result` is populated only when `status == "committed"`."""

    team_slug: str
    player_slug: str
    season: int
    status: str  # "committed" | "failed"
    result: PlayerGameLogImportResult | None = None
    error: str | None = None


@dataclass(frozen=True)
class PlayerGameLogCommittedImportRunResult:
    """
    The immutable, final outcome of one `run_player_game_log_committed_import`
    call. `status == "succeeded"` only when every supplied artifact was
    committed and independently reconciled -- a run that stopped early on
    any artifact failure always reports `"failed"`, never a partial
    success.
    """

    run_id: str
    status: str  # "succeeded" | "failed"
    artifact_outcomes: tuple[PlayerGameLogArtifactOutcome, ...] = field(default_factory=tuple)

    @property
    def committed_count(self) -> int:
        return sum(1 for o in self.artifact_outcomes if o.status == "committed")

    @property
    def failed_count(self) -> int:
        return sum(1 for o in self.artifact_outcomes if o.status == "failed")


def _validate_manifest(manifest: DryRunManifest, manifest_schema_version: str) -> SourceFamilyPlan:
    if manifest_schema_version != MANIFEST_SCHEMA_VERSION:
        raise ValidationError(
            f"manifest_schema_version {manifest_schema_version!r} does not match this runtime's current "
            f"manifest schema version {MANIFEST_SCHEMA_VERSION!r} -- refusing a stale or incompatible manifest"
        )
    if not isinstance(manifest, DryRunManifest):
        raise ValidationError("manifest must be a DryRunManifest produced by build_dry_run_manifest")
    if len(manifest.families) != 1:
        raise ValidationError(
            f"this runner accepts only a single-family player_game_log manifest, got {len(manifest.families)} "
            f"families ({[p.family for p in manifest.families]}) -- refusing an empty or mixed-family manifest"
        )
    family_plan = manifest.families[0]
    if family_plan.family != "player_game_log":
        raise ValidationError(f"this runner only ever commits the 'player_game_log' family, got {family_plan.family!r}")
    if family_plan.contract_version != PLAYER_GAMELOG_IMPORT_CONTRACT_VERSION:
        raise ValidationError(
            f"manifest player_game_log contract_version {family_plan.contract_version!r} does not match the current "
            f"player game-log adapter contract version {PLAYER_GAMELOG_IMPORT_CONTRACT_VERSION!r} -- refusing a stale "
            f"manifest (the adapter's own contract changed since this manifest was built)"
        )
    return family_plan


def _validate_artifacts(family_plan: SourceFamilyPlan, artifacts: Sequence[PlayerGameLogCommitArtifact]) -> tuple[PlayerGameLogCommitArtifact, ...]:
    if isinstance(artifacts, (str, bytes)) or not isinstance(artifacts, Sequence):
        raise ValidationError("artifacts must be a sequence of PlayerGameLogCommitArtifact")
    for artifact in artifacts:
        if not isinstance(artifact, PlayerGameLogCommitArtifact):
            raise ValidationError(f"every artifact must be a PlayerGameLogCommitArtifact, got {type(artifact).__name__}")

    seen_identities: set[tuple[str, str, int]] = set()
    for artifact in artifacts:
        if artifact.identity in seen_identities:
            raise ValidationError(f"duplicate artifact for (team_slug, player_slug, season) {artifact.identity} -- refusing to process the same player-season twice in one run")
        seen_identities.add(artifact.identity)

    manifest_by_relpath = {planned.relative_path: planned for planned in family_plan.artifacts}
    artifact_by_relpath = {artifact.discovered.relative_path: artifact for artifact in artifacts}

    for artifact in artifacts:
        if artifact.discovered.relative_path not in manifest_by_relpath:
            raise ValidationError(
                f"unplanned artifact: {artifact.discovered.relative_path!r} (identity {artifact.identity}) is not "
                f"present in the approved dry-run manifest -- refusing to commit a file that was never planned"
            )

    for planned in family_plan.artifacts:
        if planned.relative_path not in artifact_by_relpath:
            raise ValidationError(
                f"missing planned artifact: {planned.relative_path!r} is approved in the manifest but no matching "
                f"artifact was supplied to this call -- this runner requires complete manifest/artifact correspondence"
            )

    for artifact in artifacts:
        planned = manifest_by_relpath[artifact.discovered.relative_path]
        if planned.source_root_key != EXPECTED_SOURCE_ROOT_KEY:
            raise ValidationError(
                f"mismatched source_root_key for {artifact.discovered.relative_path!r}: manifest recorded "
                f"{planned.source_root_key!r}, but the player game-log adapter always persists under "
                f"{EXPECTED_SOURCE_ROOT_KEY!r} -- refusing a manifest that could never match what gets written"
            )
        if planned.content_hash != artifact.content_hash:
            raise ValidationError(
                f"artifact file {artifact.discovered.relative_path!r} (identity {artifact.identity}) has "
                f"content_hash {artifact.content_hash!r}, but the approved dry-run manifest recorded "
                f"{planned.content_hash!r} for this same file -- refusing a changed/stale artifact that no longer "
                f"matches what was approved"
            )
        if planned.status not in COMMITTABLE_MANIFEST_STATUSES:
            raise ValidationError(
                f"artifact file {artifact.discovered.relative_path!r} has manifest status {planned.status!r}, not "
                f"eligible for a committed import (expected one of {sorted(COMMITTABLE_MANIFEST_STATUSES)})"
            )

    # Deterministic order -- never the caller's own input order.
    return tuple(sorted(artifacts, key=lambda a: a.identity))


def _player_game_log_observations_persisted(session_factory, run_id: str) -> int:
    """Independent, database-read row count for `player_game_log_observations` currently tagged to `run_id`, via the existing Slice 3 reconciliation reader. Read-only: opens a session, queries, closes -- never commits or writes."""
    session = session_factory()
    try:
        report = read_source_family_reconciliation(session, run_id, "player_game_log")
    finally:
        session.close()
    return report.tables[0].persisted_rows


def _games_and_results_snapshot(session_factory, game_ids: frozenset[int]) -> tuple[frozenset[int], dict[int, tuple[str, int]]]:
    """
    Independent, database-read snapshot for exactly `game_ids`: which of
    them already have a `games` row, and the `(result_status,
    source_count)` of any existing `game_results` row for them (this
    family's own adapter never writes `game_results` -- see module
    docstring -- so this snapshot exists only to PROVE that stays true,
    never to interpret it). Read-only: opens a session, queries, closes.
    """
    if not game_ids:
        return frozenset(), {}
    session = session_factory()
    try:
        existing_games = frozenset(
            row.game_id for row in session.query(models.Game.game_id).filter(models.Game.game_id.in_(game_ids)).all()
        )
        result_rows = session.query(game_log_models.GameResult).filter(game_log_models.GameResult.game_id.in_(game_ids)).all()
    finally:
        session.close()
    results = {row.game_id: (row.result_status, row.source_count) for row in result_rows}
    return existing_games, results


def _verify_reconciliation(
    game_ids: frozenset[int],
    games_before: frozenset[int], games_after: frozenset[int],
    results_before: dict[int, tuple[str, int]], results_after: dict[int, tuple[str, int]],
) -> str | None:
    """
    Proves, for every `game_id` this one artifact's own plan touched:

    - A `games` row exists for it AFTER this artifact's own commit
      (whether newly created by this commit or already present from an
      earlier commit -- this runner never tries to tell those apart, per
      "do not assume persisted observation count equals newly inserted
      Game count").
    - `game_results` for that same `game_id` is byte-identical before and
      after this commit -- this family's own adapter never writes
      `game_results` at all, so any observed change would mean something
      outside this adapter's own documented behavior touched it.

    Returns `None` if every touched game_id satisfies both; otherwise a
    description of the first mismatch found.
    """
    for game_id in sorted(game_ids):
        if game_id not in games_after:
            return f"game_id {game_id}: expected a games row to exist after this artifact's own commit, found none"
        before_result = results_before.get(game_id)
        after_result = results_after.get(game_id)
        if before_result != after_result:
            return (
                f"game_id {game_id}: game_results changed from {before_result!r} to {after_result!r} across this "
                f"artifact's own commit -- the player game-log adapter never writes game_results, so this is unexpected"
            )
    return None


def _artifact_identity_text(artifact: PlayerGameLogCommitArtifact) -> str:
    return f"{artifact.discovered.relative_path}:{artifact.content_hash!r}"


def _record_artifact_failure(session_factory, *, run_id: str, artifact: PlayerGameLogCommitArtifact, issue_code: str, exc: Exception) -> None:
    """
    The design document section 9 failure-recording contract: a separate,
    immediately-following `unit_of_work` records exactly one
    `data_quality_issues` row identifying the failed player-season and
    exception, `source_artifact_id=None` (no artifact row exists for a
    failed/rolled-back artifact). Only ever field values already known to
    this module (team_slug, player_slug, season, relative path, content
    hash, exception type/message) -- never a credential, absolute path,
    or source file content.
    """
    context = (
        f"team_slug={artifact.team_slug!r} player_slug={artifact.player_slug!r} season={artifact.season!r} "
        f"file=[{_artifact_identity_text(artifact)}] exception_type={type(exc).__name__!r} exception_message={str(exc)!r}"
    )
    with unit_of_work(session_factory) as session:
        DataQualityIssueRepository(session).append(
            issue_code=issue_code,
            severity="critical",
            description=f"Player game-log bulk-import artifact failed: team_slug={artifact.team_slug!r}, player_slug={artifact.player_slug!r}, season={artifact.season!r}",
            pipeline_run_id=run_id,
            context=context,
        )


def run_player_game_log_committed_import(
    *,
    connection: Connection,
    target_designation: DatabaseTargetDesignation,
    session_factory,
    manifest: DryRunManifest,
    manifest_schema_version: str,
    artifacts: Sequence[PlayerGameLogCommitArtifact],
    run_id: str,
    started_at: dt.datetime,
    pipeline_name: str = "bulk_import_player_game_log",
) -> PlayerGameLogCommittedImportRunResult:
    """
    Commit `artifacts` (already-parsed, already-planned player-season
    game-log files) through the existing, unmodified
    `persist_player_game_log_import_plan`, under a verified database
    target and a held advisory lock, for the entire call.

    Lifecycle (docs/controlled_bulk_import_design.md section 16 item 7):

    1. Verify `connection` targets the exact database named by
       `target_designation` (`verify_database_target` -- existing, unmodified).
    2. Acquire the fail-fast advisory lock on `connection` for the
       verified target (`bulk_import_advisory_lock` -- existing,
       unmodified). Held for the remainder of this call; always released
       on every exit path.
    3. Validate `manifest` (schema version, exactly one
       `"player_game_log"` family, current adapter contract version) and
       every artifact against it (complete manifest/artifact
       correspondence, source-root-key match, stale-content-hash refusal,
       committable-status refusal), before any write.
    4. Create the `pipeline_runs` row (`status="running"`) through the
       existing `PipelineRunRepository`.
    5. For each artifact, in deterministic `(team_slug, player_slug,
       season)` order: read this artifact's own touched game_ids' `games`/
       `game_results` state and the family's own observation-row count
       before, call `persist_player_game_log_import_plan` (its own single
       `unit_of_work` either fully commits or fully rolls back), read
       both again after, and verify: the observation-count delta matches
       the plan's own count, every touched game_id now has a `games` row,
       and `game_results` for every touched game_id is unchanged (this
       adapter never writes it).
       - On a persist-time exception: record failure evidence in a
         separate clean transaction, mark this artifact `"failed"`, stop
         processing further artifacts.
       - On a reconciliation-mismatch (the persist call itself raised
         nothing, but the independently-read state disagrees): record
         failure evidence under a distinct issue_code, mark this artifact
         `"failed"`, stop processing further artifacts. This artifact's
         own rows are not rolled back here (they are already committed
         and real) -- only the run's own final status is prevented from
         ever reporting a false success.
       - On success: mark this artifact `"committed"`, continue.
    6. Finalize the pipeline run: `"succeeded"` only if every supplied
       artifact committed and reconciled; `"failed"` the moment any
       artifact failed. Never left `"running"`.
    7. Release the advisory lock (guaranteed by `bulk_import_advisory_lock`'s
       own `finally`, on every exit path including an exception raised
       from this function itself).
    """
    verified_target = verify_database_target(connection, target_designation)

    with bulk_import_advisory_lock(connection, verified_target):
        family_plan = _validate_manifest(manifest, manifest_schema_version)
        ordered_artifacts = _validate_artifacts(family_plan, artifacts)

        with unit_of_work(session_factory) as session:
            PipelineRunRepository(session).start(
                run_id=run_id, pipeline_name=pipeline_name, started_at=started_at, status="running",
            )

        outcomes: list[PlayerGameLogArtifactOutcome] = []
        run_failed = False

        for artifact in ordered_artifacts:
            before_observations = _player_game_log_observations_persisted(session_factory, run_id)
            game_ids = artifact.touched_game_ids
            games_before, results_before = _games_and_results_snapshot(session_factory, game_ids)

            try:
                result = persist_player_game_log_import_plan(
                    artifact.plan, session_factory,
                    discovered=artifact.discovered, content_hash=artifact.content_hash,
                    pipeline_run_id=run_id,
                )
            except Exception as exc:
                _record_artifact_failure(session_factory, run_id=run_id, artifact=artifact, issue_code=FAILED_ARTIFACT_ISSUE_CODE, exc=exc)
                outcomes.append(PlayerGameLogArtifactOutcome(team_slug=artifact.team_slug, player_slug=artifact.player_slug, season=artifact.season, status="failed", error=f"{type(exc).__name__}: {exc}"))
                run_failed = True
                break

            after_observations = _player_game_log_observations_persisted(session_factory, run_id)
            observed_delta = after_observations - before_observations
            expected_delta = artifact.plan.counts.total_source_rows

            mismatch: str | None = None
            if observed_delta != expected_delta:
                mismatch = (
                    f"observation-count mismatch for {artifact.identity}: expected a "
                    f"player_game_log_observations delta of {expected_delta}, observed {observed_delta}"
                )
            else:
                games_after, results_after = _games_and_results_snapshot(session_factory, game_ids)
                mismatch = _verify_reconciliation(game_ids, games_before, games_after, results_before, results_after)
                if mismatch is not None:
                    mismatch = f"reconciliation mismatch for {artifact.identity}: {mismatch}"

            if mismatch is not None:
                mismatch_error = ValidationError(
                    f"{mismatch} -- this artifact's own write already committed and cannot be undone; marking the "
                    f"run failed rather than reporting a false success"
                )
                _record_artifact_failure(session_factory, run_id=run_id, artifact=artifact, issue_code=RECONCILIATION_MISMATCH_ISSUE_CODE, exc=mismatch_error)
                outcomes.append(PlayerGameLogArtifactOutcome(team_slug=artifact.team_slug, player_slug=artifact.player_slug, season=artifact.season, status="failed", result=result, error=str(mismatch_error)))
                run_failed = True
                break

            outcomes.append(PlayerGameLogArtifactOutcome(team_slug=artifact.team_slug, player_slug=artifact.player_slug, season=artifact.season, status="committed", result=result))

        final_status = "failed" if run_failed else "succeeded"
        with unit_of_work(session_factory) as session:
            PipelineRunRepository(session).transition(run_id=run_id, new_status=final_status, completed_at=dt.datetime.now(dt.timezone.utc))

        return PlayerGameLogCommittedImportRunResult(run_id=run_id, status=final_status, artifact_outcomes=tuple(outcomes))
