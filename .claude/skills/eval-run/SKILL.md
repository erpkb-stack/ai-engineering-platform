---
name: eval-run
description: Run and compare AI evaluation experiments (prompt/model/retrieval versions) against a versioned dataset and record real results. Use when changing a prompt, model, retriever or agent version, or when asked "is v13 better than v12".
argument-hint: "<baseline-config> <candidate-config>"
---
# Evaluate: $ARGUMENTS

1. Confirm dataset version (`data/eval/<name>/vN/`) and that it is NOT used in any few-shot prompt (leakage check).
2. Run both configs: `uv run python -m evaluation.run --config <cfg> --dataset <ds> --seed 42`.
3. Metrics (definitions in architecture.md §20): answer correctness, retrieval recall@k / MRR, groundedness, citation accuracy, hallucination rate, tool success, p50/p95 latency, tokens, $ cost.
4. Report deltas with sample size and a 95% bootstrap CI. If n < 50, say the result is not statistically meaningful.
5. Store results in the `eval` schema; never type numbers into docs by hand.
6. LLM-as-judge: use a different model family than the system under test where possible, and spot-check ≥10 judgments manually.
