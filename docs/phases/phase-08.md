# Phase 8 — First AI agent (Log Analysis)

## 0. Gate and scope challenge
- **Gate:** Phase 7 merged (PR #6). Your Mac eval after the Phase 6 close-out:
  `rag-2026-10-06T213425.json` hybrid MRR@10 **0.785** (CI 0.72–0.85), R@5 0.935, `valid: true`.
- **Challenge 1 — "an AI agent" is mostly not AI.** Counting errors, finding the first and last
  line and grouping messages are code. If the LLM did them, a 3B model would miscount and invent
  times, and nobody could audit it. So the LLM does one thing: name and categorise clusters.
  Facts are built by code and cite real evidence ids. This is the pattern for every later agent.
- **Challenge 2 — the trace row needs an owner.** `agent_executions` belongs to the orchestrator,
  which arrives in Phase 9. You chose a **thin orchestrator runner** now (no temporary grants).
- **Challenge 3 — "fake + real LLM".** You chose to compare **both** real models. One incident is
  an anecdote, not an eval: `make agent-compare` records latency, tokens, cost and how much model
  output survived validation, with `n=1` in the file. Don't present it as a quality score.
- **Cross-phase changes:** tool-gateway evidence ids are now `LOG-<tool_call>-<n>` (ADR-017
  amended) so findings can cite them as-is; `search_logs` returns exact totals; migration 0017
  adds evidence kind `CATALOG`; incident-service gets a service-only evidence write endpoint.

## 1. Objective
One agent end to end: incident → runner → agent → tool-gateway (as the user) → LLM gateway →
facts with evidence ids → evidence rows (incident-service) + trace rows (orchestrator).

## 2. Business reason
Engineers spend the first 10–20 minutes of an incident grepping logs: which errors, since
when, how many. The agent does that in seconds and shows its evidence, so the engineer
checks a claim instead of producing it. It also sets the standard every later agent must meet:
a claim without a log line behind it doesn't ship.

## 3. Architecture
```
make agent-run INCIDENT=INC-n ── user JWT ──► orchestrator runner (orch_svc)
  1 GET /v1/incidents/INC-n as the user ─────────────────► incident-service
  2 T1 investigation RUNNING + task RUNNING                (orchestrator schema)
  3 POST /v1/agents/log_analysis/run  agents:run + X-On-Behalf-Of ─► agents :8003
        search_logs asc + desc  service:agents + OBO ─► tool-gateway (records, audits, sanitises)
        dedupe → cluster → FACTS (code, exact counts, provable times)
        ONE structured call (route fast|local) ─► llm-gateway ─► label clusters
        validate (unknown ids / foreign citations dropped) │ fail → rule labels + degraded
  4 T2 agent_execution + messages + task status            ("trace row")
  5 POST /v1/incidents/{id}/evidence  evidence:write ─► incident-service (idempotent) + timeline
  6 T3 investigation COMPLETE | FAILED + spent_usd
```
| Guarantee | Enforced by | Test |
|---|---|---|
| Every fact cites evidence that is stored | code facts + e2e check | `test_log_agent_end_to_end_*` |
| Model output can't change facts | facts built before the LLM call | `test_llm_down_degrades_*`, `test_facts_do_not_depend_*`, compare asserts equal facts |
| Forged citations / unknown clusters dropped | `_label` validation | happy-path test (mutation-checked) |
| Log text (incl. error_code) stays untrusted | `wrap_untrusted` | `test_error_code_text_stays_inside_*` (mutation-checked) |
| No permission → FAILED, not a guess | tool-gateway ∩ + agent | `test_user_without_logs_permission_*` |
| No RUNNING rows left behind | `_abort` + stale heal | `test_runner_crash_*`, `test_one_running_*` |
| Evidence POST failure recoverable | batch kept in output | `test_evidence_post_failure_*` + `repost-evidence` |
| Evidence rows are consistent | sha/key/tool-call checks | `test_evidence_write_is_service_only` |
| "first seen" only when provable | sample logic | unit tests + review |

## 4. Files
```
libs/tool-client/            aeoi_tool_client: ToolClient, HttpToolClient, FakeToolClient (new)
libs/models/.../agents.py    LogAnalysisTask, AgentRunResult, EvidenceItem/Batch, trace models
services/agents/             prompts/log_analysis/v1.md; src/aeoi_agents/{api,main,config,trace,prompts}.py,
                             log_analysis/{agent,clustering}.py; tests/test_log_agent.py
services/orchestrator/       src/aeoi_orchestrator/{runner,config,__main__}.py (thin runner + CLI)
services/incident-service/   POST /v1/incidents/{ref}/evidence + add_evidence()
services/tool-gateway/       evidence ids KIND-<call>-<n>; exact totals in search_logs
libs/db/                     0017 evidence kind CATALOG; users.py orch_svc
tests/integration/agents/    conftest.py, test_log_agent_e2e.py      scripts/smoke-phase8.sh
docs/adr/ADR-018 (+ ADR-017 amendment), this file, docs/interview/phase-08-first-agent.md
```

## 5–6. Commands (macOS, zsh)
```zsh
cd ~/projects/ai-engineering-platform
git switch main && git pull && git switch -c phase-8-first-agent
# extract the delivered files (see chat), then:
uv sync --all-packages
make up && make db-upgrade && make db-check        # 0017
make db-users                                       # adds orch_svc
make agents-tokens                                  # agents + orchestrator service tokens
make check && make test-integration
```

## 7. Config
`AEOI_AGENTS_LOG_ROUTE` (default `fast`), `AEOI_AGENTS_LOG_LLM_TIMEOUT_S` (90),
`AEOI_AGENTS_LOG_TASK_DEADLINE_S` (150), `AEOI_ORCH_WINDOW_BEFORE_MIN` / `_AFTER_MIN` (60/30),
`AEOI_ORCH_TASK_DEADLINE_S` (180). Prompt: `services/agents/prompts/log_analysis/v1.md`.

## 8. Tests
- Unit: 15 agent tests (clustering, citation validation, LLM down, denied tool, budget, notes cap,
  failed service, injection escaping, fair cluster selection, UTC, prompt pin).
- Integration (real Postgres, 4 least-privilege logins, real gateways, fake model): 10 e2e tests.
- An independent review (read-only subagent) found 11 issues; all fixed with tests, incl.
  >30 notes → HTTP 500; crashes leaving RUNNING rows; evidence cited but never stored after a
  failed POST; `search_logs` totals understated with >50 codes; a failed service silently
  missing; `error_code` outside the untrusted wrapper; no overall task deadline.
- Found by the smoke run: fact times printed 5 hours wrong (−05:00 with a literal `Z`).
- Found on the Mac: a gateway cache hit looked like a 567 ms llama answer (trace now records
  `cached`; compare sends `cache=false`); compare preflight trusted `chain[0]` with no Claude key.
- "labels passed validation 5/5" checks ids and citations only, NOT meaning. The Mac llama run
  labelled `ERR_NOT_FOUND` as `dependency_timeout` and a "Health check ok" line as `auth`.

## 9. Run (one terminal each)
```zsh
# Claude key: as in Phase 5 (ANTHROPIC_API_KEY in this shell, or secrets/anthropic_api_key.txt)
make run-llm                        # routing.yaml: fast = Claude Haiku, local = llama3.2:3b
make run-tools ; make run-audit ; make run-agents ; make dev
make agent-smoke                    # creates a demo incident on checkout-api and runs the agent
make agent-compare INCIDENT=<key printed by the smoke>   # refuses if both routes are the same model
make agent-status INCIDENT=<key>   # what ran, how long, which model, why degraded
```

## 10. Verify (gate for Phase 9)
- [ ] `make agent-smoke` all ✔ (a ⚠ "no model labels" note means the model failed: read why)
- [x] `make agent-compare` prints `facts identical across models: True` and writes
      `data/eval/results/agent-log-compare-*.json` (commit it; it is labelled n=1)
      — done on the Mac: `agent-log-compare-2026-10-07T015416.json`, valid comparison
- [ ] `make test-integration` green

## 10b. Measured on the owner's Mac (Intel CPU, llama3.2:3b, prompt v2, INC-10001, `NOCACHE=1`)
Real runs, n=2. Not a benchmark.
| run | tokens in/out | agent latency | labels valid / correct |
|---|---|---|---|
| 1 | 518 / 225 | 54.6 s | 5/5 valid; **3/5 correct** (ERR_NOT_FOUND → `dependency_timeout`, "Health check ok" → `auth`) |
| 2 | 518 / 225 | 113.6 s | same labels (temperature 0: the errors are systematic, not random) |
- Same input and output size, 2× the time: CPU contention on the host, not the prompt.
- Run 2 finished **1.4 s under** the 115 s LLM cap. The local route is at the edge of its budget on
  this Mac. When it goes over, the agent degrades to rule labels with the reason (tested on the Mac:
  gateway down → `DEGRADED`, facts unchanged). We do not raise the cap: the gateway's request cap is
  120 s, and a bigger budget would only hide slow hardware.
- Facts were identical in every run (cached, degraded, and both real runs).

## 10c. Model comparison on the Mac (`agent-log-compare-2026-10-07T015416.json`, n=1)
Same incident, same prompt v2, same facts. One run each: an anecdote, not an eval.
| | llama3.2:3b (local CPU) | claude-haiku-4-5 (API) |
|---|---|---|
| LLM latency | 79.6 s | 2.3 s |
| tokens in/out | 518 / 225 | 1451 / 272 |
| cost | $0 | $0.0028 |
| schema repairs / citations dropped | 0 / 0 | 0 / 0 |
| labels that passed validation | 5/5 | 5/5 |

Labels, judged by hand by the owner's reviewer (no rubric yet: an opinion, not a score):
| cluster (real lines) | llama | Claude |
|---|---|---|
| c1 `Timed out acquiring JDBC connection from pool (max=…, active=…)` | `resource_exhaustion` ✔ — label is the prompt's example text word for word | `dependency_timeout` (debatable: the pool is full, the DB is not slow) |
| c2 `ERR_NOT_FOUND` | `dependency_timeout` ✘ | `bad_request` ✔ (closest; the taxonomy has no "not found") |
| c4 "health check ok" | `auth` ✘ | `unknown` ✔ |
| c3, c5 (INFO-like lines) | `unknown` | `unknown` |
- **Token counts are not comparable across providers** (different tokenizers; the Anthropic
  structured-output path also counts the schema). Compare cost and latency, not tokens.
- **The prompt example leaks:** llama copied "DB connection pool timeouts" from the prompt. Follow-up
  (Phase 10 eval set): prompt v3 without a domain example, and a `not_found` category. Not changed now:
  a new prompt version needs its own measured run.
- Neither model changed a fact. That is the result that matters.

## 11. Failure scenarios
| Symptom | Cause | Fix |
|---|---|---|
| `missing tokens - run: make agents-tokens` | no service tokens | `make agents-tokens` |
| agent run FAILED `untrusted_agent_assertion` | old tool-gateway config | restart `make run-tools` |
| `DEGRADED: labels by rule: LLM unavailable (504 …)` | model slower than the budget (Mac, prompt v1: >90 s on CPU llama) | v2 prompt + 115 s budget; the reason now includes the gateway's detail; `make agent-status INCIDENT=…` |
| compare: `routes use the SAME primary model` | gateway started with `routing.local.yaml` (fast = llama) | `make stop-llm && make run-llm` (routing.yaml + Claude key) |
| compare: `would be answered by the SAME model` / llm-smoke `anthropic configured=False` | no Anthropic key: `fast` lists Claude first but falls back to llama (Mac finding; the first preflight only checked `chain[0]`) | save `secrets/anthropic_api_key.txt`, `make stop-llm && make run-llm`; no `llm_provider_disabled` in the log |
| `another investigation … is RUNNING <id> (by …)` | a second terminal ran agent-run/compare on the same incident (seen on the Mac) | one run per incident; wait. Ollama also runs ONE request at a time (bulkhead 1) |
| run says `567 ms`, `tokens 0/0` / `⚠ CACHED` | the llm-gateway answered from its cache (same prompt as an earlier run): NO model ran. Seen on the Mac | `make agent-run … NOCACHE=1`; `agent-compare` always bypasses the cache and marks cached/fallback runs invalid |
| compare: `agents service unreachable: ConnectError` | `make run-agents` not running (seen on the Mac); compare now stops at the first failed run and writes no file | start it; `curl :8003/health/live` → 200 |
| `curl …:8005/health` → 404 | wrong path | `/health/live` (or `/health/ready`) |
| `evidence was not stored` | incident-service down during step 5 | `uv run python -m aeoi_orchestrator repost-evidence <id>` |
| facts mention 0 lines | wrong window | the window is detected_at −60/+30 min; pass `--start/--end` |

## 12. Production considerations
Kafka `AgentTask` consumers scaled on lag (Phase 18); orchestrator with checkpoints (Phase 9);
per-task budgets from the investigation; evidence write verified against `tools.tool_calls`;
prompt registry mirrored to `llm.prompt_versions`; label quality tracked on a real eval set.

## 13–15. Interview
`docs/interview/phase-08-first-agent.md`.
