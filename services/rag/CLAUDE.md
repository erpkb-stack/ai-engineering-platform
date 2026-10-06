# rag (Python 3.12 / FastAPI) — local port 8004 — package `aeoi_rag`

**Purpose:** ingestion (parse → redact secrets → scrub PII → injection tripwire → chunk → embed)
and permission-aware hybrid retrieval (pgvector HNSW + Postgres FTS, RRF, optional LLM rerank). ADR-016.
**Owns schema:** `rag` (documents, document_chunks; runbooks/historical_incidents are seeded here too)
**Callers:** users via the api gateway (`/api/v1/search`, `/api/v1/documents/{id}`), perm `docs:read`.
Agents (Phase 8) forward the USER's token. A service token alone has no groups → sees nothing.
**Must never:** return a chunk without the permission filter IN the SQL; take groups from a request
body; index a file without an explicit ACL; chunk a quarantined document; call a model SDK
(embeddings/rerank go through `aeoi_llm_client`).

## Code map (`src/aeoi_rag/`)
- `parsing.py` md/txt/html/json/code/pdf → sections (+ hidden HTML text for the injection scan)
- `chunking.py` section-bounded chunks, overlap, "Title > Heading" header
- `ingest.py` per-document transaction; `embed_pending` batch + commit (resumable)
- `search.py` the ONE hybrid SQL; `identifier_terms` (IDF stand-in); defence-in-depth ACL assert
- `rerank.py` listwise graded LLM rerank; RESTRICTED → local route; failure → RRF order
- `evaluation.py` recall@k / MRR@10 / leakage / quarantine with bootstrap CIs
- `__main__.py` CLI: ingest · chunk-db · embed · stats · eval · sweep (fusion grid, dev/test) · bench
- `fuse()` in search.py is the Python twin of the SQL fusion - keep them identical (integration test)

## Commands
`make rag-token` · `make rag-ingest` · `make rag-eval [RERANK=1]` · `make run-rag` · `make rag-smoke`
Corpus: `make docpack` (deterministic; `data/sample-documents/` + `data/eval/rag_queries_v1.jsonl`).

## Rules when changing retrieval
- Any change to SQL, chunking or ranking → run `make rag-eval` before and after; compare files in
  `data/eval/results/`. No claim of "better" without that diff.
- New document source → add parser tests AND leakage-test coverage.
