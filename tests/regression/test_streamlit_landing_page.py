"""
Phase 9B/9C remediation: `app.py` must never render a link to a page file
that is absent. A deployment image ships only the tracked pages 10-12, and
`st.page_link` to a missing page raises StreamlitPageNotFoundError, which
renders a traceback (including server paths) to every visitor.

Each test copies `app.py` into a temp directory with a controlled `pages/`
folder, so the result never depends on which untracked legacy pages happen
to exist in the working tree running the tests.
"""
from __future__ import annotations

import pathlib
import shutil

import pytest
from streamlit.testing.v1 import AppTest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_ALL_PAGES = (
    "01_Team_Explorer.py",
    "02_Bet_Tracker.py",
    "03_Historical_Results.py",
    "04_Player_Cards.py",
    "05_Team_Overview.py",
    "06_Conference_Dashboard.py",
    "07_Matchup_Graphics.py",
    "08_Pipeline_Architecture.py",
    "09_AI_Research_Control_Room.py",
    "10_Team_Lookup.py",
    "11_Player_Raw_Statistics.py",
    "12_Matchup_Feature_Comparison.py",
)
_ACCEPTED_PAGES = _ALL_PAGES[-3:]


def _run_app(tmp_path: pathlib.Path, present_pages: tuple[str, ...]) -> AppTest:
    shutil.copy(_REPO_ROOT / "app.py", tmp_path / "app.py")
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    for name in present_pages:
        (pages_dir / name).write_text("import streamlit as st\nst.write('stub')\n", encoding="utf-8")
    at = AppTest.from_file(str(tmp_path / "app.py"), default_timeout=30)
    at.run()
    return at


def _link_labels(at: AppTest) -> list[str]:
    return [element.proto.label for element in at.get("page_link")]


def _all_text(at: AppTest) -> str:
    return " ".join(m.value for m in at.markdown) + " " + " ".join(s.value for s in at.subheader)


def test_accepted_pages_only_renders_cleanly_with_no_exception(tmp_path: pathlib.Path) -> None:
    at = _run_app(tmp_path, _ACCEPTED_PAGES)
    assert not at.exception, [e.value for e in at.exception]


def test_accepted_pages_only_links_exactly_the_three_present_pages(tmp_path: pathlib.Path) -> None:
    at = _run_app(tmp_path, _ACCEPTED_PAGES)
    assert _link_labels(at) == [
        "Team / NET Ranking / Injury Lookup",
        "Player Raw Statistics",
        "Matchup Feature Comparison",
    ]


def test_accepted_pages_only_never_mentions_an_absent_page(tmp_path: pathlib.Path) -> None:
    at = _run_app(tmp_path, _ACCEPTED_PAGES)
    text = _all_text(at)
    for absent_phrase in ("Team Explorer", "Bet Tracker", "Player Cards", "AI Research Control Room", "Visual Pages"):
        assert absent_phrase not in text, absent_phrase


def test_accepted_pages_only_never_leaks_a_traceback_or_server_path(tmp_path: pathlib.Path) -> None:
    at = _run_app(tmp_path, _ACCEPTED_PAGES)
    rendered = _all_text(at) + " ".join(str(e.value) for e in at.exception)
    for leak in ("Traceback", "site-packages", "StreamlitPageNotFoundError", "/app/", str(tmp_path)):
        assert leak not in rendered, leak


def test_working_now_lists_only_present_pages(tmp_path: pathlib.Path) -> None:
    at = _run_app(tmp_path, _ACCEPTED_PAGES)
    text = _all_text(at)
    assert "Team / NET Ranking / Injury lookup" in text
    assert "Team data browser" not in text
    assert "AI research control room" not in text


def test_coming_later_section_always_renders(tmp_path: pathlib.Path) -> None:
    at = _run_app(tmp_path, ())
    assert "Coming later" in _all_text(at)


def test_no_pages_present_renders_cleanly_without_empty_sections(tmp_path: pathlib.Path) -> None:
    at = _run_app(tmp_path, ())
    assert not at.exception, [e.value for e in at.exception]
    assert _link_labels(at) == []
    text = _all_text(at)
    assert "Data Pages" not in text
    assert "Visual Pages" not in text
    assert "Working now" not in text


def test_all_pages_present_links_every_page_in_the_original_order(tmp_path: pathlib.Path) -> None:
    """With every page present the output must match the original unconditional list exactly."""
    at = _run_app(tmp_path, _ALL_PAGES)
    assert not at.exception, [e.value for e in at.exception]
    assert _link_labels(at) == [
        "Team Explorer",
        "Bet Tracker",
        "Historical Results",
        "Team / NET Ranking / Injury Lookup",
        "Player Raw Statistics",
        "Matchup Feature Comparison",
        "Player Cards",
        "Team Overview",
        "Conference Dashboard",
        "Team-v-Team Graphics",
        "Pipeline Architecture",
        "AI Research Control Room",
    ]
    text = _all_text(at)
    assert "Data Pages" in text and "Visual Pages" in text and "Working now" in text


def test_link_visibility_follows_the_filesystem_per_page(tmp_path: pathlib.Path) -> None:
    at = _run_app(tmp_path, ("10_Team_Lookup.py", "01_Team_Explorer.py"))
    assert not at.exception, [e.value for e in at.exception]
    assert _link_labels(at) == ["Team Explorer", "Team / NET Ranking / Injury Lookup"]


def test_app_source_still_does_not_reference_src_consumers() -> None:
    """Preserves the existing isolation guarantee (tests/regression/test_consumers_integration_isolation.py) for this file."""
    assert "src.consumers" not in (_REPO_ROOT / "app.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", _ACCEPTED_PAGES)
def test_accepted_pages_are_tracked_in_git_so_the_clean_image_has_them(name: str) -> None:
    import subprocess

    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", f"pages/{name}"],
        cwd=_REPO_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"pages/{name} is not tracked: {result.stderr}"
