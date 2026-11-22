# Resume bullets (Phase 31a): every number with its source

Rule: a number goes on the resume only if a file below holds it. Planted scenarios = an upper
bound; say so if asked. The owner marks which bullets to use (phase-31.md Verify).

| # | Bullet | Source |
|---|---|---|
| 1 | Built a multi-agent incident investigator where facts come from code and the LLM only labels; on planted causes it identified 14/17 (Wilson 59-94%), median 35.7 s; any rollback needs human approval | ADR-018, ADR-021, ADR-026, `data/mtti/results/mtti-2026-10-29T155548.json` |
| 2 | Built an eval lab with exact scoring and a CI regression gate; one paid real-model run ($0.98, 16 cases): VALIDATED claims 9/11 right (Wilson 0.52-0.95), rollback proposals 5 correct, 0 wrong, 2 missed | ADR-037, `docs/phases/phase-25.md` §5a, §14 |
| 3 | Integrated a contract-first Java (Spring Boot) catalog behind an existing tool; parity check against the old source: 0 mismatches over the 150-service seed | ADR-032, `docs/phases/phase-20.md` §10, `scripts/phase20_parity.py` |
| 4 | Ran failure experiments with pass rules written before each run: 4/4 passed, first OK answer <= 9.4 s after the fault ended; a DB outage went from 61 dropped connections to 0 (503 + Retry-After) | ADR-040, ADR-041, `docs/phases/phase-28.md`, `data/chaos/results/` |
| 5 | Found a per-step timeout bug from traces (a "2 s" call waited up to 4.0 s); a total deadline + warm-up cut the first alert from 4.8-23.7 s to 1.7-2.0 s | ADR-044 amendment, `docs/phases/phase-29.md` §8, `tests/unit/test_alert_budget.py` |
| 6 | Led an architecture review with independent reviewers: removed 2 never-built services, fixed an audit-partition cliff (writes would fail from 2027-01), closed 2 data-exposure paths | ADR-048, `docs/phases/phase-30.md` §3 |
| 7 | Least privilege end to end: ACL inside the retrieval SQL (leakage test = 0), default-deny NetworkPolicy (122 passed / 0 failed), PII scrub before hosted models, signed alert webhook | ADR-006, ADR-034, ADR-038, ADR-044, `tests/integration/rag/test_rag.py:93`, `docs/phases/phase-29.md:88` |

Notes per bullet (what an interviewer will push on):
- 1: "identified" = top hypothesis is the planted cause AND VALIDATED; 2 more were right but not
  validated. No human comparison (S4).
- 2: real models changed no answer vs fake (15/16 both). VALIDATED after rule R1: not measured.
- 4: all 4 investigations ended PARTIAL; the cause was the fake models, not the fault
  (phase-28.md). Do not say "investigations survived intact".
- 7: names/addresses are not scrubbed (no NER). The older netpol count was 130 passed (Phase 23c);
  quote the latest run.

## DO NOT CLAIM
- Any capacity number (one laptop, 1 replica; reads bend at 10-25 rps; identical runs differed ~10x).
- "Faster than a human" (no valid baseline, S4).
- Accuracy on real incidents (planted = upper bound; no labelled real incidents exist).
- Exactly-once, autoscaling, multi-region, an LLM judge, a copilot, kustomize.
- "11 services" (9 run).
