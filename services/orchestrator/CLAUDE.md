# orchestrator (Python 3.12) — port 8002 — package `aeoi_orchestrator`

**Purpose:** start, run, resume, cancel and record investigations (ADR-019). A FastAPI service;
the investigation is a LangGraph graph (`graph.py`) checkpointed in Postgres
(`AsyncPostgresSaver`, tables in the `orchestrator` schema, DDL owned by migration 0018).
**Owns schema:** `orchestrator` (investigations, tasks, agent_executions, messages, checkpoint*); login `orch_svc`.
**Calls:** incident-service (read + mark INVESTIGATING AS THE USER, only inside the start
request; write evidence with `evidence:write`), api token exchange (`delegation:create`),
agents (`agents:run` + the DELEGATED token in `X-On-Behalf-Of`). Token: `secrets/orchestrator_service_token.txt`.
**Must never:** store a user's token; call tools or models directly; write another service's
schema; leave a live grant behind a finished investigation (revoke BEFORE finish).

## Code map
- `api.py` HTTP: start (order: read incident → insert → exchange → mark → attach grant → launch), read, trace (redacted without the agent's permission), cancel, resume
- `graph.py` `plan → Send(run_agent) ×4 → collect_evidence → finalize`; every node idempotent;
  `outcome()` COMPLETE / PARTIAL / FAILED (ADR-020); a bad task fails its branch only
- `validation.py` (Phase 11) merge ranker + critic, hard checks (citations, support, timing vs the first sustained symptom) and soft checks (critic answered, critic independent); `reasoning_outcome()` COMPLETE/PARTIAL/INCONCLUSIVE
- `salvage.py` post recorded evidence of SUCCEEDED tasks (deadline → PARTIAL; `repost-evidence`)
- `engine.py` background runs, deadline (from the first slot), resume on startup, cancel, shutdown (rows stay RUNNING)
- `store.py` every DB write, each safe to repeat · `delegation.py` STS client (5-min tokens, refresh from the grant)
- `clients.py` incident-service + agents HTTP · `__main__.py` CLI over the service (`make agent-run/compare/status`)

Rules: a node that can run twice must not do its side effect twice (check the DB first). A
kill during the agent call DOES repeat the agent's work - recorded as task attempt 2, never hidden.
