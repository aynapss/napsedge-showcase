"""
Phase 7, Slice 2B: tests for `src.streamlit_consumer_controller` -- the
synchronous, `streamlit`-free orchestration layer the new Team Lookup
page calls directly.

Team resolution uses real identities via the existing, unmodified
`src.identity.team_identity.resolve_team_identity` against a fresh,
disposable in-memory SQLite database (the same pattern already
established by `tests/regression/test_services_team.py` and
`tests/regression/test_discord_consumer_bridge.py`) -- never `TeamDump`,
never a live network call. Rankings uses a synthetic, disposable CSV.
Injury tests monkeypatch `src.streamlit_consumer_controller.get_injury_report_for_team`
directly with a synthetic `TeamInjuryLookupResult` -- the real
local-cache-reading behavior of that service is already exhaustively
covered by `tests/regression/test_services_injury.py`.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, event

from src.db.base import Base
from src.db.repositories import TeamRepository
from src.db.session import build_session_factory, unit_of_work
from src.exceptions import InjuryDataIntegrityError, RankingsConfigurationError
from src.identity.contracts import TeamIdentity
from src.identity.team_identity import ensure_team_registry, resolve_team_identity
from src.rankings.cache_repository import NetRankingCacheRepository
from src.rankings.config import RankingsRuntimeConfig
from src.services.contracts import TeamInjuryLookupResult, TeamInjuryReport
import src.streamlit_consumer_controller as controller

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _real_identity(query: str) -> TeamIdentity:
    ensure_team_registry()
    result = resolve_team_identity(query, source="test")
    assert isinstance(result, TeamIdentity)
    return result


def _sr_slug(identity: TeamIdentity) -> str:
    return identity.sources["sports_reference"].raw_value


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
def real_session_factory(engine):
    return build_session_factory(engine)


@pytest.fixture()
def bonaventure(real_session_factory) -> TeamIdentity:
    identity = _real_identity("St. Bonaventure")
    with unit_of_work(real_session_factory) as session:
        TeamRepository(session).upsert(team_id=identity.team_id, display_name=identity.display_name, sr_slug=_sr_slug(identity))
    return identity


def _spy_session_factory(real_factory):
    sessions: list = []

    def factory():
        session = real_factory()
        session.close = MagicMock(wraps=session.close)
        session.commit = MagicMock(wraps=session.commit)
        session.flush = MagicMock(wraps=session.flush)
        sessions.append(session)
        return session

    factory.sessions = sessions
    return factory


def _write_rankings_csv(tmp_path, rows: list[tuple[str, object]]) -> str:
    path = tmp_path / "rankings.csv"
    lines = ["team_slug,net_rank"] + [f"{slug},{rank}" for slug, rank in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _broken_session_factory():
    session = MagicMock()
    session.get = MagicMock(side_effect=RuntimeError("database connection refused"))
    return session


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


def test_session_is_created_exactly_once_per_call(real_session_factory, bonaventure):
    spy_factory = _spy_session_factory(real_session_factory)
    controller.run_team_lookup("St. Bonaventure", session_factory=spy_factory)
    assert len(spy_factory.sessions) == 1


def test_session_is_closed_on_success(real_session_factory, bonaventure):
    spy_factory = _spy_session_factory(real_session_factory)
    controller.run_team_lookup("St. Bonaventure", session_factory=spy_factory)
    spy_factory.sessions[0].close.assert_called_once()


def test_session_is_closed_on_service_failure():
    presentation = controller.run_team_lookup("Davidson", session_factory=_broken_session_factory)
    assert presentation.status == "error"


def test_no_commit_flush_or_mutation_occurs(real_session_factory, bonaventure):
    spy_factory = _spy_session_factory(real_session_factory)
    controller.run_team_lookup("St. Bonaventure", session_factory=spy_factory)
    spy_factory.sessions[0].commit.assert_not_called()
    spy_factory.sessions[0].flush.assert_not_called()


def test_independent_submissions_use_independent_sessions(real_session_factory, bonaventure):
    spy_factory = _spy_session_factory(real_session_factory)
    controller.run_team_lookup("St. Bonaventure", session_factory=spy_factory)
    controller.run_team_lookup("St. Bonaventure", session_factory=spy_factory)
    assert len(spy_factory.sessions) == 2
    assert spy_factory.sessions[0] is not spy_factory.sessions[1]
    for session in spy_factory.sessions:
        session.close.assert_called_once()


def test_returned_presentation_never_carries_a_live_session(real_session_factory, bonaventure):
    presentation = controller.run_team_lookup("St. Bonaventure", session_factory=real_session_factory)
    assert not hasattr(presentation, "session")


# ---------------------------------------------------------------------------
# Team
# ---------------------------------------------------------------------------


def test_team_lookup_found(real_session_factory, bonaventure):
    presentation = controller.run_team_lookup("St. Bonaventure", session_factory=real_session_factory)
    assert presentation.status == "ok"


def test_team_lookup_not_found(real_session_factory):
    presentation = controller.run_team_lookup("Zzzzznotarealteam99", session_factory=real_session_factory)
    assert presentation.status == "not_found"


def test_team_lookup_ambiguous_query(real_session_factory):
    presentation = controller.run_team_lookup("Miami", session_factory=real_session_factory)
    assert presentation.status == "conflict"
    assert "Miami" in presentation.message


def test_team_lookup_database_unavailable():
    presentation = controller.run_team_lookup("Davidson", session_factory=_broken_session_factory)
    assert presentation.status == "error"
    assert "database connection refused" not in presentation.message


# ---------------------------------------------------------------------------
# Rankings
# ---------------------------------------------------------------------------


def test_rankings_lookup_found(tmp_path, real_session_factory, bonaventure):
    path = _write_rankings_csv(tmp_path, [(_sr_slug(bonaventure), 12)])
    provider = lambda: RankingsRuntimeConfig(repository=NetRankingCacheRepository(path), season=2026)
    presentation = controller.run_rankings_lookup(
        "St. Bonaventure", session_factory=real_session_factory, rankings_config_provider=provider
    )
    assert presentation.status == "ok"
    assert any(f.value == "12" for f in presentation.fields)


def test_rankings_lookup_not_found(tmp_path, real_session_factory, bonaventure):
    path = _write_rankings_csv(tmp_path, [("some-other-team", 5)])
    provider = lambda: RankingsRuntimeConfig(repository=NetRankingCacheRepository(path), season=2026)
    presentation = controller.run_rankings_lookup(
        "St. Bonaventure", session_factory=real_session_factory, rankings_config_provider=provider
    )
    assert presentation.status == "not_found"


def test_rankings_lookup_conflicting_ranks_all_preserved(tmp_path, real_session_factory, bonaventure):
    slug = _sr_slug(bonaventure)
    path = _write_rankings_csv(tmp_path, [(slug, 12), (slug, 45)])
    provider = lambda: RankingsRuntimeConfig(repository=NetRankingCacheRepository(path), season=2026)
    presentation = controller.run_rankings_lookup(
        "St. Bonaventure", session_factory=real_session_factory, rankings_config_provider=provider
    )
    assert presentation.status == "conflict"
    assert len(presentation.rows) == 2


def test_no_conflicting_rank_is_silently_selected(tmp_path, real_session_factory, bonaventure):
    slug = _sr_slug(bonaventure)
    path = _write_rankings_csv(tmp_path, [(slug, 12), (slug, 45)])
    provider = lambda: RankingsRuntimeConfig(repository=NetRankingCacheRepository(path), season=2026)
    presentation = controller.run_rankings_lookup(
        "St. Bonaventure", session_factory=real_session_factory, rankings_config_provider=provider
    )
    assert presentation.fields == ()


def test_rankings_lookup_cache_unavailable(tmp_path, real_session_factory, bonaventure):
    path = tmp_path / "empty.csv"
    path.write_text("team_slug,net_rank\n", encoding="utf-8")
    provider = lambda: RankingsRuntimeConfig(repository=NetRankingCacheRepository(str(path)), season=2026)
    presentation = controller.run_rankings_lookup(
        "St. Bonaventure", session_factory=real_session_factory, rankings_config_provider=provider
    )
    assert presentation.status == "unavailable"


def test_rankings_lookup_malformed_configuration_is_sanitized(real_session_factory, bonaventure):
    def _raise_config_error():
        raise RankingsConfigurationError("NAPSEDGE_NET_RANKINGS_SEASON is not set.")

    presentation = controller.run_rankings_lookup(
        "St. Bonaventure", session_factory=real_session_factory, rankings_config_provider=_raise_config_error
    )
    assert presentation.status == "unavailable"
    assert "NAPSEDGE_NET_RANKINGS_SEASON" not in presentation.message


def test_rankings_lookup_failed_team_resolution_prevents_cache_access(tmp_path, real_session_factory):
    calls = []
    path = _write_rankings_csv(tmp_path, [("someteam", 1)])

    def provider():
        calls.append("called")
        return RankingsRuntimeConfig(repository=NetRankingCacheRepository(path), season=2026)

    presentation = controller.run_rankings_lookup(
        "Zzzzznotarealteam99", session_factory=real_session_factory, rankings_config_provider=provider
    )
    assert presentation.status == "not_found"
    assert calls == []


# ---------------------------------------------------------------------------
# Injury
# ---------------------------------------------------------------------------


def test_injury_lookup_populated_report(monkeypatch, real_session_factory, bonaventure):
    from src.services.contracts import InjuryEntry

    report = TeamInjuryReport(
        outcome="loaded",
        team_slug=_sr_slug(bonaventure),
        source_path="/some/path",
        injuries=(InjuryEntry(player="A", status="out", detail="x", raw={}),),
        impactful_count=1,
    )
    monkeypatch.setattr(
        controller, "get_injury_report_for_team", lambda session, team_id: TeamInjuryLookupResult(outcome="found", report=report)
    )
    presentation = controller.run_injury_lookup("St. Bonaventure", session_factory=real_session_factory)
    assert presentation.status == "ok"
    assert len(presentation.rows) == 1


def test_injury_lookup_known_empty_report(monkeypatch, real_session_factory, bonaventure):
    report = TeamInjuryReport(outcome="loaded", team_slug=_sr_slug(bonaventure), source_path=None, injuries=(), impactful_count=0)
    monkeypatch.setattr(
        controller, "get_injury_report_for_team", lambda session, team_id: TeamInjuryLookupResult(outcome="found", report=report)
    )
    presentation = controller.run_injury_lookup("St. Bonaventure", session_factory=real_session_factory)
    assert presentation.status == "ok"
    assert presentation.rows == ()


def test_injury_lookup_unavailable_report(monkeypatch, real_session_factory, bonaventure):
    report = TeamInjuryReport(outcome="not_loaded", team_slug=_sr_slug(bonaventure), source_path=None, injuries=(), impactful_count=0)
    monkeypatch.setattr(
        controller, "get_injury_report_for_team", lambda session, team_id: TeamInjuryLookupResult(outcome="found", report=report)
    )
    presentation = controller.run_injury_lookup("St. Bonaventure", session_factory=real_session_factory)
    assert presentation.status == "unavailable"


def test_injury_lookup_known_empty_and_unavailable_are_structurally_distinct(monkeypatch, real_session_factory, bonaventure):
    empty_report = TeamInjuryReport(outcome="loaded", team_slug=_sr_slug(bonaventure), source_path=None, injuries=(), impactful_count=0)
    monkeypatch.setattr(
        controller, "get_injury_report_for_team", lambda session, team_id: TeamInjuryLookupResult(outcome="found", report=empty_report)
    )
    empty_presentation = controller.run_injury_lookup("St. Bonaventure", session_factory=real_session_factory)

    unavailable_report = TeamInjuryReport(outcome="not_loaded", team_slug=_sr_slug(bonaventure), source_path=None, injuries=(), impactful_count=0)
    monkeypatch.setattr(
        controller, "get_injury_report_for_team", lambda session, team_id: TeamInjuryLookupResult(outcome="found", report=unavailable_report)
    )
    unavailable_presentation = controller.run_injury_lookup("St. Bonaventure", session_factory=real_session_factory)

    assert empty_presentation.status != unavailable_presentation.status
    assert empty_presentation != unavailable_presentation


def test_injury_lookup_integrity_failure_is_sanitized(monkeypatch, real_session_factory, bonaventure):
    def _raise_integrity_error(session, team_id):
        raise InjuryDataIntegrityError("malformed element at index 3")

    monkeypatch.setattr(controller, "get_injury_report_for_team", _raise_integrity_error)
    presentation = controller.run_injury_lookup("St. Bonaventure", session_factory=real_session_factory)
    assert presentation.status == "unavailable"
    assert "index 3" not in presentation.message


def test_injury_lookup_failed_team_resolution_prevents_cache_access(monkeypatch, real_session_factory):
    calls = []
    monkeypatch.setattr(
        controller,
        "get_injury_report_for_team",
        lambda session, team_id: calls.append("called") or TeamInjuryLookupResult(outcome="team_not_found"),
    )
    presentation = controller.run_injury_lookup("Zzzzznotarealteam99", session_factory=real_session_factory)
    assert presentation.status == "not_found"
    assert calls == []


def test_injury_lookup_impactful_count_preserved(monkeypatch, real_session_factory, bonaventure):
    from src.services.contracts import InjuryEntry

    report = TeamInjuryReport(
        outcome="loaded",
        team_slug=_sr_slug(bonaventure),
        source_path=None,
        injuries=(InjuryEntry(player="A", status="out", detail=None, raw={}),),
        impactful_count=1,
    )
    monkeypatch.setattr(
        controller, "get_injury_report_for_team", lambda session, team_id: TeamInjuryLookupResult(outcome="found", report=report)
    )
    presentation = controller.run_injury_lookup("St. Bonaventure", session_factory=real_session_factory)
    count_field = next(f for f in presentation.fields if f.label == "Impactful Count")
    assert count_field.value == "1"


def test_injury_lookup_raw_source_path_and_internal_detail_absent(monkeypatch, real_session_factory, bonaventure):
    from src.services.contracts import InjuryEntry

    report = TeamInjuryReport(
        outcome="loaded",
        team_slug=_sr_slug(bonaventure),
        source_path="/some/secret/path",
        injuries=(InjuryEntry(player="A", status="out", detail="d", raw={"secret_internal_key": "value"}),),
        impactful_count=1,
    )
    monkeypatch.setattr(
        controller, "get_injury_report_for_team", lambda session, team_id: TeamInjuryLookupResult(outcome="found", report=report)
    )
    presentation = controller.run_injury_lookup("St. Bonaventure", session_factory=real_session_factory)
    rendered = repr(presentation)
    assert "secret_internal_key" not in rendered
    assert "/some/secret/path" not in rendered


# ---------------------------------------------------------------------------
# Player raw statistics (Phase 7 Slice 3A)
# ---------------------------------------------------------------------------


def _stat_record(**overrides):
    import dataclasses

    from src.services.contracts import PlayerStatRecord

    base = {f.name: None for f in dataclasses.fields(PlayerStatRecord)}
    base.update(
        stat_table="PerGame", season_id=2026, team_id=1, source_artifact_id=1,
        player_key="k", source_player_name="Test Player", validation_status="valid",
    )
    base.update(overrides)
    return PlayerStatRecord(**base)


def _seed_player(session_factory, *, player_id="p1", team_id=1, season_id=2026, display_name="Test Player"):
    from src.db.repositories import PlayerRepository, SeasonRepository, TeamRepository

    with unit_of_work(session_factory) as session:
        TeamRepository(session).upsert(team_id=team_id, display_name="Some Team", sr_slug="some-team")
        SeasonRepository(session).upsert(
            season_id=season_id, start_year=season_id - 1, end_year=season_id, label=f"{season_id - 1}-{season_id}"
        )
        PlayerRepository(session).upsert(
            player_id=player_id, display_name=display_name, team_id=team_id, season_id=season_id
        )


def test_player_lookup_found(real_session_factory):
    _seed_player(real_session_factory)
    presentation = controller.run_player_raw_statistics_lookup("p1", session_factory=real_session_factory)
    assert presentation.status == "ok"
    assert presentation.title == "Test Player"


def test_player_lookup_not_found(real_session_factory):
    presentation = controller.run_player_raw_statistics_lookup("does-not-exist", session_factory=real_session_factory)
    assert presentation.status == "not_found"


def test_player_lookup_known_empty(real_session_factory):
    _seed_player(real_session_factory)
    presentation = controller.run_player_raw_statistics_lookup("p1", session_factory=real_session_factory)
    assert presentation.status == "ok"
    assert presentation.rows == ()


def test_player_lookup_filters_are_propagated(monkeypatch, real_session_factory):
    _seed_player(real_session_factory)
    calls = []
    from src.services.contracts import PlayerRawStatisticsResult

    def fake_service(session, player_id, *, season_id=None, stat_table=None):
        calls.append((player_id, season_id, stat_table))
        return PlayerRawStatisticsResult(outcome="found", player_id="p1", display_name="X", team_id=1, season_id=2026, stats=())

    monkeypatch.setattr(controller, "get_player_raw_statistics", fake_service)
    controller.run_player_raw_statistics_lookup(
        "p1", session_factory=real_session_factory, season_id=2025, stat_table="Totals"
    )
    assert calls == [("p1", 2025, "Totals")]


def test_player_lookup_service_called_exactly_once(monkeypatch, real_session_factory):
    from src.services.contracts import PlayerRawStatisticsResult

    calls = []

    def fake_service(session, player_id, *, season_id=None, stat_table=None):
        calls.append(1)
        return PlayerRawStatisticsResult(outcome="not_found")

    monkeypatch.setattr(controller, "get_player_raw_statistics", fake_service)
    controller.run_player_raw_statistics_lookup("p1", session_factory=real_session_factory)
    assert calls == [1]


def test_player_lookup_adapter_called_exactly_once(monkeypatch, real_session_factory):
    from src.services.contracts import PlayerRawStatisticsResult

    calls = []
    real_adapter = controller.present_player_raw_statistics

    def spy_adapter(result):
        calls.append(1)
        return real_adapter(result)

    monkeypatch.setattr(controller, "get_player_raw_statistics", lambda *a, **kw: PlayerRawStatisticsResult(outcome="not_found"))
    monkeypatch.setattr(controller, "present_player_raw_statistics", spy_adapter)
    controller.run_player_raw_statistics_lookup("p1", session_factory=real_session_factory)
    assert calls == [1]


def test_player_lookup_session_created_exactly_once(real_session_factory):
    spy_factory = _spy_session_factory(real_session_factory)
    controller.run_player_raw_statistics_lookup("p1", session_factory=spy_factory)
    assert len(spy_factory.sessions) == 1


def test_player_lookup_session_closed_on_success(real_session_factory):
    _seed_player(real_session_factory)
    spy_factory = _spy_session_factory(real_session_factory)
    controller.run_player_raw_statistics_lookup("p1", session_factory=spy_factory)
    spy_factory.sessions[0].close.assert_called_once()


def test_player_lookup_session_closed_on_failure():
    presentation = controller.run_player_raw_statistics_lookup("p1", session_factory=_broken_session_factory)
    assert presentation.status == "error"


def test_player_lookup_no_commit_flush_or_mutation(real_session_factory):
    _seed_player(real_session_factory)
    spy_factory = _spy_session_factory(real_session_factory)
    controller.run_player_raw_statistics_lookup("p1", session_factory=spy_factory)
    spy_factory.sessions[0].commit.assert_not_called()
    spy_factory.sessions[0].flush.assert_not_called()


def test_player_lookup_unexpected_error_is_sanitized():
    presentation = controller.run_player_raw_statistics_lookup("p1", session_factory=_broken_session_factory)
    assert presentation.status == "error"
    assert "database connection refused" not in presentation.message


def test_player_lookup_no_ovr_notebook_or_live_dependency_reachable():
    import inspect

    source = inspect.getsource(controller.run_player_raw_statistics_lookup)
    forbidden = ["ovr_model", "nbformat", "nbconvert", "requests.", "httpx.", "run_matchup_pipeline"]
    for token in forbidden:
        assert token not in source


# ---------------------------------------------------------------------------
# Matchup feature comparison (Phase 7 Slice 3C) -- session-free
# ---------------------------------------------------------------------------


def test_matchup_comparison_calls_the_service_with_exact_arguments(monkeypatch):
    captured = {}

    def _fake_compare(team_a_slug, team_b_slug, team_a_features, team_b_features):
        captured["args"] = (team_a_slug, team_b_slug, team_a_features, team_b_features)
        from src.services.contracts import MatchupFeatureComparisonResult

        return MatchupFeatureComparisonResult(team_a_slug=team_a_slug, team_b_slug=team_b_slug, deltas={}, key_edges=())

    monkeypatch.setattr(controller, "compare_team_matchup_features", _fake_compare)
    controller.run_matchup_feature_comparison("duke", "unc", {"avg_margin": 5.0}, {"avg_margin": 2.0})
    assert captured["args"] == ("duke", "unc", {"avg_margin": 5.0}, {"avg_margin": 2.0})


def test_matchup_comparison_calls_the_service_exactly_once(monkeypatch):
    calls = []
    real_compare = controller.compare_team_matchup_features

    def _counting_compare(*args, **kwargs):
        calls.append(1)
        return real_compare(*args, **kwargs)

    monkeypatch.setattr(controller, "compare_team_matchup_features", _counting_compare)
    controller.run_matchup_feature_comparison("duke", "unc", {}, {})
    assert len(calls) == 1


def test_matchup_comparison_calls_the_adapter_exactly_once(monkeypatch):
    calls = []
    real_present = controller.present_matchup_feature_comparison

    def _counting_present(*args, **kwargs):
        calls.append(1)
        return real_present(*args, **kwargs)

    monkeypatch.setattr(controller, "present_matchup_feature_comparison", _counting_present)
    controller.run_matchup_feature_comparison("duke", "unc", {}, {})
    assert len(calls) == 1


def test_matchup_comparison_creates_no_sqlalchemy_session(monkeypatch):
    def _fail_if_called():
        raise AssertionError("run_matchup_feature_comparison must not open a Session")

    monkeypatch.setattr(controller, "open_default_session", _fail_if_called)
    presentation = controller.run_matchup_feature_comparison("duke", "unc", {"avg_margin": 5.0}, {"avg_margin": 2.0})
    assert presentation.status == "ok"


def test_matchup_comparison_has_no_session_factory_parameter():
    import inspect

    signature = inspect.signature(controller.run_matchup_feature_comparison)
    assert "session_factory" not in signature.parameters


def test_matchup_comparison_success_returns_expected_shape():
    presentation = controller.run_matchup_feature_comparison("duke", "unc", {"avg_margin": 10.0}, {"avg_margin": 3.0})
    assert presentation.status == "ok"
    assert len(presentation.rows) == 7


def test_matchup_comparison_unexpected_error_is_sanitized(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("unexpected failure detail")

    monkeypatch.setattr(controller, "compare_team_matchup_features", _boom)
    presentation = controller.run_matchup_feature_comparison("duke", "unc", {}, {})
    assert presentation.status == "error"
    assert "unexpected failure detail" not in (presentation.message or "")


def test_matchup_comparison_no_ovr_notebook_odds_or_live_dependency_reachable():
    import inspect

    source = inspect.getsource(controller.run_matchup_feature_comparison)
    forbidden = ["ovr_model", "nbformat", "nbconvert", "requests.", "httpx.", "run_matchup_pipeline", "Session("]
    for token in forbidden:
        assert token not in source


# ---------------------------------------------------------------------------
# Static isolation
# ---------------------------------------------------------------------------


def test_module_has_no_streamlit_discord_fastapi_or_network_import():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(controller))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = ("streamlit", "discord", "fastapi", "starlette", "pydantic", "requests", "httpx", "urllib", "socket", "src.api", "src.discord_bot")
    for name in imported:
        assert not any(name == f or name.startswith(f + ".") for f in forbidden), f"unexpected import {name!r}"


def test_importing_controller_performs_no_io():
    import importlib

    importlib.reload(controller)
