# Database Schema Reference (Phase 5A + Phase 5B1)

Standalone lookup reference for all 16 tables built so far: the 13
Phase 5A foundation tables (`src/db/models.py`, migration
`alembic/versions/f496cbc68703_initial_schema.py`) plus the 3 Phase 5B1
AllStats source-evidence tables (`src/db/allstats_models.py`, migration
`alembic/versions/d990bbb62ce8_phase_5b1_allstats_source_evidence.py`,
`down_revision=f496cbc68703`). For the full design narrative, precheck,
and test results, see `docs/migration_state.md`'s "PHASE 5A: DATABASE
FOUNDATION" and "PHASE 5B1: LOCAL CBB ALLSTATS SOURCE-EVIDENCE
PERSISTENCE" sections — this file is the quick-reference companion, not
a replacement.

PostgreSQL is the authoritative target. Nothing in this schema has been
run against a live PostgreSQL server (none is available in this
environment) — see the migration_state.md section for the honest
SQLite-vs-PostgreSQL test-coverage boundary.

## Conventions used throughout

- **Canonical-identity tables** (`conferences`, `teams`, `seasons`,
  `players`, `games`, `sportsbooks`, `model_versions`) use an
  **externally-computed** primary key — never a database-generated
  surrogate. The value always comes from the existing `src.identity.*`
  resolution layer.
- **Non-identity tables** (`team_aliases`, `player_aliases`,
  `source_artifacts`, `data_quality_issues`) use a database-generated
  surrogate `id` (`BigInteger`, rendered as `BIGSERIAL`-equivalent on
  PostgreSQL, `INTEGER` on SQLite via an explicit dialect variant).
- Every timestamp column is `DateTime(timezone=True)`. The repository
  layer rejects a naive (non-tz-aware) datetime rather than assuming UTC.
- Every fixed vocabulary (point-in-time classification, validation
  status, pipeline-run status, issue severity) is enforced by an explicit
  `CHECK` constraint, not a native PostgreSQL `ENUM` type.
- "Immutable" below means: the repository's `upsert` raises
  `IdentityConflictError` if a repeated call for the same primary key
  supplies a different value for that field. "Mutable" means the
  repository updates it in place on a repeated call.

## Table reference

### `conferences`
| Column | Type | Notes |
|---|---|---|
| `conference_id` | Integer, PK | externally computed (`stable_namespaced_id`) |
| `canonical_name` | String(200), unique, not null | **immutable** |
| `created_at`, `updated_at` | DateTime(tz) | bookkeeping |

### `teams`
| Column | Type | Notes |
|---|---|---|
| `team_id` | Integer, PK | externally computed (`stable_team_id`) |
| `display_name` | String(200), not null | mutable |
| `sr_slug` | String(200), unique, not null | **immutable** |
| `conference_id` | Integer, FK → conferences, nullable | mutable |
| `created_at`, `updated_at` | DateTime(tz) | bookkeeping |

### `team_aliases`
| Column | Type | Notes |
|---|---|---|
| `id` | BigInteger, PK, surrogate | |
| `team_id` | Integer, FK → teams, not null | |
| `source` | String(50), not null | e.g. `"espn"`, `"covers"`, `"torvik"` |
| `raw_value` | String(200), not null | |
| `normalized_value` | String(200), not null | mutable |
| `created_at` | DateTime(tz) | |

Unique constraint: `(source, raw_value)` — **scoped by source**, global
across teams. Within one source, a given raw value always maps to the
same team; re-mapping it to a different team is rejected as a conflict.

### `players`
| Column | Type | Notes |
|---|---|---|
| `player_id` | String(200), PK | externally computed (`build_player_identity`) |
| `display_name` | String(200), not null | mutable |
| `team_id` | Integer, FK → teams, not null | **immutable** (baked into the ID) |
| `season_id` | Integer, FK → seasons, not null | **immutable** (baked into the ID) |
| `created_at`, `updated_at` | DateTime(tz) | |

### `player_aliases`
| Column | Type | Notes |
|---|---|---|
| `id` | BigInteger, PK, surrogate | |
| `player_id` | String(200), FK → players, not null | |
| `source` | String(50), not null | |
| `raw_value` | String(200), not null | |
| `normalized_value` | String(200), not null | mutable |
| `created_at` | DateTime(tz) | |

Unique constraint: `(player_id, source, raw_value)` — **scoped by
player**, not globally by source (unlike `team_aliases`): the same
display name legitimately belongs to two different real players.

### `seasons`
| Column | Type | Notes |
|---|---|---|
| `season_id` | Integer, PK | the season's own ending year, e.g. `2026` |
| `start_year`, `end_year` | Integer, not null | **immutable** together with `label` |
| `label` | String(20), not null | e.g. `"2025-26"` |
| `created_at` | DateTime(tz) | |

Check constraint: `end_year >= start_year`.

### `games`
| Column | Type | Notes |
|---|---|---|
| `game_id` | BigInteger, PK | externally computed (`src.identity.game_identity`, scheme v2 / `stable_wide_id` -- widened from `Integer` by migration `259058c576b5`; see `docs/identity_scheme.md`) |
| `home_team_id`, `away_team_id` | Integer, FK → teams, not null | **immutable**, must differ |
| `season_id` | Integer, FK → seasons, not null | **immutable** |
| `game_date` | Date, not null | **immutable** |
| `neutral_site` | Boolean, not null | **immutable** |
| `is_provisional` | Boolean, not null | **immutable** — see below |
| `source`, `source_game_id` | String, nullable | **immutable**; set only for a confirmed game |
| `created_at`, `updated_at` | DateTime(tz) | |

Check constraint: `home_team_id <> away_team_id`.

**Provisional vs. confirmed**: a provisional `game_id` is a structural
hash over (home_team_id, away_team_id, season_id, game_date,
neutral_site); a confirmed one is derived from a trusted
`source`/`source_game_id` pair instead — a *different* `game_id` value
for the same real event. A provisional row is never mutated into a
confirmed one in place; reconciling the two is
`src.identity.game_identity.find_game_id_conflicts`'s job at the
application layer (reported, never silently merged), not a DB constraint.

### `sportsbooks`
| Column | Type | Notes |
|---|---|---|
| `sportsbook_id` | Integer, PK | externally computed |
| `canonical_key` | String(100), unique, not null | **immutable**, e.g. `"fanduel"` |
| `display_name` | String(200), not null | mutable |
| `created_at`, `updated_at` | DateTime(tz) | |

### `model_versions`
| Column | Type | Notes |
|---|---|---|
| `model_version_id` | Integer, PK | externally computed (`compute_model_version_id`) |
| `label` | String(200), not null | **immutable** together with `config_fingerprint` |
| `config_fingerprint` | String(64), nullable | |
| `created_at` | DateTime(tz) | |

### `data_sources`
| Column | Type | Notes |
|---|---|---|
| `source_key` | String(100), PK | application-assigned, e.g. `"sports_reference_allstats"`, `"torvik"` |
| `display_name` | String(200), not null | mutable |
| `description` | Text, nullable | mutable |
| `acquisition_type` | String(30), not null, CHECK | `live_scraped_html`\|`live_scraped_json`\|`api`\|`static_file`\|`manual` — mutable |
| `is_rescrapable` | Boolean, not null | mutable |
| `created_at` | DateTime(tz) | |

Never auto-created by an importer guessing a new key — registered explicitly.

**Torvik example** (confirmed source classification, added in the Phase 5A
Torvik addendum): `source_key="torvik"`, `acquisition_type="live_scraped_json"`,
`is_rescrapable=True`. Torvik's own ratings (`AdjOE`/`AdjDE`/etc.) recalculate
as games are played, so being rescrapable does **not** make any one past
snapshot reproducible — every successful scrape must be preserved as its
own immutable `source_artifacts` row (see below), never overwritten.

### `pipeline_runs`
| Column | Type | Notes |
|---|---|---|
| `run_id` | String(32), PK | application-assigned UUID4 hex |
| `pipeline_name` | String(200), not null | |
| `status` | String(20), not null, CHECK | `pending`\|`running`\|`succeeded`\|`failed`\|`partial` |
| `started_at` | DateTime(tz), not null | rejects naive datetimes |
| `completed_at` | DateTime(tz), nullable | |
| `model_version_id` | Integer, FK → model_versions, nullable | |
| `notes` | Text, nullable | |
| `created_at` | DateTime(tz) | |

**State machine** (enforced by `PipelineRunRepository.transition`, not a
DB constraint): `pending → {running, failed}`; `running → {succeeded,
failed, partial}`; `partial → {succeeded, failed}`; `succeeded`/`failed`
are terminal (no outgoing transition).

### `source_artifacts`
| Column | Type | Notes |
|---|---|---|
| `id` | BigInteger, PK, surrogate | |
| `source_key` | String(100), FK → data_sources, not null | **immutable** for a given natural key |
| `source_root_key` | String(50), not null | names a *configured* root — a local directory for a file-based source (`"teamdump_cbb"`/`"teamdump_espn"`) or a configured cache root for a live-scraped/rescrapable source (e.g. `"torvik_cache"`) — never an absolute path |
| `relative_path` | String(500), not null | relative to the named root |
| `original_filename` | String(200), not null | mutable |
| `file_type` | String(20), not null | **immutable** for a given natural key |
| `file_size_bytes` | BigInteger, nullable | mutable |
| `content_hash` | String(64), nullable | part of the natural key |
| `filesystem_mtime` | DateTime(tz), nullable | **metadata only — never treated as `collection_timestamp`** |
| `collection_timestamp` | DateTime(tz), nullable | independently nullable |
| `effective_timestamp` | DateTime(tz), nullable | independently nullable |
| `import_timestamp` | DateTime(tz), not null, default now() | bookkeeping |
| `season_id`, `team_id`, `player_id` | FK, nullable | canonical links, only when established |
| `validation_status` | String(20), not null, CHECK | `pending`\|`valid`\|`invalid`\|`unresolved`\|`conflicting`\|`partial` |
| `point_in_time_classification` | String(50), not null, CHECK | the 6-value taxonomy below |
| `pipeline_run_id` | String(32), FK → pipeline_runs, nullable | mutable |
| `created_at` | DateTime(tz) | |

Unique constraint (natural key): `(source_root_key, relative_path,
content_hash)`.

**Point-in-time classification** (exactly 6 values, from
`docs/local_data_inventory.md` section 9):
`point_in_time_eligible`, `historical_but_not_point_in_time`,
`current_snapshot_only`, `fixture_only`, `provenance_incomplete`,
`invalid_or_unusable`.

### `data_quality_issues`
| Column | Type | Notes |
|---|---|---|
| `id` | BigInteger, PK, surrogate | |
| `issue_code` | String(100), not null | fixed vocabulary, see `src/db/enums.py` |
| `severity` | String(20), not null, CHECK | `info`\|`warning`\|`error`\|`critical` |
| `description` | Text, not null | |
| `source_artifact_id`, `team_id`, `player_id`, `game_id`, `pipeline_run_id` | FK, nullable | |
| `context` | Text, nullable | free-form structured detail |
| `detected_at`, `created_at` | DateTime(tz) | |

**Append-only** — `DataQualityIssueRepository.append()` always inserts a
new row; there is no update/dedupe path, by design (preserves audit
history).

Two fixed issue codes established this batch (see `src/db/enums.py`):
- `gamelog_trailing_season_total_row` — the confirmed defect where all
  128 basic-schema `TeamDump/CBB` team game logs end with a mislabeled
  season-total row.
- `espn_abbreviation_unresolved` — the confirmed ~77% ESPN `Team`
  abbreviation resolution gap.

## Phase 5B1 tables: local CBB AllStats source evidence

All three use a database-generated surrogate `id` (like `team_aliases`/
`source_artifacts`/`data_quality_issues`) — none has a canonical
cross-source identity of its own. Every raw stat/biographical value is
**immutable** once a row exists for its natural key (the natural key
already includes `source_artifact_id`, so a differing raw value at the
same key is a genuine content conflict, not a routine update); the two
exceptions, mutable together, are `player_id`/`validation_status` (an
initially-unresolved row can be refined later without that being a
conflict).

### `roster_memberships`
| Column | Type | Notes |
|---|---|---|
| `id` | BigInteger, PK, surrogate | |
| `player_id` | String(200), FK → players, nullable | null for an unresolved/ambiguous player |
| `team_id` | Integer, FK → teams, not null | |
| `season_id` | Integer, FK → seasons, not null | |
| `source_artifact_id` | BigInteger, FK → source_artifacts, not null | part of the natural key |
| `player_key` | String(200), not null | suffix-agnostic key, computed fresh from `Player` every time (never trusts a possibly-absent source `PlayerKey` column — confirmed real case: `albany-ny`) |
| `source_player_name` | String(200), not null | raw `Player` value |
| `jersey_number`, `class_year`, `position`, `height`, `weight`, `hometown`, `high_school`, `summary` | various, nullable | raw roster fields, never parsed further (e.g. `height` stays `"6-8"`, never converted to inches) |
| `rsci_top_100` | String(30), nullable | **raw text, not numeric** — confirmed real value `"34 (2023)"` |
| `validation_status` | String(20), not null, CHECK | mutable |
| `created_at` | DateTime(tz) | |

Natural key (unique constraint): `(player_key, team_id, season_id, source_artifact_id)`.
No unique constraint on `(player_id, season_id)` alone — a player is
never assumed to belong to only one team, even within a season.

### `player_stat_snapshots`
| Column | Type | Notes |
|---|---|---|
| `id` | BigInteger, PK, surrogate | |
| `player_id` | String(200), FK → players, nullable | |
| `team_id`, `season_id`, `source_artifact_id` | FK, not null | |
| `stat_table` | String(20), not null, CHECK | `PerGame`\|`Totals`\|`Per40`\|`Per100`\|`Advanced` — never `Roster` |
| `player_key`, `source_player_name` | not null | |
| `source_rank`, `source_position`, `awards_raw` | nullable | raw `Rk`/`Pos`/`Awards` from that specific stat table |
| ~27 box-score columns | Float/Integer, nullable | `games_played` … `pts`, `off_rating`/`def_rating` (Per100-only `ORtg`/`DRtg`) — populated for PerGame/Totals/Per40/Per100 rows |
| ~20 Advanced-only columns | Float, nullable | `per` … `bpm` — populated only for Advanced rows |
| `validation_status` | String(20), not null, CHECK | mutable |
| `created_at` | DateTime(tz) | |

Natural key: `(player_key, team_id, season_id, stat_table, source_artifact_id)`.
Sparse by design (box-score vs. Advanced column groups), never an EAV
`(stat_name, stat_value)` pair.

### `team_stat_snapshots`
Same box-score/Advanced column groups as `player_stat_snapshots`, minus
every player-identity field, plus `source_row_label` (String(50), not
null — the raw `Player` value, confirmed always `"Team Totals"` in real
data). Built only because Sports-Reference genuinely publishes this
aggregate row in `PerGame`/`Totals`/`Advanced` (confirmed absent from
`Per40`/`Per100`) — never synthesized by summing player rows.

Natural key: `(team_id, season_id, stat_table, source_artifact_id)`.

## Parser-level provenance fields (NOT persisted -- schema frozen)

The Phase 5B1 identity-matching addendum added four fields to the
*parser's own* output dataclasses (`ParsedRosterRow`/`ParsedPlayerStatRow`
in `src/pipeline/allstats_import_adapter.py`), deliberately without a
schema change: `source_row_position`, `source_player_key` (the
workbook's own `PlayerKey` column value, when present — raw evidence,
never a canonical id), `match_method`, and `identity_review_status`. They
exist for audit/dry-run reporting today; a future Phase 5B2 batch may add
matching columns to `roster_memberships`/`player_stat_snapshots` if that
provenance is worth querying directly, without needing to touch the
parsing logic again to get it.

The Phase 5 AllStats mojibake-remediation batch added one more field to
`ParsedPlayerStatRow` only, same discipline: `repaired_player_name` (the
`normalize_player_name_v15`-repaired form of `source_player_name` — the
exact string `resolve_allstats_player_row` now internally compares
against the roster's own already-repaired `display_name`; equal to
`source_player_name` whenever no repair was needed). This closed the
confirmed defect where a stat-table row's raw (un-repaired) name was
compared against a roster identity's already-repaired `display_name` —
see `docs/migration_state.md`'s dedicated section for the full root-cause
analysis, evidence, and before/after results. No table, column, or
migration changed — this was a parser/identity-resolution fix only.
`repaired_player_name`, like every other field in this "Parser-level
provenance fields" section, exists **only** on the in-memory
`ParsedPlayerStatRow` dataclass returned by `parse_allstats_workbook` — it
is never written to `player_stat_snapshots` or any other table by
`persist_allstats_import_plan` (which this batch did not modify), and no
bulk import of any corrected result into a persistent database occurred
in this batch.

## Phase 5B2 tables: normalized game-log evidence and canonical game results

Two deliberately separate persistence layers (never collapsed into one
table): `team_game_log_observations`/`team_game_stat_lines` hold each
team's own **immutable, raw, team-perspective** account of a game exactly
as its own game-log file states it; `game_results` holds the
**reconciled, canonical** truth for a game once both perspectives (or
just one, provisionally) are known. `game_results.game_id` reuses the
existing `games` table's own PK directly — no second game-identity table
or column exists anywhere in this batch.

### `team_game_log_observations`
| Column | Type | Notes |
|---|---|---|
| `id` | BigInteger, PK, surrogate | |
| `source_artifact_id` | BigInteger, FK → source_artifacts, not null | part of the natural key — a re-scrape is a new, additive row |
| `source_row_position` | Integer, not null | part of the natural key |
| `pipeline_run_id` | String, FK → pipeline_runs, nullable | |
| `season_id` | Integer, FK → seasons, not null | |
| `team_id` | Integer, FK → teams, not null | the *perspective* team — whose file this row came from |
| `raw_opponent_name` | String(200), not null | raw text, immutable |
| `opponent_team_id` | Integer, FK → teams, nullable | canonical, resolver-only — mutable (refinable once resolved) |
| `game_id` | Integer, FK → games, nullable | provisional or confirmed — mutable |
| `game_date` | Date, nullable | date-only; never a fabricated tipoff time |
| `scheduled_timestamp` | DateTime(tz), nullable | only ever set from an explicit source timestamp — no real source supplies one today, so always null in practice |
| `venue_status` | String(10), not null, CHECK | `home`\|`away`\|`neutral`\|`unknown` — `unknown` is a real 4th state (89% of basic-schema rows carry zero venue evidence), never defaulted to `neutral` |
| `raw_result_marker`, `overtime_marker`, `game_type_raw` | String, nullable | raw evidence only, immutable |
| `points_scored`, `points_allowed` | Integer, nullable | immutable |
| `schema_variant` | String(10), not null, CHECK | `basic`\|`advanced` |
| `validation_status` | String(20), not null, CHECK | mutable, standard cross-table vocabulary |
| `exclusion_reason` | String(100), nullable | mutable |
| `created_at` | DateTime(tz) | |

Natural key: `(team_id, season_id, source_artifact_id, source_row_position)`.
Every raw field above is immutable once set for that key; only
`opponent_team_id`/`game_id`/`validation_status`/`exclusion_reason` are
mutable together (an initially-unresolved row can be refined without that
being a conflict).

### `team_game_stat_lines`
1:1 with a `TeamGameLogObservation` (`observation_id` unique). ~3
columns (`off_rating`/`def_rating`/`net_rating`) populated for both real
schema variants; ~17 more (`pace`, `ts_pct`, `trb_pct`, `opp_efg_pct`, …)
populated only for the advanced schema. Sparse-wide by design, mirroring
`player_stat_snapshots`'s own precedent — never an EAV `(stat_name,
stat_value)` pair. All fields immutable once set.

### `game_results`
| Column | Type | Notes |
|---|---|---|
| `game_id` | Integer, PK, FK → games | **reuses the existing `games` PK directly — no second identity** |
| `season_id`, `team_a_id`, `team_b_id` | not null | immutable once set |
| `team_a_score`, `team_b_score`, `winner_team_id`, `loser_team_id` | nullable | null when `conflicting` |
| `overtime_status` | String(20), nullable | `overtime`\|`regulation`\|`unknown` |
| `result_status` | String(30), not null, CHECK | `reconciled`\|`single_sided_provisional`\|`conflicting` — a domain-specific status, deliberately separate from `validation_status` |
| `validation_status` | String(20), not null, CHECK | standard cross-table vocabulary |
| `source_count` | Integer, not null, CHECK 1–2 | how many team-perspective observations back this result |
| `team_a_observation_id`, `team_b_observation_id` | FK → team_game_log_observations, nullable | |
| `pipeline_run_id` | FK → pipeline_runs, nullable | |
| `first_imported_at`, `updated_at` | DateTime(tz) | |

Everything except the PK and `team_a_id`/`team_b_id` is mutable — a
result is *expected* to be refined as more observations arrive (e.g.
`single_sided_provisional` → `reconciled` once the second team's file is
imported). `CHECK (team_a_id <> team_b_id)` rejects a same-team result at
the database level, mirroring the parser's own same-team-row rejection.

**Never stores**: projected scores, win probabilities, spreads, totals,
expected value, Kelly sizing, or recommendations — this table is
historical ground truth only, not a prediction artifact.

**RESOLVED (Phase 5 identity-key-remediation batch, migration
`259058c576b5`)**: the Phase 5B2 144-team read-only dry run found that
`src/identity/stable_id.py`'s `stable_namespaced_id()` — shared,
pre-existing Phase 2 Gate 2 infrastructure used by conference/season/
sportsbook/model-version/game identity alike — truncated every hash to a
6-digit (900,000-value) space, far too small for the high-cardinality
`game` namespace: at real n≈4,543 provisional game identities, 4 of 3,320
distinct `game_id` values each collided across two genuinely unrelated
real games. A dedicated, bounded follow-up batch root-caused this to pure
hash-space exhaustion (never an insufficient input tuple — confirmed by
recomputing every colliding pair's structural key directly), added a new,
separately-versioned wide scheme (`stable_wide_id`, 63-bit, `game`/
`game_source` namespaces only — `conference`/`sportsbook`/`model_version`
stay on the original scheme, confirmed collision-free at their own real
cardinality), and widened `games.game_id` (+ its 3 dependent columns)
from `Integer` to `BigInteger` accordingly. Re-validated against the full
144-team universe: **0 collisions** (down from 4). Full root-cause
analysis, quantitative justification, and before/after results now live
in `docs/identity_scheme.md`.

## Phase 5B3 tables: ESPN raw player evidence and derived NCAA-cohort benchmark reference

Three real local files exist under `TeamDump/ESPN` (the configured
`src.paths.ESPN_DIR` root): `espn_full_player_stats.csv` (raw, 3,121
rows), `espn_full_player_stats_enriched.csv` (derived, same 3,121
players, 130 columns), and `espn_ncaa_benchmarks.xlsx` (derived, 21
cohort-level rows). All three are outputs of one already-migrated,
already-tested pipeline
(`src.transformations.player_benchmarks_transformation.
add_espn_benchmarks`) — confirmed by an earlier batch's own byte-exact
diff, not assumed. Raw and derived evidence are kept structurally
separate (never conflated in either direction): the raw file gets its
own observation table; the 130-column per-player enriched file is
**deliberately not persisted at all** (100%-reproducible from the raw
table via the already-tested transformation, no confirmed active DB
consumer justifies the duplication); only the small, well-bounded
cohort-level reference (the third file) gets its own snapshot/value
tables.

### `espn_source_observations`
| Column | Type | Notes |
|---|---|---|
| `id` | BigInteger, PK, surrogate | |
| `source_artifact_id` | BigInteger, FK → source_artifacts, not null | part of the natural key |
| `source_row_position` | Integer, not null | part of the natural key |
| `pipeline_run_id` | String, FK → pipeline_runs, nullable | |
| `season_id` | Integer, FK → seasons, not null | explicit, caller-supplied — the real file has no season column at all |
| `raw_player_name`, `raw_team_code` | String, not null | raw, immutable |
| `espn_athlete_id` | String, nullable | always null for the real CSV-sourced fixture (confirmed absent at the source) |
| `team_id` | Integer, FK → teams, nullable | resolved via `resolve_espn_team_abbreviation` (provenance via `resolve_espn_team_abbreviation_with_provenance`, Phase 5B6) — mutable (refinable) |
| `team_resolution_method` | String(30), nullable, CHECK | `exact_alias`\|`reviewed_alias`\|`ambiguous`\|`unresolved`\|`invalid_non_team_value` — `ambiguous` was reserved but unreachable for team resolution until Phase 5B6 made it reachable for one real case (`"UNCA"`, 3 named candidates, no disambiguating local evidence); no CHECK/schema change was needed since this value already existed |
| `player_id` | String, FK → players, nullable | mutable |
| `player_match_method` | String(40), nullable, CHECK | `verified_espn_athlete_id`\|`existing_canonical_identity_sr_link`\|`team_season_scoped_unique_key`\|`ambiguous`\|`unresolved`\|`team_unresolved` |
| ~19 raw stat columns | Integer/Float/String, nullable | `rk`, `pos`, `gp`, `min_per_game`, `pts`, `fgm`, `fga`, `fg_pct`, `three_pm`, `three_pa`, `three_pct`, `ftm`, `fta`, `ft_pct`, `reb`, `ast`, `stl`, `blk`, `turnovers` — exactly as ESPN reports them, no calculation |
| `validation_status` | String(20), not null, CHECK | standard cross-table vocabulary |
| `exclusion_reason` | String(100), nullable | |
| `created_at` | DateTime(tz) | |

Natural key: `(source_artifact_id, source_row_position)`. No `game_id`
column at all — this evidence is not game-scoped (confirmed by the real
file's own shape: season-to-date aggregate only).

### `espn_benchmark_snapshots` / `espn_benchmark_values`
One `espn_benchmark_snapshots` row per real `espn_ncaa_benchmarks.xlsx`
artifact (natural key: `source_artifact_id`; `season_id` nullable — the
real file is not season-scoped at all; `transformation_version` records
which version of `add_espn_benchmarks` produced it, deliberately kept
separate from `model_versions`/`model_version_id`, an *analytical model*
identity concept, never conflated with a *transformation* version tag).
One `espn_benchmark_values` row per `Metric`/`NCAA_Mean`/`NCAA_Std` line
within a snapshot (natural key: `(snapshot_id, metric_name)`) — exactly
as the real workbook reports it, no recomputation. `metric_name` is a
**controlled vocabulary**, `CHECK`-constrained against
`ESPN_BENCHMARK_METRIC_NAMES` (the exact 21 real values confirmed present
in the source file: `PTS`, `PTS_40`, `REB`, `REB_40`, `AST`, `AST_40`,
`STL`, `STL_40`, `BLK`, `BLK_40`, `TO`, `TO_40`, `FG%`, `3P%`, `FT%`,
`eFG`, `TS`, `AST_TO`, `Disruption`, `GameScore`, `Poss_per_min`) —
never an arbitrary/unvalidated free-text metric name, and never an
uncontrolled EAV table. This substitutes for a separate benchmark-
definition lookup table since the real vocabulary is confirmed small and
stable, not dynamic.

### Team-abbreviation resolution is adapter-local, not a schema concern

`espn_source_observations.team_id`/`team_resolution_method` are
populated by `src.pipeline.espn_import_adapter` via
`src.identity.espn_team_abbreviation_aliases.resolve_espn_team_abbreviation`
— baseline exact match (84/365 real codes, 23.0%), then a reviewed,
evidence-backed crosswalk this batch built via roster-overlap evidence
(231 more codes, bringing real resolution to 315/365, 86.3%). This
crosswalk **never modifies** the shared `resolve_team_identity()`
resolver or `src/slug_utils.py` — confirmed by direct source read that
resolution is purely in-memory (`TEAM_REGISTRY`/`_ALIAS_TO_ID`) and never
consults the DB `team_aliases` table at all. Every applied reviewed alias
is still persisted as an auditable `team_aliases` row
(`source="espn_stat_abbreviation"`) for provenance. Full evidence method
and the confirmed real false-positive this batch found and rejected
(`"ARST"` → a single coincidental shared player name with Milwaukee,
correctly left unresolved since Arkansas State has no local roster
evidence at all) are documented in `docs/migration_state.md`'s Phase 5B3
section and in `src/identity/espn_team_abbreviation_aliases.py`'s own
module docstring.

## Phase 5B4 table: player-level game-log evidence

One table, not three (`player_game_stat_lines`/`player_game_participation`,
both evaluated as candidates, were **not created** — see
`docs/migration_state.md`'s Phase 5B4 section for the evidence-based
reasoning: this source has exactly one confirmed real schema, no variant,
and every real row observed is `participation_status="played"`, so a
companion stat-line or participation table would carry no real
information a plain column doesn't already capture). Reuses the existing
`games.game_id` PK directly — no second game identity, and no schema
divergence from Phase 5B2's own team-level game-log tables. This table
has **zero relationship** to `src/ovr_model.py`'s own live
`_player_recency_factor`, which continues to read the same real
`TeamDump/CBB/<slug>/<player>_<season>_gamelog.csv` files directly from
disk and is completely unmodified by this batch.

### `player_game_log_observations`
| Column | Type | Notes |
|---|---|---|
| `id` | BigInteger, PK, surrogate | |
| `source_artifact_id` | BigInteger, FK → source_artifacts, not null | part of the natural key — a re-scrape is a new, additive row |
| `source_row_position` | Integer, not null | part of the natural key |
| `pipeline_run_id` | String, FK → pipeline_runs, nullable | |
| `season_id` | Integer, FK → seasons, not null | |
| `team_id` | Integer, FK → teams, not null | the player's own team, from the file's own directory |
| `raw_player_name` | String(200), not null | reconstructed from the filename slug, immutable |
| `player_id` | String, FK → players, nullable | resolved against the team's own roster via `resolve_allstats_player_row` (reused, mojibake-repair included) — mutable |
| `player_match_method` | String(40), nullable, no CHECK (free-form, unconstrained) | `roster_matched`\|`ambiguous`\|`unresolved`, plus `reviewed_alias` (Phase 5B5) — the outcome resolved via a curated, team-scoped, exact `(team_slug, filename_stem)` reviewed alias for a filename `reconstruct_player_name_from_slug` cannot deterministically recover (e.g. an irreversibly lossy diacritic-stripping artifact); the alias's own target is still resolved dynamically against the real roster at lookup time, never trusted blindly |
| `raw_opponent_name` | String(200), not null | raw text, immutable |
| `opponent_team_id` | Integer, FK → teams, nullable | canonical, resolver-only — mutable |
| `game_id` | BigInteger, FK → games, nullable | reuses the existing v2 63-bit scheme — mutable |
| `game_date` | Date, nullable | from the file's own `date` column; never derived from the file-local `game_num`, which is the player's own appearance ordinal, not the team's season game number |
| `scheduled_timestamp` | DateTime(tz), nullable | no real source supplies one — always null in practice |
| `raw_result_marker`, `overtime_marker` | String, nullable | raw evidence, immutable |
| `team_score`, `opponent_score` | Integer, nullable | parsed from the real `result` column; confirmed to always list this player's own team first |
| `participation_status` | String(20), not null, CHECK | `played`\|`dnp`\|`did_not_dress`\|`not_with_team`\|`suspended`\|`unknown` — vocabulary reused verbatim from `postgame_player_identity.SR_NON_PLAYER_ROWS` for forward compatibility with a structurally different, not-yet-persisted live box-score source; every real row from this source is `played` (the source never emits a DNP row — absence from a game is the absence of a row) |
| `minutes_played`, `points`, `rebounds`, `assists`, `steals`, `blocks`, `turnovers`, `field_goals_made`, `field_goals_attempted` | Integer, nullable | raw evidence, immutable |
| `validation_status` | String(20), not null, CHECK | standard cross-table vocabulary |
| `exclusion_reason` | String(100), nullable | mutable |
| `created_at` | DateTime(tz) | |

Natural key: `(source_artifact_id, source_row_position)`. Only
`player_id`/`opponent_team_id`/`game_id`/`validation_status`/
`exclusion_reason` are mutable; every raw field is immutable once set for
that key.

## Entity-relationship summary

```
conferences 1──* teams 1──* team_aliases
teams 1──* players 1──* player_aliases
teams, seasons ──* games
data_sources 1──* source_artifacts ──* data_quality_issues
teams, players, games, pipeline_runs ──* data_quality_issues
model_versions ──* pipeline_runs
seasons, teams, players, pipeline_runs ──* source_artifacts
teams, seasons, source_artifacts, players(nullable) ──* roster_memberships
teams, seasons, source_artifacts, players(nullable) ──* player_stat_snapshots
teams, seasons, source_artifacts ──* team_stat_snapshots
teams, seasons, source_artifacts, games(nullable), teams(opponent, nullable) ──* team_game_log_observations
team_game_log_observations 1──1 team_game_stat_lines
games 1──1 game_results ──* team_game_log_observations (team_a/team_b_observation_id)
seasons, source_artifacts, teams(nullable), players(nullable) ──* espn_source_observations
source_artifacts, seasons(nullable) ──* espn_benchmark_snapshots 1──* espn_benchmark_values
teams, seasons, source_artifacts, players(nullable), games(nullable), teams(opponent, nullable) ──* player_game_log_observations
```

## What is deliberately NOT in this schema yet

The 130-column per-player ESPN enriched view (`espn_full_player_stats_
enriched.csv` — deliberately not persisted, see Phase 5B3 section
above), Torvik normalized-metric tables, injury tables, ranking tables,
market-snapshot, prediction, simulation, recommendation, publication, or
result-beyond-final-score tables — all explicitly deferred. Player-level
game logs are now persisted (`player_game_log_observations`, Phase 5B4,
above); a `player_game_result`-style reconciled/canonical layer analogous
to `game_results` was **not** built this batch (no active consumer needs
a player-perspective reconciliation — the team-level `game_results` table
already reconciles the game itself). See `docs/migration_state.md`'s
Phase 5B4 section, "Exact next Phase 5 batch scope."

## Phase 5B7 confirmation: validated against real PostgreSQL

Every table, column type, nullability rule, primary/foreign key,
`UNIQUE`/`CHECK` constraint, and index documented above is now confirmed
— against a real, disposable PostgreSQL 16 instance, not just SQLite or
dialect-compiled DDL with no connection — to migrate cleanly (all six
revisions, zero drift via `alembic check`), enforce every constraint at
the database level, and leave zero leaked objects after a full downgrade.
`games.game_id`'s widened `BigInteger` scheme round-trips a genuine v2
63-bit id exactly; every `DateTime(timezone=True)` column round-trips a
UTC-aware timestamp with its timezone semantics intact; no native
PostgreSQL `ENUM` type or JSON/JSONB column exists anywhere in this
schema (confirmed by direct catalog inspection, not assumed from this
document's own "`CHECK` constraint instead of native enum" design
rationale). See `docs/migration_state.md`'s Phase 5B7 section for the
full provisioning method, safety controls, and before/after findings.
This remains schema-frozen: no table, column, or constraint changed as
part of this validation batch.
