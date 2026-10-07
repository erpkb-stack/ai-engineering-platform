# orchestrator (Python 3.12) — package `aeoi_orchestrator`

**Purpose:** run investigations and record them. Phase 8: a thin runner for ONE agent task
(CLI, no HTTP server). Phase 9: LangGraph graph + Postgres checkpoints replace `runner.py`.
**Owns schema:** `orchestrator` (investigations, tasks, agent_executions, messages); login `orch_svc`.
**Calls:** incident-service (read incident AS THE USER; write evidence with `evidence:write`),
agents (`agents:run` + the user's token). Token: `secrets/orchestrator_service_token.txt`.
**Must never:** call tools or models directly; write another service's schema; leave a RUNNING
investigation or task behind (abort on exceptions; stale ones are cancelled on the next run).

Commands: `make agent-run INCIDENT=INC-n [ROUTE=local|fast] [ROLE=SRE]` · `make agent-compare INCIDENT=INC-n`
· `uv run python -m aeoi_orchestrator repost-evidence <investigation_id>` (retry a failed evidence POST)
