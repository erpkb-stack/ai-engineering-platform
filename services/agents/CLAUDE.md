# agents (Python 3.12 / FastAPI) — local port 8003 — package `aeoi_agents`

**Purpose:** stateless investigation workers. Phase 8: Log Analysis (ADR-018). Phase 10: metrics,
deployment, knowledge - CODE ONLY, no LLM client (ADR-020). One endpoint: `POST /v1/agents/{agent}/run`.
**Owns schema:** none. Results go back to the caller (orchestrator), which records them.
**Callers:** service token with scope `agents:run` + `X-On-Behalf-Of: Bearer <user JWT>`.
**Calls:** tool-gateway as `service:agents` (scope `tools:invoke`, trusted for each agent name in
`services/tool-gateway/config/agents.yaml`) FOR the user; llm-gateway (scope `llm:invoke`).
One token file: `secrets/agents_service_token.txt` (`make agents-tokens`).
**Must never:** let the LLM write a Fact; cite an evidence id the tools did not return; turn an
uncitable observation into a Fact (use `notes`); call anything but the two gateways; keep state.

## Code map (`src/aeoi_agents/`)
- `log_analysis/agent.py` tools → dedupe → cluster → facts (code) → labels (LLM, validated)
- `log_analysis/clustering.py` message templates, exact counts per code (pure functions)
- `prompts.py` loads `prompts/<agent>/vN.md` (front matter, sha256) · `trace.py` per-task trace
- `metrics/anomaly.py` robust z vs pre-window baseline (pure) · `metrics/agent.py` 1 fact per series
- `deployment/agent.py` deploys + config diffs, minutes from START to detection (timing, never cause)
- `knowledge/agent.py` runbook/doc POINTERS only (rag ACLs are per group; evidence is per incident)
- `common.py` one traced tool call (`ToolResult | Failure`)
- `api.py` task endpoint · `config.py` budgets (`AEOI_AGENTS_LOG_*`, `_METRICS_*`, `_DEPLOY_*`, `_KNOWLEDGE_*`) · `main.py` factory

## Adding an agent (`/new-agent` skill)
Justify it (what can't code do?). Copy the Log Analysis shape: budgets + one deadline, facts by
code, LLM output validated against the evidence it was shown, rule fallback, pinned prompt,
tests with `FakeToolClient`/`FakeLLMClient` incl. denied tool, LLM down, injected text.
Add the agent to `agents.yaml` (allow-list) in the same change.

Tests: `uv run pytest services/agents` · e2e `uv run pytest tests/integration/agents -m integration`
