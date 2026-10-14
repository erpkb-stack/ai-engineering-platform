# Phase 18 — Kafka / distributed (18a: consumer core + audit · 18b: agent tasks)

## 0. Gate and scope challenge
- **Gate: passed.** Phase 17 verified on the Mac and merged (PR #19, main 3dc4135). 17b was
  confirmed by the owner; the last four outputs were not pasted.
- **Challenge - Kafka without a consumer is overhead.** Since Phase 4 the outbox published to
  Kafka and nothing read it. Owner decision (2026-10-13): consumer core + audit consumer +
  agent tasks over Kafka; broker = Kafka (KRaft) as in compose.
- **Split.** 18a (consumer core, audit) is verified on the Mac before 18b replaces the WORKING
  orchestrator -> agents HTTP path. 18b's cost is shown to the owner before it is written.
- **Provenance note.** An unreviewed 18a draft (`libs/messaging`, the audit consumer, two
  integration tests) appeared in the build sandbox on 2026-10-12 23:18-23:22 with no record of
  who wrote it; it never reached the Mac. It was adopted only after: a line-by-line read, a diff
  of every changed file against main, lint/types/tests, mutation checks (two gaps found and
  fixed), and the independent review below.

## 18a. Consumer core and the audit consumer
### 1-2. Objective and reason
Every incident event reaches the audit trail through Kafka exactly once in effect (at-least-once
delivery + idempotent write); one bad message never blocks the rest; an outage never fills the
DLQ; a dead letter can be replayed after a fix, but not forever.

### 3. Architecture (ADR-029)
```
incident-service ─tx─► outbox ─relay─► incident.lifecycle ─► audit Consumer ─► audit.audit_events
                                              │ poison / 3 unknown failures       (id = event_id,
                                              ▼                                     ON CONFLICT)
                                    incident.lifecycle.dlq ─► make kafka-replay (max 3)
```
| Guarantee | Enforced by | Test |
|---|---|---|
| Offset committed only after the work | `Consumer._handle` | unit (crash before commit -> redelivered); Kafka (uncommitted -> again) |
| A duplicate adds nothing and is not a failure | audit `ON CONFLICT DO NOTHING` on (event_id, occurred_at) - holds because the outbox never re-stamps a time | integration (1 row, empty DLQ) - mutation-checked |
| Poison -> DLQ at once, next message handled | `PoisonError`, `classify` | unit + integration (5 kinds of bad message) |
| Outage -> wait, never DLQ, not committed | `TransientError`, DB classify (incl. pool timeout, missing grant/migration, read-only) | unit + integration (DB down; a REAL "permission denied") |
| A future time is poison (protects partition maintenance) | audit `to_row` (> now + 1 day) | integration |
| Unknown error -> 3 tries -> DLQ | `max_attempts` | unit |
| DLQ down -> message kept, consumer `degraded` | DLQ write failure = transient | unit |
| A message too big for the DLQ never blocks | pointer (size, sha256, offset, event id) above 900 KB | unit |
| Replay bounded (3) and counted - also via `OFFSET=`, also without an event id | `dlq.replay` (max count per identity over the whole DLQ) | unit + Kafka |
| Not consuming -> not ready (dead, joining, waiting, broker silent) | readiness = state `running` + broker probe | integration (every state) + unit (probe vs no broker) |
| Lag counts only readable messages; unknown topic is an error | `lag_of`, CLI exit 2 | unit + Kafka |
| One broker address everywhere | `AEOI_KAFKA_BOOTSTRAP_SERVERS` alias | unit (mutation-checked) |
| The smoke's CLI parsing is pinned | CLI output test | unit |

### 4. Files
```
libs/messaging (new): consumer.py, transport.py (Kafka + in-memory), dlq.py, __main__.py (CLI), tests
services/audit: consumer.py (new), main.py, config.py, pyproject.toml
services/incident-service/events.py (kafka_record), config.py (shared broker address)
libs/common/settings.py (AEOI_KAFKA_BOOTSTRAP_SERVERS alias) + test
scripts: smoke-phase18a.sh, phase18_audit_rows.py (new)
tests/integration/services: test_audit_consumer.py, test_kafka_consumer.py (AEOI_TEST_KAFKA=1)
Makefile (kafka-lag, kafka-dlq, kafka-replay, consumer-smoke), .env.example, pyproject.toml, uv.lock
docs: ADR-029 (new), architecture.md §17 retries row, roadmap, CLAUDE.md files, this file
```

### 5-6. Commands (macOS, zsh) - see the hand-over; summary:
```zsh
make up PROFILE=kafka                     # Kafka + topics (kafka-init); ~1 GiB
# .env: AEOI_INCIDENT_EVENT_TRANSPORT=kafka and AEOI_AUDIT_CONSUMER_ENABLED=true; restart dev + run-audit
make consumer-smoke
AEOI_TEST_KAFKA=1 make test-integration   # includes the real-broker tests
make kafka-lag · make kafka-dlq · make kafka-replay OFFSET=n DRY=1
```

### 8. Tests (sandbox: real Postgres, in-memory broker; NO Kafka - downloads are blocked)
- Unit: libs/messaging 32 (13 + 19 review regressions); settings +1. `make check` in the
  sandbox: 852 passed, 1 skipped (Keycloak); ruff, format, mypy clean.
- Full integration in the sandbox: 260 passed, 7 skipped (Keycloak; 6 need Kafka).
- Integration: audit consumer 16; Kafka tests 5 skipped here (run on the Mac).
- Mutations: consumer 7/8 (the 8th is equivalent: it only changes a backoff), DLQ/replay 3/3,
  audit 5/5 after two test fixes (a duplicate reaching the DLQ was not caught; the actor check
  was shadowed by a database constraint), broker alias 1/1. Review fixes: 3/3 audit, 9/9
  messaging, 1/1 readiness, 1/1 header size; re-review fixes 8/8 + probe 1/1. Every guard
  added in the reviews makes a test fail when removed.
- **Not run here:** anything against a real Kafka broker (incl. the probe on a live broker and
  `lag` with `beginning_offsets`), the smoke.

### 9. Reviews (independent subagent, then a re-review of the fixes)
| Id | Finding | Fix |
|---|---|---|
| H1 | pool timeout / missing grant / read-only DB -> 3 tries -> whole backlog into the DLQ | transient in audit `classify`; real-DB test |
| H2 | a bad `dlq_replay_count` header made the DLQ write fail for ever | parsed safely (also `dlq_attempts`) |
| H3 | a ~1 MB message could never be dead-lettered -> partition stuck | pointer above 900 KB (value + headers) |
| M1 | a far-future `occurred_at` stops audit partition maintenance | poison above now + 1 day (no lower bound: an old row blocks nothing) |
| M2 | "never a second row" was claimed; the key is (event_id, occurred_at) | claim corrected (ADR, docstring); verified the outbox writes the time once |
| M3 | `replay --offset <old entry>` bypassed the limit | count = max over the whole DLQ per identity |
| M4 | a start failure killed the consumer; readiness looked only at the task | start retried; readiness = state + broker probe |
| M5/M6 | smoke checks matched earlier runs' dead letters; the duplicate check was racy | unique poison id; wait for lag 0; readiness must be `ok` |
| L1 | lag ignored retention; unknown topic = "lag 0" | `lag_of`; exit 2 |
| L2 | `sum(lag) == 0` passes for no partitions | assert 3 partitions |
| L3 | keyless poison replayed every generation (~2^k copies) | identity = sha256 of the value |
| L4 | stop not prompt; SQL parameters in error text | per-message stop check; driver's first line only |
| L5 | the CLI "dev only" guard is not a control; no Kafka ACLs locally | documented (ADR-029 tradeoffs) |
| re-M-1 | **my M4 fix was wrong**: an empty poll set "running", but aiokafka returns empty polls while the broker is DOWN | `degraded` ends only with a commit or a successful probe |
| re-M-2 | a failing DLQ write never set `degraded` | set |
| re-M-3 | the smoke ignored a failed `redeliver` | checked |
| re-L-1..6 | pointer lost the event id; refusal printed the wrong count; small headers could still overflow; `lag` could hang; smoke checked readiness once; `dlq_attempts` unsafe | all fixed, each with a test |
| Mac-1 | **lag, dlq, replay saw nothing on a real broker**: aiokafka's metadata cache holds no topics for a consumer without a subscription, and `topics()` does not refresh it. Before the L1 fix, `lag` printed "0" for every topic | `topic_partitions()` reads a fresh metadata request; a missing topic is an error (exit 2); Kafka test pins the trap |
| Mac-2 | the smoke PATCHed with an SRE token: 403 (only INCIDENT_COMMANDER has `incidents:write`) - the RBAC was right, the script was wrong | IC token; the body is printed on failure |
| open | the HTTP write path (`POST /v1/events`, Phase 8) does not reject a future time either | not changed in 18a (its only writer is the tool-gateway relay); recorded in ADR-029 |
| open | in-memory broker never fails a commit or rebalances; the crash-before-commit test uses `SystemExit` | known test gap; the Kafka tests cover redelivery on a real broker |

### 10. Verify (gate for 18b) - passed on the Mac, 2026-10-14 (after fix1 + fix2)
- [x] `make check`: 852 passed, 1 skipped
- [x] `AEOI_TEST_KAFKA=1 make test-integration`: 267 passed, 1 skipped (Keycloak) - all 6 Kafka
  consumer tests and the publisher test ran on the real broker
- [x] `make consumer-smoke`: 12/12 (INC-10070)
- [x] regressions: `make approval-smoke` 23/23, `make kc-smoke` 21/21
- Lesson: the sandbox's in-memory broker could not show either Mac finding. Both were found
  only because the gate requires the real broker and the real api.

### 11. Failure scenarios
| Symptom | Cause | Fix |
|---|---|---|
| audit `/health/ready` 503 `consumer: fail: ConsumerDegradedError` | Kafka not running / wrong address, DB down, or a missing grant (the consumer waits) | `make up PROFILE=kafka`; `AEOI_KAFKA_BOOTSTRAP_SERVERS`; `make run-audit` logs (`dependency_down`) |
| audit `/health/ready` 503 `ConsumerStoppedError` | consumer not enabled at start, or its task died | restart `make run-audit`; read its logs |
| `nothing matched` on replay (exit 1) | wrong EVENT= / OFFSET= | `make kafka-dlq` and copy the id |
| `REFUSED ... too large` | the original was > 900 KB: the DLQ holds a pointer | replay from the source topic offset in its `dlq_source_*` headers |
| smoke: no audit row via Kafka | incident-service still on the log transport | `AEOI_INCIDENT_EVENT_TRANSPORT=kafka`, restart `make dev` |
| lag grows, DLQ empty | a dependency (DB) is down: the consumer waits | fix the dependency; nothing to replay |
| `REFUSED ... fix the cause` on replay | replayed 3 times already | fix the consumer or the producer first |
| UnknownTopicOrPartition | topics not created (auto-create is off) | `make kafka-init` |

## 18b. Agent tasks over Kafka - NOT built (owner decision 2026-10-14, ADR-030)
The cost check read the real path: `run_agent` awaits one HTTP call, records the result, and the
checkpointed graph reuses it on resume; transient failures are retried; agents are stateless;
the delegated token lives 5 minutes and only the orchestrator can refresh it.
- **Gain at this scale: none** - no task outlives the 180 s deadline, 2 concurrent
  investigations never wait for a worker, restarts are already survived.
- **Cost:** the graph must end and resume per task (engine rewrite); a user's bearer token would
  sit in a Kafka log for 7 days (or agents become token-refreshing actors, widening ADR-019);
  at-least-once means paying twice for duplicate LLM work, so stateless agents would need a
  store; it rewrites the Phase 9-16 path.
- **Revisit when** (ADR-030): load tests show agents saturated, a task needs minutes, agents
  must scale on backlog, or a second consumer needs results live.
