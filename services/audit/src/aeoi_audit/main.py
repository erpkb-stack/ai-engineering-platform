"""audit entry point: uvicorn aeoi_audit.main:build_app --factory --port 8008"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aeoi_audit import __version__
from aeoi_audit.api import router
from aeoi_audit.config import Settings
from aeoi_audit.consumer import classify, handler
from aeoi_messaging import Consumer, KafkaTransport, Transport
from aeoi_web import Authenticator, create_app, oidc_verifier


class ConsumerStoppedError(RuntimeError):
    """The consumer task is not running (never started, or died)."""


class ConsumerDegradedError(RuntimeError):
    """The consumer is alive but not consuming: joining, or waiting on Kafka or the DB."""


def build_app(settings: Settings | None = None, transport: Transport | None = None) -> FastAPI:
    """`transport` (tests): the broker the consumer reads; default Kafka when enabled."""
    settings = settings or Settings()
    engine = create_async_engine(
        settings.sqlalchemy_url(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_pool_size,
        pool_pre_ping=True,
        pool_timeout=5,
    )

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    consumer: Consumer | None = None
    if settings.consumer_enabled or transport is not None:
        topics = [t.strip() for t in settings.consumer_topics.split(",") if t.strip()]
        consumer = Consumer(
            settings.consumer_group,
            transport
            or KafkaTransport(settings.kafka_bootstrap_servers, settings.consumer_group, topics),
            handler(sessions),
            classify=classify,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = (
            asyncio.create_task(consumer.run_forever(), name="audit-consumer") if consumer else None
        )
        app.state.consumer_task = task
        try:
            yield
        finally:
            if consumer and task:
                consumer.stop()  # finish the message in hand; anything uncommitted comes again
                with suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(task, timeout=10)
            await engine.dispose()

    async def db_ready() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def consumer_ready() -> None:
        # Not ready while the consumer is dead, still joining, or waiting on a dependency
        # (review M4). The read API may still work then: the cost is accepted, because until lag
        # is a metric (observability phase) readiness is the ONLY signal that audit is behind.
        # Liveness is untouched: a Kafka outage must not restart the pod.
        if consumer is None:
            return
        task = getattr(app.state, "consumer_task", None)
        if task is None or task.done():
            raise ConsumerStoppedError(consumer.status.last_error or "")
        if not consumer.status.healthy:
            raise ConsumerDegradedError(
                f"{consumer.status.state}: {consumer.status.last_error or ''}"
            )
        try:  # "running" cannot see a broker outage: aiokafka polls return nothing (re-review M-1)
            await consumer.transport.probe()
        except Exception as exc:
            raise ConsumerDegradedError(f"broker unreachable: {type(exc).__name__}") from exc

    app = create_app(
        service_name=settings.service_name,
        version=__version__,
        routers=[router],
        authenticator=Authenticator(
            settings.jwt_public_key_file.read_text(),
            settings.jwt_issuer,
            settings.jwt_audience,
            oidc=oidc_verifier(settings),
            static_users=settings.static_user_tokens,
        ),
        readiness={"database": db_ready, **({"consumer": consumer_ready} if consumer else {})},
        lifespan=lifespan,
        log_level=settings.log_level,
        log_json=settings.log_json,
        environment=settings.environment.value,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = sessions
    app.state.consumer = consumer
    return app
