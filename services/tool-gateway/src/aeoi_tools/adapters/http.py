"""Outbound HTTP for tools, behind an egress allow-list.

Every HTTP-backed tool uses ONE client built here. The transport refuses any host that is not
in the allow-list BEFORE a connection is opened - so a tool bug, a redirect, or a URL that
came from untrusted text cannot make the gateway call somewhere new (SSRF / exfiltration).
Redirects are off: a 302 to another host is just another way to leave the allow-list.
"""

from __future__ import annotations

import httpx

from aeoi_tools.contracts import EgressBlockedError


class EgressGuardTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport, allowed: frozenset[str]) -> None:
        self._inner = inner
        # host:port, lower-case. Port is part of the identity: localhost:8004 != localhost:6379
        self._allowed = frozenset(a.lower() for a in allowed)

    @staticmethod
    def origin(url: httpx.URL) -> str:
        port = url.port or (443 if url.scheme == "https" else 80)
        return f"{url.host.lower()}:{port}"

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        origin = self.origin(request.url)
        if request.url.scheme not in ("http", "https") or origin not in self._allowed:
            raise EgressBlockedError(f"egress to '{origin}' is not in the allow-list")
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def origin_of(url: str) -> str:
    return EgressGuardTransport.origin(httpx.URL(url))


def make_client(
    allowed_origins: frozenset[str],
    *,
    timeout_s: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=EgressGuardTransport(transport or httpx.AsyncHTTPTransport(), allowed_origins),
        timeout=httpx.Timeout(timeout_s, connect=1.0),
        follow_redirects=False,
        trust_env=False,  # no proxy env vars: the allow-list must be the whole story
    )
