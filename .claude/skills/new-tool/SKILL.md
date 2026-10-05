---
name: new-tool
description: Register a new Tool Gateway tool with full contract (schemas, authorization, policy, timeout, retry, audit) plus security tests. Use for any capability an agent needs to reach an external/system resource.
argument-hint: "<tool_name>"
---
# Register tool: $ARGUMENTS

A tool definition is incomplete unless it has ALL of these (Feature 14):
| Field | Rule |
|---|---|
| name | snake_case verb_noun, e.g. `search_logs` |
| description | written for the LLM: what it returns, when NOT to use it |
| input_schema | Pydantic; bound every string (max_length), every list (max_items), every time range (≤24h default) |
| output_schema | Pydantic; includes `evidence_id` per item so findings can cite it |
| authorization | required permission(s), e.g. `logs:read`; checked against the *end user* on whose behalf the agent runs |
| side_effect | `READ` / `WRITE` / `CONSEQUENTIAL`. CONSEQUENTIAL ⇒ requires `approval_id` input, verified server-side |
| timeout_s, retry | idempotent READs may retry (exp backoff + jitter); WRITEs only with idempotency key |
| audit | which fields are logged; redact PII/secrets |
| output sanitisation | strip/escape instruction-like content, size cap, mark as untrusted |

Then add tests in `tests/security/test_tool_<name>.py`: unauthorized role denied, oversized input rejected, malicious output (injection text) is wrapped not obeyed, CONSEQUENTIAL without approval rejected.
