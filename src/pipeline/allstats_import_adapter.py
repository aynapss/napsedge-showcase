"""
Phase 5B1: read-only import adapter for local CBB AllStats source evidence.

Five explicit stages, deliberately kept separate (Phase 5B1 instruction):

1. `discover_allstats_artifacts` -- pure filesystem walk + `stat()` calls
   under a configured source root. Never opens a workbook.
2. `load_allstats_dataframes` -- the one stage that actually reads Excel
   content (via `pandas.read_excel`) and computes each file's content
   hash. Read-only; never writes, never touches the database.
3. `parse_allstats_workbook` -- pure. Consumes already-loaded DataFrames
   (never re-reads them, never mutates its input -- every table is
   copied before any column is added), resolves team/season/player
   identity through the existing canonical layers only, validates every
   numeric value, and separates player rows from the confirmed real
   "Team Totals" aggregate rows. No database access.
4. `build_allstats_import_plan` -- pure. Turns a `ParsedAllStatsWorkbook`
   into repository-ready keyword-argument dicts plus a dry-run-safe
   summary (`AllStatsImportCounts`). No database access -- calling only
   this stage (and stopping) *is* the adapter's dry-run mode.
5. `persist_allstats_import_plan` -- the only stage that touches the
   database. Opens exactly one `src.db.session.unit_of_work` and applies
   every repository call inside it, so a single workbook's artifacts,
   roster rows, stat rows, and data-quality issues either all land
   together or all roll back together.

Every identity resolution in stage 3 reuses an already-tested function
from `src.identity.*` -- this module implements no new matching rule, no
new normalization function, and no player-name fallback of its own.

IDENTITY-MATCHING AUTHORITY (confirmed, corrected this batch)

Two established functions do 100% of the matching:
- `resolve_roster_player_identities(roster_df, team_query, season)` --
  the roster's own authority. Returns `(identities, duplicate_keys,
  team_result)` where `identities` is positionally aligned with
  `roster_df`'s own row order (`identities[i]` is roster row `i`'s
  `PlayerIdentity`) -- **guaranteed** one identity per roster row once
  `team_result` is a real `TeamIdentity` (confirmed by reading the
  function's own body: it iterates `roster_df["Player"]` once, appends
  exactly one `build_player_identity(...)` call's result per name, no
  filtering, no early return). This module's own positional zip is
  therefore safe **only** under that precondition, which is why
  `_pair_roster_rows_with_identities` below asserts the two sequences'
  lengths match before ever using position -- a defensive check on a
  guarantee that should never fail in practice, not a silent trust.
- `resolve_allstats_player_row(player_name, roster_identities)` (thin,
  purpose-built wrapper over `match_stat_row_to_roster`) -- the stat-row
  authority. Matches by the *already-established* suffix-agnostic key
  computed from each roster identity's own `display_name`, never by row
  position, never by surname alone. Returns the single match, or a
  structured `UnresolvedIdentity(reason="ambiguous"|"not_found")` --
  never a silent first-match guess.

This module's own `canonical_player_key_v15(...)` calls (still present)
are used **only** for two narrow, non-matching purposes: (1) populating
the `player_key` provenance column already defined in Phase 5B1's frozen
schema (the same fallback `assemble_legacy_allstats_dataframe` itself
uses when a workbook lacks its own `PlayerKey` column -- confirmed real
case: `TeamDump/CBB/albany-ny`), and (2) detecting an exact duplicate row
*within one stat table* (two rows for the literal same key in the same
table). Neither use ever feeds into cross-table/cross-source identity
matching -- that is exclusively the two functions above. A workbook's own
`PlayerKey` column value, when present, is captured separately as
`source_player_key` (raw evidence, per-row) and is never treated as, or
substituted for, a canonical player id.

PARSER PROVENANCE FIELDS (not yet persisted -- schema frozen this batch)

`ParsedRosterRow`/`ParsedPlayerStatRow` carry `source_row_position`,
`source_player_key`, `match_method`, and `identity_review_status` to
satisfy the full audit/provenance requirement at the parser's own output
level. None of these four is written to the database this batch (the
Phase 5B1 schema is explicitly frozen -- see docs/migration_state.md's
Phase 5B1 "schema and migration freeze" note); they exist so a caller
inspecting a `ParsedAllStatsWorkbook` directly (e.g. the dry-run report)
can already see this provenance, and so a future Phase 5B2 batch can add
the corresponding columns without touching this parsing logic again.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pandas as pd

from src.db.enums import ALLSTATS_STAT_TABLES, ALLSTATS_TEAM_AGGREGATE_ROW_LABELS
from src.db.session import unit_of_work
from src.db.repositories import DataQualityIssueRepository, DataSourceRepository, PlayerRepository, SourceArtifactRepository
from src.db.allstats_repositories import (
    PlayerStatSnapshotRepository,
    RosterMembershipRepository,
    TeamStatSnapshotRepository,
)
from src.exceptions import ValidationError
from src.identity.contracts import PlayerIdentity, SeasonIdentity, TeamIdentity, UnresolvedIdentity
from src.identity.player_identity import canonical_player_key_v15, normalize_player_name_v15
from src.identity.season_identity import resolve_season_identity
from src.identity.sr_allstats_player_identity import resolve_allstats_player_row, resolve_roster_player_identities

__all__ = [
    "AllStatsFileMetadata",
    "DiscoveredAllStatsWorkbook",
    "discover_allstats_artifacts",
    "load_allstats_dataframes",
    "ParsedIssue",
    "ParsedRosterRow",
    "ParsedPlayerStatRow",
    "ParsedTeamStatRow",
    "ParsedAllStatsWorkbook",
    "parse_allstats_workbook",
    "AllStatsImportCounts",
    "AllStatsImportPlan",
    "build_allstats_import_plan",
    "AllStatsImportResult",
    "persist_allstats_import_plan",
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
ALLSTATS_IMPORT_CONTRACT_VERSION = "1"

_REQUIRED_TABLES = ("Roster", "PerGame", "Totals", "Per40", "Per100", "Advanced")
_STAT_TABLE_FILE_LABEL = tuple(_REQUIRED_TABLES[1:])  # everything except Roster

_STAT_VALUE_COLUMN_MAP = {
    "G": "games_played", "GS": "games_started", "MP": "minutes_played",
    "FG": "fg_made", "FGA": "fg_attempted", "FG%": "fg_pct",
    "3P": "three_p_made", "3PA": "three_p_attempted", "3P%": "three_p_pct",
    "2P": "two_p_made", "2PA": "two_p_attempted", "2P%": "two_p_pct",
    "eFG%": "efg_pct", "FT": "ft_made", "FTA": "ft_attempted", "FT%": "ft_pct",
    "ORB": "orb", "DRB": "drb", "TRB": "trb", "AST": "ast", "STL": "stl",
    "BLK": "blk", "TOV": "tov", "PF": "pf", "PTS": "pts",
    "ORtg": "off_rating", "DRtg": "def_rating",
    "PER": "per", "TS%": "ts_pct", "3PAr": "three_par", "FTr": "ftr", "PProd": "pprod",
    "ORB%": "orb_pct", "DRB%": "drb_pct", "TRB%": "trb_pct", "AST%": "ast_pct",
    "STL%": "stl_pct", "BLK%": "blk_pct", "TOV%": "tov_pct", "USG%": "usg_pct",
    "OWS": "ows", "DWS": "dws", "WS": "ws", "WS/40": "ws_per_40",
    "OBPM": "obpm", "DBPM": "dbpm", "BPM": "bpm",
}

#: Columns G/GS are genuinely integer counts in the real source; every
#: other numeric stat column is a float (rate, percentage, or a table
#: -- Per40/Per100 -- where even "made"/"attempted" become fractional).
_INTEGER_STAT_COLUMNS = frozenset({"games_played", "games_started"})


# ---------------------------------------------------------------------------
# Stage 1: discovery (filesystem only, no file content read)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AllStatsFileMetadata:
    relative_path: str
    file_size_bytes: int
    filesystem_mtime: dt.datetime


@dataclass(frozen=True)
class DiscoveredAllStatsWorkbook:
    team_slug: str
    season: int
    files: dict[str, AllStatsFileMetadata]  # keyed by table label; only labels whose file actually exists

    @property
    def is_complete(self) -> bool:
        return all(lbl in self.files for lbl in _REQUIRED_TABLES)


def discover_allstats_artifacts(
    cbb_root: Path, *, season: int, team_slugs: list[str] | None = None,
) -> list[DiscoveredAllStatsWorkbook]:
    """
    Pure filesystem walk under the caller-supplied, already-configured
    `cbb_root` (the caller passes `src.paths.CBB_DIR` -- this function
    never hardcodes that path itself). Never opens a workbook; only
    `Path.stat()`. `team_slugs=None` discovers every team subdirectory.
    """
    cbb_root = Path(cbb_root)
    if team_slugs is None:
        team_slugs = sorted(p.name for p in cbb_root.iterdir() if p.is_dir())

    discovered = []
    for slug in team_slugs:
        team_dir = cbb_root / slug
        if not team_dir.is_dir():
            continue
        files: dict[str, AllStatsFileMetadata] = {}
        for label in _REQUIRED_TABLES:
            file_path = team_dir / f"{label}_{season}.xlsx"
            if not file_path.exists():
                continue
            stat = file_path.stat()
            files[label] = AllStatsFileMetadata(
                relative_path=f"{slug}/{label}_{season}.xlsx",
                file_size_bytes=stat.st_size,
                filesystem_mtime=dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc),
            )
        if files:
            discovered.append(DiscoveredAllStatsWorkbook(team_slug=slug, season=season, files=files))
    return discovered


# ---------------------------------------------------------------------------
# Stage 2: load (the only stage that reads file content)
# ---------------------------------------------------------------------------


def load_allstats_dataframes(
    discovered: DiscoveredAllStatsWorkbook, cbb_root: Path,
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """
    Reads each discovered file into a DataFrame and computes its content
    hash. Read-only (`pandas.read_excel` never writes back); never
    touches the database. Returns `(dfs, content_hashes)` -- the second
    dict keyed the same way as `discovered.files`.
    """
    cbb_root = Path(cbb_root)
    dfs: dict[str, pd.DataFrame] = {}
    content_hashes: dict[str, str] = {}
    for label, meta in discovered.files.items():
        abs_path = cbb_root / meta.relative_path
        dfs[label] = pd.read_excel(abs_path)
        content_hashes[label] = hashlib.sha256(abs_path.read_bytes()).hexdigest()
    return dfs, content_hashes


# ---------------------------------------------------------------------------
# Stage 3: parse (pure -- identity resolution, validation, row separation)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedIssue:
    issue_code: str
    severity: str
    description: str
    player_key: str | None = None
    stat_table: str | None = None
    context: str | None = None


@dataclass(frozen=True)
class ParsedRosterRow:
    player_key: str
    source_player_name: str
    player_identity: PlayerIdentity | UnresolvedIdentity
    validation_status: str
    jersey_number: int | None
    class_year: str | None
    position: str | None
    height: str | None
    weight: float | None
    hometown: str | None
    high_school: str | None
    rsci_top_100: str | None
    summary: str | None
    #: Provenance fields (not yet persisted -- see module docstring's
    #: "Parser provenance fields" note; kept for audit/dry-run reporting
    #: and to satisfy the explicit "PARSER OUTPUT" requirements without
    #: a schema change).
    source_row_position: int = -1
    source_player_key: str | None = None  # the workbook's OWN PlayerKey column value, if present -- raw evidence only, never used for matching
    match_method: str = "roster_positional"
    identity_review_status: str | None = None  # "source_verified" | "name_fallback" | None (unresolved)


@dataclass(frozen=True)
class ParsedPlayerStatRow:
    stat_table: str
    player_key: str
    source_player_name: str
    player_identity: PlayerIdentity | UnresolvedIdentity
    validation_status: str
    source_rank: float | None
    source_position: str | None
    awards_raw: str | None
    stat_values: dict[str, float | int | None] = field(default_factory=dict)
    source_row_position: int = -1
    source_player_key: str | None = None
    match_method: str = "stat_row_not_found"  # "stat_row_matched" | "stat_row_ambiguous" | "stat_row_not_found"
    identity_review_status: str | None = None
    #: Mojibake-remediation provenance (this batch): `normalize_player_name_v15`
    #: applied to `source_player_name` -- the exact string
    #: `resolve_allstats_player_row` now internally compares with (see that
    #: function's own docstring). Not yet persisted (parser-level audit
    #: field only, same "no schema change" discipline as every other
    #: provenance field on this dataclass); equal to `source_player_name`
    #: whenever the raw name needed no repair.
    repaired_player_name: str | None = None


@dataclass(frozen=True)
class ParsedTeamStatRow:
    stat_table: str
    source_row_label: str
    validation_status: str
    stat_values: dict[str, float | int | None] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedAllStatsWorkbook:
    team_slug: str
    season: int
    team_identity: TeamIdentity | UnresolvedIdentity
    season_identity: SeasonIdentity | UnresolvedIdentity
    roster_rows: list[ParsedRosterRow] = field(default_factory=list)
    player_stat_rows: list[ParsedPlayerStatRow] = field(default_factory=list)
    team_stat_rows: list[ParsedTeamStatRow] = field(default_factory=list)
    issues: list[ParsedIssue] = field(default_factory=list)
    status: Literal["valid", "partial", "invalid"] = "valid"


def _is_team_aggregate_row(raw_name: object) -> bool:
    return isinstance(raw_name, str) and raw_name.strip().lower() in ALLSTATS_TEAM_AGGREGATE_ROW_LABELS


def _coerce_numeric(raw_value: object, *, integer: bool) -> tuple[float | int | None, bool]:
    """Returns (value, is_non_finite). NaN -> (None, False) -- a genuinely missing source value, not an issue. Inf/-Inf -> (None, True) -- rejected, flagged by the caller."""
    if raw_value is None:
        return None, False
    try:
        f = float(raw_value)
    except (TypeError, ValueError):
        return None, False
    if math.isnan(f):
        return None, False
    if math.isinf(f):
        return None, True
    return (int(f) if integer else f), False


def _extract_stat_values(row: pd.Series, *, stat_table: str, issues: list[ParsedIssue], player_key: str | None) -> dict[str, float | int | None]:
    values: dict[str, float | int | None] = {}
    for source_col, target_field in _STAT_VALUE_COLUMN_MAP.items():
        if source_col not in row.index:
            continue
        value, non_finite = _coerce_numeric(row[source_col], integer=target_field in _INTEGER_STAT_COLUMNS)
        if non_finite:
            issues.append(ParsedIssue(
                issue_code="non_finite_stat_value", severity="error",
                description=f"{stat_table} column {source_col!r} had a non-finite value (inf/-inf) -- rejected, stored as null.",
                player_key=player_key, stat_table=stat_table, context=source_col,
            ))
        values[target_field] = value
    return values


def _raw_or_none(value: object) -> str | None:
    if value is None:
        return None
    try:
        if isinstance(value, float) and math.isnan(value):
            return None
    except TypeError:
        pass
    return str(value)


def _pair_roster_rows_with_identities(
    roster_df: pd.DataFrame, roster_identities: list[PlayerIdentity],
) -> list[tuple[int, pd.Series, PlayerIdentity]]:
    """
    The one place row position is ever used for identity purposes in this
    module -- and only under the precondition `resolve_roster_player_identities`
    itself guarantees (one identity per roster row, same order, whenever
    the team resolved). Explicitly asserts the cardinality instead of
    trusting it: `zip()` truncates silently on a length mismatch, which
    is exactly the "unconditional positional fallback" this batch's
    instructions forbid. A mismatch here would mean the roster DataFrame
    was mutated or re-ordered between the `resolve_roster_player_identities`
    call and this one -- a caller bug, surfaced immediately, never guessed
    past.
    """
    if len(roster_identities) != len(roster_df):
        raise ValidationError(
            f"roster/identity cardinality mismatch: {len(roster_df)} roster rows vs. "
            f"{len(roster_identities)} resolved identities -- positional alignment is unsafe, refusing to guess."
        )
    return [
        (position, row, identity)
        for position, ((_, row), identity) in enumerate(zip(roster_df.iterrows(), roster_identities))
    ]


def parse_allstats_workbook(
    dfs: dict[str, pd.DataFrame],
    team_query: str,
    season: int,
    *,
    source: str = "sports_reference",
    slug_map: dict[str, str] | None = None,
) -> ParsedAllStatsWorkbook:
    """
    Pure. Never mutates `dfs`: every table this function reads is only
    ever iterated (`.iterrows()`) or read via `.astype(str).tolist()`
    (inside `resolve_roster_player_identities`) -- no column is ever
    assigned back onto a caller-supplied frame.

    See the module docstring's "IDENTITY-MATCHING AUTHORITY" section for
    the full, corrected contract: roster rows are resolved via
    `resolve_roster_player_identities` (positionally aligned, safety-
    checked by `_pair_roster_rows_with_identities`); stat-table rows are
    resolved via `resolve_allstats_player_row` (name-based, never
    positional). Season resolves via `resolve_season_identity`.
    """
    issues: list[ParsedIssue] = []

    season_identity = resolve_season_identity(season, source=source)
    if isinstance(season_identity, UnresolvedIdentity):
        issues.append(ParsedIssue(
            issue_code="season_not_resolved", severity="critical",
            description=f"season {season!r} did not resolve to a canonical SeasonIdentity.",
        ))
        # No team/season identity -- nothing in this file can be safely imported.
        return ParsedAllStatsWorkbook(
            team_slug=team_query, season=season,
            team_identity=UnresolvedIdentity(query=team_query, source=source, reason="not_found"),
            season_identity=season_identity, issues=issues, status="invalid",
        )

    roster_df = dfs.get("Roster")
    if roster_df is None or roster_df.empty or "Player" not in roster_df.columns:
        issues.append(ParsedIssue(
            issue_code="roster_table_missing_or_empty", severity="critical",
            description="No usable Roster table (missing, empty, or no 'Player' column) -- file-level identity cannot be established.",
        ))
        return ParsedAllStatsWorkbook(
            team_slug=team_query, season=season,
            team_identity=UnresolvedIdentity(query=team_query, source=source, reason="not_found"),
            season_identity=season_identity, issues=issues, status="invalid",
        )

    roster_identities, duplicate_keys, team_identity = resolve_roster_player_identities(
        roster_df, team_query, season, slug_map=slug_map, source=source,
    )

    if isinstance(team_identity, UnresolvedIdentity):
        issues.append(ParsedIssue(
            issue_code="team_not_resolved", severity="critical",
            description=f"team query {team_query!r} did not resolve to a canonical TeamIdentity (reason={team_identity.reason!r}).",
        ))
        return ParsedAllStatsWorkbook(
            team_slug=team_query, season=season, team_identity=team_identity,
            season_identity=season_identity, issues=issues, status="invalid",
        )

    if duplicate_keys:
        for key, names in duplicate_keys.items():
            issues.append(ParsedIssue(
                issue_code="roster_duplicate_player_key", severity="warning",
                description=f"Roster has {len(names)} players sharing suffix-stripped key {key!r}: {names} -- cannot be safely disambiguated by name alone.",
                player_key=key,
            ))

    # -- Roster rows (positionally aligned with roster_identities via the
    #    cardinality-checked helper -- never a bare zip()) -----------------
    roster_rows: list[ParsedRosterRow] = []
    for position, row, identity in _pair_roster_rows_with_identities(roster_df, roster_identities):
        raw_name = row.get("Player")
        player_key = canonical_player_key_v15(str(raw_name))
        review_status = "source_verified" if (isinstance(identity, PlayerIdentity) and identity.sources) else "name_fallback"

        roster_rows.append(ParsedRosterRow(
            player_key=player_key, source_player_name=str(raw_name), player_identity=identity,
            validation_status="valid",
            jersey_number=(int(row["#"]) if "#" in row.index and pd.notna(row.get("#")) else None),
            class_year=_raw_or_none(row.get("Class")), position=_raw_or_none(row.get("Pos")),
            height=_raw_or_none(row.get("Height")),
            weight=(float(row["Weight"]) if "Weight" in row.index and pd.notna(row.get("Weight")) else None),
            hometown=_raw_or_none(row.get("Hometown")), high_school=_raw_or_none(row.get("High School")),
            rsci_top_100=_raw_or_none(row.get("RSCI Top 100")),
            summary=_raw_or_none(row.get("Summary")),
            source_row_position=position, source_player_key=_raw_or_none(row.get("PlayerKey")),
            match_method="roster_positional", identity_review_status=review_status,
        ))

    # -- Stat-table rows (player + team-aggregate) --------------------------
    player_stat_rows: list[ParsedPlayerStatRow] = []
    team_stat_rows: list[ParsedTeamStatRow] = []
    for stat_table in _STAT_TABLE_FILE_LABEL:
        table_df = dfs.get(stat_table)
        if table_df is None or table_df.empty or "Player" not in table_df.columns:
            continue
        seen_keys: dict[str, pd.Series] = {}
        for row_position, (_, row) in enumerate(table_df.iterrows()):
            raw_name = row.get("Player")

            if _is_team_aggregate_row(raw_name):
                team_values = _extract_stat_values(row, stat_table=stat_table, issues=issues, player_key=None)
                team_stat_rows.append(ParsedTeamStatRow(
                    stat_table=stat_table, source_row_label=str(raw_name).strip(),
                    validation_status="valid", stat_values=team_values,
                ))
                continue

            if not isinstance(raw_name, str) or not raw_name.strip():
                continue
            # Dedup key: used ONLY to detect a repeated row within this one
            # table (never for cross-table/roster matching -- that is
            # exclusively resolve_allstats_player_row below).
            dedup_key = canonical_player_key_v15(raw_name)

            if dedup_key in seen_keys:
                prior = seen_keys[dedup_key]
                same = all(
                    (pd.isna(prior.get(c)) and pd.isna(row.get(c))) or prior.get(c) == row.get(c)
                    for c in row.index
                )
                issues.append(ParsedIssue(
                    issue_code="duplicate_identical_stat_row" if same else "duplicate_conflicting_stat_row",
                    severity="warning" if same else "error",
                    description=(
                        f"{stat_table} table has more than one row for player_key {dedup_key!r} "
                        f"({'identical' if same else 'CONFLICTING'} values) -- only the first is kept."
                    ),
                    player_key=dedup_key, stat_table=stat_table,
                ))
                continue
            seen_keys[dedup_key] = row

            # Identity-matching authority: resolve_allstats_player_row (->
            # match_stat_row_to_roster) exclusively -- name-based, never
            # positional, never a re-derived key compared against anything
            # other than the roster's own already-resolved identities.
            identity = resolve_allstats_player_row(raw_name, roster_identities)
            if isinstance(identity, PlayerIdentity):
                match_method = "stat_row_matched"
                review_status = "source_verified" if identity.sources else "name_fallback"
            elif identity.reason == "ambiguous":
                match_method = "stat_row_ambiguous"
                review_status = None
            else:
                match_method = "stat_row_not_found"
                review_status = None

            if isinstance(identity, UnresolvedIdentity):
                issues.append(ParsedIssue(
                    issue_code=f"stat_row_{identity.reason}", severity="warning",
                    description=f"{stat_table} row for {raw_name!r} (player_key={dedup_key!r}) did not resolve against the roster (reason={identity.reason!r}).",
                    player_key=dedup_key, stat_table=stat_table,
                ))
            validation_status = "valid" if isinstance(identity, PlayerIdentity) else "unresolved"

            stat_values = _extract_stat_values(row, stat_table=stat_table, issues=issues, player_key=dedup_key)
            player_stat_rows.append(ParsedPlayerStatRow(
                stat_table=stat_table, player_key=dedup_key, source_player_name=str(raw_name),
                player_identity=identity, validation_status=validation_status,
                source_rank=(float(row["Rk"]) if "Rk" in row.index and pd.notna(row.get("Rk")) else None),
                source_position=_raw_or_none(row.get("Pos")), awards_raw=_raw_or_none(row.get("Awards")),
                stat_values=stat_values,
                source_row_position=row_position, source_player_key=_raw_or_none(row.get("PlayerKey")),
                match_method=match_method, identity_review_status=review_status,
                repaired_player_name=normalize_player_name_v15(str(raw_name)),
            ))

    has_critical = any(i.severity == "critical" for i in issues)
    has_error_or_warning = any(i.severity in ("error", "warning") for i in issues)
    status: Literal["valid", "partial", "invalid"] = "invalid" if has_critical else ("partial" if has_error_or_warning else "valid")

    return ParsedAllStatsWorkbook(
        team_slug=team_query, season=season, team_identity=team_identity, season_identity=season_identity,
        roster_rows=roster_rows, player_stat_rows=player_stat_rows, team_stat_rows=team_stat_rows,
        issues=issues, status=status,
    )


# ---------------------------------------------------------------------------
# Stage 4: build plan (pure -- repository-ready dicts + dry-run counts)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AllStatsImportCounts:
    roster_rows: int = 0
    player_stat_rows: int = 0
    team_stat_rows: int = 0
    resolved_players: int = 0
    unresolved_players: int = 0
    issues: int = 0


@dataclass(frozen=True)
class AllStatsImportPlan:
    team_slug: str
    season: int
    team_id: int | None
    season_id: int | None
    source_artifact_ids: dict[str, int]  # populated only after persistence -- empty for a pure dry-run plan
    resolved_player_specs: list[dict] = field(default_factory=list)  # every distinct resolved PlayerIdentity this workbook references -- registered before any roster/stat row that points at it
    roster_membership_specs: list[dict] = field(default_factory=list)
    player_stat_specs: list[dict] = field(default_factory=list)
    team_stat_specs: list[dict] = field(default_factory=list)
    issue_specs: list[dict] = field(default_factory=list)
    counts: AllStatsImportCounts = field(default_factory=AllStatsImportCounts)
    status: str = "valid"


def build_allstats_import_plan(parsed: ParsedAllStatsWorkbook) -> AllStatsImportPlan:
    """
    Pure -- no database access. This function's return value alone
    answers the adapter's dry-run requirement: counts and issues, no
    writes. `source_artifact_ids` stays empty here; `persist_allstats_import_plan`
    fills it in only once artifacts are actually registered.
    """
    team_id = parsed.team_identity.team_id if isinstance(parsed.team_identity, TeamIdentity) else None
    season_id = parsed.season_identity.season_id if isinstance(parsed.season_identity, SeasonIdentity) else None

    resolved_players: dict[str, dict] = {}

    def _note_resolved(identity) -> str | None:
        if not isinstance(identity, PlayerIdentity):
            return None
        if identity.player_id not in resolved_players and team_id is not None and season_id is not None:
            resolved_players[identity.player_id] = dict(
                player_id=identity.player_id, display_name=identity.display_name,
                team_id=team_id, season_id=season_id,
            )
        return identity.player_id

    roster_specs = []
    resolved = 0
    unresolved = 0
    for r in parsed.roster_rows:
        player_id = _note_resolved(r.player_identity)
        resolved += int(player_id is not None)
        unresolved += int(player_id is None)
        roster_specs.append(dict(
            player_key=r.player_key, source_player_name=r.source_player_name, player_id=player_id,
            validation_status=r.validation_status, jersey_number=r.jersey_number, class_year=r.class_year,
            position=r.position, height=r.height, weight=r.weight, hometown=r.hometown,
            high_school=r.high_school, rsci_top_100=r.rsci_top_100, summary=r.summary,
        ))

    stat_specs = []
    for s in parsed.player_stat_rows:
        player_id = _note_resolved(s.player_identity)
        resolved += int(player_id is not None)
        unresolved += int(player_id is None)
        stat_specs.append(dict(
            stat_table=s.stat_table, player_key=s.player_key, source_player_name=s.source_player_name,
            player_id=player_id, validation_status=s.validation_status, source_rank=s.source_rank,
            source_position=s.source_position, awards_raw=s.awards_raw, **s.stat_values,
        ))

    team_stat_specs = [
        dict(stat_table=t.stat_table, source_row_label=t.source_row_label, validation_status=t.validation_status, **t.stat_values)
        for t in parsed.team_stat_rows
    ]

    issue_specs = [
        dict(issue_code=i.issue_code, severity=i.severity, description=i.description, context=i.context)
        for i in parsed.issues
    ]

    counts = AllStatsImportCounts(
        roster_rows=len(roster_specs), player_stat_rows=len(stat_specs), team_stat_rows=len(team_stat_specs),
        resolved_players=resolved, unresolved_players=unresolved, issues=len(issue_specs),
    )

    return AllStatsImportPlan(
        team_slug=parsed.team_slug, season=parsed.season, team_id=team_id, season_id=season_id,
        source_artifact_ids={}, resolved_player_specs=list(resolved_players.values()),
        roster_membership_specs=roster_specs, player_stat_specs=stat_specs,
        team_stat_specs=team_stat_specs, issue_specs=issue_specs, counts=counts, status=parsed.status,
    )


# ---------------------------------------------------------------------------
# Stage 5: persist (the only stage that touches the database)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AllStatsImportResult:
    team_slug: str
    season: int
    source_artifact_ids: dict[str, int]
    roster_memberships_written: int
    player_stat_snapshots_written: int
    team_stat_snapshots_written: int
    issues_written: int
    status: str


def persist_allstats_import_plan(
    plan: AllStatsImportPlan,
    session_factory,
    *,
    discovered: DiscoveredAllStatsWorkbook,
    content_hashes: dict[str, str],
    source_key: str = "sports_reference_allstats",
    pipeline_run_id: str | None = None,
) -> AllStatsImportResult:
    """
    The one stage that opens a transaction -- exactly one
    `unit_of_work` for this entire workbook. Registers a `source_artifacts`
    row per real file (Roster/PerGame/Totals/Per40/Per100/Advanced, only
    the ones actually present), then every roster/stat/issue row, all in
    the same transaction. If anything raises (including an
    `IdentityConflictError` from a repository), the whole workbook's
    writes roll back together -- no orphaned artifact, roster, or stat row.

    Does not raise for a file-level `status == "invalid"` plan -- it
    still registers the source artifacts (so the failed attempt is
    auditable) but writes no roster/stat rows, only the issues.
    """
    if plan.team_id is None or plan.season_id is None:
        with unit_of_work(session_factory) as session:
            artifact_repo = SourceArtifactRepository(session)
            issue_repo = DataQualityIssueRepository(session)
            artifact_ids = _register_artifacts(
                artifact_repo, discovered, content_hashes, session=session, source_key=source_key,
                team_id=None, season_id=None, pipeline_run_id=pipeline_run_id,
            )
            for spec in plan.issue_specs:
                issue_repo.append(pipeline_run_id=pipeline_run_id, **spec)
        return AllStatsImportResult(
            team_slug=plan.team_slug, season=plan.season, source_artifact_ids=artifact_ids,
            roster_memberships_written=0, player_stat_snapshots_written=0, team_stat_snapshots_written=0,
            issues_written=len(plan.issue_specs), status=plan.status,
        )

    with unit_of_work(session_factory) as session:
        artifact_repo = SourceArtifactRepository(session)
        roster_repo = RosterMembershipRepository(session)
        stat_repo = PlayerStatSnapshotRepository(session)
        team_stat_repo = TeamStatSnapshotRepository(session)
        issue_repo = DataQualityIssueRepository(session)

        artifact_ids = _register_artifacts(
            artifact_repo, discovered, content_hashes, session=session, source_key=source_key,
            team_id=plan.team_id, season_id=plan.season_id, pipeline_run_id=pipeline_run_id,
        )

        player_repo = PlayerRepository(session)
        for spec in plan.resolved_player_specs:
            player_repo.upsert(**spec)

        roster_artifact_id = artifact_ids.get("Roster")
        for spec in plan.roster_membership_specs:
            roster_repo.upsert(team_id=plan.team_id, season_id=plan.season_id, source_artifact_id=roster_artifact_id, **spec)

        for spec in plan.player_stat_specs:
            stat_artifact_id = artifact_ids[spec["stat_table"]]
            stat_repo.upsert(team_id=plan.team_id, season_id=plan.season_id, source_artifact_id=stat_artifact_id, **spec)

        for spec in plan.team_stat_specs:
            stat_artifact_id = artifact_ids[spec["stat_table"]]
            team_stat_repo.upsert(team_id=plan.team_id, season_id=plan.season_id, source_artifact_id=stat_artifact_id, **spec)

        for spec in plan.issue_specs:
            issue_repo.append(team_id=plan.team_id, pipeline_run_id=pipeline_run_id, **spec)

    return AllStatsImportResult(
        team_slug=plan.team_slug, season=plan.season, source_artifact_ids=artifact_ids,
        roster_memberships_written=len(plan.roster_membership_specs),
        player_stat_snapshots_written=len(plan.player_stat_specs),
        team_stat_snapshots_written=len(plan.team_stat_specs),
        issues_written=len(plan.issue_specs), status=plan.status,
    )


def _register_artifacts(
    artifact_repo: SourceArtifactRepository,
    discovered: DiscoveredAllStatsWorkbook,
    content_hashes: dict[str, str],
    *,
    session,
    source_key: str,
    team_id: int | None,
    season_id: int | None,
    pipeline_run_id: str | None,
) -> dict[str, int]:
    DataSourceRepository(session).upsert(
        source_key=source_key, display_name="Sports-Reference AllStats (local cache)",
        acquisition_type="static_file", is_rescrapable=False,
        description="Local TeamDump/CBB Roster/PerGame/Totals/Per40/Per100/Advanced workbooks -- see docs/local_data_inventory.md.",
    )
    artifact_ids: dict[str, int] = {}
    for label, meta in discovered.files.items():
        row = artifact_repo.upsert(
            source_key=source_key, source_root_key="teamdump_cbb", relative_path=meta.relative_path,
            original_filename=Path(meta.relative_path).name, file_type="xlsx",
            file_size_bytes=meta.file_size_bytes, content_hash=content_hashes.get(label),
            filesystem_mtime=meta.filesystem_mtime, collection_timestamp=None,
            season_id=season_id, team_id=team_id, validation_status="valid",
            point_in_time_classification="historical_but_not_point_in_time",
            pipeline_run_id=pipeline_run_id,
        )
        artifact_ids[label] = row.id
    return artifact_ids
