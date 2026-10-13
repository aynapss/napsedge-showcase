"""
Phase 7 Slice 3A: the smallest genuine real-PostgreSQL proof that
`src.streamlit_consumer_controller.run_player_raw_statistics_lookup`
behaves correctly against a real (not SQLite-emulated) database -- calls
the controller directly, never through a live Streamlit server. Reuses
the existing disposable-PostgreSQL fixtures (`pg_session_factory`,
`pg_migrated_database_url`) -- no competing container harness is
created here.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from src.db.allstats_repositories import PlayerStatSnapshotRepository
from src.db.repositories import DataSourceRepository, PlayerRepository, SeasonRepository, SourceArtifactRepository, TeamRepository
from src.db.session import unit_of_work
from src.identity.player_identity import build_player_identity
import src.streamlit_consumer_controller as controller

pytestmark = pytest.mark.postgres


def _seed_base(session_factory, *, team_id=1, season_id=2026):
    with unit_of_work(session_factory) as session:
        TeamRepository(session).upsert(team_id=team_id, display_name="Test Team", sr_slug=f"test-team-{team_id}")
        SeasonRepository(session).upsert(
            season_id=season_id, start_year=season_id - 1, end_year=season_id, label=f"{season_id - 1}-{season_id}"
        )
        DataSourceRepository(session).upsert(
            source_key="sr_allstats", display_name="SR AllStats", acquisition_type="live_scraped_html", is_rescrapable=True
        )


def _seed_artifact(session_factory, *, team_id=1, season_id=2026, relative_path="test-team/Roster_2026.xlsx") -> int:
    with unit_of_work(session_factory) as session:
        artifact = SourceArtifactRepository(session).upsert(
            source_key="sr_allstats", source_root_key="teamdump_cbb", relative_path=relative_path,
            original_filename=relative_path.split("/")[-1], file_type="xlsx", content_hash="hash-a",
            team_id=team_id, season_id=season_id, validation_status="valid",
            point_in_time_classification="historical_but_not_point_in_time",
        )
        return artifact.id


def _seed_player(session_factory, *, team_id=1, season_id=2026, display_name="Frank Mitchell"):
    identity = build_player_identity(display_name, team_id, season_id)
    with unit_of_work(session_factory) as session:
        PlayerRepository(session).upsert(
            player_id=identity.player_id, display_name=identity.display_name, team_id=team_id, season_id=season_id
        )
    return identity


def _seed_stat_row(
    session_factory, *, player_id, team_id=1, season_id=2026, source_artifact_id,
    stat_table="PerGame", validation_status="valid", **stat_values,
):
    with unit_of_work(session_factory) as session:
        PlayerStatSnapshotRepository(session).upsert(
            team_id=team_id, season_id=season_id, source_artifact_id=source_artifact_id, stat_table=stat_table,
            player_key="frank mitchell", source_player_name="Frank Mitchell", validation_status=validation_status,
            player_id=player_id, **stat_values,
        )


def test_known_player_with_multiple_rows_against_real_postgresql(pg_migrated_database_url, pg_session_factory):
    _seed_base(pg_session_factory)
    artifact_id = _seed_artifact(pg_session_factory)
    identity = _seed_player(pg_session_factory)
    _seed_stat_row(pg_session_factory, player_id=identity.player_id, source_artifact_id=artifact_id, stat_table="PerGame", pts=16.1)
    _seed_stat_row(pg_session_factory, player_id=identity.player_id, source_artifact_id=artifact_id, stat_table="Totals", pts=580.0)

    presentation = controller.run_player_raw_statistics_lookup(identity.player_id, session_factory=pg_session_factory)

    assert presentation.status == "ok"
    assert len(presentation.rows) == 2


def test_unknown_player_against_real_postgresql(pg_migrated_database_url, pg_session_factory):
    presentation = controller.run_player_raw_statistics_lookup("does-not-exist", session_factory=pg_session_factory)
    assert presentation.status == "not_found"


def test_known_player_with_no_matching_filters_against_real_postgresql(pg_migrated_database_url, pg_session_factory):
    _seed_base(pg_session_factory)
    artifact_id = _seed_artifact(pg_session_factory)
    identity = _seed_player(pg_session_factory)
    _seed_stat_row(pg_session_factory, player_id=identity.player_id, source_artifact_id=artifact_id, stat_table="PerGame", pts=16.1)

    presentation = controller.run_player_raw_statistics_lookup(
        identity.player_id, session_factory=pg_session_factory, stat_table="Advanced"
    )

    assert presentation.status == "ok"
    assert presentation.rows == ()


def test_validation_status_and_none_zero_round_trip_against_real_postgresql(pg_migrated_database_url, pg_session_factory):
    _seed_base(pg_session_factory)
    artifact_id = _seed_artifact(pg_session_factory)
    identity = _seed_player(pg_session_factory)
    _seed_stat_row(
        pg_session_factory, player_id=identity.player_id, source_artifact_id=artifact_id,
        validation_status="conflicting", games_played=0, fg_pct=None,
    )

    presentation = controller.run_player_raw_statistics_lookup(identity.player_id, session_factory=pg_session_factory)

    row = presentation.rows[0]
    validation_field = next(f for f in row if f.label == "validation_status")
    games_field = next(f for f in row if f.label == "games_played")
    fg_pct_field = next(f for f in row if f.label == "fg_pct")
    assert validation_field.value == "conflicting"
    assert games_field.value == "0"
    assert fg_pct_field.value is None


def test_session_is_owned_and_closed_per_call_against_real_postgresql(pg_migrated_database_url, pg_session_factory):
    _seed_base(pg_session_factory)
    artifact_id = _seed_artifact(pg_session_factory)
    identity = _seed_player(pg_session_factory)
    _seed_stat_row(pg_session_factory, player_id=identity.player_id, source_artifact_id=artifact_id)

    opened_sessions = []

    def spying_factory():
        session = pg_session_factory()
        session.close = MagicMock(wraps=session.close)
        opened_sessions.append(session)
        return session

    controller.run_player_raw_statistics_lookup(identity.player_id, session_factory=spying_factory)

    assert len(opened_sessions) == 1
    opened_sessions[0].close.assert_called_once()

    with pg_session_factory() as check_session:
        active = check_session.execute(
            text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()")
        ).scalar()
    assert active == 0
