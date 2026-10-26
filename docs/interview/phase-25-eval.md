# Phase 25 - Investigation eval + calibration: interview notes (ADR-037)

Measured facts only (2026-10-22..23): 16 synthetic incidents with a planted cause (11
detectable, 5 undetectable), through the product path. Code path (FAKE models, CI and sandbox,
identical): detectable top-1 11/11, 1 confident wrong answer (S14). Real models (Haiku ranker +
Sonnet critic, the Mac on kind, $0.98 for 16 cases, p50 40.8 s): no answer changed; VALIDATED
claims 9/11 correct (Wilson 95 % 0.52-0.95); rollbacks 5 correct, 0 wrong, 2 missed. Every
number is an upper bound on scenarios written for the eval.

## Questions
1. How do you evaluate an incident-investigation system without real labelled incidents?
2. Your eval says 11/11. Why don't you believe it?
3. What did the eval find before it produced a single number?
4. What does the LLM add over the code path?
5. How confident is "HIGH confidence"?
6. A critic disagreed with a wrong answer. Why not let it lower the confidence?
7. You found a flaw in your own rubric. How did you fix it without overfitting?

## 30-second answers
1. Plant the cause. Each scenario writes deploys, logs and metrics for its own service and
   day, and the truth goes only into the eval schema (tests: no service reads it, no row
   carries the label). The runner uses the product API, so it measures what a user gets, and
   scores exactly - cause kind + subject - with no LLM judge.
2. I wrote the scenarios with clean signals. 11/11 says the code does what it was designed to
   do, not that it finds real root causes. Too-good numbers on synthetic data are a warning.
   The honest result is S14: an innocent config change minutes before certificate failures is
   blamed with HIGH confidence - correlation in time and place, which the rubric cannot see past.
3. Two silent failures. Back-to-back cases hit the tool gateway's rate limit; the metrics
   task still said SUCCEEDED with 7 of 9 queries refused, and the investigation said "no
   hypothesis is supported" - an unread signal looked like an absent one. The orchestrator now
   marks that PARTIAL and names it, and the eval refuses to score such a case (INVALID).
   Second: my runner treated the API's own "retry with the same Idempotency-Key" as fatal.
4. On this pack: no answer changed. The ranker adds explanations (one fluent, cited and
   physically wrong). The critic adds the GATE: "supported" makes a claim VALIDATED, and only
   a VALIDATED deploy gets a rollback proposal. With FAKE models nothing is ever validated, so
   CI measures the ranking and only a paid run measures the gate.
5. Per band and per status with Wilson intervals: HIGH 8/11 (0.43-0.90), VALIDATED 9/11
   (0.52-0.95). With a dozen claims the interval is the answer; "75 %" alone would be quoted
   as precise.
6. I tested the rule on paper first. The critic weakened 3 top claims: 1 wrong, 2 right. The
   cap would fix S14 and break S04, where a wrong HIGH + VALIDATED pool-saturation claim would
   then outrank the true deploy. And the gate already held: S14 stayed PROPOSED, no rollback.

7. Measure before changing. The flaw: a pool saturation caused BY a deploy was scored HIGH as
   a rival of that deploy. I added 6 counter-cases first - 3 where a resource is the
   mechanism, 3 guard rails where it IS the cause beside a deploy - and a `wrong_high` metric
   the free CI run measures. Then one rule, designed on paper against all 22 cases: wrong HIGH
   6/22 -> 2/22, no guard rail changed. One guard rail failed BEFORE any change (a deploy after
   the pool rose): I left it unfixed, because its fix breaks a case with the same signals.
   What I did not measure: whether the real critic still validates the demoted claim.

## 2-minute answers
**Q2/Q5 (where are the real errors?).** I first read S14 as "the system acts on a wrong
answer". Wrong: I read the band, not the status. S14 was HIGH but PROPOSED - the critic's
"weakened" kept it out of VALIDATED, and the rollback rule needs VALIDATED. The errors the
system actually STANDS BEHIND are two different ones: in S01 and S04, "DB pool saturation" is
HIGH + VALIDATED next to the deploy that caused it. The rubric scores a mechanism as a rival
cause, and the critic called both "supported". That is the next measured decision - a rubric
change with its own eval cases, not a hunch.

**Q4 (cost).** I estimated $0.30 for the real run from 3 cases; it cost $0.98. Those 3 runs
were PARTIAL (the critic had not answered) and did less work. Lesson: never extrapolate cost
from runs that did less. Re-scoring a stored run is free (`make eval-rescore`), so new metrics
never need a second paid run.

**What I would say I did NOT do.** No real labelled incidents (postmortem-confirmed causes are
the production dataset). No prompt/model A/B beyond FAKE vs real: the real run changed no
answer, so a second paid config would measure noise on 16 cases. No rubric change yet.
