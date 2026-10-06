# llm-gateway (Python 3.12 / FastAPI) — local port 8005

**Purpose:** The ONLY path to a model. Routes (`reasoning`, `fast`, `local`, `embed`) → model
chain → redact → bulkhead → circuit breaker → retry → validate → record usage. ADR-014.
**Owns schema:** `llm` (`model_usage` append-only for this role — migration 0014; `prompt_versions`)
**Callers:** services only, service token with scope `llm:invoke`. User tokens → 403.
**Must never:** expose provider keys; fall back silently (always `fallback_used` + `attempts` +
status FALLBACK); fall back on 400/413/422; fall back for embeddings; send unredacted text to a
`hosted: true` provider; let a caller pick a raw model name.

## Layout (`src/aeoi_llm/`)
- `providers/` — `base.py` (neutral types, error classes, HTTP status → retry semantics),
  `anthropic.py` (Messages API, forced tool use for JSON), `openai_compat.py` (Ollama/OpenAI/Gemini/vLLM),
  `fake.py` (deterministic, tests), `sse.py`
- `gateway.py` — routing, fallback, structured validation + 1 repair, cache, budget, stream, embed
- `resilience.py` — retry (full jitter), CircuitBreaker, Bulkhead
- `config.py` — env Settings + YAML policy model (validated at boot); `config/routing*.yaml`
- `usage.py` — cost formula, `DbUsageSink`, `MemoryUsageSink` · `api.py` — HTTP · `main.py` — factory

## Rules when changing this service
- New provider: implement `Provider` protocol + tests with `httpx.MockTransport`. OpenAI-compatible
  vendors need YAML only (`kind: openai_compat`).
- Prices: YAML only, with a source note. Never quote a cost number that didn't come from `llm.model_usage`.
- Never log prompt or completion text (only sizes, tokens, ids). Redaction count is fine.
- Tests: `uv run pytest services/llm-gateway` (unit, fakes) and
  `tests/integration/services/test_llm_usage.py` (real Postgres, least-privilege login).
