"""Dead letters: read them, replay them after a fix (ADR-029).

A DLQ record is the ORIGINAL key, value and headers plus `dlq_*` headers saying who failed,
how (poison / unknown), why, where it came from and how often it was replayed already.
Replay sends the original back to its SOURCE topic. Every consumer group of that topic sees
it again - harmless, because every handler dedupes on event_id. A message that comes back to
the DLQ again and again is not "unlucky": `MAX_REPLAYS` stops the loop and says so.
Kafka records are immutable: replaying does not remove the DLQ entry; `list` shows what
happened, the audit of replays is the DLQ itself (each failure is a new entry).
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Protocol

from aeoi_messaging.consumer import event_id_of, int_header, replay_count
from aeoi_messaging.transport import Headers, MemoryBroker, Message, topic_partitions

MAX_REPLAYS = 3


class TopicIO(Protocol):
    async def read(self, topic: str) -> list[Message]: ...  # whole topic, no group, no commit
    async def send(self, topic: str, key: bytes | None, value: bytes, headers: Headers) -> None: ...


@dataclass(frozen=True)
class DeadLetter:
    message: Message
    event_id: str | None
    consumer: str
    kind: str
    error: str
    attempts: int
    source_topic: str
    failed_at: str
    replay_count: int

    @classmethod
    def of(cls, m: Message) -> DeadLetter:
        return cls(m, event_id_of(m.value), m.header("dlq_consumer") or "?", m.header("dlq_error_kind") or "?",
                   m.header("dlq_error") or "", int_header(m, "dlq_attempts"),
                   m.header("dlq_source_topic") or m.topic.removesuffix(".dlq"),
                   m.header("dlq_failed_at") or "", replay_count(m))  # fmt: skip

    @property
    def identity(self) -> str:
        """The same failing message across generations: its event id, else its bytes (review
        L3 - a DLQ offset is new every generation, so it would replay every copy again)."""
        return self.event_id or "sha256:" + hashlib.sha256(self.message.value).hexdigest()

    @property
    def truncated(self) -> bool:
        return self.message.header("dlq_truncated") == "1"


async def list_dead_letters(io: TopicIO, dlq: str) -> list[DeadLetter]:
    return [DeadLetter.of(m) for m in await io.read(dlq)]


@dataclass(frozen=True)
class ReplayResult:
    replayed: list[DeadLetter]
    refused: list[tuple[DeadLetter, str]]


async def replay(io: TopicIO, dlq: str, *, event_id: str | None = None, offset: int | None = None,
                 dry_run: bool = False) -> ReplayResult:  # fmt: skip
    """Send matching dead letters back to their source topic (all if no filter is given)."""
    letters = await list_dead_letters(io, dlq)
    # the CURRENT replay count of a message is the highest over ALL its failures - also when
    # an older entry is picked by --offset (review M3: that bypassed the limit)
    count: dict[str, int] = {}
    for d in letters:
        count[d.identity] = max(count.get(d.identity, 0), d.replay_count)
    picked = [d for d in letters
              if (event_id is None or d.event_id == event_id)
              and (offset is None or d.message.offset == offset)]  # fmt: skip
    seen: set[str] = set()
    replayed, refused = [], []
    for d in reversed(picked):  # NEWEST first
        if d.identity in seen:  # the same message failed more than once: replay it once
            continue
        seen.add(d.identity)
        if d.truncated:
            refused.append((d, "too large for the DLQ: only a pointer was kept - replay from "
                               f"{d.source_topic} at the offset in its dlq_source_* headers"))  # fmt: skip
            continue
        if count[d.identity] >= MAX_REPLAYS:
            refused.append(
                (d, f"already replayed {count[d.identity]} times: fix the cause, not the queue")
            )
            continue
        if not dry_run:
            headers = tuple((k, v) for k, v in d.message.headers if not k.startswith("dlq_"))
            headers += (("dlq_replay_count", str(count[d.identity] + 1).encode()),)
            await io.send(d.source_topic, d.message.key, d.message.value, headers)
        replayed.append(d)
    return ReplayResult(replayed[::-1], refused[::-1])


class MemoryIO:
    def __init__(self, broker: MemoryBroker) -> None:
        self.broker = broker

    async def read(self, topic: str) -> list[Message]:
        return self.broker.messages(topic)

    async def send(self, topic: str, key: bytes | None, value: bytes, headers: Headers) -> None:
        self.broker.produce(topic, key, value, headers)


class KafkaIO:
    """Reads a whole topic without a group (commits nothing); sends with an idempotent producer."""

    def __init__(self, bootstrap_servers: str) -> None:
        self._bootstrap = bootstrap_servers
        self._producer: Any = None

    async def __aenter__(self) -> KafkaIO:
        from aiokafka import AIOKafkaProducer

        self._producer = AIOKafkaProducer(bootstrap_servers=self._bootstrap, acks="all",
                                          enable_idempotence=True)  # fmt: skip
        await self._producer.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._producer.stop()

    async def read(self, topic: str) -> list[Message]:
        from aiokafka import AIOKafkaConsumer, TopicPartition

        c = AIOKafkaConsumer(bootstrap_servers=self._bootstrap, enable_auto_commit=False)
        await c.start()
        try:
            parts = await topic_partitions(c, topic)
            if parts is None:  # never "0 dead letters" for a topic that is not there
                raise LookupError(f"unknown topic {topic!r} (auto-create is off: make kafka-init)")
            tps = [TopicPartition(topic, p) for p in parts]
            if not tps:
                return []
            c.assign(tps)
            await c.seek_to_beginning(*tps)
            ends = await c.end_offsets(tps)
            out: list[Message] = []
            deadline = time.monotonic() + 30  # never hang on a topic that does not drain

            async def behind() -> bool:  # a plain loop: `any(await ...)` would be an async gen
                for tp in tps:
                    if await c.position(tp) < ends[tp]:
                        return True
                return False

            while await behind():
                if time.monotonic() > deadline:
                    raise TimeoutError(f"could not read {topic} to its end within 30 s")
                for records in (await c.getmany(timeout_ms=1000)).values():
                    out += [Message(r.topic, r.partition, r.offset, r.key, r.value or b"",
                                    tuple((k, v or b"") for k, v in (r.headers or ())))
                            for r in records if r.offset < ends[TopicPartition(r.topic, r.partition)]]  # fmt: skip
            return sorted(out, key=lambda m: (m.partition, m.offset))
        finally:
            await c.stop()

    async def send(self, topic: str, key: bytes | None, value: bytes, headers: Headers) -> None:
        await self._producer.send_and_wait(topic, value, key=key, headers=list(headers))
