"""
Phase 7, Slice 2B: tests for `src.streamlit_consumer_renderer`. Uses
`streamlit.testing.v1.AppTest` against tiny, dynamically-written fixture
scripts (each one constructs a synthetic `ConsumerPresentation` and
calls `render_consumer_presentation` on it) -- no service call, no
network, no real page.
"""
from __future__ import annotations

from streamlit.testing.v1 import AppTest

_FIXTURE_HEADER = """
from src.consumers.contracts import ConsumerPresentation, PresentationField
from src.streamlit_consumer_renderer import render_consumer_presentation
"""


def _run(tmp_path, body: str) -> AppTest:
    script = tmp_path / "fixture_page.py"
    script.write_text(_FIXTURE_HEADER + body, encoding="utf-8")
    at = AppTest.from_file(str(script))
    at.run()
    assert not at.exception
    return at


def test_title_rendered_as_subheader(tmp_path):
    at = _run(tmp_path, "render_consumer_presentation(ConsumerPresentation(status='ok', title='Duke'))")
    assert at.subheader[0].value == "Duke"


def test_field_ordering_is_preserved_exactly(tmp_path):
    body = """
presentation = ConsumerPresentation(
    status="ok",
    title="Duke",
    fields=(
        PresentationField("Team", "Duke"),
        PresentationField("Slug", "duke"),
        PresentationField("Conference", "ACC"),
    ),
)
render_consumer_presentation(presentation)
"""
    at = _run(tmp_path, body)
    values = [t.value for t in at.text]
    assert values == ["Team: Duke", "Slug: duke", "Conference: ACC"]


def test_missing_field_value_renders_as_unavailable_not_blank(tmp_path):
    body = """
render_consumer_presentation(ConsumerPresentation(status="ok", title="Duke", fields=(PresentationField("Conference", None),)))
"""
    at = _run(tmp_path, body)
    assert at.text[0].value == "Conference: (unavailable)"


def test_not_found_status_renders_as_warning_with_message(tmp_path):
    body = """
render_consumer_presentation(ConsumerPresentation(status="not_found", title="Team Not Found", message="No team matched that lookup."))
"""
    at = _run(tmp_path, body)
    assert at.warning[0].value == "No team matched that lookup."


def test_unavailable_status_renders_as_error_with_message(tmp_path):
    body = """
render_consumer_presentation(ConsumerPresentation(status="unavailable", title="Rankings Unavailable", message="Rankings data is temporarily unavailable right now."))
"""
    at = _run(tmp_path, body)
    assert at.error[0].value == "Rankings data is temporarily unavailable right now."


def test_conflict_rows_are_all_preserved_via_dataframe(tmp_path):
    body = """
rows = (
    (PresentationField("Raw Key", "louisiana-state"), PresentationField("NET Rank", "77")),
    (PresentationField("Raw Key", "louisiana-state"), PresentationField("NET Rank", "245")),
)
render_consumer_presentation(ConsumerPresentation(status="conflict", title="Conflicting Ranking Data", rows=rows))
"""
    at = _run(tmp_path, body)
    table = at.dataframe[0].value
    assert len(table) == 2
    assert set(table["NET Rank"]) == {"77", "245"}


def test_table_row_ordering_is_preserved(tmp_path):
    body = """
rows = tuple((PresentationField("Player", f"Player {i}"),) for i in range(5))
render_consumer_presentation(ConsumerPresentation(status="ok", title="Injuries", rows=rows))
"""
    at = _run(tmp_path, body)
    table = at.dataframe[0].value
    assert list(table["Player"]) == [f"Player {i}" for i in range(5)]


def test_warnings_are_displayed(tmp_path):
    body = """
render_consumer_presentation(ConsumerPresentation(status="conflict", title="X", warnings=("Multiple conflicting rank records exist.",)))
"""
    at = _run(tmp_path, body)
    assert "Multiple conflicting rank records exist." in [w.value for w in at.warning]


def test_notes_are_displayed_as_captions(tmp_path):
    body = """
render_consumer_presentation(ConsumerPresentation(status="ok", title="X", notes=("Rankings reflect the locally cached snapshot only.",)))
"""
    at = _run(tmp_path, body)
    assert "Rankings reflect the locally cached snapshot only." in [c.value for c in at.caption]


def test_summary_is_rendered_as_plain_text(tmp_path):
    body = """
render_consumer_presentation(ConsumerPresentation(status="ok", title="Injury Report", summary="No injuries currently reported."))
"""
    at = _run(tmp_path, body)
    assert at.text[0].value == "No injuries currently reported."


def test_no_unsafe_html_is_ever_used(tmp_path):
    import inspect

    import src.streamlit_consumer_renderer as renderer_module

    source = inspect.getsource(renderer_module)
    assert "unsafe_allow_html=" not in source


def test_field_value_with_markdown_like_content_is_not_interpreted(tmp_path):
    """A player/status/detail value that happens to contain markdown syntax must render as literal text, never as formatted output -- confirmed here by checking the raw stored value is untouched."""
    body = """
render_consumer_presentation(ConsumerPresentation(status="ok", title="Injuries", fields=(PresentationField("Detail", "**bold** $$not_math$$ <script>alert(1)</script>"),)))
"""
    at = _run(tmp_path, body)
    assert at.text[0].value == "Detail: **bold** $$not_math$$ <script>alert(1)</script>"


def test_no_internal_field_leaks_beyond_what_the_presentation_carries(tmp_path):
    body = """
render_consumer_presentation(ConsumerPresentation(status="ok", title="Duke", fields=(PresentationField("Team", "Duke"),)))
"""
    at = _run(tmp_path, body)
    rendered_text = "\n".join(t.value for t in at.text)
    assert "source_path" not in rendered_text
    assert "raw" not in rendered_text.lower()


def test_rendering_is_deterministic_across_repeated_runs(tmp_path):
    body = """
render_consumer_presentation(ConsumerPresentation(status="ok", title="Duke", fields=(PresentationField("Team", "Duke"),)))
"""
    at1 = _run(tmp_path, body)
    script2 = tmp_path / "fixture_page2.py"
    script2.write_text(_FIXTURE_HEADER + body, encoding="utf-8")
    at2 = AppTest.from_file(str(script2))
    at2.run()
    assert [t.value for t in at1.text] == [t.value for t in at2.text]
    assert at1.subheader[0].value == at2.subheader[0].value


def test_player_record_table_renders_all_columns_via_dataframe(tmp_path):
    """Confirms the existing, unmodified generic renderer already handles a wide Player raw-statistics row (Phase 7 Slice 3A required zero renderer code changes)."""
    body = """
row = (PresentationField("stat_table", "PerGame"), PresentationField("pts", "16.1"), PresentationField("validation_status", "valid"))
render_consumer_presentation(ConsumerPresentation(status="ok", title="Test Player", rows=(row,)))
"""
    at = _run(tmp_path, body)
    table = at.dataframe[0].value
    assert list(table.columns) == ["stat_table", "pts", "validation_status"]
    assert table["validation_status"][0] == "valid"


def test_player_known_empty_result_is_distinguishable_from_not_found(tmp_path):
    empty_body = """
render_consumer_presentation(ConsumerPresentation(status="ok", title="Test Player", summary="No statistics available for this player/season."))
"""
    not_found_body = """
render_consumer_presentation(ConsumerPresentation(status="not_found", title="Player Not Found", message="No player matched that lookup."))
"""
    empty = _run(tmp_path, empty_body)
    assert len(empty.warning) == 0 and len(empty.error) == 0

    script2 = tmp_path / "fixture_page2.py"
    script2.write_text(_FIXTURE_HEADER + not_found_body, encoding="utf-8")
    not_found = AppTest.from_file(str(script2))
    not_found.run()
    assert len(not_found.warning) == 1


def test_player_sanitized_failure_shows_generic_error_only(tmp_path):
    body = """
render_consumer_presentation(ConsumerPresentation(status="error", title="Internal Error", message="An internal error occurred."))
"""
    at = _run(tmp_path, body)
    assert at.error[0].value == "An internal error occurred."


def test_player_none_value_renders_as_unavailable_and_zero_is_preserved(tmp_path):
    body = """
row = (PresentationField("fg_pct", None), PresentationField("games_played", "0"))
render_consumer_presentation(ConsumerPresentation(status="ok", title="Test Player", rows=(row,)))
"""
    at = _run(tmp_path, body)
    table = at.dataframe[0].value
    assert table["fg_pct"][0] == "(unavailable)"
    assert table["games_played"][0] == "0"


def test_matchup_comparison_table_renders_all_four_columns_via_dataframe(tmp_path):
    """Confirms the existing, unmodified generic renderer already handles the Matchup feature-comparison table (Phase 7 Slice 3C required zero renderer code changes)."""
    body = """
row = (
    PresentationField("Feature", "season scoring margin"),
    PresentationField("Team A value", "10.0"),
    PresentationField("Team B value", "3.0"),
    PresentationField("Difference", "7.0"),
)
render_consumer_presentation(ConsumerPresentation(status="ok", title="duke vs unc", fields=(PresentationField("Team A", "duke"), PresentationField("Team B", "unc")), rows=(row,)))
"""
    at = _run(tmp_path, body)
    table = at.dataframe[0].value
    assert list(table.columns) == ["Feature", "Team A value", "Team B value", "Difference"]
    assert table["Difference"][0] == "7.0"
    assert [t.value for t in at.text] == ["Team A: duke", "Team B: unc"]


def test_matchup_signed_difference_displays_negative_values_unmodified(tmp_path):
    body = """
row = (PresentationField("Feature", "offensive impact"), PresentationField("Team A value", "-8.75"), PresentationField("Team B value", "0.0"), PresentationField("Difference", "-8.75"))
render_consumer_presentation(ConsumerPresentation(status="ok", title="duke vs unc", rows=(row,)))
"""
    at = _run(tmp_path, body)
    table = at.dataframe[0].value
    assert table["Difference"][0] == "-8.75"
    assert table["Team B value"][0] == "0.0"


def test_matchup_none_value_renders_as_unavailable_and_zero_is_preserved(tmp_path):
    body = """
row = (PresentationField("Feature", "recent scoring margin"), PresentationField("Team A value", None), PresentationField("Team B value", "0.0"), PresentationField("Difference", None))
render_consumer_presentation(ConsumerPresentation(status="ok", title="duke vs unc", rows=(row,)))
"""
    at = _run(tmp_path, body)
    table = at.dataframe[0].value
    assert table["Team A value"][0] == "(unavailable)"
    assert table["Difference"][0] == "(unavailable)"
    assert table["Team B value"][0] == "0.0"


def test_matchup_rows_are_not_reordered_by_the_renderer(tmp_path):
    body = """
rows = tuple(
    (PresentationField("Feature", label), PresentationField("Team A value", None), PresentationField("Team B value", None), PresentationField("Difference", None))
    for label in ("season scoring margin", "recent scoring margin", "season net rating")
)
render_consumer_presentation(ConsumerPresentation(status="ok", title="duke vs unc", rows=rows))
"""
    at = _run(tmp_path, body)
    table = at.dataframe[0].value
    assert list(table["Feature"]) == ["season scoring margin", "recent scoring margin", "season net rating"]


def test_module_has_no_service_or_framework_import_beyond_streamlit():
    import ast
    import inspect

    import src.streamlit_consumer_renderer as renderer_module

    tree = ast.parse(inspect.getsource(renderer_module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = ("discord", "fastapi", "starlette", "pydantic", "src.services", "src.discord_bot", "src.api")
    for name in imported:
        assert not any(name == f or name.startswith(f + ".") for f in forbidden), f"unexpected import {name!r}"
