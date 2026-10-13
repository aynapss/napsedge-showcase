"""
Phase 7, Slice 2B: full-page tests for `pages/10_Team_Lookup.py` using
`streamlit.testing.v1.AppTest`. Every test drives the actual page
script; `src.streamlit_consumer_controller`'s own `run_*`/`default_dependencies`
functions are monkeypatched with canned/spy values so these tests
isolate the *page's* wiring (widget behavior, submit/clear flow,
operation routing, session-state handling) from the controller's own
already-covered orchestration logic
(`tests/regression/test_streamlit_consumer_controller.py`).
"""
from __future__ import annotations

import ast
import inspect

import pytest
from streamlit.testing.v1 import AppTest

import src.streamlit_consumer_controller as controller_module
from src.consumers.contracts import ConsumerPresentation, PresentationField

PAGE_PATH = "pages/10_Team_Lookup.py"


def _fresh_app() -> AppTest:
    return AppTest.from_file(PAGE_PATH)


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


def test_expected_controls_are_shown():
    at = _fresh_app()
    at.run()
    assert at.radio[0].options == ["Team overview", "NET Ranking", "Injury report"]
    assert "Submit" in [b.label for b in at.button]
    assert "Clear" in [b.label for b in at.button]


def test_no_result_shown_before_submission():
    at = _fresh_app()
    at.run()
    assert len(at.success) == 0
    assert len(at.error) == 0
    assert len(at.warning) == 0
    assert len(at.dataframe) == 0


def test_initial_render_performs_no_service_call(monkeypatch):
    calls = []
    monkeypatch.setattr(controller_module, "run_team_lookup", lambda *a, **kw: calls.append(1))
    monkeypatch.setattr(controller_module, "run_rankings_lookup", lambda *a, **kw: calls.append(1))
    monkeypatch.setattr(controller_module, "run_injury_lookup", lambda *a, **kw: calls.append(1))
    at = _fresh_app()
    at.run()
    assert calls == []


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def _stub_ok(*a, **kw):
    return ConsumerPresentation(status="ok", title="Duke", fields=(PresentationField("Team", "Duke"),))


def test_empty_query_is_rejected(monkeypatch):
    monkeypatch.setattr(controller_module, "run_team_lookup", _stub_ok)
    at = _fresh_app()
    at.run()
    at.text_input(key="team_lookup_query").set_value("")
    at.button(key="team_lookup_submit").click().run()
    assert any("non-empty" in w.value for w in at.warning)
    assert len(at.subheader) == 0


def test_whitespace_only_query_is_rejected(monkeypatch):
    monkeypatch.setattr(controller_module, "run_team_lookup", _stub_ok)
    at = _fresh_app()
    at.run()
    at.text_input(key="team_lookup_query").set_value("   ")
    at.button(key="team_lookup_submit").click().run()
    assert any("non-empty" in w.value for w in at.warning)


def test_overlong_query_is_rejected(monkeypatch):
    monkeypatch.setattr(controller_module, "run_team_lookup", _stub_ok)
    at = _fresh_app()
    at.run()
    at.text_input(key="team_lookup_query").set_value("x" * 101)
    at.button(key="team_lookup_submit").click().run()
    assert any("non-empty" in w.value for w in at.warning)
    assert len(at.subheader) == 0


def test_query_is_trimmed_exactly_once(monkeypatch):
    seen = []
    monkeypatch.setattr(controller_module, "run_team_lookup", lambda query, **kw: (seen.append(query), _stub_ok())[1])
    at = _fresh_app()
    at.run()
    at.text_input(key="team_lookup_query").set_value("  Duke  ")
    at.button(key="team_lookup_submit").click().run()
    assert seen == ["Duke"]


def test_page_source_contains_no_identity_normalization_logic():
    """The page's own source performs no case-folding, slug-building, or alias lookup -- the cleaned query is passed straight through to the controller."""
    with open(PAGE_PATH, encoding="utf-8") as fh:
        source = fh.read()
    forbidden = ("resolve_team_identity", "canonical_team_slug", "normalize_team_name", ".lower()", ".replace(")
    for token in forbidden:
        assert token not in source, f"unexpected {token!r} found in {PAGE_PATH}"


# ---------------------------------------------------------------------------
# Operation isolation
# ---------------------------------------------------------------------------


def test_team_overview_selection_calls_only_the_team_flow(monkeypatch):
    team_calls, rankings_calls, injury_calls = [], [], []
    monkeypatch.setattr(controller_module, "run_team_lookup", lambda *a, **kw: (team_calls.append(1), _stub_ok())[1])
    monkeypatch.setattr(controller_module, "run_rankings_lookup", lambda *a, **kw: rankings_calls.append(1))
    monkeypatch.setattr(controller_module, "run_injury_lookup", lambda *a, **kw: injury_calls.append(1))
    at = _fresh_app()
    at.run()
    at.radio(key="team_lookup_operation").set_value("Team overview")
    at.text_input(key="team_lookup_query").set_value("Duke")
    at.button(key="team_lookup_submit").click().run()
    assert team_calls == [1]
    assert rankings_calls == []
    assert injury_calls == []


def test_rankings_selection_calls_only_the_rankings_flow(monkeypatch):
    team_calls, rankings_calls, injury_calls = [], [], []
    monkeypatch.setattr(controller_module, "run_team_lookup", lambda *a, **kw: team_calls.append(1))
    monkeypatch.setattr(controller_module, "run_rankings_lookup", lambda *a, **kw: (rankings_calls.append(1), _stub_ok())[1])
    monkeypatch.setattr(controller_module, "run_injury_lookup", lambda *a, **kw: injury_calls.append(1))
    at = _fresh_app()
    at.run()
    at.radio(key="team_lookup_operation").set_value("NET Ranking")
    at.text_input(key="team_lookup_query").set_value("Duke")
    at.button(key="team_lookup_submit").click().run()
    assert rankings_calls == [1]
    assert team_calls == []
    assert injury_calls == []


def test_injury_selection_calls_only_the_injury_flow(monkeypatch):
    team_calls, rankings_calls, injury_calls = [], [], []
    monkeypatch.setattr(controller_module, "run_team_lookup", lambda *a, **kw: team_calls.append(1))
    monkeypatch.setattr(controller_module, "run_rankings_lookup", lambda *a, **kw: rankings_calls.append(1))
    monkeypatch.setattr(controller_module, "run_injury_lookup", lambda *a, **kw: (injury_calls.append(1), _stub_ok())[1])
    at = _fresh_app()
    at.run()
    at.radio(key="team_lookup_operation").set_value("Injury report")
    at.text_input(key="team_lookup_query").set_value("Duke")
    at.button(key="team_lookup_submit").click().run()
    assert injury_calls == [1]
    assert team_calls == []
    assert rankings_calls == []


def test_service_is_called_exactly_once_per_submission(monkeypatch):
    calls = []
    monkeypatch.setattr(controller_module, "run_team_lookup", lambda *a, **kw: (calls.append(1), _stub_ok())[1])
    at = _fresh_app()
    at.run()
    at.text_input(key="team_lookup_query").set_value("Duke")
    at.button(key="team_lookup_submit").click().run()
    assert len(calls) == 1


def test_rerender_without_submit_does_not_repeat_service_work(monkeypatch):
    calls = []
    monkeypatch.setattr(controller_module, "run_team_lookup", lambda *a, **kw: (calls.append(1), _stub_ok())[1])
    at = _fresh_app()
    at.run()
    at.text_input(key="team_lookup_query").set_value("Duke")
    at.button(key="team_lookup_submit").click().run()
    assert len(calls) == 1
    at.radio(key="team_lookup_operation").set_value("NET Ranking")  # any subsequent rerun, no submit
    at.run()
    assert len(calls) == 1
    assert at.subheader[0].value == "Duke"  # stored result still rendered


# ---------------------------------------------------------------------------
# Session lifecycle (page level)
# ---------------------------------------------------------------------------


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


def test_session_factory_invoked_via_controller_not_directly_by_page(monkeypatch):
    """The page never constructs a Session itself -- it only ever passes `deps.session_factory` through to the controller, which is exactly what the controller-level tests already prove opens/closes exactly one session."""
    with open(PAGE_PATH, encoding="utf-8") as fh:
        source = fh.read()
    forbidden = ("Session(", "sessionmaker(", "build_engine(", "build_session_factory(")
    for token in forbidden:
        assert token not in source


# ---------------------------------------------------------------------------
# Team / Rankings / Injury outcomes via the page
# ---------------------------------------------------------------------------


def test_team_not_found_renders_via_page(monkeypatch):
    monkeypatch.setattr(
        controller_module,
        "run_team_lookup",
        lambda *a, **kw: ConsumerPresentation(status="not_found", title="Team Not Found", message="No team matched that lookup."),
    )
    at = _fresh_app()
    at.run()
    at.text_input(key="team_lookup_query").set_value("Zzz")
    at.button(key="team_lookup_submit").click().run()
    assert any("No team matched" in w.value for w in at.warning)


def test_ambiguous_identity_renders_via_page(monkeypatch):
    monkeypatch.setattr(
        controller_module,
        "run_team_lookup",
        lambda *a, **kw: ConsumerPresentation(status="conflict", title="Ambiguous Result", message="team query 'Miami' is ambiguous."),
    )
    at = _fresh_app()
    at.run()
    at.text_input(key="team_lookup_query").set_value("Miami")
    at.button(key="team_lookup_submit").click().run()
    assert any("ambiguous" in w.value for w in at.warning)


def test_database_unavailable_renders_via_page(monkeypatch):
    monkeypatch.setattr(
        controller_module,
        "run_team_lookup",
        lambda *a, **kw: ConsumerPresentation(status="error", title="Internal Error", message="An internal error occurred."),
    )
    at = _fresh_app()
    at.run()
    at.text_input(key="team_lookup_query").set_value("Duke")
    at.button(key="team_lookup_submit").click().run()
    assert any("An internal error occurred." in e.value for e in at.error)


def test_rankings_conflict_shows_all_rows_via_page(monkeypatch):
    rows = (
        (PresentationField("Raw Key", "louisiana-state"), PresentationField("NET Rank", "77")),
        (PresentationField("Raw Key", "louisiana-state"), PresentationField("NET Rank", "245")),
    )
    monkeypatch.setattr(
        controller_module,
        "run_rankings_lookup",
        lambda *a, **kw: ConsumerPresentation(status="conflict", title="Conflicting Ranking Data", rows=rows),
    )
    at = _fresh_app()
    at.run()
    at.radio(key="team_lookup_operation").set_value("NET Ranking")
    at.text_input(key="team_lookup_query").set_value("LSU")
    at.button(key="team_lookup_submit").click().run()
    table = at.dataframe[0].value
    assert len(table) == 2


def test_injury_unavailable_renders_via_page(monkeypatch):
    monkeypatch.setattr(
        controller_module,
        "run_injury_lookup",
        lambda *a, **kw: ConsumerPresentation(status="unavailable", title="Injury Report Unavailable", message="Injury data is temporarily unavailable for this team."),
    )
    at = _fresh_app()
    at.run()
    at.radio(key="team_lookup_operation").set_value("Injury report")
    at.text_input(key="team_lookup_query").set_value("Duke")
    at.button(key="team_lookup_submit").click().run()
    assert any("temporarily unavailable" in e.value for e in at.error)


# ---------------------------------------------------------------------------
# State behavior
# ---------------------------------------------------------------------------


def test_latest_presentation_is_stored_and_rerendered(monkeypatch):
    monkeypatch.setattr(controller_module, "run_team_lookup", _stub_ok)
    at = _fresh_app()
    at.run()
    at.text_input(key="team_lookup_query").set_value("Duke")
    at.button(key="team_lookup_submit").click().run()
    assert at.session_state["team_lookup_result"] is not None
    at.run()
    assert at.subheader[0].value == "Duke"


def test_clear_removes_stored_presentation(monkeypatch):
    monkeypatch.setattr(controller_module, "run_team_lookup", _stub_ok)
    at = _fresh_app()
    at.run()
    at.text_input(key="team_lookup_query").set_value("Duke")
    at.button(key="team_lookup_submit").click().run()
    assert at.session_state["team_lookup_result"] is not None
    at.button(key="team_lookup_clear").click().run()
    assert at.session_state["team_lookup_result"] is None
    assert len(at.subheader) == 0


def test_changing_input_without_submit_does_not_execute(monkeypatch):
    calls = []
    monkeypatch.setattr(controller_module, "run_team_lookup", lambda *a, **kw: (calls.append(1), _stub_ok())[1])
    at = _fresh_app()
    at.run()
    at.text_input(key="team_lookup_query").set_value("Duke").run()
    assert calls == []


def test_new_submission_replaces_stale_output(monkeypatch):
    presentations = [
        ConsumerPresentation(status="ok", title="Duke"),
        ConsumerPresentation(status="ok", title="UNC"),
    ]
    monkeypatch.setattr(controller_module, "run_team_lookup", lambda *a, **kw: presentations.pop(0))
    at = _fresh_app()
    at.run()
    at.text_input(key="team_lookup_query").set_value("Duke")
    at.button(key="team_lookup_submit").click().run()
    assert at.subheader[0].value == "Duke"
    at.text_input(key="team_lookup_query").set_value("UNC")
    at.button(key="team_lookup_submit").click().run()
    assert at.subheader[0].value == "UNC"


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


def test_page_imports_no_discord_fastapi_or_deferred_domain_module():
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
        "src.api", "src.discord_bot",
        "src.pipeline", "src.agents", "src.ovr_model", "src.features.matchup_features",
    )
    for name in imported:
        assert not any(name == f or name.startswith(f + ".") for f in forbidden), f"unexpected import {name!r}"


def test_page_writes_no_source_data():
    with open(PAGE_PATH, encoding="utf-8") as fh:
        source = fh.read()
    assert "open(" not in source
    assert ".write_text(" not in source
    assert "to_csv(" not in source


def test_page_accesses_no_notebook_file():
    with open(PAGE_PATH, encoding="utf-8") as fh:
        source = fh.read()
    assert ".ipynb" not in source
