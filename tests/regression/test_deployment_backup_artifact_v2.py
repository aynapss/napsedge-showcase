"""
Phase 9B-6H: manifest /2 (`consistency.catalog_observation`), the early owned-partial reservation, and the
capture-failure mapping (docs/backup_artifact_contract_proposal.md section 13.16).

Everything is synthetic: temporary directories, genuinely computed digests, hand-built catalog endpoints. No database,
Docker, subprocess or network. Outcomes are judged by the PUBLIC, unchanged-in-behavior verifier, never by the writer's
opinion of itself. Lettered sections follow the unit's acceptance criteria A-L.
"""
from __future__ import annotations

import ast
import copy
import datetime as dt
import errno
import hashlib
import inspect
import json
import os
import shutil
import sys
import threading
import traceback
from pathlib import Path

import pytest

import src.deployment.backup_artifact as ba
import src.deployment.backup_artifact_writer as w
import src.deployment.backup_capture_artifact as bridge
from src.deployment.backup_artifact import (
    ArtifactFormatError,
    ArtifactManifest,
    BaselineDocument,
    CatalogObservationRecord,
    ConsistencyClaim,
    ConsistencyClaimV2,
    parse_manifest,
    verify_artifact_directory,
)
from src.deployment.backup_artifact_writer import (
    ArtifactMetadata,
    ArtifactReservation,
    DurabilityMode,
    PreconditionError,
    ReservationStateError,
    WriteFailed,
    reserve_artifact,
    write_artifact,
)
from src.deployment.backup_capture_artifact import capture_failure_reason, catalog_observation_record
from src.deployment.backup_capture_catalog import (
    CATEGORIES,
    CatalogChanged,
    CatalogEndpoint,
    CatalogObservation,
    CatalogObservationFailed,
    NoDifferenceObserved,
    SourceIdentity,
    require_unchanged,
)
from src.deployment.backup_capture_dump import DumpCleanupError, DumpCleanupReport, DumpFailed, DumpPreconditionError, DumpTimeout
from src.deployment.backup_capture_lock import VERIFIED_CLOSED, BackupLockNotHeld, BackupLockSessionLost
from src.deployment.backup_capture_snapshot import SnapshotHandleClosed, SnapshotRefusedError, SnapshotSessionLost, SnapshotSetupError
from src.exceptions import AdvisoryLockUnavailableError, DatabaseTargetVerificationError
from tests.regression.test_deployment_backup_artifact import (
    ARTIFACT_ID,
    CREATED,
    LOGICAL,
    OTHER_ID,
    Parts,
    baseline_doc,
    make_directory_link,
    manifest_doc,
    materialize,
    oracle,
    sha256,
)
from tests.regression.test_deployment_backup_artifact_writer import (
    ARCHIVE_BYTES,
    FIXED_NOW_TEXT,
    Probe,
    build_inputs,
    names,
    read_failed_record,
    tree_snapshot,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SYSID, OID, DBNAME = "7550123456789012345", 16384, "napsedge"   # manifest_doc()'s source
SOURCE = SourceIdentity(SYSID, OID, DBNAME)
SECRET = "hunter2-planted-secret"
PLANTED = f"password={SECRET} postgresql://napsedge_backup_ro:{SECRET}@db.internal:5432/napsedge sslpassword={SECRET}"
UTC = dt.timezone.utc
T_BEFORE_BACKEND = dt.datetime(2026, 10, 2, 18, 30, 0, 50000, tzinfo=UTC)
T_BEFORE_TXN = dt.datetime(2026, 10, 2, 18, 30, 0, 60000, tzinfo=UTC)
T_AFTER_BACKEND = dt.datetime(2026, 10, 2, 18, 30, 2, 900000, tzinfo=UTC)
T_AFTER_TXN = dt.datetime(2026, 10, 2, 18, 30, 2, 910000, tzinfo=UTC)
OBSERVATION = CatalogObservation(
    constraints=(("team_aliases", "pk_team_aliases", "p", True, "PRIMARY KEY (id)"), ("teams", "pk_teams", "p", True, "PRIMARY KEY (team_id)")),
    indexes=(("teams", "pk_teams", "CREATE UNIQUE INDEX pk_teams ON public.teams USING btree (team_id)"),),
    acls=(("teams", "r", "napsedge_migrator", "napsedge_migrator=arwdDxt/napsedge_migrator"),),
)


# --------------------------------------------------------------------------- synthetic observations
def endpoint(label, pid, backend_start, transaction_start, *, observation=OBSERVATION, source=SOURCE, cleanup=VERIFIED_CLOSED, isolation="read committed"):
    return CatalogEndpoint(label=label, observation=observation, backend_pid=pid, backend_start=backend_start, transaction_start=transaction_start,
                           isolation=isolation, read_only="on", source=source, cleanup_verification=cleanup)


def no_difference(**after_overrides) -> NoDifferenceObserved:
    before = endpoint("before", 811, T_BEFORE_BACKEND, T_BEFORE_TXN)
    after = endpoint("after", 812, T_AFTER_BACKEND, T_AFTER_TXN, **after_overrides)
    return require_unchanged(before, after, SOURCE)


def observation_doc() -> dict:
    """The /2 record, built INDEPENDENTLY of the bridge (hashlib + json here), for the observation above."""
    def digest(rows):
        return hashlib.sha256(json.dumps([list(r) for r in rows], default=str, separators=(",", ":")).encode()).hexdigest()

    parts = {"constraints": digest(OBSERVATION.constraints), "indexes": digest(OBSERVATION.indexes), "acls": digest(OBSERVATION.acls)}
    parts["combined"] = hashlib.sha256("|".join(parts[c] for c in ("constraints", "indexes", "acls")).encode()).hexdigest()

    def one(label, pid, backend, txn):
        return {"label": label, "backend_pid": pid, "backend_start": backend, "transaction_start": txn, "isolation": "read committed",
                "read_only": "on", "cleanup_verification": "closed_and_released", "digests": dict(parts)}

    return {"method": "fresh_endpoints_v1", "result": "no_difference_observed", "categories": ["constraints", "indexes", "acls"],
            "source": {"system_identifier": SYSID, "database_oid": OID, "database_name": DBNAME},
            "endpoints": [one("before", 811, "2026-10-02T18:30:00.050000Z", "2026-10-02T18:30:00.060000Z"),
                          one("after", 812, "2026-10-02T18:30:02.900000Z", "2026-10-02T18:30:02.910000Z")]}


def v2_manifest_doc(**kwargs) -> dict:
    m = manifest_doc(**kwargs)
    m["format"] = ba.MANIFEST_FORMAT_V2
    del m["consistency"]["catalog_bracket_equal"]
    m["consistency"]["catalog_observation"] = observation_doc()
    return m


def v2_parts() -> Parts:
    parts = Parts()
    parts.mdoc = v2_manifest_doc(archive=parts.archive, bdoc=parts.bdoc)
    return parts


def parse_doc(document: dict) -> ArtifactManifest:
    return parse_manifest(oracle(document))


def v2_metadata(inputs, record: CatalogObservationRecord | None = None) -> ArtifactMetadata:
    c = inputs.metadata.consistency
    claim = ConsistencyClaimV2(mode=c.mode, snapshot_descriptor=c.snapshot_descriptor, snapshot_unchanged_at_end=c.snapshot_unchanged_at_end,
                               sequence_rule=c.sequence_rule, ddl_exclusion=c.ddl_exclusion,
                               catalog_observation=record or catalog_observation_record(no_difference()),
                               foreign_write_sessions_start=c.foreign_write_sessions_start, foreign_write_sessions_end=c.foreign_write_sessions_end)
    return ArtifactMetadata(**{**{f: getattr(inputs.metadata, f) for f in ("artifact_id", "created_utc", "finished_utc", "source", "schema",
                                                                           "repository", "tooling", "archive")}, "consistency": claim})


def reserve(inputs, *, ops=None, artifact_id=None, **kwargs) -> ArtifactReservation:
    kwargs.setdefault("durability", DurabilityMode.REQUIRED)
    return reserve_artifact(inputs.store, artifact_id=artifact_id or inputs.metadata.artifact_id, logical_name=LOGICAL,
                            created_utc=inputs.metadata.created_utc, ops=ops if ops is not None else Probe(), **kwargs)


def all_bytes(directory: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(directory.rglob("*")) if p.is_file())


def verdict_as_final(partial: Path, tmp_path: Path, final_name: str):
    """A partial copied under its FINAL name in a separate store: what a consumer would see if someone renamed it by hand."""
    shadow = tmp_path / "shadow_store"
    shadow.mkdir(exist_ok=True)
    shutil.copytree(partial, shadow / final_name)
    return verify_artifact_directory(shadow / final_name)


def outcome_of(action) -> tuple[str, object]:
    """Runs `action` and returns ("returned", value) or ("raised", exception) -- so an unexpected escape is judged by an explicit assertion."""
    try:
        return "returned", action()
    except BaseException as exc:  # noqa: BLE001 -- includes interrupts, which must not end the test session here
        return "raised", exc


def keys_of(value) -> set:
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(key)
            found |= keys_of(item)
    elif isinstance(value, list):
        for item in value:
            found |= keys_of(item)
    return found


# =========================================================================== A. /1 compatibility
def test_a_v1_fixture_parses_round_trips_byte_identically_and_stays_v1():
    document = manifest_doc()
    manifest = parse_doc(document)
    assert manifest.format == ba.MANIFEST_FORMAT == "napsedge.backup-artifact/1"
    assert type(manifest.consistency) is ConsistencyClaim and manifest.consistency.catalog_bracket_equal is True
    assert not hasattr(manifest.consistency, "catalog_observation"), "a /1 manifest never carries fresh-endpoint evidence"
    assert manifest.to_bytes() == oracle(document), "the /1 canonical serialization is unchanged"


def test_a_v1_directory_still_verifies(tmp_path):
    verdict = verify_artifact_directory(materialize(tmp_path, Parts()))
    assert verdict.valid and verdict.manifest.format == ba.MANIFEST_FORMAT


@pytest.mark.parametrize("edit, code, detail", [
    (lambda m: m["consistency"].update(catalog_observation=observation_doc()), "FIELD_UNKNOWN", "catalog_observation"),
    (lambda m: m["consistency"].pop("catalog_bracket_equal"), "FIELD_MISSING", "catalog_bracket_equal"),
    (lambda m: m["consistency"].update(catalog_bracket_equal="yes"), "FIELD_TYPE", "catalog_bracket_equal"),
])
def test_a_v1_keeps_its_own_rejections_and_never_accepts_v2_content(edit, code, detail):
    document = manifest_doc()
    edit(document)
    with pytest.raises(ArtifactFormatError) as info:
        parse_doc(document)
    assert info.value.code == code and detail in info.value.detail


def test_a_v1_write_output_equals_the_independent_oracle(tmp_path):
    inputs = build_inputs(tmp_path)
    result = write_artifact(inputs.store, inputs.archive, inputs.metadata, inputs.baseline, durability=DurabilityMode.REQUIRED, ops=Probe())
    expected = manifest_doc(ARCHIVE_BYTES)
    assert (inputs.final / "manifest.json").read_bytes() == oracle(expected)
    assert json.loads((inputs.final / "manifest.json").read_bytes())["format"] == "napsedge.backup-artifact/1"
    assert verify_artifact_directory(result.path).valid


# =========================================================================== B. a conforming /2 artifact
def test_b_v2_document_parses_and_round_trips():
    document = v2_manifest_doc()
    manifest = parse_doc(document)
    assert manifest.format == ba.MANIFEST_FORMAT_V2 == "napsedge.backup-artifact/2"
    record = manifest.consistency.catalog_observation
    assert type(manifest.consistency) is ConsistencyClaimV2 and not hasattr(manifest.consistency, "catalog_bracket_equal")
    assert (record.method, record.result, record.categories) == ("fresh_endpoints_v1", "no_difference_observed", ("constraints", "indexes", "acls"))
    assert [e.label for e in record.endpoints] == ["before", "after"] and [e.backend_pid for e in record.endpoints] == [811, 812]
    assert manifest.to_bytes() == oracle(document)


def test_b_v2_directory_verifies_structurally(tmp_path):
    verdict = verify_artifact_directory(materialize(tmp_path, v2_parts()))
    assert verdict.valid, [str(r) for r in verdict.reasons]
    assert verdict.manifest.format == ba.MANIFEST_FORMAT_V2
    assert set(verdict.does_not_establish) >= {"database_consistency", "restore_verified", "authenticity"}


def test_b_the_bridge_derives_exactly_the_observed_facts_and_nothing_else():
    result = no_difference()
    record = catalog_observation_record(result)
    assert record.to_document() == observation_doc(), "the bridge equals the independently built record"
    for got, source_endpoint in zip(record.endpoints, (result.before, result.after)):
        assert got.backend_pid == source_endpoint.backend_pid
        assert got.backend_start == source_endpoint.backend_start.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        assert got.transaction_start == source_endpoint.transaction_start.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        assert (got.isolation, got.read_only, got.cleanup_verification) == (source_endpoint.isolation, source_endpoint.read_only, VERIFIED_CLOSED)
        assert vars(got.digests) == source_endpoint.observation.digests, "no digest dropped or renamed"
    assert vars(record.source) == vars(SOURCE)


def test_b_non_utc_server_times_are_rendered_in_utc():
    plus2 = dt.timezone(dt.timedelta(hours=2))
    before = endpoint("before", 811, T_BEFORE_BACKEND.astimezone(plus2), T_BEFORE_TXN.astimezone(plus2))
    after = endpoint("after", 812, T_AFTER_BACKEND, T_AFTER_TXN)
    record = catalog_observation_record(require_unchanged(before, after, SOURCE))
    assert record.endpoints[0].transaction_start == "2026-10-02T18:30:00.060000Z"


def test_b_v2_written_by_the_one_shot_writer_and_read_back(tmp_path):
    inputs = build_inputs(tmp_path)
    result = write_artifact(inputs.store, inputs.archive, v2_metadata(inputs), inputs.baseline, durability=DurabilityMode.REQUIRED, ops=Probe())
    written = json.loads((inputs.final / "manifest.json").read_bytes())
    assert written["format"] == "napsedge.backup-artifact/2"
    assert "catalog_bracket_equal" not in written["consistency"] and written["consistency"]["catalog_observation"] == observation_doc()
    verdict = verify_artifact_directory(result.path)
    assert verdict.valid and verdict.manifest.consistency.catalog_observation == catalog_observation_record(no_difference())


def test_b_the_baseline_and_payload_are_identical_between_v1_and_v2(tmp_path):
    """No baseline or payload field is discarded by /2: the baseline and archive bytes are the same; only consistency differs."""
    one, two = build_inputs(tmp_path, root_name="v1"), build_inputs(tmp_path, root_name="v2")
    write_artifact(one.store, one.archive, one.metadata, one.baseline, ops=Probe())
    write_artifact(two.store, two.archive, v2_metadata(two), two.baseline, ops=Probe())
    for name in ("archive.dump", "baseline.json"):
        assert (one.final / name).read_bytes() == (two.final / name).read_bytes()
    m1, m2 = (json.loads((d / "manifest.json").read_bytes()) for d in (one.final, two.final))
    c1, c2 = m1.pop("consistency"), m2.pop("consistency")
    m1.pop("format"), m2.pop("format")
    assert m1 == m2, "every non-consistency manifest field is carried unchanged"
    assert {k: v for k, v in c1.items() if k != "catalog_bracket_equal"} == {k: v for k, v in c2.items() if k != "catalog_observation"}


# =========================================================================== C. /2 rejections
def _obs(m):
    return m["consistency"]["catalog_observation"]


def _ep(m, index):
    return _obs(m)["endpoints"][index]


def _recombine(e):
    e["digests"]["combined"] = hashlib.sha256("|".join(e["digests"][c] for c in ("constraints", "indexes", "acls")).encode()).hexdigest()


def _unequal(m):
    _ep(m, 1)["digests"]["indexes"] = sha256(b"a committed index change")
    _recombine(_ep(m, 1))


V2_REJECTIONS = [
    ("missing_observation", lambda m: m["consistency"].pop("catalog_observation"), "FIELD_MISSING", "catalog_observation"),
    ("bare_boolean_true", lambda m: m["consistency"].update(catalog_observation=True), "FIELD_TYPE", "catalog_observation"),
    ("bare_string", lambda m: m["consistency"].update(catalog_observation="no_difference_observed"), "FIELD_TYPE", "catalog_observation"),
    ("empty_object", lambda m: m["consistency"].update(catalog_observation={}), "FIELD_MISSING", "catalog_observation.method"),
    ("legacy_and_new_mixed", lambda m: m["consistency"].update(catalog_bracket_equal=True), "FIELD_UNKNOWN", "catalog_bracket_equal"),
    ("legacy_only_under_v2", lambda m: (m["consistency"].pop("catalog_observation"), m["consistency"].update(catalog_bracket_equal=True)),
     "FIELD_MISSING", "catalog_observation"),
    ("unknown_version", lambda m: m.update(format="napsedge.backup-artifact/3"), "UNSUPPORTED_VERSION", "backup-artifact/3"),
    ("wrong_method", lambda m: _obs(m).update(method="in_transaction_bracket"), "FIELD_VALUE", "method"),
    ("changed_result", lambda m: _obs(m).update(result="catalog_changed"), "FIELD_VALUE", "result"),
    ("category_missing", lambda m: _obs(m).update(categories=["constraints", "indexes"]), "FIELD_VALUE", "categories"),
    ("categories_reordered", lambda m: _obs(m).update(categories=["acls", "constraints", "indexes"]), "FIELD_VALUE", "categories"),
    ("unknown_key", lambda m: _obs(m).update(stable=True), "FIELD_UNKNOWN", "stable"),
    ("one_endpoint", lambda m: _obs(m)["endpoints"].pop(), "FIELD_VALUE", "endpoints"),
    ("three_endpoints", lambda m: _obs(m)["endpoints"].append(copy.deepcopy(_ep(m, 1))), "FIELD_VALUE", "endpoints"),
    ("endpoints_out_of_order", lambda m: _obs(m)["endpoints"].reverse(), "FIELD_VALUE", "endpoints[0].label"),
    ("endpoint_without_digests", lambda m: _ep(m, 0).pop("digests"), "FIELD_MISSING", "digests"),
    ("endpoint_without_category_digest", lambda m: _ep(m, 1)["digests"].pop("acls"), "FIELD_MISSING", "digests.acls"),
    ("endpoint_without_transaction_start", lambda m: _ep(m, 1).pop("transaction_start"), "FIELD_MISSING", "transaction_start"),
    ("endpoint_unknown_key", lambda m: _ep(m, 0).update(query="SELECT 1"), "FIELD_UNKNOWN", "query"),
    ("unequal_digests", _unequal, "FIELD_VALUE", "differ"),
    ("combined_not_derived", lambda m: [_ep(m, i)["digests"].update(combined=sha256(b"x")) for i in (0, 1)], "FIELD_VALUE", "combined"),
    ("digest_upper_case", lambda m: _ep(m, 0)["digests"].update(indexes=_ep(m, 0)["digests"]["indexes"].upper()), "FIELD_VALUE", "digests.indexes"),
    ("digest_short", lambda m: _ep(m, 0)["digests"].update(acls="ab" * 31), "FIELD_VALUE", "digests.acls"),
    ("repeatable_read_endpoint", lambda m: _ep(m, 1).update(isolation="repeatable read"), "FIELD_VALUE", "isolation"),
    ("writable_endpoint", lambda m: _ep(m, 0).update(read_only="off"), "FIELD_VALUE", "read_only"),
    ("cleanup_not_verified", lambda m: _ep(m, 1).update(cleanup_verification="not_verified"), "FIELD_VALUE", "cleanup_verification"),
    ("same_transaction", lambda m: _ep(m, 1).update({k: _ep(m, 0)[k] for k in ("backend_pid", "backend_start", "transaction_start")}),
     "FIELD_VALUE", "distinct"),
    ("after_precedes_before", lambda m: _ep(m, 1).update(backend_start="2026-10-02T18:29:59.000000Z", transaction_start="2026-10-02T18:29:59.500000Z"),
     "FIELD_VALUE", "earlier than the before"),
    ("transaction_before_backend", lambda m: _ep(m, 0).update(transaction_start="2026-10-02T18:30:00.040000Z"), "FIELD_VALUE", "backend_start"),
    ("pid_boolean", lambda m: _ep(m, 0).update(backend_pid=True), "FIELD_TYPE", "backend_pid"),
    ("pid_zero", lambda m: _ep(m, 0).update(backend_pid=0), "FIELD_VALUE", "backend_pid"),
    ("timestamp_without_fraction", lambda m: _ep(m, 0).update(backend_start="2026-10-02T18:30:00Z"), "TIMESTAMP_INVALID", "backend_start"),
    ("source_system_identifier", lambda m: _obs(m)["source"].update(system_identifier="7550123456789012999"), "FIELD_VALUE",
     "source.system_identifier: differs"),
    ("source_database_oid", lambda m: _obs(m)["source"].update(database_oid=16385), "FIELD_VALUE", "source.database_oid: differs"),
    ("source_database_name", lambda m: _obs(m)["source"].update(database_name="napsedge_restore"), "FIELD_VALUE", "source.database_name: differs"),
    ("source_incomplete", lambda m: _obs(m)["source"].pop("database_oid"), "FIELD_MISSING", "source.database_oid"),
]


@pytest.mark.parametrize("case, edit, code, detail", V2_REJECTIONS, ids=[c[0] for c in V2_REJECTIONS])
def test_c_malformed_v2_records_are_rejected_with_their_code(case, edit, code, detail):
    document = v2_manifest_doc()
    edit(document)
    with pytest.raises(ArtifactFormatError) as info:
        parse_doc(document)
    assert info.value.code == code, (case, info.value.code, info.value.detail)
    assert detail in info.value.detail, (case, info.value.detail)


def test_c_the_v1_bracket_relabelled_as_v2_is_not_reinterpreted():
    document = manifest_doc()
    document["format"] = ba.MANIFEST_FORMAT_V2
    with pytest.raises(ArtifactFormatError) as info:
        parse_doc(document)
    assert info.value.code == "FIELD_MISSING" and "catalog_observation" in info.value.detail


def test_c_a_wrong_source_record_inside_a_bound_directory_is_invalid(tmp_path):
    parts = v2_parts()
    _obs(parts.mdoc)["source"]["system_identifier"] = "1"
    verdict = verify_artifact_directory(materialize(tmp_path, parts))
    assert not verdict.valid and verdict.codes == ("FIELD_VALUE",) and "source.system_identifier" in verdict.reasons[0].detail


def test_c_a_manifest_cannot_be_built_with_a_foreign_consistency_type():
    manifest = parse_doc(manifest_doc())
    forged = ArtifactManifest(**{**vars(manifest), "consistency": {"catalog_bracket_equal": True}})
    with pytest.raises(ArtifactFormatError) as info:
        forged.to_document()
    assert info.value.code == "FIELD_TYPE"


@pytest.mark.parametrize("value", [True, False, None, {}, "no_difference_observed", observation_doc()])
def test_c_the_bridge_accepts_only_a_no_difference_result(value):
    with pytest.raises(Exception) as info:
        catalog_observation_record(value)
    assert "NoDifferenceObserved result is required" in str(info.value)


def test_c_the_bridge_refuses_incomplete_changed_foreign_naive_or_tampered_results():
    complete = no_difference()
    incomplete = NoDifferenceObserved(complete.before, endpoint("after", 812, T_AFTER_BACKEND, T_AFTER_TXN, cleanup="not_verified"), complete.digests)
    with pytest.raises(CatalogObservationFailed):
        catalog_observation_record(incomplete)
    changed_observation = CatalogObservation(OBSERVATION.constraints, (), OBSERVATION.acls)
    changed = NoDifferenceObserved(complete.before, endpoint("after", 812, T_AFTER_BACKEND, T_AFTER_TXN, observation=changed_observation), complete.digests)
    with pytest.raises(CatalogChanged):
        catalog_observation_record(changed)
    foreign = NoDifferenceObserved(complete.before, endpoint("after", 812, T_AFTER_BACKEND, T_AFTER_TXN, source=SourceIdentity(SYSID, OID, "other")),
                                   complete.digests)
    with pytest.raises(CatalogObservationFailed):
        catalog_observation_record(foreign)
    naive = NoDifferenceObserved(endpoint("before", 811, T_BEFORE_BACKEND.replace(tzinfo=None), T_BEFORE_TXN.replace(tzinfo=None)),
                                 endpoint("after", 812, T_AFTER_BACKEND.replace(tzinfo=None), T_AFTER_TXN.replace(tzinfo=None)), complete.digests)
    with pytest.raises(Exception, match="timezone-aware"):
        catalog_observation_record(naive)
    tampered = NoDifferenceObserved(complete.before, complete.after, {**complete.digests, "indexes": sha256(b"other")})
    with pytest.raises(Exception, match="digests are not the digests"):
        catalog_observation_record(tampered)


def test_c_the_writer_refuses_an_observation_from_another_source_before_creating_anything(tmp_path):
    inputs = build_inputs(tmp_path)
    foreign_source = SourceIdentity("1234", OID, DBNAME)
    result = require_unchanged(endpoint("before", 811, T_BEFORE_BACKEND, T_BEFORE_TXN, source=foreign_source),
                               endpoint("after", 812, T_AFTER_BACKEND, T_AFTER_TXN, source=foreign_source), foreign_source)
    with pytest.raises(PreconditionError) as info:
        write_artifact(inputs.store, inputs.archive, v2_metadata(inputs, catalog_observation_record(result)), inputs.baseline, ops=Probe())
    assert "FIELD_VALUE" in str(info.value) and names(inputs.store) == []


# =========================================================================== D. COMPLETE is structural finalization only
def test_d_complete_claims_files_and_structure_only_and_no_field_claims_more(tmp_path):
    inputs = build_inputs(tmp_path)
    result = write_artifact(inputs.store, inputs.archive, v2_metadata(inputs), inputs.baseline, ops=Probe())
    assert result.establishes == ("files_complete", "staged_structure_validated")
    assert result.does_not_establish == ("database_consistency", "restore_verified", "authenticity")
    verdict = verify_artifact_directory(result.path)
    assert verdict.establishes == ("structural_integrity",) and "restore_verified" in verdict.does_not_establish
    words = " ".join(keys_of(json.loads((inputs.final / "manifest.json").read_bytes()))).lower()
    for forbidden in ("restore", "recover", "ready", "stable", "stability", "verified"):
        assert forbidden not in words, forbidden
    complete = (inputs.final / "COMPLETE").read_text(encoding="ascii").splitlines()
    assert [line.split("=")[0] for line in complete] == ["napsedge.backup-complete/1", "artifact_id", "manifest_sha256", "manifest_bytes"]


# =========================================================================== E. reservation before any capture input exists
def test_e_reservation_needs_no_source_baseline_or_archive_and_creates_only_running(tmp_path):
    parameters = set(inspect.signature(reserve_artifact).parameters)
    assert parameters == {"parent_directory", "artifact_id", "logical_name", "created_utc", "durability", "ops"}
    inputs = build_inputs(tmp_path)
    inputs.archive.unlink()  # no archive exists anywhere yet
    probe = Probe()
    reservation = reserve(inputs, ops=probe)
    assert reservation.state == "reserved" and reservation.path == str(inputs.partial) and reservation.final_name == inputs.final_name
    assert names(inputs.store) == [inputs.partial.name] and names(inputs.partial) == ["RUNNING"]
    running = json.loads((inputs.partial / "RUNNING").read_bytes())
    assert running == {"format": "napsedge.backup-running/1", "artifact_id": ARTIFACT_ID, "pid": 4242, "utc": FIXED_NOW_TEXT}
    assert [e for e in probe.events if e[0] != "checkpoint"] == [("fsync_directory", "parent"), ("mkdir_private", inputs.partial.name),
                                                                 ("open_exclusive", "RUNNING"), ("write", "RUNNING"), ("fsync_file", "RUNNING")]
    assert ("open_source", "dump.bin") not in probe.events
    assert verify_artifact_directory(inputs.partial).codes == ("NOT_FINAL",)
    probe.assert_every_descriptor_closed_exactly_once()


@pytest.mark.parametrize("field, value", [
    ("artifact_id", "3F2B8C1E-6A47-4D0E-9B53-1C7A2D9E5F08"), ("artifact_id", "3f2b8c1e-6a47-1d0e-9b53-1c7a2d9e5f08"),
    ("artifact_id", "../3f2b8c1e-6a47-4d0e-9b53-1c7a2d9e5f08"), ("artifact_id", ""), ("artifact_id", None),
    ("logical_name", "NapsEdge"), ("logical_name", "a/b"), ("logical_name", ""), ("created_utc", "2026-10-02T18:30:00Z"),
    ("created_utc", "2026-02-30T18:30:00.000000Z"), ("created_utc", 1759429800),
])
def test_e_invalid_reservation_inputs_are_refused_before_anything_is_created(tmp_path, field, value):
    inputs = build_inputs(tmp_path)
    probe = Probe()
    arguments = {"artifact_id": ARTIFACT_ID, "logical_name": LOGICAL, "created_utc": CREATED, field: value}
    with pytest.raises(PreconditionError) as info:
        reserve_artifact(inputs.store, durability=DurabilityMode.REQUIRED, ops=probe, **arguments)
    assert info.value.stage == "reservation" and names(inputs.store) == [] and probe.events == []


def test_e_an_unusable_or_linked_store_is_refused_before_anything_is_created(tmp_path):
    inputs = build_inputs(tmp_path)
    for store in (tmp_path / "missing", inputs.archive):
        with pytest.raises(PreconditionError) as info:
            reserve_artifact(store, artifact_id=ARTIFACT_ID, logical_name=LOGICAL, created_utc=CREATED, ops=Probe())
        assert info.value.stage == "store"
    link = tmp_path / "linked_store"
    make_directory_link(link, inputs.store)
    with pytest.raises(PreconditionError) as info:
        reserve_artifact(link, artifact_id=ARTIFACT_ID, logical_name=LOGICAL, created_utc=CREATED, ops=Probe())
    assert "SYMLINK" in str(info.value) and names(inputs.store) == []


def test_e_required_durability_that_is_unavailable_refuses_before_creating(tmp_path):
    inputs = build_inputs(tmp_path)
    with pytest.raises(PreconditionError) as info:
        reserve(inputs, ops=Probe(directory_flush="unsupported"))
    assert info.value.stage == "durability" and names(inputs.store) == []


def test_e_a_failure_after_the_exclusive_creation_records_failed_like_the_one_shot_writer(tmp_path):
    inputs = build_inputs(tmp_path)
    with pytest.raises(WriteFailed) as info:
        reserve(inputs, ops=Probe(fail={("fsync_file", "RUNNING"): OSError(errno.EIO, PLANTED)}))
    assert info.value.reason_class == "durability_failed" and info.value.failed_record == "recorded" and info.value.stage == "staging"
    assert read_failed_record(inputs.partial)["reason_class"] == "durability_failed" and SECRET not in str(info.value)


# =========================================================================== F. an early capture failure
def test_f_an_early_capture_failure_records_the_supported_failed_record_and_is_never_adoptable(tmp_path):
    inputs = build_inputs(tmp_path)
    reservation = reserve(inputs)
    primary = SnapshotSessionLost("the snapshot owner is no longer the same backend (OperationalError)")
    assert reservation.record_failure(capture_failure_reason(primary), primary) == "recorded"
    assert reservation.state == "failed" and reservation.failed_record == "recorded"
    assert not getattr(primary, "__notes__", None), "a successful recording adds nothing to the primary"
    record = read_failed_record(inputs.partial)
    assert record == {"format": "napsedge.backup-failed/1", "artifact_id": ARTIFACT_ID, "utc": FIXED_NOW_TEXT, "reason_class": "snapshot_unavailable"}
    assert names(inputs.partial) == ["FAILED"], "FAILED supersedes RUNNING; no COMPLETE, no payload"
    assert verify_artifact_directory(inputs.partial).codes == ("NOT_FINAL",)
    assert "ARTIFACT_FAILED" in verdict_as_final(inputs.partial, tmp_path, inputs.final_name).codes
    assert not inputs.final.exists()


def test_f_after_a_recorded_failure_no_finalization_or_second_record_is_possible(tmp_path):
    inputs = build_inputs(tmp_path)
    reservation = reserve(inputs)
    reservation.record_failure("dump_failed")
    before = tree_snapshot(inputs.store)
    kind, value = outcome_of(lambda: reservation.finalize(inputs.archive, inputs.metadata, inputs.baseline))
    assert kind == "raised" and type(value) is ReservationStateError, f"finalize after a recorded failure must be refused, got {kind} {type(value).__name__}"
    kind, value = outcome_of(lambda: reservation.record_failure("timeout"))
    assert kind == "raised" and type(value) is ReservationStateError, f"a second record must be refused, got {kind} {type(value).__name__}"
    assert tree_snapshot(inputs.store) == before and not inputs.final.exists()


def test_f_an_unknown_reason_class_is_refused_without_consuming_the_reservation(tmp_path):
    inputs = build_inputs(tmp_path)
    reservation = reserve(inputs)
    with pytest.raises(ValueError):
        reservation.record_failure("catalog_unstable")
    assert reservation.state == "reserved" and names(inputs.partial) == ["RUNNING"]


# =========================================================================== G. later capture failures map accurately, without secrets
G_CASES = [
    (CatalogChanged(["indexes"], {"indexes": {("teams", "ix"): (None, ("teams", "ix", PLANTED))}}), "catalog_changed"),
    (CatalogObservationFailed(f"catalog observer cleanup was not confirmed: verification=not_verified; {PLANTED}"), "internal_error"),
    (CatalogObservationFailed("the catalog observation did not complete (QueryCanceled)"), "internal_error"),
    (DumpTimeout(f"pg_dump exceeded its 600 s deadline {PLANTED}", stage="dump"), "timeout"),
    (DumpFailed("pg_dump exited with status 1", stage="dump", returncode=1, diagnostic=PLANTED), "dump_failed"),
    (DumpPreconditionError("the pg_dump client could not be started (FileNotFoundError)"), "dump_failed"),
    (DumpCleanupError(PLANTED, DumpCleanupReport(True, True, True, "not_verified", False, True, True, ())), "internal_error"),
    (SnapshotSetupError("S could not be opened (OperationalError)"), "snapshot_unavailable"),
    (SnapshotSessionLost(PLANTED), "snapshot_unavailable"),
    (SnapshotRefusedError("the snapshot session and the lock session address different sources (system_identifier)"), "snapshot_unavailable"),
    (SnapshotHandleClosed("used after exit"), "internal_error"),
    (DatabaseTargetVerificationError(PLANTED), "source_identity_mismatch"),
    (BackupLockNotHeld("L no longer holds the key (OperationalError)"), "internal_error"),
    (BackupLockSessionLost("a second physical connection was requested"), "internal_error"),
    (ValueError("catalog_changed timeout dump_failed"), "internal_error"),
    (KeyboardInterrupt(), "interrupted"),
    (SystemExit(3), "interrupted"),
]


@pytest.mark.parametrize("exc, reason", G_CASES, ids=[f"{type(c[0]).__name__}-{i}" for i, c in enumerate(G_CASES)])
def test_g_each_capture_failure_maps_to_an_existing_class_and_is_recorded_without_its_message(tmp_path, exc, reason):
    assert capture_failure_reason(exc) == reason and reason in w.FAILED_REASON_CLASSES
    inputs = build_inputs(tmp_path)
    reservation = reserve(inputs)
    assert reservation.record_failure(capture_failure_reason(exc), exc) == "recorded"
    assert read_failed_record(inputs.partial)["reason_class"] == reason
    assert SECRET.encode() not in all_bytes(inputs.store), "no exception text reaches the store"


def test_g_overlap_is_not_a_capture_failure_and_the_mapping_reads_no_message():
    assert capture_failure_reason(AdvisoryLockUnavailableError("another job holds the lock")) is None
    assert bridge.CAPTURE_FAILURE_CLASSES <= w.FAILED_REASON_CLASSES
    source = inspect.getsource(bridge.capture_failure_reason)
    assert "str(" not in source and ".args" not in source and "__notes__" not in source


def test_g_cleanup_or_observation_failures_are_never_catalog_changed():
    for exc in (CatalogObservationFailed("catalog observer cleanup was not confirmed"), DumpCleanupError("x", None)):
        assert capture_failure_reason(exc) != "catalog_changed"


# =========================================================================== H. FAILED-record I/O failure keeps the primary
def _raised_primary():
    try:
        try:
            raise ConnectionError(PLANTED)
        except ConnectionError as inner:
            raise DumpTimeout("pg_dump exceeded its 600 s deadline and was stopped", stage="dump") from inner
    except DumpTimeout as primary:
        primary.add_note("dump cleanup: backend=gone")
        return primary


@pytest.mark.parametrize("fault", [("open_exclusive", "FAILED"), ("write", "FAILED"), ("fsync_file", "FAILED")])
def test_h_failure_to_record_preserves_the_primary_and_leaves_no_artifact(tmp_path, fault):
    inputs = build_inputs(tmp_path)
    reservation = reserve(inputs, ops=Probe(fail={fault: OSError(errno.ENOSPC, PLANTED)}))
    primary = _raised_primary()
    cause, traceback_before, notes_before = primary.__cause__, traceback.format_tb(primary.__traceback__), list(primary.__notes__)
    kind, status = outcome_of(lambda: reservation.record_failure(capture_failure_reason(primary), primary))
    assert kind == "returned", f"the recording failure escaped and would replace the primary: {type(status).__name__}"
    assert status.startswith("failed: ") and reservation.failed_record == status
    assert primary.__cause__ is cause and traceback.format_tb(primary.__traceback__) == traceback_before
    assert primary.__notes__[: len(notes_before)] == notes_before and len(primary.__notes__) == len(notes_before) + 1
    added = primary.__notes__[-1]
    assert "timeout" in added and SECRET not in added and str(inputs.store) not in added and "Errno" not in added
    assert "RUNNING" in names(inputs.partial) and "COMPLETE" not in names(inputs.partial)
    assert verify_artifact_directory(inputs.partial).codes == ("NOT_FINAL",) and not inputs.final.exists()
    assert names(inputs.store) == [inputs.partial.name]


def test_h_a_failed_close_of_the_record_is_reported_conservatively_and_never_replaces_the_primary(tmp_path):
    inputs = build_inputs(tmp_path)
    probe = Probe(fail={("close", "FAILED"): OSError(errno.EIO, PLANTED)})
    reservation = reserve(inputs, ops=probe)
    primary = _raised_primary()
    kind, status = outcome_of(lambda: reservation.record_failure("timeout", primary))
    assert kind == "returned" and status.startswith("failed: "), f"the close failure escaped: {kind} {type(status).__name__}"
    assert type(primary) is DumpTimeout and SECRET not in primary.__notes__[-1]
    probe.assert_every_descriptor_closed_exactly_once()
    assert not inputs.final.exists()


# =========================================================================== I. interrupted or abandoned partials
def test_i_an_abandoned_reservation_stays_running_unadoptable_and_blocks_its_id(tmp_path):
    inputs = build_inputs(tmp_path)
    reservation = reserve(inputs)
    assert not hasattr(reservation, "__enter__") and not hasattr(reservation, "__exit__") and not hasattr(reservation, "__del__")
    del reservation
    assert names(inputs.partial) == ["RUNNING"]
    assert verify_artifact_directory(inputs.partial).codes == ("NOT_FINAL",)
    assert "ARTIFACT_RUNNING" in verdict_as_final(inputs.partial, tmp_path, inputs.final_name).codes
    before = tree_snapshot(inputs.store)
    with pytest.raises(PreconditionError, match="PARTIAL_EXISTS"):
        reserve(inputs)
    with pytest.raises(PreconditionError, match="PARTIAL_EXISTS"):
        write_artifact(inputs.store, inputs.archive, inputs.metadata, inputs.baseline, ops=Probe())
    assert tree_snapshot(inputs.store) == before


def test_i_an_interrupt_during_finalization_records_interrupted_and_propagates_itself(tmp_path):
    inputs = build_inputs(tmp_path)
    reservation = reserve(inputs, ops=Probe(fail={("write", "manifest.json"): KeyboardInterrupt()}))
    with pytest.raises(KeyboardInterrupt) as info:
        reservation.finalize(inputs.archive, inputs.metadata, inputs.baseline)
    assert any("interrupted" in note for note in info.value.__notes__)
    assert reservation.state == "failed" and read_failed_record(inputs.partial)["reason_class"] == "interrupted"
    assert "COMPLETE" not in names(inputs.partial) and not inputs.final.exists()


def test_i_an_interrupt_after_complete_but_before_publication_is_never_adoptable(tmp_path):
    """Since the 9B-6H follow-up (13.17) the marker this run wrote is removed before FAILED; the partial is never adoptable."""
    inputs = build_inputs(tmp_path)

    def interrupt(_path):
        raise KeyboardInterrupt()

    reservation = reserve(inputs, ops=Probe(hooks={"before_publication": interrupt}))
    with pytest.raises(KeyboardInterrupt):
        reservation.finalize(inputs.archive, inputs.metadata, inputs.baseline)
    assert names(inputs.partial) == ["FAILED", "archive.dump", "baseline.json", "manifest.json"] and not inputs.final.exists()
    assert verify_artifact_directory(inputs.partial).codes == ("NOT_FINAL",)
    assert "ARTIFACT_FAILED" in verdict_as_final(inputs.partial, tmp_path, inputs.final_name).codes


# =========================================================================== J. collisions and unowned paths
@pytest.mark.parametrize("occupant", ["empty_dir", "full_dir", "file"])
@pytest.mark.parametrize("where", ["destination", "partial"])
def test_j_an_existing_entry_is_refused_and_left_exactly_as_it_was(tmp_path, occupant, where):
    inputs = build_inputs(tmp_path)
    target = inputs.final if where == "destination" else inputs.partial
    if occupant == "file":
        target.write_bytes(b"someone else's file")
    else:
        target.mkdir()
        if occupant == "full_dir":
            (target / "RUNNING").write_bytes(b"another owner's marker")
    before = tree_snapshot(inputs.store)
    probe = Probe()
    with pytest.raises(PreconditionError) as info:
        reserve(inputs, ops=probe)
    assert ("DESTINATION_EXISTS" if where == "destination" else "PARTIAL_EXISTS") in str(info.value)
    assert tree_snapshot(inputs.store) == before and not [e for e in probe.events if e[0] in {"mkdir_private", "unlink", "rmdir", "rename"}]


def test_j_two_competing_reservations_have_exactly_one_owner_and_the_loser_touches_nothing(tmp_path):
    inputs = build_inputs(tmp_path)
    barrier = threading.Barrier(2, timeout=10)

    class Racing(Probe):
        def mkdir_private(self, path):
            barrier.wait()  # both have passed every check; both now attempt the exclusive creation
            super().mkdir_private(path)

    probes, outcomes = [Racing(), Racing()], [None, None]

    def contender(index):
        try:
            outcomes[index] = reserve(inputs, ops=probes[index])
        except BaseException as exc:  # noqa: BLE001 -- recorded for the assertion below
            outcomes[index] = exc

    threads = [threading.Thread(target=contender, args=(i,)) for i in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    winners = [o for o in outcomes if isinstance(o, ArtifactReservation)]
    losers = [o for o in outcomes if isinstance(o, PreconditionError)]
    assert len(winners) == 1 and len(losers) == 1 and "PARTIAL_EXISTS" in str(losers[0]), outcomes
    loser_probe = probes[outcomes.index(losers[0])]
    assert [e[0] for e in loser_probe.events if e[0] not in {"checkpoint", "fsync_directory"}] == ["mkdir_private"], "the loser only attempted creation"
    assert names(inputs.partial) == ["RUNNING"]
    winners[0].record_failure("internal_error")
    assert read_failed_record(inputs.partial)["reason_class"] == "internal_error"


def test_j_a_partial_that_appears_after_the_checks_belongs_to_its_creator(tmp_path):
    inputs = build_inputs(tmp_path)

    class Interloper(Probe):
        def mkdir_private(self, path):
            os.mkdir(path)
            Path(path, "owner.txt").write_text("someone else")
            super().mkdir_private(path)

    with pytest.raises(PreconditionError, match="PARTIAL_EXISTS"):
        reserve(inputs, ops=Interloper())
    assert names(inputs.partial) == ["owner.txt"] and (inputs.partial / "owner.txt").read_text() == "someone else"


def test_j_a_destination_that_appears_before_publication_is_never_replaced(tmp_path):
    inputs = build_inputs(tmp_path)

    def occupy(_path):
        inputs.final.mkdir()
        (inputs.final / "foreign.txt").write_text("not ours")

    reservation = reserve(inputs, ops=Probe(hooks={"before_publication": occupy}))
    with pytest.raises(WriteFailed) as info:
        reservation.finalize(inputs.archive, inputs.metadata, inputs.baseline)
    assert info.value.reason_class == "destination_exists" and info.value.published == "no"
    assert names(inputs.final) == ["foreign.txt"] and (inputs.final / "foreign.txt").read_text() == "not ours"
    assert "FAILED" in names(inputs.partial)


def test_j_recording_touches_only_the_owned_partial(tmp_path):
    inputs = build_inputs(tmp_path)
    (inputs.store / "napsedge-other-20261001T000000Z-9a1d3e7f-2c58-4b61-8f04-5d7e6a1b2c93").mkdir()
    reservation = reserve(inputs)
    bystander, neighbour = tree_snapshot(inputs.bystander), tree_snapshot(inputs.store)
    reservation.record_failure("catalog_changed")
    after = tree_snapshot(inputs.store)
    assert tree_snapshot(inputs.bystander) == bystander
    assert {k: v for k, v in after.items() if not k.startswith(inputs.partial.name)} == {k: v for k, v in neighbour.items() if not k.startswith(inputs.partial.name)}


# =========================================================================== K. explicit finalization reuses the one-shot writer
@pytest.mark.parametrize("version", ["v1", "v2"])
def test_k_finalize_produces_exactly_what_the_one_shot_writer_produces(tmp_path, version):
    one, two = build_inputs(tmp_path, root_name="oneshot"), build_inputs(tmp_path, root_name="reserved")
    meta_one, meta_two = (one.metadata, two.metadata) if version == "v1" else (v2_metadata(one), v2_metadata(two))
    expected = write_artifact(one.store, one.archive, meta_one, one.baseline, ops=Probe())
    probe = Probe()
    reservation = reserve(two, ops=probe)
    assert names(two.partial) == ["RUNNING"], "nothing is finalized until finalize() is called"
    got = reservation.finalize(two.archive, meta_two, two.baseline)
    assert reservation.state == "finalized" and got.path == str(two.final) and names(two.store) == [two.final_name]
    for name in ("archive.dump", "baseline.json", "manifest.json", "COMPLETE"):
        assert (one.final / name).read_bytes() == (two.final / name).read_bytes(), name
    assert {k: v for k, v in vars(got).items() if k != "path"} == {k: v for k, v in vars(expected).items() if k != "path"}
    assert got.archive_sha256 == sha256(ARCHIVE_BYTES) and got.durability.files_flushed[0] == "RUNNING"
    assert verify_artifact_directory(got.path).valid
    probe.assert_every_descriptor_closed_exactly_once()
    with pytest.raises(ReservationStateError):
        reservation.finalize(two.archive, meta_two, two.baseline)


@pytest.mark.parametrize("field, value", [("artifact_id", OTHER_ID), ("created_utc", "2026-10-02T18:31:00.000000Z"), ("logical_name", "napsedge_b")])
def test_k_finalize_with_metadata_for_another_artifact_records_failed_and_publishes_nothing(tmp_path, field, value):
    inputs = build_inputs(tmp_path)
    reservation = reserve(inputs)
    if field == "logical_name":
        metadata = ArtifactMetadata(**{**vars(inputs.metadata), "source": ba.SourceInfo(**{**vars(inputs.metadata.source), "logical_name": value})})
    else:
        metadata = ArtifactMetadata(**{**vars(inputs.metadata), field: value})
    with pytest.raises(WriteFailed) as info:
        reservation.finalize(inputs.archive, metadata, inputs.baseline)
    assert info.value.reason_class == "internal_error" and info.value.stage == "finalize_plan" and info.value.failed_record == "recorded"
    assert isinstance(info.value.__cause__.__cause__, PreconditionError)
    assert reservation.state == "failed" and names(inputs.partial) == ["FAILED"] and names(inputs.store) == [inputs.partial.name]


def test_k_finalize_failures_keep_the_one_shot_classification_and_cleanup_notes(tmp_path):
    inputs = build_inputs(tmp_path)
    probe = Probe(fail={("fsync_file", "archive.dump"): OSError(errno.EIO, PLANTED), ("close", "archive.dump"): OSError(errno.EBADF, PLANTED)})
    reservation = reserve(inputs, ops=probe)
    with pytest.raises(WriteFailed) as info:
        reservation.finalize(inputs.archive, inputs.metadata, inputs.baseline)
    assert info.value.reason_class == "durability_failed" and info.value.stage == "archive" and info.value.published == "no"
    assert info.value.cleanup_failures and SECRET not in " ".join(info.value.cleanup_failures) and SECRET not in str(info.value)
    assert read_failed_record(inputs.partial)["reason_class"] == "durability_failed" and not inputs.final.exists()
    probe.assert_every_descriptor_closed_exactly_once()


# =========================================================================== L. exception preservation
def test_l_an_interrupt_primary_is_recorded_as_interrupted_and_left_untouched(tmp_path):
    inputs = build_inputs(tmp_path)
    reservation = reserve(inputs)
    primary = KeyboardInterrupt()
    assert reservation.record_failure(capture_failure_reason(primary), primary) == "recorded"
    assert not getattr(primary, "__notes__", None) and read_failed_record(inputs.partial)["reason_class"] == "interrupted"


def test_l_a_new_interrupt_while_recording_does_not_replace_the_primary(tmp_path):
    """The established writer contract (`_failure`): a recording failure, even an interrupt, never replaces the primary."""
    inputs = build_inputs(tmp_path)
    reservation = reserve(inputs, ops=Probe(fail={("write", "FAILED"): KeyboardInterrupt()}))
    primary = _raised_primary()
    kind, status = outcome_of(lambda: reservation.record_failure("timeout", primary))
    assert (kind, status) == ("returned", "failed: KeyboardInterrupt"), f"the new interrupt escaped: {kind} {type(status).__name__}"
    assert type(primary) is DumpTimeout and "KeyboardInterrupt" in primary.__notes__[-1]
    assert "COMPLETE" not in names(inputs.partial) and verify_artifact_directory(inputs.partial).codes == ("NOT_FINAL",)


# =========================================================================== drift and static constraints
def test_the_contract_constants_match_the_catalog_observer():
    assert ba.CATALOG_OBSERVATION_CATEGORIES == tuple(CATEGORIES)
    assert ba.CATALOG_ENDPOINT_CLEANUP == VERIFIED_CLOSED
    assert observation_doc()["endpoints"][0]["digests"] == OBSERVATION.digests, "the contract's combined-digest rule is the observer's"


def test_the_bridge_is_pure_and_the_capture_modules_are_unaware_of_artifacts():
    tree = ast.parse((REPO_ROOT / "src" / "deployment" / "backup_capture_artifact.py").read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert imported <= {"__future__", "datetime", "src.deployment.backup_artifact", "src.deployment.backup_capture_catalog",
                        "src.deployment.backup_capture_dump", "src.deployment.backup_capture_snapshot", "src.exceptions"}, imported
    calls = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not calls & {"open", "write", "mkdir", "unlink", "rename", "execute", "connect", "run", "Popen"}
    for module in ("backup_capture_catalog", "backup_capture_dump", "backup_capture_snapshot", "backup_capture_lock"):
        text = (REPO_ROOT / "src" / "deployment" / f"{module}.py").read_text(encoding="utf-8")
        assert "backup_artifact_writer" not in text and "reserve_artifact" not in text, module


def test_no_restore_or_readiness_field_exists_in_either_manifest_version():
    for document in (manifest_doc(), v2_manifest_doc()):
        words = " ".join(keys_of(document)).lower()
        assert not any(w in words for w in ("restore", "recovery", "ready", "readiness"))


def test_the_writer_public_surface_grew_only_by_the_reservation():
    assert sorted(w.__all__) == sorted([
        "COPY_CHUNK_BYTES", "FAILED_FORMAT", "FAILED_REASON_CLASSES", "RUNNING_FORMAT", "ArtifactMetadata", "ArtifactReservation", "ArtifactWriteError",
        "DurabilityMode", "DurabilityReport", "DurabilityUnsupported", "FileSystemOps", "OwnedPartial", "PostPublicationError", "PreconditionError",
        "ReservationStateError", "WriteFailed", "WrittenArtifact", "reserve_artifact", "write_artifact", "write_failed_record"])
    assert w.FAILED_REASON_CLASSES == frozenset({"dump_failed", "snapshot_unavailable", "baseline_failed", "checksum_mismatch", "source_identity_mismatch",
                                                 "schema_head_mismatch", "catalog_changed", "timeout", "storage_full", "interrupted", "internal_error",
                                                 "destination_exists", "durability_failed"}), "the FAILED schema is unchanged"
    assert w.FAILED_FORMAT == "napsedge.backup-failed/1"


# =========================================================================== M. the completion marker on failure, and the publication commit point (13.17)
STAGED = ["archive.dump", "baseline.json", "manifest.json"]


def _corrupt_staged_archive(path):
    with open(os.path.join(path, "archive.dump"), "ab") as handle:
        handle.write(b"x")


def _raise(error):
    def hook(_path):
        raise error
    return hook


def _seams():
    """(id, the injected primary, expected stage, expected class). Every seam lies after COMPLETE was created and before the rename committed."""
    seams = [
        ("marker_write", OSError(errno.EIO, PLANTED), "marker", "internal_error"),  # a failed write is internal_error (12.1); flush/close are durability_failed
        ("marker_flush", OSError(errno.EIO, PLANTED), "marker", "durability_failed"),
        ("marker_close", OSError(errno.EIO, PLANTED), "marker", "durability_failed"),
        ("flush_after_marker", OSError(errno.EIO, PLANTED), "flush_after_marker", "durability_failed"),
        ("validation", None, "validation", "checksum_mismatch"),
        ("before_publication_error", RuntimeError(PLANTED), "publication", "internal_error"),
        ("rename_not_performed", OSError(errno.EXDEV, PLANTED), "publication", "internal_error"),
    ]
    if sys.platform != "win32":
        seams.append(("posix_reservation_refused", FileExistsError(errno.EEXIST, PLANTED), "publication", "destination_exists"))
    return seams


def _seam_probe(seam, primary, inputs) -> Probe:
    specs = {
        "marker_write": {"fail": {("write", "COMPLETE"): primary}},
        "marker_flush": {"fail": {("fsync_file", "COMPLETE"): primary}},
        "marker_close": {"fail": {("close", "COMPLETE"): primary}},
        "flush_after_marker": {"fail": {("fsync_directory", "staging"): (2, primary)}},
        "validation": {"hooks": {"before_validation": _corrupt_staged_archive}},
        "before_publication_error": {"hooks": {"before_publication": _raise(primary)}},
        "rename_not_performed": {"fail": {("rename", f".partial-{ARTIFACT_ID}"): primary}},
        "posix_reservation_refused": {"fail": {("mkdir_reservation", inputs.final_name): primary}},
    }
    return Probe(**specs[seam])


def _attempt(path_kind, inputs, probe):
    if path_kind == "reservation":
        reservation = reserve(inputs, ops=probe)
        return reservation, outcome_of(lambda: reservation.finalize(inputs.archive, inputs.metadata, inputs.baseline))
    return None, outcome_of(lambda: write_artifact(inputs.store, inputs.archive, inputs.metadata, inputs.baseline, ops=probe))


def _chain(exc):
    found = []
    while exc is not None and all(exc is not seen for seen in found):
        found.append(exc)
        exc = exc.__cause__
    return found


SEAMS = _seams()


@pytest.mark.parametrize("path_kind", ["reservation", "one_shot"])
@pytest.mark.parametrize("seam, primary, stage, reason", SEAMS, ids=[s[0] for s in SEAMS])
def test_m_a_failure_after_complete_and_before_publication_removes_only_the_owned_marker(tmp_path, path_kind, seam, primary, stage, reason):
    inputs = build_inputs(tmp_path)
    probe = _seam_probe(seam, primary, inputs)
    reservation, (kind, raised) = _attempt(path_kind, inputs, probe)
    assert kind == "raised" and type(raised) is WriteFailed, f"{seam}: {kind} {type(raised).__name__}"
    assert (raised.stage, raised.reason_class, raised.published, raised.failed_record) == (stage, reason, "no", "recorded")
    if primary is not None:
        assert any(link is primary for link in _chain(raised)), "the original exception object is in the cause chain"
    assert SECRET not in str(raised) and not raised.cleanup_failures
    assert names(inputs.partial) == ["FAILED", *STAGED], "the marker this run wrote is gone; payload, manifest and FAILED are kept"
    removals = [i for i, e in enumerate(probe.events) if e == ("unlink", "COMPLETE")]
    assert len(removals) == 1 and removals[0] < probe.events.index(("open_exclusive", "FAILED")), "removed once, before FAILED"
    assert read_failed_record(inputs.partial)["reason_class"] == reason and not inputs.final.exists()
    assert verify_artifact_directory(inputs.partial).codes == ("NOT_FINAL",)
    shadow = verdict_as_final(inputs.partial, tmp_path, inputs.final_name)
    assert not shadow.valid and {"ARTIFACT_FAILED", "MISSING_FILE"} <= set(shadow.codes)
    if reservation is not None:
        assert reservation.state == "failed"
    probe.assert_every_descriptor_closed_exactly_once()


@pytest.mark.parametrize("path_kind", ["reservation", "one_shot"])
def test_m_an_interrupt_after_complete_is_itself_propagated_and_the_marker_removed(tmp_path, path_kind):
    inputs = build_inputs(tmp_path)
    interrupt = KeyboardInterrupt()
    _, (kind, raised) = _attempt(path_kind, inputs, Probe(hooks={"before_publication": _raise(interrupt)}))
    assert kind == "raised" and raised is interrupt and any("interrupted" in note for note in raised.__notes__)
    assert names(inputs.partial) == ["FAILED", *STAGED] and read_failed_record(inputs.partial)["reason_class"] == "interrupted"


@pytest.mark.parametrize("path_kind", ["reservation", "one_shot"])
def test_m_a_failed_marker_removal_keeps_the_original_failure_and_a_non_adoptable_partial(tmp_path, path_kind):
    inputs = build_inputs(tmp_path)
    primary = OSError(errno.EXDEV, PLANTED)
    probe = Probe(fail={("rename", f".partial-{ARTIFACT_ID}"): primary, ("unlink", "COMPLETE"): OSError(errno.EACCES, PLANTED)})
    _, (kind, raised) = _attempt(path_kind, inputs, probe)
    assert kind == "raised" and type(raised) is WriteFailed and raised.__cause__ is primary and raised.failed_record == "recorded"
    assert len(raised.cleanup_failures) == 1 and "completion marker" in raised.cleanup_failures[0] and f"errno {errno.EACCES}" in raised.cleanup_failures[0]
    assert SECRET not in raised.cleanup_failures[0] and SECRET not in " ".join(getattr(primary, "__notes__", ()))
    assert names(inputs.partial) == ["COMPLETE", "FAILED", *STAGED]
    assert verify_artifact_directory(inputs.partial).codes == ("NOT_FINAL",)
    assert "CONFLICTING_MARKERS" in verdict_as_final(inputs.partial, tmp_path, inputs.final_name).codes


def test_m_marker_removed_but_failed_record_not_written_still_leaves_no_adoptable_directory(tmp_path):
    inputs = build_inputs(tmp_path)
    primary = OSError(errno.EXDEV, PLANTED)
    reservation, (kind, raised) = _attempt("reservation", inputs, Probe(fail={("rename", f".partial-{ARTIFACT_ID}"): primary,
                                                                               ("open_exclusive", "FAILED"): OSError(errno.ENOSPC, PLANTED)}))
    assert kind == "raised" and type(raised) is WriteFailed and raised.__cause__ is primary and raised.failed_record.startswith("failed: ")
    assert names(inputs.partial) == STAGED, "no marker of any kind"
    assert verify_artifact_directory(inputs.partial).codes == ("NOT_FINAL",)
    assert "MISSING_FILE" in verdict_as_final(inputs.partial, tmp_path, inputs.final_name).codes
    assert reservation.state == "failed"


def test_m_both_cleanups_failing_is_reported_and_rests_on_not_final(tmp_path):
    """Marker removal AND the FAILED record fail: the partial holds a complete-looking set. The writer never publishes or adopts it and
    the public verifier refuses `.partial-*`; a MANUAL rename would make it look valid -- the documented residual limit."""
    inputs = build_inputs(tmp_path)
    primary = OSError(errno.EXDEV, PLANTED)
    _, (kind, raised) = _attempt("one_shot", inputs, Probe(fail={("rename", f".partial-{ARTIFACT_ID}"): primary,
                                                                 ("unlink", "COMPLETE"): OSError(errno.EIO, PLANTED),
                                                                 ("open_exclusive", "FAILED"): OSError(errno.ENOSPC, PLANTED)}))
    assert kind == "raised" and type(raised) is WriteFailed and raised.__cause__ is primary
    assert raised.failed_record.startswith("failed: ") and any("completion marker" in n for n in raised.cleanup_failures)
    assert names(inputs.partial) == ["COMPLETE", *STAGED] and verify_artifact_directory(inputs.partial).codes == ("NOT_FINAL",)
    assert not inputs.final.exists()


def test_m_an_interrupt_raised_by_the_marker_removal_escapes_with_the_primary_as_context(tmp_path):
    """The established cleanup contract (`_close_after_failure`): a NEW interrupt from cleanup is not swallowed; no FAILED is written then."""
    inputs = build_inputs(tmp_path)
    primary, interrupt = OSError(errno.EXDEV, PLANTED), KeyboardInterrupt()
    _, (kind, raised) = _attempt("one_shot", inputs, Probe(fail={("rename", f".partial-{ARTIFACT_ID}"): primary, ("unlink", "COMPLETE"): interrupt}))
    assert kind == "raised" and raised is interrupt and raised.__context__ is primary
    assert "FAILED" not in names(inputs.partial) and verify_artifact_directory(inputs.partial).codes == ("NOT_FINAL",) and not inputs.final.exists()


# -- after the commit point: never an ordinary failed partial, never deleted -------------------------------------------------
@pytest.mark.parametrize("path_kind", ["reservation", "one_shot"])
def test_m_a_rename_that_happened_and_then_raised_is_reported_published_and_left_alone(tmp_path, path_kind):
    inputs = build_inputs(tmp_path)
    probe = Probe(rename_then_raise=OSError(errno.EIO, PLANTED))
    reservation, (kind, raised) = _attempt(path_kind, inputs, probe)
    assert kind == "raised" and type(raised) is w.PostPublicationError and raised.published == "yes" and raised.stage == "publication"
    assert names(inputs.final) == sorted(["COMPLETE", *STAGED]) and verify_artifact_directory(inputs.final).valid
    assert not inputs.partial.exists() and ("unlink", "COMPLETE") not in probe.events and ("open_exclusive", "FAILED") not in probe.events
    if reservation is not None:
        assert reservation.state == "publication_uncertain"
        for action in (lambda: reservation.record_failure("internal_error"), lambda: reservation.finalize(inputs.archive, inputs.metadata, inputs.baseline)):
            assert type(outcome_of(action)[1]) is ReservationStateError


def test_m_an_unclassifiable_rename_is_reported_unknown_and_nothing_is_deleted(tmp_path):
    inputs = build_inputs(tmp_path)
    elsewhere = tmp_path / "moved_by_the_failing_rename"

    class Vanishing(Probe):
        def rename(self, source, destination):
            self.events.append(("rename", os.path.basename(source)))
            os.rename(source, elsewhere)  # neither the staging nor the final name exists afterwards
            raise OSError(errno.EIO, PLANTED)

    reservation, (kind, raised) = _attempt("reservation", inputs, Vanishing())
    assert kind == "raised" and type(raised) is w.PostPublicationError and raised.published == "unknown"
    assert names(elsewhere) == sorted(["COMPLETE", *STAGED]) and reservation.state == "publication_uncertain"


@pytest.mark.parametrize("path_kind", ["reservation", "one_shot"])
def test_m_a_parent_flush_failure_after_publication_keeps_the_published_artifact(tmp_path, path_kind):
    inputs = build_inputs(tmp_path)
    reservation, (kind, raised) = _attempt(path_kind, inputs, Probe(fail={("fsync_directory", "parent"): (2, OSError(errno.EIO, PLANTED))}))
    assert kind == "raised" and type(raised) is w.PostPublicationError and (raised.published, raised.stage) == ("yes", "flush_parent")
    assert verify_artifact_directory(inputs.final).valid and not inputs.partial.exists()
    if reservation is not None:
        assert reservation.state == "publication_uncertain"


# =========================================================================== N. /2 foreign-write-session counts: null means "not sampled" (13.18)
def test_n_v2_accepts_both_counts_null_as_not_sampled_and_still_accepts_integers():
    document = v2_manifest_doc()
    document["consistency"].update(foreign_write_sessions_start=None, foreign_write_sessions_end=None)
    manifest = parse_doc(document)
    assert (manifest.consistency.foreign_write_sessions_start, manifest.consistency.foreign_write_sessions_end) == (None, None)
    assert manifest.to_bytes() == oracle(document), "null round-trips canonically"
    assert parse_doc(v2_manifest_doc()).consistency.foreign_write_sessions_end == 0, "sampled integers remain valid"


@pytest.mark.parametrize("start, end, code", [(None, 0, "FIELD_VALUE"), (0, None, "FIELD_VALUE"), (-1, -1, "FIELD_VALUE"), (True, True, "FIELD_TYPE"),
                                              ("0", "0", "FIELD_TYPE")])
def test_n_v2_rejects_mixed_negative_or_mistyped_counts(start, end, code):
    document = v2_manifest_doc()
    document["consistency"].update(foreign_write_sessions_start=start, foreign_write_sessions_end=end)
    with pytest.raises(ArtifactFormatError) as info:
        parse_doc(document)
    assert info.value.code == code and "foreign_write_sessions" in info.value.detail


def test_n_v1_keeps_requiring_integer_counts():
    document = manifest_doc()
    document["consistency"].update(foreign_write_sessions_start=None, foreign_write_sessions_end=None)
    with pytest.raises(ArtifactFormatError) as info:
        parse_doc(document)
    assert info.value.code == "FIELD_TYPE" and "foreign_write_sessions_start" in info.value.detail


# =========================================================================== O. the expected-archive guard in writer staging (13.19)
EXPECTED_SHA = hashlib.sha256(ARCHIVE_BYTES).hexdigest()


def _same_size_other_bytes(data: bytes) -> bytes:
    flipped = bytearray(data)
    flipped[len(flipped) // 2] ^= 0xFF
    return bytes(flipped)


def _finalize_with(inputs, probe, **expectations):
    reservation = reserve(inputs, ops=probe)
    return reservation, outcome_of(lambda: reservation.finalize(inputs.archive, inputs.metadata, inputs.baseline, **expectations))


def _assert_rejected_before_publication(inputs, reservation, probe, raised, codes):
    assert type(raised) is WriteFailed, f"{type(raised).__name__}: a late or post-publication error does not satisfy prevention"
    assert (raised.stage, raised.reason_class, raised.published, raised.failed_record) == ("archive", "checksum_mismatch", "no", "recorded")
    assert raised.__cause__.codes == codes
    assert names(inputs.partial) == ["FAILED", "archive.dump"], "rejected right after the copy: no baseline, manifest or COMPLETE was written"
    assert not inputs.final.exists() and names(inputs.store) == [inputs.partial.name] and reservation.state == "failed"
    assert [e for e in probe.events if e == ("open_exclusive", "FAILED")] == [("open_exclusive", "FAILED")], "recorded exactly once"
    assert ("open_exclusive", "COMPLETE") not in probe.events and ("rename", f".partial-{ARTIFACT_ID}") not in probe.events
    assert verify_artifact_directory(inputs.partial).codes == ("NOT_FINAL",)


def test_o_matching_expectations_publish_exactly_what_omitting_them_publishes(tmp_path):
    one, two = build_inputs(tmp_path, root_name="without"), build_inputs(tmp_path, root_name="with")
    plain = reserve(one).finalize(one.archive, one.metadata, one.baseline)
    guarded = reserve(two).finalize(two.archive, two.metadata, two.baseline, expected_archive_sha256=EXPECTED_SHA, expected_archive_bytes=len(ARCHIVE_BYTES))
    for name in ("archive.dump", "baseline.json", "manifest.json", "COMPLETE"):
        assert (one.final / name).read_bytes() == (two.final / name).read_bytes(), name
    assert {k: v for k, v in vars(plain).items() if k != "path"} == {k: v for k, v in vars(guarded).items() if k != "path"}
    assert guarded.archive_sha256 == EXPECTED_SHA and guarded.archive_bytes == len(ARCHIVE_BYTES) and verify_artifact_directory(guarded.path).valid


def test_o_a_wrong_digest_of_the_correct_size_is_rejected_before_publication(tmp_path):
    inputs = build_inputs(tmp_path)
    other = hashlib.sha256(_same_size_other_bytes(ARCHIVE_BYTES)).hexdigest()
    probe = Probe()
    reservation, (kind, raised) = _finalize_with(inputs, probe, expected_archive_sha256=other, expected_archive_bytes=len(ARCHIVE_BYTES))
    assert kind == "raised"
    _assert_rejected_before_publication(inputs, reservation, probe, raised, ("FILE_DIGEST_MISMATCH",))


def test_o_a_wrong_size_with_the_correct_digest_is_rejected_before_publication(tmp_path):
    inputs = build_inputs(tmp_path)
    probe = Probe()
    reservation, (kind, raised) = _finalize_with(inputs, probe, expected_archive_sha256=EXPECTED_SHA, expected_archive_bytes=len(ARCHIVE_BYTES) + 1)
    assert kind == "raised"
    _assert_rejected_before_publication(inputs, reservation, probe, raised, ("FILE_SIZE_MISMATCH",))


def test_o_same_size_substituted_source_bytes_are_rejected(tmp_path):
    """The source path itself holds different bytes of the same size when finalize copies it: only the digest can catch this."""
    inputs = build_inputs(tmp_path)
    inputs.archive.write_bytes(_same_size_other_bytes(ARCHIVE_BYTES))
    probe = Probe()
    reservation, (kind, raised) = _finalize_with(inputs, probe, expected_archive_sha256=EXPECTED_SHA, expected_archive_bytes=len(ARCHIVE_BYTES))
    assert kind == "raised"
    _assert_rejected_before_publication(inputs, reservation, probe, raised, ("FILE_DIGEST_MISMATCH",))


@pytest.mark.parametrize("path_kind", ["guarded", "unguarded_characterization"])
def test_o_a_same_size_modification_during_the_copy_is_rejected_only_by_the_guard(tmp_path, monkeypatch, path_kind):
    """Deterministic: the copy reads 4 KiB chunks; after the first read the REST of the source is rewritten (same size) and its mtime
    restored, so the writer's existing size/mtime check cannot see it. With expectations the staged bytes are rejected; without
    them (characterization) the mixed bytes are published -- which is why the coordinator supplies them."""
    inputs = build_inputs(tmp_path)
    monkeypatch.setattr(w, "COPY_CHUNK_BYTES", 4096)
    before = os.stat(inputs.archive)

    def rewrite_tail(hit):
        if hit == 1:
            with open(inputs.archive, "r+b") as handle:
                handle.seek(len(ARCHIVE_BYTES) - 10)
                handle.write(b"X" * 10)
            os.utime(inputs.archive, ns=(before.st_atime_ns, before.st_mtime_ns))

    probe = Probe(hooks={"after_read": rewrite_tail})
    expectations = {"expected_archive_sha256": EXPECTED_SHA, "expected_archive_bytes": len(ARCHIVE_BYTES)} if path_kind == "guarded" else {}
    reservation, (kind, raised) = _finalize_with(inputs, probe, **expectations)
    if path_kind == "guarded":
        assert kind == "raised"
        _assert_rejected_before_publication(inputs, reservation, probe, raised, ("FILE_DIGEST_MISMATCH",))
    else:
        assert kind == "returned" and (inputs.final / "archive.dump").read_bytes() != ARCHIVE_BYTES, "without the guard the mixed bytes are published"


@pytest.mark.parametrize("expectations", [
    {"expected_archive_sha256": EXPECTED_SHA}, {"expected_archive_bytes": len(ARCHIVE_BYTES)},
    {"expected_archive_sha256": EXPECTED_SHA.upper(), "expected_archive_bytes": len(ARCHIVE_BYTES)},
    {"expected_archive_sha256": EXPECTED_SHA[:63], "expected_archive_bytes": len(ARCHIVE_BYTES)},
    {"expected_archive_sha256": 12345, "expected_archive_bytes": len(ARCHIVE_BYTES)},
    {"expected_archive_sha256": EXPECTED_SHA, "expected_archive_bytes": 0},
    {"expected_archive_sha256": EXPECTED_SHA, "expected_archive_bytes": True},
    {"expected_archive_sha256": EXPECTED_SHA, "expected_archive_bytes": str(len(ARCHIVE_BYTES))},
], ids=["digest_only", "size_only", "upper_case", "short_digest", "digest_not_text", "size_zero", "size_boolean", "size_text"])
def test_o_malformed_expectations_are_refused_as_a_finalize_precondition(tmp_path, expectations):
    inputs = build_inputs(tmp_path)
    probe = Probe()
    reservation, (kind, raised) = _finalize_with(inputs, probe, **expectations)
    assert kind == "raised" and type(raised) is WriteFailed and (raised.stage, raised.reason_class) == ("finalize_plan", "internal_error")
    assert isinstance(raised.__cause__.__cause__, PreconditionError) and "expected archive" in str(raised.__cause__.__cause__)
    assert names(inputs.partial) == ["FAILED"] and ("open_exclusive", "archive.dump") not in probe.events and not inputs.final.exists()
    assert reservation.state == "failed"


def test_o_a_failed_record_during_a_rejection_keeps_the_rejection_and_publishes_nothing(tmp_path):
    inputs = build_inputs(tmp_path)
    probe = Probe(fail={("open_exclusive", "FAILED"): OSError(errno.ENOSPC, PLANTED)})
    other = hashlib.sha256(_same_size_other_bytes(ARCHIVE_BYTES)).hexdigest()
    reservation, (kind, raised) = _finalize_with(inputs, probe, expected_archive_sha256=other, expected_archive_bytes=len(ARCHIVE_BYTES))
    assert kind == "raised" and type(raised) is WriteFailed and raised.reason_class == "checksum_mismatch" and raised.failed_record.startswith("failed: ")
    assert raised.__cause__.codes == ("FILE_DIGEST_MISMATCH",) and SECRET not in str(raised) and SECRET not in " ".join(raised.__notes__)
    assert names(inputs.partial) == ["RUNNING", "archive.dump"] and not inputs.final.exists()
    assert verify_artifact_directory(inputs.partial).codes == ("NOT_FINAL",) and reservation.state == "failed"


def test_o_post_publication_classification_is_unchanged_with_expectations(tmp_path):
    inputs = build_inputs(tmp_path)
    probe = Probe(rename_then_raise=OSError(errno.EIO, PLANTED))
    reservation, (kind, raised) = _finalize_with(inputs, probe, expected_archive_sha256=EXPECTED_SHA, expected_archive_bytes=len(ARCHIVE_BYTES))
    assert kind == "raised" and type(raised) is w.PostPublicationError and raised.published == "yes" and reservation.state == "publication_uncertain"
    assert verify_artifact_directory(inputs.final).valid and ("open_exclusive", "FAILED") not in probe.events
