"""
Controlled bulk-import design, Slice 5 (docs/controlled_bulk_import_design.md
section 16 item 5, approved for this slice only): the AllStats
single-source-family committed-import runner.

**This slice does not authorize a real import.** Every automated test for
this module runs only against synthetic, in-memory AllStats fixtures and
the established disposable PostgreSQL container (`tests/postgres/conftest.py`).
This module never discovers, scans, or reads `TeamDump` itself, never
connects to a database on its own (every connection/session is caller-
supplied), and never invokes the AllStats parser or repositories directly
-- it calls the existing, unmodified `persist_allstats_import_plan` and
nothing else touches the database.

Corrected artifact-transaction-unit finding (approved by the user before
this slice's implementation began): `docs/controlled_bulk_import_design.md`
section 4 states "Artifact (one file) is the atomic transaction unit ...
unchanged from the existing per-adapter design" and separately calls a
team-season collection "never a transaction boundary." Direct inspection
of the actual, unmodified `persist_allstats_import_plan` shows this is
factually wrong for AllStats: that function accepts one whole team-season
`DiscoveredAllStatsWorkbook` (up to six files -- Roster/PerGame/Totals/
Per40/Per100/Advanced) and one jointly-parsed `AllStatsImportPlan`, and
commits all of it inside exactly one `unit_of_work`. The real, unmodified
AllStats atomic transaction unit is therefore the **team-season workbook**,
not the individual file. This module's own "artifact" (`AllStatsCommitArtifact`)
is defined at that corrected granularity -- a documented correction to
section 4's own AllStats row, not a new invented behavior. The Slice 2
dry-run manifest stays file-granular (`PlannedArtifact` is one file); this
module cross-validates every file inside one `AllStatsCommitArtifact`
against its own matching `PlannedArtifact` row in the manifest by
`relative_path`.

Two leaf-level conventions this module establishes, in the same spirit as
Slice 2's own documented "smallest safe reading" decisions (neither is a
new cross-cutting architectural choice):

1. **`manifest_schema_version` is caller-supplied, not read off the
   manifest object.** `DryRunManifest` itself carries no `schema_version`
   field (only `serialize_manifest` embeds the constant when writing
   JSON) -- so a caller re-consuming an in-memory manifest is the only
   party that can state what schema version it was built under. This
   runner re-checks that claim against the live `MANIFEST_SCHEMA_VERSION`
   constant before doing anything else.
2. **This runner accepts only a single-family manifest whose one family
   is `"allstats"`.** A multi-family manifest (the normal shape for a
   full four-family bulk import) is refused outright -- this runner never
   inspects, orchestrates around, or even looks at what a non-AllStats
   family entry would mean.

Key, already-proven facts this design relies on (re-confirmed by direct
inspection this slice, not assumed):

- `persist_allstats_import_plan` already opens exactly one `unit_of_work`
  per call -- this runner never wraps a second transaction around it, only
  sequences calls to it and handles failure/rollback recording around it.
- `SourceArtifactRepository.upsert`'s natural key is
  `(source_root_key, relative_path, content_hash)`; `RosterMembershipRepository`
  /`PlayerStatSnapshotRepository`/`TeamStatSnapshotRepository` all key
  their own natural key off that same `source_artifact_id`. Replaying an
  identical artifact therefore lands on the exact same rows (no
  duplication, upsert no-ops); a changed `content_hash` for the same
  `relative_path` creates a brand-new `SourceArtifact` row and a
  brand-new, coexisting set of observation rows. Both properties are
  already-existing, already-tested repository behavior -- this runner
  relies on them, never re-implements them.
- `DataQualityIssueRepository.append` is append-only (by design, for
  audit history) -- a reprocessed artifact's own data-quality issues are
  expected to accumulate on replay; this is a documented, pre-existing
  repository property, not a defect this runner works around.
- `docs/controlled_bulk_import_design.md` section 9 names the exact
  failure-recording contract this module implements: a **separate**,
  immediately-following `unit_of_work` records one `data_quality_issues`
  row with `severity="critical"`, `issue_code="bulk_import_artifact_failed"`,
  `pipeline_run_id` set, `source_artifact_id=None` (no artifact row exists
  to point to -- a documented, accepted schema gap, not fixed here), and a
  `context` string identifying every file in the failed artifact plus the
  exception's own type and message.

Per-artifact reconciliation ("reconcile expected and persisted counts",
the Slice 5 transaction-lifecycle requirement) is computed as a genuine,
independent before/after delta read via the existing, unmodified Slice 3
`read_source_family_reconciliation` reader -- never a comparison of
`AllStatsImportResult`'s own written-count fields against themselves
(those are computed as `len(plan.*_specs)`, i.e. tautological, not an
independent database read). Because `SourceArtifactRepository.upsert`
always (re)tags a touched artifact's `pipeline_run_id` to the *current*
call's `run_id` -- whether the underlying row was newly inserted or
idempotently re-touched -- an after-minus-before delta scoped to this
`run_id` correctly isolates exactly this one artifact's contribution
regardless of prior-run history. No separate final "whole run" reconciliation
call is needed at successful finalization: by induction, if every
individual artifact's own delta matched its own planned counts, the
run's aggregate reconciliation already holds.

Caller obligations this module cannot itself verify (documented here,
proven at the call site / by test construction, not enforced in code):
`session_factory` must be bound to the exact same database as `connection`
(the one just verified and lock-held) -- there is no structural way for
this module to introspect a `sessionmaker`'s own bound engine identity
without adding new coupling this slice does not authorize.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Sequence

from sqlalchemy.engine import Connection

from src.db.repositories import DataQualityIssueRepository, PipelineRunRepository
from src.db.session import unit_of_work
from src.exceptions import ValidationError
from src.pipeline.allstats_import_adapter import (
    ALLSTATS_IMPORT_CONTRACT_VERSION,
    AllStatsImportPlan,
    AllStatsImportResult,
    DiscoveredAllStatsWorkbook,
    persist_allstats_import_plan,
)
from src.pipeline.bulk_import_manifest import MANIFEST_SCHEMA_VERSION, DryRunManifest, SourceFamilyPlan
from src.pipeline.bulk_import_reconciliation import read_source_family_reconciliation
from src.pipeline.bulk_import_safety import (
    DatabaseTargetDesignation,
    bulk_import_advisory_lock,
    verify_database_target,
)

__all__ = [
    "FAILED_ARTIFACT_ISSUE_CODE",
    "RECONCILIATION_MISMATCH_ISSUE_CODE",
    "AllStatsCommitArtifact",
    "AllStatsArtifactOutcome",
    "AllStatsCommittedImportRunResult",
    "run_allstats_committed_import",
]

#: Verbatim from docs/controlled_bulk_import_design.md section 9 -- never a
#: different or locally-invented issue_code for this exact failure shape.
FAILED_ARTIFACT_ISSUE_CODE = "bulk_import_artifact_failed"

#: This runner's own issue_code for the (expected-to-be-unreachable in
#: correct operation) case where a just-committed artifact's independently
#: re-read row counts do not match its own dry-run plan's counts. Distinct
#: from FAILED_ARTIFACT_ISSUE_CODE because, unlike a rolled-back artifact,
#: this artifact's own rows are real and already committed -- there is
#: nothing to roll back, only a run to correctly mark failed rather than
#: falsely successful.
RECONCILIATION_MISMATCH_ISSUE_CODE = "bulk_import_reconciliation_mismatch"


@dataclass(frozen=True)
class AllStatsCommitArtifact:
    """
    One caller-supplied, already-parsed-and-planned team-season AllStats
    workbook, ready to commit. This is the corrected artifact-transaction
    unit for AllStats (see module docstring) -- one call to
    `persist_allstats_import_plan` per instance of this class, never a
    smaller or larger unit.

    `team_slug`/`season` are derived from `discovered` (never taken as
    separate constructor arguments) -- `discovered.team_slug` is the
    directory-slug identity `discover_allstats_artifacts` itself assigns
    and the one every `PlannedArtifact.relative_path` in the manifest is
    built from, so it is the only correct identity to key this runner's
    own manifest cross-check and duplicate-detection off. Deliberately
    NOT cross-checked against `plan.team_slug`: that field is the
    parse-time `team_query` display name passed to `parse_allstats_workbook`
    (e.g. `"St. Bonaventure"`), which the existing, unmodified adapter's
    own test suite confirms is legitimately a different string from the
    directory slug (e.g. `"st-bonaventure"`) with no fixed transform
    between them -- comparing the two would reject the adapter's own
    real, correct usage pattern.

    Self-validating at construction: `plan.season` must match
    `discovered.season` (both are the same plain integer, no naming
    ambiguity), `content_hashes` must carry exactly one entry per file in
    `discovered.files` (no more, no fewer), and at least one file must be
    present.
    """

    discovered: DiscoveredAllStatsWorkbook
    content_hashes: dict[str, str]
    plan: AllStatsImportPlan

    def __post_init__(self) -> None:
        if not isinstance(self.discovered, DiscoveredAllStatsWorkbook):
            raise ValidationError("discovered must be a DiscoveredAllStatsWorkbook produced by discover_allstats_artifacts")
        if not self.discovered.files:
            raise ValidationError(f"artifact (team_slug={self.discovered.team_slug!r}, season={self.discovered.season!r}) has no files -- refusing an empty artifact")
        if not isinstance(self.content_hashes, dict) or set(self.content_hashes) != set(self.discovered.files):
            raise ValidationError(
                "content_hashes must be a dict[str, str] whose keys exactly match discovered.files' own labels "
                f"({sorted(self.discovered.files)}), got {sorted(self.content_hashes) if isinstance(self.content_hashes, dict) else type(self.content_hashes).__name__}"
            )
        if not all(isinstance(v, str) and v for v in self.content_hashes.values()):
            raise ValidationError("every content_hashes value must be a non-empty string")
        if not isinstance(self.plan, AllStatsImportPlan):
            raise ValidationError("plan must be an AllStatsImportPlan produced by build_allstats_import_plan")
        if self.plan.season != self.discovered.season:
            raise ValidationError(
                f"plan season {self.plan.season!r} does not match discovered workbook season {self.discovered.season!r} "
                f"for team_slug={self.discovered.team_slug!r} -- refusing a mismatched discovered/plan pairing"
            )
        object.__setattr__(self, "content_hashes", dict(self.content_hashes))

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


@dataclass(frozen=True)
class AllStatsArtifactOutcome:
    """One artifact's own outcome within a committed-import run. `result` is populated only when `status == "committed"`."""

    team_slug: str
    season: int
    status: str  # "committed" | "failed"
    result: AllStatsImportResult | None = None
    error: str | None = None


@dataclass(frozen=True)
class AllStatsCommittedImportRunResult:
    """
    The immutable, final outcome of one `run_allstats_committed_import`
    call. `status == "succeeded"` only when every supplied artifact was
    committed and independently reconciled -- a run that stopped early on
    any artifact failure always reports `"failed"`, never a partial
    success.
    """

    run_id: str
    status: str  # "succeeded" | "failed"
    artifact_outcomes: tuple[AllStatsArtifactOutcome, ...] = field(default_factory=tuple)

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
            f"this runner accepts only a single-family AllStats manifest, got {len(manifest.families)} "
            f"families ({[p.family for p in manifest.families]}) -- refusing an empty or mixed-family manifest"
        )
    family_plan = manifest.families[0]
    if family_plan.family != "allstats":
        raise ValidationError(f"this runner only ever commits the 'allstats' family, got {family_plan.family!r}")
    if family_plan.contract_version != ALLSTATS_IMPORT_CONTRACT_VERSION:
        raise ValidationError(
            f"manifest allstats contract_version {family_plan.contract_version!r} does not match the current "
            f"AllStats adapter contract version {ALLSTATS_IMPORT_CONTRACT_VERSION!r} -- refusing a stale "
            f"manifest (the adapter's own contract changed since this manifest was built)"
        )
    return family_plan


def _validate_artifacts(family_plan: SourceFamilyPlan, artifacts: Sequence[AllStatsCommitArtifact]) -> tuple[AllStatsCommitArtifact, ...]:
    if isinstance(artifacts, (str, bytes)) or not isinstance(artifacts, Sequence):
        raise ValidationError("artifacts must be a sequence of AllStatsCommitArtifact")
    for artifact in artifacts:
        if not isinstance(artifact, AllStatsCommitArtifact):
            raise ValidationError(f"every artifact must be an AllStatsCommitArtifact, got {type(artifact).__name__}")

    seen_identities: set[tuple[str, int]] = set()
    for artifact in artifacts:
        if artifact.identity in seen_identities:
            raise ValidationError(f"duplicate artifact for team-season {artifact.identity} -- refusing to process the same team-season twice in one run")
        seen_identities.add(artifact.identity)

    manifest_by_relpath = {planned.relative_path: planned for planned in family_plan.artifacts}
    for artifact in artifacts:
        for label, meta in artifact.discovered.files.items():
            planned = manifest_by_relpath.get(meta.relative_path)
            if planned is None:
                raise ValidationError(
                    f"artifact file {meta.relative_path!r} (team-season {artifact.identity}) is not present in "
                    f"the approved dry-run manifest -- refusing to commit a file that was never planned"
                )
            actual_hash = artifact.content_hashes[label]
            if planned.content_hash != actual_hash:
                raise ValidationError(
                    f"artifact file {meta.relative_path!r} (team-season {artifact.identity}) has content_hash "
                    f"{actual_hash!r}, but the approved dry-run manifest recorded {planned.content_hash!r} for "
                    f"this same file -- refusing a changed/stale artifact that no longer matches what was approved"
                )

    # Deterministic order -- never the caller's own input order.
    return tuple(sorted(artifacts, key=lambda a: a.identity))


def _allstats_persisted_counts(session_factory, run_id: str) -> tuple[int, int, int]:
    """Independent, database-read row counts (roster, player_stat, team_stat) currently tagged to `run_id`, via the existing Slice 3 reconciliation reader. Read-only: opens a session, queries, closes -- never commits or writes."""
    session = session_factory()
    try:
        report = read_source_family_reconciliation(session, run_id, "allstats")
    finally:
        session.close()
    by_table = {table.table_name: table.persisted_rows for table in report.tables}
    return (
        by_table.get("roster_memberships", 0),
        by_table.get("player_stat_snapshots", 0),
        by_table.get("team_stat_snapshots", 0),
    )


def _artifact_file_identity_text(artifact: AllStatsCommitArtifact) -> str:
    return ", ".join(
        f"{label}:{meta.relative_path}:{artifact.content_hashes.get(label)!r}"
        for label, meta in sorted(artifact.discovered.files.items())
    )


def _record_artifact_failure(session_factory, *, run_id: str, artifact: AllStatsCommitArtifact, issue_code: str, exc: Exception) -> None:
    """
    The design document section 9 failure-recording contract: a separate,
    immediately-following `unit_of_work` records exactly one
    `data_quality_issues` row identifying the failed team-season and
    exception, `source_artifact_id=None` (no artifact row exists for a
    failed/rolled-back artifact). Only ever field values already known to
    this module (team_slug, season, relative paths, content hashes,
    exception type/message) -- never a credential, absolute path, or
    source file content.
    """
    context = (
        f"team_slug={artifact.team_slug!r} season={artifact.season!r} "
        f"files=[{_artifact_file_identity_text(artifact)}] "
        f"exception_type={type(exc).__name__!r} exception_message={str(exc)!r}"
    )
    with unit_of_work(session_factory) as session:
        DataQualityIssueRepository(session).append(
            issue_code=issue_code,
            severity="critical",
            description=f"AllStats bulk-import artifact failed: team_slug={artifact.team_slug!r}, season={artifact.season!r}",
            pipeline_run_id=run_id,
            context=context,
        )


def run_allstats_committed_import(
    *,
    connection: Connection,
    target_designation: DatabaseTargetDesignation,
    session_factory,
    manifest: DryRunManifest,
    manifest_schema_version: str,
    artifacts: Sequence[AllStatsCommitArtifact],
    run_id: str,
    started_at: dt.datetime,
    pipeline_name: str = "bulk_import_allstats",
) -> AllStatsCommittedImportRunResult:
    """
    Commit `artifacts` (already-parsed, already-planned AllStats
    team-season workbooks) through the existing, unmodified
    `persist_allstats_import_plan`, under a verified database target and a
    held advisory lock, for the entire call.

    Exact lifecycle (docs/controlled_bulk_import_design.md section 16 item
    5's own Slice 5 scope, as corrected for artifact granularity -- see
    module docstring):

    1. Verify `connection` targets the exact database named by
       `target_designation` (`verify_database_target` -- existing, unmodified).
    2. Acquire the fail-fast advisory lock on `connection` for the verified
       target (`bulk_import_advisory_lock` -- existing, unmodified). Held
       for the remainder of this call; always released on every exit path.
    3. Validate `manifest` (schema version, exactly one `"allstats"`
       family, current adapter contract version) and every artifact
       against it (duplicate/stale/unplanned-file refusal), before any
       write.
    4. Create the `pipeline_runs` row (`status="running"`) through the
       existing `PipelineRunRepository`.
    5. For each artifact, in deterministic `(team_slug, season)` order:
       read persisted counts before, call `persist_allstats_import_plan`
       (its own single `unit_of_work` either fully commits or fully rolls
       back), read persisted counts after, and reconcile the delta against
       the artifact's own planned counts.
       - On a persist-time exception: record failure evidence in a
         separate clean transaction, mark this artifact `"failed"`, stop
         processing further artifacts.
       - On a reconciliation-count mismatch (the persist call itself
         raised nothing, but the independently-read delta disagrees with
         the plan): record failure evidence under a distinct issue_code,
         mark this artifact `"failed"`, stop processing further artifacts.
         The artifact's own rows are not rolled back here (they are
         already committed and real) -- only the run's own final status
         is prevented from ever reporting a false success.
       - On success: mark this artifact `"committed"`, continue.
    6. Finalize the pipeline run: `"succeeded"` only if every supplied
       artifact committed and reconciled; `"failed"` the moment any
       artifact failed. Never left `"running"`.
    7. Release the advisory lock (guaranteed by `bulk_import_advisory_lock`'s
       own `finally`, on every exit path including an exception raised
       from this function itself).

    Raises whatever `verify_database_target`/`bulk_import_advisory_lock`/
    manifest-and-artifact validation raise for a target/lock/input
    failure -- none of those failure modes create a `pipeline_runs` row at
    all (they are refused before step 4). Only a failure *after* the run
    has started (step 5) is recorded as a `pipeline_runs` row transitioned
    to `"failed"`.
    """
    verified_target = verify_database_target(connection, target_designation)

    with bulk_import_advisory_lock(connection, verified_target):
        family_plan = _validate_manifest(manifest, manifest_schema_version)
        ordered_artifacts = _validate_artifacts(family_plan, artifacts)

        with unit_of_work(session_factory) as session:
            PipelineRunRepository(session).start(
                run_id=run_id, pipeline_name=pipeline_name, started_at=started_at, status="running",
            )

        outcomes: list[AllStatsArtifactOutcome] = []
        run_failed = False

        for artifact in ordered_artifacts:
            before_counts = _allstats_persisted_counts(session_factory, run_id)

            try:
                result = persist_allstats_import_plan(
                    artifact.plan, session_factory,
                    discovered=artifact.discovered, content_hashes=artifact.content_hashes,
                    pipeline_run_id=run_id,
                )
            except Exception as exc:
                _record_artifact_failure(session_factory, run_id=run_id, artifact=artifact, issue_code=FAILED_ARTIFACT_ISSUE_CODE, exc=exc)
                outcomes.append(AllStatsArtifactOutcome(team_slug=artifact.team_slug, season=artifact.season, status="failed", error=f"{type(exc).__name__}: {exc}"))
                run_failed = True
                break

            after_counts = _allstats_persisted_counts(session_factory, run_id)
            observed_delta = tuple(after - before for after, before in zip(after_counts, before_counts))
            expected_delta = (artifact.plan.counts.roster_rows, artifact.plan.counts.player_stat_rows, artifact.plan.counts.team_stat_rows)

            if observed_delta != expected_delta:
                mismatch = ValidationError(
                    f"reconciliation mismatch for team-season {artifact.identity}: expected row deltas "
                    f"{expected_delta} (roster, player_stat, team_stat) but observed {observed_delta} after "
                    f"commit -- this artifact's own write already committed and cannot be undone; marking the "
                    f"run failed rather than reporting a false success"
                )
                _record_artifact_failure(session_factory, run_id=run_id, artifact=artifact, issue_code=RECONCILIATION_MISMATCH_ISSUE_CODE, exc=mismatch)
                outcomes.append(AllStatsArtifactOutcome(team_slug=artifact.team_slug, season=artifact.season, status="failed", result=result, error=str(mismatch)))
                run_failed = True
                break

            outcomes.append(AllStatsArtifactOutcome(team_slug=artifact.team_slug, season=artifact.season, status="committed", result=result))

        final_status = "failed" if run_failed else "succeeded"
        with unit_of_work(session_factory) as session:
            PipelineRunRepository(session).transition(run_id=run_id, new_status=final_status, completed_at=dt.datetime.now(dt.timezone.utc))

        return AllStatsCommittedImportRunResult(run_id=run_id, status=final_status, artifact_outcomes=tuple(outcomes))
