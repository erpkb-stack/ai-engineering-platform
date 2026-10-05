---
paths:
  - "services/service-catalog/**"
---
# Java / Spring Boot conventions (service-catalog)

- Java 21 (records, sealed interfaces, virtual threads where IO-bound), Spring Boot 3.x, Maven wrapper.
- Layers: `api` (controllers + DTO records) → `domain` (services) → `persistence` (Spring Data JPA) → `messaging` (Kafka).
- Flyway owns this service's schema (`catalog` schema). It never writes to Python-owned tables.
- Publish domain events via transactional outbox; consumers idempotent on `eventId`.
- OpenAPI via springdoc; the Python services consume the generated spec, not hand-written clients.
- Tests: JUnit 5 + Testcontainers (PostgreSQL, Kafka). Spotless for formatting.
