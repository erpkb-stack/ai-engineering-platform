# ADR-026: Human approval, then controlled execution - decide, then act, in the approver's own request

Status: Proposed
Date: 2026-10-12
Refines: ADR-007 (human in the loop; changes HOW, not WHETHER), ADR-017 (tool gateway), ADR-019 (delegated tokens)

## Context
Since Phase 7 the one consequential tool (`rollback_deployment`) is always denied. Phase 16 must
let it run - only after a recorded human approval - without weakening anything else.
ADR-007 said: the graph pauses (`interrupt`), only an INCIDENT_COMMANDER approves, approvals are
single-use, expiring and bound to the arguments.

Constraints found while designing:
1. A delegated token lives 5 minutes and is bound to ONE investigation (ADR-019). A human may
   take an hour to decide. An `interrupt` would keep the investigation RUNNING and a grant alive
   for that hour - or act later with a credential that no longer exists.
2. The person who started the investigation must not approve its proposal (otherwise "approval"
   is one person clicking twice).
3. The approval must be checked by the component that EXECUTES (the gateway), against the
   system of record (incident-service) - not by the caller, not by the agent.

Owner decisions (2026-10-12):
- Flow = **finish, then act**: the investigation ends with a pending request; the decision runs
  the action AS THE APPROVER; no credential waits; MORE_INFO records the question only.
- Effect = **simulated CD record**: a new deployment row (previous good commit, `deployed_by =
  approval:<id>`) through an INSERT-only login.
- Approver = **an IC who is not the requester**.
- Requests = **AI proposal + SRE manual request**.
Gate: Phase 15 verified on the Mac, PR #16 merged (d40f42e).

## Decision
```
investigation (graph) ... report ─► propose (CODE rule) ─► incident-service POST .../actions/proposals
                                      service token actions:propose + the DELEGATED token: requester = the starter
SRE / IC ─► api POST /api/v1/incidents/{ref}/actions (Idempotency-Key) ─► incident-service (requester = caller)
                      incident -> AWAITING_REVIEW, one PENDING request per (incident, args), expires in 30 min

IC #2 ─► api POST /api/v1/approvals/{id}/decision ─► orchestrator (IC's OWN token, this request only)
   1 incident-service decide AS THE IC: role, NOT the requester, not decided, not expired, reason
   2 APPROVED ─► agents action_executor (code only) ─► tool gateway rollback_deployment
          own identity service:action_executor + X-On-Behalf-Of = the IC's own token + approval_id
          gateway: policy (user token only, approval + incident named) ─► validate args
                   ─► consume at incident-service AS THE IC (this approver, this action, this
                      args hash, this incident, APPROVED, not expired, not used) ─► used
                   ─► tool_calls PENDING (the intent, committed first) ─► Deployer ─► OK row
          Deployer: refuses an unknown / not-SUCCEEDED / not-running deploy or one with nothing
                    before it; inserts DEPLOY-9<approval> via cd_svc (INSERT only, idempotent)
   3 orchestrator records the outcome (service token actions:record) ─► EXECUTED -> MITIGATED
```

### A. Who may do what
| Step | Who | Enforced in |
|---|---|---|
| request | `actions:request` (SRE, IC); an investigation only FOR its starter, who must hold it | api guard, incident-service route, delegated-token binding (investigation AND incident) |
| decide | `actions:approve` (IC only; ADMIN and MANAGER never) and not the requester | api guard, orchestrator route, incident-service rule, DB CHECK `approver_is_not_requester` |
| execute | the approver's OWN token (never delegated), ONLY the executor identity (`agent = service = action_executor`; manual mode is refused: `executor_only`), a consumable approval | gateway policy + consume; agents.yaml: `service:agents` can never assert `action_executor`; consume and proposals accept a SERVICE bearer only |
| record | `actions:record` (orchestrator) | incident-service route; outcome only after consume (CHECK) |

### B. One approval = one action, once
- bound to the action and the **sha256 of the canonical arguments** (`args_sha256`, shared code);
  the gateway hashes the VALIDATED input, so one changed character is a different action;
- consumed in the same transaction that checks it, and never used after `expires_at` - a
  resumed attempt included;
- the gateway reads its own record first: an OK row → `approval_already_executed` (also when
  the record refuses a re-use, if THIS user ran it; the caller records EXECUTED, nothing runs);
  a recent intent with no finished attempt → `approval_in_progress` (result unknown); an older
  unfinished intent (the process died) → resume;
- unique partial indexes: one intent and at most one OK tool call per approval;
- the CD record key is derived from the approval (`DEPLOY-9` + 18 digits) and CHECKED
  (`deployed_by = approval:<id>`, same service): a taken key is refused, never reported as ours;
- one live request per (incident, action, deploy) - the hash includes the free-text reason -
  and one rollback at a time per service (advisory lock held over the checks and the insert);
- no request, approval or execution on a RESOLVED / CLOSED incident.

### C. Failure is told apart from "unknown"
The orchestrator records FAILED only when the TOOL ran and failed with a 4xx
(`execution_failed`: a newer deploy, nothing to go back to, a taken key). A refused ATTEMPT
(`approval_unusable` / `approval_invalid` / `approval_in_progress` …) is reported, never recorded:
a double click once recorded FAILED while the other click's rollback ran (review H2). Timeouts,
5xx and 429 are "result unknown - retry /execute": recording a guess would show a rollback that
did run as failed. Retrying is safe (B).
A failed or rejected request returns the incident to INVESTIGATING when nothing else waits
(undecided, or approved and not run yet);
an expired one is recorded EXPIRED (event `ActionExpired`, actor `service:incident-service`).

### D. Proposals are code
Rule v1 (`orchestrator/proposal.py`): the top hypothesis is a VALIDATED deploy (`change`) whose
deploy is SUCCEEDED and has an earlier successful release → propose rollback of exactly that
deploy. Otherwise no proposal, with the reason in the investigation summary. The snapshot shown
to the approver is code-written; `way_back` (paths, symbols) is shown only to `code:read`.
A proposal is idempotent per (investigation, args) whatever happened to it since - a resumed
graph never reopens a decided request.

### E. Synchronous, bounded
api 15 s ⟩ orchestrator → agents 12 s ⟩ executor → gateway 10 s ⟩ tool 8 s. The decision request waits for
one bounded action; nothing long-running is held open (same reasoning as ADR-025 C).

## Alternatives
| Option | Why not (here) |
|---|---|
| LangGraph `interrupt` (ADR-007 wording) | keeps an investigation RUNNING and a delegated grant alive for a human's hour; a 5-minute delegated token cannot act later anyway (constraint 1) |
| Approver = any IC incl. the starter | one person approves their own proposal: no second pair of eyes |
| Gateway trusts an `approved=true` claim from the caller | the caller is exactly who must not be trusted; the record must be asked |
| Executor = the existing `service:agents` identity | a leaked investigation token could execute; separate identity costs one token file |
| Orchestrator writes to devdata directly | violates "agents/services reach systems only via the gateway"; loses the audit pipeline |
| Real CD (Argo, GitHub deployments) | out of scope locally; the contract (idempotent per approval, refuses a stale target) carries over |

## Tradeoffs
- A refusal reason `approval_unusable` covers "not approved yet / used / expired" (detail says
  which): incident-service answers 409 for all three.
- The orchestrator, not the gateway, writes the outcome back. It relays what the gateway said
  (with the tool call id), so the two records can be compared; [E] the gateway emits it itself.
- `action_executor` runs in the same process as the read agents [P]: the identity is separate,
  the blast radius of a code bug is not.
- MORE_INFO only records the question and returns the incident to INVESTIGATING; nothing answers it.
- Proposal rule v1 ignores band (a MEDIUM validated deploy is proposed too): the human decides;
  the band and rubric are on the request.
- Downgrade of 0024 keeps working with data present: EXPIRED → REJECTED by the nil-uuid "system"
  decider with a "[was EXPIRED…]" reason (undecided would break the pending-request index when a
  newer request exists), PENDING → ERROR; outcome columns are dropped (the events keep them).
- Proposals have two locks against a user forging one (service bearer AND delegated token):
  removing either alone is not caught by a test, by design.
- The new CD row copies the restored config VALUES verbatim, as the undone row holds them
  (same table, same readers); secret-looking keys are redacted on READ by the gateway.
- Commits in an outcome (`outcome_detail.result`) are shown only to `code:read` readers.

## Independent review (2026-10-12)
4 high, 6 medium, 3 low. Fixed with a test each, all mutation-checked: manual-mode execution
(H1), a parallel retry recorded FAILED (H2), resume after expiry + IC consuming by hand (H3),
downgrade breaking the pending index (H4), key collision reported as ours (M1), several live
requests and racing approvals for one target (M2), a "proposal" stored by a user (M3), tool
timeout above the client's (M4), closed incidents (M5), lock order (M6), commit leak (L1),
the already-executed tool call id (L3). L2 (config values copied) is documented above.

## Consequences
- incident-service now verifies delegated tokens (on-behalf-of only, on the proposals route;
  consume refuses them): it needs `secrets/delegation_public.pem` like agents and tools.
- New secrets: `secrets/action_executor_token.txt` (make agents-tokens),
  `secrets/tools_incident_token.txt` (make tools-tokens), `secrets/cd_svc_password.txt` (make db-users).
- Test settings never fall back to the developer's real secrets or services (they did by default).

## Prototype vs Production vs Enterprise-scale
[P] one approver, simulated CD, one process for all agents, in-memory rate limits.
[Prod] two-person rule for SEV-1, real CD API with its own idempotency key = the approval id,
the executor as its own deployment and network policy, the gateway writes outcomes itself.
[Ent] policy-as-code (OPA) for who may approve what; approval in the chat tool with step-up auth.

## When we would revisit
When a reversible, well-evaluated action earns auto-approval by policy (never by default), or
when the frontend (Phase 17) needs a push channel for pending requests.

## Revisited in Phase 17b (2026-10-13): the approvals inbox
Phase 16 had no cross-incident list of approvals. The console's inbox (owner decision: poll
every 15 s, push = [Prod]) would otherwise need one request per incident. Added
`GET /api/v1/approvals?status=PENDING&limit=` (max 100): PENDING only, the one about to expire
first, the same readers (`incidents:read`) and the same `code:read` redaction as the
per-incident list - nothing a reader could not already see one incident at a time. Still no
"list every approval" route: decided history stays per incident.
