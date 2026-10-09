"""Knowledge tools via the rag service, ON BEHALF OF the user (ADR-020).

Bearer = the gateway's own service token (scope `rag:obo`); X-On-Behalf-Of = the caller's
on-behalf-of token, unchanged: a user token or an investigation's delegated token. rag
verifies both and filters by the USER's groups in SQL. The gateway's identity alone sees
nothing (rag refuses a service bearer without the header - no confused deputy).
One path for both token types: Phase 9's "user token as rag's bearer" path is gone, so a
delegated token is never presented as a bearer anywhere (ADR-019 rule)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from aeoi_common.correlation import CORRELATION_HEADER
from aeoi_models.api.tools import ON_BEHALF_OF_HEADER
from aeoi_tools import schemas as s
from aeoi_tools.contracts import ToolContext, ToolExecutionError, ToolUnavailableError


class RagAdapter:
    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        service_token: Callable[[], Awaitable[str]],
    ) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._service_token = service_token

    async def _post(self, path: str, body: dict[str, Any], ctx: ToolContext) -> Any:
        try:
            own = await self._service_token()
        except OSError as exc:
            raise ToolExecutionError(
                "knowledge tools need the gateway's rag token: run make tools-tokens", status=503
            ) from exc
        headers = {
            "Authorization": f"Bearer {own}",
            ON_BEHALF_OF_HEADER: f"Bearer {ctx.user_token}",
        }
        if ctx.correlation_id:
            headers[CORRELATION_HEADER] = ctx.correlation_id
        try:
            resp = await self._client.post(self._base + path, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise ToolUnavailableError("rag timed out") from exc
        except httpx.TransportError as exc:
            raise ToolUnavailableError(f"rag unreachable: {type(exc).__name__}") from exc
        if resp.status_code >= 500 or resp.status_code == 429:
            raise ToolUnavailableError(f"rag returned {resp.status_code}")
        if resp.status_code >= 400:
            # 401/403 here means token expiry or a perms mismatch between gateway and rag.
            raise ToolExecutionError(f"rag refused the request ({resp.status_code})", status=502)
        return resp.json()

    async def search_docs(
        self, query: str, k: int, sources: list[str] | None, ctx: ToolContext
    ) -> s.DocSearchOut:
        body: dict[str, Any] = {"query": query, "k": k, "mode": "hybrid", "rerank": False}
        if sources:
            body["filters"] = {"sources": sources}
        data = await self._post("/v1/search", body, ctx)
        return s.DocSearchOut(
            items=[
                s.DocHitItem(
                    document_id=str(h["document_id"]),
                    chunk_id=str(h["chunk_id"]),
                    title=h["title"],
                    source=h["source"],
                    source_uri=h["source_uri"],
                    content=h["content"],
                    rank=h["rank"],
                )
                for h in data.get("results", [])[:k]
            ],
            embedding_model=data.get("embedding_model"),
            degraded=data.get("degraded"),
        )

    async def search_incidents(
        self, q: s.IncidentSearchIn, ctx: ToolContext
    ) -> s.IncidentSearchOut:
        data = await self._post(
            "/v1/incidents/search", {"query": q.query, "service_key": q.service_key, "k": q.k}, ctx
        )
        fields = s.IncidentHitItem.model_fields
        return s.IncidentSearchOut(
            items=[
                s.IncidentHitItem(**{k: v for k, v in h.items() if k in fields})
                for h in data[: q.k]
            ]
        )
