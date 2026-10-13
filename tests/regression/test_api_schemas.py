"""
Phase 6B: contract tests for the Pydantic response models at the API's
external boundary. No FastAPI app/route involved -- pure schema validation.
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from src.api.schemas import ErrorResponse, HealthResponse, ReadyResponse


def test_health_response_accepts_only_the_documented_status_value():
    resp = HealthResponse(status="ok")
    assert resp.status == "ok"

    with pytest.raises(ValidationError):
        HealthResponse(status="not-a-real-status")


def test_ready_response_accepts_only_the_documented_status_value():
    resp = ReadyResponse(status="ready")
    assert resp.status == "ready"

    with pytest.raises(ValidationError):
        ReadyResponse(status="not-a-real-status")


def test_error_response_requires_both_fields_as_strings():
    resp = ErrorResponse(error="not_ready", message="service is not ready")
    assert resp.error == "not_ready"
    assert resp.message == "service is not ready"

    with pytest.raises(ValidationError):
        ErrorResponse(error="not_ready")  # missing message

    with pytest.raises(ValidationError):
        ErrorResponse(message="service is not ready")  # missing error


def test_health_response_serializes_to_deterministic_json():
    resp = HealthResponse(status="ok")
    payload = json.loads(resp.model_dump_json())
    assert payload == {"status": "ok"}


def test_ready_response_serializes_to_deterministic_json():
    resp = ReadyResponse(status="ready")
    payload = json.loads(resp.model_dump_json())
    assert payload == {"status": "ready"}


def test_error_response_has_no_extra_fields_that_could_leak_internal_detail():
    resp = ErrorResponse(error="internal_error", message="an internal error occurred")
    payload = json.loads(resp.model_dump_json())
    assert set(payload.keys()) == {"error", "message"}
