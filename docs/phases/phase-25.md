# Phase 25 - MLOps / evaluation (ADR-037)

Status: **25a VERIFIED 2026-10-23** (PR #34, main 499ede9 green, run 39891545509). **25b VERIFIED
2026-10-23** (PR #35 6/6 green, main run 39901267018 green). **25c VERIFIED
2026-10-23** (PR #36 6/6 green, main 41fcd5a run 39904511914 green, dashboard read on the Mac);
25c-fix1 VERIFIED (PR #37, main e028996 run 39907215844 green, panel re-read on the Mac).
25d VERIFIED (PR #38 6/6 green, main 60a70c4). **25e BUILT: rule R1** (a resource is the
MECHANISM of a close earlier cause), Verify = its PR's checks + one paid run for the status.

## 0. Gate and scope
- **Gate: passed.** Phase 24 verified and closed (PR #33, main ecaaabe).
- **Owner decisions (2026-10-22):** "Full eval lab" in 3 slices (my proposal), real models
  opt-in only. "go 25a" on the design below.

## 0b. Need check (read in the code, 2026-10-22)
1. **No investigable ground truth.** The 600 labelled incidents are history without
   telemetry; `incident.incidents` has no label column (owner's query: column does not exist).
   The telemetry has ONE planted scenario (the demo). An accuracy over 1 case is meaningless.
2. **The proposer's vocabulary is cause KINDS**, so scoring can be exact: kind + subject.
3. **Undetectable categories exist** (certificate, DNS, disk, downstream): the honest test is
   "no confident cause", not accuracy.
4. **The `eval` schema was never written.**

## 1. What 25a adds
- `aeoi_synth.scenarios` + `make eval-seed`: 16 cases (11 detectable, 5 undetectable), own
  service + day each, truth in `eval.eval_cases` only.
- `scripts/eval_investigations.py` + `make eval-investigations [CASES=] [COST=1] [BASELINE=]`:
  product path, exact scores, `eval.eval_runs` / `eval.evaluations` + `data/eval/results/
  investigations-*.json`; paced (rate limit); INVALID run on incomplete evidence (exit 1);
  refuses paid models without COST=1.
- CI (infra-smoke): `make eval-seed` + `make eval-investigations BASELINE=data/eval/baselines/
  investigations-v1-fake.json` - fails on a regression or an invalid run.
- Orchestrator: `outcome()` names "evidence incomplete" of a SUCCEEDED metrics/deployment task
  (PARTIAL) - a regression test failed before the fix.

## 2. Found by the eval before it produced a single trusted number
- **Run 1 (16 cases back to back): 4 detectable cases "failed" - but the system never read
  their signal.** The tool gateway's per-(user, tool) limit (60/min, burst 20) refused 7 of 9
  metric queries per case after the first two cases. The metrics task still SUCCEEDED (with
  `degraded: failed: ... rate_limited`), and the investigation said INCONCLUSIVE: "no
  hypothesis is supported by the evidence" - an absent signal and an unread signal looked the
  same. Even the 7 "passing" cases had degraded metrics. Fixed twice: the orchestrator names
  incomplete evidence (product), the runner paces and refuses to score invalid cases (eval).
  Replayed unpaced after the fix: 14 of 16 cases INVALID, exit 1 - the guard works.
- **A test caught a generator bug:** the last background log line of a case could fall 3 min
  after the pack's range, where `make eval-seed` deletes - a re-seed would have duplicated it.

## 3. First trusted run (sandbox replay of the CI services, FAKE models, 2026-10-22)
| | result |
|---|---|
| detectable: top-1 | 11/11 |
| detectable: in top 3 | 11/11 |
| undetectable: no HIGH claim (abstained) | 4/5 |
| confident wrong (HIGH and wrong) | 1/16 - S14, the planted trap |
| decoy above the cause | 1/4 - S14 |
| rule-outs correct | 6/6 |
| invalid cases | 0 |
| outcomes | 12 PARTIAL, 4 INCONCLUSIVE (FAKE: no knowledge/history service, no critic answer) |
| latency per case | p50 2.1 s, max 2.2 s |
Same result twice, and from a fresh `db-seed` + `eval-seed` through the CI step itself
("no regression vs investigations-v1-fake.json"). The gate fails on a planted regression
(baseline with S14 marked passed -> exit 1).
- **Read this as an upper bound.** 11/11 on signals I planted cleanly says the code path does
  what it was designed to do - not that it finds real root causes. Too-good numbers on
  synthetic data are a warning, not a result.
- **S14 is the real finding:** an innocent config change (log level) 16 min before certificate
  failures on the same service is blamed with HIGH confidence (deploy + 5xx + error logs = 3
  sources). The rubric measures correlation in time and place; it cannot know a log-level
  change cannot break TLS. Candidate for 25b (what does the critic say with a real model?).
- FAKE models = the code path: the ranker fell back to rubric order in every case, the critic
  gave nothing. The LLM's contribution is unmeasured until a real-model run.

## 4. Verify 25a
- [x] PR #34: 6/6 green; the CI eval = the sandbox case for case (16/16), "invalid": [],
      "no regression vs investigations-v1-fake.json"; infra-smoke 4 -> 8 min (paced cases).
- [x] Real models on the Mac (kind, Haiku ranker + Sonnet critic, 2026-10-23): see 25b.
- [x] `main` green after the merge (499ede9, run 39891545509).

## 5. 25b - what the LLM adds, and how far a band can be trusted (owner: "go", 2026-10-23)
### 5a. The real-model run (Mac, kind; 16 cases)
- First tries died on the first call after hours of idle: a 503 "retry with the same
  Idempotency-Key" (the api's own fix7 answer) and a RemoteDisconnected from the LLM gateway.
  The SYSTEM was right; the runner gave up. Fix: safe calls (GET, POST with an
  Idempotency-Key) retry twice with the same key; a final failure names the call and answer.
- Then: 16/16 valid, 12 COMPLETE + 4 INCONCLUSIVE, p50 40.8 s, max 55.8 s per case,
  **cost $0.98** ($0.04-0.10 per case). My estimate from 3 cases was $0.30 - wrong by ~3x:
  those 3 runs were PARTIAL (critic unanswered) and did less work. Do not extrapolate cost
  from runs that did less. The 4 INCONCLUSIVE cases still cost $0.05-0.07 each (the
  postmortem writer runs with no hypothesis) - a small waste, noted.
- **Per case, the real models changed no answer:** passed 15/16 = FAKE, the same cases.
### 5b. S14 - read the status, not only the band
- The ranker (Haiku) wrote a fluent, citation-valid but wrong mechanism for the innocent
  deploy ("DEBUG logging ... triggered TLS certificate validation errors").
- The critic (Sonnet): **weakened**, missing "evidence tying the DEBUG change to TLS cert
  validation, e.g. the cert expiry time"; it proposed "An expired TLS certificate is the root
  cause" (the planted truth) at MEDIUM.
- The band stayed HIGH (code; ADR-021), but the hypothesis stayed **PROPOSED** (VALIDATED
  needs the critic's "supported") and **no rollback was proposed** (the rule needs a VALIDATED
  deploy). The console already shows the verdict and the missing link. My first reading
  ("the system threw the critic's answer away") was wrong: I read the band, not the status.
  The planned "show the critic's challenge" (b) already existed - not built twice.
- A "weakened -> cap at MEDIUM" rule was checked on paper first: the critic weakened 3 tops,
  1 wrong (S14) and 2 right (S04, S15). It would fix S14 and break S04 (a wrong pool
  hypothesis, HIGH + VALIDATED, would then outrank the true deploy). Not adopted: 3 data
  points do not justify a rubric change, and the gate already holds.
### 5c. Calibration (the real run, re-scored for free: `make eval-rescore`)
| claims (code, not rejected) | correct | Wilson 95 % |
|---|---|---|
| HIGH | 8/11 | 0.43 - 0.90 |
| MEDIUM | 3/3 | 0.44 - 1.00 |
| LOW | 0/1 | 0.00 - 0.79 |
| **VALIDATED** (what the system stands behind) | **9/11** | 0.52 - 0.95 |
| PROPOSED | 2/4 | 0.15 - 0.85 |
- Rollback proposals: **5 correct, 0 wrong, 2 missed** (S04, S15: the critic weakened the
  correct deploy). I predicted ~9 correct: wrong - I counted the 4 saturation/traffic cases,
  which no rollback rule can propose. The measurement wins.
- The critic as a filter: "supported" on 11 claims, 9 right; "weakened/refuted" on 4, 2 right.
  Its doubts are a coin flip on this pack; its support is mostly right.
- **Both VALIDATED wrong claims are flaw (c):** pool saturation HIGH + VALIDATED beside the
  deploy that caused it (S01, S04) - a mechanism scored as a rival cause. The critic called
  both "supported". That is the next measured decision, not this phase.
- FAKE vs real, case by case: `passed` identical; 9 cases differ ONLY in status/proposal (the
  FAKE critic never answers, so nothing is VALIDATED and no rollback is ever proposed). CI
  therefore measures the ranking; the action gate is measured only by a real-model run.
- Read every number as an upper bound on planted scenarios, with intervals this wide.

## 6. Verify 25b
- [x] PR #35: all checks green; the infra-smoke eval still "no regression" (passed unchanged).
- [x] Real-model run + rescore + calibration on the Mac (the numbers above, files committed:
      `investigations-2026-10-23T033835-rescored.json`, `calibration-2026-10-23T041541.json`).
- [x] `main` green after the merge (run 39901267018).

## 7. Open items
- (c) mechanism vs cause: measured in 25d (section 10); the rubric rule is the next step.
- The postmortem writer runs (and costs) when no hypothesis survived.
- ~~A FAKE CI run records `cost_usd` 0.00952~~ - checked, by design: `routing.test.yaml` prices
  `fake-large` like a hosted model so CI tests the per-investigation budget ($0.01). Nothing is
  paid; the Mac uses `routing.yaml` / `routing.local.yaml`, so its cost rows are real.
- Rate limits: agents acting for a user spend that user's interactive budget (ADR-037).
- More pack versions for new categories - never by editing an older pack's truth.

## 8. 25c - the evals in one place (owner: "25c first", twice, after my "low value" need check)
- **Need check (mine, 2026-10-23): low value.** Every number already lives in a committed file;
  a dashboard adds a view, not a measurement. Owner chose it anyway: built minimal.
- `scripts/eval_import.py` + `make eval-import [FILES=]`: result FILES -> `eval.datasets` /
  `eval_runs` (+ per-case `evaluations` for investigation runs whose cases are seeded). The files
  stay the record; the schema is a copy. Idempotent (an investigation file keeps the runner's
  run id; others = uuid5 of the file's SHA-256; ON CONFLICT DO NOTHING). Known kinds only.
- **Refused, with the reason printed:** fake-embedding rag/history runs; a rag mode with
  `valid: false`; **rag files written before the Phase 7 validity check** - one of them
  (`rag-2026-10-06T043554`, RERANK=1) is the known-invalid run whose "hybrid" was keyword-only,
  and my first importer accepted it (found by reading the results README, fixed, tested); an
  investigation run with invalid cases; never: `rag-sweep-*`, `calibration-*`,
  `agent-log-compare-*`; anything unknown (e.g. `catalog-latency-*`).
- `summary` written by the importer = numbers only (test); "11/16" becomes `{"k": 11, "n": 16}`.
  A rescore is marked `rescored: 1` (two rows, ONE paid run - the dashboard shows the flag).
- The runner now also stores `validated_wrong` and `proposal_*` per case (one `METRICS` list,
  shared with the importer - a test). Runs before this show those columns empty, not 0.
- **Migration 0028:** obs_reader reads ids, names, status, times, git_sha, `summary`, metric +
  value - never `eval_cases.input/expected` (the planted truth), `eval_runs.config` or
  `evaluations.details` (hypothesis text). Checked AS obs_reader on a migrated Postgres:
  every panel query runs; `config`, `expected`, `details` are denied; downgrade/upgrade clean.
- **Dashboard "AEOI - Evaluation"** (90 days, no auto-refresh): runs table; investigation eval
  per run (passed rate, confident_wrong, validated_wrong, rollbacks correct/wrong/missed);
  rag recall@5 + its 95 % interval per mode; similar-incident hit@5 + interval per mode. Every
  panel says "upper bound" and why FAKE runs show validated_wrong 0.
- Sandbox replay (ci-sim, 2026-10-23): 29 files read -> 7 runs imported (2 rag, 3 history, the
  real-model run + its rescore), 22 refused with a reason; a second run imports 0.

- **Found on the Mac, not in the sandbox (fix1):** a real-model run that died mid-way
  (RemoteDisconnected, before 25b's retries) stayed RUNNING forever, and the investigation panel
  showed it as "10 cases, passed 1.0". The runner marked FAILED only on its own `CallError`, and
  it stored a run with INVALID cases as SUCCEEDED. Fix: any crash or Ctrl-C -> FAILED; INVALID
  cases -> FAILED; the panel shows SUCCEEDED runs without invalid cases only (older rows too).
  Checked on the Mac's own data (read-only query): the RUNNING row is gone, the rest unchanged.

## 9. Verify 25c
- [x] PR #36: 6/6 green; the CI eval "no regression", `invalid: []`.
- [x] Mac: `make db-upgrade` (0028) + `make eval-import`: 10 runs imported, 12 files refused
      with a reason, 3 already there (written by the runner); a second import: 0. Dashboard read in the browser: rescored real run
      passed 0.9375, validated_wrong 2, rollbacks 5 / 0 / 2; rag 2 runs x 3 modes; history 3
      runs x 8 modes; no panel error.
- [x] `main` green after the merge (41fcd5a, run 39904511914).
- [x] fix1 PR #37: 6/6 green, "no regression"; `main` green (e028996, run 39907215844); the
      panel re-read on the Mac: the RUNNING row is gone, the 3 real rows unchanged.

## 10. 25d - flaw (c) measured before any rule (owner: "counter-cases first", 2026-10-23)
- **Read in the code first:** a saturation is contradicted only by symptoms that started
  BEFORE it (`candidates.can_contradict`). In S01 the deploy (-17), then the pool (-12), then
  the symptoms (-10): nothing counts against the pool, so it is HIGH beside the deploy. The
  rubric has no idea of "this resource is HOW the cause did it".
- **Visible without a paid run:** the band is code, so the FAKE run shows the same wrong HIGH
  claims as the real one (S01, S04: PROPOSED with FAKE, VALIDATED with Haiku + Sonnet).
- **New metric `wrong_high`:** any code claim shown HIGH that is not the planted cause (for an
  undetectable case: any HIGH claim). Stored per case; the CI gate now also fails when a case
  WITHOUT one gains one, so a later fix stays fixed (a baseline row without the field is not
  gated).
- **Pack v2 (6 cases; v1 untouched, byte-identical output - checked by a fingerprint):** own
  services, in the gap between the main dataset and v1, 12 h apart.

| case | planted | truth | FAKE now (rubric unchanged) |
|---|---|---|---|
| S17 | deploy, CPU rises, symptoms (no error log) | the deploy | pass; CPU only MEDIUM |
| S18 | deploy, thread pool rises, symptoms + log | the deploy | pass, **wrong HIGH** (thread pool) |
| S19 | traffic surge, pool rises, symptoms | the traffic | **FAIL**: pool top, **wrong HIGH** |
| S20 | deploy 150 min early, pool rises | the pool | pass (deploy capped MEDIUM by lead) |
| S21 | partner service deploys, pool rises | the pool | pass (that deploy LOW) |
| S22 | pool rises, THEN a deploy, symptoms | the pool | **FAIL**: deploy top HIGH, **wrong HIGH** |

- **What it says (6 cases, FAKE = code path, upper bound):**
  1. The flaw is general, but needs a second source: S17's CPU has no error log, so it stops at
     MEDIUM; S18's thread pool has one and is HIGH, like S01's pool.
  2. It is not about deploys: in S19 the pool outranks the traffic surge that drove it.
  3. **S22 is a second, opposite flaw found by a guard rail:** a deploy that started AFTER the
     pool rose (but before the symptoms) is the top HIGH answer. A deploy only gains support
     from what came after it, so it never "sees" that the resource was already rising.
  4. v1 + v2 together: `wrong_high` 6/22 (S01, S04, S14, S18, S19, S22); passed 19/22.
- **Not included on purpose:** an innocent deploy minutes before a pool saturation that is the
  real cause, on the same service: its signals equal S01's and its truth is the opposite. No
  rule can pass both (a coin flip), so it is a known blind spot, like S14.
- **Found by the guard while building:** run back to back (CI: v1, then v2), v2's first case
  was rate-limited and INVALID (the guard worked). The runner now paces before its first case.
- **Next (a separate decision):** a rule must fix S01, S04, S18, S19 (and ideally S22) WITHOUT
  breaking S20, S21 or any v1 case - judged by both baselines, then one paid run.

## 11. Verify 25d
- [x] PR #38: 6/6 green (the eval job can only pass with "no regression" and no INVALID case
      for both packs: the runner exits 1 otherwise); merged as 60a70c4.

## 12. 25e - rule R1: a resource is the mechanism of a close earlier cause (owner: "agree")
- **The rule (`candidates.mechanism_of`):** a resource rise counts AGAINST itself as a separate
  cause when, on its own service, an earlier cause started no later than it: a traffic change,
  or a deploy that is itself a candidate and at most 60 min before it (the rubric's own lead
  limit). The claim stays visible (one contradiction: not HIGH) and says why: "it rose 5 min
  after DEPLOY-...: possibly that cause's mechanism, not a separate cause".
- **Designed on paper first, against all 22 cases, then measured (FAKE, sandbox replay of the
  CI step).** The prediction held case for case:

| | before R1 | with R1 |
|---|---|---|
| wrong HIGH claims | 6/22 (S01 S04 S14 S18 S19 S22) | **2/22 (S14, S22)** |
| passed | 19/22 | **20/22** (S19 fixed) |
| guard rails S20, S21, v1 S06-S10 | pass | pass (unchanged) |

- **One real-model data point agrees, independently of my pack:** in the first real Sonnet
  run on the demo, the critic cited the deploy AGAINST the pool ("an intermediate step, not a
  root cause"); code refused that citation then. With R1 code counts it itself.
- **S22 deliberately NOT fixed:** "a resource that rose before a deploy counts against the
  deploy" would break a case with S22's signals and the opposite truth (a pool drifting up,
  then a bad config deploy causes the errors). The rubric does not read what the error log
  says, so it cannot tell them apart. A second known flaw, with its reason.
- **The bet R1 makes:** a same-service deploy within 60 min before a resource rise caused it.
  An innocent deploy just before a real saturation has the same signals; only real
  postmortems can say which is more common.
- **What R1 does not promise:** VALIDATED depends only on the critic (`validation._status`),
  not on the band. A MEDIUM pool claim the critic calls "supported" is still VALIDATED and
  wrong. Measured only by a paid run; if it stays, the next rule is "a claim code contradicts
  cannot be VALIDATED".

- **CI found what my checks missed:** an integration test still asserted the OLD behaviour
  (the critic's deploy citation against the pool is "disputed"). I had run only the unit
  tests; the integration suite runs in the sandbox too (Kafka tests skip). Test updated: no
  dispute, and the deploy's evidence is now a CONTRADICTS edge of the pool. 262 passed.

## 13. Verify 25e
- [ ] PR: all checks green; CI eval v1 + v2 "no regression" (both baselines updated in it).
- [ ] One paid run on the Mac (v1 + v2, COST=1): `validated_wrong` for S01/S04/S18/S19.
