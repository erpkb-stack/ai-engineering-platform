"""Service catalog over HTTP: the Java service-catalog (Phase 20, ADR-032).

Same tool contract as the JSON adapter (`catalog.py`): an agent cannot tell which one answered,
and an integration test asserts they give the SAME answers for the same seed.

- Bearer = the gateway's OWN service token (scope `catalog:read`). No on-behalf-of token: the
  catalog is organisation-wide metadata every role may read (RBAC is checked at the gateway
  before a tool runs). The user's token never leaves the gateway for this tool.
- The response is parsed with the models GENERATED from the contract (extra fields forbidden):
  a contract break is a loud 502, never wrong data handed to an agent.
- Egress goes through the gateway's allow-listed client (localhost:8010 locally).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
from pydantic import ValidationError

from aeoi_tools import schemas as s
from aeoi_tools.adapters.catalog_contract import ServiceList
from aeoi_tools.contracts import ToolExecutionError, ToolUnavailableError


class CatalogHttpAdapter:
    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        service_token: Callable[[], Awaitable[str]],
    ) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._service_token = service_token

    async def query(self, q: s.CatalogIn) -> s.CatalogOut:
        try:
            token = await self._service_token()
        except OSError as exc:
            raise ToolExecutionError(
                "the catalog needs the gateway's catalog token: run make tools-tokens", status=503
            ) from exc
        params = {k: v for k, v in (("service_key", q.service_key), ("team", q.team),
                                     ("name_contains", q.name_contains)) if v}  # fmt: skip
        try:
            resp = await self._client.get(
                f"{self._base}/v1/services",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.TimeoutException as exc:
            raise ToolUnavailableError("service-catalog timed out") from exc
        except httpx.TransportError as exc:
            raise ToolUnavailableError(
                f"service-catalog unreachable: {type(exc).__name__} (make run-catalog)"
            ) from exc
        if resp.status_code >= 500 or resp.status_code == 429:
            raise ToolUnavailableError(f"service-catalog returned {resp.status_code}")
        if resp.status_code >= 400:
            # the gateway validated the input already: a 400/401/403 here is OUR bug or a
            # token/scope mismatch, not the agent's - never shown as "no such service"
            raise ToolExecutionError(
                f"service-catalog refused the request ({resp.status_code})", status=502
            )
        try:
            body = ServiceList.model_validate_json(resp.content)
        except ValidationError as exc:
            raise ToolExecutionError(
                f"service-catalog broke its contract: {exc.error_count()} error(s)", status=502
            ) from exc
        return s.CatalogOut(
            items=[s.ServiceItem(**i.model_dump()) for i in body.items], truncated=body.truncated
        )
