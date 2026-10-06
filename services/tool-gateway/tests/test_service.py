"""The invoke pipeline with fake handlers and a fake DB: every path is RECORDED."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel, Field

from aeoi_common.ratelimit import InMemoryTokenBucket
from aeoi_common.resilience import RetryPolicy
from aeoi_security.rbac import Perm
from aeoi_tools import schemas as s
from aeoi_tools.contracts import (
    SideEffect,
    ToolContext,
    ToolExecutionError,
    ToolSpec,
    ToolUnavailableError,
)
from aeoi_tools.policy import AgentPolicy, PolicyConfig
from aeoi_tools.service import AuditWriteError, ToolService

from .conftest import FakeDB, ctx


class EchoIn(s.In):
    text: str = Field(max_length=50)


class EchoItem(s.Item):
    text: str


class EchoOut(s.Out):
    items: list[EchoItem] = Field(default_factory=list, max_length=10)


class Behaviour:
    def __init__(self) -> None:
        self.calls = 0
        self.plan: list[Any] = []  # per call: "ok" | exception | ("sleep", s) | "wrong"

    async def __call__(self, q: EchoIn, _: ToolContext) -> BaseModel:
        self.calls += 1
        step = self.plan.pop(0) if self.plan else "ok"
        if isinstance(step, tuple):
            await asyncio.sleep(step[1])
        elif isinstance(step, BaseException):
            raise step
        elif step == "wrong":
            return s.CommitOut()
        return EchoOut(items=[EchoItem(text=q.text)])


def spec(handler: Behaviour, **kw: Any) -> ToolSpec:
    base: dict[str, Any] = {
        "name": "echo_text",
        "description": "Echo the text back. Test-only tool used by the pipeline unit tests.",
        "input_model": EchoIn,
        "output_model": EchoOut,
        "permissions": frozenset({Perm.LOGS_READ}),
        "side_effect": SideEffect.READ,
        "handler": handler,
        "dependency": "fake",
        "timeout_s": 0.5,
        "max_attempts": 2,
    }
    return ToolSpec(**(base | kw))


def svc(db: FakeDB, *specs: ToolSpec, burst: int = 100) -> ToolService:
    policy = PolicyConfig(agents={"bot": AgentPolicy(tools=frozenset({"echo_text"}))})
    return ToolService(
        {sp.name: sp for sp in specs},
        policy,
        InMemoryTokenBucket(rate_per_s=0.001, burst=burst),
        db,  # type: ignore[arg-type]
        bulkhead_limits={"fake": 2},
        breaker_threshold=2,
        breaker_cooldown_s=60,
        retry=RetryPolicy(max_attempts=2, base_delay_s=0.0, max_delay_s=0.0),
    )


def last_call(db: FakeDB) -> Any:
    return db.of("ToolCall")[-1]


async def test_ok_is_recorded_with_audit_event_in_same_unit(fake_db: FakeDB) -> None:
    h = Behaviour()
    out = await svc(fake_db, spec(h)).invoke("echo_text", {"text": "hi"}, ctx("ENGINEER"))
    assert out.status == "OK" and out.evidence_ids == [f"ev_{out.call_id.hex}_0"]
    row, ev = last_call(fake_db), fake_db.of("AuditOutbox")[-1]
    assert row.id == out.call_id and row.status == "OK" and row.input == {"text": "hi"}
    assert ev.event["details"]["tool_call_id"] == str(out.call_id)
    assert ev.event["outcome"] == "SUCCESS" and ev.event["actor"].startswith("user:")


async def test_transient_failure_is_retried_once(fake_db: FakeDB) -> None:
    h = Behaviour()
    h.plan = [ToolUnavailableError("blip"), "ok"]
    out = await svc(fake_db, spec(h)).invoke("echo_text", {"text": "x"}, ctx("SRE"))
    assert out.status == "OK" and out.attempts == 2 and last_call(fake_db).attempt == 2


async def test_permanent_failure_is_not_retried(fake_db: FakeDB) -> None:
    h = Behaviour()
    h.plan = [ToolExecutionError("ambiguous")]
    out = await svc(fake_db, spec(h)).invoke("echo_text", {"text": "x"}, ctx("SRE"))
    assert (out.status, out.http_status, h.calls) == ("ERROR", 422, 1)


async def test_timeout_is_one_deadline_and_trips_the_breaker(fake_db: FakeDB) -> None:
    h = Behaviour()
    h.plan = [("sleep", 5), ("sleep", 5)]
    service = svc(fake_db, spec(h, timeout_s=0.2))
    t0 = asyncio.get_running_loop().time()
    out = await service.invoke("echo_text", {"text": "x"}, ctx("SRE"))
    assert out.status == "TIMEOUT" and out.http_status == 504
    assert asyncio.get_running_loop().time() - t0 < 0.6  # NOT 2 x timeout
    assert last_call(fake_db).status == "TIMEOUT"
    h.plan = [("sleep", 5)]
    await service.invoke("echo_text", {"text": "x"}, ctx("SRE"))
    assert service.breaker_states()["fake"] == "open"  # timeouts count as failures
    before = h.calls
    out = await service.invoke("echo_text", {"text": "x"}, ctx("SRE"))
    assert out.reason == "circuit_open" and out.http_status == 503 and h.calls == before


async def test_invalid_input_is_recorded_without_echoing_it(fake_db: FakeDB) -> None:
    out = await svc(fake_db, spec(Behaviour())).invoke(
        "echo_text", {"text": "t" * 60, "token": "s3cr3t"}, ctx("SRE")
    )
    assert out.http_status == 422 and out.errors
    assert "s3cr3t" not in str(out.errors) and last_call(fake_db).input["token"] == "[REDACTED]"


async def test_denials_are_recorded(fake_db: FakeDB) -> None:
    service = svc(fake_db, spec(Behaviour()))
    out = await service.invoke("echo_text", {"text": "x"}, ctx("MANAGER"))  # no logs:read
    assert (out.status, out.http_status, out.reason) == ("DENIED", 403, "missing_permission")
    assert last_call(fake_db).denial_reason.startswith("missing_permission")
    assert fake_db.of("AuditOutbox")[-1].event["outcome"] == "DENIED"
    out = await service.invoke("drop_tables", {}, ctx("SRE"))
    assert out.http_status == 404 and last_call(fake_db).tool_name == "drop_tables"


async def test_rate_limit_per_user_and_tool(fake_db: FakeDB) -> None:
    service = svc(fake_db, spec(Behaviour()), burst=2)
    c = ctx("SRE")
    codes = [(await service.invoke("echo_text", {"text": "x"}, c)).http_status for _ in range(3)]
    assert codes == [200, 200, 429]
    assert last_call(fake_db).denial_reason.startswith("rate_limited")
    other = await service.invoke("echo_text", {"text": "x"}, ctx("SRE"))  # another user
    assert other.http_status == 200


async def test_output_contract_violation_is_an_error_not_data(fake_db: FakeDB) -> None:
    h = Behaviour()
    h.plan = ["wrong"]
    out = await svc(fake_db, spec(h)).invoke("echo_text", {"text": "x"}, ctx("SRE"))
    assert out.http_status == 502 and out.data is None


async def test_cannot_record_means_no_data(fake_db: FakeDB) -> None:
    fake_db.fail = True
    with pytest.raises(AuditWriteError):
        await svc(fake_db, spec(Behaviour())).invoke("echo_text", {"text": "x"}, ctx("SRE"))


async def test_crash_in_handler_is_contained(fake_db: FakeDB) -> None:
    h = Behaviour()
    h.plan = [ZeroDivisionError("boom")]
    out = await svc(fake_db, spec(h)).invoke("echo_text", {"text": "x"}, ctx("SRE"))
    assert out.http_status == 500 and "boom" not in out.detail and h.calls == 1


async def test_denials_are_rate_limited_too(fake_db: FakeDB) -> None:
    """Review finding: denials write rows, so they must not be free to flood."""
    service = svc(fake_db, spec(Behaviour()), burst=2)
    c = ctx("MANAGER")  # always denied (no logs:read)
    codes = [(await service.invoke("echo_text", {"text": "x"}, c)).http_status for _ in range(3)]
    assert codes == [403, 403, 429]
    codes = [(await service.invoke(f"made_up_{i}", {}, c)).http_status for i in range(3)]
    assert codes == [404, 404, 429]  # invented names share one bucket


async def test_service_refusal_flood_stops_writing_rows(fake_db: FakeDB) -> None:
    service = svc(fake_db, spec(Behaviour()), burst=2)
    ids = [
        await service.record_service_denial(
            service="noisy", tool="x", reason="service_without_user", detail="", correlation_id=None
        )
        for _ in range(4)
    ]
    assert ids[:2] != [None, None] and ids[2:] == [None, None]
    assert len(fake_db.of("AuditOutbox")) == 2


async def test_nul_in_args_is_a_recorded_422(fake_db: FakeDB) -> None:
    """Review finding: Postgres JSONB rejects \\u0000 -> the RECORD itself failed."""
    out = await svc(fake_db, spec(Behaviour())).invoke(
        "echo_text", {"text": "a\x00b", "k\x00": 1}, ctx("SRE")
    )
    assert out.http_status == 422
    row = last_call(fake_db)
    assert not s._has_nul(row.input) and "\x00" not in row.tool_name  # str() would hide it
