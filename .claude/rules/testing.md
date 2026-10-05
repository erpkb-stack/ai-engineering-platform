---
paths:
  - "tests/**"
  - "services/*/tests/**"
---
# Testing rules

- Pyramid: unit (no IO) → integration (the running compose Postgres/Redis/Kafka; each session creates and drops its own `aeoi_test_<random>` database — see `tests/integration/conftest.py`) → e2e (compose `full` profile).
- Integration tests are marked `@pytest.mark.integration` and run with `make test-integration` (needs `make up`).
- LLM calls in unit/integration tests use the `FakeLLMProvider` with recorded fixtures. Real-model runs live in `tests/evaluation/` and are opt-in (`-m eval`).
- Every bug fix ships with a regression test that failed before the fix.
- Security suite must include: RBAC denial per role, permission-aware retrieval leakage test, direct + indirect prompt-injection fixtures, malicious tool response, exfiltration attempt.
- Failure-recovery tests: kill a worker mid-investigation and assert resume from checkpoint; duplicate Kafka delivery is a no-op.
- Never assert on exact LLM prose; assert on structure, evidence ids and invariants.
