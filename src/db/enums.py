"""
Fixed vocabularies for the Phase 5A foundation tables.

Enforced via explicit `CHECK` constraints in `src.db.models` rather than a
native PostgreSQL `ENUM` type -- a native enum needs `ALTER TYPE ... ADD
VALUE` (which cannot run inside a transaction in older PostgreSQL and
complicates Alembic downgrade) every time this fixed list grows; a `CHECK`
constraint is a plain `ALTER TABLE` either way and behaves identically on
SQLite (used for this batch's isolated migration proof) and PostgreSQL.
"""
from __future__ import annotations

#: Required by the Phase 5A source-artifact provenance design -- see
#: docs/local_data_inventory.md section 9 for the full definition of each.
POINT_IN_TIME_CLASSIFICATIONS = (
    "point_in_time_eligible",
    "historical_but_not_point_in_time",
    "current_snapshot_only",
    "fixture_only",
    "provenance_incomplete",
    "invalid_or_unusable",
)

#: General-purpose validation status, distinguishing "not yet assessed"
#: (pending) from every terminal outcome the Phase 5A instructions require
#: ("missing, unavailable, invalid, provisional, partial, and conflicting
#: statuses"). "missing"/"unavailable" map to `unresolved` here (the
#: identity layer's own `UnresolvedIdentity.reason` vocabulary already
#: distinguishes *why* something is unresolved; this column only needs to
#: know *that* it is), "provisional" is tracked structurally on `games`
#: itself (`is_provisional`) rather than duplicated here.
VALIDATION_STATUSES = (
    "pending",
    "valid",
    "invalid",
    "unresolved",
    "conflicting",
    "partial",
)

PIPELINE_RUN_STATUSES = (
    "pending",
    "running",
    "succeeded",
    "failed",
    "partial",
)

#: Explicit state machine -- a pipeline run may only move forward along
#: these edges. `succeeded`/`failed` are terminal (no outgoing edges).
_PIPELINE_RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "failed"}),
    "running": frozenset({"succeeded", "failed", "partial"}),
    "partial": frozenset({"succeeded", "failed"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
}

ISSUE_SEVERITIES = ("info", "warning", "error", "critical")

#: How a registered `data_sources` row's evidence is actually obtained.
#: Added for the Torvik source-classification review: Torvik's real
#: acquisition mechanism is a live-scraped JSON response (confirmed by the
#: user: "the live webscraped json file that contained the adjoe and
#: adjde as well as other live metrics from the site") -- distinct from a
#: source whose evidence arrives as a static file drop or a documented
#: API contract. This column classifies the *mechanism*, never a
#: per-record value; it never appears on `source_artifacts` (which stays
#: source-agnostic) or on any canonical identity table.
ACQUISITION_TYPES = (
    "live_scraped_html",
    "live_scraped_json",
    "api",
    "static_file",
    "manual",
)

#: Confirmed this Phase 5A batch's local-data-inventory finding (see
#: docs/local_data_inventory.md section 4: all 128 basic-schema team game
#: logs end with a mislabeled season-total row). Recorded as a fixed,
#: documented issue code so a future importer can query for/suppress it
#: without re-deriving the finding.
ISSUE_CODE_GAMELOG_TRAILING_SEASON_TOTAL_ROW = "gamelog_trailing_season_total_row"

#: Confirmed this batch (docs/local_data_inventory.md section 5): only
#: ~23% of `TeamDump/ESPN` player-stats rows' `Team` abbreviation values
#: resolve through the existing canonical team-identity resolver.
ISSUE_CODE_ESPN_ABBREVIATION_UNRESOLVED = "espn_abbreviation_unresolved"


def is_valid_pipeline_run_transition(current: str, new: str) -> bool:
    return new in _PIPELINE_RUN_TRANSITIONS.get(current, frozenset())


def is_terminal_pipeline_run_status(status: str) -> bool:
    return status in ("succeeded", "failed")


#: The five real Sports-Reference AllStats per-player stat tables that
#: `player_stat_snapshots`/`team_stat_snapshots` can carry a row for.
#: "Roster" is deliberately excluded -- it is biographical/roster
#: evidence (`roster_memberships`), not a statistic table, confirmed by
#: reading the real workbooks this batch (Roster never carries a
#: "Team Totals" aggregate row the way PerGame/Totals/Advanced do).
ALLSTATS_STAT_TABLES = ("PerGame", "Totals", "Per40", "Per100", "Advanced")

#: Confirmed by direct inspection of real `TeamDump/CBB` workbooks this
#: batch: the literal `Player` value Sports-Reference uses for a team-
#: aggregate row, present in PerGame/Totals/Advanced but never in
#: Per40/Per100 or Roster. Kept as an explicit, documented, narrow set
#: (not a fuzzy "looks like a team row" heuristic) -- a value outside
#: this set is never guessed to be an aggregate row.
ALLSTATS_TEAM_AGGREGATE_ROW_LABELS = frozenset({"team totals"})


#: Phase 5B2: explicit venue knowledge state for one team-perspective game
#: observation. "unknown" is a real, distinct fourth state -- the
#: confirmed basic game-log schema (128 of 144 real teams) carries no
#: venue column at all, so venue for those rows is genuinely unknown, not
#: "neutral." Never inferred from team ordering or score (Phase 5B2
#: instruction) -- only ever set from an explicit source column
#: (`location`, advanced schema only) or left "unknown".
GAME_LOG_VENUE_STATUSES = ("home", "away", "neutral", "unknown")

#: Confirmed by direct inspection this batch: exactly two real game-log
#: column layouts exist across the 144 local teams with a 2026 log --
#: `basic` (8 columns, no venue/result/overtime/advanced-rate evidence,
#: 128 teams) and `advanced` (29 columns, explicit `location`/`result`/
#: `overtimes`/`game_type` plus ~17 rate/percentage columns, 16 teams).
GAME_LOG_SCHEMA_VARIANTS = ("basic", "advanced")

#: Phase 5B2 canonical-result reconciliation outcome -- distinct from the
#: cross-table `validation_status` vocabulary above (which still applies
#: to `game_results` for cross-cutting audit consistency); this one is
#: specific to *why* a result looks the way it does.
GAME_RESULT_STATUSES = (
    "reconciled",              # two mirrored observations agree
    "single_sided_provisional",  # exactly one team's observation exists
    "conflicting",              # two mirrored observations disagree -- score fields stay null
)

#: Confirmed this Phase 5B1/5B2 batch's local-data-inventory finding (see
#: docs/local_data_inventory.md section 4: all 128 basic-schema team game
#: logs end with a mislabeled season-total row, blank date/opponent,
#: season-aggregate points). Recorded as a fixed, documented issue code
#: (reused verbatim from the name already established in Phase 5A's own
#: inventory -- this constant's *value* was already referenced in prose
#: since Phase 5A; Phase 5B2 is the first batch to actually act on it).
ISSUE_CODE_GAMELOG_SEASON_TOTAL_ROW_EXCLUDED = "gamelog_trailing_season_total_row"

#: Phase 5B2: a mirrored pair of observations for the same provisional
#: game_id whose scores/venue/overtime evidence genuinely disagree.
ISSUE_CODE_GAMELOG_MIRRORED_CONFLICT = "gamelog_mirrored_observation_conflict"

#: Phase 5B2: a game-log row whose opponent name did not resolve through
#: the canonical, non-fuzzy team resolver.
ISSUE_CODE_GAMELOG_OPPONENT_UNRESOLVED = "gamelog_opponent_unresolved"

#: Phase 5B3: how one ESPN `Team` abbreviation code resolved to a
#: canonical team_id. `exact_alias` is the pre-existing baseline path
#: (`resolve_team_identity` succeeds directly, confirmed 84/365 codes,
#: ~23%). `reviewed_alias` is this batch's own evidence-backed crosswalk
#: (`src.identity.espn_team_abbreviation_aliases`) -- never a live change
#: to the shared resolver or `slug_utils.py`, only an adapter-local
#: fallback plus a persisted, auditable `team_aliases` record. `ambiguous`
#: is reserved for a real future case where roster-overlap evidence
#: points to more than one team with comparable strength (none found in
#: this batch's real data -- see docs/identity_scheme.md-adjacent
#: docs/migration_state.md Phase 5B3 section for the full method).
#: `invalid_non_team_value` is reserved for a raw code that is not a team
#: reference at all (none confirmed in the real fixture this batch).
ESPN_TEAM_RESOLUTION_METHODS = (
    "exact_alias",
    "reviewed_alias",
    "ambiguous",
    "unresolved",
    "invalid_non_team_value",
)

#: Phase 5B3: player-linkage evidence tier, reusing the exact vocabulary
#: `src.transformations.espn_allstats_transformation`'s in-memory linkage
#: already established (`matching_evidence` field) -- not a new matching
#: rule, the same tiers persisted this time.
ESPN_PLAYER_MATCH_METHODS = (
    "verified_espn_athlete_id",
    "existing_canonical_identity_sr_link",
    "team_season_scoped_unique_key",
    "ambiguous",
    "unresolved",
    "team_unresolved",
)

#: Phase 5B3: a player row whose ESPN team code resolved (directly or via
#: reviewed alias) but whose player name did not resolve to any canonical
#: player, or resolved ambiguously, within that team+season's roster
#: scope.
ISSUE_CODE_ESPN_PLAYER_UNRESOLVED = "espn_player_unresolved"

#: Phase 5B3: the exact, fixed set of `Metric` values confirmed present
#: in the real `TeamDump/ESPN/espn_ncaa_benchmarks.xlsx` (21 rows, read
#: directly, not assumed). A controlled vocabulary, not an arbitrary
#: free-text metric name -- `espn_benchmark_values.metric_name` is
#: guarded by a `CHECK` against this exact tuple so an uncontrolled EAV
#: table can never silently accept an unvalidated metric name. Widening
#: this tuple (a real new metric appearing in a future export) requires
#: its own reviewed migration, exactly like any other fixed vocabulary in
#: this schema.
ESPN_BENCHMARK_METRIC_NAMES = (
    "PTS", "PTS_40", "REB", "REB_40", "AST", "AST_40", "STL", "STL_40",
    "BLK", "BLK_40", "TO", "TO_40", "FG%", "3P%", "FT%", "eFG", "TS",
    "AST_TO", "Disruption", "GameScore", "Poss_per_min",
)

#: Phase 5B4: participation state for one player-game observation.
#: `played` is the confirmed real state for 100% of the 37,623 real rows
#: across all 1,561 local `<player>_<season>_gamelog.csv` files -- this
#: source's own contract only ever lists games a player actually appeared
#: in (a Sports-Reference per-player game-log page never emits a
#: DNP/Did-Not-Dress/Not-With-Team/Suspended row at all; the player's own
#: absence from a game is represented by the *absence* of a row, never a
#: zero-minute one -- confirmed by direct inspection, not assumed: zero
#: `mp=0` rows exist anywhere in the real corpus). The remaining values
#: reuse, verbatim, the already-established real vocabulary
#: `src.identity.postgame_player_identity`'s own `SR_NON_PLAYER_ROWS`
#: defines for the *live* box-score source (a structurally different,
#: not-yet-persisted source) -- reserved here for forward compatibility
#: with a future batch that persists that source, never fabricated or
#: inferred from this batch's own real data. `unknown` covers a row this
#: adapter cannot classify from the evidence it has.
PLAYER_GAME_PARTICIPATION_STATUSES = (
    "played",
    "dnp",
    "did_not_dress",
    "not_with_team",
    "suspended",
    "unknown",
)

#: Phase 5B4: a player-game-log row whose filename-derived player name did
#: not resolve to any canonical roster player, or resolved ambiguously,
#: within that team+season's roster scope.
ISSUE_CODE_PLAYER_GAMELOG_PLAYER_UNRESOLVED = "player_gamelog_player_unresolved"

#: Phase 5B4: a player-game-log row whose opponent name did not resolve
#: through the canonical, non-fuzzy team resolver -- same discipline as
#: the Phase 5B2 team-level equivalent.
ISSUE_CODE_PLAYER_GAMELOG_OPPONENT_UNRESOLVED = "player_gamelog_opponent_unresolved"


def sql_check_in(column: str, values: tuple[str, ...]) -> str:
    """Render `column IN ('a', 'b', ...)` for a CheckConstraint -- avoids relying on Python's tuple repr (which adds a fragile trailing comma for a single-element tuple)."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"
