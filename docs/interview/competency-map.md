# Interview competency map

Every question from the spec, mapped to where in AEOI you will have **evidence** to answer it.
"Phase" is when the material exists. Answers are written with the `/interview-prep` skill (6-part format) into `docs/interview/<topic>.md`.

| Question | Component / artefact | Phase | Key point to make |
|---|---|---|---|
| Why LangGraph? | orchestrator, ADR-001 | 9 | checkpoint + interrupt + fan-out; isolated to one service |
| Why multi-agent instead of one agent? | §9 | 10 | parallel independent sources, least privilege, independent critic |
| When would you NOT use multi-agent? | copilot single-agent path | 10 | simple Q&A; latency/cost; debugging |
| How do you prevent hallucination? | validation node, citation checks | 11 | evidence ids required; deterministic checks before LLM judge; can't prevent, can detect + constrain |
| How do you evaluate RAG? | eval service, recall@k/MRR/groundedness | 6, 25 | labelled dataset with planted ground truth; retrieval vs generation measured separately |
| How do you prevent prompt injection? | §16, tests/security | 7, 26 | capability limits first; detection is secondary |
| How do you secure tools? | tool-gateway, ADR-008 | 7 | user ∩ agent permissions; side-effect classes; approval binding |
| How do you scale agent workers? | K8s HPA/KEDA, partitions | 18, 22 | scale on lag; replicas ≤ partitions; LLM TPM is the real limit |
| How do you handle LLM failures? | llm-gateway breakers/fallback | 5, 28 | timeout, retry with jitter, provider failover, degrade to partial report |
| How do you guarantee idempotency? | outbox + processed_events + Idempotency-Key | 4, 18 | at-least-once + dedupe; never claim exactly-once |
| How do you handle Kafka failures? | DLQ, retry topics, outbox | 18, 28 | outbox buffers when broker down; replay from DLQ |
| How do you handle database failures? | readiness probes, retries, Multi-AZ | 22, 28 | fail fast, don't restart-loop; RPO/RTO |
| How do you reduce LLM cost? | routing, caching, budgets | 5, 25 | small models for triage; cache; context compression; measured $/investigation |
| How do you monitor agents? | OTel spans, Grafana agent dashboard | 23 | per-agent latency/tokens/failure; trace per investigation |
| How do you debug an agent in production? | agent_executions + trace + checkpoint replay | 9, 23 | replay from checkpoint with the same prompt version |
| How do you version prompts? | prompts/ + registry + hash | 5, 25 | prompt version on every call; eval gates changes |
| How do you evaluate model changes? | eval lab A/B | 25 | same dataset/seed; CIs; cost + latency, not just accuracy |
| How do you prevent unauthorized data retrieval? | ADR-006 | 6 | filter in SQL; leakage test at repository layer |
| How do you handle PII? | libs/security PII scrubber | 6, 26 | scrub before embedding, hosted LLM, traces |
| How do you deploy this to Kubernetes? | infrastructure/kubernetes | 22 | kustomize overlays, probes, HPA, NetworkPolicies |
| Zero-downtime deployments? | rolling update + expand/contract migrations | 22 | maxUnavailable 0, readiness, graceful consumer shutdown |
| Scale to millions of events? | §17 scale stages | 27, 30 | partitions, batch consumers, audit to log store |
| Integrate with existing Java services? | service-catalog, ADR-005 | 20 | OpenAPI-generated clients + events via outbox |
| Migrate from a monolith? | ADR-011 | 30 | strangler fig; extract by team/scale boundary |
| Multi-region deployment? | §17 [Ent] | 30 | active-passive first; data residency; Kafka replication |
| Disaster recovery? | RDS PITR, MSK replication | 30 | RPO/RTO per data class; audit is highest priority |
| LLM provider outage? | llm-gateway failover | 5, 28 | fallback provider; degraded mode; never silent quality drop |
| Is an AI recommendation trustworthy? | confidence rubric, critic, validation, override rate | 11, 25 | evidence count/independence; calibration; human decides |
| Evidence vs hypothesis? | Fact/Hypothesis/Recommendation types | 10 | separate types in schema and UI |

Behavioural angles to prepare: "a time you cut scope" (C1–C12 in architecture.md §0), "a decision you'd reverse" (ADR-011).
