"""Audit consumes the domain events (Phase 18a, ADR-029).

Every event on `incident.lifecycle` becomes ONE audit row:
  id = event_id, occurred_at = the event's time  -> the insert is idempotent (ON CONFLICT DO
  NOTHING on the primary key): a redelivery, a replay or a duplicate adds no second row.
  The key is (event_id, occurred_at), NOT event_id alone (the table is partitioned by time and
  Postgres needs the partition key in the PK). So the guarantee holds only while the producer
  never re-stamps a time: true for the outbox (the time is written once, in the business
  transaction) and for replay (it resends the original bytes). The same id with another time
  - a re-serialised or forged envelope - WOULD be a second row (review M2).
  action = "event.<EventType>", actor from the envelope, details = the payload + where it came
  from (`_ingested_by: kafka:<topic>`, `_source: topic[partition]@offset`).
Unknown event types are recorded too: an audit log that drops what it does not recognise is not
an audit log. A message that is not a valid envelope is POISON (to the DLQ, never guessed).
A database outage is TRANSIENT: the consumer waits; nothing goes to the DLQ.

Who may write here: anyone who can produce to the topic. [Prod] Kafka ACLs let only
incident-service produce to incident.lifecycle (mTLS identities); `_ingested_by` records the
channel, never a claim from the message.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import exc as sa_exc
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aeoi_messaging import Kind, Message, PoisonError, default_classify
from aeoi_messaging.consumer import Handler

ACTOR = re.compile(r"^(user|service|agent):.+$")
_INSERT = text("""
INSERT INTO audit.audit_events (id, occurred_at, actor, actor_role, action, resource_type,
                                resource_id, incident_id, correlation_id, outcome, details)
VALUES (:id, :occurred_at, :actor, NULL, :action, 'incident', :resource_id, :incident_id,
        :correlation_id, :outcome, CAST(:details AS jsonb))
ON CONFLICT (id, occurred_at) DO NOTHING
""")


def _str(doc: dict[str, Any], name: str, max_len: int, *, required: bool = True) -> str | None:
    v = doc.get(name)
    if v is None and not required:
        return None
    if not isinstance(v, str) or not v or len(v) > max_len:
        raise PoisonError(f"{name}: expected a string of 1..{max_len} chars")
    return v


# A time far in the future lands in audit_events_default; when the monthly job later creates
# that month's partition, Postgres refuses (a default-partition row would violate it) and
# partition maintenance stops for EVERYONE (review M1). Clock skew is minutes, not a day.
MAX_FUTURE = timedelta(days=1)


def to_row(msg: Message, *, now: datetime | None = None) -> dict[str, Any]:
    """The audit row for one event - or PoisonError, saying exactly what is wrong."""
    try:
        doc = json.loads(msg.value)
    except (ValueError, UnicodeDecodeError) as exc:
        raise PoisonError("not JSON") from exc
    if not isinstance(doc, dict):
        raise PoisonError("not a JSON object")
    try:
        event_id = UUID(str(doc.get("event_id")))
        incident_id = UUID(str(doc["incident_id"])) if doc.get("incident_id") else None
        occurred_at = datetime.fromisoformat(str(doc.get("occurred_at")))
    except (ValueError, TypeError) as exc:
        raise PoisonError(f"bad id or time: {exc}") from exc
    if occurred_at.tzinfo is None:
        raise PoisonError("occurred_at without a time zone")
    if occurred_at > (now or datetime.now(UTC)) + MAX_FUTURE:
        raise PoisonError("occurred_at more than a day in the future")
    event_type = _str(doc, "event_type", 70)
    actor = _str(doc, "actor", 160)
    if not ACTOR.match(actor or ""):
        raise PoisonError("actor must be user:/service:/agent:<id>")
    payload = doc.get("payload", {})
    if not isinstance(payload, dict):
        raise PoisonError("payload must be an object")
    return {
        "id": event_id, "occurred_at": occurred_at, "actor": actor,
        "action": f"event.{event_type}", "resource_id": str(incident_id) if incident_id else None,
        "incident_id": incident_id,
        "correlation_id": _str(doc, "correlation_id", 80, required=False),
        "outcome": outcome_of(event_type or "", payload),
        "details": json.dumps({
            "event_type": event_type, "schema_version": doc.get("schema_version"),
            "payload": payload, "_ingested_by": f"kafka:{msg.topic}",
            "_source": f"{msg.topic}[{msg.partition}]@{msg.offset}",
        }),
    }  # fmt: skip


def outcome_of(event_type: str, payload: dict[str, Any]) -> str:
    """Phase 30 (architecture review): every Kafka row was stored as SUCCESS, so an action
    that RAN AND FAILED (ActionExecuted with status FAILED) answered an outcome=FAILURE query
    with nothing. A rejection or an expiry is a decision that happened: SUCCESS. DENIED stays
    the tool gateway's word for a refused call; no lifecycle event means that."""
    if event_type == "ActionExecuted" and payload.get("status") == "FAILED":
        return "FAILURE"
    return "SUCCESS"


def handler(sessions: async_sessionmaker[AsyncSession]) -> Handler:
    async def handle(msg: Message) -> None:
        row = to_row(msg)
        async with sessions() as s, s.begin():
            await s.execute(_INSERT, row)

    return handle


def classify(exc: BaseException) -> Kind:
    """The database decides: a broken row is poison, a broken connection is transient."""
    if isinstance(exc, sa_exc.IntegrityError | sa_exc.DataError):
        return Kind.POISON  # e.g. a CHECK the envelope passed but the table does not
    if isinstance(exc, sa_exc.DBAPIError) and exc.connection_invalidated:
        return Kind.TRANSIENT
    if isinstance(exc, sa_exc.TimeoutError | TimeoutError):
        return Kind.TRANSIENT  # pool exhausted / statement timeout: the DB is busy, not the row bad
    if isinstance(exc, sa_exc.OperationalError | sa_exc.InterfaceError | OSError):
        return Kind.TRANSIENT  # server down, connection refused/reset
    if isinstance(exc, sa_exc.ProgrammingError | sa_exc.InternalError):
        # undefined table/column, permission denied, read-only replica: OUR deployment is wrong
        # (a missed migration, a grant). Every message would fail the same way, so dead-lettering
        # would empty the topic into the DLQ. Wait, and let readiness say so (review H1).
        return Kind.TRANSIENT
    return default_classify(exc)
