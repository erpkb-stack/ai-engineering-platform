# orchestrator (Python 3.12) — port 8002 — package `aeoi_orchestrator`

**Purpose:** start, run, resume, cancel and record investigations (ADR-019). A FastAPI service;
the investigation is a LangGraph graph (`graph.py`) checkpointed in Postgres
(`AsyncPostgresSaver`, tables in the `orchestrator` schema, DDL owned by migration 0018).
**Owns schema:** `orchestrator` (investigations, tasks, agent_executions, messages, checkpoint*, release_assessments); login `orch_svc`.
**Calls:** incident-service (read + mark INVESTIGATING AS THE USER, only inside the start
request; write evidence with `evidence:write`), api token exchange (`delegation:create`),
agents (`agents:run` + the DELEGATED token in `X-On-Behalf-Of`). Token: `secrets/orchestrator_service_token.txt`.
**Must never:** store a user's token; call tools or models directly; write another service's
schema; leave a live grant behind a finished investigation (revoke BEFORE finish).

## Code map
- `api.py` HTTP: start (order: read incident → insert → exchange → mark → attach grant → launch), read, trace (redacted without the agent's permission), cancel, resume
- `graph.py` `plan → Send(run_agent) ×4 → collect_evidence → history → code → hypothesize → critique → validate → report → propose → finalize`; every node idempotent;
  `outcome()` COMPLETE / PARTIAL / FAILED (ADR-020); a bad task fails its branch only
- `validation.py` (Phase 11) merge ranker + critic, hard checks (citations, support, timing vs the first sustained symptom) and soft checks (critic answered, critic independent); `reasoning_outcome()` COMPLETE/PARTIAL/INCONCLUSIVE
- `history` node (Phase 13, ADR-023): historical_incident task (reuse guard) → INCIDENT pointers via the evidence endpoint; priors go to hypothesize ONLY (critic copies are blanked); `history_outcome()` PARTIAL when search failed / compared by code / not stored
- `code` node (Phase 14, ADR-024): code_intelligence task (needs the deployment result) → COMMIT/CODE evidence; `reasoning_results()` adds its result to hypothesize/critique/validate/report ONLY when that evidence was stored; `code_outcome()` PARTIAL when it failed / degraded / not stored. `AEOI_ORCH_CODE` (true)
- `report` node (Phase 12): postmortem task → POST timeline points + postmortem (both idempotent); `report_outcome()` PARTIAL when the writer failed/was empty/not stored
- `release.py` (Phase 15, ADR-025): `POST /v1/release-risk` runs the release_risk agent SYNCHRONOUSLY with the caller's OWN token (not delegated, never stored; 12 s), stores one advisory report per request; Idempotency-Key per requester (retry = 200 + first report, other target = 409); agents unreachable = 503 + nothing stored; every read needs code:read + deploys:read. Nothing reads a report to gate a deploy.
- `propose` node + `proposal.py` (Phase 16, ADR-026): CODE rule v1 (top hypothesis = VALIDATED deploy, SUCCEEDED, earlier release exists) → proposal FOR the starter (delegated token, `actions:propose`); a 403 (starter cannot request) is an answer; not recorded → PARTIAL. `way_back` quotes code: shown only to code:read
- `actions.py` (Phase 16): `POST /v1/approvals/{id}/decision` decides AS the IC, then (APPROVED) runs the action_executor with the IC's OWN token (12 s) and records the outcome (`actions:record`); FAILED only when the TOOL failed (`execution_failed` + 4xx) - a refused attempt is never an outcome; unknown → "retry /execute" (safe: one approval never runs twice)
- `salvage.py` post recorded evidence of SUCCEEDED tasks (deadline → PARTIAL; `repost-evidence`)
- `engine.py` background runs, deadline (from the first slot), resume on startup, cancel, shutdown (rows stay RUNNING)
- `store.py` every DB write, each safe to repeat · `delegation.py` STS client (5-min tokens, refresh from the grant)
- `clients.py` incident-service + agents HTTP · `__main__.py` CLI over the service (`make agent-run/compare/status`)

Rules: a node that can run twice must not do its side effect twice (check the DB first). A
kill during the agent call DOES repeat the agent's work - recorded as task attempt 2, never hidden.
