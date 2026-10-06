"""llm-gateway entry point: uvicorn aeoi_llm.main:build_app --factory --port 8005"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI
from sqlalchemy import text

from aeoi_llm import __version__
from aeoi_llm.api import router
from aeoi_llm.cache import ResponseCache
from aeoi_llm.config import ProviderConfig, ProviderKind, RoutingConfig, Settings, load_routing
from aeoi_llm.db import make_engine, make_sessionmaker
from aeoi_llm.gateway import Gateway, ProviderRuntime
from aeoi_llm.providers import AnthropicProvider, FakeProvider, OpenAICompatProvider, Provider
from aeoi_llm.resilience import RetryPolicy
from aeoi_llm.usage import DbUsageSink, MemoryUsageSink, UsageSink
from aeoi_web import Authenticator, create_app

log = structlog.get_logger(__name__)


def make_provider(
    name: str, cfg: ProviderConfig, transport: httpx.AsyncBaseTransport | None = None
) -> tuple[Provider | None, str | None]:
    """Returns (provider, None) or (None, reason). A missing key disables, never crashes:
    the route falls back and every response says so (fallback_used=true)."""
    if cfg.kind is ProviderKind.FAKE:
        return FakeProvider(name=name, hosted=cfg.hosted), None
    if cfg.kind is ProviderKind.ANTHROPIC:
        key = cfg.api_key()
        if not key:
            return None, f"no API key (set {cfg.api_key_env} or secrets/{cfg.api_key_file})"
        return AnthropicProvider(
            name, base_url=cfg.base_url, api_key=key, timeout_s=cfg.timeout_s, transport=transport
        ), None
    key = cfg.api_key()
    if cfg.hosted and not key:
        return None, f"no API key (set {cfg.api_key_env} or secrets/{cfg.api_key_file})"
    return OpenAICompatProvider(
        name,
        base_url=cfg.base_url,
        api_key=key,
        timeout_s=cfg.timeout_s,
        hosted=cfg.hosted,
        transport=transport,
    ), None


def build_gateway(
    routing: RoutingConfig,
    sink: UsageSink,
    *,
    cache: ResponseCache | None = None,
    request_timeout_s: float = 120.0,
    providers: dict[str, Provider] | None = None,
) -> Gateway:
    runtimes: dict[str, ProviderRuntime] = {}
    for name, cfg in routing.providers.items():
        provider: Provider | None
        reason: str | None
        if providers is not None and name in providers:
            provider, reason = providers[name], None
        else:
            provider, reason = make_provider(name, cfg)
        if reason:
            log.warning("llm_provider_disabled", provider=name, reason=reason)
        runtimes[name] = ProviderRuntime(
            name=name,
            config=cfg,
            provider=provider,
            retry=RetryPolicy(max_attempts=cfg.max_attempts),
            unavailable_reason=reason,
        )
    return Gateway(routing, runtimes, sink, cache, request_timeout_s=request_timeout_s)


def build_app(
    settings: Settings | None = None, providers: dict[str, Provider] | None = None
) -> FastAPI:
    settings = settings or Settings()
    routing = load_routing(settings.routing_file)  # invalid policy => service does not start
    engine = None
    sink: UsageSink
    if settings.record_usage:
        engine = make_engine(settings.sqlalchemy_url(), settings.db_pool_size)
        sink = DbUsageSink(make_sessionmaker(engine))
    else:
        sink = MemoryUsageSink()
    cache = (
        ResponseCache(settings.cache_max_entries, settings.cache_ttl_s)
        if settings.cache_enabled
        else None
    )
    gateway = build_gateway(
        routing,
        sink,
        cache=cache,
        request_timeout_s=settings.request_timeout_s,
        providers=providers,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await gateway.aclose()
            if engine is not None:
                await engine.dispose()

    readiness: dict[str, Callable[[], Awaitable[None]]] = {}
    if engine is not None:
        db_engine = engine

        async def db_ready() -> None:
            async with db_engine.connect() as conn:
                await conn.execute(text("SELECT 1"))

        readiness["database"] = db_ready

    app = create_app(
        service_name=settings.service_name,
        version=__version__,
        routers=[router],
        authenticator=Authenticator(
            settings.jwt_public_key_file.read_text(), settings.jwt_issuer, settings.jwt_audience
        ),
        readiness=readiness,
        lifespan=lifespan,
        log_level=settings.log_level,
        log_json=settings.log_json,
        environment=settings.environment.value,
    )
    app.state.settings = settings
    app.state.gateway = gateway
    app.state.engine = engine
    return app
