"""Phase 18a (ADR-029) against a REAL broker: what the in-memory broker can only imitate.

Runs only with AEOI_TEST_KAFKA=1 (make up PROFILE=kafka). Each test makes its OWN topics
(auto-create is off on the broker) and deletes them afterwards.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from aeoi_audit.config import Settings as AuditSettings
from aeoi_audit.main import build_app as build_audit
from aeoi_db.config import libpq_dsn
from aeoi_incident.events import KafkaPublisher, OutboxRelay
from aeoi_messaging import Consumer, KafkaTransport, Message, PoisonError
from aeoi_messaging.dlq import KafkaIO, list_dead_letters, replay
from aeoi_security.testing import KeyPair

from .test_audit_consumer import audit_url, create_incident  # noqa: F401  (fixture)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("AEOI_TEST_KAFKA") != "1", reason="needs Kafka (AEOI_TEST_KAFKA=1)"
    ),
]
BOOTSTRAP = os.environ.get("AEOI_KAFKA_BOOTSTRAP_SERVERS", "localhost:9094")


@pytest.fixture
async def topic() -> AsyncIterator[str]:
    from aiokafka.admin import AIOKafkaAdminClient, NewTopic

    name = f"t18a-{secrets.token_hex(4)}"
    admin: Any = AIOKafkaAdminClient(bootstrap_servers=BOOTSTRAP)
    await admin.start()
    await admin.create_topics([NewTopic(name, 3, 1), NewTopic(f"{name}.dlq", 1, 1)])
    try:
        yield name
    finally:
        await admin.delete_topics([name, f"{name}.dlq"])
        await admin.close()


async def produce(topic: str, *values: bytes, key: bytes = b"inc-1") -> None:
    async with KafkaIO(BOOTSTRAP) as io:
        for v in values:
            await io.send(topic, key, v, ())


async def drain(c: Consumer, want: int, seconds: float = 20) -> int:
    """Poll until `want` messages were committed (joining a group takes a few seconds)."""
    done, deadline = 0, asyncio.get_running_loop().time() + seconds
    while done < want and asyncio.get_running_loop().time() < deadline:
        done += await c.run_once()
    return done


def ev(n: int) -> bytes:
    return json.dumps({"event_id": f"e{n}", "n": n}).encode()


async def test_committed_messages_are_not_delivered_again_to_the_group(topic: str) -> None:
    seen: list[int] = []

    async def h(m: Message) -> None:
        seen.append(json.loads(m.value)["n"])

    await produce(topic, ev(1), ev(2), ev(3))
    c = Consumer("g1", KafkaTransport(BOOTSTRAP, f"{topic}-g1", [topic]), h)
    await c.transport.start()
    assert await drain(c, 3) == 3
    lag = await c.transport.lag()
    assert len(lag) == 3 and sum(lag.values()) == 0  # review L2: {} also sums to 0
    await c.transport.probe()  # a real metadata round trip answers (readiness uses it)
    await c.transport.stop()

    await produce(topic, ev(4))
    again = Consumer("g1", KafkaTransport(BOOTSTRAP, f"{topic}-g1", [topic]), h)
    await again.transport.start()
    assert await drain(again, 1) == 1
    await again.transport.stop()
    assert seen == [1, 2, 3, 4]  # 1-3 were committed: never seen twice


async def test_handled_but_uncommitted_messages_come_again(topic: str) -> None:
    await produce(topic, ev(1), ev(2))
    t = KafkaTransport(BOOTSTRAP, f"{topic}-g2", [topic])
    await t.start()
    got: list[Message] = []
    for _ in range(20):
        got += await t.poll(1.0, 10)
        if len(got) == 2:
            break
    assert len(got) == 2
    await t.stop()  # "crashed" before committing

    seen: list[int] = []

    async def h(m: Message) -> None:
        seen.append(json.loads(m.value)["n"])

    c = Consumer("g2", KafkaTransport(BOOTSTRAP, f"{topic}-g2", [topic]), h)
    await c.transport.start()
    assert await drain(c, 2) == 2
    await c.transport.stop()
    assert seen == [1, 2]  # at-least-once: the handler sees them again


async def test_poison_lands_in_the_dlq_topic_and_replay_sends_it_back(topic: str) -> None:
    async def h(m: Message) -> None:
        if m.value == b"bad":
            raise PoisonError("not an envelope")

    await produce(topic, b"bad", ev(2))
    c = Consumer("g3", KafkaTransport(BOOTSTRAP, f"{topic}-g3", [topic]), h)
    await c.transport.start()
    assert await drain(c, 2) == 2
    async with KafkaIO(BOOTSTRAP) as io:
        [d] = await list_dead_letters(io, f"{topic}.dlq")
        assert (d.kind, d.consumer, d.source_topic, d.message.value) == (
            "poison",
            "g3",
            topic,
            b"bad",
        )
        res = await replay(io, f"{topic}.dlq")
        assert len(res.replayed) == 1
    assert await drain(c, 1) == 1  # it came back, failed again, went to the DLQ again
    async with KafkaIO(BOOTSTRAP) as io:
        assert [x.replay_count for x in await list_dead_letters(io, f"{topic}.dlq")] == [0, 1]
    await c.transport.stop()


async def test_incident_event_reaches_the_audit_trail_through_real_kafka(
    incident_app: FastAPI,
    audit_url: str,  # noqa: F811  (the imported fixture)
    pubfile: Path,
    keys: KeyPair,
    migrated_db: str,
) -> None:
    """incident-service outbox -> KafkaPublisher -> incident.lifecycle -> audit consumer -> row.
    Uses the real topic (make kafka-init) and a throwaway consumer group."""
    inc = await create_incident(incident_app, keys)
    pub = KafkaPublisher(BOOTSTRAP)
    await pub.start()
    try:
        assert await OutboxRelay(incident_app.state.sessionmaker, pub).run_once() >= 1
    finally:
        await pub.stop()
    group = f"audit-test-{uuid4().hex[:8]}"
    audit = build_audit(AuditSettings(db_url_override=SecretStr(audit_url), jwt_public_key_file=pubfile,
                                      environment="test", consumer_enabled=True, consumer_group=group))  # type: ignore[call-arg]  # fmt: skip
    c: Consumer = audit.state.consumer
    await c.transport.start()
    try:
        deadline = datetime.now(UTC).timestamp() + 30
        found: list[Any] = []
        while not found and datetime.now(UTC).timestamp() < deadline:
            await c.run_once()
            with psycopg.connect(libpq_dsn(database=migrated_db)) as conn:
                found = conn.execute("SELECT action FROM audit.audit_events WHERE incident_id = %s",
                                     (inc["id"],)).fetchall()  # fmt: skip
        assert found == [("event.IncidentCreated",)]
    finally:
        await c.transport.stop()
        await audit.state.engine.dispose()


async def test_the_lag_command_refuses_an_unknown_topic(topic: str) -> None:
    """Review L1: 'total lag 0' for a typo'd topic looked healthy."""
    from aeoi_messaging.__main__ import _lag

    assert await _lag(BOOTSTRAP, f"{topic}-g9", f"{topic}-no-such-topic") == 2
    assert await _lag(BOOTSTRAP, f"{topic}-g9", topic) == 0


async def test_partitions_come_from_fresh_metadata_not_the_client_cache(topic: str) -> None:
    """Found on the Mac: aiokafka's cache holds no topics for a consumer without a subscription,
    so lag/dlq/replay saw nothing. The helper must see the topic, and say None for a missing one."""
    from aiokafka import AIOKafkaConsumer

    from aeoi_messaging.transport import topic_partitions

    c: Any = AIOKafkaConsumer(bootstrap_servers=BOOTSTRAP)
    await c.start()
    try:
        assert c.partitions_for_topic(topic) is None  # the trap, as observed
        assert await topic_partitions(c, topic) == [0, 1, 2]
        assert await topic_partitions(c, f"{topic}-missing") is None
    finally:
        await c.stop()
    async with KafkaIO(BOOTSTRAP) as io:
        with pytest.raises(LookupError):
            await io.read(f"{topic}-missing")
