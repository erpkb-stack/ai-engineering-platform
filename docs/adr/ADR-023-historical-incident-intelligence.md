# ADR-023: Similar past incidents by hybrid search, compared by checked claims, used only as a tie-break

Status: Proposed
Date: 2026-10-10
Refines: ADR-016 (hybrid retrieval), ADR-020 (agents, pointers, evidence by kind), ADR-021 (candidates, rubric bands); architecture.md §10 #8 (Historical Incident Agent), FR-6 (the architecture diagram drew it in the fan-out; see Alternatives)

## Context
After Phase 12 an investigation has observations, validated hypotheses and a postmortem.
FR-6 asks: find similar past incidents, explain how they are similar AND how they differ, and
never treat "similar" as "same root cause". The data: 600 synthetic past incidents in
`rag.historical_incidents` (`allowed_groups`, a `vector(768)` column that was never filled,
a keyword index). Phase 7 already had a keyword-only `search_incidents` tool.
Constraints:
1. A past incident is visible per GROUP (rag ACL); this incident is visible per ROLE. What we
   store on this incident, anyone who may read it sees.
2. A past root cause is a base rate about other incidents, not evidence about this one.
3. A model must not invent a comparison (ADR-018/021 rule: claims are checked by code).

Owner decisions (2026-10-10): gate first (PR #12 merged); **LLM compares**; history
influence = **prior line + tie-break** (changed from "rubric raises bands" after review: a
band is evidence about THIS incident; history is not); retrieval = **hybrid + honest eval**.

## Decision
```
collect_evidence ─► history (orchestrator node) ─► hypothesize ─► critique ─► validate ─► report
                    historical_incident agent (as the user, delegated token):
                      query (code) ─► search_incidents (tool gw ─► rag hybrid, ACL in SQL)
                      keep only open_to_engineering ─► Haiku claims ─► code checks each
                      priors (code) ─► INCIDENT pointers (evidence)
```
### A. Retrieval — `rag/history.py`, `POST /v1/incidents/search`
Keyword branch (FTS, OR-query) + vector branch (HNSW cosine, same `embedding_model` only),
fused by RRF (k=60), plus a same-service vote worth half a rank-1 vote (`boost_service`).
The group ACL is in BOTH branches' SQL; groups come from the token; a defence-in-depth assert
refuses a row outside the caller's groups. `mode` defaults to `keyword` and `service_key`
stays a FILTER unless `boost_service` is set: the Phase 7 contract is unchanged. When the
embedder fails, hybrid degrades to keyword and every hit says `retrieval: keyword`; the same
label is used when no vector exists for the model yet (before `make embed-history`).
`vector` mode fails with 503 instead of silently returning keyword hits. Contract drift
(nothing breaks): `score` is now an RRF value, not `ts_rank_cd`, and ties order by id, not
recency; the tool output never carried `score`.
Vectors: `make embed-history` (resumable, `REEMBED=1` clears all first).

### B. What may be stored (the sharing rule)
Each hit carries `open_to_engineering` (`eng-all` ∈ allowed_groups). The agent asks rag for
`shared_only`, so restricted incidents are filtered IN the SQL and never read; it drops any
other hit again (defence in depth, counted in its trace only). They never reach the model,
the evidence, a prior or the postmortem. Stored per similar incident: key, root-cause CATEGORY (closed vocabulary),
services, date, retrieval ranks, checked claims. Never its text. Evidence = INCIDENT pointer
(`rag://incidents/<key>`). In the synthetic org every role, MANAGER included, is in
`eng-all`, so this exposes nothing a reader could not search; a real org must check that
`eng-all` really means "everyone who can read incidents".

### C. Comparison — Haiku proposes, code decides
Claim = (field ∈ service | error_code | symptom | deploy_before, relation ∈ same |
different). Code computes every relation it can check from typed data (observations vs the
past incident's fields). A claim is kept only if code computes the same relation; a pair code
cannot check is rejected. Model down → code's relations, `compared_by=code`, run PARTIAL.

**Honest note:** code computes every relation the model may claim, so the model adds no
information. It stays because the owner chose it; `claims_rejected` is the measurement that
decides (Phase 25) whether it is worth a Haiku call per investigation.

### D. Influence on reasoning — prior line + tie-break only
`HistoryPrior` per cause kind: "N of M similar past incidents had <kind> as root cause".
Category → kind is a code table (`CATEGORY_KINDS`). The prior is attached to the candidates
(`history_prior`, `history_n`, `history_refs`), shown in GET /hypotheses and the postmortem,
and used in `rubric_order` AFTER band, support and contradiction: it never crosses a band and
never beats more evidence. It is NOT shown to the ranker (anchoring) nor to the critic
(independence). So with a working ranker, history changes only what is displayed; it reorders
only when the ranker is down. That is the strictest reading of "never changes a band".

### E. Outcome and visibility
Search failed / compared by code / pointers not stored → PARTIAL, named
(`history_outcome`); unstored pointers also leave the postmortem section out. The history
trace (observations + past incidents' text) needs LOGS + METRICS + DEPLOYS + DOCS read. The
prior and the similar list are code text, shown to every incident reader (INCIDENT kind).
A claim's VALUE is this incident's data (error code = LOG, symptom = METRIC, deploy timing =
DEPLOY): the postmortem shows a claim only to a reader of that kind. The investigation plan
(readable by every incident reader) holds no count of restricted matches.

## Honest eval — `make history-eval`
36 hand-written paraphrases (3 per category), checked by a unit test not to copy 4-word runs
from the corpus templates. Variants: symptom words only, and words + error code. Modes:
keyword, vector, hybrid, hybrid+boost. Metrics: hit@1, hit@5, MRR@10, precision@5 (same
category), with bootstrap CIs; leakage for an `eng-all`-only caller must be 0.
**Read the numbers with these limits:** each category has ONE text template, so this measures
"find the right template", not ranking among varied write-ups; the "+code" variant is easy
for keyword BY CONSTRUCTION (one code per category); the queries were written by the author
of the templates.

**First Mac run (2026-10-11, nomic-embed-text, pgvector 0.8.7, commit 7f03474 + Phase 13):**
| 36 queries | hit@1 | hit@5 | MRR@10 |
|---|---|---|---|
| symptom / keyword | 0.778 | 0.889 | 0.815 |
| symptom / vector | 0.861 | 0.889 | 0.868 |
| symptom / hybrid | 0.917 | 0.917 | 0.920 |
| symptom+code / keyword | 0.972 | 0.972 | 0.972 |
| symptom+code / hybrid | 0.972 | 1.000 | 0.986 |
Leaks 0 everywhere. Paired, symptom-only hit@1: hybrid better on 5 queries, keyword better on
0 (sign test p ≈ 0.06; 95% CIs overlap: hybrid [0.81, 1.0], keyword [0.64, 0.92]). So: the
direction favours hybrid, the evidence is weak; hybrid stays the agent's mode, keyword stays the
API default. Adding the error code HURTS vector alone (0.861 → 0.750): the embedding model does
not use `ERR_*` tokens well - which is why hybrid keeps the keyword branch. `hybrid+boost` says
nothing here: the eval's service is unrelated to the category by construction.

**First Mac smoke (same day):** the 5 similar incidents were all the right category but none was
on the demo's service, although keyword + service vote alone ranks the 5 checkout-api twins
first (reproduced in the sandbox on the same seed). With ~90 identical texts per category, the
vector rank among twins is noise, and an incident found by both branches beats a checkout-api
twin found only by keyword plus a half vote. Changes: `EF_SEARCH = 400` (+ iterative scan on
pgvector ≥ 0.8) so the vector branch returns its full depth rather than an HNSW subset - a
defensive fix, NOT proven to be the cause (the sandbox planner uses a sequential scan);
`make history-explain` prints both ranks per hit and the vector plan, to decide with data
before touching the service weight. The smoke now checks category + service, not one key.

**Second Mac smoke: still no checkout-api incident.** Decision (no weight tuned on one query):
the agent searches TWICE - this service's own history first (service = filter, 2 hits), then
the same failure anywhere (service = boost) - own-service hits first, twins counted once,
5 kept. An SRE wants both "has this service done this before?" and "where else did this
failure happen?"; showing both as separate lists needs no weight. Cost: one more rag call per
investigation. Each hit carries `scope` (service | all); the postmortem marks own-service ones.
One list failing = PARTIAL (degraded), both failing = FAILED. `history-explain` stays the tool
to find out WHY the broad list misses the service; the eval (rag level) is unchanged.

**Third Mac smoke: history ✔, but the postmortem writer failed 3 times in a row** with
"'action_items' is a required property" - the LAST field of its schema, so most likely Sonnet was cut
off at `max_tokens` 1800 (the input did not change in Phase 13; longer critic refinements are
the likely reason). The gateway then repaired with the same limit and was cut off again, and
its error said only "still invalid". Fixed outside this ADR's scope, with a test: the gateway
reports "output cut off at max_tokens=N" and skips the useless repair; the writer may write
3000 tokens (route cap 4096). Cost: only the tokens actually written.

## Alternatives
| Option | Why not |
|---|---|
| History raises the rubric band (first owner choice) | a band says how strongly THIS incident's evidence supports a cause; base rates would inflate it with no new evidence |
| Show the prior to the ranker | anchors the model on other incidents; cannot be checked |
| Store past incidents' summary/root cause | text from a group-restricted corpus on a role-visible record |
| Code-only comparison | the better design today (see C); not the owner's choice |
| Keep keyword only | paraphrased symptoms ("db client waits to borrow a connection") share few words with the write-up |
| A 5th evidence agent in the fan-out | needs the observations first (query + comparison) |

## Review (independent, read-only subagent) — 2 high, 4 medium, 4 low
Fixed with tests: claim values (this incident's log/metric/deploy data) shown to MANAGER in
the postmortem; a count of restricted matches in the investigation plan (now: `shared_only`
in SQL, count in the trace only); no crash/resume test of the history node; smoke aborted
under `set -u` on a failed history; "deploy before" read from the FIX text ("Rolled back the
deployment") and ignored "no code deploy", and "same" read as "both" when neither had it
(values now say both / neither / which side); similar incidents put in the postmortem when
their pointers were not stored; the no-vectors label looked only at the top-k rows (now a
count of the vector branch). Documented: the eval's leakage counter is a second line behind
the assert; the score/tie-order drift above. Not tested: resuming a Phase 12 checkpoint that
has no history fields (the code reads every new field with a default).
Mutation-checked: sharing filter, claim check, "checkable only" rule, tie-break position (two
mutations), pointer-only excerpt, service boost, history outcome. Integration: ACL in each
branch (removing the vector branch's ACL fails 4 tests), model mismatch, exclude, boost vs
filter, degradation, no-vectors label, eval plumbing; end to end through the tool gateway
with a restricted hit that must never appear.

## Tradeoffs
- One more Haiku call per investigation (seconds, fractions of a cent; measured on the Mac
  only) for information code already has (C).
- `CATEGORY_KINDS` is a hand table over a synthetic taxonomy.
- Same-service boost is a fixed weight (0.5 of a rank-1 vote), not tuned: there is no labelled
  set to tune it on.
- RRF needs both branches over the same rows; a past incident with no vector competes on
  keyword only.

## Prototype vs Production vs Enterprise-scale
[P] 600 rows, one HNSW index, one search per investigation. [Prod] embed on write (outbox),
taxonomy owned by the incident process, eval on real postmortems with human labels, decide on
C from `claims_rejected`. [Ent] per-tenant corpora, cross-team sharing policy instead of one
`eng-all` group, retention.
