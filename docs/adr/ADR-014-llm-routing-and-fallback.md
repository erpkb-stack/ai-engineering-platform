# ADR-014: LLM routing policy, fallback and cost accounting in one gateway

Status: Proposed
Date: 2026-10-05
Supersedes: —

## Context
Agents (Phase 8+) need text, structured JSON, streams and embeddings. Constraints:
- Owner's Mac is an Intel CPU: local models are small (≤3–4B) and slow; quality gap vs Claude is large.
- A hosted model receives our prompts, which will contain logs. Logs contain secrets.
- Every call must record model, prompt version, tokens, latency and cost (AGENTS.md rule 6).
- Anthropic has no embeddings API, so embeddings need a different provider anyway.

## Decision
1. **Callers choose a ROUTE, never a model.** Routes (`reasoning`, `fast`, `local`, `embed`)
   map to an ordered model chain in `services/llm-gateway/config/routing.yaml`. Policy changes
   are config + review, not code. The YAML is validated at startup (unknown model/key = no boot).
2. **Fallback is allowed but never silent.** Every response carries `model`, `fallback_used`
   and `attempts[]`; the usage row is stored with status `FALLBACK`. A caller that cannot accept
   a weaker model sends `allow_fallback=false` and gets 503.
3. **When to fall back:** retryable errors (timeouts, 429, 5xx, 529 overloaded), open circuit,
   full bulkhead, missing key, auth errors (provider-side problem). **Never** on 400/413/422:
   the request is wrong, and a fallback would hide our bug → `llm-request-rejected` (400).
4. **Embeddings never fall back** (validated in config). Vectors from two models live in
   different spaces; a fallback would silently corrupt similarity search. A dimension mismatch
   is refused.
5. **Redaction before hosted providers:** secret patterns are removed from system + messages
   before any `hosted: true` provider. The `local` route has no hosted model in its chain (tested).
6. **Structured output:** provider-native constraint (Claude: forced tool use; OpenAI-compatible:
   `response_format: json_schema`) **plus** our own JSON Schema validation, plus exactly one
   repair round-trip. Native constraints are not trusted alone: small local models ignore them.
7. **Resilience per provider:** retries with full jitter (only retryable errors; a `retry-after`
   > 10 s triggers fallback instead of waiting), a circuit breaker (only retryable failures count),
   a bulkhead (Ollama: 1 in flight), and ONE deadline for the whole request (120 s).
8. **Accounting:** one `llm.model_usage` row per **attempt** (failed attempts cost time and
   sometimes money). Table is append-only for the service role (migration 0014). Budget per
   investigation is checked **before** each call.
9. **Cache:** exact-match, temperature 0 only, primary-model answers only, key includes the
   model chain (a policy change invalidates it).
10. **Only services call the gateway** (service token with scope `llm:invoke`); user tokens get 403.

## Alternatives
| Option | Why not (here) |
|---|---|
| Agents call the Anthropic SDK directly | Keys in every service; no central budget, redaction or cost record; provider lock-in in agent code. |
| LiteLLM proxy | Strong option with 100+ providers. Rejected for now: we need our own audit rows, our redaction, our "never silent" semantics, and the interview value of owning ~600 lines. Revisit if we need >4 providers. |
| Official vendor SDKs inside the gateway | Fine choice. Raw httpx chosen: one retry/timeout layer we control and test with MockTransport, no SDK-internal retries stacking on ours. Cost: we track API changes ourselves. |
| tenacity / pybreaker | `.claude/rules/python.md` said tenacity. Our policy needs "don't wait for a long retry-after, fall back" and breaker counting only retryable errors; ~100 tested lines were simpler than configuring both. Rule updated. |
| Semantic (embedding-similarity) cache | Returns an answer to a *different* question. Unsafe for incident reasoning. |

## Tradeoffs
- **Budget can overshoot by one call** (check-then-call, not a reservation). With a $2 budget and
  ~$0.01–0.10 calls that is acceptable. Enterprise: reserve estimated cost atomically, settle after.
- **Usage write failure does not fail the response** (`usage_recorded=false`, error log). We chose
  availability over accounting. Consequence: a DB outage weakens the budget. Alternative: fail
  closed. Revisit when spend is real money.
- **Fallback quality gap is large** (Claude vs a 3B CPU model). For `reasoning`, a fallback answer
  is a degraded hint, not a finding. The orchestrator (Phase 8) must lower confidence or ask for
  human review when `fallback_used=true`.
- **In-process cache and breaker** are per replica. Prod: Redis cache; breaker state per pod is fine.
- **Redaction is pattern-based** (keys, tokens, JWTs, DSN passwords). It does not catch PII or
  unknown secret formats. PII scrubbing comes in Phase 6/26.
- **Prices in YAML are a third-party snapshot** and must be verified. Cost numbers are only as good
  as that table.

## Consequences
- Agents depend on `aeoi_llm_client.LLMClient` (protocol); unit tests use `FakeLLMClient`.
- New provider = one adapter (`Provider` protocol) + YAML. OpenAI, Gemini (OpenAI-compatible
  endpoint), vLLM need **no code**: `kind: openai_compat` with a `base_url`.
- KPIs "cost per investigation" and "fallback rate" are SQL over `llm.model_usage`.

## Prototype vs Production vs Enterprise-scale
[P] in-process cache/breaker, YAML policy, pattern redaction, check-then-call budget.
[Prod] Redis cache, metrics (fallback rate, breaker state, p95 per model), alerts on spend,
key from Secrets Manager, prompt caching enabled for long stable system prompts.
[Ent] per-tenant budgets with reservations, policy as code with approval, regional routing
for data residency, eval-gated model upgrades (a new model must pass the Phase 21 eval suite).
