"""
Read-only Matchup pure-feature comparison service (Phase 6, Slice 7A).

Mirrors `src.services.team`/`player`/`rankings`/`injury`'s structure:
independent of FastAPI, no database, no filesystem, no network, no
identity resolution of its own. Calls only the existing, unmodified
`src.features.matchup_features.build_matchup_feature_comparison` (itself
built this slice from the pre-existing, unmodified `compute_feature_delta`
and `build_key_edges` -- extracted from the original private `_delta`/
`_build_key_edges`, which `build_matchup_features` had always used
internally; see that module for the extraction record).

**Explicit-input boundary, deliberately retained.** This service accepts
the two teams' already-available flat feature values directly rather than
performing any acquisition of its own -- it does not call
`build_team_summary` (file I/O, plus Injury acquisition as of Slice 6A),
does not query a database, does not resolve a team name/slug, and does
not fetch odds or market data. A caller that only has a team name/id must
resolve it and gather its own feature values first, via the existing
Team/Player/Rankings services -- exactly the same "the service doesn't
recreate identity/acquisition" boundary already established for every
other Phase 6 domain.

**Approved scope is strictly descriptive.** Nothing in this module (or
the analytical function it wraps) declares a winner, projects a score,
computes a betting edge, or produces a confidence score -- those remain
`src.features.matchup_features.build_matchup_features`'s own separate
`projection`/`market_edge` concerns (which call live-adjacent
`market_features` helpers this slice does not touch). This service only
ever describes which team has the higher value for one specific,
already-existing statistical feature.

**No HTTP endpoint exists yet.** Exposing this service is a separate,
not-yet-approved slice (Slice 7B), per the same service-first pattern
already used for Team, Player, Rankings, and Injury.
"""
from __future__ import annotations

from src.features.matchup_features import build_matchup_feature_comparison
from src.services.contracts import FeatureComparisonEntry, MatchupFeatureComparisonResult


def compare_team_matchup_features(
    team_a_slug: str,
    team_b_slug: str,
    team_a_features: dict[str, float | None],
    team_b_features: dict[str, float | None],
) -> MatchupFeatureComparisonResult:
    """
    Calls `build_matchup_feature_comparison` exactly once and wraps its
    result into an immutable typed contract -- no filtering, no
    re-deriving deltas, no normalization, no rounding of its own (all
    rounding happens inside the unchanged `compute_feature_delta`), and
    no new feature key. `team_a_features`/`team_b_features` are used
    exactly as supplied -- a key absent from either dict is treated as
    unavailable for that side (`None`), matching
    `compute_feature_delta`'s own existing convention; never defaulted
    to zero, league-average, or "even."
    """
    raw = build_matchup_feature_comparison(team_a_slug, team_b_slug, team_a_features, team_b_features)
    key_edges = tuple(
        FeatureComparisonEntry(signal=entry["signal"], label=entry["label"], leader=entry["leader"], delta=entry["delta"])
        for entry in raw["key_edges"]
    )
    return MatchupFeatureComparisonResult(
        team_a_slug=team_a_slug,
        team_b_slug=team_b_slug,
        deltas=raw["deltas"],
        key_edges=key_edges,
    )
