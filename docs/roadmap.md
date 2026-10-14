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
| 19 | Redis | **deferred to Phase 22 (ADR-031)**: nothing needs it before a service has 2 replicas; idempotency and locks stay in Postgres |  |
| 20 | Java Service Catalog | Spring Boot service + Flyway + outbox; Python client generated from OpenAPI; contract test | service-catalog |
| 21 | Docker | multi-stage non-root images; compose profiles; secrets; resource limits; healthchecks | evaluation, knowledge-service images |
| 22 | Kubernetes | kind cluster; kustomize overlays; probes; HPA; NetworkPolicies; zero-downtime rollout demo; **Redis (ADR-031): shared token bucket (Lua) for api + tool-gateway, shared LLM cache, Redis-down tests (fail open for reads, closed for actions), 2-replica shared-limit test** |  |
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
- [x] Phase 5: LLM Gateway
- [x] Phase 6: RAG pipeline (baseline + sweep on the Mac; defaults adopted in ADR-016)
- [x] Phase 7: Tool Gateway
- [x] Phase 8: First AI agent — verified on the Mac 2026-10-10 (`make agent-smoke` 15/15, `make test-integration` 189 passed); was: done when `make agent-smoke` all ✔ and `make test-integration` green on the Mac
- [x] Phase 9: LangGraph orchestration — verified on the Mac 2026-10-10 (`make orch-smoke` 19/19; kill + resume: log_analysis SUCCEEDED(x2), siblings x1, COMPLETE); was: built (orchestrator service, checkpoints, resume, delegation tokens); done when `make orch-smoke` + the crash/resume check pass on the Mac
- [x] Phase 10: Multi-agent investigation — verified on the Mac 2026-10-10 (`make multi-smoke` 26/26; 4-agent kill + resume COMPLETE, only the interrupted agent re-ran); was: built (metrics/deployment/knowledge code-only agents, PARTIAL, rag OBO, evidence by kind; ADR-020); done when `make multi-smoke` all ✔ + the 4-agent crash/resume check pass on the Mac (after Phase 9 is ticked)
- [x] Phase 11: Critic + Validation — verified on the Mac 2026-10-10 (`make hypo-smoke` 24/24 with real Sonnet critic, integration 189 passed, first real run → eval case #1; PR #11 merged); was: built (code candidates + rubric, Haiku ranks, Sonnet critic without fallback, deterministic validation, hypotheses on the incident; ADR-021); done when `make hypo-smoke` all ✔ on the Mac with a real Sonnet critic (after Phase 10 is ticked)
- [x] Phase 12: Incident timeline + postmortem — verified on the Mac 2026-10-10 (`make pm-smoke` 22/22 with real Sonnet; postmortem read by hand: judgement errors → eval cases; MANAGER view leaks nothing; kill + resume COMPLETE with postmortem x1; integration 194 passed); was: built (code timeline in incident_events, Sonnet narrative checked by code, read-time assembly redacted per reader; ADR-022); done when `make pm-smoke` all ✔ on the Mac with real Sonnet and the postmortem is read by a human (after Phase 11 is ticked)
- [x] Phase 13: Historical incident intelligence — verified on the Mac 2026-10-11 (`make history-smoke` 23/23 + 1 note, `make pm-smoke` 22/22, `make hypo-smoke` 24/24, real Haiku + Sonnet; `make history-eval` symptom-only hit@1 hybrid 0.917 vs keyword 0.778, paired 5-0, p≈0.06, leaks 0); was: built (hybrid incident search with the ACL in both branches, historical_incident agent: shared incidents only, Haiku claims checked by code, INCIDENT pointers, history prior as a tie-break only; ADR-023); done when `make history-smoke` all ✔ on the Mac, `make history-eval` has run and its limits are read, and integration is green
- [x] Phase 14: Code intelligence — verified on the Mac 2026-10-11 (`make code-smoke` 21/21, `make history-smoke` 23/23 + 1 note, `make pm-smoke` 22/22, `make hypo-smoke` 24/24, integration 223 passed + 1 skipped; code index 30 repos / 302 files, 0 parse errors); was: built (synthetic repos + real diffs, `aeoi_codeparse` tree-sitter rules, rag code index with file ACL in SQL, `compare_commits` / `find_references`, code_intelligence agent CODE ONLY, one gated source for the deploy; ADR-024)
- [x] Phase 15: Release risk analysis — verified on the Mac 2026-10-12 (`make risk-smoke` 19/19, `make risk-scan` 6/6 labelled: 2 HIGH = the planted changes, 102 MEDIUM only from unchecked first deploys, 1,680 LOW; after fix1 `make code-smoke` 21/21, `make hypo-smoke` ✔, `make pm-smoke` 22/22; integration 228 passed + 1 skipped); was: built (advisory release risk for a PR / commit range / deploy, code only, findings vs coverage, way back incl. flag_not_read; fix1: model free text cut in code, never rejected by a tight schema; ADR-025)
- [x] Phase 16: Human approval — verified on the Mac 2026-10-12 (`make approval-smoke` 23/23; an investigation proposed the DEPLOY-4821 rollback for its starter, read by a human and REJECTED by another IC; `make code-smoke` 21/21, `make risk-smoke` 19/19; integration 243 passed + 1 skipped; fix1: migration downgrade keeps the shared svc_cd role); was: built (proposal by code rule or SRE request; an IC who is not the requester decides; the action runs in the approver's own request AS the approver via the action_executor identity; the gateway consumes the approval at incident-service (action + args hash + incident + approver, once) and records the intent first; simulated CD record through an INSERT-only login; ADR-026)
- [x] Phase 17: React frontend — 17a (Keycloak OIDC, ADR-027) and 17b (operations console, ADR-028) verified on the Mac 2026-10-13 (17b: confirmed by the owner), merged as PR #19
- [x] Phase 18: Kafka / distributed — 18a verified on the Mac 2026-10-14, merged as PR #20 (consumer core: DLQ, replay, lag, readiness probe; audit consumes incident.lifecycle; ADR-029); 18b NOT built by owner decision after a cost check: agent tasks stay on HTTP (ADR-030)
- [x] Phase 19: Redis — closed without code 2026-10-14: deferred to Phase 22 by owner decision (ADR-031)
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
