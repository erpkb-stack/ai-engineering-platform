"""Kafka consumers for AEOI (ADR-029): process once, poison -> DLQ, dependency down -> wait."""

from aeoi_messaging.consumer import (
    Consumer,
    Kind,
    PoisonError,
    Status,
    TransientError,
    default_classify,
    dlq_topic,
)
from aeoi_messaging.transport import (
    KafkaTransport,
    MemoryBroker,
    MemoryTransport,
    Message,
    Transport,
)

__all__ = [
    "Consumer",
    "KafkaTransport",
    "Kind",
    "MemoryBroker",
    "MemoryTransport",
    "Message",
    "PoisonError",
    "Status",
    "TransientError",
    "Transport",
    "default_classify",
    "dlq_topic",
]
