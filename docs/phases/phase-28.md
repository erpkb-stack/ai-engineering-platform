# Phase 28 - Failure testing (ADR-040)

Status: **28a BUILT 2026-10-26; the sandbox dry run found 3 defects (fixed); not run on the Mac yet.** Owner: "go 28"; order **28a outages
first**; Redis **moved out of the default start** (2026-10-26).

## 0. Gate
- Phase 27 verified: PR #42 merged, `main` run 40399104173 must be green (the apply chain checks
  it first and stops otherwise).

## 1. Need check (code and tests read first)
| fault | tested before | gap |
|---|---|---|
| LLM provider errors / timeouts | unit (breaker, fallback, retries) | real data below |
| Kafka down | unit + `consumer-smoke` | never with the apps on kind |
| Postgres down | **never** | the main gap |
| orchestrator / agents killed mid-run | Phase 9, compose only | **never on kind** |
| Redis down | - | no service uses Redis (ADR-031): nothing to test |
| overload | Phase 27 | slow 503s, no shedding -> 28c |

Real errors on the Mac (`llm.model_usage`, `error_type`, queried 2026-10-26):
- 52 `CircuitOpenError`, all on 2026-10-23, after only 4 `ProviderTimeoutError`: the breaker
  turned 4 timeouts into 52 refused calls. -> 28b reviews the threshold, with data.
- `historical_incident` `StructuredOutputError` 11x (2026-10-14..23), still happening, NOT
  diagnosable: only the class is stored. -> 28b records the schema path + validator keyword
  (never the content).
- Older errors stopped after the prose-limits fix.

## 2. What 28a adds
- `tests/chaos/chaoslib.py` - the experiments, each with its prediction written first, and the
  rules (pure; `tests/unit/test_chaos.py`). `tests/chaos/run.py` - the runner. `make chaos EXP=`.
- `tests/load/tokens.py` - user tokens + the RUNNING count, shared by load and chaos.
- Redis: compose `profiles: ["redis"]`; `check-ports` / `verify-infra` stop expecting it;
  a test fails if a service imports redis while it is off by default.

Timeline per run: 20 s of reads -> start one investigation (scenario-pack incident, inputs only)
-> 10 s later the FAULT -> hold 60 s (pg/kafka) -> UNDO -> wait until the investigation ends
(max 5 min) -> 35 s of clean tail. Reads: `GET /api/v1/incidents?limit=5` every 0.5 s, 45 s
client timeout. Kafka: one incident created every 5 s too.

Rules (fixed now, never after a run): `no_hang`, `recovers` (<= 60 s, clean last 30 s),
`no_restarts`, `investigation_named` (never stuck RUNNING; "not exercised" if it ended before
the fault), Kafka: `writes_survive`, `outbox_drained` (<= 180 s).

Predictions (from the code, before any run):
- **pg:** no restarts (liveness depends on nothing). incident-service readiness fails, and the
  api's readiness includes incident-service -> the api leaves its Service: callers see
  connection errors, not a 503 problem+json. Risk: the investigation in flight is stuck RUNNING.
- **kafka:** creating incidents keeps working (outbox), consumers wait, outboxes drain after.
- **orchestrator:** the pod's shutdown leaves the row RUNNING; the new pod resumes it.
- **agents:** a task in flight fails or is retried; PARTIAL at worst.

Not covered: a crash (SIGKILL / OOM) - a pod delete is a graceful stop; one probe endpoint;
one run each (a PASS is "seen once").

## 3. Found by the sandbox dry run (pg_ctl stop/start; mechanics, not numbers)
The runner was dry-run in the CI simulation (processes, no kind, no kubectl: `no_restarts` =
not measured). The first two runs found three real defects, each fixed with a test that failed
before the fix:
1. **A lost database answered 500 "unexpected error"** (118 of them) - a bug report for a
   retryable condition, no `Retry-After`. Now 503 `upstream-unavailable` + `Retry-After: 5`, in
   the shared middleware (`aeoi_common.errors.database_unavailable`, matched along the MRO:
   the class raised was psycopg's `AdminShutdown`, a subclass). The log keeps the TYPE only:
   a DB error's text can carry SQL parameters. (`libs/web/tests/test_web.py`)
2. **The checkpointer's pool handed out dead connections after a DB restart** - the SQLAlchemy
   engines all had `pool_pre_ping`, this psycopg pool had no check. Now
   `check=AsyncConnectionPool.check_connection`.
3. **The investigation in flight died and stayed RUNNING** (the predicted risk, confirmed):
   `aget_state` raised OUTSIDE the graph's error handling ("Task exception was never retrieved");
   the row stayed RUNNING until the next orchestrator start. Now a lost DB WAITS (2 s doubling to
   30 s, outside the slot) and resumes from the checkpoint; any other crash there ends FAILED.
   The deadline still holds. (`services/orchestrator/tests/test_engine_db_outage.py`)
4. A runner bug of mine: an error answer to the pre-fault poll was read as "already ended",
   which would have hidden defect 3. Now only a known end counts as "not exercised".

After the fixes the same dry run: reads 503 during the outage, first OK 0.5 s after the start,
the investigation waited 4 times and ended INCONCLUSIVE (fake models), verdict PASS.
Not dry-run here: Kafka (no broker in the simulation), the pod deletes (no kind).

## 4. Runs on the Mac
(not run yet)

## 5. Verify
- [ ] The four experiments run on the Mac; result files committed; every FAIL written down as
      a finding (fixed in 28a only if small, else its own slice).
- [ ] Afterwards: `make k8s-status` all ready, `make smoke` passes, no RUNNING investigations.
- [ ] PR checks green.
