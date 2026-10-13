"""
Phase 9B-8B: retention of REMOTE copies -- a dry-run plan first; deletion disabled by default.

Inventory: a COMPLETE listing of `<prefix>/artifacts/` and `<prefix>/drills/` (an incomplete listing raises; nothing is planned from
it). An artifact is "complete" when both its payload and its completion record exist, the record parses, and the payload's size
equals the record's. Anything else is "incomplete" (an interrupted upload, a manual leftover) and is never deleted automatically.

Selection (deterministic; all buckets in UTC, newest artifact of each bucket by the manifest's `created_utc`, ties by name):
  daily   the newest artifact of each of the most recent `retain_daily` calendar days that have one
  weekly  the same per ISO week, `retain_weekly` weeks
  monthly the same per calendar month, `retain_monthly` months
  always  the newest complete artifact, and the newest artifact with a PASSING restore drill ("last known recoverable")
Everything complete and not kept is a deletion candidate.

The plan is REFUSED (nothing may be deleted) when: the listing failed; any artifact is incomplete or inconsistent; any drill record
is unreadable; no artifact has a passing drill (the last known recoverable backup is unknown); or (Phase 9B-8C) the VERSION listing
failed, or any artifact folder -- including a hidden one that the plain listing no longer shows -- has a version-history anomaly
(see `backup_offsite.analyse_versions`). Such anomalies are reported in the plan (`version_anomalies`) for manual review.

Execution needs ALL of: `NAPSEDGE_BACKUP_RETENTION_DELETE_ENABLED=true`, `--execute`, and `--plan-sha256` equal to the digest of
the plan rebuilt at execution time (so only the reviewed plan runs). Per artifact EVERY VERSION is deleted by version id (Phase
9B-8C: a delete by name only HIDES on B2, leaving the data stored and billed): all versions and delete markers of the completion
record FIRST (the artifact immediately stops counting as stored), then those of the payload; an interruption leaves an "incomplete"
payload, never a false "complete". It needs a key with `deleteFiles` (and `listFiles`). Drill records are kept (history). Each
deletion is confirmed by a VERSION listing that shows nothing left in the artifact's folder.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from typing import Callable, TextIO

from src.deployment.backup_offsite import (
    PAYLOAD_NAME,
    RECORD_LIMIT,
    RECORD_NAME,
    ObjectStore,
    OffsiteError,
    RetrievalRefused,
    canonical,
    inventory,
    object_keys,
    parse_record,
    version_inventory,
)

PLAN_FORMAT = "napsedge.backup-retention-plan/1"
DRILL_RECORD_FORMAT = "napsedge.backup-drill/1"


def _created(value: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise RetrievalRefused("a completion record has an unreadable created_utc")


def drill_record_anomalies(store: ObjectStore, prefix: str) -> dict[str, list[str]]:
    """Drill-record key -> anomalies (Phase 9B-8C). A drill record is the claim-4 verification record and must be immutable in
    practice: written once, never overwritten and never hidden. On a versioned store (B2) an upload key could overwrite or hide one;
    such a record never counts as a passing drill. Anomaly checks only, not proof of authenticity. Raises unless the listing completes."""
    grouped: dict[str, list] = {}
    for entry in store.versions(f"{prefix}/drills/"):
        grouped.setdefault(entry.key, []).append(entry)
    found: dict[str, list[str]] = {}
    for key, entries in sorted(grouped.items()):
        data = [e for e in entries if not e.delete_marker]
        problems = []
        if any(e.delete_marker for e in entries):
            problems.append("the drill record has a delete marker (it was hidden)")
        if len(data) != 1:
            problems.append(f"the drill record has {len(data)} versions (expected exactly 1)")
        if problems:
            found[key] = problems
    return found


def passing_drills(store: ObjectStore, prefix: str) -> dict[str, list[datetime]]:
    """artifact name -> dates of PASSING drills (see `passing_drill_records`)."""
    found: dict[str, list[datetime]] = {}
    for name, record in passing_drill_records(store, prefix):
        found.setdefault(name, []).append(_created(record["drilled_utc"]))
    return found


def latest_passing_drill(store: ObjectStore, prefix: str) -> dict | None:
    """The newest passing drill record (by `drilled_utc`), or None."""
    records = [record for _, record in passing_drill_records(store, prefix)]
    return max(records, key=lambda r: _created(r["drilled_utc"])) if records else None


def passing_drill_records(store: ObjectStore, prefix: str) -> list[tuple[str, dict]]:
    """(artifact name, record) for every PASSING drill. A drill record counts only if it parses, says passed with its own consistent
    fields, names the manifest digest and the runbook revision it ran under (claim 4), and has a clean version history (written
    once, never overwritten or hidden: `drill_record_anomalies`)."""
    found: list[tuple[str, dict]] = []
    anomalous = drill_record_anomalies(store, prefix)
    for name, leaves in inventory(store, prefix)["drills"].items():
        for leaf in leaves:
            if f"{prefix}/drills/{name}/{leaf}" in anomalous:
                continue
            data = store.get_bytes(f"{prefix}/drills/{name}/{leaf}", RECORD_LIMIT)
            try:
                record = json.loads(data.decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                raise RetrievalRefused("a drill record is unreadable") from None
            if not isinstance(record, dict) or record.get("format") != DRILL_RECORD_FORMAT or record.get("artifact") != name:
                raise RetrievalRefused("a drill record is malformed")
            consistent = (record.get("restore_exit") == 0 and record.get("strict_all_equal") is True and record.get("strict_sequences_equal") is True
                          and record.get("monotonic_v1_policy_accepts") is True and record.get("monotonic_v1_r2_failures") == []
                          and not (record.get("version_history") or {}).get("anomalies_accepted")
                          and isinstance(record.get("manifest_sha256"), str) and len(record["manifest_sha256"]) == 64
                          and isinstance(record.get("runbook_sha256"), str) and len(record["runbook_sha256"]) == 64)
            if record.get("passed") is True and consistent:
                _created(record["drilled_utc"])  # an unreadable date refuses, as before
                found.append((name, record))
    return found


def _newest_per_bucket(items: list[tuple[datetime, str]], bucket: Callable[[datetime], tuple], count: int) -> list[str]:
    best: dict[tuple, tuple[datetime, str]] = {}
    for created, name in items:
        key = bucket(created)
        if key not in best or (created, name) > best[key]:
            best[key] = (created, name)
    return [name for _, (created, name) in sorted(best.items(), reverse=True)[:count]]


def build_plan(store: ObjectStore, prefix: str, ops, *, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> dict:
    refusals: list[str] = []
    complete: dict[str, datetime] = {}
    incomplete: list[str] = []
    try:
        listed = inventory(store, prefix)
    except OffsiteError as exc:
        return _plan(prefix, ops, {}, [], [], [f"the remote listing failed: {exc}"], now)
    for name in sorted(listed["artifacts"]):
        leaves = listed["artifacts"][name]
        if set(leaves) != {PAYLOAD_NAME, RECORD_NAME}:
            incomplete.append(name)
            continue
        try:
            record = parse_record(store.get_bytes(object_keys(prefix, name)[1], RECORD_LIMIT), name)
            if record["payload"]["bytes"] != leaves[PAYLOAD_NAME]:
                raise RetrievalRefused("size mismatch")
            complete[name] = _created(record["created_utc"])
        except OffsiteError:
            incomplete.append(name)
    if incomplete:
        refusals.append(f"{len(incomplete)} incomplete or inconsistent remote artifact(s): review them manually first")
    version_anomalies: dict[str, list[str]] = {}
    try:
        for name, history in version_inventory(store, prefix).items():
            if history["anomalies"] and name not in incomplete:
                version_anomalies[name] = history["anomalies"] + ([] if name in listed["artifacts"] else ["hidden: no current object"])
    except OffsiteError as exc:
        refusals.append(f"the remote version listing failed: {exc}")
    try:
        for key, problems in drill_record_anomalies(store, prefix).items():
            version_anomalies[key[len(prefix) + 1:]] = problems  # e.g. "drills/<artifact>/<ts>.json"
    except OffsiteError as exc:
        refusals.append(f"the drill-record version listing failed: {exc}")
    if version_anomalies:
        refusals.append(f"{len(version_anomalies)} remote artifact(s) or drill record(s) have version-history anomalies (possible replacement or hiding): review them manually first")
    try:
        drilled = {name: dates for name, dates in passing_drills(store, prefix).items() if name in complete}
    except OffsiteError as exc:
        drilled = {}
        refusals.append(f"the drill records could not be read: {exc}")
    keep: dict[str, list[str]] = {}
    items = sorted((created, name) for name, created in complete.items())
    for reason, bucket, count in (("daily", lambda d: (d.year, d.month, d.day), ops.retain_daily),
                                  ("weekly", lambda d: tuple(d.isocalendar())[:2], ops.retain_weekly),
                                  ("monthly", lambda d: (d.year, d.month), ops.retain_monthly)):
        for name in _newest_per_bucket(items, bucket, count):
            keep.setdefault(name, []).append(reason)
    if items:
        keep.setdefault(items[-1][1], []).append("newest_complete")
    recoverable = sorted((complete[name], name) for name in drilled)
    if recoverable:
        keep.setdefault(recoverable[-1][1], []).append("last_known_recoverable")
    elif complete:
        refusals.append("no remote artifact has a passing restore drill: the last known recoverable backup is unknown")
    delete = sorted(name for name in complete if name not in keep)
    return _plan(prefix, ops, keep, delete, incomplete, refusals, now, complete=complete, version_anomalies=version_anomalies)


def _plan(prefix, ops, keep, delete, incomplete, refusals, now, complete=None, version_anomalies=None) -> dict:
    body = {"format": PLAN_FORMAT, "prefix": prefix,
            "policy": {"daily": ops.retain_daily, "weekly": ops.retain_weekly, "monthly": ops.retain_monthly, "bucketing": "UTC"},
            "complete": len(complete or {}), "keep": {k: sorted(v) for k, v in sorted(keep.items())}, "delete": delete,
            "incomplete": sorted(incomplete), "refusals": refusals, "version_anomalies": dict(sorted((version_anomalies or {}).items()))}
    digest = hashlib.sha256(canonical(body)).hexdigest()
    return {**body, "plan_sha256": digest, "deletable": not refusals and bool(delete),
            "generated_utc": now().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def execute_plan(store: ObjectStore, prefix: str, ops, plan_sha256: str, *, now=lambda: datetime.now(timezone.utc)) -> dict:
    if not ops.retention_delete_enabled:
        return {"outcome": "refused", "detail": "deletion is disabled (NAPSEDGE_BACKUP_RETENTION_DELETE_ENABLED is not true)"}
    plan = build_plan(store, prefix, ops, now=now)
    if plan["plan_sha256"] != plan_sha256:
        return {"outcome": "refused", "detail": "the current plan differs from the reviewed plan (digest mismatch); re-run the dry run", "plan": plan}
    if plan["refusals"]:
        return {"outcome": "refused", "detail": "the plan has refusals", "plan": plan}
    deleted, errors, versions_deleted = [], [], 0
    for name in plan["delete"]:
        payload_key, record_key = object_keys(prefix, name)
        folder = f"{prefix}/artifacts/{name}/"
        try:
            entries = store.versions(folder)
            # first every version of the record (the artifact stops counting as stored), then every version of the payload
            for key in (record_key, payload_key):
                for entry in sorted((e for e in entries if e.key == key), key=lambda e: (not e.is_latest, e.version_id)):
                    store.delete_version(entry.key, entry.version_id)
                    versions_deleted += 1
            deleted.append(name)
        except OffsiteError as exc:
            errors.append(f"{name}: {exc}")
            break  # stop at the first failure
    confirmed, unconfirmed = [], []
    for name in deleted:
        try:
            (unconfirmed if store.versions(f"{prefix}/artifacts/{name}/") else confirmed).append(name)
        except OffsiteError as exc:
            errors.append(f"{name}: confirmation failed: {exc}")
            unconfirmed.append(name)
    remaining = inventory(store, prefix)["artifacts"]
    still = [n for n in plan["keep"] if n not in remaining]
    outcome = "deleted" if not errors and confirmed == deleted == plan["delete"] and not still else "partial"
    return {"outcome": outcome, "deleted": deleted, "confirmed_absent": confirmed, "versions_remaining": unconfirmed, "versions_deleted": versions_deleted,
            "errors": errors, "kept_missing": still, "plan_sha256": plan_sha256}


def run_retention(argv: list[str] | None = None, *, environ: dict | None = None, out: TextIO | None = None, store: ObjectStore | None = None) -> int:
    """`backup-retention` (dry run) or `backup-retention --execute --plan-sha256 <digest>`. Exit 0 ok, 10 refused, 12 partial."""
    import argparse
    import os

    from src.deployment.backup_operations import OperationsConfigurationError, load_offsite_config, load_operations_config

    parser = argparse.ArgumentParser(prog="backup-retention")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--plan-sha256", default="")
    args = parser.parse_args(argv)
    environ = dict(os.environ if environ is None else environ)
    out = out or sys.stdout
    try:
        ops = load_operations_config(environ)
        cfg = load_offsite_config(environ, need_recipients=False)
    except OperationsConfigurationError as exc:
        out.write(json.dumps({"outcome": "configuration_refused", "detail": str(exc)}) + "\n")
        return 10
    if store is None:
        from src.deployment.backup_offsite import S3ObjectStore

        store = S3ObjectStore(endpoint_url=cfg.endpoint_url, region=cfg.region, bucket=cfg.bucket, key_id_file=cfg.key_id_file, secret_key_file=cfg.secret_key_file)
    if not args.execute:
        plan = build_plan(store, cfg.prefix, ops)
        out.write(json.dumps({"mode": "dry_run", **plan}, sort_keys=True) + "\n")
        return 10 if plan["refusals"] else 0
    result = execute_plan(store, cfg.prefix, ops, args.plan_sha256)
    out.write(json.dumps({"mode": "execute", **result}, sort_keys=True, default=str) + "\n")
    return {"deleted": 0, "refused": 10}.get(result["outcome"], 12)
