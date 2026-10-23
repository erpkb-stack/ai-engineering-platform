# Phase 25 - MLOps / evaluation (ADR-037)

Status: **25a BUILT in the sandbox (2026-10-22), Verify = the PR's checks + an optional
real-model run on the Mac.** 25b (A/B comparison with bootstrap CI, band calibration) and 25c
(rag/history/risk evals in the `eval` schema + an eval dashboard) after 25a is verified.

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

## 4. Verify
- [ ] PR: all checks green; the infra-smoke log shows the eval table, "invalid": [] and
      "no regression vs investigations-v1-fake.json".
- [ ] (optional, costs money) Mac: real models on the same pack -
      `make eval-seed && make eval-investigations COST=1` with the normal routing; paste the
      summary line and the cost. Expected to differ from FAKE only through the LLM ranker.
- [ ] `main` green after the merge.

## 5. Open items
- 25b: A/B (FAKE vs real ranker; prompt versions) with bootstrap CI on this pack; band
  calibration (how often HIGH/MEDIUM/LOW are right) - with the upper-bound caveat.
- Rate limits: agents acting for a user spend that user's interactive budget (ADR-037).
- More pack versions (v2) for new categories - never by editing v1's truth.
