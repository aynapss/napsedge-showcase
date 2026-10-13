"""
Public showcase excerpt. The descriptive comparison of user-supplied feature values (the API's matchup endpoint and
the Streamlit page) is included verbatim. The matchup feature engineering and projection model
(`build_matchup_features`) are proprietary and not included: that function raises `NotImplementedError`.
"""
from __future__ import annotations

from typing import Any

MATCHUP_FEATURE_LABELS: dict[str, str] = {
    "avg_margin": "season scoring margin",
    "recent_avg_margin": "recent scoring margin",
    "avg_net_rtg": "season net rating",
    "recent_avg_net_rtg": "recent net rating",
    "player_value_index": "player value index",
    "off_impact": "offensive impact",
    "def_impact": "defensive impact",
}


def _omitted(*_args: Any, **_kwargs: Any) -> Any:
    raise NotImplementedError("proprietary component not included in the public showcase")


def compute_feature_delta(a: float | None, b: float | None) -> float | None:
    """
    Phase 6, Slice 7A: public extraction of the original private `_delta`
    -- byte-for-byte identical logic (a simple rounded subtraction), made
    public and renamed for service-layer use; its 7 internal call sites
    inside `build_matchup_features` below were updated to call this name
    directly (no separate `_delta` wrapper retained, since `_delta` had no
    external caller to preserve compatibility for -- confirmed by search).
    `None` (an unavailable value on either side) is preserved as `None`,
    never coerced to zero or any other default -- this is the one place
    the "unavailable, not zero" contract for Matchup features is enforced.
    """
    if a is None or b is None:
        return None
    return round(float(a) - float(b), 3)

def build_key_edges(deltas: dict[str, float | None], team_a_slug: str, team_b_slug: str) -> list[dict[str, Any]]:
    """
    Phase 6, Slice 7A: public extraction of the original private
    `_build_key_edges` -- byte-for-byte identical logic, now reading its
    labels from the module-level `MATCHUP_FEATURE_LABELS` (previously a
    local literal inside this same function) instead of a second copy.
    A missing (`None`) delta is excluded entirely -- never fabricated as
    an "even" or zero edge; a genuinely stored `0.0` delta IS included
    (leader defaults to `team_a_slug` on an exact tie, matching the
    original `value >= 0` comparison unchanged). Sorted by `abs(delta)`
    descending -- the same deterministic ordering as before.

    Purely descriptive: `leader` names which team has the higher value
    for this one specific statistical feature -- never a prediction of
    the game's outcome (that remains `_build_projection`'s own, separate,
    explicitly out-of-scope concern for Phase 6, Slice 7A).
    """
    edges = []
    for key, label in MATCHUP_FEATURE_LABELS.items():
        value = deltas.get(key)
        if value is None:
            continue
        leader = team_a_slug if value >= 0 else team_b_slug
        edges.append(
            {
                "signal": key,
                "label": label,
                "leader": leader,
                "delta": round(float(value), 3),
            }
        )
    return sorted(edges, key=lambda row: abs(row["delta"]), reverse=True)

def build_matchup_feature_comparison(
    team_a_slug: str,
    team_b_slug: str,
    team_a_features: dict[str, float | None],
    team_b_features: dict[str, float | None],
) -> dict[str, Any]:
    """
    Phase 6, Slice 7A: the smallest new public analytical operation this
    slice required. `build_matchup_features` below already computes this
    exact comparison for these same 7 `MATCHUP_FEATURE_LABELS` keys, but
    only after first building two full `build_team_summary` results (file
    I/O, plus -- as of Slice 6A -- Injury acquisition, both out of this
    slice's approved scope). This function accepts the two teams' already
    -available flat feature values directly instead, and reuses
    `compute_feature_delta`/`build_key_edges` UNCHANGED for every actual
    calculation -- the only logic added here is "iterate over the same
    fixed key set and delegate," matching what `build_matchup_features`
    already does inline for these same keys. No new formula, key, or
    label; no odds, prediction, confidence, or medical/injury signal
    anywhere in this function.

    A key absent from either `team_a_features` or `team_b_features` is
    treated as unavailable (`None`) for that side, exactly like
    `compute_feature_delta`'s own existing convention -- never defaulted
    to zero, league-average, or "even."
    """
    deltas = {
        key: compute_feature_delta(team_a_features.get(key), team_b_features.get(key))
        for key in MATCHUP_FEATURE_LABELS
    }
    return {
        "deltas": deltas,
        "key_edges": build_key_edges(deltas, team_a_slug, team_b_slug),
    }


build_matchup_features = _omitted
