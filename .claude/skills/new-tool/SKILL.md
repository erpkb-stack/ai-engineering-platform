---
name: new-tool
description: Register a new Tool Gateway tool with full contract (schemas, authorization, policy, timeout, retry, audit) plus security tests. Use for any capability an agent needs to reach an external/system resource.
argument-hint: "<tool_name>"
---
# Register tool: $ARGUMENTS

Read ADR-017 and `services/tool-gateway/CLAUDE.md` first. A tool is incomplete unless it has ALL
of these. `ToolSpec.__post_init__` and `services/tool-gateway/tests/test_contracts.py` enforce most.

| Field | Rule |
|---|---|
| name | snake_case verb_noun, e.g. `search_logs` |
| description | ≥ 40 chars, written for the LLM: what it returns, its limits, when NOT to use it |
| input model (`schemas.py`) | subclass `In` (extra=forbid). Every str has max_length/pattern, every int an upper bound, every list max_length, no free dicts. Time ranges subclass `TimeWindow` (tz-aware, ≤ 24 h). Searches are scoped (one service/repo) |
| output model | subclass `Out`; `items: list[<Item>]` with max_length; `Item` carries `evidence_id` (the gateway fills it) |
| permissions | required `Perm`s, checked against the END USER (agent calls: user perms ∩ allow-list) |
| side_effect | READ / WRITE / CONSEQUENTIAL. CONSEQUENTIAL ⇒ `approval_id` verified server-side (Phase 16; denied before), `max_attempts=1`, `idempotent=False`, only the `action_executor` agent |
| dependency | breaker/bulkhead key (`devdata`, `rag`, …); add a bulkhead limit in config if new |
| timeout_s, max_attempts | one deadline for all attempts; only READ may retry |
| audit_fields | input fields copied into the audit event (prefer keys/ids; free text only when it IS the point, e.g. a rollback reason - it is PII-scrubbed) |
| sanitisation | automatic (caps, redaction, PII, injection FLAGS - never silently drop evidence). Mask secret config values in the adapter |

Then:
1. Add the tool to the right agent(s) in `services/tool-gateway/config/agents.yaml` (security review).
2. Integration tests in `tests/integration/tools/` with planted rows under a unique key:
   happy path, unauthorized role denied AND recorded, oversized/malformed input rejected,
   malicious output flagged (not obeyed, not dropped), agent outside allow-list denied,
   CONSEQUENTIAL without approval denied. Assert the `tools.tool_calls` row every time.
3. Extend `scripts/smoke-phase7.sh` if the tool is part of the demo. Run `make check`,
   `make test-integration`, `make tools-smoke`.
