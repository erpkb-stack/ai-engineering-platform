# tool-gateway (Python 3.12 / FastAPI) — local port 8006

**Purpose:** Controlled tool execution: registry, schema validation, authorization, policy, timeouts, retries, output sanitisation, audit.

**Owns schema:** `tools` (tool_calls)
**Events:** publishes ToolCalled, ToolDenied
**Must never:** Execute CONSEQUENTIAL tools without a verified approval_id. Allow egress outside the allow-list.
**Why this is a separate service:** The security choke point between non-deterministic agents and real systems.

Status: Phase 1 — design only. Scaffold with the `new-service` skill in its phase (see docs/roadmap.md).
