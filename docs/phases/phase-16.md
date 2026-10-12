# Phase 16 — Human approval, then controlled execution

## 0. Gate and scope challenge
- **Gate: passed.** Phase 15 verified on the Mac (`make risk-smoke` 19/19, integration 228
  passed); PR #16 merged (d40f42e).
- **Challenge 1 — ADR-007 said "the graph pauses (`interrupt`)".** That keeps an investigation
  RUNNING and a delegated grant alive while a human thinks, and a 5-minute delegated token
  cannot act an hour later anyway. Owner decision: **finish, then act** - the investigation
  ends with a pending request; the decision runs the action in the approver's own request.
- **Challenge 2 — "an IC approves".** Then the IC who started the investigation approves its
  own proposal: one person, two clicks. Owner decision: **an IC who is not the requester**.
- **Challenge 3 — what does "rollback" do here?** No real CD exists locally. Owner decision:
  **a simulated CD record** (a new deployment row of the previous good commit) through a login
  that can only INSERT - the contract (idempotent per approval, refuses a stale target) is
  what carries over to a real CD API.
- **Challenge 4 — the smoke must not touch demo data.** DEPLOY-4821 is checkout-api's running
  release; a real rollback in the smoke would change what `risk-scan` and older smokes count.
  The smoke plants a throwaway service and removes its rows on exit.

## 1. Objective
A consequential action (rollback) runs only after a recorded decision by an incident commander
who did not ask for it, with exactly the approved arguments, once, before it expires, as that
person - and the record says what happened, truthfully.

## 2. Business reason
Auto-remediation without accountability is unacceptable; advice without an action path is a
report. This is the controlled path between them: AI (or an SRE) proposes, a second human
decides, the platform executes and records.

## 3. Architecture
```
investigation ... report ─► propose (code rule) ─► incident-service proposal (FOR the starter)
SRE / IC ─► POST /api/v1/incidents/{ref}/actions ─► incident-service (PENDING, 30 min) ─► AWAITING_REVIEW
IC #2 ─► POST /api/v1/approvals/{id}/decision ─► orchestrator (IC's token, this request only)
   decide AS the IC ─► APPROVED ─► agents action_executor ─► tool gateway rollback_deployment
   (service:action_executor + IC token + approval_id) ─► consume at incident-service ─► intent
   ─► Deployer (cd_svc INSERT only) ─► OK ─► outcome EXECUTED ─► incident MITIGATED
```
| Guarantee | Enforced by | Test (mutation-checked) |
|---|---|---|
| Approver ≠ requester | incident-service rule + DB CHECK | e2e requester 403 |
| Only an IC decides (not SRE / MANAGER / ADMIN) | api guard, orchestrator, incident-service | e2e + smoke |
| Exact args (sha256 over validated input) | gateway hash + consume | e2e tampered reason → 403 |
| Only THIS approver executes | consume `decided_by` | e2e other IC → 403 |
| Only the executor identity runs the tool | gateway policy `executor_only` | unit + e2e (H1) |
| Never a delegated token, never by hand | policy + consume service-only | unit + e2e (H3) |
| Once: replay, double click | intent/OK unique indexes, `_prior`, key check | e2e concurrent (H2), replay |
| Never after expiry, resume included | consume | e2e (H3) |
| FAILED only when the tool failed | orchestrator `TOOL_FAILED` rule | e2e in-progress (H2) |
| Stale target / taken key refused | Deployer + advisory lock | e2e newer deploy, collision (M1) |
| One live request per target; none on closed incidents | `request`, `_open` | e2e (M2, M5) |
| Proposal bound to investigation AND incident, service + delegated only | proposals route | e2e binding (M3) |
| CD login inserts only | migration 0024 grants | e2e InsufficientPrivilege |
| Downgrade works with data | 0024 mapping | migration test (H4) |
| Commits only for code:read | `to_out` redaction | e2e MANAGER (L1) |

## 4. Files
```
docs/adr/ADR-026-human-approval-execution.md (+ README row; ADR-007 "refined by") · docs/phases/phase-16.md
docs/interview/phase-16-approval.md · docs/data-model.md · docs/roadmap.md · CLAUDE.md (root + 4 services)
libs/models   api/approvals.py (new), events.py (+3), api/agents.py (AgentRunResult.action)
libs/db       models incident/tools, alembic 0024_approvals_execution, users.py (cd_svc)
libs/web      require_acting_user(service_only=)   libs/tool-client  CallContext.approval_id
services/incident-service  approvals.py (new), api (6 routes), domain, config/main (delegation key)
services/tool-gateway      policy, service (_consequential), registry, adapters/{deployer,approvals}.py, config, main
services/agents            action/executor.py (new), api, config, main (own token, 10 s)
services/orchestrator      proposal.py, actions.py (new), graph (propose node), clients, config, main, __main__
services/api               5 routes; devtoken --nth
scripts/smoke-phase16.sh, scripts/phase16_fixture.py · Makefile approvals, approve, approval-smoke, tokens
tests  integration/agents/test_approvals_e2e.py (14), db/test_migrations (0024), tools/test_tools (updated),
       orchestrator/test_proposal.py, agents/test_action_executor.py, tool-gateway policy tests
```

## 5–6. Commands (macOS, zsh)
```zsh
cd ~/projects/ai-engineering-platform
git switch main && git pull
shasum -a 256 -c ../_to_delete/phase16-base.sha256 | grep -v ': OK$'   # no output = safe
git switch -c phase-16-human-approval
tar xzf ../_to_delete/phase16.tgz
uv sync --all-packages && make check
make db-upgrade          # 0024: approvals columns, PENDING intents, role svc_cd
make db-users            # creates the cd_svc login (secrets/cd_svc_password.txt)
make tools-tokens        # + secrets/tools_incident_token.txt (approvals:consume)
make agents-tokens       # + secrets/action_executor_token.txt; orchestrator + actions:propose/record
# restart: make dev (incident-service + api), run-tools, run-agents, run-orch
make test-integration
```

## 7. Config
`APPROVAL_TTL_MINUTES` 30. `AEOI_ORCH_ACTIONS` (true), `AEOI_ORCH_ACTION_TIMEOUT_S` 12.
Executor → gateway client 10 s; rollback tool 8 s; in-progress window = tool timeout + 5 s.
incident-service: `AEOI_INCIDENT_DELEGATION_PUBLIC_KEY_FILE` (default `secrets/delegation_public.pem`).

## 8. Tests
- `make check`: 761 passed (sandbox). Integration: 243 passed, 1 skipped (sandbox).
- 7 + 11 mutations: all caught except one, by design (proposal: service bearer AND delegated
  token; removing one alone is still blocked by the other).
- Concurrency test run 6× in a row: stable.
- Independent review: 4 high, 6 medium, 3 low; all fixed with tests except L2 (documented).
- **Not measured yet:** anything on the Mac.

## 9. Run
```zsh
make approval-smoke                          # throwaway service; never a demo deploy
make code-smoke                              # creates a DEPLOY-4821 demo incident: note its INC-n
make agent-status INCIDENT=INC-n             # "proposed: rollback DEPLOY-4821 -> approval <id>"
make approvals INCIDENT=INC-n                # the request, its snapshot and "way back"
make approve ID=<id> DECISION=REJECTED REASON="read it; not this time"   # as IC #2
```
Approving an investigation's proposal for a demo deploy DOES change the demo data (a new CD
row). Reject it, or approve only on purpose.

## 10. Verify (gate for Phase 17)
- [ ] `make approval-smoke` all ✔ on the Mac
- [ ] the `make code-smoke` incident proposes the DEPLOY-4821 rollback (`make agent-status`),
      the request + way back is read by a human (`make approvals`), then REJECTED with a reason
      (every investigation smoke now leaves such a PENDING proposal; it expires in 30 minutes)
- [ ] `make test-integration` green on the Mac
- [ ] regressions: `make risk-smoke`, `make code-smoke` (deploy-key patterns widened)

## 11. Failure scenarios
| Symptom | Cause | Fix |
|---|---|---|
| decision 200, `execution_error` "approval_unverifiable" | `secrets/tools_incident_token.txt` missing / incident-service down | `make tools-tokens`, restart run-tools |
| "executor_not_configured" | `secrets/action_executor_token.txt` missing | `make agents-tokens`, restart run-agents |
| "the CD login (cd_svc) is not configured" (503) | no `cd_svc` login | `make db-users`, restart run-tools |
| proposal `post_error` 403 "delegated tokens" | incident-service has no delegation key | `make delegation-keys` done? restart `make dev` |
| "no action proposed: … not VALIDATED" | the evidence did not validate the deploy | expected: an answer, not a failure |
| 409 "already requested" | one live request per target | decide or wait for expiry |
| "Result unknown" | agents/gateway did not answer in 12 s | `/execute` (safe) - or `make approvals` to see what ran |

## 11b. fix1 (first Mac run)
`make test-integration`: 4 migration tests failed - `DROP ROLE svc_cd`: "some objects depend on it".
Roles belong to the whole Postgres server; on the Mac the DEV database (after `make db-upgrade`)
also grants svc_cd rights, so the TEST database's downgrade could not drop it. The sandbox's dev
database was never upgraded, so it passed there. Fix: downgrade revokes the grants and keeps the
role, as 0001 does. Reproduced in the sandbox first (a grant in the dev DB), then fixed.

## 12. Production considerations
Real CD API with the approval id as its idempotency key; the executor as its own deployment
and network policy; the gateway writes the outcome itself; two-person rule for SEV-1;
approval from chat with step-up auth; a push channel for pending requests (Phase 17 UI).

## 13–15. Interview
`docs/interview/phase-16-approval.md`.
