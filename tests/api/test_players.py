"""
Phase 6, Slice 4B: API-level tests for the Bearer-authenticated
`GET /v1/players/{player_id}/raw-statistics` endpoint.

Uses service and dependency overrides where PostgreSQL is not required --
these are HTTP-translation tests, not a re-proof of the service itself
(already covered by tests/regression/test_services_player.py). No live
server is left running: every test drives the app through TestClient.
"""
from __future__ import annotations

from sqlalchemy import create_engine, text

from src.api.deps import get_authenticated_client, get_session
from src.api.main import create_app
from src.auth.api_keys import AuthenticatedClient
from src.db.repositories import TeamRepository
from src.db.session import build_session_factory, unit_of_work


def test_authenticated_request_succeeds_with_exact_response_schema(player_client, auth_headers, sample_player_identity):
    response = player_client.get(f"/v1/players/{sample_player_identity.player_id}/raw-statistics", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["player_id"] == sample_player_identity.player_id
    assert body["display_name"] == sample_player_identity.display_name
    assert body["team_id"] == 920001
    assert body["season_id"] == 2026
    assert len(body["stats"]) == 2
    assert set(body["stats"][0].keys()) == {
        "stat_table", "season_id", "team_id", "source_artifact_id", "source_player_name", "validation_status",
        "source_rank", "source_position", "awards_raw", "games_played", "games_started",
        "minutes_played", "fg_made", "fg_attempted", "fg_pct", "three_p_made", "three_p_attempted",
        "three_p_pct", "two_p_made", "two_p_attempted", "two_p_pct", "efg_pct", "ft_made",
        "ft_attempted", "ft_pct", "orb", "drb", "trb", "ast", "stl", "blk", "tov", "pf", "pts",
        "off_rating", "def_rating", "per", "ts_pct", "three_par", "ftr", "pprod", "orb_pct",
        "drb_pct", "trb_pct", "ast_pct", "stl_pct", "blk_pct", "tov_pct", "usg_pct", "ows", "dws",
        "ws", "ws_per_40", "obpm", "dbpm", "bpm",
    }
    # Explicit field-exposure decision: player_key is the only excluded field (redundant with source_player_name).
    assert "player_key" not in body["stats"][0]


def test_exposure_contract_matches_the_approved_allowlist_exactly():
    """
    Permanent, programmatic guard against silent field drift (Phase 6,
    Slice 4B correction): PlayerStatResponse must expose every
    PlayerStatRecord field except player_key -- no more, no fewer. A
    passing test here is the actual proof; prose documentation alone is
    not sufficient (this is exactly the gap the Slice 4B review found).
    """
    import dataclasses

    from src.api.schemas import PlayerStatResponse
    from src.services.contracts import PlayerStatRecord

    record_fields = {f.name for f in dataclasses.fields(PlayerStatRecord)}
    response_fields = set(PlayerStatResponse.model_fields.keys())

    assert record_fields - response_fields == {"player_key"}
    assert response_fields - record_fields == set()


def test_validation_status_is_exposed_unchanged_for_every_stored_value(player_client, auth_headers):
    """
    validation_status must reach the client exactly as stored -- for
    every value the schema allows, not only "valid" -- since
    PlayerStatSnapshotRepository.list_for_player filters only by
    player_id/season_id/stat_table and enforces no invariant that only
    valid rows can have a resolved player_id.
    """
    from src.db.allstats_repositories import PlayerStatSnapshotRepository
    from src.db.repositories import DataSourceRepository, PlayerRepository, SeasonRepository, SourceArtifactRepository, TeamRepository
    from src.db.session import build_session_factory, unit_of_work
    from src.identity.player_identity import build_player_identity

    engine = create_engine(player_client.app.state.engine.url, connect_args={"check_same_thread": False})
    try:
        session_factory = build_session_factory(engine)
        team_id, season_id = 950001, 2026
        identity = build_player_identity("Data Quality Player", team_id, season_id)
        with unit_of_work(session_factory) as session:
            TeamRepository(session).upsert(team_id=team_id, display_name="DQ Team", sr_slug="dq-team")
            SeasonRepository(session).upsert(season_id=season_id, start_year=season_id - 1, end_year=season_id, label=f"{season_id - 1}-{season_id}")
            DataSourceRepository(session).upsert(source_key="sr_allstats", display_name="SR AllStats", acquisition_type="live_scraped_html", is_rescrapable=True)
            artifact = SourceArtifactRepository(session).upsert(
                source_key="sr_allstats", source_root_key="teamdump_cbb", relative_path="dq-team/Roster.xlsx",
                original_filename="Roster.xlsx", file_type="xlsx", content_hash="dq-hash",
                team_id=team_id, season_id=season_id, validation_status="valid",
                point_in_time_classification="historical_but_not_point_in_time",
            )
            PlayerRepository(session).upsert(player_id=identity.player_id, display_name=identity.display_name, team_id=team_id, season_id=season_id)

            for i, status in enumerate(("valid", "partial", "unresolved", "conflicting", "invalid")):
                PlayerStatSnapshotRepository(session).upsert(
                    team_id=team_id, season_id=season_id, source_artifact_id=artifact.id,
                    stat_table=["PerGame", "Totals", "Per40", "Per100", "Advanced"][i],
                    player_key="data quality player", source_player_name="Data Quality Player",
                    validation_status=status, player_id=identity.player_id,
                )
    finally:
        engine.dispose()

    response = player_client.get(f"/v1/players/{identity.player_id}/raw-statistics", headers=auth_headers)
    assert response.status_code == 200
    statuses_by_table = {s["stat_table"]: s["validation_status"] for s in response.json()["stats"]}
    assert statuses_by_table == {
        "PerGame": "valid",
        "Totals": "partial",
        "Per40": "unresolved",
        "Per100": "conflicting",
        "Advanced": "invalid",
    }


def test_known_player_with_zero_matching_rows_remains_found_with_empty_array(migrated_db_url, auth_headers):
    """Duplicate-purpose guard alongside test_known_player_with_no_statistics_returns_200_and_empty_array -- explicit item in the Slice 4B data-quality checklist."""
    from src.identity.player_identity import build_player_identity

    engine = create_engine(migrated_db_url, connect_args={"check_same_thread": False})
    identity = build_player_identity("Zero Rows Player", 960001, 2026)
    try:
        session_factory = build_session_factory(engine)
        with unit_of_work(session_factory) as session:
            TeamRepository(session).upsert(team_id=960001, display_name="Zero Rows Team", sr_slug="zero-rows-team")
            from src.db.repositories import PlayerRepository, SeasonRepository

            SeasonRepository(session).upsert(season_id=2026, start_year=2025, end_year=2026, label="2025-26")
            PlayerRepository(session).upsert(player_id=identity.player_id, display_name=identity.display_name, team_id=960001, season_id=2026)
    finally:
        engine.dispose()

    app = create_app(database_url=migrated_db_url)
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        response = client.get(f"/v1/players/{identity.player_id}/raw-statistics", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["stats"] == []


def test_source_artifact_id_remains_an_opaque_integer_never_a_path(player_client, auth_headers, sample_player_identity):
    response = player_client.get(f"/v1/players/{sample_player_identity.player_id}/raw-statistics", headers=auth_headers)
    for record in response.json()["stats"]:
        assert isinstance(record["source_artifact_id"], int)
    assert "teamdump_cbb" not in response.text
    assert "Roster_2026.xlsx" not in response.text
    assert "\\" not in response.text and "/api-test-team/" not in response.text


def test_multiple_records_preserve_deterministic_order(player_client, auth_headers, sample_player_identity):
    response = player_client.get(f"/v1/players/{sample_player_identity.player_id}/raw-statistics", headers=auth_headers)
    stat_tables = [s["stat_table"] for s in response.json()["stats"]]
    assert stat_tables == ["Advanced", "PerGame"]  # alphabetical: season_id/source_artifact_id constant here

    # Repeated request returns the identical order.
    response2 = player_client.get(f"/v1/players/{sample_player_identity.player_id}/raw-statistics", headers=auth_headers)
    assert [s["stat_table"] for s in response2.json()["stats"]] == stat_tables


def test_season_id_filter(player_client, auth_headers, sample_player_identity):
    response = player_client.get(
        f"/v1/players/{sample_player_identity.player_id}/raw-statistics", params={"season_id": 2026}, headers=auth_headers
    )
    assert response.status_code == 200
    assert len(response.json()["stats"]) == 2

    response_wrong_season = player_client.get(
        f"/v1/players/{sample_player_identity.player_id}/raw-statistics", params={"season_id": 2025}, headers=auth_headers
    )
    assert response_wrong_season.status_code == 200
    assert response_wrong_season.json()["stats"] == []


def test_stat_table_filter(player_client, auth_headers, sample_player_identity):
    response = player_client.get(
        f"/v1/players/{sample_player_identity.player_id}/raw-statistics", params={"stat_table": "Advanced"}, headers=auth_headers
    )
    assert response.status_code == 200
    stats = response.json()["stats"]
    assert len(stats) == 1
    assert stats[0]["stat_table"] == "Advanced"
    assert stats[0]["usg_pct"] == 24.5


def test_combined_filters(player_client, auth_headers, sample_player_identity):
    response = player_client.get(
        f"/v1/players/{sample_player_identity.player_id}/raw-statistics",
        params={"season_id": 2026, "stat_table": "PerGame"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    stats = response.json()["stats"]
    assert len(stats) == 1
    assert stats[0]["stat_table"] == "PerGame"


def test_unknown_player_returns_typed_404(player_client, auth_headers):
    response = player_client.get("/v1/players/nobody-real/raw-statistics", headers=auth_headers)
    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "not_found"
    assert "message" in body


def test_known_player_with_no_statistics_returns_200_and_empty_array(migrated_db_url, auth_headers):
    """A Player row with zero PlayerStatSnapshot rows -- found, not missing."""
    from src.identity.player_identity import build_player_identity

    engine = create_engine(migrated_db_url, connect_args={"check_same_thread": False})
    identity = build_player_identity("No Stats Player", 930001, 2026)
    try:
        session_factory = build_session_factory(engine)
        with unit_of_work(session_factory) as session:
            TeamRepository(session).upsert(team_id=930001, display_name="No Stats Team", sr_slug="no-stats-team")
            from src.db.repositories import PlayerRepository, SeasonRepository

            SeasonRepository(session).upsert(season_id=2026, start_year=2025, end_year=2026, label="2025-26")
            PlayerRepository(session).upsert(player_id=identity.player_id, display_name=identity.display_name, team_id=930001, season_id=2026)
    finally:
        engine.dispose()

    app = create_app(database_url=migrated_db_url)
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        response = client.get(f"/v1/players/{identity.player_id}/raw-statistics", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["stats"] == []


def test_known_player_with_nonmatching_filters_returns_200_and_empty_array(player_client, auth_headers, sample_player_identity):
    response = player_client.get(
        f"/v1/players/{sample_player_identity.player_id}/raw-statistics", params={"stat_table": "Totals"}, headers=auth_headers
    )
    assert response.status_code == 200
    assert response.json()["stats"] == []


def test_none_serializes_as_json_null(player_client, auth_headers, sample_player_identity):
    response = player_client.get(
        f"/v1/players/{sample_player_identity.player_id}/raw-statistics", params={"stat_table": "PerGame"}, headers=auth_headers
    )
    record = response.json()["stats"][0]
    assert record["usg_pct"] is None
    assert record["awards_raw"] is None
    assert record["ows"] is None


def test_stored_zero_serializes_as_json_zero(player_client, auth_headers, sample_player_identity):
    response = player_client.get(
        f"/v1/players/{sample_player_identity.player_id}/raw-statistics", params={"stat_table": "PerGame"}, headers=auth_headers
    )
    record = response.json()["stats"][0]
    assert record["stl"] == 0
    assert record["blk"] == 0
    assert record["stl"] is not None


def test_malformed_season_id_returns_established_typed_422(player_client, auth_headers, sample_player_identity):
    response = player_client.get(
        f"/v1/players/{sample_player_identity.player_id}/raw-statistics", params={"season_id": "not-a-number"}, headers=auth_headers
    )
    assert response.status_code == 422


def test_missing_authentication_returns_established_401(player_client, sample_player_identity):
    response = player_client.get(f"/v1/players/{sample_player_identity.player_id}/raw-statistics")
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized", "message": "missing or invalid authentication credentials"}


def test_invalid_authentication_returns_the_same_401(player_client, sample_player_identity):
    response = player_client.get(
        f"/v1/players/{sample_player_identity.player_id}/raw-statistics", headers={"Authorization": "Bearer wrong-token"}
    )
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized", "message": "missing or invalid authentication credentials"}


def test_unsupported_authentication_scheme_returns_the_same_401(player_client, sample_player_identity):
    response = player_client.get(
        f"/v1/players/{sample_player_identity.player_id}/raw-statistics", headers={"Authorization": "Basic dXNlcjpwYXNz"}
    )
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized", "message": "missing or invalid authentication credentials"}


def test_unconfigured_authentication_returns_sanitized_503(seeded_player_db_url, auth_unconfigured, sample_player_identity):
    app = create_app(database_url=seeded_player_db_url)
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        response = client.get(
            f"/v1/players/{sample_player_identity.player_id}/raw-statistics", headers={"Authorization": "Bearer anything"}
        )
    assert response.status_code == 503
    assert response.json()["error"] == "service_unavailable"


def test_every_401_includes_www_authenticate_bearer(player_client, sample_player_identity):
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic xyz"}):
        response = player_client.get(f"/v1/players/{sample_player_identity.player_id}/raw-statistics", headers=headers)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"


def test_authentication_failure_never_opens_a_database_session(seeded_player_db_url, auth_configured):
    app = create_app(database_url=seeded_player_db_url)
    called = []

    def _spy(request):
        called.append(True)
        yield from get_session(request)

    app.dependency_overrides[get_session] = _spy
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        response = client.get("/v1/players/anyone/raw-statistics")

    assert response.status_code == 401
    assert called == []


def test_authentication_failure_never_calls_the_player_service(seeded_player_db_url, auth_configured, monkeypatch):
    import src.services.player as player_service_module

    calls = []
    monkeypatch.setattr(player_service_module, "get_player_raw_statistics", lambda *a, **k: calls.append(1))

    app = create_app(database_url=seeded_player_db_url)
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        response = client.get("/v1/players/anyone/raw-statistics")

    assert response.status_code == 401
    assert calls == []


def test_authentication_dependency_override_bypasses_real_verification(seeded_player_db_url, sample_player_identity):
    app = create_app(database_url=seeded_player_db_url)
    app.dependency_overrides[get_authenticated_client] = lambda: AuthenticatedClient(client_id="override-client")
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        response = client.get(f"/v1/players/{sample_player_identity.player_id}/raw-statistics")  # no Authorization header
    assert response.status_code == 200


def test_service_result_mapped_without_recalculation(player_client, auth_headers, sample_player_identity):
    """The response's pts/trb/ast must equal exactly what the fixture seeded -- no rounding, no recomputation."""
    response = player_client.get(
        f"/v1/players/{sample_player_identity.player_id}/raw-statistics", params={"stat_table": "PerGame"}, headers=auth_headers
    )
    record = response.json()["stats"][0]
    assert record["pts"] == 16.1
    assert record["trb"] == 9.8
    assert record["ast"] == 3.0


def test_no_internal_path_or_database_detail_exposed(player_client, auth_headers):
    response = player_client.get("/v1/players/nobody-real/raw-statistics", headers=auth_headers)
    text_body = response.text
    for forbidden in ("sqlite", "Traceback", ".db", "site-packages", "C:\\"):
        assert forbidden not in text_body


def test_repeated_requests_are_deterministic(player_client, auth_headers, sample_player_identity):
    responses = [
        player_client.get(f"/v1/players/{sample_player_identity.player_id}/raw-statistics", headers=auth_headers) for _ in range(5)
    ]
    assert all(r.status_code == 200 for r in responses)
    assert all(r.json() == responses[0].json() for r in responses)


def test_team_endpoints_remain_unchanged(player_client, auth_headers):
    response = player_client.get("/v1/teams/999999999", headers=auth_headers)
    assert response.status_code == 404  # unchanged Team not-found behavior


def test_health_and_ready_remain_unauthenticated(player_client):
    assert player_client.get("/v1/health").status_code == 200
    assert player_client.get("/v1/ready").status_code == 200


def test_openapi_applies_bearer_security_to_player_endpoint(player_client):
    schema = player_client.app.openapi()
    assert schema["paths"]["/v1/players/{player_id}/raw-statistics"]["get"]["security"] == [{"HTTPBearer": []}]


def test_no_extra_player_route_exists(player_client):
    schema = player_client.app.openapi()
    player_paths = [p for p in schema["paths"] if p.startswith("/v1/players")]
    assert player_paths == ["/v1/players/{player_id}/raw-statistics"]


def test_route_performs_no_write_to_the_database(player_client, auth_headers, sample_player_identity, seeded_player_db_url):
    engine = create_engine(seeded_player_db_url, connect_args={"check_same_thread": False})
    try:
        with engine.connect() as conn:
            before = conn.execute(text("SELECT id, player_id, pts FROM player_stat_snapshots")).fetchall()

        for _ in range(3):
            player_client.get(f"/v1/players/{sample_player_identity.player_id}/raw-statistics", headers=auth_headers)

        with engine.connect() as conn:
            after = conn.execute(text("SELECT id, player_id, pts FROM player_stat_snapshots")).fetchall()

        assert before == after
    finally:
        engine.dispose()
