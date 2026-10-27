"""incident-service entry point: uvicorn aeoi_incident.main:build_app --factory --port 8001"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from sqlalchemy import text

from aeoi_incident import __version__
from aeoi_incident.api import router
from aeoi_incident.catalog import CatalogClient
from aeoi_incident.config import EventTransport, Settings
from aeoi_incident.db import make_engine, make_sessionmaker
from aeoi_incident.events import KafkaPublisher, LogPublisher, OutboxRelay, Publisher
from aeoi_web import Authenticator, create_app, oidc_verifier


def build_app(settings: Settings | None = None, publisher: Publisher | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = make_engine(settings.sqlalchemy_url(), settings.db_pool_size)
    sessions = make_sessionmaker(engine)
    pub: Publisher = publisher or (
        KafkaPublisher(settings.kafka_bootstrap_servers)
        if settings.event_transport is EventTransport.KAFKA
        else LogPublisher()
    )
    http = httpx.AsyncClient() if settings.catalog_url else None
    catalog = CatalogClient(http, settings.catalog_url, settings.catalog_token_file)
    relay = OutboxRelay(
        sessions, pub, batch_size=settings.relay_batch_size, interval_s=settings.relay_interval_s
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task: asyncio.Task[None] | None = None
        if settings.relay_enabled:
            await pub.start()
            task = asyncio.create_task(relay.run_forever(), name="outbox-relay")
        try:
            yield
        finally:
            relay.stop()
            if task is not None:
                await task
                await pub.stop()
            await engine.dispose()
            if http is not None:
                await http.aclose()

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
        readiness={"database": db_ready},
        lifespan=lifespan,
        log_level=settings.log_level,
        log_json=settings.log_json,
        environment=settings.environment.value,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = sessions
    app.state.relay = relay
    app.state.catalog = catalog
    return app
