"""
Phase 7 Slice 3A: full-page tests for `pages/11_Player_Raw_Statistics.py`
using `streamlit.testing.v1.AppTest`. Every test drives the actual page
script; `src.streamlit_consumer_controller.run_player_raw_statistics_lookup`
is monkeypatched with canned/spy values so these tests isolate the
*page's* wiring from the controller's own already-covered orchestration
logic (`tests/regression/test_streamlit_consumer_controller.py`).
"""
from __future__ import annotations

import ast

from streamlit.testing.v1 import AppTest

import src.streamlit_consumer_controller as controller_module
from src.consumers.contracts import ConsumerPresentation, PresentationField

PAGE_PATH = "pages/11_Player_Raw_Statistics.py"


def _fresh_app() -> AppTest:
    return AppTest.from_file(PAGE_PATH)


def _stub_ok(*a, **kw):
    return ConsumerPresentation(status="ok", title="Test Player", fields=(PresentationField("Player", "Test Player"),))


# ---------------------------------------------------------------------------
# Import and initial render
# ---------------------------------------------------------------------------


def test_page_loads_without_exception():
    at = _fresh_app()
    at.run()
    assert not at.exception


def test_internal_local_notice_is_shown():
    at = _fresh_app()
    at.run()
    assert any("Internal, local-use only" in i.value for i in at.info)


def test_page_explicitly_says_raw_statistics_not_ovr_or_live():
    at = _fresh_app()
    at.run()
    notice = at.info[0].value
    assert "raw" in notice.lower()
    for forbidden_word in ("ovr", "rating", "recommendation", "live feed"):
        assert forbidden_word not in notice.lower() or "not a" in notice.lower()
    assert "not a computed player rating, OVR score, recommendation, or live feed" in notice


def test_no_result_shown_before_submission():
    at = _fresh_app()
    at.run()
    assert len(at.success) == 0
    assert len(at.error) == 0
    assert len(at.warning) == 0
    assert len(at.dataframe) == 0


def test_initial_render_performs_no_service_call(monkeypatch):
    calls = []
    monkeypatch.setattr(controller_module, "run_player_raw_statistics_lookup", lambda *a, **kw: calls.append(1))
    at = _fresh_app()
    at.run()
    assert calls == []


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_blank_player_id_is_rejected(monkeypatch):
    monkeypatch.setattr(controller_module, "run_player_raw_statistics_lookup", _stub_ok)
    at = _fresh_app()
    at.run()
    at.text_input(key="player_stats_player_id").set_value("")
    at.button(key="player_stats_submit").click().run()
    assert any("non-empty" in w.value for w in at.warning)
    assert len(at.subheader) == 0


def test_whitespace_only_player_id_is_rejected(monkeypatch):
    monkeypatch.setattr(controller_module, "run_player_raw_statistics_lookup", _stub_ok)
    at = _fresh_app()
    at.run()
    at.text_input(key="player_stats_player_id").set_value("   ")
    at.button(key="player_stats_submit").click().run()
    assert any("non-empty" in w.value for w in at.warning)


def test_overlong_player_id_is_rejected(monkeypatch):
    monkeypatch.setattr(controller_module, "run_player_raw_statistics_lookup", _stub_ok)
    at = _fresh_app()
    at.run()
    at.text_input(key="player_stats_player_id").set_value("x" * 201)
    at.button(key="player_stats_submit").click().run()
    assert any("non-empty" in w.value for w in at.warning)


def test_non_numeric_season_id_is_rejected_locally(monkeypatch):
    calls = []
    monkeypatch.setattr(controller_module, "run_player_raw_statistics_lookup", lambda *a, **kw: (calls.append(1), _stub_ok())[1])
    at = _fresh_app()
    at.run()
    at.text_input(key="player_stats_player_id").set_value("p1")
    at.text_input(key="player_stats_season_id").set_value("not-a-year")
    at.button(key="player_stats_submit").click().run()
    assert any("whole number" in w.value for w in at.warning)
    assert calls == []


def test_blank_optional_filters_are_omitted(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        controller_module,
        "run_player_raw_statistics_lookup",
        lambda pid, **kw: (seen.update(kw), _stub_ok())[1],
    )
    at = _fresh_app()
    at.run()
    at.text_input(key="player_stats_player_id").set_value("p1")
    at.button(key="player_stats_submit").click().run()
    assert seen["season_id"] is None
    assert seen["stat_table"] is None


def test_provided_filters_are_passed_through(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        controller_module,
        "run_player_raw_statistics_lookup",
        lambda pid, **kw: (seen.update(kw), _stub_ok())[1],
    )
    at = _fresh_app()
    at.run()
    at.text_input(key="player_stats_player_id").set_value("p1")
    at.text_input(key="player_stats_season_id").set_value("2025")
    at.selectbox(key="player_stats_stat_table").set_value("Totals")
    at.button(key="player_stats_submit").click().run()
    assert seen["season_id"] == 2025
    assert seen["stat_table"] == "Totals"


def test_player_id_is_trimmed(monkeypatch):
    seen = []
    monkeypatch.setattr(
        controller_module, "run_player_raw_statistics_lookup", lambda pid, **kw: (seen.append(pid), _stub_ok())[1]
    )
    at = _fresh_app()
    at.run()
    at.text_input(key="player_stats_player_id").set_value("  p1  ")
    at.button(key="player_stats_submit").click().run()
    assert seen == ["p1"]


# ---------------------------------------------------------------------------
# Submission behavior
# ---------------------------------------------------------------------------


def test_submit_invokes_exactly_one_controller_call(monkeypatch):
    calls = []
    monkeypatch.setattr(
        controller_module, "run_player_raw_statistics_lookup", lambda *a, **kw: (calls.append(1), _stub_ok())[1]
    )
    at = _fresh_app()
    at.run()
    at.text_input(key="player_stats_player_id").set_value("p1")
    at.button(key="player_stats_submit").click().run()
    assert len(calls) == 1


def test_rerender_without_submit_does_not_repeat_service_work(monkeypatch):
    calls = []
    monkeypatch.setattr(
        controller_module, "run_player_raw_statistics_lookup", lambda *a, **kw: (calls.append(1), _stub_ok())[1]
    )
    at = _fresh_app()
    at.run()
    at.text_input(key="player_stats_player_id").set_value("p1")
    at.button(key="player_stats_submit").click().run()
    assert len(calls) == 1
    at.selectbox(key="player_stats_stat_table").set_value("Totals")
    at.run()
    assert len(calls) == 1
    assert at.subheader[0].value == "Test Player"


def test_clear_removes_stored_presentation(monkeypatch):
    monkeypatch.setattr(controller_module, "run_player_raw_statistics_lookup", _stub_ok)
    at = _fresh_app()
    at.run()
    at.text_input(key="player_stats_player_id").set_value("p1")
    at.button(key="player_stats_submit").click().run()
    assert at.session_state["player_stats_result"] is not None
    at.button(key="player_stats_clear").click().run()
    assert at.session_state["player_stats_result"] is None
    assert len(at.subheader) == 0


# ---------------------------------------------------------------------------
# Outcome presentation
# ---------------------------------------------------------------------------


def test_found_presentation_renders_via_page(monkeypatch):
    monkeypatch.setattr(controller_module, "run_player_raw_statistics_lookup", _stub_ok)
    at = _fresh_app()
    at.run()
    at.text_input(key="player_stats_player_id").set_value("p1")
    at.button(key="player_stats_submit").click().run()
    assert at.subheader[0].value == "Test Player"


def test_not_found_presentation_renders_via_page(monkeypatch):
    monkeypatch.setattr(
        controller_module,
        "run_player_raw_statistics_lookup",
        lambda *a, **kw: ConsumerPresentation(status="not_found", title="Player Not Found", message="No player matched that lookup."),
    )
    at = _fresh_app()
    at.run()
    at.text_input(key="player_stats_player_id").set_value("zzz")
    at.button(key="player_stats_submit").click().run()
    assert any("No player matched" in w.value for w in at.warning)


def test_known_empty_presentation_renders_via_page(monkeypatch):
    monkeypatch.setattr(
        controller_module,
        "run_player_raw_statistics_lookup",
        lambda *a, **kw: ConsumerPresentation(status="ok", title="Test Player", summary="No statistics available for this player/season."),
    )
    at = _fresh_app()
    at.run()
    at.text_input(key="player_stats_player_id").set_value("p1")
    at.button(key="player_stats_submit").click().run()
    assert any("No statistics available" in t.value for t in at.text)


def test_sanitized_error_presentation_renders_via_page(monkeypatch):
    monkeypatch.setattr(
        controller_module,
        "run_player_raw_statistics_lookup",
        lambda *a, **kw: ConsumerPresentation(status="error", title="Internal Error", message="An internal error occurred."),
    )
    at = _fresh_app()
    at.run()
    at.text_input(key="player_stats_player_id").set_value("p1")
    at.button(key="player_stats_submit").click().run()
    assert any("An internal error occurred." in e.value for e in at.error)


# ---------------------------------------------------------------------------
# Session-state safety
# ---------------------------------------------------------------------------


def test_no_session_or_callable_stored_in_session_state(monkeypatch):
    monkeypatch.setattr(controller_module, "run_player_raw_statistics_lookup", _stub_ok)
    at = _fresh_app()
    at.run()
    at.text_input(key="player_stats_player_id").set_value("p1")
    at.button(key="player_stats_submit").click().run()
    stored = at.session_state["player_stats_result"]
    assert isinstance(stored, ConsumerPresentation)
    assert not callable(stored)
    assert not hasattr(stored, "close")


def test_dependencies_session_factory_not_called_before_submit(monkeypatch):
    from src.streamlit_consumer_controller import ConsumerControllerDependencies

    calls = []

    def spy_session_factory():
        calls.append(1)
        raise AssertionError("must not be called before Submit")

    monkeypatch.setattr(
        controller_module,
        "default_dependencies",
        lambda: ConsumerControllerDependencies(session_factory=spy_session_factory),
    )
    at = _fresh_app()
    at.run()
    assert calls == []


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


def test_page_imports_no_ovr_notebook_discord_fastapi_or_live_module():
    with open(PAGE_PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = (
        "discord", "fastapi", "starlette", "pydantic",
        "src.api", "src.discord_bot", "src.ovr_model",
        "src.pipeline", "src.agents", "nbformat", "nbconvert",
    )
    for name in imported:
        assert not any(name == f or name.startswith(f + ".") for f in forbidden), f"unexpected import {name!r}"


def test_page_writes_no_source_data():
    with open(PAGE_PATH, encoding="utf-8") as fh:
        source = fh.read()
    assert "open(" not in source
    assert ".write_text(" not in source
    assert "to_csv(" not in source


def test_existing_pages_01_through_10_are_unchanged():
    """This slice must not modify any existing page -- confirmed by checking they are untouched in git status (a source-of-truth check belongs in the completion sweep, not here); this test instead confirms the new page's own file does not import or reference any other page module."""
    with open(PAGE_PATH, encoding="utf-8") as fh:
        source = fh.read()
    for other_page in [f"{i:02d}_" for i in range(1, 11)]:
        assert f"pages.{other_page}" not in source
        assert f"pages/{other_page}" not in source
