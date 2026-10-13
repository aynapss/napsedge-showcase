"""
Phase 9A: actual container build/run validation. Every test in this
file is a genuine tool/platform skip (never a failure) when Docker is
unavailable in the current environment -- `pytest.mark.skipif` at
module level, matching the existing convention in
`tests/postgres/conftest.py` for the identical situation. No test here
ever starts the real Discord client, the Stripe webhook, or a public
network listener; every container started by this file is stopped and
removed in its own fixture teardown, and no named image/volume/network
outlives this test session.
"""
from __future__ import annotations

import shutil
import subprocess
import time
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.slow


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        return result.returncode == 0
    except Exception:
        return False


requires_docker = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker is not available in this environment (tool/platform skip, Phase 9A container validation)",
)

_IMAGE_TAG = "phase9a-test"
_API_IMAGE = f"napsedge-api:{_IMAGE_TAG}"
_STREAMLIT_IMAGE = f"napsedge-streamlit:{_IMAGE_TAG}"
_DISCORD_IMAGE = f"napsedge-discord:{_IMAGE_TAG}"

_VALID_API_KEY_HASHES_JSON = '{"test-client": "' + "0" * 64 + '"}'


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=kwargs.pop("timeout", 600), **kwargs)


@pytest.fixture(scope="module")
def built_images():
    """Builds all three targets once for this module; always removes them afterward, even on failure."""
    if not _docker_available():
        pytest.skip("Docker is not available in this environment")
    targets = {"api": _API_IMAGE, "streamlit": _STREAMLIT_IMAGE, "discord": _DISCORD_IMAGE}
    built: list[str] = []
    try:
        for target, tag in targets.items():
            result = _run(["docker", "build", "--target", target, "-t", tag, "."])
            assert result.returncode == 0, f"build failed for target {target!r}:\n{result.stdout}\n{result.stderr}"
            built.append(tag)
        yield targets
    finally:
        for tag in built:
            _run(["docker", "rmi", "-f", tag])


@requires_docker
class TestImageBuilds:
    def test_all_three_targets_build_successfully(self, built_images: dict) -> None:
        assert set(built_images.keys()) == {"api", "streamlit", "discord"}

    def test_every_image_runs_as_a_non_root_user(self, built_images: dict) -> None:
        for tag in built_images.values():
            result = _run(["docker", "run", "--rm", "--entrypoint", "python", tag, "-c", "import os; print(os.getuid())"])
            assert result.returncode == 0, result.stderr
            assert result.stdout.strip() != "0", f"{tag} ran as root"

    def test_no_dotenv_in_any_image(self, built_images: dict) -> None:
        for tag in built_images.values():
            result = _run(["docker", "run", "--rm", "--entrypoint", "sh", tag, "-c", "find / -xdev -iname '.env' 2>/dev/null"])
            assert result.stdout.strip() == "", f"{tag} contains a .env file"

    def test_no_git_directory_in_any_image(self, built_images: dict) -> None:
        for tag in built_images.values():
            result = _run(["docker", "run", "--rm", "--entrypoint", "sh", tag, "-c", "[ -d /app/.git ] && echo FOUND || echo absent"])
            assert result.stdout.strip() == "absent", f"{tag} contains .git"

    def test_no_local_data_directories_in_any_image(self, built_images: dict) -> None:
        for tag in built_images.values():
            result = _run([
                "docker", "run", "--rm", "--entrypoint", "sh", tag, "-c",
                "for d in TeamDump cache PlayerCards; do [ -e /app/$d ] && echo FOUND:$d; done; echo DONE",
            ])
            assert "FOUND" not in result.stdout, f"{tag}: {result.stdout}"

    def test_only_the_registry_notebook_is_in_any_image(self, built_images: dict) -> None:
        """Exactly one notebook is a runtime *data* input (`load_conferences()` parses it, never executes it); no other notebook may enter any image."""
        for tag in built_images.values():
            result = _run(["docker", "run", "--rm", "--entrypoint", "sh", tag, "-c", "find /app -iname '*.ipynb' 2>/dev/null"])
            assert result.stdout.split() == ["/app/cbb_teamScrape_v15_TEST.ipynb"], f"{tag}: {result.stdout!r}"

    def test_rankings_identity_data_files_present_in_every_image(self, built_images: dict) -> None:
        """Regression for the second Phase 9A defect: `docs/sr_slug_reconciliation.json` is read on every NET-ranking identity resolution, so its absence only surfaced as a 500 on the Rankings route."""
        for tag in built_images.values():
            result = _run([
                "docker", "run", "--rm", "--entrypoint", "python", tag, "-c",
                "from src.identity.sr_slug_reconciliation import reconciliation_disputes; reconciliation_disputes(); print('recon-ok')",
            ])
            assert result.returncode == 0 and "recon-ok" in result.stdout, f"{tag}: {result.stderr[-400:]}"

    def test_only_the_reconciliation_file_is_copied_from_docs(self, built_images: dict) -> None:
        for tag in built_images.values():
            result = _run(["docker", "run", "--rm", "--entrypoint", "sh", tag, "-c", "ls /app/docs"])
            assert result.stdout.split() == ["sr_slug_reconciliation.json"], f"{tag}: {result.stdout!r}"

    def test_only_accepted_streamlit_pages_in_every_image(self, built_images: dict) -> None:
        """Holds even when built from a dirty working tree that contains untracked pages 01-09."""
        for tag in built_images.values():
            result = _run(["docker", "run", "--rm", "--entrypoint", "sh", tag, "-c", "ls /app/pages | grep -v __pycache__"])
            assert result.stdout.split() == [
                "10_Team_Lookup.py", "11_Player_Raw_Statistics.py", "12_Matchup_Feature_Comparison.py",
            ], f"{tag}: {result.stdout!r}"

    def test_team_registry_builds_inside_every_image(self, built_images: dict) -> None:
        """Regression for the Phase 9A defect where an image without the registry notebook passed every health check yet failed every team-resolving request."""
        for tag in built_images.values():
            result = _run([
                "docker", "run", "--rm", "--entrypoint", "python", tag, "-c",
                "from src.identity.team_identity import ensure_team_registry; ensure_team_registry(); print('registry-ok')",
            ])
            assert result.returncode == 0 and "registry-ok" in result.stdout, f"{tag}: {result.stderr[-400:]}"

    def test_requirements_dev_not_present(self, built_images: dict) -> None:
        for tag in built_images.values():
            result = _run(["docker", "run", "--rm", "--entrypoint", "sh", tag, "-c", "[ -f /app/requirements-dev.txt ] && echo FOUND || echo absent"])
            assert result.stdout.strip() == "absent", f"{tag} contains requirements-dev.txt"

    def test_rankings_csv_present_but_not_whole_reference_directory(self, built_images: dict) -> None:
        tag = built_images["api"]
        result = _run(["docker", "run", "--rm", "--entrypoint", "sh", tag, "-c", "ls '/app/Reference Py Files/' | wc -l"])
        assert result.stdout.strip() == "1", "exactly the one CSV file, nothing else from that directory"


@requires_docker
class TestApiContainer:
    @pytest.fixture
    def running_api(self, built_images: dict):
        name = "napsedge-api-phase9a-test"
        _run(["docker", "rm", "-f", name])
        result = _run([
            "docker", "run", "-d", "--name", name,
            "-p", "127.0.0.1:18100:8000",
            "-e", f"NAPSEDGE_API_KEY_HASHES_JSON={_VALID_API_KEY_HASHES_JSON}",
            "-e", "NAPSEDGE_NET_RANKINGS_SEASON=2026",
            built_images["api"],
        ])
        assert result.returncode == 0, result.stderr
        try:
            _wait_for_http("http://127.0.0.1:18100/v1/health", timeout=20)
            yield name
        finally:
            _run(["docker", "stop", name])
            _run(["docker", "rm", "-f", name])

    def test_health_endpoint_responds_ok(self, running_api: str) -> None:
        with urllib.request.urlopen("http://127.0.0.1:18100/v1/health", timeout=5) as resp:
            assert resp.status == 200

    def test_ready_endpoint_reflects_unmigrated_database(self, running_api: str) -> None:
        """A fresh container's SQLite fallback has no migrations applied yet -- /v1/ready must truthfully report not-ready, never a false positive."""
        try:
            urllib.request.urlopen("http://127.0.0.1:18100/v1/ready", timeout=5)
            raise AssertionError("expected HTTPError 503")
        except urllib.error.HTTPError as exc:
            assert exc.code == 503

    def test_protected_route_rejects_missing_credential(self, running_api: str) -> None:
        try:
            urllib.request.urlopen("http://127.0.0.1:18100/v1/teams/resolve?query=duke", timeout=5)
            raise AssertionError("expected HTTPError 401")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401

    def test_host_port_bound_to_loopback_only(self, running_api: str) -> None:
        result = _run(["docker", "port", running_api])
        assert "127.0.0.1:18100" in result.stdout
        assert "0.0.0.0:18100" not in result.stdout


@requires_docker
class TestStreamlitContainer:
    @pytest.fixture
    def running_streamlit(self, built_images: dict):
        name = "napsedge-streamlit-phase9a-test"
        _run(["docker", "rm", "-f", name])
        result = _run([
            "docker", "run", "-d", "--name", name,
            "-p", "127.0.0.1:18501:8501",
            "-e", "NAPSEDGE_NET_RANKINGS_SEASON=2026",
            built_images["streamlit"],
        ])
        assert result.returncode == 0, result.stderr
        try:
            _wait_for_http("http://127.0.0.1:18501/_stcore/health", timeout=30)
            yield name
        finally:
            _run(["docker", "stop", name])
            _run(["docker", "rm", "-f", name])

    def test_health_endpoint_responds_ok(self, running_streamlit: str) -> None:
        with urllib.request.urlopen("http://127.0.0.1:18501/_stcore/health", timeout=5) as resp:
            assert resp.status == 200

    def test_host_port_bound_to_loopback_only(self, running_streamlit: str) -> None:
        result = _run(["docker", "port", running_streamlit])
        assert "127.0.0.1:18501" in result.stdout
        assert "0.0.0.0:18501" not in result.stdout


@requires_docker
class TestDiscordContainerSafety:
    def test_check_only_mode_exits_zero_without_connecting(self, built_images: dict) -> None:
        result = _run([
            "docker", "run", "--rm",
            "-e", "DISCORD_BOT_TOKEN=synthetic-fake-token-not-real",
            built_images["discord"], "--check-only",
        ])
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout
        assert "no connection attempted" in result.stdout

    def test_check_only_mode_without_token_fails_closed(self, built_images: dict) -> None:
        result = _run(["docker", "run", "--rm", built_images["discord"], "--check-only"])
        assert result.returncode == 1
        assert "DISCORD_BOT_TOKEN" in result.stderr

    def test_no_containers_left_running_after_check_only(self, built_images: dict) -> None:
        _run(["docker", "run", "--rm", "-e", "DISCORD_BOT_TOKEN=synthetic", built_images["discord"], "--check-only"])
        result = _run(["docker", "ps", "-a", "--filter", f"ancestor={built_images['discord']}", "--format", "{{.Names}}"])
        assert result.stdout.strip() == ""


@requires_docker
class TestComposeDefaultProfile:
    def test_config_parses_with_synthetic_values(self) -> None:
        env = {
            "NAPSEDGE_API_KEY_HASHES_JSON": _VALID_API_KEY_HASHES_JSON,
            "NAPSEDGE_NET_RANKINGS_SEASON": "2026",
            "DISCORD_BOT_TOKEN": "synthetic",
            "NAPSEDGE_LOCAL_DB_PASSWORD": "synthetic-ephemeral",
        }
        import os

        result = _run(["docker", "compose", "config", "--quiet"], env={**os.environ, **env})
        assert result.returncode == 0, result.stderr

    def test_default_up_starts_only_api_and_streamlit(self) -> None:
        import os

        env = {
            **os.environ,
            "NAPSEDGE_API_KEY_HASHES_JSON": _VALID_API_KEY_HASHES_JSON,
            "NAPSEDGE_NET_RANKINGS_SEASON": "2026",
        }
        try:
            result = _run(["docker", "compose", "up", "-d"], env=env, timeout=300)
            assert result.returncode == 0, result.stderr
            ps = _run(["docker", "compose", "ps", "--format", "{{.Service}}"], env=env)
            services = set(ps.stdout.split())
            assert services == {"api", "streamlit"}
        finally:
            _run(["docker", "compose", "down", "--remove-orphans", "-v"], env=env)

    def test_discord_profile_check_only_exits_without_connecting(self) -> None:
        import os

        env = {
            **os.environ,
            "NAPSEDGE_API_KEY_HASHES_JSON": _VALID_API_KEY_HASHES_JSON,
            "NAPSEDGE_NET_RANKINGS_SEASON": "2026",
            "DISCORD_BOT_TOKEN": "synthetic-fake-token",
        }
        try:
            result = _run(
                ["docker", "compose", "--profile", "discord", "run", "--rm", "discord", "--check-only"],
                env=env,
            )
            assert result.returncode == 0, result.stderr
            assert "OK" in result.stdout
        finally:
            _run(["docker", "compose", "down", "--remove-orphans", "-v"], env=env)


def _wait_for_http(url: str, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=3)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(1)
    raise AssertionError(f"{url} did not become reachable within {timeout}s: {last_error}")
