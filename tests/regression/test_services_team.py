"""
Phase 6, Slice 3A: unit tests for `src.services.team`, the read-only Team
application service (`get_team`, `resolve_team`). Not exposed via any HTTP
route in this batch -- Team HTTP endpoints and the authentication decision
they depend on are deferred to a separate, not-yet-approved Slice 3B.

Uses real team identities resolved via the existing, unmodified
`src.identity.team_identity.resolve_team_identity` (the same authoritative
resolver the service itself calls) against a fresh, disposable in-memory
SQLite database -- never `TeamDump`, never a live network call, never the
notebook's card-generation logic. Reading the notebook's static
`conferences = {...}` literal (via `ensure_team_registry()`) is the
existing, already-tested cold-start cost of this identity module itself
(see docs/migration_state.md Phase 2A) -- not something this batch adds.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from src.db.base import Base
from src.db.session import build_session_factory, unit_of_work
from src.db.repositories import ConferenceRepository, TeamRepository
from src.db.models import Team
from src.exceptions import IdentityConflictError
from src.identity.contracts import TeamIdentity
from src.identity.team_identity import ensure_team_registry, resolve_team_identity
from src.services.contracts import TeamLookupResult, TeamRecord
from src.services.team import get_team, resolve_team


def _real_identity(query: str) -> TeamIdentity:
    ensure_team_registry()
    result = resolve_team_identity(query, source="test")
    assert isinstance(result, TeamIdentity), f"{query!r} was expected to resolve cleanly, got {result}"
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
def session_factory(engine):
    return build_session_factory(engine)


@pytest.fixture()
def bonaventure(session_factory) -> TeamIdentity:
    identity = _real_identity("St. Bonaventure")
    with unit_of_work(session_factory) as session:
        TeamRepository(session).upsert(team_id=identity.team_id, display_name=identity.display_name, sr_slug=_sr_slug(identity))
    return identity


@pytest.fixture()
def bonaventure_with_conference(session_factory) -> tuple[TeamIdentity, str]:
    identity = _real_identity("St. Bonaventure")
    conference_name = identity.conference or "Atlantic 10"
    with unit_of_work(session_factory) as session:
        ConferenceRepository(session).upsert(conference_id=1, canonical_name=conference_name)
        TeamRepository(session).upsert(
            team_id=identity.team_id, display_name=identity.display_name, sr_slug=_sr_slug(identity), conference_id=1
        )
    return identity, conference_name


def _open_session(session_factory) -> Session:
    return session_factory()


# ---------------------------------------------------------------------------
# get_team -- canonical lookup by team_id
# ---------------------------------------------------------------------------


def test_get_team_by_id_found(session_factory, bonaventure):
    session = _open_session(session_factory)
    try:
        result = get_team(session, bonaventure.team_id)
        assert result == TeamLookupResult(
            outcome="found",
            team=TeamRecord(
                team_id=bonaventure.team_id,
                canonical_name=bonaventure.display_name,
                canonical_slug=_sr_slug(bonaventure),
                conference_name=None,
            ),
        )
    finally:
        session.close()


def test_get_team_by_id_not_found(session_factory):
    session = _open_session(session_factory)
    try:
        result = get_team(session, 999_999_999)
        assert result.outcome == "not_found"
        assert result.team is None
        assert result.detail == "no database row for this team_id"
    finally:
        session.close()


def test_get_team_conference_present(session_factory, bonaventure_with_conference):
    identity, conference_name = bonaventure_with_conference
    session = _open_session(session_factory)
    try:
        result = get_team(session, identity.team_id)
        assert result.outcome == "found"
        assert result.team.conference_name == conference_name
    finally:
        session.close()


def test_get_team_conference_unavailable_is_none_not_fabricated(session_factory, bonaventure):
    session = _open_session(session_factory)
    try:
        result = get_team(session, bonaventure.team_id)
        assert result.team.conference_name is None
    finally:
        session.close()


# ---------------------------------------------------------------------------
# resolve_team -- name / slug / alias resolution, then DB retrieval
# ---------------------------------------------------------------------------


def test_resolve_team_by_canonical_name(session_factory, bonaventure):
    session = _open_session(session_factory)
    try:
        result = resolve_team(session, "St. Bonaventure")
        assert result.outcome == "found"
        assert result.team.team_id == bonaventure.team_id
    finally:
        session.close()


def test_resolve_team_by_slug_variant(session_factory, bonaventure):
    session = _open_session(session_factory)
    try:
        result = resolve_team(session, "st-bonaventure")
        assert result.outcome == "found"
        assert result.team.team_id == bonaventure.team_id
    finally:
        session.close()


def test_resolve_team_by_alias_case_and_punctuation_variant(session_factory, bonaventure):
    session = _open_session(session_factory)
    try:
        result = resolve_team(session, "ST. BONAVENTURE")
        assert result.outcome == "found"
        assert result.team.team_id == bonaventure.team_id
    finally:
        session.close()


def test_resolve_team_unresolved(session_factory):
    session = _open_session(session_factory)
    try:
        result = resolve_team(session, "Zzzzznotarealteam99")
        assert result.outcome == "not_found"
        assert result.team is None
        assert result.detail == "unresolved"
    finally:
        session.close()


def test_resolve_team_ambiguous_query_raises_identity_conflict_error(session_factory):
    """"Miami" collapses to two real, distinct D1 programs (Miami (FL)/Miami (OH)) -- must never be silently resolved."""
    session = _open_session(session_factory)
    try:
        with pytest.raises(IdentityConflictError) as exc_info:
            resolve_team(session, "Miami")
        message = str(exc_info.value)
        assert "Miami" in message
    finally:
        session.close()


def test_resolve_team_identity_resolved_but_database_row_absent(session_factory):
    """A real, registry-known D1 team that was never seeded into this database -- distinct from "unresolved"."""
    identity = _real_identity("Davidson")
    session = _open_session(session_factory)
    try:
        result = resolve_team(session, "Davidson")
        assert result.outcome == "not_found"
        assert result.team is None
        assert result.detail == "resolved_but_missing_row"
        assert identity.team_id is not None  # sanity: the identity really did resolve
    finally:
        session.close()


def test_resolve_team_never_fabricates_a_value_for_an_unmapped_conference(session_factory, bonaventure):
    session = _open_session(session_factory)
    try:
        result = resolve_team(session, "St. Bonaventure")
        assert result.team.conference_name is None
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Duplicate source directories -> one canonical Team, never a duplicate
# ---------------------------------------------------------------------------


def test_duplicate_source_directory_upserts_never_produce_a_duplicate_team_record(session_factory):
    """
    Miami (FL) is one of the three real teams with two cell-identical
    AllStats source directories (Phase 5C5/5C6 finding). Simulates two
    independent import runs from two different directories for the same
    real team -- both upsert the SAME team_id idempotently, exactly as the
    real bulk-import pipeline does -- and confirms exactly one Team row and
    one canonical API-shaped response result, never two.
    """
    identity = _real_identity("Miami (FL)")
    with unit_of_work(session_factory) as session:
        repo = TeamRepository(session)
        repo.upsert(team_id=identity.team_id, display_name=identity.display_name, sr_slug=_sr_slug(identity))
        repo.upsert(team_id=identity.team_id, display_name=identity.display_name, sr_slug=_sr_slug(identity))

    session = _open_session(session_factory)
    try:
        row_count = session.query(Team).filter(Team.team_id == identity.team_id).count()
        assert row_count == 1

        by_id = get_team(session, identity.team_id)
        by_name = resolve_team(session, "Miami (FL)")
        assert by_id.outcome == by_name.outcome == "found"
        assert by_id.team == by_name.team == TeamRecord(
            team_id=identity.team_id,
            canonical_name=identity.display_name,
            canonical_slug=_sr_slug(identity),
            conference_name=None,
        )
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Read-only guarantee
# ---------------------------------------------------------------------------


def test_get_team_and_resolve_team_never_mutate_the_database(session_factory, bonaventure):
    session = _open_session(session_factory)
    try:
        before = [(t.team_id, t.display_name, t.sr_slug, t.conference_id) for t in session.query(Team).all()]

        for _ in range(3):
            get_team(session, bonaventure.team_id)
            resolve_team(session, "St. Bonaventure")

        after = [(t.team_id, t.display_name, t.sr_slug, t.conference_id) for t in session.query(Team).all()]
        assert before == after
    finally:
        session.close()


def test_repeated_lookups_return_identical_results(session_factory, bonaventure):
    session = _open_session(session_factory)
    try:
        results = [get_team(session, bonaventure.team_id) for _ in range(5)]
        assert all(r == results[0] for r in results)
    finally:
        session.close()
