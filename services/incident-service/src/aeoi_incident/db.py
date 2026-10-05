from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def make_engine(url: str, pool_size: int) -> AsyncEngine:
    return create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_pre_ping=True,  # survive DB restarts without serving errors
        pool_timeout=5,  # fail fast instead of piling up requests (backpressure)
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def session_dep(request: Request) -> AsyncIterator[AsyncSession]:
    maker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with maker() as session:
        yield session
