"""
Phase 5B2: read-only import adapter for local CBB team-perspective game
logs, plus cross-file reconciliation into canonical game results.

Ten explicit stages (Phase 5B2 instruction), grouped into the same five
pure/impure boundaries this migration has used since Phase 5B1:

1-2. `discover_game_log_artifacts` / `load_game_log_dataframe` -- pure
     filesystem walk, then the one stage that reads file content and
     computes a content hash. Read-only.
3-8. `parse_game_log_workbook` -- pure. Classifies the schema variant,
     excludes the confirmed season-total row (by content, never by
     position), resolves the perspective team + season once, resolves
     each row's opponent through the canonical resolver, builds a
     provisional `game_id` for every row with enough evidence, and
     extracts each row's stat line. No database access.
9.  `reconcile_game_observations` -- pure. Takes 1-2 already-resolved
     observations for the SAME `game_id` (one per perspective team) and
     decides the canonical result: agreement, single-sided, or conflict.
     Never mutates its input, never touches the database.
10. `persist_game_log_import_plan` -- the only stage that touches the
     database. Persists this file's own observations, then re-runs
     reconciliation against whatever the *other* team's observation for
     each shared `game_id` already says in the database (if anything),
     upserting `game_results` accordingly -- all in one transaction per
     workbook.

`build_game_log_import_plan` sits between parsing and persistence (pure,
dry-run-safe counts + repository-ready specs), matching Phase 5B1's own
four/five-stage shape.

VENUE, GAME IDENTITY, AND THE SEASON-TOTAL DEFECT: see the extensive
module docstring in `src.db.game_log_models` for the full, real-data-
confirmed contract this module implements -- not repeated here.

Every identity resolution below reuses an already-tested function from
`src.identity.*` -- this module implements no new matching rule, no new
normalization function, and no fuzzy matching of any kind.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pandas as pd

from src.db.enums import (
    ISSUE_CODE_GAMELOG_MIRRORED_CONFLICT,
    ISSUE_CODE_GAMELOG_OPPONENT_UNRESOLVED,
    ISSUE_CODE_GAMELOG_SEASON_TOTAL_ROW_EXCLUDED,
)
from src.db.session import unit_of_work
from src.db.models import Team
from src.db.repositories import DataQualityIssueRepository, DataSourceRepository, GameRepository, SourceArtifactRepository, TeamRepository
from src.db.game_log_models import TeamGameLogObservation
from src.db.game_log_repositories import GameResultRepository, TeamGameLogObservationRepository, TeamGameStatLineRepository
from src.exceptions import ValidationError
from src.identity.contracts import SeasonIdentity, TeamIdentity, UnresolvedIdentity
from src.identity.game_identity import build_provisional_game_identity
from src.identity.season_identity import resolve_season_identity
from src.identity.team_identity import resolve_team_identity

__all__ = [
    "DiscoveredGameLogFile",
    "discover_game_log_artifacts",
    "load_game_log_dataframe",
    "ParsedIssue",
    "ParsedGameLogRow",
    "ParsedGameLogFile",
    "parse_game_log_workbook",
    "GameLogImportCounts",
    "GameLogImportPlan",
    "build_game_log_import_plan",
    "ObservationForReconciliation",
    "ReconciliationResult",
    "reconcile_game_observations",
    "GameLogImportResult",
    "persist_game_log_import_plan",
]

#: Controlled bulk-import design, Slice 1 (docs/controlled_bulk_import_design.md
#: sections 6/16): a static, immutable marker identifying which version of
#: this module's own discover/parse/persist contract produced a given
#: artifact. Not wired into any runtime behavior in this slice -- no
#: parsing, validation, identity, or persistence logic reads this constant
#: yet. Reserved for the future dry-run manifest writer (a separate,
#: not-yet-approved slice) to record and later compare against, so a
#: commit run can refuse to consume a manifest produced by a
#: since-changed adapter contract. Bump this string whenever this
#: module's own parse/persist contract changes in a way that would
#: invalidate a previously-recorded manifest.
TEAM_GAMELOG_IMPORT_CONTRACT_VERSION = "1"

_BASIC_COLUMNS = ("game_num", "date", "opponent", "pts_scored", "pts_allowed", "ORtg", "DRtg", "net_rtg")
_ADVANCED_ONLY_MARKER_COLUMN = "location"  # confirmed present only in the 16-team richer schema

_STAT_COLUMN_MAP = {
    "ORtg": "off_rating", "DRtg": "def_rating", "net_rtg": "net_rating",
    "pace": "pace", "fta_rate": "fta_rate", "fg3a_rate": "fg3a_rate", "ts_pct": "ts_pct",
    "trb_pct": "trb_pct", "ast_pct": "ast_pct", "stl_pct": "stl_pct", "blk_pct": "blk_pct",
    "efg_pct": "efg_pct", "tov_pct": "tov_pct", "orb_pct": "orb_pct", "ft_rate": "ft_rate",
    "opp_efg_pct": "opp_efg_pct", "opp_tov_pct": "opp_tov_pct", "opp_orb_pct": "opp_orb_pct",
    "opp_ft_rate": "opp_ft_rate", "margin": "margin",
}


# ---------------------------------------------------------------------------
# Stage 1-2: discovery + load
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveredGameLogFile:
    team_slug: str
    season: int
    relative_path: str
    file_size_bytes: int
    filesystem_mtime: dt.datetime


def discover_game_log_artifacts(
    cbb_root: Path, *, season: int, team_slugs: list[str] | None = None,
) -> list[DiscoveredGameLogFile]:
    """Pure filesystem walk under the caller-supplied, already-configured `cbb_root`. Never hardcodes that path itself; never opens the file."""
    cbb_root = Path(cbb_root)
    if team_slugs is None:
        team_slugs = sorted(p.name for p in cbb_root.iterdir() if p.is_dir())

    discovered = []
    for slug in team_slugs:
        file_path = cbb_root / slug / f"{slug}_{season}_game_log.csv"
        if not file_path.exists():
            continue
        stat = file_path.stat()
        discovered.append(DiscoveredGameLogFile(
            team_slug=slug, season=season, relative_path=f"{slug}/{slug}_{season}_game_log.csv",
            file_size_bytes=stat.st_size, filesystem_mtime=dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc),
        ))
    return discovered


def load_game_log_dataframe(discovered: DiscoveredGameLogFile, cbb_root: Path) -> tuple[pd.DataFrame, str]:
    """Reads the one CSV and computes its content hash. Read-only; never touches the database."""
    abs_path = Path(cbb_root) / discovered.relative_path
    df = pd.read_csv(abs_path)
    content_hash = hashlib.sha256(abs_path.read_bytes()).hexdigest()
    return df, content_hash


# ---------------------------------------------------------------------------
# Stage 3-8: parse
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedIssue:
    issue_code: str
    severity: str
    description: str
    source_row_position: int | None = None
    context: str | None = None


@dataclass(frozen=True)
class ParsedGameLogRow:
    source_row_position: int
    raw_opponent_name: str
    opponent_team_id: int | None
    game_id: int | None
    game_date: dt.date | None
    venue_status: str  # home | away | neutral | unknown
    raw_result_marker: str | None
    points_scored: int | None
    points_allowed: int | None
    overtime_marker: str | None
    game_type_raw: str | None
    validation_status: str
    exclusion_reason: str | None
    stat_values: dict[str, float | None] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedGameLogFile:
    team_slug: str
    season: int
    team_identity: TeamIdentity | UnresolvedIdentity
    season_identity: SeasonIdentity | UnresolvedIdentity
    schema_variant: str = "basic"
    rows: list[ParsedGameLogRow] = field(default_factory=list)
    excluded_row_count: int = 0
    issues: list[ParsedIssue] = field(default_factory=list)
    status: Literal["valid", "partial", "invalid"] = "valid"
    #: Every distinct resolved opponent's FULL identity (not just its
    #: int id) -- most real opponents have no local AllStats/game-log
    #: evidence of their own in this batch's scope, so they are very
    #: likely NOT already registered in `teams`. Registering them from
    #: this real, canonically-resolved identity (never a guess) is what
    #: satisfies "never create a team merely to complete a game row" in
    #: the other direction: this is a REAL team's REAL canonical id, only
    #: newly-registered here because nothing else has registered it yet.
    resolved_opponent_identities: dict[int, TeamIdentity] = field(default_factory=dict)


def classify_schema_variant(df: pd.DataFrame) -> str:
    """Confirmed real-data classifier: the `advanced` schema is the only one carrying an explicit `location` column."""
    return "advanced" if _ADVANCED_ONLY_MARKER_COLUMN in df.columns else "basic"


def _is_season_total_row(row: pd.Series) -> bool:
    """
    Deterministic, content-based classifier for the confirmed season-
    total-row defect (all 128 real `basic`-schema game logs). Detects the
    row from its own contents -- blank `date` AND blank `opponent`,
    simultaneously -- never from being the final row or from any assumed
    sort order (a real game row always has both; no real game is missing
    both its date and its opponent at once).
    """
    date_value = row.get("date")
    opponent_value = row.get("opponent")
    date_blank = date_value is None or (isinstance(date_value, float) and pd.isna(date_value)) or (isinstance(date_value, str) and not date_value.strip())
    opponent_blank = opponent_value is None or (isinstance(opponent_value, float) and pd.isna(opponent_value)) or (isinstance(opponent_value, str) and not opponent_value.strip())
    return date_blank and opponent_blank


def _parse_date(raw_value: object) -> dt.date | None:
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    try:
        return dt.date.fromisoformat(raw_value.strip())
    except ValueError:
        return None


def _parse_venue(row: pd.Series, *, schema_variant: str) -> str:
    """Only ever set from the advanced schema's own explicit `location` column -- never inferred from team ordering or score. Confirmed real SR convention: blank = home, '@' = away, 'N' = neutral."""
    if schema_variant != "advanced":
        return "unknown"
    raw = row.get("location")
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return "home"
    raw = str(raw).strip()
    if raw == "@":
        return "away"
    if raw.upper() == "N":
        return "neutral"
    return "unknown"


def _int_or_none(value: object) -> int | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: object) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    return s if s else None


def _extract_stat_values(row: pd.Series) -> dict[str, float | None]:
    values: dict[str, float | None] = {}
    for source_col, target_field in _STAT_COLUMN_MAP.items():
        if source_col not in row.index:
            continue
        raw = row.get(source_col)
        if raw is None or (isinstance(raw, float) and pd.isna(raw)):
            values[target_field] = None
            continue
        try:
            values[target_field] = float(raw)
        except (TypeError, ValueError):
            values[target_field] = None
    return values


def parse_game_log_workbook(
    df: pd.DataFrame,
    team_query: str,
    season: int,
    *,
    source: str = "sports_reference",
) -> ParsedGameLogFile:
    """
    Pure. Never mutates `df` (only ever iterated). Resolves team + season
    once via the canonical, non-fuzzy resolvers; resolves each row's
    opponent the same way. Excludes the season-total row by content
    before it can ever become an observation, a game, or a stat line.
    """
    issues: list[ParsedIssue] = []
    schema_variant = classify_schema_variant(df)

    season_identity = resolve_season_identity(season, source=source)
    if isinstance(season_identity, UnresolvedIdentity):
        issues.append(ParsedIssue(issue_code="season_not_resolved", severity="critical", description=f"season {season!r} did not resolve to a canonical SeasonIdentity."))
        return ParsedGameLogFile(
            team_slug=team_query, season=season, team_identity=UnresolvedIdentity(query=team_query, source=source, reason="not_found"),
            season_identity=season_identity, schema_variant=schema_variant, issues=issues, status="invalid",
        )

    team_identity = resolve_team_identity(team_query, source=source)
    if isinstance(team_identity, UnresolvedIdentity):
        issues.append(ParsedIssue(issue_code="team_not_resolved", severity="critical", description=f"team query {team_query!r} did not resolve to a canonical TeamIdentity (reason={team_identity.reason!r})."))
        return ParsedGameLogFile(
            team_slug=team_query, season=season, team_identity=team_identity, season_identity=season_identity,
            schema_variant=schema_variant, issues=issues, status="invalid",
        )

    if df.empty or "Player" in df.columns or "date" not in df.columns or "opponent" not in df.columns:
        # A game log has "date"/"opponent" columns, never "Player" -- a defensive guard against
        # accidentally being handed an AllStats-shaped table instead.
        issues.append(ParsedIssue(issue_code="game_log_missing_required_columns", severity="critical", description="Missing required 'date'/'opponent' columns, or file is empty."))
        return ParsedGameLogFile(
            team_slug=team_query, season=season, team_identity=team_identity, season_identity=season_identity,
            schema_variant=schema_variant, issues=issues, status="invalid",
        )

    rows: list[ParsedGameLogRow] = []
    resolved_opponent_identities: dict[int, TeamIdentity] = {}
    excluded_row_count = 0
    for position, (_, row) in enumerate(df.iterrows()):
        if _is_season_total_row(row):
            excluded_row_count += 1
            issues.append(ParsedIssue(
                issue_code=ISSUE_CODE_GAMELOG_SEASON_TOTAL_ROW_EXCLUDED, severity="info",
                description=(
                    f"Row {position} has blank date and opponent with season-aggregate points "
                    f"(pts_scored={row.get('pts_scored')!r}, pts_allowed={row.get('pts_allowed')!r}) -- "
                    f"excluded as the confirmed season-total-row defect, never persisted as a game."
                ),
                source_row_position=position, context=f"pts_scored={row.get('pts_scored')!r},pts_allowed={row.get('pts_allowed')!r}",
            ))
            continue

        raw_opponent = row.get("opponent")
        raw_opponent_name = str(raw_opponent) if raw_opponent is not None and not (isinstance(raw_opponent, float) and pd.isna(raw_opponent)) else ""
        game_date = _parse_date(row.get("date"))
        venue_status = _parse_venue(row, schema_variant=schema_variant)
        raw_result_marker = _str_or_none(row.get("result"))
        points_scored = _int_or_none(row.get("pts_scored"))
        points_allowed = _int_or_none(row.get("pts_allowed"))
        overtime_marker = _str_or_none(row.get("overtimes"))
        game_type_raw = _str_or_none(row.get("game_type"))
        stat_values = _extract_stat_values(row)

        validation_status = "valid"
        exclusion_reason = None
        opponent_team_id = None
        game_id = None

        if not raw_opponent_name:
            validation_status = "invalid"
            exclusion_reason = "missing_opponent_name"
            issues.append(ParsedIssue(issue_code="game_log_row_missing_opponent", severity="warning", description=f"Row {position} has no opponent name.", source_row_position=position))
        else:
            opponent_identity = resolve_team_identity(raw_opponent_name, source=source)
            if isinstance(opponent_identity, TeamIdentity):
                if opponent_identity.team_id == team_identity.team_id:
                    validation_status = "invalid"
                    exclusion_reason = "same_team_game"
                    issues.append(ParsedIssue(issue_code="game_log_same_team", severity="error", description=f"Row {position}: opponent resolved to the SAME team as the perspective team -- rejected.", source_row_position=position))
                else:
                    opponent_team_id = opponent_identity.team_id
                    resolved_opponent_identities.setdefault(opponent_identity.team_id, opponent_identity)
            else:
                validation_status = "unresolved"
                exclusion_reason = "opponent_not_resolved"
                issues.append(ParsedIssue(
                    issue_code=ISSUE_CODE_GAMELOG_OPPONENT_UNRESOLVED, severity="warning",
                    description=f"Row {position}: opponent {raw_opponent_name!r} did not resolve (reason={opponent_identity.reason!r}).",
                    source_row_position=position,
                ))

        if game_date is None and validation_status == "valid":
            validation_status = "invalid"
            exclusion_reason = "unparseable_date"
            issues.append(ParsedIssue(issue_code="game_log_invalid_date", severity="error", description=f"Row {position}: date {row.get('date')!r} could not be parsed as an ISO date.", source_row_position=position))

        if opponent_team_id is not None and game_date is not None and validation_status == "valid":
            identity = build_provisional_game_identity(
                home_team_id=team_identity.team_id, away_team_id=opponent_team_id,
                season=season_identity.season_id, game_date=game_date.isoformat(), neutral_site=True,
            )
            if isinstance(identity, UnresolvedIdentity):
                validation_status = "invalid"
                exclusion_reason = "game_identity_not_constructible"
                issues.append(ParsedIssue(issue_code="game_log_game_identity_failed", severity="error", description=f"Row {position}: could not construct a game identity.", source_row_position=position))
            else:
                game_id = identity.game_id

        rows.append(ParsedGameLogRow(
            source_row_position=position, raw_opponent_name=raw_opponent_name or str(raw_opponent),
            opponent_team_id=opponent_team_id, game_id=game_id, game_date=game_date, venue_status=venue_status,
            raw_result_marker=raw_result_marker, points_scored=points_scored, points_allowed=points_allowed,
            overtime_marker=overtime_marker, game_type_raw=game_type_raw, validation_status=validation_status,
            exclusion_reason=exclusion_reason, stat_values=stat_values,
        ))

    has_critical = any(i.severity == "critical" for i in issues)
    has_error_or_warning = any(i.severity in ("error", "warning") for i in issues)
    status: Literal["valid", "partial", "invalid"] = "invalid" if has_critical else ("partial" if has_error_or_warning else "valid")

    return ParsedGameLogFile(
        team_slug=team_query, season=season, team_identity=team_identity, season_identity=season_identity,
        schema_variant=schema_variant, rows=rows, excluded_row_count=excluded_row_count, issues=issues, status=status,
        resolved_opponent_identities=resolved_opponent_identities,
    )


# ---------------------------------------------------------------------------
# Stage: build plan (pure, dry-run-safe)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GameLogImportCounts:
    total_source_rows: int = 0
    season_total_rows_excluded: int = 0
    genuine_observations: int = 0
    resolved_opponents: int = 0
    unresolved_opponents: int = 0
    games_constructed: int = 0
    issues: int = 0


@dataclass(frozen=True)
class GameLogImportPlan:
    team_slug: str
    season: int
    team_id: int | None
    season_id: int | None
    schema_variant: str
    resolved_team_specs: list[dict] = field(default_factory=list)  # perspective team + every distinct resolved opponent -- registered before any observation/result references them
    observation_specs: list[dict] = field(default_factory=list)
    issue_specs: list[dict] = field(default_factory=list)
    counts: GameLogImportCounts = field(default_factory=GameLogImportCounts)
    status: str = "valid"


def _team_spec(identity: TeamIdentity) -> dict:
    sr_source = identity.sources.get("sports_reference")
    return dict(
        team_id=identity.team_id, display_name=identity.display_name,
        sr_slug=sr_source.raw_value if sr_source else f"unknown-slug-{identity.team_id}",
    )


def build_game_log_import_plan(parsed: ParsedGameLogFile) -> GameLogImportPlan:
    """Pure -- no database access. Calling only this stage (and stopping) is the adapter's dry-run mode."""
    team_id = parsed.team_identity.team_id if isinstance(parsed.team_identity, TeamIdentity) else None
    season_id = parsed.season_identity.season_id if isinstance(parsed.season_identity, SeasonIdentity) else None

    specs = []
    resolved = unresolved = games_constructed = 0
    for r in parsed.rows:
        resolved += int(r.opponent_team_id is not None)
        unresolved += int(r.opponent_team_id is None)
        games_constructed += int(r.game_id is not None)
        specs.append(dict(
            source_row_position=r.source_row_position, raw_opponent_name=r.raw_opponent_name,
            opponent_team_id=r.opponent_team_id, game_id=r.game_id, game_date=r.game_date,
            venue_status=r.venue_status, raw_result_marker=r.raw_result_marker,
            points_scored=r.points_scored, points_allowed=r.points_allowed,
            overtime_marker=r.overtime_marker, game_type_raw=r.game_type_raw,
            schema_variant=parsed.schema_variant, validation_status=r.validation_status,
            exclusion_reason=r.exclusion_reason, stat_values=r.stat_values,
        ))

    issue_specs = [dict(issue_code=i.issue_code, severity=i.severity, description=i.description, context=i.context) for i in parsed.issues]

    counts = GameLogImportCounts(
        total_source_rows=len(parsed.rows) + parsed.excluded_row_count,
        season_total_rows_excluded=parsed.excluded_row_count,
        genuine_observations=len(parsed.rows), resolved_opponents=resolved, unresolved_opponents=unresolved,
        games_constructed=games_constructed, issues=len(issue_specs),
    )

    resolved_team_specs = []
    if isinstance(parsed.team_identity, TeamIdentity):
        resolved_team_specs.append(_team_spec(parsed.team_identity))
    for opponent_identity in parsed.resolved_opponent_identities.values():
        resolved_team_specs.append(_team_spec(opponent_identity))

    return GameLogImportPlan(
        team_slug=parsed.team_slug, season=parsed.season, team_id=team_id, season_id=season_id,
        schema_variant=parsed.schema_variant, resolved_team_specs=resolved_team_specs,
        observation_specs=specs, issue_specs=issue_specs, counts=counts, status=parsed.status,
    )


# ---------------------------------------------------------------------------
# Stage 9: reconciliation (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservationForReconciliation:
    observation_id: int | None
    team_id: int
    points_scored: int | None
    points_allowed: int | None
    venue_status: str
    overtime_marker: str | None
    raw_result_marker: str | None


@dataclass(frozen=True)
class ReconciliationResult:
    result_status: str  # reconciled | single_sided_provisional | conflicting
    validation_status: str
    team_a_score: int | None
    team_b_score: int | None
    winner_team_id: int | None
    loser_team_id: int | None
    overtime_status: str | None
    source_count: int
    team_a_observation_id: int | None
    team_b_observation_id: int | None
    conflict_reasons: list[str] = field(default_factory=list)


_COMPLEMENTARY_VENUES = {("home", "away"), ("away", "home"), ("neutral", "neutral")}


def reconcile_game_observations(
    observations: list[ObservationForReconciliation], *, team_a_id: int, team_b_id: int,
) -> ReconciliationResult:
    """
    Pure. `observations` holds 1 or 2 already-resolved observations for
    the SAME `game_id` (never more than one per perspective team -- a
    caller passing more than two, or two from the same team, is a
    programmer error, rejected explicitly). Never mutates its input.
    """
    if team_a_id == team_b_id:
        raise ValidationError("reconcile_game_observations: team_a_id and team_b_id must be distinct -- cannot reconcile a game against itself")
    if len(observations) > 2:
        raise ValidationError(f"reconcile_game_observations: expected at most 2 observations, got {len(observations)}")
    if len(observations) == 2 and observations[0].team_id == observations[1].team_id:
        raise ValidationError("reconcile_game_observations: both observations belong to the same team -- cannot reconcile a game against itself")

    by_team = {o.team_id: o for o in observations}
    obs_a = by_team.get(team_a_id)
    obs_b = by_team.get(team_b_id)

    if obs_a is not None and obs_b is None:
        return ReconciliationResult(
            result_status="single_sided_provisional", validation_status="partial",
            team_a_score=obs_a.points_scored, team_b_score=obs_a.points_allowed,
            winner_team_id=(team_a_id if _wins(obs_a) else (team_b_id if _wins(obs_a) is False else None)),
            loser_team_id=(team_b_id if _wins(obs_a) else (team_a_id if _wins(obs_a) is False else None)),
            overtime_status=_overtime_status([obs_a]), source_count=1,
            team_a_observation_id=obs_a.observation_id, team_b_observation_id=None,
        )
    if obs_b is not None and obs_a is None:
        return ReconciliationResult(
            result_status="single_sided_provisional", validation_status="partial",
            team_a_score=obs_b.points_allowed, team_b_score=obs_b.points_scored,
            winner_team_id=(team_b_id if _wins(obs_b) else (team_a_id if _wins(obs_b) is False else None)),
            loser_team_id=(team_a_id if _wins(obs_b) else (team_b_id if _wins(obs_b) is False else None)),
            overtime_status=_overtime_status([obs_b]), source_count=1,
            team_a_observation_id=None, team_b_observation_id=obs_b.observation_id,
        )
    if obs_a is None and obs_b is None:
        raise ValidationError("reconcile_game_observations: no observation supplied for either team")

    # Both sides present -- check agreement.
    conflicts: list[str] = []
    if obs_a.points_scored != obs_b.points_allowed or obs_a.points_allowed != obs_b.points_scored:
        conflicts.append("score_mismatch")
    if obs_a.venue_status != "unknown" and obs_b.venue_status != "unknown":
        if (obs_a.venue_status, obs_b.venue_status) not in _COMPLEMENTARY_VENUES:
            conflicts.append("venue_mismatch")
    ot_a = bool(obs_a.overtime_marker)
    ot_b = bool(obs_b.overtime_marker)
    if obs_a.overtime_marker is not None and obs_b.overtime_marker is not None and ot_a != ot_b:
        conflicts.append("overtime_mismatch")
    if obs_a.raw_result_marker and obs_b.raw_result_marker:
        a_won_marker = obs_a.raw_result_marker.upper() == "W"
        b_won_marker = obs_b.raw_result_marker.upper() == "W"
        if a_won_marker == b_won_marker:  # both W or both L -- can't both be true
            conflicts.append("result_marker_mismatch")

    if conflicts:
        return ReconciliationResult(
            result_status="conflicting", validation_status="conflicting",
            team_a_score=None, team_b_score=None, winner_team_id=None, loser_team_id=None,
            overtime_status=None, source_count=2,
            team_a_observation_id=obs_a.observation_id, team_b_observation_id=obs_b.observation_id,
            conflict_reasons=conflicts,
        )

    team_a_score, team_b_score = obs_a.points_scored, obs_a.points_allowed
    winner_team_id = loser_team_id = None
    if team_a_score is not None and team_b_score is not None and team_a_score != team_b_score:
        winner_team_id = team_a_id if team_a_score > team_b_score else team_b_id
        loser_team_id = team_b_id if winner_team_id == team_a_id else team_a_id

    return ReconciliationResult(
        result_status="reconciled", validation_status="valid",
        team_a_score=team_a_score, team_b_score=team_b_score,
        winner_team_id=winner_team_id, loser_team_id=loser_team_id,
        overtime_status=_overtime_status([obs_a, obs_b]), source_count=2,
        team_a_observation_id=obs_a.observation_id, team_b_observation_id=obs_b.observation_id,
    )


def _wins(obs: ObservationForReconciliation) -> bool | None:
    if obs.raw_result_marker:
        return obs.raw_result_marker.upper() == "W"
    if obs.points_scored is not None and obs.points_allowed is not None and obs.points_scored != obs.points_allowed:
        return obs.points_scored > obs.points_allowed
    return None


def _overtime_status(obs_list: list[ObservationForReconciliation]) -> str | None:
    markers = [bool(o.overtime_marker) for o in obs_list if o.overtime_marker is not None]
    if not markers:
        return "unknown"
    return "overtime" if any(markers) else "regulation"


# ---------------------------------------------------------------------------
# Stage 10: persist (the only stage that touches the database)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GameLogImportResult:
    team_slug: str
    season: int
    source_artifact_id: int | None
    observations_written: int
    stat_lines_written: int
    results_upserted: int
    issues_written: int
    status: str


def persist_game_log_import_plan(
    plan: GameLogImportPlan,
    session_factory,
    *,
    discovered: DiscoveredGameLogFile,
    content_hash: str,
    source_key: str = "sports_reference_game_log",
    pipeline_run_id: str | None = None,
) -> GameLogImportResult:
    """
    The one stage that opens a transaction -- exactly one `unit_of_work`
    per workbook. Persists this team's own observations and stat lines,
    then re-reconciles every `game_id` this file touched against
    whichever *other* team's observation the database already has for
    it, upserting `game_results` accordingly.
    """
    with unit_of_work(session_factory) as session:
        DataSourceRepository(session).upsert(
            source_key=source_key, display_name="Sports-Reference team game log (local cache)",
            acquisition_type="static_file", is_rescrapable=False,
            description="Local TeamDump/CBB <slug>_<season>_game_log.csv files -- see docs/local_data_inventory.md.",
        )
        issue_repo = DataQualityIssueRepository(session)

        # Register every resolved team (perspective team + every distinct
        # resolved opponent) BEFORE the SourceArtifact row below, since
        # that row's own `team_id` column FKs to `teams.team_id` -- for a
        # team with no prior local evidence of its own (its first-ever
        # ingested game log), that FK would otherwise fail against a
        # completely fresh database. Most real opponents have no local
        # file of their own in this batch's scope and are very likely not
        # yet registered -- register them from their REAL, canonically-
        # resolved identity (never a guess). Preserve an already-
        # registered team's own conference_id (e.g. set by an earlier
        # AllStats import) rather than blanking it to None -- this
        # repository call itself has no conference evidence to offer.
        team_repo = TeamRepository(session)
        for spec in plan.resolved_team_specs:
            existing_team = session.get(Team, spec["team_id"])
            team_repo.upsert(conference_id=existing_team.conference_id if existing_team else None, **spec)

        artifact = SourceArtifactRepository(session).upsert(
            source_key=source_key, source_root_key="teamdump_cbb", relative_path=discovered.relative_path,
            original_filename=Path(discovered.relative_path).name, file_type="csv",
            file_size_bytes=discovered.file_size_bytes, content_hash=content_hash,
            filesystem_mtime=discovered.filesystem_mtime, collection_timestamp=None,
            season_id=plan.season_id, team_id=plan.team_id, validation_status="valid",
            point_in_time_classification="historical_but_not_point_in_time",
            pipeline_run_id=pipeline_run_id,
        )

        if plan.team_id is None or plan.season_id is None:
            for spec in plan.issue_specs:
                issue_repo.append(pipeline_run_id=pipeline_run_id, **spec)
            return GameLogImportResult(
                team_slug=plan.team_slug, season=plan.season, source_artifact_id=artifact.id,
                observations_written=0, stat_lines_written=0, results_upserted=0,
                issues_written=len(plan.issue_specs), status=plan.status,
            )

        observation_repo = TeamGameLogObservationRepository(session)
        stat_repo = TeamGameStatLineRepository(session)
        result_repo = GameResultRepository(session)

        written_observations: list[TeamGameLogObservation] = []
        game_repo = GameRepository(session)
        stat_lines_written = 0
        for spec in plan.observation_specs:
            # Never mutate the caller's plan -- copy before removing the
            # stat_values key, which isn't one of the repository's own
            # observation columns.
            spec_copy = dict(spec)
            stat_values = spec_copy.pop("stat_values")

            if spec_copy["game_id"] is not None and spec_copy["opponent_team_id"] is not None:
                # Always store the SAME (min, max) team-id pair as home/away,
                # regardless of which team's own file is being processed --
                # this is the only way both perspectives' imports agree on
                # one Game row for the same neutral-mode provisional game_id
                # (see module docstring's "VENUE" section: the structural
                # key itself is already order-independent this same way).
                sorted_home, sorted_away = sorted((plan.team_id, spec_copy["opponent_team_id"]))
                game_repo.upsert(
                    game_id=spec_copy["game_id"], home_team_id=sorted_home, away_team_id=sorted_away,
                    season_id=plan.season_id, game_date=spec_copy["game_date"], neutral_site=True, is_provisional=True,
                )

            row = observation_repo.upsert(
                team_id=plan.team_id, season_id=plan.season_id, source_artifact_id=artifact.id,
                pipeline_run_id=pipeline_run_id, **spec_copy,
            )
            written_observations.append(row)
            if any(v is not None for v in stat_values.values()):
                stat_repo.upsert(observation_id=row.id, **stat_values)
                stat_lines_written += 1

        results_upserted = 0
        for row in written_observations:
            if row.game_id is None or row.opponent_team_id is None:
                continue
            this_obs = ObservationForReconciliation(
                observation_id=row.id, team_id=row.team_id, points_scored=row.points_scored,
                points_allowed=row.points_allowed, venue_status=row.venue_status,
                overtime_marker=row.overtime_marker, raw_result_marker=row.raw_result_marker,
            )
            other_db_row = (
                session.query(TeamGameLogObservation)
                .filter(TeamGameLogObservation.game_id == row.game_id, TeamGameLogObservation.team_id == row.opponent_team_id)
                .one_or_none()
            )
            observations = [this_obs]
            if other_db_row is not None:
                observations.append(ObservationForReconciliation(
                    observation_id=other_db_row.id, team_id=other_db_row.team_id,
                    points_scored=other_db_row.points_scored, points_allowed=other_db_row.points_allowed,
                    venue_status=other_db_row.venue_status, overtime_marker=other_db_row.overtime_marker,
                    raw_result_marker=other_db_row.raw_result_marker,
                ))
            team_a_id, team_b_id = sorted((row.team_id, row.opponent_team_id))
            reconciliation = reconcile_game_observations(observations, team_a_id=team_a_id, team_b_id=team_b_id)

            result_repo.upsert(
                game_id=row.game_id, season_id=plan.season_id, team_a_id=team_a_id, team_b_id=team_b_id,
                result_status=reconciliation.result_status, validation_status=reconciliation.validation_status,
                source_count=reconciliation.source_count, team_a_score=reconciliation.team_a_score,
                team_b_score=reconciliation.team_b_score, winner_team_id=reconciliation.winner_team_id,
                loser_team_id=reconciliation.loser_team_id, overtime_status=reconciliation.overtime_status,
                team_a_observation_id=reconciliation.team_a_observation_id,
                team_b_observation_id=reconciliation.team_b_observation_id, pipeline_run_id=pipeline_run_id,
            )
            results_upserted += 1
            if reconciliation.result_status == "conflicting":
                issue_repo.append(
                    issue_code=ISSUE_CODE_GAMELOG_MIRRORED_CONFLICT, severity="error",
                    description=f"game_id {row.game_id}: mirrored observations conflict ({', '.join(reconciliation.conflict_reasons)}).",
                    game_id=row.game_id, team_id=row.team_id, pipeline_run_id=pipeline_run_id,
                    context=",".join(reconciliation.conflict_reasons),
                )

        for spec in plan.issue_specs:
            issue_repo.append(team_id=plan.team_id, pipeline_run_id=pipeline_run_id, **spec)

    return GameLogImportResult(
        team_slug=plan.team_slug, season=plan.season, source_artifact_id=artifact.id,
        observations_written=len(written_observations), stat_lines_written=stat_lines_written,
        results_upserted=results_upserted, issues_written=len(plan.issue_specs), status=plan.status,
    )
