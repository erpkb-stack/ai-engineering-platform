# service-catalog (Java 21 / Spring Boot 3) — local port 8010

**Purpose:** read-only system of record for services and their dependencies (Phase 20, ADR-005,
ADR-032). One endpoint: `GET /v1/services?service_key=|team=|name_contains=` (the
`query_service_catalog` tool's exact rules). Contract: `openapi.yaml` (hand-written, served at
`/v1/openapi.yaml`).
**Owns schema:** `catalog` (Flyway, `src/main/resources/db/migration`): services, dependencies, role `svc_catalog` (SELECT only).
**Callers:** tool-gateway only, with its OWN service token (scope `catalog:read`, `secrets/tools_catalog_token.txt`). User tokens and delegated tokens are refused.
**Must never:** accept writes over HTTP; hold DDL credentials while serving; write any non-`catalog` schema.
**Why separate & why Java:** the integration story - AI services using an existing Java system of record through a typed contract (ADR-005). In a real company this service already exists.

## Code map (`src/main/java/com/northwind/aeoi/catalog/`)
- `api/` controller + records (`ServiceItem`, `ServiceList`) · `persistence/CatalogRepository` 3 SQL queries, `COLLATE "C"` ordering
- `security/SecurityConfig` RS256 + iss/aud/exp/iat + service principal + scope; delegated = 401
- `importer/CatalogImporter` DEV ONLY: `--catalog.import-file=` loads catalog.json in ONE transaction, then exits

## Commands
`make catalog-build` (mvn verify; needs `make up` - tests use the compose Postgres) · `make catalog-import` (Flyway + load, as owner) ·
`make db-users` (creates catalog_svc after the first import) · `make run-catalog` · `make stop-catalog` ·
`make catalog-smoke` (incl. the JSON-vs-Java parity check) · `make catalog-contract` (after ANY openapi.yaml change).
The gateway uses it only with `AEOI_TOOLS_CATALOG_URL=http://localhost:8010` (default: the JSON file).
Rules: `.claude/rules/java.md`.
