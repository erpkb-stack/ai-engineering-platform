"""llm-gateway usage accounting against real Postgres, connected as a least-privilege login."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aeoi_db.config import database_url, libpq_dsn
from aeoi_db.users import ensure_login
from aeoi_llm.db import make_engine, make_sessionmaker
from aeoi_llm.providers.base import Usage
from aeoi_llm.usage import DbUsageSink, UsageRow

pytestmark = pytest.mark.integration
LOGIN = "llm_svc_test"


@pytest.fixture(scope="module")
def llm_db_url(migrated_db: str) -> str:
    password = secrets.token_hex(16)
    with psycopg.connect(libpq_dsn(database=migrated_db), autocommit=True) as conn:
        ensure_login(conn, LOGIN, "svc_llm_gateway", password)
    url = database_url(database=migrated_db).set(username=LOGIN, password=password)
    return url.render_as_string(hide_password=False)


@pytest.fixture
async def engine(llm_db_url: str) -> AsyncIterator[AsyncEngine]:
    eng = make_engine(llm_db_url, 2)
    yield eng
    await eng.dispose()


def row(inv: uuid.UUID, cost: str, rid: str | None = None) -> UsageRow:
    return UsageRow(
        request_id=rid or f"{uuid.uuid4()}:1",
        provider="anthropic",
        model="claude-sonnet-5-5",
        operation="generate",
        status="OK",
        latency_ms=1200,
        usage=Usage(input_tokens=1000, output_tokens=200, cached_tokens=100),
        cost_usd=Decimal(cost),
        agent_name="hypothesis",
        prompt_id="hypothesis",
        prompt_version=1,
        investigation_id=inv,
    )


async def test_records_rows_and_sums_spend(engine: AsyncEngine) -> None:
    sink = DbUsageSink(make_sessionmaker(engine))
    inv = uuid.uuid4()
    assert await sink.record(row(inv, "0.004200"))
    assert await sink.record(row(inv, "0.001000"))
    assert await sink.spent(inv) == Decimal("0.005200")
    assert await sink.spent(uuid.uuid4()) == 0


async def test_duplicate_request_id_is_rejected_not_double_counted(engine: AsyncEngine) -> None:
    sink = DbUsageSink(make_sessionmaker(engine))
    inv = uuid.uuid4()
    rid = f"{uuid.uuid4()}:1"
    assert await sink.record(row(inv, "0.01", rid))
    assert await sink.record(row(inv, "0.01", rid)) is False  # unique(request_id)
    assert await sink.spent(inv) == Decimal("0.01")


async def test_db_check_constraint_rejects_unknown_status(engine: AsyncEngine) -> None:
    sink = DbUsageSink(make_sessionmaker(engine))
    bad = UsageRow(**{**row(uuid.uuid4(), "0").__dict__, "status": "MAYBE"})
    assert await sink.record(bad) is False


async def test_llm_login_cannot_read_other_schemas(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        with pytest.raises(Exception, match="permission denied"):
            await conn.execute(text("SELECT 1 FROM incident.incidents LIMIT 1"))


async def test_llm_login_cannot_rewrite_history(engine: AsyncEngine) -> None:
    """Cost rows are evidence for budgets and KPIs: the service may INSERT, never UPDATE/DELETE
    (migration 0014). Otherwise a bug could erase spend and bypass the budget."""
    sink = DbUsageSink(make_sessionmaker(engine))
    inv = uuid.uuid4()
    await sink.record(row(inv, "1.00"))
    for stmt in ("DELETE FROM llm.model_usage", "UPDATE llm.model_usage SET cost_usd = 0"):
        async with engine.connect() as conn:
            with pytest.raises(Exception, match="permission denied"):
                await conn.execute(text(stmt))
    assert await sink.spent(inv) == Decimal("1.00")
