"""
Phase 3, batch 4: The Odds API canonical ingestion.
No live requests, no credentials -- narrowly constructed mocked JSON
shaped from the real Odds API v4 schema (confirmed against
build_market_consensus's own field access: bookmakers[].markets[].outcomes[].point).
"""
from src.identity.contracts import GameIdentity, SportsbookIdentity, TeamIdentity, UnresolvedIdentity
from src.ingestion.odds_ingestion import (
    OddsOutcomeRecord,
    find_duplicate_odds_rows,
    parse_commence_time_utc,
    parse_odds_event_outcomes,
    resolve_odds_outcome_row,
    run_odds_ingestion_from_json,
)


def _event(
    *,
    event_id="evt-1",
    home="Duke Blue Devils",
    away="St. Bonaventure Bonnies",
    commence="2026-01-15T00:00:00Z",
    bookmakers=None,
):
    return {
        "id": event_id,
        "sport_key": "basketball_ncaab",
        "commence_time": commence,
        "home_team": home,
        "away_team": away,
        "bookmakers": bookmakers if bookmakers is not None else [
            {
                "key": "fanduel", "title": "FanDuel", "last_update": "2026-01-14T23:00:00Z",
                "markets": [
                    {"key": "spreads", "outcomes": [
                        {"name": "Duke", "price": -110, "point": -12.5},
                        {"name": "St. Bonaventure", "price": -110, "point": 12.5},
                    ]},
                    {"key": "totals", "outcomes": [
                        {"name": "Over", "price": -105, "point": 145.5},
                        {"name": "Under", "price": -115, "point": 145.5},
                    ]},
                    {"key": "h2h", "outcomes": [
                        {"name": "Duke", "price": -500},
                        {"name": "St. Bonaventure", "price": 400},
                    ]},
                ],
            },
        ],
    }


# ---------------------------------------------------------------------------
# Parsing: valid event, missing bookmakers/markets, malformed payloads
# ---------------------------------------------------------------------------

def test_valid_event_ingestion_produces_one_row_per_outcome():
    rows = parse_odds_event_outcomes(_event())
    assert len(rows) == 6  # 2 spreads + 2 totals + 2 h2h


def test_source_event_id_preserved():
    rows = parse_odds_event_outcomes(_event(event_id="evt-999"))
    assert all(r["source_event_id"] == "evt-999" for r in rows)


def test_home_away_preserved_exactly_as_source():
    rows = parse_odds_event_outcomes(_event(home="Duke Blue Devils", away="St. Bonaventure Bonnies"))
    assert rows[0]["raw_home_team"] == "Duke Blue Devils"
    assert rows[0]["raw_away_team"] == "St. Bonaventure Bonnies"


def test_missing_bookmakers_yields_zero_rows_not_a_crash():
    event = _event(bookmakers=[])
    assert parse_odds_event_outcomes(event) == []


def test_missing_markets_yields_zero_rows_for_that_bookmaker():
    event = _event(bookmakers=[{"key": "fanduel", "title": "FanDuel", "markets": []}])
    assert parse_odds_event_outcomes(event) == []


def test_missing_price_or_point_preserved_as_none_not_fabricated():
    event = _event(bookmakers=[{
        "key": "fanduel", "title": "FanDuel",
        "markets": [{"key": "h2h", "outcomes": [{"name": "Duke", "price": None}]}],
    }])
    rows = parse_odds_event_outcomes(event)
    assert rows[0]["price"] is None
    assert rows[0]["point"] is None


def test_malformed_partial_payload_missing_top_level_fields_no_crash():
    partial = {"id": "evt-x"}  # no bookmakers, no team names, no commence_time
    rows = parse_odds_event_outcomes(partial)
    assert rows == []


def test_moneyline_outcomes_have_no_point():
    rows = parse_odds_event_outcomes(_event())
    h2h_rows = [r for r in rows if r["market_key"] == "h2h"]
    assert all(r["point"] is None for r in h2h_rows)


def test_spread_outcomes_preserve_point_signs():
    rows = parse_odds_event_outcomes(_event())
    spread_rows = {r["outcome_name"]: r["point"] for r in rows if r["market_key"] == "spreads"}
    assert spread_rows["Duke"] == -12.5
    assert spread_rows["St. Bonaventure"] == 12.5


def test_total_outcomes_preserve_point():
    rows = parse_odds_event_outcomes(_event())
    total_rows = {r["outcome_name"]: r["point"] for r in rows if r["market_key"] == "totals"}
    assert total_rows["Over"] == 145.5
    assert total_rows["Under"] == 145.5


# ---------------------------------------------------------------------------
# Commence-time parsing
# ---------------------------------------------------------------------------

def test_utc_commence_time_parsing():
    dt = parse_commence_time_utc("2026-01-15T19:00:00Z")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 1 and dt.day == 15
    assert dt.utcoffset().total_seconds() == 0


def test_commence_time_missing_returns_none():
    assert parse_commence_time_utc(None) is None
    assert parse_commence_time_utc("") is None


def test_commence_time_malformed_returns_none_not_a_crash():
    assert parse_commence_time_utc("not-a-date") is None


# ---------------------------------------------------------------------------
# Team resolution: exact, ambiguous Miami/Loyola, unresolved
# ---------------------------------------------------------------------------

def test_exact_team_resolution():
    row = parse_odds_event_outcomes(_event())[0]
    record = resolve_odds_outcome_row(row, season=2026)
    assert isinstance(record.home_team_identity, TeamIdentity)
    assert isinstance(record.away_team_identity, TeamIdentity)
    assert record.home_team_identity.display_name == "Duke"
    assert record.away_team_identity.display_name == "St. Bonaventure"


def test_ambiguous_miami_resolution():
    event = _event(home="Miami", away="Duke Blue Devils")
    row = parse_odds_event_outcomes(event)[0]
    record = resolve_odds_outcome_row(row, season=2026)
    assert not isinstance(record.home_team_identity, TeamIdentity)
    assert record.home_team_identity.reason == "ambiguous"
    assert record.resolution_status == "team_unresolved"


def test_ambiguous_loyola_resolution():
    event = _event(home="Loyola", away="Duke Blue Devils")
    row = parse_odds_event_outcomes(event)[0]
    record = resolve_odds_outcome_row(row, season=2026)
    assert not isinstance(record.home_team_identity, TeamIdentity)


def test_unresolved_team_behavior():
    event = _event(home="Definitely Not A Real School Xyzzy123", away="Duke Blue Devils")
    row = parse_odds_event_outcomes(event)[0]
    record = resolve_odds_outcome_row(row, season=2026)
    assert not isinstance(record.home_team_identity, TeamIdentity)
    assert record.home_team_identity.reason == "not_found"
    assert record.game_identity is None  # never fabricated when a team is unresolved
    assert record.resolution_status == "team_unresolved"


# ---------------------------------------------------------------------------
# Sportsbook resolution: FanDuel, unknown books preserved not dropped
# ---------------------------------------------------------------------------

def test_fanduel_sportsbook_resolution():
    row = parse_odds_event_outcomes(_event())[0]
    record = resolve_odds_outcome_row(row, season=2026)
    assert isinstance(record.bookmaker_identity, SportsbookIdentity)
    assert record.bookmaker_identity.canonical_key == "fanduel"


def test_unknown_sportsbook_preserved_as_unresolved_not_dropped():
    event = _event(bookmakers=[{
        "key": "some_new_book", "title": "Some New Book",
        "markets": [{"key": "h2h", "outcomes": [{"name": "Duke", "price": -500}]}],
    }])
    row = parse_odds_event_outcomes(event)[0]
    record = resolve_odds_outcome_row(row, season=2026)
    assert not isinstance(record.bookmaker_identity, SportsbookIdentity)
    assert record.bookmaker_key_raw == "some_new_book"  # preserved, not dropped
    assert record.bookmaker_title_raw == "Some New Book"


def test_sportsbook_never_fabricated_when_key_missing():
    event = _event(bookmakers=[{
        "key": None, "title": None,
        "markets": [{"key": "h2h", "outcomes": [{"name": "Duke", "price": -500}]}],
    }])
    row = parse_odds_event_outcomes(event)[0]
    record = resolve_odds_outcome_row(row, season=2026)
    assert not isinstance(record.bookmaker_identity, SportsbookIdentity)


# ---------------------------------------------------------------------------
# Game identity: provisional, source event ID preserved separately
# ---------------------------------------------------------------------------

def test_provisional_game_id_behavior():
    row = parse_odds_event_outcomes(_event())[0]
    record = resolve_odds_outcome_row(row, season=2026)
    assert isinstance(record.game_identity, GameIdentity)
    assert record.game_identity.is_provisional is True
    assert record.game_identity.home_team_id == record.home_team_identity.team_id
    assert record.game_identity.away_team_id == record.away_team_identity.team_id


def test_source_event_id_never_promoted_to_authoritative_game_id():
    """
    The raw Odds API event id is preserved on the record, but the
    canonical game_identity is never derived FROM it (per instruction #7 --
    not independently verified as authoritative).
    """
    row = parse_odds_event_outcomes(_event(event_id="evt-abc-123"))[0]
    record = resolve_odds_outcome_row(row, season=2026)
    assert record.source_event_id == "evt-abc-123"
    assert record.game_identity.sources == {}  # no source id attached -- purely structural/provisional
    assert record.game_identity.is_provisional is True


def test_never_derives_game_solely_from_team_name_similarity():
    """No difflib/fuzzy step anywhere in this module's team or game resolution path."""
    import ast
    import inspect

    import src.ingestion.odds_ingestion as mod

    tree = ast.parse(inspect.getsource(mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "difflib" not in imported


def test_neutral_site_symmetry_only_when_explicitly_requested():
    row = parse_odds_event_outcomes(_event())[0]
    default_record = resolve_odds_outcome_row(row, season=2026)
    assert default_record.game_identity.neutral_site is False  # never assumed

    neutral_record = resolve_odds_outcome_row(row, season=2026, neutral_site=True)
    assert neutral_record.game_identity.neutral_site is True


# ---------------------------------------------------------------------------
# Duplicate events / rows
# ---------------------------------------------------------------------------

def test_duplicate_events_detected():
    events = [_event(event_id="evt-1"), _event(event_id="evt-1")]
    result = run_odds_ingestion_from_json(events, season=2026)
    assert len(result.duplicate_rows) > 0


def test_duplicate_bookmaker_market_outcome_rows_detected():
    event = _event(bookmakers=[
        {"key": "fanduel", "title": "FanDuel", "markets": [
            {"key": "h2h", "outcomes": [{"name": "Duke", "price": -500}]},
        ]},
        {"key": "fanduel", "title": "FanDuel", "markets": [
            {"key": "h2h", "outcomes": [{"name": "Duke", "price": -500}]},
        ]},
    ])
    result = run_odds_ingestion_from_json([event], season=2026)
    assert len(result.duplicate_rows) == 1
    assert result.duplicate_rows[0]["count"] == 2


def test_no_duplicates_for_distinct_outcomes():
    result = run_odds_ingestion_from_json([_event()], season=2026)
    assert result.duplicate_rows == []


# ---------------------------------------------------------------------------
# Determinism / idempotency / ordering
# ---------------------------------------------------------------------------

def test_deterministic_record_ordering():
    events = [_event()]
    r1 = run_odds_ingestion_from_json(events, season=2026)
    r2 = run_odds_ingestion_from_json(events, season=2026)
    order1 = [(r.market_key, r.outcome_name) for r in r1.records]
    order2 = [(r.market_key, r.outcome_name) for r in r2.records]
    assert order1 == order2


def test_idempotent_repeated_ingestion_no_duplicate_records():
    events = [_event()]
    r1 = run_odds_ingestion_from_json(events, season=2026)
    r2 = run_odds_ingestion_from_json(events, season=2026)
    assert len(r1.records) == len(r2.records) == 6


# ---------------------------------------------------------------------------
# Raw provenance retention
# ---------------------------------------------------------------------------

def test_raw_json_provenance_retained():
    result = run_odds_ingestion_from_json([_event()], season=2026, fixture_provenance="mocked_fixture:test")
    assert result.evidence.fixture_provenance == "mocked_fixture:test"
    assert result.evidence.source_endpoint == "https://api.the-odds-api.com/v4/sports/basketball_ncaab/odds"
    assert result.evidence.parser_version
    assert result.evidence.collected_at_utc


def test_bookmaker_last_update_preserved():
    row = parse_odds_event_outcomes(_event())[0]
    assert row["bookmaker_last_update"] == "2026-01-14T23:00:00Z"


# ---------------------------------------------------------------------------
# No betting-formula execution, no live network, no credential exposure
# ---------------------------------------------------------------------------

def test_no_betting_formula_or_consensus_calculation():
    import ast
    import inspect

    import src.ingestion.odds_ingestion as mod

    tree = ast.parse(inspect.getsource(mod))
    called_names = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not ({"build_market_consensus", "find_game_odds", "get_live_ncaab_odds"} & called_names)
    source = inspect.getsource(mod)
    assert "np.mean" not in source
    assert "compute_edge" not in source


def test_no_live_network_access():
    import ast
    import inspect

    import src.ingestion.odds_ingestion as mod

    tree = ast.parse(inspect.getsource(mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "requests" not in imported


def test_no_credential_exposure():
    """
    Confirmed via AST (not a raw substring search -- this module's own
    docstring legitimately mentions ODDS_API_KEY in prose explaining why
    it is NOT touched, which a substring check would misflag): no call to
    os.getenv/os.environ anywhere in the module's actual code.
    """
    import ast
    import inspect

    import src.ingestion.odds_ingestion as mod

    tree = ast.parse(inspect.getsource(mod))
    called_attrs = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "getenv" not in called_attrs

    accessed_names = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert "ODDS_API_KEY" not in accessed_names


def test_no_production_file_mutation():
    import ast
    import inspect

    import src.ingestion.odds_ingestion as mod

    tree = ast.parse(inspect.getsource(mod))
    called_names = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "open" not in called_names


def test_does_not_mutate_team_registry():
    from src import slug_utils

    before_size = len(slug_utils.TEAM_REGISTRY)
    run_odds_ingestion_from_json([_event()], season=2026)
    assert len(slug_utils.TEAM_REGISTRY) == before_size


# ---------------------------------------------------------------------------
# Legacy return-format parity
# ---------------------------------------------------------------------------

def test_result_object_carries_no_legacy_odds_list_shape():
    from dataclasses import fields

    from src.ingestion.odds_ingestion import OddsIngestionResult

    field_names = {f.name for f in fields(OddsIngestionResult)}
    assert "odds_data" not in field_names
    assert "consensus" not in field_names


def test_legacy_odds_identity_functions_still_importable_and_unchanged():
    from src.identity.odds_identity import canonical_odds_team_v15, find_game_odds_v15_verbatim

    assert canonical_odds_team_v15("Duke Blue Devils") == "duke"
    assert callable(find_game_odds_v15_verbatim)
