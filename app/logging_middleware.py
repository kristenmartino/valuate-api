"""Structured JSON request logging with per-request correlation IDs.

Why JSON: Railway's log search is plaintext-only by default but JSON lines
are still grep-able and become ingestible if anyone later pipes the logs
to Datadog / Better Stack / etc. The schema is small and stable:

    {
      "ts": "2026-05-15T16:21:34.123Z",
      "level": "info",
      "request_id": "01HXXX...",
      "method": "POST",
      "path": "/extract",
      "status": 200,
      "duration_ms": 14210,
      "client_ip": "203.0.113.5"
    }

The request_id is exposed in the X-Request-ID response header so a user
who hits an error can paste the ID into a bug report and it can be grepped
out of the logs. Generated as a ULID-style timestamp + random suffix —
no external dep required.

This replaces the prior `print(..., file=sys.stderr)` calls in graph.py
that lacked structure and correlation. The pipeline-level Track-B
failure log stays as-is for now (it's a transient, best-effort failure
in the middle of a request and would conflate with the request log line).
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware


def _generate_request_id() -> str:
    """ULID-style: 10 hex chars of timestamp + 12 hex chars of randomness.

    Timestamp prefix means request IDs sort lexically by request time —
    useful when grep'ing logs around an incident.
    """
    ts_ms = int(time.time() * 1000)
    return f"{ts_ms:013x}-{secrets.token_hex(6)}"


def _client_ip(request: Request) -> str:
    """Mirror the resolver in app/rate_limit.py — Railway sits behind an
    edge proxy, so the first X-Forwarded-For hop is the real client."""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# Endpoints to skip logging for (healthcheck spam from Railway probes
# would otherwise dominate the log volume).
_SKIP_PATHS = {"/healthz"}


class StructuredRequestLoggingMiddleware(BaseHTTPMiddleware):
    """Emit one JSON log line per request, with timing + correlation ID.

    The middleware injects the X-Request-ID header on every response so
    error reports can be grepped out of the logs by ID.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Allow callers to pass their own X-Request-ID for cross-service
        # correlation; otherwise generate one.
        request_id = request.headers.get("X-Request-ID") or _generate_request_id()
        request.state.request_id = request_id

        start = time.perf_counter()
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception as exc:  # noqa: BLE001
            duration_ms = int((time.perf_counter() - start) * 1000)
            self._emit(
                level="error",
                request_id=request_id,
                method=request.method,
                path=request.url.path,
                status=500,
                duration_ms=duration_ms,
                client_ip=_client_ip(request),
                error=type(exc).__name__,
                error_msg=str(exc)[:500],
            )
            raise

        duration_ms = int((time.perf_counter() - start) * 1000)
        response.headers["X-Request-ID"] = request_id

        if request.url.path not in _SKIP_PATHS:
            self._emit(
                level="info" if status < 400 else "warn",
                request_id=request_id,
                method=request.method,
                path=request.url.path,
                status=status,
                duration_ms=duration_ms,
                client_ip=_client_ip(request),
            )
        return response

    @staticmethod
    def _emit(**fields: object) -> None:
        """Single line of JSON to stdout. Stdout (not stderr) so Railway's
        log streaming captures these as-is and existing stderr noise
        (deprecation warnings, transient Track-B failures) doesn't get
        confused with structured request logs."""
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            **fields,
        }
        print(json.dumps(payload, separators=(",", ":")), file=sys.stdout, flush=True)


def install_structured_logging(app) -> None:
    """Attach the middleware to a FastAPI app.

    Skipped when VALUATE_DISABLE_STRUCTURED_LOGGING=1 — useful for local
    dev when you'd rather just see plain Python tracebacks in stderr.
    """
    if os.environ.get("VALUATE_DISABLE_STRUCTURED_LOGGING") == "1":
        return
    app.add_middleware(StructuredRequestLoggingMiddleware)
