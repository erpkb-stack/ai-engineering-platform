# Phase 6 — RAG pipeline

## 0. Gate and scope challenge
- **Gate:** Phase 5 smoke passed on your Mac, including `embed` (you confirmed). Phase 6 depends on it: every vector comes from `nomic-embed-text` through the gateway.
- **Challenge 1 — the corpus was useless for evaluation.** The Phase 3 seed has 1,100 documents of 1–3 template sentences each (average 164 characters, all markdown). Any recall number on it would be meaningless. So Phase 6 adds a **document pack**: 60 realistic documents in all 6 formats (md, pdf, html, json, code, txt), each with a planted rare identifier and a planted fact. That gives 116 eval queries: 56 keyword, 52 paraphrase, 4 leakage, 4 adversarial. The 1,100 seed documents stay in the index as distractors.
- **Challenge 2 — no upload API in this phase.** Nobody uploads documents until the UI exists (Phase 17), and a sync upload endpoint with no consumer for `DocumentUploaded` (Phase 18) would be a half-feature. Ingestion is a CLI (`make rag-ingest`). The spec's search and document endpoints are real and go through the gateway.
- **Challenge 3 — your reranker choice.** You chose LLM rerank. On your CPU, one rerank call with 10 passages through `llama3.2:3b` will likely take many seconds (measure it). So it is built, and the eval measures it, but it is **off by default**. It turns on only if `make rag-eval RERANK=1` shows a recall gain that is worth the latency.
- **New:** ADR-016 (hybrid retrieval), migration 0015 (`quarantine_reason`), `libs/security` PII scrubber and injection tripwire, `aeoi_llm_client` now maps network errors to `LLMError`.

## 1. Objective
Permission-aware hybrid retrieval that never leaks a restricted document, with a measured baseline (recall@k, MRR) that later phases can improve against.

## 2. Business reason
Every investigation agent will ground its claims in retrieved runbooks, postmortems and configs. Two failures matter:
- **wrong or missing context:** the agent guesses, and MTTI does not improve.
- **a leaked restricted document:** a security incident caused by the AI tool.
The eval measures the first. Leakage = 0 is a hard gate for the second.

## 3. Architecture
```
make rag-ingest (CLI, rag_svc login)                       POST /api/v1/search (user JWT)
  manifest.json (ACL per file) ─┐                            api gateway: 401→429→403(docs:read)
  parse (md/pdf/html/json/code/txt)                                │ forwards user token
  redact secrets → scrub PII → injection tripwire ─► quarantine     ▼
  chunk (section-bounded, overlap, "Title > Heading")        rag :8004  groups FROM TOKEN only
  one transaction per document                                     │
  embed_pending: batches → llm-gateway /v1/embed ◄── service token ─┤ embed query ("search_query: …")
                                                                   ▼
                     ONE SQL statement:  vec branch  (HNSW, ACL + NOT quarantined, LIMIT 40)
                                         kw branch   (FTS OR-query + identifier boost, ACL, LIMIT 40)
                                         RRF fusion  (k=60) → max 2 chunks/doc → top k
                                                                   │ optional (off by default)
                                                                   ▼
                     LLM rerank: 1 call, top 10, graded 0-3, untrusted-wrapped, RESTRICTED → local route
```
| Guarantee | Enforced by | Test |
|---|---|---|
| No restricted chunk for an outsider (all modes) | ACL inside each SQL branch + Python assert | `test_leakage_matrix_is_zero` (≥100 combos), mutation-tested |
| Members still find their documents | same | `test_members_do_find_restricted_docs` |
| ACL change applies at once | trigger 0011 + filter | `test_acl_change_takes_effect_immediately` |
| Quarantined docs never returned, even after indexing | `NOT d.quarantined` in both branches | `test_quarantine_after_indexing_hides_existing_chunks`, mutation-tested |
| Restricted document existence not revealed | GET returns 404, not 403 | `test_get_document_404_for_outsider_200_for_member` |
| Groups can't be smuggled in the body | `extra="forbid"` | `test_groups_in_body_are_rejected` |
| PII never stored | scrub before storing | `test_pii_never_stored` |
| No ACL → not indexed | manifest required | `ingest_pack` rejects unlisted files |
| Gateway down → keyword-only answer, says so | `degraded` field | `test_gateway_down_hybrid_degrades_vector_fails` |

## 4. Files
```
services/rag/  pyproject.toml CLAUDE.md
  src/aeoi_rag/ config.py db.py parsing.py chunking.py embedder.py ingest.py search.py rerank.py
                evaluation.py api.py main.py __main__.py
  tests/ test_parsing_chunking.py test_rerank.py
libs/security/src/aeoi_security/{pii.py, injection.py} + tests/test_pii_injection.py
libs/models/src/aeoi_models/api/search.py            (shared contract: rag + api gateway)
libs/llm-client (transport errors → LLMError)
libs/db: models/rag.py (quarantine_reason) · alembic/versions/0015 · users.py (+rag_svc)
scripts/synth/src/aeoi_synth/{docpack.py, pdfmini.py} + tests/test_docpack.py · __main__ (docpack)
data/sample-documents/** (60 files + manifest.json) · data/eval/rag_queries_v1.jsonl · data/eval/results/README.md
services/api: routes.py (/search, /documents/{id}) · config.py · main.py · devtoken.py (--group/--without-group)
services/llm-gateway: fake embeddings = hashed bag-of-words · routing.test.yaml (+fast)
tests/integration/rag/{conftest.py, test_rag.py}
scripts/smoke-phase6.sh · Makefile · .github/workflows/ci.yml · .pre-commit-config.yaml · pyproject.toml
docs/adr/ADR-016-hybrid-retrieval.md · docs/phases/phase-06.md · docs/interview/phase-06-rag.md
CLAUDE.md · docs/roadmap.md · architecture.md · docs/data-model.md · .claude/settings.json
```

## 5–7. Commands (macOS / zsh)
```zsh
cd ~/projects/ai-engineering-platform
git switch -c phase-6-rag                       # from phase-5-llm-gateway
uv sync --all-packages                          # new dep: pypdfium2 (binary wheel)
make up && make db-upgrade && make db-users     # 0015 + login rag_svc
make check                                      # lint + mypy + unit tests

make run-llm LLM_ROUTING=routing.local.yaml     # terminal 1 (Ollama: nomic-embed-text)
make rag-token                                  # rag -> gateway service token (dev, 30 days)
make rag-ingest                                 # 60 pack docs + 1,100 seed docs, then embeds (progress + chunks/s)
make rag-eval                                   # baseline: keyword / vector / hybrid
make rag-eval RERANK=1                          # + hybrid+rerank (aborts after 3 failed calls)
make rag-sweep                                  # fusion settings: tune on dev, report on test
make rag-bench                                  # embedding chunks/s + query-embedding latency

make run-rag                                    # terminal 2  (:8004)
make run-api                                    # terminal 3  (:8000)
make rag-smoke                                  # terminal 4
make test-integration
```
**Configuration** (env `AEOI_RAG_*`): `LLM_GATEWAY_URL`, `RERANK_DEFAULT` (false), `RERANK_ROUTE` (fast), `RERANK_TIMEOUT_S` (25), `CHUNK_TARGET_TOKENS` (350), `RRF_K` (60), `KEYWORD_WEIGHT_IDENTIFIERS` (1.0). Api gateway: `AEOI_API_RAG_SERVICE_URL`, `AEOI_API_RAG_TIMEOUT_S` (35).

## 8–10. Tests and verification
**Verified in the sandbox** (Linux, Postgres 16 + pgvector **0.6**, real gateway process with **fake** embeddings):
- 245 unit tests and 96 integration tests pass (20 new for RAG). Ruff, mypy `--strict` and `alembic check` are clean.
- Ingestion: 56 indexed, 4 adversarial documents quarantined with reasons, 4 PII items scrubbed. One planted injection in the **Phase 3 seed** (`docs://northwind/00032`) was also caught. 0 false positives on the other 1,155 documents. Re-ingest: 60 unchanged.
- Leakage 0 and quarantine violations 0 in every mode. Two mutation tests: removing the ACL filter (and the Python assert) makes the leakage test fail; removing the quarantine filter makes the post-index quarantine test fail.
- Smoke through the api gateway: 8 ✔, 0 ✘.
- **Fake-embedding eval numbers are not reported as results.** They only proved the plumbing. They did find a real bug: `ts_rank_cd` has no IDF, so "tls" outranked the error code "2535". The fix was the identifier boost; keyword-query recall@1 went from 0.98 to 1.0 in keyword mode.

**Bugs the tests caught before you saw them:** (1) a single very long "word" was cut and its rest silently dropped. Fixed: it is now split into slices, never dropped. (2) The overlap tail made chunks larger than the target. (3) The phone pattern matched 4-4-4 digit groups. (4) `make help` printed "Makefile" instead of target names, because `.env` is also in `MAKEFILE_LIST`. That bug was from Phase 3.

**Your Mac must show (Phase 6 is done when):**
| Check | Expected |
|---|---|
| `make rag-ingest` | `{"indexed": 56, "quarantined": 4}`, then 1,099 + 1 for the seed, `pending: 0` in `make rag-stats` |
| `make rag-eval` | leaks **0**, quarantine **0**; `embedding_model=nomic-embed-text…`, `is_baseline: true` |
| baseline file | `data/eval/results/rag-<timestamp>.json`: **commit it**. It is the number Phase 13/25 must beat |
| `make rag-smoke` | 0 ✘ (notes are allowed) |
| CI | the step "RAG ingest + eval gate" is green |

Write down your real numbers: embedding `chunks_per_s`, search p50, and rerank latency. Those are the only numbers you may quote.

### Baseline v1 — measured on the owner's Mac (Intel, Ollama `nomic-embed-text`, pgvector 0.8.7)
From `data/eval/results/rag-2026-10-06T040533.json`; 108 relevance queries; leakage 0, quarantine 0 in every mode.

| mode | all R@5 | all MRR@10 | keyword-type R@1 | paraphrase-type R@5 | p50 latency |
|---|---|---|---|---|---|
| keyword | 0.75 | 0.662 | **1.0** | 0.481 | 82–255 ms |
| vector | 0.556 | 0.361 | 0.125 | **0.865** | ~171 ms |
| hybrid (plain RRF, k=60, depth 40) | 0.667 | 0.505 | 0.464 | 0.635 | 337–672 ms |

**Finding: plain RRF hybrid was worse than the better single branch for BOTH query types.**
- Each branch is strong where the other is weak: keyword wins on identifiers, vector wins on descriptions. That part was expected.
- Why fusion lost: with k=60 and 40 candidates per branch, a document that is #1 in one branch only scores 1/61 = 0.0164. A document ranked about 30th in both branches scores 2/90 = 0.0222. RRF rewards agreement, and here the branches agree mostly on mediocre documents. A unit test now pins this arithmetic.
- **Rerank on CPU:** with the `fast` route on Ollama `llama3.2:3b`, every call hit the 25 s budget. LLM rerank is not viable on this machine. The eval now stops a rerank mode after 3 failed calls instead of running for about an hour. Options: test it with Claude Haiku (needs a key, ~1–2 s, costs money per query), or replace it with a small cross-encoder. Until then it stays off.

**Next step is measured, not guessed:** `make rag-sweep` gets each query's two candidate lists once. It then evaluates 135 fusion settings (rrf_k, depth, and the weights for identifier and descriptive queries) in Python. Tests prove the Python fusion gives the same order as the SQL. It picks the best setting on a **dev** half and reports it on a held-out **test** half.
**Caveat:** in this eval set every keyword question contains an identifier and no paraphrase question does, because of how the set was built. So "route by identifier" will look better here than on real queries. Report the test-split number, and say this when you show it.

## 11. Failure scenarios
| Symptom | Cause | Fix |
|---|---|---|
| `service token file … missing - run make rag-token` | no token | `make rag-token` |
| `password authentication failed for user "rag_svc"` | logins not created | `make db-users` |
| ingest: `model … returns 384-d vectors; schema expects 768` | wrong embedding model on the `embed` route | `make ollama-pull`; never change dims without a new column + backfill |
| eval: `vector` recall ≈ 0 after switching model | chunks embedded with another model are filtered out (`embedding_model = :model`) | `make rag-embed REEMBED=1` |
| search response `degraded: vector search unavailable` | gateway down / Ollama stopped | `make run-llm`; keyword results are still correct |
| `rerank.applied=false, error=timeout` | CPU rerank slower than 25 s (measured: every call on the Intel Mac) | keep rerank off; try `fast` on Claude Haiku, or a cross-encoder |
| `make rag-bench`/ingest: `SaturatedError: provider 'ollama' saturated (1 in flight)` | (fixed) abandoned rerank generations kept Ollama's only slot busy for up to 180 s after the client gave up; the slot was shared by ALL models | now: one slot per model, and the caller's deadline (`timeout_ms`) stops the gateway's work. If it still appears: `make stop-llm && make run-llm` |
| `make rag-eval RERANK=1` prints `ABORTED` | first 3 rerank calls failed | intended fail-fast; see the error in the JSON |
| a document you expected is missing | quarantined (see `make rag-ingest` output) or not in the manifest | read `quarantine_reason`; fix the doc, or release it with a reviewed SQL update |
| `PDF has no extractable text` | scanned image PDF | OCR is out of scope (documented) |

## 12. Production considerations
- **pgvector ≥ 0.8:** `iterative_scan` is enabled automatically when detected. Check `SELECT extversion FROM pg_extension WHERE extname='vector'` in the compose image.
- **Ingestion** becomes an event consumer (`DocumentUploaded`, Phase 18) with the same two-phase design. `embed_pending` already resumes after crashes.
- **Audit:** every search logs actor, retrieval id, chunk ids, and a hash of the query (not the text). The audit service (Phase 7) makes this durable.
- **Scale:** HNSW build after bulk load; `maintenance_work_mem`; partition chunks per tenant at enterprise scale; BM25 extension if keyword misses on frequent terms show up in evals.

## Known limitations (honest list)
1. The eval set is synthetic and written by the same author as the corpus. Paraphrase queries still share service names with documents. Real query logs (Phase 25) are the real test.
2. PII scrubbing is regex: no names, no free-text addresses.
3. The injection tripwire is heuristic; false negatives are expected. Wrapping and tool authorization are the real controls.
4. Chunk size, overlap and the RRF weight are guesses until swept by the eval on real embeddings.
5. HNSW + narrow ACL on pgvector < 0.8 can return fewer than k vector hits (keyword hits fill in).

## 13–15. Interview prep
See [`../interview/phase-06-rag.md`](../interview/phase-06-rag.md).
