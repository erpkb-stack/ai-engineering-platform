"""Phase 28a (ADR-040): a lost database must not leave an investigation stuck RUNNING.

Found by the Postgres-stop experiment (sandbox dry run, 2026-10-26): after a DB restart the
checkpointer's pool handed out a dead connection, `aget_state` raised AdminShutdown OUTSIDE the
graph's error handling, the task died ("exception was never retrieved") and the row stayed
RUNNING until the next orchestrator start.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from aeoi_orchestrator import engine as engine_mod
from aeoi_orchestrator.engine import Engine


class Store:
    def __init__(self) -> None:
        self.inv = SimpleNamespace(status="RUNNING", delegation_grant_id=None, incident_id=uuid4(),
                                   trace_context=None, correlation_id=None)  # fmt: skip
        self.finished: list[tuple[str, str]] = []

    async def get(self, _id: Any) -> Any:
        return self.inv

    async def begin_run(self, _id: Any, _deadline_s: float) -> datetime:
        return datetime.now(UTC) + timedelta(minutes=5)

    async def finish(self, _id: Any, status: str, error: str, **_kw: Any) -> None:
        self.finished.append((status, error))


class Graph:
    """aget_state fails `down` times with the error the dry run saw, then the run completes."""

    def __init__(self, down: int, error: Exception) -> None:
        self.down, self.error, self.invoked = down, error, 0

    async def aget_state(self, _cfg: Any) -> Any:
        if self.down:
            self.down -= 1
            raise self.error
        return SimpleNamespace(values={"x": 1}, next=("agents",))

    async def ainvoke(self, _arg: Any, _cfg: Any, durability: str) -> None:
        self.invoked += 1


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_mod, "DB_RETRY_FIRST_S", 0.0)


def _engine(graph: Graph, store: Store) -> Engine:
    return Engine(graph, store, delegation=None, max_concurrent=1, deadline_s=60)  # type: ignore[arg-type]


async def test_a_lost_database_waits_and_resumes_instead_of_dying() -> None:
    store, graph = Store(), Graph(3, psycopg.errors.AdminShutdown("terminating connection"))
    await _engine(graph, store)._run(uuid4(), {})
    assert graph.invoked == 1  # resumed from the checkpoint once the DB answered
    assert store.finished == []  # a DB blip is not a FAILED investigation


async def test_any_other_crash_outside_the_graph_ends_named_not_running() -> None:
    store, graph = Store(), Graph(1, RuntimeError("bug"))
    await _engine(graph, store)._run(uuid4(), {})
    assert graph.invoked == 0
    assert store.finished == [("FAILED", "orchestrator error: RuntimeError")]
