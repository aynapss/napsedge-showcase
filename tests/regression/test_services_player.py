"""
Phase 6, Slice 4A: unit tests for `src.services.player` -- the read-only
Player raw-statistics service behind (not yet exposed via HTTP)
`get_player_raw_statistics`.

Every fixture here is a disposable, isolated in-memory SQLite database --
never `TeamDump`, never a live network call, never notebook execution.
Player identities are built via the real, unmodified
`src.identity.player_identity.build_player_identity` (the actual
production constructor), never guessed/hardcoded.
"""
from __future__ import annotations

import inspect

import pytest
from sqlalchemy import create_engine, event

from src.db.allstats_repositories import PlayerStatSnapshotRepository
from src.db.base import Base
from src.db.repositories import DataSourceRepository, PlayerRepository, SeasonRepository, SourceArtifactRepository, TeamRepository
from src.db.session import build_session_factory, unit_of_work
from src.identity.player_identity import build_player_identity
from src.services import player as player_service
from src.services.contracts import PlayerRawStatisticsResult, PlayerStatRecord
from src.services.player import get_player_raw_statistics


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


def _seed_base(session_factory, *, team_id=1, season_id=2026):
    with unit_of_work(session_factory) as session:
        TeamRepository(session).upsert(team_id=team_id, display_name="Test Team", sr_slug=f"test-team-{team_id}")
        SeasonRepository(session).upsert(season_id=season_id, start_year=season_id - 1, end_year=season_id, label=f"{season_id - 1}-{season_id}")
        DataSourceRepository(session).upsert(source_key="sr_allstats", display_name="SR AllStats", acquisition_type="live_scraped_html", is_rescrapable=True)


def _seed_artifact(session_factory, *, team_id=1, season_id=2026, relative_path="test-team/Roster_2026.xlsx", content_hash="hashA") -> int:
    with unit_of_work(session_factory) as session:
        artifact = SourceArtifactRepository(session).upsert(
            source_key="sr_allstats", source_root_key="teamdump_cbb", relative_path=relative_path,
            original_filename=relative_path.split("/")[-1], file_type="xlsx", content_hash=content_hash,
            team_id=team_id, season_id=season_id, validation_status="valid",
            point_in_time_classification="historical_but_not_point_in_time",
        )
        return artifact.id


def _seed_player(session_factory, *, team_id=1, season_id=2026, display_name="Frank Mitchell"):
    identity = build_player_identity(display_name, team_id, season_id)
    with unit_of_work(session_factory) as session:
        PlayerRepository(session).upsert(player_id=identity.player_id, display_name=identity.display_name, team_id=team_id, season_id=season_id)
    return identity


def _seed_stat_row(session_factory, *, player_id, team_id=1, season_id=2026, source_artifact_id, stat_table="PerGame", player_key="frank mitchell", **stat_values):
    with unit_of_work(session_factory) as session:
        PlayerStatSnapshotRepository(session).upsert(
            team_id=team_id, season_id=season_id, source_artifact_id=source_artifact_id, stat_table=stat_table,
            player_key=player_key, source_player_name="Frank Mitchell", validation_status="valid",
            player_id=player_id, **stat_values,
        )


# ---------------------------------------------------------------------------
# Found / not-found / empty-but-known
# ---------------------------------------------------------------------------


def test_player_found_with_one_statistics_record(session_factory):
    _seed_base(session_factory)
    artifact_id = _seed_artifact(session_factory)
    identity = _seed_player(session_factory)
    _seed_stat_row(session_factory, player_id=identity.player_id, source_artifact_id=artifact_id, pts=16.1, trb=9.8, ast=3.0)

    with session_factory() as session:
        result = get_player_raw_statistics(session, identity.player_id)

    assert result.outcome == "found"
    assert result.player_id == identity.player_id
    assert result.display_name == identity.display_name
    assert result.team_id == 1
    assert result.season_id == 2026
    assert len(result.stats) == 1
    assert result.stats[0].pts == 16.1
    assert result.stats[0].trb == 9.8
    assert result.stats[0].ast == 3.0


def test_player_found_with_multiple_records(session_factory):
    _seed_base(session_factory)
    artifact_id = _seed_artifact(session_factory)
    identity = _seed_player(session_factory)
    _seed_stat_row(session_factory, player_id=identity.player_id, source_artifact_id=artifact_id, stat_table="PerGame", pts=16.1)
    _seed_stat_row(session_factory, player_id=identity.player_id, source_artifact_id=artifact_id, stat_table="Totals", pts=450.0)
    _seed_stat_row(session_factory, player_id=identity.player_id, source_artifact_id=artifact_id, stat_table="Advanced", usg_pct=24.5)

    with session_factory() as session:
        result = get_player_raw_statistics(session, identity.player_id)

    assert result.outcome == "found"
    assert len(result.stats) == 3
    assert {s.stat_table for s in result.stats} == {"PerGame", "Totals", "Advanced"}


def test_unknown_player_returns_not_found(session_factory):
    _seed_base(session_factory)
    with session_factory() as session:
        result = get_player_raw_statistics(session, "team:1:season:2026:key:nobody-real")

    assert result == PlayerRawStatisticsResult(outcome="not_found", detail="no database row for this player_id")


def test_known_player_with_no_matching_statistics_is_found_not_missing(session_factory):
    """A known Player with zero stat rows must be distinguishable from an unknown player_id -- found, empty stats, not "not_found"."""
    _seed_base(session_factory)
    identity = _seed_player(session_factory)

    with session_factory() as session:
        result = get_player_raw_statistics(session, identity.player_id)

    assert result.outcome == "found"
    assert result.stats == ()
    assert result.detail is None


# ---------------------------------------------------------------------------
# Filtering and ordering
# ---------------------------------------------------------------------------


def test_deterministic_record_ordering(session_factory):
    _seed_base(session_factory)
    artifact_id = _seed_artifact(session_factory)
    identity = _seed_player(session_factory)
    # Inserted out of natural order on purpose.
    _seed_stat_row(session_factory, player_id=identity.player_id, source_artifact_id=artifact_id, stat_table="Totals")
    _seed_stat_row(session_factory, player_id=identity.player_id, source_artifact_id=artifact_id, stat_table="Advanced")
    _seed_stat_row(session_factory, player_id=identity.player_id, source_artifact_id=artifact_id, stat_table="PerGame")

    with session_factory() as session:
        result_a = get_player_raw_statistics(session, identity.player_id)
    with session_factory() as session:
        result_b = get_player_raw_statistics(session, identity.player_id)

    order_a = [s.stat_table for s in result_a.stats]
    order_b = [s.stat_table for s in result_b.stats]
    assert order_a == order_b == sorted(order_a)  # alphabetical, since season_id/source_artifact_id are constant here


def test_season_filtering(session_factory):
    _seed_base(session_factory, season_id=2025)
    _seed_base(session_factory, season_id=2026)
    artifact_2025 = _seed_artifact(session_factory, season_id=2025, relative_path="test-team/Roster_2025.xlsx", content_hash="hash2025")
    artifact_2026 = _seed_artifact(session_factory, season_id=2026, relative_path="test-team/Roster_2026.xlsx", content_hash="hash2026")
    identity = _seed_player(session_factory, season_id=2026)
    _seed_stat_row(session_factory, player_id=identity.player_id, season_id=2025, source_artifact_id=artifact_2025, stat_table="PerGame", pts=10.0)
    _seed_stat_row(session_factory, player_id=identity.player_id, season_id=2026, source_artifact_id=artifact_2026, stat_table="PerGame", pts=16.1)

    with session_factory() as session:
        result_2025 = get_player_raw_statistics(session, identity.player_id, season_id=2025)
        result_2026 = get_player_raw_statistics(session, identity.player_id, season_id=2026)
        result_all = get_player_raw_statistics(session, identity.player_id)

    assert [s.pts for s in result_2025.stats] == [10.0]
    assert [s.pts for s in result_2026.stats] == [16.1]
    assert len(result_all.stats) == 2


def test_stat_table_filtering(session_factory):
    _seed_base(session_factory)
    artifact_id = _seed_artifact(session_factory)
    identity = _seed_player(session_factory)
    _seed_stat_row(session_factory, player_id=identity.player_id, source_artifact_id=artifact_id, stat_table="PerGame", pts=16.1)
    _seed_stat_row(session_factory, player_id=identity.player_id, source_artifact_id=artifact_id, stat_table="Advanced", usg_pct=24.5)

    with session_factory() as session:
        result = get_player_raw_statistics(session, identity.player_id, stat_table="Advanced")

    assert len(result.stats) == 1
    assert result.stats[0].stat_table == "Advanced"
    assert result.stats[0].usg_pct == 24.5


def test_repository_receives_the_exact_filters_passed(session_factory, monkeypatch):
    _seed_base(session_factory)
    identity = _seed_player(session_factory)

    captured = {}
    original = PlayerStatSnapshotRepository.list_for_player

    def spy(self, player_id, *, season_id=None, stat_table=None):
        captured["player_id"] = player_id
        captured["season_id"] = season_id
        captured["stat_table"] = stat_table
        return original(self, player_id, season_id=season_id, stat_table=stat_table)

    monkeypatch.setattr(PlayerStatSnapshotRepository, "list_for_player", spy)

    with session_factory() as session:
        get_player_raw_statistics(session, identity.player_id, season_id=2026, stat_table="PerGame")

    assert captured == {"player_id": identity.player_id, "season_id": 2026, "stat_table": "PerGame"}


# ---------------------------------------------------------------------------
# Value fidelity
# ---------------------------------------------------------------------------


def test_nullable_fields_remain_none(session_factory):
    _seed_base(session_factory)
    artifact_id = _seed_artifact(session_factory)
    identity = _seed_player(session_factory)
    _seed_stat_row(session_factory, player_id=identity.player_id, source_artifact_id=artifact_id, stat_table="PerGame", pts=16.1)  # usg_pct etc. left unset

    with session_factory() as session:
        result = get_player_raw_statistics(session, identity.player_id)

    record = result.stats[0]
    assert record.usg_pct is None
    assert record.ows is None
    assert record.awards_raw is None


def test_stored_zeros_remain_zero_not_none(session_factory):
    _seed_base(session_factory)
    artifact_id = _seed_artifact(session_factory)
    identity = _seed_player(session_factory)
    _seed_stat_row(session_factory, player_id=identity.player_id, source_artifact_id=artifact_id, stat_table="PerGame", stl=0.0, blk=0.0, tov=0.0)

    with session_factory() as session:
        result = get_player_raw_statistics(session, identity.player_id)

    record = result.stats[0]
    assert record.stl == 0.0
    assert record.blk == 0.0
    assert record.tov == 0.0
    assert record.stl is not None


def test_source_stat_table_values_remain_distinguishable(session_factory):
    _seed_base(session_factory)
    artifact_id = _seed_artifact(session_factory)
    identity = _seed_player(session_factory)
    _seed_stat_row(session_factory, player_id=identity.player_id, source_artifact_id=artifact_id, stat_table="PerGame", pts=16.1)
    _seed_stat_row(session_factory, player_id=identity.player_id, source_artifact_id=artifact_id, stat_table="Per40", pts=20.3)

    with session_factory() as session:
        result = get_player_raw_statistics(session, identity.player_id)

    by_table = {s.stat_table: s.pts for s in result.stats}
    assert by_table == {"PerGame": 16.1, "Per40": 20.3}


def test_no_calculated_ovr_or_derived_statistic_field_exists():
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(PlayerStatRecord)}
    forbidden = {"ovr", "ovr_score", "composite_score", "per_game_derived", "role_consistency"}
    assert field_names.isdisjoint(forbidden)


# ---------------------------------------------------------------------------
# Boundary guarantees
# ---------------------------------------------------------------------------


def test_orm_records_do_not_escape_the_service(session_factory):
    _seed_base(session_factory)
    artifact_id = _seed_artifact(session_factory)
    identity = _seed_player(session_factory)
    _seed_stat_row(session_factory, player_id=identity.player_id, source_artifact_id=artifact_id, pts=16.1)

    with session_factory() as session:
        result = get_player_raw_statistics(session, identity.player_id)

    from src.db.allstats_models import PlayerStatSnapshot
    from src.db.models import Player

    assert not isinstance(result, (Player, PlayerStatSnapshot))
    for record in result.stats:
        assert isinstance(record, PlayerStatRecord)
        assert not isinstance(record, PlayerStatSnapshot)


def test_service_performs_no_write_commit_flush_or_rollback(session_factory):
    _seed_base(session_factory)
    artifact_id = _seed_artifact(session_factory)
    identity = _seed_player(session_factory)
    _seed_stat_row(session_factory, player_id=identity.player_id, source_artifact_id=artifact_id, pts=16.1)

    with session_factory() as session:
        before = [(row.id, row.pts) for row in session.query(_players_stat_model()).all()]

    for _ in range(3):
        with session_factory() as session:
            get_player_raw_statistics(session, identity.player_id, season_id=2026)
            get_player_raw_statistics(session, "unknown-player")

    with session_factory() as session:
        after = [(row.id, row.pts) for row in session.query(_players_stat_model()).all()]

    assert before == after


def _players_stat_model():
    from src.db.allstats_models import PlayerStatSnapshot

    return PlayerStatSnapshot


def test_service_module_imports_no_fastapi_pydantic_or_engine_construction():
    source = inspect.getsource(player_service)
    forbidden = ["fastapi", "pydantic", "create_engine", "sessionmaker", "starlette"]
    for token in forbidden:
        assert token not in source, f"unexpected {token!r} found in src/services/player.py"


def test_repeated_calls_are_deterministic(session_factory):
    _seed_base(session_factory)
    artifact_id = _seed_artifact(session_factory)
    identity = _seed_player(session_factory)
    _seed_stat_row(session_factory, player_id=identity.player_id, source_artifact_id=artifact_id, pts=16.1)

    results = []
    for _ in range(5):
        with session_factory() as session:
            results.append(get_player_raw_statistics(session, identity.player_id))

    assert all(r == results[0] for r in results)
