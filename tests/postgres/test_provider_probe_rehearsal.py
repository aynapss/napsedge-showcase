"""
Phase 9B-7B: LOCAL rehearsal of the live-provider probe runbook (docs/phase9b_provider_probe_runbook.md).

The "provider" is one disposable, verified-TLS + SCRAM PostgreSQL 16 cluster with NO NapsEdge roles (the accepted
`launched_cluster`). Its superuser stands in for nothing in the probe: it only prepares the stand-in provider ADMIN --
a NOSUPERUSER role with CREATEROLE and CREATEDB, the shape a managed provider's admin typically has -- and observes
afterwards. Every probe step runs exactly as the runbook prescribes: the probe-tooling image (read-only root, uid 10001,
no capabilities, the repository mounted read-only), credential and CA files on read-only volumes, and the retained backup
image for the capture, with a job credential volume holding ONLY the backup role's password.

What a pass here shows: the runbook works end to end against this local stand-in. It does NOT show managed-provider
compatibility (the differences are listed in the runbook). Runs only when `NAPSEDGE_BACKUP_IMAGE` and
`NAPSEDGE_PROBE_TOOLING_IMAGE` name built images. Every container and volume is labelled and removed.
"""
from __future__ import annotations

import io
import json
import os
import secrets
import subprocess
import tarfile
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.postgres.conftest import TEST_DB_PREFIX
from tests.postgres.pg_restore_compare import acl_snapshot, all_fingerprints
from tests.postgres.provider_probe import PROBE_ROLES, _plain
from tests.postgres.test_postgres_backup_sequence_consistency import read_sequence_details
from tests.postgres.test_postgres_fresh_cluster_recovery import launched_cluster, owned_resources

BACKUP_IMAGE = os.environ.get("NAPSEDGE_BACKUP_IMAGE", "")
TOOLING_IMAGE = os.environ.get("NAPSEDGE_PROBE_TOOLING_IMAGE", "")
pytestmark = [pytest.mark.postgres, pytest.mark.skipif(not (BACKUP_IMAGE and TOOLING_IMAGE),
                                                       reason="set NAPSEDGE_BACKUP_IMAGE and NAPSEDGE_PROBE_TOOLING_IMAGE to rehearse the probe")]

LABEL = "org.napsedge.unit=9b7b"
REPO = Path.cwd()
ADMIN_ROLE = "napsedge_probe_admin"  # the stand-in provider admin (never one of the probe's roles)
CONFIG_PATH = "/probe/config/probe.json"
REVISION = "373b35452bb47f71d5f85cbca1e0624a9e4139f3"


def docker(*args: str, stdin: bytes | None = None, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], input=stdin if stdin is not None else b"", capture_output=True, timeout=timeout)


def helper(mounts: list[str], script: str, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    """A root helper from the tooling image: prepares or reads a volume (never touches the database)."""
    return docker("run", "--rm", "-i", "--network", "none", "--user", "0:0", "--label", LABEL, *mounts, "--entrypoint", "sh", TOOLING_IMAGE, "-c", script,
                  stdin=stdin)


class Rehearsal:
    def __init__(self, cluster, hostaddr: str, admin_password: str) -> None:
        self.cluster, self.hostaddr = cluster, hostaddr
        self.tag = uuid.uuid4().hex[:10]
        self.passwords = {"admin": admin_password, "migrator": secrets.token_urlsafe(24), "runtime": secrets.token_urlsafe(24),
                          "backup": secrets.token_urlsafe(24)}
        self.volumes = {k: f"napsedge-9b7b-{k}-{self.tag}" for k in ("config", "cred", "jobcred", "ca", "evidence", "store", "scratch")}
        self.source = f"{TEST_DB_PREFIX}probe_src_{self.tag}"
        self.target = f"{TEST_DB_PREFIX}probe_tgt_{self.tag}"
        self.outputs: list[str] = []

    @property
    def secrets(self) -> tuple[str, ...]:
        return (*self.passwords.values(), self.cluster.admin_password)

    def config(self) -> dict:
        return {
            "endpoint": {"host": "localhost", "hostaddr": self.hostaddr, "port": 5432, "admin_database": self.cluster.admin_url.database,
                         "ca_file": "/probe/ca/ca.pem"},
            "admin_user": ADMIN_ROLE,
            "credential_files": {k: f"/probe/credentials/{k}" for k in ("admin", "migrator", "runtime", "backup")},
            "source_database": self.source, "target_database": self.target, "evidence_dir": "/probe/evidence",
            "job": {"git_revision": REVISION, "git_dirty": True, "tooling_version": "0.0.0-probe-rehearsal",
                    "container_ca_file": "/run/napsedge-backup/ca/ca.pem", "container_password_file": "/run/napsedge-backup/credentials/backup",
                    "container_store_dir": "/var/lib/napsedge-backup/store"},
        }

    def prepare(self) -> None:
        for volume in self.volumes.values():
            assert docker("volume", "create", "--label", LABEL, volume).returncode == 0
        v = self.volumes
        assert helper(["-v", f"{v['config']}:/v"], "umask 022 && cat > /v/probe.json", json.dumps(self.config()).encode()).returncode == 0
        for key, password in self.passwords.items():
            assert helper(["-v", f"{v['cred']}:/v"], f"umask 077 && cat > /v/{key} && chown 10001:10001 /v/{key} && chmod 0400 /v/{key}",
                          password.encode() + b"\n").returncode == 0
        assert helper(["-v", f"{v['jobcred']}:/v"], "umask 077 && cat > /v/backup && chown 10001:10001 /v/backup && chmod 0400 /v/backup",
                      self.passwords["backup"].encode() + b"\n").returncode == 0
        ca = Path(self.cluster.ca_path).read_bytes()
        assert helper(["-v", f"{v['ca']}:/v"], "umask 022 && cat > /v/ca.pem", ca).returncode == 0
        assert helper(["-v", f"{v['evidence']}:/e", "-v", f"{v['store']}:/s", "-v", f"{v['scratch']}:/t"],
                      "chown 10001:10001 /e /s /t && chmod 0700 /e /s /t").returncode == 0

    def probe_argv(self, step: str, *extra: str) -> list[str]:
        v = self.volumes
        return ["run", "--rm", "--read-only", "--tmpfs", "/tmp:rw,mode=1777", "--user", "10001:10001", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--network", "bridge", "--label", LABEL, "-e", "PYTHONDONTWRITEBYTECODE=1",
                "-v", f"{v['config']}:/probe/config:ro", "-v", f"{v['cred']}:/probe/credentials:ro", "-v", f"{v['ca']}:/probe/ca:ro",
                "-v", f"{v['evidence']}:/probe/evidence", "-v", f"{v['store']}:/probe/store:ro",
                "--mount", f"type=bind,source={REPO},target=/repo,readonly", TOOLING_IMAGE, "--config", CONFIG_PATH, step, *extra]

    def probe(self, step: str, *extra: str) -> tuple[int, dict, str]:
        result = docker(*self.probe_argv(step, *extra), timeout=900)
        out, err = result.stdout.decode(errors="replace"), result.stderr.decode(errors="replace")
        self.outputs += [out, err]
        lines = [line for line in out.splitlines() if line.startswith("{")]
        return result.returncode, (json.loads(lines[-1]) if lines else {}), err

    def evidence(self) -> dict[str, str]:
        result = helper(["-v", f"{self.volumes['evidence']}:/e:ro"], "cd /e && tar -cf - .")
        assert result.returncode == 0
        files = {}
        with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
            for member in archive.getmembers():
                if member.isfile():
                    files[member.name.removeprefix("./")] = archive.extractfile(member).read().decode()
        return files

    def record(self, name: str) -> dict:
        return json.loads(self.evidence()[f"{name}.json"])

    def run_job(self, env_file: Path) -> tuple[int, dict, str]:
        v = self.volumes
        result = docker("run", "--rm", "--read-only", "--user", "10001:10001", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                        "--network", "bridge", "--label", LABEL, "-v", f"{v['jobcred']}:/run/napsedge-backup/credentials:ro",
                        "-v", f"{v['ca']}:/run/napsedge-backup/ca:ro", "-v", f"{v['scratch']}:/var/lib/napsedge-backup/scratch",
                        "-v", f"{v['store']}:/var/lib/napsedge-backup/store", "--env-file", str(env_file), "-e", "NAPSEDGE_LOG_LEVEL=WARNING",
                        BACKUP_IMAGE, timeout=900)
        out, err = result.stdout.decode(errors="replace"), result.stderr.decode(errors="replace")
        self.outputs += [out, err]
        return result.returncode, (json.loads(out.strip().splitlines()[-1]) if out.strip() else {}), err

    def scratch_entries(self) -> list[str]:
        return helper(["-v", f"{self.volumes['scratch']}:/t:ro"], "ls -A /t").stdout.decode().split()

    def assert_no_secret_anywhere(self) -> None:
        texts = [*self.outputs, *self.evidence().values()]
        for secret in self.secrets:
            assert not any(secret in t for t in texts), "a credential appeared in probe output or evidence"

    def remove(self) -> None:
        for volume in self.volumes.values():
            docker("volume", "rm", "-f", volume)


@pytest.fixture(scope="module")
def cluster(docker_available):
    with owned_resources() as ledger, launched_cluster(ledger) as (cluster, container_id):
        networks = docker("inspect", container_id, "--format", "{{json .NetworkSettings.Networks}}").stdout
        hostaddr = json.loads(networks)["bridge"]["IPAddress"]
        admin_password = secrets.token_urlsafe(24)
        engine = cluster.admin_engine()
        try:
            with engine.connect() as conn:  # SETUP of the stand-in provider admin (by the cluster's superuser)
                conn.execute(text(f"CREATE ROLE {ADMIN_ROLE} LOGIN PASSWORD '{admin_password}' NOSUPERUSER CREATEROLE CREATEDB NOREPLICATION NOBYPASSRLS"))
        finally:
            engine.dispose()
        yield cluster, hostaddr, admin_password


@pytest.fixture()
def rehearsal(cluster):
    run = Rehearsal(*cluster)
    run.prepare()
    try:
        yield run
    finally:
        run.remove()
        leftovers = docker("ps", "-aq", "--filter", f"label={LABEL}").stdout.split()
        assert leftovers == [], "a labelled container was left behind"


def superuser_scalar(cluster, sql: str, database: str | None = None, **params):
    engine = cluster.admin_engine(database)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql), params).scalar()
    finally:
        engine.dispose()


def probe_resources(cluster, run: Rehearsal) -> dict:
    return {"roles": superuser_scalar(cluster, "SELECT count(*) FROM pg_roles WHERE rolname = ANY(CAST(:r AS text[]))", r=list(PROBE_ROLES)),
            "databases": superuser_scalar(cluster, "SELECT count(*) FROM pg_database WHERE datname IN (:s, :t)", s=run.source, t=run.target)}


def test_a_the_runbook_runs_from_configuration_to_cleanup(rehearsal, tmp_path):
    run, cluster = rehearsal, rehearsal.cluster
    code, line, _ = run.probe("inspect")
    assert (code, line["outcome"]) == (0, "pass"), line
    inspect = run.record("inspect")["facts"]
    assert inspect["admin_role"] == {"superuser": False, "createrole": True, "createdb": True} and inspect["ssl"] is True
    assert inspect["server_version_num"] // 10000 == 16

    code, line, _ = run.probe("setup", "--confirm", run.source)
    assert (code, line["outcome"]) == (0, "pass"), (line, run.record("setup"))
    assert [e["name"] for e in json.loads(run.evidence()["ledger.json"])] == [*PROBE_ROLES, run.source]
    roles = superuser_scalar(cluster, "SELECT json_agg(json_build_array(rolname, rolsuper, rolcreatedb, rolcreaterole) ORDER BY rolname) "
                                      "FROM pg_roles WHERE rolname = ANY(CAST(:r AS text[]))", r=list(PROBE_ROLES))
    assert all(not s and not d and not c for _, s, d, c in roles), "the probe roles are NOSUPERUSER NOCREATEDB NOCREATEROLE"
    assert superuser_scalar(cluster, "SELECT count(*) FROM pg_authid WHERE rolname = ANY(CAST(:r AS text[])) AND rolpassword LIKE 'SCRAM-SHA-256$%'",
                            r=list(PROBE_ROLES)) == 3
    membership = superuser_scalar(cluster, "SELECT json_agg(json_build_array(r.rolname, m.admin_option, m.inherit_option, m.set_option) ORDER BY r.rolname) "
                                           "FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid JOIN pg_roles u ON u.oid = m.member "
                                           "WHERE u.rolname = :a", a=ADMIN_ROLE)
    per_role = {role: {"admin": any(r[1] for r in membership if r[0] == role), "inherit": any(r[2] for r in membership if r[0] == role),
                       "set": any(r[3] for r in membership if r[0] == role)} for role in PROBE_ROLES}
    # PostgreSQL 16: ADMIN on every role the admin created (automatic); SET only on the migrator (the probe's one explicit
    # session-scoped self-grant, needed by CREATE DATABASE ... OWNER); INHERIT on none, so no access to their objects
    assert per_role == {"napsedge_migrator": {"admin": True, "inherit": False, "set": True},
                        "napsedge_runtime_ro": {"admin": True, "inherit": False, "set": False},
                        "napsedge_backup_ro": {"admin": True, "inherit": False, "set": False}}, membership

    code, line, _ = run.probe("identity")
    identity = run.record("identity")["facts"]
    assert (code, line["outcome"]) == (0, "pass"), identity
    assert identity["system_identifier"] == str(superuser_scalar(cluster, "SELECT system_identifier FROM pg_control_system()"))
    assert identity["database_oid"] == superuser_scalar(cluster, "SELECT oid FROM pg_database WHERE datname = :d", d=run.source)
    assert identity["same_backend_across_transactions"] and identity["accepted"] and identity["ssl"]
    assert identity["own_backend_termination"] == {"terminated": True, "backend_gone": True}
    assert inspect["connection_headroom_observed"]["max_connections"] > 0
    env_file = tmp_path / "job.env"
    env_file.write_text(run.evidence()["job.env"], encoding="utf-8")

    code, report, err = run.run_job(env_file)
    assert (code, report.get("outcome")) == (0, "published"), (report, err[-500:])
    assert run.scratch_entries() == [], "the job's scratch is empty afterwards"

    holder = subprocess.Popen(["docker", *run.probe_argv("hold-lock", "--seconds", "45")], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 120
        while "hold_lock.held" not in run.evidence():
            assert time.monotonic() < deadline and holder.poll() is None, "the lock holder never reported the lock as held"
            time.sleep(1)
        overlap_code, overlap, _ = run.run_job(env_file)
    finally:
        out, err = holder.communicate(timeout=180)
        run.outputs += [out.decode(errors="replace"), err.decode(errors="replace")]
    assert (overlap_code, overlap.get("outcome")) == (3, "skipped_overlap"), overlap
    assert holder.returncode == 0 and run.record("hold_lock")["facts"]["released_and_session_verified_gone"]

    artifact = f"/probe/store/{report['artifact']}"
    code, line, _ = run.probe("verify-restore", "--confirm", run.target, "--artifact", artifact)
    restored = run.record("verify_restore")["facts"]
    assert (code, line["outcome"]) == (0, "pass"), restored
    assert restored["artifact"]["format"] == "napsedge.backup-artifact/2" and restored["restore_exit"] == 0
    assert all(restored["strict_verdict"].values()) and restored["strict_sequences_equal"] and restored["monotonic_v1_policy_accepts"]
    assert restored["client_processes_after"] == [] and restored["scratch_removed"]
    # the comparison reader is the target's owner; what it read equals what the cluster's superuser reads
    engine = cluster.admin_engine(run.target)
    try:
        superuser_reads = {"fingerprints": all_fingerprints(engine), "acls": acl_snapshot(engine), "sequences": [_plain(d) for d in read_sequence_details(engine)]}
    finally:
        engine.dispose()
    assert json.loads(json.dumps(superuser_reads, default=str)) == restored["restored_reads"]

    code, line, _ = run.probe("verify-restore", "--confirm", run.target, "--artifact", artifact)  # REFUSAL: the target now exists
    assert (code, line["outcome"]) == (3, "refused") and "already exists" in line["reason"], line

    code, line, _ = run.probe("residue")
    assert (code, line["outcome"]) == (0, "pass"), run.record("residue")
    code, line, _ = run.probe("cleanup", "--confirm", "cleanup")
    assert (code, line["outcome"]) == (0, "pass"), run.record("cleanup")
    assert probe_resources(cluster, run) == {"roles": 0, "databases": 0}
    assert json.loads(run.evidence()["ledger.json"]) == []
    assert "PASSWORD" not in run.evidence()["job.env"].upper().replace("DATABASE_PASSWORD_FILE", "")
    run.assert_no_secret_anywhere()


def test_b_a_provider_restriction_is_a_blocker_and_nothing_is_substituted(rehearsal):
    run, cluster = rehearsal, rehearsal.cluster
    assert run.probe("setup", "--confirm", run.source)[0] == 0
    engine = cluster.admin_engine(run.source)  # function privileges are per database: the restriction is applied in the source
    try:
        with engine.connect() as conn:  # NEGATIVE-CONTROL SETUP: the restriction a provider might impose
            conn.execute(text("REVOKE EXECUTE ON FUNCTION pg_catalog.pg_control_system() FROM PUBLIC"))
        try:
            code, line, _ = run.probe("identity")
        finally:
            with engine.connect() as conn:
                conn.execute(text("GRANT EXECUTE ON FUNCTION pg_catalog.pg_control_system() TO PUBLIC"))
    finally:
        engine.dispose()
    facts = run.record("identity")["facts"]
    assert (code, line["outcome"]) == (2, "blocker") and "pg_control_system" in line["reason"], line
    assert facts["system_identifier_readable"] is False and facts["system_identifier"] is None
    assert "job.env" not in run.evidence(), "no job configuration is written without a source identity the backup role read"
    assert superuser_scalar(cluster, "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member WHERE r.rolname = 'napsedge_backup_ro'") == 0
    assert run.probe("cleanup", "--confirm", "cleanup")[0] == 0 and probe_resources(cluster, run) == {"roles": 0, "databases": 0}
    run.assert_no_secret_anywhere()


def test_c_an_unexpected_existing_resource_is_refused_and_left_untouched(rehearsal):
    run, cluster = rehearsal, rehearsal.cluster
    engine = cluster.admin_engine()
    try:
        with engine.connect() as conn:  # an unrelated, pre-existing role with a probe role's name
            conn.execute(text("CREATE ROLE napsedge_runtime_ro NOLOGIN"))
        oid = superuser_scalar(cluster, "SELECT oid FROM pg_roles WHERE rolname = 'napsedge_runtime_ro'")
        code, line, _ = run.probe("setup", "--confirm", run.source)
        assert (code, line["outcome"]) == (3, "refused"), line
        assert superuser_scalar(cluster, "SELECT oid FROM pg_roles WHERE rolname = 'napsedge_runtime_ro'") == oid, "the existing role is untouched"
        assert probe_resources(cluster, run) == {"roles": 1, "databases": 0} and "ledger.json" not in run.evidence()
        code, line, _ = run.probe("cleanup", "--confirm", "cleanup")
        assert code == 0 and superuser_scalar(cluster, "SELECT oid FROM pg_roles WHERE rolname = 'napsedge_runtime_ro'") == oid, "cleanup never drops what it did not create"
    finally:
        with engine.connect() as conn:
            conn.execute(text("DROP ROLE IF EXISTS napsedge_runtime_ro"))
        engine.dispose()
    run.assert_no_secret_anywhere()
