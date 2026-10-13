"""
Phase 9B-8C: the FIRST REAL Backblaze B2 validation and restore drill (opt-in; never part of a normal run).

Everything runs through the DELIVERED artifacts only: the built backup image (`backup-cycle`, `backup-offsite`,
`backup-retention`, `backup-status`, and its real S3 adapter) and the probe-tooling image (`tests.postgres.restore_drill`).
The PostgreSQL source and restore target are a disposable local PostgreSQL 16 cluster with synthetic data; B2 is REAL.

Inputs (environment; none secret):
  NAPSEDGE_B2_LIVE=1                     explicit opt-in
  NAPSEDGE_BACKUP_IMAGE / NAPSEDGE_PROBE_TOOLING_IMAGE    the retained 9B-8B images
  NAPSEDGE_B2_ENDPOINT, NAPSEDGE_B2_REGION, NAPSEDGE_B2_BUCKET, NAPSEDGE_B2_PREFIX   the validation bucket and prefix
  NAPSEDGE_B2_CRED_VOLUME   a Docker volume holding rw-key-id, rw-secret, ro-key-id, ro-secret (0400, uid 10001), written by the
                            key-creation command in docs/backup_b2_validation.md -- the keys never touch a host file
  NAPSEDGE_B2_RECIPIENT     the PUBLIC age recipient (age1...)
  NAPSEDGE_B2_IDENTITY_DIR  the host directory of offline key copy A. The file is read by this test and STREAMED on stdin into a
                            0700 tmpfs (memory) inside the drill container ONLY; it is never bind-mounted, copied to a disk or a volume,
                            or printed (Docker Desktop cannot bind-mount a USB stick inserted after it started)
  NAPSEDGE_B2_EVIDENCE_DIR  a host directory for the sanitized evidence JSON

Order: capture + encrypted upload (rw key) -> repeat upload (already stored) -> OBSERVED permission behaviour on sentinel objects (ro key cannot write;
rw key: overwrite, hide-by-name, version-specific delete, retrieval of the hidden version, listing outside the prefix) -> a second capture uploaded through an injected interruption (retry on the real service)
-> missing-object refusal -> integrity failure (the second copy's payload overwritten, then refused) -> the local copy of the first
artifact deleted -> the drill restores the DOWNLOADED first copy into a new guarded database with copy A of the key -> retention dry
run and status with the READ-ONLY key. Remote objects are retained (no delete key exists); they are listed in the evidence.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import pytest

from tests.postgres.test_postgres_backup_capture_lock import harness  # noqa: F401  (module fixture)
from tests.postgres.test_postgres_backup_capture_roundtrip import case  # noqa: F401  (fixture)
from tests.postgres.test_postgres_backup_image import CA_DIR, CRED_DIR, IMAGE, SCRATCH_DIR, STORE_DIR, cluster_ip, docker, helper, job_env, put_password, volumes  # noqa: F401

ENV = {k: os.environ.get(k, "") for k in ("NAPSEDGE_B2_LIVE", "NAPSEDGE_PROBE_TOOLING_IMAGE", "NAPSEDGE_B2_ENDPOINT", "NAPSEDGE_B2_REGION", "NAPSEDGE_B2_BUCKET",
                                          "NAPSEDGE_B2_PREFIX", "NAPSEDGE_B2_CRED_VOLUME", "NAPSEDGE_B2_RECIPIENT", "NAPSEDGE_B2_IDENTITY_DIR", "NAPSEDGE_B2_EVIDENCE_DIR")}
pytestmark = [pytest.mark.postgres, pytest.mark.skipif(ENV["NAPSEDGE_B2_LIVE"] != "1" or not IMAGE or not all(ENV.values()),
                                                       reason="opt-in real-B2 validation: set NAPSEDGE_B2_LIVE=1 and every NAPSEDGE_B2_* input")]
LABEL = "org.napsedge.unit=9b8c"
B2_DIR, STATE_DIR, RECIPIENTS_DIR = "/run/napsedge-backup/b2", "/var/lib/napsedge-backup/state", "/run/napsedge-backup/recipients"
REPO = Path.cwd()


def offsite_env(which: str, prefix: str | None = None) -> dict:
    return {"NAPSEDGE_BACKUP_OFFSITE_ENDPOINT_URL": ENV["NAPSEDGE_B2_ENDPOINT"], "NAPSEDGE_BACKUP_OFFSITE_REGION": ENV["NAPSEDGE_B2_REGION"],
            "NAPSEDGE_BACKUP_OFFSITE_BUCKET": ENV["NAPSEDGE_B2_BUCKET"], "NAPSEDGE_BACKUP_OFFSITE_PREFIX": prefix or ENV["NAPSEDGE_B2_PREFIX"],
            "NAPSEDGE_BACKUP_OFFSITE_KEY_ID_FILE": f"{B2_DIR}/{which}-key-id", "NAPSEDGE_BACKUP_OFFSITE_SECRET_KEY_FILE": f"{B2_DIR}/{which}-secret",
            "NAPSEDGE_BACKUP_RECIPIENTS_FILE": f"{RECIPIENTS_DIR}/recipients.txt", "NAPSEDGE_BACKUP_STATE_DIR": STATE_DIR}


def secrets_in(volume: str) -> list[str]:
    """The key secrets, read only to prove they appear in NO output (never printed or stored)."""
    out = helper(["-v", f"{volume}:/v:ro"], "cat /v/rw-secret /v/ro-secret /v/rw-key-id /v/ro-key-id").stdout.decode().split()
    assert len(out) == 4, "the credential volume must hold rw-key-id, rw-secret, ro-key-id, ro-secret"
    return out


@pytest.fixture()
def live(case, volumes):
    suffix = uuid.uuid4().hex[:8]
    extra = {kind: f"napsedge-9b8c-{kind}-{suffix}" for kind in ("state", "recipients", "drillcred", "drillevidence")}
    for name in extra.values():
        assert docker("volume", "create", "--label", LABEL, name).returncode == 0
    try:
        assert helper(["-v", f"{extra['recipients']}:/r"], "umask 022 && cat > /r/recipients.txt", stdin=(ENV["NAPSEDGE_B2_RECIPIENT"] + "\n").encode()).returncode == 0
        assert helper(["-v", f"{extra['state']}:/s", "-v", f"{extra['drillevidence']}:/e"], "chown 10001:10001 /s /e && chmod 0700 /s /e").returncode == 0
        cluster = case.cluster
        for name, value in (("admin", cluster.admin_password), ("migrator", cluster.migrator_password), ("runtime", cluster.runtime_password), ("backup", cluster.backup_password)):
            assert helper(["-v", f"{extra['drillcred']}:/v"], f"umask 077 && cat > /v/{name} && chown 10001:10001 /v/{name} && chmod 0400 /v/{name}",
                          stdin=(value + "\n").encode()).returncode == 0
        put_password(volumes, cluster.backup_url(case.database).password.encode() + b"\n")
        yield {**volumes, **extra}
    finally:
        for name in extra.values():
            docker("volume", "rm", "-f", name)


def run_backup(v: dict, env: dict, *command: str, key: str = "rw", timeout: int = 1800, prefix: str | None = None, cred_volume: str | None = None):
    argv = ["run", "--rm", "--read-only", "--user", "10001:10001", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--network", "bridge",
            "--label", LABEL, "-v", f"{v['cred']}:{CRED_DIR}:ro", "-v", f"{v['ca']}:{CA_DIR}:ro", "-v", f"{v['recipients']}:{RECIPIENTS_DIR}:ro",
            "-v", f"{cred_volume or ENV['NAPSEDGE_B2_CRED_VOLUME']}:{B2_DIR}:ro", "-v", f"{v['scratch']}:{SCRATCH_DIR}", "-v", f"{v['store']}:{STORE_DIR}",
            "-v", f"{v['state']}:{STATE_DIR}"]
    for k, val in {**env, **offsite_env(key, prefix)}.items():
        argv += ["-e", f"{k}={val}"]
    argv += ["--entrypoint", "python", IMAGE, "-m", "src.deployment.entrypoints", *command]
    started = time.monotonic()
    completed = docker(*argv, timeout=timeout)
    lines = completed.stdout.decode().strip().splitlines()
    return completed.returncode, (json.loads(lines[-1]) if lines else {}), completed, round(time.monotonic() - started, 2)


def adapter_script(v: dict, script: str, key: str, prefix: str | None = None):
    """A short script in the BUILT image using the delivered adapter (`backup_offsite`) against real B2."""
    return docker("run", "--rm", "--read-only", "--tmpfs", "/tmp:rw,mode=1777", "--user", "10001:10001", "--network", "bridge", "--label", LABEL,
                  "-v", f"{ENV['NAPSEDGE_B2_CRED_VOLUME']}:{B2_DIR}:ro", "-v", f"{v['store']}:{STORE_DIR}:ro", "-v", f"{v['recipients']}:{RECIPIENTS_DIR}:ro",
                  "-v", f"{v['state']}:{STATE_DIR}", "-e", f"KEY={key}", "-e", f"PREFIX={prefix or ENV['NAPSEDGE_B2_PREFIX']}",
                  "--entrypoint", "python", IMAGE, "-c",
                  "import os,json\nfrom src.deployment import backup_offsite as off\n"
                  f"store=off.S3ObjectStore(endpoint_url={ENV['NAPSEDGE_B2_ENDPOINT']!r},region={ENV['NAPSEDGE_B2_REGION']!r},bucket={ENV['NAPSEDGE_B2_BUCKET']!r},"
                  f"key_id_file='{B2_DIR}/'+os.environ['KEY']+'-key-id',secret_key_file='{B2_DIR}/'+os.environ['KEY']+'-secret')\nP=os.environ['PREFIX']\n" + script,
                  timeout=900)


def cycle_log_offset(v: dict) -> str:
    """What the deadman drop-in's ExecStartPre records: the cycle log's byte size BEFORE a run (same command, same image)."""
    completed = docker("run", "--rm", "--read-only", "--network", "none", "--user", "10001:10001", "--label", LABEL, "-v", f"{v['state']}:/s:ro",
                       "--entrypoint", "sh", IMAGE, "-c", "if [ -e /s/cycles.jsonl ]; then wc -c < /s/cycles.jsonl; else echo 0; fi")
    assert completed.returncode == 0, completed.stderr[-400:]
    return completed.stdout.decode().strip()


def heartbeat_gate(v: dict, offset: str):
    """The dead-man's-switch gate (deploy/backup/verified-offsite-gate.py) on the REAL record appended by one run, read-only, no network."""
    gate = REPO / "deploy" / "backup" / "verified-offsite-gate.py"
    return docker("run", "--rm", "--read-only", "--network", "none", "--user", "10001:10001", "--label", LABEL, "-v", f"{v['state']}:/s:ro",
                  "--mount", f"type=bind,source={gate},target=/g.py,readonly", "--entrypoint", "python", IMAGE, "/g.py", "/s/cycles.jsonl", "--offset", offset)


def last_json(completed) -> dict:
    lines = completed.stdout.decode().strip().splitlines()
    assert completed.returncode == 0 and lines, (completed.stdout[-400:], completed.stderr.decode()[-800:])
    return json.loads(lines[-1])


def test_real_b2_round_trip_recovery_and_provider_checks(case, live):
    v, evidence = live, {"format": "napsedge.b2-validation/1", "bucket": ENV["NAPSEDGE_B2_BUCKET"], "endpoint": ENV["NAPSEDGE_B2_ENDPOINT"],
                         "prefix": ENV["NAPSEDGE_B2_PREFIX"], "image": IMAGE, "tooling_image": ENV["NAPSEDGE_PROBE_TOOLING_IMAGE"], "steps": {}}
    secrets = secrets_in(ENV["NAPSEDGE_B2_CRED_VOLUME"]) + list(case.cluster.secrets_to_scrub())
    identity_text = (Path(ENV["NAPSEDGE_B2_IDENTITY_DIR"]) / "napsedge-backup-identity.txt").read_text(encoding="ascii")
    secrets += [line.strip() for line in identity_text.splitlines() if line.startswith("AGE-SECRET-KEY-")]  # the private key must never appear
    del identity_text
    outputs: list[bytes] = []
    base = job_env(case)
    base.pop("NAPSEDGE_BACKUP_ARTIFACT_ID")
    base.update({"NAPSEDGE_BACKUP_OFFSITE_ENABLED": "true", "NAPSEDGE_BACKUP_UPLOAD_BACKOFF_SECONDS": "2", "NAPSEDGE_LOG_LEVEL": "WARNING"})
    try:
        # 1. capture + encrypted upload + read-back + completion record, through the built image and the rw key
        offset1 = cycle_log_offset(v)
        code, rec, completed, seconds = run_backup(v, base, "backup-cycle")
        outputs += [completed.stdout, completed.stderr]
        evidence["steps"]["1_cycle_record"] = {"exit": code, "offsite": rec.get("offsite"), "states": rec.get("states")}  # kept even on failure
        assert code == 0 and rec["offsite"]["outcome"] == "stored_verified", (rec.get("offsite"), completed.stderr[-600:])
        first = rec["capture"]["report"]["artifact"]
        state = json.loads(helper(["-v", f"{v['state']}:/s:ro"], f"cat /s/offsite/{rec['capture']['report']['artifact_id']}.json").stdout)
        evidence["steps"]["1_capture_and_upload"] = {"exit": code, "artifact": first, "offsite": rec["offsite"], "state": state, "wall_seconds": seconds,
                                                     "cycle_started_utc": rec["started_utc"], "cycle_finished_utc": rec["finished_utc"]}
        inventory = last_json(adapter_script(v, "inv=off.inventory(store,P)\nrec=off.parse_record(store.get_bytes(off.object_keys(P,%r)[1],off.RECORD_LIMIT),%r)\n"
                                                "print(json.dumps({'objects':inv['artifacts'],'record_payload':rec['payload'],'plaintext':rec['plaintext'],"
                                                "'encryption':rec['encryption'],'readback_verified':rec['readback_verified']}))" % (first, first), "ro"))
        evidence["steps"]["1_remote_object"] = inventory
        gate = heartbeat_gate(v, offset1)
        evidence["steps"]["1_heartbeat_gate"] = {"exit": gate.returncode, "output": gate.stdout.decode().strip()}
        assert gate.returncode == 0 and "PING" in gate.stdout.decode(), ("a verified off-site cycle must pass the heartbeat gate", gate.stdout, gate.stderr[-400:])
        assert inventory["objects"][first]["payload.tar.age"] == inventory["record_payload"]["bytes"]
        # 2. repeat upload: the same artifact is recognised as already stored (no re-upload)
        code, rep, completed, seconds = run_backup(v, base, "backup-offsite", f"{STORE_DIR}/{first}")
        outputs += [completed.stdout, completed.stderr]
        evidence["steps"]["2_repeat_upload"] = {"exit": code, "report": rep, "wall_seconds": seconds}
        assert code == 0 and rep["outcome"] == "already_stored", rep
        # 3. ACTUAL permission behaviour on the REAL service, on disposable SENTINEL objects only (never the real artifact objects).
        #    Documented: writeFiles allows overwrite (new version) and DeleteObject WITHOUT a version id (a delete marker becomes current);
        #    a version-specific delete needs deleteFiles, which the upload key does not have. Everything is RECORDED; the asserts cover only
        #    what the design relies on: the upload key cannot PERMANENTLY delete a version, and a hidden version stays retrievable.
        probe = ("from botocore.exceptions import ClientError\nres={}\nc=store._client\nB=store.bucket\n"
                 "def attempt(label, fn):\n"
                 "    try:\n        out=fn(); res[label]='ALLOWED' if out is None else ('ALLOWED: '+str(out))\n"
                 "    except (off.StoreError, ClientError) as exc:\n"
                 "        code=exc.response.get('Error',{}).get('Code') if isinstance(exc, ClientError) else str(exc)\n"
                 "        res[label]='refused: '+str(code)\n")
        sentinel = f"{ENV['NAPSEDGE_B2_PREFIX']}/sentinel/{uuid.uuid4().hex[:10]}.txt"
        ro = last_json(adapter_script(v, probe + (
            "attempt('ro_put', lambda: store.put_bytes(P+'/sentinel/ro-write.txt', b'x'))\n"
            "attempt('ro_read_real_record', lambda: len(store.get_bytes(off.object_keys(P,%r)[1], off.RECORD_LIMIT)))\n"
            "print(json.dumps(res))") % first, "ro"))
        rw = last_json(adapter_script(v, probe + (
            "S=%r\nv={}\n"
            "attempt('put_v1', lambda: v.__setitem__('v1', c.put_object(Bucket=B,Key=S,Body=b'sentinel-v1').get('VersionId')))\n"
            "attempt('overwrite_v2', lambda: v.__setitem__('v2', c.put_object(Bucket=B,Key=S,Body=b'sentinel-v2').get('VersionId')))\n"
            "attempt('versions_listed', lambda: [(x.get('VersionId') is not None, x.get('IsLatest')) for x in c.list_object_versions(Bucket=B,Prefix=S).get('Versions',[])])\n"
            "attempt('delete_by_name_no_version_id', lambda: c.delete_object(Bucket=B,Key=S).get('DeleteMarker'))\n"
            "attempt('head_after_hide', lambda: c.head_object(Bucket=B,Key=S)['ContentLength'])\n"
            "attempt('permanent_delete_of_v1', lambda: c.delete_object(Bucket=B,Key=S,VersionId=v['v1']).get('VersionId') and 'deleted')\n"
            "attempt('get_retained_v1', lambda: c.get_object(Bucket=B,Key=S,VersionId=v['v1'])['Body'].read().decode())\n"
            "attempt('list_outside_prefix', lambda: store.list('outside-'+P+'/'))\n"
            "attempt('real_record_still_current', lambda: store.head(off.object_keys(P,%r)[1]).size)\n"
            "print(json.dumps(res))") % (sentinel, first), "rw"))
        evidence["steps"]["3_permission_behaviour"] = {"sentinel_object": sentinel, "read_only_key": ro, "upload_key": rw,
                                                       "note": "recorded as observed; writeFiles permits overwrite and hide (not immutability)"}
        assert ro["ro_put"].startswith("refused") and ro["ro_read_real_record"].startswith("ALLOWED"), ro
        assert rw["permanent_delete_of_v1"].startswith("refused"), ("the upload key must not permanently delete a version", rw)
        assert rw["get_retained_v1"] == "ALLOWED: sentinel-v1", ("a hidden version must remain retrievable", rw)
        assert rw["list_outside_prefix"].startswith("refused") and rw["real_record_still_current"].startswith("ALLOWED"), rw
        # 4. a second capture (off-site disabled), then uploaded through an injected interruption: the retry on the real service
        offset2 = cycle_log_offset(v)
        code, rec2, completed, _ = run_backup(v, {**base, "NAPSEDGE_BACKUP_OFFSITE_ENABLED": "false"}, "backup-cycle")
        outputs += [completed.stdout, completed.stderr]
        assert code == 0, rec2
        second = rec2["capture"]["report"]["artifact"]
        gate = heartbeat_gate(v, offset2)
        evidence["steps"]["4_heartbeat_gate_offsite_disabled"] = {"exit": gate.returncode, "output": gate.stdout.decode().strip()}
        assert gate.returncode == 1 and "disabled" in gate.stdout.decode(), ("exit 0 with off-site disabled must NOT ping", gate.stdout)
        # an EARLIER verified run (step 1) must not qualify a later run that appended nothing
        gate = heartbeat_gate(v, cycle_log_offset(v))
        evidence["steps"]["4_heartbeat_gate_no_new_record"] = {"exit": gate.returncode, "output": gate.stdout.decode().strip()}
        assert gate.returncode == 1 and "no cycle record" in gate.stdout.decode(), gate.stdout
        retry = last_json(adapter_script(v, (
            "from pathlib import Path\n"
            "class Faulty(off.S3ObjectStore):\n    fail=1\n    def put_file(self,key,path):\n"
            "        if Faulty.fail and key.endswith(off.PAYLOAD_NAME):\n            Faulty.fail-=1\n"
            "            self.put_bytes(key, Path(path).read_bytes()[:64])\n            raise off.StoreError('upload failed (injected interruption)')\n"
            "        super().put_file(key,path)\n"
            "store.__class__=Faulty\n"
            "r=off.upload_artifact('%s/%s',store=store,prefix=P,age=off.AgeTool('/usr/bin/age'),recipients_file='%s/recipients.txt',workdir='/tmp',"
            "state_dir='%s',attempts=3,backoff_seconds=2)\nprint(json.dumps(r.report()))") % (STORE_DIR, second, RECIPIENTS_DIR, STATE_DIR), "rw"))
        evidence["steps"]["4_interrupted_upload_retried"] = retry
        assert retry["outcome"] == "stored_verified" and retry["attempts"] == 2, retry
        # the interrupted attempt left a partial payload version BEFORE the record: expected, reported, not an anomaly
        retry_history = last_json(adapter_script(v, "print(json.dumps(off.version_history(store,P,%r)))" % second, "ro"))
        evidence["steps"]["4_retry_version_history"] = retry_history
        assert retry_history["anomalies"] == [] and retry_history["payload_versions"] == 2 and retry_history["payload_versions_before_record"] == 2, retry_history
        # 5. missing-object refusal
        missing = last_json(adapter_script(v, (
            "try:\n    off.fetch_artifact('napsedge-missing-%s',store=store,prefix=P,age=off.AgeTool('/usr/bin/age'),identity_file='/nonexistent',destination='/tmp')\n"
            "    out='ACCEPTED'\nexcept off.OffsiteError as exc:\n    out=type(exc).__name__+': '+str(exc)\nprint(json.dumps({'missing':out}))") % uuid.uuid4().hex[:8], "ro"))
        evidence["steps"]["5_missing_object"] = missing
        assert missing["missing"] != "ACCEPTED", missing
        # 6. integrity failure: the SECOND copy's payload is overwritten with one changed byte (same size), then a drill must refuse it
        corrupt = last_json(adapter_script(v, (
            "k=off.object_keys(P,%r)[0]\nimport tempfile\nd=tempfile.mkdtemp()\nstore.get_to_file(k,d+'/p')\nb=bytearray(open(d+'/p','rb').read())\n"
            "b[len(b)//2]^=1\nstore.put_bytes(k,bytes(b))\nprint(json.dumps({'overwritten':k,'bytes':len(b)}))") % second, "rw"))
        evidence["steps"]["6_corruption_injected"] = corrupt
        # 7. the original host's copy of the FIRST artifact is removed: recovery must use the download only
        assert helper(["-v", f"{v['store']}:/s"], f"rm -rf /s/{first} && ls -A /s").returncode == 0
        drill_ok = drill(case, v, first, "rw", publish=True)
        drill_bad = drill(case, v, second, "ro", publish=False)
        drill_bad_accepted = drill(case, v, second, "ro", publish=False, accept_version_anomalies=True)
        evidence["steps"]["7_drill_first_from_download"] = drill_ok
        evidence["steps"]["8_drill_corrupted_second_refused"] = {"exit": drill_bad["exit"], "refused": drill_bad["facts"].get("refused"),
                                                                 "with_anomaly_accepted": {"exit": drill_bad_accepted["exit"], "refused": drill_bad_accepted["facts"].get("refused")}}
        assert drill_ok["exit"] == 0 and drill_ok["facts"]["drill_record"]["passed"], drill_ok
        history = drill_ok["facts"]["drill_record"]["version_history"]
        assert history["anomalies"] == [] and history["record_versions"] == 1 and history["payload_versions"] == 1, history
        refused = drill_bad["facts"].get("refused", "")
        assert drill_bad["exit"] == 3 and "version history anomaly" in refused and "after its completion record" in refused, drill_bad
        assert drill_bad_accepted["exit"] == 3 and "SHA-256" in drill_bad_accepted["facts"].get("refused", ""), drill_bad_accepted
        # 9. retention dry run and status with the READ-ONLY key (deletion stays disabled)
        code, plan, completed, _ = run_backup(v, base, "backup-retention", key="ro")
        outputs += [completed.stdout, completed.stderr]
        evidence["steps"]["9_retention_dry_run"] = {"exit": code, "plan": plan}
        assert plan.get("mode") == "dry_run" and first in plan.get("keep", {}) and "last_known_recoverable" in plan["keep"][first], plan
        assert code == 10 and plan["deletable"] is False and second in plan["version_anomalies"] and first not in plan["version_anomalies"], plan
        code, status, completed, _ = run_backup(v, base, "backup-status", key="ro")
        outputs += [completed.stdout, completed.stderr]
        evidence["steps"]["10_status"] = {"exit": code, "status": status}
        assert code == 10 and second in status["version_history"]["artifacts_with_anomalies"], status
        assert first not in status["version_history"]["artifacts_with_anomalies"], status
        remaining = last_json(adapter_script(v, "print(json.dumps({'retained':[o.key+' '+str(o.size) for o in store.list(P+'/')]}))", "ro"))
        evidence["retained_remote_objects"] = remaining["retained"]
    finally:
        text = b"".join(outputs).decode(errors="replace") + json.dumps(evidence)
        evidence["credential_leak_check"] = "none found" if not any(s and s in text for s in secrets) else "LEAK"
        Path(ENV["NAPSEDGE_B2_EVIDENCE_DIR"], "b2_validation.json").write_text(json.dumps(evidence, indent=1, sort_keys=True, default=str), encoding="utf-8")
    assert evidence["credential_leak_check"] == "none found"


def drill(case, v: dict, artifact: str, key: str, *, publish: bool, accept_version_anomalies: bool | None = None, prefix: str | None = None) -> dict:
    """The delivered restore drill, in the tooling image: ONLY the B2 objects, key copy A (streamed into memory) and a fresh database. No store volume."""
    url = case.cluster.admin_url
    target = f"napsedge_pg_test_b2drill_{uuid.uuid4().hex[:10]}"
    config = {"endpoint": {"host": "localhost", "hostaddr": cluster_ip(case), "port": 5432, "admin_database": url.database, "ca_file": "/drill/ca/ca.pem"},
              "admin_user": url.username, "credential_files": {k: f"/drill/credentials/{k}" for k in ("admin", "migrator", "runtime", "backup")},
              "target_database": target, "evidence_dir": "/drill/evidence",
              "offsite": {"endpoint_url": ENV["NAPSEDGE_B2_ENDPOINT"], "region": ENV["NAPSEDGE_B2_REGION"], "bucket": ENV["NAPSEDGE_B2_BUCKET"],
                          "prefix": prefix or ENV["NAPSEDGE_B2_PREFIX"], "key_id_file": f"/drill/b2/{key}-key-id", "secret_key_file": f"/drill/b2/{key}-secret",
                          "allow_http_for_tests": False},
              "identity_file": "/drill/keys/napsedge-backup-identity.txt", "artifact": artifact, "publish_record": publish, "rto_target_hours": 4}
    if accept_version_anomalies is not None:
        config["accept_version_anomalies"] = accept_version_anomalies
    assert helper(["-v", f"{v['drillevidence']}:/e"], "cat > /e/config.json && chown 10001:10001 /e/config.json", stdin=json.dumps(config).encode()).returncode == 0
    common = ["--rm", "--read-only", "--tmpfs", "/tmp:rw,mode=1777", "--user", "10001:10001", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
              "--network", "bridge", "--label", LABEL, "-e", "PYTHONDONTWRITEBYTECODE=1", "-v", f"{v['drillcred']}:/drill/credentials:ro",
              "-v", f"{v['ca']}:/drill/ca:ro", "-v", f"{ENV['NAPSEDGE_B2_CRED_VOLUME']}:/drill/b2:ro",
              "--tmpfs", "/drill/keys:rw,noexec,nosuid,size=64k,mode=0700,uid=10001,gid=10001", "-v", f"{v['drillevidence']}:/drill/evidence",
              "--mount", f"type=bind,source={REPO},target=/repo,readonly", "-w", "/repo", "--entrypoint", "sh", ENV["NAPSEDGE_PROBE_TOOLING_IMAGE"], "-c"]
    drill_cmd = "python -m tests.postgres.restore_drill --config /drill/evidence/config.json"
    identity = (Path(ENV["NAPSEDGE_B2_IDENTITY_DIR"]) / "napsedge-backup-identity.txt").read_bytes()
    started = time.monotonic()
    run = docker("run", "-i", *common, f"umask 077 && cat > /drill/keys/napsedge-backup-identity.txt && exec {drill_cmd} run", stdin=identity, timeout=1800)
    del identity
    seconds = round(time.monotonic() - started, 2)
    facts = json.loads(helper(["-v", f"{v['drillevidence']}:/e:ro"], "cat /e/drill_run.json").stdout)["facts"]
    cleanup = docker("run", *common, f"exec {drill_cmd} cleanup --confirm cleanup", timeout=600)  # no key needed or given
    helper(["-v", f"{v['drillevidence']}:/e"], "rm -f /e/ledger.json /e/drill_run.json /e/drill_cleanup.json /e/config.json")
    return {"exit": run.returncode, "wall_seconds": seconds, "facts": facts, "cleanup_exit": cleanup.returncode, "target": target}
