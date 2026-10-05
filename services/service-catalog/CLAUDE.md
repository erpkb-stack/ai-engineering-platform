# service-catalog (Java 21 / Spring Boot 3) — local port 8010

**Purpose:** Source of truth for services, owners/teams, repositories, APIs, databases, pipelines, dashboards, runbook links and dependency edges.
**Owns schema:** `catalog` (Flyway migrations).
**Events:** publishes CatalogChanged (outbox → Kafka).
**Consumers:** knowledge-service, tool-gateway (`query_service_catalog`), frontend via api.
**Must never:** accept writes from agents. Write to any non-`catalog` schema.
**Why separate & why Java:** demonstrates integrating AI services with an existing enterprise Java system through REST + Kafka (ADR-005). In a real company this service already exists and AEOI integrates with it.

Build: `./mvnw verify` (Phase 20). Rules: `.claude/rules/java.md`.
