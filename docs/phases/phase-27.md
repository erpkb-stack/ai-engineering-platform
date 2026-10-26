# Phase 27 - Load testing (ADR-039)

Status: **BUILT (tools), not yet MEASURED.** Verify = PR checks + the runs on the Mac below.
Owner: "go 27"; target **kind**; scope **read path + slow investigations** (2026-10-26).

## 0. Gate
- Phase 26 verified (PR #41, main 997989a, run 40277732958 green).

## 1. Need check (read in the code first)
1. **Rate limits answer a naive test:** api 120/min per user (burst 30); 31 seeded users read
   -> the team is capped near 62 rps BY POLICY. One test user would measure the limiter.
2. **"100 rps" is reads:** an investigation is ~9 tool calls + several LLM calls; §17 already
   says the prototype's bottleneck is LLM latency.
3. **Real LLMs cost money per request; fakes take 0 ms.** Fakes now wait the MEASURED p50:

| agent | model | p50 (OK calls) | p95 | n |
|---|---|---|---|---|
| log_analysis | Haiku | 2494 ms | 3103 ms | 49 |
| hypothesis | Haiku | 5248 ms | 6661 ms | 39 |
| historical_incident | Haiku | 5865 ms | 6402 ms | 38 |
| critic | Sonnet | 7593 ms | 10886 ms | 68 |
| postmortem | Sonnet | 14693 ms | 26384 ms | 79 |
(`llm.model_usage` on the Mac, Phase 25 runs, queried 2026-10-26.)

4. **Found in the same query, for Phase 28:** real calls ERRORED often - historical_incident
   22 of 60, critic 14 of 84, postmortem 15 of 95, hypothesis 11 of 50, plus TIMEOUTs. Cause not
   read yet (`error_type`). Failure testing starts there.

## 2. What Phase 27 adds
- `make load PLAN=smoke|policy|capacity|investigations` (Locust, uv group `load`): every reader
  is a seeded user with their own token; result JSON in `data/load/results/` per stage: target
  vs achieved rps, p50/p95/p99 per endpoint, 429 share apart from errors, the client's own CPU,
  the containers' CPU/memory (`docker stats`), each service's server p95 (Prometheus, if up).
- Investigations: per step QUEUE (wait for one of the orchestrator's 2 slots) apart from RUN.
- `routing.load.yaml` + `simulated_latency_ms` (refused unless every provider is fake; shown by
  `/v1/routes`). The tool refuses a non-fake model and a backlog of RUNNING investigations.
- `API_RATE_LIMIT_PER_MINUTE` (compose, default 120): raised for the capacity run only.
- Fixed on the way: the fake LLM kept EVERY request in memory (unbounded under load) - now the
  last 200.

## 3. Found while building (sandbox dry runs; mechanics only - numbers not quoted)
- My first version started 100 investigations in 2 minutes instead of 12: Locust split the
  users across classes by weight. Pinned to one starter (`fixed_count = 1`).
- Investigations still running at the end were dropped from the report; now counted at start.
- Those 100 runs stayed in the orchestrator's queue and silently slowed the next run: the
  tool now REFUSES to start on a backlog.
- The simulated delays are applied for real: with no RAG service the run took 15.4 s = log
  2.5 + hypothesis 5.2 + critic 7.6 s. At 12/min the QUEUE grew (p50 5.7 s), RUN did not.

## 4. Runs on the Mac (kind) - the owner runs these
See the commands in the PR; order: smoke -> policy -> capacity -> investigations; then the
default routing and limit are restored. Every number in section 5 comes from those files.

## 5. Results
(to be filled from `data/load/results/*.json` - measured only)

## 6. Verify
- [ ] PR: all checks green.
- [ ] Mac: smoke, policy, capacity, investigations result files committed; knee and bottleneck
      written from them; default routing + rate limit restored on kind.
