# ADR-019: LangGraph orchestrator service and investigation-bound delegation tokens

Status: Proposed
Date: 2026-10-07
Refines: ADR-001 (LangGraph), ADR-017 (OBO + "ids are claims"), ADR-018 (thin runner)

## Context
Phase 8 ran one agent from a CLI, holding the user's JWT for the whole run. Phase 9 must:
1. start investigations from the product (`POST /api/v1/incidents/{id}/investigate`), which on
   the Mac has no consumer: Kafka is Phase 18, the outbox publisher is a log line;
2. survive a crash: resume from the last finished step, without re-running work or
   duplicating evidence;
3. keep acting **as the user** (tool-gateway authz is user perms ∩ agent allow-list) after the
   request that started the run has returned, and after a restart, when the user's token has
   expired or is gone.

Owner decisions: an orchestrator HTTP service (not CLI-only), and real delegation now (not
"supply a fresh token on resume").

## Decision
### A. Orchestrator service (:8002) with a LangGraph graph
- `POST /v1/investigations` (user token, `investigations:run`, `Idempotency-Key` required).
  Order (review finding: every check that can refuse runs before anything changes elsewhere):
  read the incident **as the user** → insert the investigation (one RUNNING per incident) →
  exchange the user token for a delegation grant (directory re-check) → only then mark the
  incident INVESTIGATING (incident-service, as the user) → save grant + graph input in ONE
  transaction → start the graph in the background → **202** with the orchestrator-issued
  `investigation_id`. A start that fails after the insert revokes any grant, marks the row
  FAILED and clears its key, so a retry with the same key really retries.
  The api's `/investigate` now forwards here. The user's own token is used only inside
  this request and never stored.
- Graph: `plan → (Send per task) run_agent → collect_evidence → finalize`, compiled with
  `AsyncPostgresSaver` (checkpoint tables in the `orchestrator` schema, created by our
  migration 0018, never by `setup()` at runtime), `thread_id = investigation_id`,
  `durability="sync"` (the checkpoint is written before the next step starts).
- **Idempotent nodes**, because a hard kill re-runs the interrupted node from scratch (measured
  in a prototype: a crashed Send branch re-runs, a finished sibling's result is kept):
  `plan` inserts tasks `ON CONFLICT (idempotency_key) DO NOTHING`; `run_agent` first looks
  for a recorded execution of its task and returns it instead of calling the agent again;
  evidence POST is idempotent on the key; `finalize` only moves RUNNING rows.
- **Resume:** at startup the service resumes RUNNING investigations whose grant is live and
  deadline not passed; others become FAILED with the reason. `POST /v1/investigations/{id}/resume`
  does the same for one (operator aid). Phase 8's "cancel RUNNING after 2× deadline" heal is
  replaced by this: a RUNNING row is now resumable, not dead.
- Read APIs: `GET /v1/investigations/{id}`, `GET /v1/investigations/{id}/trace`,
  `GET /v1/incidents/{ref}/investigations`; each first reads the incident as the caller
  (incident-service decides visibility). `POST /v1/investigations/{id}/cancel` revokes the grant.

### B. Delegation: RFC 8693-shaped token exchange in the api (owner of `identity`)
- `POST /internal/v1/delegations` — caller: service token with scope `delegation:create`
  (orchestrator only). Body: `subject_token` (the user's JWT), `investigation_id`, `incident_id`.
  The api verifies the user token (user key; not a service, not a delegated token), loads the
  user from `identity.users` (active, same `sub`), re-reads roles/groups **from the DB**,
  requires `investigations:run`, creates `identity.delegation_grants` (one per investigation,
  `expires_at = now + 2 h`), returns a delegated access token.
- `POST /internal/v1/delegations/{grant_id}/token` — refresh, **no user token needed**:
  caller must be the grant's actor; grant not revoked, not expired; user still active and
  still has `investigations:run` (else the grant is revoked with the reason). Roles/groups are
  re-read on every refresh, so a role removal takes effect within one token lifetime.
- `POST /internal/v1/delegations/{grant_id}/revoke` — actor only; the orchestrator calls it on
  COMPLETE / FAILED / CANCELLED.
- Every create / issue / deny / revoke is appended to `identity.delegation_events` in the same
  transaction (append-only: UPDATE/DELETE revoked from every role).
- **Delegated token:** RS256 with a **separate key pair** (`secrets/delegation_*.pem`),
  `iss=aeoi-sts`, `aud=aeoi-internal`, `token_use=delegated`, the user's `sub/uid/email/name`,
  fresh `roles/groups`, `act={"sub":"service:orchestrator"}` (RFC 8693 actor), `inv`
  (investigation id), `inc` (incident id), `grt` (grant id), `exp ≤ 5 min`, never past the
  grant; verified with 5 s clock-skew leeway (not 30 s: leeway extends the after-revoke window).
- **Where it is accepted:** only in the **on-behalf-of slot** (`X-On-Behalf-Of`, next to an
  authenticated service token) — `Authenticator.verify_obo()`. The primary bearer path
  (`verify()`) uses the user-token key/issuer/audience only, so a leaked delegated token
  cannot call the api, incident-service, or any user endpoint directly.
- **Bound to its investigation AND incident:** agents refuse a task whose `investigation_id`
  or `incident_id` differs from `inv`/`inc`; tool-gateway refuses such a call (403
  `delegation_scope_mismatch`) and records `delegation_grant` in the audit details. This
  closes ADR-017's "context ids are claims" gap for delegated calls; `task_id` stays a label
  inside the bound investigation.
- **History survives everything the service can do:** events have no FK (soft refs);
  grants → users is `ON DELETE RESTRICT`; `svc_api` has no UPDATE/DELETE on events and no
  DELETE on grants or users. (Review finding: FK cascades run as the table owner, so the
  first version's REVOKEs could be bypassed by deleting a user.)
- **Trace content needs what the agent needed:** `/trace` returns prompts and output only to
  callers with the agent's tool permission (`logs:read` for log_analysis); others get metadata
  with `redacted: true`.

## Alternatives
| Option | Why not |
|---|---|
| Keep the user JWT for the run, ask for a fresh one on resume | Simplest and no standing credential, but a run cannot outlive the request; the owner chose real delegation |
| Store the user's JWT (outbox payload / DB) | A bearer credential at rest, valid until its own expiry, not bound to one investigation, not revocable |
| Orchestrator mints user tokens itself | Gives the orchestrator the power to impersonate anyone, forever; no DB re-check of roles |
| Long-lived delegated token (= grant TTL) | A role removal or revocation would not take effect until expiry; 5-min tokens + refresh bound the window |
| Same signing key as user tokens | A key compromise in the api would forge IdP user tokens; separate key + OBO-slot-only keeps blast radius small |
| Plain asyncio state machine instead of LangGraph | ~1–2k lines for checkpoints/fan-out/HITL (architecture.md §26); ADR-001 stands, LangGraph stays inside the orchestrator only |
| Let `AsyncPostgresSaver.setup()` create its tables | Needs CREATE on the schema at runtime and bypasses the single Alembic stream; a test pins the library's migration count instead |

## Review (independent, read-only subagent) — 12 findings, 11 fixed with tests
No auth bypass found. Fixed: cascade could erase delegation history; a transient exchange
failure poisoned the Idempotency-Key; incident_id was an unbound claim; the trace exposed log
lines to roles without `logs:read`; a crash between two writes resumed without `grant_id`;
grants could stay live after cancel/crash races (now revoke-before-finish, revoke on cancel
always, revoke when a start is cancelled midway); the incident was changed before the last
authorization check; transient agent/STS errors became permanent FAILED (now 2 in-place
retries; timeouts are not retried); several refusals left no event; queued runs burned their
deadline (the clock now starts at the first slot); a re-run overwrote `evidence_inserted`
with 0. Not fixed, documented below: revocation cannot reach tokens already issued.

## Tradeoffs
- **Revocation stops new tokens, not issued ones.** No verifier asks the STS per call. After
  a cancel/revoke, an agent call already in flight can keep calling tools as the user until
  its token expires: ≤ 5 min + 5 s leeway. [Prod] tool-gateway checks a cached revoked-grant
  list (or token introspection) before every call.
- **A kill during the agent call repeats the work.** The orchestrator dies, the agents worker
  finishes the orphaned call (its tool calls are recorded, its result is lost), and the
  resumed run calls again (task attempt 2). Seen live: 4 tool calls instead of 2. Recorded,
  not hidden; [Prod] an `AgentTask` with an idempotency key the worker dedupes (Phase 18).
- **Standing authority while the user is away:** for up to 2 h the orchestrator can act as the
  user for that one investigation. Bounded by: grant TTL, investigation binding, DB re-check
  of roles on each 5-min refresh, revocation on finish/cancel, append-only events.
- **Orchestrator compromise** = tokens for any live grant (not for arbitrary users). [Prod]
  workload identity + per-grant proof-of-possession (DPoP / mTLS-bound tokens).
- **The api now has a DB login** (`api_svc`, schema `identity` only) and a signing key.
  [Prod] this is the corporate IdP's token-exchange endpoint, not our api.
- **Knowledge tools (rag)** verify the user as a primary bearer; they refuse delegated calls
  until rag gets an OBO path (Phase 10, when the RAG agent arrives). Explicit DENIED, not a
  silent failure. *[Phase 10: done - ADR-020 §C, the gateway's `rag:obo` token + the delegated
  token in X-On-Behalf-Of.]*
- **Background tasks live in one process.** A second orchestrator replica would also try to
  resume; the one-RUNNING-per-incident index does not stop two resumers. [Prod] lease
  (`SELECT … FOR UPDATE SKIP LOCKED` on the investigation) or Kafka partition ownership
  (Phase 18).
- LangGraph adds `langchain-core` and ~40 transitive packages to the orchestrator only.

## Consequences
- Phase 10 adds agents as more `Send` targets; the graph, rows and delegation stay. *(Done:
  ADR-020 - plus the PARTIAL outcome and evidence salvage at the deadline.)*
- Phase 16 adds `interrupt()` for human approval; the checkpoint makes that resumable.
- Phase 18 replaces the HTTP start with an `InvestigationRequested` consumer and the
  in-process task with a worker; the delegation exchange is unchanged.

## Prototype vs Production vs Enterprise-scale
[P] one orchestrator process, in-process background tasks, api acts as STS, RS256 keys in files.
[Prod] IdP token exchange (RFC 8693), sender-constrained tokens, leased resumption, Kafka start.
[Ent] per-tenant grant policies, step-up auth for consequential actions, central revocation feed.
