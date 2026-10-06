# AEOI Roadmap — 31 phases with verify gates

Rule: a phase is ticked only after it runs on the owner's Mac and its Verify item is demonstrated. Use `/phase N`.

| # | Phase | Verify gate | New services |
|---|---|---|---|
| 1 | Requirements & architecture | architecture.md + ADR-001..012 reviewed; challenges C1–C12 accepted/rejected | — |
| 2 | Repository & dev environment | `make doctor` green; uv workspace; compose `infra` profile healthy (pg+pgvector, redis, kafka); libs skeleton; pre-commit | — |
| 3 | Database & data model | Alembic migrations per schema; `alembic upgrade head` + downgrade round-trip; index comments; `GRANT USAGE ON SCHEMA` to `aeoi_readonly` (role exists since Phase 2); synthetic data seed v1 | incident, rag, audit schemas |
| 4 | FastAPI backend | api + incident-service: incidents CRUD, idempotency, problem+json, JWT dev issuer, RBAC deps, health; in-process event bus with Kafka-shaped interface | api, incident-service |
| 5 | LLM Gateway | Provider interface (generate/structured/stream/embed); Claude + Ollama + Fake; routing config; usage + cost table; ADR-014 | llm-gateway |
| 6 | RAG pipeline | ingest md/pdf/txt/html/json/code; hybrid search + RRF + rerank; permission filter; leakage test = 0; recall@k baseline measured | rag |
| 7 | Tool Gateway | registry + contracts for 11 tools over simulated data; authz ∩ allow-list; audit; security tests | tool-gateway, audit (minimal) |
| 8 | First AI agent | Log agent end-to-end with fake + real LLM; evidence ids; trace row | agents |
| 9 | LangGraph orchestration | graph with triage→plan→1 agent→report; Postgres checkpoint; kill/resume test | orchestrator |
| 10 | Multi-agent investigation | parallel fan-out to 6 agents; reducers; budgets; partial-failure handling |  |
| 11 | Critic + Validation | critic loop guard; deterministic citation checks + LLM judge; confidence rubric; ADR-013 |  |
| 12 | Incident timeline + postmortem | TimelineBuilder (code); postmortem generator with evidence refs |  |
| 13 | Historical incident intelligence | 500+ incidents embedded; similar-incident search with similarities/differences |  |
| 14 | Code intelligence | tree-sitter chunking; dependency + change-impact via graph edges | knowledge-service |
| 15 | Release risk analysis | PR/RC analysis report; advisory only |  |
| 16 | Human approval | interrupt/resume; approvals single-use + expiry + args hash; controlled tool execution; audit |  |
| 17 | React frontend | 12 screens; investigation workspace; evidence graph; trace waterfall; approval panel; Playwright e2e |  |
| 18 | Kafka / distributed | consumers with `processed_events` dedupe, retry topics, DLQ + replay tool, duplicate-delivery test, consumer-lag metrics (the producer side - outbox + relay + KafkaPublisher - shipped in Phase 4) |  |
| 19 | Redis | caches, rate limits, locks, idempotency store; Redis-down behaviour tests |  |
| 20 | Java Service Catalog | Spring Boot service + Flyway + outbox; Python client generated from OpenAPI; contract test | service-catalog |
| 21 | Docker | multi-stage non-root images; compose profiles; secrets; resource limits; healthchecks | evaluation, knowledge-service images |
| 22 | Kubernetes | kind cluster; kustomize overlays; probes; HPA; NetworkPolicies; zero-downtime rollout demo |  |
| 23 | Observability | OTel everywhere; collector; Prometheus/Tempo/Grafana dashboards |  |
| 24 | CI/CD | GitHub Actions CI + CD (GHCR, kind smoke deploy, envs dev/staging/prod) |  |
| 25 | MLOps / evaluation | eval datasets v1; runner; metrics; prompt A/B comparison with CIs; calibration of confidence | evaluation |
| 26 | Security hardening | threat model; injection/exfil suites; PII; secrets scan; dependency scan |  |
| 27 | Load testing | Locust/k6 at 10 / 100 rps on laptop; bottleneck report with measured numbers |  |
| 28 | Failure testing | Toxiproxy/chaos: LLM timeout, rate limit, Kafka/DB/Redis down, worker kill |  |
| 29 | End-to-end demonstration | scripted demo passes 3 times in a row from a clean `make up` |  |
| 30 | Architecture review | architecture-critic + security-reviewer full review; V2 proposal; merge decisions per ADR-011 |  |
| 31 | Interview preparation | competency map complete; mock interviews with interview-coach; resume bullets from measured metrics only |  |

## Checklist

- [x] Phase 1: Requirements & architecture
- [x] Phase 2: Repository & dev environment
- [x] Phase 3: Database & data model
- [x] Phase 4: FastAPI backend
- [ ] Phase 5: LLM Gateway — built; done when `make llm-smoke` passes on the Mac with a real key or Ollama
- [ ] Phase 6: RAG pipeline
- [ ] Phase 7: Tool Gateway
- [ ] Phase 8: First AI agent
- [ ] Phase 9: LangGraph orchestration
- [ ] Phase 10: Multi-agent investigation
- [ ] Phase 11: Critic + Validation
- [ ] Phase 12: Incident timeline + postmortem
- [ ] Phase 13: Historical incident intelligence
- [ ] Phase 14: Code intelligence
- [ ] Phase 15: Release risk analysis
- [ ] Phase 16: Human approval
- [ ] Phase 17: React frontend
- [ ] Phase 18: Kafka / distributed
- [ ] Phase 19: Redis
- [ ] Phase 20: Java Service Catalog
- [ ] Phase 21: Docker
- [ ] Phase 22: Kubernetes
- [ ] Phase 23: Observability
- [ ] Phase 24: CI/CD
- [ ] Phase 25: MLOps / evaluation
- [ ] Phase 26: Security hardening
- [ ] Phase 27: Load testing
- [ ] Phase 28: Failure testing
- [ ] Phase 29: End-to-end demonstration
- [ ] Phase 30: Architecture review
- [ ] Phase 31: Interview preparation
