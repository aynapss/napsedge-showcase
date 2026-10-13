"""
Phase 9B-8B: the complete local workflow on the accepted Linux runner -- a REAL capture (the accepted job and coordinator against a
disposable PostgreSQL 16 cluster, production `LocalClientHost`), REAL `age` encryption (Debian `age`), a REAL S3 protocol path (boto3
over HTTP) against an EMULATOR (moto's S3 server: a stand-in for Backblaze B2, NOT B2), and the restore drill from the DOWNLOADED copy
only (the local artifact is deleted first) into a guarded, migrator-owned target.

Labelled substitutes: the object store is moto (behaviour of B2 itself -- consistency, multipart ETags, Object Lock, quotas -- is NOT
established here); HTTP instead of HTTPS to the emulator (`NAPSEDGE_BACKUP_OFFSITE_ALLOW_HTTP_FOR_TESTS=1`, a test-only switch);
synthetic credentials and a throwaway age key pair generated per test.

Requires `NAPSEDGE_TEST_CLIENT=local`, `age`/`age-keygen`, boto3 and moto (the 9B-8B runner).
"""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest

from src.deployment import backup_offsite as off
from src.deployment.backup_capture_lock import backup_capture_lock
from src.deployment.backup_cycle import backup_status, run_cycle
from src.deployment.backup_operations import OperationsConfig
from src.deployment.backup_retention import build_plan
from tests.postgres import restore_drill
from tests.postgres.capture_client_support import native_mode
from tests.postgres.provider_probe import PASS, REFUSED
from tests.postgres.test_postgres_backup_capture_lock import designation_for, harness, record  # noqa: F401  (harness: module fixture)
from tests.postgres.test_postgres_backup_capture_roundtrip import case  # noqa: F401  (case: fixture)
from tests.postgres.test_postgres_backup_job import job_env, leftovers

boto3 = pytest.importorskip("boto3")
moto_server = pytest.importorskip("moto.server")
pytestmark = [pytest.mark.postgres,
              pytest.mark.skipif(not native_mode() or not shutil.which("age"), reason="needs the 9B-8B Linux runner: NAPSEDGE_TEST_CLIENT=local and age")]

PREFIX = "napsedge"


@pytest.fixture()
def s3(tmp_path):
    """moto's S3 server (an EMULATOR), on a free local port, with synthetic credentials in 0600 files."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = moto_server.ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    creds = tmp_path / "creds"
    creds.mkdir(mode=0o700)
    for name, value in (("key-id", "testing-key-id"), ("secret", f"testing-secret-{uuid.uuid4().hex}")):
        path = creds / name
        path.write_text(value + "\n")
        path.chmod(0o400)
    endpoint = f"http://127.0.0.1:{port}"
    bucket = f"napsedge-test-{uuid.uuid4().hex[:12]}"  # moto keeps ONE in-memory backend per process: a bucket per test isolates them
    admin = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1", aws_access_key_id="testing-key-id", aws_secret_access_key="x")
    admin.create_bucket(Bucket=bucket)
    admin.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})  # like B2: every version is kept
    try:
        yield {"endpoint": endpoint, "bucket": bucket, "key_id_file": str(creds / "key-id"), "secret_key_file": str(creds / "secret"),
               "secret": (creds / "secret").read_text().strip()}
    finally:
        server.stop()


@pytest.fixture()
def keys(tmp_path):
    """A throwaway age key pair: the identity (private, 0600) and the recipients file (public)."""
    identity = tmp_path / "identity.txt"
    subprocess.run(["age-keygen", "-o", str(identity)], check=True, capture_output=True)
    recipients = tmp_path / "recipients.txt"
    recipients.write_text(subprocess.run(["age-keygen", "-y", str(identity)], check=True, capture_output=True, text=True).stdout)
    other = tmp_path / "other_identity.txt"
    subprocess.run(["age-keygen", "-o", str(other)], check=True, capture_output=True)
    return {"identity": str(identity), "recipients": str(recipients), "other_identity": str(other)}


def store_of(s3) -> off.S3ObjectStore:
    return off.S3ObjectStore(endpoint_url=s3["endpoint"], region="us-east-1", bucket=s3["bucket"], key_id_file=s3["key_id_file"], secret_key_file=s3["secret_key_file"])


def cycle_env(case, root: Path, s3, keys, **overrides) -> dict:
    env = job_env(case, root)
    env.pop("NAPSEDGE_BACKUP_ARTIFACT_ID")  # each attempt gets a fresh id, as in production
    env.update({"NAPSEDGE_BACKUP_STATE_DIR": str(root / "state"), "NAPSEDGE_BACKUP_OFFSITE_ENABLED": "true",
                "NAPSEDGE_BACKUP_OFFSITE_ENDPOINT_URL": s3["endpoint"], "NAPSEDGE_BACKUP_OFFSITE_ALLOW_HTTP_FOR_TESTS": "1",
                "NAPSEDGE_BACKUP_OFFSITE_REGION": "us-east-1", "NAPSEDGE_BACKUP_OFFSITE_BUCKET": s3["bucket"], "NAPSEDGE_BACKUP_OFFSITE_PREFIX": PREFIX,
                "NAPSEDGE_BACKUP_OFFSITE_KEY_ID_FILE": s3["key_id_file"], "NAPSEDGE_BACKUP_OFFSITE_SECRET_KEY_FILE": s3["secret_key_file"],
                "NAPSEDGE_BACKUP_RECIPIENTS_FILE": keys["recipients"], "NAPSEDGE_BACKUP_AGE": shutil.which("age"),
                "NAPSEDGE_BACKUP_UPLOAD_BACKOFF_SECONDS": "0", "TMPDIR": str(root / "tmp")})
    env.update(overrides)
    return env


@pytest.fixture()
def root(case):
    base = case.host_dir / "ops"
    for sub in ("store", "tmp", "state"):
        (base / sub).mkdir(parents=True)
    return base


def cycle(env) -> tuple[int, dict]:
    out = io.StringIO()
    os.environ["NAPSEDGE_BACKUP_OFFSITE_ALLOW_HTTP_FOR_TESTS"] = "1"
    code = run_cycle(env, out=out)
    return code, json.loads(out.getvalue().strip().splitlines()[-1])


def drill_config(case, tmp_path: Path, s3, identity: str, target: str, *, artifact="latest", accept_version_anomalies=None) -> str:
    cluster = case.cluster
    files = tmp_path / f"drill_creds_{uuid.uuid4().hex[:6]}"
    files.mkdir(mode=0o700)
    for name, value in (("admin", cluster.admin_password), ("migrator", cluster.migrator_password), ("runtime", cluster.runtime_password),
                        ("backup", cluster.backup_password)):
        (files / name).write_text(value + "\n")
        (files / name).chmod(0o400)
    evidence = tmp_path / f"drill_evidence_{uuid.uuid4().hex[:6]}"
    evidence.mkdir()
    url = cluster.admin_url
    config = {"endpoint": {"host": url.host, "hostaddr": url.query["hostaddr"], "port": url.port, "admin_database": url.database,
                           "ca_file": url.query["sslrootcert"]},
              "admin_user": url.username, "credential_files": {k: str(files / k) for k in ("admin", "migrator", "runtime", "backup")},
              "target_database": target, "evidence_dir": str(evidence),
              "offsite": {"endpoint_url": s3["endpoint"], "region": "us-east-1", "bucket": s3["bucket"], "prefix": PREFIX, "key_id_file": s3["key_id_file"],
                          "secret_key_file": s3["secret_key_file"], "allow_http_for_tests": True},
              "identity_file": identity, "artifact": artifact, "publish_record": True, "rto_target_hours": 4}
    if accept_version_anomalies is not None:
        config["accept_version_anomalies"] = accept_version_anomalies
    path = tmp_path / f"drill_{uuid.uuid4().hex[:6]}.json"
    path.write_text(json.dumps(config))
    return str(path)


def drill(config_path: str, step: str = "run", *extra) -> tuple[int, dict]:
    code = restore_drill.main(["--config", config_path, step, *extra])
    evidence = Path(json.loads(Path(config_path).read_text())["evidence_dir"]) / f"drill_{step}.json"
    return code, json.loads(evidence.read_text())["facts"]


def target_name() -> str:
    return f"napsedge_pg_test_drill_{uuid.uuid4().hex[:10]}"


def test_a_cycle_to_remote_store_then_a_drill_restores_from_the_download_alone(case, root, s3, keys, tmp_path, record_property):
    env = cycle_env(case, root, s3, keys)
    code, rec = cycle(env)
    assert code == 0 and rec["capture"]["report"]["outcome"] == "published" and rec["offsite"]["outcome"] == "stored_verified", rec
    assert rec["states"] == {"locally_published": True, "remotely_stored": True, "restore_demonstrated": "not established by a cycle (see the drill)"}
    name = rec["capture"]["report"]["artifact"]
    store = store_of(s3)
    payload_key, record_key = off.object_keys(PREFIX, name)
    remote = off.parse_record(store.get_bytes(record_key, off.RECORD_LIMIT), name)
    assert store.head(payload_key).size == remote["payload"]["bytes"] and remote["encryption"]["tool"] == "age"
    leftovers(case, root)
    shutil.rmtree(root / "store" / name)  # the original host's copy is GONE: recovery must use the off-site copy only
    target = target_name()
    config = drill_config(case, tmp_path, s3, keys["identity"], target)
    code, facts = drill(config)
    assert code == PASS, facts
    result = facts["drill_record"]
    assert result["passed"] and result["restore_exit"] == 0 and result["strict_all_equal"] and result["strict_sequences_equal"]
    assert result["monotonic_v1_policy_accepts"] and result["monotonic_v1_r2_failures"] == [] and result["duration_seconds_measured"] > 0
    assert facts["retrieved_from"].startswith("the off-site store") and facts["drill_record_published"]["read_back_equal"] is True
    plan = build_plan(store, PREFIX, OperationsConfig())
    assert plan["refusals"] == [] and plan["delete"] == [] and "last_known_recoverable" in plan["keep"][name]
    out = io.StringIO()
    status_code = backup_status(env, out=out)
    status = json.loads(out.getvalue())
    assert status_code == 0 and status["status"] == "ok", status
    assert drill(config, "cleanup", "--confirm", "cleanup")[0] == PASS
    record(record_property, "A_cycle_drill", cycle=rec, drill=result, plan=plan, status=status)
    assert s3["secret"] not in json.dumps([rec, facts, plan, status]) and case.cluster.backup_password not in json.dumps([rec, facts])


def test_b_a_wrong_key_and_a_corrupted_payload_are_refused_by_the_drill(case, root, s3, keys, tmp_path):
    code, rec = cycle(cycle_env(case, root, s3, keys))
    assert code == 0, rec
    name = rec["capture"]["report"]["artifact"]
    code, facts = drill(drill_config(case, tmp_path, s3, keys["other_identity"], target_name()))
    assert code == REFUSED and "decryption" in facts["refused"], facts
    code, facts = drill(drill_config(case, tmp_path, s3, str(tmp_path / "no_such_identity.txt"), target_name()))
    assert code == REFUSED, facts
    payload_key = off.object_keys(PREFIX, name)[0]
    client = store_of(s3)._client
    body = client.get_object(Bucket=s3["bucket"], Key=payload_key)["Body"].read()
    client.put_object(Bucket=s3["bucket"], Key=payload_key, Body=bytes([body[0] ^ 1]) + body[1:])  # same size, different bytes
    code, facts = drill(drill_config(case, tmp_path, s3, keys["identity"], target_name()))
    assert code == REFUSED and "version history anomaly" in facts["refused"] and "after its completion record" in facts["refused"], facts
    # explicitly accepting the reviewed anomaly does not bypass the digest checks: the replaced bytes are still refused
    code, facts = drill(drill_config(case, tmp_path, s3, keys["identity"], target_name(), accept_version_anomalies=True))
    assert code == REFUSED and "SHA-256" in facts["refused"], facts


def test_c_an_unsafe_restore_target_is_refused_and_left_untouched(case, root, s3, keys, tmp_path):
    code, rec = cycle(cycle_env(case, root, s3, keys))
    assert code == 0, rec
    code, facts = drill(drill_config(case, tmp_path, s3, keys["identity"], case.database))  # the SOURCE database itself
    assert code == REFUSED and "already exists" in facts["refused"], facts
    from sqlalchemy import text

    with case.admin.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM teams")).scalar() > 0, "the source is untouched"


class FaultyStore(off.S3ObjectStore):
    """The REAL boto3 client, with an injected interruption: the first `fail` uploads leave a truncated object, then fail."""

    fail = 0

    def put_file(self, key, path):
        if self.fail and key.endswith(off.PAYLOAD_NAME):
            type(self).fail -= 1
            self.put_bytes(key, Path(path).read_bytes()[:64])
            raise off.StoreError("upload failed (injected interruption)")
        super().put_file(key, path)


@pytest.mark.parametrize("failures, expected", [(1, "stored_verified"), (5, "failed")])
def test_d_an_interrupted_real_upload_is_retried_or_fails_without_a_completion_record(case, root, s3, keys, failures, expected):
    code, rec = cycle(cycle_env(case, root, s3, keys, NAPSEDGE_BACKUP_OFFSITE_ENABLED="false"))
    assert code == 0 and rec["offsite"]["outcome"] == "disabled", rec
    name = rec["capture"]["report"]["artifact"]
    FaultyStore.fail = failures
    store = FaultyStore(endpoint_url=s3["endpoint"], region="us-east-1", bucket=s3["bucket"], key_id_file=s3["key_id_file"], secret_key_file=s3["secret_key_file"])
    result = off.upload_artifact(root / "store" / name, store=store, prefix=PREFIX, age=off.AgeTool(shutil.which("age")), recipients_file=keys["recipients"],
                                 workdir=str(root / "tmp"), state_dir=str(root / "state"), attempts=3, backoff_seconds=0)
    assert result.outcome == expected, result
    record_present = store.head(off.object_keys(PREFIX, name)[1]) is not None
    assert record_present is (expected == "stored_verified")
    assert result.attempts == (2 if expected == "stored_verified" else 3) and os.listdir(root / "tmp") == []


def test_e_overlap_skips_the_cycle_after_bounded_retries_and_uploads_nothing(case, root, s3, keys):
    held, release = threading.Event(), threading.Event()

    def holder():
        with backup_capture_lock(case.cluster.backup_url(case.database), designation_for(case.database)):
            held.set()
            release.wait(60)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert held.wait(30)
        code, rec = cycle(cycle_env(case, root, s3, keys, NAPSEDGE_BACKUP_OVERLAP_ATTEMPTS="2", NAPSEDGE_BACKUP_OVERLAP_DELAY_SECONDS="1"))
    finally:
        release.set()
        thread.join(30)
    assert code == 3 and [a["exit_code"] for a in rec["capture"]["attempts"]] == [3, 3] and rec["offsite"]["outcome"] == "not_attempted"
    assert store_of(s3).list(f"{PREFIX}/") == [] and os.listdir(root / "store") == []


def test_f_the_backup_job_s_own_connection_use_is_measured(case, root, s3, keys, record_property):
    """Supports the 17-of-22 budget: the job's maximum concurrent backends as the server sees them, and an overlapping attempt's."""
    from sqlalchemy import text

    stop, samples = threading.Event(), []

    def sample():
        while not stop.is_set():
            with case.admin.engine.connect() as conn:
                samples.append(conn.execute(text("SELECT count(*) FROM pg_stat_activity WHERE usename = 'napsedge_backup_ro'")).scalar())
            time.sleep(0.01)

    thread = threading.Thread(target=sample)
    thread.start()
    try:
        code, rec = cycle(cycle_env(case, root, s3, keys, NAPSEDGE_BACKUP_OFFSITE_ENABLED="false"))
    finally:
        stop.set()
        thread.join(10)
    assert code == 0, rec
    job_max = max(samples)
    # an overlapping attempt: another holder has the lock (1 session) while a second job tries and is refused
    held, release = threading.Event(), threading.Event()

    def holder():
        with backup_capture_lock(case.cluster.backup_url(case.database), designation_for(case.database)):
            held.set()
            release.wait(60)

    holder_thread = threading.Thread(target=holder)
    holder_thread.start()
    stop.clear()
    samples_job = list(samples)
    samples.clear()
    thread = threading.Thread(target=sample)
    try:
        assert held.wait(30)
        thread.start()
        overlap_code, overlap_rec = cycle(cycle_env(case, root, s3, keys, NAPSEDGE_BACKUP_OFFSITE_ENABLED="false", NAPSEDGE_BACKUP_OVERLAP_ATTEMPTS="1"))
    finally:
        stop.set()
        thread.join(10)
        release.set()
        holder_thread.join(30)
    assert overlap_code == 3, overlap_rec
    overlap_max = max(samples)
    assert overlap_max <= 4, f"a refused overlapping attempt must stay within the job's 4-connection allowance; observed {overlap_max}"
    print(f"\nholder + refused overlapping attempt: maximum concurrent backends observed = {overlap_max}")
    samples[:] = samples_job
    record(record_property, "F_connection_budget", job_max_backends=job_max, overlap_max_backends_including_holder=overlap_max, samples=len(samples))
    print(f"\nbackup job: maximum concurrent backends observed = {job_max} over {len(samples)} samples")
    assert 1 <= job_max <= 4, f"the budget assumes at most 4 backup-job connections; observed {job_max}"
