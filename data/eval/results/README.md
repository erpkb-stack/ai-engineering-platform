# Retrieval eval results

`make rag-eval` writes one JSON file per run here. Each file records what produced it
(embedding model, pgvector version, dataset hash, git commit, host).

Commit **only** runs with a real embedding model (`environment.is_baseline: true`).
Runs with `fake-embed` (CI, sandbox) measure plumbing, not retrieval quality.
