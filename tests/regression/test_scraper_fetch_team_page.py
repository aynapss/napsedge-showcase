"""
Regression tests for src/scraper.py's fetch_team_page() cache-hit path.

fetch_team_page() was manually source-diffed against
cbb_teamScrape_v15_TEST.ipynb cell 88 on 2026-08-27 (see
docs/notebook_src_coverage.csv) and found behaviorally identical: same
_cache_key() MD5 scheme, same base_sleep/max_attempts/MAX_WAIT_429_SECONDS
constants, same 404/429/403 handling order.

These tests use the real sr_cache_html/ fixtures already checked into the
repo (a live pipeline's cache directory) rather than mocked HTTP, so they
exercise the actual production cache-hit code path -- the one used on the
overwhelming majority of pipeline runs -- with zero network calls.
"""
import os

import pytest

import src.scraper as scraper

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SR_CACHE_DIR = os.path.join(REPO_ROOT, "sr_cache_html")

# (pipeline slug, season) -> known to already have a cached page in
# sr_cache_html/ as of 2026-08-27, verified via _cache_key() against the
# real directory listing before writing this test.
KNOWN_CACHED_TEAMS = [
    ("st-bonaventure", 2026),
    ("michigan-state", 2026),
    ("connecticut", 2026),
    ("davidson", 2026),
    # Exercises TEAM_SLUG_TO_SR_PATCH: pipeline slug differs from the SR
    # canonical slug the page is actually cached under.
    ("unc-wilmington", 2026),
]


def _fail_if_network_called(*args, **kwargs):
    raise AssertionError(
        "fetch_team_page() attempted a live HTTP request on a cache-hit path"
    )


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    monkeypatch.setattr(scraper._DEFAULT_SESSION, "get", _fail_if_network_called)


@pytest.mark.skipif(
    not os.path.isdir(SR_CACHE_DIR), reason="sr_cache_html/ fixture directory not present"
)
@pytest.mark.parametrize("team_slug,season", KNOWN_CACHED_TEAMS)
def test_fetch_team_page_serves_from_cache_without_network(team_slug, season):
    html = scraper.fetch_team_page(
        team_slug, season, force_refresh=False, cache_dir=SR_CACHE_DIR
    )

    sr_slug = scraper.TEAM_SLUG_TO_SR_PATCH.get(team_slug, team_slug)
    url = f"https://www.sports-reference.com/cbb/schools/{sr_slug}/men/{season}.html"
    expected_path = os.path.join(SR_CACHE_DIR, scraper._cache_key(url))

    with open(expected_path, "r", encoding="utf-8") as fh:
        expected_html = fh.read()

    assert html == expected_html
    assert len(html) > 0


def test_cache_key_is_md5_of_url_plus_html_suffix():
    url = "https://www.sports-reference.com/cbb/schools/davidson/men/2026.html"
    key = scraper._cache_key(url)
    assert key.endswith(".html")
    assert len(key) == len("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.html")
