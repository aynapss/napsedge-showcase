"""
Phase 5A: behavior tests for `src/db/repositories.py` and
`src/db/session.py`. Every test runs against a fresh, disposable in-memory
SQLite database (never a shared file, never `TeamDump`, never a
production database, never a live network call).
"""
from __future__ import annotations

import ast
import datetime as dt
import inspect

import pytest
from sqlalchemy import create_engine, event

from src.db.base import Base
from src.db.session import build_session_factory, unit_of_work
from src.db.repositories import (
    ConferenceRepository,
    DataQualityIssueRepository,
    DataSourceRepository,
    GameRepository,
    ModelVersionRepository,
    PipelineRunRepository,
    PlayerAliasRepository,
    PlayerRepository,
    SeasonRepository,
    SourceArtifactRepository,
    SportsbookRepository,
    TeamAliasRepository,
    TeamRepository,
)
from src.db.enums import (
    ISSUE_CODE_ESPN_ABBREVIATION_UNRESOLVED,
    ISSUE_CODE_GAMELOG_TRAILING_SEASON_TOTAL_ROW,
)
from src.exceptions import IdentityConflictError, ValidationError


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


def _utc(*args) -> dt.datetime:
    return dt.datetime(*args, tzinfo=dt.timezone.utc)


# ---------------------------------------------------------------------------
# Idempotent upsert + immutable-field conflict rejection, per table
# ---------------------------------------------------------------------------


def test_conference_upsert_idempotent_and_conflict_rejected(session_factory):
    with unit_of_work(session_factory) as session:
        repo = ConferenceRepository(session)
        repo.upsert(conference_id=1, canonical_name="Atlantic 10")
        repo.upsert(conference_id=1, canonical_name="Atlantic 10")  # identical repeat -- no-op

    with unit_of_work(session_factory) as session:
        repo = ConferenceRepository(session)
        with pytest.raises(IdentityConflictError):
            repo.upsert(conference_id=1, canonical_name="Big Ten")  # conflicting immutable field


def test_team_upsert_idempotent_mutable_fields_update_immutable_fields_conflict(session_factory):
    with unit_of_work(session_factory) as session:
        repo = TeamRepository(session)
        repo.upsert(team_id=1, display_name="St. Bonaventure", sr_slug="st-bonaventure")
        row = repo.upsert(team_id=1, display_name="Saint Bonaventure", sr_slug="st-bonaventure")  # mutable field updated
        assert row.display_name == "Saint Bonaventure"

    with unit_of_work(session_factory) as session:
        repo = TeamRepository(session)
        with pytest.raises(IdentityConflictError):
            repo.upsert(team_id=1, display_name="Saint Bonaventure", sr_slug="different-slug")  # immutable field conflict


def test_team_alias_scoped_by_source_conflict_rejected(session_factory):
    with unit_of_work(session_factory) as session:
        TeamRepository(session).upsert(team_id=1, display_name="A", sr_slug="a")
        TeamRepository(session).upsert(team_id=2, display_name="B", sr_slug="b")
        alias_repo = TeamAliasRepository(session)
        alias_repo.upsert(team_id=1, source="espn", raw_value="Team A", normalized_value="team a")
        alias_repo.upsert(team_id=1, source="espn", raw_value="Team A", normalized_value="team a")  # identical repeat

    with unit_of_work(session_factory) as session:
        alias_repo = TeamAliasRepository(session)
        with pytest.raises(IdentityConflictError):
            alias_repo.upsert(team_id=2, source="espn", raw_value="Team A", normalized_value="team a")


def test_team_alias_same_raw_value_different_source_not_a_conflict(session_factory):
    with unit_of_work(session_factory) as session:
        TeamRepository(session).upsert(team_id=1, display_name="A", sr_slug="a")
        alias_repo = TeamAliasRepository(session)
        alias_repo.upsert(team_id=1, source="espn", raw_value="TA", normalized_value="ta")
        alias_repo.upsert(team_id=1, source="covers", raw_value="TA", normalized_value="ta")  # must not raise


def test_player_alias_scoped_by_player_not_globally_by_source(session_factory):
    """Two different players may legitimately share a raw display name under the same source -- only team_aliases is source-globally scoped."""
    with unit_of_work(session_factory) as session:
        TeamRepository(session).upsert(team_id=1, display_name="A", sr_slug="a")
        SeasonRepository(session).upsert(season_id=2026, start_year=2025, end_year=2026, label="2025-26")
        PlayerRepository(session).upsert(player_id="p1", display_name="Michael Jordan", team_id=1, season_id=2026)
        PlayerRepository(session).upsert(player_id="p2", display_name="Michael Jordan", team_id=1, season_id=2026)
        alias_repo = PlayerAliasRepository(session)
        alias_repo.upsert(player_id="p1", source="espn", raw_value="M. Jordan", normalized_value="michael jordan")
        alias_repo.upsert(player_id="p2", source="espn", raw_value="M. Jordan", normalized_value="michael jordan")  # must not raise


def test_season_upsert_idempotent_and_conflict_rejected(session_factory):
    with unit_of_work(session_factory) as session:
        repo = SeasonRepository(session)
        repo.upsert(season_id=2026, start_year=2025, end_year=2026, label="2025-26")
        repo.upsert(season_id=2026, start_year=2025, end_year=2026, label="2025-26")

    with unit_of_work(session_factory) as session:
        repo = SeasonRepository(session)
        with pytest.raises(IdentityConflictError):
            repo.upsert(season_id=2026, start_year=2024, end_year=2026, label="2025-26")


def test_player_upsert_idempotent_and_immutable_team_season_conflict(session_factory):
    with unit_of_work(session_factory) as session:
        TeamRepository(session).upsert(team_id=1, display_name="A", sr_slug="a")
        TeamRepository(session).upsert(team_id=2, display_name="B", sr_slug="b")
        SeasonRepository(session).upsert(season_id=2026, start_year=2025, end_year=2026, label="2025-26")
        repo = PlayerRepository(session)
        repo.upsert(player_id="p1", display_name="Frank Mitchell", team_id=1, season_id=2026)
        row = repo.upsert(player_id="p1", display_name="Frank T. Mitchell", team_id=1, season_id=2026)
        assert row.display_name == "Frank T. Mitchell"

    with unit_of_work(session_factory) as session:
        repo = PlayerRepository(session)
        with pytest.raises(IdentityConflictError):
            repo.upsert(player_id="p1", display_name="Frank Mitchell", team_id=2, season_id=2026)


def test_sportsbook_upsert_idempotent_and_conflict_rejected(session_factory):
    with unit_of_work(session_factory) as session:
        repo = SportsbookRepository(session)
        repo.upsert(sportsbook_id=1, canonical_key="fanduel", display_name="FanDuel")
        row = repo.upsert(sportsbook_id=1, canonical_key="fanduel", display_name="FanDuel Sportsbook")
        assert row.display_name == "FanDuel Sportsbook"

    with unit_of_work(session_factory) as session:
        repo = SportsbookRepository(session)
        with pytest.raises(IdentityConflictError):
            repo.upsert(sportsbook_id=1, canonical_key="draftkings", display_name="DraftKings")


def test_model_version_upsert_idempotent_and_conflict_rejected(session_factory):
    with unit_of_work(session_factory) as session:
        repo = ModelVersionRepository(session)
        repo.upsert(model_version_id=1, label="legacy_v15_radar", config_fingerprint="abc123")
        repo.upsert(model_version_id=1, label="legacy_v15_radar", config_fingerprint="abc123")

    with unit_of_work(session_factory) as session:
        repo = ModelVersionRepository(session)
        with pytest.raises(IdentityConflictError):
            repo.upsert(model_version_id=1, label="legacy_v15_radar", config_fingerprint="different")


def test_data_source_upsert_is_fully_mutable_except_key(session_factory):
    with unit_of_work(session_factory) as session:
        repo = DataSourceRepository(session)
        repo.upsert(source_key="espn_player_stats", display_name="ESPN Player Stats", acquisition_type="static_file", is_rescrapable=False)
        row = repo.upsert(
            source_key="espn_player_stats", display_name="ESPN Player Stats (raw)", description="season-to-date",
            acquisition_type="live_scraped_html", is_rescrapable=True,
        )
        assert row.display_name == "ESPN Player Stats (raw)"
        assert row.description == "season-to-date"
        assert row.acquisition_type == "live_scraped_html"
        assert row.is_rescrapable is True


# ---------------------------------------------------------------------------
# Games: provisional vs. confirmed identity distinguishability
# ---------------------------------------------------------------------------


def _seed_two_teams_and_season(session):
    TeamRepository(session).upsert(team_id=1, display_name="A", sr_slug="a")
    TeamRepository(session).upsert(team_id=2, display_name="B", sr_slug="b")
    SeasonRepository(session).upsert(season_id=2026, start_year=2025, end_year=2026, label="2025-26")


def test_game_upsert_idempotent_and_structural_conflict_rejected(session_factory):
    with unit_of_work(session_factory) as session:
        _seed_two_teams_and_season(session)
        repo = GameRepository(session)
        repo.upsert(game_id=100, home_team_id=1, away_team_id=2, season_id=2026, game_date="2026-01-15", neutral_site=False, is_provisional=True)
        repo.upsert(game_id=100, home_team_id=1, away_team_id=2, season_id=2026, game_date="2026-01-15", neutral_site=False, is_provisional=True)

    with unit_of_work(session_factory) as session:
        repo = GameRepository(session)
        with pytest.raises(IdentityConflictError):
            repo.upsert(game_id=100, home_team_id=1, away_team_id=2, season_id=2026, game_date="2026-01-16", neutral_site=False, is_provisional=True)


def test_game_rejects_same_team_on_both_sides(session_factory):
    with unit_of_work(session_factory) as session:
        _seed_two_teams_and_season(session)
        repo = GameRepository(session)
        with pytest.raises(ValidationError):
            repo.upsert(game_id=101, home_team_id=1, away_team_id=1, season_id=2026, game_date="2026-01-15", neutral_site=False, is_provisional=True)


def test_provisional_and_confirmed_game_ids_coexist_distinguishably(session_factory):
    with unit_of_work(session_factory) as session:
        _seed_two_teams_and_season(session)
        repo = GameRepository(session)
        provisional = repo.upsert(
            game_id=200, home_team_id=1, away_team_id=2, season_id=2026, game_date="2026-01-15",
            neutral_site=False, is_provisional=True,
        )
        confirmed = repo.upsert(
            game_id=300, home_team_id=1, away_team_id=2, season_id=2026, game_date="2026-01-15",
            neutral_site=False, is_provisional=False, source="espn", source_game_id="evt-1",
        )
        assert provisional.is_provisional is True
        assert confirmed.is_provisional is False
        assert provisional.game_id != confirmed.game_id


# ---------------------------------------------------------------------------
# Transaction rollback: the whole unit of work reverts on any failure
# ---------------------------------------------------------------------------


def test_transaction_rolls_back_entire_unit_of_work_on_conflict(session_factory):
    with unit_of_work(session_factory) as session:
        ConferenceRepository(session).upsert(conference_id=1, canonical_name="Atlantic 10")

    with pytest.raises(IdentityConflictError):
        with unit_of_work(session_factory) as session:
            TeamRepository(session).upsert(team_id=1, display_name="A", sr_slug="a", conference_id=1)
            # This second call raises -- the team insert above must NOT survive the rollback.
            ConferenceRepository(session).upsert(conference_id=1, canonical_name="CONFLICTING NAME")

    with unit_of_work(session_factory) as session:
        assert session.get.__self__ is session
        from src.db.models import Team
        assert session.get(Team, 1) is None  # proves the rollback reverted the whole block, not just the failing call


# ---------------------------------------------------------------------------
# UTC timestamp handling
# ---------------------------------------------------------------------------


def test_pipeline_run_rejects_naive_datetime(session_factory):
    with unit_of_work(session_factory) as session:
        repo = PipelineRunRepository(session)
        with pytest.raises(ValidationError):
            repo.start(run_id="run-1", pipeline_name="test", started_at=dt.datetime(2026, 1, 1))  # naive -- must be rejected


def test_pipeline_run_accepts_and_normalizes_non_utc_timezone(session_factory):
    tz_est = dt.timezone(dt.timedelta(hours=-5))
    with unit_of_work(session_factory) as session:
        repo = PipelineRunRepository(session)
        row = repo.start(run_id="run-1", pipeline_name="test", started_at=dt.datetime(2026, 1, 1, 12, 0, tzinfo=tz_est))
        assert row.started_at.tzinfo is not None
        assert row.started_at.utcoffset() == dt.timedelta(0)
        assert row.started_at.hour == 17  # 12:00 EST -> 17:00 UTC


# ---------------------------------------------------------------------------
# Pipeline-run state transitions
# ---------------------------------------------------------------------------


def test_pipeline_run_valid_transitions(session_factory):
    with unit_of_work(session_factory) as session:
        repo = PipelineRunRepository(session)
        repo.start(run_id="run-1", pipeline_name="test", started_at=_utc(2026, 1, 1))
        row = repo.transition(run_id="run-1", new_status="succeeded", completed_at=_utc(2026, 1, 1, 0, 5))
        assert row.status == "succeeded"
        assert row.completed_at is not None


def test_pipeline_run_invalid_transition_rejected(session_factory):
    with unit_of_work(session_factory) as session:
        repo = PipelineRunRepository(session)
        repo.start(run_id="run-1", pipeline_name="test", started_at=_utc(2026, 1, 1))
        repo.transition(run_id="run-1", new_status="succeeded")

    with unit_of_work(session_factory) as session:
        repo = PipelineRunRepository(session)
        with pytest.raises(ValidationError):
            repo.transition(run_id="run-1", new_status="running")  # succeeded is terminal


def test_pipeline_run_duplicate_run_id_rejected(session_factory):
    with unit_of_work(session_factory) as session:
        repo = PipelineRunRepository(session)
        repo.start(run_id="run-1", pipeline_name="test", started_at=_utc(2026, 1, 1))

    with unit_of_work(session_factory) as session:
        repo = PipelineRunRepository(session)
        with pytest.raises(IdentityConflictError):
            repo.start(run_id="run-1", pipeline_name="test-2", started_at=_utc(2026, 1, 2))


# ---------------------------------------------------------------------------
# Source-artifact provenance: content hash, relative path, nullable
# collection timestamp, mtime-is-not-collection-time
# ---------------------------------------------------------------------------


def test_source_artifact_upsert_natural_key_and_mutable_refinement(session_factory):
    with unit_of_work(session_factory) as session:
        DataSourceRepository(session).upsert(source_key="sr_allstats", display_name="Sports-Reference AllStats", acquisition_type="live_scraped_html", is_rescrapable=True)
        repo = SourceArtifactRepository(session)
        row = repo.upsert(
            source_key="sr_allstats", source_root_key="teamdump_cbb", relative_path="st-bonaventure/Roster_2026.xlsx",
            original_filename="Roster_2026.xlsx", file_type="xlsx", content_hash="hash1",
            point_in_time_classification="historical_but_not_point_in_time", validation_status="pending",
        )
        assert row.validation_status == "pending"

        refined = repo.upsert(
            source_key="sr_allstats", source_root_key="teamdump_cbb", relative_path="st-bonaventure/Roster_2026.xlsx",
            original_filename="Roster_2026.xlsx", file_type="xlsx", content_hash="hash1",
            point_in_time_classification="historical_but_not_point_in_time", validation_status="valid",
        )
        assert refined.validation_status == "valid"
        assert refined.id == row.id  # same natural key -- refined in place, not duplicated


def test_source_artifact_immutable_file_type_conflict_rejected(session_factory):
    with unit_of_work(session_factory) as session:
        DataSourceRepository(session).upsert(source_key="sr_allstats", display_name="SR AllStats", acquisition_type="live_scraped_html", is_rescrapable=True)
        repo = SourceArtifactRepository(session)
        repo.upsert(
            source_key="sr_allstats", source_root_key="teamdump_cbb", relative_path="x/Roster_2026.xlsx",
            original_filename="Roster_2026.xlsx", file_type="xlsx", content_hash="hashA",
            point_in_time_classification="current_snapshot_only",
        )

    with unit_of_work(session_factory) as session:
        repo = SourceArtifactRepository(session)
        with pytest.raises(IdentityConflictError):
            repo.upsert(
                source_key="sr_allstats", source_root_key="teamdump_cbb", relative_path="x/Roster_2026.xlsx",
                original_filename="Roster_2026.xlsx", file_type="csv", content_hash="hashA",  # file_type changed -- immutable
                point_in_time_classification="current_snapshot_only",
            )


def test_source_artifact_nullable_collection_timestamp(session_factory):
    with unit_of_work(session_factory) as session:
        DataSourceRepository(session).upsert(source_key="src", display_name="Src", acquisition_type="static_file", is_rescrapable=False)
        row = SourceArtifactRepository(session).upsert(
            source_key="src", source_root_key="teamdump_espn", relative_path="espn_full_player_stats.csv",
            original_filename="espn_full_player_stats.csv", file_type="csv",
            point_in_time_classification="current_snapshot_only",
            collection_timestamp=None,  # confirmed this batch: no ESPN file has a real collection timestamp
        )
        assert row.collection_timestamp is None


def test_source_artifact_relative_path_never_stores_absolute_windows_path(session_factory):
    """Phase 5A instruction: never hardcode the absolute source root as a business identifier -- relative_path must be relative."""
    with unit_of_work(session_factory) as session:
        DataSourceRepository(session).upsert(source_key="src", display_name="Src", acquisition_type="static_file", is_rescrapable=False)
        row = SourceArtifactRepository(session).upsert(
            source_key="src", source_root_key="teamdump_cbb", relative_path="st-bonaventure/Roster_2026.xlsx",
            original_filename="Roster_2026.xlsx", file_type="xlsx",
            point_in_time_classification="historical_but_not_point_in_time",
        )
        assert ":" not in row.relative_path
        assert "TeamDump" not in row.relative_path
        assert not row.relative_path.startswith("/")


# ---------------------------------------------------------------------------
# Torvik source classification: live_scraped_json, rescrapable, and every
# snapshot preserved immutably (never overwritten by a later scrape)
# ---------------------------------------------------------------------------


def test_torvik_registered_as_live_scraped_json_and_rescrapable(session_factory):
    with unit_of_work(session_factory) as session:
        row = DataSourceRepository(session).upsert(
            source_key="torvik", display_name="Torvik ratings (AdjOE/AdjDE and other live metrics)",
            acquisition_type="live_scraped_json", is_rescrapable=True,
        )
        assert row.acquisition_type == "live_scraped_json"
        assert row.is_rescrapable is True


def test_torvik_repeat_scrape_with_identical_content_hash_is_idempotent(session_factory):
    """An identical repeat scrape (same content, same hash) is the ordinary idempotent no-op -- not a new snapshot, and not an error."""
    with unit_of_work(session_factory) as session:
        DataSourceRepository(session).upsert(source_key="torvik", display_name="Torvik", acquisition_type="live_scraped_json", is_rescrapable=True)
        repo = SourceArtifactRepository(session)
        common = dict(
            source_key="torvik", source_root_key="torvik_cache", relative_path="torvik_2026.json",
            original_filename="torvik_2026.json", file_type="json", content_hash="same-hash",
            collection_timestamp=_utc(2026, 1, 10),
            point_in_time_classification="historical_but_not_point_in_time",
        )
        first = repo.upsert(**common)
        second = repo.upsert(**common)
        assert first.id == second.id  # same natural key -- not duplicated


def test_torvik_two_distinct_scrapes_never_overwrite_each_other(session_factory):
    """Confirmed requirement: never overwrite an older Torvik snapshot with a newer scrape. A new content_hash (ratings recalculated) always produces a NEW, separate row -- the old snapshot's own row is untouched."""
    with unit_of_work(session_factory) as session:
        DataSourceRepository(session).upsert(source_key="torvik", display_name="Torvik", acquisition_type="live_scraped_json", is_rescrapable=True)
        repo = SourceArtifactRepository(session)
        early = repo.upsert(
            source_key="torvik", source_root_key="torvik_cache", relative_path="torvik_2026.json",
            original_filename="torvik_2026.json", file_type="json", content_hash="hash-early-season",
            collection_timestamp=_utc(2026, 1, 10), season_id=None,
            point_in_time_classification="historical_but_not_point_in_time",
        )
        late = repo.upsert(
            source_key="torvik", source_root_key="torvik_cache", relative_path="torvik_2026.json",
            original_filename="torvik_2026.json", file_type="json", content_hash="hash-late-season",
            collection_timestamp=_utc(2026, 3, 1), season_id=None,
            point_in_time_classification="historical_but_not_point_in_time",
        )

    with unit_of_work(session_factory) as session:
        from src.db.models import SourceArtifact
        still_present = session.get(SourceArtifact, early.id)
        assert still_present is not None
        assert still_present.content_hash == "hash-early-season"  # unchanged by the later scrape
        # SQLite has no native timezone-aware datetime type, so a genuine round-trip read
        # (a fresh session.get, as opposed to the same in-memory object the upsert call
        # itself returned) comes back naive -- a real, honest SQLite-dialect limitation,
        # not a repository bug. On PostgreSQL, TIMESTAMPTZ round-trips with tzinfo intact;
        # this comparison is written to pass correctly under either dialect's behavior.
        assert still_present.collection_timestamp.replace(tzinfo=dt.timezone.utc) == _utc(2026, 1, 10)
        assert late.id != early.id


def test_source_artifact_mtime_is_metadata_only_repository_never_reads_it_as_collection_time():
    """Confirmed via AST inspection: the repository never assigns filesystem_mtime's value to collection_timestamp anywhere."""
    import textwrap
    from src.db import repositories as repo_module
    source = textwrap.dedent(inspect.getsource(repo_module.SourceArtifactRepository.upsert))
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "collection_timestamp" in targets:
                assert not (isinstance(node.value, ast.Name) and node.value.id == "filesystem_mtime")


@pytest.mark.parametrize("classification", [
    "point_in_time_eligible", "historical_but_not_point_in_time", "current_snapshot_only",
    "fixture_only", "provenance_incomplete", "invalid_or_unusable",
])
def test_source_artifact_accepts_all_six_point_in_time_classifications(session_factory, classification):
    with unit_of_work(session_factory) as session:
        DataSourceRepository(session).upsert(source_key="src", display_name="Src", acquisition_type="static_file", is_rescrapable=False)
        row = SourceArtifactRepository(session).upsert(
            source_key="src", source_root_key="teamdump_cbb", relative_path=f"x/{classification}.csv",
            original_filename=f"{classification}.csv", file_type="csv",
            point_in_time_classification=classification,
        )
        assert row.point_in_time_classification == classification


# ---------------------------------------------------------------------------
# Data-quality issues: append-only, and the two confirmed batch findings
# ---------------------------------------------------------------------------


def test_data_quality_issue_append_never_updates_existing_row(session_factory):
    with unit_of_work(session_factory) as session:
        repo = DataQualityIssueRepository(session)
        first = repo.append(issue_code="x", severity="warning", description="first finding")
        second = repo.append(issue_code="x", severity="warning", description="first finding")  # identical -- still a NEW row
        assert first.id != second.id


def test_gamelog_trailing_season_total_row_issue_recorded(session_factory):
    """Confirmed this batch (docs/local_data_inventory.md section 4): all 128 basic-schema team game logs end with a mislabeled season-total row. This test proves the issue can be recorded -- it does not implement the game-log importer."""
    with unit_of_work(session_factory) as session:
        TeamRepository(session).upsert(team_id=1, display_name="Akron", sr_slug="akron")
        DataSourceRepository(session).upsert(source_key="sr_gamelog", display_name="Sports-Reference team game log", acquisition_type="live_scraped_html", is_rescrapable=True)
        artifact = SourceArtifactRepository(session).upsert(
            source_key="sr_gamelog", source_root_key="teamdump_cbb", relative_path="akron/akron_2026_game_log.csv",
            original_filename="akron_2026_game_log.csv", file_type="csv", team_id=1,
            point_in_time_classification="historical_but_not_point_in_time",
        )
        issue = DataQualityIssueRepository(session).append(
            issue_code=ISSUE_CODE_GAMELOG_TRAILING_SEASON_TOTAL_ROW, severity="warning",
            description="Trailing row has blank date/opponent and season-total pts_scored/pts_allowed values, not a real game.",
            source_artifact_id=artifact.id, team_id=1,
            context='{"row_index": 35, "pts_scored": 3004, "pts_allowed": 2499}',
        )
        assert issue.issue_code == ISSUE_CODE_GAMELOG_TRAILING_SEASON_TOTAL_ROW
        assert issue.source_artifact_id == artifact.id


def test_espn_abbreviation_unresolved_issue_recorded(session_factory):
    """Confirmed this batch (docs/local_data_inventory.md section 5): ~23% of ESPN Team abbreviation values resolve through the canonical resolver. Recorded as an open gap, never guessed."""
    with unit_of_work(session_factory) as session:
        DataSourceRepository(session).upsert(source_key="espn_player_stats", display_name="ESPN player stats", acquisition_type="static_file", is_rescrapable=False)
        artifact = SourceArtifactRepository(session).upsert(
            source_key="espn_player_stats", source_root_key="teamdump_espn", relative_path="espn_full_player_stats.csv",
            original_filename="espn_full_player_stats.csv", file_type="csv",
            point_in_time_classification="current_snapshot_only", validation_status="unresolved",
        )
        issue = DataQualityIssueRepository(session).append(
            issue_code=ISSUE_CODE_ESPN_ABBREVIATION_UNRESOLVED, severity="info",
            description="ESPN Team abbreviation 'ECU' did not resolve via resolve_team_identity().",
            source_artifact_id=artifact.id, context='{"raw_abbreviation": "ECU"}',
        )
        assert issue.issue_code == ISSUE_CODE_ESPN_ABBREVIATION_UNRESOLVED
        # team_id is deliberately left unset -- never guessed
        assert issue.team_id is None


# ---------------------------------------------------------------------------
# AST guards: no fuzzy matching, no analytical recalculation, no network,
# no secret access, no filesystem writes
# ---------------------------------------------------------------------------


def _imported_module_roots(module) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_repositories_module_never_imports_fuzzy_matching():
    import src.db.repositories as mod
    tree = ast.parse(inspect.getsource(mod))
    called = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not ({"get_close_matches", "ratio", "SequenceMatcher"} & called)
    assert "difflib" not in _imported_module_roots(mod)


def test_repositories_module_never_imports_transformation_or_pipeline_modules():
    """No analytical recalculation: this module must never import a formula/scoring/probability transformation."""
    import src.db.repositories as mod
    tree = ast.parse(inspect.getsource(mod))
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    assert not any(m.startswith("src.transformations") for m in imported_modules)
    assert not any(m.startswith("src.pipeline") for m in imported_modules)


def test_repositories_and_session_modules_never_import_network_libraries():
    import src.db.repositories as repo_mod
    import src.db.session as session_mod
    for mod in (repo_mod, session_mod):
        roots = _imported_module_roots(mod)
        assert not ({"requests", "urllib", "socket", "http"} & roots)


def test_config_module_never_reads_env_file_contents():
    """Phase 5A safety constraint: never inspect/print/validate .env secret values. src.db.config only ever passes os.environ.get('NAPSEDGE_DATABASE_URL') through unexamined -- checked at the AST level (calls/imports), not by banning the word from this module's own explanatory docstring."""
    import src.db.config as mod
    tree = ast.parse(inspect.getsource(mod))
    called_names = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    called_attrs = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "open" not in called_names
    assert "print" not in called_names
    assert "load_env_file" not in called_names
    # os.environ.get is fine (used for NAPSEDGE_DATABASE_URL); confirm no secret-named env var is ever read here.
    env_get_args = [
        n.args[0].value for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
        and n.args and isinstance(n.args[0], ast.Constant)
    ]
    assert "ODDS_API_KEY" not in env_get_args
    assert "DISCORD_BOT_TOKEN" not in env_get_args


def test_repositories_module_never_performs_filesystem_io():
    import src.db.repositories as mod
    tree = ast.parse(inspect.getsource(mod))
    called_names = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not ({"open", "to_excel", "to_csv"} & called_names)


def test_no_repository_method_calls_session_commit():
    """Phase 5A instruction: 'do not commit inside low-level model constructors' -- commits happen only in src.db.session.unit_of_work."""
    import src.db.repositories as mod
    tree = ast.parse(inspect.getsource(mod))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "commit":
            pytest.fail("src.db.repositories must never call .commit() directly")


def test_this_test_file_never_imports_network_libraries():
    import sys
    roots = _imported_module_roots(sys.modules[__name__])
    assert not ({"requests", "urllib", "socket", "http"} & roots)
