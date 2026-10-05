# llm-gateway (Python 3.12 / FastAPI) — local port 8005

**Purpose:** Provider abstraction (Claude, OpenAI-compatible, Gemini, Ollama): generate, generate_structured, stream, embed; routing, caching, budgets, cost + token accounting.

**Owns schema:** `llm` (model_usage)
**Events:** publishes ModelUsageRecorded (batched)
**Must never:** Expose provider API keys to callers. Silently fall back to a weaker model for a reasoning-critical step without recording it.
**Why this is a separate service:** Central place for keys, rate limits, cost control and provider failover.

Status: Phase 1 — design only. Scaffold with the `new-service` skill in its phase (see docs/roadmap.md).
