# Identity-key scheme reference

Canonical reference for every stable-id generation function in this
codebase, why each namespace is on the scheme it's on, and the collision
defect found and fixed in the Phase 5 identity-key-remediation batch
(2026-09-08). Read this before adding a new entity type's identity
function or changing an existing one.

## Active ID-generation functions

| Function | Module | Used by |
|---|---|---|
| `stable_namespaced_id` (scheme v1) | `src/identity/stable_id.py` | `conference`, `sportsbook`, `model_version` namespaces |
| `stable_wide_id` (scheme v2) | `src/identity/stable_id.py` | `game`, `game_source` namespaces only |
| `stable_team_id` | `src/slug_utils.py` | team identity (`team_id`) -- a separate, older, pre-existing function, same 900,000-value width as v1 but with no namespace prefix |
| (none -- plain int) | `src/identity/season_identity.py` | `season_id` is the ending-year integer itself, never hashed |

Every function is a pure SHA-256 truncation. **None depends on Python's
randomized built-in `hash()` or a runtime UUID** -- all are reproducible
across processes and independent of dict/DataFrame iteration order.

## Scheme v1 (`stable_namespaced_id`) -- UNCHANGED, byte-faithful legacy

```python
int(hashlib.sha256(f"{namespace}:{key}".encode()).hexdigest(), 16) % 900_000 + 100_000
```

A 6-digit (900,000-value) truncated digest. Confirmed, by direct
measurement against every real entity universe this codebase knows about
today, to be collision-free in practice:

| Namespace | Real cardinality | Expected collisions (birthday bound, n²/2m) | Observed collisions |
|---|---|---|---|
| `conference` | 33 | ~0.0006 | 0 |
| `sportsbook` | 1 (FanDuel only) | ~0 | 0 |
| `model_version` | small, dynamic | negligible at this scale | 0 (not exhaustively enumerable -- no fixed real universe exists to scan) |
| `team` (`stable_team_id`, separate function, same width) | 365 | ~0.074 | 0 |

This function is kept **completely unchanged** -- both as the ongoing
scheme for these low-cardinality namespaces (per "do not widen every ID
type automatically if the defect is confined to one identity domain")
and as the exact, byte-faithful legacy-comparison function for anything
that already computed an id this way. No caller of `stable_namespaced_id`
was touched by this batch except `game_identity.py`, which moved off it
entirely (see below).

## The confirmed defect: `game`/`game_source` under scheme v1

The Phase 5B2 144-team read-only dry run found 4 real `game_id` values,
each shared by two **genuinely distinct, correctly-input** real games.
Reproduced and classified before any code was touched:

| Old `game_id` | Colliding real game 1 | Colliding real game 2 |
|---|---|---|
| 970231 | Colorado State (599252) @ Nevada (185503), 2025-12-30 | North Carolina (143834) vs. Syracuse (772030), 2026-02-21 |
| 970984 | Eastern Washington (115212) vs. opponent 247859, 2026-02-19 | Indiana (460807) vs. Northwestern (394237), 2026-03-11 |
| 258390 | Kentucky (157913) vs. St. John's NY (923441), 2025-12-20 | Liberty (996703) vs. opponent 514850, 2026-02-21 |
| 228434 | La Salle (468989) vs. Richmond (977583), 2026-01-14 | McNeese State (539023) vs. opponent 960106, 2026-02-07 |

### Root-cause classification (all 10 candidate causes checked)

For every one of the 8 real games above, the exact `_structural_key(...)`
string (`season:{season}:date:{game_date}:teams:{min}-{max}:neutral`) was
recomputed directly and confirmed **fully distinct** between every
colliding pair -- different team-id pairs, different real dates, same
season. This rules out every cause except pure hash-space exhaustion:

| # | Candidate cause | Verdict | Evidence |
|---|---|---|---|
| 1 | Truncated hash space | **CONFIRMED -- the root cause** | See quantitative justification below |
| 2 | Insufficient identity input tuple | Ruled out | The tuple (season, home_team_id, away_team_id, date, neutral_site) is fully discriminating; every colliding pair's own tuple differed in at least team-ids AND date |
| 3 | Missing game date / event discriminator | Ruled out | `date` was present and genuinely different in every colliding pair |
| 4 | Namespace reuse | Ruled out | `game`/`game_source` keys never collide with other namespaces' key strings by construction (namespace is part of the hashed string) |
| 5 | Input-order behavior | Ruled out | `_structural_key`'s neutral-site mode already sorts `(home, away)` into `(min, max)` before hashing -- proven order-independent by existing tests |
| 6 | Normalization collapse | Ruled out | Game identity never hashes a display name or slug -- canonical `team_id` integers only |
| 7 | Provisional/confirmed sharing a key | Ruled out for these 4 buckets | All 8 colliding real games came from `build_provisional_game_identity` (namespace `game`) only; `build_game_identity_from_source` (namespace `game_source`) uses an unrelated key format (`f"{source}:{source_game_id}"`) and was never exercised against real data with a trusted source id this batch. Separately noted: a provisional and confirmed identity for the same conceptual real game are *never* meant to numerically coincide (that's `find_game_id_conflicts`'s job at the application layer) -- not a defect, a design property, preserved by v2 |
| 8 | Caller supplying incorrect/incomplete arguments | Ruled out | Every argument (team ids, season, date) was independently verified correct against the real source CSV rows |
| 9 | Duplicate source evidence for the same entity | Ruled out | All 8 games are confirmed, by team identity and date, to be 8 distinct real games -- never the same game observed twice |
| 10 | Another cause | N/A | Not needed -- cause #1 fully explains all 4 buckets |

### Quantitative justification for cause #1

At real n≈4,543 provisional game identities in a single season (the
Phase 5B2 144-team dry run's own genuine-observation count), the
birthday-bound expected number of colliding pairs in a 900,000-slot space
is `n²/(2m) = 4,543² / 1,800,000 ≈ 11.5` -- consistent with the 4 observed
groups (several holding 3-4 rows, i.e. more than one colliding pair).
This is not a fluke; it is the expected behavior of a 900,000-value space
at this cardinality.

## Scheme v2 (`stable_wide_id`) -- the fix, `game`/`game_source` only

```python
int(hashlib.sha256(f"{namespace}:v2:{key}".encode()).hexdigest(), 16) % (2**63)
```

- **63 bits**, not the legacy 900,000-value width, and deliberately **not
  a full 128-bit/UUID representation**. See "Why 63 bits, not 128"
  below for the quantitative justification and the established-project-
  constraint reasoning the batch's own instructions anticipated.
- The `:v2:` scheme tag lives **inside** the hashed string (not appended
  to the truncated output), so a v2 id can never numerically coincide
  with what v1 would have produced for the same `(namespace, key)` pair,
  and a future v3 has an unambiguous, structurally-enforced place to
  diverge the same way.
- Applied to **exactly two namespaces** (`game`, `game_source`) inside
  `src/identity/game_identity.py`. No other identity module was changed.
  `conference`/`sportsbook`/`model_version`/`team`/`season` are
  untouched, per "do not widen every ID type automatically if the defect
  is confined to one identity domain."

### Why 63 bits, not 128

The instruction's default preference is "at least 128 effective bits
unless an established project constraint requires another
representation." That constraint is real and explicit here: every
identity in this schema (`team_id`, `season_id`, `conference_id`,
`sportsbook_id`, `model_version_id`, and `game_id` itself) is a plain SQL
integer PK/FK, every ORM column is `Mapped[int]`, and PostgreSQL's
largest native integer type is a 64-bit `BIGINT` -- there is no native
128-bit integer column type without moving to `NUMERIC` or `UUID`, which
would require changing the Python type of every FK column across 4
tables (`games`, `data_quality_issues`, `team_game_log_observations`,
`game_results`) and every downstream integer-typed reference in
application code, for zero practical safety benefit at this namespace's
realistic cardinality. 63 bits fits a signed 64-bit `BigInteger` column
exactly (always non-negative: `2**63 - 1` is the maximum signed 64-bit
value) and reduces collision probability to a practical zero even under
wildly generous growth assumptions:

| Assumed total real games ever (n) | Expected collisions at 63 bits (n²/2m, m=2⁶³) |
|---|---|
| 4,543 (one real season, confirmed) | ~6.0 × 10⁻¹³ |
| 1,000,000 (a multi-decade, multi-division upper bound) | ~2.7 × 10⁻⁸ |

Both are many orders of magnitude below any real risk tolerance --
narrowly versioned, minimal, and proportionate to the confirmed defect,
per "implement the smallest correction supported by the collision
analysis."

## Compatibility policy

- **No legacy persisted `game_id` values exist to translate.** Phase 5B2
  never performed a bulk or persistent import (explicitly prohibited);
  every `game_id` ever computed in this project lived only in a
  disposable, per-test SQLite database or a read-only in-memory dry-run
  script, both destroyed after the run. **No legacy-to-canonical mapping
  or redirect table was added** -- per "do not add a mapping table unless
  existing persisted IDs genuinely need translation," confirmed they
  don't.
- `stable_namespaced_id` (v1) is preserved byte-for-byte as the ongoing
  scheme for `conference`/`sportsbook`/`model_version`, and remains
  directly callable for regression comparison or interpreting any
  historical fixture/test value computed under it.
- Scheme selection is explicit and centralized: `game_identity.py`'s two
  builder functions are the **only** two call sites that choose `v2`;
  every other identity module continues calling `stable_namespaced_id`
  (v1) exactly as before. No caller chooses a scheme dynamically or by
  configuration.
- Identity-scheme version (`v1`/`v2`, an implementation detail of *how an
  id is computed*) is a completely separate concept from
  `model_version_identity`'s `model_version_id` (which identifies an
  *analytical model configuration*) -- the two are never conflated, and
  no analytical model version is encoded into any identity-scheme
  computation.

## Database implications

`games.game_id` (the PK the whole `game`/`game_source` namespace
produces) was `Integer` (32-bit signed, max ~2.1 billion) -- far too
narrow for a 63-bit v2 id. Widened to `BigInteger` (`.with_variant(Integer(), "sqlite")`,
the same convention already used for every other wide surrogate key in
this schema) by migration `259058c576b5` (`down_revision=4fed1844dcda`),
along with its three dependents (SQLAlchemy's FK-type inference
propagated the type change automatically once `games.game_id` itself was
retyped -- confirmed by direct inspection of `Base.metadata`, no manual
edit needed on the dependent columns):

| Table | Column | Role |
|---|---|---|
| `games` | `game_id` | PK |
| `data_quality_issues` | `game_id` | nullable FK |
| `team_game_log_observations` | `game_id` | nullable FK |
| `game_results` | `game_id` | PK + FK |

No table was added or removed by this migration -- table set at head is
identical to Phase 5B2's own. Hand-written (not left as Alembic
autogenerated it): autogenerating against a SQLite target produces an
**empty diff** for this exact change, since SQLite's dynamic type
affinity makes `INTEGER` and `BIGINT` reflect identically -- a real gap
in relying on autogenerate for a type-only ALTER, documented here so a
future batch doesn't re-trust it blindly for a similar change. Uses
`op.batch_alter_table` for every column, which compiles to Postgres as a
plain, lossless `ALTER TABLE ... ALTER COLUMN ... TYPE BIGINT` (a
non-truncating INTEGER->BIGINT widen is always safe there) and to SQLite
as its standard table-recreate dance.

**Downgrade is explicitly guarded, never blindly destructive.**
`downgrade()` queries all four columns for any value outside the 32-bit
signed `Integer` range *before* attempting to narrow anything, and raises
`RuntimeError` (changing nothing) if it finds one -- per this batch's own
instruction: "if safe downgrade is impossible after data conversion, stop
and report the migration decision rather than implementing a destructive
downgrade." Proven both ways against a disposable database: a real
63-bit `game_id` blocks the downgrade with a clear, actionable error and
leaves all data untouched; a database with no such value (e.g. only
legacy 6-digit-shaped ids, or empty) downgrades cleanly.

## Four-bucket before/after results

| Bucket (old `game_id`) | v1 result | v2 result |
|---|---|---|
| 970231 | 2 real games share 1 id | 2 distinct ids |
| 970984 | 2 real games share 1 id | 2 distinct ids |
| 258390 | 2 real games share 1 id | 2 distinct ids |
| 228434 | 2 real games share 1 id | 2 distinct ids |

All 8 real games now receive 8 distinct v2 ids, each fitting within 63
bits (`0 <= game_id < 2**63`).

## Repository-wide collision audit (post-fix)

| Universe | Entities scanned | Duplicate IDs found |
|---|---|---|
| Team (`stable_team_id`, unchanged, out of scope) | 365 real `sr_slug` values | 0 |
| Conference (`stable_namespaced_id`, unchanged, out of scope) | 33 real conference names | 0 |
| Game (`stable_wide_id`, v2, the fix) | Full 144-team real dry-run universe (~4,543 provisional game identities, ~3,320+ distinct real games) | 0 (down from 4 confirmed under v1) |

Sportsbook (1 real entry) and model-version (small, dynamic, no fixed
enumerable universe) were not exhaustively scanned -- their real
cardinality is far too low for a meaningful audit, consistent with the
zero-risk classification above.

## Point-in-time / PostgreSQL-specific vs. isolated-test coverage

Unchanged from every prior Phase 5 batch: this migration's DDL (the
`BigInteger` column type on all four columns) compiles cleanly against
the PostgreSQL dialect with no connection required; constraint/type
*enforcement*, the round-trip of a real 63-bit value, and both downgrade
paths (refused-with-overflow, clean-without-overflow) were proven only
against a disposable SQLite database with `PRAGMA foreign_keys=ON`. No
PostgreSQL server exists in this environment -- this remains the one
named, undischarged "PostgreSQL-specific vs. isolated-test" gap carried
over from every earlier Phase 5 batch, not a new one introduced here.

## Phase 5B3 readiness

This batch closes the one confirmed blocking finding from Phase 5B2's
144-team dry run. `game_id` collisions are fixed and verified against the
full real 144-team universe. Every other Phase 5B3 scope item (player-
level game logs, ESPN imports, mojibake remediation, bulk import, a real
PostgreSQL server, Torvik/Sports-Reference live adapters) is unaffected
and unchanged by this batch -- see `docs/migration_state.md`'s Phase 5B3
scope section, still current.

## Phase 5B5 confirmation (player-game-log identity remediation)

This scheme is entirely unaffected. Phase 5B5's remediation was scoped to
player-name-to-roster resolution only, never to the `game`/`game_source`
scheme this document describes. Confirmed directly: the full 1,561-file
dry run's `resolved_opponents_total` (34,945), `unresolved_opponents_
total` (2,039), and `games_constructed_total` (34,945) are byte-identical
before and after this batch's change -- the only counts that moved were
player-level (`resolved_players` 1,528 -> 1,529). See
`docs/migration_state.md`'s Phase 5B5 section for the full writeup.

## Phase 5B6 confirmation (ESPN abbreviation crosswalk remediation)

This scheme is entirely unaffected -- Phase 5B6 touched only
`src/identity/espn_team_abbreviation_aliases.py` and
`src/pipeline/espn_import_adapter.py`, neither of which participates in
`game`/`game_source` identity construction at all (ESPN's raw player-stats
file is season-to-date aggregate, not game-scoped -- confirmed already in
Phase 5B3). No `game_id` was computed, read, or affected by this batch.
The one behavioral change (`"UNCA"`'s 8 rows reclassified from
`team_resolution_method="unresolved"` to `"ambiguous"`) is a team-identity
provenance change only. See `docs/migration_state.md`'s Phase 5B6 section
for the full writeup.

## Phase 5B7 confirmation (disposable PostgreSQL validation)

The v2 63-bit wide-ID scheme this document describes is now confirmed,
for the first time, against a real running PostgreSQL 16 server (not
just SQLite mechanics or dialect-compiled DDL): a genuine provisional
`game_id` computed via the real, unmodified `build_provisional_game_
identity()` round-trips exactly through `games.game_id` (`BigInteger`)
and every one of its FK-dependent columns
(`data_quality_issues.game_id`, `team_game_log_observations.game_id`,
`game_results.game_id`, `player_game_log_observations.game_id`), with
zero truncation and zero collision-relevant behavior change. This batch
made no change to this scheme, `stable_wide_id()`, or any identity
resolver — see `docs/migration_state.md`'s Phase 5B7 section for the
full provisioning method and results.

## Protected team-slug remediation confirmation (`miami-(oh)` / `saint-joseph's`)

Two new literal-string entries were added to `src/slug_utils.py`'s
`_SR_OVERRIDES` dict (`"miami-(oh)": "miami-oh"`,
`"saint-joseph's": "saint-josephs"`) under explicit user approval to
touch this protected file, scoped exactly to these two directory-slug
resolution failures Phase 5B5 identified. This is **not** a change to
this document's own `stable_namespaced_id`/`stable_wide_id` schemes, nor
to `stable_team_id()` (the SHA-256-based team-id derivation) -- both new
entries resolve to already-existing, already-registered SR slugs
(`miami-oh`, `saint-josephs`), so `stable_team_id("miami-oh")` and
`stable_team_id("saint-josephs")` produce the exact same `team_id`s these
two programs already had; no team gained, lost, or changed its canonical
numeric identity. The two entries are matched at `resolve_team_identity`'s
Step 1 (exact alias) -- confirmed, by direct reproduction, to never reach
the lossy Step 3 canonical-slug-derivation path (where `"miami-(oh)"` and
`"miami-(fl)"` collide into the same bare `"miami"` key, a real,
already-known, still-correctly-detected collision this fix does not
touch or mask). See `docs/migration_state.md`'s dedicated remediation
section for the full root-cause, exact mappings, and before/after counts.

## Protected team-slug remediation confirmation (`bowling-green-state` / `william-mary`)

Phase 5C4A: two more narrow, literal-string entries were added to the
same `_SR_OVERRIDES` dict (`"bowling-green-state": "bowling-green"`,
`"william-mary": "william-&-mary"`), same mechanism, same scope
discipline as the `miami-(oh)`/`saint-joseph's` remediation above. Both
target slugs were already real, already-registered SR slugs before this
batch (`bowling-green` -> team_id 249863, MAC; `william-&-mary` -> team_id
870596, CAA) -- neither team gained, lost, or changed its canonical
numeric identity, and `stable_team_id()` is untouched. `william-mary`
(no ampersand) is a *different* literal string from the separate,
not-yet-wired-in `sr_slug_reconciliation.json`/
`resolve_team_identity_with_reconciliation` path's own confirmed current
SR slug for the same team (also `"william-mary"`, coincidentally
identical) -- that reconciliation path resolves it through a different
mechanism not used by the bulk-import adapters this override serves; the
two paths are independent and this batch did not touch or wire into the
reconciliation path. See `docs/migration_state.md`'s dedicated Phase
5C4A section for the full root-cause, exact mappings, and before/after
manifest counts.
