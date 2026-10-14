---
paths:
  - "services/service-catalog/**"
---
# Java / Spring Boot conventions (service-catalog)

- Java 21 (records, virtual threads), Spring Boot 3.x. Build with the system Maven 3.9 (`make catalog-build`); no wrapper (ADR-032).
- Layers: `api` (controllers + DTO records) → `persistence` (JdbcClient, plain SQL) → `security`. No JPA: three queries do not need an ORM.
- Flyway owns this service's schema (`catalog` schema). It never writes to Python-owned tables. Migrations run only from `make catalog-import` (owner credentials); the serving login is SELECT-only.
- CONTRACT FIRST (ADR-032): `services/service-catalog/openapi.yaml` is hand-written and is the contract. Change it first, then `make catalog-contract` (Python models), then the Java code. `CatalogContractTest` fails when a response drifts from the file.
- Order text keys `COLLATE "C"`: the JSON source sorts in Python's code-point order.
- Tests: JUnit 5 against the compose Postgres (`make up`) in a throwaway `aeoi_catalog_test_<random>` database, like the Python integration tests. No Testcontainers: its Docker API version broke on newer Docker engines (ADR-032). Every rule the tool relies on has a test.
- No outbox/Kafka until something consumes catalog changes (ADR-032).
