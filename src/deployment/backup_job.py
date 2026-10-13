"""
Phase 9B-6K: the one-shot backup capture job (docs/backup_artifact_contract_proposal.md section 13.21).

`python -m src.deployment.entrypoints backup-capture` runs ONE `run_guarded_capture` with the production
`LocalClientHost`, prints ONE machine-readable JSON report line on stdout, and exits with a documented status. It is not a
scheduler, a retry loop, a restore path or a storage integration, and a published artifact is not a recovery-readiness
claim (claim 1 plus the structural check; the observation means only "no difference observed").

Configuration: the process environment, the established convention of this project (`NAPSEDGE_DATABASE_URL`,
`.env.example`). Using it here does NOT settle the production secrets-storage decision (readiness, open decision 9).
Required:
  NAPSEDGE_BACKUP_DATABASE_URL           postgresql+psycopg URL WITHOUT a password: backup role as user, host, port, database,
                                         sslmode=verify-full, sslrootcert=<CA file>   (a URL carrying a password is refused)
  exactly ONE credential source (Phase 9B-7A):
    NAPSEDGE_BACKUP_DATABASE_PASSWORD      the backup role's password (never printed, never in argv), or
    NAPSEDGE_BACKUP_DATABASE_PASSWORD_FILE an absolute path to a file holding it (for a read-only credential mount,
                                           e.g. a systemd credential or a container secret); both or neither: refused
  NAPSEDGE_BACKUP_TARGET_ACKNOWLEDGEMENT the exact acknowledgement phrase for the database named in the URL
  NAPSEDGE_BACKUP_EXPECTED_SYSTEM_IDENTIFIER  the source cluster's system_identifier (decimal)
  NAPSEDGE_BACKUP_EXPECTED_DATABASE_OID       the source database's OID
  NAPSEDGE_BACKUP_STORE_DIR              an existing artifact store directory
  NAPSEDGE_BACKUP_LOGICAL_NAME           the artifact logical name ([a-z0-9_]{1,40})
  NAPSEDGE_BACKUP_PG_DUMP, NAPSEDGE_BACKUP_PG_RESTORE   absolute paths of the PostgreSQL 16 client executables
  NAPSEDGE_BACKUP_GIT_REVISION, NAPSEDGE_BACKUP_GIT_DIRTY   the job's code provenance (40 hex; true|false)
  NAPSEDGE_BACKUP_TOOLING_VERSION        the job's version label
Password file rules (applied before any capture resource; the file's content never appears in any output):
  * an absolute path to a regular file (symbolic links are followed, as container secret and credential mounts use them),
    opened read-only; on POSIX it must not be writable by group or others (readability is not checked: container secret
    mounts are commonly 0444, so the mount boundary, not the mode, is the access control); Windows applies no mode check;
  * at most 4096 bytes, strict UTF-8 (a byte-order mark is refused);
  * exactly one terminal LF is removed if present (the usual line ending of a secret file); NOTHING else is removed or
    normalized -- leading/trailing spaces and tabs are part of the password;
  * after that, the password must be non-empty and contain no CR, LF or NUL (the characters the downstream libpq passfile
    and the dump's own check refuse), so a CRLF-terminated file is refused rather than silently rewritten.
  The environment-variable source is unchanged from 9B-6K.
Optional:
  NAPSEDGE_BACKUP_DURABILITY             required (default, the writer's own default) | best_effort_reported
  NAPSEDGE_BACKUP_ARTIFACT_ID            a lower-case UUID4 (default: a fresh one)
The expected Alembic head is not configured: it is the repository's single Alembic head, read the way
`src.services.health` reads it.

stdout carries ONLY the report. stderr is the process's diagnostic logging under the project's existing convention
(`src.logging_config`, level `NAPSEDGE_LOG_LEVEL`, default INFO; e.g. Alembic's plugin-setup lines); it is not part of the
report, and nothing in this module writes to it.

Report and exit status (one JSON object per run; only explicitly selected, sanitized fields -- exception TYPE names and
enumerated classes, never an exception message, note, traceback, URL, credential or tool output):
  0   published              the /2 artifact was published and the coordinator's checks completed
  1   failed                 refused or failed before publication; `artifact_record` says whether a FAILED record exists
                             (none before the reservation) and its enumerated class, read from the owned partial on disk
  3   skipped_overlap        another job held the lock: nothing was created. NOT a failed backup
  4   published_incomplete   published, but the coordinator's follow-up (scratch cleanup or digest re-check) did not complete
  5   publication_uncertain  the writer reported the publication as "yes" or "unknown" after a failure at/after the rename
  64  configuration_refused  configuration invalid: refused before any capture resource or connection
  130 interrupted            KeyboardInterrupt/SystemExit during the run (the coordinator's own policy recorded what it could)
Nothing here records FAILED, retries, re-reserves, deletes or re-runs anything: the coordinator and writer own those.
Foreign-write counts are null ("not sampled") and are reported as such; they never imply quiescence.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import uuid
from dataclasses import dataclass
from typing import Any, Callable, TextIO

from sqlalchemy.engine import URL, make_url

REPORT_FORMAT = "napsedge.backup-job-report/1"
JOB_NAME = "napsedge-backup-capture"
TOOLING_NAME = "napsedge-backup"
EXIT_CODES = {"published": 0, "failed": 1, "skipped_overlap": 3, "published_incomplete": 4, "publication_uncertain": 5,
              "configuration_refused": 64, "interrupted": 130}
CREDENTIAL_SOURCES = ("NAPSEDGE_BACKUP_DATABASE_PASSWORD", "NAPSEDGE_BACKUP_DATABASE_PASSWORD_FILE")
PASSWORD_FILE_MAX_BYTES = 4096
REQUIRED = ("NAPSEDGE_BACKUP_DATABASE_URL", "NAPSEDGE_BACKUP_TARGET_ACKNOWLEDGEMENT",
            "NAPSEDGE_BACKUP_EXPECTED_SYSTEM_IDENTIFIER", "NAPSEDGE_BACKUP_EXPECTED_DATABASE_OID", "NAPSEDGE_BACKUP_STORE_DIR",
            "NAPSEDGE_BACKUP_LOGICAL_NAME", "NAPSEDGE_BACKUP_PG_DUMP", "NAPSEDGE_BACKUP_PG_RESTORE", "NAPSEDGE_BACKUP_GIT_REVISION",
            "NAPSEDGE_BACKUP_GIT_DIRTY", "NAPSEDGE_BACKUP_TOOLING_VERSION")
OPTIONAL = ("NAPSEDGE_BACKUP_DURABILITY", "NAPSEDGE_BACKUP_ARTIFACT_ID")


class JobConfigurationError(Exception):
    """Configuration refused before capture. The message names variables only, never a value."""


@dataclass(frozen=True)
class JobPlan:
    request: Any
    client: Any


def _expected_alembic_head() -> str:
    """The repository's own single Alembic head, read exactly as `src.services.health` reads it."""
    from alembic.script import ScriptDirectory

    from src.services.health import _alembic_config

    heads = ScriptDirectory.from_config(_alembic_config()).get_heads()
    if len(heads) != 1:
        raise JobConfigurationError("the repository does not have exactly one Alembic head")
    return heads[0]


def _read_password_file(path: str) -> str:
    """The password file rules of the module docstring. Every refusal names the variable only, never the path's content."""
    name = "NAPSEDGE_BACKUP_DATABASE_PASSWORD_FILE"
    if not os.path.isabs(path):
        raise JobConfigurationError(f"{name} must be an absolute path")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise JobConfigurationError(f"{name} cannot be opened ({type(exc).__name__})") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise JobConfigurationError(f"{name} is not a regular file")
        if sys.platform != "win32" and info.st_mode & 0o022:
            raise JobConfigurationError(f"{name} is writable by group or others")
        if info.st_size > PASSWORD_FILE_MAX_BYTES:
            raise JobConfigurationError(f"{name} is larger than {PASSWORD_FILE_MAX_BYTES} bytes")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, PASSWORD_FILE_MAX_BYTES + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > PASSWORD_FILE_MAX_BYTES:
                raise JobConfigurationError(f"{name} is larger than {PASSWORD_FILE_MAX_BYTES} bytes")
    except OSError as exc:
        raise JobConfigurationError(f"{name} cannot be read ({type(exc).__name__})") from None
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    if raw.startswith(b"\xef\xbb\xbf"):
        raise JobConfigurationError(f"{name} begins with a byte-order mark")
    try:
        password = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise JobConfigurationError(f"{name} is not valid UTF-8") from None
    if password.endswith("\n"):
        password = password[:-1]  # exactly one terminal LF; nothing else is stripped
    if not password:
        raise JobConfigurationError(f"{name} holds an empty password")
    if any(ch in password for ch in "\r\n\x00"):
        raise JobConfigurationError(f"{name} contains a carriage return, line feed or NUL")
    return password


def build_plan(environ: dict) -> JobPlan:
    """Validates the configuration and builds the coordinator's request and the production client. Creates and connects nothing."""
    from src.deployment.backup_artifact import RepositoryState, ToolingInfo
    from src.deployment.backup_artifact_writer import DurabilityMode
    from src.deployment.backup_capture_catalog import SourceIdentity
    from src.deployment.backup_capture_coordinator import CaptureRequest, _validate
    from src.deployment.backup_capture_dump import LocalClientHost
    from src.exceptions import ValidationError
    from src.pipeline.bulk_import_safety import DatabaseTargetDesignation

    missing = [name for name in REQUIRED if not environ.get(name, "").strip()]
    supplied = [name for name in CREDENTIAL_SOURCES if environ.get(name, "").strip()]
    if not supplied:
        missing.append(" or ".join(CREDENTIAL_SOURCES))
    if missing:
        raise JobConfigurationError("required configuration is missing: " + ", ".join(missing))
    if len(supplied) > 1:
        raise JobConfigurationError("exactly one credential source may be set: " + " or ".join(CREDENTIAL_SOURCES))
    problems: list[str] = []
    try:
        url = make_url(environ["NAPSEDGE_BACKUP_DATABASE_URL"].strip())
    except Exception:  # noqa: BLE001 -- the parser's message could echo the value
        raise JobConfigurationError("NAPSEDGE_BACKUP_DATABASE_URL is not a valid database URL") from None
    if url.password is not None:
        raise JobConfigurationError("NAPSEDGE_BACKUP_DATABASE_URL must not contain a password; use NAPSEDGE_BACKUP_DATABASE_PASSWORD")
    if not url.database:
        raise JobConfigurationError("NAPSEDGE_BACKUP_DATABASE_URL must name the database")
    if supplied[0] == "NAPSEDGE_BACKUP_DATABASE_PASSWORD":
        password = environ["NAPSEDGE_BACKUP_DATABASE_PASSWORD"]  # unchanged 9B-6K behavior
    else:
        password = _read_password_file(environ["NAPSEDGE_BACKUP_DATABASE_PASSWORD_FILE"].strip())
    url = url.set(password=password)  # assembled in memory only; never printed, never placed in another variable or argv
    try:
        designation = DatabaseTargetDesignation(url.database, environ["NAPSEDGE_BACKUP_TARGET_ACKNOWLEDGEMENT"].strip())
    except Exception:  # noqa: BLE001 -- the designation's own check; its message is not repeated
        problems.append("NAPSEDGE_BACKUP_TARGET_ACKNOWLEDGEMENT")
    system_identifier = environ["NAPSEDGE_BACKUP_EXPECTED_SYSTEM_IDENTIFIER"].strip()
    if not system_identifier.isdigit():
        problems.append("NAPSEDGE_BACKUP_EXPECTED_SYSTEM_IDENTIFIER")
    oid_text = environ["NAPSEDGE_BACKUP_EXPECTED_DATABASE_OID"].strip()
    if not oid_text.isdigit():
        problems.append("NAPSEDGE_BACKUP_EXPECTED_DATABASE_OID")
    store = environ["NAPSEDGE_BACKUP_STORE_DIR"].strip()
    if not os.path.isdir(store):
        problems.append("NAPSEDGE_BACKUP_STORE_DIR")
    tools = {}
    for name in ("NAPSEDGE_BACKUP_PG_DUMP", "NAPSEDGE_BACKUP_PG_RESTORE"):
        value = environ[name].strip()
        if not os.path.isabs(value):
            problems.append(name)
        tools[name] = value
    dirty_text = environ["NAPSEDGE_BACKUP_GIT_DIRTY"].strip().lower()
    if dirty_text not in ("true", "false"):
        problems.append("NAPSEDGE_BACKUP_GIT_DIRTY")
    durability_text = environ.get("NAPSEDGE_BACKUP_DURABILITY", "").strip() or DurabilityMode.REQUIRED.value
    durability = next((mode for mode in DurabilityMode if mode.value == durability_text), None)
    if durability is None:
        problems.append("NAPSEDGE_BACKUP_DURABILITY")
    artifact_id = environ.get("NAPSEDGE_BACKUP_ARTIFACT_ID", "").strip() or str(uuid.uuid4())
    if problems:
        raise JobConfigurationError("invalid configuration: " + ", ".join(problems))
    try:
        head = _expected_alembic_head()
        request = CaptureRequest(store=store, database_url=url, designation=designation,
                                 expected_source=SourceIdentity(system_identifier, int(oid_text), url.database),
                                 expected_alembic_head=head, logical_name=environ["NAPSEDGE_BACKUP_LOGICAL_NAME"].strip(), artifact_id=artifact_id,
                                 repository=RepositoryState(environ["NAPSEDGE_BACKUP_GIT_REVISION"].strip(), dirty_text == "true"),
                                 tooling=ToolingInfo(TOOLING_NAME, environ["NAPSEDGE_BACKUP_TOOLING_VERSION"].strip()), durability=durability)
        client = LocalClientHost(pg_dump=(tools["NAPSEDGE_BACKUP_PG_DUMP"],), pg_restore=(tools["NAPSEDGE_BACKUP_PG_RESTORE"],))
        _validate(request, client)  # the coordinator's OWN pre-resource validation (verify-full URL, patterns, client operations)
    except ValidationError as exc:
        raise JobConfigurationError(f"the capture request was refused by the coordinator's validation ({type(exc).__name__})") from None
    return JobPlan(request=request, client=client)


def _artifact_record(store: str, artifact_id: str) -> dict:
    """What the store actually holds for this artifact id: read-only, enumerated fields only."""
    partial = os.path.join(store, f".partial-{artifact_id}")
    if not os.path.isdir(partial):
        return {"partial_present": False, "failed_record": "none"}
    record = os.path.join(partial, "FAILED")
    reason = None
    if os.path.isfile(record):
        try:
            from src.deployment.backup_artifact_writer import FAILED_REASON_CLASSES

            value = json.loads(open(record, "rb").read(512)).get("reason_class")
            reason = value if value in FAILED_REASON_CLASSES else "unrecognized"
        except Exception:  # noqa: BLE001 -- reported as unreadable, never as a message
            reason = "unreadable"
    return {"partial_present": True, "partial": os.path.basename(partial), "failed_record": "present" if reason else "absent", "failed_reason_class": reason}


def _classify(exc: BaseException, plan: JobPlan) -> dict:
    from src.deployment.backup_artifact_writer import ArtifactWriteError, PostPublicationError
    from src.deployment.backup_capture_coordinator import PublishedCaptureIncomplete

    request = plan.request
    if isinstance(exc, PublishedCaptureIncomplete):
        return {"outcome": "published_incomplete", "problem": exc.problem, "published": exc.published,
                "artifact": os.path.basename(exc.written.path), "scratch_remaining": exc.scratch_path is not None}
    if isinstance(exc, PostPublicationError):
        return {"outcome": "publication_uncertain", "published": exc.published, "stage": exc.stage, "error_type": type(exc).__name__}
    fields = {"error_type": type(exc).__name__, "artifact_record": _artifact_record(request.store, request.artifact_id)}
    if isinstance(exc, ArtifactWriteError):
        fields.update(stage=exc.stage, reason_class=exc.reason_class, published=exc.published)
    if not isinstance(exc, Exception):
        return {"outcome": "interrupted", **fields}
    return {"outcome": "failed", **fields}


def run_job(environ: dict | None = None, *, out: TextIO | None = None, capture: Callable | None = None) -> int:
    """Validates, runs ONE capture, prints ONE report line, returns the exit status. `capture` is a test seam only."""
    environ = dict(os.environ if environ is None else environ)
    out = out or sys.stdout
    report: dict = {"job": JOB_NAME, "report_format": REPORT_FORMAT}
    try:
        plan = build_plan(environ)
    except JobConfigurationError as exc:
        report.update(outcome="configuration_refused", detail=str(exc))  # variable names only, by construction
        return _emit(report, out)
    except BaseException as exc:  # noqa: BLE001 -- an interrupt or an unexpected error while validating: nothing was created
        report.update(outcome="interrupted" if not isinstance(exc, Exception) else "failed", error_type=type(exc).__name__,
                      artifact_record={"partial_present": False, "failed_record": "none"})
        return _emit(report, out)
    report["artifact_id"] = plan.request.artifact_id
    if capture is None:
        from src.deployment.backup_capture_coordinator import run_guarded_capture as capture
    try:
        result = capture(plan.request, client=plan.client)
    except BaseException as exc:  # noqa: BLE001 -- classified into an enumerated outcome; never re-raised as a traceback
        report.update(_classify(exc, plan))
        return _emit(report, out)
    from src.deployment.backup_capture_coordinator import PublishedCapture, SkippedOverlap

    if isinstance(result, SkippedOverlap):
        report.update(outcome="skipped_overlap", meaning="another job held the lock; nothing was created; not a failed backup")
    elif isinstance(result, PublishedCapture):
        written = result.written
        report.update(outcome="published", artifact=os.path.basename(written.path), manifest_format="napsedge.backup-artifact/2",
                      manifest_sha256=written.manifest_sha256, archive_sha256=written.archive_sha256, archive_bytes=written.archive_bytes,
                      foreign_write_sessions="not_sampled", establishes=list(result.establishes), does_not_establish=list(result.does_not_establish))
    else:
        report.update(outcome="failed", error_type=type(result).__name__, artifact_record=_artifact_record(plan.request.store, plan.request.artifact_id))
    return _emit(report, out)


def _emit(report: dict, out: TextIO) -> int:
    report["exit_code"] = EXIT_CODES[report["outcome"]]
    out.write(json.dumps(report, sort_keys=True) + "\n")
    out.flush()
    return report["exit_code"]
