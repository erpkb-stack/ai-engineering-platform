# ADR-005: Java 21 / Spring Boot for the Service Catalog

Status: Proposed
Date: 2026-10-02
Supersedes: —

## Context
Real enterprises already have Java systems of record. The platform must integrate, not replace.

## Decision
Service Catalog in Java 21 + Spring Boot 3 + Flyway + Kafka outbox; Python consumes via generated OpenAPI client and CatalogChanged events.

## Alternatives
| Option | Why not (here) |
|---|---|
| Python like the rest | One toolchain; loses the integration story. |
| Backstage | Real-world answer for catalogs; too large to run here, but mention it in interviews. |

## Tradeoffs
Against: second build system, JVM memory, slower iteration. Mitigation: one service only, container limits, Testcontainers.

## Consequences
Polyglot CI; contract tests between Java producer and Python consumers.

## Prototype vs Production vs Enterprise-scale
[P] JVM 512 MB. [Prod] 2+ replicas. [Ent] it's an existing platform (e.g. Backstage) and AEOI only integrates.

## When we would revisit
If the integration story is shown elsewhere, fold the catalog into Python.
