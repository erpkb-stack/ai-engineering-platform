"""Phase 29b (ADR-044): the service catalog, read by the alert triage (tier + dependents).

- Bearer = incident-service's OWN service token (scope `catalog:read`), minted by
  `make tools-tokens` into secrets/incident_catalog_token.txt.
- Only the 3 fields triage uses are read; other fields are ignored here (the tool-gateway
  holds the full, extra-forbid contract and the drift test against openapi.yaml).
- NEVER raises: an alert must not be lost because the catalog is down. Every failure is an
  answer (`None` + a note) and triage falls back to the label severity, saying so.
- One short attempt with a TOTAL deadline (fix5, 2026-10-29): the webhook answers
  Alertmanager, which retries on its own. The first version passed `timeout=2.0` to httpx,
  which limits EACH step (connect, write, read, pool), not the whole call: traces on the Mac
  measured 2.2-4.0 s waits for a "2 s" timeout, so incident-service answered after the api's
  5 s upstream timeout and the api said 503 for incidents that WERE created (INC-10332/34/36).
  Budget (ADR-044 amendment): api upstream 5 s > catalog 1.5 s + DB (0.3-0.7 s measured)
  + the CPU-starved slack measured on the Mac (1.3-1.5 s).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = structlog.get_logger("aeoi.incident.catalog")
DEADLINE_S = 1.5  # the WHOLE lookup (test_alert_budget keeps it well inside the api timeout)


class CatalogEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str
    tier: int = Field(ge=1, le=9)
    dependents: list[str] = Field(default_factory=list, max_length=200)


class _List(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[CatalogEntry] = Field(max_length=200)


class CatalogClient:
    def __init__(self, client: httpx.AsyncClient | None, base_url: str | None,
                 token_file: Path) -> None:  # fmt: skip
        self._client = client
        self._base = (base_url or "").rstrip("/")
        self._token_file = token_file

    @property
    def configured(self) -> bool:
        return bool(self._base) and self._client is not None

    async def lookup(self, service_key: str) -> tuple[CatalogEntry | None, str]:
        """(entry, note). entry None = not known or not reachable; the note says which."""
        if not self.configured or self._client is None:
            return None, "catalog not configured"
        try:
            token = self._token_file.read_text().strip()
        except OSError:
            return None, "catalog token missing (make tools-tokens)"
        try:
            async with asyncio.timeout(DEADLINE_S):
                resp = await self._client.get(
                    f"{self._base}/v1/services",
                    params={"service_key": service_key},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=DEADLINE_S,  # each step too; the block above bounds the total
                )
        except TimeoutError:
            log.warning("catalog_too_slow", deadline_s=DEADLINE_S)
            return None, f"catalog too slow (no answer in {DEADLINE_S} s)"
        except httpx.HTTPError as exc:
            log.warning("catalog_unreachable", error=type(exc).__name__)
            return None, f"catalog unreachable ({type(exc).__name__})"
        if resp.status_code != 200:
            log.warning("catalog_refused", status=resp.status_code)
            return None, f"catalog answered {resp.status_code}"
        try:
            items = _List.model_validate_json(resp.content).items
        except ValidationError:
            return None, "catalog answer did not match the contract"
        for item in items:
            if item.key == service_key:
                return item, "catalog"
        return None, "service not in the catalog"
