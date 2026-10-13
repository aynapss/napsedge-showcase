"""
Phase 6, Slice 3A: the smallest real-PostgreSQL proof that the read-only
Team service (`src.services.team`) behaves correctly against a real (not
SQLite-emulated) database. Calls the service directly -- no FastAPI
application is constructed or used anywhere in this file, since the Team
service is not exposed via HTTP in this batch (see docs/phase6_service_api_design.md
and docs/migration_state.md's Phase 6 Slice 3A section).

Reuses the existing disposable-PostgreSQL fixtures (`pg_session_factory`,
`pg_migrated_database_url`) -- no competing container harness is created
here, and no TeamDump/live scraping/notebook/production data is touched.
"""
from __future__ import annotations

import pytest

from src.db.models import Team
from src.db.repositories import ConferenceRepository, TeamRepository
from src.db.session import build_session_factory, unit_of_work
from src.exceptions import IdentityConflictError
from src.identity.contracts import TeamIdentity
from src.identity.team_identity import ensure_team_registry, resolve_team_identity
from src.services.contracts import TeamLookupResult, TeamRecord
from src.services.team import get_team, resolve_team

pytestmark = pytest.mark.postgres


def _real_identity(query: str) -> TeamIdentity:
    ensure_team_registry()
    result = resolve_team_identity(query, source="test")
    assert isinstance(result, TeamIdentity)
    return result


def test_lookup_by_canonical_team_id_against_real_postgresql(pg_migrated_database_url, pg_session_factory):
    identity = _real_identity("St. Bonaventure")
    with unit_of_work(pg_session_factory) as session:
        TeamRepository(session).upsert(
            team_id=identity.team_id,
            display_name=identity.display_name,
            sr_slug=identity.sources["sports_reference"].raw_value,
        )

    with pg_session_factory() as session:
        result = get_team(session, identity.team_id)

    assert result == TeamLookupResult(
        outcome="found",
        team=TeamRecord(
            team_id=identity.team_id,
            canonical_name=identity.display_name,
            canonical_slug=identity.sources["sports_reference"].raw_value,
            conference_name=None,
        ),
    )


def test_resolution_by_canonical_name_against_real_postgresql(pg_migrated_database_url, pg_session_factory):
    identity = _real_identity("St. Bonaventure")
    with unit_of_work(pg_session_factory) as session:
        TeamRepository(session).upsert(
            team_id=identity.team_id,
            display_name=identity.display_name,
            sr_slug=identity.sources["sports_reference"].raw_value,
        )

    with pg_session_factory() as session:
        result = resolve_team(session, "St. Bonaventure")

    assert result.outcome == "found"
    assert result.team.team_id == identity.team_id


def test_resolution_through_a_persisted_source_alias_against_real_postgresql(pg_migrated_database_url, pg_session_factory):
    """The identity resolver's own registry (not the DB's TeamAlias table) supplies the alias -- "st-bonaventure" is a real source-slug-shaped variant."""
    identity = _real_identity("St. Bonaventure")
    with unit_of_work(pg_session_factory) as session:
        TeamRepository(session).upsert(
            team_id=identity.team_id,
            display_name=identity.display_name,
            sr_slug=identity.sources["sports_reference"].raw_value,
        )

    with pg_session_factory() as session:
        result = resolve_team(session, "st-bonaventure")

    assert result.outcome == "found"
    assert result.team.team_id == identity.team_id


def test_unknown_team_by_id_against_real_postgresql(pg_migrated_database_url, pg_session_factory):
    with pg_session_factory() as session:
        result = get_team(session, 999_999_999)
    assert result == TeamLookupResult(outcome="not_found", detail="no database row for this team_id")


def test_unresolved_team_name_against_real_postgresql(pg_migrated_database_url, pg_session_factory):
    with pg_session_factory() as session:
        result = resolve_team(session, "Zzzzznotarealteam99")
    assert result == TeamLookupResult(outcome="not_found", detail="unresolved")


def test_resolved_identity_with_no_persisted_row_against_real_postgresql(pg_migrated_database_url, pg_session_factory):
    """A real, registry-known D1 team never seeded into this disposable database."""
    identity = _real_identity("Davidson")
    with pg_session_factory() as session:
        result = resolve_team(session, "Davidson")
    assert result == TeamLookupResult(outcome="not_found", detail="resolved_but_missing_row")
    assert identity.team_id is not None


def test_ambiguous_team_query_raises_identity_conflict_error_against_real_postgresql(pg_migrated_database_url, pg_session_factory):
    """"Miami" collapses to two real, distinct D1 programs (Miami (FL)/Miami (OH)) -- must never be silently resolved."""
    with pg_session_factory() as session:
        with pytest.raises(IdentityConflictError) as exc_info:
            resolve_team(session, "Miami")
    assert "Miami" in str(exc_info.value)


def test_duplicate_source_directory_upserts_never_produce_a_duplicate_team_record_on_real_postgresql(
    pg_migrated_database_url, pg_session_factory
):
    """Miami (FL): the real duplicate-AllStats-directory team (Phase 5C5/5C6). Two idempotent upserts (simulating two directories), one canonical service result."""
    identity = _real_identity("Miami (FL)")
    with unit_of_work(pg_session_factory) as session:
        repo = TeamRepository(session)
        repo.upsert(team_id=identity.team_id, display_name=identity.display_name, sr_slug=identity.sources["sports_reference"].raw_value)
        repo.upsert(team_id=identity.team_id, display_name=identity.display_name, sr_slug=identity.sources["sports_reference"].raw_value)

    with pg_session_factory() as session:
        count = session.query(Team).filter(Team.team_id == identity.team_id).count()
        assert count == 1

        by_id = get_team(session, identity.team_id)
        by_name = resolve_team(session, "Miami (FL)")

    assert by_id.outcome == by_name.outcome == "found"
    assert by_id.team == by_name.team == TeamRecord(
        team_id=identity.team_id,
        canonical_name=identity.display_name,
        canonical_slug=identity.sources["sports_reference"].raw_value,
        conference_name=None,
    )


def test_conference_relationship_against_real_postgresql(pg_migrated_database_url, pg_session_factory):
    identity = _real_identity("St. Bonaventure")
    with unit_of_work(pg_session_factory) as session:
        ConferenceRepository(session).upsert(conference_id=1, canonical_name="Atlantic 10")
        TeamRepository(session).upsert(
            team_id=identity.team_id,
            display_name=identity.display_name,
            sr_slug=identity.sources["sports_reference"].raw_value,
            conference_id=1,
        )

    with pg_session_factory() as session:
        result = get_team(session, identity.team_id)

    assert result.team.conference_name == "Atlantic 10"


def test_repeated_calls_against_real_postgresql_are_deterministic(pg_migrated_database_url, pg_session_factory):
    identity = _real_identity("St. Bonaventure")
    with unit_of_work(pg_session_factory) as session:
        TeamRepository(session).upsert(
            team_id=identity.team_id,
            display_name=identity.display_name,
            sr_slug=identity.sources["sports_reference"].raw_value,
        )

    with pg_session_factory() as session:
        results = [get_team(session, identity.team_id) for _ in range(5)]
    assert all(r == results[0] for r in results)


def test_service_calls_against_real_postgresql_never_mutate_the_database(pg_migrated_database_url, pg_session_factory):
    from sqlalchemy import text

    identity = _real_identity("St. Bonaventure")
    with unit_of_work(pg_session_factory) as session:
        TeamRepository(session).upsert(
            team_id=identity.team_id,
            display_name=identity.display_name,
            sr_slug=identity.sources["sports_reference"].raw_value,
        )

    with pg_session_factory() as session:
        before = session.execute(text("SELECT team_id, display_name, sr_slug, conference_id FROM teams")).fetchall()

    with pg_session_factory() as session:
        for _ in range(3):
            get_team(session, identity.team_id)
            resolve_team(session, "St. Bonaventure")

    with pg_session_factory() as session:
        after = session.execute(text("SELECT team_id, display_name, sr_slug, conference_id FROM teams")).fetchall()

    assert before == after


def test_resource_cleanup_after_direct_service_use_against_real_postgresql(pg_migrated_database_url):
    """
    Seeds and reads via a short-lived engine that this test disposes
    itself (rather than the long-lived `pg_session_factory` fixture, whose
    own engine is only disposed at fixture teardown, i.e. after this test
    function returns) so the only engine that could hold a connection at
    assertion time is the one this test built and disposed.
    """
    from sqlalchemy import create_engine, text

    identity = _real_identity("St. Bonaventure")
    seed_engine = create_engine(pg_migrated_database_url)
    try:
        seed_session_factory = build_session_factory(seed_engine)
        with unit_of_work(seed_session_factory) as session:
            TeamRepository(session).upsert(
                team_id=identity.team_id,
                display_name=identity.display_name,
                sr_slug=identity.sources["sports_reference"].raw_value,
            )
        with seed_session_factory() as session:
            result = get_team(session, identity.team_id)
        assert result.outcome == "found"
    finally:
        seed_engine.dispose()

    admin_engine = create_engine(pg_migrated_database_url)
    try:
        with admin_engine.connect() as conn:
            active = conn.execute(
                text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()")
            ).scalar()
        assert active == 0
    finally:
        admin_engine.dispose()
