# Retrieval eval results

`make rag-eval` writes one JSON file per run here. Each file records what produced it
(embedding model, pgvector version, dataset hash, git commit, host).

Commit **only** runs with a real embedding model (`environment.is_baseline: true`).
Runs with `fake-embed` (CI, sandbox) measure plumbing, not retrieval quality.

## Known-invalid runs (kept for the record, never quoted)
- `rag-2026-10-06T043554.json` (RERANK=1): every rerank call failed (25 s budget, CPU model),
  p95 86 s, and searches silently fell back to keyword-only — the "hybrid" numbers are not
  hybrid. Since Phase 7 the eval counts degraded queries and marks such a mode `valid: false`.
