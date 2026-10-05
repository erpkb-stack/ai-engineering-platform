"""App factory: every AEOI FastAPI service starts the same way."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager

from fastapi import APIRouter, FastAPI

from aeoi_observability import configure_logging
from aeoi_web.auth import Authenticator
from aeoi_web.health import ReadinessCheck, health_router
from aeoi_web.middleware import CorrelationIdMiddleware
from aeoi_web.problems import install_problem_handlers

Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def create_app(
    *,
    service_name: str,
    version: str,
    routers: Sequence[APIRouter],
    authenticator: Authenticator | None,
    readiness: dict[str, ReadinessCheck] | None = None,
    lifespan: Lifespan | None = None,
    log_level: str = "INFO",
    log_json: bool = True,
    environment: str = "development",
) -> FastAPI:
    configure_logging(service_name, level=log_level, json_output=log_json, environment=environment)
    app = FastAPI(
        title=f"AEOI {service_name}",
        version=version,
        lifespan=lifespan,
        docs_url="/docs" if environment != "production" else None,
        redoc_url=None,
    )
    app.state.auth = authenticator
    app.state.service_name = service_name
    install_problem_handlers(app)
    app.add_middleware(CorrelationIdMiddleware)
    app.include_router(health_router(readiness))
    for router in routers:
        app.include_router(router)
    return app
