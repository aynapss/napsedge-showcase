"""
Phase 6, Slice 6A: extraction-parity and unit tests for the newly public
`src.features.team_summary.build_injury_report` (extracted this slice from
the original private `_injury_report`) and the new, read-only
`src.services.injury.get_team_injury_report` service built on top of it.

No HTTP endpoint exists yet -- this file proves the extracted authority and
the service layer, not an API. Every test uses either a temporary synthetic
cache directory (via the new `search_paths` parameter) or the real,
already-committed `TeamDump/` fixtures, read-only, clearly marked
`@pytest.mark.integration` -- never mutated by any test here.
"""
from __future__ import annotations

import ast
import inspect
import json

import pytest
from sqlalchemy import create_engine, event, text

from src.config.paths import CBB_DIR
from src.db.base import Base
from src.db.session import build_session_factory, unit_of_work
from src.db.repositories import TeamRepository
from src.exceptions import InjuryDataIntegrityError
from src.features import team_summary as team_summary_module
from src.features.team_summary import _injury_report, build_injury_report, build_team_summary
from src.services import injury as injury_service_module
from src.services.contracts import InjuryEntry, TeamInjuryLookupResult, TeamInjuryReport
from src.services.injury import get_injury_report_for_team, get_team_injury_report

# ---------------------------------------------------------------------------
# Pre-extraction outputs, captured (read-only) from the original
# `_injury_report` before this slice's edit -- see the completion report for
# the exact commands used to capture these. Real, unmodified TeamDump fixtures.
# ---------------------------------------------------------------------------

REAL_CACHE_PARITY = {
    "duke": {
        "status": "loaded",
        "source": None,  # asserted separately -- machine-specific absolute path
        "injuries": [
            {"player": "ufochukwu", "status": "out", "detail": "Out - Undisclosed"},
            {"player": "wilkins", "status": "out", "detail": "Out - Redshirt"},
        ],
        "impactful_count": 2,
    },
    "connecticut": {
        "status": "loaded",
        "source": None,
        "injuries": [],
        "impactful_count": 0,
    },
    "nonexistent-team-xyz-slice6a": {
        "status": "not_loaded",
        "source": None,
        "injuries": [],
        "impactful_count": 0,
        "message": "No confirmed local injury report loaded.",
    },
}


def _real_injury_cache_exists(team_slug: str) -> bool:
    """Mirrors build_injury_report's own two-candidate default lookup (src/features/team_summary.py)."""
    return (CBB_DIR / team_slug / f"{team_slug}_injuries.json").exists() or (
        CBB_DIR.parent / "injury_cache" / f"{team_slug}_injuries.json"
    ).exists()


# ---------------------------------------------------------------------------
# Extraction parity -- public function vs. captured pre-extraction behavior,
# and public function vs. the still-present private wrapper
# ---------------------------------------------------------------------------


_PARITY_PARAMS = [
    pytest.param(
        slug,
        marks=pytest.mark.skipif(
            expected["status"] == "loaded" and not _real_injury_cache_exists(slug),
            reason=f"real local Injury cache fixture for {slug!r} not present in this checkout",
        ),
    )
    for slug, expected in REAL_CACHE_PARITY.items()
]


@pytest.mark.integration
@pytest.mark.parametrize("team_slug", _PARITY_PARAMS)
def test_public_function_matches_captured_pre_extraction_output(team_slug):
    expected = REAL_CACHE_PARITY[team_slug]
    got = build_injury_report(team_slug)
    assert got["status"] == expected["status"]
    assert got["injuries"] == expected["injuries"]
    assert got["impactful_count"] == expected["impactful_count"]
    if expected["status"] == "not_loaded":
        assert got["source"] is None
        assert got["message"] == expected["message"]
    else:
        assert got["source"] is not None and got["source"].endswith(f"{team_slug}_injuries.json")


@pytest.mark.integration
@pytest.mark.parametrize("team_slug", list(REAL_CACHE_PARITY.keys()))
def test_private_wrapper_still_delegates_to_public_function_identically(team_slug):
    """The original call site (`_injury_report`) must return byte-identical output to the new public function -- proves the delegation introduced no behavior change."""
    assert _injury_report(team_slug) == build_injury_report(team_slug)


@pytest.mark.integration
def test_existing_caller_build_team_summary_is_unaffected():
    """build_team_summary (the one existing caller of _injury_report) must still embed the identical injury_report dict, unchanged."""
    summary = build_team_summary("duke", season=2026)
    assert summary["injury_report"] == build_injury_report("duke")


# ---------------------------------------------------------------------------
# Synthetic-cache behavior -- deterministic, no dependency on TeamDump/
# ---------------------------------------------------------------------------


def _write_injuries(tmp_path, name: str, payload) -> "object":
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_known_empty_report_is_loaded_not_unavailable(tmp_path):
    path = _write_injuries(tmp_path, "team_injuries.json", [])
    result = build_injury_report("team", search_paths=[path])
    assert result["status"] == "loaded"
    assert result["injuries"] == []
    assert result["impactful_count"] == 0
    assert "message" not in result


def test_missing_file_is_not_loaded(tmp_path):
    result = build_injury_report("team", search_paths=[tmp_path / "does_not_exist.json"])
    assert result["status"] == "not_loaded"
    assert result["source"] is None
    assert result["message"] == "No confirmed local injury report loaded."


def test_malformed_json_falls_through_to_not_loaded(tmp_path):
    path = tmp_path / "team_injuries.json"
    path.write_text("{not valid json", encoding="utf-8")
    result = build_injury_report("team", search_paths=[path])
    assert result["status"] == "not_loaded"


def test_non_list_json_falls_through_to_not_loaded(tmp_path):
    path = _write_injuries(tmp_path, "team_injuries.json", {"player": "not-a-list"})
    result = build_injury_report("team", search_paths=[path])
    assert result["status"] == "not_loaded"


def test_second_candidate_used_when_first_is_malformed(tmp_path):
    """Preserves the existing multi-candidate fallback behavior exactly -- a malformed first candidate must not prevent a valid second candidate from being used."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid", encoding="utf-8")
    good = _write_injuries(tmp_path, "good.json", [{"player": "smith", "status": "questionable"}])
    result = build_injury_report("team", search_paths=[bad, good])
    assert result["status"] == "loaded"
    assert result["source"] == str(good)


def test_deterministic_ordering_preserves_source_list_order(tmp_path):
    payload = [{"player": "a", "status": "out"}, {"player": "b", "status": "questionable"}, {"player": "c", "status": "doubtful"}]
    path = _write_injuries(tmp_path, "team_injuries.json", payload)
    result = build_injury_report("team", search_paths=[path])
    assert [item["player"] for item in result["injuries"]] == ["a", "b", "c"]


def test_repeated_calls_are_deterministic(tmp_path):
    path = _write_injuries(tmp_path, "team_injuries.json", [{"player": "a", "status": "out"}])
    first = build_injury_report("team", search_paths=[path])
    second = build_injury_report("team", search_paths=[path])
    assert first == second


def test_partial_entry_missing_fields_preserves_available_data(tmp_path):
    """An entry missing 'status'/'detail' must not be dropped or fabricated -- those keys are simply absent/None."""
    path = _write_injuries(tmp_path, "team_injuries.json", [{"player": "onlyname"}])
    result = build_injury_report("team", search_paths=[path])
    assert result["status"] == "loaded"
    assert result["injuries"] == [{"player": "onlyname"}]
    assert result["impactful_count"] == 0  # no "status" key means it never matches the impactful set


def test_source_status_designation_preserved_verbatim(tmp_path):
    """An unusual/free-text status string must pass through unchanged -- never normalized, mapped, or rejected."""
    path = _write_injuries(tmp_path, "team_injuries.json", [{"player": "x", "status": "Day-To-Day (Ankle)"}])
    result = build_injury_report("team", search_paths=[path])
    assert result["injuries"][0]["status"] == "Day-To-Day (Ankle)"
    assert result["impactful_count"] == 0  # not one of the fixed {"out","doubtful","questionable"} designations


def test_impactful_status_matching_is_case_insensitive_exactly_as_before(tmp_path):
    path = _write_injuries(tmp_path, "team_injuries.json", [{"player": "x", "status": "OUT"}, {"player": "y", "status": "Doubtful"}])
    result = build_injury_report("team", search_paths=[path])
    assert result["impactful_count"] == 2


def test_stored_false_and_zero_values_in_raw_items_are_preserved(tmp_path):
    path = _write_injuries(tmp_path, "team_injuries.json", [{"player": "x", "status": "out", "confirmed": False, "days_out": 0}])
    result = build_injury_report("team", search_paths=[path])
    item = result["injuries"][0]
    assert item["confirmed"] is False
    assert item["days_out"] == 0


def test_no_input_mutation_of_the_cache_file(tmp_path):
    path = _write_injuries(tmp_path, "team_injuries.json", [{"player": "x", "status": "out"}])
    before = path.read_bytes()
    build_injury_report("team", search_paths=[path])
    build_injury_report("team", search_paths=[path])
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# Service layer -- typed contract, orthogonal outcome semantics
# ---------------------------------------------------------------------------


def test_service_wraps_loaded_result_into_typed_contract(tmp_path):
    payload = [{"player": "a", "status": "out", "detail": "Out - Ankle"}]
    path = _write_injuries(tmp_path, "team_injuries.json", payload)
    result = get_team_injury_report("team", search_paths=[path])
    assert isinstance(result, TeamInjuryReport)
    assert result.outcome == "loaded"
    assert result.team_slug == "team"
    assert result.source_path == str(path)
    assert result.impactful_count == 1
    assert result.detail is None
    assert len(result.injuries) == 1
    entry = result.injuries[0]
    assert isinstance(entry, InjuryEntry)
    assert entry.player == "a"
    assert entry.status == "out"
    assert entry.detail == "Out - Ankle"
    assert entry.raw == payload[0]


def test_service_wraps_not_loaded_result_with_diagnostic_detail(tmp_path):
    result = get_team_injury_report("team", search_paths=[tmp_path / "missing.json"])
    assert result.outcome == "not_loaded"
    assert result.source_path is None
    assert result.injuries == ()
    assert result.impactful_count == 0
    assert result.detail == "No confirmed local injury report loaded."


def test_service_known_empty_is_loaded_with_zero_injuries_not_conflated_with_unavailable(tmp_path):
    path = _write_injuries(tmp_path, "team_injuries.json", [])
    result = get_team_injury_report("team", search_paths=[path])
    assert result.outcome == "loaded"
    assert result.injuries == ()
    assert result.detail is None  # detail is only ever populated for outcome="not_loaded"


def test_a_non_dict_list_element_preserves_the_pre_existing_crash_behavior(tmp_path):
    """
    Genuine, pre-existing limitation confirmed preserved by extraction, not
    fixed: the original (unmodified-in-substance) `_injury_report` already
    computed `impactful_count` via an unconditional `item.get(...)` over
    every list element, so a non-dict element already raised
    `AttributeError` before this slice's extraction. Silently catching this
    in the new public function or the service would be an unauthorized
    behavior change beyond "extract the existing logic unchanged" -- so
    every entry point (public function, service) must raise the identical
    exception type for identical input, proving the extraction did not
    alter this (admittedly rough) existing behavior.

    **This is a known service-boundary limitation, not yet safe for HTTP
    exposure** -- an uncaught `AttributeError` must not reach an HTTP
    client as a raw 500 with a stack trace; Slice 6B (a separate,
    not-yet-approved batch) must add an explicit, sanitized translation
    for this case (or a validation step) before any Injury route exists.
    Not fixed here, since doing so would be a behavior change beyond this
    slice's extraction-only scope.

    Because this is a raised exception, not a returned value, it cannot
    be confused with any typed outcome (known-empty, unavailable, a
    healthy player) -- `pytest.raises` succeeding here is itself the
    proof that no dict of any shape was returned for this input.
    """
    path = _write_injuries(tmp_path, "team_injuries.json", ["not-a-dict-entry"])

    with pytest.raises(AttributeError) as excinfo_public:
        build_injury_report("team", search_paths=[path])
    # The exception must originate inside the authoritative extracted
    # function's own module (team_summary.py), not inside the service --
    # confirms the service adds no new failure mode of its own here.
    assert str(excinfo_public.traceback[-1].path).endswith("team_summary.py")

    with pytest.raises(AttributeError) as excinfo_service:
        get_team_injury_report("team", search_paths=[path])
    assert str(excinfo_service.traceback[-1].path).endswith("team_summary.py")


def test_wrapper_and_default_caller_raise_the_identical_crash_via_default_paths(tmp_path, monkeypatch):
    """
    Proves the compatibility wrapper (`_injury_report`, used only through
    the default two-candidate lookup, never `search_paths`) and the one
    existing caller (`build_team_summary`) have not silently diverged from
    the public function's own behavior for this same malformed input --
    exercised through `CBB_DIR`, monkeypatched to a disposable tmp_path
    directory so no real `TeamDump/` file is ever touched.
    """
    fake_cbb_dir = tmp_path / "CBB"
    monkeypatch.setattr(team_summary_module, "CBB_DIR", fake_cbb_dir)

    team_slug = "malformed-crash-test-team"
    team_dir = fake_cbb_dir / team_slug
    team_dir.mkdir(parents=True)
    (team_dir / f"{team_slug}_injuries.json").write_text(json.dumps(["not-a-dict-entry"]), encoding="utf-8")

    with pytest.raises(AttributeError):
        _injury_report(team_slug)
    with pytest.raises(AttributeError):
        build_team_summary(team_slug, season=2026)


def test_service_repeated_calls_are_deterministic(tmp_path):
    path = _write_injuries(tmp_path, "team_injuries.json", [{"player": "a", "status": "out"}, {"player": "b", "status": "questionable"}])
    first = get_team_injury_report("team", search_paths=[path])
    second = get_team_injury_report("team", search_paths=[path])
    assert first == second


def test_service_no_fabricated_freshness_field():
    field_names = {f for f in TeamInjuryReport.__dataclass_fields__}
    assert "as_of" not in field_names
    assert "is_current" not in field_names
    assert "freshness" not in field_names


# ---------------------------------------------------------------------------
# Framework independence / no-network / no-database static guards
# ---------------------------------------------------------------------------


def test_injury_service_module_imports_no_fastapi_pydantic_or_network_function():
    tree = ast.parse(inspect.getsource(injury_service_module))
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
            imported_names.update(a.name for a in node.names)
    forbidden = ("fastapi", "starlette", "pydantic", "requests", "httpx", "urllib", "selenium")
    for name in imported_names:
        assert not any(f in name for f in forbidden), f"unexpected import {name!r} in src/services/injury.py"


def test_injury_service_module_contains_no_sql_or_repository_logic():
    """
    Phase 6, Slice 6B legitimately imports `sqlalchemy.orm.Session` (a
    type annotation only, to accept and pass through the caller's own
    session to `src.services.team.get_team`) -- this guard therefore
    checks for actual raw-SQL/repository construction, not the mere
    presence of the word "sqlalchemy". `get_injury_report_for_team` never
    constructs a repository, a `select(...)`, or a raw SQL string itself.
    """
    source = inspect.getsource(injury_service_module)
    forbidden = ["select(", "SELECT ", ".query(", 'text("', "text('", "Repository(", "session.execute"]
    for token in forbidden:
        assert token not in source, f"unexpected {token!r} found in src/services/injury.py"


def test_build_injury_report_module_imports_no_network_capable_function():
    """AST-level check on the extracted authority's own module -- team_summary.py must remain local-file-only for the injury path."""
    tree = ast.parse(inspect.getsource(team_summary_module))
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
            imported_names.update(a.name for a in node.names)
    forbidden = ("requests", "httpx", "urllib", "selenium")
    for name in imported_names:
        assert not any(f in name for f in forbidden), f"unexpected network-capable import {name!r} in src/features/team_summary.py"


def test_injury_service_succeeds_with_outbound_network_functions_disabled(tmp_path):
    """
    Dynamic proof at the direct service/function layer only -- deliberately
    NOT run through FastAPI TestClient (Slice 5B proved that globally
    patching socket.socket while using TestClient can deadlock AnyIO/httpx
    on Windows, unrelated to any real network path).
    """
    import socket

    def _network_disabled(*args, **kwargs):
        raise AssertionError("network access attempted during an Injury service call")

    import unittest.mock as mock

    path = _write_injuries(tmp_path, "team_injuries.json", [{"player": "a", "status": "out"}])
    with mock.patch.object(socket, "socket", _network_disabled):
        result = get_team_injury_report("team", search_paths=[path])
    assert result.outcome == "loaded"


def test_injury_service_makes_no_database_write(tmp_path):
    """
    The extracted authority module (`team_summary.py`, backing
    `build_injury_report`/`_injury_report`) must remain fully database-free
    -- confirmed structurally, no import at all. The Injury service module
    legitimately type-annotates with `sqlalchemy.orm.Session` (Phase 6,
    Slice 6B, to pass a caller-supplied session through to
    `src.services.team.get_team`), but must never construct its own engine
    or session, and must never write/commit -- it only ever reads via the
    already-tested Team service.
    """
    assert "sqlalchemy" not in inspect.getsource(team_summary_module)

    injury_source = inspect.getsource(injury_service_module)
    for forbidden in ("Session(", "create_engine", "engine.connect", "session.commit", "session.add", "session.delete"):
        assert forbidden not in injury_source


# ---------------------------------------------------------------------------
# get_injury_report_for_team -- Phase 6, Slice 6B canonical-ID coordination
# ---------------------------------------------------------------------------


@pytest.fixture()
def injury_engine():
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
def injury_session_factory(injury_engine):
    return build_session_factory(injury_engine)


INJURY_TEAM_ID = 970001
INJURY_TEAM_SLUG = "injury-coordination-test-team"


@pytest.fixture()
def seeded_injury_team(injury_session_factory):
    with unit_of_work(injury_session_factory) as session:
        TeamRepository(session).upsert(team_id=INJURY_TEAM_ID, display_name="Injury Test Team", sr_slug=INJURY_TEAM_SLUG)
    return INJURY_TEAM_ID


def test_get_injury_report_for_team_unknown_team_is_team_not_found(injury_session_factory):
    with unit_of_work(injury_session_factory) as session:
        result = get_injury_report_for_team(session, 1)
    assert isinstance(result, TeamInjuryLookupResult)
    assert result.outcome == "team_not_found"
    assert result.report is None


def test_get_injury_report_for_team_resolves_canonical_slug_and_loads_report(injury_session_factory, seeded_injury_team, tmp_path):
    path = _write_injuries(tmp_path, "team_injuries.json", [{"player": "x", "status": "out", "detail": "Out - Ankle"}])
    with unit_of_work(injury_session_factory) as session:
        result = get_injury_report_for_team(session, seeded_injury_team, search_paths=[path])
    assert result.outcome == "found"
    assert result.report.outcome == "loaded"
    assert result.report.team_slug == INJURY_TEAM_SLUG  # confirms the canonical slug, not a client-supplied one, was used
    assert result.report.impactful_count == 1


def test_get_injury_report_for_team_known_team_with_unavailable_data_is_not_loaded_not_team_not_found(
    injury_session_factory, seeded_injury_team, tmp_path
):
    """A known team whose Injury source is unavailable must never be conflated with an unknown team."""
    with unit_of_work(injury_session_factory) as session:
        result = get_injury_report_for_team(session, seeded_injury_team, search_paths=[tmp_path / "missing.json"])
    assert result.outcome == "found"
    assert result.report.outcome == "not_loaded"


def test_get_injury_report_for_team_known_team_with_known_empty_report(injury_session_factory, seeded_injury_team, tmp_path):
    path = _write_injuries(tmp_path, "team_injuries.json", [])
    with unit_of_work(injury_session_factory) as session:
        result = get_injury_report_for_team(session, seeded_injury_team, search_paths=[path])
    assert result.outcome == "found"
    assert result.report.outcome == "loaded"
    assert result.report.injuries == ()


def test_get_injury_report_for_team_translates_malformed_element_to_dedicated_exception(
    injury_session_factory, seeded_injury_team, tmp_path
):
    """The only call site that translates the preserved AttributeError into a typed, sanitized exception."""
    path = _write_injuries(tmp_path, "team_injuries.json", ["not-a-dict-entry"])
    with pytest.raises(InjuryDataIntegrityError) as excinfo:
        with unit_of_work(injury_session_factory) as session:
            get_injury_report_for_team(session, seeded_injury_team, search_paths=[path])
    assert excinfo.value.__cause__ is not None
    assert isinstance(excinfo.value.__cause__, AttributeError)
    # Sanitized: the exception's own message names neither the malformed element nor the cache path.
    assert "not-a-dict-entry" not in str(excinfo.value)
    assert str(path) not in str(excinfo.value)


def test_an_unrelated_attribute_error_elsewhere_is_not_mislabeled_as_injury_integrity_error(
    injury_session_factory, seeded_injury_team, monkeypatch
):
    """A genuinely unrelated AttributeError (not from build_injury_report's own malformed-element path) must propagate as itself, never silently reclassified."""

    def _boom(*args, **kwargs):
        raise AttributeError("unrelated failure, nothing to do with the injury cache")

    monkeypatch.setattr(injury_service_module, "get_team_injury_report", _boom)

    with pytest.raises(AttributeError, match="unrelated failure"):
        with unit_of_work(injury_session_factory) as session:
            get_injury_report_for_team(session, seeded_injury_team)


def test_get_injury_report_for_team_does_not_mutate_the_session_or_write(injury_session_factory, seeded_injury_team, tmp_path):
    path = _write_injuries(tmp_path, "team_injuries.json", [{"player": "x", "status": "out"}])
    with unit_of_work(injury_session_factory) as session:
        get_injury_report_for_team(session, seeded_injury_team, search_paths=[path])
        get_injury_report_for_team(session, seeded_injury_team, search_paths=[path])

    with injury_session_factory() as verify_session:
        rows = verify_session.execute(text("SELECT COUNT(*) FROM teams")).scalar()
    assert rows == 1  # unchanged -- no write occurred as a side effect of two read-only lookups
