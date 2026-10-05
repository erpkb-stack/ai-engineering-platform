"""Liveness vs readiness (architecture.md §22).

live  = the process works. NO dependency checks: a DB outage must not restart every pod.
ready = can serve traffic now: each registered check must pass within its timeout.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

ReadinessCheck = Callable[[], Awaitable[None]]


def health_router(
    checks: dict[str, ReadinessCheck] | None = None, timeout_s: float = 2.0
) -> APIRouter:
    router = APIRouter(tags=["health"])
    registered = checks or {}

    @router.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/health/ready")
    async def ready(request: Request) -> JSONResponse:
        results: dict[str, str] = {}
        for name, check in registered.items():
            try:
                await asyncio.wait_for(check(), timeout=timeout_s)
                results[name] = "ok"
            except Exception as exc:
                results[name] = f"fail: {type(exc).__name__}"
        ok = all(v == "ok" for v in results.values())
        return JSONResponse(
            {"status": "ok" if ok else "degraded", "checks": results},
            status_code=200 if ok else 503,
        )

    return router
