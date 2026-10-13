"""
Phase 9B-7A: the BUILT backup image (`docker build --target backup`), run against a disposable PostgreSQL 16 cluster.

The image named by `NAPSEDGE_BACKUP_IMAGE` runs exactly as a production container would: `--read-only` root filesystem,
uid 10001, all capabilities dropped, `no-new-privileges`, NO Docker socket, NO test-transport setting, and its own real
PostgreSQL 16 `pg_dump`/`pg_restore`. The credential and the CA certificate are READ-ONLY mounts (named volumes written
through stdin by a helper, so the synthetic password never appears in any argv); scratch and the artifact store are
writable named volumes, so their contents can be inspected afterwards. The container reaches the cluster through the
canonical URL (`host=localhost` for verify-full, `hostaddr` = the cluster container's bridge address, port 5432).

The orchestrating test process may run on Windows; everything the job does happens inside the Linux image. Skipped
unless `NAPSEDGE_BACKUP_IMAGE` names an image. Every volume this module creates is labelled and removed by it.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
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
from tests.postgres.test_postgres_backup_capture_lock import designation_for, harness, record  # noqa: F401  (harness: module fixture)
from tests.postgres.test_postgres_backup_capture_roundtrip import case  # noqa: F401  (case: fixture)
from tests.postgres.test_postgres_backup_capture_snapshot import admin_scalar
from tests.postgres.test_postgres_tls_backup_restore import _container_id

IMAGE = os.environ.get("NAPSEDGE_BACKUP_IMAGE", "")
pytestmark = [pytest.mark.postgres, pytest.mark.skipif(not IMAGE, reason="set NAPSEDGE_BACKUP_IMAGE to the built backup image to test it")]

LABEL = "org.napsedge.unit=9b7a"
CRED_DIR, CA_DIR = "/run/napsedge-backup/credentials", "/run/napsedge-backup/ca"
SCRATCH_DIR, STORE_DIR = "/var/lib/napsedge-backup/scratch", "/var/lib/napsedge-backup/store"
OWNED_APPS = (LOCK_APPLICATION_NAME, VERIFIER_APPLICATION_NAME, SNAPSHOT_APPLICATION_NAME, DUMP_VERIFIER_APPLICATION_NAME, OBSERVER_APPLICATION_NAME)
LOG_LINE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[(DEBUG|INFO|WARNING)\] [\w.]+: .*")
BYSTANDER = b"caller-owned file: never touched by the backup job\n"


def docker(*args: str, stdin: bytes | None = None, timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], input=stdin, capture_output=True, timeout=timeout)


def helper(volume_args: list[str], script: str, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    """A root helper container from the SAME image, used only to prepare or read test volumes (never the job itself)."""
    return docker("run", "--rm", "-i", "--network", "none", "--label", LABEL, "--user", "0:0", *volume_args, "--entrypoint", "sh", IMAGE, "-c", script,
                  stdin=stdin if stdin is not None else b"")  # never the test process's own stdin


@pytest.fixture()
def volumes(case):
    suffix = uuid.uuid4().hex[:12]
    names = {kind: f"napsedge-9b7a-{kind}-{suffix}" for kind in ("cred", "ca", "scratch", "store")}
    for name in names.values():
        assert docker("volume", "create", "--label", LABEL, name).returncode == 0
    try:
        ca = Path(case.cluster.ca_path).read_bytes()
        assert helper(["-v", f"{names['ca']}:/v"], 'umask 022 && cat > /v/ca.pem && chmod 0444 /v/ca.pem', stdin=ca).returncode == 0
        assert helper(["-v", f"{names['scratch']}:/s", "-v", f"{names['store']}:/t"], "chown 10001:10001 /s /t && chmod 0700 /s /t").returncode == 0
        assert helper(["-v", f"{names['store']}:/t"], "cat > /t/bystander.txt && chown 10001:10001 /t/bystander.txt", stdin=BYSTANDER).returncode == 0
        yield names
    finally:
        for name in names.values():
            docker("volume", "rm", "-f", name)
        leftover = docker("volume", "ls", "-q", "--filter", f"label={LABEL}").stdout.decode().split()
        assert not [v for v in leftover if v.endswith(suffix)], "a test volume was left behind"


def put_password(volumes: dict, password: bytes) -> None:
    """The synthetic password reaches the read-only credential volume through STDIN only (0400, owned by uid 10001)."""
    result = helper(["-v", f"{volumes['cred']}:/v"], 'umask 077 && cat > /v/db-password && chown 10001:10001 /v/db-password && chmod 0400 /v/db-password',
                    stdin=password)
    assert result.returncode == 0, result.stderr[-300:]


def cluster_ip(case) -> str:
    import docker as docker_sdk

    networks = docker_sdk.from_env().containers.get(_container_id(case.cluster)).attrs["NetworkSettings"]["Networks"]
    return networks["bridge"]["IPAddress"]


def job_env(case, **overrides) -> dict:
    url = URL.create("postgresql+psycopg", username=case.cluster.backup_url(case.database).username, host="localhost", port=5432, database=case.database,
                     query={"sslmode": "verify-full", "sslrootcert": f"{CA_DIR}/ca.pem", "hostaddr": cluster_ip(case)})
    env = {
        "NAPSEDGE_BACKUP_DATABASE_URL": url.render_as_string(hide_password=False),
        "NAPSEDGE_BACKUP_DATABASE_PASSWORD_FILE": f"{CRED_DIR}/db-password",
        "NAPSEDGE_BACKUP_TARGET_ACKNOWLEDGEMENT": build_expected_acknowledgement(case.database),
        "NAPSEDGE_BACKUP_EXPECTED_SYSTEM_IDENTIFIER": str(admin_scalar(case.admin, "SELECT system_identifier FROM pg_control_system()")),
        "NAPSEDGE_BACKUP_EXPECTED_DATABASE_OID": str(admin_scalar(case.admin, "SELECT oid FROM pg_database WHERE datname = current_database()")),
        "NAPSEDGE_BACKUP_STORE_DIR": STORE_DIR,
        "NAPSEDGE_BACKUP_LOGICAL_NAME": "napsedge",
        "NAPSEDGE_BACKUP_PG_DUMP": "/usr/lib/postgresql/16/bin/pg_dump",
        "NAPSEDGE_BACKUP_PG_RESTORE": "/usr/lib/postgresql/16/bin/pg_restore",
        "NAPSEDGE_BACKUP_GIT_REVISION": "373b35452bb47f71d5f85cbca1e0624a9e4139f3",
        "NAPSEDGE_BACKUP_GIT_DIRTY": "true",
        "NAPSEDGE_BACKUP_TOOLING_VERSION": "0.0.0-test",
        "NAPSEDGE_BACKUP_ARTIFACT_ID": str(uuid.uuid4()),
    }
    env.update(overrides)
    return env


def run_image(volumes: dict, env: dict) -> tuple[int, dict, subprocess.CompletedProcess, list[str]]:
    argv = ["run", "--rm", "--read-only", "--user", "10001:10001", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--network", "bridge",
            "--label", LABEL, "-v", f"{volumes['cred']}:{CRED_DIR}:ro", "-v", f"{volumes['ca']}:{CA_DIR}:ro", "-v", f"{volumes['scratch']}:{SCRATCH_DIR}",
            "-v", f"{volumes['store']}:{STORE_DIR}"]
    for key, value in env.items():
        argv += ["-e", f"{key}={value}"]
    argv.append(IMAGE)
    completed = docker(*argv, timeout=900)
    lines = completed.stdout.decode().strip().splitlines()
    assert len(lines) == 1, (completed.stdout[-500:], completed.stderr[-800:])
    return completed.returncode, json.loads(lines[0]), completed, argv


def store_contents(volumes: dict, destination: Path) -> list[str]:
    data = helper(["-v", f"{volumes['store']}:/s:ro"], "tar -C /s -cf - .").stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        archive.extractall(destination, filter="data")
    return sorted(os.listdir(destination))


def scratch_listing(volumes: dict) -> str:
    return helper(["-v", f"{volumes['scratch']}:/s:ro"], "ls -A /s").stdout.decode().strip()


def credential_digest(volumes: dict) -> str:
    return hashlib.sha256(helper(["-v", f"{volumes['cred']}:/v:ro"], "cat /v/db-password").stdout).hexdigest()


def assert_clean(case, volumes: dict, completed: subprocess.CompletedProcess, argv: list[str], password: bytes) -> dict:
    text = completed.stdout + completed.stderr
    assert password not in text and b"postgresql" not in text and b"Traceback" not in text
    for line in completed.stderr.decode().splitlines():
        assert LOG_LINE.fullmatch(line), f"stderr carries something other than a log line: {line[:120]!r}"
    assert not any("docker.sock" in a for a in argv) and not any("NAPSEDGE_TEST_CLIENT" in a for a in argv), "no socket, no test transport"
    assert not any(password.decode() in a for a in argv), "the password is in no argv"
    found = {"owned_sessions": {app: case.admin.sessions(app) for app in OWNED_APPS}, "lock_holders": case.admin.holders(case.key),
             "scratch": scratch_listing(volumes), "containers_left": docker("ps", "-aq", "--filter", f"label={LABEL}").stdout.decode().split()}
    assert all(v == [] for v in found["owned_sessions"].values()) and found["lock_holders"] == [], found
    assert found["scratch"] == "", "scratch (dump work directory, passfile, coordinator copy) is empty after the run"
    assert found["containers_left"] == [], found
    return found


# ---------------------------------------------------------------------------
def test_a_the_image_publishes_a_verified_v2_artifact_from_a_password_file(case, volumes, tmp_path, record_property):
    password = case.cluster.backup_url(case.database).password.encode()
    put_password(volumes, password + b"\n")  # with the usual terminal newline, removed by the file rule
    before = credential_digest(volumes)
    env = job_env(case)
    code, report, completed, argv = run_image(volumes, env)
    assert (code, report["outcome"]) == (0, "published"), report
    names = store_contents(volumes, tmp_path / "store")
    assert names == sorted(["bystander.txt", report["artifact"]]), names
    assert (tmp_path / "store" / "bystander.txt").read_bytes() == BYSTANDER, "the caller-owned file is untouched"
    verdict = verify_artifact_directory(tmp_path / "store" / report["artifact"])
    assert verdict.valid and verdict.manifest.format == "napsedge.backup-artifact/2" and verdict.manifest.files[0].sha256 == report["archive_sha256"]
    assert verdict.manifest.source.pg_dump_version.startswith("pg_dump (PostgreSQL) 16.15")
    assert credential_digest(volumes) == before, "the read-only credential is unchanged"
    left = assert_clean(case, volumes, completed, argv, password)
    record(record_property, "A_published", acquired=True, body_entered=True, transport="image", exit_code=code, report=report, leftovers=left)


def test_b_a_real_overlap_is_reported_as_a_skip(case, volumes, tmp_path, record_property):
    password = case.cluster.backup_url(case.database).password.encode()
    put_password(volumes, password)
    with backup_capture_lock(case.cluster.backup_url(case.database), designation_for(case.database)):
        code, report, completed, argv = run_image(volumes, job_env(case, NAPSEDGE_LOG_LEVEL="WARNING"))
    assert (code, report["outcome"]) == (3, "skipped_overlap"), report
    assert store_contents(volumes, tmp_path / "store") == ["bystander.txt"] and completed.stderr == b""
    left = assert_clean(case, volumes, completed, argv, password)
    record(record_property, "B_overlap", acquired=True, body_entered=True, transport="image", exit_code=code, report=report, leftovers=left)


def test_c_a_wrong_password_is_refused_before_any_artifact_exists(case, volumes, tmp_path, record_property):
    wrong = b"synthetic-wrong-password-" + uuid.uuid4().hex.encode()
    put_password(volumes, wrong)
    code, report, completed, argv = run_image(volumes, job_env(case))
    assert (code, report["outcome"]) == (1, "failed"), report
    assert report["artifact_record"] == {"partial_present": False, "failed_record": "none"}, "authentication fails at the lock, before the reservation"
    assert store_contents(volumes, tmp_path / "store") == ["bystander.txt"]
    left = assert_clean(case, volumes, completed, argv, wrong)
    record(record_property, "C_wrong_password", acquired=True, body_entered=True, transport="image", exit_code=code, report=report, leftovers=left)


def test_d_a_wrong_source_identity_is_refused_and_recorded(case, volumes, tmp_path, record_property):
    password = case.cluster.backup_url(case.database).password.encode()
    put_password(volumes, password)
    env = job_env(case, NAPSEDGE_BACKUP_EXPECTED_SYSTEM_IDENTIFIER="1")
    code, report, completed, argv = run_image(volumes, env)
    assert (code, report["outcome"], report["error_type"]) == (1, "failed", "DatabaseTargetVerificationError"), report
    assert report["artifact_record"]["failed_reason_class"] == "source_identity_mismatch"
    assert store_contents(volumes, tmp_path / "store") == sorted(["bystander.txt", f".partial-{env['NAPSEDGE_BACKUP_ARTIFACT_ID']}"])
    left = assert_clean(case, volumes, completed, argv, password)
    record(record_property, "D_wrong_source", acquired=True, body_entered=True, transport="image", exit_code=code, report=report, leftovers=left)


def test_e_the_image_runs_non_root_on_a_read_only_root_with_real_client_tools(record_property):
    probe = docker("run", "--rm", "--read-only", "--network", "none", "--label", LABEL, "--entrypoint", "sh", IMAGE, "-c",
                   'id -u; touch /app/probe 2>/dev/null && echo root-writable || echo root-read-only; '
                   'for t in pg_dump pg_restore; do f=/usr/lib/postgresql/16/bin/$t; dpkg -S "$f"; head -c 4 "$f" | od -An -c | tr -d " \\n"; echo; "$f" --version; done; '
                   'echo "TMPDIR=$TMPDIR"; test -S /var/run/docker.sock && echo socket || echo no-socket')
    lines = probe.stdout.decode().splitlines()
    assert probe.returncode == 0, probe.stderr[-300:]
    assert lines[0] == "10001" and lines[1] == "root-read-only"
    assert lines[2] == "postgresql-client-16: /usr/lib/postgresql/16/bin/pg_dump" and lines[3] == "177ELF" and lines[4].startswith("pg_dump (PostgreSQL) 16.15")
    assert lines[5] == "postgresql-client-16: /usr/lib/postgresql/16/bin/pg_restore" and lines[6] == "177ELF" and lines[7].startswith("pg_restore (PostgreSQL) 16.15")
    assert lines[8] == f"TMPDIR={SCRATCH_DIR}" and lines[9] == "no-socket"
    record(record_property, "E_image_facts", acquired=True, body_entered=True, transport="image", probe=lines)
