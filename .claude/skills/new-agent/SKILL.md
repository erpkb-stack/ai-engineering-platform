---
name: new-agent
description: Add a LangGraph investigation agent (node) with versioned prompt, typed IO schemas, tool allow-list, budgets, fake-LLM tests and trace instrumentation. Use when creating or substantially changing any agent in services/agents.
argument-hint: "<agent-name>"
---
# Create agent: $ARGUMENTS

Read ADR-018 and `services/agents/CLAUDE.md` first. The Log Analysis agent is the reference.

First, justify it (write the answer in the PR description):
- What does this need that plain code or an existing agent can't do?
- Deterministic work (counting, joins, sorting, anomaly maths, timelines) is CODE, not an LLM call.

Then produce:
1. `services/agents/src/aeoi_agents/<name>/agent.py` — `AGENT_NAME`, `AGENT_VERSION`, a class with
   `async run(task, user_token, correlation_id) -> AgentRunResult`. Facts are built by code and cite
   only evidence ids the tools returned; anything uncitable goes to `notes`.
2. Task + result models in `libs/models/src/aeoi_models/api/agents.py` (bounded, tz-aware windows).
3. `services/agents/prompts/<name>/v1.md` with front matter (prompt_id, version, route): role, the
   untrusted-data clause, what the model may and may not decide. Pin its sha256 in a test.
4. LLM output: `generate_structured` with a JSON schema, then validate in code (ids, citations,
   enums); fall back to rule output and set `degraded` with the reason. Never loop.
5. Tool allow-list in `services/tool-gateway/config/agents.yaml` (least privilege) and trust for
   `service:agents` to act as the new agent name.
6. Budgets in `config.py`: max tool calls, max items to the model, one task deadline below the
   orchestrator's HTTP timeout; the LLM timeout = min(cap, time left).
7. Tests (`FakeToolClient`, `FakeLLMClient`): happy path with citation validation; LLM down →
   same facts; tool denied → FAILED with the reason; injected text stays inside the untrusted
   wrapper; budget caps; an e2e test in `tests/integration/agents/` asserting the trace rows.
8. Trace: tool calls, model, prompt id/version/sha, tokens, cost, latency, retries, messages
   (the runner stores them in `orchestrator.agent_executions` / `messages`).
