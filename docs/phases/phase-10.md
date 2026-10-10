# Phase 10 — Multi-agent investigation

## 0. Gate and scope challenge
- **Gate: NOT passed when this was built.** Phase 9 Verify (§10 of phase-09.md) is still open on
  the Mac: the crash/resume check, `make agent-smoke`, `make test-integration`. Built in the
  sandbox on top of Phase 9. **Do not apply or merge before those pass and Phase 9 is merged.**
- **Challenge 1 — six specialists?** No. Code = Phase 14, Historical = Phase 13. Triage is
  unnecessary: the incident record already names its services and time. Built: **Metrics,
  Deployment, Knowledge** next to Log Analysis.
- **Challenge 2 — "parallel agents are faster" (architecture.md §9).** False on your Mac for LLM
  work: Intel-CPU Ollama answers one call at a time (60–110 s each). So the three new agents are
  **code only** — no model call. Anomaly maths, deploy timing and retrieval are arithmetic; an
  LLM would only reword numbers, more slowly. The model earns its place in Phase 11 (hypotheses).
- **Challenge 3 — what if one agent fails?** COMPLETE hides a missing source; FAILED throws
  away good evidence. New status **PARTIAL** (migration 0019).
- **Found while planning (two leaks):** (1) `GET /incidents/{id}/evidence` showed raw log lines
  to anyone with `incidents:read` — a Phase 8 gap, the same one the Phase 9 review closed only
  for the trace. (2) Knowledge results stored on the incident would leak group-restricted docs.
  Fixed: evidence readable per kind; knowledge evidence = pointers only.
- **Cut:** Triage, Code, Historical agents; LLM planner; seasonality-aware baselines; the
  phase-08 §10c prompt follow-ups (example leak, `not_found` category) — still open.

## 1. Objective
One investigation = four agents in parallel (log_analysis, metrics, deployment, knowledge), each
through the tool-gateway as the user (delegated token), each citing stored evidence; outcome
COMPLETE / PARTIAL / FAILED; resume after a crash re-runs only unfinished agents.

## 2. Business reason
MTTR (architecture.md §4): the on-call engineer's first 15 minutes are "what changed, what moved,
what does the runbook say". This phase answers all three with timestamps and evidence, in about a
second for the code agents. Trust: negative facts ("traffic did not move") are stated, not implied.

## 3. Architecture
```
plan (code: every configured agent) ──Send──► run_agent(log_analysis)  LLM labels (Phase 8)
                                      ├──────► run_agent(metrics)       query_metrics ×≤18, robust z
                                      ├──────► run_agent(deployment)    get_deployment, get_config_diff
                                      └──────► run_agent(knowledge)     search_runbooks, search_docs
                                                  │ tool-gateway ──► rag: Bearer <gateway rag:obo>
                                                  │                     X-On-Behalf-Of <delegated>
            collect_evidence ◄────────────────────┘   (groups = the USER's, in SQL)
            finalize: COMPLETE | PARTIAL | FAILED  (revoke grant first)
deadline: post finished tasks' evidence (salvage) → PARTIAL, live tasks TIMED_OUT
```
| Guarantee | Enforced by | Test |
|---|---|---|
| One failed agent → PARTIAL, siblings' evidence kept | `graph.outcome`, per-branch records | `test_one_failed_agent_makes_the_run_partial_not_failed` (mutation-checked) |
| Deadline keeps finished work | `engine._deadline` + `salvage.py` | `test_deadline_keeps_what_finished_partial_not_failed` (mutation-checked) |
| Crash: finished siblings not re-called | LangGraph pending writes (+ Phase 9 reuse guard) | `test_crash_while_one_agent_hangs_resumes_only_that_agent` |
| rag filters by the on-behalf-of USER's groups | `require_acting_user` | `test_obo_uses_the_users_groups_not_the_services` (mutation-checked) |
| Delegated token: search only, never a bearer, never whole documents | same, `allow_delegated=False` | `test_obo_refusals`, `test_delegated_tokens_cannot_open_whole_documents` |
| Evidence readable per kind | `EVIDENCE_KIND_PERMS`, SQL filter | `test_evidence_kinds_need_the_tools_permission` (mutation-checked) |
| Knowledge stores no doc text or title | pointers only | unit + e2e + smoke (mutation-checked) |
| Trace content per agent's permissions | `AGENT_CONTENT_PERMS` | same e2e (mutation-checked) |
| Facts are true statements | code; review-driven unit tests | `test_single_spikes_are_never_called_within_baseline` etc. |

[P] fixed thresholds, all agents every run. [Prod] seasonality baselines, metric catalog per
service, cost-aware plan. [Ent] per-tenant agent sets, reference filtering per reader.

## 4. Files
```
docs/adr/ADR-020-multi-agent-code-first-specialists.md  (+ notes in ADR-019, README)
libs/models    api/agents.py: AgentTask (LogAnalysisTask alias), MetricAnomaly, DeployChange,
               KnowledgeRef; api/investigations.py: agents subset, 24 h window check
libs/security  rbac.py: EVIDENCE_KIND_PERMS, may_read_evidence
libs/web       auth.py: require_acting_user(perm, obo_scope, allow_delegated)
libs/db        alembic 0019 (PARTIAL); models/orchestrator.py
services/agents        common.py, metrics/{anomaly,agent}.py, deployment/agent.py,
                       knowledge/agent.py; api.py /v1/agents/{agent}/run; config budgets
services/orchestrator  graph (4 agents, outcome, bad task fails its branch), engine (deadline
                       salvage), salvage.py, store (repost → COMPLETE|PARTIAL), api (agents,
                       redaction per agent), config `agents`, __main__ `investigate`
services/rag           api/config/main: OBO path, delegation key
services/tool-gateway  rag adapter always OBO (rag:obo token); sanitize: UUIDs skip PII scrubber
services/incident-service  evidence filtered by kind permissions
scripts/smoke-phase10.sh (new); smoke-phase9.sh (needs rag now, picks log_analysis by name)
tests  agents unit (20 new), orchestrator unit (outcome), integration: agents/test_multi_agent_e2e.py (7),
       rag/test_rag_obo.py (10), db 0019 downgrade, tools OBO headers, sanitize UUID
Makefile  tools-tokens (+ rag:obo), investigate, multi-smoke
```

## 5–6. Commands (macOS, zsh) — only after Phase 9 is merged
```zsh
cd ~/projects/ai-engineering-platform
git switch main && git pull                  # must include the Phase 9 merge
git switch -c phase-10-multi-agent
tar xzf ~/projects/_to_delete/phase10.tgz
uv sync --all-packages
make up && make db-upgrade && make db-check  # 0019
make tools-tokens                            # NEW: secrets/tools_rag_token.txt (rag:obo)
make check && make test-integration
```

## 7. Config
`AEOI_ORCH_AGENTS` (default all four). `AEOI_AGENTS_METRICS_*`: `max_tool_calls` 18,
`baseline_min` 120, `z_threshold` 4, `min_shift_buckets` 3, `max_facts` 20.
`AEOI_AGENTS_DEPLOY_*`: `lookback_h` 6, `max_deploys_per_service` 5, `max_config_diffs` 6.
`AEOI_AGENTS_KNOWLEDGE_*`: `runbooks_k` 3, `docs_k` 3. `AEOI_TOOLS_RAG_TOKEN_FILE`,
`AEOI_RAG_DELEGATION_PUBLIC_KEY_FILE` (absent = rag refuses delegated tokens).

## 8. Tests
- `make check`: 550 passed (unit; fake tools, no IO). Integration: 183 passed, 1 skipped.
- Mutation checks (test fails when the guard is removed): rag OBO user groups, evidence kind
  filter, knowledge pointer-only, PARTIAL outcome, deadline salvage, trace redaction per agent,
  UUID scrubber bypass. **One survived on purpose:** removing run_agent's reuse guard does not
  fail the 4-branch crash test — LangGraph's pending writes keep finished siblings; the guard
  is pinned by the Phase 9 test (re-checked: it still fails without the guard).
- Independent review: 3 high, 4 medium, 6 low. 9 fixed (7 with a regression test; the window
  clamp, "last bucket" wording and the bad-task branch have none), 3 documented, 1 no change
  (ADR-020 "Review").
- Found by the live run, not by tests or review: the PII scrubber turned a digit-only UUID
  into `[CARD]…`, so the knowledge agent crashed on the stub's document id.

## 9. Run (one terminal each)
```zsh
make dev ; make run-llm ; make run-rag ; make run-tools ; make run-audit ; make run-agents ; make run-orch
make multi-smoke                       # Phase 10 product path, 4 agents
make investigate INCIDENT=INC-10001    # human-readable output per agent
make orch-smoke && make agent-smoke    # Phase 9 / 8 regressions (orch-smoke now needs run-rag)
```

## 10. Verify (gate for Phase 11)
- [ ] Phase 9 Verify ticked first (phase-09.md §10)
- [ ] `make multi-smoke` all ✔ (26 checks)
- [ ] **Crash + resume with four agents, by hand:**
      ```zsh
      make investigate INCIDENT=INC-10001 NOCACHE=1     # terminal A; llama gives you ~60 s
      # terminal B (run-orch): Ctrl-C as soon as A prints "started investigation", then:
      make run-orch
      make agent-status INCIDENT=INC-10001
      ```
      Expect `COMPLETE … log_analysis=SUCCEEDED(x2)` and the three code agents `SUCCEEDED`
      (x1 if they had finished before the kill — they take < 1 s — else x2).
- [ ] Stop rag (`make stop-rag`), run `make investigate INCIDENT=INC-10001`: expect `PARTIAL`,
      `missing sources - knowledge: …`, and the other three agents' facts.
- [ ] `make orch-smoke`, `make agent-smoke`, `make test-integration` green

Measured here (cloud sandbox, fake model, no latency claims): smoke 26/26; Phase 9 smoke
18/18 and Phase 8 smoke 14/14 still pass; live `kill -9` of the orchestrator with all four
agent calls frozen (SIGSTOP on the agents worker) → restart → COMPLETE, every task x2, one
refresh-issued token, tool calls doubled by the orphaned calls (the documented tradeoff).
On the planted demo: p95 latency shift 09:49Z → pool utilisation 09:50Z → 5xx 09:51Z →
thread pool 09:52Z; traffic and CPU steady; DEPLOY-4821 started 09:42Z with
`order_batching false → true`.

## 11. Failure scenarios
| Symptom | Cause | Fix |
|---|---|---|
| PARTIAL `knowledge: no knowledge data: search_runbooks unavailable` | rag not running | `make run-rag` |
| knowledge `503 … need the gateway's rag token` | no `secrets/tools_rag_token.txt` | `make tools-tokens`, restart `run-tools` |
| knowledge 403 from rag | rag has no delegation key, or old rag process | `make delegation-keys` (once), restart `run-rag` |
| metrics degraded `failed: …/… (rate_limited)` | > 20 `query_metrics` per user per burst (two runs in a minute) | wait a minute; by design, the series are named |
| metrics note `not judged - baseline too short` | no data before the window | expected for new services |
| `orch-smoke` fails on `rag not running` | Phase 10 product path runs knowledge | `make run-rag` |
| status PARTIAL `deadline exceeded - missing sources: x: TIMED_OUT` | one agent slower than 900 s | finished agents' evidence was kept; investigate x |
| `reasoning` route → 400 "provider rejected the request" | `claude-sonnet-5-5` rejects `temperature` (found on the Mac, Phase 11 preflight) | fixed: `supports_temperature: false` in routing.yaml; the gateway omits it for that model |
| 400 `agents not enabled on this orchestrator` | asked for an agent not in `AEOI_ORCH_AGENTS` | set the env var or drop it |

## 12. Production considerations
Seasonality-aware baselines (same hour last week) and calibrated thresholds (Phase 25); a metric
catalog per service instead of a blind 9-metric sweep; a revoked-grant check at the gateway
(still the ≤ 5 min window, ADR-019); reference filtering per reader so pointers don't reveal
existence; workload identity for the gateway → rag hop; Redis rate limits (Phase 19).

## 13–15. Interview
`docs/interview/phase-10-multi-agent.md`.
