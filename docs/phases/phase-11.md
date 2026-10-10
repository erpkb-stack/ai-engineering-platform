# Phase 11 — Critic + Validation

## 0. Gate and scope challenge
- **Gate: NOT passed when this was built.** Phase 10 is unmerged (PR #9) with three open items:
  `make test-integration` on the branch, the crash + resume check, and the Sonnet route after
  the temperature fix. The owner chose "gate first", then asked to build anyway. Built in the
  sandbox on top of Phase 10. **Do not merge before Phase 10 is merged.**
- **Challenge 1 — "the LLM finds the root cause".** No. A model that writes causes invents
  times, numbers and confidences, and prompt-injected log text could steer conclusions.
  **Code proposes every candidate and its band**; the model only ranks/explains (Haiku) and
  attacks (Sonnet), and every model claim must cite observation refs that code checks.
- **Challenge 2 — critic independence.** Owner decision: **Haiku ranks, Sonnet critiques**.
  The critic never sees the ranker's prose or rank, and runs with fallback OFF: a 3B llama
  "critic" would look like Sonnet in every report. A check fails the run (PARTIAL) if the two
  models are ever the same.
- **Challenge 3 — LLM judge and loops.** Owner decision: **core only**. A judge nobody can
  measure yet adds cost, not trust; loops add latency without new evidence sources.
- **Found before building:** the `reasoning` route had been broken since Phase 5
  (`claude-sonnet-5-5` rejects `temperature`, HTTP 400) and nothing noticed, because fallback
  hid it and `llm-smoke` only tested `fast`. Fixed in Phase 10 (`14566b6`); `llm-smoke` now
  calls every hosted-primary route with fallback off.

## 1. Objective
After the evidence agents: code builds a timeline and candidate causes with rubric bands,
Haiku ranks and explains them, Sonnet critiques them blind, code validates, and the result is
stored on the incident as hypotheses with SUPPORTS/CONTRADICTS evidence edges.

## 2. Business reason
MTTI (architecture.md §4): "first validated hypothesis accepted by a human". A ranked,
critiqued, evidence-linked hypothesis list is that first artefact. Trust: every band is a
rule a human can read, and every sentence a model wrote cites the evidence it rests on.

## 3. Architecture
```
collect_evidence ─► hypothesize (agents)                     ─► critique (agents)              ─► validate (orch, code) ─► finalize
                    observations (code, o1..oN)                  same observations (sha-checked)   merge ranker + critic
                    candidates + rubric bands (code)             code statements + refs ONLY        hard: citations, support, timing
                    Haiku (`fast`): order + cited explanation    Sonnet (`reasoning`, NO fallback)  soft: critic answered, independent
                                                                 verdicts, contradictions, ≤2 alts  POST incident.hypotheses (idempotent)
```
| Guarantee | Enforced by | Test (mutation-checked unless noted) |
|---|---|---|
| Critic on Sonnet, never a fallback, not overridable | `ReasoningBudget`, `critic.py` | `test_critic_uses_the_reasoning_route_without_fallback` |
| Critic independent of the ranker | prose removed twice; `critic_independent` check | `test_critic_never_sees_the_rankers_prose`, `test_critic_on_the_rankers_model_is_flagged` |
| Same evidence for both agents | `observations_sha` | `test_critic_refuses_different_observations_than_the_rankers` |
| Log text cannot forge an observation | error-code pattern + one-line statements | `test_a_newline_in_an_error_code_cannot_forge_an_observation` |
| Deploy support = own service; rival deploys count against | `candidates.propose` | `test_a_symptom_on_another_service…`, `test_a_second_early_deploy_competes…` |
| VALIDATED = reviewed and not refuted | `validation._status` | `test_validated_means_reviewed_and_not_refuted` |
| Model text hidden from roles that can't read the evidence | incident-service `hypotheses()` | e2e `test_hypotheses_ranked_critiqued_validated_and_stored` |
| Sampled answers never cached | gateway `deterministic` | `test_a_model_without_temperature_is_never_cached` |
| Resume never re-ranks a recorded task | reuse guard | e2e crash tests (Phase 11) |

[P] fixed rubric, one critic pass. [Prod] calibrated bands, a measured judge, loops with
budgets. [Ent] human review queue (Phase 16) before a hypothesis drives an action.

## 4. Files
```
docs/adr/ADR-021-hypotheses-critic-validation.md (+ README)
libs/models    api/hypotheses.py (Observation, HypothesisOut, Critique, batch/record, fingerprint),
               api/reasoning.py (ReasoningTask), api/agents.py (agent names, result fields)
libs/db        alembic 0020 (hypotheses: investigation_id, hypothesis_key, detail); models/incident.py
services/agents        reasoning/{observations,candidates,hypothesis,critic,llm}.py; prompts/
                       hypothesis/v1.md, critic/v1.md; api/config/main
services/orchestrator  graph (hypothesize, critique, validate), validation.py, store, clients,
                       api (plan.reasoning, trace perms), __main__
services/incident-service  POST/GET /incidents/{id}/hypotheses (scope hypotheses:write)
services/api           GET /api/v1/incidents/{id}/hypotheses
services/llm-gateway   no cache for models without temperature (+ Phase 10 fix 14566b6)
scripts  smoke-phase11.sh (new), smoke-phase5.sh (every hosted route, fallback off),
         smoke-phase9/10.sh (evidence path; reasoning gaps are notes there)
tests    agents/test_reasoning_agents.py (20), orchestrator/test_validation.py (9),
         integration/agents/test_reasoning_e2e.py (6), llm-gateway (+1)
Makefile agents-tokens (+ hypotheses:write), hypo-smoke
```

## 5–6. Commands (macOS, zsh) — only after PR #9 (Phase 10) is merged
```zsh
cd ~/projects/ai-engineering-platform
git switch main && git pull                  # must include Phase 10 + 14566b6
git switch -c phase-11-critic
tar xzf ~/projects/_to_delete/phase11.tgz
uv sync --all-packages
make db-upgrade && make db-check             # 0020
make agents-tokens                           # orchestrator token gets hypotheses:write
make check && make test-integration
```

## 7. Config
Prompts: hypothesis v1, critic v2. `AEOI_AGENTS_REASONING_*`: `rank_route` (fast), `critic_route` (reasoning),
`critic_allow_fallback` (false — do not change), `rank_max_tokens` 900, `critic_max_tokens`
1200, `llm_timeout_s` 115, `task_deadline_s` 150. `AEOI_ORCH_REASONING` (true).
routing.yaml: `supports_temperature: false` for `claude-sonnet-5-5`.

## 8. Tests
- `make check`: 583 passed. Integration: 189 passed, 1 skipped (sandbox, fake models).
- Mutation checks (each guard removed → its test fails): critic fallback, critic route
  override, observation fingerprint, error-code sanitising (two layers: removing both fails),
  own-service support, competing deploys, VALIDATED status, critic independence, model-text
  redaction, no-cache for sampled answers.
- Independent review: 4 high, 6 medium, 2 low; all but one fixed (ADR-021 "Review").
- **Not verified anywhere yet: real model behaviour.** The sandbox has no Anthropic key; the
  fake model returns empty rankings/critiques, so the sandbox smoke is PARTIAL by design.

## 9. Run
```zsh
make dev ; make run-llm ; make run-rag ; make run-tools ; make run-audit ; make run-agents ; make run-orch
make llm-smoke       # every hosted route answers with its PRIMARY model, fallback off
make hypo-smoke      # Phase 11
make multi-smoke && make orch-smoke && make agent-smoke   # regressions
```

## 10. Verify (gate for Phase 12)
- [ ] Phase 10 merged (its Verify ticked)
- [ ] `make llm-smoke`: `route reasoning: primary claude-sonnet-5-5 answered (fallback off)`
- [ ] `make hypo-smoke` all ✔: COMPLETE; ranker = Haiku, critic = Sonnet; the deploy is the top
      HIGH hypothesis; traffic surge and thread pool ruled out; MANAGER sees no edges/model text
- [ ] Read the stored hypotheses once (`curl … /api/v1/incidents/INC-n/hypotheses`): do the
      explanation and the critic's alternatives make sense? Write down what is wrong — that
      list becomes the first eval cases (Phase 25). No quality claim before that.
- [ ] `make test-integration` green
- [x] First real run read by hand (2026-10-09): found the critic lowering the deploy with
      steady metrics and restating it as a rival (ADR-021 §I) — fixed in phase11-fix2

Measured here: sandbox only, fake models — no latency, cost or quality numbers are claimed.

## 11. Failure scenarios
| Symptom | Cause | Fix |
|---|---|---|
| PARTIAL `critic: LLM unavailable (400 …)` | the reasoning model rejected the request | `make llm-smoke`; check routing.yaml `supports_temperature` |
| PARTIAL `critic: … LLM unavailable (400 llm-request-rejected …)` with plain Sonnet working | the model rejects forced tool choice (found on the Mac) | routing.yaml `supports_forced_tool: false` (fixed); `make llm-smoke` shows `route reasoning structured` |
| deploy HIGH → LOW, CHALLENGED, with critic contradictions = steady metrics | critic cited observations that rule out RIVALS (found on the first real run) | fixed: `can_contradict`; such refs show as "critic disputed (not counted)" |
| a critic alternative restates the deploy as a rival | prompt v1 had no way to say "more specific mechanism" | fixed: prompt v2 `refines`; hypo-smoke prints a note if it still happens |
| PARTIAL `critic: … not independent` | `fast` and `reasoning` route to the same model | fix routing.yaml; never point both at one model |
| PARTIAL `hypothesis: … ranked by rubric` | Haiku down/skipped candidates | bands are still code's; read the ranker trace |
| INCONCLUSIVE `no hypothesis is supported by the evidence` | no symptom in the window | expected for quiet services |
| PARTIAL `validation dropped hN` | a code candidate failed a hard check | a bug or bad data: read `plan.hypotheses.dropped` |
| PARTIAL `hypotheses were not stored` (422) | an edge points at evidence not on the incident | whole batch refused by design (ADR-021, not fixed) |
| start → 403 `hypotheses:write` on POST | old orchestrator token | `make agents-tokens`, restart run-orch |

## 12. Production considerations
Calibrated bands on labelled incidents; an LLM judge with its own eval set; critic→plan loop
with a per-investigation budget once evidence agents can be asked for more; per-reader filtering
of conclusions; read-back of stored evidence keys before the POST; idempotency keys on paid
LLM retries.

## 13–15. Interview
`docs/interview/phase-11-critic.md`.
