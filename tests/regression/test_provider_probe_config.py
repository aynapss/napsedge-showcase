"""
Phase 9B-7B: the provider probe's own preparation logic, without a database or Docker -- configuration refusals,
credential-file handling, the ledger's scope, evidence scrubbing and the job's env file. The probe's database behavior is
covered by `tests/postgres/test_provider_probe_rehearsal.py`.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.postgres import provider_probe as probe

SECRET = "hunter2-planted-probe-secret"


def base_config(tmp_path: Path) -> dict:
    return {
        "endpoint": {"host": "db.example.invalid", "hostaddr": "192.0.2.10", "port": 25060, "admin_database": "defaultdb", "ca_file": "/probe/ca/ca.pem"},
        "admin_user": "provider_admin",
        "credential_files": {k: f"/probe/credentials/{k}" for k in probe.CREDENTIAL_KEYS},
        "source_database": "napsedge_pg_test_probe_src", "target_database": "napsedge_pg_test_probe_tgt",
        "evidence_dir": "/probe/evidence",
        "job": {"git_revision": "0" * 40, "git_dirty": False, "tooling_version": "x", "container_ca_file": "/run/napsedge-backup/ca/ca.pem",
                "container_password_file": "/run/napsedge-backup/credentials/backup", "container_store_dir": "/var/lib/napsedge-backup/store"},
    }


def write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "probe.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_a_valid_placeholder_configuration_loads(tmp_path):
    config = probe.load_config(write(tmp_path, base_config(tmp_path)))
    assert (config.host, config.port, config.source_database, config.admin_user) == ("db.example.invalid", 25060, "napsedge_pg_test_probe_src", "provider_admin")


def _mutate(path: str, value):
    def apply(data):
        keys = path.split(".")
        node = data
        for key in keys[:-1]:
            node = node[key]
        if value is KeyError:
            del node[keys[-1]]
        else:
            node[keys[-1]] = value
        return data
    return apply


REFUSALS = {
    "inline_password_key": lambda d: {**d, "admin_password": SECRET},
    "missing_target": _mutate("target_database", KeyError),
    "unknown_endpoint_key": _mutate("endpoint.password", SECRET),
    "inline_credential_value": _mutate("credential_files.admin", SECRET),
    "relative_credential_path": _mutate("credential_files.backup", "credentials/backup"),
    "shared_credential_file": _mutate("credential_files.runtime", "/probe/credentials/admin"),
    "missing_credential_role": _mutate("credential_files.migrator", KeyError),
    "host_with_userinfo": _mutate("endpoint.host", f"admin:{SECRET}@db.example.invalid"),
    "hostaddr_not_ip": _mutate("endpoint.hostaddr", "db.example.invalid"),
    "port_string": _mutate("endpoint.port", "25060"),
    "non_disposable_source": _mutate("source_database", "napsedge"),
    "non_disposable_target": _mutate("target_database", "defaultdb"),
    "source_equals_target": _mutate("target_database", "napsedge_pg_test_probe_src"),
    "admin_is_a_probe_role": _mutate("admin_user", "napsedge_migrator"),
    "short_revision": _mutate("job.git_revision", "abc"),
    "dirty_not_boolean": _mutate("job.git_dirty", "false"),
    "relative_container_path": _mutate("job.container_password_file", "credentials/backup"),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_b_every_malformed_or_secret_bearing_configuration_is_refused_without_echoing_it(tmp_path, case):
    data = REFUSALS[case](copy.deepcopy(base_config(tmp_path)))
    with pytest.raises(probe.ProbeRefused) as excinfo:
        probe.load_config(write(tmp_path, data))
    assert SECRET not in str(excinfo.value)


def test_c_an_unreadable_configuration_is_refused(tmp_path):
    with pytest.raises(probe.ProbeRefused):
        probe.load_config(tmp_path / "absent.json")


def _config(tmp_path: Path, **overrides) -> probe.ProbeConfig:
    data = base_config(tmp_path)
    config = probe.load_config(write(tmp_path, data))
    files = {k: str(tmp_path / f"cred_{k}") for k in probe.CREDENTIAL_KEYS}
    values = {"credential_files": files, "evidence_dir": tmp_path, **overrides}
    return probe.ProbeConfig(**{**config.__dict__, **values})


def test_d_credentials_follow_the_jobs_password_file_rules_and_never_echo(tmp_path):
    config = _config(tmp_path)
    Path(config.credential_files["admin"]).write_bytes(SECRET.encode() + b"\n")
    if sys.platform != "win32":
        os.chmod(config.credential_files["admin"], 0o400)
    assert probe.read_secret(config, "admin") == SECRET  # one terminal LF removed, nothing else
    Path(config.credential_files["backup"]).write_bytes(SECRET.encode() + b"\r\n")
    with pytest.raises(probe.ProbeRefused) as excinfo:
        probe.read_secret(config, "backup")
    assert SECRET not in str(excinfo.value) and "credential_files.backup" in str(excinfo.value)
    with pytest.raises(probe.ProbeRefused) as excinfo:
        probe.read_secret(config, "runtime")  # missing file
    assert "credential_files.runtime" in str(excinfo.value)


def test_e_evidence_carrying_a_credential_is_never_written(tmp_path):
    config = _config(tmp_path)
    with pytest.raises(RuntimeError):
        probe.write_evidence(config, "x", {"detail": f"password {SECRET}"}, (SECRET,))
    assert not (tmp_path / "x.json").exists()
    assert json.loads(probe.write_evidence(config, "y", {"ok": True}, (SECRET,)).read_text())["ok"] is True


def test_f_the_ledger_only_ever_names_the_probes_own_resources(tmp_path):
    config = _config(tmp_path)
    probe.record_created(config, "role", "napsedge_backup_ro")
    probe.record_created(config, "database", config.source_database)
    assert [e["name"] for e in probe.read_ledger(config)] == ["napsedge_backup_ro", config.source_database]
    for foreign in ({"kind": "role", "name": "provider_admin"}, {"kind": "database", "name": "defaultdb"}, {"kind": "table", "name": "x"}):
        (tmp_path / probe.LEDGER).write_text(json.dumps([foreign]), encoding="utf-8")
        with pytest.raises(probe.ProbeRefused):
            probe.read_ledger(config)


def test_g_the_job_env_carries_no_password_and_only_backup_role_facts(tmp_path):
    config = _config(tmp_path)
    preflight = SimpleNamespace(system_identifier="7550123456789012345", database_oid=16384)
    env = probe.job_env(config, "192.0.2.10", preflight)
    values = dict(line.split("=", 1) for line in env.splitlines())
    assert values["NAPSEDGE_BACKUP_DATABASE_URL"].startswith("postgresql+psycopg://napsedge_backup_ro@db.example.invalid:25060/napsedge_pg_test_probe_src?")
    assert "sslmode=verify-full" in values["NAPSEDGE_BACKUP_DATABASE_URL"] and "hostaddr=192.0.2.10" in values["NAPSEDGE_BACKUP_DATABASE_URL"]
    assert values["NAPSEDGE_BACKUP_DATABASE_PASSWORD_FILE"] == "/run/napsedge-backup/credentials/backup"
    assert "NAPSEDGE_BACKUP_DATABASE_PASSWORD" not in values
    assert (values["NAPSEDGE_BACKUP_EXPECTED_SYSTEM_IDENTIFIER"], values["NAPSEDGE_BACKUP_EXPECTED_DATABASE_OID"]) == ("7550123456789012345", "16384")


def test_h_mutating_steps_require_their_explicit_confirmation(tmp_path):
    config = _config(tmp_path)
    for step, kwargs in ((probe.step_setup, {"confirm": "yes"}), (probe.step_verify_restore, {"confirm": config.source_database, "artifact": "/a"}),
                         (probe.step_cleanup, {"confirm": "yes"})):
        with pytest.raises(probe.ProbeRefused):
            step(config, None, SimpleNamespace(**{"seconds": 1, "artifact": "", **kwargs}))


def test_i_the_cli_reports_a_refusal_as_one_sanitized_line_with_exit_3(tmp_path, capsys):
    path = write(tmp_path, {**base_config(tmp_path), "admin_password": SECRET})
    assert probe.main(["--config", str(path), "inspect"]) == probe.REFUSED
    line = json.loads(capsys.readouterr().out.strip())
    assert line["outcome"] == "refused" and line["exit"] == 3 and SECRET not in json.dumps(line)
