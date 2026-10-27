# ADR-042: LLM failures - an open circuit fails over at once; a schema failure says WHERE

Status: Proposed (28b built 2026-10-27 from stored data; no paid run)
Date: 2026-10-27
Refines: ADR-014 (gateway resilience), ADR-040 (Phase 28)

## Context (data from the owner's Mac, read 2026-10-27)
- `historical_incident` showed 11 `StructuredOutputError` rows (2026-10-14..23) that could not be
  diagnosed: `llm.model_usage` stores only the class. The agent's traces show what the CALLER
  saw instead: 10 x "504 llm-timeout: No answer within 40.0s (retries and fallbacks included)".
  Haiku's output failed the schema (after one repair), the gateway fell back to `llama3.2:3b` on
  the Intel CPU, and the agent's 40 s deadline ran out - the real cause was hidden twice.
- 2026-10-23 03:03:10-03:03:42: hosted Haiku and Sonnet AND the local Ollama models (chat and
  embeddings) all timed out within 32 s. Both a hosted and a local provider at once is not a
  provider outage - it is this machine (the same days the disk was 97-99 % full, phase-28.md
  section 4). The breakers then opened on every model and refused for at least 4.5 min.
- Every refusal row took 5-7 s: `CircuitOpenError` is a `TransientError`, so `with_retries`
  slept and retried the breaker's own refusal before the gateway could fail over - against the
  class's own docstring ("don't spend the caller's deadline on it").

## Decision
- **An open circuit is not retried:** `with_retries` re-raises `CircuitOpenError` at once; the
  gateway fails over to the next model immediately (test: 1 call, 0 sleeps).
- **A structured failure records WHERE, from the schema side only:** `model_usage.error_detail`
  (migration 0029, varchar 300) = the failing schema paths with their keyword (e.g.
  `properties/severity/enum`), for the first answer and the repair, or `cut_off:max_tokens`.
  Never the jsonschema message and never the instance path: both can quote the model's text
  (test: a planted value never reaches the row). obs_reader gets no grant on it.

## Not decided here (needs data first)
- Whether a `fast` route should fall back to a 3B model on the Intel CPU at all, when the
  caller's deadline is 40 s: a query of the fallback's OK rate decides it (phase-28.md).
- Why the breakers refused for >= 4.5 min after a ~30 s stall with no trial call recorded: a
  trial cancelled by the caller's deadline leaves no row. Breaker state changes are not logged,
  so 2026-10-23 cannot be reconstructed - logged transitions are the next step if it recurs.

## Tradeoffs
- One more column on an append-only table (additive, nullable). A 300-char cap may cut a long
  path list (marked with `…` / `+n`).
