"""
Regression tests for src/scraper.py's pure normalization helpers.

These functions were verified by manual source-diff against
cbb_teamScrape_v15_TEST.ipynb (cells 47, 301, 311, 312) on 2026-08-27 and
found logically identical (see docs/notebook_src_coverage.csv). These tests
lock in that verified behavior so a future edit to src/scraper.py can't
silently drift from the notebook without a test failing.
"""
from src.scraper import (
    canonicalize_team,
    covers_team_alias,
    filter_relevant_injuries,
    is_relevant_injury,
    normalize_covers_player,
    normalize_covers_status,
)


def test_canonicalize_team_strips_punctuation_and_lowercases():
    assert canonicalize_team("St. Bonaventure Bonnies") == "st bonaventure bonnies"
    assert canonicalize_team(None) == ""


def test_covers_team_alias_disambiguates_miami():
    assert covers_team_alias("Miami (OH) RedHawks") == "miami"
    assert covers_team_alias("Miami Florida Hurricanes") == "miami florida"
    assert covers_team_alias("Davidson Wildcats") == "davidson"


def test_normalize_covers_status_maps_known_variants():
    assert normalize_covers_status("Out - Injury") == "out"
    assert normalize_covers_status("Doubtful") == "doubtful"
    assert normalize_covers_status("Questionable - Undisclosed") == "questionable"
    assert normalize_covers_status("Probable") == "probable"
    assert normalize_covers_status("") == "active"
    assert normalize_covers_status("Healthy") == "active"


def test_normalize_covers_player_strips_suffixes_and_returns_last_name():
    assert normalize_covers_player("P. Ngongba II") == "ngongba"
    assert normalize_covers_player("A. Marshall") == "marshall"
    assert normalize_covers_player("J. Smith Jr.") == "smith"
    assert normalize_covers_player("") == ""


def test_is_relevant_injury_includes_questionable_and_probable():
    assert is_relevant_injury("out") is True
    assert is_relevant_injury("doubtful") is True
    assert is_relevant_injury("questionable") is True
    assert is_relevant_injury("probable") is True
    assert is_relevant_injury("active") is False


def test_filter_relevant_injuries_is_more_conservative_than_is_relevant_injury():
    injuries = [
        {"status": "Out"},
        {"status": "Doubtful"},
        {"status": "Questionable"},
        {"status": "Probable"},
        {"status": "Active"},
    ]
    filtered = filter_relevant_injuries(injuries)
    assert filtered == [{"status": "Out"}, {"status": "Doubtful"}]
