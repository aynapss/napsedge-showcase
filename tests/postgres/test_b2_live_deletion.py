"""
Phase 9B-8C (opt-in): the ONE-TIME real Backblaze B2 test of retention DELETION by version id, on disposable artifacts.

Everything happens under a fresh sub-prefix `<NAPSEDGE_B2_PREFIX>/deltest-<id>` (inside the keys' scope), through the delivered
image and drill only:
1. three artifacts A, B, C are stored; B through one injected interruption, so it carries a partial RETRY LEFTOVER version;
2. A gets a passing drill from the download (published), so it is the last known recoverable copy;
3. the dry run (read-only key) must keep A (last_known_recoverable) and C (newest) and plan to delete exactly B, with no refusals;
4. execution with the UPLOAD key must fail (it has no deleteFiles) and delete nothing;
5. execution with the separate DELETION key (its own volume, read-only) must delete EVERY version of B -- record first, then both
   payload versions -- and confirm it with a version listing; afterwards (read-only key) B's folder has no version and no delete
   marker, and A, C and the drill record are untouched and anomaly-free.
Retention stays disabled everywhere else; this test sets NAPSEDGE_BACKUP_RETENTION_DELETE_ENABLED=true only for its own two runs.

Inputs: every NAPSEDGE_B2_* input of test_b2_live_validation.py, plus NAPSEDGE_B2_DELETE_LIVE=1 and NAPSEDGE_B2_DEL_CRED_VOLUME
(the volume made by deploy/b2/create-deletion-key.sh: del-key-id, del-secret).
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from tests.postgres.test_b2_live_validation import (  # noqa: F401  (fixtures and helpers)
    ENV,
    RECIPIENTS_DIR,
    STATE_DIR,
    STORE_DIR,
    adapter_script,
    case,
    drill,
    harness,
    helper,
    job_env,
    last_json,
    live,
    run_backup,
    secrets_in,
    volumes,
)

DEL = {k: os.environ.get(k, "") for k in ("NAPSEDGE_B2_DELETE_LIVE", "NAPSEDGE_B2_DEL_CRED_VOLUME")}
pytestmark = [pytest.mark.postgres, pytest.mark.skipif(DEL["NAPSEDGE_B2_DELETE_LIVE"] != "1" or not all(DEL.values()) or not all(ENV.values()),
                                                       reason="opt-in real-B2 deletion test: NAPSEDGE_B2_DELETE_LIVE=1, NAPSEDGE_B2_DEL_CRED_VOLUME and every NAPSEDGE_B2_* input")]


def versions_of(v, prefix: str, name: str) -> list:
    return last_json(adapter_script(v, "print(json.dumps([[e.key.rsplit('/',1)[1], e.delete_marker] for e in store.versions(P+'/artifacts/%s/')]))" % name, "ro", prefix))


def test_real_b2_retention_deletes_every_version_with_the_deletion_key_only(case, live):
    v, evidence = live, {"format": "napsedge.b2-deletion-test/1", "bucket": ENV["NAPSEDGE_B2_BUCKET"], "steps": {}}
    sub = f"{ENV['NAPSEDGE_B2_PREFIX']}/deltest-{uuid.uuid4().hex[:8]}"
    evidence["prefix"] = sub
    del_secrets = helper(["-v", f"{DEL['NAPSEDGE_B2_DEL_CRED_VOLUME']}:/v:ro"], "cat /v/del-key-id /v/del-secret").stdout.decode().split()
    assert len(del_secrets) == 2, "the deletion volume must hold del-key-id and del-secret"
    secrets = secrets_in(ENV["NAPSEDGE_B2_CRED_VOLUME"]) + del_secrets + list(case.cluster.secrets_to_scrub())
    outputs: list[bytes] = []
    base = job_env(case)
    base.pop("NAPSEDGE_BACKUP_ARTIFACT_ID")
    base.update({"NAPSEDGE_BACKUP_OFFSITE_ENABLED": "true", "NAPSEDGE_BACKUP_UPLOAD_BACKOFF_SECONDS": "2", "NAPSEDGE_LOG_LEVEL": "WARNING"})
    try:
        # 1-2. A stored and drilled (the last known recoverable copy)
        code, rec, completed, _ = run_backup(v, base, "backup-cycle", prefix=sub)
        outputs += [completed.stdout, completed.stderr]
        assert code == 0 and rec["offsite"]["outcome"] == "stored_verified", rec.get("offsite")
        a = rec["capture"]["report"]["artifact"]
        drilled = drill(case, v, a, "rw", publish=True, prefix=sub)
        assert drilled["exit"] == 0 and drilled["facts"]["drill_record"]["passed"], drilled
        # 1. B through one injected interruption (a partial retry leftover version), then C
        code, rec, completed, _ = run_backup(v, {**base, "NAPSEDGE_BACKUP_OFFSITE_ENABLED": "false"}, "backup-cycle", prefix=sub)
        outputs += [completed.stdout, completed.stderr]
        assert code == 0, rec
        b = rec["capture"]["report"]["artifact"]
        retry = last_json(adapter_script(v, (
            "from pathlib import Path\n"
            "class Faulty(off.S3ObjectStore):\n    fail=1\n    def put_file(self,key,path):\n"
            "        if Faulty.fail and key.endswith(off.PAYLOAD_NAME):\n            Faulty.fail-=1\n"
            "            self.put_bytes(key, Path(path).read_bytes()[:64])\n            raise off.StoreError('upload failed (injected interruption)')\n"
            "        super().put_file(key,path)\n"
            "store.__class__=Faulty\n"
            "r=off.upload_artifact('%s/%s',store=store,prefix=P,age=off.AgeTool('/usr/bin/age'),recipients_file='%s/recipients.txt',workdir='/tmp',"
            "state_dir='%s',attempts=3,backoff_seconds=2)\nprint(json.dumps(r.report()))") % (STORE_DIR, b, RECIPIENTS_DIR, STATE_DIR), "rw", sub))
        assert retry["outcome"] == "stored_verified" and retry["attempts"] == 2, retry
        code, rec, completed, _ = run_backup(v, base, "backup-cycle", prefix=sub)
        outputs += [completed.stdout, completed.stderr]
        assert code == 0 and rec["offsite"]["outcome"] == "stored_verified", rec.get("offsite")
        c = rec["capture"]["report"]["artifact"]
        evidence["artifacts"] = {"A_drilled": a, "B_to_delete": b, "C_newest": c}
        b_before = versions_of(v, sub, b)
        evidence["steps"]["1_B_versions_before"] = b_before
        assert sorted(b_before) == [["offsite.json", False], ["payload.tar.age", False], ["payload.tar.age", False]], b_before
        # 3. dry run (read-only key)
        code, plan, completed, _ = run_backup(v, base, "backup-retention", key="ro", prefix=sub)
        outputs += [completed.stdout, completed.stderr]
        evidence["steps"]["3_dry_run"] = {"exit": code, "plan": plan}
        assert code == 0 and plan["deletable"] and plan["refusals"] == [] and plan["version_anomalies"] == {}, plan
        assert plan["delete"] == [b] and "last_known_recoverable" in plan["keep"][a] and "newest_complete" in plan["keep"][c], plan
        enabled = {**base, "NAPSEDGE_BACKUP_RETENTION_DELETE_ENABLED": "true"}
        # 4. the UPLOAD key cannot delete a version: nothing is deleted
        code, refused, completed, _ = run_backup(v, enabled, "backup-retention", "--execute", "--plan-sha256", plan["plan_sha256"], key="rw", prefix=sub)
        outputs += [completed.stdout, completed.stderr]
        evidence["steps"]["4_execute_with_upload_key"] = {"exit": code, "result": refused}
        assert code == 12 and refused["deleted"] == [] and any("AccessDenied" in e for e in refused["errors"]), refused
        assert sorted(versions_of(v, sub, b)) == sorted(b_before), "the upload key removed nothing"
        # 5. the DELETION key (its own volume) deletes every version of B, record first, and confirms it
        code, result, completed, seconds = run_backup(v, enabled, "backup-retention", "--execute", "--plan-sha256", plan["plan_sha256"], key="del",
                                                      prefix=sub, cred_volume=DEL["NAPSEDGE_B2_DEL_CRED_VOLUME"])
        outputs += [completed.stdout, completed.stderr]
        evidence["steps"]["5_execute_with_deletion_key"] = {"exit": code, "result": result, "wall_seconds": seconds}
        assert code == 0 and result["outcome"] == "deleted" and result["deleted"] == [b] and result["confirmed_absent"] == [b], result
        assert result["versions_deleted"] == 3 and result["versions_remaining"] == [] and result["kept_missing"] == [], result
        after = {"B": versions_of(v, sub, b), "A": versions_of(v, sub, a), "C": versions_of(v, sub, c)}
        histories = last_json(adapter_script(v, "print(json.dumps({n: off.version_history(store,P,n)['anomalies'] for n in %r}))" % [a, c], "ro", sub))
        drills = last_json(adapter_script(v, "print(json.dumps([o.key for o in store.list(P+'/drills/')]))", "ro", sub))
        evidence["steps"]["5_after"] = {"versions": after, "anomalies_A_C": histories, "drill_records": drills}
        assert after["B"] == [], "no version and no delete marker of B remains"
        assert len(after["A"]) == 2 and len(after["C"]) == 2 and histories == {a: [], c: []}, (after, histories)
        assert len(drills) == 1 and a in drills[0], drills
    finally:
        text = json.dumps(evidence, default=str)
        blob = b"".join(outputs).decode("utf-8", "replace") + text
        evidence["credential_leak_check"] = "none found" if not any(s and s in blob for s in secrets) else "LEAK"
        Path(ENV["NAPSEDGE_B2_EVIDENCE_DIR"], "b2_deletion.json").write_text(json.dumps(evidence, indent=1, sort_keys=True, default=str), encoding="utf-8")
    assert evidence["credential_leak_check"] == "none found"
