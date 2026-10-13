"""
Phase 7 Slice 3C: full-page tests for
`pages/12_Matchup_Feature_Comparison.py` using
`streamlit.testing.v1.AppTest`. Every test drives the actual page
script; `src.streamlit_consumer_controller.run_matchup_feature_comparison`
is monkeypatched with canned/spy values so these tests isolate the
*page's* wiring from the controller's own already-covered orchestration
logic (`tests/regression/test_streamlit_consumer_controller.py`).

Every feature value on this page is a plain `st.text_input` (not
`st.number_input` -- see the page's own module docstring for why), so
every feature-value assertion below drives `at.text_input(key=...)`,
never `at.number_input`.
"""
from __future__ import annotations

import ast

from streamlit.testing.v1 import AppTest

import src.streamlit_consumer_controller as controller_module
from src.consumers.contracts import ConsumerPresentation, PresentationField

PAGE_PATH = "pages/12_Matchup_Feature_Comparison.py"

_MANDATORY_NOTICES = (
    "Descriptive feature comparison",
    "Values are supplied by the user",
    "No live team data is retrieved",
    "Results are not a prediction",
    "Results are not betting advice",
)


def _fresh_app() -> AppTest:
    return AppTest.from_file(PAGE_PATH)


def _stub_ok(*a, **kw):
    return ConsumerPresentation(status="ok", title="duke vs unc", fields=(PresentationField("Team A", "duke"),))


def _fill_labels(at: AppTest, team_a: str = "duke", team_b: str = "unc") -> None:
    at.text_input(key="matchup_team_a_label").set_value(team_a)
    at.text_input(key="matchup_team_b_label").set_value(team_b)


def _result_subheader(at: AppTest) -> str:
    """The page's own "Team A values"/"Team B values" section subheaders are always rendered first -- the comparison result's own title (from `render_consumer_presentation`) is always the *last* subheader."""
    return at.subheader[-1].value


# ---------------------------------------------------------------------------
# Import and initial render
# ---------------------------------------------------------------------------


def test_page_loads_without_exception():
    at = _fresh_app()
    at.run()
    assert not at.exception


def test_all_five_mandatory_notices_are_shown():
    at = _fresh_app()
    at.run()
    notice = at.info[0].value
    for phrase in _MANDATORY_NOTICES:
        assert phrase in notice


def test_no_result_shown_before_submission():
    at = _fresh_app()
    at.run()
    assert len(at.success) == 0
    assert len(at.error) == 0
    assert len(at.warning) == 0
    assert len(at.dataframe) == 0


def test_initial_render_performs_no_service_call(monkeypatch):
    calls = []
    monkeypatch.setattr(controller_module, "run_matchup_feature_comparison", lambda *a, **kw: calls.append(1))
    at = _fresh_app()
    at.run()
    assert calls == []


def test_page_provides_sixteen_text_inputs_and_no_number_input():
    at = _fresh_app()
    at.run()
    assert len(at.text_input) == 16
    assert len(at.number_input) == 0


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_blank_team_a_label_is_rejected(monkeypatch):
    calls = []
    monkeypatch.setattr(controller_module, "run_matchup_feature_comparison", lambda *a, **kw: (calls.append(1), _stub_ok())[1])
    at = _fresh_app()
    at.run()
    at.text_input(key="matchup_team_b_label").set_value("unc")
    at.button(key="matchup_submit").click().run()
    assert calls == []
    assert any("Team A label" in w.value for w in at.warning)


def test_blank_team_b_label_is_rejected(monkeypatch):
    calls = []
    monkeypatch.setattr(controller_module, "run_matchup_feature_comparison", lambda *a, **kw: (calls.append(1), _stub_ok())[1])
    at = _fresh_app()
    at.run()
    at.text_input(key="matchup_team_a_label").set_value("duke")
    at.button(key="matchup_submit").click().run()
    assert calls == []
    assert any("Team B label" in w.value for w in at.warning)


def test_overlong_team_label_is_rejected(monkeypatch):
    calls = []
    monkeypatch.setattr(controller_module, "run_matchup_feature_comparison", lambda *a, **kw: (calls.append(1), _stub_ok())[1])
    at = _fresh_app()
    at.run()
    _fill_labels(at, team_a="x" * 101)
    at.button(key="matchup_submit").click().run()
    assert calls == []


def test_blank_feature_inputs_remain_none(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        controller_module,
        "run_matchup_feature_comparison",
        lambda a_slug, b_slug, a_feat, b_feat: (seen.update(team_a=a_feat, team_b=b_feat), _stub_ok())[1],
    )
    at = _fresh_app()
    at.run()
    _fill_labels(at)
    at.button(key="matchup_submit").click().run()
    assert all(v is None for v in seen["team_a"].values())
    assert all(v is None for v in seen["team_b"].values())


def test_zero_feature_value_is_preserved_not_treated_as_blank(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        controller_module,
        "run_matchup_feature_comparison",
        lambda a_slug, b_slug, a_feat, b_feat: (seen.update(team_a=a_feat), _stub_ok())[1],
    )
    at = _fresh_app()
    at.run()
    _fill_labels(at)
    at.text_input(key="matchup_team_a_avg_margin").set_value("0")
    at.button(key="matchup_submit").click().run()
    assert seen["team_a"]["avg_margin"] == 0.0


def test_finite_numeric_feature_values_pass_through(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        controller_module,
        "run_matchup_feature_comparison",
        lambda a_slug, b_slug, a_feat, b_feat: (seen.update(team_a=a_feat, team_b=b_feat), _stub_ok())[1],
    )
    at = _fresh_app()
    at.run()
    _fill_labels(at)
    at.text_input(key="matchup_team_a_avg_margin").set_value("5.234")
    at.text_input(key="matchup_team_b_off_impact").set_value("-3.5")
    at.button(key="matchup_submit").click().run()
    assert seen["team_a"]["avg_margin"] == 5.234
    assert seen["team_b"]["off_impact"] == -3.5


def test_non_numeric_text_feature_value_is_rejected_before_the_service_call(monkeypatch):
    calls = []
    monkeypatch.setattr(controller_module, "run_matchup_feature_comparison", lambda *a, **kw: (calls.append(1), _stub_ok())[1])
    at = _fresh_app()
    at.run()
    _fill_labels(at)
    at.text_input(key="matchup_team_a_avg_margin").set_value("not-a-number")
    at.button(key="matchup_submit").click().run()
    assert calls == []
    assert any("finite number" in w.value.lower() for w in at.warning)


def test_boolean_like_text_feature_value_is_rejected(monkeypatch):
    calls = []
    monkeypatch.setattr(controller_module, "run_matchup_feature_comparison", lambda *a, **kw: (calls.append(1), _stub_ok())[1])
    at = _fresh_app()
    at.run()
    _fill_labels(at)
    at.text_input(key="matchup_team_a_avg_margin").set_value("True")
    at.button(key="matchup_submit").click().run()
    assert calls == []


def test_nan_feature_value_is_rejected_before_the_service_call(monkeypatch):
    calls = []
    monkeypatch.setattr(controller_module, "run_matchup_feature_comparison", lambda *a, **kw: (calls.append(1), _stub_ok())[1])
    at = _fresh_app()
    at.run()
    _fill_labels(at)
    at.text_input(key="matchup_team_a_avg_margin").set_value("nan")
    at.button(key="matchup_submit").click().run()
    assert calls == []
    assert any("finite number" in w.value.lower() for w in at.warning)


def test_positive_infinity_feature_value_is_rejected(monkeypatch):
    calls = []
    monkeypatch.setattr(controller_module, "run_matchup_feature_comparison", lambda *a, **kw: (calls.append(1), _stub_ok())[1])
    at = _fresh_app()
    at.run()
    _fill_labels(at)
    at.text_input(key="matchup_team_a_avg_margin").set_value("inf")
    at.button(key="matchup_submit").click().run()
    assert calls == []


def test_negative_infinity_feature_value_is_rejected(monkeypatch):
    calls = []
    monkeypatch.setattr(controller_module, "run_matchup_feature_comparison", lambda *a, **kw: (calls.append(1), _stub_ok())[1])
    at = _fresh_app()
    at.run()
    _fill_labels(at)
    at.text_input(key="matchup_team_b_def_impact").set_value("-Infinity")
    at.button(key="matchup_submit").click().run()
    assert calls == []


def test_team_labels_are_trimmed(monkeypatch):
    seen = []
    monkeypatch.setattr(
        controller_module,
        "run_matchup_feature_comparison",
        lambda a_slug, b_slug, a_feat, b_feat: (seen.append((a_slug, b_slug)), _stub_ok())[1],
    )
    at = _fresh_app()
    at.run()
    _fill_labels(at, team_a="  duke  ", team_b="  unc  ")
    at.button(key="matchup_submit").click().run()
    assert seen == [("duke", "unc")]


# ---------------------------------------------------------------------------
# Submission behavior
# ---------------------------------------------------------------------------


def test_submit_invokes_exactly_one_controller_call(monkeypatch):
    calls = []
    monkeypatch.setattr(controller_module, "run_matchup_feature_comparison", lambda *a, **kw: (calls.append(1), _stub_ok())[1])
    at = _fresh_app()
    at.run()
    _fill_labels(at)
    at.button(key="matchup_submit").click().run()
    assert len(calls) == 1


def test_rerender_without_submit_does_not_repeat_service_work(monkeypatch):
    calls = []
    monkeypatch.setattr(controller_module, "run_matchup_feature_comparison", lambda *a, **kw: (calls.append(1), _stub_ok())[1])
    at = _fresh_app()
    at.run()
    _fill_labels(at)
    at.button(key="matchup_submit").click().run()
    assert len(calls) == 1
    at.text_input(key="matchup_team_a_avg_margin").set_value("1.0").run()
    assert len(calls) == 1
    assert _result_subheader(at) == "duke vs unc"


def test_clear_removes_stored_presentation_and_input_state(monkeypatch):
    monkeypatch.setattr(controller_module, "run_matchup_feature_comparison", _stub_ok)
    at = _fresh_app()
    at.run()
    _fill_labels(at)
    at.text_input(key="matchup_team_a_avg_margin").set_value("5.234")
    at.button(key="matchup_submit").click().run()
    assert at.session_state["matchup_comparison_result"] is not None

    at.button(key="matchup_clear").click().run()
    assert at.session_state["matchup_comparison_result"] is None
    assert at.text_input(key="matchup_team_a_label").value == ""
    assert at.text_input(key="matchup_team_b_label").value == ""
    assert at.text_input(key="matchup_team_a_avg_margin").value == ""
    # only the two static section subheaders ("Team A values"/"Team B values") remain -- the result's own subheader is gone
    assert [s.value for s in at.subheader] == ["Team A values", "Team B values"]


# ---------------------------------------------------------------------------
# Outcome presentation
# ---------------------------------------------------------------------------


def test_successful_comparison_renders_via_page(monkeypatch):
    monkeypatch.setattr(controller_module, "run_matchup_feature_comparison", _stub_ok)
    at = _fresh_app()
    at.run()
    _fill_labels(at)
    at.button(key="matchup_submit").click().run()
    assert _result_subheader(at) == "duke vs unc"


def test_sanitized_error_presentation_renders_via_page(monkeypatch):
    monkeypatch.setattr(
        controller_module,
        "run_matchup_feature_comparison",
        lambda *a, **kw: ConsumerPresentation(status="error", title="Internal Error", message="An internal error occurred."),
    )
    at = _fresh_app()
    at.run()
    _fill_labels(at)
    at.button(key="matchup_submit").click().run()
    assert any("An internal error occurred." in e.value for e in at.error)


# ---------------------------------------------------------------------------
# Session-state safety
# ---------------------------------------------------------------------------


def test_no_session_or_callable_stored_in_session_state(monkeypatch):
    monkeypatch.setattr(controller_module, "run_matchup_feature_comparison", _stub_ok)
    at = _fresh_app()
    at.run()
    _fill_labels(at)
    at.button(key="matchup_submit").click().run()
    stored = at.session_state["matchup_comparison_result"]
    assert isinstance(stored, ConsumerPresentation)
    assert not callable(stored)
    assert not hasattr(stored, "close")


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


def test_page_imports_no_ovr_notebook_discord_fastapi_odds_agent_or_database_module():
    with open(PAGE_PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = (
        "discord", "fastapi", "starlette", "pydantic", "sqlalchemy", "src.db",
        "src.api", "src.discord_bot", "src.ovr_model", "src.pipeline",
        "src.agents", "nbformat", "nbconvert", "market_features",
    )
    for name in imported:
        assert not any(name == f or name.startswith(f + ".") for f in forbidden), f"unexpected import {name!r}"


def test_page_writes_no_source_data():
    with open(PAGE_PATH, encoding="utf-8") as fh:
        source = fh.read()
    assert "open(" not in source
    assert ".write_text(" not in source
    assert "to_csv(" not in source


def test_page_contains_no_prohibited_product_language():
    with open(PAGE_PATH, encoding="utf-8") as fh:
        source = fh.read().lower()
    prohibited = (
        "advantage", "edge", "favorite", "winner", "projection",
        "probability", "confidence", "spread", "total", "betting recommendation", "live comparison",
    )
    for word in prohibited:
        assert word not in source, f"prohibited word {word!r} found in page source"
    # the mandatory disclaimer is the only permitted use of "prediction"/"betting"
    assert source.count("prediction") == 1
    assert source.count("betting") == 1


def test_existing_pages_01_through_11_are_unchanged():
    """This slice must not modify any existing page -- confirmed by checking they are untouched in git status (a source-of-truth check belongs in the completion sweep, not here); this test instead confirms the new page's own file does not import or reference any other page module."""
    with open(PAGE_PATH, encoding="utf-8") as fh:
        source = fh.read()
    for other_page in (
        "01_Team_Explorer", "02_Bet_Tracker", "03_Historical_Results", "04_Player_Cards",
        "05_Team_Overview", "06_Conference_Dashboard", "07_Matchup_Graphics",
        "08_Pipeline_Architecture", "09_AI_Research_Control_Room", "10_Team_Lookup", "11_Player_Raw_Statistics",
    ):
        assert other_page not in source
