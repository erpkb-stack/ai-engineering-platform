"""Transactional outbox relay: at-least-once, ordered, no double publish across relays."""

from __future__ import annotations

import asyncio
import uuid

import httpx
import psycopg
import pytest
from fastapi import FastAPI

from aeoi_incident.events import InMemoryPublisher, OutboxRelay
from aeoi_security.testing import KeyPair
from tests.integration.services.conftest import bearer

pytestmark = pytest.mark.integration


async def drain(app: FastAPI) -> None:
    relay = OutboxRelay(app.state.sessionmaker, InMemoryPublisher(), batch_size=1000)
    while await relay.run_once():
        pass


async def new_incidents(client: httpx.AsyncClient, keys: KeyPair, n: int) -> list[str]:
    ids = []
    for _ in range(n):
        r = await client.post(
            "/v1/incidents",
            json={"title": "Outbox relay test incident", "severity": "SEV3"},
            headers=bearer(keys, "SRE") | {"Idempotency-Key": f"ob-{uuid.uuid4()}"},
        )
        ids.append(r.json()["id"])
    return ids


async def test_publishes_in_order_and_marks_published(
    incident_app: FastAPI, client: httpx.AsyncClient, keys: KeyPair, owner_conn: psycopg.Connection
) -> None:
    await drain(incident_app)
    ids = await new_incidents(client, keys, 3)
    pub = InMemoryPublisher()
    relay = OutboxRelay(incident_app.state.sessionmaker, pub)
    assert await relay.run_once() == 3
    assert [e.key.decode() for e in pub.published] == ids  # creation order
    assert all(e.topic == "incident.lifecycle" for e in pub.published)
    assert pub.published[0].headers["correlation_id"] is not None
    assert await relay.run_once() == 0  # nothing left
    left = owner_conn.execute(
        "SELECT count(*) FROM incident.outbox WHERE published_at IS NULL"
    ).fetchone()
    assert left == (0,)


async def test_failure_counts_attempt_and_keeps_order(
    incident_app: FastAPI, client: httpx.AsyncClient, keys: KeyPair, owner_conn: psycopg.Connection
) -> None:
    await drain(incident_app)
    ids = await new_incidents(client, keys, 3)
    pub = InMemoryPublisher(fail_times=1)
    relay = OutboxRelay(incident_app.state.sessionmaker, pub)
    with pytest.raises(ConnectionError):
        await relay.run_once()
    assert pub.published == []  # first event failed -> batch stopped, nothing overtook it
    attempts = owner_conn.execute(
        "SELECT attempts FROM incident.outbox WHERE aggregate_id = %s", (ids[0],)
    ).fetchone()
    assert attempts == (1,)
    assert await relay.run_once() == 3
    assert [e.key.decode() for e in pub.published] == ids


async def test_two_relays_never_publish_the_same_event(
    incident_app: FastAPI, client: httpx.AsyncClient, keys: KeyPair
) -> None:
    await drain(incident_app)
    await new_incidents(client, keys, 20)
    a, b = InMemoryPublisher(), InMemoryPublisher()
    ra = OutboxRelay(incident_app.state.sessionmaker, a, batch_size=5)
    rb = OutboxRelay(incident_app.state.sessionmaker, b, batch_size=5)
    for _ in range(6):
        await asyncio.gather(ra.run_once(), rb.run_once())
    ids_a = [e.event_id for e in a.published]
    ids_b = [e.event_id for e in b.published]
    assert len(ids_a) + len(ids_b) == 20
    assert not set(ids_a) & set(ids_b)  # SKIP LOCKED: no event went to both
