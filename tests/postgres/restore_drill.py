"""
Phase 9B-8B: the RESTORE DRILL -- recovery from an off-site copy alone (TEST LAYER verification procedure; `tests/` never enters an
image; it runs in the probe-tooling image on any host, without the original application host).

Inputs, and nothing else: the off-site store (a READ key; a write key only to publish the drill record), the separately held
`age` identity (the private decryption key), and credentials for ONE recovery PostgreSQL 16 endpoint.

`python -m tests.postgres.restore_drill --config <file> run` performs, stopping at the first refusal:
1. choose the artifact (`latest` complete remote artifact, or a named one);
2. `backup_offsite.fetch_artifact`: download, payload SHA-256 against the completion record, decrypt, plaintext SHA-256, strict
   extraction, the accepted verifier, the manifest against the record -- the restore uses ONLY this downloaded copy;
3. roles: if none of the three NapsEdge roles exists, create them (the probe's SCRAM bootstrap, ledger-recorded); if all exist, use
   them; a partial set is refused;
4. a NEW, guarded, migrator-owned target (refused if it exists; refused if it is the artifact's source by name or OID), then 9B-6E's
   `restore` (`pg_restore --exit-on-error --single-transaction` as the migrator) and `compare` against the artifact's SAVED baseline
   (never a newly read source): the strict verdict and `monotonic_v1` reported SEPARATELY, R2 blocking;
5. a drill record `napsedge.backup-drill/1` (local evidence; with `publish_record` also uploaded to `<prefix>/drills/<artifact>/<ts>.json`
   and read back). `passed` requires restore exit 0, every strict check equal, strictly equal sequences and the policy accepting.

`cleanup --confirm cleanup` drops exactly the ledger's target and created roles (the probe's own cleanup).

Exit: 0 passed; 4 completed with a difference (NOT passed); 3 refused (corrupt or replaced payload, wrong or missing key, unsafe
target, existing resource); 2 blocker (the endpoint refused a step or the restore failed); 1 error (type only).
A passed drill establishes "restore demonstrated" for that copy on that endpoint at that time. It is not recovery readiness by
itself (the readiness policies stay open; a strict difference or a failed R2 always prevents it).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from tests.postgres import provider_probe as probe

DRILL_FORMAT = "napsedge.backup-drill/1"
DRILL_KEYS = {"endpoint", "admin_user", "credential_files", "target_database", "evidence_dir", "offsite", "identity_file", "artifact",
              "publish_record", "rto_target_hours"}
OPTIONAL_DRILL_KEYS = {"accept_version_anomalies"}  # Phase 9B-8C: false unless an operator explicitly accepts a reviewed anomaly
OFFSITE_KEYS = {"endpoint_url", "region", "bucket", "prefix", "key_id_file", "secret_key_file", "allow_http_for_tests"}
NO_SOURCE = "napsedge_pg_test_drill_has_no_source"  # a drill never creates or reads a source database


def load_drill_config(path: str):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise probe.ProbeRefused(f"the drill configuration cannot be read ({type(exc).__name__})") from None
    probe._require(isinstance(data, dict) and DRILL_KEYS <= set(data) <= DRILL_KEYS | OPTIONAL_DRILL_KEYS,
                   f"drill configuration keys must be exactly {sorted(DRILL_KEYS)} (optionally {sorted(OPTIONAL_DRILL_KEYS)})")
    probe._require(isinstance(data.get("accept_version_anomalies", False), bool), "accept_version_anomalies must be true or false")
    offsite = data["offsite"]
    probe._require(isinstance(offsite, dict) and set(offsite) == OFFSITE_KEYS, f"offsite keys must be exactly {sorted(OFFSITE_KEYS)}")
    probe._require(isinstance(data["identity_file"], str) and data["identity_file"].startswith("/"), "identity_file must be an absolute POSIX path")
    probe._require(isinstance(data["artifact"], str) and data["artifact"], "artifact must be 'latest' or an artifact name")
    probe._require(isinstance(data["publish_record"], bool), "publish_record must be true or false")
    probe._require(isinstance(data["rto_target_hours"], int) and 1 <= data["rto_target_hours"] <= 168, "rto_target_hours must be 1-168")
    for key in ("key_id_file", "secret_key_file"):
        probe._require(isinstance(offsite[key], str) and offsite[key].startswith("/"), f"offsite.{key} must be an absolute POSIX path")
    probe._require(isinstance(offsite["allow_http_for_tests"], bool), "offsite.allow_http_for_tests must be true or false")
    probe._require(offsite["endpoint_url"].startswith("https://") or offsite["allow_http_for_tests"], "offsite.endpoint_url must be https://")
    probe_data = {k: data[k] for k in ("endpoint", "admin_user", "credential_files", "target_database", "evidence_dir")}
    probe_data.update(source_database=NO_SOURCE, job={"git_revision": "0" * 40, "git_dirty": True, "tooling_version": "drill",
                                                      "container_ca_file": "/unused", "container_password_file": "/unused", "container_store_dir": "/unused"})
    return probe.load_config_data(probe_data), data


def _store(drill: dict):
    from src.deployment.backup_offsite import S3ObjectStore

    o = drill["offsite"]
    return S3ObjectStore(endpoint_url=o["endpoint_url"], region=o["region"], bucket=o["bucket"], key_id_file=o["key_id_file"], secret_key_file=o["secret_key_file"])


def choose_artifact(store, prefix: str, wanted: str) -> str:
    from src.deployment.backup_offsite import PAYLOAD_NAME, RECORD_LIMIT, RECORD_NAME, inventory, object_keys, parse_record
    from src.deployment.backup_retention import _created

    if wanted != "latest":
        return wanted
    complete = []
    for name, leaves in inventory(store, prefix)["artifacts"].items():
        if set(leaves) == {PAYLOAD_NAME, RECORD_NAME}:
            record = parse_record(store.get_bytes(object_keys(prefix, name)[1], RECORD_LIMIT), name)
            complete.append((_created(record["created_utc"]), name))
    probe._require(bool(complete), "no complete remote artifact exists")
    return max(complete)[1]


def ensure_roles(config, cluster, facts: dict) -> list[str]:
    from sqlalchemy.exc import DBAPIError

    admin = probe._engine(cluster.admin_url)
    try:
        probe._connect_or_block(admin, "the recovery admin")
        existing = probe.existing_resources(admin, config)
        if existing["databases"]:
            raise probe.ProbeRefused("the drill target already exists (never reused or dropped)")
        if existing["roles"] and len(existing["roles"]) != len(probe.PROBE_ROLES):
            raise probe.ProbeRefused(f"a partial NapsEdge role set exists ({existing['roles']}): refused")
        if existing["roles"]:
            return ["used the existing NapsEdge roles (not created; not dropped by cleanup)"]
        superuser = probe._rows(admin, "SELECT rolsuper FROM pg_roles WHERE rolname = current_user")[0][0]
        passwords = {"napsedge_migrator": cluster.migrator_password, "napsedge_runtime_ro": cluster.runtime_password, "napsedge_backup_ro": cluster.backup_password}
        done = []
        for name in probe.PROBE_ROLES:
            try:
                probe._create_role(admin, name, passwords[name], self_grant_set=(name == "napsedge_migrator" and not superuser))
            except DBAPIError as exc:
                raise probe._blocker_from(exc, f"CREATE ROLE {name}") from None
            probe.record_created(config, "role", name)
            done.append(f"MUTATION: created role {name}")
        return done
    finally:
        admin.dispose()


def run_drill(config, drill: dict, cluster) -> tuple[int, dict]:
    from sqlalchemy.exc import DBAPIError

    from src.deployment.backup_offsite import AgeTool, OffsiteError, canonical, fetch_artifact
    from tests.postgres.capture_client_support import NativeTestClient
    from tests.postgres.test_postgres_backup_capture_roundtrip import compare, restore, rule_rows
    from tests.postgres.test_postgres_backup_published_roundtrip import ArtifactRefused, open_published

    started = time.monotonic()
    store = _store(drill)
    prefix = drill["offsite"]["prefix"]
    facts: dict = {"recovery_inputs": "off-site store + separately held identity + recovery endpoint credentials only"}
    work = Path(tempfile.mkdtemp(prefix="napsedge_drill_"))
    try:
        try:
            name = choose_artifact(store, prefix, drill["artifact"])
            facts["artifact"] = name
            fetched = fetch_artifact(name, store=store, prefix=prefix, age=AgeTool(os.environ.get("NAPSEDGE_BACKUP_AGE", "/usr/bin/age")),
                                     identity_file=drill["identity_file"], destination=str(work),
                                     accept_version_anomalies=drill.get("accept_version_anomalies", False))
            published = open_published(fetched.artifact_dir)  # the accepted 9B-6J reader on the DOWNLOADED copy
        except (OffsiteError, ArtifactRefused) as exc:
            return probe.REFUSED, {**facts, "refused": f"retrieval refused: {exc}"}
        facts["version_history"] = fetched.history
        facts.update(payload_sha256=fetched.record["payload"]["sha256"], artifact_id=fetched.record["artifact_id"], manifest_sha256=fetched.record["manifest_sha256"],
                     artifact_source=published.facts["source"], retrieved_from="the off-site store (no local artifact used)")
        facts["mutations"] = ensure_roles(config, cluster, facts)
        admin = probe._engine(cluster.admin_url)
        try:
            try:
                probe._create_database(admin, config.target_database)
            except DBAPIError as exc:
                raise probe._blocker_from(exc, "CREATE DATABASE (drill target) ... OWNER napsedge_migrator") from None
            probe.record_created(config, "database", config.target_database)
            facts["mutations"].append(f"MUTATION: created database {config.target_database} owned by napsedge_migrator")
        finally:
            admin.dispose()
        client = NativeTestClient(cluster)
        scratch = tempfile.mkdtemp(prefix="napsedge_drill_restore_")
        case = SimpleNamespace(cluster=cluster, client=client, container_dir=scratch, targets=[config.target_database], ledger=None)
        try:
            try:
                outcome = restore(case, published.captured, config.target_database)
            except AssertionError as exc:  # the accepted guards (RestoreRefused / PreconditionFailed) are AssertionError subclasses
                return probe.REFUSED, {**facts, "refused": f"the restore target was refused: {type(exc).__name__}: {str(exc)[:160]}"}
            comparison = compare(SimpleNamespace(cluster=probe.MigratorReader(cluster)), published.captured, config.target_database) if outcome.returncode == 0 else None
            processes = client.client_processes()
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    duration = round(time.monotonic() - started, 2)
    runbook = Path("docs/backup_operations_runbook.md")  # the runbook revision this drill ran under (claim 4 names it)
    runbook_sha256 = hashlib.sha256(runbook.read_bytes()).hexdigest() if runbook.is_file() else None
    record = {"format": DRILL_FORMAT, "artifact": name, "artifact_id": facts["artifact_id"], "payload_sha256": facts["payload_sha256"],
              "manifest_sha256": facts["manifest_sha256"], "runbook": str(runbook), "runbook_sha256": runbook_sha256,
              "endpoint_host": drill["endpoint"]["host"],
              "drilled_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "duration_seconds_measured": duration,
              "rto_target_hours_configured": drill["rto_target_hours"], "restore_exit": outcome.returncode, "restore_role": "napsedge_migrator",
              "target": outcome.identity, "client_processes_after": processes,
              "strict_verdict": comparison.verdict if comparison else None,
              "strict_all_equal": bool(comparison and all(comparison.verdict.values())),
              "strict_sequences_equal": bool(comparison and comparison.strict_sequences_equal),
              "sequence_differences": comparison.sequence_differences if comparison else None,
              "monotonic_v1_rules": rule_rows(comparison) if comparison else None,
              "monotonic_v1_policy_accepts": bool(comparison and comparison.policy_accepts),
              "monotonic_v1_r2_failures": comparison.r2_failures if comparison else None,
              "version_history": facts["version_history"],
              "recovery_readiness": "not claimed by a drill (policies open; a strict difference or failed R2 always prevents it)"}
    # a drill on an artifact whose version anomaly was explicitly accepted never counts as a PASS (it is manual-review evidence only)
    record["passed"] = (not record["version_history"].get("anomalies_accepted") and record["runbook_sha256"] is not None
                        and record["restore_exit"] == 0 and record["strict_all_equal"]
                        and record["strict_sequences_equal"]
                        and record["monotonic_v1_policy_accepts"] and record["monotonic_v1_r2_failures"] == [])
    facts["drill_record"] = record
    if drill["publish_record"]:
        key = f"{prefix}/drills/{name}/{record['drilled_utc'].replace('-', '').replace(':', '')}.json"
        body = canonical(record)
        try:
            store.put_bytes(key, body)
            facts["drill_record_published"] = {"key": key, "read_back_equal": store.get_bytes(key, 64 * 1024) == body}
        except OffsiteError as exc:
            facts["drill_record_published"] = {"key": key, "error": str(exc)}
    if outcome.returncode != 0:
        return probe.BLOCKER, {**facts, "blocker": "the restore into the guarded target failed"}
    return (probe.PASS if record["passed"] else probe.DIFFERENCE), facts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.postgres.restore_drill")
    parser.add_argument("--config", required=True)
    parser.add_argument("step", choices=["run", "cleanup"])
    parser.add_argument("--confirm", default="")
    args = parser.parse_args(argv)
    secrets: tuple[str, ...] = ()
    try:
        config, drill = load_drill_config(args.config)
        if drill["offsite"]["allow_http_for_tests"]:
            os.environ["NAPSEDGE_BACKUP_OFFSITE_ALLOW_HTTP_FOR_TESTS"] = "1"
        cluster = probe.probe_cluster(config, probe.resolve_hostaddr(config))
        secrets = cluster.secrets_to_scrub()
        if args.step == "run":
            code, facts = run_drill(config, drill, cluster)
        else:
            code, facts = probe.step_cleanup(config, cluster, SimpleNamespace(confirm=args.confirm))
    except probe.ProbeRefused as exc:
        code, facts = probe.REFUSED, {"refused": str(exc)}
    except probe.ProbeBlocker as exc:
        code, facts = probe.BLOCKER, {"blocker": str(exc)}
    except Exception as exc:  # noqa: BLE001
        import traceback

        code, facts = probe.ERROR, {"error_type": type(exc).__name__,
                                    "at": [f"{os.path.basename(f.filename)}:{f.lineno}:{f.name}" for f in traceback.extract_tb(exc.__traceback__)][-8:]}
    record = {"step": f"drill-{args.step}", "outcome": probe.OUTCOMES[code], "exit": code, "facts": facts}
    try:
        probe.write_evidence(config, f"drill_{args.step}", record, secrets)
    except (NameError, OSError):
        pass
    line = json.dumps({"step": f"drill-{args.step}", "outcome": probe.OUTCOMES[code], "exit": code,
                       **({"reason": facts.get("blocker") or facts.get("refused")} if code in (probe.BLOCKER, probe.REFUSED) else {})}, sort_keys=True, default=str)
    for secret in secrets:
        line = line.replace(secret, "[removed]") if secret else line
    print(line)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
