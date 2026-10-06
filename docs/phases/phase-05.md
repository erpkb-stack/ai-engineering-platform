# Phase 5 — LLM Gateway

## 0. Gate and scope challenge
- **Gate:** Phase 4 must be merged (CI green) before you branch Phase 5. If the Phase 3 and 4 PRs are still open, merge them first. Otherwise the CI Kafka test has never run against a real broker.
- **Challenge to the spec: "provider abstraction for Claude, OpenAI, Gemini, Ollama".** Four adapters is cost with no benefit. OpenAI, Gemini and Ollama all speak the **OpenAI Chat Completions format** (Gemini has an OpenAI-compatible endpoint). So there are only **two adapters**: `anthropic` and `openai_compat`. A new vendor is YAML, not code.
- **Fact that shapes the design:** Anthropic has **no embeddings API**. Embeddings use a local model (`nomic-embed-text`, 768-d) through Ollama. That is also why the `embed` route has no fallback (see ADR-014 §4).
- **Your Intel Mac:** Ollama runs on CPU only. A 3B model is a *degraded fallback*, not a peer of Claude. The gateway reports every fallback so later phases can lower confidence.
- **Not in this phase:** prompt registry sync (Phase 8, with the first prompts), Prometheus metrics (Phase 23), Redis cache (Prod). `ModelUsageRecorded` events: dropped. The KPIs are SQL over `llm.model_usage`; an event with no consumer is unnecessary.
- **New ADR-014** (routing, fallback, accounting). **New migration 0014** (`model_usage` is append-only for the service role).

## 1. Objective
One service that is the only path to any model. It gives `generate`, `generate_structured`, `stream` and `embed`, with routing, fallback, redaction, retries, a circuit breaker, a concurrency limit, a cache, a budget per investigation, and one cost row per attempt.

## 2. Business reason
Without a gateway, every agent holds an API key, retries in its own way, and spends money with no limit and no record. Then "cost per investigation" (KPI) cannot be measured, a provider outage stops every agent, and log lines with secrets go to a third party. One gateway fixes all of this in one place.

## 3. Architecture
```
orchestrator / rag / eval ──(service JWT, scope llm:invoke)──► llm-gateway :8005
   aeoi_llm_client.LLMClient                                     │
                                                                 ├─ route → chain (routing.yaml, validated at boot)
                                                                 ├─ budget check (SUM cost_usd for investigation)
                                                                 ├─ cache (temp 0, primary answers only)
                                                                 ├─ for model in chain:
                                                                 │    redact (hosted only) → bulkhead → breaker → retry
                                                                 │    → adapter (anthropic | openai_compat | fake)
                                                                 │    → JSON Schema check + 1 repair (structured)
                                                                 │    → INSERT llm.model_usage (one row per attempt)
                                                                 └─ response: model, fallback_used, attempts[], usage, cost
```
| Route | Default chain (`routing.yaml`) | Why |
|---|---|---|
| `reasoning` | claude-sonnet-5-5 → llama3.2:3b | quality first |
| `fast` | claude-haiku-4-5 → llama3.2:3b | cheap classification / extraction |
| `local` | llama3.2:3b only | data must not leave the machine; no hosted fallback (tested) |
| `embed` | nomic-embed-text only | different models = different vector spaces; never fall back |

Fallback rules: yes on timeout, 429, 5xx, 529, open breaker, full bulkhead, missing key, 401/403. **No** on 400/413/422 (the request is wrong; a fallback would hide our bug).

## 4. Files
```
services/llm-gateway/  pyproject.toml  CLAUDE.md
  config/routing.yaml  routing.local.yaml  routing.test.yaml
  src/aeoi_llm/  main.py api.py gateway.py resilience.py config.py usage.py cache.py errors.py db.py
                 providers/{base,anthropic,openai_compat,fake,sse}.py
  tests/  conftest.py test_providers.py test_resilience.py test_config.py test_gateway.py
          test_api.py test_client_contract.py
libs/llm-client/  (aeoi_llm_client: LLMClient protocol, HttpLLMClient, FakeLLMClient)
libs/security/    service tokens (sub=service:<name>, scope claim), redact_text_counted
libs/web/         require_scope()
libs/db/          alembic/versions/0014_model_usage_append_only.py · users.py (+llm_svc)
services/api/src/aeoi_api/devtoken.py  (--service/--scope)
tests/integration/services/test_llm_usage.py
scripts/smoke-phase5.sh · Makefile (service-token, ollama-pull, run-llm, llm-smoke)
docs/adr/ADR-014-llm-routing-and-fallback.md · docs/interview/phase-05-llm-gateway.md
pyproject.toml (workspace, mypy, stubs) · .github/workflows/ci.yml · .claude/settings.json
.claude/rules/python.md · CLAUDE.md · docs/roadmap.md · architecture.md · .env.example
```

## 5–7. Commands (macOS / zsh)
```zsh
cd ~/projects/ai-engineering-platform
git switch -c phase-5-llm-gateway    # from phase-4-backend; the new files are already in the folder
uv sync --all-packages
make up && make db-upgrade && make db-users          # 0014 + new login llm_svc
make check                                            # lint + mypy + unit tests

# Option A - no key, $0, offline (needs Ollama)
brew install ollama && open -a Ollama                 # native app: no Docker RAM
make ollama-pull                                      # llama3.2:3b (~2 GB) + nomic-embed-text
make run-llm LLM_ROUTING=routing.local.yaml           # terminal 1
LLM_SMOKE_ROUTE=local make llm-smoke                  # terminal 2

# Option B - Claude primary + Ollama fallback (the real policy)
export ANTHROPIC_API_KEY=...                          # in your shell, NOT in .env
make run-llm                                          # terminal 1
make llm-smoke                                        # terminal 2 (route "fast" = Haiku, cents)

# Option C - no key and no Ollama: fake providers (what CI runs)
make run-llm LLM_ROUTING=routing.test.yaml
LLM_SMOKE_ROUTE=reasoning make llm-smoke
```
**Configuration:** `AEOI_LLM_ROUTING_FILE`, `AEOI_LLM_REQUEST_TIMEOUT_S` (120), `AEOI_LLM_CACHE_ENABLED`, `AEOI_LLM_RECORD_USAGE`. The key comes from `ANTHROPIC_API_KEY` or `secrets/anthropic_api_key.txt`. Prices are in the YAML, with a source note.

## 8–10. Tests and verification
**Verified in the sandbox (Linux, real Postgres 16):**
- 78 gateway unit tests + full unit suite green; `mypy --strict` and ruff clean; 76 integration tests pass (5 new for llm usage).
- Mutation checks: removing redaction, the budget check, the "no fallback on 400" rule, the schema validation, or the "don't cache fallbacks" rule each makes a test fail.
- Real uvicorn process + real DB, three ways:
  1. **No key + mock Ollama server:** primary `skipped` (reason: no API key), fallback answered, `fallback_used=true`, structured output **repaired** after one invalid answer, stream, embed 768-d, rows with status `FALLBACK`.
  2. **Mock Anthropic HTTP server + key:** Haiku answered, cost `$0.000080` for 30/10 tokens (matches the YAML price), 2nd call from cache, secret replaced by `[REDACTED]` in what the mock server received (checked on the server side), HTTP 529 → 3 attempts → fallback.
  3. **Fake providers** (the CI step): 12/12.

**Not verified (I can't, from here):** a real Claude call and a real Ollama model. Mock servers prove our side of the wire format, not the vendor's. Two things to check carefully on your Mac:
- Ollama structured output via `response_format: json_schema` and streamed usage (`stream_options.include_usage`). If your Ollama version ignores them, the gateway still works (validation + repair; usage marked `estimated: true`), but tell me what you see.
- Real latency of `llama3.2:3b` on your CPU. Write down the number from the smoke output; don't guess it.

**Phase 5 is done when:** `make llm-smoke` passes on your Mac with Option A **or** B, and the CI step "LLM gateway smoke" is green.

## 11. Failure scenarios
| Symptom | Cause | Fix |
|---|---|---|
| Service won't start: `ValidationError ... unknown model` | typo in routing YAML | fix the YAML; validation at boot is on purpose |
| `password authentication failed for user "llm_svc"` | `make db-users` not run | `make db-users` |
| Every answer `fallback_used=true`, attempts say `no API key` | key not visible to the process | `export ANTHROPIC_API_KEY=...` in the same terminal as `make run-llm` |
| `llama3.2:3b=error(RetryableError: cannot reach http://localhost:11434/v1 (ConnectError))` | Ollama not running, **or** your shell has `HTTP_PROXY`/`ALL_PROXY` set (fixed: local providers now ignore proxy env) | `curl -s localhost:11434/api/tags`; `open -a Ollama`; the smoke preflight now checks this first |
| `make run-llm`: `[Errno 48] Address already in use` | an old gateway (often a leftover `--reload` worker) still holds :8005 | `make stop-llm` (kills it only if it is the gateway; otherwise shows who owns the port) |
| First call fails, all later calls say `CircuitOpenError` | the first failure opened that model's breaker (30 s). The **first** error is the real cause | read the first ✘ line, not the last |
| `attempts: ... CircuitOpenError` | 5 retryable failures in a row; breaker open 30 s | check provider status; it closes after one good trial |
| `llm-unavailable` 503 on `local` | Ollama not running | `open -a Ollama`; `curl localhost:11434/v1/models` |
| Embed: `returned 384-d vectors; config says 768` | a different model under that name | `ollama pull nomic-embed-text`; never change dims without re-embedding |
| 504 `llm-timeout` | 3B model on CPU + long output | lower `max_tokens`; use `fast`; raise `AEOI_LLM_REQUEST_TIMEOUT_S` for dev only |
| `llm-budget-exceeded` 429 | investigation spent its budget | intended; raise `budget_usd_per_investigation` in YAML with a reason |
| Smoke: `USER token -> 200` | `require_scope` removed | that is a security bug; `test_user_token_is_403_even_for_admin` must fail first |

## 12. Production considerations
- **Keys:** AWS Secrets Manager → env at start; rotate without redeploy (re-read on 401).
- **Cache and breaker:** Redis cache shared by replicas (same key). Breaker per pod is fine.
- **Budget:** reserve estimated cost atomically before the call, settle after (today: check-then-call, can overshoot by one call).
- **Prompt caching:** long, stable system prompts (Phase 8) should use Anthropic prompt caching. `cached_tokens` is already counted and priced.
- **Observability (Phase 23):** fallback rate, breaker state, p95 latency per model, cost per investigation, redaction count.
- **Data residency:** a per-tenant policy file can force `local` or an in-region provider.

## Known limitations (honest list)
1. Redaction is regex-based: known key/token formats only, no PII. Phase 6/26 adds PII scrubbing.
2. A usage-write failure does not fail the response (`usage_recorded=false`). Availability over accounting; see ADR-014.
3. The cache is per process and is lost on restart.
4. Prices are a third-party snapshot (benchlm.ai, 2026-10-01). Verify against the vendor pricing page before you quote a cost.

## 13–15. Interview prep
See [`../interview/phase-05-llm-gateway.md`](../interview/phase-05-llm-gateway.md).
