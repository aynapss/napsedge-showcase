"""
Phase 5B4: read-only import adapter for local, per-player game-log
evidence under the configured `src.paths.CBB_DIR` root
(`<slug>/<player>_<season>_gamelog.csv`).

Explicit stages (pure/impure boundary, mirroring every earlier Phase 5
adapter in this migration):

1. `discover_player_game_log_artifacts` -- pure filesystem walk for the
   real `*_<season>_gamelog.csv` pattern (deliberately distinct from the
   Phase 5B2 team-level `*_<season>_game_log.csv` pattern -- confirmed by
   direct glob against the real fixtures, never conflated). Never opens a
   file itself.
2. `load_player_game_log_dataframe` -- the one stage that reads one
   player's own file content and computes its content hash. Read-only.
3. `resolve_team_roster_for_player_gamelogs` -- reads the team's own
   `Roster_<season>.xlsx` **once per team** (not once per player file)
   and resolves it via the already-established `resolve_roster_player_identities`
   -- reused unmodified, never a new roster-reading path.
4. `parse_player_game_log_workbook` -- pure. Reconstructs a raw display
   name from the filename (the file itself carries no player-name
   column, confirmed by reading every real file), resolves each row's
   opponent via the canonical `resolve_team_identity`, resolves the
   player against the already-resolved roster via
   `resolve_allstats_player_row` (the corrected, mojibake-aware AllStats/
   Sports-Reference authority -- this source is the same source family,
   confirmed by this batch's own investigation), and computes a
   provisional game_id via the exact same
   `build_provisional_game_identity` the Phase 5B2 team-level adapter
   already uses. No database access.
5. `build_player_game_log_import_plan` -- pure, dry-run-safe counts +
   repository-ready specs.
6. `persist_player_game_log_import_plan` -- the only stage that touches
   the database, one `unit_of_work` per player file.

This module implements no new player- or team-name normalization or
matching rule anywhere -- every identity resolution reuses an
already-tested function from `src.identity.*`.

Phase 5B5 addendum -- team-scoped reviewed filename aliases: the full
1,561-file dry run's 33 unresolved player identities were independently
reproduced and classified into 3 exact root causes (see
`docs/migration_state.md`'s Phase 5B5 section for the full inventory):

1. Two teams (`miami-(oh)`, `saint-joseph's`) have real, present
   `Roster_<season>.xlsx` evidence, but their own directory slug does not
   resolve to a canonical `TeamIdentity` at all (a `src/slug_utils.py`
   registry-alias gap, confirmed by direct reproduction to be the true
   cause -- not "no roster file," as an earlier, less precise pass had
   assumed). Fixing this would require editing `src/slug_utils.py`, an
   explicitly protected file for this batch -- left unresolved, deferred.
2. One team (`st-thomas-(mn)`) has no `Roster_<season>.xlsx`/AllStats
   evidence at all co-located with its 2 player-gamelog files -- genuine
   roster absence, not a resolution defect. Left unresolved; no local
   evidence exists to close this without a new external source.
3. One real, narrow, irreversible case: `st-johns-ny`'s
   `rub_n_prey_2026_gamelog.csv` -- the roster's own real, resolvable
   entry is "Ruben Prey" (confirmed: `canonical_player_key_v15` matches
   on surname alone, and the roster carries exactly one "Prey"), but the
   filename's own sanitization dropped a character (a diacritic in the
   original "Rubén" collapsed to a bare underscore, not transliterated)
   in a way `reconstruct_player_name_from_slug`'s plain, deterministic
   `"_"->" "` reversal cannot recover. This is exactly the kind of
   "irreversible/lossy filename" case this batch's own instructions
   require explicit reviewed-alias evidence for, never fuzzy or
   surname-only matching -- so `_REVIEWED_PLAYER_GAMELOG_FILENAME_ALIASES`
   below adds ONE narrow, team-scoped, evidence-backed entry. It is
   consulted only for an exact `(team_slug, player_slug)` tuple (never a
   global or cross-team alias), and its target name is still resolved
   dynamically through the same `resolve_allstats_player_row` authority
   used everywhere else in this module -- so if the roster ever gained a
   second "Ruben Prey"-keyed entry, resolution would correctly report
   ambiguous rather than blindly trusting the alias.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pandas as pd

from src.db.enums import ISSUE_CODE_PLAYER_GAMELOG_OPPONENT_UNRESOLVED, ISSUE_CODE_PLAYER_GAMELOG_PLAYER_UNRESOLVED
from src.db.models import Team
from src.db.player_game_log_repositories import PlayerGameLogObservationRepository
from src.db.repositories import DataQualityIssueRepository, DataSourceRepository, GameRepository, PlayerRepository, SourceArtifactRepository, TeamRepository
from src.db.session import unit_of_work
from src.identity.contracts import PlayerIdentity, SeasonIdentity, TeamIdentity, UnresolvedIdentity
from src.identity.game_identity import build_provisional_game_identity
from src.identity.season_identity import resolve_season_identity
from src.identity.sr_allstats_player_identity import resolve_allstats_player_row, resolve_roster_player_identities
from src.identity.team_identity import resolve_team_identity

__all__ = [
    "DiscoveredPlayerGameLogFile",
    "discover_player_game_log_artifacts",
    "load_player_game_log_dataframe",
    "reconstruct_player_name_from_slug",
    "resolve_reviewed_player_gamelog_filename_alias",
    "resolve_team_roster_for_player_gamelogs",
    "ParsedIssue",
    "ParsedPlayerGameLogRow",
    "ParsedPlayerGameLogFile",
    "parse_player_game_log_workbook",
    "PlayerGameLogImportCounts",
    "PlayerGameLogImportPlan",
    "build_player_game_log_import_plan",
    "PlayerGameLogImportResult",
    "persist_player_game_log_import_plan",
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
PLAYER_GAMELOG_IMPORT_CONTRACT_VERSION = "1"

_REQUIRED_COLUMNS = ("date", "opponent", "result", "mp", "pts", "trb", "ast", "stl", "blk", "tov", "fg", "fga")
_RESULT_PATTERN = re.compile(r"^([WL])\s+(\d+)-(\d+)(?:\s+\((\d*OT)\))?$")

#: Team-scoped reviewed aliases for genuinely lossy/irreversible player-
#: game-log FILENAME encodings -- keyed by the exact `(team_slug,
#: player_slug)` tuple, never a bare name (so an alias for one team can
#: never accidentally fire for a same-named file under a different team's
#: own directory). Each entry's target is still resolved dynamically
#: through `resolve_allstats_player_row` against that team's own real
#: roster at lookup time -- never assumed correct -- so a future roster
#: change that introduces a genuine ambiguity is caught, not masked.
#: Reserved for filenames where `reconstruct_player_name_from_slug`'s
#: plain, deterministic `"_"->" "` reversal cannot recover the real name
#: (a lossy upstream sanitization, not a punctuation/suffix/mojibake
#: pattern already handled elsewhere) -- see this module's docstring and
#: docs/migration_state.md's Phase 5B5 section for the evidence behind
#: each entry.
_REVIEWED_PLAYER_GAMELOG_FILENAME_ALIASES: dict[tuple[str, str], str] = {
    ("st-johns-ny", "rub_n_prey"): "Ruben Prey",
}


def resolve_reviewed_player_gamelog_filename_alias(team_slug: str, player_slug: str) -> str | None:
    """Returns the reviewed target display name for an exact `(team_slug, player_slug)` tuple, or `None` if no alias is curated for it."""
    return _REVIEWED_PLAYER_GAMELOG_FILENAME_ALIASES.get((team_slug, player_slug))


# ---------------------------------------------------------------------------
# Stage 1-2: discovery + load
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveredPlayerGameLogFile:
    team_slug: str
    player_slug: str  # filename stem, minus "_<season>_gamelog"
    season: int
    relative_path: str
    file_size_bytes: int
    filesystem_mtime: dt.datetime


def discover_player_game_log_artifacts(
    cbb_root: Path, *, season: int, team_slugs: list[str] | None = None,
) -> list[DiscoveredPlayerGameLogFile]:
    """
    Pure filesystem walk under the caller-supplied, already-configured
    `cbb_root`. Never hardcodes that path itself; never opens a file.
    Matches only `*_<season>_gamelog.csv` -- deliberately distinct from
    the team-level `*_<season>_game_log.csv` pattern (confirmed real,
    non-overlapping glob results against the fixtures). A team with no
    player game-log files at all (confirmed real for many teams --
    "do not assume player game logs exist for every team") simply
    contributes nothing, never an error.
    """
    cbb_root = Path(cbb_root)
    if team_slugs is None:
        team_slugs = sorted(p.name for p in cbb_root.iterdir() if p.is_dir())

    suffix = f"_{season}_gamelog.csv"
    discovered = []
    for slug in team_slugs:
        team_dir = cbb_root / slug
        if not team_dir.is_dir():
            continue
        for file_path in sorted(team_dir.glob(f"*{suffix}")):
            player_slug = file_path.name[: -len(suffix)]
            stat = file_path.stat()
            discovered.append(DiscoveredPlayerGameLogFile(
                team_slug=slug, player_slug=player_slug, season=season,
                relative_path=f"{slug}/{file_path.name}",
                file_size_bytes=stat.st_size, filesystem_mtime=dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc),
            ))
    return discovered


def load_player_game_log_dataframe(discovered: DiscoveredPlayerGameLogFile, cbb_root: Path) -> tuple[pd.DataFrame, str]:
    """Reads the one CSV and computes its content hash. Read-only; never touches the database."""
    abs_path = Path(cbb_root) / discovered.relative_path
    df = pd.read_csv(abs_path)
    content_hash = hashlib.sha256(abs_path.read_bytes()).hexdigest()
    return df, content_hash


def reconstruct_player_name_from_slug(player_slug: str) -> str:
    """
    The file itself carries no player-name column (confirmed by reading
    every real file) -- the only raw evidence for a display name is the
    filename slug itself. Title-cased for readability; matching against
    the roster never depends on case (`canonical_player_key_v15`
    lowercases both sides), so an imperfect title-case (e.g. a suffix
    becoming "Ii" instead of "II", or a dropped apostrophe never present
    in the filename to begin with) never affects resolution correctness.
    """
    return player_slug.replace("_", " ").strip().title()


# ---------------------------------------------------------------------------
# Stage 3: team roster resolution (once per team, reused across every
# player file for that team)
# ---------------------------------------------------------------------------


def resolve_team_roster_for_player_gamelogs(
    cbb_root: Path, team_slug: str, season: int, *, source: str = "sports_reference",
) -> tuple[TeamIdentity | UnresolvedIdentity, SeasonIdentity | UnresolvedIdentity, list[PlayerIdentity]]:
    """
    Reads the team's own `Roster_<season>.xlsx` once and resolves it via
    the already-established `resolve_roster_player_identities` -- reused
    unmodified. A missing roster file (confirmed real: not every team
    with player game-log files necessarily has a co-located Roster
    workbook checked by this specific call) returns an empty roster, not
    an error -- every subsequent row simply becomes unresolved.
    """
    season_identity = resolve_season_identity(season, source=source)
    roster_path = Path(cbb_root) / team_slug / f"Roster_{season}.xlsx"
    if not roster_path.exists():
        return resolve_team_identity(team_slug, source=source), season_identity, []

    roster_df = pd.read_excel(roster_path)
    identities, _duplicates, team_identity = resolve_roster_player_identities(roster_df, team_slug, season, source=source)
    return team_identity, season_identity, identities


# ---------------------------------------------------------------------------
# Stage 4: parse (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedIssue:
    issue_code: str
    severity: str
    description: str
    source_row_position: int | None = None
    context: str | None = None


@dataclass(frozen=True)
class ParsedPlayerGameLogRow:
    source_row_position: int
    raw_opponent_name: str
    opponent_team_id: int | None
    game_id: int | None
    game_date: dt.date | None
    raw_result_marker: str | None
    team_score: int | None
    opponent_score: int | None
    overtime_marker: str | None
    participation_status: str
    player_id: str | None
    player_match_method: str | None
    validation_status: str
    exclusion_reason: str | None
    stat_values: dict[str, int | None] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedPlayerGameLogFile:
    team_slug: str
    player_slug: str
    season: int
    raw_player_name: str
    team_identity: TeamIdentity | UnresolvedIdentity
    season_identity: SeasonIdentity | UnresolvedIdentity
    player_id: str | None = None
    player_match_method: str | None = None
    rows: list[ParsedPlayerGameLogRow] = field(default_factory=list)
    issues: list[ParsedIssue] = field(default_factory=list)
    status: Literal["valid", "partial", "invalid"] = "valid"
    #: Every distinct resolved opponent's FULL identity -- registered
    #: before any Game row references it. Most real opponents have no
    #: local file of their own in this batch's scope and are very likely
    #: not yet registered (the exact same "register a real resolved
    #: identity before referencing it" gap confirmed and fixed for the
    #: Phase 5B2 team-level adapter).
    resolved_opponent_identities: dict[int, TeamIdentity] = field(default_factory=dict)


def _parse_result(raw_value: object) -> tuple[str | None, int | None, int | None, str | None]:
    if not isinstance(raw_value, str):
        return None, None, None, None
    match = _RESULT_PATTERN.match(raw_value.strip())
    if not match:
        return None, None, None, None
    marker, own_score, opp_score, overtime = match.groups()
    return marker, int(own_score), int(opp_score), overtime


def _int_or_none(value: object) -> int | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_player_game_log_workbook(
    df: pd.DataFrame,
    *,
    team_identity: TeamIdentity | UnresolvedIdentity,
    season_identity: SeasonIdentity | UnresolvedIdentity,
    roster_identities: list[PlayerIdentity],
    team_slug: str,
    player_slug: str,
    season: int,
    source: str = "sports_reference",
) -> ParsedPlayerGameLogFile:
    """
    Pure. Never mutates `df` (only ever iterated). Resolves the player
    ONCE for the whole file (this file's own filename-derived name is
    matched against the already-resolved roster exactly once, since every
    row in one player's own gamelog file is, by construction, that same
    player), then resolves each row's opponent and game individually.
    """
    issues: list[ParsedIssue] = []
    raw_player_name = reconstruct_player_name_from_slug(player_slug)

    if isinstance(season_identity, UnresolvedIdentity):
        issues.append(ParsedIssue(issue_code="season_not_resolved", severity="critical", description=f"season {season!r} did not resolve to a canonical SeasonIdentity."))
        return ParsedPlayerGameLogFile(team_slug=team_slug, player_slug=player_slug, season=season, raw_player_name=raw_player_name, team_identity=team_identity, season_identity=season_identity, issues=issues, status="invalid")

    if not isinstance(team_identity, TeamIdentity):
        issues.append(ParsedIssue(issue_code="team_not_resolved", severity="critical", description=f"team {team_slug!r} did not resolve to a canonical TeamIdentity."))
        return ParsedPlayerGameLogFile(team_slug=team_slug, player_slug=player_slug, season=season, raw_player_name=raw_player_name, team_identity=team_identity, season_identity=season_identity, issues=issues, status="invalid")

    if df.empty or not set(_REQUIRED_COLUMNS) <= set(df.columns) or "Player" in df.columns:
        # A player game log never has its own "Player" column (confirmed real shape) -- a
        # defensive guard against accidentally being handed a differently-shaped table.
        issues.append(ParsedIssue(issue_code="player_gamelog_missing_required_columns", severity="critical", description="Missing required game-log columns, an unexpected 'Player' column, or file is empty."))
        return ParsedPlayerGameLogFile(team_slug=team_slug, player_slug=player_slug, season=season, raw_player_name=raw_player_name, team_identity=team_identity, season_identity=season_identity, issues=issues, status="invalid")

    # A reviewed alias, if curated for this EXACT (team_slug, player_slug) tuple, supplies
    # the name actually matched against the roster -- `raw_player_name` itself (the plain,
    # deterministic filename reconstruction) is never overwritten, preserving it as
    # immutable raw evidence. The alias's own target is still resolved dynamically through
    # the same authority below, never trusted blindly (see this module's docstring).
    alias_target_name = resolve_reviewed_player_gamelog_filename_alias(team_slug, player_slug)
    name_to_match = alias_target_name if alias_target_name is not None else raw_player_name
    alias_note = f" via reviewed filename alias -> {alias_target_name!r}" if alias_target_name is not None else ""

    player_result = resolve_allstats_player_row(name_to_match, roster_identities)
    if isinstance(player_result, PlayerIdentity):
        player_id = player_result.player_id
        player_match_method = "reviewed_alias" if alias_target_name is not None else "roster_matched"
    elif isinstance(player_result, UnresolvedIdentity) and player_result.reason == "ambiguous":
        player_id = None
        player_match_method = "ambiguous"
        issues.append(ParsedIssue(issue_code=ISSUE_CODE_PLAYER_GAMELOG_PLAYER_UNRESOLVED, severity="warning", description=f"Player {raw_player_name!r} (from filename {player_slug!r}) matched ambiguously against the roster{alias_note}."))
    else:
        player_id = None
        player_match_method = "unresolved"
        issues.append(ParsedIssue(issue_code=ISSUE_CODE_PLAYER_GAMELOG_PLAYER_UNRESOLVED, severity="warning", description=f"Player {raw_player_name!r} (from filename {player_slug!r}) did not resolve against the roster{alias_note}."))

    rows: list[ParsedPlayerGameLogRow] = []
    resolved_opponent_identities: dict[int, TeamIdentity] = {}
    for position, (_, row) in enumerate(df.iterrows()):
        raw_opponent = row.get("opponent")
        raw_opponent_name = str(raw_opponent) if isinstance(raw_opponent, str) and raw_opponent.strip() else ""
        game_date = None
        raw_date = row.get("date")
        if isinstance(raw_date, str) and raw_date.strip():
            try:
                game_date = dt.date.fromisoformat(raw_date.strip())
            except ValueError:
                game_date = None

        marker, team_score, opponent_score, overtime = _parse_result(row.get("result"))
        stat_values = {
            "minutes_played": _int_or_none(row.get("mp")), "points": _int_or_none(row.get("pts")),
            "rebounds": _int_or_none(row.get("trb")), "assists": _int_or_none(row.get("ast")),
            "steals": _int_or_none(row.get("stl")), "blocks": _int_or_none(row.get("blk")),
            "turnovers": _int_or_none(row.get("tov")), "field_goals_made": _int_or_none(row.get("fg")),
            "field_goals_attempted": _int_or_none(row.get("fga")),
        }

        validation_status = "valid"
        exclusion_reason = None
        opponent_team_id = None
        game_id = None

        if not raw_opponent_name:
            validation_status = "invalid"
            exclusion_reason = "missing_opponent_name"
            issues.append(ParsedIssue(issue_code="player_gamelog_row_missing_opponent", severity="warning", description=f"Row {position} has no opponent name.", source_row_position=position))
        else:
            opponent_identity = resolve_team_identity(raw_opponent_name, source=source)
            if isinstance(opponent_identity, TeamIdentity):
                if opponent_identity.team_id == team_identity.team_id:
                    validation_status = "invalid"
                    exclusion_reason = "same_team_game"
                    issues.append(ParsedIssue(issue_code="player_gamelog_same_team", severity="error", description=f"Row {position}: opponent resolved to the SAME team as the perspective team.", source_row_position=position))
                else:
                    opponent_team_id = opponent_identity.team_id
                    resolved_opponent_identities.setdefault(opponent_identity.team_id, opponent_identity)
            else:
                validation_status = "unresolved"
                exclusion_reason = "opponent_not_resolved"
                issues.append(ParsedIssue(issue_code=ISSUE_CODE_PLAYER_GAMELOG_OPPONENT_UNRESOLVED, severity="warning", description=f"Row {position}: opponent {raw_opponent_name!r} did not resolve.", source_row_position=position))

        if game_date is None and validation_status == "valid":
            validation_status = "invalid"
            exclusion_reason = "unparseable_date"
            issues.append(ParsedIssue(issue_code="player_gamelog_invalid_date", severity="error", description=f"Row {position}: date {row.get('date')!r} could not be parsed as an ISO date.", source_row_position=position))

        if opponent_team_id is not None and game_date is not None and validation_status == "valid":
            identity = build_provisional_game_identity(
                home_team_id=team_identity.team_id, away_team_id=opponent_team_id,
                season=season_identity.season_id, game_date=game_date.isoformat(), neutral_site=True,
            )
            if isinstance(identity, UnresolvedIdentity):
                validation_status = "invalid"
                exclusion_reason = "game_identity_not_constructible"
            else:
                game_id = identity.game_id

        rows.append(ParsedPlayerGameLogRow(
            source_row_position=position, raw_opponent_name=raw_opponent_name or str(raw_opponent),
            opponent_team_id=opponent_team_id, game_id=game_id, game_date=game_date,
            raw_result_marker=marker, team_score=team_score, opponent_score=opponent_score, overtime_marker=overtime,
            participation_status="played",  # confirmed real: every row in this source represents a genuine appearance
            player_id=player_id, player_match_method=player_match_method,
            validation_status=validation_status, exclusion_reason=exclusion_reason, stat_values=stat_values,
        ))

    has_critical = any(i.severity == "critical" for i in issues)
    has_error_or_warning = any(i.severity in ("error", "warning") for i in issues)
    status: Literal["valid", "partial", "invalid"] = "invalid" if has_critical else ("partial" if has_error_or_warning else "valid")

    return ParsedPlayerGameLogFile(
        team_slug=team_slug, player_slug=player_slug, season=season, raw_player_name=raw_player_name,
        team_identity=team_identity, season_identity=season_identity,
        player_id=player_id, player_match_method=player_match_method,
        rows=rows, issues=issues, status=status,
        resolved_opponent_identities=resolved_opponent_identities,
    )


# ---------------------------------------------------------------------------
# Stage 5: build plan (pure, dry-run-safe)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlayerGameLogImportCounts:
    total_source_rows: int = 0
    resolved_opponents: int = 0
    unresolved_opponents: int = 0
    games_constructed: int = 0
    issues: int = 0


@dataclass(frozen=True)
class PlayerGameLogImportPlan:
    team_slug: str
    player_slug: str
    season: int
    team_id: int | None
    season_id: int | None
    player_id: str | None
    player_match_method: str | None
    raw_player_name: str
    #: The perspective team's own real, resolved spec -- registered
    #: before the SourceArtifact/Game rows that reference it (a team
    #: with no prior local file of its own may not yet be registered,
    #: same "register a real resolved identity before referencing it"
    #: pattern established in Phase 5B1/5B2).
    team_spec: dict | None = None
    #: Every distinct resolved opponent's team spec -- registered before
    #: any Game row references it.
    resolved_opponent_specs: list[dict] = field(default_factory=list)
    observation_specs: list[dict] = field(default_factory=list)
    issue_specs: list[dict] = field(default_factory=list)
    counts: PlayerGameLogImportCounts = field(default_factory=PlayerGameLogImportCounts)
    status: str = "valid"


def _team_spec(identity: TeamIdentity) -> dict:
    sr_source = identity.sources.get("sports_reference")
    return dict(team_id=identity.team_id, display_name=identity.display_name, sr_slug=sr_source.raw_value if sr_source else f"unknown-slug-{identity.team_id}")


def build_player_game_log_import_plan(parsed: ParsedPlayerGameLogFile) -> PlayerGameLogImportPlan:
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
            raw_result_marker=r.raw_result_marker, team_score=r.team_score, opponent_score=r.opponent_score,
            overtime_marker=r.overtime_marker, participation_status=r.participation_status,
            player_id=r.player_id, player_match_method=r.player_match_method,
            validation_status=r.validation_status, exclusion_reason=r.exclusion_reason, stat_values=r.stat_values,
        ))

    issue_specs = [dict(issue_code=i.issue_code, severity=i.severity, description=i.description, context=i.context) for i in parsed.issues]
    counts = PlayerGameLogImportCounts(
        total_source_rows=len(parsed.rows), resolved_opponents=resolved, unresolved_opponents=unresolved,
        games_constructed=games_constructed, issues=len(issue_specs),
    )

    team_spec = _team_spec(parsed.team_identity) if isinstance(parsed.team_identity, TeamIdentity) else None
    resolved_opponent_specs = [_team_spec(identity) for identity in parsed.resolved_opponent_identities.values()]

    return PlayerGameLogImportPlan(
        team_slug=parsed.team_slug, player_slug=parsed.player_slug, season=parsed.season,
        team_id=team_id, season_id=season_id, player_id=parsed.player_id, player_match_method=parsed.player_match_method,
        raw_player_name=parsed.raw_player_name, team_spec=team_spec, resolved_opponent_specs=resolved_opponent_specs,
        observation_specs=specs, issue_specs=issue_specs,
        counts=counts, status=parsed.status,
    )


# ---------------------------------------------------------------------------
# Stage 6: persist (the only stage that touches the database)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlayerGameLogImportResult:
    source_artifact_id: int | None
    observations_written: int
    issues_written: int
    status: str


def persist_player_game_log_import_plan(
    plan: PlayerGameLogImportPlan,
    session_factory,
    *,
    discovered: DiscoveredPlayerGameLogFile,
    content_hash: str,
    source_key: str = "sports_reference_player_game_log",
    pipeline_run_id: str | None = None,
) -> PlayerGameLogImportResult:
    """
    The one stage that opens a transaction -- exactly one `unit_of_work`
    per player file. If a row's own resolved player is not yet registered
    (a real risk: a roster player with no prior AllStats/game-log import
    of their own), registers them first -- the same "register a real
    resolved identity before referencing it" pattern already established
    in Phase 5B1/5B2. Never creates a player from an unresolved name.
    """
    with unit_of_work(session_factory) as session:
        DataSourceRepository(session).upsert(
            source_key=source_key, display_name="Sports-Reference per-player game log (local cache)",
            acquisition_type="static_file", is_rescrapable=False,
            description="Local TeamDump/CBB/<slug>/<player>_<season>_gamelog.csv files -- see docs/local_data_inventory.md.",
        )

        if plan.team_id is None or plan.season_id is None:
            artifact = SourceArtifactRepository(session).upsert(
                source_key=source_key, source_root_key="teamdump_cbb", relative_path=discovered.relative_path,
                original_filename=Path(discovered.relative_path).name, file_type="csv",
                file_size_bytes=discovered.file_size_bytes, content_hash=content_hash,
                filesystem_mtime=discovered.filesystem_mtime, collection_timestamp=None,
                season_id=plan.season_id, team_id=plan.team_id, validation_status="valid",
                point_in_time_classification="historical_but_not_point_in_time",
                pipeline_run_id=pipeline_run_id,
            )
            issue_repo = DataQualityIssueRepository(session)
            for spec in plan.issue_specs:
                issue_repo.append(pipeline_run_id=pipeline_run_id, **spec)
            return PlayerGameLogImportResult(source_artifact_id=artifact.id, observations_written=0, issues_written=len(plan.issue_specs), status=plan.status)

        # Register the perspective team AND every distinct resolved opponent BEFORE the
        # SourceArtifact/Game rows that reference them -- the same ordering fix confirmed
        # necessary during the Phase 5B2 game-log adapter's own identity-remediation audit
        # (registering with a placeholder display_name/sr_slug derived from the raw
        # directory slug, rather than the team's own real resolved identity, was the
        # exact bug found there -- avoided here by using `plan.team_spec` directly).
        team_repo = TeamRepository(session)
        for spec in [plan.team_spec, *plan.resolved_opponent_specs]:
            if spec is None:
                continue
            existing_team = session.get(Team, spec["team_id"])
            team_repo.upsert(conference_id=existing_team.conference_id if existing_team else None, **spec)

        if plan.player_id is not None:
            player_repo = PlayerRepository(session)
            player_repo.upsert(player_id=plan.player_id, display_name=plan.raw_player_name, team_id=plan.team_id, season_id=plan.season_id)

        artifact = SourceArtifactRepository(session).upsert(
            source_key=source_key, source_root_key="teamdump_cbb", relative_path=discovered.relative_path,
            original_filename=Path(discovered.relative_path).name, file_type="csv",
            file_size_bytes=discovered.file_size_bytes, content_hash=content_hash,
            filesystem_mtime=discovered.filesystem_mtime, collection_timestamp=None,
            season_id=plan.season_id, team_id=plan.team_id, validation_status="valid",
            point_in_time_classification="historical_but_not_point_in_time",
            pipeline_run_id=pipeline_run_id,
        )

        issue_repo = DataQualityIssueRepository(session)
        observation_repo = PlayerGameLogObservationRepository(session)
        game_repo = GameRepository(session)
        written = 0

        for spec in plan.observation_specs:
            spec_copy = dict(spec)
            stat_values = spec_copy.pop("stat_values")

            if spec_copy["game_id"] is not None and spec_copy["opponent_team_id"] is not None:
                # Always the SAME sorted (min, max) team-id pair, regardless of which
                # team's own file is processed first or which player's file gets there
                # first -- the identical convention Phase 5B2's own team-level adapter
                # uses, so both perspectives (and every player's own file) agree on one
                # Game row for the same neutral-mode provisional game_id.
                sorted_home, sorted_away = sorted((plan.team_id, spec_copy["opponent_team_id"]))
                game_repo.upsert(
                    game_id=spec_copy["game_id"], home_team_id=sorted_home, away_team_id=sorted_away,
                    season_id=plan.season_id, game_date=spec_copy["game_date"], neutral_site=True, is_provisional=True,
                )

            observation_repo.upsert(
                source_artifact_id=artifact.id, season_id=plan.season_id, team_id=plan.team_id,
                raw_player_name=plan.raw_player_name, pipeline_run_id=pipeline_run_id, **spec_copy, **stat_values,
            )
            written += 1

        for spec in plan.issue_specs:
            issue_repo.append(team_id=plan.team_id, player_id=plan.player_id, pipeline_run_id=pipeline_run_id, **spec)

    return PlayerGameLogImportResult(source_artifact_id=artifact.id, observations_written=written, issues_written=len(plan.issue_specs), status=plan.status)
