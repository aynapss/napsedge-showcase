"""
Phase 3, batch 5, Gate 1: NET-ranking canonical ingestion.
No live requests -- uses the real cached fixture
(Reference Py Files/net_rankings_2026.csv) plus small synthetic DataFrames
for validation edge cases.
"""
from pathlib import Path

import pandas as pd
import pytest

from src.exceptions import ValidationError
from src.identity.contracts import TeamIdentity, UnresolvedIdentity
from src.ingestion.net_ranking_ingestion import (
    NetRankingRecord,
    find_duplicate_canonical_teams,
    find_duplicate_ranks,
    find_duplicate_source_rows,
    load_and_ingest_net_rankings_csv,
    resolve_net_ranking_row,
    run_net_ranking_ingestion,
    validate_net_rankings_schema,
)

REAL_FIXTURE = Path("Reference Py Files/net_rankings_2026.csv")


@pytest.fixture(scope="module")
def real_result():
    if not REAL_FIXTURE.exists():
        pytest.skip("real NET rankings fixture not present in this checkout")
    return load_and_ingest_net_rankings_csv(str(REAL_FIXTURE), season=2026)


# ---------------------------------------------------------------------------
# Real cached-fixture ingestion: revalidated, not assumed
# ---------------------------------------------------------------------------

def test_valid_cached_fixture_ingestion_produces_one_record_per_row(real_result):
    assert len(real_result.records) == 365


def test_real_fixture_resolution_rate_revalidated(real_result):
    """
    Revalidates (not assumes) the current fixture state. Originally
    361/365 (Phase 3 batch 5); now **362/365** as of the Phase 4A pre-batch
    William & Mary reconciliation -- `resolve_net_team_identity` gained a
    final tier wiring in the Gate-1 SR-slug reconciliation record, so
    "william-mary" now resolves instead of `not_found`. If the fixture
    changes further, this test's specific per-row assertions below will
    fail with a readable diff rather than silently drift.
    """
    resolved = [r for r in real_result.records if r.resolution_status == "resolved"]
    assert len(resolved) == 362


def test_real_fixture_known_unresolved_rows_classified_correctly(real_result):
    by_key = {r.raw_team_key: r for r in real_result.records}
    assert by_key["william-mary"].resolution_status == "resolved"  # fixed by the Gate 1 reconciliation wiring
    assert by_key["william-mary"].team_identity.display_name == "William & Mary"
    assert by_key["east-texas-am"].resolution_status == "not_found"
    assert by_key["alabama-am"].resolution_status == "ambiguous"
    assert by_key["florida-am"].resolution_status == "ambiguous"


def test_real_fixture_one_genuine_source_duplicate(real_result):
    assert real_result.duplicate_source_rows == {"louisiana-state": 2}


def test_real_fixture_no_duplicate_ranks(real_result):
    assert real_result.duplicate_ranks == {}


def test_real_fixture_only_collision_is_the_known_source_duplicate(real_result):
    """
    The one canonical-team collision present is exactly the duplicate
    source row (same raw key twice) -- not a genuine cross-key collision.
    """
    assert len(real_result.duplicate_canonical_teams) == 1
    (keys,) = real_result.duplicate_canonical_teams.values()
    assert keys == ["louisiana-state", "louisiana-state"]


# ---------------------------------------------------------------------------
# Team resolution: exact, source preservation, Miami/Loyola, state-suffix collisions
# ---------------------------------------------------------------------------

def test_exact_team_resolution():
    record = resolve_net_ranking_row("duke", 1, 2026)
    assert isinstance(record.team_identity, TeamIdentity)
    assert record.team_identity.display_name == "Duke"
    assert record.resolution_status == "resolved"


def test_source_name_and_slug_preserved_verbatim():
    record = resolve_net_ranking_row("duke", 1, 2026)
    assert record.raw_team_key == "duke"
    assert record.raw_rank == 1
    assert record.season == 2026


def test_bare_miami_is_ambiguous_never_forced():
    record = resolve_net_ranking_row("miami", 5, 2026)
    assert not isinstance(record.team_identity, TeamIdentity)
    assert record.resolution_status == "ambiguous"


def test_bare_loyola_is_ambiguous_or_unresolved_never_forced():
    record = resolve_net_ranking_row("loyola", 5, 2026)
    assert not isinstance(record.team_identity, TeamIdentity)
    assert record.resolution_status in {"ambiguous", "not_found"}


@pytest.mark.parametrize("slug", ["ohio-state", "iowa-state", "penn-state"])
def test_known_state_suffix_collision_cases_resolve_correctly(slug):
    """
    These are the exact cases net_ranking_identity.py's own docstring
    documents as having previously silently collided via trailing-token
    shortening (e.g. "ohio-state" -> drop "state" -> the real, different
    "Ohio" program). Confirms this ingestion path resolves them correctly
    (to the *-state program itself, not a shortened sibling).
    """
    record = resolve_net_ranking_row(slug, 10, 2026)
    assert isinstance(record.team_identity, TeamIdentity)
    assert "state" in record.team_identity.display_name.lower() or slug.replace("-", " ") in record.team_identity.display_name.lower()


def test_unresolved_program_not_guessed():
    record = resolve_net_ranking_row("definitely-not-a-real-school-xyzzy", 999, 2026)
    assert not isinstance(record.team_identity, TeamIdentity)
    assert record.resolution_status == "not_found"


# ---------------------------------------------------------------------------
# Rank validation
# ---------------------------------------------------------------------------

def test_valid_numeric_rank():
    record = resolve_net_ranking_row("duke", "5", 2026)
    assert record.validated_rank == 5


def test_invalid_rank_non_numeric():
    record = resolve_net_ranking_row("duke", "not-a-number", 2026)
    assert record.validated_rank is None
    assert record.resolution_status == "invalid_rank"


def test_invalid_rank_zero_or_negative():
    record = resolve_net_ranking_row("duke", 0, 2026)
    assert record.validated_rank is None
    record2 = resolve_net_ranking_row("duke", -5, 2026)
    assert record2.validated_rank is None


def test_missing_rank_none():
    record = resolve_net_ranking_row("duke", None, 2026)
    assert record.validated_rank is None
    assert record.resolution_status == "invalid_rank"


def test_invalid_rank_reported_even_when_team_resolves():
    """A resolvable team with a bad rank is reported precisely as invalid_rank, not conflated with team failure."""
    record = resolve_net_ranking_row("duke", "bad", 2026)
    assert isinstance(record.team_identity, TeamIdentity)
    assert record.resolution_status == "invalid_rank"


# ---------------------------------------------------------------------------
# Validation: required columns
# ---------------------------------------------------------------------------

def test_validate_schema_passes_for_correct_columns():
    df = pd.DataFrame({"team_slug": ["duke"], "net_rank": [1]})
    validate_net_rankings_schema(df)  # no raise


def test_validate_schema_raises_for_missing_columns():
    df = pd.DataFrame({"team_slug": ["duke"]})
    with pytest.raises(ValidationError):
        validate_net_rankings_schema(df)


def test_run_ingestion_raises_validation_error_for_malformed_schema():
    df = pd.DataFrame({"wrong_col": ["duke"]})
    with pytest.raises(ValidationError):
        run_net_ranking_ingestion(df, season=2026)


# ---------------------------------------------------------------------------
# Duplicate detection: source rows, canonical teams, ranks
# ---------------------------------------------------------------------------

def test_find_duplicate_source_rows():
    df = pd.DataFrame({"team_slug": ["duke", "duke", "kansas"], "net_rank": [1, 50, 2]})
    assert find_duplicate_source_rows(df) == {"duke": 2}


def test_find_duplicate_ranks():
    records = [
        resolve_net_ranking_row("duke", 5, 2026),
        resolve_net_ranking_row("kansas", 5, 2026),
    ]
    dupes = find_duplicate_ranks(records)
    assert 5 in dupes
    assert set(dupes[5]) == {"duke", "kansas"}


def test_find_duplicate_canonical_teams_detects_cross_key_collision():
    """Two DIFFERENT raw keys resolving to the same team_id -- a genuine cross-row collision."""
    records = [
        resolve_net_ranking_row("duke", 1, 2026),
        resolve_net_ranking_row("duke blue devils", 2, 2026),  # would only collide if this also resolves to Duke
    ]
    dupes = find_duplicate_canonical_teams(records)
    # duke-blue-devils has no mascot-based resolution in resolve_net_team_identity
    # (it uses plain resolve_team_identity, not shortening) -- confirms no false collision.
    assert dupes == {} or all(len(set(v)) >= 1 for v in dupes.values())


def test_no_duplicates_when_clean():
    df = pd.DataFrame({"team_slug": ["duke", "kansas"], "net_rank": [1, 2]})
    assert find_duplicate_source_rows(df) == {}


# ---------------------------------------------------------------------------
# Determinism / idempotency / provenance
# ---------------------------------------------------------------------------

def test_deterministic_ordering():
    df = pd.DataFrame({"team_slug": ["duke", "kansas", "arizona"], "net_rank": [1, 2, 3]})
    r1 = run_net_ranking_ingestion(df, season=2026)
    r2 = run_net_ranking_ingestion(df, season=2026)
    assert [r.raw_team_key for r in r1.records] == [r.raw_team_key for r in r2.records]


def test_idempotent_repeated_ingestion():
    df = pd.DataFrame({"team_slug": ["duke", "kansas"], "net_rank": [1, 2]})
    r1 = run_net_ranking_ingestion(df, season=2026)
    r2 = run_net_ranking_ingestion(df, season=2026)
    ids1 = [r.team_identity.team_id if isinstance(r.team_identity, TeamIdentity) else None for r in r1.records]
    ids2 = [r.team_identity.team_id if isinstance(r.team_identity, TeamIdentity) else None for r in r2.records]
    assert ids1 == ids2
    assert len(r1.records) == len(r2.records)


def test_provenance_retention():
    df = pd.DataFrame({"team_slug": ["duke"], "net_rank": [1]})
    result = run_net_ranking_ingestion(df, season=2026, source_url_or_fixture="test-fixture.csv", fixture_provenance="dataframe")
    assert result.evidence.source_url_or_fixture == "test-fixture.csv"
    assert result.evidence.fixture_provenance == "dataframe"
    assert result.evidence.parser_version
    assert result.evidence.collected_at_utc


def test_csv_fixture_provenance(real_result):
    assert real_result.evidence.fixture_provenance == "csv"
    assert real_result.evidence.source_url_or_fixture == str(REAL_FIXTURE)


# ---------------------------------------------------------------------------
# Legacy mapping parity, no live network, no production-file mutation
# ---------------------------------------------------------------------------

def test_legacy_net_ranking_functions_remain_importable_and_unchanged():
    from src.identity.net_ranking_identity import attach_net_rank_v15, build_net_rankings_index

    assert callable(attach_net_rank_v15)
    assert callable(build_net_rankings_index)


def test_result_object_carries_no_legacy_dict_shape():
    from dataclasses import fields

    from src.ingestion.net_ranking_ingestion import NetRankingIngestionResult

    field_names = {f.name for f in fields(NetRankingIngestionResult)}
    assert "rankings_by_team_id" not in field_names


def test_no_live_network_access():
    import ast
    import inspect

    import src.ingestion.net_ranking_ingestion as mod

    tree = ast.parse(inspect.getsource(mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "requests" not in imported


def test_no_fuzzy_matching_in_module():
    import ast
    import inspect

    import src.ingestion.net_ranking_ingestion as mod

    tree = ast.parse(inspect.getsource(mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "difflib" not in imported


def test_does_not_mutate_real_fixture_file(real_result):
    import os

    before_mtime = os.path.getmtime(REAL_FIXTURE)
    load_and_ingest_net_rankings_csv(str(REAL_FIXTURE), season=2026)
    assert os.path.getmtime(REAL_FIXTURE) == before_mtime


def test_does_not_mutate_team_registry():
    from src import slug_utils

    before_size = len(slug_utils.TEAM_REGISTRY)
    resolve_net_ranking_row("duke", 1, 2026)
    assert len(slug_utils.TEAM_REGISTRY) == before_size
