# ADR-041: Readiness gates a pod's START; a shared-dependency outage is answered, not hidden

Status: Proposed (measured on kind 2026-10-27: pg outage 61 dropped connections -> 0, 85 x 503 + Retry-After, recovery 9.4 s -> 4.3 s)
Date: 2026-10-27
Refines: architecture.md §22 (probes), ADR-034 (kind probes), ADR-040 (finding 1)

## Context
- Phase 28a, pg stopped 60 s on kind: 61 dropped connections, only 4 answers (503). Readiness
  checked the DB (incident-service, orchestrator, rag, tool-gateway, llm-gateway, audit) and the
  api checked incident-service, so Kubernetes took the api out of its Service. The 503 +
  `Retry-After` added in 28a almost never reached a caller.
- Readiness exists to send traffic to ANOTHER pod. The DB is shared by every replica: failing
  readiness on it can never find a better pod, it only removes all of them. With one replica
  (ADR-034) that is a full outage of every endpoint - including ones that need no DB.
- Readiness is also the rollout gate: a new pod with a wrong DB password or a network policy that
  blocks it must NOT replace a working one. Dropping the check would lose that.

## Decision
- **A dependency check gates the start only (latched):** `/health/ready` is 503 until each check
  has passed once; after that a failure is reported in the body (`"status": "degraded"` + the
  failing check) and the status stays 200. The pod keeps serving and answers 503 +
  `Retry-After` itself (28a's middleware). Liveness is unchanged (no dependency).
- **A continuous check** (`create_app(readiness_continuous=...)`) is state of THIS pod that
  another replica would not share. Only one so far: audit's consumer (review M4 - readiness is
  still audit's only lag signal).
- The api keeps its incident-service check, latched: it proves at deploy time that the token,
  address and network policy work.
- `make chaos` gains a rule before its 28c run: `always_answers` - every request gets an HTTP
  status (a dropped connection gives the caller nothing to act on). Under it 28a's pg run FAILS.

## Tradeoffs
- A pod whose OWN connection breaks permanently (bad pool, not a DB outage) stays in the Service
  and answers 503s; before, it would have been removed. Its body says `degraded`, so a monitor
  can see it; restarting it stays a human / liveness-free decision.
- Not fixed by this: overload (Phase 27: slow 503s). That is 28c-2, load shedding, measured
  with `make load`.

## Consequences
- Prototype: one replica - an outage is now 503s with `Retry-After` instead of no answer.
- Production: replicas behind a load balancer - same rule; per-pod faults use continuous checks.
- Enterprise-scale: the "degraded" body feeds an alert; dependency health is a dashboard, not a
  routing decision.
