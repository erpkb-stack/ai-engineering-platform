# ADR-047: MTTI is measured on the same planted cases, paired, with the rules fixed before timing

Status: Proposed (29e built 2026-10-28; sandbox only)
Date: 2026-10-28
Refines: ADR-043 (demo minute 12:00), ADR-037 (planted cases, exact scoring)

## Context
- The demo script promised "measured MTTI (manual vs AI on the 20-incident set)". Nothing measured
  the manual side. The rule of this repo: no invented numbers.
- The only person available to time is the owner, who built the data with Claude. Any manual
  number is a single, non-blind, optimistic baseline.
- Owner decisions (2026-10-28): manual = a CLI over the same read tools; one fresh paid run of all
  planted cases (~$1.30); 6 manual cases, 30-minute cap.

## Decision
- Cases: the detectable planted cases of packs v1 + v2 (17). AI: all 17. Manual: 4 from v1 + 2
  from v2, seeded; v1-S01 excluded from the manual sample (the demo's own pattern). A plain seeded
  shuffle gave six v1 cases led by S01 - changed BEFORE any timing.
- `make manual`: shows the alert only; tools = the agents' read tools through the api AS the
  owner (SRE token, manual mode, audited like any call); every call and its time is recorded; the
  clock never pauses; `answer <kind> <subject>` stops it; `abandon <reason>` replaces a case with
  a reserve and is reported. The truth is held in memory to score and printed only by `make mtti`.
  Tool output is untrusted on a terminal too: control characters removed, times shown in UTC.
- Identified: AI = top cause is the planted one AND VALIDATED (what the system stands behind; a
  correct but unvalidated top is reported apart). Manual = exact (kind, subject) within 30 min.
  A miss or over-cap answer has NO time (the cap is never used as a time).
- Reported as data, not a headline: per case, identified rates with Wilson 95%, medians with
  min-max over identified cases only, and the paired result on the 6 shared cases (who identified
  first; exact sign test, smallest possible p 0.031). The report refuses a fake-model AI run.
- Demo step `mtti`: PASS only with a report of 6 scored manual cases and a real-model AI run;
  n/a on a rehearsal, FAIL on a real run without one.

- fix1 (owner's first session: 4 cases abandoned before one was worked - a stray Return from a
  paste opened cases, an open case looked like an idle prompt, and there was no practice round):
  a case opens only on the typed word `start`; a resumed case says CLOCK RUNNING; an untimed
  `make manual PRACTICE=1` on v1-S01 (never in the sample); an abandoned case is replaced from
  its own pack, so the 4 + 2 mix holds.

## Tradeoffs
- n = 6, one person, not blind: the comparison shows a direction and a size on THESE cases, not a
  population MTTI. The protocol says so, and so must the demo.
- The AI clock starts at incident creation; the manual clock when the person opens the case. In
  both, the time before someone starts working is excluded (the same for both).
- The AI's 2 s status polling adds up to 2 s to its time (against the AI).
- The CLI is a terminal, not the dashboards an engineer has at work: the manual side may be
  slower than with a good UI - or faster, with no noise to wade through. Recorded, not corrected.
