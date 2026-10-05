# rag (Python 3.12 / FastAPI) — local port 8004

**Purpose:** Ingestion (parse, chunk, metadata, PII scrub, embed) and hybrid retrieval (pgvector + Postgres FTS + metadata + permission filter) with reranking.

**Owns schema:** `rag` (documents, document_chunks)
**Events:** consumes DocumentUploaded; publishes DocumentIndexed
**Must never:** Return a chunk without applying the caller's permission filter in SQL.
**Why this is a separate service:** Ingestion is batch/heavy; retrieval is latency-sensitive; both share the corpus.

Status: Phase 1 — design only. Scaffold with the `new-service` skill in its phase (see docs/roadmap.md).
