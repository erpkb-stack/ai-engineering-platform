"""Phase 30 (architecture review): Kafka-sourced audit rows were ALL stored as SUCCESS, so an
approved action that ran and FAILED was invisible to `GET /v1/events?outcome=FAILURE`."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from aeoi_audit.consumer import outcome_of, to_row
from aeoi_messaging import Message

pytestmark = pytest.mark.unit


def _msg(event_type: str, payload: dict) -> Message:
    doc = {"event_id": str(uuid4()), "incident_id": str(uuid4()), "event_type": event_type,
           "occurred_at": datetime.now(UTC).isoformat(), "actor": "user:1", "payload": payload,
           "schema_version": 1}  # fmt: skip
    return Message(topic="incident.lifecycle", partition=0, offset=7, key=b"k",
                   value=json.dumps(doc).encode(), headers=())  # fmt: skip


@pytest.mark.parametrize(("event_type", "payload", "outcome"), [
    ("ActionExecuted", {"status": "FAILED"}, "FAILURE"),
    ("ActionExecuted", {"status": "EXECUTED"}, "SUCCESS"),
    ("HumanRejected", {"status": "REJECTED"}, "SUCCESS"),  # a decision that happened
    ("ActionExpired", {"status": "EXPIRED"}, "SUCCESS"),
    ("IncidentCreated", {}, "SUCCESS"),
    ("SomethingNew", {"status": "FAILED"}, "SUCCESS"),  # only a known failure is called one
])  # fmt: skip
def test_the_row_outcome_follows_the_event(event_type: str, payload: dict, outcome: str) -> None:
    assert outcome_of(event_type, payload) == outcome
    assert to_row(_msg(event_type, payload))["outcome"] == outcome
