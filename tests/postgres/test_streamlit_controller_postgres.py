"""
Phase 7, Slice 2B: the smallest genuine real-PostgreSQL proof that
`src.streamlit_consumer_controller` behaves correctly against a real
(not SQLite-emulated) database -- calls the controller's `run_*`
functions directly, never through a live Streamlit server. Reuses the
existing disposable-PostgreSQL fixtures (`pg_session_factory`,
`pg_migrated_database_url`) -- no competing container harness is
created here, and no `TeamDump`/live scraping/production data is
touched.

Proves the same four things `tests/postgres/test_discord_bridge_postgres.py`
already proved for the Discord bridge, now for the Streamlit controller:
Team resolution through the controller; per-action session ownership
and closure; Injury Team lookup (the identity-to-injury-service wiring);
and Rankings Team resolution before the local cache lookup runs.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from src.db.repositories import TeamRepository
from src.db.session import unit_of_work
from src.identity.contracts import TeamIdentity
from src.identity.team_identity import ensure_team_registry, resolve_team_identity
from src.rankings.cache_repository import NetRankingCacheRepository
from src.rankings.config import RankingsRuntimeConfig
import src.streamlit_consumer_controller as controller

pytestmark = pytest.mark.postgres


def _real_identity(query: str) -> TeamIdentity:
    ensure_team_registry()
    result = resolve_team_identity(query, source="test")
    assert isinstance(result, TeamIdentity)
    return result


def _sr_slug(identity: TeamIdentity) -> str:
    return identity.sources["sports_reference"].raw_value


def test_team_lookup_resolves_through_the_controller_against_real_postgresql(pg_migrated_database_url, pg_session_factory):
    identity = _real_identity("St. Bonaventure")
    with unit_of_work(pg_session_factory) as session:
        TeamRepository(session).upsert(team_id=identity.team_id, display_name=identity.display_name, sr_slug=_sr_slug(identity))

    presentation = controller.run_team_lookup("St. Bonaventure", session_factory=pg_session_factory)

    assert presentation.status == "ok"
    assert presentation.title == identity.display_name


def test_session_is_owned_and_closed_per_action_against_real_postgresql(pg_migrated_database_url, pg_session_factory):
    identity = _real_identity("St. Bonaventure")
    with unit_of_work(pg_session_factory) as session:
        TeamRepository(session).upsert(team_id=identity.team_id, display_name=identity.display_name, sr_slug=_sr_slug(identity))

    opened_sessions = []

    def spying_factory():
        session = pg_session_factory()
        session.close = MagicMock(wraps=session.close)
        opened_sessions.append(session)
        return session

    controller.run_team_lookup("St. Bonaventure", session_factory=spying_factory)

    assert len(opened_sessions) == 1
    opened_sessions[0].close.assert_called_once()

    with pg_session_factory() as check_session:
        active = check_session.execute(
            text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()")
        ).scalar()
    assert active == 0


def test_injury_lookup_reaches_the_injury_service_for_a_resolved_team_against_real_postgresql(
    pg_migrated_database_url, pg_session_factory
):
    identity = _real_identity("Davidson")
    with unit_of_work(pg_session_factory) as session:
        TeamRepository(session).upsert(team_id=identity.team_id, display_name=identity.display_name, sr_slug=_sr_slug(identity))

    presentation = controller.run_injury_lookup("Davidson", session_factory=pg_session_factory)

    assert presentation.status in ("ok", "unavailable")


def test_rankings_lookup_resolves_team_before_touching_local_cache_against_real_postgresql(
    pg_migrated_database_url, pg_session_factory, tmp_path
):
    identity = _real_identity("St. Bonaventure")
    slug = _sr_slug(identity)
    with unit_of_work(pg_session_factory) as session:
        TeamRepository(session).upsert(team_id=identity.team_id, display_name=identity.display_name, sr_slug=slug)

    csv_path = tmp_path / "rankings.csv"
    csv_path.write_text(f"team_slug,net_rank\n{slug},9\n", encoding="utf-8")
    provider = lambda: RankingsRuntimeConfig(repository=NetRankingCacheRepository(str(csv_path)), season=2026)

    presentation = controller.run_rankings_lookup(
        "St. Bonaventure", session_factory=pg_session_factory, rankings_config_provider=provider
    )

    assert presentation.status == "ok"
    assert any(field.value == str(identity.team_id) for field in presentation.fields)
    assert any(field.value == "9" for field in presentation.fields)
