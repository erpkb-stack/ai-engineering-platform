# Phase 27 - Load testing (ADR-039)

Status: **MEASURED on the Mac 2026-10-26** (5 result files in `data/load/results/`). Verify =
PR #42 checks. The bottleneck is NOT proven; absolute capacity is NOT reproducible (section 5).
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

## 4. Runs on the Mac (kind, the owner ran them; commit f2d050b)
Intel Mac, 12 CPUs (Docker: 12), 16 GB; kind with 1 replica per service, each Python service
capped at 1 CPU (compose `cpus: "1.0"`); fake LLMs waiting the measured p50s. Order: smoke,
policy, capacity (limit 100000/min), investigations, then a second capacity run. Defaults
(real routing, limit 120) restored after, the investigation queue drained first; `make smoke`
12/12 and `make k8s-netpol-test` 130 passed afterwards.

## 5. Results (only what the files say)
### 5a. Read path - capacity, two runs, same code and plan
| target rps | run 1 (19:05): delivered / p50 / p95 / errors | run 2 (20:15): delivered / p50 / p95 / errors |
|---|---|---|
| 10 | 10.0 / 64 ms / 315 ms / 0 % | 9.1 / 148 ms / 3.2 s / 1.8 % |
| 25 | 21.1 / 96 ms / 529 ms / 0 % | 9.2 / 719 ms / 4.7 s / 0 % |
| 50 | 13.7 / 1.3 s / 3.8 s / 0 % | 6.0 / 2.1 s / 13.0 s / 32.6 % |
| 75 | 19.7 / 1.0 s / 2.5 s / 0 % | 6.8 / 3.7 s / 8.6 s / 34.1 % |
| 100 | 16.6 / 1.4 s / 3.6 s / 0.6 % | 5.3 / 4.8 s / 10.9 s / 38.9 % |
| 150 | 22.9 / 0.9 s / 2.5 s / 0.2 % | 3.1 / 4.7 s / 15.9 s / 71.0 % |
- **The two runs differ by about 10x at the same load.** No absolute capacity from this laptop
  is quotable. What holds in both: the system bends between 10 and 25 rps, far below 100.
- Not the load client (1-4 % CPU in every stage). No backlog of investigations in either run.
  In run 2 the cluster used up to ~10 of 12 CPUs and Postgres up to ~2.4 CPUs while delivering
  <= 9 rps: more work per request, not only contention - cause NOT found.
- Server p95 (Prometheus): the api is the highest in most stages (every request crosses it);
  several values are the histogram's top bucket and mean ">= 10 s", not 10 s.
### 5b. Policy run (limits as shipped, 120/min per user)
- 0 % 429 in every stage: the team's policy cap (~62 rps) was never reached, because the system
  bent first (delivered 10.2, 10.1, 5.7, 19.0 rps at targets 10, 30, 60, 100). My prediction
  ("429s from ~62 rps") was wrong.
### 5c. Overload = slow 503s, not fast refusals
- In all three overloaded runs errors are 503 from the api (an upstream timed out) or a closed
  connection - after seconds of waiting. Nothing sheds load early. A Phase 28 item.
### 5d. Investigations (simulated LLM delays, 10 rps reads beside)
| per min | started | finished | queue p50 / max | run p50 / max | end-to-end p50 |
|---|---|---|---|---|---|
| 2 | 11 | 11 | 1.4 s / 9.4 s | 43.5 s / 56.1 s | 50.2 s |
| 4 | 19 | 5 (6 still running, 8 failed to start: 503 or closed connection) | 20.1 s / 32.6 s | 46.5 s / 103.7 s | 64.9 s |
- Capacity = 2 slots / 43.5 s = ~2.8 per minute; at 4/min the queue grows - the measurement
  matches the model. The 43.5 s run is close to the real paid run's 40.8 s per case (25b): the
  simulated delay shape holds. At 4/min the reads beside it degrade too (p95 1.4 s -> 6.5 s).

## 6. Not proven, and how to prove it
- **Which service saturates.** Hypothesis: the api hits its 1-CPU limit. My check was wrong:
  `ps %CPU` is a lifetime average (api showed 46-50 % on a pod started minutes before). The
  right measurement is the pod's throttling counters (`nr_throttled` in its cgroup `cpu.stat`)
  read before and after a stage. Not run.
- **Why run 2 was 10x worse.** Measured differences: Postgres CPU higher in 4 of 6 stages (up to
  235 % vs at most 85 % in run 1), cluster CPU up to ~10 CPUs (vs at most ~4). Candidates (unproven): work on the Mac after the 5 GB Docker prune; other laptop load.
- **Error rates:** at 50-60 rps the three runs gave 0 %, 32.6 % and 44.7 %: not quotable from
  one run.

## 7. Open items -> Phase 28
- No load shedding: overload becomes slow 503s (fast 429/503 with Retry-After, admission control).
- Real Claude calls ERRORED 15-37 % per agent in Phase 25 (`error_type` not read yet).
- The old service-catalog pod had restarted 9 times in ~6 days (seen during the deploy).
- The Mac's disk was 97 % full (409 MB free) and failed a deploy; 5.3 GB of build cache freed.

## 8. Verify
- [x] Runs on the Mac: smoke, policy, capacity x2, investigations - files committed; defaults
      restored (queue drained first), smoke 12/12, netpol 130 passed afterwards.
- [ ] PR #42: all checks green.
