# ADR-038: Security hardening = a threat model that is a TESTED index; PII scrubbed at the hosted-LLM boundary; no NER

Status: Proposed
Date: 2026-10-26
Refines: architecture.md §15-16, ADR-014 (LLM gateway), ADR-021/022 (model text)

## Context
- Phase 26's roadmap line: threat model; injection/exfiltration suites; PII; secrets scan;
  dependency scan.
- Need check (read in the code first): about 120 security tests already exist, spread over
  every service; `tests/security/` is empty. gitleaks (CI + pre-commit) and Trivy (both images,
  gated) exist. There was NO threat model, and no way to answer "is X covered?" without
  searching the repo.
- One suspected gap, tested before it was called one: text a USER types (an incident title)
  reached the postmortem prompt for Sonnet unscrubbed. Tool outputs, documents and traces were
  scrubbed at their own boundaries; the LLM gateway redacted secrets only. The new test failed.

## Decision
- **`docs/security/threat-model.md`:** each threat -> its control -> the existing tests that
  prove it -> the gap, if any. `tests/unit/test_threat_model.py` checks that every named test
  EXISTS and that every row has a test or says "gap". On its first run it caught two test
  names I had written from memory.
- **No move of tests into `tests/security/`:** the tests live next to the code they protect;
  the model is the index. Moving 120 tests buys tidiness, not protection.
- **PII at the hosted-LLM boundary:** the LLM gateway now scrubs PII (after secrets) before
  every HOSTED provider (chat and embeddings), counted in `redactions`. Every prompt passes
  there, so it covers inputs no other boundary sees. Local providers still get the original.
- **No NER model** for names and free-text addresses: a large dependency on a CPU-only Intel
  Mac, run on every prompt, and it would need tuning not to flag service and team names on
  this data. Recorded as an accepted gap (T-PII-3), and `aeoi_security.pii` says so.
- **No `npm audit`:** the console is not in any image; nothing from it ships. Scan it when it
  ships.

## Tradeoffs
- The gateway scrub is pattern-based: a 13-19 digit number that passes Luhn becomes `[CARD]`
  in a prompt. Identifiers in prompts are hex UUIDs and short keys; the existing tests that
  check operational numbers are not scrubbed still pass.
- An index can list a test that exists but no longer tests the threat (a renamed assertion).
  The check proves existence, not meaning; the model is reviewed when a control changes.

## Prototype / Production / Enterprise-scale
- Prototype (this): patterns at 4 boundaries, a tested index, image scans in CI.
- Production: + NER and DLP on egress, a secrets manager with rotation, mTLS, SBOMs.
- Enterprise: threat model owned per team, reviewed per release, findings tracked to closure.
