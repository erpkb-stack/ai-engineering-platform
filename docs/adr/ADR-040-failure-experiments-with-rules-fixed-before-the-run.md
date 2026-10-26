# ADR-040: Failure experiments - one fault at a time, plain stop/delete, rules fixed before the run

Status: Proposed (28a built 2026-10-26; dry run found 3 defects, fixed; not run on the Mac yet)
Date: 2026-10-26
Refines: ADR-014 (resilience), ADR-019 (checkpoints + resume), ADR-029 (consumers wait), ADR-031 (Redis), ADR-034 (kind)

## Context
- Roadmap: "Failure testing". Need check (code + tests read first, 2026-10-26):

| fault | tested before | how |
|---|---|---|
| LLM provider errors / timeouts | yes | unit (breaker, fallback, retries) |
| Kafka down | yes | unit + `consumer-smoke` (consumers wait, never DLQ) |
| **Postgres down** | **never** | - |
| worker killed mid-investigation | Phase 9, compose only | **never on kind** |
| Redis down | not needed | no service uses Redis (ADR-031) |
| overload | Phase 27 | slow 503s, no load shedding |

- Real data (`llm.model_usage`, owner's Mac): 52 `CircuitOpenError`, ALL on 2026-10-23, after
  only 4 `ProviderTimeoutError` - one breaker opening became a cascade. `historical_incident`
  failed `StructuredOutputError` 11 times (2026-10-14..23) and cannot be diagnosed: only the
  error CLASS is stored.
- Redis was started by `make up` although nothing reads it: memory and a "failure" to test
  that cannot fail anything.

## Decision
- **Three slices:** 28a outages (this ADR's runner), 28b LLM failures (record WHERE structured
  output fails - schema path + validator keyword, never content; then fix with data; review the
  breaker's threshold), 28c load shedding (fast 503 + `Retry-After`, measured with `make load`).
- **Redis out of the default start** (owner decision 2026-10-26): compose `profiles: ["redis"]`,
  `make up PROFILE=redis` when replicas arrive; a test fails if a service imports redis while
  it is still off by default.
- **Inject with plain tools, no chaos framework:** `docker compose stop|start postgres|kafka`
  (infra lives in compose) and `kubectl --context kind-aeoi delete pod -l app=...` for a pod
  (Kubernetes brings it back). No Toxiproxy / Chaos Mesh: one more service to learn and run on
  a laptop that is already the bottleneck, for faults the plain tools already make.
- **One experiment per run, the OWNER runs it** (`make chaos EXP=pg|kafka|orchestrator|agents`);
  Claude never stops containers or deletes pods.
- **Rules fixed BEFORE the run** (`tests/chaos/chaoslib.py`, pinned by `tests/unit/test_chaos.py`):
  - `no_hang`: every request answers or fails within 45 s (above the api's longest backend timeout);
  - `recovers`: a successful read within 60 s of the undo, and the last 30 s all successful;
  - `no_restarts`: no container restarts (liveness depends on nothing) - "not measured" without kubectl;
  - `investigation_named`: the run in flight ends COMPLETE/PARTIAL/INCONCLUSIVE/FAILED, never stuck
    RUNNING; if it had already ended before the fault the rule is "not exercised", never a PASS;
  - Kafka only: `writes_survive` (incidents created DURING the outage succeed - the outbox) and
    `outbox_drained` (both outboxes empty within 180 s after).
  A FAIL is a finding, written down as one; the rule is not changed after the run.
- **Each experiment writes its prediction first** (`Exp.expect`), e.g. Postgres: the api's
  readiness includes incident-service, whose readiness includes the DB, so the api may leave its
  Service - callers see connection errors, not a 503 problem+json.
- **Fake LLMs only** (the load tool's guard: every retry would be paid) and **no backlog**
  (refuses if investigations are already RUNNING). Results: `data/chaos/results/<exp>-<UTC>.json`.

## Found so far (sandbox dry run of `pg`, phase-28.md section 3)
- A lost DB was a 500; now 503 + `Retry-After` (shared middleware, error TYPE only in the log).
- The checkpointer's psycopg pool served dead connections after a DB restart; now checked.
- The run in flight died outside the graph's error handling and stayed RUNNING; now a lost DB
  waits and resumes from the checkpoint (bounded by the deadline), any other crash ends FAILED.

## Tradeoffs
- A pod delete is a GRACEFUL stop (SIGTERM, preStop, `shutdown()` leaves rows RUNNING to resume).
  A crash (SIGKILL, OOM) is not covered by 28a; the resume path on start is the same code.
- One read endpoint is the probe; a fault visible only on another path is missed.
- Single run per experiment: a PASS is "seen once", not a rate.

## Consequences
- Prototype: laptop, single replicas (ADR-034) - an outage of one pod IS an outage of the service.
  Production: the same rules run in a staging cluster with replicas, where `recovers` should be
  near-instant. Enterprise-scale: scheduled game days with error budgets.
