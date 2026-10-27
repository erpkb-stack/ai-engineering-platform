"""App factory: every AEOI FastAPI service starts the same way."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager

from fastapi import APIRouter, FastAPI

from aeoi_observability import configure_logging
from aeoi_observability.telemetry import EXCLUDED_URLS, server_no_query, setup_telemetry
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
    readiness_continuous: Sequence[str] = (),
    lifespan: Lifespan | None = None,
    log_level: str = "INFO",
    log_json: bool = True,
    environment: str = "development",
    untrusted_trace_paths: Sequence[str] = (),
) -> FastAPI:
    configure_logging(service_name, level=log_level, json_output=log_json, environment=environment)
    app = FastAPI(
        title=f"AEOI {service_name}",
        version=version,
        lifespan=lifespan,
        docs_url="/docs" if environment != "production" else None,
        redoc_url=None,
    )
    # Phase 23a (ADR-035): OFF unless AEOI_OTEL_ENDPOINT is set; health routes never traced
    tel = setup_telemetry(
        service_name, environment=environment, untrusted_trace_paths=untrusted_trace_paths
    )
    if tel is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(
            app,
            excluded_urls=EXCLUDED_URLS,
            server_request_hook=server_no_query,
            exclude_spans=["receive", "send"],  # 4 ASGI sub-spans per request: noise
            tracer_provider=tel.tracer_provider,
            meter_provider=tel.meter_provider,
        )
    app.state.auth = authenticator
    app.state.service_name = service_name
    install_problem_handlers(app)
    app.add_middleware(CorrelationIdMiddleware)
    app.include_router(health_router(readiness, continuous=readiness_continuous))
    for router in routers:
        app.include_router(router)
    return app
