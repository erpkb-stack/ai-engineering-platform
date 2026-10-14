"""Phase 18a (ADR-029): incident events reach the audit trail through the broker - once.

Real Postgres (incident-service + audit logins), the REAL outbox relay and wire format
(`kafka_record`), the in-memory broker between them (the sandbox has no Kafka; the Kafka
transport itself is proven by tests/integration/messaging with AEOI_TEST_KAFKA=1).
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import exc as sa_exc
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aeoi_audit.config import Settings as AuditSettings
from aeoi_audit.consumer import classify, handler
from aeoi_audit.main import build_app as build_audit
from aeoi_db.config import database_url, libpq_dsn
from aeoi_db.users import ensure_login
from aeoi_incident.events import OutboxRelay, OutgoingEvent, kafka_record
from aeoi_messaging import Consumer, Kind, MemoryBroker, MemoryTransport
from aeoi_messaging.dlq import MemoryIO, list_dead_letters
from aeoi_security.testing import KeyPair, token_for

pytestmark = pytest.mark.integration
TOPIC = "incident.lifecycle"


class BrokerPublisher:
    """incident-service's Publisher, writing the REAL wire format into the in-memory broker."""

    def __init__(self, broker: MemoryBroker) -> None:
        self.broker = broker

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def publish(self, event: OutgoingEvent) -> None:
        key, value, headers = kafka_record(event)
        self.broker.produce(event.topic, key, value, tuple(headers))


@pytest.fixture(scope="session")
def audit_url(migrated_db: str) -> str:
    password = secrets.token_hex(16)
    with psycopg.connect(libpq_dsn(database=migrated_db), autocommit=True) as conn:
        ensure_login(conn, "audit_svc_t18", "svc_audit", password)
    return database_url(database=migrated_db).set(username="audit_svc_t18", password=password
                                                  ).render_as_string(hide_password=False)  # fmt: skip


@pytest.fixture
def broker() -> MemoryBroker:
    return MemoryBroker()


@pytest.fixture
async def audit(audit_url: str, pubfile: Path, broker: MemoryBroker) -> AsyncIterator[FastAPI]:
    app = build_audit(AuditSettings(db_url_override=SecretStr(audit_url), jwt_public_key_file=pubfile,
                                    environment="test", log_json=False),  # type: ignore[call-arg]
                      transport=MemoryTransport(broker, "audit", [TOPIC]))  # fmt: skip
    await app.state.consumer.transport.start()
    yield app
    await app.state.engine.dispose()


def rows(migrated_db: str, incident_id: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(libpq_dsn(database=migrated_db)) as c:
        return c.execute("SELECT id, action, actor, correlation_id, details FROM audit.audit_events "
                         "WHERE incident_id = %s ORDER BY occurred_at", (incident_id,)).fetchall()  # fmt: skip


async def create_incident(incident_app: FastAPI, keys: KeyPair) -> dict[str, Any]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=incident_app), base_url="http://i"
    ) as c:
        r = await c.post("/v1/incidents", json={
            "title": "Checkout 5xx", "severity": "SEV2", "affected_services": ["checkout-api"],
            "detected_at": datetime.now(UTC).isoformat()},
            headers={"Authorization": f"Bearer {token_for(keys, 'SRE')}",
                     "Idempotency-Key": secrets.token_hex(8), "X-Correlation-ID": "corr-18a-test"})  # fmt: skip
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


async def test_an_incident_event_becomes_exactly_one_audit_row(
    incident_app: FastAPI, audit: FastAPI, broker: MemoryBroker, keys: KeyPair, migrated_db: str
) -> None:
    inc = await create_incident(incident_app, keys)
    relay = OutboxRelay(incident_app.state.sessionmaker, BrokerPublisher(broker))
    assert await relay.run_once() >= 1  # the real relay, the real wire format
    consumer: Consumer = audit.state.consumer
    assert await consumer.run_once() >= 1
    [(event_id, action, actor, corr, details)] = rows(migrated_db, inc["id"])
    assert action == "event.IncidentCreated" and actor.startswith("user:")
    assert corr == "corr-18a-test"  # one request followed from the api to the audit trail
    assert details["_ingested_by"] == "kafka:incident.lifecycle"
    assert (details["payload"]["key"], details["payload"]["title"]) == (inc["key"], "Checkout 5xx")

    # the same event again (a relay retry, a replay, a duplicate): still ONE row
    [original] = [
        m for m in broker.messages(TOPIC) if json.loads(m.value)["event_id"] == str(event_id)
    ]
    broker.produce(TOPIC, original.key, original.value, original.headers)
    restarted = Consumer("audit", MemoryTransport(broker, "audit", [TOPIC]),
                         handler(audit.state.sessionmaker), classify=classify)  # fmt: skip
    await restarted.transport.start()
    assert await restarted.run_once() >= 1
    assert len(rows(migrated_db, inc["id"])) == 1
    # ... and the duplicate is NOT a failure: a DLQ full of false alarms hides the real ones
    assert broker.messages(f"{TOPIC}.dlq") == []


@pytest.mark.parametrize(("value", "why"), [
    (b"not json", "not JSON"),
    (json.dumps({"event_id": "x"}).encode(), "bad id or time"),
    (json.dumps({"event_id": str(uuid4()), "occurred_at": "2026-10-13T01:00:00",
                 "event_type": "IncidentCreated", "actor": "user:1"}).encode(), "time zone"),
    (json.dumps({"event_id": str(uuid4()), "occurred_at": "2026-10-13T01:00:00+00:00",
                 "event_type": "IncidentCreated", "actor": "root"}).encode(), "actor must be"),
    # exactly what scripts/smoke-phase18a.sh sends as its poison: a valid id, no time
    (json.dumps({"event_id": str(uuid4()), "smoke": "phase 18a"}).encode(), "bad id or time"),
    # a far-future time would land in the default partition and block partition maintenance
    (json.dumps({"event_id": str(uuid4()), "occurred_at": "2099-01-01T00:00:00+00:00",
                 "event_type": "IncidentCreated", "actor": "user:1"}).encode(), "in the future"),
    # passes OUR checks, refused by Postgres (jsonb cannot hold \\u0000): the DB decides -> poison
    (json.dumps({"event_id": str(uuid4()), "occurred_at": "2026-10-13T01:00:00+00:00",
                 "event_type": "IncidentCreated", "actor": "user:1",
                 "payload": {"title": "a\u0000b"}}).encode(), "DataError"),
])  # fmt: skip
async def test_bad_messages_go_to_the_dlq_and_the_next_good_one_is_still_audited(
    audit: FastAPI, broker: MemoryBroker, migrated_db: str, value: bytes, why: str
) -> None:
    incident_id = str(uuid4())
    good = {"event_id": str(uuid4()), "event_type": "IncidentUpdated", "occurred_at": datetime.now(UTC).isoformat(),
            "actor": "service:incident-service", "incident_id": incident_id, "payload": {}}  # fmt: skip
    broker.produce(TOPIC, incident_id.encode(), value)
    broker.produce(TOPIC, incident_id.encode(), json.dumps(good).encode())
    consumer: Consumer = audit.state.consumer
    assert await consumer.run_once() == 2
    [d] = await list_dead_letters(MemoryIO(broker), f"{TOPIC}.dlq")
    assert d.kind == "poison" and why in d.error and d.consumer == "audit"
    assert [r[1] for r in rows(migrated_db, incident_id)] == ["event.IncidentUpdated"]


async def test_a_database_outage_is_waited_out_not_dead_lettered(broker: MemoryBroker) -> None:
    dead = create_async_engine("postgresql+psycopg://nobody:x@127.0.0.1:1/none", pool_timeout=1)
    sessions = async_sessionmaker(dead)
    good = {"event_id": str(uuid4()), "event_type": "IncidentCreated", "occurred_at": datetime.now(UTC).isoformat(),
            "actor": "user:1", "payload": {}}  # fmt: skip
    broker.produce(TOPIC, b"k", json.dumps(good).encode())
    kinds: list[Kind] = []

    def spy(exc: BaseException) -> Kind:
        kinds.append(classify(exc))
        return kinds[-1]

    c = Consumer(
        "audit", MemoryTransport(broker, "audit", [TOPIC]), handler(sessions), classify=spy
    )
    waits = {"n": 0}

    async def stop_after(_s: float) -> None:
        waits["n"] += 1
        if waits["n"] >= 4:
            c.stop()

    c.sleep = stop_after
    await c.transport.start()
    assert await c.run_once() == 0
    assert set(kinds) == {Kind.TRANSIENT} and len(kinds) >= 4  # > max_attempts, still no DLQ
    assert broker.messages(f"{TOPIC}.dlq") == []
    assert sum((await c.transport.lag()).values()) == 1  # owed, not lost
    await dead.dispose()


async def test_a_deployment_error_is_waited_out_not_dead_lettered(
    broker: MemoryBroker, migrated_db: str
) -> None:
    """Review H1: a login without the audit grant gets a REAL 'permission denied'
    (ProgrammingError). Every message would fail the same way: dead-lettering would move the
    whole topic into the DLQ. The consumer must wait (lag grows, readiness says so)."""
    password = secrets.token_hex(16)
    with psycopg.connect(libpq_dsn(database=migrated_db), autocommit=True) as conn:
        ensure_login(conn, "no_audit_t18", "svc_incident", password)  # a real login, wrong grants
    url = database_url(database=migrated_db).set(username="no_audit_t18", password=password)
    engine = create_async_engine(url)
    good = {"event_id": str(uuid4()), "event_type": "IncidentCreated", "occurred_at": datetime.now(UTC).isoformat(),
            "actor": "user:1", "payload": {}}  # fmt: skip
    broker.produce(TOPIC, b"k", json.dumps(good).encode())
    seen: list[tuple[str, Kind]] = []

    def spy(exc: BaseException) -> Kind:
        seen.append((type(exc).__name__, classify(exc)))
        return seen[-1][1]

    c = Consumer("audit", MemoryTransport(broker, "audit", [TOPIC]), handler(async_sessionmaker(engine)),
                 classify=spy)  # fmt: skip
    waits = {"n": 0}

    async def stop_after(_s: float) -> None:
        waits["n"] += 1
        if waits["n"] >= 4:
            c.stop()

    c.sleep = stop_after
    await c.transport.start()
    assert await c.run_once() == 0
    assert seen and all(k is Kind.TRANSIENT for _, k in seen), seen
    assert seen[0][0] == "ProgrammingError"  # the real error, not a stand-in
    assert broker.messages(f"{TOPIC}.dlq") == []
    await engine.dispose()


@pytest.mark.parametrize(("exc", "kind"), [
    (sa_exc.TimeoutError("QueuePool limit reached"), Kind.TRANSIENT),
    (TimeoutError(), Kind.TRANSIENT),
    (sa_exc.InternalError("x", {}, Exception("read-only transaction")), Kind.TRANSIENT),
    (sa_exc.IntegrityError("x", {}, Exception("check violated")), Kind.POISON),
    (sa_exc.DataError("x", {}, Exception("bad value")), Kind.POISON),
])  # fmt: skip
def test_classify(exc: BaseException, kind: Kind) -> None:
    assert classify(exc) is kind


async def test_readiness_follows_the_consumer_not_only_its_task(
    audit: FastAPI, broker: MemoryBroker
) -> None:
    """Review M4: alive but waiting (Kafka or the DB down, wrong grants) is NOT ready."""
    consumer: Consumer = audit.state.consumer
    loop = asyncio.get_running_loop()

    async def ready() -> tuple[int, str]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=audit), base_url="http://a"
        ) as c:
            r = await c.get("/health/ready")
        return r.status_code, r.json()["checks"]["consumer"]

    assert await ready() == (503, "fail: ConsumerStoppedError")  # no task: never started
    audit.state.consumer_task = loop.create_future()  # a live task
    for state, expected in [("starting", (503, "fail: ConsumerDegradedError")),
                            ("degraded", (503, "fail: ConsumerDegradedError")),
                            ("running", (200, "ok"))]:  # fmt: skip
        consumer.status.state = state
        assert await ready() == expected, state
    broker.reachable = False  # running, but the broker is gone (aiokafka hides it in polls)
    assert await ready() == (503, "fail: ConsumerDegradedError")
    broker.reachable = True
    assert await ready() == (200, "ok")
    audit.state.consumer_task.set_result(None)  # the task ended
    assert await ready() == (503, "fail: ConsumerStoppedError")
