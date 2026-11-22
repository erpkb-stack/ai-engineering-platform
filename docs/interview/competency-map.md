# Interview competency map (Phase 31a, rewritten 2026-11-21 against the code)

Target role: **AI Solutions Architect**: decisions, tradeoffs, integration, cost, risk, with AI
depth behind them. Every row was checked in the code or a result file on 2026-11-21.
Status: **built** (runs, has a test or a result file) / **partial** (some of it runs; the gap is
named) / **not built** (design only: say so in the interview, never imply it runs).

## Corrected: the old map claimed these. All FALSE.
| Old claim | The truth | Proof |
|---|---|---|
| HPA / KEDA scaling on Kafka lag | 1 replica per service; no HPA object anywhere. 1 replica is a REQUIREMENT (resume has no lease, rate limits are in-process) | `scripts/k8s_manifests.py:491`, ADR-034 "Replicas" |
| LangGraph `interrupt()` for approvals | Approvals are separate HTTP requests; the investigation ends first | `services/orchestrator/src/aeoi_orchestrator/actions.py:12`, ADR-026 |
| "Deterministic checks before the LLM judge" | There is no LLM judge. Validation is code only | `services/orchestrator/src/aeoi_orchestrator/validation.py:3` |
| An evaluation service | An offline job (`make eval-*`, `eval` schema); the stub was removed | ADR-048, Makefile `eval-*` |
| Kafka retry topics | None. In-consumer retry with backoff, then DLQ (a retry topic reorders a partition) | ADR-029 lines 81-83 |
| A copilot single-agent path | Not built. Only a `copilot` identity in the tool-gateway policy | `services/tool-gateway/config/agents.yaml:12,43` |
| kustomize overlays | No kustomize. Manifests are generated from `docker compose config` | `scripts/k8s_manifests.py`, ADR-034 |
| "11 services" | 9 run; 2 stubs were never built and were removed | `ls services`, ADR-048 |

## A. Architecture decisions
| Question | One-line answer | Proof | Status |
|---|---|---|---|
| Why 9 services, Kafka, k8s and Java for one developer? | Each one shows a pattern (contract-first integration, outbox -> consumer, least-privilege netpol), not load; I removed 2 stubs and named what I would merge | ADR-011, ADR-048 | built |
| What would you build at a real company first? | A modular monolith + Postgres + one worker; split only on a team or scale boundary | ADR-048 Alternatives | not built (opinion) |
| Why LangGraph? | Checkpoint after every step + fan-out (`Send`); a lost DB waits and resumes from the checkpoint; isolated to the orchestrator | `graph.py:10`, `engine.py:126`, ADR-001/019 | built |
| Why multi-agent, not one agent? | Specialists with their own tool sets (least privilege); facts by code, the LLM only labels; an independent critic | `agents.yaml`, ADR-018/020/021 | built |
| When NOT multi-agent? | Simple Q&A, tight latency or cost; one agent is easier to debug | - | not built (no single-agent path to compare) |
| Why Kafka for one consumer? | Kept as the outbox -> idempotent consumer -> DLQ/replay pattern; ONE flow (`incident.lifecycle` -> audit) | ADR-048 §3, ADR-029 | built |
| Why approvals as separate requests, not `interrupt()`? | A human can take an hour; `interrupt` keeps the run and a delegated grant alive that long | ADR-026 | built |
| Integrate with an existing Java service? | Contract-first OpenAPI; the Java catalog sits behind the same tool; a parity check against the JSON source | ADR-032, `scripts/phase20_parity.py` | built |
| Postgres for vectors and graph? | One store, ACL in the same SQL; pgvector + FTS hybrid | ADR-002/012/016 | built |
| Migrate from a monolith? | Strangler fig; extract by team/scale boundary | ADR-011 | not built |
| Multi-region / DR? | Active-passive first; RPO/RTO per data class; audit has the highest priority | architecture.md §17 | not built |

## B. AI quality and trust
| Question | One-line answer | Proof | Status |
|---|---|---|---|
| How do you prevent hallucination? | You cannot prevent it; you constrain and detect it: facts come from code, claims must cite evidence ids, validation is code, a critic can weaken a claim | `validation.py`, ADR-018/021 | built |
| Evidence vs hypothesis? | Separate types in the schema, the API and the UI (evidence graph) | ADR-021, ADR-046 | built |
| How do you evaluate it? | Planted causes, exact scoring, a CI regression gate on fake models; one paid real-model run | ADR-037, phase-25.md §5a, §14 | built |
| How good is it? | Planted scenarios only (an upper bound): VALIDATED 9/11 right (Wilson 0.52-0.95); rollback proposals 5 correct, 0 wrong, 2 missed | phase-25.md §5c, §14 | built |
| How fast vs a human? | AI only: 14/17 identified (Wilson 0.59-0.94), median 35.7 s. NO valid human baseline: I declared mine invalid | `data/mtti/results/mtti-2026-10-29T155548.json`, ADR-047 amendment | built (AI side only) |
| How do you evaluate RAG? | Retrieval measured apart from generation; keyword vs vector vs hybrid on labelled history | `make history-eval`, ADR-016/023 | built |
| How do you evaluate a model change? | The same planted set and exact scorer; stored runs re-scored for free | `make eval-rescore`, `make eval-calibration` | built |
| Is an AI recommendation trustworthy? | Only VALIDATED claims are stood behind; calibration per status with Wilson bands; a human approves any action | phase-25.md §5c, ADR-026 | built |
| How do you version prompts? | `prompt_version` stored on every model call | `services/llm-gateway/src/aeoi_llm/usage.py:50` | built |
| Removed model text? | Kept and shown apart, never silently dropped | ADR-045 | built |

## C. Security
| Question | One-line answer | Proof | Status |
|---|---|---|---|
| How do you secure tools? | Tool gateway: user ∩ agent permissions; agents are read-only; only `action_executor` can act | `agents.yaml`, ADR-008/017 | built |
| How do you bind an approval? | Action + sha256 of the validated args + incident + approver; consumed once in the same transaction; runs AS the IC | ADR-026 §B | built |
| Who can approve? | IC only, never the requester (DB CHECK); ENGINEER gets 403 | ADR-026 §A, `tests/demo/demolib.py:247` | built |
| Prevent unauthorized retrieval? | ACL in the retrieval SQL (`allowed_groups && :user_groups`); groups from the verified JWT | ADR-006, `tests/integration/rag/test_rag.py:93` (leakage matrix = 0) | built |
| Prompt injection? | Capability limits first (read-only agents, approval for actions); untrusted text is wrapped (`aeoi_security.untrusted`) and flagged; the flag is heuristic | ADR-008, ADR-038, `reasoning/hypothesis.py:73` | partial (detection is heuristic) |
| PII? | Scrubbed at ingest, in tool output, in traces and before every hosted model. Names/addresses NOT caught (no NER, accepted) | phase-26.md, ADR-038 | partial |
| Network isolation? | Default-deny NetworkPolicy, tested both ways (allowed calls work, forbidden calls fail) | phase-29.md:88 (122 passed, 16 skipped, 0 failed) | built |
| Alert intake? | Signed webhook; triage in code | ADR-044 | built (rate limit charged before the signature: open) |
| Threat model? | A tested index: every row names a test or says "gap"; a unit test enforces it | `docs/security/threat-model.md`, `tests/unit/test_threat_model.py` | built |
| Kafka security? | PLAINTEXT, no ACLs | ADR-048 §5 | not built |

## D. Reliability and scale
| Question | One-line answer | Proof | Status |
|---|---|---|---|
| Idempotency? | Outbox + idempotent consumer (ON CONFLICT on the event id) + Idempotency-Key at the api. At-least-once; never claim exactly-once | `aeoi_tools/relay.py`, `aeoi_audit/consumer.py`, `aeoi_incident/idempotency.py` | built |
| Kafka failures? | The outbox keeps events while the broker is down; poison -> DLQ at once; replay max 3 | ADR-029, phase-28.md (kafka stopped 60 s: PASS) | built |
| DB failures? | Readiness gates the START only; after that 503 + Retry-After, no dropped connections | ADR-041 (61 -> 0 dropped) | built |
| LLM failures? | Timeout, retry, failover; an open circuit fails over at once; the schema path of a failure is stored | ADR-042, `aeoi_llm/resilience.py` | built |
| How do you test failure? | Chaos rules written BEFORE the run; a FAIL is a finding, never a reason to change a rule | ADR-040, `tests/chaos/chaoslib.py` | built |
| How do you scale agent workers? | Today I don't: 1 replica is a requirement. Next: resume lease, Redis limits, then replicas | ADR-034 "Replicas", ADR-031 | not built |
| What is your capacity? | No number is quotable: one laptop, 1 replica, two identical runs differed ~10x; reads bend at 10-25 rps | ADR-039, phase-27.md | built (and that IS the answer) |
| Zero-downtime deploys? | Rolling: maxUnavailable 0 / maxSurge 1, readiness, preStop 5 s | phase-22a.md:64 (0 failed requests) | built |
| Monitor / debug agents? | OTel trace per investigation; per-model usage rows; resume from the checkpoint | ADR-035, ADR-010 | built |
| LLM cost? | Per-model pricing, a $2.00 budget per investigation, an exact-match cache (temperature 0, primary model only); measured $0.98 for 16 cases | `services/llm-gateway/config/routing.yaml`, `aeoi_llm/cache.py`, phase-25.md §5a | built |

## E. Delivery
| Question | One-line answer | Proof | Status |
|---|---|---|---|
| CI? | 6 jobs per PR; images scanned and gated; actions SHA-pinned; no deploy from CI | ADR-036 | built |
| How do you know a phase is done? | A Verify list with result files; a gate rule fixed before the runs | phase-29.md §8 | built |
| The demo? | A checked script: 16 checks on the deployed system; every run committed, FAILs too | ADR-043, `data/demo/results/` | built |
| Reviewing your own work? | Independent subagent reviewers, every finding re-checked; one reviewer claim was false | phase-30.md §3 | built |
| A time you were wrong? | Cost 3x too low; a per-step timeout; an invalid human baseline (all in the story bank) | `story-bank.md` | built |
