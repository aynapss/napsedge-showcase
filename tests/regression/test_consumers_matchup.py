"""
Phase 7 Slice 3C: tests for
`src.consumers.matchup.present_matchup_feature_comparison`. Uses only
synthetic `MatchupFeatureComparisonResult`/`FeatureComparisonEntry`
instances and hand-built feature dicts -- never the real
`src.services.matchup.compare_team_matchup_features` itself (that
service's own behavior is covered by `tests/regression/test_services_matchup.py`
and `tests/api/test_matchup.py`, both unaffected by this batch).
"""
from __future__ import annotations

import ast
import inspect

import src.consumers.matchup as matchup_adapter_module
from src.consumers.matchup import present_matchup_feature_comparison
from src.features.matchup_features import MATCHUP_FEATURE_LABELS
from src.services.contracts import FeatureComparisonEntry, MatchupFeatureComparisonResult

_ALL_KEYS_NONE: dict[str, float | None] = {key: None for key in MATCHUP_FEATURE_LABELS}

#: Product-language boundary approved for Phase 7 Slice 3C -- none of
#: these words may ever appear anywhere this adapter's output could
#: render, since the underlying service never declares a winner,
#: projection, or betting value and this presentation must not either.
_PROHIBITED_WORDS = (
    "advantage", "edge", "favorite", "winner", "projection", "prediction",
    "probability", "confidence", "spread", "total", "betting", "live comparison",
)


def _result(
    team_a_slug: str = "duke",
    team_b_slug: str = "unc",
    deltas: dict[str, float | None] | None = None,
    key_edges: tuple[FeatureComparisonEntry, ...] = (),
) -> MatchupFeatureComparisonResult:
    return MatchupFeatureComparisonResult(
        team_a_slug=team_a_slug,
        team_b_slug=team_b_slug,
        deltas=dict(_ALL_KEYS_NONE) if deltas is None else deltas,
        key_edges=key_edges,
    )


def test_all_seven_feature_keys_produce_one_row_each():
    presentation = present_matchup_feature_comparison(_result(), {}, {})
    assert len(presentation.rows) == 7


def test_exact_feature_labels_match_the_authority():
    presentation = present_matchup_feature_comparison(_result(), {}, {})
    shown_labels = [next(f.value for f in row if f.label == "Feature") for row in presentation.rows]
    assert shown_labels == list(MATCHUP_FEATURE_LABELS.values())


def test_team_labels_are_echoed_exactly_as_supplied():
    presentation = present_matchup_feature_comparison(_result(team_a_slug="Duke Blue Devils", team_b_slug="UNC Tar Heels"), {}, {})
    field_values = {f.label: f.value for f in presentation.fields}
    assert field_values == {"Team A": "Duke Blue Devils", "Team B": "UNC Tar Heels"}
    assert presentation.title == "Duke Blue Devils vs UNC Tar Heels"


def test_status_is_always_ok_never_not_found_or_conflict():
    presentation = present_matchup_feature_comparison(_result(), {}, {})
    assert presentation.status == "ok"


def test_row_ordering_matches_the_fixed_authority_declaration_order():
    presentation = present_matchup_feature_comparison(_result(), {}, {})
    shown_keys_by_label = {label: key for key, label in MATCHUP_FEATURE_LABELS.items()}
    shown_order = [shown_keys_by_label[next(f.value for f in row if f.label == "Feature")] for row in presentation.rows]
    assert shown_order == list(MATCHUP_FEATURE_LABELS.keys())


def test_signed_delta_is_preserved_exactly_from_the_result():
    deltas = dict(_ALL_KEYS_NONE, avg_margin=7.0, off_impact=-8.75)
    presentation = present_matchup_feature_comparison(_result(deltas=deltas), {"avg_margin": 10.0}, {"avg_margin": 3.0})
    row = next(r for r in presentation.rows if any(f.value == "season scoring margin" for f in r))
    assert next(f.value for f in row if f.label == "Difference") == "7.0"


def test_three_decimal_service_rounding_is_preserved_not_recomputed():
    """The adapter must echo `result.deltas` verbatim, never re-derive it from the raw team dicts."""
    deltas = dict(_ALL_KEYS_NONE, avg_margin=0.333)
    presentation = present_matchup_feature_comparison(_result(deltas=deltas), {"avg_margin": 999.0}, {"avg_margin": -999.0})
    row = next(r for r in presentation.rows if any(f.value == "season scoring margin" for f in r))
    assert next(f.value for f in row if f.label == "Difference") == "0.333"


def test_team_a_and_team_b_values_come_from_the_raw_dicts_not_the_result():
    presentation = present_matchup_feature_comparison(_result(), {"avg_margin": 5.25}, {"avg_margin": 2.5})
    row = next(r for r in presentation.rows if any(f.value == "season scoring margin" for f in r))
    assert next(f.value for f in row if f.label == "Team A value") == "5.25"
    assert next(f.value for f in row if f.label == "Team B value") == "2.5"


def test_missing_value_on_either_side_is_none_not_a_placeholder():
    presentation = present_matchup_feature_comparison(_result(), {}, {"off_impact": 3.0})
    row = next(r for r in presentation.rows if any(f.value == "season scoring margin" for f in r))
    assert next(f.value for f in row if f.label == "Team A value") is None
    assert next(f.value for f in row if f.label == "Team B value") is None
    assert next(f.value for f in row if f.label == "Difference") is None


def test_genuine_stored_zero_is_preserved_not_treated_as_missing():
    deltas = dict(_ALL_KEYS_NONE, avg_margin=0.0)
    presentation = present_matchup_feature_comparison(_result(deltas=deltas), {"avg_margin": 0.0}, {"avg_margin": 0.0})
    row = next(r for r in presentation.rows if any(f.value == "season scoring margin" for f in r))
    assert next(f.value for f in row if f.label == "Team A value") == "0.0"
    assert next(f.value for f in row if f.label == "Team B value") == "0.0"
    assert next(f.value for f in row if f.label == "Difference") == "0.0"


def test_both_sides_missing_for_every_feature_is_a_valid_ok_result_not_an_error():
    presentation = present_matchup_feature_comparison(_result(), {}, {})
    assert presentation.status == "ok"
    assert len(presentation.rows) == 7
    for row in presentation.rows:
        assert next(f.value for f in row if f.label == "Team A value") is None
        assert next(f.value for f in row if f.label == "Team B value") is None
        assert next(f.value for f in row if f.label == "Difference") is None


def test_key_edges_and_leader_are_never_read_or_exposed():
    """`key_edges`/`leader` risk reading as an advantage/winner claim -- the adapter must ignore them entirely, even when the result carries some."""
    key_edges = (FeatureComparisonEntry(signal="avg_margin", label="season scoring margin", leader="duke", delta=7.0),)
    presentation = present_matchup_feature_comparison(_result(key_edges=key_edges), {"avg_margin": 10.0}, {"avg_margin": 3.0})
    rendered = repr(presentation)
    assert "leader" not in rendered.lower()
    assert "FeatureComparisonEntry" not in rendered


def test_no_prohibited_product_language_appears_anywhere_in_output():
    deltas = dict(_ALL_KEYS_NONE, avg_margin=7.0)
    presentation = present_matchup_feature_comparison(_result(deltas=deltas), {"avg_margin": 10.0}, {"avg_margin": 3.0})
    rendered = repr(presentation).lower()
    for word in _PROHIBITED_WORDS:
        assert word not in rendered, f"prohibited word {word!r} found in adapter output"


def test_repeated_calls_produce_equal_presentations():
    deltas = dict(_ALL_KEYS_NONE, avg_margin=7.0)
    result = _result(deltas=deltas)
    presentation_one = present_matchup_feature_comparison(result, {"avg_margin": 10.0}, {"avg_margin": 3.0})
    presentation_two = present_matchup_feature_comparison(result, {"avg_margin": 10.0}, {"avg_margin": 3.0})
    assert presentation_one == presentation_two


def test_module_makes_no_service_database_or_calculation_call():
    source = inspect.getsource(matchup_adapter_module)
    forbidden = ["compare_team_matchup_features(", "session.", "Session(", "round(", "sorted(", "build_matchup_feature_comparison(", "resolve_"]
    for token in forbidden:
        assert token not in source, f"unexpected {token!r} found in src/consumers/matchup.py"


def test_module_imports_no_framework_database_odds_agent_or_notebook_dependency():
    tree = ast.parse(inspect.getsource(matchup_adapter_module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = (
        "discord", "streamlit", "fastapi", "starlette", "pydantic",
        "sqlalchemy", "src.db", "src.ovr_model", "src.identity",
        "requests", "httpx", "urllib", "socket", "nbformat", "nbconvert",
        "src.agents", "market_features",
    )
    for name in imported:
        assert not any(name == f or name.startswith(f + ".") for f in forbidden), f"unexpected import {name!r}"
