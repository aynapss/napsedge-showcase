"""
Controlled bulk-import design, Phase 5C1 (docs/controlled_import_rehearsal_design.md
section 4 / section 32, approved for this batch only): the read-only
discovery-to-manifest planner.

**This batch does not authorize a real import, a database connection, or
any write.** This module performs filesystem reads only. It calls each of
the four adapters' own existing, unmodified discovery / load / parse /
build-plan functions -- never a new hashing method, never a new identity
rule, never a new normalization function. It never imports or calls any
`persist_*` function, `unit_of_work`, `src.db.session`, a SQLAlchemy
`Connection`/`Session`, any advisory-lock helper, or anything from
`src.pipeline.bulk_import_coordinator`/`bulk_import_safety`. It never
writes a file (the existing atomic manifest writer,
`write_dry_run_manifest`, may be called separately by a later, separately-
approved batch -- never from here).

ARCHITECTURAL STOP GATE (resolved before writing this module, per
instruction) -- one genuine gap was found and reported, not resolved by
invention:

`docs/controlled_import_rehearsal_design.md` section 4 (item 2) describes
the manifest's own `status` classification as requiring a DATABASE
COMPARISON -- "`(source_root_key, relative_path, content_hash)` against
what already exists in `source_artifacts` (via a caller-supplied
session)" -- to distinguish `"planned_insert"` (no matching row) from
`"idempotent_match"` (an identical one already exists). This batch's own
explicit requirements forbid any database access and exclude
"sessions, connections, database designations" from this module's result
contract, so that comparison cannot be performed here. This was presented
to the user as a real architectural tension (not resolved by invention);
the user's approved resolution:

    This planner NEVER emits `"idempotent_match"`. Every adapter plan
    with `status == "valid"` is classified `"planned_insert"`; `"invalid"`
    maps to `"unresolved"`. `"conflict"` is also never emitted here (it
    likewise requires a database read).

    **Correction (Phase 5C2A, 2026-09-13): a `"partial"` plan is also
    `"planned_insert"` whenever that family's own already-established
    persist function would actually write at least one real row for it**
    -- confirmed by direct inspection of all five persist paths
    (`persist_allstats_import_plan`, `persist_game_log_import_plan`,
    `persist_player_game_log_import_plan`,
    `persist_espn_source_import_plan`,
    `persist_espn_benchmark_import_plan`): none of them reject a row for
    its own identity ambiguity or an unresolved reference -- only a
    plan-level `team_id`/`season_id` failure (i.e. `"invalid"`, never
    `"partial"`) blocks persistence entirely. A `"partial"` plan with
    literally zero persistable rows (the one confirmed real case: an ESPN
    benchmark file where every row's own value was rejected) still falls
    through to `"ambiguous"`, unchanged from Phase 5C1. See
    `_classify_status`'s own docstring for the exact rule and the
    per-family persistability check.

    This is a documented, scope-consistent narrowing, not a missing status
    mapping: every existing committed-import runner's own
    `COMMITTABLE_MANIFEST_STATUSES = frozenset({"planned_insert",
    "idempotent_match"})` already treats the two statuses identically for
    commit-eligibility, and the real insert-vs-idempotent-reuse outcome is
    still decided correctly at commit time by each adapter's own
    natural-key upsert (`SourceArtifactRepository`) -- independent of
    whatever this dry-run planner's manifest said. This planner's `status`
    field gates commit-eligibility only; it never claims to predict the
    exact database outcome of a future commit.

    Precisely: `"planned_insert"` here means "committable, and not
    database-classified" -- it is NEVER evidence that no matching row
    already exists in `source_artifacts`, and must never be read as
    proof of database absence. A future, separately-approved
    database-aware rehearsal preflight step may re-classify a
    `"planned_insert"` artifact to `"idempotent_match"` after actually
    inspecting the designated disposable database; that reclassification
    is out of scope here. This planner remains fully database-independent.

Every real adapter plan status is covered: `AllStatsImportPlan.status`,
`GameLogImportPlan.status`, `PlayerGameLogImportPlan.status`,
`EspnSourceImportPlan.status`, and `EspnBenchmarkImportPlan.status` are
all the identical `Literal["valid", "partial", "invalid"]` (confirmed by
direct inspection of all four adapters) -- so no real adapter state lacks
an approved mapping here.

Other stop-gate points, each resolved by direct inspection rather than
invention:

- **`source_root_key`**: a private, hardcoded literal inside each
  adapter's own persist function, never an importable constant from the
  adapter itself. Three of the four committed-import runners
  (`bulk_import_game_log_runner`, `bulk_import_player_game_log_runner`,
  `bulk_import_espn_runner`) already re-export it as their own
  `EXPECTED_SOURCE_ROOT_KEY` -- imported from there, never re-derived.
  The AllStats runner (Slice 5) never validates `source_root_key` and so
  never exports a constant for it; `"teamdump_cbb"` is confirmed here by
  direct inspection of `allstats_import_adapter._register_artifacts`
  (the adapter's own persist-stage call site).
- **Relative-path rules**: every `discover_*_artifacts` function already
  returns a portable, forward-slash-joined, source-root-relative path
  (`DiscoveredAllStatsWorkbook`/`DiscoveredGameLogFile`/
  `DiscoveredPlayerGameLogFile`/`DiscoveredEspnFile`'s own
  `relative_path`) -- this module passes that value through unchanged
  into every `PlannedArtifact`, and additionally verifies (via
  `os.path.realpath`) that it resolves inside the declared source root,
  catching both `..`-style traversal and a symlink escape -- a real
  filesystem check `PlannedArtifact.__post_init__`'s own string-level
  checks cannot perform on their own.
- **Content-hash authority**: `hashlib.sha256(path.read_bytes()).hexdigest()`,
  computed only inside each adapter's own `load_*_dataframe(s)()` --
  called here, never re-implemented.
- **AllStats grouped-workbook mapping**: one `AllStatsCommitArtifact` per
  team-season workbook (the corrected Slice 5 transaction unit), one
  `PlannedArtifact` per underlying file, sharing the workbook's own single
  dry-run status and an exact `content_hashes` file-to-hash mapping --
  the existing adapter transaction is never split.
- **ESPN source/benchmark separation and enriched-file exclusion**:
  `EspnSourceStatsCommitArtifact` and `EspnBenchmarkCommitArtifact`
  remain two distinct typed tuples in this module's own result (never
  merged); the enriched file (`ESPN_ENRICHED_FILENAME`) is structurally
  excluded -- no `PlannedArtifact`, no commit artifact, no persist path
  exists for it anywhere in this migration, confirmed by Slice 8's own
  artifact-unit precheck and re-confirmed here.
- **`generated_at`**: always caller-supplied to this module's own entry
  point, exactly as `build_dry_run_manifest` already requires -- this
  module never derives a timestamp from wall-clock time itself.
- **Separation from the four future family run IDs**: this module never
  constructs a `FamilyRequest`, never generates or accepts a `run_id`,
  and never imports anything from `bulk_import_coordinator`. It returns
  typed commit-artifact tuples only -- the exact `artifacts=` sequence a
  later, separately-approved batch would pass into each family's own
  `FamilyRequest`.
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from src.exceptions import ValidationError
from src.pipeline.allstats_import_adapter import (
    ALLSTATS_IMPORT_CONTRACT_VERSION,
    AllStatsImportPlan,
    DiscoveredAllStatsWorkbook,
    build_allstats_import_plan,
    discover_allstats_artifacts,
    load_allstats_dataframes,
    parse_allstats_workbook,
)
from src.pipeline.bulk_import_allstats_runner import AllStatsCommitArtifact
from src.pipeline.bulk_import_espn_runner import (
    ESPN_BENCHMARK_FILENAME,
    ESPN_ENRICHED_FILENAME,
    ESPN_RAW_PLAYER_STATS_FILENAME,
    EXPECTED_SOURCE_ROOT_KEY as _ESPN_SOURCE_ROOT_KEY,
    EspnBenchmarkCommitArtifact,
    EspnSourceStatsCommitArtifact,
)
from src.pipeline.bulk_import_game_log_runner import (
    EXPECTED_SOURCE_ROOT_KEY as _TEAM_GAME_LOG_SOURCE_ROOT_KEY,
    TeamGameLogCommitArtifact,
)
from src.pipeline.bulk_import_manifest import (
    ARTIFACT_STATUSES,
    DryRunManifest,
    PlannedArtifact,
    SourceFamilyPlan,
    build_dry_run_manifest,
)
from src.pipeline.bulk_import_player_game_log_runner import (
    EXPECTED_SOURCE_ROOT_KEY as _PLAYER_GAME_LOG_SOURCE_ROOT_KEY,
    PlayerGameLogCommitArtifact,
)
from src.pipeline.espn_import_adapter import (
    ESPN_IMPORT_CONTRACT_VERSION,
    DiscoveredEspnFile,
    discover_espn_artifacts,
    load_espn_dataframe,
    parse_espn_benchmark_workbook,
    parse_espn_player_stats_workbook,
    build_espn_benchmark_import_plan,
    build_espn_source_import_plan,
)
from src.pipeline.game_log_import_adapter import (
    TEAM_GAMELOG_IMPORT_CONTRACT_VERSION,
    DiscoveredGameLogFile,
    build_game_log_import_plan,
    discover_game_log_artifacts,
    load_game_log_dataframe,
    parse_game_log_workbook,
)
from src.pipeline.player_game_log_import_adapter import (
    PLAYER_GAMELOG_IMPORT_CONTRACT_VERSION,
    DiscoveredPlayerGameLogFile,
    build_player_game_log_import_plan,
    discover_player_game_log_artifacts,
    load_player_game_log_dataframe,
    parse_player_game_log_workbook,
    resolve_team_roster_for_player_gamelogs,
)

__all__ = [
    "PLANNER_STATUS_MAP",
    "PlanningIssue",
    "DiscoveryToManifestPlanningResult",
    "build_discovery_to_manifest_plan",
]

#: See module docstring's "ARCHITECTURAL STOP GATE" section -- the
#: user-approved resolution of the idempotent_match/database-access
#: tension. `"conflict"` is likewise never produced here, for the same
#: reason. Both remaining values require a database read this module is
#: forbidden to perform.
#: Fallback mapping, applied whenever the `partial`+persistable override in
#: `_classify_status` below does not apply (i.e. `valid`, `invalid`, or a
#: `partial` plan with no persistable evidence at all -- see Phase 5C2A).
#: Kept exactly as originally approved in Phase 5C1: `partial` still maps
#: to `"ambiguous"` here, since that remains the correct disposition for a
#: partial plan this batch's own persistability check finds nothing to
#: persist.
PLANNER_STATUS_MAP: dict[str, str] = {
    "valid": "planned_insert",
    "partial": "ambiguous",
    "invalid": "unresolved",
}

#: Confirmed by direct inspection of `allstats_import_adapter._register_artifacts`
#: own `persist_allstats_import_plan` call site -- a private, hardcoded
#: literal never exported by that adapter or its own Slice 5 runner
#: (unlike the other three families' runners).
_ALLSTATS_SOURCE_ROOT_KEY = "teamdump_cbb"

_CONTRACT_VERSIONS = {
    "allstats": ALLSTATS_IMPORT_CONTRACT_VERSION,
    "team_game_log": TEAM_GAMELOG_IMPORT_CONTRACT_VERSION,
    "player_game_log": PLAYER_GAMELOG_IMPORT_CONTRACT_VERSION,
    "espn": ESPN_IMPORT_CONTRACT_VERSION,
}


def _classify_status(adapter_status: str, *, family: str, identity: str, persistable: bool) -> str:
    """
    Phase 5C2A correction: a `"partial"` adapter plan is `"planned_insert"`
    whenever `persistable` is True -- i.e. whenever that family's own
    already-established persist function would actually write at least
    one real row for this plan (confirmed by direct inspection of all
    five persist paths: identity ambiguity/unresolved identities never
    block a row's own persistence, only the plan's top-level
    `team_id`/`season_id` being unresolved does, and that condition is
    exactly what makes the adapter's own status `"invalid"`, never
    `"partial"`). `persistable` is computed by the caller from that
    family's own already-existing plan fields (row/observation counts, or
    -- for the ESPN benchmark family, whose plan carries no aggregate
    counts object -- the already-filtered `value_specs` length) -- never a
    new field, never a new adapter concept.

    `planned_insert` here still means only "committable, and not
    database-classified" (see module docstring) -- it is not evidence
    that the underlying data is fully clean. Every adapter issue
    (identity ambiguity, unresolved rows, rejected rows) remains fully
    preserved in the plan and in this planner's own `planning_issues` --
    this function only changes whether the ARTIFACT is committable, never
    whether an issue is resolved or a row's own data is rewritten.

    A `"partial"` plan with no persistable evidence at all (the one
    genuine edge case: an ESPN benchmark file where every row's own
    metric/value was rejected, leaving zero `value_specs`) falls through
    to the original `"ambiguous"` mapping below, unchanged from Phase 5C1.
    """
    if adapter_status == "partial" and persistable:
        return "planned_insert"
    try:
        return PLANNER_STATUS_MAP[adapter_status]
    except KeyError as exc:
        raise ValidationError(
            f"{family} artifact {identity!r} has adapter plan status {adapter_status!r}, "
            f"which has no approved manifest-status mapping in this planner"
        ) from exc


def _verify_within_source_root(source_root: Path, relative_path: str, *, family: str) -> None:
    """
    Real filesystem containment check -- catches both `..`-style
    traversal and a symlink escape via `os.path.realpath`, which
    `PlannedArtifact.__post_init__`'s own string-level checks cannot
    perform (they only ever inspect the literal path text).
    """
    root_real = Path(os.path.realpath(source_root))
    candidate_real = Path(os.path.realpath(root_real / relative_path))
    try:
        candidate_real.relative_to(root_real)
    except ValueError as exc:
        raise ValidationError(
            f"{family} relative_path {relative_path!r} resolves outside the declared "
            f"source root {source_root!r} (resolved to {candidate_real!r}) -- refusing a "
            f"path-traversal or symlink-escape attempt"
        ) from exc


@dataclass(frozen=True)
class PlanningIssue:
    """One planning-time observation, carried alongside (never in place of) each adapter's own validation issues."""

    family: str
    identity: str
    issue_code: str
    severity: str
    description: str


@dataclass(frozen=True)
class DiscoveryToManifestPlanningResult:
    """
    The frozen, complete output of one `build_discovery_to_manifest_plan`
    call. Contains no session, connection, database designation, advisory
    lock, run ID, or mutable client of any kind -- every field is a plain,
    immutable value or a tuple of already-validated, immutable records.
    """

    manifest: DryRunManifest
    allstats_artifacts: tuple[AllStatsCommitArtifact, ...] = field(default_factory=tuple)
    team_game_log_artifacts: tuple[TeamGameLogCommitArtifact, ...] = field(default_factory=tuple)
    player_game_log_artifacts: tuple[PlayerGameLogCommitArtifact, ...] = field(default_factory=tuple)
    espn_source_artifacts: tuple[EspnSourceStatsCommitArtifact, ...] = field(default_factory=tuple)
    espn_benchmark_artifacts: tuple[EspnBenchmarkCommitArtifact, ...] = field(default_factory=tuple)
    planning_issues: tuple[PlanningIssue, ...] = field(default_factory=tuple)

    @property
    def counts(self) -> dict[str, int]:
        """Derived, never stored -- always recomputed from the returned records themselves."""
        return {
            "allstats": len(self.allstats_artifacts),
            "team_game_log": len(self.team_game_log_artifacts),
            "player_game_log": len(self.player_game_log_artifacts),
            "espn_source": len(self.espn_source_artifacts),
            "espn_benchmark": len(self.espn_benchmark_artifacts),
            "planned_artifacts": sum(len(plan.artifacts) for plan in self.manifest.families),
            "planning_issues": len(self.planning_issues),
        }


def _plan_allstats(
    cbb_root: Path, season: int, team_slugs: list[str] | None, source: str,
) -> tuple[tuple[AllStatsCommitArtifact, ...], tuple[PlannedArtifact, ...], list[PlanningIssue]]:
    commit_artifacts: list[AllStatsCommitArtifact] = []
    planned: list[PlannedArtifact] = []
    issues: list[PlanningIssue] = []

    for discovered in discover_allstats_artifacts(cbb_root, season=season, team_slugs=team_slugs):
        identity = f"{discovered.team_slug}/{discovered.season}"
        for meta in discovered.files.values():
            _verify_within_source_root(cbb_root, meta.relative_path, family="allstats")
        try:
            dfs, content_hashes = load_allstats_dataframes(discovered, cbb_root)
        except (FileNotFoundError, OSError) as exc:
            issues.append(PlanningIssue(
                family="allstats", identity=identity, issue_code="planner_file_missing_at_load",
                severity="error", description=f"one or more files for {identity} could not be read at planning time: {exc}",
            ))
            continue

        parsed = parse_allstats_workbook(dfs, discovered.team_slug, season, source=source)
        plan = build_allstats_import_plan(parsed)
        commit_artifacts.append(AllStatsCommitArtifact(discovered=discovered, content_hashes=content_hashes, plan=plan))

        #: AllStats persistability: confirmed by direct inspection of
        #: `persist_allstats_import_plan` -- it writes every
        #: roster/player-stat/team-stat spec unconditionally once
        #: `team_id`/`season_id` resolve (never filtered by individual-row
        #: identity), and only fully skips (zero rows) when they do not --
        #: which is exactly the `"invalid"` condition, never `"partial"`.
        persistable = (plan.counts.roster_rows + plan.counts.player_stat_rows + plan.counts.team_stat_rows) > 0
        status = _classify_status(plan.status, family="allstats", identity=identity, persistable=persistable)
        for label, meta in discovered.files.items():
            planned.append(PlannedArtifact(
                source_root_key=_ALLSTATS_SOURCE_ROOT_KEY,
                relative_path=meta.relative_path,
                content_hash=content_hashes[label],
                status=status,
                metadata={
                    "family": "allstats", "team_slug": discovered.team_slug,
                    "season": str(season), "table": label,
                },
            ))
        for issue in plan.issue_specs:
            issues.append(PlanningIssue(
                family="allstats", identity=identity, issue_code=issue["issue_code"],
                severity=issue["severity"], description=issue["description"],
            ))

    return tuple(commit_artifacts), tuple(planned), issues


def _plan_team_game_log(
    cbb_root: Path, season: int, team_slugs: list[str] | None, source: str,
) -> tuple[tuple[TeamGameLogCommitArtifact, ...], tuple[PlannedArtifact, ...], list[PlanningIssue]]:
    commit_artifacts: list[TeamGameLogCommitArtifact] = []
    planned: list[PlannedArtifact] = []
    issues: list[PlanningIssue] = []

    for discovered in discover_game_log_artifacts(cbb_root, season=season, team_slugs=team_slugs):
        identity = f"{discovered.team_slug}/{discovered.season}"
        _verify_within_source_root(cbb_root, discovered.relative_path, family="team_game_log")
        try:
            df, content_hash = load_game_log_dataframe(discovered, cbb_root)
        except (FileNotFoundError, OSError) as exc:
            issues.append(PlanningIssue(
                family="team_game_log", identity=identity, issue_code="planner_file_missing_at_load",
                severity="error", description=f"{identity} could not be read at planning time: {exc}",
            ))
            continue

        parsed = parse_game_log_workbook(df, discovered.team_slug, season, source=source)
        plan = build_game_log_import_plan(parsed)
        commit_artifacts.append(TeamGameLogCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan))

        #: Team game-log persistability: confirmed by direct inspection of
        #: `persist_game_log_import_plan` -- it writes every observation
        #: spec unconditionally once `team_id`/`season_id` resolve (an
        #: unresolved opponent only skips that one row's own Game/result
        #: reconciliation, never the observation row itself), and only
        #: fully skips (zero rows) when they do not -- the `"invalid"`
        #: condition, never `"partial"`.
        persistable = plan.counts.genuine_observations > 0
        status = _classify_status(plan.status, family="team_game_log", identity=identity, persistable=persistable)
        planned.append(PlannedArtifact(
            source_root_key=_TEAM_GAME_LOG_SOURCE_ROOT_KEY,
            relative_path=discovered.relative_path,
            content_hash=content_hash,
            status=status,
            metadata={"family": "team_game_log", "team_slug": discovered.team_slug, "season": str(season)},
        ))
        for issue in plan.issue_specs:
            issues.append(PlanningIssue(
                family="team_game_log", identity=identity, issue_code=issue["issue_code"],
                severity=issue["severity"], description=issue["description"],
            ))

    return tuple(commit_artifacts), tuple(planned), issues


def _plan_player_game_log(
    cbb_root: Path, season: int, team_slugs: list[str] | None, source: str,
) -> tuple[tuple[PlayerGameLogCommitArtifact, ...], tuple[PlannedArtifact, ...], list[PlanningIssue]]:
    commit_artifacts: list[PlayerGameLogCommitArtifact] = []
    planned: list[PlannedArtifact] = []
    issues: list[PlanningIssue] = []

    discovered_files = discover_player_game_log_artifacts(cbb_root, season=season, team_slugs=team_slugs)
    #: Roster resolution is per-team, reused across every player file for
    #: that team -- exactly the usage pattern
    #: `resolve_team_roster_for_player_gamelogs`'s own docstring
    #: recommends. A plain dict cache, local to this one call, never a
    #: module-level global.
    roster_cache: dict[str, tuple] = {}

    for discovered in discovered_files:
        identity = f"{discovered.team_slug}/{discovered.player_slug}/{discovered.season}"
        _verify_within_source_root(cbb_root, discovered.relative_path, family="player_game_log")

        if discovered.team_slug not in roster_cache:
            roster_cache[discovered.team_slug] = resolve_team_roster_for_player_gamelogs(
                cbb_root, discovered.team_slug, season, source=source,
            )
        team_identity, season_identity, roster_identities = roster_cache[discovered.team_slug]

        try:
            df, content_hash = load_player_game_log_dataframe(discovered, cbb_root)
        except (FileNotFoundError, OSError) as exc:
            issues.append(PlanningIssue(
                family="player_game_log", identity=identity, issue_code="planner_file_missing_at_load",
                severity="error", description=f"{identity} could not be read at planning time: {exc}",
            ))
            continue

        parsed = parse_player_game_log_workbook(
            df, team_identity=team_identity, season_identity=season_identity,
            roster_identities=roster_identities, team_slug=discovered.team_slug,
            player_slug=discovered.player_slug, season=season, source=source,
        )
        plan = build_player_game_log_import_plan(parsed)
        commit_artifacts.append(PlayerGameLogCommitArtifact(discovered=discovered, content_hash=content_hash, plan=plan))

        #: Player game-log persistability: confirmed by direct inspection
        #: of `persist_player_game_log_import_plan` -- it writes every
        #: observation spec unconditionally once `team_id`/`season_id`
        #: resolve, even when the file's own player identity
        #: (`plan.player_id`) never resolved at all (the observation row
        #: is still written with `player_id=None`), and only fully skips
        #: (zero rows) when `team_id`/`season_id` do not resolve -- the
        #: `"invalid"` condition, never `"partial"`.
        persistable = plan.counts.total_source_rows > 0
        status = _classify_status(plan.status, family="player_game_log", identity=identity, persistable=persistable)
        planned.append(PlannedArtifact(
            source_root_key=_PLAYER_GAME_LOG_SOURCE_ROOT_KEY,
            relative_path=discovered.relative_path,
            content_hash=content_hash,
            status=status,
            metadata={
                "family": "player_game_log", "team_slug": discovered.team_slug,
                "player_slug": discovered.player_slug, "season": str(season),
            },
        ))
        for issue in plan.issue_specs:
            issues.append(PlanningIssue(
                family="player_game_log", identity=identity, issue_code=issue["issue_code"],
                severity=issue["severity"], description=issue["description"],
            ))

    return tuple(commit_artifacts), tuple(planned), issues


def _plan_espn(
    espn_root: Path, season: int, source: str,
) -> tuple[tuple[EspnSourceStatsCommitArtifact, ...], tuple[EspnBenchmarkCommitArtifact, ...], tuple[PlannedArtifact, ...], list[PlanningIssue]]:
    source_artifacts: list[EspnSourceStatsCommitArtifact] = []
    benchmark_artifacts: list[EspnBenchmarkCommitArtifact] = []
    planned: list[PlannedArtifact] = []
    issues: list[PlanningIssue] = []

    for discovered in discover_espn_artifacts(espn_root):
        identity = discovered.filename
        _verify_within_source_root(espn_root, discovered.relative_path, family="espn")

        if discovered.filename == ESPN_ENRICHED_FILENAME:
            #: Structural exclusion (Slice 8's own finding, re-confirmed
            #: here): no persist path exists for this file anywhere in
            #: this migration, so no PlannedArtifact and no commit
            #: artifact of any kind is ever constructed for it.
            issues.append(PlanningIssue(
                family="espn", identity=identity, issue_code="espn_enriched_file_structurally_excluded",
                severity="info", description=f"{identity} is the derived/enriched ESPN view -- no persist path exists for it; excluded by design, not an error.",
            ))
            continue

        try:
            df, content_hash = load_espn_dataframe(discovered, espn_root)
        except (FileNotFoundError, OSError) as exc:
            issues.append(PlanningIssue(
                family="espn", identity=identity, issue_code="planner_file_missing_at_load",
                severity="error", description=f"{identity} could not be read at planning time: {exc}",
            ))
            continue

        if discovered.filename == ESPN_RAW_PLAYER_STATS_FILENAME:
            parsed = parse_espn_player_stats_workbook(df, season, source=source)
            plan = build_espn_source_import_plan(parsed)
            source_artifacts.append(EspnSourceStatsCommitArtifact(
                discovered=discovered, content_hash=content_hash, plan=plan, season_id=season,
            ))
            subtype = "source_stats"
            #: ESPN source-stats persistability: confirmed by direct
            #: inspection of `persist_espn_source_import_plan` -- it has
            #: NO top-level identity gate at all (this file is not
            #: team/season-scoped as a whole); it writes every observation
            #: spec unconditionally, regardless of that row's own
            #: team_id/player_id. `"invalid"` here (wrong schema, or
            #: season unresolved) is an early-return in the parser itself,
            #: before any row is ever added -- so `observation_specs`
            #: (and therefore `total_source_rows`) is always 0 for an
            #: `"invalid"` plan and always >0 for a real `"partial"` one.
            persistable = plan.counts.total_source_rows > 0
        elif discovered.filename == ESPN_BENCHMARK_FILENAME:
            parsed = parse_espn_benchmark_workbook(df)
            plan = build_espn_benchmark_import_plan(parsed)
            #: `season_id=None` -- the benchmark file is not season-scoped
            #: (confirmed by the adapter's own docstring and test suite;
            #: the same established convention Slice 8's own fixture fix
            #: recorded), so there is nothing to cross-check it against.
            benchmark_artifacts.append(EspnBenchmarkCommitArtifact(
                discovered=discovered, content_hash=content_hash, plan=plan, season_id=None,
            ))
            subtype = "benchmark"
            #: ESPN benchmark persistability: `EspnBenchmarkImportPlan`
            #: carries no aggregate counts object, so this uses its own
            #: `value_specs` directly -- already filtered, by
            #: `build_espn_benchmark_import_plan` itself, to only rows
            #: with `validation_status == "valid"` (a row with a bad
            #: metric name or a non-numeric mean/std is excluded from
            #: `value_specs`, never guessed or coerced). This is the one
            #: real case where a `"partial"` plan can have zero
            #: persistable rows -- every row's own value rejected, while
            #: the file's own schema was still recognized (so the parser
            #: never returned `"invalid"`).
            persistable = len(plan.value_specs) > 0
        else:
            issues.append(PlanningIssue(
                family="espn", identity=identity, issue_code="espn_unrecognized_discovered_filename",
                severity="error", description=f"{identity} was discovered but matches none of the three known ESPN filenames.",
            ))
            continue

        status = _classify_status(plan.status, family="espn", identity=identity, persistable=persistable)
        planned.append(PlannedArtifact(
            source_root_key=_ESPN_SOURCE_ROOT_KEY,
            relative_path=discovered.relative_path,
            content_hash=content_hash,
            status=status,
            metadata={"family": "espn", "subtype": subtype, "season": str(season)},
        ))
        for issue in plan.issue_specs:
            issues.append(PlanningIssue(
                family="espn", identity=identity, issue_code=issue["issue_code"],
                severity=issue["severity"], description=issue["description"],
            ))

    return tuple(source_artifacts), tuple(benchmark_artifacts), tuple(planned), issues


def _check_no_cross_family_duplicate_identities(all_planned: list[PlannedArtifact]) -> None:
    """
    `SourceFamilyPlan` already refuses a duplicate identity WITHIN one
    family. `source_artifacts`' own natural key is global across the
    whole table, not scoped per family, so this checks across all four
    families too -- defense against an accidental filename collision
    between families that happen to share one `source_root_key`.
    """
    seen: dict[tuple[str, str, str], None] = {}
    for artifact in all_planned:
        if artifact.identity in seen:
            raise ValidationError(f"duplicate artifact identity {artifact.identity} found across more than one source family")
        seen[artifact.identity] = None


def build_discovery_to_manifest_plan(
    *,
    cbb_root: Path,
    espn_root: Path,
    season: int,
    generated_at: dt.datetime,
    team_slugs: list[str] | None = None,
    source: str = "sports_reference",
) -> DiscoveryToManifestPlanningResult:
    """
    Pure, read-only. Performs filesystem reads only (via the four
    adapters' own existing discover/load/parse/build-plan functions);
    makes no database, network, runner, lock, or environment access;
    writes no file. `generated_at` must be supplied by the caller (see
    module docstring) -- never derived from wall-clock time here.

    Returns one frozen `DiscoveryToManifestPlanningResult` containing one
    complete `DryRunManifest` (all four families, built via the existing,
    unmodified `build_dry_run_manifest`) plus the matching typed commit
    artifacts for each family, ready to be passed as a future,
    separately-approved batch's own `FamilyRequest.artifacts`.
    """
    allstats_artifacts, allstats_planned, allstats_issues = _plan_allstats(cbb_root, season, team_slugs, source)
    team_game_log_artifacts, team_game_log_planned, team_game_log_issues = _plan_team_game_log(cbb_root, season, team_slugs, source)
    player_game_log_artifacts, player_game_log_planned, player_game_log_issues = _plan_player_game_log(cbb_root, season, team_slugs, source)
    espn_source_artifacts, espn_benchmark_artifacts, espn_planned, espn_issues = _plan_espn(espn_root, season, source)

    all_planned = list(allstats_planned) + list(team_game_log_planned) + list(player_game_log_planned) + list(espn_planned)
    _check_no_cross_family_duplicate_identities(all_planned)

    families = (
        SourceFamilyPlan(family="allstats", contract_version=_CONTRACT_VERSIONS["allstats"], artifacts=tuple(allstats_planned)),
        SourceFamilyPlan(family="team_game_log", contract_version=_CONTRACT_VERSIONS["team_game_log"], artifacts=tuple(team_game_log_planned)),
        SourceFamilyPlan(family="player_game_log", contract_version=_CONTRACT_VERSIONS["player_game_log"], artifacts=tuple(player_game_log_planned)),
        SourceFamilyPlan(family="espn", contract_version=_CONTRACT_VERSIONS["espn"], artifacts=tuple(espn_planned)),
    )
    manifest = build_dry_run_manifest(generated_at=generated_at, families=families)

    planning_issues = tuple(allstats_issues + team_game_log_issues + player_game_log_issues + espn_issues)

    return DiscoveryToManifestPlanningResult(
        manifest=manifest,
        allstats_artifacts=tuple(sorted(allstats_artifacts, key=lambda a: a.identity)),
        team_game_log_artifacts=tuple(sorted(team_game_log_artifacts, key=lambda a: a.identity)),
        player_game_log_artifacts=tuple(sorted(player_game_log_artifacts, key=lambda a: a.identity)),
        espn_source_artifacts=tuple(sorted(espn_source_artifacts, key=lambda a: a.identity)),
        espn_benchmark_artifacts=tuple(sorted(espn_benchmark_artifacts, key=lambda a: a.identity)),
        planning_issues=planning_issues,
    )
