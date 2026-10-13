"""
Controlled bulk-import design, Slice 6 (docs/controlled_bulk_import_design.md
section 16 item 6, approved for this slice only): the team game-log
single-source-family committed-import runner.

**This slice does not authorize a real import.** Every automated test for
this module runs only against synthetic, in-memory team game-log
fixtures and the established disposable PostgreSQL container
(`tests/postgres/conftest.py`). This module never discovers, scans, or
reads `TeamDump` itself, never connects to a database on its own (every
connection/session is caller-supplied), and never invokes the team
game-log parser directly -- it calls the existing, unmodified
`persist_game_log_import_plan` and nothing else touches the database.

Artifact-unit precheck (done before writing any code, per instruction):
unlike AllStats (Slice 5, corrected to a team-season *workbook* unit
after direct inspection showed the real adapter bundles up to six
files), direct inspection of `persist_game_log_import_plan` here
confirms the design's own stated model is CORRECT for this family --
the function accepts exactly one `DiscoveredGameLogFile` (one CSV) and
one `content_hash: str` (singular, never a dict of labels), and commits
that one file inside exactly one `unit_of_work`. No contradiction was
found; `TeamGameLogCommitArtifact` below is defined at that one-file
granularity, matching the design document's own §4 table without any
correction needed for this family.

Same leaf-level "team_slug is a slug, not a display name" finding as
Slice 5 applies here too: `discovered.team_slug` (the directory slug,
e.g. `"michigan-state"`) and `plan.team_slug` (the parse-time
`team_query` display name, e.g. `"Michigan State"`) are legitimately
different strings with no fixed transform between them -- confirmed by
direct inspection of `parse_game_log_workbook`, which sets
`ParsedGameLogFile.team_slug = team_query` verbatim. `TeamGameLogCommitArtifact.team_slug`
is derived from `discovered`, never compared against `plan.team_slug`.

Deliberate divergence from the Slice 5 AllStats runner (both remain
independent, single-family runners -- this is not a shared framework):
Slice 5 allowed a caller to commit a strict subset of an approved
manifest's own artifacts in one call (a legitimate "commit in chunks
across multiple calls" allowance). This slice's own approval explicitly
requires strict, complete correspondence instead: every artifact the
manifest approves must be supplied in one call ("missing planned
artifacts" is refused, not merely "not yet processed"), and every
supplied artifact must be one the manifest approved ("unplanned
artifacts" is refused). This is an intentional, instruction-driven
per-slice choice, not an inconsistency to reconcile later.

This slice also validates the manifest's own `PlannedArtifact.source_root_key`
against `EXPECTED_SOURCE_ROOT_KEY` ("teamdump_cbb") -- the literal,
hardcoded value `persist_game_log_import_plan` itself always writes to
`source_artifacts.source_root_key` (confirmed by direct inspection, not
a caller-configurable parameter) -- and restricts a committable
artifact's own manifest `status` to `{"planned_insert", "idempotent_match"}`
(an `"unresolved"`/`"ambiguous"`/`"conflict"` disposition is a dry-run
data-quality finding, never eligible for an actual commit).

GAMES/GAME_RESULTS RECONCILIATION -- the first committed runner to touch
shared, cross-artifact state. `games`/`game_results` are NOT scoped to
one artifact's own `source_artifact_id` the way AllStats' three tables
are: `GameResult` is keyed by `game_id` alone (one row per canonical
game, mutable via `GameResultRepository.upsert`, confirmed by direct
inspection of `src/db/game_log_repositories.py`) and is expected to
transition -- `single_sided_provisional` (`source_count=1`, one team's
file imported) -> `reconciled` or `conflicting` (`source_count=2`, the
mirrored team's file now also imported) -- as a LATER, unrelated
artifact's own commit updates the SAME row a PRIOR artifact created. A
before/after ROW-COUNT delta (the AllStats pattern) cannot prove this:
"rows inserted" does not capture an existing row silently changing
state. Per the explicit instruction not to fabricate parity from row
counts and not to invent a new schema field/migration/reconciliation
policy for this, this module instead reads the exact, already-existing
`GameResult` columns (`result_status`, `source_count`) for precisely the
`game_id`s this one artifact's own plan references, before and after its
persist call, and proves the observed state transition is one of the
three the existing adapter's own `reconcile_game_observations` can ever
produce (see `_verify_game_result_transitions` below) -- using only
already-established schema/adapter semantics, no new ones. This is a
smaller, more precise proof than a whole-run aggregate for exactly this
purpose, so it is implemented as a direct, explicit, read-only query in
this module rather than as an addition to the shared, separately-owned
Slice 3 `bulk_import_reconciliation.py` (which is not modified this
slice). `team_game_log_observations`' own row count IS scoped per run
(via the existing Slice 3 `read_source_family_reconciliation`, same
before/after delta pattern as AllStats) and is reconciled that way.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Sequence

from sqlalchemy.engine import Connection

from src.db import game_log_models
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
from src.pipeline.game_log_import_adapter import (
    TEAM_GAMELOG_IMPORT_CONTRACT_VERSION,
    DiscoveredGameLogFile,
    GameLogImportPlan,
    GameLogImportResult,
    persist_game_log_import_plan,
)

__all__ = [
    "FAILED_ARTIFACT_ISSUE_CODE",
    "RECONCILIATION_MISMATCH_ISSUE_CODE",
    "EXPECTED_SOURCE_ROOT_KEY",
    "COMMITTABLE_MANIFEST_STATUSES",
    "TeamGameLogCommitArtifact",
    "TeamGameLogArtifactOutcome",
    "TeamGameLogCommittedImportRunResult",
    "run_team_game_log_committed_import",
]

#: Verbatim from docs/controlled_bulk_import_design.md section 9 -- the
#: same fixed issue_code Slice 5's own runner uses, intentionally
#: duplicated here (not imported from that module) since both runners
#: independently derive it from the same approved design text, not from
#: each other -- no cross-runner coupling.
FAILED_ARTIFACT_ISSUE_CODE = "bulk_import_artifact_failed"

#: This runner's own issue_code for the (expected-to-be-unreachable in
#: correct operation) case where a just-committed artifact's independently
#: re-read game/game-result state does not match one of the three
#: transitions `reconcile_game_observations` can ever produce.
RECONCILIATION_MISMATCH_ISSUE_CODE = "bulk_import_reconciliation_mismatch"

#: The literal, hardcoded `source_root_key` value `persist_game_log_import_plan`
#: itself always writes (confirmed by direct inspection -- not a caller-
#: configurable parameter of that function). A manifest entry naming any
#: other value could never actually match what gets persisted.
EXPECTED_SOURCE_ROOT_KEY = "teamdump_cbb"

#: A dry-run manifest disposition eligible for an actual commit. An
#: `"unresolved"`/`"ambiguous"`/`"conflict"` status is a recorded data-
#: quality finding from the dry run, never something this runner commits.
COMMITTABLE_MANIFEST_STATUSES = frozenset({"planned_insert", "idempotent_match"})

#: Verbatim from src/db/enums.py's own GAME_RESULT_STATUSES -- repeated
#: here only as the fixed vocabulary this module's own transition proof
#: checks against, never a second, competing definition.
_GAME_RESULT_STATUSES = frozenset({"reconciled", "single_sided_provisional", "conflicting"})


@dataclass(frozen=True)
class TeamGameLogCommitArtifact:
    """
    One caller-supplied, already-parsed-and-planned team-season game-log
    file, ready to commit. Confirmed (see module docstring) to be the
    real, unmodified adapter's own atomic transaction unit for this
    family -- no artifact-granularity correction needed here.

    `team_slug`/`season` are derived from `discovered` (never taken as
    separate constructor arguments, and never cross-checked against
    `plan.team_slug` -- see module docstring for why that comparison
    would reject the adapter's own real, correct usage pattern).
    """

    discovered: DiscoveredGameLogFile
    content_hash: str
    plan: GameLogImportPlan

    def __post_init__(self) -> None:
        if not isinstance(self.discovered, DiscoveredGameLogFile):
            raise ValidationError("discovered must be a DiscoveredGameLogFile produced by discover_game_log_artifacts")
        if not isinstance(self.content_hash, str) or not self.content_hash:
            raise ValidationError("content_hash must be a non-empty string")
        if not isinstance(self.plan, GameLogImportPlan):
            raise ValidationError("plan must be a GameLogImportPlan produced by build_game_log_import_plan")
        if self.plan.season != self.discovered.season:
            raise ValidationError(
                f"plan season {self.plan.season!r} does not match discovered file season {self.discovered.season!r} "
                f"for team_slug={self.discovered.team_slug!r} -- refusing a mismatched discovered/plan pairing"
            )

    @property
    def team_slug(self) -> str:
        return self.discovered.team_slug

    @property
    def season(self) -> int:
        return self.discovered.season

    @property
    def identity(self) -> tuple[str, int]:
        """The `(team_slug, season)` pair this artifact would occupy -- this runner's own duplicate/ordering key."""
        return (self.team_slug, self.season)

    @property
    def touched_game_ids(self) -> frozenset[int]:
        return frozenset(spec["game_id"] for spec in self.plan.observation_specs if spec["game_id"] is not None)

    @property
    def touched_game_sides(self) -> dict[int, str]:
        """
        Maps every `game_id` this artifact's own plan touches to which
        canonical side (`"team_a"`/`"team_b"`, per the same `sorted((team_id,
        opponent_team_id))` pairing `persist_game_log_import_plan` itself
        uses) THIS artifact's own perspective team occupies. Requires a
        resolved `plan.team_id` -- empty when unresolved (matching
        `touched_game_ids` being empty in that same case, since an
        unresolved team/season never produces a `game_id` in the first
        place).
        """
        if self.plan.team_id is None:
            return {}
        sides: dict[int, str] = {}
        for spec in self.plan.observation_specs:
            game_id = spec["game_id"]
            opponent_team_id = spec["opponent_team_id"]
            if game_id is None or opponent_team_id is None:
                continue
            team_a_id, _ = sorted((self.plan.team_id, opponent_team_id))
            sides[game_id] = "team_a" if self.plan.team_id == team_a_id else "team_b"
        return sides


@dataclass(frozen=True)
class TeamGameLogArtifactOutcome:
    """One artifact's own outcome within a committed-import run. `result` is populated only when `status == "committed"`."""

    team_slug: str
    season: int
    status: str  # "committed" | "failed"
    result: GameLogImportResult | None = None
    error: str | None = None


@dataclass(frozen=True)
class TeamGameLogCommittedImportRunResult:
    """
    The immutable, final outcome of one `run_team_game_log_committed_import`
    call. `status == "succeeded"` only when every supplied artifact was
    committed and independently reconciled -- a run that stopped early on
    any artifact failure always reports `"failed"`, never a partial
    success.
    """

    run_id: str
    status: str  # "succeeded" | "failed"
    artifact_outcomes: tuple[TeamGameLogArtifactOutcome, ...] = field(default_factory=tuple)

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
            f"this runner accepts only a single-family team_game_log manifest, got {len(manifest.families)} "
            f"families ({[p.family for p in manifest.families]}) -- refusing an empty or mixed-family manifest"
        )
    family_plan = manifest.families[0]
    if family_plan.family != "team_game_log":
        raise ValidationError(f"this runner only ever commits the 'team_game_log' family, got {family_plan.family!r}")
    if family_plan.contract_version != TEAM_GAMELOG_IMPORT_CONTRACT_VERSION:
        raise ValidationError(
            f"manifest team_game_log contract_version {family_plan.contract_version!r} does not match the current "
            f"team game-log adapter contract version {TEAM_GAMELOG_IMPORT_CONTRACT_VERSION!r} -- refusing a stale "
            f"manifest (the adapter's own contract changed since this manifest was built)"
        )
    return family_plan


def _validate_artifacts(family_plan: SourceFamilyPlan, artifacts: Sequence[TeamGameLogCommitArtifact]) -> tuple[TeamGameLogCommitArtifact, ...]:
    if isinstance(artifacts, (str, bytes)) or not isinstance(artifacts, Sequence):
        raise ValidationError("artifacts must be a sequence of TeamGameLogCommitArtifact")
    for artifact in artifacts:
        if not isinstance(artifact, TeamGameLogCommitArtifact):
            raise ValidationError(f"every artifact must be a TeamGameLogCommitArtifact, got {type(artifact).__name__}")

    seen_identities: set[tuple[str, int]] = set()
    for artifact in artifacts:
        if artifact.identity in seen_identities:
            raise ValidationError(f"duplicate artifact for team-season {artifact.identity} -- refusing to process the same team-season twice in one run")
        seen_identities.add(artifact.identity)

    manifest_by_relpath = {planned.relative_path: planned for planned in family_plan.artifacts}
    artifact_by_relpath = {artifact.discovered.relative_path: artifact for artifact in artifacts}

    for artifact in artifacts:
        if artifact.discovered.relative_path not in manifest_by_relpath:
            raise ValidationError(
                f"unplanned artifact: {artifact.discovered.relative_path!r} (team-season {artifact.identity}) is not "
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
                f"{planned.source_root_key!r}, but the team game-log adapter always persists under "
                f"{EXPECTED_SOURCE_ROOT_KEY!r} -- refusing a manifest that could never match what gets written"
            )
        if planned.content_hash != artifact.content_hash:
            raise ValidationError(
                f"artifact file {artifact.discovered.relative_path!r} (team-season {artifact.identity}) has "
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


def _team_game_log_observations_persisted(session_factory, run_id: str) -> int:
    """Independent, database-read row count for `team_game_log_observations` currently tagged to `run_id`, via the existing Slice 3 reconciliation reader. Read-only: opens a session, queries, closes -- never commits or writes."""
    session = session_factory()
    try:
        report = read_source_family_reconciliation(session, run_id, "team_game_log")
    finally:
        session.close()
    return report.tables[0].persisted_rows


def _game_result_snapshot(session_factory, game_ids: frozenset[int]) -> dict[int, tuple[str, int, bool, bool]]:
    """
    Independent, database-read snapshot of `game_results` for exactly
    `game_ids` -- `(result_status, source_count, team_a_observation_id is
    not None, team_b_observation_id is not None)` per game_id currently
    present. A `game_id` with no row yet is simply absent from the
    returned dict. Read-only: opens a session, queries, closes.
    """
    if not game_ids:
        return {}
    session = session_factory()
    try:
        rows = session.query(game_log_models.GameResult).filter(game_log_models.GameResult.game_id.in_(game_ids)).all()
    finally:
        session.close()
    return {
        row.game_id: (row.result_status, row.source_count, row.team_a_observation_id is not None, row.team_b_observation_id is not None)
        for row in rows
    }


def _verify_game_result_transitions(
    game_sides: dict[int, str], before: dict[int, tuple[str, int, bool, bool]], after: dict[int, tuple[str, int, bool, bool]],
) -> str | None:
    """
    Proves, for every `game_id` this one artifact's own plan touched
    (`game_sides` maps each to `"team_a"`/`"team_b"` -- which canonical
    side THIS artifact's own perspective team occupies), that the
    observed before/after `game_results` state is one this family's own,
    existing, unmodified `reconcile_game_observations` could actually
    produce. Side-aware (not just a `source_count` transition) because a
    single artifact-identity check alone cannot tell apart two genuinely
    different single-sided-staying-single-sided cases from a real
    mirrored-convergence case -- see the three branches below:

    - The OTHER side was not present, and still is not -> stays
      `source_count=1`, `"single_sided_provisional"`, with only THIS
      side's own observation-id populated. Covers both "first-ever import
      of this game" (no prior row) and "this same team re-imports a
      changed/different snapshot of its own side" (a prior row already
      existed, but only for this same side) -- both are legitimate,
      neither is a mirrored convergence.
    - The OTHER side WAS already present (with or without this side) ->
      now must be `source_count=2`, `"reconciled"` or `"conflicting"`,
      with BOTH sides' own observation-ids populated.

    Returns `None` if every touched game_id's own transition matches one
    of the above; otherwise a description of the first mismatch found
    (never fabricated from a row-count delta -- every check here reads
    the exact `result_status`/`source_count`/observation-id-presence
    columns the adapter itself writes).
    """
    for game_id in sorted(game_sides):
        my_side = game_sides[game_id]
        after_state = after.get(game_id)
        if after_state is None:
            return f"game_id {game_id}: expected a game_results row to exist after this artifact's own commit, found none"
        after_status, after_count, after_a, after_b = after_state
        if after_status not in _GAME_RESULT_STATUSES:
            return f"game_id {game_id}: after-commit result_status {after_status!r} is not a recognized value"
        if after_count not in (1, 2):
            return f"game_id {game_id}: after-commit source_count {after_count!r} is not 1 or 2"
        if int(after_a) + int(after_b) != after_count:
            return f"game_id {game_id}: after-commit observation-id presence ({after_a}, {after_b}) does not match source_count {after_count!r}"
        after_my_side_populated = after_a if my_side == "team_a" else after_b
        if not after_my_side_populated:
            return f"game_id {game_id}: this artifact's own side ({my_side}) is not populated on the row it just wrote to"

        before_state = before.get(game_id)
        before_other_side_populated = False
        if before_state is not None:
            _, _, before_a, before_b = before_state
            before_other_side_populated = before_b if my_side == "team_a" else before_a

        if before_other_side_populated:
            if not (after_count == 2 and after_status in ("reconciled", "conflicting")):
                return f"game_id {game_id}: the other side was already present, but this commit did not converge to a mirrored reconciled/conflicting row (observed {after_state!r})"
            if before_state is not None and before_state[1] == 2 and after_state != before_state:
                return f"game_id {game_id}: an already-mirrored row (source_count=2) changed on what should be an idempotent replay -- before={before_state!r}, after={after_state!r}"
        else:
            if not (after_count == 1 and after_status == "single_sided_provisional"):
                return f"game_id {game_id}: the other side was not present, but this commit produced a non-single-sided row (observed {after_state!r})"
    return None


def _artifact_identity_text(artifact: TeamGameLogCommitArtifact) -> str:
    return f"{artifact.discovered.relative_path}:{artifact.content_hash!r}"


def _record_artifact_failure(session_factory, *, run_id: str, artifact: TeamGameLogCommitArtifact, issue_code: str, exc: Exception) -> None:
    """
    The design document section 9 failure-recording contract: a separate,
    immediately-following `unit_of_work` records exactly one
    `data_quality_issues` row identifying the failed team-season and
    exception, `source_artifact_id=None` (no artifact row exists for a
    failed/rolled-back artifact). Only ever field values already known to
    this module (team_slug, season, relative path, content hash,
    exception type/message) -- never a credential, absolute path, or
    source file content.
    """
    context = (
        f"team_slug={artifact.team_slug!r} season={artifact.season!r} file=[{_artifact_identity_text(artifact)}] "
        f"exception_type={type(exc).__name__!r} exception_message={str(exc)!r}"
    )
    with unit_of_work(session_factory) as session:
        DataQualityIssueRepository(session).append(
            issue_code=issue_code,
            severity="critical",
            description=f"Team game-log bulk-import artifact failed: team_slug={artifact.team_slug!r}, season={artifact.season!r}",
            pipeline_run_id=run_id,
            context=context,
        )


def run_team_game_log_committed_import(
    *,
    connection: Connection,
    target_designation: DatabaseTargetDesignation,
    session_factory,
    manifest: DryRunManifest,
    manifest_schema_version: str,
    artifacts: Sequence[TeamGameLogCommitArtifact],
    run_id: str,
    started_at: dt.datetime,
    pipeline_name: str = "bulk_import_team_game_log",
) -> TeamGameLogCommittedImportRunResult:
    """
    Commit `artifacts` (already-parsed, already-planned team game-log
    files) through the existing, unmodified `persist_game_log_import_plan`,
    under a verified database target and a held advisory lock, for the
    entire call.

    Lifecycle (docs/controlled_bulk_import_design.md section 16 item 6):

    1. Verify `connection` targets the exact database named by
       `target_designation` (`verify_database_target` -- existing, unmodified).
    2. Acquire the fail-fast advisory lock on `connection` for the
       verified target (`bulk_import_advisory_lock` -- existing,
       unmodified). Held for the remainder of this call; always released
       on every exit path.
    3. Validate `manifest` (schema version, exactly one `"team_game_log"`
       family, current adapter contract version) and every artifact
       against it (complete manifest/artifact correspondence, source-root-key
       match, stale-content-hash refusal, committable-status refusal),
       before any write.
    4. Create the `pipeline_runs` row (`status="running"`) through the
       existing `PipelineRunRepository`.
    5. For each artifact, in deterministic `(team_slug, season)` order:
       read this artifact's own touched game_ids' `game_results` state
       and the family's own observation-row count before, call
       `persist_game_log_import_plan` (its own single `unit_of_work`
       either fully commits or fully rolls back), read both again after,
       and verify: the observation-count delta matches the plan's own
       count, and every touched game_id's own `game_results` transition
       is one `reconcile_game_observations` could actually produce
       (single-sided creation, mirrored convergence to
       reconciled/conflicting, or an unchanged idempotent replay).
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

        outcomes: list[TeamGameLogArtifactOutcome] = []
        run_failed = False

        for artifact in ordered_artifacts:
            before_observations = _team_game_log_observations_persisted(session_factory, run_id)
            game_sides = artifact.touched_game_sides
            before_results = _game_result_snapshot(session_factory, frozenset(game_sides))

            try:
                result = persist_game_log_import_plan(
                    artifact.plan, session_factory,
                    discovered=artifact.discovered, content_hash=artifact.content_hash,
                    pipeline_run_id=run_id,
                )
            except Exception as exc:
                _record_artifact_failure(session_factory, run_id=run_id, artifact=artifact, issue_code=FAILED_ARTIFACT_ISSUE_CODE, exc=exc)
                outcomes.append(TeamGameLogArtifactOutcome(team_slug=artifact.team_slug, season=artifact.season, status="failed", error=f"{type(exc).__name__}: {exc}"))
                run_failed = True
                break

            after_observations = _team_game_log_observations_persisted(session_factory, run_id)
            observed_delta = after_observations - before_observations
            expected_delta = artifact.plan.counts.genuine_observations

            mismatch: str | None = None
            if observed_delta != expected_delta:
                mismatch = (
                    f"observation-count mismatch for team-season {artifact.identity}: expected a "
                    f"team_game_log_observations delta of {expected_delta}, observed {observed_delta}"
                )
            else:
                after_results = _game_result_snapshot(session_factory, frozenset(game_sides))
                mismatch = _verify_game_result_transitions(game_sides, before_results, after_results)
                if mismatch is not None:
                    mismatch = f"reconciliation mismatch for team-season {artifact.identity}: {mismatch}"

            if mismatch is not None:
                mismatch_error = ValidationError(
                    f"{mismatch} -- this artifact's own write already committed and cannot be undone; marking the "
                    f"run failed rather than reporting a false success"
                )
                _record_artifact_failure(session_factory, run_id=run_id, artifact=artifact, issue_code=RECONCILIATION_MISMATCH_ISSUE_CODE, exc=mismatch_error)
                outcomes.append(TeamGameLogArtifactOutcome(team_slug=artifact.team_slug, season=artifact.season, status="failed", result=result, error=str(mismatch_error)))
                run_failed = True
                break

            outcomes.append(TeamGameLogArtifactOutcome(team_slug=artifact.team_slug, season=artifact.season, status="committed", result=result))

        final_status = "failed" if run_failed else "succeeded"
        with unit_of_work(session_factory) as session:
            PipelineRunRepository(session).transition(run_id=run_id, new_status=final_status, completed_at=dt.datetime.now(dt.timezone.utc))

        return TeamGameLogCommittedImportRunResult(run_id=run_id, status=final_status, artifact_outcomes=tuple(outcomes))
