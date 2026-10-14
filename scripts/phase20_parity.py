"""Phase 20 (ADR-032): the Java catalog must answer the tool EXACTLY like the JSON file.

Runs the REAL adapters on both sides - CatalogAdapter(data/generated/catalog.json) and
CatalogHttpAdapter(the running service-catalog) - over every service key, every team, and a set
of name searches (incl. team + name). Prints one line per kind of query; exit 1 on any mismatch.

    uv run python scripts/phase20_parity.py [http://localhost:8010]
Needs: make db-seed, make catalog-import, make run-catalog, make tools-tokens.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx

from aeoi_tools import schemas as s
from aeoi_tools.adapters.catalog import CatalogAdapter
from aeoi_tools.adapters.catalog_http import CatalogHttpAdapter
from aeoi_tools.registry import CatalogSource
from aeoi_tools.relay import FileToken

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data" / "generated" / "catalog.json"
TOKEN = ROOT / "secrets" / "tools_catalog_token.txt"
NEEDLES = ("api", "db", "-w", "pay", "check", "zz-none", "Web")


def queries_for(services: list[dict[str, Any]]) -> dict[str, list[s.CatalogIn]]:
    keys = sorted(x["key"] for x in services)
    teams = sorted({x["team"] for x in services})
    return {
        "service_key": [s.CatalogIn(service_key=k) for k in keys] + [s.CatalogIn(service_key="no-such-svc")],
        "team": [s.CatalogIn(team=t) for t in teams],
        "name_contains": [s.CatalogIn(name_contains=n) for n in NEEDLES],
        "team+name": [s.CatalogIn(team=t, name_contains=n) for t in teams[:5] for n in NEEDLES[:3]],
    }  # fmt: skip


async def compare(
    file: CatalogSource, http: CatalogSource, queries: dict[str, list[s.CatalogIn]]
) -> tuple[list[str], int]:
    """Every query against both sources. Returns the report lines and the mismatch count."""
    lines: list[str] = []
    failed = 0
    for kind, qs in queries.items():
        bad, items = [], 0
        for q in qs:
            a, b = await file.query(q), await http.query(q)
            items += len(a.items)
            if a != b:
                bad.append((q, a, b))
        lines.append(f"{kind:<14} {len(qs):>4} queries, {items:>5} items: "
                     f"{'MISMATCH' if bad else 'same'}")  # fmt: skip
        for q, a, b in bad[:3]:
            lines.append(f"  {q.model_dump(exclude_none=True)}\n    json: {a.model_dump()}\n    java: {b.model_dump()}")  # fmt: skip
        failed += len(bad)
    return lines, failed


async def main(base: str) -> int:
    services = json.loads(CATALOG.read_text())["services"]
    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
        lines, failed = await compare(
            CatalogAdapter(CATALOG), CatalogHttpAdapter(client, base, FileToken(TOKEN)),
            queries_for(services),
        )  # fmt: skip
    print("\n".join(lines))
    teams = len({x["team"] for x in services})
    print(f"parity: {failed} mismatch(es) over {len(services)} services, {teams} teams")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(
        asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8010"))
    )
