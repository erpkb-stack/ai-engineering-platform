"""orchestrator entry point: uvicorn aeoi_orchestrator.main:build_app --factory --port 8002"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
from fastapi import FastAPI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aeoi_orchestrator import __version__
from aeoi_orchestrator.api import router
from aeoi_orchestrator.clients import AgentsClient, IncidentClient
from aeoi_orchestrator.config import Settings
from aeoi_orchestrator.delegation import DelegationClient
from aeoi_orchestrator.engine import Engine
from aeoi_orchestrator.graph import Deps, build_graph
from aeoi_orchestrator.store import Store
from aeoi_web import Authenticator, create_app

SCHEMA = "orchestrator"


def build_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    service_token: str | None = None,
) -> FastAPI:
    settings = settings or Settings()
    token = service_token or settings.service_token_file.read_text().strip()
    engine = create_async_engine(
        settings.sqlalchemy_url(), pool_size=settings.db_pool_size, pool_pre_ping=True
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    # LangGraph's saver: psycopg3, autocommit, no prepared statements (pgbouncer-safe), its
    # tables in OUR schema (migration 0018), unqualified names -> search_path
    pool: AsyncConnectionPool = AsyncConnectionPool(
        settings.libpq_url(),
        max_size=settings.checkpoint_pool_size,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
            "options": f"-c search_path={SCHEMA}",
        },
        open=False,
    )
    http = httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(settings.http_timeout_s, connect=2.0),
        trust_env=False,
    )
    store = Store(sessions)
    incidents = IncidentClient(http, settings.incident_service_url, token)
    delegation = DelegationClient(
        http,
        settings.api_url,
        token,
        refresh_margin=timedelta(seconds=settings.token_refresh_margin_s),
        timeout_s=settings.http_timeout_s,
    )
    deps = Deps(
        settings=settings,
        store=store,
        incidents=incidents,
        agents=AgentsClient(http, settings.agents_url, token, settings.task_deadline_s),
        delegation=delegation,
    )
    graph = build_graph(deps).compile(checkpointer=AsyncPostgresSaver(pool))  # type: ignore[arg-type]
    runner = Engine(
        graph,
        store,
        delegation,
        settings.max_concurrent_investigations,
        settings.investigation_deadline_s,
        incidents=incidents,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await pool.open()
        try:
            if settings.resume_on_startup:
                await runner.resume_pending()
            yield
        finally:
            await runner.shutdown()  # rows stay RUNNING: the next start resumes them
            await pool.close()
            await http.aclose()
            await engine.dispose()

    async def db_ready() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    app = create_app(
        service_name=settings.service_name,
        version=__version__,
        routers=[router],
        authenticator=Authenticator(
            settings.jwt_public_key_file.read_text(), settings.jwt_issuer, settings.jwt_audience
        ),
        readiness={"database": db_ready},
        lifespan=lifespan,
        log_level=settings.log_level,
        log_json=settings.log_json,
        environment=settings.environment.value,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.engine = runner
    app.state.incidents = incidents
    app.state.delegation = delegation
    app.state.db_engine = engine
    return app
