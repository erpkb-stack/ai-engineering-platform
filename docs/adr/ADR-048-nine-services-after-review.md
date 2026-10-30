# ADR-048: Nine services after the Phase 30 review - audit and Kafka kept, two stubs removed

Status: Proposed
Date: 2026-10-30
Supersedes: — (amends ADR-011's "When we would revisit"; amends ADR-003 and ADR-029 on defaults)

## Context
ADR-011 chose 11 services and named the Phase 30 merge candidates: knowledge-service ->
service-catalog/rag, evaluation -> an offline job, audit -> a log pipeline. The Phase 30 review
(an independent architecture critic and security reviewer, every finding re-checked in the code
and, where it needed the real system, on the owner's Mac) found:
- **knowledge-service and evaluation were never built**: each folder holds only a CLAUDE.md.
  Evaluation already IS an offline job (`make eval-*`, the `eval` schema). architecture.md still
  drew both, a Kafka consumer for them, and Redis as shared state; 9 services run.
- **audit works** (81 approval rows on the Mac) but had two real defects: monthly partitions
  were created once by migration 0011 (they ended at 2026-12; from 2027-01 every row would land
  in the default partition and every later partition creation would fail), and every Kafka row
  was stored as outcome SUCCESS (a failed action was invisible to an outcome filter).
- **Kafka carries ONE flow** (`incident.lifecycle` -> audit). The audit consumer and the kafka
  transport were OFF by default: a deployed system without two `.env` lines recorded no
  approval in the audit trail (the owner's Mac had them on). 3 of 4 topics had no producer and
  no consumer (`investigation.tasks/results`: ADR-030; `catalog.changes`: ADR-032).

## Decision (owner, 2026-10-30)
1. **Nine services.** Remove the knowledge-service and evaluation stubs; fix the docs so the
   diagrams show what runs (architecture.md sections 7-11, 17, 18, 24).
2. **Keep audit as a service; fix its defects** (Phase 30 code): the service keeps partitions 3
   months ahead itself (`audit.ensure_partitions` v2: SECURITY DEFINER, pinned search_path,
   EXECUTE only for svc_audit, a month already in the default partition skipped with a WARNING;
   called at start and every 6 h); `ActionExecuted` with status FAILED is stored as FAILURE.
   The critic's alternative (fold audit into a library + the api) is NOT taken: at 1 replica the
   cost of the service is one pod (256M), one HTTP hop and its secrets; folding it means a
   rewrite of the write path and an exception to "a service never writes another schema",
   late in the project, for no measured problem.
3. **Keep Kafka as the pattern it demonstrates, not for load**, with SAFE DEFAULTS: compose
   (and so kind, generated from compose) defaults to the kafka transport + the audit consumer.
   Host runs (`make dev`) still default to log/off (no broker needed); `.env.example` says what
   that costs. The three dead topics are no longer created; `incident.processed_events` (never
   written or read) is dropped (migration 0030).
4. **Two leaks fixed** (security review): every untrusted field of the postmortem markdown is
   escaped; ALL model-written text (not only removed text, ADR-045) needs a reader of every
   evidence kind of the incident, because the models were shown every observation.
5. **Not fixed now (recorded, accepted for the prototype):** the webhook's rate limit is charged
   before the signature check; the warm-up does not refuse to start if its zero-signature token
   is accepted; the demo harness can be pointed at a non-local host; defang misses bare and
   mixed-case domains; outbox tables are never purged; Kafka is PLAINTEXT without ACLs; the
   orchestrator's resume has no lease (2 replicas would double-run - 1 replica is a requirement,
   ADR-034); the Redis container stays an unused profile until replicas exist (ADR-031).

## Alternatives
| Option | Why not (here) |
|---|---|
| Fold audit into a library + the api (critic) | Rewrite + ownership exception for one pod at 1 replica; no measured problem. Revisit with replicas or a SIEM. |
| Audit as a log pipeline (Loki) | No row-level authorisation for the scoped reads, no idempotent dedupe, no append-only guarantee; observability is optional (a profile); logs must not carry content (ADR-035). |
| Remove Kafka | Simpler (one container less), but undoes a verified phase (18a) and ADR-003 for a small saving; the outbox -> consumer pattern with DLQ/replay is worth showing. |
| Kafka on by default for host runs too | `make dev` would need a broker for every quick test. |

## Tradeoffs
- A deployed system now NEEDS Kafka: `make k8s-apply` refuses without compose kafka (the
  existing preflight), and `make apps-up` adds the kafka profile. Kafka cost two demo-setup
  failures on a power-limited Mac (Phase 29).
- Stricter redaction hides the narrative from a reader who lacks any evidence kind (e.g. a
  MANAGER sees code-written facts and statements, not the model's sentences). Correct by the
  ADR-045 reasoning; a product cost.
- The partition keeper runs in the service: with 1 replica there is no race; with N replicas
  two callers can race on one CREATE - the loser gets an error, logs a warning, and the next
  check finds the partition (idempotent), so it is redundant but not harmful.

## Consequences
Migration 0030; tests for the partition function as the audit login, the outcome mapping, the
escaped markdown and the model-text rule; threat-model rows T-EXF-2/3 extended, T-AUD-1 new;
architecture.md shows the 9 services; CLAUDE.md no longer says "all 11".

## Prototype vs Production vs Enterprise-scale
[P] as decided. [Prod] Kafka with mTLS + ACLs (only incident-service may produce to
incident.lifecycle), audit to WORM storage, a partition job with an alert on the next month
missing. [Ent] audit to a log store (S3 object lock / ClickHouse), architecture.md §17.

## When we would revisit
Replicas (22b): orchestrator resume lease, Redis-backed limits. A second Kafka consumer or the
outbox growing: purge job. Any public alert sender: verify-then-charge rate limiting.
