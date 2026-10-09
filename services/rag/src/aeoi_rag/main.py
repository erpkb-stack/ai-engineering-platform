"""rag entry point: uvicorn aeoi_rag.main:build_app --factory --port 8004"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from sqlalchemy import text

from aeoi_llm_client import HttpLLMClient, LLMClient
from aeoi_rag import __version__
from aeoi_rag.api import router
from aeoi_rag.config import Settings
from aeoi_rag.db import make_engine, make_sessionmaker
from aeoi_rag.embedder import Embedder, FileTokenProvider, GatewayEmbedder
from aeoi_rag.rerank import LLMReranker
from aeoi_rag.search import Retriever, detect_iterative_scan
from aeoi_web import Authenticator, create_app


def make_llm_client(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> HttpLLMClient:
    return HttpLLMClient(
        settings.llm_gateway_url,
        FileTokenProvider(settings.llm_token_file),
        timeout_s=130.0,
        transport=transport,
    )


def make_embedder(settings: Settings, client: LLMClient) -> GatewayEmbedder:
    return GatewayEmbedder(
        client,
        route=settings.embed_route,
        document_prefix=settings.document_prefix,
        query_prefix=settings.query_prefix,
        batch_size=settings.embed_batch_size,
    )


def make_reranker(settings: Settings, client: LLMClient) -> LLMReranker:
    return LLMReranker(
        client,
        route=settings.rerank_route,
        restricted_route=settings.rerank_route_restricted,
        candidates=settings.rerank_candidates,
        snippet_chars=settings.rerank_snippet_chars,
        timeout_s=settings.rerank_timeout_s,
    )


def build_app(
    settings: Settings | None = None,
    *,
    llm_client: LLMClient | None = None,
    embedder: Embedder | None = None,
) -> FastAPI:
    settings = settings or Settings()
    engine = make_engine(settings.sqlalchemy_url(), settings.db_pool_size)
    sessions = make_sessionmaker(engine)
    owned_client = llm_client is None
    client: LLMClient = llm_client or make_llm_client(settings)
    emb = embedder or make_embedder(settings, client)
    retriever = Retriever(sessions, emb, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        retriever._iterative_scan = await detect_iterative_scan(sessions)
        try:
            yield
        finally:
            if owned_client and isinstance(client, HttpLLMClient):
                await client.aclose()
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
        ),
        # The LLM gateway is NOT a readiness dependency: keyword search still works without it.
        readiness={"database": db_ready},
        lifespan=lifespan,
        log_level=settings.log_level,
        log_json=settings.log_json,
        environment=settings.environment.value,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = sessions
    app.state.retriever = retriever
    app.state.reranker = make_reranker(settings, client)
    return app
