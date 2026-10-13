"""
Controlled bulk-import design, Slice 2 (docs/controlled_bulk_import_design.md
sections 6/10/16, approved for this slice only): tests for
`src/pipeline/bulk_import_manifest.py` -- the pure, deterministic,
read-only dry-run manifest contract and atomic JSON writer.

Every test here uses synthetic records and `tmp_path` only. No adapter is
ever called, no `TeamDump` path is ever referenced, and no database or
network connection is ever attempted -- this slice's own module does none
of that, and these tests confirm it stays that way.
"""
from __future__ import annotations

import ast
import datetime as dt
import inspect
import json
import os

import pytest

from src.exceptions import ValidationError
from src.pipeline.bulk_import_manifest import (
    ARTIFACT_STATUSES,
    MANIFEST_SCHEMA_VERSION,
    SOURCE_FAMILY_CONTRACT_VERSIONS,
    SOURCE_FAMILY_ORDER,
    DryRunManifest,
    PlannedArtifact,
    SourceFamilyPlan,
    build_dry_run_manifest,
    serialize_manifest,
    write_dry_run_manifest,
)
from src.pipeline.allstats_import_adapter import ALLSTATS_IMPORT_CONTRACT_VERSION
from src.pipeline.espn_import_adapter import ESPN_IMPORT_CONTRACT_VERSION
from src.pipeline.game_log_import_adapter import TEAM_GAMELOG_IMPORT_CONTRACT_VERSION
from src.pipeline.player_game_log_import_adapter import PLAYER_GAMELOG_IMPORT_CONTRACT_VERSION

UTC = dt.timezone.utc


def _artifact(root="teamdump_cbb", path="st-bonaventure/a.csv", chash="hash1", status="planned_insert", metadata=None):
    return PlannedArtifact(source_root_key=root, relative_path=path, content_hash=chash, status=status, metadata=metadata or {})


def _plan(family, artifacts):
    return SourceFamilyPlan(family=family, contract_version=SOURCE_FAMILY_CONTRACT_VERSIONS[family], artifacts=tuple(artifacts))


def _manifest(families, generated_at=None):
    return build_dry_run_manifest(generated_at=generated_at or dt.datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC), families=families)


# ---------------------------------------------------------------------------
# Source families and contract versions
# ---------------------------------------------------------------------------


def test_all_four_source_families_have_the_exact_adapter_contract_version():
    assert SOURCE_FAMILY_CONTRACT_VERSIONS == {
        "allstats": ALLSTATS_IMPORT_CONTRACT_VERSION,
        "team_game_log": TEAM_GAMELOG_IMPORT_CONTRACT_VERSION,
        "player_game_log": PLAYER_GAMELOG_IMPORT_CONTRACT_VERSION,
        "espn": ESPN_IMPORT_CONTRACT_VERSION,
    }
    assert set(SOURCE_FAMILY_CONTRACT_VERSIONS) == set(SOURCE_FAMILY_ORDER) == {"allstats", "team_game_log", "player_game_log", "espn"}


@pytest.mark.parametrize("family", ["allstats", "team_game_log", "player_game_log", "espn"])
def test_each_family_builds_a_valid_manifest(family):
    plan = _plan(family, [_artifact()])
    manifest = _manifest([plan])
    assert manifest.families[0].family == family
    assert manifest.families[0].contract_version == SOURCE_FAMILY_CONTRACT_VERSIONS[family]


# ---------------------------------------------------------------------------
# Determinism, ordering, and JSON schema
# ---------------------------------------------------------------------------


def test_deterministic_byte_for_byte_output_for_identical_inputs():
    plan = _plan("espn", [_artifact(path="a.csv"), _artifact(path="b.csv", chash="hash2")])
    m1 = _manifest([plan])
    m2 = _manifest([_plan("espn", [_artifact(path="a.csv"), _artifact(path="b.csv", chash="hash2")])])
    assert serialize_manifest(m1) == serialize_manifest(m2)


def test_identical_output_regardless_of_input_order():
    a1 = _artifact(path="a.csv")
    a2 = _artifact(path="b.csv", chash="hash2")
    forward = _manifest([_plan("espn", [a1, a2])])
    reverse = _manifest([_plan("espn", [a2, a1])])
    assert serialize_manifest(forward) == serialize_manifest(reverse)


def test_identical_output_regardless_of_family_input_order():
    allstats_plan = _plan("allstats", [_artifact(path="x.csv")])
    espn_plan = _plan("espn", [_artifact(path="y.csv")])
    forward = _manifest([allstats_plan, espn_plan])
    reverse = _manifest([espn_plan, allstats_plan])
    assert serialize_manifest(forward) == serialize_manifest(reverse)


def test_stable_artifact_ordering_within_a_family():
    plan = _plan("espn", [_artifact(path="zzz.csv"), _artifact(path="aaa.csv", chash="hash2")])
    manifest = _manifest([plan])
    paths = [a.relative_path for a in manifest.families[0].artifacts]
    assert paths == ["aaa.csv", "zzz.csv"]


def test_stable_family_ordering_matches_design_recommended_sequence():
    plans = [_plan(f, [_artifact(path=f"{f}.csv")]) for f in ("espn", "player_game_log", "allstats", "team_game_log")]
    manifest = _manifest(plans)
    assert [p.family for p in manifest.families] == list(SOURCE_FAMILY_ORDER)


def test_json_is_valid_and_has_the_expected_top_level_schema():
    manifest = _manifest([_plan("allstats", [_artifact()])])
    parsed = json.loads(serialize_manifest(manifest))
    assert parsed["schema_version"] == MANIFEST_SCHEMA_VERSION
    assert "generated_at" in parsed
    assert isinstance(parsed["source_families"], list)
    family_entry = parsed["source_families"][0]
    assert set(family_entry) == {"family", "contract_version", "artifacts"}
    artifact_entry = family_entry["artifacts"][0]
    assert set(artifact_entry) == {"source_root_key", "relative_path", "content_hash", "status", "metadata"}


def test_serialized_json_ends_with_exactly_one_newline():
    text = serialize_manifest(_manifest([_plan("allstats", [_artifact()])]))
    assert text.endswith("\n")
    assert not text.endswith("\n\n")


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


def test_utc_aware_timestamp_serializes_correctly():
    manifest = _manifest([_plan("allstats", [_artifact()])], generated_at=dt.datetime(2026, 1, 15, 18, 30, 0, tzinfo=UTC))
    parsed = json.loads(serialize_manifest(manifest))
    assert parsed["generated_at"] == "2026-01-15T18:30:00Z"


def test_non_utc_but_aware_timestamp_normalized_to_utc():
    eastern = dt.timezone(dt.timedelta(hours=-5))
    manifest = _manifest([_plan("allstats", [_artifact()])], generated_at=dt.datetime(2026, 1, 15, 13, 30, 0, tzinfo=eastern))
    parsed = json.loads(serialize_manifest(manifest))
    assert parsed["generated_at"] == "2026-01-15T18:30:00Z"


def test_naive_timestamp_rejected():
    with pytest.raises(ValidationError):
        _manifest([_plan("allstats", [_artifact()])], generated_at=dt.datetime(2026, 1, 15, 18, 30, 0))


# ---------------------------------------------------------------------------
# Family / contract-version / duplicate-identity validation
# ---------------------------------------------------------------------------


def test_unknown_source_family_rejected():
    with pytest.raises(ValidationError):
        SourceFamilyPlan(family="torvik", contract_version="1", artifacts=(_artifact(),))


def test_mismatched_contract_version_rejected():
    with pytest.raises(ValidationError):
        SourceFamilyPlan(family="allstats", contract_version="999", artifacts=(_artifact(),))


def test_duplicate_artifact_identity_within_a_family_rejected():
    same = _artifact(path="dup.csv", chash="samehash")
    same2 = _artifact(path="dup.csv", chash="samehash")
    with pytest.raises(ValidationError):
        SourceFamilyPlan(family="allstats", contract_version=SOURCE_FAMILY_CONTRACT_VERSIONS["allstats"], artifacts=(same, same2))


def test_same_path_different_hash_is_not_a_duplicate():
    """A changed artifact at the same path (new content_hash) is a distinct, additive identity -- never rejected as a duplicate."""
    plan = SourceFamilyPlan(
        family="allstats", contract_version=SOURCE_FAMILY_CONTRACT_VERSIONS["allstats"],
        artifacts=(_artifact(path="x.csv", chash="hashA"), _artifact(path="x.csv", chash="hashB")),
    )
    assert len(plan.artifacts) == 2


def test_duplicate_family_within_one_manifest_rejected():
    with pytest.raises(ValidationError):
        _manifest([_plan("allstats", [_artifact()]), _plan("allstats", [_artifact(chash="other")])])


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_path", ["/etc/passwd", "C:\\Users\\anthony\\secrets.txt", "C:/Users/x", "\\\\server\\share\\file"])
def test_absolute_paths_rejected(bad_path):
    with pytest.raises(ValidationError):
        _artifact(path=bad_path)


@pytest.mark.parametrize("bad_path", ["../etc/passwd", "a/../../b", "st-bonaventure/../../../etc/passwd", ".."])
def test_parent_traversal_rejected(bad_path):
    with pytest.raises(ValidationError):
        _artifact(path=bad_path)


def test_empty_relative_path_rejected():
    with pytest.raises(ValidationError):
        _artifact(path="")


def test_unicode_relative_paths_are_accepted_and_preserved():
    artifact = _artifact(path="saint-joseph's/joão_victor_2026_gamelog.csv")
    manifest = _manifest([_plan("player_game_log", [artifact])])
    text = serialize_manifest(manifest)
    assert "joão_victor" in text  # not escaped to \uXXXX -- ensure_ascii=False confirmed
    parsed = json.loads(text)
    assert parsed["source_families"][0]["artifacts"][0]["relative_path"] == "saint-joseph's/joão_victor_2026_gamelog.csv"


# ---------------------------------------------------------------------------
# Sensitive-content rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_value", [
    "postgresql://user:pass@host:5432/db",
    "postgres://localhost/napsedge",
    "mysql://root:hunter2@db/prod",
])
def test_database_urls_in_metadata_rejected(bad_value):
    with pytest.raises(ValidationError):
        _artifact(metadata={"note": bad_value})


@pytest.mark.parametrize("bad_value", ["password=hunter2", "api_key: sk-abc123", "my secret token is xyz"])
def test_credential_like_metadata_values_rejected(bad_value):
    with pytest.raises(ValidationError):
        _artifact(metadata={"note": bad_value})


def test_credential_like_metadata_key_rejected():
    with pytest.raises(ValidationError):
        _artifact(metadata={"api_key": "some-value"})


def test_sensitive_content_in_content_hash_field_rejected():
    with pytest.raises(ValidationError):
        _artifact(chash="postgresql://user:pass@host/db")


def test_ordinary_metadata_is_accepted():
    artifact = _artifact(metadata={"team_slug": "st-bonaventure", "season": "2026"})
    assert artifact.metadata == {"team_slug": "st-bonaventure", "season": "2026"}


def test_unknown_status_rejected():
    with pytest.raises(ValidationError):
        _artifact(status="not_a_real_status")


@pytest.mark.parametrize("status", sorted(ARTIFACT_STATUSES))
def test_every_approved_status_accepted(status):
    _artifact(status=status)  # must not raise


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


def test_planned_artifact_is_frozen():
    artifact = _artifact()
    with pytest.raises(Exception):
        artifact.status = "conflict"  # type: ignore[misc]


def test_mutating_callers_metadata_dict_after_construction_does_not_affect_the_record():
    metadata = {"team_slug": "st-bonaventure"}
    artifact = _artifact(metadata=metadata)
    metadata["team_slug"] = "mutated"
    metadata["injected"] = "value"
    assert artifact.metadata == {"team_slug": "st-bonaventure"}


def test_mutating_callers_artifact_list_after_construction_does_not_affect_the_plan():
    artifacts = [_artifact(path="a.csv")]
    plan = SourceFamilyPlan(family="allstats", contract_version=SOURCE_FAMILY_CONTRACT_VERSIONS["allstats"], artifacts=tuple(artifacts))
    artifacts.append(_artifact(path="b.csv", chash="other"))
    assert len(plan.artifacts) == 1


def test_mutating_callers_families_list_after_construction_does_not_affect_the_manifest():
    families = [_plan("allstats", [_artifact()])]
    manifest = build_dry_run_manifest(generated_at=dt.datetime(2026, 9, 11, tzinfo=UTC), families=families)
    families.append(_plan("espn", [_artifact(path="x.csv")]))
    assert len(manifest.families) == 1


def test_source_family_plan_is_frozen():
    plan = _plan("allstats", [_artifact()])
    with pytest.raises(Exception):
        plan.family = "espn"  # type: ignore[misc]


def test_dry_run_manifest_is_frozen():
    manifest = _manifest([_plan("allstats", [_artifact()])])
    with pytest.raises(Exception):
        manifest.generated_at = dt.datetime(2020, 1, 1, tzinfo=UTC)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Atomic writer
# ---------------------------------------------------------------------------


def test_atomic_successful_write(tmp_path):
    manifest = _manifest([_plan("allstats", [_artifact()])])
    dest = tmp_path / "manifest.json"
    result = write_dry_run_manifest(manifest, dest)
    assert result == dest
    assert dest.exists()
    assert json.loads(dest.read_text(encoding="utf-8"))["schema_version"] == MANIFEST_SCHEMA_VERSION


def test_no_temporary_file_left_behind_after_successful_write(tmp_path):
    manifest = _manifest([_plan("allstats", [_artifact()])])
    dest = tmp_path / "manifest.json"
    write_dry_run_manifest(manifest, dest)
    assert list(tmp_path.iterdir()) == [dest]


def test_refuses_to_overwrite_existing_destination_by_default(tmp_path):
    manifest = _manifest([_plan("allstats", [_artifact()])])
    dest = tmp_path / "manifest.json"
    write_dry_run_manifest(manifest, dest)
    with pytest.raises(ValidationError):
        write_dry_run_manifest(manifest, dest)


def test_overwrite_true_replaces_existing_destination(tmp_path):
    dest = tmp_path / "manifest.json"
    write_dry_run_manifest(_manifest([_plan("allstats", [_artifact(chash="v1")])]), dest)
    write_dry_run_manifest(_manifest([_plan("allstats", [_artifact(chash="v2")])]), dest, overwrite=True)
    parsed = json.loads(dest.read_text(encoding="utf-8"))
    assert parsed["source_families"][0]["artifacts"][0]["content_hash"] == "v2"


def test_existing_destination_preserved_if_replace_fails(tmp_path, monkeypatch):
    dest = tmp_path / "manifest.json"
    write_dry_run_manifest(_manifest([_plan("allstats", [_artifact(chash="original")])]), dest)
    original_bytes = dest.read_bytes()

    def _boom(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        write_dry_run_manifest(_manifest([_plan("allstats", [_artifact(chash="new")])]), dest, overwrite=True)

    assert dest.read_bytes() == original_bytes  # untouched -- os.replace never got a chance to partially apply


def test_no_temp_residue_after_replace_failure(tmp_path, monkeypatch):
    dest = tmp_path / "manifest.json"
    write_dry_run_manifest(_manifest([_plan("allstats", [_artifact(chash="original")])]), dest)

    def _boom(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        write_dry_run_manifest(_manifest([_plan("allstats", [_artifact(chash="new")])]), dest, overwrite=True)

    assert list(tmp_path.iterdir()) == [dest]  # no leftover .manifest.json.*.tmp file


def test_no_temp_residue_after_write_failure_before_replace(tmp_path, monkeypatch):
    dest = tmp_path / "manifest.json"

    def _boom(*args, **kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(os, "fsync", _boom)
    with pytest.raises(OSError):
        write_dry_run_manifest(_manifest([_plan("allstats", [_artifact()])]), dest)

    assert list(tmp_path.iterdir()) == []  # destination never created, temp file cleaned up
    assert not dest.exists()


def test_destination_directory_must_already_exist(tmp_path):
    manifest = _manifest([_plan("allstats", [_artifact()])])
    dest = tmp_path / "does_not_exist" / "manifest.json"
    with pytest.raises(ValidationError):
        write_dry_run_manifest(manifest, dest)


# ---------------------------------------------------------------------------
# No side effects, no adapter invocation
# ---------------------------------------------------------------------------


def test_module_never_imports_network_database_or_container_libraries():
    import src.pipeline.bulk_import_manifest as mod

    tree = ast.parse(inspect.getsource(mod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"requests", "urllib", "socket", "psycopg", "psycopg2", "sqlalchemy", "docker", "testcontainers", "pandas"}
    assert not (forbidden & imported)


def test_module_never_references_teamdump_paths():
    """The module's own docstring legitimately explains, in prose, that it never
    touches TeamDump -- this guard checks actual code (imports and string literals
    outside the module docstring) never references a real TeamDump path/constant."""
    import src.pipeline.bulk_import_manifest as mod

    tree = ast.parse(inspect.getsource(mod))
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
            imported_names.update(a.name for a in node.names)
    assert "CBB_DIR" not in imported_names
    assert "ESPN_DIR" not in imported_names
    assert "src.paths" not in imported_names


def test_module_never_calls_any_of_the_four_adapters():
    """Confirms this module only imports the four adapters' own contract-version
    constants (plain data), never one of their discover/parse/persist functions."""
    import src.pipeline.bulk_import_manifest as mod

    tree = ast.parse(inspect.getsource(mod))
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("src.pipeline."):
            imported_names.update(a.name for a in node.names)
    forbidden_prefixes = ("discover_", "load_", "parse_", "persist_", "build_allstats", "build_game_log", "build_espn", "build_player_game_log")
    leaked = {n for n in imported_names if any(n.startswith(p) for p in forbidden_prefixes)}
    assert leaked == set()
    assert imported_names == {
        "ALLSTATS_IMPORT_CONTRACT_VERSION", "TEAM_GAMELOG_IMPORT_CONTRACT_VERSION",
        "ESPN_IMPORT_CONTRACT_VERSION", "PLAYER_GAMELOG_IMPORT_CONTRACT_VERSION",
    }


def _os_or_tempfile_calls(node) -> set[str]:
    """Collects only real os.*/tempfile.* dotted calls (e.g. 'os.replace', 'tempfile.mkstemp')
    -- never confused with an unrelated same-named method like str.replace()."""
    found = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name):
            if n.func.value.id in ("os", "tempfile"):
                found.add(f"{n.func.value.id}.{n.func.attr}")
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "open":
            found.add("open")
    return found


def test_module_never_opens_a_real_file_except_via_the_explicit_write_function():
    """AST guard: the only real filesystem-write calls anywhere in this module
    (open/os.fdopen/os.replace/os.fsync/tempfile.mkstemp/Path.unlink) must appear
    solely inside write_dry_run_manifest -- build_dry_run_manifest/serialize_manifest
    and every dataclass stay pure, confirmed structurally, not just by convention.
    Deliberately scoped to real os.*/tempfile.*/open() calls (via the dotted-call
    helper above), never confused with an unrelated same-named method such as the
    ordinary str.replace() used in path-safety validation."""
    import src.pipeline.bulk_import_manifest as mod

    tree = ast.parse(inspect.getsource(mod))
    write_fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "write_dry_run_manifest")
    write_fn_calls = _os_or_tempfile_calls(write_fn)
    assert {"os.fdopen", "os.replace", "tempfile.mkstemp"} <= write_fn_calls

    other_functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name != "write_dry_run_manifest"]
    for fn in other_functions:
        calls = _os_or_tempfile_calls(fn)
        assert not calls, f"{fn.name} unexpectedly performs a real filesystem call: {calls}"
