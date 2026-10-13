"""
Phase 9B-8C (opt-in; NOT a test of behaviour): hold ONE disposable, synthetic PostgreSQL 16 source open for the local WSL systemd
rehearsal (`deploy/rehearsal/`), then tear it down. It reuses the accepted fixtures (TLS cluster, the three roles, migrations,
synthetic rows) exactly as the image tests do, so nothing here is a new database path.

It writes into NAPSEDGE_REHEARSAL_DIR:
  backup.env    the job's NON-secret configuration for the systemd unit (container paths; off-site disabled; no password)
  ca.pem        the disposable cluster's CA certificate (public)
  db-password   the backup role's SYNTHETIC password for this throwaway cluster only (0600; the installer moves it into a systemd
                encrypted credential through stdin and deletes it)
  READY         written last
and then waits until RELEASE appears there (or 2 hours pass); the fixtures then drop the cluster. The protected database volume
`bonnieswebscraping_napsedge-local-db-data` is never used.
Run:  NAPSEDGE_REHEARSAL_HOLD=1 NAPSEDGE_REHEARSAL_DIR=<dir> python -m pytest tests/postgres/test_wsl_rehearsal_source.py -m postgres -q -s
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from tests.postgres.test_postgres_backup_capture_lock import harness  # noqa: F401  (module fixture)
from tests.postgres.test_postgres_backup_capture_roundtrip import case  # noqa: F401  (fixture)
from tests.postgres.test_postgres_backup_image import job_env

HOLD = os.environ.get("NAPSEDGE_REHEARSAL_HOLD") == "1" and bool(os.environ.get("NAPSEDGE_REHEARSAL_DIR"))
pytestmark = [pytest.mark.postgres, pytest.mark.skipif(not HOLD, reason="opt-in WSL rehearsal source: NAPSEDGE_REHEARSAL_HOLD=1 and NAPSEDGE_REHEARSAL_DIR")]
MAX_HOLD_SECONDS = 2 * 3600


def test_hold_a_disposable_source_for_the_wsl_rehearsal(case):
    out = Path(os.environ["NAPSEDGE_REHEARSAL_DIR"])
    out.mkdir(parents=True, exist_ok=True)
    for stale in ("READY", "RELEASE", "backup.env", "ca.pem", "db-password"):
        (out / stale).unlink(missing_ok=True)
    env = job_env(case)
    env.pop("NAPSEDGE_BACKUP_ARTIFACT_ID")  # each cycle mints its own
    env.update({"NAPSEDGE_BACKUP_STATE_DIR": "/var/lib/napsedge-backup/state", "NAPSEDGE_BACKUP_OFFSITE_ENABLED": "false",
                "NAPSEDGE_BACKUP_RETENTION_DELETE_ENABLED": "false", "NAPSEDGE_BACKUP_TOOLING_VERSION": "9b8c-wsl-rehearsal"})
    from sqlalchemy.engine import make_url

    assert make_url(env["NAPSEDGE_BACKUP_DATABASE_URL"]).password is None, "the URL must carry no password"
    (out / "backup.env").write_bytes("".join(f"{k}={v}\n" for k, v in sorted(env.items())).encode())
    (out / "ca.pem").write_bytes(Path(case.cluster.ca_path).read_bytes())
    password = out / "db-password"
    password.write_bytes(case.cluster.backup_url(case.database).password.encode() + b"\n")
    os.chmod(password, 0o600)
    (out / "READY").write_text("ready\n", encoding="utf-8")
    deadline = time.monotonic() + MAX_HOLD_SECONDS
    try:
        while not (out / "RELEASE").exists() and time.monotonic() < deadline:
            time.sleep(5)
    finally:
        password.unlink(missing_ok=True)
        (out / "READY").unlink(missing_ok=True)
