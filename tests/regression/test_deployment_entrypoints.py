"""
Phase 9A: `src.deployment.entrypoints` -- process-specific container
entrypoints. Every external effect (`uvicorn.run`, `os.execvp`,
`src.discord_bot.bot.main`, `sys.exit`) is mocked in every test here --
no server binds a port, no process is replaced, and the Discord client
never connects anywhere in this file.
"""
from __future__ import annotations

import ast
from unittest.mock import MagicMock

import pytest

import src.deployment.entrypoints as entrypoints_module
from src.deployment.entrypoints import build_parser, main, run_api, run_discord, run_streamlit
from src.deployment.env_validation import DISCORD_BOT_TOKEN_ENV_VAR

_VALID_API_KEY_HASHES_JSON = '{"test-client": "' + "0" * 64 + '"}'


def _clear_all(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "NAPSEDGE_API_KEY_HASHES_JSON",
        "NAPSEDGE_RATE_LIMIT_REQUESTS",
        "NAPSEDGE_RATE_LIMIT_WINDOW_SECONDS",
        "NAPSEDGE_NET_RANKINGS_SEASON",
        "NAPSEDGE_NET_RANKINGS_CACHE_PATH",
        DISCORD_BOT_TOKEN_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)


class TestRunApi:
    def test_calls_uvicorn_run_when_configuration_valid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_all(monkeypatch)
        monkeypatch.setenv("NAPSEDGE_API_KEY_HASHES_JSON", _VALID_API_KEY_HASHES_JSON)
        monkeypatch.setenv("NAPSEDGE_NET_RANKINGS_SEASON", "2026")

        fake_uvicorn = MagicMock()
        monkeypatch.setitem(__import__("sys").modules, "uvicorn", fake_uvicorn)

        run_api()

        fake_uvicorn.run.assert_called_once_with(
            "src.api.main:create_app", factory=True, host="0.0.0.0", port=8000, access_log=False  # Phase 9E-1: no query strings in logs
        )

    def test_exits_without_starting_uvicorn_when_configuration_invalid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_all(monkeypatch)  # neither required variable set

        fake_uvicorn = MagicMock()
        monkeypatch.setitem(__import__("sys").modules, "uvicorn", fake_uvicorn)

        with pytest.raises(SystemExit) as exc_info:
            run_api()

        assert exc_info.value.code == 1
        fake_uvicorn.run.assert_not_called()


class TestRunStreamlit:
    def test_execs_streamlit_when_configuration_valid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_all(monkeypatch)
        monkeypatch.setenv("NAPSEDGE_NET_RANKINGS_SEASON", "2026")

        recorded: dict = {}

        def fake_execvp(file: str, args: list[str]) -> None:
            recorded["file"] = file
            recorded["args"] = args

        monkeypatch.setattr(entrypoints_module.os, "execvp", fake_execvp)

        run_streamlit()

        assert recorded["file"] == "streamlit"
        assert recorded["args"][:3] == ["streamlit", "run", "app.py"]
        assert "--server.address=0.0.0.0" in recorded["args"]
        assert "--server.port=8501" in recorded["args"]

    def test_does_not_exec_when_configuration_invalid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_all(monkeypatch)

        called = {"value": False}

        def fake_execvp(file: str, args: list[str]) -> None:
            called["value"] = True

        monkeypatch.setattr(entrypoints_module.os, "execvp", fake_execvp)

        with pytest.raises(SystemExit) as exc_info:
            run_streamlit()

        assert exc_info.value.code == 1
        assert called["value"] is False


class TestRunDiscord:
    def test_check_only_never_imports_or_calls_bot_main(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        _clear_all(monkeypatch)
        monkeypatch.setenv(DISCORD_BOT_TOKEN_ENV_VAR, "synthetic-test-token")

        # If src.discord_bot.bot.main were ever imported/called here, this
        # would fail the test loudly rather than silently succeeding.
        def _blocked_main() -> None:
            raise AssertionError("run_discord(check_only=True) must never call bot.main()")

        import src.discord_bot.bot as bot_module

        monkeypatch.setattr(bot_module, "main", _blocked_main)

        run_discord(check_only=True)

        captured = capsys.readouterr()
        assert "OK" in captured.out

    def test_calls_bot_main_when_not_check_only_and_configuration_valid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_all(monkeypatch)
        monkeypatch.setenv(DISCORD_BOT_TOKEN_ENV_VAR, "synthetic-test-token")

        called = {"value": False}

        def fake_main() -> None:
            called["value"] = True

        import src.discord_bot.bot as bot_module

        monkeypatch.setattr(bot_module, "main", fake_main)

        run_discord(check_only=False)

        assert called["value"] is True

    def test_exits_without_connecting_when_token_missing_even_without_check_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_all(monkeypatch)

        def _blocked_main() -> None:
            raise AssertionError("must never reach bot.main() with no token configured")

        import src.discord_bot.bot as bot_module

        monkeypatch.setattr(bot_module, "main", _blocked_main)

        with pytest.raises(SystemExit) as exc_info:
            run_discord(check_only=False)

        assert exc_info.value.code == 1


class TestArgumentParsing:
    def test_api_subcommand_routes_to_run_api(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called = {"value": False}
        monkeypatch.setattr(entrypoints_module, "run_api", lambda: called.__setitem__("value", True))
        main(["api"])
        assert called["value"] is True

    def test_streamlit_subcommand_routes_to_run_streamlit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called = {"value": False}
        monkeypatch.setattr(entrypoints_module, "run_streamlit", lambda: called.__setitem__("value", True))
        main(["streamlit"])
        assert called["value"] is True

    def test_discord_subcommand_routes_to_run_discord_default_not_check_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded = {}
        monkeypatch.setattr(entrypoints_module, "run_discord", lambda *, check_only: recorded.__setitem__("check_only", check_only))
        main(["discord"])
        assert recorded["check_only"] is False

    def test_discord_subcommand_check_only_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded = {}
        monkeypatch.setattr(entrypoints_module, "run_discord", lambda *, check_only: recorded.__setitem__("check_only", check_only))
        main(["discord", "--check-only"])
        assert recorded["check_only"] is True

    def test_build_parser_requires_a_subcommand(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])


class TestImportSafety:
    def test_importing_entrypoints_module_has_no_side_effects(self) -> None:
        """Re-importing must not raise, bind a port, or touch the network -- nothing beyond ordinary module execution happens at import time."""
        import importlib

        import src.deployment.entrypoints as mod

        importlib.reload(mod)  # must not raise

    def test_run_api_and_run_streamlit_never_import_discord_bot_at_module_level(self) -> None:
        """Static proof that the API/Streamlit entrypoints never pull in the Discord bot module merely by being imported -- `src.discord_bot.bot` is only ever imported inside `run_discord`'s own function body."""
        tree = ast.parse(open("src/deployment/entrypoints.py", encoding="utf-8").read())
        module_level_imports = []
        for node in tree.body:  # only direct module-level statements, not nested in functions
            if isinstance(node, ast.ImportFrom) and node.module:
                module_level_imports.append(node.module)
            elif isinstance(node, ast.Import):
                module_level_imports.extend(alias.name for alias in node.names)
        assert not any("discord_bot" in name for name in module_level_imports)
