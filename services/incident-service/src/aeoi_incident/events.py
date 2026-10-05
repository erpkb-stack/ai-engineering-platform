"""Transactional outbox + relay (ADR-003).

Write side: `stage_event()` adds an outbox row in the SAME transaction as the state change,
so "incident saved but event lost" (or the reverse) cannot happen.

Relay: reads unpublished rows with FOR UPDATE SKIP LOCKED (several relays never publish the
same row twice at the same time), publishes, marks published_at. Delivery is AT-LEAST-ONCE:
if the process dies after publish and before commit, the row is published again, so
consumers must dedupe on event_id (incident.processed_events). Exactly-once is not claimed.

Ordering: one relay publishes in created_at order and stops a batch at the first failure,
so a later event for an incident never overtakes an earlier one. [Prod] with many relays,
use one leader (advisory lock) or CDC (Debezium) to keep per-aggregate order.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import UUID

import structlog
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aeoi_common.correlation import get_correlation_id
from aeoi_db.models.incident import Outbox
from aeoi_models.events import EventEnvelope, EventType

log = structlog.get_logger("aeoi.outbox")
TOPIC_INCIDENT_LIFECYCLE = "incident.lifecycle"


def stage_event(
    session: AsyncSession,
    *,
    event_type: EventType,
    payload: BaseModel,
    actor: str,
    incident_id: UUID,
    topic: str = TOPIC_INCIDENT_LIFECYCLE,
) -> UUID:
    """Add an event to the outbox. Caller owns the transaction."""
    # Parametrise with the payload's RUNTIME type so pydantic serialises all its fields.
    envelope_cls: Any = cast(Any, EventEnvelope)[type(payload)]
    envelope = envelope_cls(
        event_type=event_type, actor=actor, incident_id=incident_id, payload=payload
    )
    session.add(
        Outbox(
            id=envelope.event_id,
            aggregate_type="incident",
            aggregate_id=incident_id,
            topic=topic,
            event_type=event_type.value,
            payload=json.loads(envelope.model_dump_json()),
            headers={"correlation_id": get_correlation_id() or "", "schema_version": "1"},
        )
    )
    return cast(UUID, envelope.event_id)


@dataclass(frozen=True)
class OutgoingEvent:
    event_id: UUID
    topic: str
    key: bytes
    event_type: str
    value: dict[str, Any]
    headers: dict[str, str]


class Publisher(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def publish(self, event: OutgoingEvent) -> None: ...


class LogPublisher:
    """Default transport before Kafka is running: the event is logged, then marked published."""

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def publish(self, event: OutgoingEvent) -> None:
        log.info(
            "event_published",
            transport="log",
            topic=event.topic,
            event_type=event.event_type,
            event_id=str(event.event_id),
        )


@dataclass
class InMemoryPublisher:
    """Tests: records events; can be told to fail N times."""

    published: list[OutgoingEvent] = field(default_factory=list)
    fail_times: int = 0

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def publish(self, event: OutgoingEvent) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("broker unavailable (simulated)")
        self.published.append(event)


class KafkaPublisher:
    """Kafka via aiokafka: idempotent producer, acks=all, key = incident id (per-incident order)."""

    def __init__(self, bootstrap_servers: str) -> None:
        self._bootstrap = bootstrap_servers
        self._producer: Any = None

    async def start(self) -> None:
        from aiokafka import AIOKafkaProducer

        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._bootstrap,
            acks="all",
            enable_idempotence=True,
            request_timeout_ms=5000,
            value_serializer=lambda v: json.dumps(v).encode(),
        )
        await self._producer.start()

    async def stop(self) -> None:
        if self._producer is not None:
            await self._producer.stop()

    async def publish(self, event: OutgoingEvent) -> None:
        headers = [(k, v.encode()) for k, v in event.headers.items()]
        headers.append(("event_type", event.event_type.encode()))
        await self._producer.send_and_wait(event.topic, event.value, key=event.key, headers=headers)


class OutboxRelay:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        publisher: Publisher,
        *,
        batch_size: int = 100,
        interval_s: float = 1.0,
        max_backoff_s: float = 30.0,
    ) -> None:
        self._sessions = sessionmaker
        self._publisher = publisher
        self._batch = batch_size
        self._interval = interval_s
        self._max_backoff = max_backoff_s
        self._stopping = asyncio.Event()

    async def run_once(self) -> int:
        """Publish one batch; returns how many were published.

        On a publish failure: count the attempt, stop the batch (keep order), COMMIT what
        already succeeded, then raise so the caller backs off.
        """
        published = 0
        failure: Exception | None = None
        async with self._sessions() as session, session.begin():
            rows = (
                await session.scalars(
                    select(Outbox)
                    .where(Outbox.published_at.is_(None))
                    .order_by(Outbox.created_at, Outbox.id)
                    .limit(self._batch)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for row in rows:
                event = OutgoingEvent(
                    event_id=row.id,
                    topic=row.topic,
                    key=str(row.aggregate_id).encode(),
                    event_type=row.event_type,
                    value=row.payload,
                    headers={k: str(v) for k, v in row.headers.items()},
                )
                try:
                    await self._publisher.publish(event)
                except Exception as exc:
                    await session.execute(
                        update(Outbox)
                        .where(Outbox.id == row.id)
                        .values(attempts=Outbox.attempts + 1)
                    )
                    failure = exc
                    break
                await session.execute(
                    update(Outbox).where(Outbox.id == row.id).values(published_at=datetime.now(UTC))
                )
                published += 1
        if failure is not None:
            raise failure
        return published

    async def run_forever(self) -> None:
        backoff = self._interval
        while not self._stopping.is_set():
            try:
                n = await self.run_once()
                backoff = self._interval
                if n == self._batch:
                    continue  # backlog: go again immediately
            except Exception as exc:
                log.warning("outbox_publish_failed", error=type(exc).__name__, retry_in_s=backoff)
                backoff = min(backoff * 2, self._max_backoff)
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=backoff)

    def stop(self) -> None:
        self._stopping.set()
