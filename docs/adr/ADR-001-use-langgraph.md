# ADR-001: Use LangGraph for investigation orchestration

Status: Proposed
Date: 2026-10-02
Supersedes: —

## Context
Investigations are multi-step, stateful, partly parallel, must survive worker crashes, and must pause for human approval for minutes to hours.

## Decision
Use LangGraph `StateGraph` in `services/orchestrator` with typed state, `Send` fan-out, reducers, PostgresSaver checkpoints and `interrupt()` for human review. Agents are graph nodes that dispatch work to stateless workers via Kafka.

## Alternatives
| Option | Why not (here) |
|---|---|
| Plain asyncio + own state machine | Must rebuild checkpointing, resume, interrupts, and visualisation (~1–2k LOC + bugs). |
| Temporal | Excellent durable execution, but adds a cluster to run on a laptop and is not AI-specific; a strong [Ent] candidate. |
| CrewAI / AutoGen | Higher-level role abstractions; less control over state, determinism and checkpoints. |

## Tradeoffs
Against: framework lock-in and frequent API changes; the debugging abstraction leaks. Mitigation: LangGraph is used only inside orchestrator; agents are plain functions with a stable interface; version pinned; `langgraph-engineer` subagent checks docs before changes.

## Consequences
Checkpoint storage grows (prune completed runs). Engineers must learn graph semantics.

## Prototype vs Production vs Enterprise-scale
[P] single orchestrator replica. [Prod] multiple replicas, partition ownership by incident_id. [Ent] evaluate Temporal for durable execution with LangGraph only for the reasoning subgraph.

## When we would revisit
If checkpoint/resume or interrupt semantics break across upgrades, or if >50% of nodes become deterministic (then a workflow engine fits better).
