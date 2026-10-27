# Phase 28 - Failure testing (ADR-040)

Status: **28a RUN on the Mac 2026-10-27: 4 of 4 PASS** (3 defects found + fixed by the sandbox dry run first; 5 findings written down, section 4). Verify = PR checks. Owner: "go 28"; order **28a outages
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

## 4. Runs on the Mac (kind, the owner ran them; commit f00127c, 2026-10-27 ~03:12-03:23 UTC)
Before the runs the Mac was NOT fit to measure: the disk was 99 % full (3.7 GB free), Kafka had
been unhealthy for 5 h (offset loads waiting 7-13 s "in the scheduler"), every pod's probes timed
out in the same 5 s, the kube-scheduler / controller-manager had restarted ~52 times in 6 days,
and the orchestrator was SIGKILLed once (exit 137) by its liveness probe. The owner freed space
(20+ GB), restarted Kafka, rebuilt and redeployed with `routing.load.yaml`. Probe timeouts were
STILL seen on every pod ~40 s after that deploy (a rollout stalls the node) - the pg run started
soon after them (Gate 3 skipped; noted, results kept).

| experiment | verdict | what callers saw during the fault | first OK after undo | investigation | restarts |
|---|---|---|---|---|---|
| pg (stop 60 s) | PASS | 96 OK, **61 dropped connections**, 4 x 503 | 9.4 s | PARTIAL, 135 s | 0 |
| kafka (stop 60 s) | PASS | 186 OK; incidents created during it: 201 | 0.2 s | PARTIAL, 20 s | 0 |
| orchestrator (pod delete) | PASS | 110 OK | 0.0 s | PARTIAL, 39 s | 0 |
| agents (pod delete) | PASS | 82 OK | 0.5 s | PARTIAL, 20 s | 0 |

Read from the DB after the runs (owner's queries, `orchestrator.investigations` + `tasks`):
- **All four ended PARTIAL for the same reason, and it is the fake models', not the fault's:**
  "hypothesis: model skipped them; critic: gave no alternative; postmortem: narrative empty".
  No run lists a missing SOURCE. (A fault-free baseline from Phase 27 was not found in the
  queried window - the identical reason across four different faults stands in for it.)
- **pg:** the run took 135 s (others 20 s): it WAITED for the DB and resumed - fix 3 working on
  kind. `historical_incident` ran twice (attempt 2): the interrupted node re-ran, and it is
  recorded, as ADR-019 requires.
- **orchestrator:** `postmortem` attempt 2 - the kill landed inside postmortem; the new pod
  resumed from the checkpoint and re-ran only that node (39 s vs 20 s).
- **agents:** every task attempt 1. The delete did NOT interrupt work: a pod delete is graceful
  (preStop + grace period), so the request in flight finished. This PASS proves graceful
  shutdown, not failure handling - a mid-task CRASH is still untested.
- **kafka:** both outboxes drained to 0; the DLQ holds 4 letters, all from 2026-10-15 (2 audit
  poison events with `None` times, 2 with no metadata): nothing new went to the DLQ.
- After the runs: default routing redeployed, all pods 0 restarts, `make smoke` 12/12.

### Findings (written down, not fixed in 28a)
1. **A DB outage is a full api outage, with dropped connections instead of 503s.** The api's
   readiness includes incident-service, whose readiness includes the DB: Kubernetes removes the
   api from its Service, so fix 1's 503 + Retry-After reached a caller only 4 times (61 drops).
   Readiness that depends on downstream services is a known anti-pattern. -> 28c (with shedding).
2. **The node itself stalls** during rollouts and under disk pressure: all probes time out at
   once, and the liveness probe (3 s x 3, 10 s apart) killed a healthy orchestrator once. One
   data point - probes NOT changed on it; freeing the disk did not remove the rollout stalls.
3. **Crash (SIGKILL) mid-task is untested** (finding from the agents run). A forced delete
   (`--grace-period=0 --force`) is the next experiment if the owner wants it.
4. **My rules missed the DLQ** (the Kafka prediction says "nothing to the DLQ"); checked by hand.
5. Old: 4 DLQ letters from 2026-10-15, two without metadata - not investigated.

## 5. Verify
- [x] The four experiments run on the Mac; result files committed (`data/chaos/results/`, 4 PASS);
      findings written down (section 4).
- [x] Afterwards: `make k8s-status` all ready (0 restarts), `make smoke` 12/12.
- [ ] PR checks green.
