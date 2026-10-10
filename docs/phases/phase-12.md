# Phase 12 — Incident timeline + postmortem

## 0. Gate and scope challenge
- **Gate: NOT passed when this was built.** Phase 11 is in PR #11 (not merged); the Phase 9/10
  crash + resume check has never been run on the Mac (`make orch-smoke` 19/19 is). The owner
  chose "build now". **Do not merge before PR #11 is merged and crash + resume passes.**
- **Challenge 1 — "a full postmortem now".** A postmortem states a root cause; only a human
  confirms one (Phase 16). Owner decision: full postmortem anyway. Kept honest by labels, not by
  cutting sections: "Most likely root cause (top hypothesis)", "**not yet confirmed by a
  human**", action items "proposed, no owner, nothing executes them" (AGENTS.md rule 1).
- **Challenge 2 — "TimelineBuilder"** already existed as Phase 11 observations. New work is
  storing them on the incident (FR-5), merging with human events, and visibility per kind.
- **Challenge 3 — Sonnet for prose.** Owner decision. The model writes sentences only; every
  number and time is code's, and code drops sentences it cannot check.

## 1. Objective
After validation, code builds the timeline, Sonnet writes the narrative, code checks it, and
incident-service serves a postmortem assembled at read time (JSON + markdown), redacted per
reader.

## 2. Business reason
Write-up toil (architecture.md §2 pain 4): timelines and postmortems are rebuilt from chat
logs afterwards. Here the timeline exists when the investigation ends, with evidence ids.

## 3. Architecture
```
validate ─► report ─► finalize
            postmortem agent: timeline (code) · numbers (code) · Sonnet sections (checked)
            POST /v1/incidents/{id}/timeline     (idempotent per source|service|subject|instant)
            POST /v1/incidents/{id}/postmortems  (idempotent per investigation)
GET /api/v1/incidents/{id}/postmortem[?format=markdown]  ← assembled now, per reader
GET /api/v1/incidents/{id}/timeline                       ← human + agent rows, X-Hidden-Rows
```
| Guarantee | Enforced by | Test (mutation-checked) |
|---|---|---|
| Sentences cite observations with evidence | `postmortem/agent.check` | `test_sentences_must_cite_and_must_not_invent_numbers` |
| No invented number/date ("900ms", "3x", "12 min") | `unknown_number` | `test_the_number_check_catches_units_and_dates_but_not_names` |
| No links/markup from the model | `MARKUP` + markdown escaping | `test_links_and_markup_…`, `test_model_markup_is_escaped_…` |
| Writer = Sonnet, never a fallback | `PostmortemBudget` | `test_writer_is_sonnet_without_fallback_…` |
| Model sentence / negative evidence redacted per reader (ids too) | `postmortem._redact` | e2e `…_timeline_doc_and_redaction`, `test_negative_evidence_is_redacted_…` |
| Timeline row visibility = stored evidence kind | `add_timeline` (server-derived source) | e2e (AGENT claim ignored, mixed kinds 422) |
| Stored once across resume / reruns | dedupe key + unique index, reuse guard | e2e crash test, resent point inserts 0 |
| Numbers from THIS run; trigger chosen by code | `PostmortemIn.points`, `trigger_ref` | `test_first_symptom_is_this_runs_first_sustained_effect` |
| Times are UTC whatever the DB offset | `hhmm`, `_utc`, dedupe key | `test_times_are_utc_…` (both services) |
| Writer problems = PARTIAL, named | `report_outcome` | `test_a_postmortem_problem_is_partial_…` |
| Writer trace hidden from MANAGER | `AGENT_CONTENT_PERMS` | e2e |

## 4. Files
```
docs/adr/ADR-022-timeline-and-postmortem.md (+ README)
libs/models    api/postmortem.py (TimelinePoint/Batch, PmSentence, ActionItem, NoChange,
               PostmortemDraft/In/Doc, RootCauseState), api/agents.py, api/reasoning.py, events.py
libs/db        alembic 0021 (incident_events.dedupe_key, incident.postmortems); models/incident.py
services/agents        postmortem/agent.py; prompts/postmortem/v1.md; config/main/api
services/incident-service  postmortem.py (store, assemble, redact, markdown); api.py routes
services/orchestrator  graph (report node, validated(), report_outcome), clients, config, api, __main__
services/api           GET /api/v1/incidents/{id}/postmortem; proxy forwards X-Hidden-Rows
scripts  smoke-phase12.sh (new); smoke-phase9/10/11 count the new task
tests    agents (postmortem section of test_reasoning_agents.py), incident-service
         test_postmortem_render.py, orchestrator test_graph_units.py,
         integration/agents/test_postmortem_e2e.py (5)
Makefile agents-tokens (+ timeline:write, postmortems:write), pm-smoke, postmortem
```

## 5–6. Commands (macOS, zsh) — on top of phase-11-critic until PR #11 is merged
```zsh
cd ~/projects/ai-engineering-platform
git switch phase-11-critic && git switch -c phase-12-postmortem
tar xzf ~/projects/_to_delete/phase12.tgz
uv sync --all-packages
make db-upgrade && make db-check             # 0021
make agents-tokens                           # orchestrator token: + timeline:write, postmortems:write
make check && make test-integration
```

## 7. Config
`AEOI_AGENTS_POSTMORTEM_*`: `route` (reasoning), `allow_fallback` (false — do not change),
`max_tokens` 1800, `llm_timeout_s` 115, `task_deadline_s` 150. `AEOI_ORCH_POSTMORTEM` (true).
Prompts: postmortem v1.

## 8. Tests
- `make check`: 606 passed. Integration: 194 passed, 1 skipped (sandbox, fake models).
- Mutation checks (guard removed → a test fails): citation required, number check, names need
  a letter, markup drop, writer fallback, sentence redaction, redacted ids cleared, negative
  evidence redaction, timeline kind filter, server-derived source, stored-evidence check,
  dedupe, sustained anchor, writer trace redaction, UTC in agent and incident-service.
- Independent review: 4 high, 5 medium, 3 low; all fixed but one (ADR-022 "Review").
- Live sandbox stack (mock Anthropic that rejects temperature and forced tool choice like the
  real Sonnet): `smoke-phase12` 21/21 + 1 note (the mock's planted "40%" sentence was dropped,
  as designed); hypo/multi/orch smokes 24/26/19. **Found only by the live run:** times labelled
  Z were local (-05:00) — fixed with tests.
- **Not verified anywhere yet: real Sonnet prose.** Read it on the Mac.

## 9. Run
```zsh
make dev ; make run-llm ; make run-rag ; make run-tools ; make run-audit ; make run-agents ; make run-orch
make pm-smoke                       # Phase 12; prints the postmortem markdown at the end
make postmortem INCIDENT=INC-n      # any incident; ROLE=MANAGER shows the redacted view
make hypo-smoke && make multi-smoke && make orch-smoke   # regressions (each now pays one Sonnet write)
```

## 10. Verify (gate for Phase 13)
- [ ] PR #11 merged; Phase 9/10 crash + resume passed (`SUCCEEDED(x2)`), Phases 9–11 ticked
- [ ] `make pm-smoke` all ✔ with real Sonnet (notes allowed only for dropped sentences)
- [ ] Read the printed postmortem once. Write down every sentence that is wrong, vague or
      unsupported — those become eval cases (Phase 25). No quality claim before that.
- [ ] `make postmortem INCIDENT=<the smoke's> ROLE=MANAGER`: no LOG/METRIC/DEPLOY rows, model
      sentences redacted, root cause shown
- [ ] `make test-integration` green

Measured here: sandbox only, fake/mock models — no latency, cost or quality numbers claimed.

## 11. Failure scenarios
| Symptom | Cause | Fix |
|---|---|---|
| PARTIAL `postmortem: narrative not written: LLM unavailable (400 …)` | Sonnet rejected the call | `make llm-smoke` (route reasoning structured) |
| PARTIAL `postmortem: narrative empty: …` | every sentence failed the checks | read the writer trace; model behaviour → eval case |
| PARTIAL `postmortem: timeline was not stored: 403` | old orchestrator token | `make agents-tokens`, restart run-orch |
| PARTIAL `… 422 … cites evidence kinds […]` | an observation mixes permissions | a bug in observations: read the point |
| GET /postmortem 404 | no run with the writer on this incident | run an investigation (AEOI_ORCH_POSTMORTEM=true) |
| Root cause "reasoning FAILED" | the hypothesis step failed | read the investigation error; rerun |
| header times look 5 h off | a time path without UTC normalisation | regression: `test_times_are_utc_…` should catch it |

## 12. Production considerations
Regenerate after resolution with human notes; human edits as versions; per-reader writer
input (closes the latent quoting gap); narrative eval with a judge (Phase 25); export.

## 13–15. Interview
`docs/interview/phase-12-postmortem.md`.
