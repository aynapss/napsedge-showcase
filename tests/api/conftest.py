"""Shared fixtures for `src.api` route-level tests (Phase 6B/Slice 4B)."""
from __future__ import annotations

import hashlib
import json

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from src.api.deps import RANKINGS_CACHE_PATH_ENV_VAR, RANKINGS_SEASON_ENV_VAR
from src.api.main import create_app
from src.auth.api_keys import ENV_VAR as AUTH_ENV_VAR
from src.db.allstats_repositories import PlayerStatSnapshotRepository
from src.db.repositories import DataSourceRepository, PlayerRepository, SeasonRepository, SourceArtifactRepository, TeamRepository
from src.db.session import build_session_factory, unit_of_work
from src.identity.contracts import TeamIdentity
from src.identity.player_identity import build_player_identity
from src.identity.team_identity import ensure_team_registry, resolve_team_identity


def _alembic_config(db_path) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{db_path.as_posix()}")
    return cfg


@pytest.fixture()
def migrated_db_url(tmp_path) -> str:
    """A disposable SQLite database migrated to head -- never a shared file."""
    db_path = tmp_path / "api_test.db"
    command.upgrade(_alembic_config(db_path), "head")
    return f"sqlite+pysqlite:///{db_path.as_posix()}"


@pytest.fixture()
def unreachable_db_url(tmp_path) -> str:
    """A URL whose parent directory does not exist -- every connection attempt fails fast, no migration ever run."""
    bad_path = tmp_path / "does_not_exist_dir" / "impossible.db"
    return f"sqlite+pysqlite:///{bad_path.as_posix()}"


@pytest.fixture()
def client(migrated_db_url) -> TestClient:
    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Authentication fixtures (Phase 6, Slice 3B)
# ---------------------------------------------------------------------------

VALID_TOKEN = "test-suite-token-" + "a" * 24
VALID_CLIENT_ID = "test-suite-client"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@pytest.fixture()
def auth_configured(monkeypatch) -> None:
    """Configures exactly one valid test client/token pair -- never a real credential."""
    monkeypatch.setenv(AUTH_ENV_VAR, json.dumps({VALID_CLIENT_ID: _digest(VALID_TOKEN)}))


@pytest.fixture()
def auth_unconfigured(monkeypatch) -> None:
    monkeypatch.delenv(AUTH_ENV_VAR, raising=False)


@pytest.fixture()
def auth_headers(auth_configured) -> dict[str, str]:
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


# ---------------------------------------------------------------------------
# Team-seeding fixtures (Phase 6, Slice 3B -- reinstated after Slice 3A)
# ---------------------------------------------------------------------------


def _real_identity(query: str) -> TeamIdentity:
    ensure_team_registry()
    result = resolve_team_identity(query, source="test")
    assert isinstance(result, TeamIdentity), f"{query!r} was expected to resolve cleanly, got {result}"
    return result


@pytest.fixture()
def bonaventure_identity() -> TeamIdentity:
    return _real_identity("St. Bonaventure")


@pytest.fixture()
def seeded_team_db_url(migrated_db_url, bonaventure_identity) -> str:
    """A migrated SQLite file, pre-seeded with one real, resolvable team (St. Bonaventure) -- the same file `create_app` will later open."""
    engine = create_engine(migrated_db_url, connect_args={"check_same_thread": False})
    try:
        session_factory = build_session_factory(engine)
        with unit_of_work(session_factory) as session:
            TeamRepository(session).upsert(
                team_id=bonaventure_identity.team_id,
                display_name=bonaventure_identity.display_name,
                sr_slug=bonaventure_identity.sources["sports_reference"].raw_value,
            )
    finally:
        engine.dispose()
    return migrated_db_url


@pytest.fixture()
def team_client(seeded_team_db_url, auth_configured) -> TestClient:
    app = create_app(database_url=seeded_team_db_url)
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Player-seeding fixtures (Phase 6, Slice 4B)
# ---------------------------------------------------------------------------

PLAYER_TEAM_ID = 920001
PLAYER_SEASON_ID = 2026


@pytest.fixture()
def sample_player_identity():
    return build_player_identity("API Test Player", PLAYER_TEAM_ID, PLAYER_SEASON_ID)


@pytest.fixture()
def seeded_player_db_url(migrated_db_url, sample_player_identity) -> str:
    """A migrated SQLite file, pre-seeded with one Player and two PlayerStatSnapshot rows (including a stored zero and several never-set nullable columns)."""
    engine = create_engine(migrated_db_url, connect_args={"check_same_thread": False})
    try:
        session_factory = build_session_factory(engine)
        with unit_of_work(session_factory) as session:
            TeamRepository(session).upsert(team_id=PLAYER_TEAM_ID, display_name="API Test Team", sr_slug="api-test-team-player")
            SeasonRepository(session).upsert(
                season_id=PLAYER_SEASON_ID, start_year=PLAYER_SEASON_ID - 1, end_year=PLAYER_SEASON_ID,
                label=f"{PLAYER_SEASON_ID - 1}-{PLAYER_SEASON_ID}",
            )
            DataSourceRepository(session).upsert(source_key="sr_allstats", display_name="SR AllStats", acquisition_type="live_scraped_html", is_rescrapable=True)
            artifact = SourceArtifactRepository(session).upsert(
                source_key="sr_allstats", source_root_key="teamdump_cbb", relative_path="api-test-team/Roster_2026.xlsx",
                original_filename="Roster_2026.xlsx", file_type="xlsx", content_hash="api-test-hash",
                team_id=PLAYER_TEAM_ID, season_id=PLAYER_SEASON_ID, validation_status="valid",
                point_in_time_classification="historical_but_not_point_in_time",
            )
            identity = sample_player_identity
            PlayerRepository(session).upsert(
                player_id=identity.player_id, display_name=identity.display_name, team_id=PLAYER_TEAM_ID, season_id=PLAYER_SEASON_ID,
            )
            PlayerStatSnapshotRepository(session).upsert(
                team_id=PLAYER_TEAM_ID, season_id=PLAYER_SEASON_ID, source_artifact_id=artifact.id, stat_table="PerGame",
                player_key="api test player", source_player_name="API Test Player", validation_status="valid",
                player_id=identity.player_id, pts=16.1, trb=9.8, ast=3.0, stl=0.0, blk=0.0,
            )
            PlayerStatSnapshotRepository(session).upsert(
                team_id=PLAYER_TEAM_ID, season_id=PLAYER_SEASON_ID, source_artifact_id=artifact.id, stat_table="Advanced",
                player_key="api test player", source_player_name="API Test Player", validation_status="valid",
                player_id=identity.player_id, usg_pct=24.5, per=18.2,
            )
    finally:
        engine.dispose()
    return migrated_db_url


@pytest.fixture()
def player_client(seeded_player_db_url, auth_configured) -> TestClient:
    app = create_app(database_url=seeded_player_db_url)
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Rankings fixtures (Phase 6, Slice 5B)
# ---------------------------------------------------------------------------

RANKINGS_TEST_SEASON = 2026
#: Real, stable canonical team_id (SHA-256 of Duke's SR slug) -- same constant
#: `tests/regression/test_rankings_cache_repository.py` already uses.
RANKINGS_DUKE_TEAM_ID = 848850


def write_rankings_csv(tmp_path, rows: list[tuple[str, object]], *, filename: str = "rankings.csv"):
    """Same synthetic-CSV shape as tests/regression/test_rankings_cache_repository.py's own `_write_csv` -- never touches the real cache file."""
    path = tmp_path / filename
    lines = ["team_slug,net_rank"] + [f"{slug},{rank}" for slug, rank in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture()
def rankings_cache_path(tmp_path):
    return write_rankings_csv(tmp_path, [("duke", 1), ("michigan", 2), ("arizona", 3)])


@pytest.fixture()
def rankings_configured(monkeypatch, rankings_cache_path):
    """Server-side Rankings configuration only -- never accepted from a request."""
    monkeypatch.setenv(RANKINGS_SEASON_ENV_VAR, str(RANKINGS_TEST_SEASON))
    monkeypatch.setenv(RANKINGS_CACHE_PATH_ENV_VAR, str(rankings_cache_path))
    return rankings_cache_path


@pytest.fixture()
def rankings_unconfigured(monkeypatch):
    monkeypatch.delenv(RANKINGS_SEASON_ENV_VAR, raising=False)
    monkeypatch.delenv(RANKINGS_CACHE_PATH_ENV_VAR, raising=False)


@pytest.fixture()
def rankings_client(migrated_db_url, auth_configured, rankings_configured) -> TestClient:
    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as test_client:
        yield test_client
