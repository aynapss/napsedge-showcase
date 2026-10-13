"""
Phase 9B-7A follow-up: the build context excludes HOST-generated Python bytecode at every depth -- through a real build.

A disposable copy of the build inputs (the repository's own `Dockerfile` and `.dockerignore`, the COPY sources, the PGDG
key-verification script) is made WITHOUT the host's caches, then harmless sentinel files are planted: bytecode at the
context root and at several nested application paths, plus one ordinary `.py` sentinel that must survive (the positive
control). Two builds read that context:

* a context probe (`FROM scratch` + `COPY . /`, exported to a local directory): exactly what the context transfers;
* the real `backup` target, built from the same context: what the image actually holds under `/app`.

Only `/app` -- the copied application -- is required to be free of bytecode. Bytecode that pip legitimately generates
while installing dependencies (under `/usr/local/lib/python3.13`) is reported, never claimed absent.

Needs Docker and network access for the base image and PGDG (deselected by default with the `postgres` marker, as for every
Docker-backed module); runs only when `NAPSEDGE_BUILD_CONTEXT_TEST=1`. `NAPSEDGE_BUILD_BASE_REF` (for example
`python:3.13-slim@sha256:<digest>`) pins the base image through a named build context, so the build never follows a
moved floating tag. Every image is labelled and removed afterwards.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

pytestmark = [pytest.mark.postgres, pytest.mark.slow,
              pytest.mark.skipif(os.environ.get("NAPSEDGE_BUILD_CONTEXT_TEST") != "1", reason="set NAPSEDGE_BUILD_CONTEXT_TEST=1 to build images")]

REPO = Path.cwd()
LABEL = "org.napsedge.unit=9b7a1"
INPUTS = ["Dockerfile", ".dockerignore", "requirements.txt", "requirements-dev.txt", "requirements-backup.txt", "constraints-release-py313.txt", "src", "pages", "app.py",
          "alembic", "alembic.ini", ".python-version", "Reference Py Files/net_rankings_2026.csv", "cbb_teamScrape_v15_TEST.ipynb",
          "docs/sr_slug_reconciliation.json", "docker/backup/fetch_pgdg_key.py"]
SENTINEL_BYTES = b"NAPSEDGE-9B7A1-SENTINEL: not real bytecode, never imported\n"
BYTECODE_SENTINELS = [
    "napsedge_sentinel_root.pyc",
    "__pycache__/napsedge_sentinel.cpython-313.pyc",
    "src/napsedge_sentinel.pyc",
    "src/__pycache__/napsedge_sentinel.cpython-313.pyc",
    "src/deployment/__pycache__/napsedge_sentinel.cpython-313.pyc",
    "src/deployment/napsedge_sentinel.pyo",
    "alembic/versions/__pycache__/napsedge_sentinel.cpython-313.pyc",
    "pages/__pycache__/napsedge_sentinel.cpython-313.opt-1.pyc",
]
KEEP_SENTINEL = "src/napsedge_sentinel_keep.py"  # positive control: ordinary source next to the bytecode, must survive
HOST_CACHE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")


def docker(*args: str, timeout: int = 1800, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, **kwargs)


def base_args() -> list[str]:
    ref = os.environ.get("NAPSEDGE_BUILD_BASE_REF", "")
    pin = ["--build-context", f"python:3.13-slim=docker-image://{ref}"] if ref else []
    return ["--platform", "linux/amd64", "--label", LABEL, *pin]


@pytest.fixture(scope="module")
def context(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("build_context")
    for item in INPUTS:
        source, target = REPO / item, root / item
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, ignore=HOST_CACHE)
        else:
            shutil.copy2(source, target)
    for rel in BYTECODE_SENTINELS:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(SENTINEL_BYTES)
    (root / KEEP_SENTINEL).write_text("# NAPSEDGE-9B7A1 positive-control sentinel\n", encoding="utf-8")
    return root


def sources(root: Path) -> dict[str, str]:
    """Every application source and migration the image must keep: relative path -> sha256."""
    found = {}
    for top in ("src", "alembic", "pages"):
        for path in sorted((root / top).rglob("*")):
            if path.is_file() and path.suffix not in (".pyc", ".pyo") and "__pycache__" not in path.parts:
                found[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


@pytest.fixture(scope="module")
def transferred(context, tmp_path_factory) -> Path:
    """What the build context actually transfers under the repository's `.dockerignore`."""
    probe_dir = tmp_path_factory.mktemp("probe")
    (probe_dir / "probe.Dockerfile").write_text("FROM scratch\nCOPY . /\n", encoding="utf-8")
    out = tmp_path_factory.mktemp("transferred")
    result = docker("build", "-f", str(probe_dir / "probe.Dockerfile"), "--output", f"type=local,dest={out}", str(context))
    assert result.returncode == 0, result.stderr[-2000:]
    return out


@pytest.fixture(scope="module")
def image(context):
    tag = f"napsedge-backup:9b7a1-context-{uuid.uuid4().hex[:12]}"
    result = docker("build", "--target", "backup", *base_args(), "-t", tag, str(context))
    assert result.returncode == 0, result.stderr[-3000:]
    try:
        yield tag
    finally:
        docker("image", "rm", "-f", tag)


def in_image(tag: str, script: str) -> subprocess.CompletedProcess:
    return docker("run", "--rm", "--read-only", "--network", "none", "--user", "10001:10001", "--label", LABEL, "--entrypoint", "sh", tag, "-c", script, timeout=300)


def test_the_context_transfers_no_planted_bytecode(transferred):
    leaked = sorted(rel for rel in BYTECODE_SENTINELS if (transferred / rel).exists())
    assert leaked == [], f"host bytecode entered the build context: {leaked}"
    assert not any(p.name == "__pycache__" or p.suffix in (".pyc", ".pyo") for p in transferred.rglob("*")), "no bytecode of any name"


def test_the_context_keeps_every_required_input(context, transferred):
    assert (transferred / KEEP_SENTINEL).is_file(), "positive control: ordinary source beside the bytecode survives"
    for item in ("Dockerfile", "requirements.txt", "constraints-release-py313.txt", "app.py", "alembic.ini", ".python-version",
                 "Reference Py Files/net_rankings_2026.csv", "cbb_teamScrape_v15_TEST.ipynb", "docs/sr_slug_reconciliation.json",
                 "docker/backup/fetch_pgdg_key.py"):
        assert (transferred / item).is_file(), item
    expected = {rel: digest for rel, digest in sources(context).items() if not rel.startswith("pages/0")}  # pages 01-09: excluded by design
    assert sources(transferred) == expected
    assert any(rel.startswith("alembic/versions/") for rel in expected), "the migrations are part of the check"


def test_the_backup_image_holds_no_host_bytecode_and_every_source(context, image):
    listing = in_image(image, 'cd /app && find . -type f -not -path "./cbb_*" | sort | while read f; do sha256sum "$f"; done')
    assert listing.returncode == 0, listing.stderr
    in_app = {line.split(None, 1)[1].removeprefix("./"): line.split(None, 1)[0] for line in listing.stdout.splitlines()}
    leaked = sorted(rel for rel in in_app if rel.endswith((".pyc", ".pyo")) or "__pycache__/" in rel)
    assert leaked == [], f"bytecode under /app (copied from the build context): {leaked}"
    expected = {rel: digest for rel, digest in sources(context).items() if not rel.startswith("pages/0")}
    assert {rel: digest for rel, digest in in_app.items() if rel.startswith(("src/", "alembic/", "pages/"))} == expected
    assert KEEP_SENTINEL in in_app
    tools = in_image(image, "/usr/lib/postgresql/16/bin/pg_dump --version && test -f /opt/napsedge-backup/fetch_pgdg_key.py && "
                            'find /usr/local/lib/python3.13 -name "*.pyc" | wc -l')
    assert tools.returncode == 0 and "16.15" in tools.stdout, tools.stderr
    print(f"install-generated bytecode under /usr/local/lib/python3.13 (expected, not host-copied): {tools.stdout.split()[-1]} files")


def test_the_backup_image_still_runs_the_accepted_job(image):
    run = docker("run", "--rm", "--read-only", "--network", "none", "--user", "10001:10001", "--cap-drop", "ALL",
                 "--security-opt", "no-new-privileges", "--label", LABEL, image, timeout=300)
    report = json.loads(run.stdout)
    assert (run.returncode, report["outcome"], report["job"]) == (64, "configuration_refused", "napsedge-backup-capture"), run.stdout
