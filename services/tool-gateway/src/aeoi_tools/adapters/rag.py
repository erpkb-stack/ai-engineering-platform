"""Knowledge tools via the rag service. The USER's token is forwarded, so rag applies the
user's group ACL in SQL. The gateway never searches with its own identity (a service token
has no groups in rag and sees nothing - no confused deputy)."""

from __future__ import annotations

from typing import Any

import httpx

from aeoi_common.correlation import CORRELATION_HEADER
from aeoi_tools import schemas as s
from aeoi_tools.contracts import ToolContext, ToolExecutionError, ToolUnavailableError


class RagAdapter:
    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self._client = client
        self._base = base_url.rstrip("/")

    async def _post(self, path: str, body: dict[str, Any], ctx: ToolContext) -> Any:
        if ctx.user.delegation is not None:
            # rag verifies the user as a PRIMARY bearer; delegated tokens are OBO-only by
            # design (ADR-019). Fail explicitly until rag gets an OBO path (Phase 10).
            raise ToolExecutionError(
                "knowledge tools do not accept delegated calls yet (ADR-019, Phase 10)",
                status=501,
            )
        headers = {"Authorization": f"Bearer {ctx.user_token}"}
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
