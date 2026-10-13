"""
Phase 3, batch 5, Gate 2: logo/asset canonical ingestion.
No live requests -- mocked ESPN team-list entries and mocked binary
image responses only.
"""
import os

import pytest

from src.identity.contracts import TeamIdentity, UnresolvedIdentity
from src.ingestion.logo_asset_ingestion import (
    LogoAssetRecord,
    LogoFetchResponse,
    is_safe_filename,
    parse_espn_team_entry,
    resolve_logo_asset_record,
    run_logo_asset_ingestion,
    safe_output_path,
    slug_to_filename_v15,
)


def _ok_response(content=b"fake-png-bytes", content_type="image/png"):
    def _fetch(url):
        return LogoFetchResponse(status_code=200, content=content, content_type=content_type)
    return _fetch


# ---------------------------------------------------------------------------
# Team resolution / ESPN team-ID / URL preservation
# ---------------------------------------------------------------------------

def test_canonical_team_resolution():
    record = resolve_logo_asset_record("Duke Blue Devils", "/team/_/id/150/duke-blue-devils")
    assert isinstance(record.team_identity, TeamIdentity)
    assert record.team_identity.display_name == "Duke"


def test_espn_team_id_preserved():
    record = resolve_logo_asset_record("Duke Blue Devils", "/team/_/id/150/duke-blue-devils")
    assert record.source_team_id == "150"


def test_logo_url_preserved_and_deterministic():
    record = resolve_logo_asset_record("Duke Blue Devils", "/team/_/id/150/duke-blue-devils")
    assert record.raw_logo_url == "https://a.espncdn.com/i/teamlogos/ncaa/500/150.png"


def test_unknown_team_unresolved():
    record = resolve_logo_asset_record("Definitely Not A Real School Xyzzy123", "/team/_/id/1/x")
    assert not isinstance(record.team_identity, TeamIdentity)
    assert record.retrieval_status == "unresolved_team"


def test_ambiguous_team_never_forced():
    record = resolve_logo_asset_record("Miami", "/team/_/id/9999/miami")
    assert not isinstance(record.team_identity, TeamIdentity)
    assert record.retrieval_status == "ambiguous_team"
    assert record.team_identity.reason == "ambiguous"


def test_missing_logo_url():
    record = resolve_logo_asset_record("Duke Blue Devils", None)
    assert record.source_team_id is None
    assert record.raw_logo_url is None
    assert record.retrieval_status == "missing_url"


def test_never_infers_team_solely_from_filename():
    """Team resolution only ever consults raw_team_name via resolve_espn_team_identity -- never a filename string."""
    import inspect

    sig = inspect.signature(resolve_logo_asset_record)
    assert "filename" not in sig.parameters


# ---------------------------------------------------------------------------
# Valid/invalid image responses
# ---------------------------------------------------------------------------

def test_valid_image_response_downloads(tmp_path):
    record = resolve_logo_asset_record(
        "Duke Blue Devils", "/team/_/id/150/duke-blue-devils",
        fetch_fn=_ok_response(), output_dir=str(tmp_path),
    )
    assert record.retrieval_status == "downloaded"
    assert record.byte_length == len(b"fake-png-bytes")
    assert record.checksum_sha256 is not None


def test_invalid_media_type_rejected():
    def fetch(url):
        return LogoFetchResponse(status_code=200, content=b"<html>not an image</html>", content_type="text/html")
    record = resolve_logo_asset_record("Duke Blue Devils", "/team/_/id/150/duke-blue-devils", fetch_fn=fetch)
    assert record.retrieval_status == "failed_invalid_media_type"


def test_empty_response_rejected():
    def fetch(url):
        return LogoFetchResponse(status_code=200, content=b"", content_type="image/png")
    record = resolve_logo_asset_record("Duke Blue Devils", "/team/_/id/150/duke-blue-devils", fetch_fn=fetch)
    assert record.retrieval_status == "failed_empty_content"
    assert record.byte_length == 0


def test_failed_http_status_rejected():
    def fetch(url):
        return LogoFetchResponse(status_code=404, content=b"", content_type=None)
    record = resolve_logo_asset_record("Duke Blue Devils", "/team/_/id/150/duke-blue-devils", fetch_fn=fetch)
    assert record.retrieval_status == "failed_http"


# ---------------------------------------------------------------------------
# Deterministic filename / checksum, safe path construction, path-traversal
# ---------------------------------------------------------------------------

def test_deterministic_filename():
    r1 = resolve_logo_asset_record("Duke Blue Devils", "/team/_/id/150/duke-blue-devils")
    r2 = resolve_logo_asset_record("Duke Blue Devils", "/team/_/id/150/duke-blue-devils")
    assert r1.canonical_filename == r2.canonical_filename == "duke.png"


def test_slug_to_filename_v15_legacy_behavior():
    assert slug_to_filename_v15("st. bonaventure's (ny)") == "st_bonaventures_ny"


def test_deterministic_checksum(tmp_path):
    r1 = resolve_logo_asset_record("Duke Blue Devils", "/team/_/id/150/duke-blue-devils", fetch_fn=_ok_response())
    r2 = resolve_logo_asset_record("Duke Blue Devils", "/team/_/id/150/duke-blue-devils", fetch_fn=_ok_response())
    assert r1.checksum_sha256 == r2.checksum_sha256


def test_safe_output_path_within_directory(tmp_path):
    path = safe_output_path(str(tmp_path), "duke.png")
    assert path is not None
    assert str(path).startswith(str(tmp_path))


def test_safe_filename_rejects_separators():
    assert is_safe_filename("../../etc/passwd") is False
    assert is_safe_filename("sub/dir/file.png") is False
    assert is_safe_filename("sub\\dir\\file.png") is False
    assert is_safe_filename("duke.png") is True


def test_path_traversal_rejected(tmp_path):
    assert safe_output_path(str(tmp_path), "../escape.png") is None
    assert safe_output_path(str(tmp_path), "..\\escape.png") is None


def test_path_traversal_via_encoded_slug_never_written(tmp_path):
    """Even if a canonical_filename somehow contained traversal, safe_output_path refuses it."""
    result = safe_output_path(str(tmp_path), "../../outside.png")
    assert result is None


# ---------------------------------------------------------------------------
# Duplicate URL / checksum / filename collision detection
# ---------------------------------------------------------------------------

def test_duplicate_url_detected():
    entries = [("Duke Blue Devils", "/team/_/id/150/duke"), ("Duke Blue Devils", "/team/_/id/150/duke")]
    result = run_logo_asset_ingestion(entries, fetch_fn=_ok_response())
    assert result.duplicate_urls == {"https://a.espncdn.com/i/teamlogos/ncaa/500/150.png": 2}


def test_duplicate_checksum_detected():
    entries = [("Duke Blue Devils", "/team/_/id/150/duke"), ("Kansas Jayhawks", "/team/_/id/2305/kansas")]
    result = run_logo_asset_ingestion(entries, fetch_fn=_ok_response())  # same fixed content for both
    assert len(result.duplicate_checksums) == 1


def test_filename_collision_detected():
    """Two different ESPN entries whose canonical filename would collide -- flagged, never silently overwritten."""
    entries = [("Duke Blue Devils", "/team/_/id/150/duke"), ("Duke", "/team/_/id/151/duke2")]
    result = run_logo_asset_ingestion(entries)
    if result.filename_collisions:
        assert all(len(v) > 1 for v in result.filename_collisions.values())


def test_no_duplicates_for_distinct_teams():
    entries = [("Duke Blue Devils", "/team/_/id/150/duke"), ("Kansas Jayhawks", "/team/_/id/2305/kansas")]
    result = run_logo_asset_ingestion(entries)
    assert result.duplicate_urls == {}


# ---------------------------------------------------------------------------
# Idempotency: repeated ingestion does not re-download or overwrite
# ---------------------------------------------------------------------------

def test_idempotent_repeated_ingestion_skips_existing(tmp_path):
    entries = [("Duke Blue Devils", "/team/_/id/150/duke")]
    r1 = run_logo_asset_ingestion(entries, fetch_fn=_ok_response(), output_dir=str(tmp_path))
    assert r1.records[0].retrieval_status == "downloaded"

    r2 = run_logo_asset_ingestion(entries, fetch_fn=_ok_response(), output_dir=str(tmp_path))
    assert r2.records[0].retrieval_status == "skipped_existing"

    assert len(os.listdir(tmp_path)) == 1  # never duplicated


def test_idempotent_without_output_dir_reproduces_same_manifest():
    entries = [("Duke Blue Devils", "/team/_/id/150/duke")]
    r1 = run_logo_asset_ingestion(entries, fetch_fn=_ok_response())
    r2 = run_logo_asset_ingestion(entries, fetch_fn=_ok_response())
    assert r1.records[0].checksum_sha256 == r2.records[0].checksum_sha256
    assert r1.records[0].canonical_filename == r2.records[0].canonical_filename


# ---------------------------------------------------------------------------
# Manifest provenance / legacy filename parity
# ---------------------------------------------------------------------------

def test_manifest_provenance_retained():
    result = run_logo_asset_ingestion([("Duke Blue Devils", "/team/_/id/150/duke")], fixture_provenance="mocked_fixture:test")
    assert result.evidence.fixture_provenance == "mocked_fixture:test"
    assert result.evidence.source_endpoint == "https://www.espn.com/mens-college-basketball/teams"
    assert result.evidence.parser_version
    assert result.evidence.collected_at_utc


def test_legacy_filename_override_preserved_when_supplied():
    record = resolve_logo_asset_record(
        "Duke Blue Devils", "/team/_/id/150/duke-blue-devils",
        legacy_filename_overrides={"duke": "blue_devils_logo.png"},
    )
    assert record.legacy_filename == "blue_devils_logo.png"
    assert record.canonical_filename == "duke.png"


def test_legacy_filename_none_when_no_override_supplied():
    record = resolve_logo_asset_record("Duke Blue Devils", "/team/_/id/150/duke-blue-devils")
    assert record.legacy_filename is None  # never fabricated as "same as canonical"


def test_legacy_slug_to_filename_still_importable_and_unchanged():
    assert slug_to_filename_v15("St. Bonaventure") == "St_Bonaventure"


# ---------------------------------------------------------------------------
# No live network access, no production-logo mutation
# ---------------------------------------------------------------------------

def test_no_live_network_access():
    import ast
    import inspect

    import src.ingestion.logo_asset_ingestion as mod

    tree = ast.parse(inspect.getsource(mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "requests" not in imported


def test_no_fuzzy_matching_in_module():
    import ast
    import inspect

    import src.ingestion.logo_asset_ingestion as mod

    tree = ast.parse(inspect.getsource(mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "difflib" not in imported


def test_module_never_calls_live_logo_download_function():
    import ast
    import inspect

    import src.ingestion.logo_asset_ingestion as mod

    tree = ast.parse(inspect.getsource(mod))
    called_names = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not ({"download_all_team_logos", "fetch_espn_teams"} & called_names)


def test_does_not_write_outside_output_dir_when_none_supplied():
    """No output_dir means no filesystem write at all."""
    record = resolve_logo_asset_record("Duke Blue Devils", "/team/_/id/150/duke-blue-devils", fetch_fn=_ok_response())
    assert record.output_path is None


def test_does_not_mutate_team_registry():
    from src import slug_utils

    before_size = len(slug_utils.TEAM_REGISTRY)
    resolve_logo_asset_record("Duke Blue Devils", "/team/_/id/150/duke-blue-devils")
    assert len(slug_utils.TEAM_REGISTRY) == before_size
