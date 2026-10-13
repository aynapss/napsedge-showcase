"""
Container process entrypoints (Phase 9A).

Each subcommand validates its own process's required configuration
(`src.deployment.env_validation`) before binding a port or contacting
an external service, then hands off to the real process. Nothing in
this module runs on import -- only `main()`, invoked from
`if __name__ == "__main__":` below (the container's own `CMD`), does
anything. Importing this module is exactly as side-effect-free as
importing any other `src/` module; it performs no I/O and contacts
nothing.

The Streamlit subcommand execs into the real `streamlit` CLI
(`os.execvp`) rather than wrapping it, since Streamlit has no public
"run programmatically" API this project can call safely -- `execvp`
replaces this process image entirely, so the real `streamlit` process
receives OS signals (SIGTERM on `docker stop`) directly, exactly as if
it had been the container's own `CMD`. The API subcommand calls
`uvicorn.run(...)` in-process instead, since `uvicorn` is designed to
be invoked exactly this way and installs its own graceful-shutdown
signal handling when run as the main entry point. The Discord
subcommand calls the existing, unchanged `src.discord_bot.bot.main()`
directly -- `client.run()` already blocks the calling thread/process,
so no `execvp`/wrapper is needed there either.

The `backup-capture` subcommand (Phase 9B-6K) runs ONE guarded backup capture
and exits with the documented status of `src.deployment.backup_job`; it is a
one-shot job, not a long-running process, and nothing schedules it.
"""
from __future__ import annotations

import argparse
import os
import sys

from src.deployment.env_validation import (
    DeploymentConfigurationError,
    validate_api_environment,
    validate_discord_environment,
    validate_streamlit_environment,
)

_API_HOST = "0.0.0.0"
_API_PORT = 8000
_STREAMLIT_HOST = "0.0.0.0"
_STREAMLIT_PORT = 8501


def _fail(exc: DeploymentConfigurationError) -> None:
    """Prints to stderr and exits 1 -- never raises past this point, so a container's own `CMD` reports a clean non-zero exit rather than a Python traceback."""
    print(f"configuration error: {exc}", file=sys.stderr)
    sys.exit(1)


def run_api() -> None:
    try:
        validate_api_environment()
    except DeploymentConfigurationError as exc:
        _fail(exc)
        return
    import uvicorn

    # Phase 9E-1: uvicorn's own access log is off -- it would write the full URL INCLUDING the query string; the API's
    # request-context middleware writes one access line per request with the path only and the request ID.
    uvicorn.run("src.api.main:create_app", factory=True, host=_API_HOST, port=_API_PORT, access_log=False)


def run_streamlit() -> None:
    try:
        validate_streamlit_environment()
    except DeploymentConfigurationError as exc:
        _fail(exc)
        return
    os.execvp(
        "streamlit",
        [
            "streamlit",
            "run",
            "app.py",
            f"--server.address={_STREAMLIT_HOST}",
            f"--server.port={_STREAMLIT_PORT}",
            "--server.headless=true",
            "--browser.gatherUsageStats=false",
        ],
    )


def run_discord(*, check_only: bool) -> None:
    try:
        validate_discord_environment()
    except DeploymentConfigurationError as exc:
        _fail(exc)
        return
    if check_only:
        # Configuration is valid; deliberately never imports
        # `src.discord_bot.bot` at all in this branch, so there is no
        # code path here that could reach `client.run(...)` even
        # indirectly.
        print("discord configuration check: OK (no connection attempted)")
        return
    from src.discord_bot.bot import main as discord_main

    discord_main()


def run_backup_capture() -> None:
    from src.deployment.backup_job import run_job  # imported only for this subcommand

    sys.exit(run_job())


def run_backup_cycle() -> None:
    from src.deployment.backup_cycle import run_cycle  # Phase 9B-8B: imported only for this subcommand

    sys.exit(run_cycle())


def run_backup_status() -> None:
    from src.deployment.backup_cycle import backup_status

    sys.exit(backup_status())


def run_backup_retention(argv: list[str]) -> None:
    from src.deployment.backup_retention import run_retention

    sys.exit(run_retention(argv))


def run_backup_offsite(artifact_dir: str) -> None:
    """Operator-initiated upload of ONE published artifact (for example after reviewing an exit 4/5); the same verified path."""
    import json
    import os

    from src.deployment.backup_cycle import _default_uploader
    from src.deployment.backup_operations import OperationsConfigurationError, load_operations_config

    try:
        ops = load_operations_config()
    except OperationsConfigurationError as exc:
        print(json.dumps({"outcome": "configuration_refused", "detail": str(exc)}))
        sys.exit(23)
    report = _default_uploader(dict(os.environ), artifact_dir, ops)
    print(json.dumps(report, sort_keys=True))
    sys.exit({"stored_verified": 0, "already_stored": 0, "failed": 20, "uncertain": 21, "refused": 22}.get(report.get("outcome"), 23))


def run_data_freshness() -> None:
    """Phase 9E-3: the operator's view of how old the local caches are (JSON on stdout). Exit 0 ok, 10 stale, 64 bad setting."""
    import json

    from src.services.freshness import FreshnessConfigurationError, data_freshness

    try:
        report = data_freshness()
    except FreshnessConfigurationError as exc:
        print(json.dumps({"status": "configuration_refused", "detail": str(exc)}))
        sys.exit(64)
    print(json.dumps(report, sort_keys=True))
    sys.exit(0 if report["status"] == "ok" else 10)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.deployment.entrypoints")
    subparsers = parser.add_subparsers(dest="process", required=True)
    subparsers.add_parser("api")
    subparsers.add_parser("streamlit")
    subparsers.add_parser("backup-capture", help="Run one guarded backup capture (configuration from the environment) and exit.")
    subparsers.add_parser("backup-cycle", help="One scheduled cycle: capture (bounded overlap retries), then the encrypted off-site copy if enabled.")
    subparsers.add_parser("backup-status", help="Report ok/attention: overdue remote copy, repeated overlap skips, failed cycles, overdue drill.")
    retention = subparsers.add_parser("backup-retention", help="Plan (dry run) or execute remote retention; deletion is disabled by default.")
    retention.add_argument("retention_args", nargs=argparse.REMAINDER)
    subparsers.add_parser("data-freshness", help="Report how old the local rankings and injury caches are (exit 10 if stale).")
    offsite = subparsers.add_parser("backup-offsite", help="Upload ONE published, verified artifact directory (operator-initiated).")
    offsite.add_argument("artifact_dir")
    discord_parser = subparsers.add_parser("discord")
    discord_parser.add_argument(
        "--check-only",
        action="store_true",
        help="Validate configuration and exit without connecting to Discord.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["backup-retention"]:
        # Phase 9B-8C (found by the real-B2 deletion test): argparse.REMAINDER in a subparser drops LEADING options such as
        # `--execute --plan-sha256 <digest>` ("unrecognized arguments"), so the retention CLI receives everything after the
        # subcommand verbatim and parses it itself (its own parser rejects anything unknown).
        run_backup_retention(argv[1:])
        return
    args = build_parser().parse_args(argv)
    if args.process == "api":
        run_api()
    elif args.process == "streamlit":
        run_streamlit()
    elif args.process == "discord":
        run_discord(check_only=args.check_only)
    elif args.process == "backup-capture":
        run_backup_capture()
    elif args.process == "backup-cycle":
        run_backup_cycle()
    elif args.process == "backup-status":
        run_backup_status()
    elif args.process == "backup-retention":
        run_backup_retention(args.retention_args)
    elif args.process == "backup-offsite":
        run_backup_offsite(args.artifact_dir)
    elif args.process == "data-freshness":
        run_data_freshness()


if __name__ == "__main__":
    main()
