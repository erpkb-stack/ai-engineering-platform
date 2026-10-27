"""Liveness vs readiness (architecture.md §22, ADR-041).

live  = the process works. NO dependency checks: a DB outage must not restart every pod.
ready = this pod may receive traffic. A DEPENDENCY check (the DB, a downstream service) gates the
        START only: once it has passed, a later failure is REPORTED (body "degraded", the failing
        check named) but the pod stays in its Service and answers 503 + Retry-After itself.
        Phase 28a measured why: every replica shares the DB, so failing readiness on it removed
        the api from its Service - callers got 61 dropped connections and only 4 answers.
        A CONTINUOUS check (`continuous=`) is process-local state that this pod alone has
        wrong (a dead consumer task) and keeps failing readiness for as long as it fails.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

ReadinessCheck = Callable[[], Awaitable[None]]


def health_router(
    checks: dict[str, ReadinessCheck] | None = None,
    timeout_s: float = 2.0,
    continuous: Iterable[str] = (),
) -> APIRouter:
    router = APIRouter(tags=["health"])
    registered = checks or {}
    always = frozenset(continuous)
    unknown = always - registered.keys()
    if unknown:
        raise ValueError(f"continuous readiness checks not registered: {sorted(unknown)}")
    passed: set[str] = set()  # dependency checks that have passed once since start

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
                passed.add(name)
            except Exception as exc:
                results[name] = f"fail: {type(exc).__name__}"
        healthy = all(v == "ok" for v in results.values())
        serving = all(results[n] == "ok" if n in always else n in passed for n in registered)
        return JSONResponse(
            {"status": "ok" if healthy else "degraded", "checks": results},
            status_code=200 if serving else 503,
        )

    return router
