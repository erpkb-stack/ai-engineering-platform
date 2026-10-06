"""Service catalog adapter. [P] reads data/generated/catalog.json (written by `make db-seed`).
[Phase 20] the Java service-catalog API replaces this file; the tool contract stays the same."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aeoi_tools import schemas as s
from aeoi_tools.contracts import ToolUnavailableError


class CatalogAdapter:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._services: dict[str, dict[str, Any]] | None = None
        self._dependents: dict[str, list[str]] = {}
        self._mtime: float | None = None

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError as exc:
            # 503 with the fix in the message (a retry is cheap and harmless here)
            raise ToolUnavailableError(
                f"catalog not found at {self._path.name}; run `make db-seed`"
            ) from exc
        if self._services is None or mtime != self._mtime:  # reload after a re-seed
            data = json.loads(self._path.read_text())
            services = {svc["key"]: svc for svc in data["services"]}
            dependents: dict[str, list[str]] = {k: [] for k in services}
            for svc in services.values():
                for dep in svc.get("depends_on", []):
                    dependents.setdefault(dep, []).append(svc["key"])
            self._services, self._dependents, self._mtime = services, dependents, mtime
        return self._services

    def known(self, service_key: str) -> bool:
        return service_key in self._load()

    async def query(self, q: s.CatalogIn) -> s.CatalogOut:
        services = self._load()
        if q.service_key:
            found = [services[q.service_key]] if q.service_key in services else []
        else:
            needle = (q.name_contains or "").lower()
            found = [
                svc
                for svc in services.values()
                if (q.team is None or svc.get("team") == q.team)
                and (not needle or needle in svc["name"].lower() or needle in svc["key"])
            ]
        found.sort(key=lambda svc: (svc.get("tier", 9), svc["key"]))
        items = [
            s.ServiceItem(
                key=svc["key"],
                name=svc["name"],
                type=svc.get("type", "unknown"),
                tier=int(svc.get("tier", 3)),
                team=svc.get("team", "unknown"),
                language=svc.get("language"),
                repository=svc.get("repository"),
                depends_on=list(svc.get("depends_on", []))[:100],
                dependents=sorted(self._dependents.get(svc["key"], []))[:200],
            )
            for svc in found[:200]
        ]
        return s.CatalogOut(items=items, truncated=len(found) > 200)
