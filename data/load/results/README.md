# Load results (Phase 27, ADR-039)

`make load PLAN=...` writes one JSON per run here: measured numbers only, with the environment
that produced them (git commit, api rate limit it ran with, the LLM routes and the SIMULATED
delays that were live, the machine, the backlog of running investigations at start and end).

- `policy-*`: the shipped rate limits. 429s are POLICY (reported apart), never a "knee".
- `capacity-*`: the api limit raised at deploy time for that run only (value recorded).
- `investigations-*`: per step, QUEUE (wait for one of the orchestrator's slots) apart from
  RUN (the work). LLM delays are simulated p50s measured on real calls: no tail.
- `smoke-*`: a 30 s set-up check, never quoted.

The load client and the system share ONE laptop: each stage records the client's own CPU.
Sandbox runs (2 CPUs, no RAG service) checked the mechanics only and are not committed.

2026-10-27 (Phase 28c-2): two capacity re-runs on a healthy disk - errors gone, throughput still varies up to 2x (phase-28.md section 7). Known flaw: `api_rate_limit_per_minute` in a result is the load tool's OWN environment, not the deployed value (both runs were deployed with 100000).
