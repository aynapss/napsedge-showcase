"""
Phase 9E-1 (decision 8, approved 2026-10-09): structured JSON logs, the API request correlation ID with one access line per
request, the redaction safety net, and the NAPSEDGE_LOG_FORMAT startup check. The default text format is unchanged.
"""
from __future__ import annotations

import io
import json
import logging

import pytest
from fastapi.testclient import TestClient

from src.logging_config import REDACTED, REQUEST_ID, JsonFormatter, RedactingFilter, build_handler, redact, register_secret

TOKEN = "discord-token-value-for-the-test"


@pytest.fixture()
def captured(monkeypatch):
    """A root handler built exactly as production builds it, in JSON, writing to a buffer."""
    monkeypatch.setenv("NAPSEDGE_LOG_FORMAT", "json")
    buffer = io.StringIO()
    handler = build_handler(buffer)
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield buffer
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)


def lines(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


def test_json_lines_carry_utc_time_level_logger_message_and_no_request_id_outside_a_request(captured):
    logging.getLogger("napsedge.test").warning("hello %s", "world")
    entry = lines(captured)[-1]
    assert entry["level"] == "WARNING" and entry["logger"] == "napsedge.test" and entry["message"] == "hello world"
    assert entry["ts"].endswith("Z") and "T" in entry["ts"] and "request_id" not in entry


def test_the_text_format_stays_the_default_and_unchanged(monkeypatch):
    monkeypatch.delenv("NAPSEDGE_LOG_FORMAT", raising=False)
    buffer = io.StringIO()
    handler = build_handler(buffer)
    record = logging.LogRecord("napsedge.test", logging.INFO, __file__, 1, "plain %d", (7,), None)
    handler.handle(record)
    assert buffer.getvalue().rstrip().endswith("[INFO] napsedge.test: plain 7")


@pytest.fixture()
def client():
    from src.api.main import create_app

    return TestClient(create_app(database_url="sqlite+pysqlite:///:memory:"))


def test_every_response_carries_a_request_id_and_one_access_line_names_it(client, captured):
    response = client.get("/v1/health?token=should-never-be-logged")
    rid = response.headers["X-Request-ID"]
    assert response.status_code == 200 and len(rid) == 32
    ours = [e for e in lines(captured) if e["logger"].startswith(("napsedge", "src"))]  # the test client's own httpx logger is not ours
    access = [e for e in ours if e["logger"] == "napsedge.api.access"]
    assert access and access[-1]["request_id"] == rid
    assert access[-1]["message"].startswith("GET /v1/health -> 200 in ") and not any("should-never-be-logged" in e["message"] for e in ours)


@pytest.mark.parametrize("incoming, kept", [("abc-123_X.y", True), ("x" * 64, True), ("x" * 65, False), ("bad id", False), ("<script>", False), ("", False)])
def test_a_client_request_id_is_kept_only_when_it_is_safe(client, incoming, kept):
    rid = client.get("/v1/health", headers={"X-Request-ID": incoming} if incoming else {}).headers["X-Request-ID"]
    assert (rid == incoming) is kept and (kept or len(rid) == 32)


def test_log_lines_written_while_handling_a_request_carry_its_id(client, captured):
    from src.api.main import create_app

    app = create_app(database_url="sqlite+pysqlite:///:memory:")

    @app.get("/v1/_test_log")
    def _endpoint():
        logging.getLogger("napsedge.test.endpoint").info("inside the endpoint")
        return {"ok": True}

    rid = TestClient(app).get("/v1/_test_log", headers={"X-Request-ID": "trace-me"}).headers["X-Request-ID"]
    inside = [e for e in lines(captured) if e["logger"] == "napsedge.test.endpoint"]
    assert rid == "trace-me" and inside and inside[-1]["request_id"] == "trace-me"
    assert REQUEST_ID.get() is None, "the ID never leaks past its request"


def test_known_secrets_are_redacted_from_messages_and_tracebacks(captured, monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("NAPSEDGE_DATABASE_URL", "postgresql+psycopg://user:db-password-for-the-test@db.example/napsedge")
    register_secret("file-secret-for-the-test")
    log = logging.getLogger("napsedge.test.redact")
    log.error("token=%s password=db-password-for-the-test file=file-secret-for-the-test", TOKEN)
    try:
        raise RuntimeError(f"boom {TOKEN}")
    except RuntimeError:
        log.exception("failed")
    text = captured.getvalue()
    assert TOKEN not in text and "db-password-for-the-test" not in text and "file-secret-for-the-test" not in text
    assert text.count(REDACTED) >= 4


def test_a_secret_read_from_a_credential_file_is_redacted_without_any_extra_step(captured, tmp_path):
    from src.secret_files import read_secret_file

    path = tmp_path / "secret"
    path.write_bytes(b"only-in-the-credential-file-1234" + bytes([10]))  # trailing newline
    value = read_secret_file("X_FILE", str(path))
    logging.getLogger("napsedge.test.redact").error("oops: %s", value)
    assert "only-in-the-credential-file-1234" not in captured.getvalue() and REDACTED in captured.getvalue()


def test_short_values_are_never_registered_and_redact_is_a_no_op_without_secrets(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("NAPSEDGE_DATABASE_URL", raising=False)
    register_secret("abc")
    assert redact("abc is a common word") == "abc is a common word"


def test_the_filter_never_drops_a_record_and_the_formatter_tolerates_odd_values():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "%s", (object(),), None)
    assert RedactingFilter().filter(record) is True
    assert json.loads(JsonFormatter().format(record))["message"].startswith("<object object")


def test_an_unknown_log_format_is_refused_at_startup(monkeypatch):
    from src.deployment.env_validation import DeploymentConfigurationError, validate_discord_environment

    monkeypatch.setenv("DISCORD_BOT_TOKEN", TOKEN)
    monkeypatch.delenv("DISCORD_BOT_TOKEN_FILE", raising=False)
    monkeypatch.delenv("NAPSEDGE_DATABASE_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("NAPSEDGE_LOG_FORMAT", "xml")
    with pytest.raises(DeploymentConfigurationError, match="NAPSEDGE_LOG_FORMAT must be one of text, json"):
        validate_discord_environment()
    monkeypatch.setenv("NAPSEDGE_LOG_FORMAT", "json")
    validate_discord_environment()
