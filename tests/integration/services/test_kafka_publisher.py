"""KafkaPublisher against a real broker. Runs only when AEOI_TEST_KAFKA=1 (CI starts Kafka).

Locally: make up PROFILE=kafka && AEOI_TEST_KAFKA=1 make test-integration
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

from aeoi_incident.events import KafkaPublisher, OutgoingEvent

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("AEOI_TEST_KAFKA") != "1", reason="needs Kafka (AEOI_TEST_KAFKA=1)"
    ),
]
BOOTSTRAP = os.environ.get("AEOI_KAFKA_BOOTSTRAP_SERVERS", "localhost:9094")


async def test_publish_then_consume_with_key_and_headers() -> None:
    from aiokafka import AIOKafkaConsumer

    incident_id = uuid.uuid4()
    event = OutgoingEvent(
        event_id=uuid.uuid4(),
        topic="incident.lifecycle",
        key=str(incident_id).encode(),
        event_type="IncidentCreated",
        value={"probe": str(incident_id)},
        headers={"correlation_id": "kafka-test-0001"},
    )
    pub = KafkaPublisher(BOOTSTRAP)
    await pub.start()
    try:
        await pub.publish(event)
    finally:
        await pub.stop()

    consumer = AIOKafkaConsumer(
        "incident.lifecycle",
        bootstrap_servers=BOOTSTRAP,
        auto_offset_reset="earliest",
        group_id=f"test-{uuid.uuid4()}",
        enable_auto_commit=False,
    )
    await consumer.start()
    try:
        found = None
        async for msg in consumer:
            if msg.key == event.key:
                found = msg
                break
    finally:
        await consumer.stop()
    assert found is not None
    assert json.loads(found.value) == {"probe": str(incident_id)}
    headers = {k: v.decode() for k, v in found.headers}
    assert headers["event_type"] == "IncidentCreated"
    assert headers["correlation_id"] == "kafka-test-0001"
