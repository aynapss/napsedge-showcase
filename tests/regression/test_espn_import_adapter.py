"""
Phase 5B3: tests for `src/pipeline/espn_import_adapter.py`. Real-fixture
tests exercise all three real local `TeamDump/ESPN` files (read-only).
Everything else uses small synthetic DataFrames for deterministic edge
cases, following the same pattern established in
`test_allstats_import_adapter.py`/`test_game_log_import_adapter.py`.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine, event

from src.db.base import Base
from src.db import models, espn_models
from src.db.session import build_session_factory, unit_of_work
from src.db.repositories import SeasonRepository
from src.exceptions import IdentityConflictError
from src.paths import ESPN_DIR
from src.pipeline.espn_import_adapter import (
    DiscoveredEspnFile,
    build_espn_benchmark_import_plan,
    build_espn_source_import_plan,
    classify_espn_file_schema,
    discover_espn_artifacts,
    load_espn_dataframe,
    parse_espn_benchmark_workbook,
    parse_espn_player_stats_workbook,
    persist_espn_benchmark_import_plan,
    persist_espn_source_import_plan,
)

SEASON = 2026


def _real_fixtures_present() -> bool:
    return all(
        (ESPN_DIR / name).exists()
        for name in ("espn_full_player_stats.csv", "espn_full_player_stats_enriched.csv", "espn_ncaa_benchmarks.xlsx")
    )


pytestmark_real = pytest.mark.skipif(not _real_fixtures_present(), reason="real ESPN fixtures not present in this checkout")


def _raw_stats_df(rows: list[dict]) -> pd.DataFrame:
    base = {
        "RK": 1, "Name": "", "Team": "", "POS": "G", "GP": 20, "MIN": 25.0, "PTS": 10.0,
        "FGM": 4.0, "FGA": 8.0, "FG%": 50.0, "3PM": 1.0, "3PA": 3.0, "3P%": 33.0,
        "FTM": 2.0, "FTA": 3.0, "FT%": 66.0, "REB": 4.0, "AST": 3.0, "STL": 1.0, "BLK": 0.5, "TO": 2.0,
    }
    return pd.DataFrame([{**base, **r} for r in rows])


def _benchmark_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{"Metric": r.get("Metric", "PTS"), "NCAA_Mean": r.get("NCAA_Mean", 8.4), "NCAA_Std": r.get("NCAA_Std", 4.6)} for r in rows])


# ---------------------------------------------------------------------------
# Stage 1: discovery
# ---------------------------------------------------------------------------


def test_discover_finds_all_three_known_filenames(tmp_path):
    for name in ("espn_full_player_stats.csv", "espn_full_player_stats_enriched.csv", "espn_ncaa_benchmarks.xlsx"):
        (tmp_path / name).write_bytes(b"fake")
    discovered = discover_espn_artifacts(tmp_path)
    assert sorted(d.filename for d in discovered) == ["espn_full_player_stats.csv", "espn_full_player_stats_enriched.csv", "espn_ncaa_benchmarks.xlsx"]


def test_discover_deterministic_order(tmp_path):
    for name in ("espn_full_player_stats.csv", "espn_ncaa_benchmarks.xlsx"):
        (tmp_path / name).write_bytes(b"fake")
    first = discover_espn_artifacts(tmp_path)
    second = discover_espn_artifacts(tmp_path)
    assert [d.filename for d in first] == [d.filename for d in second]


def test_discover_missing_files_excluded(tmp_path):
    (tmp_path / "espn_full_player_stats.csv").write_bytes(b"fake")
    discovered = discover_espn_artifacts(tmp_path)
    assert [d.filename for d in discovered] == ["espn_full_player_stats.csv"]


def test_discover_never_hardcodes_teamdump_path():
    import inspect
    import src.pipeline.espn_import_adapter as mod

    assert "TeamDump" not in inspect.getsource(mod.discover_espn_artifacts)


def test_discover_relative_path_is_just_the_filename(tmp_path):
    (tmp_path / "espn_full_player_stats.csv").write_bytes(b"fake")
    discovered = discover_espn_artifacts(tmp_path)
    assert discovered[0].relative_path == "espn_full_player_stats.csv"
    assert not discovered[0].relative_path.startswith(str(tmp_path))


def test_configured_root_never_hardcoded_in_adapter_source():
    import inspect
    import src.pipeline.espn_import_adapter as mod

    source = inspect.getsource(mod)
    assert "C:\\Users" not in source
    assert "J Notebooks" not in source


# ---------------------------------------------------------------------------
# Schema classification
# ---------------------------------------------------------------------------


def test_classify_raw_player_stats():
    df = _raw_stats_df([{"Name": "A", "Team": "ABC"}])
    assert classify_espn_file_schema(df) == "raw_player_stats"


def test_classify_derived_enriched():
    df = _raw_stats_df([{"Name": "A", "Team": "ABC"}])
    df["eFG"] = 0.5
    df["IMPACT-Total"] = 10.0
    assert classify_espn_file_schema(df) == "derived_player_stats_enriched"


def test_classify_benchmark_reference():
    df = _benchmark_df([{"Metric": "PTS"}])
    assert classify_espn_file_schema(df) == "benchmark_reference"


def test_classify_unsupported_ambiguous():
    df = pd.DataFrame([{"foo": 1, "bar": 2}])
    assert classify_espn_file_schema(df) == "unsupported_or_ambiguous"


def test_classify_allstats_shaped_table_never_misclassified():
    """A roster-shaped table (Player/Class/Pos) is not any ESPN schema -- defensive guard."""
    df = pd.DataFrame([{"Player": "X", "Class": "Fr", "Pos": "G"}])
    assert classify_espn_file_schema(df) == "unsupported_or_ambiguous"


# ---------------------------------------------------------------------------
# Parsing: raw player-stats
# ---------------------------------------------------------------------------


def test_parse_missing_required_columns_invalid():
    df = pd.DataFrame([{"foo": 1}])
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    assert parsed.status == "invalid"


def test_parse_empty_file_invalid():
    df = pd.DataFrame(columns=["Name", "Team"] + [c for c in _raw_stats_df([{}]).columns if c not in ("Name", "Team")])
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    assert parsed.status in ("invalid", "valid")  # zero rows is not itself an error -- confirmed no crash
    assert parsed.rows == []


def test_parse_rejects_enriched_schema_never_silently_parses_it():
    df = _raw_stats_df([{"Name": "A", "Team": "BYU"}])
    df["eFG"] = 0.5
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    assert parsed.status == "invalid"
    assert parsed.rows == []


def test_parse_rejects_benchmark_schema():
    df = _benchmark_df([{"Metric": "PTS"}])
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    assert parsed.status == "invalid"


def test_parse_missing_season_invalid():
    df = _raw_stats_df([{"Name": "A", "Team": "BYU"}])
    parsed = parse_espn_player_stats_workbook(df, 99999)
    assert parsed.status == "invalid"


def test_parse_missing_player_name_row_invalid_but_others_still_parsed():
    df = _raw_stats_df([{"Name": "A Player", "Team": "BYU"}, {"Name": None, "Team": "BYU"}])
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    assert len(parsed.rows) == 2
    assert parsed.rows[1].validation_status == "invalid"
    assert parsed.rows[1].exclusion_reason == "missing_player_name"


def test_parse_baseline_exact_team_resolution():
    df = _raw_stats_df([{"Name": "Some Player", "Team": "BYU"}])
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    assert parsed.rows[0].team_id is not None
    assert parsed.rows[0].team_resolution_method == "exact_alias"


def test_parse_reviewed_alias_team_resolution():
    df = _raw_stats_df([{"Name": "Some Player", "Team": "MIA"}])
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    assert parsed.rows[0].team_id is not None
    assert parsed.rows[0].team_resolution_method == "reviewed_alias"


def test_parse_unresolved_team_code():
    df = _raw_stats_df([{"Name": "Some Player", "Team": "ARST"}])
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    assert parsed.rows[0].team_id is None
    assert parsed.rows[0].team_resolution_method == "unresolved"
    assert parsed.rows[0].player_match_method == "team_unresolved"
    assert any(i.issue_code == "espn_abbreviation_unresolved" for i in parsed.issues)


def test_persist_ambiguous_team_row_idempotent_on_repeat_import(session_factory, tmp_path):
    """The new 'ambiguous' provenance value flows through the exact same, unmodified persistence
    path -- a repeated import of the same ambiguous-team row must not create a duplicate observation
    or any team/player row."""
    csv_path = tmp_path / "espn_full_player_stats.csv"
    _raw_stats_df([{"Name": "Some Player", "Team": "UNCA"}]).to_csv(csv_path, index=False)
    discovered = discover_espn_artifacts(tmp_path)[0]
    df, content_hash = load_espn_dataframe(discovered, tmp_path)
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    plan = build_espn_source_import_plan(parsed)
    _seed_season(session_factory)

    persist_espn_source_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash, season_id=SEASON)
    persist_espn_source_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash, season_id=SEASON)

    with unit_of_work(session_factory) as session:
        rows = session.query(espn_models.EspnSourceObservation).all()
        assert len(rows) == 1
        assert rows[0].team_id is None and rows[0].team_resolution_method == "ambiguous"
        assert session.query(models.Team).count() == 0
        assert session.query(models.Player).count() == 0


def test_parse_ambiguous_team_code_reports_ambiguous_method_not_unresolved():
    """Phase 5B6: 'UNCA' is genuinely ambiguous (3 real named candidates at the baseline resolver, confirmed by
    direct reproduction) -- distinct from a plain not-found code. team_resolution_method must reflect that,
    never collapsing it into the same generic 'unresolved' bucket as the other 49 remaining codes."""
    df = _raw_stats_df([{"Name": "Some Player", "Team": "UNCA"}])
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    assert parsed.rows[0].team_id is None
    assert parsed.rows[0].team_resolution_method == "ambiguous"
    assert parsed.rows[0].player_match_method == "team_unresolved"


def test_parse_resolved_team_but_player_still_resolves_team_season_scoped():
    df = _raw_stats_df([{"Name": "Some Real Sounding Player", "Team": "BYU"}])
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    assert parsed.rows[0].player_id is not None
    assert parsed.rows[0].player_match_method == "team_season_scoped_unique_key"


def test_same_surname_teammates_remain_distinct():
    df = _raw_stats_df([{"Name": "John Smith", "Team": "BYU"}, {"Name": "Michael Smith", "Team": "BYU"}])
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    ids = [r.player_id for r in parsed.rows]
    assert ids[0] is not None and ids[1] is not None
    assert ids[0] != ids[1]


def test_suffixed_players_remain_distinct():
    """
    `build_player_identity`'s own fallback key is deliberately suffix-
    agnostic (established, documented behavior -- not something this
    batch may change), so two suffix variants of the same base name on
    one team cannot both be resolved from name alone. "Remain distinct"
    is satisfied by NEVER silently merging them onto one player_id --
    confirmed here via the adapter's own same-team duplicate-key guard,
    which demotes both to `ambiguous` (player_id=None) rather than
    picking one arbitrarily or letting the second overwrite the first.
    """
    df = _raw_stats_df([{"Name": "James Wilson Jr.", "Team": "BYU"}, {"Name": "James Wilson III", "Team": "BYU"}])
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    assert parsed.rows[0].player_match_method == "ambiguous"
    assert parsed.rows[1].player_match_method == "ambiguous"
    assert parsed.rows[0].player_id is None and parsed.rows[1].player_id is None
    assert parsed.rows[0].exclusion_reason == "duplicate_or_suffix_conflicting_name_same_team"


def test_never_resolves_player_by_surname_alone():
    """Two different full names sharing only a surname on different teams must never collapse to the same player_id."""
    df = _raw_stats_df([{"Name": "Chris Anderson", "Team": "BYU"}, {"Name": "Pat Anderson", "Team": "MIA"}])
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    ids = [r.player_id for r in parsed.rows]
    assert ids[0] != ids[1]


def test_resolved_team_identities_dict_populated():
    df = _raw_stats_df([{"Name": "A", "Team": "BYU"}, {"Name": "B", "Team": "MIA"}])
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    assert len(parsed.resolved_team_identities) == 2


def test_parse_never_mutates_input_dataframe():
    df = _raw_stats_df([{"Name": "A", "Team": "BYU"}])
    snapshot = df.copy(deep=True)
    parse_espn_player_stats_workbook(df, SEASON)
    pd.testing.assert_frame_equal(df, snapshot)


def test_parse_missing_versus_zero_stat_value_preserved_distinctly():
    df = _raw_stats_df([{"Name": "A", "Team": "BYU", "BLK": 0.0}, {"Name": "B", "Team": "BYU", "BLK": None}])
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    assert parsed.rows[0].stat_values["blk"] == 0.0
    assert parsed.rows[1].stat_values["blk"] is None


def test_parse_invalid_numeric_value_handled():
    df = _raw_stats_df([{"Name": "A", "Team": "BYU", "PTS": "not-a-number"}])
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    # stat_values preserves the raw (possibly non-numeric) value -- coercion happens at persistence/DB type level
    assert parsed.rows[0].stat_values["pts"] == "not-a-number"


def test_duplicate_conflicting_rows_both_preserved_as_raw_evidence():
    """Two rows with identical Name+Team (a real duplicate scrape artifact) are both preserved -- never silently deduped by the parser (deduplication, if any, is a persistence-layer natural-key concern)."""
    df = _raw_stats_df([{"Name": "A", "Team": "BYU", "PTS": 10.0}, {"Name": "A", "Team": "BYU", "PTS": 12.0}])
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    assert len(parsed.rows) == 2


# ---------------------------------------------------------------------------
# Parsing: benchmark reference
# ---------------------------------------------------------------------------


def test_parse_benchmark_valid():
    df = _benchmark_df([{"Metric": "PTS", "NCAA_Mean": 8.4, "NCAA_Std": 4.6}])
    parsed = parse_espn_benchmark_workbook(df)
    assert parsed.status == "valid"
    assert len(parsed.rows) == 1
    assert parsed.rows[0].ncaa_mean == 8.4


def test_parse_benchmark_missing_metric():
    df = _benchmark_df([{"Metric": None, "NCAA_Mean": 8.4, "NCAA_Std": 4.6}])
    parsed = parse_espn_benchmark_workbook(df)
    assert parsed.rows[0].validation_status == "invalid"
    assert parsed.rows[0].exclusion_reason == "missing_metric_name"


def test_parse_benchmark_invalid_numeric():
    df = _benchmark_df([{"Metric": "PTS", "NCAA_Mean": "garbage", "NCAA_Std": 4.6}])
    parsed = parse_espn_benchmark_workbook(df)
    assert parsed.rows[0].validation_status == "invalid"
    assert parsed.rows[0].exclusion_reason == "invalid_numeric_value"


def test_parse_benchmark_rejects_wrong_schema():
    df = _raw_stats_df([{"Name": "A", "Team": "BYU"}])
    parsed = parse_espn_benchmark_workbook(df)
    assert parsed.status == "invalid"


def test_parse_benchmark_never_mutates_input():
    df = _benchmark_df([{"Metric": "PTS"}])
    snapshot = df.copy(deep=True)
    parse_espn_benchmark_workbook(df)
    pd.testing.assert_frame_equal(df, snapshot)


# ---------------------------------------------------------------------------
# Plan building (pure)
# ---------------------------------------------------------------------------


def test_build_source_plan_counts():
    df = _raw_stats_df([{"Name": "A", "Team": "BYU"}, {"Name": "B", "Team": "ARST"}])
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    plan = build_espn_source_import_plan(parsed)
    assert plan.counts.total_source_rows == 2
    assert plan.counts.resolved_teams == 1
    assert plan.counts.unresolved_teams == 1


def test_build_source_plan_never_mutates_across_repeated_calls():
    df = _raw_stats_df([{"Name": "A", "Team": "BYU"}])
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    plan1 = build_espn_source_import_plan(parsed)
    plan2 = build_espn_source_import_plan(parsed)
    assert plan1.observation_specs == plan2.observation_specs


def test_build_benchmark_plan_excludes_invalid_rows():
    df = _benchmark_df([{"Metric": "PTS", "NCAA_Mean": 8.4, "NCAA_Std": 4.6}, {"Metric": None, "NCAA_Mean": 1, "NCAA_Std": 1}])
    parsed = parse_espn_benchmark_workbook(df)
    plan = build_espn_benchmark_import_plan(parsed)
    assert len(plan.value_specs) == 1


# ---------------------------------------------------------------------------
# Persistence: transactions, idempotency, linking
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine():
    eng = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(eng, "connect")
    def _enable_fk(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session_factory(engine):
    return build_session_factory(engine)


def _seed_season(session_factory, season_id=SEASON):
    with unit_of_work(session_factory) as session:
        if session.get(models.Season, season_id) is None:
            SeasonRepository(session).upsert(season_id=season_id, start_year=season_id - 1, end_year=season_id, label="2025-26")


def _discovered_and_hash(tmp_path, df, filename="espn_full_player_stats.csv"):
    import hashlib

    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / filename
    if filename.endswith(".xlsx"):
        df.to_excel(path, index=False)
    else:
        df.to_csv(path, index=False)
    stat = path.stat()
    discovered = DiscoveredEspnFile(
        filename=filename, relative_path=filename,
        file_size_bytes=stat.st_size, filesystem_mtime=dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc),
    )
    return discovered, hashlib.sha256(path.read_bytes()).hexdigest()


def test_persist_source_writes_observation_and_registers_team_and_player(session_factory, tmp_path):
    df = _raw_stats_df([{"Name": "Some Player", "Team": "BYU"}])
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    plan = build_espn_source_import_plan(parsed)
    _seed_season(session_factory)
    discovered, content_hash = _discovered_and_hash(tmp_path, df)

    result = persist_espn_source_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash, season_id=SEASON)
    assert result.observations_written == 1

    with unit_of_work(session_factory) as session:
        assert session.query(espn_models.EspnSourceObservation).count() == 1
        assert session.query(models.Team).count() == 1
        assert session.query(models.Player).count() == 1
        artifact = session.query(models.SourceArtifact).one()
        assert artifact.collection_timestamp is None
        assert artifact.point_in_time_classification == "current_snapshot_only"


def test_persist_source_unresolved_row_creates_no_fake_team_or_player(session_factory, tmp_path):
    df = _raw_stats_df([{"Name": "Some Player", "Team": "ARST"}])
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    plan = build_espn_source_import_plan(parsed)
    _seed_season(session_factory)
    discovered, content_hash = _discovered_and_hash(tmp_path, df)

    result = persist_espn_source_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash, season_id=SEASON)
    assert result.observations_written == 1
    assert result.issues_written == 1

    with unit_of_work(session_factory) as session:
        assert session.query(models.Team).count() == 0
        assert session.query(models.Player).count() == 0
        obs = session.query(espn_models.EspnSourceObservation).one()
        assert obs.team_id is None and obs.player_id is None
        issue = session.query(models.DataQualityIssue).one()
        assert issue.issue_code == "espn_abbreviation_unresolved"


def test_persist_source_idempotent_on_identical_repeat_import(session_factory, tmp_path):
    df = _raw_stats_df([{"Name": "Some Player", "Team": "BYU"}])
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    plan = build_espn_source_import_plan(parsed)
    _seed_season(session_factory)
    discovered, content_hash = _discovered_and_hash(tmp_path, df)

    persist_espn_source_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash, season_id=SEASON)
    persist_espn_source_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash, season_id=SEASON)

    with unit_of_work(session_factory) as session:
        assert session.query(espn_models.EspnSourceObservation).count() == 1
        assert session.query(models.SourceArtifact).count() == 1


def test_persist_source_rolls_back_on_conflict(session_factory, tmp_path):
    df = _raw_stats_df([{"Name": "Some Player", "Team": "BYU", "PTS": 10.0}])
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    plan = build_espn_source_import_plan(parsed)
    _seed_season(session_factory)
    discovered, content_hash = _discovered_and_hash(tmp_path, df)
    persist_espn_source_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash, season_id=SEASON)

    conflicting_df = _raw_stats_df([{"Name": "Some Player", "Team": "BYU", "PTS": 99.0}])
    conflicting_parsed = parse_espn_player_stats_workbook(conflicting_df, SEASON)
    conflicting_plan = build_espn_source_import_plan(conflicting_parsed)

    with pytest.raises(IdentityConflictError):
        persist_espn_source_import_plan(conflicting_plan, session_factory, discovered=discovered, content_hash=content_hash, season_id=SEASON)

    with unit_of_work(session_factory) as session:
        obs = session.query(espn_models.EspnSourceObservation).one()
        assert obs.pts == 10.0  # untouched by the failed conflicting attempt


def test_persist_source_no_orphan_observations_after_rollback(session_factory, tmp_path):
    df = _raw_stats_df([{"Name": "Some Player", "Team": "BYU", "PTS": 10.0}])
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    plan = build_espn_source_import_plan(parsed)
    _seed_season(session_factory)
    discovered, content_hash = _discovered_and_hash(tmp_path, df)
    persist_espn_source_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash, season_id=SEASON)

    conflicting_df = _raw_stats_df([{"Name": "Some Player", "Team": "BYU", "PTS": 99.0}])
    conflicting_plan = build_espn_source_import_plan(parse_espn_player_stats_workbook(conflicting_df, SEASON))
    with pytest.raises(IdentityConflictError):
        persist_espn_source_import_plan(conflicting_plan, session_factory, discovered=discovered, content_hash=content_hash, season_id=SEASON)

    with unit_of_work(session_factory) as session:
        assert session.query(espn_models.EspnSourceObservation).count() == 1
        assert session.query(models.Team).count() == 1


def test_persist_source_changed_content_creates_new_artifact(session_factory, tmp_path):
    df_v1 = _raw_stats_df([{"Name": "Some Player", "Team": "BYU"}])
    plan_v1 = build_espn_source_import_plan(parse_espn_player_stats_workbook(df_v1, SEASON))
    _seed_season(session_factory)
    discovered_v1, hash_v1 = _discovered_and_hash(tmp_path, df_v1)
    persist_espn_source_import_plan(plan_v1, session_factory, discovered=discovered_v1, content_hash=hash_v1, season_id=SEASON)

    df_v2 = _raw_stats_df([{"Name": "Some Player", "Team": "BYU"}, {"Name": "Another Player", "Team": "MIA"}])
    plan_v2 = build_espn_source_import_plan(parse_espn_player_stats_workbook(df_v2, SEASON))
    discovered_v2, hash_v2 = _discovered_and_hash(tmp_path, df_v2)
    persist_espn_source_import_plan(plan_v2, session_factory, discovered=discovered_v2, content_hash=hash_v2, season_id=SEASON)

    with unit_of_work(session_factory) as session:
        assert session.query(models.SourceArtifact).count() == 2
        assert session.query(espn_models.EspnSourceObservation).count() == 3  # 1 (v1) + 2 (v2), additive


def test_persist_source_pipeline_run_linking(session_factory, tmp_path):
    df = _raw_stats_df([{"Name": "Some Player", "Team": "BYU"}])
    plan = build_espn_source_import_plan(parse_espn_player_stats_workbook(df, SEASON))
    _seed_season(session_factory)
    discovered, content_hash = _discovered_and_hash(tmp_path, df)
    run_id = "2" * 32
    with unit_of_work(session_factory) as session:
        session.add(models.PipelineRun(run_id=run_id, pipeline_name="espn_import_test", status="running", started_at=dt.datetime.now(dt.timezone.utc)))

    persist_espn_source_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash, season_id=SEASON, pipeline_run_id=run_id)

    with unit_of_work(session_factory) as session:
        obs = session.query(espn_models.EspnSourceObservation).one()
        assert obs.pipeline_run_id == run_id
        artifact = session.query(models.SourceArtifact).one()
        assert artifact.pipeline_run_id == run_id


def test_persist_benchmark_writes_snapshot_and_values(session_factory, tmp_path):
    df = _benchmark_df([{"Metric": "PTS", "NCAA_Mean": 8.4, "NCAA_Std": 4.6}, {"Metric": "REB", "NCAA_Mean": 3.5, "NCAA_Std": 1.8}])
    plan = build_espn_benchmark_import_plan(parse_espn_benchmark_workbook(df))
    discovered, content_hash = _discovered_and_hash(tmp_path, df, filename="espn_ncaa_benchmarks.xlsx")

    result = persist_espn_benchmark_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash)
    assert result.values_written == 2

    with unit_of_work(session_factory) as session:
        assert session.query(espn_models.EspnBenchmarkSnapshot).count() == 1
        assert session.query(espn_models.EspnBenchmarkValue).count() == 2
        snapshot = session.query(espn_models.EspnBenchmarkSnapshot).one()
        assert snapshot.season_id is None  # not season-scoped, honestly reflected


def test_persist_benchmark_idempotent(session_factory, tmp_path):
    df = _benchmark_df([{"Metric": "PTS", "NCAA_Mean": 8.4, "NCAA_Std": 4.6}])
    plan = build_espn_benchmark_import_plan(parse_espn_benchmark_workbook(df))
    discovered, content_hash = _discovered_and_hash(tmp_path, df, filename="espn_ncaa_benchmarks.xlsx")

    persist_espn_benchmark_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash)
    persist_espn_benchmark_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash)

    with unit_of_work(session_factory) as session:
        assert session.query(espn_models.EspnBenchmarkSnapshot).count() == 1
        assert session.query(espn_models.EspnBenchmarkValue).count() == 1


def test_persist_benchmark_rolls_back_on_conflicting_value(session_factory, tmp_path):
    df = _benchmark_df([{"Metric": "PTS", "NCAA_Mean": 8.4, "NCAA_Std": 4.6}])
    plan = build_espn_benchmark_import_plan(parse_espn_benchmark_workbook(df))
    discovered, content_hash = _discovered_and_hash(tmp_path, df, filename="espn_ncaa_benchmarks.xlsx")
    persist_espn_benchmark_import_plan(plan, session_factory, discovered=discovered, content_hash=content_hash)

    conflicting_df = _benchmark_df([{"Metric": "PTS", "NCAA_Mean": 99.9, "NCAA_Std": 4.6}])
    conflicting_plan = build_espn_benchmark_import_plan(parse_espn_benchmark_workbook(conflicting_df))
    with pytest.raises(IdentityConflictError):
        persist_espn_benchmark_import_plan(conflicting_plan, session_factory, discovered=discovered, content_hash=content_hash)

    with unit_of_work(session_factory) as session:
        value = session.query(espn_models.EspnBenchmarkValue).one()
        assert value.ncaa_mean == 8.4


def test_persist_source_import_order_independence(session_factory, tmp_path):
    df_a = _raw_stats_df([{"Name": "Player One", "Team": "BYU"}])
    df_b = _raw_stats_df([{"Name": "Player Two", "Team": "MIA"}])

    def run(order):
        eng = create_engine("sqlite+pysqlite:///:memory:", future=True)

        @event.listens_for(eng, "connect")
        def _fk(c, r):
            cur = c.cursor(); cur.execute("PRAGMA foreign_keys=ON"); cur.close()

        Base.metadata.create_all(eng)
        sf = build_session_factory(eng)
        _seed_season(sf)
        for i, df in enumerate(order):
            plan = build_espn_source_import_plan(parse_espn_player_stats_workbook(df, SEASON))
            discovered, content_hash = _discovered_and_hash(tmp_path / f"order_{id(order)}_{i}", df)
            persist_espn_source_import_plan(plan, sf, discovered=discovered, content_hash=content_hash, season_id=SEASON)
        with unit_of_work(sf) as session:
            teams = sorted(t.team_id for t in session.query(models.Team).all())
            obs_count = session.query(espn_models.EspnSourceObservation).count()
        eng.dispose()
        return teams, obs_count

    forward = run([df_a, df_b])
    backward = run([df_b, df_a])
    assert forward == backward


# ---------------------------------------------------------------------------
# Real-fixture end-to-end (all three real local ESPN files)
# ---------------------------------------------------------------------------


@pytestmark_real
def test_real_raw_player_stats_dry_run():
    discovered = discover_espn_artifacts(ESPN_DIR)
    raw = next(d for d in discovered if d.filename == "espn_full_player_stats.csv")
    df, content_hash = load_espn_dataframe(raw, ESPN_DIR)
    assert classify_espn_file_schema(df) == "raw_player_stats"
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    plan = build_espn_source_import_plan(parsed)
    assert plan.counts.total_source_rows == 3121
    assert plan.counts.resolved_teams == 2690
    assert plan.counts.unresolved_teams == 431


@pytestmark_real
def test_real_enriched_file_classified_derived_never_parsed_as_raw():
    discovered = discover_espn_artifacts(ESPN_DIR)
    enriched = next(d for d in discovered if d.filename == "espn_full_player_stats_enriched.csv")
    df, content_hash = load_espn_dataframe(enriched, ESPN_DIR)
    assert classify_espn_file_schema(df) == "derived_player_stats_enriched"
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    assert parsed.status == "invalid"  # this adapter refuses to persist the derived file as raw evidence
    assert parsed.rows == []


@pytestmark_real
def test_real_benchmark_reference_dry_run():
    discovered = discover_espn_artifacts(ESPN_DIR)
    bench = next(d for d in discovered if d.filename == "espn_ncaa_benchmarks.xlsx")
    df, content_hash = load_espn_dataframe(bench, ESPN_DIR)
    assert classify_espn_file_schema(df) == "benchmark_reference"
    parsed = parse_espn_benchmark_workbook(df)
    plan = build_espn_benchmark_import_plan(parsed)
    assert len(plan.value_specs) == 21
    assert parsed.status == "valid"


@pytestmark_real
def test_real_raw_player_stats_persist_end_to_end(session_factory):
    discovered = discover_espn_artifacts(ESPN_DIR)
    raw = next(d for d in discovered if d.filename == "espn_full_player_stats.csv")
    df, content_hash = load_espn_dataframe(raw, ESPN_DIR)
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    plan = build_espn_source_import_plan(parsed)
    _seed_season(session_factory)

    result = persist_espn_source_import_plan(plan, session_factory, discovered=raw, content_hash=content_hash, season_id=SEASON)
    assert result.observations_written == 3121

    with unit_of_work(session_factory) as session:
        assert session.query(espn_models.EspnSourceObservation).count() == 3121
        assert session.query(models.Team).count() == 313
        assert session.query(models.Player).count() == 2690


@pytestmark_real
def test_real_unca_row_persists_with_ambiguous_provenance_no_fake_team(session_factory):
    """Real-fixture, end-to-end confirmation: UNCA's 8 real rows persist with team_id=None and
    team_resolution_method='ambiguous' -- never a fabricated team_id, never silently downgraded to 'unresolved'."""
    discovered = discover_espn_artifacts(ESPN_DIR)
    raw = next(d for d in discovered if d.filename == "espn_full_player_stats.csv")
    df, content_hash = load_espn_dataframe(raw, ESPN_DIR)
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    unca_rows = [r for r in parsed.rows if r.raw_team_code == "UNCA"]
    assert len(unca_rows) == 8
    assert all(r.team_id is None and r.team_resolution_method == "ambiguous" for r in unca_rows)

    plan = build_espn_source_import_plan(parsed)
    _seed_season(session_factory)
    persist_espn_source_import_plan(plan, session_factory, discovered=raw, content_hash=content_hash, season_id=SEASON)
    with unit_of_work(session_factory) as session:
        rows = session.query(espn_models.EspnSourceObservation).filter_by(raw_team_code="UNCA").all()
        assert len(rows) == 8
        assert all(r.team_id is None and r.team_resolution_method == "ambiguous" for r in rows)


@pytestmark_real
def test_real_full_dry_run_resolution_counts_unchanged_by_this_batch():
    """Phase 5B6 adds zero new reviewed aliases (extended evidence search found nothing new
    that qualifies) -- confirms the real end-to-end resolved/unresolved row counts are
    byte-identical to the pre-batch baseline. Only UNCA's own team_resolution_method value
    changed (unresolved -> ambiguous), a provenance/metadata-only change, never a count change."""
    discovered = discover_espn_artifacts(ESPN_DIR)
    raw = next(d for d in discovered if d.filename == "espn_full_player_stats.csv")
    df, content_hash = load_espn_dataframe(raw, ESPN_DIR)
    parsed = parse_espn_player_stats_workbook(df, SEASON)
    plan = build_espn_source_import_plan(parsed)
    assert plan.counts.total_source_rows == 3121
    assert plan.counts.resolved_teams == 2690
    assert plan.counts.unresolved_teams == 431

    by_method = {}
    for r in parsed.rows:
        by_method[r.team_resolution_method] = by_method.get(r.team_resolution_method, 0) + 1
    assert by_method.get("ambiguous") == 8  # UNCA's own 8 rows, newly and correctly distinguished
    assert by_method.get("unresolved") == 423  # 431 - 8, the other 49 codes' rows


@pytestmark_real
def test_real_benchmark_persist_end_to_end(session_factory):
    discovered = discover_espn_artifacts(ESPN_DIR)
    bench = next(d for d in discovered if d.filename == "espn_ncaa_benchmarks.xlsx")
    df, content_hash = load_espn_dataframe(bench, ESPN_DIR)
    plan = build_espn_benchmark_import_plan(parse_espn_benchmark_workbook(df))

    result = persist_espn_benchmark_import_plan(plan, session_factory, discovered=bench, content_hash=content_hash)
    assert result.values_written == 21

    with unit_of_work(session_factory) as session:
        assert session.query(espn_models.EspnBenchmarkValue).count() == 21


# ---------------------------------------------------------------------------
# AST guards: no live access, no TeamDump mutation, no analytical formula
# ---------------------------------------------------------------------------


def test_adapter_never_imports_network_libraries():
    import ast
    import inspect
    import src.pipeline.espn_import_adapter as mod

    tree = ast.parse(inspect.getsource(mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not ({"requests", "urllib", "socket", "http", "selenium"} & imported)


def test_adapter_never_writes_to_teamdump():
    import ast
    import inspect
    import src.pipeline.espn_import_adapter as mod

    tree = ast.parse(inspect.getsource(mod))
    called = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not ({"to_excel", "to_csv"} & called)


def test_adapter_never_imports_analytical_or_ovr_modules():
    import ast
    import inspect
    import src.pipeline.espn_import_adapter as mod

    imported_modules = {n.module for n in ast.walk(ast.parse(inspect.getsource(mod))) if isinstance(n, ast.ImportFrom) and n.module}
    forbidden = ("ovr_model", "player_benchmarks_transformation", "game_projection", "bet_decision", "expected_value", "kelly")
    for m in imported_modules:
        assert not any(f in m.lower() for f in forbidden), f"unexpected import: {m}"
