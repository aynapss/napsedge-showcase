"""
Phase 9B-6K: the one-shot backup job boundary (`src/deployment/backup_job.py`, `entrypoints backup-capture`).

Configuration, request construction, outcome classification, exit codes and redaction are tested with the REAL production
types (`CaptureRequest`, `LocalClientHost`, `PublishedCapture`, `SkippedOverlap`, the coordinator and writer exception
classes). Only `run_guarded_capture` is replaced, through the job's own `capture` seam, by a recording stand-in that
returns or raises what the coordinator does; the real coordinator runs in
tests/postgres/test_postgres_backup_job.py. Partial directories are created on disk so the report is judged by what the
store actually holds.
"""
from __future__ import annotations

import ast
import datetime as dt
import errno
import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import src.deployment.backup_job as job
from src.deployment.backup_artifact_writer import DurabilityMode, DurabilityReport, PostPublicationError, PreconditionError, WriteFailed, WrittenArtifact
from src.deployment.backup_capture_coordinator import CaptureRequest, PublishedCapture, PublishedCaptureIncomplete, SkippedOverlap, StagedArchiveMismatch
from src.deployment.backup_capture_dump import DumpFailed, LocalClientHost
from src.exceptions import AdvisoryLockUnavailableError
from src.pipeline.bulk_import_safety import build_expected_acknowledgement

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET = "hunter2-planted-job-secret"
HOST = "db-secret-host.internal"
ARTIFACT_ID = "3f2b8c1e-6a47-4d0e-9b53-1c7a2d9e5f08"


def base_env(tmp_path: Path) -> dict:
    store = tmp_path / "store"
    store.mkdir(exist_ok=True)
    tools = tmp_path / "pg16" / "bin"
    return {
        "NAPSEDGE_BACKUP_DATABASE_URL": f"postgresql+psycopg://napsedge_backup_ro@{HOST}:5432/napsedge?sslmode=verify-full&sslrootcert=/run/ca.pem",
        "NAPSEDGE_BACKUP_DATABASE_PASSWORD": SECRET,
        "NAPSEDGE_BACKUP_TARGET_ACKNOWLEDGEMENT": build_expected_acknowledgement("napsedge"),
        "NAPSEDGE_BACKUP_EXPECTED_SYSTEM_IDENTIFIER": "7550123456789012345",
        "NAPSEDGE_BACKUP_EXPECTED_DATABASE_OID": "16384",
        "NAPSEDGE_BACKUP_STORE_DIR": str(store),
        "NAPSEDGE_BACKUP_LOGICAL_NAME": "napsedge",
        "NAPSEDGE_BACKUP_PG_DUMP": str(tools / "pg_dump"),
        "NAPSEDGE_BACKUP_PG_RESTORE": str(tools / "pg_restore"),
        "NAPSEDGE_BACKUP_GIT_REVISION": "373b35452bb47f71d5f85cbca1e0624a9e4139f3",
        "NAPSEDGE_BACKUP_GIT_DIRTY": "true",
        "NAPSEDGE_BACKUP_TOOLING_VERSION": "0.0.0-test",
        "NAPSEDGE_BACKUP_ARTIFACT_ID": ARTIFACT_ID,
    }


class Capture:
    """Stands in for `run_guarded_capture`: records each call, then returns or raises the configured outcome."""

    def __init__(self, outcome, before=None) -> None:
        self.outcome, self.before, self.calls = outcome, before, []

    def __call__(self, request, *, client):
        self.calls.append((request, client))
        if self.before is not None:
            self.before(request)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def run(env: dict, capture=None) -> tuple[int, dict, str]:
    out = io.StringIO()
    code = job.run_job(env, out=out, capture=capture)
    text = out.getvalue()
    assert text.count("\n") == 1, "exactly one report line"
    return code, json.loads(text), text


def written(store: Path) -> WrittenArtifact:
    path = store / f"napsedge-napsedge-20261007T000000Z-{ARTIFACT_ID}"
    return WrittenArtifact(path=str(path), artifact_id=ARTIFACT_ID, manifest_sha256="a" * 64, manifest_bytes=10, archive_sha256="b" * 64, archive_bytes=20,
                           baseline_sha256="c" * 64, baseline_bytes=30, durability=DurabilityReport("required", (), (), (), True), privacy="test")


def failed_partial(store: Path, reason: str | None) -> Path:
    partial = store / f".partial-{ARTIFACT_ID}"
    partial.mkdir()
    if reason is not None:
        (partial / "FAILED").write_text(json.dumps({"format": "napsedge.backup-failed/1", "artifact_id": ARTIFACT_ID, "utc": "x", "reason_class": reason}))
    else:
        (partial / "RUNNING").write_text("{}")
    return partial


def tree(path: Path) -> dict:
    return {str(p.relative_to(path)): (p.read_bytes() if p.is_file() else None) for p in sorted(path.rglob("*"))}


def assert_redacted(text: str) -> None:
    assert SECRET not in text and HOST not in text and "postgresql" not in text and "Traceback" not in text, text


# =========================================================================== configuration
def test_a_valid_configuration_builds_the_accepted_request_with_the_production_client(tmp_path):
    plan = job.build_plan(base_env(tmp_path))
    request, client = plan.request, plan.client
    assert type(request) is CaptureRequest and type(client) is LocalClientHost
    assert client.pg_dump == (str(tmp_path / "pg16" / "bin" / "pg_dump"),) and client.pg_restore == (str(tmp_path / "pg16" / "bin" / "pg_restore"),)
    assert request.database_url.password == SECRET and request.database_url.username == "napsedge_backup_ro"
    assert (request.expected_source.system_identifier, request.expected_source.database_oid, request.expected_source.database_name) == (
        "7550123456789012345", 16384, "napsedge")
    assert request.designation.expected_database_name == "napsedge" and request.durability is DurabilityMode.REQUIRED
    assert request.artifact_id == ARTIFACT_ID and request.repository.dirty is True and request.tooling.name == "napsedge-backup"
    from alembic.script import ScriptDirectory
    from src.services.health import _alembic_config

    assert [request.expected_alembic_head] == list(ScriptDirectory.from_config(_alembic_config()).get_heads()), "the repository's own head"


def test_a_optional_settings_have_documented_defaults(tmp_path):
    env = base_env(tmp_path)
    del env["NAPSEDGE_BACKUP_ARTIFACT_ID"]
    first, second = job.build_plan(env).request, job.build_plan(env).request
    assert first.artifact_id != second.artifact_id and len(first.artifact_id) == 36, "a fresh UUID4 per run when not supplied"
    env["NAPSEDGE_BACKUP_DURABILITY"] = "best_effort_reported"
    assert job.build_plan(env).request.durability is DurabilityMode.BEST_EFFORT_REPORTED


@pytest.mark.parametrize("name", job.REQUIRED)
def test_b_each_missing_required_variable_is_refused_by_name_before_capture(tmp_path, name):
    env = base_env(tmp_path)
    env[name] = "   "
    capture = Capture(SkippedOverlap(ARTIFACT_ID))
    code, report, text = run(env, capture)
    assert (code, report["outcome"]) == (64, "configuration_refused") and name in report["detail"] and capture.calls == []
    assert_redacted(text)


INVALID = [
    ("url_with_password", {"NAPSEDGE_BACKUP_DATABASE_URL": f"postgresql+psycopg://napsedge_backup_ro:{SECRET}@{HOST}:5432/napsedge?sslmode=verify-full&sslrootcert=/run/ca.pem"},
     "must not contain a password"),
    ("url_unparseable", {"NAPSEDGE_BACKUP_DATABASE_URL": f"::{HOST}::not a url"}, "not a valid database URL"),
    ("url_without_database", {"NAPSEDGE_BACKUP_DATABASE_URL": f"postgresql+psycopg://napsedge_backup_ro@{HOST}:5432"}, "must name the database"),
    ("url_not_verify_full", {"NAPSEDGE_BACKUP_DATABASE_URL": f"postgresql+psycopg://napsedge_backup_ro@{HOST}:5432/napsedge?sslmode=require"},
     "coordinator's validation"),
    ("wrong_acknowledgement", {"NAPSEDGE_BACKUP_TARGET_ACKNOWLEDGEMENT": "yes"}, "NAPSEDGE_BACKUP_TARGET_ACKNOWLEDGEMENT"),
    ("system_identifier_not_decimal", {"NAPSEDGE_BACKUP_EXPECTED_SYSTEM_IDENTIFIER": "abc"}, "NAPSEDGE_BACKUP_EXPECTED_SYSTEM_IDENTIFIER"),
    ("oid_not_decimal", {"NAPSEDGE_BACKUP_EXPECTED_DATABASE_OID": "-1"}, "NAPSEDGE_BACKUP_EXPECTED_DATABASE_OID"),
    ("store_missing", {"NAPSEDGE_BACKUP_STORE_DIR": "__missing__"}, "NAPSEDGE_BACKUP_STORE_DIR"),
    ("relative_pg_dump", {"NAPSEDGE_BACKUP_PG_DUMP": "pg_dump"}, "NAPSEDGE_BACKUP_PG_DUMP"),
    ("dirty_not_boolean", {"NAPSEDGE_BACKUP_GIT_DIRTY": "maybe"}, "NAPSEDGE_BACKUP_GIT_DIRTY"),
    ("unknown_durability", {"NAPSEDGE_BACKUP_DURABILITY": "fast"}, "NAPSEDGE_BACKUP_DURABILITY"),
    ("bad_artifact_id", {"NAPSEDGE_BACKUP_ARTIFACT_ID": "not-a-uuid"}, "coordinator's validation"),
    ("bad_logical_name", {"NAPSEDGE_BACKUP_LOGICAL_NAME": "Bad/Name"}, "coordinator's validation"),
    ("bad_git_revision", {"NAPSEDGE_BACKUP_GIT_REVISION": "abc"}, "coordinator's validation"),
]


@pytest.mark.parametrize("case, override, fragment", INVALID, ids=[i[0] for i in INVALID])
def test_b_invalid_configuration_is_refused_before_capture_without_echoing_values(tmp_path, case, override, fragment):
    env = base_env(tmp_path)
    env.update({k: (str(tmp_path / v) if v == "__missing__" else v) for k, v in override.items()})
    store_before = tree(tmp_path / "store")
    capture = Capture(SkippedOverlap(ARTIFACT_ID))
    code, report, text = run(env, capture)
    assert (code, report["outcome"]) == (64, "configuration_refused") and fragment in report["detail"], report
    assert capture.calls == [], "no capture is attempted on refused configuration"
    assert tree(tmp_path / "store") == store_before
    assert_redacted(text)


# =========================================================================== outcomes and exit codes
def test_c_published(tmp_path):
    env = base_env(tmp_path)
    capture = Capture(PublishedCapture(written=written(tmp_path / "store"), archive_sha256="b" * 64, archive_bytes=20, observation=None, cleanup={}))
    code, report, text = run(env, capture)
    assert (code, report["outcome"], report["exit_code"]) == (0, "published", 0)
    assert report["artifact"] == f"napsedge-napsedge-20261007T000000Z-{ARTIFACT_ID}" and report["manifest_format"] == "napsedge.backup-artifact/2"
    assert report["foreign_write_sessions"] == "not_sampled" and "recovery_readiness" in report["does_not_establish"]
    assert len(capture.calls) == 1 and type(capture.calls[0][1]) is LocalClientHost
    assert_redacted(text)


def test_c_skipped_overlap_is_not_a_failure(tmp_path):
    code, report, text = run(base_env(tmp_path), Capture(SkippedOverlap(ARTIFACT_ID)))
    assert (code, report["outcome"]) == (3, "skipped_overlap") and "not a failed backup" in report["meaning"]
    assert os.listdir(tmp_path / "store") == []


def test_c_failure_before_the_reservation_reports_no_artifact_record(tmp_path):
    error = PreconditionError("partial", f"PARTIAL_EXISTS {SECRET} {HOST}")
    code, report, text = run(base_env(tmp_path), Capture(error))
    assert (code, report["outcome"], report["error_type"]) == (1, "failed", "PreconditionError")
    assert report["artifact_record"] == {"partial_present": False, "failed_record": "none"} and report["published"] == "no"
    assert_redacted(text)


@pytest.mark.parametrize("error, reason", [
    (DumpFailed(f"pg_dump exited with status 1 {SECRET}", stage="dump", returncode=1, diagnostic=f"{HOST} {SECRET}"), "dump_failed"),
    (StagedArchiveMismatch("the preserved archive changed"), "checksum_mismatch"),
    (AdvisoryLockUnavailableError(f"{SECRET}"), "internal_error"),
])
def test_c_a_capture_failure_reports_the_failed_record_read_from_the_store(tmp_path, error, reason):
    env = base_env(tmp_path)
    store = tmp_path / "store"
    code, report, text = run(env, Capture(error, before=lambda request: failed_partial(store, reason)))
    assert (code, report["outcome"], report["error_type"]) == (1, "failed", type(error).__name__)
    assert report["artifact_record"] == {"partial_present": True, "partial": f".partial-{ARTIFACT_ID}", "failed_record": "present", "failed_reason_class": reason}
    assert_redacted(text)


def test_c_an_unrecorded_partial_is_reported_as_absent_record(tmp_path):
    store = tmp_path / "store"
    error = DumpFailed("x", stage="dump")
    code, report, _ = run(base_env(tmp_path), Capture(error, before=lambda request: failed_partial(store, None)))
    assert code == 1 and report["artifact_record"]["failed_record"] == "absent" and report["artifact_record"]["failed_reason_class"] is None


def test_c_a_writer_failure_reports_its_own_stage_and_class(tmp_path):
    store = tmp_path / "store"
    error = WriteFailed("archive", f"FILE_DIGEST_MISMATCH {SECRET}", reason_class="checksum_mismatch", published="no", failed_record="recorded")
    code, report, text = run(base_env(tmp_path), Capture(error, before=lambda request: failed_partial(store, "checksum_mismatch")))
    assert (code, report["outcome"], report["stage"], report["reason_class"], report["published"]) == (1, "failed", "archive", "checksum_mismatch", "no")
    assert_redacted(text)


def test_c_published_incomplete_is_not_clean_success(tmp_path):
    error = PublishedCaptureIncomplete("scratch_cleanup_failed", written(tmp_path / "store"), scratch_path=str(tmp_path / "scratch"))
    error.add_note(f"capture scratch cleanup failed: {SECRET}")
    code, report, text = run(base_env(tmp_path), Capture(error))
    assert (code, report["outcome"], report["problem"], report["published"], report["scratch_remaining"]) == (
        4, "published_incomplete", "scratch_cleanup_failed", "yes", True)
    assert_redacted(text)


@pytest.mark.parametrize("published", ["yes", "unknown"])
def test_c_publication_uncertain_keeps_the_writers_distinction(tmp_path, published):
    error = PostPublicationError("publication", f"the rename raised {SECRET}", published=published)
    code, report, text = run(base_env(tmp_path), Capture(error))
    assert (code, report["outcome"], report["published"], report["stage"]) == (5, "publication_uncertain", published, "publication")
    assert_redacted(text)


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt(), SystemExit(9)])
def test_c_an_interruption_is_reported_with_its_own_exit_code(tmp_path, interrupt):
    store = tmp_path / "store"
    capture = Capture(interrupt, before=lambda request: failed_partial(store, "interrupted"))
    code, report, text = run(base_env(tmp_path), capture)
    assert (code, report["outcome"], report["error_type"]) == (130, "interrupted", type(interrupt).__name__)
    assert report["artifact_record"]["failed_reason_class"] == "interrupted" and len(capture.calls) == 1


def test_c_an_interruption_during_validation_creates_nothing(tmp_path, monkeypatch):
    def interrupted(environ):
        raise KeyboardInterrupt()

    monkeypatch.setattr(job, "build_plan", interrupted)
    capture = Capture(SkippedOverlap(ARTIFACT_ID))
    code, report, _ = run(base_env(tmp_path), capture)
    assert (code, report["outcome"]) == (130, "interrupted") and capture.calls == [] and os.listdir(tmp_path / "store") == []


@pytest.mark.parametrize("outcome", [DumpFailed("x", stage="dump"), PostPublicationError("publication", "x", published="unknown"), SkippedOverlap(ARTIFACT_ID)])
def test_d_exactly_one_capture_and_no_retry_or_cleanup(tmp_path, outcome):
    env = base_env(tmp_path)
    store = tmp_path / "store"
    bystander = store / "napsedge-other-20261001T000000Z-9a1d3e7f-2c58-4b61-8f04-5d7e6a1b2c93"
    bystander.mkdir()
    (bystander / "COMPLETE").write_text("not ours")
    partial = failed_partial(store, "dump_failed")
    before = tree(store)
    capture = Capture(outcome)
    run(env, capture)
    assert len(capture.calls) == 1, "one invocation, never a retry"
    assert tree(store) == before and partial.exists(), "the job neither deletes, re-records nor touches anything in the store"


def test_d_every_documented_outcome_has_a_distinct_exit_code():
    assert job.EXIT_CODES == {"published": 0, "failed": 1, "skipped_overlap": 3, "published_incomplete": 4, "publication_uncertain": 5,
                              "configuration_refused": 64, "interrupted": 130}
    assert len(set(job.EXIT_CODES.values())) == len(job.EXIT_CODES)


# =========================================================================== the real command
def _subprocess_env(env: dict) -> dict:
    full = {k: v for k, v in os.environ.items() if not k.startswith("NAPSEDGE_BACKUP_")}
    full.update(env)
    return full


def test_e_the_command_refuses_invalid_configuration_with_a_report_and_code_64(tmp_path):
    env = base_env(tmp_path)
    env["NAPSEDGE_BACKUP_DATABASE_URL"] = f"postgresql+psycopg://napsedge_backup_ro:{SECRET}@{HOST}:5432/napsedge?sslmode=verify-full&sslrootcert=/run/ca.pem"
    completed = subprocess.run([sys.executable, "-m", "src.deployment.entrypoints", "backup-capture"], cwd=REPO_ROOT, env=_subprocess_env(env),
                               capture_output=True, text=True, timeout=120)
    assert completed.returncode == 64, completed.stderr[-500:]
    report = json.loads(completed.stdout)
    assert report["outcome"] == "configuration_refused" and completed.stderr == ""
    assert_redacted(completed.stdout + completed.stderr)


def test_e_the_parser_offers_backup_capture():
    from src.deployment.entrypoints import build_parser

    assert build_parser().parse_args(["backup-capture"]).process == "backup-capture"


def test_f_the_job_module_never_inspects_transport_settings_and_never_prints_exception_text():
    source = (REPO_ROOT / "src" / "deployment" / "backup_job.py").read_text(encoding="utf-8")
    tree_ = ast.parse(source)
    docstring = ast.get_docstring(tree_, clean=False)
    imported = {n.module for n in ast.walk(tree_) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree_) if isinstance(n, ast.Import) for a in n.names}
    assert not {"traceback", "subprocess", "docker", "logging"} & imported, imported
    attributes = {n.attr for n in ast.walk(tree_) if isinstance(n, ast.Attribute)}
    assert not {"__notes__", "diagnostic", "__traceback__", "args"} & attributes, attributes & {"__notes__", "diagnostic", "__traceback__", "args"}
    constants = " ".join(n.value for n in ast.walk(tree_) if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value != docstring)
    assert "NAPSEDGE_TEST_CLIENT" not in constants and "docker" not in constants.lower()
    str_calls = [ast.unparse(n) for n in ast.walk(tree_) if isinstance(n, ast.Call) and ast.unparse(n.func) == "str"]
    assert sorted(str_calls) == ["str(exc)", "str(uuid.uuid4())"], str_calls  # the one str(exc) is the JobConfigurationError, which names variables only


# =========================================================================== Phase 9B-7A: the password-file credential source
def file_env(tmp_path: Path, content: bytes, *, name: str = "pw") -> dict:
    env = base_env(tmp_path)
    del env["NAPSEDGE_BACKUP_DATABASE_PASSWORD"]
    secret_file = tmp_path / name
    secret_file.write_bytes(content)
    if sys.platform != "win32":
        os.chmod(secret_file, 0o400)
    env["NAPSEDGE_BACKUP_DATABASE_PASSWORD_FILE"] = str(secret_file)
    return env


@pytest.mark.parametrize("content, expected", [
    (SECRET.encode(), SECRET),
    (SECRET.encode() + b"\n", SECRET),  # exactly one terminal LF removed
    (b"  " + SECRET.encode() + b"\t \n", "  " + SECRET + "\t "),  # spaces and tabs are part of the password
    ("päss-€-wörd".encode("utf-8") + b"\n", "päss-€-wörd"),
    (b"pass:with\\colon", "pass:with\\colon"),  # passfile escaping happens downstream, unchanged
])
def test_g_a_password_file_supplies_the_exact_password(tmp_path, content, expected):
    plan = job.build_plan(file_env(tmp_path, content))
    assert plan.request.database_url.password == expected
    assert "NAPSEDGE_BACKUP_DATABASE_PASSWORD" not in os.environ, "the password is not placed into another variable"


MALFORMED = [
    ("empty", b"", "empty password"),
    ("only_newline", b"\n", "empty password"),
    ("two_newlines", SECRET.encode() + b"\n\n", "carriage return, line feed or NUL"),
    ("crlf_terminated", SECRET.encode() + b"\r\n", "carriage return, line feed or NUL"),
    ("embedded_newline", b"abc\ndef", "carriage return, line feed or NUL"),
    ("nul", b"abc\x00def", "carriage return, line feed or NUL"),
    ("not_utf8", b"\xff\xfe" + SECRET.encode(), "not valid UTF-8"),
    ("byte_order_mark", b"\xef\xbb\xbf" + SECRET.encode(), "byte-order mark"),
    ("too_large", b"x" * (job.PASSWORD_FILE_MAX_BYTES + 1), "larger than 4096 bytes"),
]


@pytest.mark.parametrize("case, content, fragment", MALFORMED, ids=[m[0] for m in MALFORMED])
def test_g_a_malformed_password_file_is_refused_before_capture_without_echoing_it(tmp_path, case, content, fragment):
    capture = Capture(SkippedOverlap(ARTIFACT_ID))
    code, report, text = run(file_env(tmp_path, content), capture)
    assert (code, report["outcome"]) == (64, "configuration_refused") and fragment in report["detail"], report
    assert "NAPSEDGE_BACKUP_DATABASE_PASSWORD_FILE" in report["detail"] and capture.calls == []
    assert_redacted(text)
    assert str(tmp_path) not in text, "the file path is not echoed either"


def test_g_exactly_4096_bytes_is_accepted(tmp_path):
    assert len(job.build_plan(file_env(tmp_path, b"y" * 4096)).request.database_url.password) == 4096


@pytest.mark.parametrize("case", ["missing", "relative", "directory"])
def test_g_an_unusable_password_file_path_is_refused(tmp_path, case):
    env = file_env(tmp_path, SECRET.encode())
    env["NAPSEDGE_BACKUP_DATABASE_PASSWORD_FILE"] = {"missing": str(tmp_path / "absent"), "relative": "pw", "directory": str(tmp_path)}[case]
    capture = Capture(SkippedOverlap(ARTIFACT_ID))
    code, report, text = run(env, capture)
    assert (code, report["outcome"]) == (64, "configuration_refused") and capture.calls == []
    assert "NAPSEDGE_BACKUP_DATABASE_PASSWORD_FILE" in report["detail"]
    assert_redacted(text)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits: Windows applies no mode check (documented)")
@pytest.mark.parametrize("mode", [0o620, 0o602, 0o666])
def test_g_a_group_or_world_writable_password_file_is_refused(tmp_path, mode):
    env = file_env(tmp_path, SECRET.encode())
    os.chmod(env["NAPSEDGE_BACKUP_DATABASE_PASSWORD_FILE"], mode)
    code, report, _ = run(env, Capture(SkippedOverlap(ARTIFACT_ID)))
    assert (code, report["outcome"]) == (64, "configuration_refused") and "writable by group or others" in report["detail"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits: Windows applies no mode check (documented)")
def test_g_a_world_readable_read_only_mount_style_file_is_accepted(tmp_path):
    env = file_env(tmp_path, SECRET.encode())
    os.chmod(env["NAPSEDGE_BACKUP_DATABASE_PASSWORD_FILE"], 0o444)  # the common container-secret mode
    assert job.build_plan(env).request.database_url.password == SECRET


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need privileges on Windows")
def test_g_a_symlinked_credential_file_is_followed(tmp_path):
    env = file_env(tmp_path, SECRET.encode())
    link = tmp_path / "link"
    os.symlink(env["NAPSEDGE_BACKUP_DATABASE_PASSWORD_FILE"], link)
    env["NAPSEDGE_BACKUP_DATABASE_PASSWORD_FILE"] = str(link)
    assert job.build_plan(env).request.database_url.password == SECRET


def test_g_both_or_neither_credential_source_is_refused(tmp_path):
    both = file_env(tmp_path, SECRET.encode())
    both["NAPSEDGE_BACKUP_DATABASE_PASSWORD"] = SECRET
    capture = Capture(SkippedOverlap(ARTIFACT_ID))
    code, report, text = run(both, capture)
    assert (code, report["outcome"]) == (64, "configuration_refused") and "exactly one credential source" in report["detail"] and capture.calls == []
    assert_redacted(text)
    neither = base_env(tmp_path)
    del neither["NAPSEDGE_BACKUP_DATABASE_PASSWORD"]
    code, report, _ = run(neither, capture)
    assert code == 64 and "NAPSEDGE_BACKUP_DATABASE_PASSWORD or NAPSEDGE_BACKUP_DATABASE_PASSWORD_FILE" in report["detail"] and capture.calls == []


def test_g_a_url_password_is_still_refused_with_a_password_file(tmp_path):
    env = file_env(tmp_path, SECRET.encode())
    env["NAPSEDGE_BACKUP_DATABASE_URL"] = f"postgresql+psycopg://napsedge_backup_ro:{SECRET}@{HOST}:5432/napsedge?sslmode=verify-full&sslrootcert=/run/ca.pem"
    code, report, text = run(env, Capture(SkippedOverlap(ARTIFACT_ID)))
    assert code == 64 and "must not contain a password" in report["detail"]
    assert_redacted(text)


def test_g_the_environment_password_behaves_exactly_as_in_9b6k(tmp_path):
    env = base_env(tmp_path)
    env["NAPSEDGE_BACKUP_DATABASE_PASSWORD"] = "  " + SECRET + "  "  # unchanged: used as given, whitespace preserved
    assert job.build_plan(env).request.database_url.password == "  " + SECRET + "  "
    env["NAPSEDGE_BACKUP_DATABASE_PASSWORD"] = "   "
    code, report, _ = run(env, Capture(SkippedOverlap(ARTIFACT_ID)))
    assert code == 64 and "NAPSEDGE_BACKUP_DATABASE_PASSWORD" in report["detail"], "whitespace-only still counts as not supplied"


def test_g_a_file_sourced_password_never_appears_in_any_outcome(tmp_path):
    env = file_env(tmp_path, SECRET.encode() + b"\n")
    for outcome in (DumpFailed(f"x {SECRET}", stage="dump", diagnostic=SECRET), PostPublicationError("publication", SECRET, published="unknown"),
                    KeyboardInterrupt(), SkippedOverlap(ARTIFACT_ID)):
        _, _, text = run(env, Capture(outcome))
        assert_redacted(text)
