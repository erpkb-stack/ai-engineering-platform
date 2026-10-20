"""Phase 23a (ADR-035): a run - also a RESUME with no request - continues the stored trace.

Before 23a a resumed investigation ran with no correlation id: its logs and every call it made
could not be tied to the investigation (found in the Phase 23 need check).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from aeoi_common.correlation import get_correlation_id
from aeoi_observability.telemetry import current_traceparent
from aeoi_orchestrator.engine import Engine

STORED = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


class FakeStore:
    def __init__(self, inv: Any) -> None:
        self.inv = inv

    async def get(self, _id: Any) -> Any:
        return self.inv

    async def begin_run(self, _id: Any, _deadline_s: float) -> datetime:
        return datetime.now(UTC) + timedelta(minutes=5)


class FakeGraph:
    def __init__(self) -> None:
        self.seen: dict[str, Any] = {}

    async def aget_state(self, _cfg: Any) -> Any:
        return SimpleNamespace(values={"x": 1}, next=("agents",))  # a checkpoint: a RESUME

    async def ainvoke(self, arg: Any, _cfg: Any, durability: str) -> None:
        self.seen = {"arg": arg, "cid": get_correlation_id(), "tp": current_traceparent()}


def _engine(inv: Any) -> tuple[Engine, FakeGraph]:
    graph = FakeGraph()
    engine = Engine(graph, FakeStore(inv), delegation=None, max_concurrent=1, deadline_s=60)  # type: ignore[arg-type]
    return engine, graph


def _inv(**kw: Any) -> Any:
    base = {"status": "RUNNING", "delegation_grant_id": uuid4(), "incident_id": uuid4(),
            "trace_context": STORED, "correlation_id": "corr-from-start-01"}  # fmt: skip
    return SimpleNamespace(**{**base, **kw})


async def test_a_resumed_run_has_the_stored_correlation_id_and_trace() -> None:
    engine, graph = _engine(_inv())
    assert get_correlation_id() is None  # a resume after a restart: no request around it
    await engine._run(uuid4(), {})
    assert graph.seen["arg"] is None  # it really resumed from the checkpoint
    assert graph.seen["cid"] == "corr-from-start-01"
    assert graph.seen["tp"] is not None and graph.seen["tp"].split("-")[1] == STORED.split("-")[1]
    assert get_correlation_id() is None  # restored after the run


async def test_an_old_row_without_stored_context_still_runs() -> None:
    engine, graph = _engine(_inv(trace_context=None, correlation_id=None))
    await engine._run(uuid4(), {})
    assert graph.seen["arg"] is None and graph.seen["cid"] is None
