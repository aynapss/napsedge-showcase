"""Phase 2, Gate 2 tests: season identity. No live requests."""
import pytest

from src.identity.contracts import SeasonIdentity, UnresolvedIdentity
from src.identity.season_identity import parse_season_label, resolve_season_identity


def test_resolve_season_identity_from_int():
    result = resolve_season_identity(2026)
    assert isinstance(result, SeasonIdentity)
    assert result.season_id == 2026
    assert result.start_year == 2025
    assert result.end_year == 2026
    assert result.label == "2025-26"


def test_resolve_season_identity_from_numeric_string_preserves_compatibility():
    result = resolve_season_identity("2026")
    assert isinstance(result, SeasonIdentity)
    assert result.season_id == 2026


def test_resolve_season_identity_rejects_bool():
    result = resolve_season_identity(True)
    assert not isinstance(result, SeasonIdentity)


@pytest.mark.parametrize("bad", [-5, 0, 99999, "not-a-year", "20.26", None])
def test_resolve_season_identity_rejects_malformed_years(bad):
    result = resolve_season_identity(bad)
    assert not isinstance(result, SeasonIdentity)
    assert result.reason == "not_found"


def test_adjacent_seasons_are_distinct():
    a = resolve_season_identity(2025)
    b = resolve_season_identity(2026)
    assert a.season_id != b.season_id
    assert a.end_year + 1 == b.end_year


def test_repeated_execution_is_deterministic():
    ids = {resolve_season_identity(2026).season_id for _ in range(3)}
    assert len(ids) == 1


def test_parse_season_label_matches_ending_year_convention():
    result = parse_season_label("2025-26")
    assert isinstance(result, SeasonIdentity)
    assert result.season_id == 2026  # ending year, not the label's leading 2025


def test_parse_season_label_rejects_non_adjacent_years():
    result = parse_season_label("2025-30")
    assert not isinstance(result, SeasonIdentity)
    assert result.reason == "not_found"


def test_parse_season_label_rejects_malformed_label():
    result = parse_season_label("not a season")
    assert not isinstance(result, SeasonIdentity)
    assert result.reason == "not_found"


def test_parse_season_label_and_int_input_agree():
    """Prevents accidental mixing of calendar year and season-ending year."""
    from_label = parse_season_label("2025-26")
    from_int = resolve_season_identity(2026)
    assert from_label.season_id == from_int.season_id


def test_season_identity_never_silently_inferred():
    """resolve_season_identity requires an explicit value -- no default/inference path exists."""
    import inspect

    from src.identity.season_identity import resolve_season_identity as fn

    sig = inspect.signature(fn)
    assert sig.parameters["value"].default is inspect.Parameter.empty
