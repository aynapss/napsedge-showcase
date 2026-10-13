"""
Controlled bulk-import design, Slice 9 (docs/controlled_bulk_import_design.md
section 16 item 9, approved for this slice only): the controlled multi-
family coordinator.

**This slice does not authorize a real import.** Every automated test for
this module runs only against synthetic, in-memory fixtures and the
established disposable PostgreSQL container (`tests/postgres/conftest.py`).
This module never discovers, scans, or reads any source-family's own
files itself, never invokes any of the four import adapters directly,
never builds a manifest, and never opens a database write of its own --
every actual write is delegated entirely to the four existing, unmodified
Slice 5-8 runners (`run_allstats_committed_import`,
`run_team_game_log_committed_import`, `run_player_game_log_committed_import`,
`run_espn_committed_import`). This module's own connection is used only
for target verification and the outer advisory lock -- never a session,
never a `unit_of_work`, never a repository call.

ARCHITECTURAL PRECHECK (done before writing any code, per instruction) --
a genuine, confirmed contradiction was found and reported before any code
was written, resolved by explicit approval:

**Pipeline-run ownership.** `docs/controlled_bulk_import_design.md`
section 4's own table states "All four families in one invocation share
the same `run_id`," and section 16 item 9 calls this "the first slice
that exercises all four families in one `pipeline_runs` row." Direct
inspection of all four completed runners shows this is not achievable
without modifying them: each one unconditionally calls
`PipelineRunRepository(session).start(run_id=run_id, ...)` as its own
first write, and `PipelineRunRepository.start` explicitly rejects a
pre-existing `run_id` (`IdentityConflictError`, "run_id must be freshly
generated per run") -- so the second family's own `start()` call would
fail immediately if given the first family's own `run_id`. Independently,
each runner also finalizes its own `run_id` to a **terminal**
`PIPELINE_RUN_STATUSES` value (`succeeded`/`failed`, both with zero
outgoing edges in `_PIPELINE_RUN_TRANSITIONS`) as its own last write --
so even past the `start()` conflict, a second family's own closing
`.transition()` call on that same, already-terminal `run_id` would also
be refused. **Approved resolution**: four separate, per-family
`pipeline_runs` rows (one fresh `run_id` per family, each family runner
completely unmodified and used exactly as shipped), aggregated in memory
by this coordinator into one `CoordinatedImportRunResult` -- not the
literal "one shared `pipeline_runs` row" text. This is a documented
factual correction to sections 4 and 16 item 9 (see
`docs/controlled_bulk_import_design.md` itself), the same category of
correction Slice 5 made for AllStats' own artifact-transaction-unit
finding -- never silently invented, reported and approved before this
module was written.

**Lock ownership** (confirmed, no correction needed): the existing,
unmodified `bulk_import_advisory_lock` is documented and tested (Slice 4)
as naturally reentrant per `(session, key)` via PostgreSQL's own native
reference counting. This coordinator acquires it exactly once, at the
outer level, on its own caller-supplied `connection`; each family
runner's own internal, unmodified call to the same function -- given the
SAME `connection` and a `VerifiedDatabaseTarget` whose lock key depends
only on the (identical) database name -- re-acquires the SAME PostgreSQL
session-level lock instantly (already held by this same session) and
each inner release decrements only that acquisition, never the outer
one. The outer lock therefore remains held continuously across every
family, and no other connection can acquire it until this coordinator's
own `with` block exits.

**Manifest partitioning** (confirmed, no correction needed): each
family's own `SourceFamilyPlan` inside the caller-supplied, multi-family
`DryRunManifest` is already a complete, immutable, self-contained record
(family name, contract version, and its own already-validated,
already-sorted tuple of `PlannedArtifact` records). Partitioning is
therefore a lossless, zero-reinterpretation extraction: build a new,
single-family `DryRunManifest` wrapping that exact same `SourceFamilyPlan`
object, unchanged, via the existing `build_dry_run_manifest`. No artifact
record is ever rebuilt from filesystem discovery -- this module never
discovers anything.

**Failure semantics** (confirmed, no correction needed): no distributed
transaction is added. A family that fails leaves every earlier family's
own already-committed rows exactly as they are (each family runner's own
already-approved rollback/commit boundary is untouched); the coordinator
stops delegating to any further family; families after the failed one
are reported with a new, purely in-memory, never-persisted status --
`"not_started"` -- distinguishing "never attempted" from "attempted and
failed" (`"failed"`) and "attempted and succeeded" (`"committed"`). This
three-value vocabulary is this module's own leaf-level return-type
decision (the same category of decision every prior slice's own
`{Family}ArtifactOutcome.status` vocabulary already made), never a new
database status, relationship, or notes convention -- nothing here is
ever written to `pipeline_runs`.

**Resume semantics** (confirmed, no correction needed): this coordinator
never infers "which families already succeeded" from existing database
rows (an earlier run may have been partial, or may reference a different
manifest -- inferring from row existence alone would be exactly the kind
of fabricated fact this design forbids). The caller explicitly supplies,
via `family_requests`, exactly the families (and each family's own fresh
`run_id`) to run in this one call -- re-running all four, or an explicit
subset, is entirely the caller's own decision, made safe purely by each
family runner's own already-proven natural-key idempotency. No resume
orchestration of any kind is implemented here (`docs/controlled_bulk_import_design.md`
section 7 itself leaves "resumed" as an explicitly open, unresolved
design decision -- section 16 item 2 -- and this slice does not touch it).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Sequence, Union

from sqlalchemy.engine import Connection

from src.exceptions import ValidationError
from src.pipeline.bulk_import_allstats_runner import (
    AllStatsCommitArtifact,
    AllStatsCommittedImportRunResult,
    _validate_artifacts as _validate_allstats_artifacts,
    _validate_manifest as _validate_allstats_manifest,
    run_allstats_committed_import,
)
from src.pipeline.bulk_import_espn_runner import (
    EspnCommitArtifact,
    EspnCommittedImportRunResult,
    _validate_artifacts as _validate_espn_artifacts,
    _validate_manifest as _validate_espn_manifest,
    run_espn_committed_import,
)
from src.pipeline.bulk_import_game_log_runner import (
    TeamGameLogCommitArtifact,
    TeamGameLogCommittedImportRunResult,
    _validate_artifacts as _validate_team_game_log_artifacts,
    _validate_manifest as _validate_team_game_log_manifest,
    run_team_game_log_committed_import,
)
from src.pipeline.bulk_import_manifest import SOURCE_FAMILY_ORDER, DryRunManifest, build_dry_run_manifest
from src.pipeline.bulk_import_player_game_log_runner import (
    PlayerGameLogCommitArtifact,
    PlayerGameLogCommittedImportRunResult,
    _validate_artifacts as _validate_player_game_log_artifacts,
    _validate_manifest as _validate_player_game_log_manifest,
    run_player_game_log_committed_import,
)
from src.pipeline.bulk_import_safety import (
    DatabaseTargetDesignation,
    bulk_import_advisory_lock,
    verify_database_target,
)

__all__ = [
    "AllStatsFamilyRequest",
    "TeamGameLogFamilyRequest",
    "PlayerGameLogFamilyRequest",
    "EspnFamilyRequest",
    "FamilyRequest",
    "CoordinatedFamilyOutcome",
    "CoordinatedImportRunResult",
    "run_controlled_bulk_import",
]


@dataclass(frozen=True)
class AllStatsFamilyRequest:
    """One caller-supplied request to run the AllStats family within one coordinated call -- its own fresh `run_id`, own artifacts, own pipeline_name."""

    artifacts: Sequence[AllStatsCommitArtifact]
    run_id: str
    started_at: dt.datetime
    pipeline_name: str = "bulk_import_allstats"

    @property
    def family(self) -> str:
        return "allstats"


@dataclass(frozen=True)
class TeamGameLogFamilyRequest:
    artifacts: Sequence[TeamGameLogCommitArtifact]
    run_id: str
    started_at: dt.datetime
    pipeline_name: str = "bulk_import_team_game_log"

    @property
    def family(self) -> str:
        return "team_game_log"


@dataclass(frozen=True)
class PlayerGameLogFamilyRequest:
    artifacts: Sequence[PlayerGameLogCommitArtifact]
    run_id: str
    started_at: dt.datetime
    pipeline_name: str = "bulk_import_player_game_log"

    @property
    def family(self) -> str:
        return "player_game_log"


@dataclass(frozen=True)
class EspnFamilyRequest:
    artifacts: Sequence[EspnCommitArtifact]
    run_id: str
    started_at: dt.datetime
    pipeline_name: str = "bulk_import_espn"

    @property
    def family(self) -> str:
        return "espn"


FamilyRequest = Union[AllStatsFamilyRequest, TeamGameLogFamilyRequest, PlayerGameLogFamilyRequest, EspnFamilyRequest]

FamilyRunResult = Union[
    AllStatsCommittedImportRunResult, TeamGameLogCommittedImportRunResult,
    PlayerGameLogCommittedImportRunResult, EspnCommittedImportRunResult,
]

#: (manifest_validator, artifact_validator, runner_entrypoint) per family --
#: every one of these is the existing, unmodified function from that
#: family's own already-approved runner module. Never reimplemented here.
_FAMILY_HANDLERS = {
    "allstats": (_validate_allstats_manifest, _validate_allstats_artifacts, run_allstats_committed_import),
    "team_game_log": (_validate_team_game_log_manifest, _validate_team_game_log_artifacts, run_team_game_log_committed_import),
    "player_game_log": (_validate_player_game_log_manifest, _validate_player_game_log_artifacts, run_player_game_log_committed_import),
    "espn": (_validate_espn_manifest, _validate_espn_artifacts, run_espn_committed_import),
}


@dataclass(frozen=True)
class CoordinatedFamilyOutcome:
    """
    One family's own outcome within a coordinated multi-family run.
    `status` is `"committed"` (this family's own runner reported
    `"succeeded"`), `"failed"` (this family's own runner either raised or
    itself reported `"failed"`), or `"not_started"` (an earlier family in
    the same coordinated call already failed, so this one was never
    attempted at all) -- a purely in-memory vocabulary, never written to
    `pipeline_runs` or any other table.
    """

    family: str
    status: str  # "committed" | "failed" | "not_started"
    run_result: FamilyRunResult | None = None
    error: str | None = None


@dataclass(frozen=True)
class CoordinatedImportRunResult:
    """
    The immutable, final outcome of one `run_controlled_bulk_import` call.
    `status == "succeeded"` only when every requested family committed --
    a run that stopped early on any family failure always reports
    `"failed"`, never a partial success, regardless of how many earlier
    families actually committed (see each such family's own
    `CoordinatedFamilyOutcome`, still `"committed"`, for the accurate
    per-family picture).
    """

    status: str  # "succeeded" | "failed"
    family_outcomes: tuple[CoordinatedFamilyOutcome, ...] = field(default_factory=tuple)

    @property
    def committed_families(self) -> tuple[str, ...]:
        return tuple(o.family for o in self.family_outcomes if o.status == "committed")

    @property
    def failed_families(self) -> tuple[str, ...]:
        return tuple(o.family for o in self.family_outcomes if o.status == "failed")

    @property
    def not_started_families(self) -> tuple[str, ...]:
        return tuple(o.family for o in self.family_outcomes if o.status == "not_started")


def _extract_family_manifest(manifest: DryRunManifest, family: str) -> DryRunManifest:
    """
    Lossless, zero-reinterpretation partition: wraps the exact same,
    already-validated `SourceFamilyPlan` object for `family` (unchanged)
    in a new, single-family `DryRunManifest`, reusing the caller's own
    `generated_at` timestamp. Never rebuilds an artifact record.
    """
    matches = [plan for plan in manifest.families if plan.family == family]
    if not matches:
        raise ValidationError(
            f"requested family {family!r} has no matching entry in the supplied manifest -- refusing to run a "
            f"family the manifest never planned"
        )
    # DryRunManifest.__post_init__ already forbids a duplicate family name, so at most one match exists.
    return build_dry_run_manifest(generated_at=manifest.generated_at, families=[matches[0]])


def _validate_family_requests(manifest: DryRunManifest, manifest_schema_version: str, family_requests: Sequence[FamilyRequest]) -> tuple[tuple[FamilyRequest, DryRunManifest], ...]:
    if isinstance(family_requests, (str, bytes)) or not isinstance(family_requests, Sequence):
        raise ValidationError("family_requests must be a sequence of AllStatsFamilyRequest/TeamGameLogFamilyRequest/PlayerGameLogFamilyRequest/EspnFamilyRequest")
    if not isinstance(manifest, DryRunManifest):
        raise ValidationError("manifest must be a DryRunManifest produced by build_dry_run_manifest")

    for request in family_requests:
        if not isinstance(request, (AllStatsFamilyRequest, TeamGameLogFamilyRequest, PlayerGameLogFamilyRequest, EspnFamilyRequest)):
            raise ValidationError(f"every family request must be a known FamilyRequest type, got {type(request).__name__} -- refusing an unknown family")

    seen_families: set[str] = set()
    for request in family_requests:
        if request.family in seen_families:
            raise ValidationError(f"duplicate family request for {request.family!r} -- refusing to process the same family twice in one coordinated run")
        seen_families.add(request.family)

    # Each family runner owns its own run_id's entire lifecycle (create it fresh, finalize it to a
    # terminal status) -- reusing the same run_id across two families in one call would only be
    # caught reactively, mid-sequence, when the second family's own PipelineRunRepository.start()
    # raises IdentityConflictError (since the first family's own call already created that row).
    # Refused explicitly, upfront, so a malformed request never partially executes first.
    seen_run_ids: dict[str, str] = {}
    for request in family_requests:
        if request.run_id in seen_run_ids:
            raise ValidationError(
                f"duplicate run_id {request.run_id!r} supplied for both {seen_run_ids[request.run_id]!r} and "
                f"{request.family!r} -- every family in one coordinated call must have its own fresh, unique run_id"
            )
        seen_run_ids[request.run_id] = request.family

    prepared: list[tuple[FamilyRequest, DryRunManifest]] = []
    for request in family_requests:
        family_manifest = _extract_family_manifest(manifest, request.family)
        manifest_validator, artifact_validator, _ = _FAMILY_HANDLERS[request.family]
        # Redundant with each runner's own internal re-check at execution time, by design (defense in depth,
        # never a rule reimplementation -- these are literal calls to the same, existing, unmodified
        # functions) -- this is what makes "validate all requested families before starting the first one"
        # possible without touching any runner's own internal validation order.
        family_plan = manifest_validator(family_manifest, manifest_schema_version)
        artifact_validator(family_plan, request.artifacts)
        prepared.append((request, family_manifest))

    # Deterministic order -- the established SOURCE_FAMILY_ORDER, never the caller's own input order.
    prepared.sort(key=lambda pair: SOURCE_FAMILY_ORDER.index(pair[0].family))
    return tuple(prepared)


def run_controlled_bulk_import(
    *,
    connection: Connection,
    target_designation: DatabaseTargetDesignation,
    session_factory,
    manifest: DryRunManifest,
    manifest_schema_version: str,
    family_requests: Sequence[FamilyRequest],
) -> CoordinatedImportRunResult:
    """
    Compose the four existing, unmodified family runners into one
    coordinated, ordered execution -- under a single verified target and
    one continuously-held outer advisory lock.

    Lifecycle (docs/controlled_bulk_import_design.md section 16 item 9,
    as corrected for pipeline-run ownership -- see module docstring):

    1. Verify `connection` targets the exact database named by
       `target_designation` (`verify_database_target` -- existing, unmodified).
    2. Acquire the fail-fast advisory lock on `connection` for the
       verified target (`bulk_import_advisory_lock` -- existing,
       unmodified). Held continuously across every family in this call;
       always released on every exit path. Each family runner's own
       internal, unmodified acquisition of the same lock on the same
       connection re-enters it (PostgreSQL's own native per-session
       reentrancy), never weakening or bypassing that runner's own guard.
    3. Partition `manifest` per requested family (lossless, see module
       docstring) and validate every family's own manifest/artifacts --
       using that family's own existing, unmodified validators -- before
       delegating to the first family's runner. Refuses an unknown,
       duplicate-family, duplicate-run_id, or manifest-missing family,
       exactly as each family's own runner already refuses a missing/
       unplanned/stale/mismatched artifact. A duplicate `run_id` across
       two different families is refused here, upfront -- never left to
       surface reactively as a mid-sequence `IdentityConflictError` from
       the second family's own `PipelineRunRepository.start()`.
    4. For each family, in the established `SOURCE_FAMILY_ORDER` (AllStats,
       team game-log, player game-log, ESPN) -- never the caller's own
       input order -- delegate exactly once to that family's own existing,
       unmodified `run_*_committed_import`, passing through the same
       `connection`, `target_designation`, and `session_factory` this
       coordinator itself received.
       - If an earlier family already failed, every later family is
         recorded `"not_started"` and never invoked at all.
       - If this family's own runner call raises, or itself reports
         `"failed"`, this family is recorded `"failed"` and no further
         family is invoked. That family runner's own already-approved
         rollback/failure-recording behavior is entirely untouched --
         this coordinator adds no additional transaction of its own.
       - On success, this family is recorded `"committed"`.
    5. Release the advisory lock (guaranteed by `bulk_import_advisory_lock`'s
       own `finally`, on every exit path including an exception raised
       from this function itself).

    Returns an immutable `CoordinatedImportRunResult`: `status == "succeeded"`
    only if every requested family committed; `"failed"` the moment any
    family failed -- never a silent partial success, regardless of how
    many earlier families' own commits remain intact (visible per-family
    in `family_outcomes`).
    """
    verified_target = verify_database_target(connection, target_designation)

    with bulk_import_advisory_lock(connection, verified_target):
        prepared = _validate_family_requests(manifest, manifest_schema_version, family_requests)

        outcomes: list[CoordinatedFamilyOutcome] = []
        run_failed = False

        for request, family_manifest in prepared:
            if run_failed:
                outcomes.append(CoordinatedFamilyOutcome(family=request.family, status="not_started"))
                continue

            _, _, run_family = _FAMILY_HANDLERS[request.family]
            try:
                result = run_family(
                    connection=connection, target_designation=target_designation, session_factory=session_factory,
                    manifest=family_manifest, manifest_schema_version=manifest_schema_version,
                    artifacts=request.artifacts, run_id=request.run_id, started_at=request.started_at,
                    pipeline_name=request.pipeline_name,
                )
            except Exception as exc:
                outcomes.append(CoordinatedFamilyOutcome(family=request.family, status="failed", error=f"{type(exc).__name__}: {exc}"))
                run_failed = True
                continue

            if result.status != "succeeded":
                outcomes.append(CoordinatedFamilyOutcome(family=request.family, status="failed", run_result=result))
                run_failed = True
                continue

            outcomes.append(CoordinatedFamilyOutcome(family=request.family, status="committed", run_result=result))

        final_status = "failed" if run_failed else "succeeded"
        return CoordinatedImportRunResult(status=final_status, family_outcomes=tuple(outcomes))
