# ADR-037: Investigation eval = planted causes through the product path, a regression gate in CI

Status: Proposed (25a verified PR #34, 25b verified PR #35; 25c adds the import + dashboard)
Date: 2026-10-22
Refines: architecture.md §20 (MLOps), ADR-021 (code proposes, the LLM ranks), ADR-020 (outcome)

## Context
- Nothing measured whether an investigation finds the RIGHT cause. The 600 labelled incidents
  (`rag.historical_incidents.root_cause_category`) are history: no logs, metrics or deploys,
  so they cannot be investigated. The telemetry held ONE investigable scenario (the demo).
- The code proposer names causes by KIND (`change` = a deploy, `saturation` = a resource
  metric, `traffic`), not by the 12 root-cause categories. Some categories (certificate, DNS,
  disk, a downstream service) leave no signal the system models.
- The `eval` schema (datasets, eval_cases, eval_runs, evaluations) existed since Phase 3;
  nothing wrote it.
- Owner decisions (2026-10-22): the full eval lab in slices (25a here; 25b comparison +
  calibration; 25c the other evals in the schema + a dashboard); real models opt-in only.

## Decision
- **A scenario pack** (`aeoi_synth.scenarios`, v1, 16 cases): deterministic from the seed, each
  case on its own service and day AFTER the demo day (no overlap with the main dataset - a
  test checks the ranges). 11 detectable (deploy, config change, flag, pool / thread pool /
  CPU saturation, traffic; with decoys: a later hotfix, another service's earlier deploy, a
  saturation that is a CONSEQUENCE, a deploy 170 min early) and 5 undetectable (certificate x2,
  DNS, disk, a downstream service). One undetectable case has an innocent deploy just before
  the symptoms: a planted correlation trap.
- **The truth is written from what was planted**, before any run, and stored only in
  `eval.eval_cases.expected`. No row the agents can read carries the label, and no service
  imports the pack or reads `eval.eval_cases` (tests).
- **The runner uses the product path** (`scripts/eval_investigations.py`): create the incident
  via the api, `POST /investigate`, read `/hypotheses`. Scores per case: top1, in_top3,
  confident_wrong (a HIGH answer that is wrong - for an undetectable case, ANY HIGH),
  decoy_above, rule_out_ok, passed. Results: `eval.eval_runs` + `eval.evaluations` + a JSON
  file. Matching is exact (cause kind + subject in the statement): no LLM judge.
- **A run with incomplete evidence is INVALID, not wrong.** The first runs found it: 16 cases
  back to back hit the tool gateway's per-user rate limit (60/min, burst 20); metrics
  SUCCEEDED with 7 of 9 queries refused, and 4 saturation/traffic cases "failed" because the
  signal was never read. The runner now paces cases (15 s) and exits 1 on any case whose
  evidence was incomplete.
- **Product fix (same PR):** the orchestrator's `outcome()` now names a SUCCEEDED metrics or
  deployment task whose `degraded` says evidence is missing (`evidence incomplete (...)`) and
  makes the run PARTIAL. Before, such a run said "no hypothesis is supported by the evidence"
  as if the signal was absent. log_analysis' `degraded` (labels by rule) is not evidence loss.
- **CI = a regression gate, not an accuracy target**: the infra-smoke job seeds the pack and
  runs the eval with the FAKE models (= the code path, rubric order) against a committed
  baseline (`data/eval/baselines/investigations-v1-fake.json`). It fails when a case that
  passed there fails now, or when any case is invalid. A fix that turns a FAIL into a PASS
  updates the baseline in the same PR.
- **Paid models are opt-in**: the runner reads the LLM gateway's routes and refuses if any
  route can reach a paid provider, unless `COST=1`; it then reports the cost the
  investigations recorded.

## What the numbers mean (and do not)
- Measured on scenarios written for this eval: an UPPER bound. A clean planted signal is easy;
  real incidents are not. "11/11 detectable cases right" says the code path does what it was
  designed to do on clean signals - not that it finds real root causes 100 % of the time.
- The trap case is the honest result: an innocent deploy minutes before the symptoms is
  blamed with HIGH confidence. Timing + same service is correlation; the rubric cannot tell.
- The FAKE-model run measures the CODE path only (the ranker falls back to rubric order, the
  critic does not answer). What the LLM ranker and critic add is a real-model run (COST=1).

## 25b additions (2026-10-23)
- **Shown vs stood behind:** the eval scores the band (`confident_wrong`: HIGH and wrong) apart
  from the status (`validated_wrong`: VALIDATED and wrong) and the rollback proposal
  (correct / wrong / missed). The band is advice; VALIDATED + the proposal is what the system
  acts on. The first reading of S14 looked only at the band and was wrong.
- **Calibration = Wilson 95 % intervals per band and per status**, never a bare percentage:
  with a dozen claims the interval is the honest answer (HIGH 8/11 -> 0.43-0.90). No
  bootstrap: one proportion has a closed-form interval. Critic alternatives are counted, not
  scored (no exact match without a judge).
- **Re-scoring is free:** a run's stored hypotheses and plan are scored again by a new scorer
  (`make eval-rescore`), so a paid run is never repeated to measure something new.
- **No rubric change from 3 critic verdicts:** "weakened -> cap at MEDIUM" would fix S14 and
  break S04. The critic's doubt is right 2 of 4 times on this pack; its support 9 of 11.
- **The runner retries only safe calls** (GET, or POST with an Idempotency-Key, same key);
  everything else is never repeated.

## 25c additions (2026-10-23)
- **Files are the record, the schema is a read copy.** `make eval-import` maps known result
  kinds into `eval`; anything it cannot prove valid is refused and named (fake embeddings,
  `valid: false`, files older than the validity check, invalid investigation cases). Derived
  files (calibration), tuning (rag-sweep) and anecdotes (agent-log-compare) are never imported.
- **An importer, not each eval writing `eval`:** the rag service does not own the eval schema.
- **Grafana reads numbers only** (0028): never the planted truth, run config or per-case text.

## Not done (and why)
- No `services/evaluation` service: the runner is a batch CLI; a service adds deploy, auth
  and a port for no new guarantee. [Prod] a worker consuming InvestigationCompleted.
- The runner writes `eval` as the schema owner (dev login). [Prod] the evaluation service's
  own login.
- No prompt/model A/B beyond FAKE vs real: the real run changed no answer, so a second paid
  config would measure noise on 16 cases. Revisit with a bigger pack.
- The rate-limit design question (agents acting for a user spend that user's interactive
  budget: 3 investigations in a minute exhaust it) is recorded, not changed here.

## Tradeoffs
- +~4 min on the infra-smoke job (16 paced cases).
- A baseline must be updated on purpose when behaviour improves - one more file in a PR.
- 16 hand-written scenarios can be gamed by tuning the rubric to them. Mitigation: the
  baseline makes every change in outcome visible in review; new scenarios go in a new pack
  version, never by editing v1's truth.

## Prototype / Production / Enterprise-scale
- Prototype (this): a planted pack, exact scoring, a regression gate on the code path.
- Production: labelled REAL incidents (postmortem-confirmed causes) as a second dataset;
  real-model runs nightly with cost budgets; online feedback (`feedback` table) joined in.
- Enterprise: per-team datasets, drift detection, eval results gating prompt/model releases.
