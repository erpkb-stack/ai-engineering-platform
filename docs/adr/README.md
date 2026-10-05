# Architecture Decision Records

| ADR | Title | Status |
|---|---|---|
| [ADR-001](ADR-001-use-langgraph.md) | Use LangGraph for investigation orchestration | Proposed |
| [ADR-002](ADR-002-postgres-pgvector.md) | PostgreSQL + pgvector for relational, full-text and vector data | Proposed |
| [ADR-003](ADR-003-kafka-events.md) | Kafka for asynchronous events with outbox and idempotent consumers | Proposed |
| [ADR-004](ADR-004-fastapi.md) | FastAPI for Python services | Proposed |
| [ADR-005](ADR-005-java-service-catalog.md) | Java 21 / Spring Boot for the Service Catalog | Proposed |
| [ADR-006](ADR-006-permission-aware-rag.md) | Permission-aware retrieval enforced in SQL | Proposed |
| [ADR-007](ADR-007-human-in-the-loop.md) | Mandatory human-in-the-loop for consequential actions | Proposed |
| [ADR-008](ADR-008-tool-gateway.md) | Tool Gateway as the only path from agents to systems | Proposed |
| [ADR-009](ADR-009-kubernetes.md) | Kubernetes for deployment (kind locally, EKS reference) | Proposed |
| [ADR-010](ADR-010-opentelemetry.md) | OpenTelemetry for traces, metrics and logs | Proposed |
| [ADR-011](ADR-011-eleven-services.md) | Eleven services from day one (instead of a modular monolith) | Proposed |
| [ADR-012](ADR-012-graph-in-postgres.md) | Postgres edges + recursive CTE for the knowledge graph (no graph DB) | Proposed |

Rules: Accepted ADRs are immutable (enforced by `.claude/hooks/protect-files.sh`). Use the `/adr` skill to supersede.
