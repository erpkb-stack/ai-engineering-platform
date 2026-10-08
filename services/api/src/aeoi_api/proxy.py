"""Forward a request to an internal service.

Forwards: the user's bearer token (the service verifies it again), correlation id,
Idempotency-Key, If-Match. Never forwards arbitrary client headers (no header injection
into internal calls). Rewrites Location from /v1/... to /api/v1/...
Retries: only safe methods (GET) on CONNECT errors, once. A POST is never retried here -
the client's Idempotency-Key makes the client's own retry safe instead.
"""

from __future__ import annotations

import httpx
from fastapi import Request, Response

from aeoi_common.correlation import CORRELATION_HEADER, get_correlation_id
from aeoi_common.errors import UpstreamUnavailableError

FORWARD_REQUEST_HEADERS = ("authorization", "idempotency-key", "if-match", "content-type")
FORWARD_RESPONSE_HEADERS = (
    "content-type",
    "etag",
    "location",
    "idempotent-replayed",
    "retry-after",
)


class Upstream:
    def __init__(self, name: str, client: httpx.AsyncClient) -> None:
        self.name = name
        self.client = client

    async def forward(
        self,
        request: Request,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> Response:
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARD_REQUEST_HEADERS}
        if content_type is not None:  # the api built the body itself (e.g. investigate)
            headers["content-type"] = content_type
        headers[CORRELATION_HEADER] = get_correlation_id() or ""
        attempts = 2 if request.method == "GET" else 1
        for attempt in range(1, attempts + 1):
            try:
                upstream = await self.client.request(
                    request.method,
                    path,
                    params=list(request.query_params.multi_items()),
                    headers=headers,
                    content=body if body is not None else await request.body(),
                )
                break
            except httpx.ConnectError as exc:
                if attempt == attempts:
                    raise UpstreamUnavailableError(f"{self.name} is unavailable.") from exc
            except httpx.TimeoutException as exc:
                raise UpstreamUnavailableError(f"{self.name} timed out.") from exc
        out_headers = {
            k: v for k, v in upstream.headers.items() if k.lower() in FORWARD_RESPONSE_HEADERS
        }
        if "location" in out_headers and out_headers["location"].startswith("/v1/"):
            out_headers["location"] = "/api" + out_headers["location"]
        return Response(
            content=upstream.content, status_code=upstream.status_code, headers=out_headers
        )
