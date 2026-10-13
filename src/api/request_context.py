"""
Phase 9E-1 (decision 8, approved 2026-10-09): a request correlation ID and one access log line per API request.

- `X-Request-ID`: a client-supplied value is kept only if it is 1-64 characters of `[A-Za-z0-9._-]`; otherwise a new random ID
  is generated. It is returned in the response's `X-Request-ID` header and attached (`src.logging_config.REQUEST_ID`) to every
  log line written while the request is handled, including the sanitized error handlers' own lines.
- Access line (logger `napsedge.api.access`, INFO): method, PATH ONLY (never the query string), status and duration in ms.
"""
from __future__ import annotations

import re
import time
import uuid

from fastapi import FastAPI, Request

from src.logging_config import REQUEST_ID, get_logger

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


def install_request_context(app: FastAPI) -> None:
    log = get_logger("napsedge.api.access")

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = incoming if _VALID_REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex
        token = REQUEST_ID.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.info("%s %s -> 500 in %.1f ms", request.method, request.url.path, (time.perf_counter() - started) * 1000)
            raise
        else:
            response.headers[REQUEST_ID_HEADER] = request_id
            log.info("%s %s -> %d in %.1f ms", request.method, request.url.path, response.status_code, (time.perf_counter() - started) * 1000)
            return response
        finally:
            REQUEST_ID.reset(token)
