"""
Phase 9B-8B: the backup OPERATING configuration -- configured targets and switches, never measured results.

Defaults (adopted 2026-10-08 as documented, configurable initial values; docs/backup_operations_runbook.md):

* schedule: one capture cycle daily at 03:30 UTC (systemd timer; `Persistent=true` runs ONE catch-up cycle after a missed
  run); an optional extra cycle after a successful controlled import (the operator or import hook starts the same unit);
* overlap: a cycle whose capture reports `skipped_overlap` retries up to `overlap_attempts` times, `overlap_delay_seconds`
  apart, all inside `cycle_deadline_seconds`; still overlapped after that = this cycle's capture is skipped (exit 3);
* recovery-point target (RPO): 24 hours -- the newest REMOTELY VERIFIED copy must be younger than this (the status check);
* recovery-time target (RTO): 4 hours from the decision to restore to a verified restored database -- a TARGET; the drill
  records the measured duration, which is never reported as meeting the target unless measured;
* retention: 7 daily, 4 weekly (ISO weeks), 6 monthly, all bucketed in UTC; deletion DISABLED by default;
* restore drill: one per calendar month (UTC), on a remotely stored copy.

Everything that would act on a remote service is OFF until deployment: `offsite_enabled` and `retention_delete_enabled`
default to false. No credential is ever read from an environment value here: credential variables name FILES.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

__all__ = ["OperationsConfigurationError", "OperationsConfig", "OffsiteConfig", "load_operations_config", "load_offsite_config"]

_INT = re.compile(r"[0-9]{1,6}")


class OperationsConfigurationError(Exception):
    """A malformed operating setting. Names the variable only, never its value."""


#: variable -> (minimum, maximum, default)
INTEGER_SETTINGS: dict[str, tuple[int, int, int]] = {
    "NAPSEDGE_BACKUP_RPO_HOURS": (1, 168, 24),
    "NAPSEDGE_BACKUP_RTO_HOURS": (1, 168, 4),
    "NAPSEDGE_BACKUP_OVERLAP_ATTEMPTS": (1, 12, 4),
    "NAPSEDGE_BACKUP_OVERLAP_DELAY_SECONDS": (1, 3600, 900),
    "NAPSEDGE_BACKUP_CYCLE_DEADLINE_SECONDS": (60, 86400, 7200),
    "NAPSEDGE_BACKUP_UPLOAD_ATTEMPTS": (1, 10, 3),
    "NAPSEDGE_BACKUP_UPLOAD_BACKOFF_SECONDS": (0, 600, 30),
    "NAPSEDGE_BACKUP_RETAIN_DAILY": (1, 366, 7),
    "NAPSEDGE_BACKUP_RETAIN_WEEKLY": (0, 260, 4),
    "NAPSEDGE_BACKUP_RETAIN_MONTHLY": (0, 120, 6),
    "NAPSEDGE_BACKUP_DRILL_INTERVAL_DAYS": (1, 92, 31),
}
BOOLEAN_SETTINGS = {"NAPSEDGE_BACKUP_OFFSITE_ENABLED": False, "NAPSEDGE_BACKUP_RETENTION_DELETE_ENABLED": False}
SCHEDULE = {"on_calendar": "*-*-* 03:30:00 UTC", "timezone": "UTC", "persistent": True, "randomized_delay": "10min"}


@dataclass(frozen=True)
class OperationsConfig:
    rpo_hours: int = 24
    rto_hours: int = 4
    overlap_attempts: int = 4
    overlap_delay_seconds: int = 900
    cycle_deadline_seconds: int = 7200
    upload_attempts: int = 3
    upload_backoff_seconds: int = 30
    retain_daily: int = 7
    retain_weekly: int = 4
    retain_monthly: int = 6
    drill_interval_days: int = 31
    offsite_enabled: bool = False
    retention_delete_enabled: bool = False
    schedule: dict = field(default_factory=lambda: dict(SCHEDULE))

    def as_record(self) -> dict:
        """The configured targets, labelled as such (evidence; never a measured result)."""
        return {"kind": "configured_targets", "rpo_hours": self.rpo_hours, "rto_hours": self.rto_hours,
                "retention": {"daily": self.retain_daily, "weekly": self.retain_weekly, "monthly": self.retain_monthly, "bucketing": "UTC"},
                "drill_interval_days": self.drill_interval_days, "schedule": self.schedule,
                "overlap": {"attempts": self.overlap_attempts, "delay_seconds": self.overlap_delay_seconds},
                "cycle_deadline_seconds": self.cycle_deadline_seconds,
                "upload": {"attempts": self.upload_attempts, "backoff_seconds": self.upload_backoff_seconds},
                "offsite_enabled": self.offsite_enabled, "retention_delete_enabled": self.retention_delete_enabled}


def _integer(environ: dict, name: str, errors: list[str]) -> int:
    low, high, default = INTEGER_SETTINGS[name]
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    if _INT.fullmatch(raw) and low <= int(raw) <= high:
        return int(raw)
    errors.append(f"{name} must be a whole number from {low} to {high}")
    return default


def _boolean(environ: dict, name: str, errors: list[str]) -> bool:
    raw = environ.get(name, "").strip().lower()
    if not raw:
        return BOOLEAN_SETTINGS[name]
    if raw in ("true", "false"):
        return raw == "true"
    errors.append(f"{name} must be true or false")
    return BOOLEAN_SETTINGS[name]


def load_operations_config(environ: dict | None = None) -> OperationsConfig:
    environ = dict(os.environ if environ is None else environ)
    errors: list[str] = []
    ints = {name: _integer(environ, name, errors) for name in INTEGER_SETTINGS}
    bools = {name: _boolean(environ, name, errors) for name in BOOLEAN_SETTINGS}
    if errors:
        raise OperationsConfigurationError("; ".join(errors))
    return OperationsConfig(
        rpo_hours=ints["NAPSEDGE_BACKUP_RPO_HOURS"], rto_hours=ints["NAPSEDGE_BACKUP_RTO_HOURS"],
        overlap_attempts=ints["NAPSEDGE_BACKUP_OVERLAP_ATTEMPTS"], overlap_delay_seconds=ints["NAPSEDGE_BACKUP_OVERLAP_DELAY_SECONDS"],
        cycle_deadline_seconds=ints["NAPSEDGE_BACKUP_CYCLE_DEADLINE_SECONDS"], upload_attempts=ints["NAPSEDGE_BACKUP_UPLOAD_ATTEMPTS"],
        upload_backoff_seconds=ints["NAPSEDGE_BACKUP_UPLOAD_BACKOFF_SECONDS"], retain_daily=ints["NAPSEDGE_BACKUP_RETAIN_DAILY"],
        retain_weekly=ints["NAPSEDGE_BACKUP_RETAIN_WEEKLY"], retain_monthly=ints["NAPSEDGE_BACKUP_RETAIN_MONTHLY"],
        drill_interval_days=ints["NAPSEDGE_BACKUP_DRILL_INTERVAL_DAYS"], offsite_enabled=bools["NAPSEDGE_BACKUP_OFFSITE_ENABLED"],
        retention_delete_enabled=bools["NAPSEDGE_BACKUP_RETENTION_DELETE_ENABLED"])


#: Off-site settings. Credential variables name FILES (read under the job's password-file rules), never values.
OFFSITE_REQUIRED = ("NAPSEDGE_BACKUP_OFFSITE_ENDPOINT_URL", "NAPSEDGE_BACKUP_OFFSITE_REGION", "NAPSEDGE_BACKUP_OFFSITE_BUCKET",
                    "NAPSEDGE_BACKUP_OFFSITE_PREFIX", "NAPSEDGE_BACKUP_OFFSITE_KEY_ID_FILE", "NAPSEDGE_BACKUP_OFFSITE_SECRET_KEY_FILE")
_BUCKET = re.compile(r"[a-z0-9][a-z0-9-]{4,61}[a-z0-9]")
_PREFIX = re.compile(r"[a-z0-9][a-z0-9/_-]{0,62}")


@dataclass(frozen=True)
class OffsiteConfig:
    endpoint_url: str
    region: str
    bucket: str
    prefix: str
    key_id_file: str
    secret_key_file: str
    recipients_file: str | None  # needed to upload; the drill host needs the identity instead
    state_dir: str


def load_offsite_config(environ: dict | None = None, *, need_recipients: bool = True) -> OffsiteConfig:
    environ = dict(os.environ if environ is None else environ)
    missing = [n for n in OFFSITE_REQUIRED if not environ.get(n, "").strip()]
    if need_recipients and not environ.get("NAPSEDGE_BACKUP_RECIPIENTS_FILE", "").strip():
        missing.append("NAPSEDGE_BACKUP_RECIPIENTS_FILE")
    if not environ.get("NAPSEDGE_BACKUP_STATE_DIR", "").strip():
        missing.append("NAPSEDGE_BACKUP_STATE_DIR")
    if missing:
        raise OperationsConfigurationError("required off-site configuration is missing: " + ", ".join(missing))
    value = {n: environ[n].strip() for n in OFFSITE_REQUIRED}
    errors = []
    if not value["NAPSEDGE_BACKUP_OFFSITE_ENDPOINT_URL"].startswith("https://") and not environ.get("NAPSEDGE_BACKUP_OFFSITE_ALLOW_HTTP_FOR_TESTS") == "1":
        errors.append("NAPSEDGE_BACKUP_OFFSITE_ENDPOINT_URL must be https://")
    if "@" in value["NAPSEDGE_BACKUP_OFFSITE_ENDPOINT_URL"]:
        errors.append("NAPSEDGE_BACKUP_OFFSITE_ENDPOINT_URL must not carry credentials")
    if not _BUCKET.fullmatch(value["NAPSEDGE_BACKUP_OFFSITE_BUCKET"]):
        errors.append("NAPSEDGE_BACKUP_OFFSITE_BUCKET is not a valid bucket name")
    if not _PREFIX.fullmatch(value["NAPSEDGE_BACKUP_OFFSITE_PREFIX"]) or "//" in value["NAPSEDGE_BACKUP_OFFSITE_PREFIX"] or value["NAPSEDGE_BACKUP_OFFSITE_PREFIX"].endswith("/"):
        errors.append("NAPSEDGE_BACKUP_OFFSITE_PREFIX must be a plain relative prefix")
    for name in ("NAPSEDGE_BACKUP_OFFSITE_KEY_ID_FILE", "NAPSEDGE_BACKUP_OFFSITE_SECRET_KEY_FILE", "NAPSEDGE_BACKUP_RECIPIENTS_FILE", "NAPSEDGE_BACKUP_STATE_DIR"):
        raw = environ.get(name, "").strip()
        if raw and not os.path.isabs(raw):
            errors.append(f"{name} must be an absolute path")
    if errors:
        raise OperationsConfigurationError("; ".join(errors))
    recipients = environ.get("NAPSEDGE_BACKUP_RECIPIENTS_FILE", "").strip() or None
    return OffsiteConfig(value["NAPSEDGE_BACKUP_OFFSITE_ENDPOINT_URL"], value["NAPSEDGE_BACKUP_OFFSITE_REGION"], value["NAPSEDGE_BACKUP_OFFSITE_BUCKET"],
                         value["NAPSEDGE_BACKUP_OFFSITE_PREFIX"], value["NAPSEDGE_BACKUP_OFFSITE_KEY_ID_FILE"], value["NAPSEDGE_BACKUP_OFFSITE_SECRET_KEY_FILE"],
                         recipients, environ["NAPSEDGE_BACKUP_STATE_DIR"].strip())
