"""
Phase 7 Slice 3C: pure presentation adapter for
`src.services.matchup.compare_team_matchup_features`'s existing
`MatchupFeatureComparisonResult` -- no database, no filesystem, no
network, no notebook-execution, and no Odds/agent/LLM import anywhere in
this module. The adapter only ever reads fields already carried by the
typed service result, plus the same two raw per-team feature-value
mappings the caller already built before calling the service --
`MatchupFeatureComparisonResult` itself carries only the computed
`deltas`, not each side's own input value, so a comparison table showing
both sides' raw values alongside the difference needs both.

Strictly descriptive, matching the underlying service's own explicit
scope. No delta calculation, rounding, sorting, or normalization happens
here -- `deltas` is used exactly as the service already computed it
(three-decimal rounding already applied there), in the exact key order
`src.features.matchup_features.MATCHUP_FEATURE_LABELS` already fixes.

This adapter never reads or exposes `result.key_edges`/`leader`:
`key_edges` is magnitude-sorted and names one side as the numeric
"leader" for a feature, which risks reading as declaring an advantage
for that side -- language this tool's approved product boundary
explicitly prohibits. Every one of the 7 fixed features is shown for
every comparison, in the same fixed order, with each side's own raw
value and the signed difference -- nothing is ranked, sorted by
magnitude, or singled out as a "winner."
"""
from __future__ import annotations

from src.consumers.contracts import ConsumerPresentation, PresentationField
from src.features.matchup_features import MATCHUP_FEATURE_LABELS
from src.services.contracts import MatchupFeatureComparisonResult

_TITLE_TEMPLATE = "{team_a} vs {team_b}"


def _value_to_str(value: float | None) -> str | None:
    """`None` stays `None` -- never coerced to "unavailable" or zero here (that is a rendering decision, not a presentation-contract one). A genuinely-supplied or genuinely-computed `0.0` is preserved via plain `str()`, never treated as missing. No rounding, no normalization, no recalculation."""
    return None if value is None else str(value)


def _feature_row(
    feature_key: str,
    feature_label: str,
    team_a_features: dict[str, float | None],
    team_b_features: dict[str, float | None],
    deltas: dict[str, float | None],
) -> tuple[PresentationField, ...]:
    return (
        PresentationField("Feature", feature_label),
        PresentationField("Team A value", _value_to_str(team_a_features.get(feature_key))),
        PresentationField("Team B value", _value_to_str(team_b_features.get(feature_key))),
        PresentationField("Difference", _value_to_str(deltas.get(feature_key))),
    )


def present_matchup_feature_comparison(
    result: MatchupFeatureComparisonResult,
    team_a_features: dict[str, float | None],
    team_b_features: dict[str, float | None],
) -> ConsumerPresentation:
    """
    Maps one `MatchupFeatureComparisonResult` -- together with the same
    two raw feature-value mappings the caller already passed into
    `compare_team_matchup_features` -- to a `ConsumerPresentation`.

    `compare_team_matchup_features` has no identity-resolution or
    database dependency of its own and never raises for a normal call --
    unlike Team/Player, it has no domain-specific `not_found`/`conflict`
    outcome, so this adapter always returns `status="ok"`; any
    unexpected exception a caller's own call raises is mapped separately
    by the existing, unmodified `src.consumers.errors.present_error`,
    exactly matching the Team/Rankings/Injury/Player precedent (an
    adapter only ever receives an already-computed result, never an
    exception).

    `team_a_slug`/`team_b_slug` (caller-supplied labels only, never a
    validated canonical Team identity) are exposed as top-level `fields`,
    echoed exactly as supplied. One `PresentationRow` is built per fixed
    feature key -- all 7, always, in
    `src.features.matchup_features.MATCHUP_FEATURE_LABELS`'s own
    declaration order, never magnitude-sorted -- showing that feature's
    label, each side's own raw value, and the signed difference. A value
    missing on either side renders as `None` (never coerced to zero, and
    never silently dropped from the table), and a genuinely-supplied or
    genuinely-computed `0.0` is preserved and never hidden.
    """
    fields = (
        PresentationField("Team A", result.team_a_slug),
        PresentationField("Team B", result.team_b_slug),
    )
    rows = tuple(
        _feature_row(feature_key, feature_label, team_a_features, team_b_features, result.deltas)
        for feature_key, feature_label in MATCHUP_FEATURE_LABELS.items()
    )
    return ConsumerPresentation(
        status="ok",
        title=_TITLE_TEMPLATE.format(team_a=result.team_a_slug, team_b=result.team_b_slug),
        fields=fields,
        rows=rows,
    )
