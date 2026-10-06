# tool-gateway (Python 3.12 / FastAPI) — local port 8006 — package `aeoi_tools`

**Purpose:** the ONLY path from agents/users to "external" systems: registry, bounded contracts,
authz (user perms ∩ agent allow-list), side-effect policy, rate limit, timeout/retry/breaker,
output sanitisation, audit. ADR-017.
**Owns schemas:** `tools` (tool_calls append-only, audit_outbox), `devdata` (read-only for this login)
**Callers:** users (manual mode, via api `/api/v1/tools`) and services with scope `tools:invoke`
+ `X-On-Behalf-Of: Bearer <user JWT>` + `agent_name` (trusted per `config/agents.yaml`).
**Must never:** run a tool without a verified human; trust identity from a body field; execute a
CONSEQUENTIAL tool without a VERIFIED approval (Phase 7: always deny); return data it could not
record; reach a host outside the egress allow-list; drop (instead of flag) suspicious output.

## Code map (`src/aeoi_tools/`)
- `contracts.py` ToolSpec (validated at import) · `schemas.py` bounded I/O models
- `registry.py` the 12 specs (11 READ + rollback) · `policy.py` decide() + agents.yaml model
- `service.py` invoke pipeline, one-transaction recording · `sanitize.py` caps/redaction/flags/evidence ids
- `relay.py` outbox → audit · `api.py` caller modes · `adapters/` devdata SQL, catalog JSON, rag HTTP, egress

## Adding a tool (`/new-tool` skill)
1. Bounded input/output in `schemas.py` (the contract-lint test fails otherwise).
2. ToolSpec in `registry.py`: permissions, side_effect, dependency, timeout, audit_fields,
   a description that says when NOT to use it.
3. Add it to the right agent(s) in `config/agents.yaml` (security review).
4. Integration test with planted data + a denial test. Run `make tools-smoke`.

## Commands
`make db-users` · `make tools-tokens` · `make run-tools` · `make stop-tools` · `make tools-smoke`
Tests: `uv run pytest services/tool-gateway` · `uv run pytest tests/integration/tools -m integration`
