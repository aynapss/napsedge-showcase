"""
Controlled bulk-import design, Slice 8 (docs/controlled_bulk_import_design.md
section 16 item 8, approved for this slice only): the ESPN single-source-
family committed-import runner.

**This slice does not authorize a real import.** Every automated test for
this module runs only against synthetic, in-memory ESPN fixtures and the
established disposable PostgreSQL container (`tests/postgres/conftest.py`).
This module never discovers, scans, or reads `TeamDump/ESPN` itself,
never connects to a database on its own (every connection/session is
caller-supplied), and never invokes the ESPN parser directly -- it calls
the existing, unmodified `persist_espn_source_import_plan`/
`persist_espn_benchmark_import_plan` and nothing else touches the database.

ARTIFACT-UNIT PRECHECK (done before writing any code, per instruction):
the ESPN source family is structurally different from the three prior
families -- `discover_espn_artifacts` returns up to THREE known, flat
files (raw player-stats, a derived/enriched view, a benchmark reference),
and direct inspection of the adapter's own persist stage shows TWO
separate, dedicated persist entrypoints exist:
`persist_espn_source_import_plan` (for the raw player-stats file only)
and `persist_espn_benchmark_import_plan` (for the benchmark file only) --
each accepts exactly ONE `DiscoveredEspnFile` and ONE `content_hash: str`
(singular) and commits it inside exactly ONE `unit_of_work`. **No
contradiction was found**: each individually persistable file is still
its own atomic transaction unit, exactly matching the design's own
"one file = one artifact = one transaction" model (§4) -- the only
adapter-specific elaboration is that there are two distinct, sibling
persist paths (never a joint multi-file transaction, never one reading
or depending on the other's own content at persist time) rather than one
generic dispatcher, and the third file (the enriched view) has **no
persist path at all** -- confirmed by grepping this adapter's own
`__all__` and body for any `persist_espn_enriched*` function: none
exists. This finding is recorded in `docs/controlled_bulk_import_design.md`
§18 (a confirmation, not a correction, matching Slice 6/7's own findings,
unlike Slice 5's AllStats correction).

This runner therefore defines TWO sibling artifact types --
`EspnSourceStatsCommitArtifact` (wraps `EspnSourceImportPlan`, calls
`persist_espn_source_import_plan`) and `EspnBenchmarkCommitArtifact`
(wraps `EspnBenchmarkImportPlan`, calls `persist_espn_benchmark_import_plan`)
-- dispatched by Python type, never a stringly-typed discriminator. There
is deliberately NO third artifact type for the enriched file: since no
persist function exists for it, no such artifact can even be
constructed, which is the strongest possible guarantee that "the
enriched 130-column view is not persisted as an uncontrolled table" (a
structural guarantee, not merely a validation rule). The manifest's own
completeness check (see `_validate_artifacts`) explicitly exempts the
enriched file's own filename from the "every planned artifact must have
a matching supplied artifact" requirement, since no matching artifact
type could ever exist for it.

`EXPECTED_SOURCE_ROOT_KEY = "teamdump_espn"` here -- confirmed by direct
inspection to be a DIFFERENT literal, hardcoded value than the three CBB-
family runners' own `"teamdump_cbb"` (both ESPN persist functions write
`source_root_key="teamdump_espn"`).

Reconciliation reuses the existing, unmodified Slice 3
`read_source_family_reconciliation` reader's own `"espn"` family report
directly -- it already exposes exactly the facts this slice needs to
independently prove (per-table `persisted_rows`, a full three-way
team/player resolved/ambiguous/unresolved breakdown for
`espn_source_observations`, and a separate `espn_benchmark_values_persisted`
count) with no addition to that shared, separately-owned module. A
before/after delta of these same fields, scoped to `run_id`, proves the
adapter's own team/player resolution outcome (canonical, reviewed-alias,
ambiguous, unresolved) without fabricating a "newly inserted vs.
idempotently reused" distinction that reader does not, and never will,
claim to provide.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Sequence, Union

from sqlalchemy.engine import Connection

from src.db.repositories import DataQualityIssueRepository, PipelineRunRepository
from src.db.session import unit_of_work
from src.exceptions import ValidationError
from src.pipeline.bulk_import_manifest import MANIFEST_SCHEMA_VERSION, DryRunManifest, SourceFamilyPlan
from src.pipeline.bulk_import_reconciliation import Unavailable, read_source_family_reconciliation
from src.pipeline.bulk_import_safety import (
    DatabaseTargetDesignation,
    bulk_import_advisory_lock,
    verify_database_target,
)
from src.pipeline.espn_import_adapter import (
    ESPN_IMPORT_CONTRACT_VERSION,
    DiscoveredEspnFile,
    EspnBenchmarkImportPlan,
    EspnBenchmarkImportResult,
    EspnSourceImportPlan,
    EspnSourceImportResult,
    persist_espn_benchmark_import_plan,
    persist_espn_source_import_plan,
)

__all__ = [
    "FAILED_ARTIFACT_ISSUE_CODE",
    "RECONCILIATION_MISMATCH_ISSUE_CODE",
    "EXPECTED_SOURCE_ROOT_KEY",
    "COMMITTABLE_MANIFEST_STATUSES",
    "ESPN_RAW_PLAYER_STATS_FILENAME",
    "ESPN_BENCHMARK_FILENAME",
    "ESPN_ENRICHED_FILENAME",
    "EspnSourceStatsCommitArtifact",
    "EspnBenchmarkCommitArtifact",
    "EspnArtifactOutcome",
    "EspnCommittedImportRunResult",
    "run_espn_committed_import",
]

#: Verbatim from docs/controlled_bulk_import_design.md section 9 -- the
#: same fixed issue_code every prior runner uses, intentionally duplicated
#: here (not imported from any of them) since each runner independently
#: derives it from the same approved design text -- no cross-runner coupling.
FAILED_ARTIFACT_ISSUE_CODE = "bulk_import_artifact_failed"

#: This runner's own issue_code for the (expected-to-be-unreachable in
#: correct operation) case where a just-committed artifact's independently
#: re-read state does not match what this family's own adapter could
#: actually have produced.
RECONCILIATION_MISMATCH_ISSUE_CODE = "bulk_import_reconciliation_mismatch"

#: The literal, hardcoded `source_root_key` value BOTH ESPN persist
#: functions always write (confirmed by direct inspection) -- distinct
#: from the three CBB-family runners' own `"teamdump_cbb"`.
EXPECTED_SOURCE_ROOT_KEY = "teamdump_espn"

#: A dry-run manifest disposition eligible for an actual commit. An
#: `"unresolved"`/`"ambiguous"`/`"conflict"` status is a recorded data-
#: quality finding from the dry run, never something this runner commits.
COMMITTABLE_MANIFEST_STATUSES = frozenset({"planned_insert", "idempotent_match"})

#: The three known, flat ESPN filenames `discover_espn_artifacts` itself
#: checks for (confirmed by direct inspection of that function's own
#: private filename constants) -- repeated here as this runner's own
#: public constants rather than importing the adapter's private ones,
#: since only this runner needs them (for artifact-type validation and
#: the enriched-file manifest exemption below).
ESPN_RAW_PLAYER_STATS_FILENAME = "espn_full_player_stats.csv"
ESPN_ENRICHED_FILENAME = "espn_full_player_stats_enriched.csv"
ESPN_BENCHMARK_FILENAME = "espn_ncaa_benchmarks.xlsx"


@dataclass(frozen=True)
class EspnSourceStatsCommitArtifact:
    """
    The raw ESPN player-stats file, ready to commit via the existing,
    unmodified `persist_espn_source_import_plan`. `season_id` is
    cross-checked against `plan.season` (safe here -- `parse_espn_player_stats_workbook`
    stores its own `season` argument verbatim, confirmed by direct
    inspection) and is otherwise this artifact's own duplicate/ordering
    identity, since exactly one such file exists per season.
    """

    discovered: DiscoveredEspnFile
    content_hash: str
    plan: EspnSourceImportPlan
    season_id: int

    def __post_init__(self) -> None:
        if not isinstance(self.discovered, DiscoveredEspnFile):
            raise ValidationError("discovered must be a DiscoveredEspnFile produced by discover_espn_artifacts")
        if self.discovered.filename != ESPN_RAW_PLAYER_STATS_FILENAME:
            raise ValidationError(
                f"EspnSourceStatsCommitArtifact requires discovered.filename == {ESPN_RAW_PLAYER_STATS_FILENAME!r}, "
                f"got {self.discovered.filename!r} -- use EspnBenchmarkCommitArtifact for the benchmark file, and "
                f"note the enriched file has no committable artifact type at all"
            )
        if not isinstance(self.content_hash, str) or not self.content_hash:
            raise ValidationError("content_hash must be a non-empty string")
        if not isinstance(self.plan, EspnSourceImportPlan):
            raise ValidationError("plan must be an EspnSourceImportPlan produced by build_espn_source_import_plan")
        if self.plan.season != self.season_id:
            raise ValidationError(
                f"plan season {self.plan.season!r} does not match this artifact's own season_id {self.season_id!r} "
                f"-- refusing a mismatched discovered/plan pairing"
            )

    @property
    def relative_path(self) -> str:
        return self.discovered.relative_path

    @property
    def identity(self) -> tuple[str, int]:
        return ("source_stats", self.season_id)


@dataclass(frozen=True)
class EspnBenchmarkCommitArtifact:
    """
    The ESPN NCAA-cohort benchmark file, ready to commit via the
    existing, unmodified `persist_espn_benchmark_import_plan`. Not
    team/player/season-scoped (confirmed by direct inspection of
    `parse_espn_benchmark_workbook`'s own docstring) -- `season_id` is a
    caller-supplied, purely-administrative value with nothing on `plan`
    to cross-check it against.
    """

    discovered: DiscoveredEspnFile
    content_hash: str
    plan: EspnBenchmarkImportPlan
    season_id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.discovered, DiscoveredEspnFile):
            raise ValidationError("discovered must be a DiscoveredEspnFile produced by discover_espn_artifacts")
        if self.discovered.filename != ESPN_BENCHMARK_FILENAME:
            raise ValidationError(
                f"EspnBenchmarkCommitArtifact requires discovered.filename == {ESPN_BENCHMARK_FILENAME!r}, "
                f"got {self.discovered.filename!r} -- use EspnSourceStatsCommitArtifact for the raw player-stats "
                f"file, and note the enriched file has no committable artifact type at all"
            )
        if not isinstance(self.content_hash, str) or not self.content_hash:
            raise ValidationError("content_hash must be a non-empty string")
        if not isinstance(self.plan, EspnBenchmarkImportPlan):
            raise ValidationError("plan must be an EspnBenchmarkImportPlan produced by build_espn_benchmark_import_plan")

    @property
    def relative_path(self) -> str:
        return self.discovered.relative_path

    @property
    def identity(self) -> tuple[str, int | None]:
        return ("benchmark", self.season_id)


EspnCommitArtifact = Union[EspnSourceStatsCommitArtifact, EspnBenchmarkCommitArtifact]


@dataclass(frozen=True)
class EspnArtifactOutcome:
    """One artifact's own outcome within a committed-import run. `result` is populated only when `status == "committed"`."""

    kind: str  # "source_stats" | "benchmark"
    season_id: int | None
    status: str  # "committed" | "failed"
    result: EspnSourceImportResult | EspnBenchmarkImportResult | None = None
    error: str | None = None


@dataclass(frozen=True)
class EspnCommittedImportRunResult:
    """
    The immutable, final outcome of one `run_espn_committed_import` call.
    `status == "succeeded"` only when every supplied artifact was
    committed and independently reconciled -- a run that stopped early on
    any artifact failure always reports `"failed"`, never a partial
    success.
    """

    run_id: str
    status: str  # "succeeded" | "failed"
    artifact_outcomes: tuple[EspnArtifactOutcome, ...] = field(default_factory=tuple)

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
            f"this runner accepts only a single-family espn manifest, got {len(manifest.families)} "
            f"families ({[p.family for p in manifest.families]}) -- refusing an empty or mixed-family manifest"
        )
    family_plan = manifest.families[0]
    if family_plan.family != "espn":
        raise ValidationError(f"this runner only ever commits the 'espn' family, got {family_plan.family!r}")
    # Deliberately ESPN_IMPORT_CONTRACT_VERSION, never the pre-existing, distinct
    # TRANSFORMATION_VERSION (which tags only the benchmark-snapshot derivation
    # itself, already persisted to espn_benchmark_snapshots.transformation_version
    # by the unmodified adapter) -- see module docstring and the adapter's own.
    if family_plan.contract_version != ESPN_IMPORT_CONTRACT_VERSION:
        raise ValidationError(
            f"manifest espn contract_version {family_plan.contract_version!r} does not match the current "
            f"ESPN adapter import-contract version {ESPN_IMPORT_CONTRACT_VERSION!r} -- refusing a stale "
            f"manifest (the adapter's own contract changed since this manifest was built)"
        )
    return family_plan


def _validate_artifacts(family_plan: SourceFamilyPlan, artifacts: Sequence[EspnCommitArtifact]) -> tuple[EspnCommitArtifact, ...]:
    if isinstance(artifacts, (str, bytes)) or not isinstance(artifacts, Sequence):
        raise ValidationError("artifacts must be a sequence of EspnSourceStatsCommitArtifact/EspnBenchmarkCommitArtifact")
    for artifact in artifacts:
        if not isinstance(artifact, (EspnSourceStatsCommitArtifact, EspnBenchmarkCommitArtifact)):
            raise ValidationError(f"every artifact must be an EspnSourceStatsCommitArtifact or EspnBenchmarkCommitArtifact, got {type(artifact).__name__}")

    seen_identities: set[tuple] = set()
    for artifact in artifacts:
        if artifact.identity in seen_identities:
            raise ValidationError(f"duplicate artifact for identity {artifact.identity} -- refusing to process the same ESPN artifact twice in one run")
        seen_identities.add(artifact.identity)

    manifest_by_relpath = {planned.relative_path: planned for planned in family_plan.artifacts}
    artifact_by_relpath = {artifact.relative_path: artifact for artifact in artifacts}

    for artifact in artifacts:
        if artifact.relative_path not in manifest_by_relpath:
            raise ValidationError(
                f"unplanned artifact: {artifact.relative_path!r} (identity {artifact.identity}) is not present in "
                f"the approved dry-run manifest -- refusing to commit a file that was never planned"
            )

    for planned in family_plan.artifacts:
        if planned.relative_path == ESPN_ENRICHED_FILENAME:
            # No committable artifact type exists for the enriched file at all (see module
            # docstring) -- a manifest may legitimately record it as a discovered, dry-run-only
            # entry, but this runner never requires (or accepts) a matching commit artifact for it.
            continue
        if planned.relative_path not in artifact_by_relpath:
            raise ValidationError(
                f"missing planned artifact: {planned.relative_path!r} is approved in the manifest but no matching "
                f"artifact was supplied to this call -- this runner requires complete manifest/artifact correspondence"
            )

    for artifact in artifacts:
        planned = manifest_by_relpath[artifact.relative_path]
        if planned.source_root_key != EXPECTED_SOURCE_ROOT_KEY:
            raise ValidationError(
                f"mismatched source_root_key for {artifact.relative_path!r}: manifest recorded "
                f"{planned.source_root_key!r}, but the ESPN adapter always persists under "
                f"{EXPECTED_SOURCE_ROOT_KEY!r} -- refusing a manifest that could never match what gets written"
            )
        if planned.content_hash != artifact.content_hash:
            raise ValidationError(
                f"artifact file {artifact.relative_path!r} (identity {artifact.identity}) has content_hash "
                f"{artifact.content_hash!r}, but the approved dry-run manifest recorded {planned.content_hash!r} "
                f"for this same file -- refusing a changed/stale artifact that no longer matches what was approved"
            )
        if planned.status not in COMMITTABLE_MANIFEST_STATUSES:
            raise ValidationError(
                f"artifact file {artifact.relative_path!r} has manifest status {planned.status!r}, not eligible "
                f"for a committed import (expected one of {sorted(COMMITTABLE_MANIFEST_STATUSES)})"
            )

    # Deterministic order -- never the caller's own input order.
    return tuple(sorted(artifacts, key=lambda a: (a.identity[0], "" if a.identity[1] is None else str(a.identity[1]))))


def _require_int(value, *, label: str) -> int:
    if isinstance(value, Unavailable):
        raise ValidationError(
            f"reconciliation cannot proceed: {label} is Unavailable ({value.reason}) -- the existing Slice 3 "
            f"reader is expected to report a concrete int for the espn family's own team/player identity "
            f"breakdown; an Unavailable value here would require a schema or policy decision, not something "
            f"this runner fabricates a number for"
        )
    return value


def _espn_source_snapshot(session_factory, run_id: str) -> tuple[int, int, int, int, int, int]:
    """
    Independent, database-read snapshot for the `espn_source_observations`
    table, scoped to `run_id`, via the existing, unmodified Slice 3
    `read_source_family_reconciliation` reader -- never fabricated.
    Returns `(persisted_rows, team_resolved, team_ambiguous_or_unresolved,
    player_resolved, player_ambiguous, player_unresolved)`.
    """
    session = session_factory()
    try:
        report = read_source_family_reconciliation(session, run_id, "espn")
    finally:
        session.close()
    table = report.tables[0]
    team = table.team_identity
    player = table.player_identity
    team_ambiguous = _require_int(team.ambiguous, label="espn team_identity.ambiguous")
    team_unresolved = _require_int(team.unresolved, label="espn team_identity.unresolved")
    player_ambiguous = _require_int(player.ambiguous, label="espn player_identity.ambiguous")
    player_unresolved = _require_int(player.unresolved, label="espn player_identity.unresolved")
    return (
        table.persisted_rows, team.resolved, team_ambiguous + team_unresolved,
        player.resolved, player_ambiguous, player_unresolved,
    )


def _espn_benchmark_values_persisted(session_factory, run_id: str) -> int:
    """Independent, database-read count of `espn_benchmark_values` currently tagged to `run_id`, via the existing Slice 3 reader."""
    session = session_factory()
    try:
        report = read_source_family_reconciliation(session, run_id, "espn")
    finally:
        session.close()
    return report.espn_benchmark_values_persisted or 0


def _artifact_identity_text(artifact: EspnCommitArtifact) -> str:
    return f"{artifact.relative_path}:{artifact.content_hash!r}"


def _record_artifact_failure(session_factory, *, run_id: str, artifact: EspnCommitArtifact, issue_code: str, exc: Exception) -> None:
    """
    The design document section 9 failure-recording contract: a separate,
    immediately-following `unit_of_work` records exactly one
    `data_quality_issues` row identifying the failed artifact and
    exception, `source_artifact_id=None` (no artifact row exists for a
    failed/rolled-back artifact). Only ever field values already known to
    this module (identity, relative path, content hash, exception type/
    message) -- never a credential, absolute path, or source file content.
    """
    context = (
        f"identity={artifact.identity!r} file=[{_artifact_identity_text(artifact)}] "
        f"exception_type={type(exc).__name__!r} exception_message={str(exc)!r}"
    )
    with unit_of_work(session_factory) as session:
        DataQualityIssueRepository(session).append(
            issue_code=issue_code,
            severity="critical",
            description=f"ESPN bulk-import artifact failed: identity={artifact.identity!r}",
            pipeline_run_id=run_id,
            context=context,
        )


def run_espn_committed_import(
    *,
    connection: Connection,
    target_designation: DatabaseTargetDesignation,
    session_factory,
    manifest: DryRunManifest,
    manifest_schema_version: str,
    artifacts: Sequence[EspnCommitArtifact],
    run_id: str,
    started_at: dt.datetime,
    pipeline_name: str = "bulk_import_espn",
) -> EspnCommittedImportRunResult:
    """
    Commit `artifacts` (already-parsed, already-planned ESPN raw-stats
    and/or benchmark files) through the existing, unmodified
    `persist_espn_source_import_plan`/`persist_espn_benchmark_import_plan`,
    under a verified database target and a held advisory lock, for the
    entire call.

    Lifecycle (docs/controlled_bulk_import_design.md section 16 item 8):

    1. Verify `connection` targets the exact database named by
       `target_designation` (`verify_database_target` -- existing, unmodified).
    2. Acquire the fail-fast advisory lock on `connection` for the
       verified target (`bulk_import_advisory_lock` -- existing,
       unmodified). Held for the remainder of this call; always released
       on every exit path.
    3. Validate `manifest` (schema version, exactly one `"espn"` family,
       current `ESPN_IMPORT_CONTRACT_VERSION` -- never the distinct
       `TRANSFORMATION_VERSION`) and every artifact against it (complete
       manifest/artifact correspondence -- with the enriched file's own
       filename explicitly exempt, since no committable artifact type
       exists for it -- source-root-key match, stale-content-hash
       refusal, committable-status refusal), before any write.
    4. Create the `pipeline_runs` row (`status="running"`) through the
       existing `PipelineRunRepository`.
    5. For each artifact, in deterministic order: read this artifact's
       own family-level reconciliation snapshot before, call the
       matching existing persist function for this artifact's own type
       (its own single `unit_of_work` either fully commits or fully
       rolls back), read the same snapshot again after, and verify every
       relevant delta matches the plan's own pure counts.
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

        outcomes: list[EspnArtifactOutcome] = []
        run_failed = False

        for artifact in ordered_artifacts:
            kind, season_id = artifact.identity

            if isinstance(artifact, EspnSourceStatsCommitArtifact):
                before = _espn_source_snapshot(session_factory, run_id)
                try:
                    result = persist_espn_source_import_plan(
                        artifact.plan, session_factory,
                        discovered=artifact.discovered, content_hash=artifact.content_hash,
                        season_id=artifact.season_id, pipeline_run_id=run_id,
                    )
                except Exception as exc:
                    _record_artifact_failure(session_factory, run_id=run_id, artifact=artifact, issue_code=FAILED_ARTIFACT_ISSUE_CODE, exc=exc)
                    outcomes.append(EspnArtifactOutcome(kind=kind, season_id=season_id, status="failed", error=f"{type(exc).__name__}: {exc}"))
                    run_failed = True
                    break

                after = _espn_source_snapshot(session_factory, run_id)
                observed = tuple(a - b for a, b in zip(after, before))
                expected = (
                    artifact.plan.counts.total_source_rows, artifact.plan.counts.resolved_teams,
                    artifact.plan.counts.unresolved_teams, artifact.plan.counts.resolved_players,
                    artifact.plan.counts.ambiguous_players, artifact.plan.counts.unresolved_players,
                )
                mismatch = None if observed == expected else (
                    f"espn source-stats reconciliation mismatch for identity {artifact.identity}: expected "
                    f"(rows, team_resolved, team_unresolved_or_ambiguous, player_resolved, player_ambiguous, "
                    f"player_unresolved)={expected}, observed {observed}"
                )
            else:
                before_count = _espn_benchmark_values_persisted(session_factory, run_id)
                try:
                    result = persist_espn_benchmark_import_plan(
                        artifact.plan, session_factory,
                        discovered=artifact.discovered, content_hash=artifact.content_hash,
                        season_id=artifact.season_id, pipeline_run_id=run_id,
                    )
                except Exception as exc:
                    _record_artifact_failure(session_factory, run_id=run_id, artifact=artifact, issue_code=FAILED_ARTIFACT_ISSUE_CODE, exc=exc)
                    outcomes.append(EspnArtifactOutcome(kind=kind, season_id=season_id, status="failed", error=f"{type(exc).__name__}: {exc}"))
                    run_failed = True
                    break

                after_count = _espn_benchmark_values_persisted(session_factory, run_id)
                observed_delta = after_count - before_count
                expected_delta = len(artifact.plan.value_specs)
                mismatch = None if observed_delta == expected_delta else (
                    f"espn benchmark reconciliation mismatch for identity {artifact.identity}: expected an "
                    f"espn_benchmark_values delta of {expected_delta}, observed {observed_delta}"
                )

            if mismatch is not None:
                mismatch_error = ValidationError(
                    f"{mismatch} -- this artifact's own write already committed and cannot be undone; marking the "
                    f"run failed rather than reporting a false success"
                )
                _record_artifact_failure(session_factory, run_id=run_id, artifact=artifact, issue_code=RECONCILIATION_MISMATCH_ISSUE_CODE, exc=mismatch_error)
                outcomes.append(EspnArtifactOutcome(kind=kind, season_id=season_id, status="failed", result=result, error=str(mismatch_error)))
                run_failed = True
                break

            outcomes.append(EspnArtifactOutcome(kind=kind, season_id=season_id, status="committed", result=result))

        final_status = "failed" if run_failed else "succeeded"
        with unit_of_work(session_factory) as session:
            PipelineRunRepository(session).transition(run_id=run_id, new_status=final_status, completed_at=dt.datetime.now(dt.timezone.utc))

        return EspnCommittedImportRunResult(run_id=run_id, status=final_status, artifact_outcomes=tuple(outcomes))
