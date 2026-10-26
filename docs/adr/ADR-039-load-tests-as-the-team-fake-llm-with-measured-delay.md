# ADR-039: Load tests run AS the seeded team, against kind, with fake LLMs that wait the MEASURED delay

Status: Proposed (measured 2026-10-26; bottleneck not proven)
Date: 2026-10-26
Refines: architecture.md §17 (10 rps prototype / 100 rps production), ADR-014, ADR-034

## Context
- Roadmap: "Locust/k6 at 10 / 100 rps on laptop; bottleneck report with measured numbers".
- Need check: (1) the api limits each user to 120/min (burst 30); the seed has 31 users who
  read (23 engineers + 8 SREs), so the TEAM is capped near 62 rps by policy - one test user
  would measure the limiter, not the system. (2) 100 investigations/s is not a workload: an
  investigation is ~9 tool calls and several LLM calls (§17: the bottleneck is LLM latency).
  (3) Real models under load cost money per request; FAKE models answer in 0 ms and would
  make investigations look free. (4) The client and the system share one Intel laptop.

## Decision
- **Locust** (Python, `FastHttpUser`) in a separate uv group `load` (not installed by CI or
  `make check`). One language and toolchain; at <= 150 rps its client CPU is recorded per stage.
- **Every reader is a real seeded user with their own token** (minted like `make token`), so
  per-user limits behave as for a team. Plans: `policy` (limits as shipped; 429s reported apart
  as policy), `capacity` (api limit raised at DEPLOY time only, `API_RATE_LIMIT_PER_MINUTE`,
  the value recorded in the result), `investigations` (2 then 4 per minute on a 10 rps read
  load), `smoke` (30 s set-up check).
- **Fake LLMs with the measured delay:** `routing.load.yaml` = the CI fakes + a per-agent wait
  equal to the p50 of OK calls in `llm.model_usage` (Haiku + Sonnet, owner's Mac). The config
  REFUSES `simulated_latency_ms` unless every provider is fake, and `/v1/routes` reports the
  delays that were live. The load tool refuses any non-fake model (paid OR Ollama).
- **Queue vs run:** each investigation's wait for an orchestrator slot (first task start -
  created) is reported apart from its run time: `max_concurrent_investigations` is 2.
- **A run refuses to start on a backlog:** investigations already RUNNING (a crash, an earlier
  run) share the same slots; found in the sandbox (100 left by a buggy first version).
- **Knee** = the first stage that delivers < 90 % of its target, or p95 > 2x the first
  stage's, or > 1 % errors (5xx / no answer). 429 is never a knee.

## Results (Mac, 2026-10-26; phase-27.md section 5)
- The read path bends between 10 and 25 rps on this laptop, far below 100; two identical capacity
  runs differed ~10x, so no absolute number is quotable. The per-user limit never engaged (0 %
  429): the system bends before the team's ~62 rps policy cap. Overload = slow 503s.
- Investigations: ~2.8/min = 2 slots / 43.5 s run, as modelled; above it the queue grows.
- Not proven: which service saturates (the `ps` check used a lifetime average - wrong tool).

## Tradeoffs
- A fixed p50 delay has no tail (real p95 is 1.2-1.8x p50): queueing is UNDERstated.
- Search uses fake embeddings: real embedding cost is measured apart (`make rag-bench`).
- Closed-loop Poisson clients: when the system slows, the offered rate falls - that is read
  as the knee, not hidden.
- Measured on one laptop with the client on it: numbers describe THIS machine, not capacity.

## Prototype / Production / Enterprise-scale
- Prototype (this): one laptop, kind with 1 replica, fake LLMs with measured delays.
- Production: a separate load cluster, open-loop arrivals, real models against a provider
  sandbox with token budgets, SLOs with error budgets.
- Enterprise: continuous load in CI on a fixed environment, per-release capacity reports.
