"""Pure-ASGI middleware (cheaper than BaseHTTPMiddleware, and streaming-safe)."""

from __future__ import annotations

import re
import time
from typing import Any

import structlog
from opentelemetry import trace
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aeoi_common.correlation import CORRELATION_HEADER, new_correlation_id, set_correlation_id
from aeoi_common.errors import database_unavailable

_VALID_CID = re.compile(r"^[A-Za-z0-9._:-]{8,80}$")
_HEADER = CORRELATION_HEADER.lower().encode()
log = structlog.get_logger("aeoi.http")


class CorrelationIdMiddleware:
    """Accept a well-formed incoming X-Correlation-ID or create one; echo it back.

    Malformed ids are replaced (not trusted): they end up in logs and audit rows.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope["headers"]).get(_HEADER, b"").decode("latin-1")
        if _VALID_CID.match(incoming):
            cid = incoming
            set_correlation_id(cid)
        else:
            cid = new_correlation_id()
        structlog.contextvars.bind_contextvars(correlation_id=cid)
        # Phase 23a: the id on the request's span too (search a trace by the id users quote)
        trace.get_current_span().set_attribute("aeoi.correlation_id", cid)
        start = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers: list[Any] = list(message.get("headers", []))
                headers.append((_HEADER, cid.encode()))
                message["headers"] = headers
            await send(message)

        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send_wrapper(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:
            # Error boundary INSIDE the correlation scope, so the error carries the id.
            if (db := database_unavailable(exc)) is not None:
                # Phase 28a: the DB down is a retryable 503, not an "unexpected" 500. The TYPE
                # only - a DB error's text can carry SQL parameters (rows).
                log.warning(
                    "dependency_unavailable",
                    dependency="database",
                    error=db,
                    path=scope.get("path"),
                )
                if response_started:
                    raise
                await _send_problem(cid, scope.get("path", ""), tracking_send, 503)
                return
            log.exception("unhandled_error", path=scope.get("path"))
            if response_started:
                raise
            await _send_problem(cid, scope.get("path", ""), tracking_send, 500)
        finally:
            log.info(
                "http_request",
                method=scope.get("method"),
                path=scope.get("path"),
                status=status,
                duration_ms=round((time.perf_counter() - start) * 1000, 1),
            )
            structlog.contextvars.unbind_contextvars("correlation_id")
            set_correlation_id(None)


_PROBLEMS = {
    500: ("internal-error", "Internal error",
          "Unexpected error. Quote the correlation_id when reporting it."),
    503: ("upstream-unavailable", "Dependency unavailable",
          "The database is unavailable. Retry later; quote the correlation_id if it lasts."),
}  # fmt: skip
DB_RETRY_AFTER_S = 5


async def _send_problem(cid: str, path: str, send: Send, status: int) -> None:
    import json

    slug, title, detail = _PROBLEMS[status]
    body = json.dumps(
        {
            "type": f"https://aeoi.example/problems/{slug}",
            "title": title,
            "status": status,
            "detail": detail,
            "instance": path,
            "correlation_id": cid,
        }
    ).encode()
    headers = [
        (b"content-type", b"application/problem+json"),
        (b"content-length", str(len(body)).encode()),
    ]
    if status == 503:
        headers.append((b"retry-after", str(DB_RETRY_AFTER_S).encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
