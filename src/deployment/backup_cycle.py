"""
Phase 9B-8B: one scheduled backup CYCLE, and the status check that detects missed or failed cycles.

`run_cycle` (`python -m src.deployment.entrypoints backup-cycle`, started by the systemd timer):
1. runs the accepted job (`backup_job.run_job`, unchanged; its report and exit classification are kept verbatim);
2. on `skipped_overlap` (exit 3) retries, at most `overlap_attempts` captures, `overlap_delay_seconds` apart, never past
   `cycle_deadline_seconds` -- an overlap is never a failure, and a cycle still overlapped after its attempts is "skipped";
3. ONLY after `published` (exit 0), and only when `NAPSEDGE_BACKUP_OFFSITE_ENABLED=true`, uploads that artifact
   (`backup_offsite.upload_artifact`). Exits 4/5 (published but incomplete/uncertain) are never uploaded automatically;
4. writes ONE sanitized cycle record (stdout, and appended to `<state>/cycles.jsonl`) that keeps the capture outcome and the
   off-site outcome in separate fields. An upload failure is recorded only in the off-site part and in the off-site state record;
   it never rewrites the capture writer's records.

Cycle exit status (the systemd unit treats 0 and 3 as success; everything else triggers its OnFailure= hook):
  0  captured, and either remotely stored (verified) or off-site disabled
  3  every capture attempt was skipped because another job held the lock (not a failure; the status check counts these)
  1/4/5/64/130  the capture's own exit status, passed through (no upload)
  20 off-site failed   21 off-site uncertain   22 off-site refused   23 off-site configuration refused   65 operations config refused

`backup_status` (`backup-status`, its own timer): reads the local cycle records (and, when off-site is enabled, the remote drill
records) and reports `ok` or `attention` with explicit alerts -- no remotely verified copy within the RPO, K consecutive overlap
skips, a failed last cycle, an overdue drill, and (Phase 9B-8C) any remote artifact folder whose VERSION history shows an anomaly
(a change or hide after verification; hidden artifacts included). Exit 0 ok, 10 attention.

It also EVALUATES the system-level "recovery ready" definition Anthony approved on 2026-10-09 (specification section 11) and reports
each criterion as met or not, with the reason. All five must hold:
  1 the newest remotely verified copy is within the RPO;
  2 a passing claim-4 drill within the drill interval ran against the PROVIDER endpoint (its `endpoint_host` equals or ends with
    NAPSEDGE_BACKUP_RECOVERY_DRILL_HOST, e.g. `.db.ondigitalocean.com`; unset means "no provider configured", never met);
  3 that drill's measured duration is within the RTO;
  4 the status has no other alert (version anomalies included);
  5 both offline identity copies were verified within 365 days (NAPSEDGE_BACKUP_KEY_COPIES_VERIFIED_UTC, YYYY-MM-DD, recorded by
    the operator after the per-copy check; unset or malformed means not met).
The evaluation is a report: it never changes the exit status and never claims readiness when any input is missing.
"""
from __future__ import annotations

import io
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, TextIO

from src.deployment.backup_operations import OperationsConfigurationError, load_offsite_config, load_operations_config

CYCLE_RECORD_FORMAT = "napsedge.backup-cycle/1"
STATUS_FORMAT = "napsedge.backup-status/1"
OFFSITE_EXIT = {"failed": 20, "uncertain": 21, "refused": 22}
SKIP_ALERT_THRESHOLD = 2
_TAIL_LINES = 2000


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _last_json_line(text: str) -> dict:
    for line in reversed(text.strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return {"outcome": "unreported"}


def _default_uploader(environ: dict, artifact_dir: str, ops) -> dict:
    from src.deployment.backup_offsite import AgeTool, OffsiteError, S3ObjectStore, upload_artifact

    try:
        cfg = load_offsite_config(environ)
    except OperationsConfigurationError as exc:
        return {"outcome": "configuration_refused", "detail": str(exc)}
    try:
        store = S3ObjectStore(endpoint_url=cfg.endpoint_url, region=cfg.region, bucket=cfg.bucket, key_id_file=cfg.key_id_file, secret_key_file=cfg.secret_key_file)
    except OffsiteError as exc:
        return {"outcome": "configuration_refused", "detail": str(exc)}
    workdir = environ.get("TMPDIR") or None
    result = upload_artifact(artifact_dir, store=store, prefix=cfg.prefix, age=AgeTool(environ.get("NAPSEDGE_BACKUP_AGE", "/usr/bin/age")),
                             recipients_file=cfg.recipients_file, workdir=workdir, state_dir=cfg.state_dir,
                             attempts=ops.upload_attempts, backoff_seconds=ops.upload_backoff_seconds)
    return result.report()


def run_cycle(environ: dict | None = None, *, out: TextIO | None = None, capture_job: Callable | None = None, uploader: Callable | None = None,
              sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic, now: Callable[[], datetime] = _now) -> int:
    environ = dict(os.environ if environ is None else environ)
    out = out or __import__("sys").stdout
    started = now()
    record: dict = {"format": CYCLE_RECORD_FORMAT, "started_utc": _utc(started)}
    try:
        ops = load_operations_config(environ)
    except OperationsConfigurationError as exc:
        record.update(exit_code=65, outcome="operations_configuration_refused", detail=str(exc), finished_utc=_utc(now()))
        return _finish(record, environ, out)
    record["configured_targets"] = ops.as_record()
    if capture_job is None:
        from src.deployment.backup_job import run_job as capture_job
    deadline = monotonic() + ops.cycle_deadline_seconds
    attempts, code, report = [], None, {}
    for attempt in range(1, ops.overlap_attempts + 1):
        buffer = io.StringIO()
        code = capture_job(environ, out=buffer)
        report = _last_json_line(buffer.getvalue())
        attempts.append({"attempt": attempt, "exit_code": code, "outcome": report.get("outcome"), "artifact_id": report.get("artifact_id")})
        if code != 3:
            break
        if attempt == ops.overlap_attempts or monotonic() + ops.overlap_delay_seconds > deadline:
            break
        sleep(ops.overlap_delay_seconds)
    record["capture"] = {"attempts": attempts, "exit_code": code, "report": report}
    published = code == 0
    if not published:
        record["offsite"] = {"outcome": "not_attempted", "reason": "the capture did not publish" if code != 3 else "every capture attempt overlapped"}
    elif not ops.offsite_enabled:
        record["offsite"] = {"outcome": "disabled", "reason": "NAPSEDGE_BACKUP_OFFSITE_ENABLED is not true (inactive until deployment)"}
    else:
        artifact_dir = os.path.join(environ.get("NAPSEDGE_BACKUP_STORE_DIR", ""), str(report.get("artifact", "")))
        try:
            record["offsite"] = (uploader or _default_uploader)(environ, artifact_dir, ops)
        except Exception as exc:  # noqa: BLE001 -- an unexpected uploader error is an off-site failure, never a capture failure
            record["offsite"] = {"outcome": "failed", "detail": f"unexpected error ({type(exc).__name__})"}
    offsite = record["offsite"]["outcome"]
    record["states"] = {"locally_published": published, "remotely_stored": offsite in ("stored_verified", "already_stored"),
                        "restore_demonstrated": "not established by a cycle (see the drill)"}
    if not published:
        record["exit_code"] = code
    elif offsite == "configuration_refused":
        record["exit_code"] = 23
    else:
        record["exit_code"] = OFFSITE_EXIT.get(offsite, 0)
    record["outcome"] = {0: "ok", 3: "skipped_overlap"}.get(record["exit_code"], "attention")
    record["finished_utc"] = _utc(now())
    return _finish(record, environ, out)


def _finish(record: dict, environ: dict, out: TextIO) -> int:
    line = json.dumps(record, sort_keys=True, default=str)
    state = environ.get("NAPSEDGE_BACKUP_STATE_DIR", "").strip()
    if state:
        try:
            Path(state).mkdir(parents=True, exist_ok=True, mode=0o700)
            with open(Path(state) / "cycles.jsonl", "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            record["state_write_error"] = type(exc).__name__
            line = json.dumps(record, sort_keys=True, default=str)
    out.write(line + "\n")
    out.flush()
    return int(record["exit_code"])


def _read_cycles(state_dir: str) -> list[dict]:
    path = Path(state_dir) / "cycles.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-_TAIL_LINES:]
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except ValueError:
            records.append({"format": "unreadable"})
    return records


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if ts else None
    except ValueError:
        return None


def backup_status(environ: dict | None = None, *, out: TextIO | None = None, now: Callable[[], datetime] = _now,
                  drill_lister: Callable | None = None, version_checker: Callable | None = None, drill_summarizer: Callable | None = None) -> int:
    environ = dict(os.environ if environ is None else environ)
    out = out or __import__("sys").stdout
    alerts, current = [], now()
    try:
        ops = load_operations_config(environ)
    except OperationsConfigurationError as exc:
        out.write(json.dumps({"format": STATUS_FORMAT, "status": "attention", "alerts": [f"operations configuration refused: {exc}"]}) + "\n")
        return 10
    state = environ.get("NAPSEDGE_BACKUP_STATE_DIR", "").strip()
    cycles = _read_cycles(state) if state else []
    if not state:
        alerts.append("NAPSEDGE_BACKUP_STATE_DIR is not set: no cycle history can be checked")
    last_stored = max((_parse(c.get("finished_utc")) for c in cycles if c.get("states", {}).get("remotely_stored")), default=None, key=lambda d: d or datetime.min.replace(tzinfo=timezone.utc))
    last_published = max((_parse(c.get("finished_utc")) for c in cycles if c.get("states", {}).get("locally_published")), default=None, key=lambda d: d or datetime.min.replace(tzinfo=timezone.utc))
    skips = 0
    for c in reversed(cycles):
        if c.get("exit_code") == 3:
            skips += 1
        else:
            break
    rpo = timedelta(hours=ops.rpo_hours)
    if ops.offsite_enabled:
        if last_stored is None or current - last_stored > rpo:
            alerts.append(f"no remotely verified copy within the {ops.rpo_hours} h recovery-point target")
    elif last_published is None or current - last_published > rpo:
        alerts.append(f"no locally published capture within the {ops.rpo_hours} h recovery-point target (off-site is disabled)")
    if skips >= SKIP_ALERT_THRESHOLD:
        alerts.append(f"{skips} consecutive cycles skipped because of lock overlap")
    if cycles and cycles[-1].get("exit_code") not in (0, 3):
        alerts.append(f"the last cycle needs attention (exit {cycles[-1].get('exit_code')})")
    drill = {"status": "not checked (off-site disabled)"}
    if ops.offsite_enabled:
        try:
            newest = (drill_lister or _remote_last_passing_drill)(environ)
            drill = {"last_passing_drill_utc": _utc(newest) if newest else None}
            if newest is None or current - newest > timedelta(days=ops.drill_interval_days):
                alerts.append(f"no passing restore drill within {ops.drill_interval_days} days")
        except Exception as exc:  # noqa: BLE001 -- the check itself failing is an alert, reported by type
            alerts.append(f"the remote drill records could not be listed ({type(exc).__name__})")
        try:
            anomalies = (version_checker or _remote_version_anomalies)(environ)
            versions = {"artifacts_with_anomalies": sorted(anomalies)}
            if anomalies:
                alerts.append(f"{len(anomalies)} remote artifact(s) have version-history anomalies (possible replacement or hiding): "
                              + ", ".join(sorted(anomalies)[:3]))
        except Exception as exc:  # noqa: BLE001 -- the check itself failing is an alert, reported by type
            versions = {"status": "not checked"}
            alerts.append(f"the remote version history could not be listed ({type(exc).__name__})")
    else:
        versions = {"status": "not checked (off-site disabled)"}
    readiness = _recovery_ready(ops, environ, current, last_stored, alerts, drill_summarizer)
    result = {"format": STATUS_FORMAT, "checked_utc": _utc(current), "status": "attention" if alerts else "ok", "alerts": alerts,
              "last_remotely_stored_utc": _utc(last_stored) if last_stored else None,
              "last_locally_published_utc": _utc(last_published) if last_published else None,
              "consecutive_overlap_skips": skips, "drill": drill, "version_history": versions, "recovery_ready": readiness,
              "configured_targets": ops.as_record()}
    out.write(json.dumps(result, sort_keys=True) + "\n")
    return 10 if alerts else 0


def _remote_last_passing_drill(environ: dict) -> datetime | None:
    from src.deployment.backup_offsite import S3ObjectStore
    from src.deployment.backup_retention import passing_drills

    cfg = load_offsite_config(environ, need_recipients=False)
    store = S3ObjectStore(endpoint_url=cfg.endpoint_url, region=cfg.region, bucket=cfg.bucket, key_id_file=cfg.key_id_file, secret_key_file=cfg.secret_key_file)
    dates = [d for drills in passing_drills(store, cfg.prefix).values() for d in drills]
    return max(dates) if dates else None


def _remote_version_anomalies(environ: dict) -> dict[str, list[str]]:
    from src.deployment.backup_offsite import S3ObjectStore, version_inventory
    from src.deployment.backup_retention import drill_record_anomalies

    cfg = load_offsite_config(environ, need_recipients=False)
    store = S3ObjectStore(endpoint_url=cfg.endpoint_url, region=cfg.region, bucket=cfg.bucket, key_id_file=cfg.key_id_file, secret_key_file=cfg.secret_key_file)
    found = {name: history["anomalies"] for name, history in version_inventory(store, cfg.prefix).items() if history["anomalies"]}
    found.update({key[len(cfg.prefix) + 1:]: problems for key, problems in drill_record_anomalies(store, cfg.prefix).items()})
    return found


def _recovery_ready(ops, environ: dict, current: datetime, last_stored: datetime | None, alerts: list[str], drill_summarizer) -> dict:
    """The approved system-level definition (module docstring), criterion by criterion. Missing input is never "met"."""
    criteria: dict[str, dict] = {}

    def put(name: str, met: bool, detail: str) -> None:
        criteria[name] = {"met": bool(met), "detail": detail}

    rpo = timedelta(hours=ops.rpo_hours)
    put("1_remote_copy_within_rpo", ops.offsite_enabled and last_stored is not None and current - last_stored <= rpo,
        "off-site disabled" if not ops.offsite_enabled else f"newest remotely verified copy: {_utc(last_stored) if last_stored else 'none'}")
    provider = environ.get("NAPSEDGE_BACKUP_RECOVERY_DRILL_HOST", "").strip().lower()
    record, drill_error = None, None
    if ops.offsite_enabled:
        try:
            record = (drill_summarizer or _remote_latest_passing_drill)(environ)
        except Exception as exc:  # noqa: BLE001 -- reported by type, never as met
            drill_error = type(exc).__name__
    host = str((record or {}).get("endpoint_host") or "").lower()
    drilled = _parse((record or {}).get("drilled_utc"))
    on_provider = bool(provider) and bool(host) and (host == provider.lstrip(".") or host.endswith(provider if provider.startswith(".") else "." + provider))
    recent = drilled is not None and current - drilled <= timedelta(days=ops.drill_interval_days)
    if drill_error:
        detail = f"drill records could not be read ({drill_error})"
    elif not provider:
        detail = "NAPSEDGE_BACKUP_RECOVERY_DRILL_HOST is not set: no provider endpoint configured"
    elif record is None:
        detail = "no passing claim-4 drill"
    else:
        detail = f"newest passing drill {_utc(drilled) if drilled else 'undated'} on {host or 'an unrecorded endpoint'}"
    put("2_provider_drill_within_interval", on_provider and recent and not drill_error, detail)
    duration = (record or {}).get("duration_seconds_measured")
    put("3_drill_duration_within_rto", on_provider and recent and isinstance(duration, (int, float)) and duration <= ops.rto_hours * 3600,
        f"measured {duration} s against {ops.rto_hours} h" if isinstance(duration, (int, float)) else "no measured duration")
    put("4_status_ok", not alerts, "no other alert" if not alerts else f"{len(alerts)} alert(s)")
    verified = environ.get("NAPSEDGE_BACKUP_KEY_COPIES_VERIFIED_UTC", "").strip()
    try:
        when = datetime.strptime(verified, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        put("5_key_copies_verified_within_365_days", timedelta(0) <= current - when <= timedelta(days=365), f"both copies verified {verified}")
    except ValueError:
        put("5_key_copies_verified_within_365_days", False, "NAPSEDGE_BACKUP_KEY_COPIES_VERIFIED_UTC is not set or not YYYY-MM-DD")
    ready = all(c["met"] for c in criteria.values())
    return {"ready": ready, "criteria": criteria, "definition": "specification section 11, approved by Anthony on 2026-10-09",
            "note": "a report only; it never changes the exit status" + ("" if ready else "; NOT recovery ready")}


def _remote_latest_passing_drill(environ: dict) -> dict | None:
    from src.deployment.backup_offsite import S3ObjectStore
    from src.deployment.backup_retention import latest_passing_drill

    cfg = load_offsite_config(environ, need_recipients=False)
    store = S3ObjectStore(endpoint_url=cfg.endpoint_url, region=cfg.region, bucket=cfg.bucket, key_id_file=cfg.key_id_file, secret_key_file=cfg.secret_key_file)
    return latest_passing_drill(store, cfg.prefix)
