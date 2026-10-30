# ADR-003: Kafka for asynchronous events with outbox and idempotent consumers

Status: Proposed
Date: 2026-10-02
Supersedes: —

## Context
Agent work is slow, bursty and parallel; audit must receive every event; consumers must be able to replay; services must stay decoupled.

## Decision
Kafka (KRaft). At-least-once delivery, transactional outbox for producers, `processed_events` dedupe for consumers, retry topic + DLQ, key = incident_id for ordering.

## Alternatives
| Option | Why not (here) |
|---|---|
| RabbitMQ | Good work queue; weaker replay/log semantics for audit and re-processing. |
| Redis Streams | Light, but durability and ops at scale are weaker; Redis is meant to be disposable here. |
| Direct HTTP | Simple, but couples availability and has no buffering or replay. |

## Tradeoffs
Against: JVM memory on a laptop, operational complexity, and overkill at 10 rps. Mitigation: single KRaft broker with heap cap; in-process bus with the same interface until Phase 18; Redpanda as a drop-in.

## Consequences
Exactly-once is NOT claimed. Every consumer must be idempotent.

Implementation (Phase 4): `incident.outbox` + `OutboxRelay` (FOR UPDATE SKIP LOCKED, ordered, stops a batch at the first failure) + `Publisher` interface with `LogPublisher` (default, no broker needed) and `KafkaPublisher` (aiokafka, `acks=all`, idempotent producer, key = incident id).

## Prototype vs Production vs Enterprise-scale
[P] 1 broker. [Prod] MSK 3 brokers, RF=3, min.insync=2. [Ent] multi-region with MirrorMaker 2/cluster linking.

## When we would revisit
If the only consumers are agent workers and audit moves to a log store, a managed queue (SQS) may be simpler.

## Amendment 2026-10-30 (Phase 30, ADR-048)
As built: ONE topic in use (`incident.lifecycle` -> the audit consumer), ON by default in compose/kind. Consumer dedupe is the audit insert's ON CONFLICT (id, occurred_at) (ADR-029); `processed_events` was never used and was dropped (migration 0030). Agent tasks stay on HTTP (ADR-030); `investigation.*` and `catalog.changes` are no longer created.
