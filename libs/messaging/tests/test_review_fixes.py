"""Regression tests for the Phase 18a review findings (docs/phases/phase-18.md, review table).

Each test names its finding and failed (or did not exist) before the fix.
"""

from __future__ import annotations

import json

from aeoi_messaging import Consumer, MemoryBroker, MemoryTransport, Message, PoisonError
from aeoi_messaging.consumer import MAX_DLQ_VALUE_BYTES, describe, replay_count
from aeoi_messaging.dlq import MAX_REPLAYS, MemoryIO, list_dead_letters, replay

TOPIC = "incident.lifecycle"
DLQ = f"{TOPIC}.dlq"


async def no_sleep(_s: float) -> None:
    return None


async def poison(_m: Message) -> None:
    raise PoisonError("bad schema")


def consumer(b: MemoryBroker, handler=poison, transport=None) -> Consumer:  # type: ignore[no-untyped-def]
    return Consumer("audit", transport or MemoryTransport(b, "audit", [TOPIC]), handler,
                    sleep=no_sleep)  # fmt: skip


async def started(c: Consumer) -> Consumer:
    await c.transport.start()
    return c


# --- H2: a bad dlq_replay_count header must not make the DLQ write fail for ever -------------
async def test_h2_a_bad_replay_header_is_read_as_zero_and_the_message_still_dead_letters() -> None:
    b = MemoryBroker()
    b.produce(TOPIC, b"k", b'{"event_id":"e1"}', (("dlq_replay_count", b"abc"),))
    b.produce(TOPIC, b"k", b'{"event_id":"e2"}', (("dlq_replay_count", b"-7"),))
    c = await started(consumer(b))
    assert await c.run_once() == 2  # before the fix: ValueError in the DLQ write -> stuck
    assert [m.header("dlq_replay_count") for m in b.messages(DLQ)] == ["0", "0"]
    assert replay_count(b.messages(DLQ)[0]) == 0


# --- H3: an original near Kafka's size limit cannot be copied into the DLQ -------------------
async def test_h3_an_oversized_message_leaves_a_pointer_and_is_never_replayed_blindly() -> None:
    b = MemoryBroker()
    big = json.dumps({"event_id": "big", "pad": "x" * MAX_DLQ_VALUE_BYTES}).encode()
    b.produce(TOPIC, b"k", big, (("trace", b"t1"), ("huge", b"h" * 5000)))
    c = await started(consumer(b))
    assert await c.run_once() == 1
    [d] = b.messages(DLQ)
    pointer = json.loads(d.value)
    assert pointer["dlq_truncated"] is True and pointer["bytes"] == len(big)
    assert len(pointer["sha256"]) == 64 and d.header("dlq_truncated") == "1"
    assert d.header("trace") == "t1" and d.header("huge") is None  # big headers dropped too
    assert d.header("dlq_source_offset") == "0"  # where the original still is
    res = await replay(MemoryIO(b), DLQ)
    assert res.replayed == [] and "too large" in res.refused[0][1]
    assert len(b.messages(TOPIC)) == 1  # the pointer was NOT sent back as if it were the event


# --- L4: error text must not carry SQL parameters (event payloads) ---------------------------
def test_describe_keeps_the_first_line_of_the_driver_error_only() -> None:
    class DBError(Exception):
        def __init__(self) -> None:
            super().__init__("[SQL: INSERT ...]\n[parameters: {'payload': 'SECRET-PAYLOAD'}]")
            self.orig = Exception("duplicate key value\nDETAIL:  Key (id)=(SECRET-ID) exists.")

    text = describe(DBError())
    assert text == "DBError: duplicate key value"
    assert "SECRET" not in text and "parameters" not in text


# --- M4: the broker is not there at boot -> keep trying, say degraded, then recover ----------
class FlakyStart(MemoryTransport):
    def __init__(self, b: MemoryBroker, fail_starts: int = 0, fail_polls: int = 0) -> None:
        super().__init__(b, "audit", [TOPIC])
        self.fail_starts, self.fail_polls, self.polls = fail_starts, fail_polls, 0

    async def start(self) -> None:
        if self.fail_starts:
            self.fail_starts -= 1
            raise ConnectionError("broker not up")
        await super().start()

    async def poll(self, timeout_s: float, max_records: int) -> list[Message]:
        self.polls += 1
        if self.fail_polls:
            self.fail_polls -= 1
            raise ConnectionError("broker blip")
        return await super().poll(timeout_s, max_records)


async def test_m4_start_is_retried_and_the_state_says_degraded_meanwhile() -> None:
    b = MemoryBroker()
    t = FlakyStart(b, fail_starts=2)
    states: list[str] = []

    async def h(_m: Message) -> None:
        return None

    c = consumer(b, h, t)

    async def watch(_s: float) -> None:
        states.append(c.status.state)

    c.sleep = watch
    b.produce(TOPIC, b"k", b'{"event_id":"e1"}')

    async def stop_when_done(m: Message) -> None:
        c.stop()

    c.handler = stop_when_done
    await c.run_forever()
    assert states == ["degraded", "degraded"]  # before the fix: the task died at boot
    assert c.status.processed == 1 and "broker not up" in (c.status.last_error or "")


async def test_m4_one_poll_blip_does_not_leave_an_idle_consumer_degraded_for_ever() -> None:
    b = MemoryBroker()
    t = FlakyStart(b, fail_polls=1)
    c = consumer(b, transport=t)

    async def stop_later(_s: float) -> None:
        if t.polls >= 3:
            c.stop()

    c.sleep = stop_later
    c.poll_timeout_s = 0
    seen: list[str] = []
    real_poll = t.poll

    async def spy(timeout_s: float, max_records: int) -> list[Message]:
        out = await real_poll(timeout_s, max_records)
        seen.append(c.status.state)
        if t.polls >= 3:
            c.stop()
        return out

    t.poll = spy  # type: ignore[method-assign]
    await c.run_forever()
    assert "running" in seen and c.status.healthy is False  # stopped at the end, not stuck
    assert seen[-1] == "running"  # recovered on an IDLE topic (no message to commit)


# --- L4: stop() is honoured between messages, the rest stays uncommitted ---------------------
async def test_stop_is_prompt_and_the_rest_of_the_batch_is_redelivered() -> None:
    b = MemoryBroker()
    for i in range(3):
        b.produce(TOPIC, b"k", json.dumps({"event_id": f"e{i}"}).encode())
    c = await started(consumer(b))

    async def h(_m: Message) -> None:
        c.stop()

    c.handler = h
    assert await c.run_once() == 1
    assert sum((await c.transport.lag()).values()) == 2


# --- M3: --offset must not bypass the replay limit --------------------------------------------
def dlq_entry(b: MemoryBroker, value: bytes, replays: int) -> None:
    b.produce(DLQ, b"k", value, (("dlq_source_topic", TOPIC.encode()),
                                 ("dlq_replay_count", str(replays).encode())))  # fmt: skip


async def test_m3_an_old_dlq_entry_picked_by_offset_still_counts_every_replay() -> None:
    b = MemoryBroker()
    dlq_entry(b, b'{"event_id":"e1"}', 0)  # offset 0: the first failure
    dlq_entry(b, b'{"event_id":"e1"}', MAX_REPLAYS)  # offset 1: after 3 replays
    res = await replay(MemoryIO(b), DLQ, offset=0)
    assert res.replayed == [] and "fix the cause" in res.refused[0][1]
    assert b.messages(TOPIC) == []


async def test_a_replay_carries_the_highest_count_forward() -> None:
    b = MemoryBroker()
    dlq_entry(b, b'{"event_id":"e1"}', 2)
    dlq_entry(b, b'{"event_id":"e1"}', 1)  # an older generation listed later must not reset it
    res = await replay(MemoryIO(b), DLQ)
    assert len(res.replayed) == 1
    assert b.messages(TOPIC)[0].header("dlq_replay_count") == "3"


# --- L3: a message without an event id is still ONE message across generations ---------------
async def test_l3_a_keyless_message_is_replayed_once_and_its_limit_is_kept() -> None:
    b = MemoryBroker()
    dlq_entry(b, b"not json", 0)
    dlq_entry(b, b"not json", MAX_REPLAYS)
    dlq_entry(b, b"other bytes", 0)
    [d1, _, d3] = await list_dead_letters(MemoryIO(b), DLQ)
    assert d1.event_id is None and d1.identity.startswith("sha256:") and d1.identity != d3.identity
    res = await replay(MemoryIO(b), DLQ)
    assert [m.value for m in b.messages(TOPIC)] == [b"other bytes"]  # before: offset identity
    assert len(res.refused) == 1  # "not json" hit the limit, counted across both entries


# --- L1: lag counts only what can still be read ------------------------------------------------
def test_l1_lag_of() -> None:
    from aeoi_messaging.transport import lag_of

    assert lag_of(end=10, committed=4, beginning=0) == 6
    assert lag_of(end=10, committed=None, beginning=0) == 10  # never committed: all of it
    assert lag_of(end=10, committed=None, beginning=7) == 3  # retention deleted 0..6
    assert lag_of(end=10, committed=2, beginning=7) == 3  # committed before what still exists
    assert lag_of(end=10, committed=10, beginning=0) == 0


async def test_h3_large_headers_count_towards_the_limit() -> None:
    b = MemoryBroker()
    half = MAX_DLQ_VALUE_BYTES // 2 + 1
    b.produce(TOPIC, b"k", json.dumps({"event_id": "h", "pad": "x" * half}).encode(),
              (("big", b"h" * half),))  # fmt: skip
    c = await started(consumer(b))
    assert await c.run_once() == 1
    [d] = b.messages(DLQ)
    assert d.header("dlq_truncated") == "1" and d.header("big") is None


# --- re-review --------------------------------------------------------------------------------
async def test_rr_m1_an_empty_poll_is_not_proof_that_the_broker_is_back() -> None:
    b = MemoryBroker()
    t = FlakyStart(b, fail_polls=1)
    c = await started(consumer(b, transport=t))
    c.poll_timeout_s = 0
    import contextlib

    with contextlib.suppress(ConnectionError):
        await c.run_once()  # the blip
    c.status.state = "degraded"  # what run_forever records for it
    b.reachable = False  # aiokafka: polls return nothing while the broker is down
    await c.run_once()
    assert c.status.state == "degraded"  # before: an empty poll flipped it to "running"
    b.reachable = True
    await c.run_once()
    assert c.status.state == "running" and c.status.healthy


async def test_rr_m2_a_dlq_that_is_down_makes_the_consumer_degraded() -> None:
    b = MemoryBroker(fail_sends=10**6)
    b.produce(TOPIC, b"k", b'{"event_id":"p"}')
    c = await started(consumer(b))
    states: list[str] = []

    async def watch(_s: float) -> None:
        states.append(c.status.state)
        if len(states) >= 3:
            c.stop()

    c.sleep = watch
    assert await c.run_once() == 0
    assert states and set(states) == {"degraded"}  # before: "running" while stuck on the DLQ


async def test_rr_l1_l2_a_truncated_letter_keeps_its_event_id_and_says_why() -> None:
    b = MemoryBroker()
    big = json.dumps({"event_id": "big-1", "pad": "x" * MAX_DLQ_VALUE_BYTES}).encode()
    b.produce(TOPIC, b"k", big)
    c = await started(consumer(b))
    await c.run_once()
    [d] = await list_dead_letters(MemoryIO(b), DLQ)
    assert d.event_id == "big-1" and d.truncated
    res = await replay(MemoryIO(b), DLQ, event_id="big-1")  # before: matched nothing, exit 0
    assert res.replayed == [] and "too large" in res.refused[0][1]


async def test_rr_l2_the_refusal_states_the_real_count() -> None:
    b = MemoryBroker()
    dlq_entry(b, b'{"event_id":"e1"}', 0)
    dlq_entry(b, b'{"event_id":"e1"}', MAX_REPLAYS)
    res = await replay(MemoryIO(b), DLQ, offset=0)
    assert f"already replayed {MAX_REPLAYS} times" in res.refused[0][1]


async def test_rr_l3_many_small_headers_cannot_overflow_the_dlq_record() -> None:
    b = MemoryBroker()
    big = json.dumps({"event_id": "h2", "pad": "x" * (MAX_DLQ_VALUE_BYTES // 2)}).encode()
    b.produce(TOPIC, b"k", big, tuple((f"h{i}", b"v" * 4000) for i in range(240)))
    c = await started(consumer(b))
    await c.run_once()
    [d] = b.messages(DLQ)
    size = len(d.value) + sum(len(k) + len(v) for k, v in d.headers)
    assert size < 10_000 and d.header("h0") is None


async def test_rr_l6_a_bad_attempts_header_does_not_break_the_listing() -> None:
    b = MemoryBroker()
    b.produce(DLQ, b"k", b'{"event_id":"x"}', (("dlq_attempts", b"lots"),))
    [d] = await list_dead_letters(MemoryIO(b), DLQ)  # before: ValueError for the whole topic
    assert d.attempts == 0


async def test_rr_m1_the_kafka_probe_fails_when_no_broker_answers() -> None:
    """Readiness relies on this: aiokafka polls return nothing while the broker is down, but a
    metadata request fails. No broker needed: nothing listens on port 1."""
    import pytest
    from aiokafka import AIOKafkaConsumer
    from aiokafka.errors import KafkaError

    from aeoi_messaging import KafkaTransport

    t = KafkaTransport("127.0.0.1:1", "probe", [])
    with pytest.raises(ConnectionError):
        await t.probe()  # not started
    t._consumer = AIOKafkaConsumer(bootstrap_servers="127.0.0.1:1")
    try:
        with pytest.raises((KafkaError, TimeoutError)):
            await t.probe(timeout_s=3)
    finally:
        await t._consumer.stop()
