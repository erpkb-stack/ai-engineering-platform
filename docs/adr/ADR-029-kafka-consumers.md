# ADR-029: Kafka consumers - process once, poison to a DLQ, wait out outages, bounded replay

Status: Proposed
Date: 2026-10-13
Refines: ADR-003 (Kafka events, outbox), architecture.md §17 (topics, reliability patterns)

## Context
Since Phase 4 the transactional outbox publishes incident events (`incident.lifecycle`), to the
log by default or to Kafka with `make up PROFILE=kafka` - and nothing consumed them. Owner
decision (2026-10-13): Phase 18 builds the consumer core, the audit service as its first
consumer (18a), and agent tasks over Kafka (18b, separate checkpoint; later NOT built, see ADR-030). Broker: Kafka (KRaft) as
in compose.

What a consumer has to get right, and what goes wrong otherwise:
1. Delivery is at-least-once (the relay can publish twice; a consumer can crash after the work
   and before the commit). Without idempotent handlers, duplicates double-count.
2. One malformed message must not block every message behind it on its partition.
3. A database outage is not the message's fault. Sending messages to a DLQ during an outage
   fills it with good events that then need a manual replay.
4. A replay that fails again, replayed again, forever, is a loop, not a fix.

## Decision
`libs/messaging` (`aeoi_messaging`) - one consumer every service uses:
```
poll (partition order) -> handler(msg) -> commit offset   (only after the handler's tx committed)
                            | PoisonError           -> DLQ at once, commit, next
                            | TransientError / DB down -> back off and retry, NOT committed,
                            |                          never to the DLQ (lag grows, readiness says)
                            | anything else         -> 3 tries (exp. backoff, full jitter) -> DLQ
                            | DLQ write fails       -> treated as transient (never dropped)
```
- **Offsets are committed by us, after the work** (`enable_auto_commit=False`). A crash between
  work and commit redelivers; handlers dedupe on `event_id` (audit: `ON CONFLICT DO NOTHING` on
  the primary key `(id = event_id, occurred_at)`). A duplicate is not an error: it must not
  reach the DLQ (tested). The audit key includes the time (partitioned table): it dedupes only
  because the outbox writes the envelope ONCE and replay resends the original bytes. The same
  id with another time (a re-serialised or forged envelope) would be a second row (review M2).
- **Classification belongs to the consumer** (`classify`): for audit, an `IntegrityError` or
  `DataError` is poison (THIS row can never be written). Transient: a lost connection, a pool
  or statement timeout, and `ProgrammingError` / `InternalError` (missing grant or migration,
  read-only replica) - those fail EVERY message the same way, so dead-lettering them would empty
  the topic into the DLQ (review H1).
- **Audit rejects** an `occurred_at` more than a day in the future (poison): such a row lands in
  the default partition and later stops `audit.ensure_partitions()` for everyone (review M1).
  There is no lower bound: an old row blocks nothing, and a bound could turn a late replay into
  poison.
- **DLQ record** = the original key, value and headers + `dlq_*` headers (consumer, kind, error,
  attempts, source topic/partition/offset, time, replay count). `make kafka-dlq` lists them.
  Error text is the driver's first line only - SQLAlchemy errors embed the bound parameters
  (event payloads). An original above 900 KB (value + headers) cannot fit next to the headers
  under Kafka's ~1 MB limit: the DLQ keeps a POINTER (size, sha256, source offset;
  `dlq_truncated=1`, plus the event id so `EVENT=` still finds it) and replay refuses it
  (review H3); if many small headers still overflow, all original headers are dropped. A bad `dlq_replay_count` header reads
  as 0, never as an error (review H2).
- **Replay** (`make kafka-replay`) sends originals back to the source topic, with the replay
  count incremented; after `MAX_REPLAYS` = 3 it refuses: "fix the cause, not the queue". A
  message's count is the highest over ALL its DLQ entries, also when an old entry is picked by
  `OFFSET=` (review M3); its identity is the event id, else the sha256 of its bytes (review L3:
  by DLQ offset, every generation was replayed again). A filter matching nothing exits 1. Every consumer group on that topic sees a
  replayed message again - harmless, since all dedupe.
- **Lag**: `make kafka-lag` = end - max(committed, beginning) per partition (messages deleted
  by retention are not lag; an unknown topic exits 2, never "lag 0") (review L1).
- **Readiness**: `/health/ready` is 503 while the consumer is dead, still joining (start is
  retried with backoff - Kafka may come up after the service), waiting on a dependency or on
  the DLQ (`degraded`), or the broker does not answer a metadata request within 1.5 s
  (`probe()`). The probe is needed because aiokafka HIDES an outage: a poll returns nothing
  and a commit retries for ever, so "no error" never means "Kafka is there". For the same
  reason `degraded` ends only with a commit or a successful probe, never with an empty poll.
  Liveness ignores all of it: an outage must not restart pods. Cost: one metadata request per
  readiness check.
- **Audit consumer** (`AEOI_AUDIT_CONSUMER_ENABLED=true`): every `incident.lifecycle` event
  becomes ONE audit row (`event.<EventType>`, actor from the envelope, correlation id,
  `_ingested_by: kafka:<topic>`, `_source: topic[partition]@offset`). Unknown event types are
  recorded, not dropped. An envelope that fails validation is poison, never guessed.
- **One wire format**: `kafka_record()` in incident-service builds key/value/headers; the
  publisher and the tests use the same function.
- **One broker address** for every service and the CLI: `AEOI_KAFKA_BOOTSTRAP_SERVERS` (before,
  a service with its own settings prefix silently ignored the shared `.env` value).

## Deviation from architecture.md §17
The architecture planned "retry 3x -> **retry topic** -> DLQ". Not built: in-consumer retry
with backoff covers transient failures, and a retry topic reorders a partition (a retried event
lands after newer ones) and adds a topic per consumer group. Revisit if a consumer's retries
must not block its partition (would matter for long agent tasks, which stay on HTTP: ADR-030).

## Alternatives
| Option | Why not (here) |
|---|---|
| Auto-commit | commits before the work is done: a crash loses messages |
| DLQ after N tries for every error | an outage moves good events to the DLQ |
| Exactly-once (Kafka transactions end to end) | the side effect is a Postgres write, not a Kafka write; idempotent handlers are simpler and cover redelivery and replay alike |
| Keep audit on HTTP calls | no replay, no lag, and the producer must know every consumer |

## Tradeoffs
- **Order is per partition, not absolute:** a dead-lettered event is skipped, later events for
  the same incident go on. Right for audit (append-only, each row has its own time); a task queue would have to
  decide again for agent tasks.
- **An outage blocks the partition** (by design: nothing is skipped). Past
  `max_poll_interval_ms` (10 min) Kafka moves the partition to another member; nothing is lost
  (the offset was never committed), the work is redelivered.
- **Who can write audit rows = who can produce to the topic.** [Prod] Kafka ACLs (mTLS
  identities) let only incident-service produce to `incident.lifecycle`. Locally the broker is
  plaintext without ACLs: any local process can write an audit row with any `actor`. The CLI's
  "dev only" refusal of `produce-raw`/`redeliver` in production is a convenience, NOT a control
  (review L5); the ACLs are not built or tested in this phase.
- **Readiness takes the read API out of rotation** while the consumer waits (e.g. Kafka down,
  DB fine). Accepted: until lag is a metric, readiness is the only signal that audit is behind.
- **H1's cost:** a message-caused error that the driver reports as `ProgrammingError` would
  block its partition (wait, not DLQ) instead of being dead-lettered. None is known for this
  fixed INSERT with bound parameters; readiness and lag make it visible.
- **Lag is a CLI and a readiness signal, not yet a Prometheus metric** (observability phase).
- **Verified where:** consumer logic, DLQ and replay with an in-memory broker of the same
  semantics (per-key partitions, group offsets, redelivery) and the real Postgres + relay + wire
  format; the Kafka transport itself only with a real broker (`AEOI_TEST_KAFKA=1`, the Mac -
  the sandbox cannot download Kafka). The real broker found what the imitation could not:
  aiokafka's metadata cache is empty for a consumer without a subscription, so partitions
  must come from a fresh metadata request (`topic_partitions`).
- The HTTP write path (`POST /v1/events`, Phase 8) does not yet reject a future
  `occurred_at` (M1 applies there too); its only writer is the tool-gateway's own relay.
- The tool-gateway's audit records still go over HTTP (its own outbox): moving them is a
  separate change with nothing to gain until a second consumer needs them.

## Prototype vs Production vs Enterprise-scale
[P] one broker, one audit consumer, CLI replay. [Prod] 3 brokers, RF 3, `min.insync.replicas=2`,
ACLs, lag metrics and alerts per group, DLQ alert on any new record. [Ent] schema registry with
compatibility rules instead of envelope checks; tiered storage for long replay windows.

## When we would revisit
When a consumer must not block its partition while retrying (retry topic), or when more than one
team produces to the same topic (schema registry).
