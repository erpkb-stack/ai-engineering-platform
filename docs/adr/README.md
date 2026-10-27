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
| [ADR-013](ADR-013-single-migration-stream.md) | One Alembic migration stream for all service-owned schemas (for now) | Proposed |
| [ADR-014](ADR-014-llm-routing-and-fallback.md) | LLM routing policy, fallback and cost accounting in one gateway | Proposed |
| [ADR-016](ADR-016-hybrid-retrieval.md) | Hybrid retrieval in one SQL statement, RRF fusion, eval-gated LLM rerank | Proposed |
| [ADR-017](ADR-017-tool-gateway-authz-and-audit.md) | Tool Gateway: on-behalf-of authz (user ∩ agent allow-list), fail-closed audit via outbox | Proposed |
| [ADR-018](ADR-018-first-agent-code-facts-llm-labels.md) | First agent: facts by code, labels by LLM, thin runner that records the trace | Proposed |
| [ADR-019](ADR-019-orchestrator-graph-and-delegation.md) | Orchestrator service + LangGraph checkpoints; investigation-bound delegation tokens (token exchange) | Proposed |
| [ADR-021](ADR-021-hypotheses-critic-validation.md) | Hypotheses by code, ranking by Haiku, critique by Sonnet (no fallback), deterministic validation; model text redacted by evidence kind | Proposed |
| [ADR-022](ADR-022-timeline-and-postmortem.md) | Timeline by code in incident_events; postmortem narrative by Sonnet (checked: refs, numbers, markup); assembled at read time, redacted per reader | Proposed |
| [ADR-023](ADR-023-historical-incident-intelligence.md) | Hybrid past-incident search (ACL in both branches); only incidents shared with all engineers stored, as pointers; Haiku claims checked by code; history prior = tie-break inside a band only | Proposed |
| [ADR-024](ADR-024-code-intelligence.md) | Code only: tree-sitter rules over the deploy's diff (pre-image rebuilt), file-ACL code index for change impact; a loop query is ONE more source for its own deploy, only with a symptom on that service; plan holds counts only | Proposed |
| [ADR-025](ADR-025-release-risk.md) | Advisory release risk (PR / range / deploy), code only: rubric band over cited factors, findings vs coverage reported apart, way back says when a flag flip would NOT undo the code; stored per request, never a gate | Proposed |
| [ADR-026](ADR-026-human-approval-execution.md) | Human approval, then controlled execution: proposals by code or an SRE, an IC who is not the requester decides, the action runs in the approver's own request AS the approver (executor identity, gateway consumes the approval at incident-service: action + args hash + incident + approver, once); refines ADR-007 (no interrupt) | Proposed |
| [ADR-037](ADR-037-investigation-eval-planted-causes.md) | Investigation eval: 16 synthetic incidents with PLANTED causes through the product path, exact scoring (kind + subject) into the `eval` schema; truth only in eval_cases; incomplete evidence = INVALID run (and the orchestrator now says so); CI = regression gate on the code path, not an accuracy target; paid models opt-in | Proposed |
| [ADR-038](ADR-038-security-hardening-threat-model-as-tested-index.md) | Security hardening: a threat model that is a tested index of existing tests; PII scrubbed at the hosted-LLM boundary; no NER model, no npm audit (nothing ships) | Proposed |
| [ADR-041](ADR-041-readiness-gates-the-start-not-shared-outages.md) | Readiness gates a pod's start only (a dependency check latches once passed); a shared-dependency outage keeps pods in the Service answering 503 + Retry-After; only pod-local state (audit's consumer) is continuous | Proposed |
| [ADR-040](ADR-040-failure-experiments-with-rules-fixed-before-the-run.md) | Failure experiments: one fault at a time with plain `docker compose stop` / pod delete (no chaos framework), the owner runs them, rules fixed before the run (no hang, recovers, no restarts, investigation never stuck RUNNING, outbox drains); Redis off by default | Proposed |
| [ADR-039](ADR-039-load-tests-as-the-team-fake-llm-with-measured-delay.md) | Load tests as the seeded team (per-user limits behave), against kind, fake LLMs that wait the MEASURED per-agent p50; queue apart from run; 429 is policy, never a knee | Proposed |
| [ADR-036](ADR-036-ci-every-pr-no-deploy.md) | CI runs everything on every PR (Java tests, console, both images built + Trivy-scanned); Trivy = a release binary pinned by SHA-256 (the 2026 compromise), scan report-only until the baseline is reviewed; no push, no deploy; merge only on green | Proposed |
| [ADR-035](ADR-035-observability-pipeline.md) | One OTLP pipeline in compose (collector + Tempo, Prometheus, Loki, Grafana); an investigation stores its trace context, so a resume and the Kafka hop stay in one trace; no content in telemetry (exceptions keep their type only); LLM cost stays in Postgres | Proposed |
| [ADR-034](ADR-034-kind-apps-only-generated-manifests.md) | Only the apps run in kind (infra stays in compose); manifests are GENERATED from `docker compose config` at deploy time; Pod Security restricted; default-deny NetworkPolicies from the URL graph; 22a = 1 replica, 22b = replicas + Redis | Proposed |
| [ADR-033](ADR-033-one-python-image.md) | ONE non-root image for the 8 Python services + the catalog image; addresses from config (tested against the Settings classes); each container mounts only its own secret files; limits from measured peaks | Proposed |
| [ADR-032](ADR-032-service-catalog-contract-first.md) | The service catalog is a small read-only Java service with a HAND-WRITTEN OpenAPI contract (Python models generated from it, drift tested on both sides); the JSON file stays the default source, a parity check proves both answer the same; no outbox (no consumer) | Proposed |
| [ADR-031](ADR-031-redis-when-replicas-exist.md) | Redis is added when a service has more than one replica (Phase 22): every planned use works correctly for one replica today; idempotency and locks stay in Postgres | Proposed |
| [ADR-030](ADR-030-agent-tasks-stay-on-http.md) | Agent tasks stay on HTTP: a task queue gains nothing at this scale and would put a user's bearer token in a Kafka log, pay twice for duplicate LLM work and rewrite the graph's resume; revisit on measured triggers | Proposed |
| [ADR-029](ADR-029-kafka-consumers.md) | Kafka consumers (`libs/messaging`): commit after the work, idempotent handlers, poison to a DLQ at once, outages waited out (never DLQ), bounded replay; audit consumes incident.lifecycle | Proposed |
| [ADR-028](ADR-028-operations-console.md) | The console is a SPA: PKCE public client, tokens in memory, types generated from the api's OpenAPI (drift fails a test), the UI gates and the server decides, untrusted content as text; inbox by 15 s polling | Proposed |
| [ADR-027](ADR-027-oidc-identity-provider.md) | Every service trusts the OIDC IdP (Keycloak) for USER tokens, routed by issuer; the realm is GENERATED from identity.users (uid + aeoi_sub admin-only attributes), so the directory stays the source of truth; production refuses static user tokens | Proposed |
| [ADR-020](ADR-020-multi-agent-code-first-specialists.md) | Multi-agent: code-only metrics/deployment/knowledge agents, PARTIAL outcome, rag on-behalf-of, evidence visibility by kind | Proposed |

Rules: Accepted ADRs are immutable (enforced by `.claude/hooks/protect-files.sh`). Use the `/adr` skill to supersede.
