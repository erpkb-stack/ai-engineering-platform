# ADR-017: Tool Gateway — on-behalf-of authorization, fail-closed audit via outbox

Status: Proposed
Date: 2026-10-06
Refines: ADR-007 (human approval), architecture.md §14

## Context
Agents (Phase 8+) are non-deterministic and read untrusted text (logs, commit messages, PR
bodies, documents). A prompt injection in that text will eventually make an agent *want* to do
something it should not. The tool gateway is where that intent meets real systems, so it must
hold even when the agent is fully compromised. Questions to answer:
1. Whose permissions does a tool call run with?
2. How does the gateway know which agent is calling, and can that be faked?
3. How is "every tool call is audited" guaranteed when the audit service can be down?
4. What stops a consequential action before the approval system exists (Phase 16)?

## Decision
1. **A tool call always runs FOR a verified human.** Two caller modes:
   - *manual*: user JWT → agent = none → effective permissions = the user's.
   - *agent*: service JWT with scope `tools:invoke` **and** `X-On-Behalf-Of: Bearer <user JWT>`
     **and** body `agent_name`. Effective permissions = **user permissions ∩ agent allow-list**.
   A service token without a user can do nothing (`service_without_user`). The edge (api gateway)
   never forwards `X-On-Behalf-Of`, and a user token that sends it gets 400.
2. **Agent identity is asserted by a trusted service, not by the body alone.** `agents.yaml`
   lists which service may act as which agents (`trusted_services`). `service:agents` may claim
   `log_analysis` but not `action_executor`. Unknown names fail at boot.
3. **Downstream services authorize again with the USER's token.** Knowledge tools forward the
   OBO user token to rag, which applies its group ACL in SQL (ADR-016). No confused deputy.
4. **Policy is code, checked in a fixed order:** rate limit (user, tool) → permissions → agent
   allow-list → side effect → input schema. The rate limit is FIRST because denials write rows
   too: they must not be free to flood. Every outcome has a stable reason code.
5. **CONSEQUENTIAL = always denied in Phase 7.** An `approval_id` is required, and because no
   approval store exists yet, any id is `approval_unverifiable` (fail closed). The DB CHECK
   `consequential_needs_approval` is a second line. Only `action_executor` may hold such a tool.
6. **Bounded contracts.** Every input field is length/range-bounded, extra fields are forbidden,
   time windows are ≤ 24 h and timezone-aware, search is scoped (no org-wide code search, no
   catalog dumps). A contract-lint test enforces this for every tool (and is itself tested).
7. **Output is sanitised and labelled, not trusted:** item and byte caps, per-string cap, secret
   redaction (patterns + `key=value` + sensitive keys), PII scrub, injection *flags* (flagged, not
   removed — a malicious commit message is evidence), `untrusted: true`. One evidence id per item,
   derived from the tool_call id: `ev_<call_id>_<n>`.
8. **Audit = transactional outbox, fail closed.** The `tools.tool_calls` row and a
   `tools.audit_outbox` event are written in **one transaction** for every outcome (OK, DENIED,
   ERROR, TIMEOUT). If that write fails, the caller gets 503 and no data. A relay delivers events
   in batches to the audit service, which inserts with `ON CONFLICT (id, occurred_at) DO NOTHING`
   (at-least-once + idempotent = effectively once). Refusals before a user is known (no/forged
   OBO) are recorded as audit events with the service as actor; refusals for a known user
   (oversized body, bad envelope, user sending `X-On-Behalf-Of`) are normal tool_calls rows.
   A poison event (receiver 4xx) is isolated and marked `rejected:`, so it cannot block delivery.
9. **Least privilege in the database (migration 0016):** `tool_calls` is append-only for the
   gateway login; `devdata.*` is read-only (all Phase 7 tools are READ).
10. **Egress allow-list** (host:port) in the HTTP transport, checked before any connection; no
    redirects, no proxy env vars.
11. **Resilience per dependency** (`devdata`, `rag`, `catalog`, `deployer`): bulkhead outside the
    breaker (busy ≠ broken), one deadline for all attempts, per-attempt timeouts count as breaker
    failures, retries only for READ tools. Shared code moved to `aeoi_common.resilience`.

## Alternatives
| Option | Why not |
|---|---|
| Agent calls tools with its own service identity + broad perms | The agent becomes a super-user; a prompt injection gets everything. The ∩ rule caps blast radius at what the human could do. |
| Token exchange (RFC 8693) for a down-scoped OBO token | The right production answer (one token, audience-bound, short TTL). Needs an IdP that supports it; the header pair is the same trust model, cheaper to build now. |
| Call the audit service synchronously | Audit outage = tool outage, or silently missing records. Outbox gives both availability and completeness. |
| Kafka for audit now | Phase 18 moves the relay to Kafka; the outbox row and the idempotent receiver stay the same. |
| OPA/Cedar for policy | Real value once policies are edited by non-engineers (Phase 26 `policies:manage`). Today ~40 lines of tested Python are clearer than a second language. |
| LLM-based "is this call safe?" check | Non-deterministic, injectable. Policy must be code. |
| Drop outputs that look like injection | Hides evidence and is trivially bypassed. Flag + wrap + capability limits instead. |

## Tradeoffs
- **READ tools execute before they are recorded.** A read leaves nothing to undo, so recording
  after is acceptable; the result is withheld if recording fails. CONSEQUENTIAL tools (Phase 16)
  must record intent *before* executing.
- **Forwarding the user's JWT** means the OBO token's lifetime bounds an investigation. Phase 8/9
  must handle expiry (refresh via the orchestrator, or token exchange).
- **Rate limits are in memory** (per replica). Phase 19 moves them to Redis.
- **Context ids are claims.** `incident_id`, `investigation_id`, `task_id` in the request are
  stored as given; nothing checks that the user works on that incident yet. So an engineer
  could attach calls to another incident's trace. Phase 9 (the orchestrator issues
  investigation ids) and Phase 16 (incident assignment) close this.
- **`audit:read_incident` is treated as `read_own`** until assignment data exists: an
  `incident_id` parameter only narrows. (Found in review: as first written, the parameter
  widened an IC's scope to every actor of any incident.)
- **Breakers are per dependency, shared by all users:** one user's slow queries can open
  `devdata` for everyone for 30 s. Bounded inputs make that hard; per-user breakers are not
  worth it at this scale.
- **Service-refusal recording is capped** per service (token bucket). A service hammering us
  with bad calls stops producing rows after the burst; the log line still counts them.
- **Audit `read_own`** filters `details->>'on_behalf_of'` without an index: a scan inside ≤ 2
  monthly partitions (window ≤ 31 days). [Prod] a `subject_user_id` column + index.
- **Audit idempotency key is (id, occurred_at)** because the table is partitioned. The relay
  resends the stored event, so occurred_at never changes; a producer that regenerated it would
  create a duplicate.
- **Inputs with NUL are rejected (422)** and NUL is stripped before recording: Postgres JSONB
  rejects `\u0000`, which would otherwise make the RECORD fail (found in review).
- **Redaction over-matches on purpose:** a config key named `token_bucket.size` is masked. Wrong
  in the safe direction.
- **Any service with `audit:write` can write any actor.** The receiver stamps `_ingested_by`, so a
  lie is attributable. [Prod] bind producers to the actor prefixes they may write.

## Consequences
- Phase 8 agents get a `ToolClient` that sends the service token + the user's token and wraps
  results with `wrap_untrusted` before they reach a prompt.
- Phase 16 replaces `approval_unverifiable` with a real check: approval exists, APPROVED, not
  expired, not used, same action + arguments, approver ≠ requester.
- Security dashboard (Phase 17/21) reads denials from `tools.tool_calls` (partial index) and
  service refusals from `audit.audit_events`.

## Prototype vs Production vs Enterprise-scale
[P] header-pair OBO, YAML allow-lists, in-memory rate limit, outbox polled every second, devdata
tables standing in for real systems.
[Prod] RFC 8693 token exchange, mTLS between services, Redis rate limits, Kafka relay, alerts on
outbox backlog age, adapters for Loki/Prometheus/GitHub/Argo behind the same contracts.
[Ent] policy-as-code with review workflow (OPA/Cedar), per-tenant allow-lists, signed audit log
(hash chain / WORM storage), DLP on tool outputs.
