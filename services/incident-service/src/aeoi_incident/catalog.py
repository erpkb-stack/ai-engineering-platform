"""Phase 29b (ADR-044): the service catalog, read by the alert triage (tier + dependents).

- Bearer = incident-service's OWN service token (scope `catalog:read`), minted by
  `make tools-tokens` into secrets/incident_catalog_token.txt.
- Only the 3 fields triage uses are read; other fields are ignored here (the tool-gateway
  holds the full, extra-forbid contract and the drift test against openapi.yaml).
- NEVER raises: an alert must not be lost because the catalog is down. Every failure is an
  answer (`None` + a note) and triage falls back to the label severity, saying so.
- One short attempt (2 s): the webhook answers Alertmanager, which retries on its own.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = structlog.get_logger("aeoi.incident.catalog")
TIMEOUT_S = 2.0


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
            resp = await self._client.get(
                f"{self._base}/v1/services",
                params={"service_key": service_key},
                headers={"Authorization": f"Bearer {token}"},
                timeout=TIMEOUT_S,
            )
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
