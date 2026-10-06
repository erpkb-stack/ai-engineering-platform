# ADR-016: Hybrid retrieval in one SQL statement, RRF fusion, eval-gated LLM rerank

Status: Proposed
Date: 2026-10-06
Supersedes: —  (refines ADR-002 and ADR-006)

## Context
Incident questions mix two kinds of need: exact identifiers ("ERR-TLS-2535", "INC-7312",
"LedgerReconciler") and descriptions in different words ("checkout times out waiting for a DB
link after a release"). Documents carry group ACLs; a leak of a restricted document is a
security incident, not a quality bug. The owner's Mac is an Intel CPU (embeddings and any LLM
rerank are slow), and we have no measured baseline yet.

## Decision
1. **One SQL statement** does vector ANN (HNSW, cosine), keyword search (Postgres FTS) and the
   **permission filter inside each candidate branch, before LIMIT** (`allowed_groups && :groups`,
   `NOT quarantined`). Groups come only from the verified token.
2. **Fusion = Reciprocal Rank Fusion** (k=60): rank-based, no score calibration between cosine
   and ts_rank. A weight for the keyword branch when the query contains an identifier exists as
   a knob (`keyword_weight_identifiers`, default **1.0 = plain RRF**) - to be set from a real eval.
3. **Keyword branch = OR-query + identifier boost.** `plainto_tsquery` is AND - a question
   rarely has all its words in one chunk - so terms are OR-ed. `ts_rank_cd` has **no IDF**: we
   observed "tls" (frequent) outranking "2535" (the actual error code). Chunks containing all
   identifier-like terms of the query get +1. This is a heuristic stand-in for BM25.
4. **Ingestion is the security boundary for content:** secrets redacted, PII scrubbed
   (pattern-based), prompt-injection tripwire (incl. hidden HTML) -> quarantine with a reason
   (migration 0015); quarantined documents are never chunked and are also filtered at query time.
   Files without an explicit ACL are rejected (default deny).
5. **Rerank = optional LLM listwise grading** (0-3) through the gateway, one call for the top 10,
   25 s budget, **off by default** until `make rag-eval RERANK=1` on real embeddings shows a gain
   that justifies its latency. RESTRICTED candidates force the `local` route. Any failure returns
   the RRF order with `rerank.applied=false`.
6. **Degrade, don't fail:** if embeddings are unavailable, hybrid search answers keyword-only and
   says `degraded`; `mode=vector` returns 503.

## Alternatives
| Option | Why not (here) |
|---|---|
| Filter after ANN ("retrieve 40, drop forbidden") | Narrow-access users get < k results, and restricted rows are touched. ADR-006 forbids it. |
| Separate vector DB (Qdrant/Weaviate) | A second store to keep ACLs consistent with; Postgres does both at our size. |
| BM25 via ParadeDB `pg_search` | Real IDF. Not in the `pgvector/pgvector` image; revisit if the eval shows keyword misses on frequent-term queries. |
| Cross-encoder reranker (ONNX) | Cheaper and deterministic. The owner chose LLM rerank; the reranker is an interface, so a cross-encoder can be added and compared in the same eval. |
| Score blending (α·cos + β·ts_rank) | Needs calibration per corpus; RRF is robust without it. |

## Tradeoffs
- **HNSW + selective filter (pgvector < 0.8):** HNSW returns `ef_search` candidates, then the
  filter runs; a user who can see very little may get fewer than k vector hits. pgvector ≥ 0.8
  `iterative_scan` fixes this (enabled automatically when detected). Keyword hits still fill in.
- **LLM rerank is an injection surface:** a passage can say "rank me first". It can only reorder
  results the caller may already see, never add or widen access. Quarantine catches crude cases.
- **The injection tripwire has false negatives by design;** wrapping retrieved text as untrusted
  data and tool-level authorization remain the real controls (rules 2, 4, 5).
- **Chunk size is a guess** (~350 tokens, 50 overlap, header "Title > Heading"). It is a knob the
  eval should sweep, not a fact.

## Measured (owner's Mac, 2026-10-06) — this changes the decision's default
Plain RRF (k=60, 40 candidates per branch) scored **below** the better single branch for both
query types (hybrid MRR@10 0.505 vs keyword 0.662; paraphrase R@5 0.635 vs vector 0.865).
Mechanism: RRF rewards documents present in both lists; with deep lists and k=60, "mediocre in
both" (2/90) beats "#1 in one" (1/61). Fusion parameters are therefore no longer defaults we
trust - they are chosen by `make rag-sweep` on a dev split and confirmed on a held-out test
split, then written back here with the numbers. LLM rerank on the local CPU model timed out on
every call (25 s budget): not viable on this hardware.

### Adopted defaults (Phase 6 close-out, done in the Phase 7 change)
Sweep file `rag-sweep-2026-10-06T043023.json` (owner's Mac, `nomic-embed-text`): best on the
**dev** split = `rrf_k=1, depth=40, keyword_weight_identifiers=2, vector_weight_no_identifiers=4`.
On the **held-out test** split: MRR@10 **0.799** (95% CI 0.71–0.88) vs **0.598** (0.48–0.71) for
plain RRF k=60; R@5 0.963. These are now the code defaults (`aeoi_rag.config`), replacing
decision item 2's "k=60, weight 1.0".
- **Caveat (say it every time):** the identifier routing matches how the eval set was built
  (keyword queries contain planted identifiers). Real queries are messier. The two CIs just touch
  (0.71) and the test split is small (about half of the 108 ranked queries), so the true gain
  may be well below 0.2 MRR.
- **Invalid run, not quoted:** `rag-2026-10-06T043554.json` (RERANK=1) - every rerank call failed,
  p95 86 s, and searches silently fell back to keyword-only. The eval now counts `degraded`
  queries per mode and marks such a mode `valid: false` / INVALID in the table (regression test).
- Runbooks are now indexed with source `runbook` (a document KIND, parsed as markdown), so the
  `search_runbooks` tool can filter on it. Re-run `make rag-ingest` once (the pack's runbooks
  change source; the content hash check also compares source, so they are re-indexed).

## Consequences
- Phase 6 gate = `make rag-eval` on the Mac: leakage 0, quarantine 0, recall@k and MRR recorded
  with the embedding model and dataset hash. CI runs the same eval with fake embeddings as a
  leakage/quarantine gate only.
- Agents (Phase 8) search with the **user's** token (on-behalf-of); a service token alone has no
  groups and sees nothing.

## Prototype vs Production vs Enterprise-scale
[P] one Postgres, CLI ingestion, in-process cache-less retrieval, regex PII.
[Prod] async ingestion via `DocumentUploaded` events (Phase 18), pgvector ≥ 0.8 iterative scan,
NER-based PII, reranker chosen by eval, retrieval audit events (Phase 7 audit service).
[Ent] per-tenant indexes or partitions, BM25 extension, document-level encryption keys,
continuous retrieval evals on production query samples (with consent).
