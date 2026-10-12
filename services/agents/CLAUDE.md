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
- `reasoning/` (Phase 11, ADR-021): `observations.py` timeline (code), `candidates.py` causes + rubric bands (code), `hypothesis.py` Haiku ranks/explains with refs, `critic.py` Sonnet critique (route fixed, fallback off, refuses a different observation set)
- `postmortem/agent.py` (Phase 12, ADR-022): timeline + negative evidence + trigger (code), Sonnet sentences (route fixed, fallback off), `check()`: refs, markup, unknown numbers
- `history/agent.py` (Phase 13, ADR-023): query (code) → search_incidents AS THE USER, twice (own service = filter, then anywhere = boost) → keep `open_to_engineering` only → Haiku claims, kept only if `relations()` (code) agrees → `priors()` → INCIDENT pointers. `candidates.attach_priors` + `rubric_order` tie-break (after band/support/contradiction)
- `code/agent.py` (Phase 14, ADR-024): CODE ONLY. deploys started <= first symptom (max 3) → `compare_commits` (previous successful deploy's commit → this one) → `aeoi_codeparse.analyze_diff` → COMMIT evidence (hunk excerpt) → `find_references` for a removed field (CODE evidence, other services only). Observations: role `code`, at the deploy's start; `candidates.code_support` adds at most ONE per deploy (strongest rule)
- `action/executor.py` (Phase 16, ADR-026): CODE ONLY, no choice: runs ONE approved action with exactly the approved args, AS the approver (their own token; delegated refused), through its OWN gateway client (`secrets/action_executor_token.txt`, subject `service:action_executor`, 10 s). Reports what the gateway said; never retries.
- `release/` (Phase 15, ADR-025): CODE ONLY, ADVISORY. `agent.py` target (PR merge commit | range | deploy) → `compare_commits` → `code.agent.scan_commit` (the Phase 14 rules, one place) → catalog blast radius → `find_references` (shared code only) → `rubric.assess`: factors (each cites evidence), band, `findings_band` vs `coverage` (unknown is not safe, but reported apart), way back (flag guard → flag_off; else redeploy; a changed flag the code does not read → flag_not_read). `scan.py` = `make risk-scan` (every synthetic deploy + labelled cases)
- `common.py` one traced tool call (`ToolResult | Failure`)
- `api.py` task endpoint · `config.py` budgets (`AEOI_AGENTS_LOG_*`, `_METRICS_*`, `_DEPLOY_*`, `_KNOWLEDGE_*`, `_HISTORY_*`, `_CODE_*`, `_RELEASE_*`) · `main.py` factory

## Adding an agent (`/new-agent` skill)
Justify it (what can't code do?). Copy the Log Analysis shape: budgets + one deadline, facts by
code, LLM output validated against the evidence it was shown, rule fallback, pinned prompt,
tests with `FakeToolClient`/`FakeLLMClient` incl. denied tool, LLM down, injected text.
Add the agent to `agents.yaml` (allow-list) in the same change.

Tests: `uv run pytest services/agents` · e2e `uv run pytest tests/integration/agents -m integration`
