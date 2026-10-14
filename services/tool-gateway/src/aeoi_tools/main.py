"""tool-gateway entry point: uvicorn aeoi_tools.main:build_app --factory --port 8006"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import httpx
import structlog
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aeoi_common.ratelimit import InMemoryTokenBucket, RateLimiter
from aeoi_tools import __version__
from aeoi_tools.adapters.approvals import ApprovalsClient
from aeoi_tools.adapters.catalog import CatalogAdapter
from aeoi_tools.adapters.catalog_http import CatalogHttpAdapter
from aeoi_tools.adapters.deployer import Deployer
from aeoi_tools.adapters.devdata import DevDataAdapter
from aeoi_tools.adapters.http import make_client, origin_of
from aeoi_tools.adapters.rag import RagAdapter
from aeoi_tools.api import router
from aeoi_tools.config import Settings
from aeoi_tools.policy import PolicyConfig
from aeoi_tools.registry import build_registry
from aeoi_tools.relay import AuditRelay, FileToken
from aeoi_tools.service import ToolService
from aeoi_web import Authenticator, create_app, oidc_verifier

log = structlog.get_logger(__name__)


def build_app(
    settings: Settings | None = None,
    *,
    rag_transport: httpx.AsyncBaseTransport | None = None,
    audit_transport: httpx.AsyncBaseTransport | None = None,
    incident_transport: httpx.AsyncBaseTransport | None = None,
    limiter: RateLimiter | None = None,
) -> FastAPI:
    settings = settings or Settings()
    engine = create_async_engine(
        settings.sqlalchemy_url(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_pool_size,
        pool_pre_ping=True,
        pool_timeout=2,
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    allowed = frozenset(settings.egress_allowlist)
    if origin_of(settings.rag_url) not in allowed:
        log.warning("rag_url_not_in_egress_allowlist", rag=origin_of(settings.rag_url))
    tool_http = make_client(allowed, timeout_s=settings.http_timeout_s, transport=rag_transport)
    cd_url = settings.cd_sqlalchemy_url()
    cd_engine = (create_async_engine(cd_url, pool_size=1, max_overflow=1, pool_pre_ping=True,
                                     pool_timeout=2) if cd_url else None)  # fmt: skip
    if cd_engine is None:
        log.warning("deployer_not_configured", hint="make db-users (cd_svc login)")
    if settings.catalog_url and origin_of(settings.catalog_url) not in allowed:
        log.warning("catalog_url_not_in_egress_allowlist", catalog=origin_of(settings.catalog_url))
    catalog = (
        CatalogHttpAdapter(tool_http, settings.catalog_url, FileToken(settings.catalog_token_file))
        if settings.catalog_url
        else CatalogAdapter(settings.catalog_file)
    )
    registry = build_registry(
        catalog,
        DevDataAdapter(sessions),
        RagAdapter(tool_http, settings.rag_url, FileToken(settings.rag_token_file)),
        Deployer(
            sessions, async_sessionmaker(cd_engine, expire_on_commit=False) if cd_engine else None
        ),
    )
    # control plane, not tool egress: the approval record lives in incident-service
    incident_http = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=1.0),
                                      transport=incident_transport, trust_env=False)  # fmt: skip
    approvals = ApprovalsClient(incident_http, settings.incident_service_url,
                                FileToken(settings.approvals_token_file))  # fmt: skip
    policy = PolicyConfig.load(settings.agents_file, set(registry))
    service = ToolService(
        registry,
        policy,
        limiter
        or InMemoryTokenBucket(
            rate_per_s=settings.rate_limit_per_minute / 60, burst=settings.rate_limit_burst
        ),
        sessions,
        bulkhead_limits=settings.bulkhead,
        breaker_threshold=settings.breaker_threshold,
        breaker_cooldown_s=settings.breaker_cooldown_s,
        approvals=approvals,
    )
    audit_http = httpx.AsyncClient(
        base_url=settings.audit_url,
        timeout=httpx.Timeout(5.0, connect=1.0),
        transport=audit_transport,
        trust_env=False,
    )
    relay = AuditRelay(
        sessions,
        audit_http,
        FileToken(settings.audit_token_file),
        batch_size=settings.relay_batch_size,
        interval_s=settings.relay_interval_s,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = None
        if settings.relay_enabled:
            if not settings.audit_token_file.exists():
                log.warning("audit_relay_no_token", hint="run: make tools-tokens")
            task = asyncio.create_task(relay.run_forever(), name="audit-relay")
        try:
            yield
        finally:
            relay.stop()
            if task is not None:
                with suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(task, timeout=5)
            await tool_http.aclose()
            await audit_http.aclose()
            await incident_http.aclose()
            if cd_engine is not None:
                await cd_engine.dispose()
            await engine.dispose()

    async def db_ready() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    app = create_app(
        service_name=settings.service_name,
        version=__version__,
        routers=[router],
        authenticator=Authenticator(
            settings.jwt_public_key_file.read_text(),
            settings.jwt_issuer,
            settings.jwt_audience,
            delegation_public_key=(
                settings.delegation_public_key_file.read_text()
                if settings.delegation_public_key_file.is_file()
                else None
            ),
            oidc=oidc_verifier(settings),
            static_users=settings.static_user_tokens,
        ),
        # Ready = we can RECORD calls. rag/audit being down degrades some tools / delays
        # audit delivery; it must not take every tool out of the load balancer.
        readiness={"database": db_ready},
        lifespan=lifespan,
        log_level=settings.log_level,
        log_json=settings.log_json,
        environment=settings.environment.value,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = sessions
    app.state.service = service
    app.state.relay = relay
    return app
