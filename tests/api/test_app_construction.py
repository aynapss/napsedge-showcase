"""
Phase 6B/Slice 3B: application-factory construction tests -- no database
access, no live server, and no scope creep beyond the approved routes.
"""
from __future__ import annotations

import inspect

from src.api import main
from src.api.main import create_app


def test_create_app_succeeds_without_touching_a_real_database(tmp_path):
    """SQLAlchemy's create_engine is lazy -- pointing at a nonexistent path must not raise at construction time."""
    bad_path = tmp_path / "does_not_exist_dir" / "impossible.db"
    app = create_app(database_url=f"sqlite+pysqlite:///{bad_path.as_posix()}")
    assert app is not None


def test_create_app_succeeds_with_no_authentication_configured(tmp_path, monkeypatch):
    """create_app() itself must never depend on NAPSEDGE_API_KEY_HASHES_JSON -- only the Team auth dependency reads it, per-request."""
    from src.auth.api_keys import ENV_VAR

    monkeypatch.delenv(ENV_VAR, raising=False)
    bad_path = tmp_path / "no_db.db"
    app = create_app(database_url=f"sqlite+pysqlite:///{bad_path.as_posix()}")
    assert app is not None


def test_importing_the_application_module_starts_no_server_or_connection():
    """Import alone (already done at module load above) must have created no engine, no app, no side effect -- create_app() is a separate, explicit call."""
    assert not hasattr(main, "app"), "main.py must not construct a module-level app instance at import time"


def test_openapi_includes_exactly_the_nine_approved_routes(tmp_path):
    bad_path = tmp_path / "no_db.db"
    app = create_app(database_url=f"sqlite+pysqlite:///{bad_path.as_posix()}")
    schema = app.openapi()
    paths = set(schema["paths"].keys())
    assert paths == {
        "/v1/health", "/v1/ready", "/v1/teams/resolve", "/v1/teams/{team_id}",
        "/v1/players/{player_id}/raw-statistics",
        "/v1/rankings/net", "/v1/rankings/net/teams/{team_id}",
        "/v1/teams/{team_id}/injuries",
        "/v1/matchups/feature-comparison",
    }


def test_openapi_applies_bearer_security_only_to_team_player_rankings_injury_and_matchup_endpoints(tmp_path):
    bad_path = tmp_path / "no_db.db"
    app = create_app(database_url=f"sqlite+pysqlite:///{bad_path.as_posix()}")
    schema = app.openapi()

    assert schema["components"]["securitySchemes"]["HTTPBearer"]["type"] == "http"
    assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"

    assert schema["paths"]["/v1/health"]["get"].get("security") in (None, [])
    assert schema["paths"]["/v1/ready"]["get"].get("security") in (None, [])
    assert schema["paths"]["/v1/teams/resolve"]["get"]["security"] == [{"HTTPBearer": []}]
    assert schema["paths"]["/v1/teams/{team_id}"]["get"]["security"] == [{"HTTPBearer": []}]
    assert schema["paths"]["/v1/players/{player_id}/raw-statistics"]["get"]["security"] == [{"HTTPBearer": []}]
    assert schema["paths"]["/v1/rankings/net"]["get"]["security"] == [{"HTTPBearer": []}]
    assert schema["paths"]["/v1/rankings/net/teams/{team_id}"]["get"]["security"] == [{"HTTPBearer": []}]
    assert schema["paths"]["/v1/teams/{team_id}/injuries"]["get"]["security"] == [{"HTTPBearer": []}]
    assert schema["paths"]["/v1/matchups/feature-comparison"]["post"]["security"] == [{"HTTPBearer": []}]


def test_openapi_documents_expected_status_codes_on_team_player_rankings_injury_and_matchup_endpoints(tmp_path):
    bad_path = tmp_path / "no_db.db"
    app = create_app(database_url=f"sqlite+pysqlite:///{bad_path.as_posix()}")
    schema = app.openapi()

    resolve_responses = set(schema["paths"]["/v1/teams/resolve"]["get"]["responses"].keys())
    by_id_responses = set(schema["paths"]["/v1/teams/{team_id}"]["get"]["responses"].keys())
    player_responses = set(schema["paths"]["/v1/players/{player_id}/raw-statistics"]["get"]["responses"].keys())
    rankings_list_responses = set(schema["paths"]["/v1/rankings/net"]["get"]["responses"].keys())
    rankings_lookup_responses = set(schema["paths"]["/v1/rankings/net/teams/{team_id}"]["get"]["responses"].keys())
    injury_responses = set(schema["paths"]["/v1/teams/{team_id}/injuries"]["get"]["responses"].keys())
    matchup_responses = set(schema["paths"]["/v1/matchups/feature-comparison"]["post"]["responses"].keys())
    assert {"200", "401", "404", "409", "422", "429", "503"}.issubset(resolve_responses)
    assert {"200", "401", "404", "429", "503"}.issubset(by_id_responses)
    assert {"200", "401", "404", "422", "429", "503"}.issubset(player_responses)
    assert {"200", "401", "422", "429", "503"}.issubset(matchup_responses)
    assert {"200", "401", "429", "503"}.issubset(rankings_list_responses)
    assert {"200", "401", "404", "409", "429", "503"}.issubset(rankings_lookup_responses)
    assert {"200", "401", "404", "429", "503"}.issubset(injury_responses)


def test_openapi_contains_no_token_or_credential_example(tmp_path):
    bad_path = tmp_path / "no_db.db"
    app = create_app(database_url=f"sqlite+pysqlite:///{bad_path.as_posix()}")
    schema_text = str(app.openapi())
    for forbidden in ("test-suite-token", "NAPSEDGE_API_KEY_HASHES_JSON", "sha256"):
        assert forbidden not in schema_text


def test_route_module_contains_no_sql_or_repository_calls():
    """Static guard: src/api/main.py must stay HTTP-translation-only -- no SQL literal, ORM query, or repository construction."""
    source = inspect.getsource(main)
    forbidden_substrings = ["select(", "SELECT ", ".query(", 'text("', "text('", "Repository(", "session.execute"]
    for token in forbidden_substrings:
        assert token not in source, f"unexpected {token!r} found in src/api/main.py"


def test_repeated_construction_is_independent_per_app_instance(tmp_path):
    """Two apps built from two different database URLs must not share engine/session-factory state."""
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    app_a = create_app(database_url=f"sqlite+pysqlite:///{db_a.as_posix()}")
    app_b = create_app(database_url=f"sqlite+pysqlite:///{db_b.as_posix()}")
    assert app_a.state.engine is not app_b.state.engine
    assert app_a.state.session_factory is not app_b.state.session_factory


def test_team_service_module_imports_no_fastapi_starlette_or_api_schema():
    """Service-boundary requirement: src.services.team must stay independent of the web framework."""
    from src.services import team as team_service

    source = inspect.getsource(team_service)
    forbidden = ["fastapi", "starlette", "src.api.schemas", "pydantic", "HTTPException", "status_code"]
    for token in forbidden:
        assert token not in source, f"unexpected {token!r} found in src/services/team.py"


def test_team_route_module_contains_no_sql_identity_or_repository_logic():
    """Static guard: src/api/routes/teams.py must stay HTTP-translation-only."""
    from src.api.routes import teams as teams_routes

    source = inspect.getsource(teams_routes)
    forbidden = ["select(", "SELECT ", ".query(", 'text("', "text('", "TeamRepository(", "resolve_team_identity"]
    for token in forbidden:
        assert token not in source, f"unexpected {token!r} found in src/api/routes/teams.py"


def test_no_extra_player_route_beyond_the_one_approved_endpoint(tmp_path):
    """
    Phase 6, Slice 4B exposes exactly one Player route. No name resolution,
    search, list-all, game-log, roster-history, or OVR-card route exists.
    """
    bad_path = tmp_path / "no_db.db"
    app = create_app(database_url=f"sqlite+pysqlite:///{bad_path.as_posix()}")
    schema = app.openapi()
    player_paths = [p for p in schema["paths"] if p.startswith("/v1/players")]
    assert player_paths == ["/v1/players/{player_id}/raw-statistics"]


def test_requesting_an_unregistered_player_shaped_path_gets_a_plain_404(tmp_path):
    """A request to a Player-shaped path OTHER than the one approved route must get FastAPI's own unmatched-route 404."""
    bad_path = tmp_path / "no_db.db"
    app = create_app(database_url=f"sqlite+pysqlite:///{bad_path.as_posix()}")
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        for path in ("/v1/players/some-id", "/v1/players/some-id/game-log", "/v1/players", "/v1/players/some-id/card"):
            response = client.get(path)
            assert response.status_code == 404
            assert set(response.json().keys()) == {"detail"}  # FastAPI's own body, not this app's {"error": ...} shape


def test_player_route_module_contains_no_sql_identity_or_repository_logic():
    """Static guard: src/api/routes/players.py must stay HTTP-translation-only."""
    from src.api.routes import players as players_routes

    source = inspect.getsource(players_routes)
    forbidden = [
        "select(", "SELECT ", ".query(", 'text("', "text('", "PlayerRepository(", "PlayerStatSnapshotRepository(",
        "build_player_identity", "normalize_player_name",
    ]
    for token in forbidden:
        assert token not in source, f"unexpected {token!r} found in src/api/routes/players.py"


def test_team_authentication_behavior_is_unaffected_by_the_player_service(migrated_db_url, auth_headers, monkeypatch):
    """Confirms Slice 4B introduced no regression in Slice 3B's authentication behavior."""
    from fastapi.testclient import TestClient

    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        missing = client.get("/v1/teams/1")
        valid = client.get("/v1/teams/1", headers=auth_headers)

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert valid.status_code == 404  # authenticated, reaches the service, team just doesn't exist in this empty DB


def test_exactly_two_rankings_routes_exist(tmp_path):
    """
    Phase 6, Slice 5B exposes the Rankings service via exactly two
    protected, read-only routes -- no search, no bulk-refresh, no
    conference-scoped listing, no season-selection route.
    """
    bad_path = tmp_path / "no_db.db"
    app = create_app(database_url=f"sqlite+pysqlite:///{bad_path.as_posix()}")
    schema = app.openapi()
    rankings_paths = sorted(p for p in schema["paths"] if p.startswith("/v1/rankings"))
    assert rankings_paths == ["/v1/rankings/net", "/v1/rankings/net/teams/{team_id}"]


def test_only_the_approved_api_modules_reference_the_rankings_service():
    """
    Static guard: since Phase 6 Slice 5B, exactly `src/api/routes/rankings.py`
    and `src/api/deps.py` may reference `src.services.rankings`/`src.rankings`
    under `src/api/` -- no other module (main.py, schemas.py, errors.py,
    teams.py, players.py) may reach into the Rankings service or cache
    layer directly.
    """
    import pathlib

    api_dir = pathlib.Path(main.__file__).parent
    approved = {api_dir / "routes" / "rankings.py", api_dir / "deps.py"}
    for py_file in api_dir.rglob("*.py"):
        source = py_file.read_text(encoding="utf-8")
        references_rankings = "services.rankings" in source or "src.rankings" in source
        if py_file in approved:
            assert references_rankings, f"expected {py_file} to reference the Rankings service/cache layer"
        else:
            assert not references_rankings, f"unexpected reference to the Rankings service found in {py_file}"


def test_team_and_player_authentication_unaffected_by_the_rankings_service(migrated_db_url, auth_headers):
    """Confirms Slice 5A/5B introduced no regression in existing Team/Player authentication behavior."""
    from fastapi.testclient import TestClient

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


def test_player_service_module_imports_no_fastapi_starlette_or_api_schema():
    """Service-boundary requirement: src.services.player must stay independent of the web framework, matching src.services.team's own precedent."""
    from src.services import player as player_service

    source = inspect.getsource(player_service)
    forbidden = ["fastapi", "starlette", "src.api.schemas", "pydantic", "HTTPException", "status_code"]
    for token in forbidden:
        assert token not in source, f"unexpected {token!r} found in src/services/player.py"


def test_exactly_one_injury_route_exists(tmp_path):
    """
    Phase 6, Slice 6B exposes the Injury service via exactly one protected,
    read-only route -- `GET /v1/teams/{team_id}/injuries` -- no search, no
    bulk-refresh, no conference-scoped listing, no other Injury path.
    """
    bad_path = tmp_path / "no_db.db"
    app = create_app(database_url=f"sqlite+pysqlite:///{bad_path.as_posix()}")
    schema = app.openapi()
    paths = set(schema["paths"].keys())
    injury_paths = [p for p in paths if p.endswith("/injuries")]
    assert injury_paths == ["/v1/teams/{team_id}/injuries"]


def test_exactly_one_injury_router_module_exists():
    """Static guard: exactly one Injury router file exists (src/api/routes/injury.py) -- no second/duplicate router."""
    import pathlib

    from src.api import main

    routes_dir = pathlib.Path(main.__file__).parent / "routes"
    injury_route_files = sorted(p.name for p in routes_dir.glob("*.py") if "injur" in p.name.lower())
    assert injury_route_files == ["injury.py"]


def test_only_the_approved_injury_schemas_exist_in_api_schemas():
    """Static guard: src/api/schemas.py must define exactly the two approved Injury Pydantic response schemas -- no speculative extra Injury schema."""
    from src.api import schemas as api_schemas

    injury_schema_names = [
        name for name, obj in vars(api_schemas).items() if isinstance(obj, type) and "Injury" in name
    ]
    assert sorted(injury_schema_names) == ["InjuryEntryResponse", "TeamInjuryReportResponse"]


def test_only_the_approved_api_modules_reference_the_injury_service():
    """Static guard: since Phase 6 Slice 6B, exactly src/api/routes/injury.py may reference src.services.injury/the Injury authority under src/api/ -- no other module (main.py, schemas.py, errors.py, teams.py, players.py, rankings.py, deps.py) may reach into it directly."""
    import pathlib

    from src.api import main

    api_dir = pathlib.Path(main.__file__).parent
    approved = {api_dir / "routes" / "injury.py"}
    for py_file in api_dir.rglob("*.py"):
        source = py_file.read_text(encoding="utf-8")
        references_injury = "services.injury" in source or "build_injury_report" in source
        if py_file in approved:
            assert references_injury, f"expected {py_file} to reference the Injury service"
        else:
            assert not references_injury, f"unexpected reference to the Injury service found in {py_file}"


def test_importing_the_api_does_not_invoke_injury_logic(monkeypatch, tmp_path):
    """Constructing the application and generating its OpenAPI schema must never call into the Injury authority -- it is reachable only from direct Python code this slice, never from app construction or request handling."""
    import src.features.team_summary as team_summary_module

    calls = []
    monkeypatch.setattr(team_summary_module, "build_injury_report", lambda *a, **k: calls.append(1))

    bad_path = tmp_path / "no_db.db"
    app = create_app(database_url=f"sqlite+pysqlite:///{bad_path.as_posix()}")
    app.openapi()
    assert calls == []


def test_team_player_rankings_and_diagnostics_unaffected_by_the_injury_service(migrated_db_url, auth_headers, rankings_configured):
    """Confirms Slice 6A/6B introduced no regression in existing Team/Player/Rankings/diagnostic behavior."""
    from fastapi.testclient import TestClient

    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        health = client.get("/v1/health")
        ready = client.get("/v1/ready")
        team_missing_auth = client.get("/v1/teams/1")
        player_missing_auth = client.get("/v1/players/x/raw-statistics")
        rankings_missing_auth = client.get("/v1/rankings/net")
        team_valid_auth = client.get("/v1/teams/1", headers=auth_headers)
        player_valid_auth = client.get("/v1/players/x/raw-statistics", headers=auth_headers)
        rankings_valid_auth = client.get("/v1/rankings/net", headers=auth_headers)

    assert health.status_code == 200
    assert ready.status_code == 200
    assert team_missing_auth.status_code == 401
    assert player_missing_auth.status_code == 401
    assert rankings_missing_auth.status_code == 401
    assert team_valid_auth.status_code == 404
    assert player_valid_auth.status_code == 404
    assert rankings_valid_auth.status_code == 200


def test_exactly_one_matchup_route_exists(tmp_path):
    """
    Phase 6, Slice 7B exposes the Matchup service via exactly one
    protected, read-only comparison route --
    `POST /v1/matchups/feature-comparison` -- no alternate, alias,
    debug, or convenience Matchup route.
    """
    bad_path = tmp_path / "no_db.db"
    app = create_app(database_url=f"sqlite+pysqlite:///{bad_path.as_posix()}")
    schema = app.openapi()
    matchup_paths = sorted(p for p in schema["paths"] if "matchup" in p.lower())
    assert matchup_paths == ["/v1/matchups/feature-comparison"]


def test_exactly_one_matchup_router_module_exists():
    """Static guard: exactly one Matchup router file exists (src/api/routes/matchup.py) -- no second/duplicate router."""
    import pathlib

    from src.api import main

    routes_dir = pathlib.Path(main.__file__).parent / "routes"
    matchup_route_files = sorted(p.name for p in routes_dir.glob("*.py") if "matchup" in p.name.lower())
    assert matchup_route_files == ["matchup.py"]


def test_only_the_approved_matchup_schemas_exist_in_api_schemas():
    """Static guard: src/api/schemas.py must define exactly the four approved Matchup Pydantic schemas -- no speculative extra Matchup schema."""
    from src.api import schemas as api_schemas

    matchup_schema_names = [
        name for name, obj in vars(api_schemas).items() if isinstance(obj, type) and "Matchup" in name
    ]
    assert sorted(matchup_schema_names) == ["MatchupFeatureComparisonRequest", "MatchupFeatureComparisonResponse"]


def test_only_the_approved_api_modules_import_the_matchup_service():
    """
    Static guard: since Phase 6 Slice 7B, exactly src/api/routes/matchup.py
    may *import* src.services.matchup/src.features.matchup_features under
    src/api/ -- no other module (main.py, errors.py, teams.py, players.py,
    rankings.py, injury.py, deps.py) may reach into it directly. Uses
    AST-level import inspection rather than a substring search, since
    src/api/schemas.py's own docstrings legitimately *mention*
    MATCHUP_FEATURE_LABELS in prose without importing it.
    """
    import ast
    import pathlib

    from src.api import main

    api_dir = pathlib.Path(main.__file__).parent
    approved = {api_dir / "routes" / "matchup.py"}
    for py_file in api_dir.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
        imports_matchup = any("services.matchup" in m or "matchup_features" in m for m in imported_modules)
        if py_file in approved:
            assert imports_matchup, f"expected {py_file} to import the Matchup service"
        else:
            assert not imports_matchup, f"unexpected import of the Matchup service found in {py_file}"


def test_importing_the_api_does_not_invoke_matchup_logic(monkeypatch, tmp_path):
    """Constructing the application and generating its OpenAPI schema must never call into the Matchup analytical authority -- it is only invoked from an actual request."""
    import src.features.matchup_features as matchup_features_module

    calls = []
    monkeypatch.setattr(matchup_features_module, "build_matchup_feature_comparison", lambda *a, **k: calls.append(1))

    bad_path = tmp_path / "no_db.db"
    app = create_app(database_url=f"sqlite+pysqlite:///{bad_path.as_posix()}")
    app.openapi()
    assert calls == []


def test_team_player_rankings_injury_and_diagnostics_unaffected_by_the_matchup_service(
    migrated_db_url, auth_headers, rankings_configured
):
    """Confirms Slice 7A/7B introduced no regression in existing Team/Player/Rankings/Injury/diagnostic behavior."""
    from fastapi.testclient import TestClient

    app = create_app(database_url=migrated_db_url)
    with TestClient(app) as client:
        health = client.get("/v1/health")
        ready = client.get("/v1/ready")
        team_missing_auth = client.get("/v1/teams/1")
        player_missing_auth = client.get("/v1/players/x/raw-statistics")
        rankings_missing_auth = client.get("/v1/rankings/net")
        injury_missing_auth = client.get("/v1/teams/1/injuries")
        team_valid_auth = client.get("/v1/teams/1", headers=auth_headers)
        player_valid_auth = client.get("/v1/players/x/raw-statistics", headers=auth_headers)
        rankings_valid_auth = client.get("/v1/rankings/net", headers=auth_headers)
        injury_valid_auth = client.get("/v1/teams/1/injuries", headers=auth_headers)

    assert health.status_code == 200
    assert ready.status_code == 200
    assert team_missing_auth.status_code == 401
    assert player_missing_auth.status_code == 401
    assert rankings_missing_auth.status_code == 401
    assert injury_missing_auth.status_code == 401
    assert team_valid_auth.status_code == 404
    assert player_valid_auth.status_code == 404
    assert rankings_valid_auth.status_code == 200
    assert injury_valid_auth.status_code == 404
