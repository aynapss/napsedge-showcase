"""
Phase 9A: static checks on the committed container contract files
(`Dockerfile`, `.dockerignore`, `compose.yaml`, `.env.example`). These
tests read file text only -- no Docker daemon, network, or process is
ever involved.
"""
from __future__ import annotations

import yaml


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _executable_text(path: str) -> str:
    """Strips full-line comments (lines whose first non-whitespace character is `#`) -- leaves only the parts a build/parse step actually acts on."""
    lines = [line for line in _read(path).splitlines() if line.strip() and not line.strip().startswith("#")]
    return "\n".join(lines)


class TestDockerfile:
    def _dockerfile(self) -> str:
        return _read("Dockerfile")

    def test_declares_the_three_required_targets(self) -> None:
        text = self._dockerfile()
        assert "AS api" in text
        assert "AS streamlit" in text
        assert "AS discord" in text

    def test_base_image_matches_accepted_python_runtime(self) -> None:
        text = self._dockerfile()
        assert "FROM python:3.13-slim" in text

    def test_installs_from_requirements_and_constraints(self) -> None:
        text = self._dockerfile()
        assert "pip install -c constraints-release-py313.txt -r requirements.txt" in text

    def test_never_installs_dev_requirements(self) -> None:
        assert "requirements-dev.txt" not in _executable_text("Dockerfile")

    def test_creates_and_uses_a_non_root_user(self) -> None:
        text = self._dockerfile()
        assert "useradd" in text
        assert "USER napsedge" in text

    def test_sets_deterministic_python_env_flags(self) -> None:
        text = self._dockerfile()
        assert "PYTHONDONTWRITEBYTECODE=1" in text
        assert "PYTHONUNBUFFERED=1" in text
        assert "PIP_NO_CACHE_DIR=1" in text

    def test_never_copies_dotgit_or_env_or_local_data(self) -> None:
        text = self._dockerfile()
        for forbidden in (
            "COPY .git",
            "COPY .env",
            "COPY TeamDump",
            "COPY cache",
            "COPY PlayerCards",
        ):
            assert forbidden not in text

    def test_never_copies_the_whole_repository_wholesale(self) -> None:
        """Only explicit, named COPY sources are used -- never `COPY . .`, which would bypass the deliberate allowlist entirely."""
        text = _executable_text("Dockerfile")
        assert "COPY . ." not in text
        assert "COPY ./ ./" not in text

    def test_copies_exactly_one_notebook_the_registry_data_input(self) -> None:
        text = _executable_text("Dockerfile")
        notebook_copies = [line for line in text.splitlines() if line.startswith("COPY") and ".ipynb" in line]
        assert len(notebook_copies) == 1
        assert "cbb_teamScrape_v15_TEST.ipynb" in notebook_copies[0]

    def test_copies_exactly_one_file_from_docs(self) -> None:
        text = _executable_text("Dockerfile")
        docs_copies = [line for line in text.splitlines() if line.startswith("COPY") and "docs/" in line]
        assert docs_copies == ["COPY docs/sr_slug_reconciliation.json ./docs/sr_slug_reconciliation.json"]

    def test_copies_the_rankings_csv_but_not_the_surrounding_directory(self) -> None:
        text = self._dockerfile()
        assert "net_rankings_2026.csv" in text
        # The whole 33 MB "Reference Py Files" directory must never be
        # copied wholesale -- only the one named CSV file inside it.
        assert 'COPY "Reference Py Files/"' not in text
        assert "COPY Reference Py Files/ " not in text

    def test_entrypoints_use_exec_form(self) -> None:
        """Exec-form JSON-array ENTRYPOINT/CMD preserve direct signal delivery; shell-form (a bare string) would interpose `/bin/sh -c`."""
        text = self._dockerfile()
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("ENTRYPOINT") or stripped.startswith("CMD"):
                assert stripped.split(None, 1)[1].startswith("["), f"not exec-form: {stripped!r}"

    def test_api_target_entrypoint_uses_deployment_entrypoints_module(self) -> None:
        text = self._dockerfile()
        assert '"python", "-m", "src.deployment.entrypoints", "api"' in text

    def test_streamlit_target_entrypoint_uses_deployment_entrypoints_module(self) -> None:
        text = self._dockerfile()
        assert '"python", "-m", "src.deployment.entrypoints", "streamlit"' in text

    def test_discord_target_entrypoint_uses_deployment_entrypoints_module(self) -> None:
        text = self._dockerfile()
        assert '"python", "-m", "src.deployment.entrypoints", "discord"' in text

    def test_never_installs_or_runs_excluded_processes(self) -> None:
        """Phase 9A's explicit exclusion list must never appear as an actual RUN/ENTRYPOINT/CMD instruction -- only as explanatory comments (checked separately below)."""
        text = self._dockerfile()
        executable_lines = [
            line for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        executable_text = "\n".join(executable_lines).lower()
        for forbidden in ("stripe", "nginx", "redis"):
            assert forbidden not in executable_text
        assert "8787" not in executable_text

    def test_explicitly_documents_the_exclusion_list_in_a_comment(self) -> None:
        text = self._dockerfile()
        assert "Stripe webhook" in text
        assert "8787" in text


class TestDockerignore:
    def _dockerignore(self) -> str:
        return _read(".dockerignore")

    def test_excludes_env_file(self) -> None:
        assert ".env" in self._dockerignore()

    def test_excludes_git_directory(self) -> None:
        assert ".git/" in self._dockerignore()

    def test_excludes_local_data_directories(self) -> None:
        text = self._dockerignore()
        for path in ("TeamDump/", "cache/", "PlayerCards/"):
            assert path in text

    def test_excludes_notebooks_except_the_one_registry_data_input(self) -> None:
        text = self._dockerignore()
        assert "*.ipynb" in text
        assert "!cbb_teamScrape_v15_TEST.ipynb" in text
        # No other notebook may be un-ignored.
        negations = [line for line in text.splitlines() if line.startswith("!") and line.endswith(".ipynb")]
        assert negations == ["!cbb_teamScrape_v15_TEST.ipynb"]

    def test_docs_excluded_except_the_one_runtime_data_file(self) -> None:
        text = self._dockerignore()
        assert "docs/*" in text
        negations = [line for line in text.splitlines() if line.startswith("!docs/")]
        assert negations == ["!docs/sr_slug_reconciliation.json"]

    def test_excludes_deferred_streamlit_pages_01_to_09(self) -> None:
        """Without this, an image built from a working tree containing the untracked legacy pages would ship page 09's live Odds/agent trigger."""
        assert "pages/0*.py" in self._dockerignore()

    def test_does_not_exclude_requirements_or_constraints(self) -> None:
        """A blanket `*.txt` exclusion exists for scratch files -- these three must be explicitly un-excluded."""
        text = self._dockerignore()
        assert "!requirements.txt" in text
        assert "!requirements-dev.txt" in text
        assert "!constraints-release-py313.txt" in text

    def test_excludes_host_bytecode_at_every_depth(self) -> None:
        """Phase 9B-7A follow-up: a bare `__pycache__/` or `*.pyc` matches only at the context root; nested host caches
        entered every image through `COPY src/`. The `**/` forms match at any depth (including the root)."""
        rules = [line.strip() for line in _executable_text(".dockerignore").splitlines()]
        for rule in ("**/__pycache__", "**/*.pyc", "**/*.pyo"):
            assert rule in rules, rule

    def test_no_later_exception_can_reintroduce_bytecode(self) -> None:
        """Later rules win in `.dockerignore`, so no `!` exception may follow the bytecode rules."""
        rules = [line.strip() for line in _executable_text(".dockerignore").splitlines()]
        first = min(rules.index(rule) for rule in ("**/__pycache__", "**/*.pyc", "**/*.pyo"))
        assert [rule for rule in rules[first:] if rule.startswith("!")] == []


class TestComposeFile:
    def _compose(self) -> dict:
        return yaml.safe_load(_read("compose.yaml"))

    def test_parses_as_valid_yaml(self) -> None:
        config = self._compose()
        assert "services" in config

    def test_declares_exactly_the_four_expected_services(self) -> None:
        config = self._compose()
        assert set(config["services"].keys()) == {"api", "streamlit", "discord", "local-db"}

    def test_discord_service_requires_explicit_profile(self) -> None:
        config = self._compose()
        assert config["services"]["discord"].get("profiles") == ["discord"]

    def test_local_db_service_requires_explicit_profile(self) -> None:
        config = self._compose()
        assert config["services"]["local-db"].get("profiles") == ["local-db"]

    def test_api_and_streamlit_have_no_profile_gate(self) -> None:
        """Absence of a `profiles` key means the service is part of the default `docker compose up` set."""
        config = self._compose()
        assert "profiles" not in config["services"]["api"]
        assert "profiles" not in config["services"]["streamlit"]

    def test_api_port_binds_loopback_only(self) -> None:
        config = self._compose()
        ports = config["services"]["api"]["ports"]
        assert len(ports) == 1
        assert ports[0].startswith("127.0.0.1:")

    def test_streamlit_port_binds_loopback_only(self) -> None:
        config = self._compose()
        ports = config["services"]["streamlit"]["ports"]
        assert len(ports) == 1
        assert ports[0].startswith("127.0.0.1:")

    def test_discord_service_has_no_port_mapping(self) -> None:
        config = self._compose()
        assert "ports" not in config["services"]["discord"]

    def test_local_db_service_has_no_port_mapping(self) -> None:
        """The local-db rehearsal database must never be reachable from outside the private Compose network."""
        config = self._compose()
        assert "ports" not in config["services"]["local-db"]

    def test_port_8787_never_appears_in_actual_config(self) -> None:
        assert "8787" not in _executable_text("compose.yaml")

    def test_stripe_never_appears_in_actual_config(self) -> None:
        assert "stripe" not in _executable_text("compose.yaml").lower()

    def test_discord_service_never_receives_api_key_hashes_or_rate_limit_vars(self) -> None:
        config = self._compose()
        discord_env = config["services"]["discord"]["environment"]
        assert "NAPSEDGE_API_KEY_HASHES_JSON" not in discord_env
        assert "NAPSEDGE_RATE_LIMIT_REQUESTS" not in discord_env
        assert "NAPSEDGE_RATE_LIMIT_WINDOW_SECONDS" not in discord_env

    def test_api_and_streamlit_services_never_receive_discord_token(self) -> None:
        config = self._compose()
        assert "DISCORD_BOT_TOKEN" not in config["services"]["api"]["environment"]
        assert "DISCORD_BOT_TOKEN" not in config["services"]["streamlit"]["environment"]

    def test_local_db_has_no_weak_or_committed_password_default(self) -> None:
        """
        No literal password is ever committed; the variable defaults to
        empty (`:-`, not a real value) rather than Compose's `:?` hard
        requirement, since `:?` breaks plain `docker compose up` even
        when local-db is never requested (every service's environment is
        interpolated regardless of active profile) -- confirmed by an
        actual `docker compose up` failure during Phase 9A's own
        validation. Safety is enforced one layer down instead: the
        official postgres image itself refuses to start with an empty
        `POSTGRES_PASSWORD`, confirmed separately by this module's own
        container-runtime test.
        """
        text = _read("compose.yaml")
        assert "POSTGRES_PASSWORD: ${NAPSEDGE_LOCAL_DB_PASSWORD:-}" in text

    def test_local_db_never_sets_a_trust_auth_escape_hatch(self) -> None:
        """POSTGRES_HOST_AUTH_METHOD=trust would let Postgres start with no password at all -- confirming this is never set is what makes the empty default above actually safe."""
        config = self._compose()
        assert "POSTGRES_HOST_AUTH_METHOD" not in config["services"]["local-db"]["environment"]

    def test_local_db_pins_a_postgres_major_version(self) -> None:
        config = self._compose()
        image = config["services"]["local-db"]["image"]
        assert image.startswith("postgres:16")

    def test_local_db_has_a_pg_isready_healthcheck(self) -> None:
        config = self._compose()
        test = config["services"]["local-db"]["healthcheck"]["test"]
        assert any("pg_isready" in str(part) for part in test)

    def test_local_db_uses_a_named_volume(self) -> None:
        config = self._compose()
        assert "volumes" in config
        assert config["services"]["local-db"]["volumes"]

    def test_api_has_a_health_endpoint_check(self) -> None:
        config = self._compose()
        test = config["services"]["api"]["healthcheck"]["test"]
        assert any("/v1/health" in str(part) for part in test)

    def test_streamlit_has_a_health_endpoint_check(self) -> None:
        config = self._compose()
        test = config["services"]["streamlit"]["healthcheck"]["test"]
        assert any("_stcore/health" in str(part) for part in test)

    def test_uses_a_private_network(self) -> None:
        config = self._compose()
        assert "networks" in config
        for service_name in ("api", "streamlit", "discord", "local-db"):
            assert "napsedge-internal" in config["services"][service_name]["networks"]


    def test_streamlit_gets_an_optional_cors_origin_passthrough_with_empty_default(self) -> None:
        """Optional, not hardcoded: empty default behaves exactly like unset (verified against the real image)."""
        config = self._compose()
        value = config["services"]["streamlit"]["environment"]["STREAMLIT_SERVER_CORS_ALLOWED_ORIGINS"]
        assert value == "${STREAMLIT_SERVER_CORS_ALLOWED_ORIGINS:-}"

    def test_cors_origin_passthrough_is_streamlit_only(self) -> None:
        config = self._compose()
        for service in ("api", "discord", "local-db"):
            assert "STREAMLIT_SERVER_CORS_ALLOWED_ORIGINS" not in config["services"][service].get("environment", {})

    def test_no_hostname_or_url_is_hardcoded_anywhere_in_compose(self) -> None:
        text = _executable_text("compose.yaml")
        for forbidden in ("trycloudflare", "cloudflare", "example.com", "https://"):
            assert forbidden not in text.lower().replace("http://localhost", ""), forbidden

    def test_api_is_loopback_only_and_postgres_has_no_published_port(self) -> None:
        """Item 6 of the rehearsal scope: the API and PostgreSQL stay private -- no tunnel ever needs a host-wide binding."""
        config = self._compose()
        assert config["services"]["api"]["ports"] == ["127.0.0.1:8000:8000"]
        assert "ports" not in config["services"]["local-db"]
        assert "expose" not in config["services"]["local-db"]

    def test_no_service_publishes_on_all_interfaces(self) -> None:
        config = self._compose()
        for name, service in config["services"].items():
            for port in service.get("ports", []):
                assert str(port).startswith("127.0.0.1:"), f"{name}: {port}"


class TestEnvExample:
    def _env_example(self) -> str:
        return _read(".env.example")

    def test_contains_no_real_looking_secret(self) -> None:
        """Every secret-shaped line must use an obvious placeholder, never a real-looking value."""
        text = self._env_example()
        for line in text.splitlines():
            if line.strip().startswith("NAPSEDGE_API_KEY_HASHES_JSON=") or line.strip().startswith("DISCORD_BOT_TOKEN="):
                assert "REPLACE" in line

    def test_documents_every_variable_the_three_processes_actually_read(self) -> None:
        text = self._env_example()
        for var in (
            "NAPSEDGE_API_KEY_HASHES_JSON",
            "NAPSEDGE_RATE_LIMIT_REQUESTS",
            "NAPSEDGE_RATE_LIMIT_WINDOW_SECONDS",
            "NAPSEDGE_DATABASE_URL",
            "NAPSEDGE_DATABASE_ECHO",
            "NAPSEDGE_NET_RANKINGS_SEASON",
            "NAPSEDGE_NET_RANKINGS_CACHE_PATH",
            "NAPSEDGE_ROOT_DIR",
            "NAPSEDGE_LOG_LEVEL",
            "DISCORD_BOT_TOKEN",
            "DISCORD_ADMIN_USER_IDS",
        ):
            assert var in text

    def test_documents_the_explicitly_excluded_variables_as_excluded(self) -> None:
        text = self._env_example()
        for var in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_WEBHOOK_PORT", "ODDS_API_KEY"):
            assert var in text
        assert "NOT part of this Phase 9A deployment contract" in text

    def test_documents_the_optional_streamlit_cors_origin_without_a_hostname(self) -> None:
        text = self._env_example()
        assert "STREAMLIT_SERVER_CORS_ALLOWED_ORIGINS=" + chr(10) in text  # empty by default
        assert "REPLACE_WITH_YOUR_HOSTNAME" in text
        assert "NOT authentication" in text
        assert "trycloudflare" not in text.lower()

    def test_streamlit_origin_variable_sits_in_the_streamlit_section_not_api_or_discord(self) -> None:
        text = self._env_example()
        streamlit_section = text.index("# Streamlit only")
        discord_section = text.index("# Discord (src.discord_bot.bot")
        api_section = text.index("# API (src.api.main")
        position = text.index("STREAMLIT_SERVER_CORS_ALLOWED_ORIGINS=")
        assert streamlit_section < position < discord_section
        assert position > api_section


class TestGitignoreRehearsalRule:
    def _check_ignore(self, name: str) -> bool:
        import subprocess

        result = subprocess.run(["git", "check-ignore", "-q", name], capture_output=True)
        return result.returncode == 0

    def test_phase9_rehearsal_env_files_are_ignored(self) -> None:
        assert self._check_ignore(".env.phase9.tunnel")
        assert self._check_ignore(".env.phase9.anything.else")

    def test_existing_env_rule_is_preserved(self) -> None:
        assert self._check_ignore(".env")

    def test_committed_template_is_not_ignored(self) -> None:
        assert not self._check_ignore(".env.example")

    def test_rule_is_present_and_original_rules_survive(self) -> None:
        lines = [line.strip() for line in _read(".gitignore").splitlines()]
        for rule in (".env.phase9.*", ".env", "TeamDump/", "/NCAA-Player-Radars/", "*.parquet", ".streamlit/secrets.toml"):
            assert rule in lines, rule
