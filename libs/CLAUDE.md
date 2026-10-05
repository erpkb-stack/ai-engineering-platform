# libs (shared Python packages)

- `common` — config base, errors (problem+json), retry/circuit-breaker helpers, idempotency
- `models` — Pydantic contracts: API DTOs, Kafka event envelopes, Fact/Hypothesis/Recommendation/Evidence
- `security` — JWT verification, RBAC permission map, PII scrubber, untrusted-data wrapper
- `observability` — structlog JSON logger, OTel setup, standard metrics

Rule: libs contain NO service business logic. A breaking change to `models` requires a version bump and an ADR if it changes an event schema.
