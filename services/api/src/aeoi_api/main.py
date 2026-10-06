"""api entry point: uvicorn aeoi_api.main:build_app --factory --port 8000"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from aeoi_api import __version__
from aeoi_api.config import Settings
from aeoi_api.proxy import Upstream
from aeoi_api.ratelimit import InMemoryTokenBucket, RateLimiter
from aeoi_api.routes import router
from aeoi_web import Authenticator, create_app


def build_app(
    settings: Settings | None = None,
    *,
    transports: dict[str, httpx.AsyncBaseTransport] | None = None,
    limiter: RateLimiter | None = None,
) -> FastAPI:
    settings = settings or Settings()

    def timeout(seconds: float) -> httpx.Timeout:
        return httpx.Timeout(seconds, connect=settings.upstream_connect_timeout_s)

    targets = {
        "incident-service": (settings.incident_service_url, timeout(settings.upstream_timeout_s)),
        "rag": (settings.rag_service_url, timeout(settings.rag_timeout_s)),
    }
    clients = {
        name: httpx.AsyncClient(
            base_url=url,
            timeout=t,
            transport=(transports or {}).get(name),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
        for name, (url, t) in targets.items()
    }

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            for client in clients.values():
                await client.aclose()

    async def incident_service_ready() -> None:
        r = await clients["incident-service"].get("/health/ready")
        r.raise_for_status()

    app = create_app(
        service_name=settings.service_name,
        version=__version__,
        routers=[router],
        authenticator=Authenticator(
            settings.jwt_public_key_file.read_text(), settings.jwt_issuer, settings.jwt_audience
        ),
        # rag is deliberately NOT a readiness dependency: search being down must not take
        # incident management out of the load balancer.
        readiness={"incident-service": incident_service_ready},
        lifespan=lifespan,
        log_level=settings.log_level,
        log_json=settings.log_json,
        environment=settings.environment.value,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "If-Match",
            "X-Correlation-ID",
        ],
        expose_headers=[
            "ETag",
            "Location",
            "Idempotent-Replayed",
            "X-Correlation-ID",
            "Retry-After",
        ],
    )
    app.state.settings = settings
    app.state.upstreams = {name: Upstream(name, c) for name, c in clients.items()}
    app.state.limiter = limiter or InMemoryTokenBucket(
        rate_per_s=settings.rate_limit_per_minute / 60, burst=settings.rate_limit_burst
    )

    @app.get("/api/v1/health", tags=["health"])
    async def api_health() -> dict[str, str]:
        """Public liveness for load balancers. Dependency detail lives on /health/ready."""
        return {"status": "ok", "service": "api", "version": __version__}

    return app
