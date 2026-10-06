# Interview prep — Phase 5: LLM gateway

## Q1. Why build an LLM gateway instead of calling the SDK from each agent?
**30-second answer:** Four things must happen on *every* call: a cost record, a budget check, secret redaction, and failover. If each agent does them, one will forget. So there is one gateway. Agents choose a *route*, like "reasoning" or "fast". The gateway maps the route to a model chain in a reviewed YAML file. It is the only place that holds API keys. Only services can call it; a user token gets 403.

**2-minute answer:** Start from the KPI: cost per investigation. To measure it, every call must write model, tokens, latency, cost, prompt version and investigation id. That is rule 6 of the repo. Then add failure: providers return 429 and 529 under load, so you need retries with jitter, a circuit breaker and a fallback. Then add security: incident prompts contain log lines, and log lines contain secrets. Put all three in every agent and they drift. In one gateway they are tested once. Two design choices matter most. First, callers choose a route, not a model, so a model change is a config review, not a code change in ten agents. Second, fallback is never silent: every response says which model answered and lists the attempts.

**Tradeoff:** one more network hop and one more service to run. I accepted that because LLM latency is seconds and the hop is milliseconds.

**Common mistakes:** letting callers pass a raw model name (the policy is gone); retries inside the vendor SDK *and* in your code (they multiply); logging prompts "for debugging".

**Follow-ups:**
- "Why not LiteLLM?" It's a good choice. I needed my own audit rows, redaction and "never silent" rules, and only two wire formats. I would revisit it beyond ~4 providers. (ADR-014)
- "Why only two adapters for four vendors?" OpenAI, Gemini and Ollama all accept the OpenAI chat format, so they share one adapter.

## Q2. When should a gateway fall back to another model — and when not?
**30-second answer:** Fall back when the *provider* is the problem: timeout, 429, 5xx, Anthropic's 529, open circuit, missing key. Never fall back when the *request* is the problem: 400, 413, 422. A smaller model would hide my bug or fail the same way. Never fall back for embeddings: two models give vectors in different spaces, so search returns garbage with no error. And never silently: the response has `fallback_used` and the attempts list.

**2-minute answer:** The danger of fallback is that it looks like success. My fallback for "reasoning" is a 3B model on a CPU. Its answer is a hint, not a finding. So the gateway marks it, writes status FALLBACK in the usage table, and the orchestrator in a later phase must lower confidence or ask a human. Callers that can't accept a weaker model send `allow_fallback=false` and get 503. One more rule: if the provider says "retry after 60 seconds", I don't wait; I fall back. Waiting a minute inside an incident is worse than a weaker first answer. A short retry-after, under 10 seconds, I honour.

**Follow-up — "How do you know your fallback rules work?"** Unit tests for each class of error, and a mutation check: I removed the "no fallback on 400" rule and a test failed.

## Q3. How do you get reliable JSON out of an LLM?
**30-second answer:** Three layers. Use the provider's native constraint: with Claude, a forced tool call whose input schema is my JSON Schema; with OpenAI-compatible APIs, `response_format: json_schema`. Then validate it myself with a JSON Schema validator. If it fails, I send the validation errors back once and ask for a correction. If it still fails, the caller gets a 422 with a stable error type. Every attempt is recorded, because failed attempts also cost tokens.

**Follow-ups:**
- "Why validate if the provider already constrains output?" Small local models often ignore the constraint. And providers support different subsets of JSON Schema. Trust, but verify.
- "Why only one repair?" Each repair is a full paid call. If one round doesn't fix it, the prompt or schema is the problem.

## Q4. How do you stop secrets leaking to a hosted model?
**30-second answer:** Before any provider marked `hosted: true`, the gateway removes known secret patterns from the system prompt and the messages: API keys, GitHub tokens, AWS keys, bearer tokens, JWTs, passwords in connection strings. The response says how many were removed, never what. The `local` route can't reach a hosted model; a test checks the config. In the sandbox I checked on the *receiving* mock server that the secret never arrived.

**Honest limit:** this is regex. It doesn't catch PII or new secret formats. PII scrubbing is a later phase. Don't oversell it.

## Q5. How do you control LLM cost?
**30-second answer:** Route by need: cheap model for classification, strong model for reasoning. A budget per investigation is checked before each call, from `SUM(cost_usd)` in the usage table. Deterministic calls (temperature 0) are cached, but only answers from the primary model. Every attempt has a cost row. The usage table is append-only for the service role, so a bug can't erase spend.

**Tradeoff:** the budget is check-then-call, so it can overshoot by one call. At enterprise scale, I would reserve the estimated cost first and settle after.

**Common mistake:** quoting cost numbers from a price list instead of from your own usage table. My prices come from a third-party snapshot that I mark "verify".

## Q6. Explain your resilience patterns in one minute.
- **Retry:** only retryable errors, exponential backoff with *full jitter* so clients don't retry in sync. Max 3 attempts.
- **Circuit breaker:** opens after 5 retryable failures in a row; after 30 s one trial call. A 400 does not count: a bad prompt says nothing about provider health.
- **Bulkhead:** a concurrency limit per provider. Ollama on a CPU gets 1. Queueing 20 requests behind it only turns load into timeouts.
- **Deadline:** one timeout (120 s) for the whole request, including retries and fallbacks. Per-attempt timeouts alone can add up to minutes.

## Q7. Streaming: what changes?
**30-second answer:** Fallback is possible only before the first token. Once text reaches the user, I can't switch model in the middle of a sentence. So retries and the breaker cover "get the first chunk"; after that, a failure ends the stream with an `error` event. The concurrency slot is held until the stream ends. I read the first event *before* sending HTTP 200, so a bad route returns a proper 400 instead of a stream that starts and dies.

## Resume bullet (only after you run it on your Mac)
"Built an LLM gateway (FastAPI) with route-based model selection, transparent fallback, JSON-Schema-validated structured output with repair, secret redaction before hosted providers, circuit breaking and per-investigation budgets; every call cost-accounted in Postgres."
Do **not** add latency, cost or quality numbers until they come from your own `llm.model_usage` rows or eval runs.
