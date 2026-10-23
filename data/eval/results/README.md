# Eval results

`make rag-eval` writes one JSON file per run here. Each file records what produced it
(embedding model, pgvector version, dataset hash, git commit, host).

Commit **only** runs with a real embedding model (`environment.is_baseline: true`).
Runs with `fake-embed` (CI, sandbox) measure plumbing, not retrieval quality.

## Known-invalid runs (kept for the record, never quoted)
- `rag-2026-10-06T043554.json` (RERANK=1): every rerank call failed (25 s budget, CPU model),
  p95 86 s, and searches silently fell back to keyword-only — the "hybrid" numbers are not
  hybrid. Since Phase 7 the eval counts degraded queries and marks such a mode `valid: false`.

## Investigation eval (Phase 25a, ADR-037)
`make eval-investigations` writes `investigations-*.json` here (and the same into the `eval`
schema). FAKE-model runs (CI, sandbox) measure the CODE path only and are not committed; the
committed regression baseline is `data/eval/baselines/investigations-v1-fake.json`. Every
number is on 16 planted scenarios: an upper bound, never "accuracy" without that sentence.
Runs with `"invalid"` cases (incomplete evidence, e.g. rate-limited queries) are never quoted.
Committed (25b): the real-model run `investigations-2026-10-23T033835-rescored.json` (Haiku +
Sonnet on kind, $0.98, re-scored with the status/proposal metrics) and its
`calibration-2026-10-23T041541.json` (vs a FAKE sandbox run). Calibration is always quoted
with its Wilson interval.
