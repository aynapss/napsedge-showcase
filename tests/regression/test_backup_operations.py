"""
Phase 9B-8B: scheduling, encrypted off-site transfer, retrieval, status and retention -- WITHOUT a database, Docker, a real store
or the real `age` tool. Artifacts are REAL (written by the accepted writer, accepted by the accepted verifier). The store and the
encryption tool are in-memory FAKES (`FakeStore`, `FakeAge`, labelled as such); the real `age` and a real S3 protocol path are covered by
`tests/postgres/test_postgres_backup_offsite.py` on the Linux runner.
"""
from __future__ import annotations

import configparser
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.deployment import backup_offsite as off
from src.deployment.backup_artifact_writer import DurabilityMode, write_artifact
from src.deployment.backup_cycle import backup_status, run_cycle
from src.deployment.backup_operations import OperationsConfig, OperationsConfigurationError, load_offsite_config, load_operations_config
from src.deployment.backup_retention import build_plan, execute_plan, passing_drills, run_retention
from tests.regression.test_deployment_backup_artifact_v2 import v2_metadata
from tests.regression.test_deployment_backup_artifact_writer import Probe, build_inputs

PLANTED = "hunter2-planted-offsite-secret"
NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------------------------------------------------------ fakes
class FakeStore:
    """IN-MEMORY FAKE of an S3-compatible store, with fault injection. Not an emulator of any real service's semantics.
    Versioned like B2 (Phase 9B-8C): every put through the API adds a version, `delete` by name adds a delete marker (hides),
    `delete_version` removes one version for good. Objects planted directly in `objects` count as ONE version written at time 0."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.calls: list[str] = []
        self.fail_put = 0
        self.fail_record_put = False
        self.corrupt_reads = 0
        self.fail_list = False
        self.fail_versions = False
        self.fail_delete_on: str | None = None
        self.log: list[tuple[int, str, str, bytes | None]] = []  # (sequence, key, version id, data or None for a delete marker)
        self.seq = 0

    def _version(self, key, data):
        if key in self.objects and not any(k == key for _, k, _, _ in self.log):
            self.log.append((0, key, "v0", self.objects[key]))  # a planted object is its own first version
        self.seq += 1
        self.log.append((self.seq, key, f"v{self.seq}", data))
        if data is None:
            self.objects.pop(key, None)
        else:
            self.objects[key] = data

    def put_file(self, key, path):
        self.calls.append(f"put:{key}")
        if self.fail_put:
            self.fail_put -= 1
            self._version(key, Path(path).read_bytes()[:10])  # an interrupted upload leaves a truncated object behind
            raise off.StoreError("upload failed (ClientError RequestTimeout)")
        self._version(key, Path(path).read_bytes())

    def put_bytes(self, key, data):
        self.calls.append(f"put:{key}")
        if self.fail_record_put and key.endswith(off.RECORD_NAME):
            raise off.StoreError("upload failed (ClientError InternalError)")
        self._version(key, bytes(data))

    def versions(self, prefix):
        if self.fail_versions:
            raise off.StoreError("version list failed (EndpointConnectionError)")
        logged = {k for _, k, _, _ in self.log}
        rows = [(0, k, "v0", v) for k, v in self.objects.items() if k not in logged] + list(self.log)
        latest = {}
        for seq, key, _, _ in rows:
            latest[key] = max(latest.get(key, -1), seq)
        return [off.VersionInfo(key, vid, len(data or b""), NOW + timedelta(seconds=seq), latest[key] == seq, data is None)
                for seq, key, vid, data in sorted(rows) if key.startswith(prefix)]

    def delete_version(self, key, version_id):
        self.calls.append(f"delete:{key}@{version_id}")
        if self.fail_delete_on and key.endswith(self.fail_delete_on):
            raise off.StoreError("delete failed (ClientError AccessDenied)")
        if version_id == "v0" and not any(k == key for _, k, _, _ in self.log):
            self.objects.pop(key, None)  # a planted object never versioned through the API
            return
        self.log = [row for row in self.log if not (row[1] == key and row[2] == version_id)]
        rest = [row for row in self.log if row[1] == key]
        if not rest:
            self.objects.pop(key, None)
        elif rest[-1][3] is None:
            self.objects.pop(key, None)
        else:
            self.objects[key] = rest[-1][3]

    def head(self, key):
        if key not in self.objects:
            return None
        logged = [vid for _, k, vid, _ in self.log if k == key]
        return off.ObjectInfo(key, len(self.objects[key]), logged[-1] if logged else None)  # planted objects report no version id

    def _read(self, key):
        data = self.objects[key]
        if self.corrupt_reads and key.endswith(off.PAYLOAD_NAME):
            self.corrupt_reads -= 1
            data = bytes([data[0] ^ 1]) + data[1:]
        return data

    def get_to_file(self, key, path):
        self.calls.append(f"get:{key}")
        Path(path).write_bytes(self._read(key))

    def get_bytes(self, key, limit):
        if key not in self.objects:
            raise off.StoreError("download failed (ClientError NoSuchKey)")
        return self._read(key)[: limit + 1]

    def list(self, prefix):
        if self.fail_list:
            raise off.StoreError("list failed (EndpointConnectionError)")
        return [off.ObjectInfo(k, len(v)) for k, v in sorted(self.objects.items()) if k.startswith(prefix)]

    def delete(self, key):
        self.calls.append(f"hide:{key}")
        if self.fail_delete_on and key.endswith(self.fail_delete_on):
            raise off.StoreError("delete failed (ClientError AccessDenied)")
        self._version(key, None)


class FakeAge:
    """FAKE of the `age` tool: a reversible transform, and an identity that must equal the planted key file content."""

    decrypts = 0

    def version(self):
        return "fake-age"

    def encrypt(self, source, target, recipients_file):
        off.read_recipients(recipients_file)
        Path(target).write_bytes(b"FAKEAGE" + Path(source).read_bytes()[::-1])

    def decrypt(self, source, target, identity_file):
        type(self).decrypts += 1
        if not os.path.exists(identity_file) or Path(identity_file).read_text().strip() != "FAKE-IDENTITY-OK":
            raise off.EncryptionError("decryption failed (age exit 1)")
        data = Path(source).read_bytes()
        if not data.startswith(b"FAKEAGE"):
            raise off.EncryptionError("decryption failed (age exit 1)")
        Path(target).write_bytes(data[len(b"FAKEAGE"):][::-1])


# --------------------------------------------------------------------------------------------------------------- fixtures
@pytest.fixture()
def artifact(tmp_path):
    inputs = build_inputs(tmp_path, root_name="capture")
    written = write_artifact(inputs.store, inputs.archive, v2_metadata(inputs), inputs.baseline, durability=DurabilityMode.REQUIRED, ops=Probe())
    return Path(written.path)


@pytest.fixture()
def env(tmp_path):
    recipients = tmp_path / "recipients.txt"
    recipients.write_text("# public key only\nage1examplepublicrecipientxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n", encoding="utf-8")
    identity = tmp_path / "identity.txt"
    identity.write_text("FAKE-IDENTITY-OK\n", encoding="utf-8")
    state = tmp_path / "state"
    work = tmp_path / "work"
    work.mkdir()
    return {"recipients": str(recipients), "identity": str(identity), "state": str(state), "work": str(work)}


def upload(artifact, env, store, **kw):
    return off.upload_artifact(artifact, store=store, prefix="napsedge", age=FakeAge(), recipients_file=env["recipients"], workdir=env["work"],
                               state_dir=env["state"], sleep=kw.pop("sleep", lambda s: None), now=lambda: NOW, **kw)


def tree_digest(path: Path) -> dict:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(path.iterdir())}


# ---------------------------------------------------------------------------------------------------- A. configuration
def test_a_defaults_are_the_documented_configured_targets():
    ops = load_operations_config({})
    record = ops.as_record()
    assert record["kind"] == "configured_targets" and record["rpo_hours"] == 24 and record["rto_hours"] == 4
    assert record["retention"] == {"daily": 7, "weekly": 4, "monthly": 6, "bucketing": "UTC"} and record["drill_interval_days"] == 31
    assert record["schedule"]["on_calendar"] == "*-*-* 03:30:00 UTC" and record["schedule"]["persistent"] is True
    assert ops.offsite_enabled is False and ops.retention_delete_enabled is False, "remote operations stay off until deployment"


@pytest.mark.parametrize("name, value", [("NAPSEDGE_BACKUP_RPO_HOURS", "0"), ("NAPSEDGE_BACKUP_RETAIN_DAILY", PLANTED),
                                         ("NAPSEDGE_BACKUP_OFFSITE_ENABLED", "yes"), ("NAPSEDGE_BACKUP_UPLOAD_ATTEMPTS", "11")])
def test_a_malformed_settings_are_refused_by_name_never_by_value(name, value):
    with pytest.raises(OperationsConfigurationError) as excinfo:
        load_operations_config({name: value})
    assert name in str(excinfo.value) and value not in str(excinfo.value)


def offsite_env(**overrides):
    base = {"NAPSEDGE_BACKUP_OFFSITE_ENDPOINT_URL": "https://s3.us-west-004.backblazeb2.com", "NAPSEDGE_BACKUP_OFFSITE_REGION": "us-west-004",
            "NAPSEDGE_BACKUP_OFFSITE_BUCKET": "napsedge-backups", "NAPSEDGE_BACKUP_OFFSITE_PREFIX": "napsedge",
            "NAPSEDGE_BACKUP_OFFSITE_KEY_ID_FILE": "/run/k/id", "NAPSEDGE_BACKUP_OFFSITE_SECRET_KEY_FILE": "/run/k/secret",
            "NAPSEDGE_BACKUP_RECIPIENTS_FILE": "/run/r/recipients.txt", "NAPSEDGE_BACKUP_STATE_DIR": "/var/lib/napsedge-backup/state"}
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


@pytest.mark.parametrize("overrides, fragment", [
    ({"NAPSEDGE_BACKUP_OFFSITE_ENDPOINT_URL": "http://s3.example"}, "must be https://"),
    ({"NAPSEDGE_BACKUP_OFFSITE_ENDPOINT_URL": f"https://key:{PLANTED}@s3.example"}, "must not carry credentials"),
    ({"NAPSEDGE_BACKUP_OFFSITE_PREFIX": "../escape"}, "plain relative prefix"),
    ({"NAPSEDGE_BACKUP_OFFSITE_SECRET_KEY_FILE": "secret"}, "absolute path"),
    ({"NAPSEDGE_BACKUP_RECIPIENTS_FILE": None}, "NAPSEDGE_BACKUP_RECIPIENTS_FILE"),
])
def test_a_offsite_configuration_is_strict_and_never_echoes_values(overrides, fragment):
    if os.name == "nt" and "absolute" in fragment:
        pytest.skip("POSIX absolute-path semantics")
    with pytest.raises(OperationsConfigurationError) as excinfo:
        load_offsite_config(offsite_env(**overrides))
    assert fragment in str(excinfo.value) and PLANTED not in str(excinfo.value)


# -------------------------------------------------------------------------------------------------------------- B. upload
def test_b_a_verified_artifact_is_stored_and_its_record_published_only_after_the_read_back(artifact, env):
    store, before = FakeStore(), tree_digest(artifact)
    result = upload(artifact, env, store)
    payload_key, record_key = off.object_keys("napsedge", artifact.name)
    assert result.outcome == "stored_verified" and result.attempts == 1, result
    assert f"get:{payload_key}" in store.calls and f"put:{record_key}" in store.calls, "the payload was read back and the record written"
    assert store.calls.index(f"get:{payload_key}") < store.calls.index(f"put:{record_key}"), "record only after the payload read-back"
    record = off.parse_record(store.objects[record_key], artifact.name)
    assert record["payload"]["sha256"] == hashlib.sha256(store.objects[payload_key]).hexdigest() and record["readback_verified"] is True
    assert record["encryption"]["recipients"][0].startswith("age1") and "FAKE-IDENTITY" not in json.dumps(record)
    assert tree_digest(artifact) == before, "the published artifact is never modified"
    assert sorted(os.listdir(artifact.parent)) == [artifact.name], "nothing added to the capture store (no FAILED, no partial)"
    assert os.listdir(env["work"]) == [], "the unencrypted tar and the ciphertext never outlive the upload"
    state = json.loads(Path(result.state_file).read_text())
    assert state["outcome"] == "stored_verified" and state["format"] == off.STATE_RECORD_FORMAT


def test_b_the_complete_artifact_round_trips_byte_for_byte(artifact, env, tmp_path):
    store = FakeStore()
    upload(artifact, env, store)
    destination = tmp_path / "restore_host"
    destination.mkdir()
    fetched = off.fetch_artifact(artifact.name, store=store, prefix="napsedge", age=FakeAge(), identity_file=env["identity"], destination=str(destination))
    assert tree_digest(fetched.artifact_dir) == tree_digest(artifact) and fetched.verdict.valid
    assert sorted(p.name for p in fetched.artifact_dir.iterdir()) == sorted(off.ARTIFACT_FILES), "manifest, baseline, archive and marker"


def test_b_an_unverified_artifact_is_refused_and_nothing_is_uploaded(artifact, env):
    (artifact / "archive.dump").write_bytes(b"tampered")
    store = FakeStore()
    result = upload(artifact, env, store)
    assert result.outcome == "refused" and store.objects == {} and "verifier" in result.detail


def test_b_transient_failures_are_retried_within_the_bound_with_backoff(artifact, env):
    store, sleeps = FakeStore(), []
    store.fail_put = 2
    result = upload(artifact, env, store, attempts=3, backoff_seconds=5, sleep=sleeps.append)
    assert result.outcome == "stored_verified" and result.attempts == 3 and sleeps == [5, 10]


def test_b_exhausted_retries_fail_and_publish_no_completion_record(artifact, env):
    store = FakeStore()
    store.fail_put = 5
    result = upload(artifact, env, store, attempts=3)
    assert result.outcome == "failed" and result.attempts == 3
    assert off.object_keys("napsedge", artifact.name)[1] not in store.objects, "an interrupted upload never looks stored"


def test_b_a_corrupted_read_back_is_never_accepted(artifact, env):
    store = FakeStore()
    store.corrupt_reads = 10
    result = upload(artifact, env, store, attempts=2)
    assert result.outcome == "failed" and "read-back" in result.detail and off.object_keys("napsedge", artifact.name)[1] not in store.objects


def test_b_an_unconfirmed_record_is_uncertain_never_stored(artifact, env):
    store = FakeStore()
    store.fail_record_put = True
    result = upload(artifact, env, store)
    assert result.outcome == "uncertain" and "NOT remotely stored" in result.detail


def test_b_an_existing_verified_record_is_not_re_uploaded_and_a_conflict_is_refused(artifact, env):
    store = FakeStore()
    upload(artifact, env, store)
    calls = len(store.calls)
    again = upload(artifact, env, store)
    assert again.outcome == "already_stored" and not any(c.startswith("put:") for c in store.calls[calls:])
    store.objects[off.object_keys("napsedge", artifact.name)[1]] = b"{not json"
    assert upload(artifact, env, store).outcome == "refused"


# ----------------------------------------------------------------------------------------------------------- C. retrieval
@pytest.fixture()
def stored(artifact, env, tmp_path):
    store = FakeStore()
    assert upload(artifact, env, store).outcome == "stored_verified"
    destination = tmp_path / "fetch"
    destination.mkdir()
    return store, destination


def test_c_a_wrong_or_missing_key_refuses_decryption(artifact, env, stored, tmp_path):
    store, destination = stored
    wrong = tmp_path / "wrong.txt"
    wrong.write_text("NOT-THE-KEY\n")
    for identity in (str(wrong), str(tmp_path / "absent.txt")):
        with pytest.raises(off.EncryptionError):
            off.fetch_artifact(artifact.name, store=store, prefix="napsedge", age=FakeAge(), identity_file=identity, destination=str(destination))


def test_c_a_corrupted_or_replaced_payload_is_refused_before_decryption(artifact, env, stored):
    store, destination = stored
    payload_key = off.object_keys("napsedge", artifact.name)[0]
    data = store.objects[payload_key]
    middle = len(data) // 2
    store.objects[payload_key] = data[:middle] + bytes([data[middle] ^ 1]) + data[middle + 1:]  # same size; still "decryptable" by the fake
    FakeAge.decrypts = 0
    try:
        off.fetch_artifact(artifact.name, store=store, prefix="napsedge", age=FakeAge(), identity_file=env["identity"], destination=str(destination))
        outcome = "accepted"
    except Exception as exc:  # noqa: BLE001 -- the type and message are the assertion
        outcome = f"{type(exc).__name__}: {exc}"
    assert outcome.startswith("RetrievalRefused") and "downloaded payload" in outcome, outcome
    assert FakeAge.decrypts == 0, "a payload that fails its digest is never decrypted"
    store.objects[payload_key] = store.objects[payload_key][:-1]
    with pytest.raises(off.RetrievalRefused, match="size"):
        off.fetch_artifact(artifact.name, store=store, prefix="napsedge", age=FakeAge(), identity_file=env["identity"], destination=str(destination))


@pytest.mark.parametrize("evil", ["extra_member", "traversal", "symlink"])
def test_c_a_hostile_archive_is_refused_whole(tmp_path, evil):
    name = "napsedge-napsedge-20261008T000000Z-x"
    archive = tmp_path / "a.tar"
    with tarfile.open(archive, "w") as tar:
        for f in off.ARTIFACT_FILES:
            info = tarfile.TarInfo(f"{name}/{f}")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        if evil == "extra_member":
            info = tarfile.TarInfo(f"{name}/extra")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        elif evil == "traversal":
            info = tarfile.TarInfo(f"{name}/../../escape")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        else:
            info = tarfile.TarInfo(f"{name}/link")
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tar.addfile(info)
    destination = tmp_path / "out"
    destination.mkdir()
    try:
        off.safe_extract(str(archive), destination, name, max_bytes=10_000)
        outcome = "extracted"
    except Exception as exc:  # noqa: BLE001 -- the type is the assertion
        outcome = type(exc).__name__
    assert outcome == "RetrievalRefused", outcome
    assert not (destination / name).exists() and not (tmp_path / "escape").exists()


def test_c_a_secret_key_in_the_recipients_file_is_refused(tmp_path):
    path = tmp_path / "r.txt"
    path.write_text("AGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ\n")
    with pytest.raises(off.EncryptionError, match="never a secret key"):
        off.read_recipients(str(path))


# --------------------------------------------------------------------------------------------------------------- D. cycle
def job_returning(*codes, artifact_name="napsedge-napsedge-20261008T033000Z-x"):
    sequence = list(codes)

    def job(environ, out):
        code = sequence.pop(0)
        outcome = {0: "published", 1: "failed", 3: "skipped_overlap", 4: "published_incomplete", 64: "configuration_refused"}[code]
        out.write(json.dumps({"job": "napsedge-backup-capture", "outcome": outcome, "exit_code": code, "artifact": artifact_name if code == 0 else None}) + "\n")
        return code

    return job


def cycle(env_vars, job, uploader=None, **kw):
    out = io.StringIO()
    code = run_cycle(env_vars, out=out, capture_job=job, uploader=uploader, now=lambda: NOW, **kw)
    return code, json.loads(out.getvalue().strip().splitlines()[-1])


def test_d_overlap_is_retried_a_bounded_number_of_times_then_reported_as_skipped(env):
    sleeps = []
    code, record = cycle({"NAPSEDGE_BACKUP_STATE_DIR": env["state"], "NAPSEDGE_BACKUP_OVERLAP_ATTEMPTS": "3", "NAPSEDGE_BACKUP_OVERLAP_DELAY_SECONDS": "60"},
                         job_returning(3, 3, 3), sleep=sleeps.append)
    assert code == 3 and record["outcome"] == "skipped_overlap" and len(record["capture"]["attempts"]) == 3 and sleeps == [60, 60]
    assert record["offsite"]["outcome"] == "not_attempted"
    code, record = cycle({"NAPSEDGE_BACKUP_STATE_DIR": env["state"]}, job_returning(3, 0), sleep=lambda s: None)
    assert code == 0 and [a["exit_code"] for a in record["capture"]["attempts"]] == [3, 0]


def test_d_the_cycle_deadline_bounds_the_retries(env):
    clock = iter([0, 7000, 7000, 7000])
    code, record = cycle({"NAPSEDGE_BACKUP_STATE_DIR": env["state"]}, job_returning(3, 3, 3, 3), sleep=lambda s: None, monotonic=lambda: next(clock))
    assert code == 3 and len(record["capture"]["attempts"]) == 1, "no attempt starts past the cycle deadline"


@pytest.mark.parametrize("capture_code", [1, 4, 64])
def test_d_capture_failures_pass_through_and_nothing_is_uploaded(env, capture_code):
    called = []
    code, record = cycle({"NAPSEDGE_BACKUP_STATE_DIR": env["state"], "NAPSEDGE_BACKUP_OFFSITE_ENABLED": "true"}, job_returning(capture_code),
                         uploader=lambda *a: called.append(a))
    assert code == capture_code and called == [] and record["states"]["locally_published"] is False


def test_d_offsite_is_off_by_default(env):
    code, record = cycle({"NAPSEDGE_BACKUP_STATE_DIR": env["state"]}, job_returning(0))
    assert code == 0 and record["offsite"]["outcome"] == "disabled" and record["states"]["remotely_stored"] is False


@pytest.mark.parametrize("outcome, exit_code", [("stored_verified", 0), ("already_stored", 0), ("failed", 20), ("uncertain", 21), ("refused", 22)])
def test_d_offsite_outcomes_are_kept_separate_from_the_capture(env, outcome, exit_code):
    code, record = cycle({"NAPSEDGE_BACKUP_STATE_DIR": env["state"], "NAPSEDGE_BACKUP_OFFSITE_ENABLED": "true", "NAPSEDGE_BACKUP_DATABASE_PASSWORD": PLANTED},
                         job_returning(0), uploader=lambda e, d, o: {"outcome": outcome})
    assert code == exit_code and record["capture"]["exit_code"] == 0 and record["capture"]["report"]["outcome"] == "published"
    assert record["offsite"]["outcome"] == outcome and record["states"]["remotely_stored"] is (exit_code == 0)
    lines = (Path(env["state"]) / "cycles.jsonl").read_text().splitlines()
    assert json.loads(lines[-1])["exit_code"] == exit_code and PLANTED not in "".join(lines)


def test_d_an_uploader_crash_is_an_offsite_failure_not_a_capture_failure(env):
    def boom(*a):
        raise RuntimeError(PLANTED)

    code, record = cycle({"NAPSEDGE_BACKUP_STATE_DIR": env["state"], "NAPSEDGE_BACKUP_OFFSITE_ENABLED": "true"}, job_returning(0), uploader=boom)
    assert code == 20 and record["capture"]["exit_code"] == 0 and PLANTED not in json.dumps(record)


# -------------------------------------------------------------------------------------------------------------- E. status
def write_cycles(state, entries):
    Path(state).mkdir(parents=True, exist_ok=True)
    with open(Path(state) / "cycles.jsonl", "w", encoding="utf-8") as handle:
        for hours_ago, code, stored in entries:
            handle.write(json.dumps({"finished_utc": (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ"), "exit_code": code,
                                     "states": {"locally_published": code == 0, "remotely_stored": stored}}) + "\n")


def status(env_vars, **kw):
    out = io.StringIO()
    code = backup_status(env_vars, out=out, now=lambda: NOW, **kw)
    return code, json.loads(out.getvalue())


def test_e_a_recent_remote_copy_and_a_recent_drill_are_ok(env):
    write_cycles(env["state"], [(30, 0, True), (6, 0, True)])
    code, result = status({"NAPSEDGE_BACKUP_STATE_DIR": env["state"], "NAPSEDGE_BACKUP_OFFSITE_ENABLED": "true"}, drill_lister=lambda e: NOW - timedelta(days=3),
                          version_checker=lambda e: {})
    assert code == 0 and result["status"] == "ok" and result["alerts"] == []


def test_e_missed_and_failed_backups_are_attention(env):
    write_cycles(env["state"], [(30, 0, True), (8, 3, False), (2, 3, False)])
    code, result = status({"NAPSEDGE_BACKUP_STATE_DIR": env["state"], "NAPSEDGE_BACKUP_OFFSITE_ENABLED": "true"}, drill_lister=lambda e: None,
                          version_checker=lambda e: {})
    assert code == 10 and any("24 h" in a for a in result["alerts"]) and any("consecutive" in a for a in result["alerts"])
    assert any("restore drill" in a for a in result["alerts"])
    write_cycles(env["state"], [(2, 0, True), (1, 20, False)])
    code, result = status({"NAPSEDGE_BACKUP_STATE_DIR": env["state"], "NAPSEDGE_BACKUP_OFFSITE_ENABLED": "true"}, drill_lister=lambda e: NOW,
                          version_checker=lambda e: {})
    assert code == 10 and result["alerts"] == ["the last cycle needs attention (exit 20)"]


def test_e_remote_version_anomalies_and_an_unlistable_history_are_attention(env):
    write_cycles(env["state"], [(2, 0, True)])
    base = {"NAPSEDGE_BACKUP_STATE_DIR": env["state"], "NAPSEDGE_BACKUP_OFFSITE_ENABLED": "true"}
    code, result = status(base, drill_lister=lambda e: NOW, version_checker=lambda e: {"napsedge-a": ["the payload changed after its completion record was written"]})
    assert code == 10 and len(result["alerts"]) == 1 and "version-history anomalies" in result["alerts"][0] and "napsedge-a" in result["alerts"][0]
    assert result["version_history"] == {"artifacts_with_anomalies": ["napsedge-a"]}

    def broken(e):
        raise off.StoreError("version list failed (ClientError AccessDenied)")

    code, result = status(base, drill_lister=lambda e: NOW, version_checker=broken)
    assert code == 10 and result["alerts"] == ["the remote version history could not be listed (StoreError)"]


def test_e_no_history_at_all_is_attention(env):
    code, result = status({"NAPSEDGE_BACKUP_STATE_DIR": env["state"]})
    assert code == 10 and result["last_locally_published_utc"] is None


READY_ENV = {"NAPSEDGE_BACKUP_OFFSITE_ENABLED": "true", "NAPSEDGE_BACKUP_RECOVERY_DRILL_HOST": ".db.ondigitalocean.com",
             "NAPSEDGE_BACKUP_KEY_COPIES_VERIFIED_UTC": "2026-10-01"}  # the test clock is 2026-10-08; a future date is never "met"


def provider_drill(**over):
    record = {"drilled_utc": (NOW - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ"), "duration_seconds_measured": 900.0,
              "endpoint_host": "napsedge-db-do-user-1-0.b.db.ondigitalocean.com"}
    record.update(over)
    return lambda e: record


def readiness(env, **kw):
    write_cycles(env["state"], [(6, 0, True)])
    kw.setdefault("drill_lister", lambda e: NOW - timedelta(days=3))
    kw.setdefault("version_checker", lambda e: {})
    code, result = status({"NAPSEDGE_BACKUP_STATE_DIR": env["state"], **READY_ENV, **kw.pop("extra_env", {})}, **kw)
    return code, result["recovery_ready"]


def test_e_recovery_ready_only_when_all_five_approved_criteria_hold(env):
    code, ready = readiness(env, drill_summarizer=provider_drill())
    assert code == 0 and ready["ready"] is True and all(c["met"] for c in ready["criteria"].values()), ready


@pytest.mark.parametrize("case, criterion", [
    ("local_test_drill", "2_provider_drill_within_interval"),
    ("no_provider_configured", "2_provider_drill_within_interval"),
    ("stale_drill", "2_provider_drill_within_interval"),
    ("slow_restore", "3_drill_duration_within_rto"),
    ("old_copy", "1_remote_copy_within_rpo"),
    ("other_alert", "4_status_ok"),
    ("key_copies_unset", "5_key_copies_verified_within_365_days"),
    ("key_copies_old", "5_key_copies_verified_within_365_days"),
    ("drill_unreadable", "2_provider_drill_within_interval"),
])
def test_e_any_unmet_criterion_means_not_recovery_ready_and_never_changes_the_exit(env, case, criterion):
    kw: dict = {"drill_summarizer": provider_drill()}
    if case == "local_test_drill":
        kw["drill_summarizer"] = provider_drill(endpoint_host="localhost")
    elif case == "no_provider_configured":
        kw["extra_env"] = {"NAPSEDGE_BACKUP_RECOVERY_DRILL_HOST": ""}
    elif case == "stale_drill":
        kw["drill_summarizer"] = provider_drill(drilled_utc=(NOW - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    elif case == "slow_restore":
        kw["drill_summarizer"] = provider_drill(duration_seconds_measured=5 * 3600.0)
    elif case == "other_alert":
        kw["version_checker"] = lambda e: {"napsedge-x": ["the payload changed after its completion record was written"]}
    elif case == "key_copies_unset":
        kw["extra_env"] = {"NAPSEDGE_BACKUP_KEY_COPIES_VERIFIED_UTC": ""}
    elif case == "key_copies_old":
        kw["extra_env"] = {"NAPSEDGE_BACKUP_KEY_COPIES_VERIFIED_UTC": "2025-09-01"}
    elif case == "drill_unreadable":
        def broken(e):
            raise off.StoreError("version list failed (ClientError AccessDenied)")
        kw["drill_summarizer"] = broken
    if case == "old_copy":
        write_cycles(env["state"], [(30, 0, True)])
        kw.setdefault("drill_lister", lambda e: NOW - timedelta(days=3))
        kw.setdefault("version_checker", lambda e: {})
        code, result = status({"NAPSEDGE_BACKUP_STATE_DIR": env["state"], **READY_ENV}, **kw)
        ready = result["recovery_ready"]
        expected_code = 10  # the existing RPO alert, unchanged
    else:
        code, ready = readiness(env, **kw)
        expected_code = 10 if case == "other_alert" else 0
    assert ready["ready"] is False and ready["criteria"][criterion]["met"] is False, ready
    assert code == expected_code, "the readiness report never changes the exit status by itself"


def test_e_a_drill_record_names_its_endpoint_and_the_newest_passing_one_is_found():
    from src.deployment.backup_retention import latest_passing_drill

    store = FakeStore()
    names = seed_remote(store, [1, 2, 3], drilled=[2, 3])
    newest = latest_passing_drill(store, "napsedge")
    assert newest is not None and newest["artifact"] in {names[2], names[3]} and newest["passed"] is True
    assert latest_passing_drill(FakeStore(), "napsedge") is None
    assert "endpoint_host" in Path("tests/postgres/restore_drill.py").read_text(encoding="utf-8")


# ----------------------------------------------------------------------------------------------------------- F. retention
def seed_remote(store, days_ago: list[int], *, drilled: list[int] = (), incomplete: list[str] = ()):
    names = {}
    for d in days_ago:
        created = NOW - timedelta(days=d)
        name = f"napsedge-napsedge-{created:%Y%m%dT%H%M%SZ}-{d:04d}"
        payload_key, record_key = off.object_keys("napsedge", name)
        store.objects[payload_key] = b"x" * (100 + d)
        store.objects[record_key] = off.canonical({"format": off.OFFSITE_RECORD_FORMAT, "artifact": name, "artifact_id": f"id-{d}",
                                                   "created_utc": created.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "manifest_sha256": "0" * 64,
                                                   "payload": {"key": payload_key, "bytes": 100 + d, "sha256": "0" * 64},
                                                   "plaintext": {"bytes": 1, "sha256": "0" * 64}, "readback_verified": True})
        names[d] = name
        if d in drilled:
            store.objects[f"napsedge/drills/{name}/{NOW:%Y%m%dT%H%M%SZ}.json"] = off.canonical(
                {"format": "napsedge.backup-drill/1", "artifact": name, "drilled_utc": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "passed": True, "restore_exit": 0,
                 "strict_all_equal": True, "strict_sequences_equal": True, "monotonic_v1_policy_accepts": True, "monotonic_v1_r2_failures": [],
                 "manifest_sha256": "1" * 64, "runbook_sha256": "2" * 64})
    for name in incomplete:
        store.objects[off.object_keys("napsedge", name)[0]] = b"partial"
    return names


OPS = OperationsConfig()


def test_f_the_selection_is_deterministic_gfs_plus_newest_and_last_recoverable():
    store = FakeStore()
    names = seed_remote(store, list(range(0, 400, 1)), drilled=[45])
    plan = build_plan(store, "napsedge", OPS, now=lambda: NOW)
    assert plan["refusals"] == [] and plan["deletable"]
    reasons = plan["keep"]
    daily = sorted(n for n, r in reasons.items() if "daily" in r)
    assert daily == sorted(names[d] for d in range(7)), "the newest of each of the last 7 days"
    assert sum("weekly" in r for r in reasons.values()) == 4 and sum("monthly" in r for r in reasons.values()) == 6
    assert reasons[names[0]].count("newest_complete") == 1 and "last_known_recoverable" in reasons[names[45]]
    assert build_plan(store, "napsedge", OPS, now=lambda: NOW)["plan_sha256"] == plan["plan_sha256"], "same inventory, same plan digest"
    assert set(plan["delete"]).isdisjoint(reasons) and len(plan["delete"]) == 400 - len(reasons)


@pytest.mark.parametrize("case", ["no_drill", "incomplete", "listing_failed", "bad_drill_record"])
def test_f_insufficient_inventory_or_recovery_status_refuses_deletion(case):
    store = FakeStore()
    names = seed_remote(store, list(range(20)), drilled=[] if case == "no_drill" else [3], incomplete=["napsedge-napsedge-x-orphan"] if case == "incomplete" else [])
    if case == "listing_failed":
        store.fail_list = True
    if case == "bad_drill_record":
        store.objects[f"napsedge/drills/{names[3]}/bad.json"] = b"{"
    plan = build_plan(store, "napsedge", OPS, now=lambda: NOW)
    assert plan["refusals"] and plan["deletable"] is False


def test_f_execution_is_disabled_by_default_and_bound_to_the_reviewed_plan():
    store = FakeStore()
    seed_remote(store, list(range(20)), drilled=[3])
    plan = build_plan(store, "napsedge", OPS, now=lambda: NOW)
    before = dict(store.objects)
    assert execute_plan(store, "napsedge", OPS, plan["plan_sha256"])["outcome"] == "refused" and store.objects == before
    enabled = OperationsConfig(retention_delete_enabled=True)
    assert execute_plan(store, "napsedge", enabled, "0" * 64)["outcome"] == "refused" and store.objects == before


def test_f_execution_deletes_record_first_and_confirms_every_deletion():
    store = FakeStore()
    names = seed_remote(store, list(range(20)), drilled=[3])
    enabled = OperationsConfig(retention_delete_enabled=True)
    plan = build_plan(store, "napsedge", enabled, now=lambda: NOW)
    result = execute_plan(store, "napsedge", enabled, plan["plan_sha256"], now=lambda: NOW)
    assert result["outcome"] == "deleted" and result["confirmed_absent"] == plan["delete"] and result["kept_missing"] == []
    first = plan["delete"][0]
    deletes = [c.split("@")[0] for c in store.calls if c.startswith("delete:") and first in c]
    assert deletes[0].endswith(off.RECORD_NAME) and deletes[1].endswith(off.PAYLOAD_NAME)
    assert not any(c.startswith("hide:") for c in store.calls), "retention never deletes by name (that only hides on B2)"
    assert result["versions_remaining"] == [] and all(not store.versions(f"napsedge/artifacts/{n}/") for n in plan["delete"])
    assert off.object_keys("napsedge", names[3])[1] in store.objects, "the last known recoverable copy survives"
    assert passing_drills(store, "napsedge"), "drill history is kept"


def test_f_a_failed_deletion_stops_and_is_reported_partial():
    store = FakeStore()
    seed_remote(store, list(range(20)), drilled=[3])
    enabled = OperationsConfig(retention_delete_enabled=True)
    plan = build_plan(store, "napsedge", enabled, now=lambda: NOW)
    store.fail_delete_on = off.PAYLOAD_NAME
    result = execute_plan(store, "napsedge", enabled, plan["plan_sha256"], now=lambda: NOW)
    assert result["outcome"] == "partial" and len(result["errors"]) == 1 and result["deleted"] == []


# ------------------------------------------------------------------------------- F2. version history (Phase 9B-8C, B2-like)
def fetch(artifact, env, store, destination, **kw):
    return off.fetch_artifact(artifact.name, store=store, prefix="napsedge", age=FakeAge(), identity_file=env["identity"], destination=str(destination), **kw)


def test_f2_retry_leftovers_written_before_the_record_are_expected_not_anomalies(artifact, env, tmp_path):
    store = FakeStore()
    store.fail_put = 1  # one interrupted attempt leaves a truncated payload version behind, then the retry succeeds
    assert upload(artifact, env, store).outcome == "stored_verified"
    history = off.version_history(store, "napsedge", artifact.name)
    assert history["anomalies"] == [] and history["payload_versions"] == 2 and history["payload_versions_before_record"] == 2
    destination = tmp_path / "fetch"
    destination.mkdir()
    fetched = fetch(artifact, env, store, destination)
    assert fetched.history["anomalies"] == [] and fetched.history["anomalies_accepted"] is False


@pytest.mark.parametrize("tamper", ["payload_overwritten", "payload_hidden", "record_hidden", "consistent_forgery", "extra_object"])
def test_f2_a_change_or_hide_after_verification_refuses_before_anything_is_downloaded(artifact, env, stored, tamper):
    store, destination = stored
    payload_key, record_key = off.object_keys("napsedge", artifact.name)
    good_payload, good_record = store.objects[payload_key], store.objects[record_key]
    if tamper == "payload_overwritten":
        store.put_bytes(payload_key, good_payload)  # even identical bytes: a version written after the record is an anomaly
    elif tamper == "payload_hidden":
        store.delete(payload_key)
    elif tamper == "record_hidden":
        store.delete(record_key)
    elif tamper == "consistent_forgery":  # a new payload AND a matching new record (the recipient is public, so this is possible)
        store.put_bytes(payload_key, good_payload)
        store.put_bytes(record_key, good_record)
    else:
        store.put_bytes(f"napsedge/artifacts/{artifact.name}/extra.bin", b"x")
    store.calls.clear()
    with pytest.raises(off.RetrievalRefused, match="version history anomaly"):
        fetch(artifact, env, store, destination)
    assert not any(c.startswith("get:") for c in store.calls), "nothing is downloaded when the history is anomalous"


def test_f2_an_explicitly_accepted_anomaly_is_reported_and_every_digest_check_still_applies(artifact, env, stored):
    store, destination = stored
    payload_key = off.object_keys("napsedge", artifact.name)[0]
    data = store.objects[payload_key]
    store.put_bytes(payload_key, data[:5] + bytes([data[5] ^ 1]) + data[6:])  # replaced after verification, same size
    with pytest.raises(off.RetrievalRefused, match="SHA-256"):
        fetch(artifact, env, store, destination, accept_version_anomalies=True)
    store.put_bytes(payload_key, data)  # replaced again, now with the original bytes: accepted explicitly, still reported
    fetched = fetch(artifact, env, store, destination, accept_version_anomalies=True)
    assert fetched.history["anomalies"] and fetched.history["anomalies_accepted"] is True


def test_f2_the_record_names_the_verified_payload_version_and_a_same_second_change_is_caught_exactly(artifact, env, stored):
    store, destination = stored
    payload_key, record_key = off.object_keys("napsedge", artifact.name)
    record = json.loads(store.objects[record_key])
    assert record["payload"]["version_id"] == store.head(payload_key).version_id
    assert off.version_history(store, "napsedge", artifact.name)["binding"] == "version_id"
    # every timestamp identical (one-second listing precision): only the version binding can see the change
    v = lambda key, vid, latest, marker=False: off.VersionInfo(key, vid, 0 if marker else 5, NOW, latest, marker)
    entries = [v(record_key, "r1", True), v(payload_key, "p2", True), v(payload_key, "p1", False)]
    exact = off.analyse_versions(entries, "napsedge", artifact.name, payload_version_id="p1")
    assert any("verified version is no longer current" in a for a in exact["anomalies"])
    assert off.analyse_versions(entries, "napsedge", artifact.name)["anomalies"] == [], "the timestamp fallback cannot see it (documented)"
    gone = off.analyse_versions([v(record_key, "r1", True), v(payload_key, "p9", True)], "napsedge", artifact.name, payload_version_id="p1")
    assert any("no longer exists" in a for a in gone["anomalies"])


def test_f2_a_payload_that_changes_during_the_read_back_is_never_verified(artifact, env):
    class Racing(FakeStore):
        racing = 1

        def get_to_file(self, key, path):
            super().get_to_file(key, path)
            if self.racing and key.endswith(off.PAYLOAD_NAME):
                type(self).racing -= 1
                self.put_bytes(key, self.objects[key])  # same bytes, NEW version: the read-back no longer describes the current one

    store = Racing()
    result = upload(artifact, env, store)
    assert result.outcome == "stored_verified" and result.attempts == 2, result
    record = json.loads(store.objects[off.object_keys("napsedge", artifact.name)[1]])
    assert record["payload"]["version_id"] == store.head(off.object_keys("napsedge", artifact.name)[0]).version_id


def test_f2_an_interrupted_upload_without_a_record_is_incomplete_not_an_anomaly():
    entries = [off.VersionInfo("napsedge/artifacts/a/payload.tar.age", "v1", 10, NOW, True, False)]
    history = off.analyse_versions(entries, "napsedge", "a")
    assert history["incomplete"] is True and history["anomalies"] == []


@pytest.mark.parametrize("case", ["hidden_artifact", "overwritten_payload", "version_listing_failed"])
def test_f2_retention_refuses_on_version_anomalies_including_hidden_artifacts(case):
    store = FakeStore()
    names = seed_remote(store, list(range(20)), drilled=[3])
    victim = names[10]
    payload_key, record_key = off.object_keys("napsedge", victim)
    if case == "hidden_artifact":  # a by-name delete: invisible to the plain listing, still stored and billed
        store.delete(record_key)
        store.delete(payload_key)
    elif case == "overwritten_payload":
        store.put_bytes(payload_key, store.objects[payload_key])
    else:
        store.fail_versions = True
    plan = build_plan(store, "napsedge", OperationsConfig(retention_delete_enabled=True), now=lambda: NOW)
    assert plan["deletable"] is False and plan["refusals"]
    if case != "version_listing_failed":
        assert victim in plan["version_anomalies"] and any("version-history" in r for r in plan["refusals"])
        if case == "hidden_artifact":
            assert "hidden: no current object" in plan["version_anomalies"][victim]
    else:
        assert any("version listing failed" in r for r in plan["refusals"])


def test_f2_retention_execution_removes_every_version_including_retry_leftovers():
    store = FakeStore()
    names = seed_remote(store, list(range(20)), drilled=[3])
    # rewrite one old artifact THROUGH THE API as a retried upload: a partial payload, the good payload, then the record
    victim = names[15]
    payload_key, record_key = off.object_keys("napsedge", victim)
    good_payload, good_record = store.objects.pop(payload_key), store.objects.pop(record_key)
    store.put_bytes(payload_key, good_payload[:3])
    store.put_bytes(payload_key, good_payload)
    store.put_bytes(record_key, good_record)
    assert off.version_history(store, "napsedge", victim)["anomalies"] == []
    enabled = OperationsConfig(retention_delete_enabled=True)
    plan = build_plan(store, "napsedge", enabled, now=lambda: NOW)
    assert victim in plan["delete"] and plan["deletable"]
    result = execute_plan(store, "napsedge", enabled, plan["plan_sha256"], now=lambda: NOW)
    assert result["outcome"] == "deleted" and result["versions_remaining"] == []
    assert result["versions_deleted"] == 2 * len(plan["delete"]) + 1, "the leftover partial version is deleted too"
    assert store.versions(f"napsedge/artifacts/{victim}/") == [] and payload_key not in store.objects
    assert not any(c.startswith("hide:") for c in store.calls), "never a by-name delete (that only hides on B2)"
    assert store.versions(f"napsedge/artifacts/{names[3]}/"), "the kept copies keep their versions"


def test_f2_a_drill_on_an_accepted_anomaly_never_counts_as_recoverable():
    store = FakeStore()
    names = seed_remote(store, [1, 2], drilled=[2])
    key = next(k for k in store.objects if k.startswith(f"napsedge/drills/{names[2]}/"))
    record = json.loads(store.objects[key])
    record["version_history"] = {"anomalies": ["x"], "anomalies_accepted": True}
    store.objects[key] = off.canonical(record)
    assert passing_drills(store, "napsedge") == {}


@pytest.mark.parametrize("tamper", ["overwritten", "hidden"])
def test_f2_an_overwritten_or_hidden_drill_record_never_counts_and_retention_status_flag_it(tamper):
    store = FakeStore()
    names = seed_remote(store, list(range(20)), drilled=[3])
    key = next(k for k in store.objects if k.startswith(f"napsedge/drills/{names[3]}/"))
    assert passing_drills(store, "napsedge"), "the untouched record counts"
    if tamper == "overwritten":
        store.put_bytes(key, store.objects[key])  # identical bytes, but a second version: no longer written-once
    else:
        store.delete(key)
    assert passing_drills(store, "napsedge") == {}
    plan = build_plan(store, "napsedge", OperationsConfig(retention_delete_enabled=True), now=lambda: NOW)
    assert plan["deletable"] is False and key[len("napsedge/"):] in plan["version_anomalies"]


@pytest.mark.parametrize("missing", ["manifest_sha256", "runbook_sha256"])
def test_f2_a_drill_record_without_its_claim_4_identifiers_never_counts(missing):
    store = FakeStore()
    names = seed_remote(store, [1, 2], drilled=[2])
    key = next(k for k in store.objects if k.startswith(f"napsedge/drills/{names[2]}/"))
    record = json.loads(store.objects[key])
    record.pop(missing)
    store.objects[key] = off.canonical(record)
    assert passing_drills(store, "napsedge") == {}


def test_f2_the_container_entrypoint_passes_retention_execute_arguments_verbatim(monkeypatch):
    """Found by the real-B2 deletion test: argparse.REMAINDER dropped `--execute --plan-sha256 <digest>` (exit 2, nothing ran)."""
    from src.deployment import entrypoints

    seen = []
    monkeypatch.setattr(entrypoints, "run_backup_retention", lambda argv: seen.append(argv))
    entrypoints.main(["backup-retention", "--execute", "--plan-sha256", "a" * 64])
    entrypoints.main(["backup-retention"])
    assert seen == [["--execute", "--plan-sha256", "a" * 64], []]
    with pytest.raises(SystemExit) as exc:
        run_retention(["--bogus"], environ={}, out=io.StringIO())
    assert exc.value.code == 2, "the retention CLI's own parser still rejects unknown options"


# ------------------------------------------------------------------------------------------------- G. deployment files
def unit(name) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string(Path("deploy/systemd", name).read_text(encoding="utf-8"))
    return parser


def test_g_the_timer_is_daily_utc_persistent_and_the_service_bounded_and_overlap_aware():
    timer, service = unit("napsedge-backup.timer"), unit("napsedge-backup.service")
    assert timer["Timer"]["OnCalendar"] == "*-*-* 03:30:00 UTC" and timer["Timer"]["Persistent"] == "true"
    s = service["Service"]
    assert s["Type"] == "oneshot" and s["SuccessExitStatus"] == "3" and s["TimeoutStartSec"] == "3h"
    assert service["Unit"]["OnFailure"].startswith("napsedge-backup-notify@")
    exec_start = s["ExecStart"]
    for flag in ("--read-only", "--user 10001:10001", "--cap-drop ALL", "no-new-privileges", "backup-cycle"):
        assert flag in exec_start, flag
    text = Path("deploy/systemd/napsedge-backup.service").read_text(encoding="utf-8")
    assert "LoadCredentialEncrypted=db-password" in text and not re.search(r"(?im)^Environment=.*(PASSWORD|SECRET)", text)
    assert "/var/run/docker.sock" not in exec_start


def test_g_the_notification_hook_is_inactive_and_secrets_stay_out_of_the_examples():
    assert unit("napsedge-backup-notify@.service")["Service"]["ExecStart"].startswith("/usr/bin/logger ")
    example = Path("deploy/backup/backup.env.example").read_text(encoding="utf-8")
    assert "NAPSEDGE_BACKUP_OFFSITE_ENABLED=false" in example and "NAPSEDGE_BACKUP_RETENTION_DELETE_ENABLED=false" in example
    assert not re.search(r"(?im)^[A-Z_]*(PASSWORD|SECRET|KEY)=(?!/)", example), "credential settings name files only"


def test_g_only_the_backup_image_receives_age_and_the_pinned_boto3_set():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    backup = dockerfile[dockerfile.index("FROM base AS backup"):]
    assert "age=1.2.1-1+b5" in backup and "-c /app/constraints-release-py313.txt -r /opt/napsedge-backup/requirements-backup.txt" in backup
    executable = [l for l in dockerfile[: dockerfile.index("FROM base AS backup")].splitlines() if not l.lstrip().startswith("#")]
    before_backup = " ".join(executable)
    assert "requirements-backup" not in before_backup and "age=" not in before_backup, "no other target installs them"
    pins = [l for l in Path("requirements-backup.txt").read_text(encoding="utf-8").splitlines() if l and not l.startswith("#")]
    assert pins == ["boto3==1.43.110", "botocore==1.43.110", "s3transfer==0.19.2", "jmespath==1.1.0"]


# ---------------------------------------------------------------------------------------------- H. the connection budget
def test_h_each_database_process_is_one_process_with_one_capped_engine():
    """Supports the 1 GiB budget (22): API + Streamlit + Discord, one process and one engine each, at most 4 connections each."""
    import ast

    entry = ast.parse(Path("src/deployment/entrypoints.py").read_text(encoding="utf-8"))
    uvicorn_calls = [n for n in ast.walk(entry) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "run"
                     and getattr(n.func.value, "id", "") == "uvicorn"]
    assert len(uvicorn_calls) == 1 and not any(k.arg == "workers" for k in uvicorn_calls[0].keywords), "one uvicorn worker"
    for module in ("src/discord_bot/consumer_bridge.py", "src/streamlit_consumer_controller.py"):
        text = Path(module).read_text(encoding="utf-8")
        assert text.count("build_engine()") == 1 and "global _default_session_factory" in text, f"{module}: one memoized engine per process"
    assert Path("src/api/main.py").read_text(encoding="utf-8").count("build_engine(") == 1, "one engine per API application"
    import yaml

    with open("compose.yaml", encoding="utf-8") as handle:
        services = yaml.safe_load(handle)["services"]
    for name in ("api", "streamlit", "discord"):
        assert "deploy" not in services[name] and "scale" not in services[name], f"{name} is not replicated"
    from src.db.config import DatabasePoolSettings

    per_process, measured_job_max, budgeted_job, operator = DatabasePoolSettings().max_connections, 3, 4, 1
    assert 3 * per_process + budgeted_job + operator == 17 and measured_job_max <= budgeted_job <= 22
