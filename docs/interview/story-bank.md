# Story bank (Phase 31a): 6 true stories

Format: situation, what I did, evidence, result, what I would change. Every number has a file.
Tell each one in about 2 minutes. Lead with the decision, not the tech.

## S1. A "2 s timeout" that was really 4 s
- **Situation:** 3 of 7 demo gate runs failed: `webhook answered 503: incident-service timed out.`
- **What I did:** read Tempo traces of the 4 slowest alert requests instead of guessing.
  httpx `timeout=2.0` limits EACH step, not the call: the catalog call waited 2.2-4.0 s. Add
  1.3-1.5 s lost before the handler started (CPU-starved node) and 0.3-0.7 s of DB work, and the
  answer came after the api's 5 s. The api said 503 for incidents that WERE created
  (INC-10332, 10334, 10336). Fix: a TOTAL 1.5 s deadline (`asyncio.timeout`), a budget test,
  warm-up before "ready", and a timed-out POST now says it may have been applied.
- **Evidence:** `docs/phases/phase-29.md` §8, ADR-044 amendment, `tests/unit/test_alert_budget.py`.
- **Result:** first alert after a fresh start 4.8-23.7 s -> 1.7-2.0 s; no 503 since fix5.
- **What I would change:** deadline propagation (the caller's remaining budget in a header) from
  day one. Still open: one run lost ~3.3 s outside the catalog on a starved node, more than the
  budget test assumes.

## S2. The laptop, not the code (and I did not hide it)
- **Situation:** a restart storm: 6 services failed liveness in the same second; the
  control plane had 95-96 restarts in 9 days.
- **What I did:** measured the machine read-only: node CPU pressure (PSI) 39%, `pmset`
  CPU_Speed_Limit 39: power-limited by a 60 W charger (Apple ships an 87 W one for this 15"
  2018 MBP; that is the spec, not a measurement). I did NOT loosen probes or timeouts: that turns
  a starved machine into green checks. Every result file now records the machine, and the gate
  rule (fixed before the gate restarted) counts only runs where no pod restarted.
- **Evidence:** `docs/phases/phase-29.md` §8 ("Finding: the machine, not the code"), `machine`
  block in every `data/demo/results/demo-*.json`.
- **Result:** the gate passed 4 counted runs in a row (041438, 042936, 165011, 170026).
- **What I would change:** record the machine from the first result file, not after the first
  confusion.

## S3. The audit trail that would have stopped in January
- **Situation:** the Phase 30 architecture review.
- **What I did:** two independent reviewer subagents; I re-checked every finding in the code and
  on the Mac. Audit partitions were created ONCE by migration 0011 and ended at 2026-12: from
  2027-01 rows would go to the default partition and every later creation would fail. Fix:
  migration 0030 (`ensure_partitions` as SECURITY DEFINER, pinned search_path, EXECUTE only for
  the audit login) and a keeper in the service, 3 months ahead, every 6 h.
  One reviewer claim was false: alert retries do NOT create duplicate incidents (one incident per
  fingerprint + startsAt; every demo re-send answers `duplicate`).
- **Evidence:** `docs/phases/phase-30.md` §3 #1 and #13, ADR-048, migration 0030.
- **Result:** on 2026-11-21 partitions exist up to `audit_events_y2027m02`.
- **What I would change:** in production, an alert on "next month's partition missing", and audit
  to WORM storage.

## S4. I threw away my best number
- **Situation:** the MTTI report said "AI first 6 of 6, sign test p=0.031" against a human.
- **What I did:** read what the human side actually did: the last 5 answers took 56-174 s, with no
  metrics call; two queried the previous case's services; answers repeated. Nobody investigated.
  I declared the baseline invalid (my own demo check would have PASSED on it, also a fault).
- **Evidence:** `data/mtti/results/mtti-2026-10-29T155548.json` (`manual_valid: false`),
  ADR-047 amendment.
- **Result:** I quote the AI side only: 14/17 identified (Wilson 59-94%), median 35.7 s, on
  planted scenarios. No "faster than a human" claim.
- **What I would change:** a valid baseline needs a blind person, a practice round, one focused
  sitting per case. Design that before collecting data.

## S5. My cost estimate was 3x wrong
- **Situation:** I estimated $0.30 for the 16-case real-model eval from 3 runs.
- **What I did:** ran it: $0.98 ($0.04-0.10 per case). The 3 runs were PARTIAL (critic unanswered)
  and did less work. Lesson written down: never extrapolate cost from runs that did less.
- **Evidence:** `docs/phases/phase-25.md` §5a, §5c, §14.
- **Result:** real models changed no answer vs the fake ones (15/16 passed in both); VALIDATED
  9/11 right (Wilson 0.52-0.95); rollback proposals 5 correct, 0 wrong, 2 missed.
- **What I would change:** a budget per run before the run, and not running the postmortem writer
  on INCONCLUSIVE cases ($0.05-0.07 each, wasted).

## S6. Least privilege, and the leak I found anyway
- **Situation:** agents can read production data; one action (rollback) can change it.
- **What I did:** agents are read-only. A rollback needs an approval bound to the action + the
  sha256 of the validated args + the incident + the approver, consumed once, executed AS the IC
  with the IC's own token. The requester cannot approve (DB CHECK); an ENGINEER gets 403.
  The Phase 26 threat-model tests found a real gap first: an email typed in an incident title
  reached the hosted model. Fixed: the LLM gateway scrubs PII before every hosted provider.
- **Evidence:** ADR-026 §A-B, `tests/demo/demolib.py:247`, `docs/phases/phase-26.md`, ADR-038.
- **Result:** every demo run checks ENGINEER 403 -> IC approves -> EXECUTED.
- **What I would change / still open:** names and addresses are not caught (no NER model,
  accepted gap); Kafka is PLAINTEXT without ACLs.
