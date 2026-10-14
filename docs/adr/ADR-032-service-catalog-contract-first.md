# ADR-032: The service catalog is a small, read-only Java service with a hand-written contract

Status: Proposed
Date: 2026-10-14
Refines: ADR-005 (Java 21 / Spring Boot for the catalog), ADR-013 (the `catalog` schema is Flyway's), `.claude/rules/java.md`

## Context
ADR-005 chose a Java service for the catalog for the integration story (AI services working
with an existing Java system of record), not for a system need: until now a JSON file
(`data/generated/catalog.json`) served the one tool that reads it (`query_service_catalog`,
used for blast radius by release risk). Owner decision (2026-10-14): build a LEAN Java service.

Two facts shaped it:
- **The build sandbox cannot build Java** (Maven Central is blocked by its network policy), so
  every Java line is compiled and tested first on the owner's Mac.
- **Nothing consumes `CatalogChanged`**: knowledge-service is a stub, and the tool reads on demand.

## Decision
- **Contract first.** `services/service-catalog/openapi.yaml` is written by hand and is THE
  contract. Python client models are GENERATED from it (`make catalog-contract`,
  datamodel-code-generator, `extra="forbid"`). Drift fails three ways: the committed models vs a
  fresh generation (unit), live Java responses vs the YAML's field sets (Java
  `CatalogContractTest`), and the running jar serves `/v1/openapi.yaml` byte-equal to the file
  (smoke). This reverses java.md's "springdoc generates the spec": generation needs a running
  JVM, which the sandbox cannot provide, and design-first is the stronger contract anyway.
- **One endpoint:** `GET /v1/services` with the tool's exact rules (at least one filter;
  service_key wins; team AND name_contains; tier then key; max 200 + truncated; direct
  dependents). It replaces nothing the tool does not need.
- **Plain Spring:** Web + JdbcClient (3 SQL queries, no JPA) + Flyway + JWT resource server.
  Keys sort `COLLATE "C"`: the default `en_US` collation orders `-` differently from Python, and
  both sources must give the same answer.
- **Same identity rules as Python:** RS256, our issuer + audience, `exp`/`iat` required, a
  SERVICE principal with scope `catalog:read`; a delegated token is never a bearer (ADR-019);
  user tokens are refused. The gateway calls with its OWN token only (`tools_catalog_token.txt`)
  - the catalog is org-wide metadata, RBAC is checked at the gateway before the tool runs.
- **Read-only by grants.** Flyway (V1) creates the schema and the `svc_catalog` role with SELECT
  only. Migrations and the dev import run as the schema owner (`make catalog-import`); the
  serving process (`catalog_svc`) never holds DDL credentials. `make db-users` skips
  `catalog_svc` until Flyway created its role.
- **The JSON file stays the default source.** `AEOI_TOOLS_CATALOG_URL=http://localhost:8010`
  switches the gateway to the Java service. A parity check (smoke) runs the REAL adapters over
  every key and team and fails on any difference - so the switch is proven, not assumed, and a
  demo does not need a JVM (measured: 197 MB resident at -Xmx256m) unless it shows the
  Java path.
- **No outbox, no Kafka.** Nothing consumes `CatalogChanged`; an event nobody reads is the
  ADR-030 mistake. **No Testcontainers** (tests use the compose Postgres, see Tradeoffs). **No Spotless, no Maven wrapper:** both need downloads the sandbox cannot do;
  the owner's Maven (3.9) builds it.

## Alternatives
| Option | Why not (here) |
|---|---|
| springdoc-generated spec (java.md) | needs a running JVM to produce the spec the Python side generates from; the sandbox has none |
| Full ADR-005 (outbox -> Kafka CatalogChanged) | no consumer exists; 1-2 more days for an unread event |
| Fold the catalog into Python (ADR-005's revisit clause) | half a day, but no cross-language story - the reason the catalog exists here |
| Make the Java service the only source | every demo would need the JVM; the parity check gives the same confidence |

## Tradeoffs
- Two sources of the same answer. Accepted: the parity check fails on any difference, and the
  data is generated from one seed.
- Java bugs surface only on the Mac. Accepted: small surface (one endpoint, three queries), and
  every rule is pinned by a test that runs there (`make catalog-build`).
- Java tests need the compose Postgres running (`make up`), like the Python integration tests.
  Testcontainers was dropped before its first run: the version Boot 3.4.4 manages (1.20.x) uses
  a Docker API that Docker Engine 29 rejects (review finding; unverified on the Mac).

## Prototype vs Production vs Enterprise-scale
[P] one JVM (-Xmx256m), JSON default, dev importer. [Prod] the catalog has its own write path and
owners, migrations as a job, mTLS / workload identity instead of a file token. [Ent] the catalog
already exists (e.g. Backstage); AEOI only integrates through a contract like this one.

## When we would revisit
A second consumer of catalog changes appears (then the outbox + CatalogChanged), or the catalog
becomes the only source (then the JSON default goes).
