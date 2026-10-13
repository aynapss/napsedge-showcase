"""
Phase 5B3: read-only import adapter for the three local ESPN files under
the configured `src.paths.ESPN_DIR` root, plus persistence of raw player
evidence and the derived NCAA-cohort benchmark reference.

Explicit stages (pure/impure boundary, mirroring every earlier Phase 5
adapter in this migration):

1. `discover_espn_artifacts` -- pure filesystem check for the three known
   filenames under a caller-supplied root. Never hardcodes the absolute
   path itself.
2. `load_espn_dataframe` -- the one stage that reads file content and
   computes a content hash. Read-only.
3. `classify_espn_file_schema` -- pure. Distinguishes raw player evidence,
   the fully-reproducible derived enrichment (never persisted -- see
   `src.db.espn_models` module docstring), and the derived cohort
   benchmark reference, from column signature alone.
4. `parse_espn_player_stats_workbook` / `parse_espn_benchmark_workbook` --
   pure. Team resolution via
   `src.identity.espn_team_abbreviation_aliases.resolve_espn_team_abbreviation`
   (baseline exact, then this batch's own reviewed crosswalk -- never a
   new matching rule); player resolution via the already-established
   `resolve_espn_player_identity`. No database access.
5. `build_espn_source_import_plan` / `build_espn_benchmark_import_plan` --
   pure, dry-run-safe counts + repository-ready specs.
6. `persist_espn_source_import_plan` / `persist_espn_benchmark_import_plan`
   -- the only stages that touch the database, one `unit_of_work` per
   workbook.

This module implements no new team/player normalization or matching rule
anywhere -- every identity resolution reuses an already-tested function
from `src.identity.*`.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pandas as pd

from src.db.enums import ISSUE_CODE_ESPN_ABBREVIATION_UNRESOLVED, ISSUE_CODE_ESPN_PLAYER_UNRESOLVED
from src.db.espn_repositories import EspnBenchmarkSnapshotRepository, EspnBenchmarkValueRepository, EspnSourceObservationRepository
from src.db.models import Team
from src.db.repositories import DataQualityIssueRepository, DataSourceRepository, PlayerRepository, SourceArtifactRepository, TeamRepository
from src.db.session import unit_of_work
from src.identity.contracts import PlayerIdentity, TeamIdentity, UnresolvedIdentity
from src.identity.espn_player_identity import resolve_espn_player_identity
from src.identity.espn_team_abbreviation_aliases import resolve_espn_team_abbreviation_with_provenance
from src.identity.player_identity import canonical_player_key_v15
from src.identity.season_identity import resolve_season_identity

__all__ = [
    "DiscoveredEspnFile",
    "discover_espn_artifacts",
    "load_espn_dataframe",
    "classify_espn_file_schema",
    "ParsedIssue",
    "ParsedEspnPlayerRow",
    "ParsedEspnPlayerStatsFile",
    "parse_espn_player_stats_workbook",
    "ParsedEspnBenchmarkRow",
    "ParsedEspnBenchmarkFile",
    "parse_espn_benchmark_workbook",
    "EspnSourceImportCounts",
    "EspnSourceImportPlan",
    "build_espn_source_import_plan",
    "EspnBenchmarkImportPlan",
    "build_espn_benchmark_import_plan",
    "EspnSourceImportResult",
    "persist_espn_source_import_plan",
    "EspnBenchmarkImportResult",
    "persist_espn_benchmark_import_plan",
]

TRANSFORMATION_VERSION = "espn_player_benchmarks_v1"

#: Controlled bulk-import design, Slice 1 (docs/controlled_bulk_import_design.md
#: sections 6/16): a static, immutable marker identifying which version of
#: this module's own discover/parse/persist contract (covering BOTH the raw
#: player-stats and benchmark-reference stages) produced a given artifact --
#: deliberately distinct from `TRANSFORMATION_VERSION` above, which only
#: tags the benchmark-snapshot derivation and is already persisted to
#: `espn_benchmark_snapshots.transformation_version`. Not wired into any
#: runtime behavior in this slice -- no parsing, validation, identity, or
#: persistence logic reads this constant yet. Reserved for the future
#: dry-run manifest writer (a separate, not-yet-approved slice) to record
#: and later compare against, so a commit run can refuse to consume a
#: manifest produced by a since-changed adapter contract. Bump this string
#: whenever this module's own parse/persist contract changes in a way that
#: would invalidate a previously-recorded manifest.
ESPN_IMPORT_CONTRACT_VERSION = "1"

_RAW_PLAYER_STATS_FILENAME = "espn_full_player_stats.csv"
_ENRICHED_PLAYER_STATS_FILENAME = "espn_full_player_stats_enriched.csv"
_BENCHMARK_FILENAME = "espn_ncaa_benchmarks.xlsx"

_RAW_STAT_COLUMN_MAP = {
    "RK": "rk", "POS": "pos", "GP": "gp", "MIN": "min_per_game", "PTS": "pts",
    "FGM": "fgm", "FGA": "fga", "FG%": "fg_pct", "3PM": "three_pm", "3PA": "three_pa",
    "3P%": "three_pct", "FTM": "ftm", "FTA": "fta", "FT%": "ft_pct", "REB": "reb",
    "AST": "ast", "STL": "stl", "BLK": "blk", "TO": "turnovers",
}
_RAW_PLAYER_STATS_REQUIRED_COLUMNS = frozenset({"Name", "Team"} | set(_RAW_STAT_COLUMN_MAP))
_BENCHMARK_REQUIRED_COLUMNS = frozenset({"Metric", "NCAA_Mean", "NCAA_Std"})


# ---------------------------------------------------------------------------
# Stage 1-2: discovery + load
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveredEspnFile:
    filename: str
    relative_path: str
    file_size_bytes: int
    filesystem_mtime: dt.datetime


def discover_espn_artifacts(espn_root: Path) -> list[DiscoveredEspnFile]:
    """
    Pure filesystem check for the three known, flat ESPN filenames under
    the caller-supplied, already-configured `espn_root`. Never hardcodes
    that path itself; never opens a file. Deterministic order (fixed
    filename list, not directory iteration order).
    """
    espn_root = Path(espn_root)
    discovered = []
    for filename in (_RAW_PLAYER_STATS_FILENAME, _ENRICHED_PLAYER_STATS_FILENAME, _BENCHMARK_FILENAME):
        file_path = espn_root / filename
        if not file_path.exists():
            continue
        stat = file_path.stat()
        discovered.append(DiscoveredEspnFile(
            filename=filename, relative_path=filename,
            file_size_bytes=stat.st_size, filesystem_mtime=dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc),
        ))
    return discovered


def load_espn_dataframe(discovered: DiscoveredEspnFile, espn_root: Path) -> tuple[pd.DataFrame, str]:
    """Reads the one file and computes its content hash. Read-only; never touches the database."""
    abs_path = Path(espn_root) / discovered.relative_path
    df = pd.read_excel(abs_path) if abs_path.suffix.lower() in (".xlsx", ".xls") else pd.read_csv(abs_path)
    content_hash = hashlib.sha256(abs_path.read_bytes()).hexdigest()
    return df, content_hash


# ---------------------------------------------------------------------------
# Stage 3: schema classification -- raw vs. derived, never conflated
# ---------------------------------------------------------------------------


def classify_espn_file_schema(df: pd.DataFrame) -> str:
    """
    Pure, column-signature-based classification -- never assumes a
    filename. Returns one of: "raw_player_stats" |
    "derived_player_stats_enriched" | "benchmark_reference" |
    "unsupported_or_ambiguous". A file with every raw-stat column PLUS
    extra derived columns (e.g. "eFG", "IMPACT-Total") is the enriched
    file -- never mistaken for the raw one.
    """
    columns = set(df.columns)
    if _BENCHMARK_REQUIRED_COLUMNS <= columns:
        return "benchmark_reference"
    if _RAW_PLAYER_STATS_REQUIRED_COLUMNS <= columns:
        if len(columns) > len(_RAW_PLAYER_STATS_REQUIRED_COLUMNS):
            return "derived_player_stats_enriched"
        return "raw_player_stats"
    return "unsupported_or_ambiguous"


# ---------------------------------------------------------------------------
# Stage 4a: parse raw player-stats workbook
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedIssue:
    issue_code: str
    severity: str
    description: str
    source_row_position: int | None = None
    context: str | None = None


@dataclass(frozen=True)
class ParsedEspnPlayerRow:
    source_row_position: int
    raw_player_name: str
    raw_team_code: str
    espn_athlete_id: str | None
    team_id: int | None
    team_resolution_method: str | None
    player_id: str | None
    player_match_method: str | None
    validation_status: str
    exclusion_reason: str | None
    stat_values: dict[str, float | int | str | None] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedEspnPlayerStatsFile:
    season: int
    schema_variant: str = "raw_player_stats"
    rows: list[ParsedEspnPlayerRow] = field(default_factory=list)
    issues: list[ParsedIssue] = field(default_factory=list)
    status: Literal["valid", "partial", "invalid"] = "valid"
    #: Every distinct resolved team's FULL identity -- registered before
    #: any observation references it, mirroring the game-log adapter's
    #: own "register a real resolved identity before referencing it"
    #: pattern (an ESPN-abbreviation-resolved team may have no prior local
    #: evidence of its own).
    resolved_team_identities: dict[int, TeamIdentity] = field(default_factory=dict)


def _str_or_none(value: object) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    return s if s else None


def _num_or_none(value: object):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return value


def parse_espn_player_stats_workbook(
    df: pd.DataFrame,
    season: int,
    *,
    roster_identities: list[PlayerIdentity] | None = None,
    source: str = "espn",
) -> ParsedEspnPlayerStatsFile:
    """
    Pure. Never mutates `df`. Classifies the schema first (rejects the
    derived/enriched shape and any unsupported shape outright -- this
    function only ever parses the genuine raw file). Resolves each row's
    team via the adapter-local reviewed crosswalk, then its player via the
    already-established `resolve_espn_player_identity`, team+season
    scoped -- never a surname-only match, never fuzzy.
    """
    issues: list[ParsedIssue] = []
    schema_variant = classify_espn_file_schema(df)

    season_identity = resolve_season_identity(season, source=source)
    if isinstance(season_identity, UnresolvedIdentity):
        issues.append(ParsedIssue(issue_code="season_not_resolved", severity="critical", description=f"season {season!r} did not resolve to a canonical SeasonIdentity."))
        return ParsedEspnPlayerStatsFile(season=season, schema_variant=schema_variant, issues=issues, status="invalid")

    if schema_variant != "raw_player_stats":
        issues.append(ParsedIssue(
            issue_code="espn_player_stats_wrong_schema", severity="critical",
            description=f"Expected the raw ESPN player-stats schema; classified as {schema_variant!r} instead.",
        ))
        return ParsedEspnPlayerStatsFile(season=season, schema_variant=schema_variant, issues=issues, status="invalid")

    rows: list[ParsedEspnPlayerRow] = []
    resolved_team_identities: dict[int, TeamIdentity] = {}

    for position, (_, row) in enumerate(df.iterrows()):
        raw_name = _str_or_none(row.get("Name")) or ""
        raw_team_code = _str_or_none(row.get("Team")) or ""
        stat_values = {py_field: _num_or_none(row.get(src_col)) for src_col, py_field in _RAW_STAT_COLUMN_MAP.items()}

        if not raw_name:
            rows.append(ParsedEspnPlayerRow(
                source_row_position=position, raw_player_name=raw_name, raw_team_code=raw_team_code,
                espn_athlete_id=None, team_id=None, team_resolution_method=None, player_id=None,
                player_match_method=None, validation_status="invalid", exclusion_reason="missing_player_name",
                stat_values=stat_values,
            ))
            issues.append(ParsedIssue(issue_code="espn_row_missing_player_name", severity="warning", description=f"Row {position} has no player name.", source_row_position=position))
            continue

        team_resolution = resolve_espn_team_abbreviation_with_provenance(raw_team_code, source=source)
        team_result = team_resolution.identity
        team_method = team_resolution.method  # "exact_alias" | "reviewed_alias" | "ambiguous" | "unresolved" -- reported directly by the resolver, never re-derived here
        if isinstance(team_result, TeamIdentity):
            team_id = team_result.team_id
            resolved_team_identities.setdefault(team_id, team_result)
            # Pass the team's own canonical SR slug (always resolves trivially, exact
            # match) as resolve_espn_player_identity's team_query -- that function
            # internally re-resolves via resolve_team_identity, which does not itself
            # know about this adapter's reviewed-alias fallback.
            player_result = resolve_espn_player_identity(raw_name, _team_sr_slug(team_result), season, source=source)
        else:
            team_id = None
            player_result = team_result  # propagate the same UnresolvedIdentity -- team_unresolved

        if isinstance(player_result, PlayerIdentity):
            player_id = player_result.player_id
            player_method = "team_season_scoped_unique_key"
            linked = None
            if roster_identities:
                from src.identity.espn_player_identity import link_espn_identity_to_roster
                scoped = [p for p in roster_identities if p.team_id == team_id and p.season == season]
                linked = link_espn_identity_to_roster(player_result, scoped, source=f"{source}_to_sr_link")
                if isinstance(linked, PlayerIdentity):
                    player_id = linked.player_id
                    player_method = "existing_canonical_identity_sr_link"
                elif isinstance(linked, UnresolvedIdentity) and linked.reason == "ambiguous":
                    player_id = None
                    player_method = "ambiguous"
            validation_status = "valid" if player_method != "ambiguous" else "conflicting"
            exclusion_reason = None if player_method != "ambiguous" else "player_ambiguous_roster_link"
        elif team_id is None:
            player_id = None
            player_method = "team_unresolved"
            validation_status = "unresolved"
            exclusion_reason = "team_unresolved"
            issues.append(ParsedIssue(issue_code=ISSUE_CODE_ESPN_ABBREVIATION_UNRESOLVED, severity="warning", description=f"Row {position}: team code {raw_team_code!r} did not resolve.", source_row_position=position))
        else:
            player_id = None
            player_method = "unresolved"
            validation_status = "unresolved"
            exclusion_reason = "player_unresolved"
            issues.append(ParsedIssue(issue_code=ISSUE_CODE_ESPN_PLAYER_UNRESOLVED, severity="warning", description=f"Row {position}: player {raw_name!r} did not resolve within team_id={team_id}.", source_row_position=position))

        rows.append(ParsedEspnPlayerRow(
            source_row_position=position, raw_player_name=raw_name, raw_team_code=raw_team_code,
            espn_athlete_id=None, team_id=team_id, team_resolution_method=team_method,
            player_id=player_id, player_match_method=player_method,
            validation_status=validation_status, exclusion_reason=exclusion_reason, stat_values=stat_values,
        ))

    # Same-team duplicate/suffix-conflict guard, reusing the exact pattern
    # already established in
    # `src.transformations.espn_allstats_transformation.link_espn_records_to_allstats`
    # (`duplicate_espn_player_name_same_team`): `build_player_identity`'s
    # own fallback key is deliberately suffix-agnostic (its own docstring:
    # "suffix-agnostic matching key"), so two genuinely different players
    # sharing a base name on the same team (a real same-surname coincidence,
    # or a suffix variant like "Jr."/"III" the base key cannot distinguish)
    # would otherwise silently collide on one `player_id`. Never resolved
    # by roster linking here (this stage only demotes to `ambiguous` when
    # NO roster-confirmed link already separated them) -- "same-surname
    # teammates and suffixed players must remain distinct" per instruction.
    key_counts: dict[tuple[int, str], int] = {}
    for r in rows:
        if r.team_id is not None and r.player_match_method == "team_season_scoped_unique_key":
            key = (r.team_id, canonical_player_key_v15(r.raw_player_name))
            key_counts[key] = key_counts.get(key, 0) + 1

    for i, r in enumerate(rows):
        if r.team_id is None or r.player_match_method != "team_season_scoped_unique_key":
            continue
        key = (r.team_id, canonical_player_key_v15(r.raw_player_name))
        if key_counts.get(key, 0) > 1:
            issues.append(ParsedIssue(
                issue_code=ISSUE_CODE_ESPN_PLAYER_UNRESOLVED, severity="warning",
                description=f"Row {r.source_row_position}: canonical key {key[1]!r} shared by multiple rows within team_id={r.team_id} -- ambiguous without roster-confirmed evidence, never guessed.",
                source_row_position=r.source_row_position,
            ))
            rows[i] = dataclasses.replace(
                r, player_id=None, player_match_method="ambiguous",
                validation_status="conflicting", exclusion_reason="duplicate_or_suffix_conflicting_name_same_team",
            )

    has_critical = any(i.severity == "critical" for i in issues)
    has_error_or_warning = any(i.severity in ("error", "warning") for i in issues)
    status: Literal["valid", "partial", "invalid"] = "invalid" if has_critical else ("partial" if has_error_or_warning else "valid")

    return ParsedEspnPlayerStatsFile(
        season=season, schema_variant=schema_variant, rows=rows, issues=issues, status=status,
        resolved_team_identities=resolved_team_identities,
    )


# ---------------------------------------------------------------------------
# Stage 4b: parse benchmark reference workbook
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedEspnBenchmarkRow:
    source_row_position: int
    metric_name: str
    ncaa_mean: float | None
    ncaa_std: float | None
    validation_status: str
    exclusion_reason: str | None = None


@dataclass(frozen=True)
class ParsedEspnBenchmarkFile:
    schema_variant: str = "benchmark_reference"
    rows: list[ParsedEspnBenchmarkRow] = field(default_factory=list)
    issues: list[ParsedIssue] = field(default_factory=list)
    status: Literal["valid", "partial", "invalid"] = "valid"


def parse_espn_benchmark_workbook(df: pd.DataFrame) -> ParsedEspnBenchmarkFile:
    """Pure. Never mutates `df`. No team/player/season scoping exists in this file -- none is invented here."""
    schema_variant = classify_espn_file_schema(df)
    issues: list[ParsedIssue] = []
    if schema_variant != "benchmark_reference":
        issues.append(ParsedIssue(issue_code="espn_benchmark_wrong_schema", severity="critical", description=f"Expected the benchmark-reference schema; classified as {schema_variant!r} instead."))
        return ParsedEspnBenchmarkFile(schema_variant=schema_variant, issues=issues, status="invalid")

    rows = []
    for position, (_, row) in enumerate(df.iterrows()):
        metric = _str_or_none(row.get("Metric"))
        mean = _num_or_none(row.get("NCAA_Mean"))
        std = _num_or_none(row.get("NCAA_Std"))
        if not metric:
            issues.append(ParsedIssue(issue_code="espn_benchmark_missing_metric", severity="warning", description=f"Row {position} has no Metric name.", source_row_position=position))
            rows.append(ParsedEspnBenchmarkRow(source_row_position=position, metric_name="", ncaa_mean=mean, ncaa_std=std, validation_status="invalid", exclusion_reason="missing_metric_name"))
            continue
        try:
            mean_f = float(mean) if mean is not None else None
            std_f = float(std) if std is not None else None
        except (TypeError, ValueError):
            mean_f = std_f = None
        if mean_f is None or std_f is None:
            issues.append(ParsedIssue(issue_code="espn_benchmark_invalid_value", severity="warning", description=f"Row {position} ({metric!r}) has a non-numeric or missing mean/std.", source_row_position=position))
            rows.append(ParsedEspnBenchmarkRow(source_row_position=position, metric_name=metric, ncaa_mean=mean_f, ncaa_std=std_f, validation_status="invalid", exclusion_reason="invalid_numeric_value"))
            continue
        rows.append(ParsedEspnBenchmarkRow(source_row_position=position, metric_name=metric, ncaa_mean=mean_f, ncaa_std=std_f, validation_status="valid"))

    has_warning = any(i.severity in ("error", "warning") for i in issues)
    status: Literal["valid", "partial", "invalid"] = "partial" if has_warning else "valid"
    return ParsedEspnBenchmarkFile(schema_variant=schema_variant, rows=rows, issues=issues, status=status)


# ---------------------------------------------------------------------------
# Stage 5: build plans (pure, dry-run-safe)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EspnSourceImportCounts:
    total_source_rows: int = 0
    resolved_teams: int = 0
    unresolved_teams: int = 0
    resolved_players: int = 0
    unresolved_players: int = 0
    ambiguous_players: int = 0
    issues: int = 0


def _team_sr_slug(identity: TeamIdentity) -> str:
    sr_source = identity.sources.get("sports_reference")
    return sr_source.raw_value if sr_source else f"unknown-slug-{identity.team_id}"


def _team_spec(identity: TeamIdentity) -> dict:
    return dict(team_id=identity.team_id, display_name=identity.display_name, sr_slug=_team_sr_slug(identity))


@dataclass(frozen=True)
class EspnSourceImportPlan:
    season: int
    resolved_team_specs: list[dict] = field(default_factory=list)
    observation_specs: list[dict] = field(default_factory=list)
    issue_specs: list[dict] = field(default_factory=list)
    counts: EspnSourceImportCounts = field(default_factory=EspnSourceImportCounts)
    status: str = "valid"


def build_espn_source_import_plan(parsed: ParsedEspnPlayerStatsFile) -> EspnSourceImportPlan:
    """Pure -- no database access. Calling only this stage (and stopping) is the adapter's dry-run mode."""
    specs = []
    resolved_teams = unresolved_teams = resolved_players = unresolved_players = ambiguous_players = 0
    for r in parsed.rows:
        resolved_teams += int(r.team_id is not None)
        unresolved_teams += int(r.team_id is None)
        if r.player_match_method == "ambiguous":
            ambiguous_players += 1
        elif r.player_id is not None:
            resolved_players += 1
        else:
            unresolved_players += 1
        specs.append(dict(
            source_row_position=r.source_row_position, raw_player_name=r.raw_player_name,
            raw_team_code=r.raw_team_code, espn_athlete_id=r.espn_athlete_id,
            team_id=r.team_id, team_resolution_method=r.team_resolution_method,
            player_id=r.player_id, player_match_method=r.player_match_method,
            validation_status=r.validation_status, exclusion_reason=r.exclusion_reason,
            stat_values=r.stat_values,
        ))

    issue_specs = [dict(issue_code=i.issue_code, severity=i.severity, description=i.description, context=i.context) for i in parsed.issues]
    counts = EspnSourceImportCounts(
        total_source_rows=len(parsed.rows), resolved_teams=resolved_teams, unresolved_teams=unresolved_teams,
        resolved_players=resolved_players, unresolved_players=unresolved_players, ambiguous_players=ambiguous_players,
        issues=len(issue_specs),
    )
    resolved_team_specs = [_team_spec(identity) for identity in parsed.resolved_team_identities.values()]

    return EspnSourceImportPlan(
        season=parsed.season, resolved_team_specs=resolved_team_specs, observation_specs=specs,
        issue_specs=issue_specs, counts=counts, status=parsed.status,
    )


@dataclass(frozen=True)
class EspnBenchmarkImportPlan:
    value_specs: list[dict] = field(default_factory=list)
    issue_specs: list[dict] = field(default_factory=list)
    status: str = "valid"


def build_espn_benchmark_import_plan(parsed: ParsedEspnBenchmarkFile) -> EspnBenchmarkImportPlan:
    value_specs = [
        dict(metric_name=r.metric_name, ncaa_mean=r.ncaa_mean, ncaa_std=r.ncaa_std)
        for r in parsed.rows if r.validation_status == "valid"
    ]
    issue_specs = [dict(issue_code=i.issue_code, severity=i.severity, description=i.description, context=i.context) for i in parsed.issues]
    return EspnBenchmarkImportPlan(value_specs=value_specs, issue_specs=issue_specs, status=parsed.status)


# ---------------------------------------------------------------------------
# Stage 6: persist (the only stage that touches the database)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EspnSourceImportResult:
    source_artifact_id: int | None
    observations_written: int
    issues_written: int
    status: str


def persist_espn_source_import_plan(
    plan: EspnSourceImportPlan,
    session_factory,
    *,
    discovered: DiscoveredEspnFile,
    content_hash: str,
    season_id: int,
    source_key: str = "espn_player_stats",
    pipeline_run_id: str | None = None,
) -> EspnSourceImportResult:
    """
    The one stage that opens a transaction -- exactly one `unit_of_work`
    per workbook. Registers every resolved team BEFORE the SourceArtifact
    row (which itself carries no team_id, unlike the game-log adapter --
    this file is not team-scoped as a whole) and before any observation
    references it, then registers each row's resolved player (a real,
    canonically-resolved identity, never a guess) before its own
    observation row.
    """
    with unit_of_work(session_factory) as session:
        DataSourceRepository(session).upsert(
            source_key=source_key, display_name="ESPN player-stats (local cache)",
            acquisition_type="static_file", is_rescrapable=False,
            description="Local TeamDump/ESPN/espn_full_player_stats.csv -- see docs/local_data_inventory.md.",
        )

        team_repo = TeamRepository(session)
        for spec in plan.resolved_team_specs:
            existing_team = session.get(Team, spec["team_id"])
            team_repo.upsert(conference_id=existing_team.conference_id if existing_team else None, **spec)

        artifact = SourceArtifactRepository(session).upsert(
            source_key=source_key, source_root_key="teamdump_espn", relative_path=discovered.relative_path,
            original_filename=discovered.filename, file_type="csv",
            file_size_bytes=discovered.file_size_bytes, content_hash=content_hash,
            filesystem_mtime=discovered.filesystem_mtime, collection_timestamp=None,
            season_id=season_id, validation_status="valid",
            point_in_time_classification="current_snapshot_only",
            pipeline_run_id=pipeline_run_id,
        )

        issue_repo = DataQualityIssueRepository(session)
        observation_repo = EspnSourceObservationRepository(session)
        player_repo = PlayerRepository(session)
        written = 0

        for spec in plan.observation_specs:
            spec_copy = dict(spec)
            stat_values = spec_copy.pop("stat_values")

            if spec_copy["player_id"] is not None and spec_copy["team_id"] is not None:
                player_repo.upsert(
                    player_id=spec_copy["player_id"], display_name=spec_copy["raw_player_name"],
                    team_id=spec_copy["team_id"], season_id=season_id,
                )

            observation_repo.upsert(
                source_artifact_id=artifact.id, season_id=season_id,
                pipeline_run_id=pipeline_run_id, **spec_copy, **stat_values,
            )
            written += 1

        for spec in plan.issue_specs:
            issue_repo.append(pipeline_run_id=pipeline_run_id, **spec)

    return EspnSourceImportResult(source_artifact_id=artifact.id, observations_written=written, issues_written=len(plan.issue_specs), status=plan.status)


@dataclass(frozen=True)
class EspnBenchmarkImportResult:
    source_artifact_id: int | None
    snapshot_id: int | None
    values_written: int
    issues_written: int
    status: str


def persist_espn_benchmark_import_plan(
    plan: EspnBenchmarkImportPlan,
    session_factory,
    *,
    discovered: DiscoveredEspnFile,
    content_hash: str,
    season_id: int | None = None,
    source_key: str = "espn_ncaa_benchmarks",
    pipeline_run_id: str | None = None,
) -> EspnBenchmarkImportResult:
    """Not team/player-scoped -- registers only DataSource, SourceArtifact, one snapshot row, and its value rows."""
    with unit_of_work(session_factory) as session:
        DataSourceRepository(session).upsert(
            source_key=source_key, display_name="ESPN NCAA cohort benchmarks (local cache)",
            acquisition_type="static_file", is_rescrapable=False,
            description="Local TeamDump/ESPN/espn_ncaa_benchmarks.xlsx -- derived cohort reference, see docs/database_schema.md.",
        )
        artifact = SourceArtifactRepository(session).upsert(
            source_key=source_key, source_root_key="teamdump_espn", relative_path=discovered.relative_path,
            original_filename=discovered.filename, file_type="xlsx",
            file_size_bytes=discovered.file_size_bytes, content_hash=content_hash,
            filesystem_mtime=discovered.filesystem_mtime, collection_timestamp=None,
            season_id=season_id, validation_status="valid",
            point_in_time_classification="current_snapshot_only",
            pipeline_run_id=pipeline_run_id,
        )

        snapshot = EspnBenchmarkSnapshotRepository(session).upsert(
            source_artifact_id=artifact.id, transformation_version=TRANSFORMATION_VERSION,
            validation_status="valid" if plan.status == "valid" else plan.status,
            season_id=season_id, pipeline_run_id=pipeline_run_id,
        )

        value_repo = EspnBenchmarkValueRepository(session)
        for spec in plan.value_specs:
            value_repo.upsert(snapshot_id=snapshot.id, **spec)

        issue_repo = DataQualityIssueRepository(session)
        for spec in plan.issue_specs:
            issue_repo.append(pipeline_run_id=pipeline_run_id, **spec)

    return EspnBenchmarkImportResult(
        source_artifact_id=artifact.id, snapshot_id=snapshot.id,
        values_written=len(plan.value_specs), issues_written=len(plan.issue_specs), status=plan.status,
    )
