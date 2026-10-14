# Phase 20 - Java service catalog (lean, contract-first; ADR-005, ADR-032)

## 0. Gate and scope challenge
- **Gate: passed.** Phase 19 closed and merged (PR #22, main ce44d20).
- **Challenge - the system does not need it.** The catalog was a JSON file read by one tool
  (`query_service_catalog`, blast radius in release risk). ADR-005's reason is the integration
  story, not a system need. Owner decision (2026-10-14): build it LEAN - one read endpoint, no
  outbox (nothing consumes CatalogChanged), the JSON file stays the default source.
- **New fact:** the build sandbox cannot build Java (Maven Central blocked). The owner chose to
  continue: Java is compiled and tested first on the Mac (`make catalog-build`).

## 1-2. Objective and reason
The `query_service_catalog` tool can be served by a Java system of record through a typed
contract, with the SAME answers as before - proven, not assumed.

## 3. Architecture
```
agents -> tool-gateway --(AEOI_TOOLS_CATALOG_URL set)--> service-catalog (Java) -> catalog.* (Flyway)
              |  own token, catalog:read                  GET /v1/services
              +--(default)--> data/generated/catalog.json
contract: services/service-catalog/openapi.yaml  ->  generated Python models (catalog_contract.py)
```
| Guarantee | Enforced by | Test |
|---|---|---|
| One contract, no drift | hand-written openapi.yaml; generated models | unit: models == fresh generation, and the check sees a change; Java: live fields == YAML fields, served file == committed file |
| Same answers as the JSON source | same rules; `COLLATE "C"` ordering | Java: rules incl. byte order; Python: fake-catalog parity; Mac smoke: REAL adapters over every key and team |
| Only the gateway reads | RS256 + iss/aud/exp/iat + service principal + scope | Java: 401/403 matrix (no token, other key, issuer, audience, no exp, no iat, delegated x2, user, empty service, wrong scope) |
| Read-only | SELECT-only role; DDL only in the import command | Java: INSERT/UPDATE/DELETE as the serving role = 42501 |
| A bad import changes nothing | one transaction; FKs | Java: unknown dependency -> rollback |
| Contract breaks are loud | `extra="forbid"` models; 4xx/5xx classified | Python unit: 7 failure cases with the REASON pinned |
| `db-users` before Flyway does not crash | optional groups skipped, others still fail | integration (real roles, temp secrets root) |

## 4. Files
```
services/service-catalog: openapi.yaml, pom.xml, application.yml, V1__catalog.sql,
  api/ (controller, records), persistence/CatalogRepository, security/SecurityConfig,
  importer/CatalogImporter, 4 test classes (compose Postgres, throwaway database)
services/tool-gateway: adapters/catalog_http.py, adapters/catalog_contract.py (GENERATED),
  registry.py (CatalogSource protocol), config.py, main.py, adapters/catalog.py (dead known() removed)
scripts: catalog_contract.py, phase20_parity.py, smoke-phase20.sh
libs/db users.py (catalog_svc, optional groups) · Makefile (catalog-*, tools-tokens) · pyproject/uv.lock (datamodel-code-generator, dev)
tests: tool-gateway test_catalog_http.py, tests/unit/test_catalog_contract.py, tests/integration/db/test_db_users.py
docs: ADR-032 (new), ADR-005 note, java.md, CLAUDE.md files, roadmap, this file
```

## 5-6. Commands (macOS, zsh)
```zsh
make db-seed                   # writes data/generated/catalog.json (if not there yet)
make up                        # the compose Postgres (the Java tests use it, like the Python ones)
make catalog-build             # mvn verify: compiles + all Java tests
make catalog-import            # Flyway V1 + load the JSON, as the schema owner; exits
make db-users                  # now also creates catalog_svc (SELECT only)
make tools-tokens              # adds secrets/tools_catalog_token.txt (catalog:read)
make run-catalog               # :8010
make catalog-smoke             # contract, auth matrix, parity, the tool via the gateway
# to switch the gateway: AEOI_TOOLS_CATALOG_URL=http://localhost:8010 in .env, restart run-tools
```

## 8. Tests (sandbox)
- Python: tool-gateway adapter 13, contract 3, db-users integration 2. Mutations 6/6 caught
  (one first MISSED: a 403 still failed as "contract broken" - the test now pins the reason).
- Java: **not compiled here.** `javac` without libraries reports only missing-symbol errors
  (syntax clean). Everything else is verified on the Mac.

## 9. Review (independent, of code nobody could compile)
| Id | Finding | Fix |
|---|---|---|
| H1 | Testcontainers 1.20.x (managed by Boot 3.4.4) vs Docker Engine 29's minimum API: every Java test class would fail (unverified) | Testcontainers dropped: the compose Postgres + a throwaway database, like the Python tests |
| M1 | `denyAll()` also blocked Spring's /error forward: a DB failure became a 403 ("refused") | error dispatch permitted |
| L1 | grants on ALL TABLES included Flyway's history table | grants by table name (tested: 42501) |
| L2 | 60 s clock skew (Python 30 s); a future `iat` accepted | 30 s leeway; future `iat` = 401 (tested) |
| L3 | `team=""`: the JSON source matched nothing, the HTTP source dropped the filter | the tool rejects an empty team (tested) |
| L5/L7 | unquoted repo path in the Makefile; temp files left by tests | quoted; deleteOnExit |
| - | the grants test left a known-password login on the shared cluster | random name + password, always dropped |
| open | L4 non-ASCII length/lowercase differences; L6 Python sorts a missing tier as 9 | seed is ASCII and always has a tier: no parity impact; recorded |

## 9b. Mac findings
- The Java service built, migrated, imported and served on the FIRST run (no compile round).
  Measured: JVM resident memory 197 MB with -Xmx256m (`make catalog-smoke`, 2026-10-14).
- fix1: the parity checker crashed (`sum()` over an `await` in a generator = async generator).
  It had never run: it needs the live services. Now `compare()` is unit-tested (equal sources ->
  "same"; a shifted source -> every difference counted) and the script was run end to end in
  the sandbox against a fake catalog over the real 150-service seed (0 mismatches).

- fix2: with `AEOI_TOOLS_CATALOG_URL` in `.env`, `make test-integration` failed 2 tests
  (`test_catalog`, release risk coverage `partial`): the Makefile exports `.env`, so the tests'
  gateway used the HTTP catalog, and the tests' egress allow-list blocked it. A root
  `conftest.py` removes dev-only switches before any fixture (17b-fix1 was the same class of
  bug). Reproduced in the sandbox with the variable set (2 failed), then 2 passed; full runs WITH
  it set: make check 871 passed, integration 262 passed.

## 10. Verify (gate for Phase 21) - passed on the Mac, 2026-10-14 (after fix1 + fix2)
- [x] `make check` green (incl. the contract drift test)
- [x] `make catalog-build` green: all 21 Java tests (API 9, security 7, contract 2, import/grants 3)
  against the compose Postgres; the Java service compiled on the FIRST build
- [x] `make catalog-import` + `make db-users` + `make run-catalog`; `make catalog-smoke` 6/6,
  parity 0 mismatches, gateway source http://localhost:8010
- [x] with `AEOI_TOOLS_CATALOG_URL` set + run-tools restarted: `make risk-smoke` 19/19 (unchanged)
- [x] `make test-integration` 262 passed (switch ON in .env); `make tools-smoke` 16/16
- Seen by accident: with the catalog DOWN, release risk stayed honest - DEPLOY-4821 HIGH but
  PARTIAL ("blast radius (service catalog): circuit_open"), DEPLOY-1186 raised by `not_checked`.
  Nothing became LOW because a source was missing (ADR-025).
- Open: Maven ran on JDK 25 (Boot 3.4 supports up to 24). `make catalog-build` now selects JDK 21
  on macOS; re-run once to confirm.

## 11. Failure scenarios
| Symptom | Cause | Fix |
|---|---|---|
| `make catalog-build`: "is the compose Postgres up?" | Postgres not running, or AEOI_PG_PORT differs | `make up`; check `.env` |
| `run-catalog`: password authentication failed for catalog_svc | `make db-users` ran before the first import | `make catalog-import`, then `make db-users` |
| tool: "service-catalog refused the request (403)" | old tools token without catalog:read | `make tools-tokens`, restart run-tools |
| tool: "broke its contract" | jar built from another openapi.yaml | `make catalog-contract`, `make catalog-build` |
| parity MISMATCH | a rule differs (sort, filter) | the smoke prints the query and both answers |
