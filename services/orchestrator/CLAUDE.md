# orchestrator (Python 3.12 / FastAPI) — local port 8002

**Purpose:** LangGraph investigation graph: planning, agent dispatch, state, checkpointing, retries, completion, human interrupt.

**Owns schema:** `orchestrator` (LangGraph checkpoints, tasks)
**Events:** consumes IncidentCreated/InvestigationRequested; publishes InvestigationStarted, AgentStarted (dispatch), HypothesisChallenged
**Must never:** Talk to tools or LLM providers directly except via gateways.
**Why this is a separate service:** Stateful long-running coordination with different scaling needs from the workers.

Status: Phase 1 — design only. Scaffold with the `new-service` skill in its phase (see docs/roadmap.md).
