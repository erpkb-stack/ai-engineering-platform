"""Cost calculation + usage recording (llm.model_usage) + per-investigation budget."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aeoi_db.models.llm import ModelUsage
from aeoi_llm.config import ModelConfig
from aeoi_llm.providers.base import Usage

log = structlog.get_logger(__name__)
MTOK = Decimal(1_000_000)
MICRO = Decimal("0.000001")


def cost_usd(model: ModelConfig, usage: Usage) -> Decimal:
    """input_tokens includes cached tokens (normalised by adapters); cached ones are cheaper."""
    cached_price = (
        model.cached_input_per_mtok
        if model.cached_input_per_mtok is not None
        else model.input_per_mtok
    )
    uncached = max(0, usage.input_tokens - usage.cached_tokens)
    total = (
        Decimal(uncached) * model.input_per_mtok
        + Decimal(usage.cached_tokens) * cached_price
        + Decimal(usage.output_tokens) * model.output_per_mtok
    ) / MTOK
    return total.quantize(MICRO, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class UsageRow:
    request_id: str
    provider: str
    model: str
    operation: str
    status: str  # OK / ERROR / TIMEOUT / RATE_LIMITED / FALLBACK
    latency_ms: int
    usage: Usage
    cost_usd: Decimal
    prompt_id: str | None = None
    prompt_version: int | None = None
    agent_name: str | None = None
    investigation_id: uuid.UUID | None = None
    error_type: str | None = None


class UsageSink(Protocol):
    async def record(self, row: UsageRow) -> bool: ...
    async def spent(self, investigation_id: uuid.UUID) -> Decimal: ...


class DbUsageSink:
    """Writes one row per ATTEMPT (failed attempts cost latency and sometimes money too)."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def record(self, row: UsageRow) -> bool:
        try:
            async with self._sessions() as s, s.begin():
                s.add(
                    ModelUsage(
                        request_id=row.request_id,
                        provider=row.provider,
                        model=row.model,
                        operation=row.operation,
                        status=row.status,
                        latency_ms=row.latency_ms,
                        input_tokens=row.usage.input_tokens,
                        output_tokens=row.usage.output_tokens,
                        cached_tokens=row.usage.cached_tokens,
                        cost_usd=row.cost_usd,
                        prompt_id=row.prompt_id,
                        prompt_version=row.prompt_version,
                        agent_name=row.agent_name,
                        investigation_id=row.investigation_id,
                        error_type=row.error_type,
                    )
                )
        except Exception as exc:  # accounting must not take the answer away (ADR-014)
            log.error("usage_record_failed", request_id=row.request_id, error=type(exc).__name__)
            return False
        return True

    async def spent(self, investigation_id: uuid.UUID) -> Decimal:
        async with self._sessions() as s:
            total = await s.scalar(
                select(func.coalesce(func.sum(ModelUsage.cost_usd), 0)).where(
                    ModelUsage.investigation_id == investigation_id
                )
            )
        return Decimal(total or 0)


class MemoryUsageSink:
    """Tests and `record_usage=false` runs."""

    def __init__(self) -> None:
        self.rows: list[UsageRow] = []
        self.fail = False

    async def record(self, row: UsageRow) -> bool:
        if self.fail:
            return False
        self.rows.append(row)
        return True

    async def spent(self, investigation_id: uuid.UUID) -> Decimal:
        return sum(
            (r.cost_usd for r in self.rows if r.investigation_id == investigation_id), Decimal(0)
        )
