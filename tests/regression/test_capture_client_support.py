"""
Phase 9B-6G: the TEST-ONLY client-transport selection (tests/postgres/capture_client_support.py). No database, no Docker.
It pins what the native validation depends on: the default stays the historical container transport, `local` hands
production code the real `LocalClientHost` class (never a test subclass), the recording `Popen` subclass records the
client tools without changing behavior, and the conninfo parser inverts the production quoting rule exactly.
"""
from __future__ import annotations

import inspect
import subprocess
import sys

import pytest

import tests.postgres.capture_client_support as support
import tests.postgres.test_postgres_fresh_cluster_recovery as harness_module
from src.deployment.backup_capture_dump import LibpqTarget, LocalClientHost, _conninfo


@pytest.mark.parametrize("value, expected", [(None, False), ("container", False), ("local", True)])
def test_the_default_is_the_historical_container_transport_and_local_is_explicit(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(support.SELECTION_VARIABLE, raising=False)
    else:
        monkeypatch.setenv(support.SELECTION_VARIABLE, value)
    assert support.native_mode() is expected


def test_an_unknown_selection_is_refused(monkeypatch):
    monkeypatch.setenv(support.SELECTION_VARIABLE, "docker-exec-but-call-it-native")
    with pytest.raises(RuntimeError):
        support.native_mode()


def test_local_mode_hands_production_code_the_real_local_client_host_with_the_native_tool_paths():
    client = support.NativeTestClient(cluster=None)
    production = support.production_client(client)
    assert type(production) is LocalClientHost  # exactly the production class, not a subclass
    assert production.pg_dump == (f"{support.NATIVE_TOOL_DIR}/pg_dump",) and production.pg_restore == (f"{support.NATIVE_TOOL_DIR}/pg_restore",)
    assert client.localized is None and client.killed_returncode == -9
    other = object()
    assert support.production_client(other) is other  # the container transport is passed through unchanged


def test_the_recording_popen_records_only_the_client_tools_and_otherwise_behaves_like_popen():
    client = support.NativeTestClient(cluster=None, pg_dump=(sys.executable,), pg_restore=("/never/used",))
    tool = client.popen_class([sys.executable, "-c", "import sys; sys.exit(3)"], env={"PGPASSFILE": "/x/pgpass", "SYSTEMROOT": "C:\\Windows"} if sys.platform == "win32" else {"PGPASSFILE": "/x/pgpass"})
    assert tool.wait(timeout=30) == 3 and isinstance(tool, subprocess.Popen)
    other = client.popen_class(["git", "--version"], stdout=subprocess.DEVNULL)
    other.wait(timeout=30)
    assert [p.argv[0] for p in client.processes] == [sys.executable] and client.files == ["/x/pgpass"]
    assert client.calls == [f"start:{support.os.path.basename(sys.executable)}"] and client.dump_attempts() == []


def test_the_conninfo_parser_inverts_the_production_quoting_rule():
    target = LibpqTarget("localhost", "172.17.0.3", 5432, "db", "napsedge_backup_ro", "verify-full", "/tmp/a b/c'd\\e.crt", "15")
    parsed = support.parse_conninfo(["--format=custom", "--dbname=" + _conninfo(target, "app")])
    assert parsed == {"host": "localhost", "hostaddr": "172.17.0.3", "port": "5432", "dbname": "db", "user": "napsedge_backup_ro",
                      "sslmode": "verify-full", "sslrootcert": "/tmp/a b/c'd\\e.crt", "application_name": "app", "connect_timeout": "15"}


def test_the_harness_builds_the_runner_url_only_under_the_test_selection_and_keeps_verify_full_and_the_host_name():
    source = inspect.getsource(harness_module.launched_cluster)
    assert 'if os.environ.get("NAPSEDGE_TEST_CLIENT") == "local":' in source
    assert "admin_url.set(port=port).update_query_dict({\"hostaddr\":" in source  # only the network path changes; host and CA stay
    assert "admin_url = tls_url(port, material.ca_cert.as_posix(), admin_user, admin_password, admin_db)" in source  # the default is unchanged
    assert "NAPSEDGE_TEST_CLIENT" not in "".join(inspect.getsource(m) for m in _production_modules())


def _production_modules():
    import src.deployment.backup_capture_catalog as catalog
    import src.deployment.backup_capture_dump as dump
    import src.deployment.backup_capture_lock as lock
    import src.deployment.backup_capture_snapshot as snapshot

    return (catalog, dump, lock, snapshot)
