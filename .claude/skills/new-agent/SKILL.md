---
name: new-agent
description: Add a LangGraph investigation agent (node) with versioned prompt, typed IO schemas, tool allow-list, budgets, fake-LLM tests and trace instrumentation. Use when creating or substantially changing any agent in services/agents.
argument-hint: "<agent-name>"
---
# Create agent: $ARGUMENTS

First, justify it (write the answer in the PR/description):
- What specialised reasoning or tool use does this need that an existing agent or plain code can't do?
- If the work is deterministic (math, joins, sorting, regex), make it a **function node**, not an LLM agent.

Then produce:
1. `services/agents/app/agents/<name>/agent.py` — `AGENT_NAME`, `AGENT_VERSION`, `async def run(state, ctx) -> PartialState`.
2. `services/agents/app/agents/<name>/schemas.py` — input + output Pydantic models. Output findings use `Fact | Hypothesis | Recommendation` from `libs/models`.
3. `prompts/<name>/v1.md` — system prompt with: role, allowed tools, untrusted-data handling clause, output schema reference. Register in `prompts/registry.yaml`.
4. Tool allow-list in `services/tool-gateway/policies/agents.yaml` (least privilege).
5. Budgets: max_tokens, max_tool_calls, timeout_s in agent config.
6. Tests: happy path with `FakeLLMProvider`; schema-violation retry; tool-denied path; prompt-injection fixture in a retrieved doc must not change behaviour.
7. Emit `AgentStarted/AgentCompleted/AgentFailed` events and an OTel span with attributes: agent, agent_version, model, prompt_version, tokens_in/out, tool_calls, retries.
