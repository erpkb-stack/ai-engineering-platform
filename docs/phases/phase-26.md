# Phase 26 - Security hardening (ADR-038)

Status: **BUILT**, Verify = its PR's checks (owner: "GO" on the scope below, 2026-10-26).

## 0. Gate
- Phase 25 closed (PR #40, main e617ba1); this PR's chain checks that `main` run is green.

## 1. Need check (read in the code first)
| roadmap item | found |
|---|---|
| threat model | **missing** |
| injection suites (direct + indirect) | exist, spread over agents, tool gateway, rag |
| exfiltration | egress allow-list (checked before connecting); console renders model text as text; postmortem drops link/markup sentences |
| PII | at ingest, tool gateway output, traces - not at the LLM gateway |
| secrets scan | gitleaks in CI + pre-commit |
| dependency scan | Trivy on both images, gated (what ships) |
About 120 security-related tests; `tests/security/` empty.

## 2. What Phase 26 adds
- `docs/security/threat-model.md`: 16 threats, each -> control -> the existing tests -> gap.
- `tests/unit/test_threat_model.py`: every named test must exist; every row has a test or
  says "gap". **First run: it caught 2 test names I had written from memory** - the reason it
  exists.
- **One real gap, found by a failing test first:** an email in an incident title reached the
  hosted model (the postmortem prompt). The LLM gateway now scrubs PII after secrets before
  every hosted provider (chat + embeddings); local providers get the original text.
- Decisions (ADR-038): no NER model (names/addresses = accepted gap), no npm audit (the console
  ships nowhere), tests are NOT moved into `tests/security/` (the model is the index).

## 3. Checks before packaging
- `make check`; the integration suite in the sandbox (Kafka tests skip) - added to my checks
  after 25e, where an integration test caught what unit tests did not.

## 4. Verify
- [ ] PR: all checks green.
