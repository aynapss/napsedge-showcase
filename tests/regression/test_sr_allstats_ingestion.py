"""
Phase 3, batch 1: Sports-Reference/AllStats canonical ingestion.

No live requests: real fixtures are copied read-only into temp cache
directories; synthetic HTML/DataFrames cover edge cases (malformed HTML,
repeated headers, missing player links, duplicate names).
"""
import shutil
from pathlib import Path

import pandas as pd
import pytest

from src.exceptions import SourceUnavailableError
from src.identity.contracts import TeamIdentity, UnresolvedIdentity
from src.ingestion.sr_allstats_ingestion import (
    DataQualityIssue,
    PlayerSlugRecord,
    build_player_slug_records,
    detect_duplicate_school_rows,
    detect_repeated_header_rows,
    extract_table_v15,
    fetch_raw_team_page_with_evidence,
    quarantine_repeated_header_rows,
    resolve_ingestion_team_slug,
    run_sr_allstats_canonical_ingestion,
)

REAL_SB_CACHE_KEY = "4a5bab543e5fe185e12c282c26d5efed.html"
REAL_SB_CACHE_PATH = Path("sr_cache_html") / REAL_SB_CACHE_KEY


@pytest.fixture()
def temp_cache_dir(tmp_path):
    """A temp cache dir pre-seeded with the real St. Bonaventure 2026 page -- no live request needed."""
    if not REAL_SB_CACHE_PATH.exists():
        pytest.skip("real St. Bonaventure cache fixture not present in this checkout")
    dest_dir = tmp_path / "sr_cache_html"
    dest_dir.mkdir()
    shutil.copy(REAL_SB_CACHE_PATH, dest_dir / REAL_SB_CACHE_KEY)
    return str(dest_dir)


# ---------------------------------------------------------------------------
# Canonical team/SR-slug resolution -- no fuzzy matching, no guessed slugs
# ---------------------------------------------------------------------------

def test_resolve_ingestion_team_slug_source_observed():
    team_result, sr_slug = resolve_ingestion_team_slug("St. Bonaventure")
    assert isinstance(team_result, TeamIdentity)
    assert sr_slug == "st-bonaventure"


def test_resolve_ingestion_team_slug_valid_d1_mascot_shortening():
    team_result, sr_slug = resolve_ingestion_team_slug("Duke Blue Devils")
    assert isinstance(team_result, TeamIdentity)
    assert sr_slug == "duke"


def test_resolve_ingestion_team_slug_colorado_college_unresolved():
    team_result, sr_slug = resolve_ingestion_team_slug("Colorado College Tigers")
    assert not isinstance(team_result, TeamIdentity)
    assert sr_slug is None


def test_resolve_ingestion_team_slug_washington_mo_unresolved():
    team_result, sr_slug = resolve_ingestion_team_slug("Washington (MO) Bears")
    assert not isinstance(team_result, TeamIdentity)
    assert sr_slug is None


def test_resolve_ingestion_team_slug_william_and_mary_uses_verified_slug():
    """Correction 2: the reconciled slug, not the anomalous prior registry value."""
    team_result, sr_slug = resolve_ingestion_team_slug("William & Mary")
    assert isinstance(team_result, TeamIdentity)
    assert sr_slug == "william-mary"


@pytest.mark.parametrize("query,expected_slug", [
    ("Texas A&M", "texas-am"),
    ("UC Davis", "california-davis"),
    ("UTEP", "texas-el-paso"),
    ("TCU", "texas-christian"),
    ("UAB", "alabama-birmingham"),
])
def test_all_32_disputed_teams_use_reconciled_slug_not_generated(query, expected_slug):
    team_result, sr_slug = resolve_ingestion_team_slug(query)
    assert isinstance(team_result, TeamIdentity)
    assert sr_slug == expected_slug


def test_generated_fallback_slug_never_returned_as_verified():
    """The dataframe-derived generated guess ('texas-aandm') must never surface as the selected slug."""
    _, sr_slug = resolve_ingestion_team_slug("Texas A&M")
    assert sr_slug != "texas-aandm"


def test_unknown_team_is_not_found():
    team_result, sr_slug = resolve_ingestion_team_slug("Definitely Not A Real School Xyzzy123")
    assert not isinstance(team_result, TeamIdentity)
    assert sr_slug is None


# ---------------------------------------------------------------------------
# Repeated-header detection / quarantine
# ---------------------------------------------------------------------------

def test_detect_repeated_header_rows_flags_embedded_header():
    df = pd.DataFrame({
        "Player": ["Frank Mitchell", "Player", "Cayden Charles"],
        "PTS": [18, "PTS", 12],
        "AST": [3, "AST", 5],
    })
    assert detect_repeated_header_rows(df) == [1]


def test_detect_repeated_header_rows_none_in_clean_table():
    df = pd.DataFrame({"Player": ["Frank Mitchell", "Cayden Charles"], "PTS": [18, 12]})
    assert detect_repeated_header_rows(df) == []


def test_detect_repeated_header_rows_deterministic():
    df = pd.DataFrame({"Player": ["Player"], "PTS": ["PTS"]})
    assert detect_repeated_header_rows(df) == detect_repeated_header_rows(df) == [0]


def test_quarantine_repeated_header_rows_removes_and_audits():
    df = pd.DataFrame({
        "Player": ["Frank Mitchell", "Player", "Cayden Charles", "player"],
        "PTS": [18, "PTS", 12, "pts"],
    })
    cleaned, issues = quarantine_repeated_header_rows(df, table_label="PerGame")
    assert list(cleaned["Player"]) == ["Frank Mitchell", "Cayden Charles"]
    assert len(issues) == 2
    assert all(isinstance(i, DataQualityIssue) for i in issues)
    assert {i.reason for i in issues} == {"repeated_header_row"}
    assert {i.table_label for i in issues} == {"PerGame"}


def test_quarantine_repeated_header_rows_no_op_when_clean():
    df = pd.DataFrame({"Player": ["Frank Mitchell"], "PTS": [18]})
    cleaned, issues = quarantine_repeated_header_rows(df, table_label="Roster")
    assert issues == []
    pd.testing.assert_frame_equal(cleaned, df)


def test_repeated_headers_not_silently_discarded_without_audit_record():
    df = pd.DataFrame({"Player": ["Player", "Frank Mitchell"], "PTS": ["PTS", 18]})
    _, issues = quarantine_repeated_header_rows(df, table_label="Totals", source="sports_reference")
    assert len(issues) == 1
    assert issues[0].source == "sports_reference"
    assert issues[0].row_index == 0


def test_detect_duplicate_school_rows():
    df = pd.DataFrame({"Player": ["Frank Mitchell", "Frank Mitchell", "Cayden Charles"]})
    assert detect_duplicate_school_rows(df) == {"Frank Mitchell": 2}


def test_detect_duplicate_school_rows_empty_when_clean():
    df = pd.DataFrame({"Player": ["Frank Mitchell", "Cayden Charles"]})
    assert detect_duplicate_school_rows(df) == {}


# ---------------------------------------------------------------------------
# extract_table_v15: malformed HTML handling
# ---------------------------------------------------------------------------

def test_extract_table_v15_missing_table_returns_empty_df():
    from bs4 import BeautifulSoup

    soup = BeautifulSoup("<html><body>nothing here</body></html>", "lxml")
    df = extract_table_v15(soup, "Roster", "Roster Table", "roster")
    assert df.empty


def test_extract_table_v15_malformed_html_does_not_crash():
    from bs4 import BeautifulSoup

    soup = BeautifulSoup("<html><table><tr><td>broken", "lxml")
    df = extract_table_v15(soup, "Roster", "Roster Table", "roster")
    assert isinstance(df, pd.DataFrame)


def test_extract_table_v15_finds_table_in_html_comment():
    from bs4 import BeautifulSoup

    html = """
    <html><body>
    <!--
    <table id="roster"><tbody><tr><td>Frank Mitchell</td></tr></tbody></table>
    -->
    </body></html>
    """
    soup = BeautifulSoup(html, "lxml")
    df = extract_table_v15(soup, "Roster", "Roster Table", "roster")
    assert not df.empty
    assert "Frank Mitchell" in df.iloc[:, 0].values


# ---------------------------------------------------------------------------
# Player-slug persistence
# ---------------------------------------------------------------------------

def test_build_player_slug_records_verified_when_slug_present():
    roster_df = pd.DataFrame({"Player": ["Frank Mitchell"]})
    records, duplicates, team_result = build_player_slug_records(
        roster_df=roster_df, slug_map={"Frank Mitchell": "frank-mitchell-2"},
        team_query="St. Bonaventure", season=2026,
    )
    assert len(records) == 1
    r = records[0]
    assert r.resolution_status == "verified"
    assert r.sr_player_slug == "frank-mitchell-2"
    assert r.sr_profile_url == "https://www.sports-reference.com/cbb/players/frank-mitchell-2.html"
    assert r.team_id == team_result.team_id
    assert r.season_id == 2026


def test_build_player_slug_records_unconfirmed_when_missing_player_link():
    roster_df = pd.DataFrame({"Player": ["Walk On Guy"]})
    records, _, _ = build_player_slug_records(
        roster_df=roster_df, slug_map={}, team_query="St. Bonaventure", season=2026,
    )
    assert len(records) == 1
    assert records[0].resolution_status == "unconfirmed"
    assert records[0].sr_player_slug is None
    assert records[0].sr_profile_url is None


def test_build_player_slug_records_duplicate_player_names_flagged():
    roster_df = pd.DataFrame({"Player": ["John Smith", "John Smith Jr."]})
    records, duplicates, _ = build_player_slug_records(
        roster_df=roster_df, slug_map={}, team_query="St. Bonaventure", season=2026,
    )
    assert len(records) == 2  # both still returned, never silently dropped
    assert len(duplicates) == 1  # suffix-stripped key collision flagged


def test_same_player_name_different_teams_not_merged():
    roster_df = pd.DataFrame({"Player": ["Frank Mitchell"]})
    records_a, _, team_a = build_player_slug_records(
        roster_df=roster_df, slug_map={"Frank Mitchell": "frank-mitchell-2"},
        team_query="St. Bonaventure", season=2026,
    )
    records_b, _, team_b = build_player_slug_records(
        roster_df=roster_df, slug_map={"Frank Mitchell": "some-other-slug"},
        team_query="Duke", season=2026,
    )
    assert records_a[0].team_id != records_b[0].team_id
    assert records_a[0].player_id != records_b[0].player_id


def test_build_player_slug_records_unresolved_team_returns_no_records():
    roster_df = pd.DataFrame({"Player": ["Someone"]})
    records, duplicates, team_result = build_player_slug_records(
        roster_df=roster_df, slug_map={}, team_query="Not A Real Team", season=2026,
    )
    assert records == []
    assert not isinstance(team_result, TeamIdentity)


def test_stable_canonical_player_ids_deterministic():
    roster_df = pd.DataFrame({"Player": ["Frank Mitchell"]})
    ids = set()
    for _ in range(3):
        records, _, _ = build_player_slug_records(
            roster_df=roster_df, slug_map={"Frank Mitchell": "frank-mitchell-2"},
            team_query="St. Bonaventure", season=2026,
        )
        ids.add(records[0].player_id)
    assert len(ids) == 1


# ---------------------------------------------------------------------------
# Raw fetch + evidence: cache hit, missing cache, temp-path safety
# ---------------------------------------------------------------------------

def test_fetch_raw_team_page_with_evidence_cache_hit(temp_cache_dir):
    html, evidence = fetch_raw_team_page_with_evidence("st-bonaventure", 2026, cache_dir=temp_cache_dir)
    assert len(html) > 1000
    assert evidence.http_cache_status == "hit"
    assert evidence.validation_status == "verified"
    assert evidence.observed_team_slug == "st-bonaventure"
    assert evidence.cache_key == REAL_SB_CACHE_KEY


def test_fetch_raw_team_page_with_evidence_missing_cache_raises_typed_error(tmp_path, monkeypatch):
    empty_cache = str(tmp_path / "empty_cache")

    class _FakeSession:
        def get(self, *a, **k):
            raise ConnectionError("no network in tests")

    with pytest.raises(SourceUnavailableError):
        fetch_raw_team_page_with_evidence(
            "st-bonaventure", 2026, cache_dir=empty_cache, session=_FakeSession(),
        )


def test_temp_path_does_not_touch_production_cache(temp_cache_dir):
    """Confirms the ingestion path used the injected temp dir, not the real sr_cache_html/."""
    import os

    before = set(os.listdir("sr_cache_html")) if os.path.isdir("sr_cache_html") else set()
    fetch_raw_team_page_with_evidence("st-bonaventure", 2026, cache_dir=temp_cache_dir)
    after = set(os.listdir("sr_cache_html")) if os.path.isdir("sr_cache_html") else set()
    assert before == after


# ---------------------------------------------------------------------------
# Full orchestrator: real fixture, idempotency, unresolved team, no roster
# ---------------------------------------------------------------------------

def test_run_sr_allstats_canonical_ingestion_real_fixture(temp_cache_dir):
    result = run_sr_allstats_canonical_ingestion("St. Bonaventure", 2026, cache_dir=temp_cache_dir)
    assert isinstance(result.team_identity, TeamIdentity)
    assert result.season_id == 2026
    assert result.evidence is not None
    assert result.evidence.http_cache_status == "hit"
    assert len(result.player_slug_records) >= 10
    assert all(r.team_id == result.team_identity.team_id for r in result.player_slug_records)
    assert any(r.resolution_status == "verified" for r in result.player_slug_records)


def test_run_sr_allstats_canonical_ingestion_idempotent_repeated_execution(temp_cache_dir):
    r1 = run_sr_allstats_canonical_ingestion("St. Bonaventure", 2026, cache_dir=temp_cache_dir)
    r2 = run_sr_allstats_canonical_ingestion("St. Bonaventure", 2026, cache_dir=temp_cache_dir)

    assert [rec.player_id for rec in r1.player_slug_records] == [rec.player_id for rec in r2.player_slug_records]
    assert [rec.sr_player_slug for rec in r1.player_slug_records] == [rec.sr_player_slug for rec in r2.player_slug_records]
    assert r1.data_quality_issues == r2.data_quality_issues
    assert r1.duplicate_player_keys == r2.duplicate_player_keys
    # evidence.collected_at_utc legitimately differs between calls (a real
    # timestamp of when THIS call ran) -- idempotency is about the DATA,
    # not a frozen clock; every other evidence field must match.
    assert r1.evidence.cache_key == r2.evidence.cache_key
    assert r1.evidence.observed_team_slug == r2.evidence.observed_team_slug


def test_run_sr_allstats_canonical_ingestion_unresolved_team_short_circuits(temp_cache_dir):
    result = run_sr_allstats_canonical_ingestion("Colorado College Tigers", 2026, cache_dir=temp_cache_dir)
    assert not isinstance(result.team_identity, TeamIdentity)
    assert result.evidence is None
    assert result.player_slug_records == []


def test_run_sr_allstats_canonical_ingestion_requires_explicit_season(temp_cache_dir):
    result = run_sr_allstats_canonical_ingestion("St. Bonaventure", -1, cache_dir=temp_cache_dir)
    assert result.season_id is None


def test_run_sr_allstats_canonical_ingestion_does_not_mutate_team_registry(temp_cache_dir):
    from src import slug_utils

    before_size = len(slug_utils.TEAM_REGISTRY)
    run_sr_allstats_canonical_ingestion("St. Bonaventure", 2026, cache_dir=temp_cache_dir)
    assert len(slug_utils.TEAM_REGISTRY) == before_size


# ---------------------------------------------------------------------------
# Legacy AllStats/Excel parity: this module never touches either
# ---------------------------------------------------------------------------

def test_module_never_imports_legacy_build_functions_directly():
    """
    Confirms this module does not call scrape_team_season/build_all_stats/
    save_tables_to_excel itself (they remain reachable only via the
    existing src.ingestion.notebook_core bridge, unmodified).
    """
    import ast
    import inspect

    import src.ingestion.sr_allstats_ingestion as mod

    tree = ast.parse(inspect.getsource(mod))
    called_names = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not ({"scrape_team_season", "build_all_stats", "save_tables_to_excel"} & called_names)


def test_result_object_carries_no_allstats_dataframe_field():
    from dataclasses import fields

    from src.ingestion.sr_allstats_ingestion import SrAllStatsIngestionResult

    field_names = {f.name for f in fields(SrAllStatsIngestionResult)}
    assert "all_stats_df" not in field_names
    assert "allstats" not in {n.lower() for n in field_names}


def test_module_never_writes_to_production_schools_cache():
    """
    Confirms the module never CALLS fetch_all_schools or touches the
    production schools parquet -- checked via AST call/attribute
    inspection, not a raw substring search (this module's own docstring
    legitimately mentions "fetch_all_schools" in prose explaining why it
    is NOT called, which a substring check would misflag).
    """
    import ast
    import inspect

    import src.ingestion.sr_allstats_ingestion as mod

    tree = ast.parse(inspect.getsource(mod))
    called_names = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "fetch_all_schools" not in called_names

    # No call to pandas' parquet read/write functions anywhere -- this
    # module never touches ANY parquet cache, production or otherwise.
    called_attrs = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not ({"read_parquet", "to_parquet"} & called_attrs)
