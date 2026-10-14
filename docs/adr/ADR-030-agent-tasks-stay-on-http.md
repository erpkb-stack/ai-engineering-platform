# ADR-030: Agent tasks stay on HTTP - no task queue until a measured need

Status: Proposed
Date: 2026-10-14
Refines: architecture.md §17 (topics `investigation.tasks` / `investigation.results`), §22 (agent scaling); ADR-019 (delegation), ADR-029 (consumers)

## Context
architecture.md planned agent work as a queue: the orchestrator produces `AgentTask` to
`investigation.tasks`, agent workers consume it, results come back on `investigation.results`,
and workers scale on consumer lag. Phase 18 was meant to build it (18b). Owner decision
(2026-10-14), after the cost check below: do not build it now.

How the path works today (read in the code, not assumed):
- The graph's `run_agent` node (and `reason` for the reasoning agents) awaits ONE HTTP call
  `POST /v1/agents/{agent}/run` (`graph.py` `call_agent`), records the result in
  `orchestrator.agent_executions`, and LangGraph checkpoints after the step. On resume a
  recorded task is reused, never re-run (deterministic task id).
- Transient failures (agents unreachable, 502/503) already get 2 retries with backoff.
- The agents service is stateless: no schema, no store (services/agents/CLAUDE.md).
- The call carries a DELEGATED user token in `X-On-Behalf-Of`. It lives 5 minutes, and only
  the orchestrator can refresh it (refresh is actor-only at the api's token exchange, ADR-019).
- Limits: task deadline 180 s (agents' own 150 s), investigation 900 s, at most 2 concurrent
  investigations (`max_concurrent_investigations`).

## Decision
Keep agent tasks on HTTP. Do not build `investigation.tasks` / `investigation.results`
consumers or producers. The topics stay created (empty, cheap; `verify-infra` checks them) and
are marked "not used - ADR-030" in architecture.md.

## Why a queue gains nothing here
| What a queue would give | Today |
|---|---|
| Survive an agents restart | transient retry + checkpointed resume (a lost call becomes attempt 2, recorded) |
| Tasks longer than an HTTP request | every task ends within 150 s, inside the 180 s deadline |
| Workers that scale on backlog | 2 concurrent investigations on one Mac; nothing waits for a worker |
| Decoupling | one producer, one consumer, same team: the HTTP contract is already typed and tested |

## What it would cost (the 18b estimate)
1. **The graph must stop waiting.** A node cannot wait minutes for a message. Either it still
   blocks (a queue with an HTTP-shaped wait: all the parts, none of the gain) or the graph ends
   the run at each task and resumes when the result message arrives: a rewrite of `engine.py`
   (resume, deadline, cancel, shutdown) and of the reuse guard.
2. **A user's token would sit in a log.** A delegated token in a Kafka record is a bearer
   credential kept for the topic's retention (7 days) and readable by anyone who can read the
   topic. A 5-minute token is also often expired when a busy worker reaches it. The fix -
   agents refreshing tokens themselves - makes agents an actor at the token exchange, widening
   ADR-019's trust model.
3. **Duplicates cost money.** At-least-once delivery means an agent task can run twice: twice
   the tool calls and LLM spend. Agents would need a store to dedupe on task id - the stateless
   service gains a schema.
4. **Blast radius.** It rewrites the investigation path that Phases 9-16 built and verified
   (about 10 end-to-end test files).

Estimate (not measured): several days of work and re-verification, for no change a user or
an operator could see at this scale.

## Alternatives
| Option | Why not (now) |
|---|---|
| Full queue (as planned) | costs 1-4 above, benefits none at this scale |
| Queue, but the node blocks on the result | adds a broker, two topics and a correlation map, and changes nothing: the same deadline, the same blocking |
| Publish agent lifecycle events only (AgentStarted/Completed) via an outbox, for audit | possible and cheap (~1 day), but no consumer needs them yet; `agent_executions` already records every attempt |

## Tradeoffs
- Agent workers cannot scale on backlog. [Prod] they scale on CPU (HPA), behind the same
  Service; max 2 concurrent investigations per orchestrator replica is the real limit.
- The architecture document described something that was never built; now it says so.
- The planned `investigation.results` consumers (audit, incident) do not exist; audit gets the
  incident's story from `incident.lifecycle` (ADR-029).

## Prototype vs Production vs Enterprise-scale
[P] HTTP, 2 concurrent investigations, CPU scaling. [Prod] the same, more agent replicas behind
the Service, investigation concurrency per orchestrator replica. [Ent] a task queue becomes worth
it when many teams run many investigations: then with a token that is NOT a bearer credential in
the message (e.g. the worker exchanges a grant id under its own identity, with a narrow scope).

## When we would revisit (any one is enough)
- Load tests (Phase 27) show agent calls queueing or timing out while agents are saturated.
- A task must run longer than an HTTP deadline (minutes).
- Agents must scale on backlog independently of CPU (e.g. KEDA in Phase 22 for a cost reason).
- More than one consumer needs the agent results as they happen.
