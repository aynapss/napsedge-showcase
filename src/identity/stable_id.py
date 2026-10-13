"""
Shared deterministic ID helpers for the Phase 2 Gate 2 entity-identity
modules (conference, season, sportsbook, model version, game) plus the
Phase 5 identity-key remediation batch's wide-space scheme for `game`.

See `docs/identity_scheme.md` for the full collision-root-cause analysis
and scheme-selection rationale this module implements. Summary:

- `stable_namespaced_id` (scheme v1, UNCHANGED) -- a 6-digit
  (900,000-value) truncated SHA-256 digest. Confirmed, by direct
  measurement against every real entity universe this codebase knows
  about (365 teams, 33 conferences, 1 sportsbook, the model-version
  registry), to be collision-free in practice at THEIR cardinality --
  the birthday-bound expected-collision count at n<=365 in a 900,000-slot
  space is ~0.07, consistent with zero observed collisions. Kept
  completely unchanged (byte-faithful) as the ongoing scheme for those
  low-cardinality namespaces (`conference`, `sportsbook`, `model_version`)
  and as the exact legacy-comparison function for anything that already
  called it under the v1 scheme.
- `stable_wide_id` (scheme v2, NEW) -- a 63-bit truncated SHA-256 digest,
  with an explicit `:v2:` scheme tag baked into the hashed string (so a
  v2 id can never coincidentally equal what v1 would have produced for
  the same namespace/key, and so a future v3 has an unambiguous place to
  diverge from). Originally used exclusively by `game`/`game_source`
  identity (`src.identity.game_identity`) -- the one namespace that batch
  confirmed (via the Phase 5B2 144-team dry run) has enough real cardinality
  (~4,543 provisional game identities in a single season) to make v1's
  900,000-slot space produce actual collisions (4 confirmed, root-caused
  to hash-space exhaustion, never to an insufficient input tuple -- see
  `docs/identity_scheme.md`). 63 bits (not the 900,000-value legacy width,
  and not a full 128-bit/UUID representation) is the deliberate, minimal,
  quantitatively-justified choice: it fits the existing `BigInteger`
  convention this codebase already uses for every other wide surrogate
  key (`_BigIntegerPk`), avoiding a much larger, unrelated migration to a
  UUID/NUMERIC representation across every FK column that would be needed
  for a literal 128-bit id, while reducing collision probability at this
  namespace's realistic multi-decade cardinality to a practical zero (see
  the quantitative justification in `docs/identity_scheme.md`). Reused as
  a general-purpose deterministic-key primitive (not a second, competing
  implementation) by the controlled bulk-import design's Slice 4
  (`src.pipeline.bulk_import_safety`) for its own, distinct
  `"napsedge_bulk_import_advisory_lock"` namespace -- a PostgreSQL
  advisory-lock key, never an entity identity, and never confusable with
  a `game`/`game_source` id since the namespace string itself differs.

Neither function ever uses Python's randomized built-in `hash()`, a
runtime UUID, or any other non-reproducible source -- both are pure,
deterministic SHA-256 truncations, reproducible across processes and
independent of dict/DataFrame iteration order.
"""
from __future__ import annotations

import hashlib


def stable_namespaced_id(namespace: str, key: str) -> int:
    """
    Scheme v1 -- UNCHANGED, byte-faithful, kept as the ongoing scheme for
    low-cardinality namespaces (`conference`, `sportsbook`,
    `model_version`) and as the legacy-comparison function for anything
    that already computed an id this way. Deterministic 6-digit ID from
    ``f"{namespace}:{key}"``. Reproducible across runs/processes,
    independent of dict/DataFrame iteration order, independent of any
    timestamp or file path -- the same guarantees `stable_team_id`
    already provides for team identity, extended to every other entity
    type this migration defines a stable ID for.

    Do NOT use this for a namespace whose real cardinality could approach
    or exceed a few hundred entities -- see `stable_wide_id` and
    `docs/identity_scheme.md`'s collision-root-cause analysis for why
    `game` was moved off this scheme.
    """
    digest_input = f"{namespace}:{key}".encode()
    return int(hashlib.sha256(digest_input).hexdigest(), 16) % 900_000 + 100_000


#: Scheme v2's positive-range width. 63 bits keeps every v2 id inside a
#: signed 64-bit `BigInteger` column (max signed 64-bit value is
#: 2**63 - 1) -- always non-negative, always representable without a
#: NUMERIC/UUID column type change. See `docs/identity_scheme.md` for the
#: quantitative collision-risk justification at this width.
_WIDE_ID_BITS = 63


def stable_wide_id(namespace: str, key: str, *, bits: int = _WIDE_ID_BITS) -> int:
    """
    Scheme v2 -- a wide-space deterministic id for a namespace whose real
    cardinality is too large for `stable_namespaced_id`'s 900,000-value
    space (confirmed, this batch, for `game`/`game_source` only -- see
    `docs/identity_scheme.md`). The `:v2:` tag is part of the hashed
    string itself, not appended after truncation, so a v2 id can never
    numerically coincide with what v1 would compute for the same
    `(namespace, key)` pair, and a future v3 scheme has an explicit,
    unambiguous place to diverge in the same way.

    Deterministic, process- and ordering-independent, SHA-256-based --
    the same guarantees as `stable_namespaced_id`, at a wider truncation
    width chosen to fit a signed 64-bit `BigInteger` column exactly
    (never a 32-bit `Integer`, which this scheme's own namespace's
    canonical entity table was widened to accommodate -- see the Phase 5
    identity-key-remediation migration).
    """
    digest_input = f"{namespace}:v2:{key}".encode()
    return int(hashlib.sha256(digest_input).hexdigest(), 16) % (2**bits)
