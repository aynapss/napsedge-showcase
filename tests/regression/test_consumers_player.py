"""
Phase 7 Slice 3A: tests for `src.consumers.player.present_player_raw_statistics`.
Uses only synthetic `PlayerRawStatisticsResult`/`PlayerStatRecord`
instances -- never the real database or `src.services.player` itself.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect

import src.consumers.player as player_adapter_module
from src.consumers.player import present_player_raw_statistics
from src.services.contracts import PlayerRawStatisticsResult, PlayerStatRecord


def _stat_record(**overrides) -> PlayerStatRecord:
    base = {f.name: None for f in dataclasses.fields(PlayerStatRecord)}
    base.update(
        stat_table="PerGame",
        season_id=2026,
        team_id=1,
        source_artifact_id=42,
        player_key="test-player-key",
        source_player_name="Test Player",
        validation_status="valid",
    )
    base.update(overrides)
    return PlayerStatRecord(**base)


def test_not_found_is_an_explicit_not_found_presentation():
    presentation = present_player_raw_statistics(PlayerRawStatisticsResult(outcome="not_found"))
    assert presentation.status == "not_found"
    assert presentation.message is not None


def test_known_player_with_empty_stats_is_a_successful_known_empty_presentation():
    result = PlayerRawStatisticsResult(
        outcome="found", player_id="p1", display_name="Test Player", team_id=1, season_id=2026, stats=()
    )
    presentation = present_player_raw_statistics(result)
    assert presentation.status == "ok"
    assert presentation.rows == ()
    assert presentation.summary is not None


def test_known_empty_and_not_found_are_structurally_distinct():
    not_found = present_player_raw_statistics(PlayerRawStatisticsResult(outcome="not_found"))
    known_empty = present_player_raw_statistics(
        PlayerRawStatisticsResult(outcome="found", player_id="p1", display_name="X", team_id=1, season_id=2026, stats=())
    )
    assert not_found.status != known_empty.status
    assert not_found != known_empty


def test_found_with_records_produces_one_row_per_record():
    records = (_stat_record(stat_table="PerGame"), _stat_record(stat_table="Totals"))
    result = PlayerRawStatisticsResult(
        outcome="found", player_id="p1", display_name="Test Player", team_id=1, season_id=2026, stats=records
    )
    presentation = present_player_raw_statistics(result)
    assert presentation.status == "ok"
    assert len(presentation.rows) == 2


def test_exact_field_allowlist_excludes_only_player_key():
    all_fields = {f.name for f in dataclasses.fields(PlayerStatRecord)}
    result = PlayerRawStatisticsResult(
        outcome="found", player_id="p1", display_name="Test Player", team_id=1, season_id=2026, stats=(_stat_record(),)
    )
    presentation = present_player_raw_statistics(result)
    row_labels = {f.label for f in presentation.rows[0]}
    assert all_fields - row_labels == {"player_key"}
    assert row_labels == all_fields - {"player_key"}


def test_player_key_is_never_present_in_any_field_or_row_label():
    result = PlayerRawStatisticsResult(
        outcome="found", player_id="p1", display_name="Test Player", team_id=1, season_id=2026, stats=(_stat_record(),)
    )
    presentation = present_player_raw_statistics(result)
    all_labels = {f.label for f in presentation.fields} | {f.label for row in presentation.rows for f in row}
    assert "player_key" not in all_labels


def test_player_key_value_never_leaks_even_as_a_value():
    record = _stat_record(player_key="super-secret-internal-key-xyz")
    result = PlayerRawStatisticsResult(
        outcome="found", player_id="p1", display_name="Test Player", team_id=1, season_id=2026, stats=(record,)
    )
    presentation = present_player_raw_statistics(result)
    rendered = repr(presentation)
    assert "super-secret-internal-key-xyz" not in rendered


def test_validation_status_is_retained_exactly_as_stored():
    for status in ("valid", "pending", "conflicting", "invalid", "unresolved"):
        record = _stat_record(validation_status=status)
        result = PlayerRawStatisticsResult(
            outcome="found", player_id="p1", display_name="Test Player", team_id=1, season_id=2026, stats=(record,)
        )
        presentation = present_player_raw_statistics(result)
        value = next(f.value for f in presentation.rows[0] if f.label == "validation_status")
        assert value == status


def test_none_values_are_preserved_as_none_not_a_placeholder():
    record = _stat_record(fg_pct=None, source_position=None)
    result = PlayerRawStatisticsResult(
        outcome="found", player_id="p1", display_name="Test Player", team_id=1, season_id=2026, stats=(record,)
    )
    presentation = present_player_raw_statistics(result)
    fg_pct_field = next(f for f in presentation.rows[0] if f.label == "fg_pct")
    position_field = next(f for f in presentation.rows[0] if f.label == "source_position")
    assert fg_pct_field.value is None
    assert position_field.value is None


def test_stored_zero_and_false_values_are_preserved_not_treated_as_missing():
    record = _stat_record(games_played=0, fg_made=0.0, blk=0.0)
    result = PlayerRawStatisticsResult(
        outcome="found", player_id="p1", display_name="Test Player", team_id=1, season_id=2026, stats=(record,)
    )
    presentation = present_player_raw_statistics(result)
    games_field = next(f for f in presentation.rows[0] if f.label == "games_played")
    fg_field = next(f for f in presentation.rows[0] if f.label == "fg_made")
    assert games_field.value == "0"
    assert fg_field.value == "0.0"


def test_deterministic_ordering_matches_stats_tuple_order():
    records = (
        _stat_record(source_player_name="A", stat_table="PerGame"),
        _stat_record(source_player_name="B", stat_table="Totals"),
        _stat_record(source_player_name="C", stat_table="Advanced"),
    )
    result = PlayerRawStatisticsResult(
        outcome="found", player_id="p1", display_name="Test Player", team_id=1, season_id=2026, stats=records
    )
    presentation = present_player_raw_statistics(result)
    stat_tables_shown = [next(f.value for f in row if f.label == "stat_table") for row in presentation.rows]
    assert stat_tables_shown == ["PerGame", "Totals", "Advanced"]


def test_source_artifact_id_is_retained():
    record = _stat_record(source_artifact_id=999)
    result = PlayerRawStatisticsResult(
        outcome="found", player_id="p1", display_name="Test Player", team_id=1, season_id=2026, stats=(record,)
    )
    presentation = present_player_raw_statistics(result)
    value = next(f.value for f in presentation.rows[0] if f.label == "source_artifact_id")
    assert value == "999"


def test_player_metadata_fields_are_exposed_separately_from_rows():
    result = PlayerRawStatisticsResult(
        outcome="found", player_id="p42", display_name="Metadata Player", team_id=7, season_id=2025, stats=(_stat_record(),)
    )
    presentation = present_player_raw_statistics(result)
    field_labels = {f.label for f in presentation.fields}
    assert field_labels == {"Player", "Player ID", "Team ID", "Season ID"}
    assert presentation.title == "Metadata Player"


def test_repeated_calls_produce_equal_presentations():
    result = PlayerRawStatisticsResult(
        outcome="found", player_id="p1", display_name="Test Player", team_id=1, season_id=2026, stats=(_stat_record(),)
    )
    assert present_player_raw_statistics(result) == present_player_raw_statistics(result)


def test_no_calculation_or_defaulting_occurs():
    """A field value round-trips exactly as supplied, with no rounding/normalization."""
    record = _stat_record(fg_pct=0.123456789)
    result = PlayerRawStatisticsResult(
        outcome="found", player_id="p1", display_name="Test Player", team_id=1, season_id=2026, stats=(record,)
    )
    presentation = present_player_raw_statistics(result)
    value = next(f.value for f in presentation.rows[0] if f.label == "fg_pct")
    assert value == str(0.123456789)


def test_module_makes_no_database_service_or_calculation_call():
    source = inspect.getsource(player_adapter_module)
    forbidden = ["get_player_raw_statistics(", "session.", "Session(", "round(", "sum(", "resolve_"]
    for token in forbidden:
        assert token not in source, f"unexpected {token!r} found in src/consumers/player.py"


def test_module_imports_no_framework_database_ovr_or_notebook_dependency():
    tree = ast.parse(inspect.getsource(player_adapter_module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = (
        "discord", "streamlit", "fastapi", "starlette", "pydantic",
        "sqlalchemy", "src.db", "src.ovr_model", "src.identity",
        "requests", "httpx", "urllib", "socket", "nbformat", "nbconvert",
    )
    for name in imported:
        assert not any(name == f or name.startswith(f + ".") for f in forbidden), f"unexpected import {name!r}"
