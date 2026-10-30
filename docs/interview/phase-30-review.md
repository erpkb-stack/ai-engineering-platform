# Phase 30 - Architecture review: interview notes (ADR-048)

Review of the running system (9 services) by two independent reviewer agents; every finding
re-checked in the code, and on the Mac where it needed the real system. `docs/phases/phase-30.md`.

## Questions
1. You reviewed your own architecture - how do you avoid grading your own homework?
2. What was the most serious thing the review found?
3. You had 11 services. Was that right?
4. Why keep Kafka for one consumer?
5. What did the reviewers get wrong?
6. What did you leave open, and why is that acceptable?

## 30-second answers
1. The reviews ran as separate agents with fixed instructions, read-only, and every finding
   had to cite file and line or say what would prove it. Then I checked them: two findings
   needed the real system, so I queried the Mac - one got worse (partitions end in 2 months),
   one got milder (approvals ARE audited there; it is a default, not a broken trail).
2. A time bomb: the audit table's monthly partitions were created once by a migration. On the
   Mac they ended at December 2026; from January every row lands in the default partition, and
   after that new partitions cannot be created at all. The service now keeps three months
   ahead itself, through a SECURITY DEFINER function only its role may execute.
3. No - 9 run. Two were never built: evaluation became the offline eval job, and the knowledge
   service never earned its place. The doc still drew both, plus Kafka flows that do not exist.
   ADR-011 already said I would start with a modular monolith in a real company; I built
   services to practise boundaries, and Phase 30 makes the doc match what I built.
4. As a working pattern, with honest scope: outbox, idempotent consumer, DLQ and replay are
   verified and the audit trail of approvals depends on it. I removed what it did not do (three
   topics nobody used, a dedupe table nobody wrote) and made it ON by default, because "off by
   default" meant a deployment without two env lines audited no approvals.
5. One HYPOTHESIS was false - alert retries do not duplicate incidents (dedupe on fingerprint +
   start time; every demo re-send proves it). And some line numbers were wrong. That is why a
   review finding is input, not truth: I verify before it reaches the doc.
6. A pre-signature rate limit, a warm-up that does not fail closed, a dev harness that could be
   aimed at another host, defang gaps, no outbox purge, no resume lease. Each is written in the
   ADR and the threat model with its trigger: none is reachable in the prototype's threat
   model, and fixing them now would trade interview-prep time for risks that do not exist yet.

## 2-minute answer (Q2, the one to practise)
The review found that the audit trail - the thing that proves who approved a rollback - would
stop working on its own. Audit events are partitioned by month, and the partitions were created
once, inside a migration, with a comment saying "run monthly". Nothing ran it. I checked the
Mac: partitions up to December 2026. Two things then happen: from January every row falls into
the default partition, and Postgres refuses to create a January partition while the default
holds January rows - so it is not a slow degradation, it is a cliff. The fix has three parts:
the function became SECURITY DEFINER, so the service's least-privilege login can run it without
DDL rights, and only that role may execute it; it skips a month that is already in the default
partition with a warning instead of failing every later month; and the audit service calls it
at start and every six hours, keeping three months ahead. The tests run as the real audit login
and fail on the old code. What I would add in production: an alert when next month's partition
is missing - the fix should not depend on anyone reading a log.
