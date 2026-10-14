"""ADR-029 guarantees, one test each, on the in-memory broker (same semantics as Kafka here:
per-key partitions, committed offsets per group, redelivery after a restart)."""

from __future__ import annotations

import json

import pytest

from aeoi_messaging import (
    Consumer,
    Kind,
    MemoryBroker,
    MemoryTransport,
    Message,
    PoisonError,
    TransientError,
)
from aeoi_messaging.dlq import MAX_REPLAYS, MemoryIO, list_dead_letters, replay

TOPIC = "incident.lifecycle"


def ev(i: int, key: str = "inc-1") -> tuple[bytes, bytes]:
    return key.encode(), json.dumps({"event_id": f"e{i}", "n": i}).encode()


async def no_sleep(_s: float) -> None:
    return None


def make(broker: MemoryBroker, handler, group: str = "audit", **kw) -> Consumer:  # type: ignore[no-untyped-def]
    return Consumer(group, MemoryTransport(broker, group, [TOPIC]), handler, sleep=no_sleep, **kw)


async def start(c: Consumer) -> Consumer:
    await c.transport.start()
    return c


async def test_handled_messages_are_committed_in_order() -> None:
    b, seen = MemoryBroker(), []
    for i in range(3):
        b.produce(TOPIC, *ev(i))

    async def h(m: Message) -> None:
        seen.append(json.loads(m.value)["n"])

    c = await start(make(b, h))
    assert await c.run_once() == 3
    assert seen == [0, 1, 2]  # one key -> one partition -> order kept
    assert await c.transport.lag() == {(TOPIC, p): 0 for p in range(3)}


async def test_a_crash_after_the_work_and_before_the_commit_redelivers() -> None:
    """At-least-once: the handler MUST be idempotent (dedupe on event_id)."""
    b, applied = MemoryBroker(), []
    b.produce(TOPIC, *ev(1))

    async def crash_on_commit(_m: Message) -> None:
        raise SystemExit  # the process dies before commit

    async def h(m: Message) -> None:
        applied.append(m.value)

    c = await start(make(b, h))
    c.transport.commit = crash_on_commit  # type: ignore[method-assign]
    with pytest.raises(SystemExit):
        await c.run_once()
    assert len(applied) == 1
    restarted = await start(make(b, h))  # a new member resumes from the COMMITTED offset
    assert await restarted.run_once() == 1
    assert len(applied) == 2  # delivered twice: dedupe is the handler's job (tested in audit)


async def test_poison_goes_to_the_dlq_at_once_and_does_not_block_the_next() -> None:
    b, done, calls = MemoryBroker(), [], []
    b.produce(TOPIC, *ev(1))
    b.produce(TOPIC, b"inc-1", b"not json", (("event_type", b"IncidentCreated"),))
    b.produce(TOPIC, *ev(3))

    async def h(m: Message) -> None:
        calls.append(m.offset)
        try:
            done.append(json.loads(m.value)["n"])
        except ValueError as exc:
            raise PoisonError("not JSON") from exc

    c = await start(make(b, h))
    assert await c.run_once() == 3
    assert done == [1, 3]
    assert calls.count(1) == 1  # tried ONCE - retrying a bad message only blocks the others
    [d] = await list_dead_letters(MemoryIO(b), f"{TOPIC}.dlq")
    assert (d.kind, d.consumer, d.attempts, d.source_topic) == ("poison", "audit", 1, TOPIC)
    assert d.message.value == b"not json" and d.message.key == b"inc-1"
    assert d.message.header("event_type") == "IncidentCreated"  # original headers kept
    assert c.status.dead_lettered == 1


async def test_a_dependency_outage_never_fills_the_dlq_and_nothing_is_lost() -> None:
    b, done = MemoryBroker(), []
    b.produce(TOPIC, *ev(1))
    down = {"n": 7}  # the DB is down for 7 tries (> max_attempts)

    async def h(m: Message) -> None:
        if down["n"] > 0:
            down["n"] -= 1
            raise TransientError("database unavailable")
        done.append(m.value)

    c = await start(make(b, h, max_attempts=3))
    assert await c.run_once() == 1
    assert len(done) == 1
    assert b.messages(f"{TOPIC}.dlq") == []
    assert (
        c.status.state == "running"
        and c.status.last_error == "TransientError: database unavailable"
    )


async def test_during_an_outage_the_offset_is_not_committed() -> None:
    b = MemoryBroker()
    b.produce(TOPIC, *ev(1))

    async def h(_m: Message) -> None:
        raise ConnectionError("db down")

    c = await start(make(b, h))
    waits = {"n": 0}

    async def stop_after_3(_s: float) -> None:
        waits["n"] += 1
        if waits["n"] == 3:
            c.stop()  # shut down mid-outage

    c.sleep = stop_after_3
    assert await c.run_once() == 0
    assert c.status.state == "degraded"
    assert sum((await c.transport.lag()).values()) == 1  # still owed: redelivered on restart
    assert b.messages(f"{TOPIC}.dlq") == []


async def test_an_unknown_error_gets_max_attempts_then_the_dlq() -> None:
    b, calls = MemoryBroker(), []
    b.produce(TOPIC, *ev(1))
    b.produce(TOPIC, *ev(2))

    async def h(m: Message) -> None:
        calls.append(m.offset)
        if m.offset == 0:
            raise KeyError("a bug only this message triggers")

    c = await start(make(b, h, max_attempts=3))
    assert await c.run_once() == 2
    assert calls == [0, 0, 0, 1]
    [d] = await list_dead_letters(MemoryIO(b), f"{TOPIC}.dlq")
    assert (d.kind, d.attempts) == ("unknown", 3)
    assert "KeyError" in d.error


async def test_if_the_dlq_is_down_the_message_is_kept_not_dropped() -> None:
    b = MemoryBroker()
    b.produce(TOPIC, b"inc-1", b"bad")

    async def h(_m: Message) -> None:
        raise PoisonError("bad")

    b.fail_sends = 2  # the DLQ write fails twice
    c = await start(make(b, h))
    assert await c.run_once() == 1
    assert len(b.messages(f"{TOPIC}.dlq")) == 1  # written on the 3rd try
    assert sum((await c.transport.lag()).values()) == 0  # committed only after the DLQ write


async def test_replay_sends_the_original_back_and_a_repeat_offender_is_refused() -> None:
    b, fixed = MemoryBroker(), {"ok": False}
    b.produce(TOPIC, *ev(1))

    async def h(m: Message) -> None:
        if not fixed["ok"]:
            raise PoisonError("unknown schema_version")

    c = await start(make(b, h))
    await c.run_once()
    io = MemoryIO(b)
    for round_ in range(1, MAX_REPLAYS + 1):  # replayed, still failing, back in the DLQ
        res = await replay(io, f"{TOPIC}.dlq", event_id="e1")
        assert [d.event_id for d in res.replayed] == ["e1"] and res.refused == []
        await c.run_once()
        assert len(b.messages(f"{TOPIC}.dlq")) == round_ + 1
    res = await replay(io, f"{TOPIC}.dlq", event_id="e1")
    assert res.replayed == [] and "fix the cause" in res.refused[0][1]

    fresh = MemoryBroker()  # after a fix: replay -> handled -> not dead-lettered again
    fresh.produce(TOPIC, *ev(9))
    c2 = await start(make(fresh, h))
    await c2.run_once()
    fixed["ok"] = True
    res = await replay(MemoryIO(fresh), f"{TOPIC}.dlq")
    assert [d.event_id for d in res.replayed] == ["e9"]
    replayed_msg = fresh.messages(TOPIC)[-1]
    assert replayed_msg.header("dlq_replay_count") == "1"
    assert replayed_msg.header("dlq_error") is None  # failure metadata does not travel back
    assert await c2.run_once() == 1 and len(fresh.messages(f"{TOPIC}.dlq")) == 1


async def test_dry_run_replays_nothing() -> None:
    b = MemoryBroker()
    b.produce(f"{TOPIC}.dlq", b"k", json.dumps({"event_id": "x"}).encode(),
              (("dlq_source_topic", TOPIC.encode()), ("dlq_replay_count", b"0")))  # fmt: skip
    res = await replay(MemoryIO(b), f"{TOPIC}.dlq", dry_run=True)
    assert len(res.replayed) == 1 and b.messages(TOPIC) == []


def test_classification() -> None:
    from aeoi_messaging import default_classify

    assert default_classify(PoisonError("x")) is Kind.POISON
    assert default_classify(TransientError("x")) is Kind.TRANSIENT
    assert default_classify(ConnectionError()) is Kind.TRANSIENT
    assert default_classify(TimeoutError()) is Kind.TRANSIENT
    assert default_classify(ValueError()) is Kind.UNKNOWN
