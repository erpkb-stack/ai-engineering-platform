"""Where messages come from and go to (ADR-029).

`Transport` is the only thing a consumer knows about the broker. Two implementations:
- `KafkaTransport` (aiokafka): a consumer group, offsets committed BY US after the work is
  done (never auto-commit), an idempotent producer for the dead-letter topic.
- `MemoryTransport` over a `MemoryBroker`: the same semantics in-process - per-key partitions,
  committed offsets per group, redelivery of everything after the last commit when a new
  transport (= a restarted consumer) joins. Tests and the sandbox use it; the Kafka-specific
  behaviour is proven by tests/integration/messaging (AEOI_TEST_KAFKA=1, a real broker).
"""

from __future__ import annotations

import asyncio
import contextlib
import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Protocol

Headers = tuple[tuple[str, bytes], ...]


@dataclass(frozen=True)
class Message:
    topic: str
    partition: int
    offset: int
    key: bytes | None
    value: bytes
    headers: Headers = ()

    def header(self, name: str) -> str | None:
        for k, v in self.headers:
            if k == name:
                return v.decode(errors="replace")
        return None


class Transport(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def poll(self, timeout_s: float, max_records: int) -> list[Message]: ...
    async def commit(self, message: Message) -> None: ...  # done up to and incl. this one
    async def send(self, topic: str, key: bytes | None, value: bytes, headers: Headers) -> None: ...
    async def lag(self) -> dict[tuple[str, int], int]: ...  # (topic, partition) -> messages behind
    async def probe(
        self, timeout_s: float = 1.5
    ) -> None: ...  # raises if the broker is unreachable


# ------------------------------------------------------------------ in-memory
@dataclass
class MemoryBroker:
    """Topics -> partitions -> messages; group -> committed next offset per partition."""

    partitions: int = 3
    log: dict[str, list[list[Message]]] = field(default_factory=dict)
    committed: dict[str, dict[tuple[str, int], int]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    fail_sends: int = 0  # the next N sends raise (a DLQ that is down)
    reachable: bool = True  # False = the broker is down: probe() raises (polls just return nothing)

    def topic(self, name: str) -> list[list[Message]]:
        return self.log.setdefault(name, [[] for _ in range(self.partitions)])

    def produce(
        self, topic: str, key: bytes | None, value: bytes, headers: Headers = ()
    ) -> Message:
        parts = self.topic(topic)
        p = zlib.crc32(key) % len(parts) if key else 0  # same key -> same partition (order)
        msg = Message(topic, p, len(parts[p]), key, value, headers)
        parts[p].append(msg)
        return msg

    def messages(self, topic: str) -> list[Message]:
        return [m for part in self.topic(topic) for m in part]


async def topic_partitions(consumer: Any, topic: str) -> list[int] | None:
    """The partitions of `topic` from a FRESH metadata request; None = the topic does not exist.

    Do not use `partitions_for_topic()` on a consumer that has no subscription: aiokafka's cache
    holds only tracked topics (bootstrap asks for none), and `topics()` fills a NEW metadata
    object without updating that cache - so it answers None for every topic (found on the Mac,
    Phase 18a: lag, dlq and replay saw nothing on a real broker).
    """
    cluster = await consumer._client.fetch_all_metadata()
    parts = cluster.partitions_for_topic(topic)
    return sorted(parts) if parts is not None else None


def lag_of(end: int, committed: int | None, beginning: int) -> int:
    """Messages this group still has to read (review L1). Never committed -> it starts at the
    beginning (auto_offset_reset=earliest); messages already deleted by retention can never be
    read, so they are not lag."""
    return max(0, end - max(committed if committed is not None else beginning, beginning))


class MemoryTransport:
    def __init__(self, broker: MemoryBroker, group: str, topics: list[str]) -> None:
        self.broker, self.group, self.topics = broker, group, topics
        self._position: dict[tuple[str, int], int] = {}

    async def start(self) -> None:
        # a (re)started member resumes from the group's COMMITTED offsets: whatever was handled
        # but not committed is delivered again (at-least-once)
        self._position = {}
        for t in self.topics:
            for p in range(len(self.broker.topic(t))):
                self._position[(t, p)] = self.broker.committed[self.group].get((t, p), 0)

    async def stop(self) -> None:
        return None

    async def poll(self, timeout_s: float, max_records: int) -> list[Message]:
        out: list[Message] = []
        for (t, p), pos in sorted(self._position.items()):
            for msg in self.broker.topic(t)[p][pos:]:
                if len(out) >= max_records:
                    return out
                out.append(msg)
                self._position[(t, p)] = msg.offset + 1
        return out

    def rewind(self, message: Message) -> None:
        """Deliver this message (and everything after it on its partition) again."""
        self._position[(message.topic, message.partition)] = message.offset

    async def commit(self, message: Message) -> None:
        self.broker.committed[self.group][(message.topic, message.partition)] = message.offset + 1

    async def send(self, topic: str, key: bytes | None, value: bytes, headers: Headers) -> None:
        if self.broker.fail_sends > 0:
            self.broker.fail_sends -= 1
            raise ConnectionError("broker unavailable (simulated)")
        self.broker.produce(topic, key, value, headers)

    async def probe(self, timeout_s: float = 1.5) -> None:
        if not self.broker.reachable:
            raise ConnectionError("broker unreachable")

    async def lag(self) -> dict[tuple[str, int], int]:
        return {(t, p): len(self.broker.topic(t)[p]) - self.broker.committed[self.group].get((t, p), 0)
                for t in self.topics for p in range(len(self.broker.topic(t)))}  # fmt: skip


# ------------------------------------------------------------------ Kafka
class KafkaTransport:
    """A consumer-group member. Offsets are committed only by `commit()` (after the work)."""

    def __init__(self, bootstrap_servers: str, group: str, topics: list[str], *,
                 max_poll_interval_ms: int = 600_000) -> None:  # fmt: skip
        self._bootstrap, self.group, self.topics = bootstrap_servers, group, topics
        self._max_poll_interval_ms = max_poll_interval_ms
        self._consumer: Any = None
        self._producer: Any = None

    async def start(self) -> None:
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

        self._consumer = AIOKafkaConsumer(
            *self.topics,
            bootstrap_servers=self._bootstrap,
            group_id=self.group,
            enable_auto_commit=False,  # we commit after the side effect, never before
            auto_offset_reset="earliest",  # a new group reads history, not just new events
            isolation_level="read_committed",
            # a dependency outage blocks the partition while we retry; past this the member is
            # removed and the partition re-assigned - nothing is lost (offset not committed)
            max_poll_interval_ms=self._max_poll_interval_ms,
        )
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._bootstrap, acks="all", enable_idempotence=True,
            request_timeout_ms=5000,
        )  # fmt: skip
        try:
            await self._producer.start()
            await self._consumer.start()
        except BaseException:
            with contextlib.suppress(Exception):  # the START failure is the error to report
                await self.stop()  # a half-started member must not leak a producer per retry
            raise

    async def stop(self) -> None:
        consumer, producer, self._consumer, self._producer = (
            self._consumer,
            self._producer,
            None,
            None,
        )
        try:
            if consumer is not None:
                await consumer.stop()
        finally:  # the producer is stopped even if the consumer's stop fails
            if producer is not None:
                await producer.stop()

    async def poll(self, timeout_s: float, max_records: int) -> list[Message]:
        batches = await self._consumer.getmany(timeout_ms=int(timeout_s * 1000),
                                               max_records=max_records)  # fmt: skip
        out = [Message(r.topic, r.partition, r.offset, r.key, r.value or b"",
                       tuple((k, v or b"") for k, v in (r.headers or ())))
               for records in batches.values() for r in records]  # fmt: skip
        return sorted(out, key=lambda m: (m.topic, m.partition, m.offset))

    def rewind(self, message: Message) -> None:
        from aiokafka import TopicPartition

        self._consumer.seek(TopicPartition(message.topic, message.partition), message.offset)

    async def commit(self, message: Message) -> None:
        from aiokafka import TopicPartition

        await self._consumer.commit(
            {TopicPartition(message.topic, message.partition): message.offset + 1}
        )

    async def send(self, topic: str, key: bytes | None, value: bytes, headers: Headers) -> None:
        await self._producer.send_and_wait(topic, value, key=key, headers=list(headers))

    async def probe(self, timeout_s: float = 1.5) -> None:
        """A fresh metadata request. Needed because aiokafka HIDES an outage: getmany() returns
        nothing and commit() retries for ever, so 'no error' never means 'the broker is there'
        (re-review M-1). Raises KafkaError (no broker answered) or TimeoutError."""
        if self._consumer is None:
            raise ConnectionError("not started")
        await asyncio.wait_for(self._consumer.topics(), timeout_s)

    async def lag(self) -> dict[tuple[str, int], int]:
        tps = list(self._consumer.assignment())
        if not tps:
            return {}
        ends = await self._consumer.end_offsets(tps)
        starts = await self._consumer.beginning_offsets(tps)
        return {(tp.topic, tp.partition): lag_of(ends[tp], await self._consumer.committed(tp),
                                                 starts[tp]) for tp in tps}  # fmt: skip
