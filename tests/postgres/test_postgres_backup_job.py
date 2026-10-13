"""
Phase 9B-6K: the REAL backup job command against a disposable PostgreSQL 16 cluster (synthetic data, the 9B-2 grants).

Each test runs `python -m src.deployment.entrypoints backup-capture` as a SUBPROCESS, configured only through its
environment (the password in its own variable; the URL without one), with real PostgreSQL 16.15 client executables through
the production `LocalClientHost`. The child's environment has NO `NAPSEDGE_TEST_CLIENT` (the job never reads it) and a
`TMPDIR` inside the case, so the coordinator's private scratch is observable. The report on stdout and the exit status are
judged, then the store is judged by the public verifier and on disk, and the cluster by the `case` fixture's own teardown
(no owned session, lock holder, client process or dump backend remains).

Requires `NAPSEDGE_TEST_CLIENT=local` on the accepted Linux runner (it selects the test-side transport; the child does not see it).
"""
from __future__ import annotations

import json
import re
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy.engine import URL

from src.deployment.backup_artifact import verify_artifact_directory
from src.deployment.backup_capture_catalog import OBSERVER_APPLICATION_NAME
from src.deployment.backup_capture_dump import DUMP_VERIFIER_APPLICATION_NAME
from src.deployment.backup_capture_lock import LOCK_APPLICATION_NAME, VERIFIER_APPLICATION_NAME, backup_capture_lock
from src.deployment.backup_capture_snapshot import SNAPSHOT_APPLICATION_NAME
from src.pipeline.bulk_import_safety import build_expected_acknowledgement
from tests.postgres.capture_client_support import NATIVE_TOOL_DIR, native_mode
from tests.postgres.test_postgres_backup_capture_lock import designation_for, harness, record  # noqa: F401  (harness: module fixture)
from tests.postgres.test_postgres_backup_capture_roundtrip import case  # noqa: F401  (case: fixture)
from tests.postgres.test_postgres_backup_capture_snapshot import admin_scalar

pytestmark = [pytest.mark.postgres,
              pytest.mark.skipif(not native_mode(), reason="requires the real PostgreSQL 16 client tools on the accepted Linux runner "
                                                           "(NAPSEDGE_TEST_CLIENT=local); the job uses the production LocalClientHost")]

REPO_ROOT = Path(__file__).resolve().parents[2]
LOG_LINE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[(DEBUG|INFO|WARNING)\] [\w.]+: .*")
OWNED_APPS = (LOCK_APPLICATION_NAME, VERIFIER_APPLICATION_NAME, SNAPSHOT_APPLICATION_NAME, DUMP_VERIFIER_APPLICATION_NAME, OBSERVER_APPLICATION_NAME)


def job_env(case, root: Path, **overrides) -> dict:
    url = case.cluster.backup_url(case.database)
    env = {
        # URL.set(password=None) leaves the password in place, so the password-free URL is built field by field
        "NAPSEDGE_BACKUP_DATABASE_URL": URL.create(url.drivername, username=url.username, host=url.host, port=url.port, database=url.database,
                                                   query=dict(url.query)).render_as_string(hide_password=False),
        "NAPSEDGE_BACKUP_DATABASE_PASSWORD": url.password,
        "NAPSEDGE_BACKUP_TARGET_ACKNOWLEDGEMENT": build_expected_acknowledgement(case.database),
        "NAPSEDGE_BACKUP_EXPECTED_SYSTEM_IDENTIFIER": str(admin_scalar(case.admin, "SELECT system_identifier FROM pg_control_system()")),
        "NAPSEDGE_BACKUP_EXPECTED_DATABASE_OID": str(admin_scalar(case.admin, "SELECT oid FROM pg_database WHERE datname = current_database()")),
        "NAPSEDGE_BACKUP_STORE_DIR": str(root / "store"),
        "NAPSEDGE_BACKUP_LOGICAL_NAME": "napsedge",
        "NAPSEDGE_BACKUP_PG_DUMP": f"{NATIVE_TOOL_DIR}/pg_dump",
        "NAPSEDGE_BACKUP_PG_RESTORE": f"{NATIVE_TOOL_DIR}/pg_restore",
        "NAPSEDGE_BACKUP_GIT_REVISION": "373b35452bb47f71d5f85cbca1e0624a9e4139f3",
        "NAPSEDGE_BACKUP_GIT_DIRTY": "true",
        "NAPSEDGE_BACKUP_TOOLING_VERSION": "0.0.0-test",
        "NAPSEDGE_BACKUP_ARTIFACT_ID": str(uuid.uuid4()),
    }
    env.update(overrides)
    return env


@pytest.fixture()
def root(case):
    base = case.host_dir / "job"
    (base / "store").mkdir(parents=True)
    (base / "tmp").mkdir()
    return base


def run_job(env: dict, root: Path) -> tuple[int, dict, subprocess.CompletedProcess]:
    child = {k: v for k, v in os.environ.items() if not k.startswith("NAPSEDGE_")}  # no NAPSEDGE_TEST_CLIENT, no inherited job settings
    child.update(env, TMPDIR=str(root / "tmp"))
    completed = subprocess.run([sys.executable, "-m", "src.deployment.entrypoints", "backup-capture"], cwd=REPO_ROOT, env=child,
                               capture_output=True, text=True, timeout=600)
    lines = completed.stdout.strip().splitlines()
    assert len(lines) == 1, (completed.stdout[-500:], completed.stderr[-500:])
    return completed.returncode, json.loads(lines[0]), completed


def assert_sanitized(case, env: dict, completed: subprocess.CompletedProcess) -> None:
    text = completed.stdout + completed.stderr
    assert env["NAPSEDGE_BACKUP_DATABASE_PASSWORD"] not in text and "postgresql" not in text and "Traceback" not in text
    for secret in case.cluster.secrets_to_scrub():
        assert secret not in text
    # stderr is diagnostic logging under the project's convention (src.logging_config, NAPSEDGE_LOG_LEVEL): only well-formed log lines
    for line in completed.stderr.splitlines():
        assert LOG_LINE.fullmatch(line), f"stderr carries something other than a log line: {line[:120]!r}"


def leftovers(case, root: Path) -> dict:
    found = {"owned_sessions": {app: case.admin.sessions(app) for app in OWNED_APPS}, "lock_holders": case.admin.holders(case.key),
             "client_processes": case.client.client_processes(), "scratch": sorted(os.listdir(root / "tmp"))}
    assert all(v == [] for v in found["owned_sessions"].values()) and found["lock_holders"] == [] and found["client_processes"] == [], found
    assert found["scratch"] == [], found
    return found


def test_a_a_successful_job_publishes_a_verified_v2_artifact(case, root, record_property):
    env = job_env(case, root)
    code, report, completed = run_job(env, root)
    assert (code, report["outcome"], report["exit_code"]) == (0, "published", 0), report
    artifact = root / "store" / report["artifact"]
    verdict = verify_artifact_directory(artifact)
    assert verdict.valid and verdict.manifest.format == "napsedge.backup-artifact/2" == report["manifest_format"]
    assert verdict.manifest.files[0].sha256 == report["archive_sha256"] and verdict.manifest.files[0].bytes == report["archive_bytes"]
    assert verdict.manifest.artifact_id == env["NAPSEDGE_BACKUP_ARTIFACT_ID"] == report["artifact_id"]
    assert (verdict.manifest.consistency.foreign_write_sessions_start, verdict.manifest.consistency.foreign_write_sessions_end) == (None, None)
    assert report["foreign_write_sessions"] == "not_sampled" and "recovery_readiness" in report["does_not_establish"]
    assert os.listdir(root / "store") == [report["artifact"]]
    assert_sanitized(case, env, completed)
    left = leftovers(case, root)
    record(record_property, "A_published", acquired=True, body_entered=True, transport="local", exit_code=code, report=report, leftovers=left)


def test_b_a_real_overlapping_job_reports_a_skip_and_creates_nothing(case, root, record_property):
    env = job_env(case, root, NAPSEDGE_LOG_LEVEL="WARNING")  # the project's own log-level setting: stderr is then empty
    with backup_capture_lock(case.cluster.backup_url(case.database), designation_for(case.database)):
        code, report, completed = run_job(env, root)
    assert (code, report["outcome"]) == (3, "skipped_overlap"), report
    assert completed.stderr == "", "with NAPSEDGE_LOG_LEVEL=WARNING nothing but the stdout report is written"
    assert os.listdir(root / "store") == [] and os.listdir(root / "tmp") == []
    assert_sanitized(case, env, completed)
    left = leftovers(case, root)
    record(record_property, "B_overlap", acquired=True, body_entered=True, transport="local", exit_code=code, report=report, leftovers=left)


@pytest.mark.parametrize("variant", ["missing_pg_dump", "wrong_source_identity"])
def test_c_a_representative_failure_reports_failed_with_the_recorded_class(case, root, record_property, variant):
    overrides = {"NAPSEDGE_BACKUP_PG_DUMP": "/nonexistent/pg_dump"} if variant == "missing_pg_dump" else {"NAPSEDGE_BACKUP_EXPECTED_SYSTEM_IDENTIFIER": "1"}
    env = job_env(case, root, **overrides)
    code, report, completed = run_job(env, root)
    expected = ("DumpPreconditionError", "dump_failed") if variant == "missing_pg_dump" else ("DatabaseTargetVerificationError", "source_identity_mismatch")
    assert (code, report["outcome"], report["error_type"]) == (1, "failed", expected[0]), report
    assert report["artifact_record"] == {"partial_present": True, "partial": f".partial-{env['NAPSEDGE_BACKUP_ARTIFACT_ID']}", "failed_record": "present",
                                         "failed_reason_class": expected[1]}
    assert os.listdir(root / "store") == [report["artifact_record"]["partial"]], "nothing was published"
    assert_sanitized(case, env, completed)
    left = leftovers(case, root)
    record(record_property, f"C_{variant}", acquired=True, body_entered=True, transport="local", exit_code=code, report=report, leftovers=left)
