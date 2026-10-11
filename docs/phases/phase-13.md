# Phase 13 — Historical incident intelligence

## 0. Gate and scope challenge
- **Gate: passed.** PR #12 merged (7f03474); Phase 12 verified on the Mac (`make pm-smoke`
  22/22, kill + resume COMPLETE, integration 194 passed). The roadmap tick was uncommitted on
  main (protected); it ships in this branch.
- **Challenge 1 — "rubric raises bands"** (first owner choice). Rejected after review: a band
  says how strongly THIS incident's evidence supports a cause. A base rate from other incidents
  is not evidence here. Owner decision: **prior line + tie-break inside a band**.
- **Challenge 2 — "LLM compares".** Owner decision. Kept honest: Haiku proposes typed claims,
  code computes every relation itself and keeps a claim only on agreement. Code alone could do
  the whole job (ADR-023 C); `claims_rejected` is the data for removing the model later.
- **Challenge 3 — "hybrid".** The 600 past incidents had no vectors. Hybrid is only worth it
  if an eval shows it. Owner decision: **hybrid + honest eval**, with its limits printed.

## 1. Objective
During an investigation, find similar past incidents the user may read, keep only those every
engineer may read, say what is the same and what differs (checked), store pointers, and show
a base rate next to each candidate cause — without changing any band.

## 2. Business reason
Repeat incidents (architecture.md §2 pain 3): "we had this last quarter" lives in people's
heads. Here it arrives with the investigation, labelled as a base rate, not a diagnosis.

## 3. Architecture
```
collect_evidence ─► history ─► hypothesize ─► critique ─► validate ─► report ─► finalize
                    historical_incident agent (delegated token = the user)
                      query (code) ─► search_incidents ─► tool gateway ─► rag (hybrid, ACL in SQL)
                      shared only ─► Haiku claims ─► code check ─► priors ─► INCIDENT pointers
```
| Guarantee | Enforced by | Test (mutation-checked) |
|---|---|---|
| Group ACL in BOTH branches' SQL | `rag/history._SQL` + assert | `test_vector_only_respects_the_acl` (vector ACL removed → 4 fail) |
| No cross-model cosine | `embedding_model = :model` | `test_vectors_from_another_model_are_not_compared` |
| Honest retrieval label | api: embedder down / no vectors → `keyword`; vector-only → 503 | `test_embedder_down_…`, `test_hybrid_without_vectors_says_keyword` |
| Phase 7 contract kept | `mode=keyword`, service = filter by default | `test_incident_search_applies_group_acl_and_service_filter` (unchanged) |
| Only `eng-all` incidents read / stored / prompted / used | `shared_only` in rag SQL + `HistoryAgent.run` | `test_shared_only_drops_…` (each branch), `test_only_shared_incidents_…`, e2e (INC-9001 never appears) |
| A claim's value (this incident's LOG/METRIC/DEPLOY data) shown only to readers of that kind | `postmortem._similar_for` | `test_similar_claims_are_filtered_…` + e2e MANAGER |
| Stored once across crash + resume; history not re-run | reuse guard, idempotent evidence | e2e `test_crash_after_history_…` |
| Claims kept only if code agrees; unchecked = rejected | `check_claims`, `relations` | `test_a_claim_survives_only_if_…` |
| Pointers, no past text, on this incident | `_pointer` | unit + e2e |
| Prior = tie-break: never across a band or past more evidence | `rubric_order` key order | `test_prior_breaks_a_tie_inside_a_band_…` (2 mutations) |
| Prior never shown to ranker / critic | `candidate_block`; blind copies blank it | `test_hypothesis_agent_shows_priors_but_never_to_the_ranker` |
| Own-service history shown first (no tuned weight) | two searches in `HistoryAgent.run` | `test_own_service_history_comes_first_…` (same_service=0 → 3 fail) |
| Problems = PARTIAL, named | `history_outcome` | `test_history_problems_are_partial_…` + e2e (rag 503) |
| History trace hidden from MANAGER | `AGENT_CONTENT_PERMS` | e2e |

## 4. Files
```
docs/adr/ADR-023-historical-incident-intelligence.md
libs/models     api/history.py (new: Claim, SimilarIncident, HistoryPrior, HistoryStats);
                api/search.py (mode, boost_service, exclude_key; hit ranks + open_to_engineering);
                api/agents.py, api/hypotheses.py, api/postmortem.py, api/reasoning.py (fields)
services/rag    history.py (new: SQL, embed_pending, search); history_eval.py (new); api.py;
                main.py (app.state.embedder); __main__.py (embed-history, history-eval)
services/tool-gateway  schemas (IncidentSearchIn/HitItem), adapters/rag.py, registry description
services/agents history/agent.py (new); prompts/history/v1.md; reasoning/candidates.py
                (attach_priors, tie-break); reasoning/hypothesis.py; config/main/api
services/orchestrator  graph (history node, history_outcome, priors to hypothesize only,
                similar to the postmortem); validation.to_batch; config `history`; api perms
services/incident-service  postmortem.py (similar section, prior line)
data/eval/history_queries_v1.jsonl (36 paraphrases)
scripts  smoke-phase13.sh (new); smoke-phase9/10/11 count the new task
tests    rag unit test_history_eval.py; integration rag/test_history.py (10);
         agents test_history_agent.py (13); incident-service test_postmortem_render.py;
         orchestrator test_graph_units.py; integration/agents/test_history_e2e.py (3);
         tools conftest stub configurable
Makefile embed-history, history-eval, history-smoke
```

## 5–6. Commands (macOS, zsh)
```zsh
cd ~/projects/ai-engineering-platform
git switch main && git pull
shasum -a 256 -c ../_to_delete/phase13-base.sha256 | grep -v ': OK$'   # no output = safe
git switch -c phase-13-history               # carries the uncommitted Phase 12 roadmap tick
tar xzf ../_to_delete/phase13.tgz
uv sync --all-packages
make check && make test-integration          # no migration in this phase
```

## 7. Config
`AEOI_AGENTS_HISTORY_*`: `route` (fast = Haiku), `k` 8, `same_service` 2, `max_compared` 5, `max_tokens` 900,
`llm_timeout_s` 40, `task_deadline_s` 60. `AEOI_ORCH_HISTORY` (true; needs reasoning on).
Prompt: history v1. rag: `RRF_K` 60, `SERVICE_WEIGHT` 0.5, `DEPTH` 50 (constants, untuned).

## 8. Tests
- `make check`: 626 passed (sandbox). Integration: 207 passed, 1 skipped (sandbox, fake models).
- Mutation checks: sharing filter, claim check, checkable-only rule, tie-break position (2),
  pointer-only excerpt, service boost, history outcome, `shared_only` request, "no deploy"
  negation, claim filtering per reader; rag: vector ACL, model match, service boost, open flag,
  exclude, `shared_only` in each branch, no-vectors label.
- Independent review: 2 high, 4 medium, 4 low; all fixed or documented (ADR-023 "Review").
- **Not measured anywhere yet:** retrieval quality with the real embedding model, Haiku claim
  quality, latency and cost. All come from the Mac.

## 9. Run
```zsh
make dev ; make run-llm ; make run-rag ; make run-tools ; make run-audit ; make run-agents ; make run-orch
make embed-history                  # once: 600 vectors (resumable)
make history-eval                   # keyword vs vector vs hybrid; READ the printed limits
make history-explain Q="..." SERVICE=checkout-api   # debug: both ranks per hit + vector plan
make history-smoke                  # Phase 13 end to end; prints the similar-incident section
make pm-smoke && make hypo-smoke    # regressions (each now pays one Haiku comparison)
```

## 10. Verify (gate for Phase 14)
- [ ] `make embed-history`: `without_vector: 0`
- [ ] `make history-eval`: leaks 0 in every row. Write down symptom-only hit@5 for keyword vs
      hybrid with their CIs. If hybrid does not beat keyword on symptom-only queries, say so in
      ADR-023 and keep keyword as the default (`mode` is per call).
- [ ] `make history-smoke` all ✔ (notes allowed for rejected Haiku claims and INC-2911)
- [ ] If "no similar incident on checkout-api" still appears after fix2: run the printed
      `make history-explain` line and send the table
- [ ] Read the printed section once: is each "same/different" line true? Wrong ones = eval cases
- [ ] `make test-integration` green

Measured here: sandbox only, fake embeddings and fake models — no quality, latency or cost
numbers claimed.

## 11. Failure scenarios
| Symptom | Cause | Fix |
|---|---|---|
| smoke: `vector search: 503` / 0 hits | no vectors or embedder down | `make run-llm`, `make embed-history` |
| hits say `retrieval: keyword` in hybrid mode | embedder down or vectors from another model | `make embed-history REEMBED=1` after a model change |
| PARTIAL `historical_incident: search_incidents: …` | rag down / tool denied | `make run-rag`; agents.yaml lists search_incidents |
| PARTIAL `historical_incident: compared by code: LLM unavailable` | Haiku down | `make llm-smoke` |
| PARTIAL `… similar incidents were not stored` | incident-service refused the pointers | read the error; evidence kind INCIDENT |
| no prior on any hypothesis | no shared similar incident, or its category maps to no kind | expected; see `CATEGORY_KINDS` |
| `history-eval` exits 1 "have no vector" | embed first | `make embed-history` |

## 12. Production considerations
Embed on write; a real taxonomy for `CATEGORY_KINDS`; labelled similar pairs from real
postmortems; drop the Haiku comparison if `claims_rejected` shows it adds nothing; a sharing
policy richer than one `eng-all` group.

## 13–15. Interview
`docs/interview/phase-13-history.md`.
