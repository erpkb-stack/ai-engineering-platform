# Phase 9 — LangGraph orchestration + delegation

## 0. Gate and scope challenge
- **Gate:** Phase 8 built and committed (`c8ea526`), valid model comparison recorded on the Mac.
  Still open from Phase 8 on the Mac: `make agent-smoke` all ✔ and `make test-integration`
  green — run them before you merge Phase 9 (the Phase 8 smoke now also needs `run-orch`).
- **Challenge 1 — "LangGraph" with ONE agent is mostly ceremony.** True for the graph shape.
  What earns it now is the **checkpointer**: a run survives a crash and resumes at the step
  it was in, without the user. That is the Phase 9 deliverable; Phase 10 adds agents as more
  `Send` targets on the same graph.
- **Challenge 2 — nothing starts an investigation.** `/investigate` wrote an event nobody
  consumed (Kafka = Phase 18). You chose an **orchestrator HTTP service**: `/investigate`
  now returns 202 + an orchestrator-issued `investigation_id`.
- **Challenge 3 — the run must act as the user after the request is gone.** You chose
  **real delegation now** (not "fresh token on resume"): RFC 8693-shaped token exchange in
  the api, investigation- and incident-bound tokens, accepted only in the on-behalf-of slot.
  This is most of the phase's risk, so it got an independent review (12 findings, 11 fixed).
- **Cut:** HITL `interrupt()` (Phase 16), more agents (Phase 10), Kafka start (Phase 18),
  rag accepting delegated tokens (Phase 10 - refused explicitly until then).

## 1. Objective
`POST /api/v1/incidents/{id}/investigate` → orchestrator → delegation grant → LangGraph graph
(checkpointed every step) → agent (as the user, via a delegated token) → evidence + trace.
Kill the orchestrator mid-run: the next start resumes it and finishes, without the user.

## 2. Business reason
MTTR (architecture.md §4): an investigation that dies with a pod restart is an engineer
re-running it 10 minutes later. Trust/compliance: every action the AI takes "as you" is
bound to one investigation, revocable, and recorded (who allowed which service to act for
whom, and when it stopped).

## 3. Architecture
```
user ── POST /api/v1/incidents/{id}/investigate ──► api ──► orchestrator :8002 (user token)
  orchestrator, in the request:  1 GET incident AS USER (incident-service)
                                 2 INSERT investigation RUNNING (one per incident)
                                 3 POST /internal/v1/delegations  ──► api STS (identity schema)
                                      user token verified, roles from the DIRECTORY,
                                      grant(user, actor, investigation, incident, 2 h) + events
                                 4 POST /incidents/{id}/investigate AS USER (status, timeline)
                                 5 grant + graph input in one transaction ── 202
  background (asyncio task, semaphore 2):
    plan ─Send─► run_agent ──► collect_evidence ──► finalize
     │             │ token = refresh(grant) (5 min, roles re-read)   │ revoke grant, then finish
     │             └─► agents (service token + X-On-Behalf-Of: delegated) ─► tool-gateway
     │                   both check inv + inc of the token against the call
     └── checkpoint after every step: orchestrator.checkpoints (thread = investigation id)
  restart: RUNNING rows → resume from checkpoint (grant refresh, no user token)
```
| Guarantee | Enforced by | Test |
|---|---|---|
| Delegated token never a primary bearer | separate key/iss/aud + `token_use` guard; `verify_obo` only | `test_a_delegated_token_is_never_a_primary_bearer`, unit `test_delegation.py` |
| Token bound to investigation AND incident | tool-gateway `_context`, agents `_user_token` | `test_tool_gateway_binds_*`, `test_agents_refuse_*` (mutation-checked) |
| Roles come from the directory, re-checked on refresh | `sts.py` `_load_user` | `test_directory_not_the_token_decides`, `test_refresh_rechecks_*` |
| History can't be rewritten or cascaded away | 0018 REVOKEs, no FK on events, RESTRICT | `test_delegation_history_is_append_only_*`, `test_even_the_owner_cannot_cascade_*` |
| Kill mid-agent-call → resume, finish | checkpoints + idempotent nodes | `test_crash_mid_agent_call_*`, live kill -9 (§10) |
| Recorded task never re-runs its agent | `run_agent` reuses the execution | `test_crash_between_record_and_checkpoint_*` (mutation-checked) |
| Deactivated user stops a resumed run | refresh refused → FAILED, grant revoked | `test_deactivated_user_stops_a_resumed_run` |
| No live grant behind a finished run | revoke before finish; cancel always revokes | happy path + cancel + deadline tests (mutation-checked) |
| Trace content needs the agent's permission | `get_trace` redaction | `test_trace_content_needs_what_the_agent_needed` |

[P] one orchestrator process, api as STS. [Prod] IdP token exchange, leased resumption,
Kafka start (Phase 18). [Ent] per-tenant grant policy, step-up auth, revocation feed.

## 4. Files
```
docs/adr/ADR-019-orchestrator-graph-and-delegation.md   (+ notes in ADR-017, ADR-018)
libs/security  auth.py: Delegation, issue/verify_delegated_token; testing.py: delegated_token_for
libs/web       auth.py: Authenticator(delegation_public_key=...).verify_obo()
libs/models    api/delegation.py, api/investigations.py, InvestigationAccepted.investigation_id
libs/db        models identity.DelegationGrant/Event, orchestrator checkpoint tables + 3 columns;
               alembic 0018; users.py api_svc
services/api   sts.py (token exchange), config/main (DB + key), routes (investigate → orchestrator,
               investigations read/trace/cancel), proxy content_type
services/orchestrator  api, graph, engine, store, delegation, clients, main, config, __main__ (CLI)
               runner.py REMOVED; tests: test_graph_units.py, test_compare_preflight.py
services/tool-gateway  verify_obo + inv/inc binding + audit delegation_grant; rag refuses delegated
services/agents        verify_obo + inv/inc binding
scripts/smoke-phase9.sh (+ smoke-phase8.sh needs run-orch); scripts/synth load.py truncates new tables
tests/integration  agents/test_orchestrator_e2e.py (15), security/test_delegation.py (14), fixtures
Makefile  delegation-keys, run-orch, stop-orch, orch-smoke; agents-tokens adds delegation:create
```

## 5–6. Commands (macOS, zsh)
```zsh
cd ~/projects/ai-engineering-platform
git switch main && git pull           # must include the Phase 8 merge
git switch -c phase-9-orchestration
tar xzf ~/projects/_to_delete/phase9.tgz
uv sync --all-packages                 # adds langgraph + langgraph-checkpoint-postgres
make delegation-keys                   # secrets/delegation_{private,public}.pem
make up && make db-upgrade && make db-check     # 0018
make db-users                          # adds api_svc
make agents-tokens                     # orchestrator token gets delegation:create
make check && make test-integration
```

## 7. Config
`AEOI_ORCH_*`: `investigation_deadline_s` (900, clock starts at the first run slot),
`task_deadline_s` (180), `max_concurrent_investigations` (2), `resume_on_startup` (true),
`token_refresh_margin_s` (60), `api_url` (:8000). `AEOI_API_*`: `delegation_grant_ttl_s`
(7200), `delegation_token_ttl_s` (300), `db_user` (api_svc), `orchestrator_url` (:8002).
Agents / tool-gateway: `delegation_public_key_file` (absent = delegated tokens refused).

## 8. Tests
- Unit: delegated token verify/issue (12), `verify_obo` (1), checkpoint-migration pin, task id
  determinism, start validation, UTC display. `make check`: 528 passed.
- Integration (real Postgres, least-privilege logins, real checkpointer, fake model): 15
  orchestrator e2e + 14 delegation security + the moved Phase 8 tests. Full suite: 165 passed.
- **Mutation checks** (each test fails when its guard is removed): recorded-execution reuse,
  revoke at finalize, directory re-check on refresh, tool-gateway binding, agents binding.
  One mutation first SURVIVED: the "crash after evidence" test never reached the reuse guard;
  a new test kills the process between the DB write and the checkpoint.
- Independent review: 12 findings, no auth bypass; 11 fixed with tests (ADR-019 "Review").
- Found by tests/runs, not by review: `make db-seed` would fail after the first investigation
  (TRUNCATE users vs grants FK); the Phase 7 `planted` fixture collided across conftests;
  `agent-status` printed local time labelled `Z` (same bug class as Phase 8 facts).

## 9. Run (one terminal each)
```zsh
make dev ; make run-llm ; make run-tools ; make run-audit ; make run-agents
make run-orch          # :8002 - resumes RUNNING investigations at start
make orch-smoke        # product path + delegation record
make agent-smoke       # Phase 8 checks, now through the orchestrator
```

## 10. Verify (gate for Phase 10)
- [ ] `make orch-smoke` all ✔ (a "no model labels" note means the model degraded - read why)
- [ ] **Crash + resume, by hand** (llama gives you ~60 s):
      ```zsh
      make agent-run INCIDENT=INC-10001 ROUTE=local NOCACHE=1   # terminal A; wait for the dots
      # terminal B (run-orch) as soon as A prints "started investigation": Ctrl-C, then:
      make run-orch                                              # resumes it at start
      make agent-status INCIDENT=INC-10001
      ```
      Expect the newest line `COMPLETE … log_analysis=SUCCEEDED(x2)`: attempt 2 = resumed.
      (Terminal A keeps polling through the restart: `orchestrator down, waiting…`.)
- [ ] `make agent-smoke` all ✔ and `make test-integration` green

Measured here (cloud sandbox, fake model, so no latency numbers are claimed): `kill -9` of
the orchestrator during the agent call → restart → COMPLETE, task attempt 2, token obtained
by refresh (no user token), 4 tool calls recorded instead of 2 (the orphaned call finished).

## 11. Failure scenarios
| Symptom | Cause | Fix |
|---|---|---|
| `run-orch`: `missing delegation keys` | new key pair not created | `make delegation-keys`, restart `make dev` (the api loads the private key at start) |
| start → 503 `Token exchange is not configured` | api has no `api_svc` login or key | `make db-users`, `make delegation-keys`, restart `make dev` |
| start → 403 `orchestrator's own service token was rejected` / `delegation:create` | old orchestrator token | `make agents-tokens`, restart `run-orch` |
| start → 403 `lacks investigations:run (directory roles)` | token claims a role the directory does not give | the directory wins, by design |
| start → 400 `Idempotency-Key header is required` | client sent none | the CLI/smoke send one; curl: `-H "Idempotency-Key: …"` |
| 409 `already RUNNING` | one investigation per incident | wait, or `cancel <id>` |
| task `x2` in status | resumed after a crash during the agent call | expected; the first call's tool calls are also recorded |
| FAILED `delegation refused … deactivated` after restart | user deactivated / lost role while down | by design: the grant is revoked |
| FAILED `deadline exceeded` | run took > 900 s from its first slot | raise `AEOI_ORCH_INVESTIGATION_DEADLINE_S` only with a reason |
| FAILED `evidence was not stored` | incident-service down at collect_evidence | `uv run python -m aeoi_orchestrator repost-evidence <id>` → COMPLETE |
| knowledge tools → 501 for an agent | rag doesn't accept delegated tokens yet | Phase 10 |
| `make db-seed` fails `cannot truncate … delegation_grants` | old loader | this phase's `load.py` truncates them |

## 12. Production considerations
IdP token exchange (RFC 8693) instead of the api; sender-constrained tokens (DPoP/mTLS) so a
stolen delegated token is useless; tool-gateway revoked-grant check (closes the ≤ 5 min
window); leased resumption (`FOR UPDATE SKIP LOCKED`) for >1 replica; `AgentTask` with an
idempotency key so a resumed call doesn't repeat work (Phase 18); checkpoint retention job.

## 13–15. Interview
`docs/interview/phase-09-orchestration.md`.
