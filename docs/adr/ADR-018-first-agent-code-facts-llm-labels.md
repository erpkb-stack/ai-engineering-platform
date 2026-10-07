# ADR-018: First agent — facts by code, labels by LLM, a thin runner that records everything

Status: Proposed
Date: 2026-10-07
Refines: ADR-017 (evidence ids), architecture.md §10 (#3 Log Analysis), §11 (data flow)

## Context
Phase 8 builds the first real agent, the one every later agent copies. The open questions:
1. What may the LLM decide, and what must code decide?
2. Who writes the trace row (`orchestrator.agent_executions`) before the orchestrator exists
   (Phase 9), without breaking "one owner per schema"?
3. How does a finding point at evidence that survives, so an auditor can follow
   finding → evidence → tool call → audit event?
4. How do we compare a local CPU model with Claude without pretending one run is an eval?

## Decision
1. **Facts are code; the LLM labels.** The Log Analysis agent:
   search_logs (oldest-first AND newest-first) → dedupe → cluster by message template (code) →
   one `Fact` per error code with exact window counts (code) → ONE structured LLM call that names
   and categorises the clusters → validation (unknown cluster ids dropped, citations outside the
   cluster dropped) → rule labels if the model fails. Facts never depend on model output.
   "first seen"/"last seen" are claimed only when the sample proves them (oldest-first sample
   holds every line up to its cut-off); otherwise "earliest/latest sampled".
2. **No fact without evidence.** An observation with nothing to cite ("no error lines",
   "N lines with code X, none sampled", "service B: NO DATA") is a `note`, never a `Fact`.
   A silently missing service would read as "all clear", so failures are always noted.
3. **Evidence ids are kind-prefixed tool-call ids:** `LOG-<tool_call_hex>-<n>` (ADR-017 amended).
   They satisfy `findings.EvidenceRef` and `incident.evidence` (`key LIKE kind || '-%'`) as-is,
   and name the tool call that produced them. New kind `CATALOG` (migration 0017).
4. **Agents are stateless workers.** `POST /v1/agents/log_analysis/run`: service token with
   `agents:run` + `X-On-Behalf-Of` user token. The worker calls tools as `service:agents`
   (trusted for `log_analysis`) for that user. [Phase 18] the same handler consumes `AgentTask`.
5. **A thin orchestrator runner owns the trace** (owner chose this over a temporary grant):
   it writes `investigations`, `tasks`, `agent_executions`, `messages` with its own login
   (`orch_svc`), and posts *cited* evidence to incident-service
   (`POST /v1/incidents/{ref}/evidence`, scope `evidence:write`, idempotent on the key).
   Phase 9 replaces the single call with a LangGraph graph; the rows stay the same.
6. **Versioned prompt in git** (`services/agents/prompts/log_analysis/v1.md`, front matter id +
   version); its sha256 is in every trace and pinned by a test, so editing v1 in place fails CI.
7. **Budgets per task:** ≤ 6 tool calls, ≤ 6 clusters × 2 lines to the model (each service keeps
   its top 2), ≤ 10 facts, one task deadline (150 s) below the runner's HTTP timeout (180 s); the
   LLM timeout is the smaller of its cap (115 s, under the gateway's 120 s) and the time left.
   **Mac finding:** prompt v1 (8 clusters × 3 lines, full evidence ids in the answer) timed out at
   90 s on Intel-CPU llama3.2:3b. Prompt v2 cites lines by short refs (`c1.2`, mapped back in
   code) and drops the summary: much less input and output.
8. **Model comparison is an anecdote, labelled as such.** `make agent-compare` runs the same
   incident on `local` (llama3.2:3b) and `fast` (Claude Haiku) and records latency, tokens, cost,
   schema repairs, labels that passed validation and dropped citations, with `n=1` in the file.
   A run is only a valid comparison when each route was answered by a different model, with no
   fallback and no cache hit; the file records `valid_comparison`. Mac findings behind each rule:
   - the gateway ran `routing.local.yaml`, so `fast` was llama too → preflight refuses same model;
   - no Anthropic key: `fast` listed Claude first but fell back to llama → preflight judges the
     first model whose provider is *configured*, not `chain[0]`;
   - a gateway cache hit looked like a 567 ms llama answer → traces record `cached`, compare sends
     `cache=false`.
   It also asserts the facts are identical across models (they must be).

## Alternatives
| Option | Why not |
|---|---|
| LLM writes the findings from raw logs | Small models miscount and invent times; big models still can't be audited line by line. Counting is code's job. |
| Agent writes the trace into the orchestrator schema (temporary grant) | Breaks schema ownership; "temporary" grants stay. |
| No trace until Phase 9 | Fails the Phase 8 gate; the first agent is exactly where tracing must be proven. |
| One LLM call per cluster | Contains cross-cluster injection, but 8× the latency on a CPU model. We note when labels were produced next to flagged text instead. |
| LangGraph already in Phase 8 | One node needs no graph; Phase 9 introduces it with checkpoints, where it pays off. |

## Tradeoffs
- **One labelling call for all clusters:** injected text in one cluster can sway another's label.
  Labels are never facts, the flag is on the cluster, and `degraded` says so when it happens.
- **Fact budget = 10 per task:** a service with many codes gets a note "see clusters".
- **Two samples of 150 lines** bound cost; counts stay exact (window-wide SQL), times are exact
  only when provable. Long windows with many lines rely on counts more than samples.
- **Per-investigation USD budget** is recorded (`budget_usd`) but enforced by the LLM gateway's
  configured cap (ADR-014); Phase 9 passes the remaining budget per task.
- **Evidence POST is a separate step** (two services, no shared transaction): a failure leaves the
  investigation FAILED with the batch kept in `agent_executions.output`; `repost-evidence`
  retries it. A *re-run* is not a retry (new tool calls → new evidence ids).
- **Evidence integrity at the boundary:** `content_sha256` must match the excerpt and the key
  must name its `tool_call_id`. A holder of `evidence:write` can still post plausible fake rows;
  [Prod] the incident-service would verify the tool call against `tools.tool_calls`.

## Consequences
- Phase 9: the runner's steps become graph nodes; `investigation/task/execution` rows unchanged.
- Phase 10: every new agent follows this pattern (code facts, LLM labels/interpretation,
  validated citations, notes for the uncitable, budget + deadline, prompt pinned by sha).
- Phase 11: the Critic reads facts + evidence rows, never another agent's prose.

## Prototype vs Production vs Enterprise-scale
[P] synchronous HTTP worker, CLI runner, in-git prompts, one investigation per run.
[Prod] Kafka `AgentTask` consumers scaled on lag, orchestrator service with checkpoints, prompt
registry mirrored to `llm.prompt_versions`, tool-call verification on evidence write.
[Ent] per-tenant budgets and model routing, signed evidence, offline eval sets per agent.
