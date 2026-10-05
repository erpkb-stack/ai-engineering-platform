# agents (Python 3.12 / FastAPI) — local port 8003

**Purpose:** Horizontally scalable agent workers (log, metrics, code, deployment, RAG, historical, critic, validation, report).

**Owns schema:** none (stateless workers; results go to orchestrator/incident via events)
**Events:** consumes AgentTask; publishes AgentCompleted, AgentFailed, EvidenceRetrieved
**Must never:** Hold state between tasks. Access systems except via Tool Gateway.
**Why this is a separate service:** CPU/IO-heavy and bursty → scales on Kafka lag independently.

Status: Phase 1 — design only. Scaffold with the `new-service` skill in its phase (see docs/roadmap.md).
