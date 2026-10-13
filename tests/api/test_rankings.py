"""
Phase 6, Slice 5B: API-level tests for the Bearer-authenticated,
local-cache-only Rankings endpoints:
`GET /v1/rankings/net` and `GET /v1/rankings/net/teams/{team_id}`.

HTTP-translation tests only -- the underlying service/repository behavior
(cache_empty/not_found/conflicting/RankingsCacheIntegrityError, real-cache
parity, no-network isolation) is already fully proven at the service level
by tests/regression/test_rankings_cache_repository.py and is not
re-verified here beyond confirming the route preserves each outcome
faithfully. Every test drives the app through TestClient; none starts a
live server, and none ever points at or mutates the real
`Reference Py Files/net_rankings_2026.csv` cache file.
"""
from __future__ import annotations

import ast
import inspect

from fastapi.testclient import TestClient

from src.api.deps import get_authenticated_client, get_rankings_context
from src.api.main import create_app
from src.auth.api_keys import AuthenticatedClient
from tests.api.conftest import RANKINGS_DUKE_TEAM_ID, RANKINGS_TEST_SEASON, write_rankings_csv

# ---------------------------------------------------------------------------
# List endpoint -- success and field fidelity
# ---------------------------------------------------------------------------


def test_authenticated_list_succeeds_with_exact_response_schema(rankings_client, auth_headers):
    response = rankings_client.get("/v1/rankings/net", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"season", "total_rows", "rows"}
    assert body["season"] == RANKINGS_TEST_SEASON
    assert body["total_rows"] == 3
    assert len(body["rows"]) == 3
    for row in body["rows"]:
        assert set(row.keys()) == {"raw_team_key", "resolution_status", "team_id", "net_rank"}


def test_list_preserves_deterministic_source_row_order(rankings_client, auth_headers):
    response = rankings_client.get("/v1/rankings/net", headers=auth_headers)
    keys = [r["raw_team_key"] for r in response.json()["rows"]]
    assert keys == ["duke", "michigan", "arizona"]

    response2 = rankings_client.get("/v1/rankings/net", headers=auth_headers)
    assert [r["raw_team_key"] for r in response2.json()["rows"]] == keys


def test_list_never_silently_implies_full_resolution(migrated_db_url, auth_configured, auth_headers, monkeypatch, tmp_path):
    """A cache with an unresolved/ambiguous row must still surface it -- never dropped, never fabricated as resolved."""
    from src.api.deps import RANKINGS_CACHE_PATH_ENV_VAR, RANKINGS_SEASON_ENV_VAR

    path = write_rankings_csv(tmp_path, [("duke", 1), ("zzznotarealteam99", 5), ("garbage-rank-team", "abc")])
    monkeypatch.setenv(RANKINGS_SEASON_ENV_VAR, str(RANKINGS_TEST_SEASON))
    monkeypatch.setenv(RANKINGS_CACHE_PATH_ENV_VAR, str(path))

    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        response = client.get("/v1/rankings/net", headers=auth_headers)

    assert response.status_code == 200
    rows = response.json()["rows"]
    assert len(rows) == 3
    statuses = {r["raw_team_key"]: r["resolution_status"] for r in rows}
    assert statuses["zzznotarealteam99"] == "not_found"
    assert statuses["garbage-rank-team"] == "invalid_rank"

    # Team resolution and rank validity are independent, truthfully-preserved
    # dimensions -- an unresolved team can still carry a validly-parsed rank.
    unresolved = next(r for r in rows if r["raw_team_key"] == "zzznotarealteam99")
    assert unresolved["team_id"] is None
    assert unresolved["net_rank"] == 5

    invalid_rank_row = next(r for r in rows if r["raw_team_key"] == "garbage-rank-team")
    assert invalid_rank_row["net_rank"] is None


def test_list_represents_a_genuine_conflicting_duplicate_as_two_rows(migrated_db_url, auth_configured, auth_headers, monkeypatch, tmp_path):
    """A raw key appearing twice with different ranks (e.g. real "louisiana-state") must appear twice, each with its own rank -- never collapsed."""
    from src.api.deps import RANKINGS_CACHE_PATH_ENV_VAR, RANKINGS_SEASON_ENV_VAR

    path = write_rankings_csv(tmp_path, [("duke", 1), ("duke", 50)])
    monkeypatch.setenv(RANKINGS_SEASON_ENV_VAR, str(RANKINGS_TEST_SEASON))
    monkeypatch.setenv(RANKINGS_CACHE_PATH_ENV_VAR, str(path))

    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        response = client.get("/v1/rankings/net", headers=auth_headers)

    assert response.status_code == 200
    rows = response.json()["rows"]
    assert len(rows) == 2
    ranks = {r["net_rank"] for r in rows}
    assert ranks == {1, 50}
    assert all(r["raw_team_key"] == "duke" for r in rows)


def test_list_has_no_pagination_parameters(rankings_client, auth_headers):
    """Bounded-snapshot decision (documented, not yet needed): a page/limit/offset param must have no effect on this fixed-size local cache."""
    response = rankings_client.get("/v1/rankings/net", params={"page": 2, "limit": 1, "offset": 1}, headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["total_rows"] == 3


def test_list_ignores_a_client_supplied_season_query_parameter(rankings_client, auth_headers):
    """Season is server-controlled only -- a client-supplied `season` query param must be silently ignored, never trusted."""
    response = rankings_client.get("/v1/rankings/net", params={"season": 1999}, headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["season"] == RANKINGS_TEST_SEASON


def test_list_response_never_carries_an_as_of_or_freshness_field(rankings_client, auth_headers):
    response = rankings_client.get("/v1/rankings/net", headers=auth_headers)
    body = response.json()
    for forbidden in ("as_of", "is_current", "freshness", "last_updated"):
        assert forbidden not in body


def test_list_response_never_exposes_the_cache_filesystem_path(rankings_client, auth_headers, rankings_cache_path):
    response = rankings_client.get("/v1/rankings/net", headers=auth_headers)
    assert str(rankings_cache_path) not in response.text
    assert "Reference Py Files" not in response.text


# ---------------------------------------------------------------------------
# Lookup endpoint -- outcome translation
# ---------------------------------------------------------------------------


def test_authenticated_lookup_succeeds_with_exact_response_schema(rankings_client, auth_headers):
    response = rankings_client.get(f"/v1/rankings/net/teams/{RANKINGS_DUKE_TEAM_ID}", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"team_id", "net_rank", "raw_team_key", "season"}
    assert body["team_id"] == RANKINGS_DUKE_TEAM_ID
    assert body["net_rank"] == 1
    assert body["raw_team_key"] == "duke"
    assert body["season"] == RANKINGS_TEST_SEASON


def test_lookup_ignores_a_client_supplied_season_query_parameter(rankings_client, auth_headers):
    response = rankings_client.get(f"/v1/rankings/net/teams/{RANKINGS_DUKE_TEAM_ID}", params={"season": 1999}, headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["season"] == RANKINGS_TEST_SEASON


def test_lookup_response_never_carries_an_as_of_or_freshness_field(rankings_client, auth_headers):
    response = rankings_client.get(f"/v1/rankings/net/teams/{RANKINGS_DUKE_TEAM_ID}", headers=auth_headers)
    body = response.json()
    for forbidden in ("as_of", "is_current", "freshness", "last_updated"):
        assert forbidden not in body


def test_unknown_team_returns_typed_404(rankings_client, auth_headers):
    response = rankings_client.get("/v1/rankings/net/teams/1", headers=auth_headers)  # guaranteed absent from the synthetic cache
    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "not_found"
    assert "message" in body


def test_conflicting_team_returns_409_with_every_conflicting_record_preserved(
    migrated_db_url, auth_configured, auth_headers, monkeypatch, tmp_path
):
    """The real cache's own "louisiana-state" (ranks 77/245) shape, reproduced synthetically -- neither rank may be silently chosen."""
    from src.api.deps import RANKINGS_CACHE_PATH_ENV_VAR, RANKINGS_SEASON_ENV_VAR

    path = write_rankings_csv(tmp_path, [("duke", 1), ("duke", 50)])
    monkeypatch.setenv(RANKINGS_SEASON_ENV_VAR, str(RANKINGS_TEST_SEASON))
    monkeypatch.setenv(RANKINGS_CACHE_PATH_ENV_VAR, str(path))

    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        response = client.get(f"/v1/rankings/net/teams/{RANKINGS_DUKE_TEAM_ID}", headers=auth_headers)

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "conflict"
    assert body["team_id"] == RANKINGS_DUKE_TEAM_ID
    assert len(body["conflicting_records"]) == 2
    ranks = {r["net_rank"] for r in body["conflicting_records"]}
    assert ranks == {1, 50}
    assert all(r["raw_team_key"] == "duke" for r in body["conflicting_records"])


def test_empty_cache_returns_sanitized_503_not_404(migrated_db_url, auth_configured, auth_headers, monkeypatch, tmp_path):
    from src.api.deps import RANKINGS_CACHE_PATH_ENV_VAR, RANKINGS_SEASON_ENV_VAR

    path = tmp_path / "empty.csv"
    path.write_text("team_slug,net_rank\n", encoding="utf-8")
    monkeypatch.setenv(RANKINGS_SEASON_ENV_VAR, str(RANKINGS_TEST_SEASON))
    monkeypatch.setenv(RANKINGS_CACHE_PATH_ENV_VAR, str(path))

    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        response = client.get(f"/v1/rankings/net/teams/{RANKINGS_DUKE_TEAM_ID}", headers=auth_headers)

    assert response.status_code == 503
    assert response.json()["error"] == "service_unavailable"


def test_missing_cache_file_returns_sanitized_503(migrated_db_url, auth_configured, auth_headers, monkeypatch, tmp_path):
    from src.api.deps import RANKINGS_CACHE_PATH_ENV_VAR, RANKINGS_SEASON_ENV_VAR

    monkeypatch.setenv(RANKINGS_SEASON_ENV_VAR, str(RANKINGS_TEST_SEASON))
    monkeypatch.setenv(RANKINGS_CACHE_PATH_ENV_VAR, str(tmp_path / "does_not_exist.csv"))

    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        response = client.get(f"/v1/rankings/net/teams/{RANKINGS_DUKE_TEAM_ID}", headers=auth_headers)

    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "service_unavailable"
    assert str(tmp_path) not in response.text
    assert "does_not_exist.csv" not in response.text


def test_malformed_cache_schema_returns_sanitized_503_not_422(migrated_db_url, auth_configured, auth_headers, monkeypatch, tmp_path):
    """A ValidationError from a missing required column is a server-side cache defect, not a client-input problem -- must be 503, not the globally-registered 422."""
    from src.api.deps import RANKINGS_CACHE_PATH_ENV_VAR, RANKINGS_SEASON_ENV_VAR

    path = tmp_path / "bad_headers.csv"
    path.write_text("school,rank\nduke,1\n", encoding="utf-8")
    monkeypatch.setenv(RANKINGS_SEASON_ENV_VAR, str(RANKINGS_TEST_SEASON))
    monkeypatch.setenv(RANKINGS_CACHE_PATH_ENV_VAR, str(path))

    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        response = client.get(f"/v1/rankings/net/teams/{RANKINGS_DUKE_TEAM_ID}", headers=auth_headers)

    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "service_unavailable"
    assert "school" not in response.text and "ValidationError" not in response.text


def test_missing_season_configuration_returns_sanitized_503(migrated_db_url, auth_configured, rankings_unconfigured, auth_headers):
    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        response = client.get(f"/v1/rankings/net/teams/{RANKINGS_DUKE_TEAM_ID}", headers=auth_headers)
    assert response.status_code == 503
    assert response.json()["error"] == "service_unavailable"


def test_non_integer_season_configuration_returns_sanitized_503(migrated_db_url, auth_configured, monkeypatch, auth_headers):
    from src.api.deps import RANKINGS_SEASON_ENV_VAR

    monkeypatch.setenv(RANKINGS_SEASON_ENV_VAR, "not-a-year")
    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        response = client.get(f"/v1/rankings/net/teams/{RANKINGS_DUKE_TEAM_ID}", headers=auth_headers)
    assert response.status_code == 503
    assert response.json()["error"] == "service_unavailable"
    assert "not-a-year" not in response.text


def test_non_positive_season_configuration_returns_sanitized_503(migrated_db_url, auth_configured, monkeypatch, auth_headers):
    from src.api.deps import RANKINGS_SEASON_ENV_VAR

    monkeypatch.setenv(RANKINGS_SEASON_ENV_VAR, "0")
    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        response = client.get(f"/v1/rankings/net/teams/{RANKINGS_DUKE_TEAM_ID}", headers=auth_headers)
    assert response.status_code == 503
    assert response.json()["error"] == "service_unavailable"


# ---------------------------------------------------------------------------
# Authentication -- both endpoints
# ---------------------------------------------------------------------------


def test_missing_authentication_returns_established_401_on_both_endpoints(rankings_client):
    for path in ("/v1/rankings/net", f"/v1/rankings/net/teams/{RANKINGS_DUKE_TEAM_ID}"):
        response = rankings_client.get(path)
        assert response.status_code == 401
        assert response.json() == {"error": "unauthorized", "message": "missing or invalid authentication credentials"}
        assert response.headers["www-authenticate"] == "Bearer"


def test_invalid_token_returns_the_same_401(rankings_client):
    response = rankings_client.get("/v1/rankings/net", headers={"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401


def test_unsupported_scheme_returns_the_same_401(rankings_client):
    response = rankings_client.get("/v1/rankings/net", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert response.status_code == 401


def test_empty_bearer_credential_returns_the_same_401(rankings_client):
    response = rankings_client.get("/v1/rankings/net", headers={"Authorization": "Bearer "})
    assert response.status_code == 401


def test_unconfigured_authentication_returns_sanitized_503(migrated_db_url, auth_unconfigured, rankings_configured):
    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        response = client.get("/v1/rankings/net", headers={"Authorization": "Bearer anything"})
    assert response.status_code == 503
    assert response.json()["error"] == "service_unavailable"


def test_authentication_failure_never_constructs_the_rankings_repository(migrated_db_url, auth_configured, rankings_configured):
    """Auth-before-resource DI ordering: a failed-auth request must never reach get_rankings_context (and therefore never open the cache file)."""
    app = create_app(database_url=migrated_db_url)
    called = []

    def _spy():
        called.append(True)
        return get_rankings_context()

    app.dependency_overrides[get_rankings_context] = _spy
    with TestClient(app) as client:
        response_list = client.get("/v1/rankings/net")
        response_lookup = client.get(f"/v1/rankings/net/teams/{RANKINGS_DUKE_TEAM_ID}")

    assert response_list.status_code == 401
    assert response_lookup.status_code == 401
    assert called == []


def test_authentication_failure_never_calls_the_rankings_service(migrated_db_url, auth_configured, rankings_configured, monkeypatch):
    import src.services.rankings as rankings_service_module

    calls = []
    monkeypatch.setattr(rankings_service_module, "list_rankings", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(rankings_service_module, "get_team_ranking", lambda *a, **k: calls.append(1))

    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        client.get("/v1/rankings/net")
        client.get(f"/v1/rankings/net/teams/{RANKINGS_DUKE_TEAM_ID}")

    assert calls == []


def test_authentication_dependency_override_bypasses_real_verification(migrated_db_url, rankings_configured):
    app = create_app(database_url=migrated_db_url)
    app.dependency_overrides[get_authenticated_client] = lambda: AuthenticatedClient(client_id="override-client")
    with TestClient(app) as client:
        response = client.get("/v1/rankings/net")  # no Authorization header at all
    assert response.status_code == 200


def test_rankings_context_dependency_override_works(migrated_db_url, auth_configured, auth_headers):
    """Proves DI works independent of environment configuration -- the override wins even with no env vars set at all."""
    from src.api.deps import RankingsContext
    from src.rankings.cache_repository import NetRankingCacheRepository

    app = create_app(database_url=migrated_db_url)
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        import pathlib

        path = write_rankings_csv(pathlib.Path(d), [("duke", 7)])
        app.dependency_overrides[get_rankings_context] = lambda: RankingsContext(
            repository=NetRankingCacheRepository(path), season=2030
        )
        with TestClient(app) as client:
            response = client.get(f"/v1/rankings/net/teams/{RANKINGS_DUKE_TEAM_ID}", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {"team_id": RANKINGS_DUKE_TEAM_ID, "net_rank": 7, "raw_team_key": "duke", "season": 2030}


# ---------------------------------------------------------------------------
# No leaked internals
# ---------------------------------------------------------------------------


def test_no_internal_path_or_exception_text_exposed_in_any_response(rankings_client, auth_headers):
    responses = [
        rankings_client.get("/v1/rankings/net", headers=auth_headers),
        rankings_client.get("/v1/rankings/net/teams/1", headers=auth_headers),
    ]
    for response in responses:
        for forbidden in ("Traceback", "site-packages", "C:\\", "sqlite"):
            assert forbidden not in response.text


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_repeated_list_requests_are_deterministic(rankings_client, auth_headers):
    responses = [rankings_client.get("/v1/rankings/net", headers=auth_headers) for _ in range(5)]
    assert all(r.status_code == 200 for r in responses)
    assert all(r.json() == responses[0].json() for r in responses)


def test_repeated_lookup_requests_are_deterministic(rankings_client, auth_headers):
    responses = [rankings_client.get(f"/v1/rankings/net/teams/{RANKINGS_DUKE_TEAM_ID}", headers=auth_headers) for _ in range(5)]
    assert all(r.status_code == 200 for r in responses)
    assert all(r.json() == responses[0].json() for r in responses)


# ---------------------------------------------------------------------------
# No-network proof
# ---------------------------------------------------------------------------


def test_rankings_route_module_imports_no_network_capable_function():
    from src.api.routes import rankings as rankings_routes_module

    tree = ast.parse(inspect.getsource(rankings_routes_module))
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
            imported_names.update(a.name for a in node.names)
    forbidden = ("requests", "httpx", "urllib", "selenium", "build_net_rankings_from_cbs", "build_net_rankings_from_ncaa_v15")
    for name in imported_names:
        assert not any(f in name for f in forbidden), f"unexpected network-capable import {name!r}"


# Deliberately NOT duplicated here: a dynamic `socket.socket`-disabled proof
# through `TestClient` (unlike calling the service function directly) hangs
# on Windows -- `TestClient`/httpx/anyio bridge a sync call onto a background
# event loop using `socket.socketpair()` for inter-thread wakeup, which
# Windows emulates via real `socket.socket()` calls; globally patching
# `socket.socket` to raise breaks that internal machinery in a background
# thread (silently, with no visible traceback) and deadlocks the foreground
# request forever -- unrelated to any actual outbound network call. Confirmed
# empirically the first time this test was run: it hung indefinitely (near-zero
# CPU) at exactly this point, not at collection or any other test.
#
# The route layer adds no network-capable code of its own (proven by
# `test_rankings_route_module_imports_no_network_capable_function` above),
# and the service layer it delegates to is already dynamically proven
# network-free via
# `test_rankings_service_succeeds_with_outbound_network_functions_disabled`
# in `tests/regression/test_rankings_cache_repository.py`, which calls
# `get_team_ranking` directly with no ASGI/TestClient transport involved --
# so no coverage gap results from not repeating that proof at the HTTP layer.


# ---------------------------------------------------------------------------
# Static guards -- route stays HTTP-translation-only
# ---------------------------------------------------------------------------


def test_rankings_route_module_contains_no_file_parsing_identity_or_calculation_logic():
    from src.api.routes import rankings as rankings_routes_module

    source = inspect.getsource(rankings_routes_module)
    forbidden = [
        "pd.read_csv", "read_csv(", "resolve_net_team_identity", "resolve_team_identity",
        "NetRankingCacheRepository(", "difflib", "get_close_matches",
    ]
    for token in forbidden:
        assert token not in source, f"unexpected {token!r} found in src/api/routes/rankings.py"


def test_rankings_service_module_still_imports_no_fastapi_or_pydantic():
    """Re-confirms Slice 5A's own framework-independence guarantee after this slice's service-layer edits."""
    import src.services.rankings as rankings_service_module

    source = inspect.getsource(rankings_service_module)
    forbidden = ["fastapi", "starlette", "pydantic"]
    for token in forbidden:
        assert token not in source, f"unexpected {token!r} found in src/services/rankings.py"


# ---------------------------------------------------------------------------
# Team/Player/health/ready unaffected
# ---------------------------------------------------------------------------


def test_team_and_player_authentication_unaffected_by_rankings(migrated_db_url, auth_configured, rankings_configured, auth_headers):
    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        team_missing_auth = client.get("/v1/teams/1")
        player_missing_auth = client.get("/v1/players/x/raw-statistics")
        team_valid_auth = client.get("/v1/teams/1", headers=auth_headers)
        player_valid_auth = client.get("/v1/players/x/raw-statistics", headers=auth_headers)

    assert team_missing_auth.status_code == 401
    assert player_missing_auth.status_code == 401
    assert team_valid_auth.status_code == 404
    assert player_valid_auth.status_code == 404


def test_health_and_ready_remain_unauthenticated(rankings_client):
    assert rankings_client.get("/v1/health").status_code == 200
    assert rankings_client.get("/v1/ready").status_code == 200


def test_health_and_ready_unaffected_by_missing_rankings_configuration(migrated_db_url, rankings_unconfigured):
    """create_app()/health/ready must never depend on Rankings configuration -- only the Rankings routes themselves read it."""
    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        assert client.get("/v1/health").status_code == 200
        assert client.get("/v1/ready").status_code == 200


# ---------------------------------------------------------------------------
# OpenAPI scoping / route-set closure
# ---------------------------------------------------------------------------


def test_openapi_applies_bearer_security_to_both_rankings_endpoints(rankings_client):
    schema = rankings_client.app.openapi()
    assert schema["paths"]["/v1/rankings/net"]["get"]["security"] == [{"HTTPBearer": []}]
    assert schema["paths"]["/v1/rankings/net/teams/{team_id}"]["get"]["security"] == [{"HTTPBearer": []}]


def test_only_the_two_approved_rankings_routes_exist(rankings_client):
    schema = rankings_client.app.openapi()
    rankings_paths = sorted(p for p in schema["paths"] if p.startswith("/v1/rankings"))
    assert rankings_paths == ["/v1/rankings/net", "/v1/rankings/net/teams/{team_id}"]


def test_openapi_documents_expected_status_codes_on_rankings_endpoints(rankings_client):
    schema = rankings_client.app.openapi()
    list_responses = set(schema["paths"]["/v1/rankings/net"]["get"]["responses"].keys())
    lookup_responses = set(schema["paths"]["/v1/rankings/net/teams/{team_id}"]["get"]["responses"].keys())
    assert {"200", "401", "503"}.issubset(list_responses)
    assert {"200", "401", "404", "409", "503"}.issubset(lookup_responses)


def test_openapi_contains_no_cache_path_or_credential_example(rankings_client):
    schema_text = str(rankings_client.app.openapi())
    for forbidden in ("Reference Py Files", "net_rankings_2026.csv", "test-suite-token", "NAPSEDGE_API_KEY_HASHES_JSON"):
        assert forbidden not in schema_text
