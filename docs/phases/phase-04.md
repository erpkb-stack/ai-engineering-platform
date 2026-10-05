# Phase 4 — FastAPI backend (api gateway + incident-service)

## 0. Gate and scope challenge
- **Phase 3 PR:** merge it first (CI green), then branch Phase 4 from `main`. Your fingerprint `497466df4a89f163` matched mine, so the seed data is deterministic on macOS and Linux.
- **Correction to my own architecture (C12):** I planned an "in-process event bus until Phase 18". That cannot work: the api, incident-service and orchestrator are **separate processes**, and an in-process bus cannot connect them. The fix is the **transactional outbox**, which acts as the bus:
  - A service writes its events to its outbox table in the same transaction as the data.
  - A relay publishes them through a `Publisher` interface: `log` by default (no broker needed), or `kafka`.
  - Phase 18 now only adds consumers, DLQ and replay. architecture.md C12 and the roadmap are updated.
- **Scope cut: only real endpoints.** The spec lists 13 endpoints. Phase 4 builds the 8 that belong to incidents. Documents/search (Phase 6), agents/trace (Phase 9), approvals (Phase 16) and metrics (Phase 23) come with the phases that own them. Stubs that return 501 would make the OpenAPI contract lie.
- **One RBAC fix:** in 0012 only INCIDENT_COMMANDER had `incidents:write`, so an on-call engineer could not even *declare* an incident. 0012 is merged and therefore immutable, so migration **0013** adds a separate `incidents:create` permission for ENGINEER, SRE and IC. Declaring and managing an incident are different risks, so they get different permissions.

## 1. Objective
Run the first two services for real, with the production patterns that are hard to retrofit later:
- JWT authentication and RBAC checked at two layers
- idempotent POSTs
- optimistic locking
- keyset pagination
- problem+json errors with correlation ids
- a transactional outbox with a relay
- a least-privilege database login per service

## 2. Business reason
Incident creation is the entry point of every KPI (MTTI starts at `IncidentCreated`). Alert webhooks and on-call engineers both retry when a network blips, so duplicate incidents would corrupt MTTI and page people twice. Idempotency and the outbox are therefore **correctness requirements**, not polish.

## 3. Architecture
```
client ──JWT──► api :8000 (aeoi_api)                           incident-service :8001 (aeoi_incident)
               1 verify JWT (RS256, pinned alg)  ──httpx──►    1 verify JWT AGAIN (defence in depth)
               2 rate limit (token bucket/user)   allow-list   2 require(Perm.X) AGAIN
               3 require(Perm.X)                  headers only 3 one transaction:
               4 validate body (Pydantic)                         incident + incident_events + outbox
               5 forward; GET retried once                        (+ idempotency_keys row)
               timeouts → 503 problem+json                     4 OutboxRelay (SKIP LOCKED) ──► Publisher
                                                                    log (default) | kafka (PROFILE=kafka)
                                     Postgres: login incident_svc ∈ svc_incident (schema incident ONLY)
```
**[P]** both services run with `uv run uvicorn` on your Mac. **[Prod]** a managed gateway (ALB/API Gateway) in front of the BFF; JWKS from the IdP instead of a PEM file; Redis-backed rate limits; the relay as its own deployment or CDC.

## 4. Endpoints
| Spec endpoint | Phase 4 | Permission | Notes |
|---|---|---|---|
| `POST /api/v1/incidents` | ✅ | incidents:create | **Idempotency-Key required** (428 without); 201 + Location + ETag |
| `GET /api/v1/incidents` | ✅ (new) | incidents:read | keyset pagination `?limit&cursor`, filters `status,severity,service` |
| `GET /api/v1/incidents/{id or INC-n}` | ✅ | incidents:read | ETag = version |
| `PATCH /api/v1/incidents/{id}` | ✅ (new) | incidents:write | **If-Match required** (428), stale → 412, invalid transition → 409 |
| `POST /api/v1/incidents/{id}/investigate` | ✅ | investigations:run | 202 + `request_id`; outbox `InvestigationRequested` (orchestrator consumes it in Phase 9) |
| `GET /api/v1/incidents/{id}/timeline` | ✅ | incidents:read | from `incident_events` |
| `GET /api/v1/incidents/{id}/evidence` | ✅ | incidents:read | empty until agents write evidence (Phase 8+) |
| `POST /api/v1/feedback` | ✅ | feedback:write | Idempotency-Key optional |
| `GET /api/v1/health`, `/health/live`, `/health/ready` | ✅ | public | live never checks dependencies; ready does |
| `GET /api/v1/me` | ✅ (new) | any user | roles, groups, permissions from the token |
| `GET …/agents`, `GET …/trace` | Phase 9 | | |
| `POST /api/v1/documents`, `POST /api/v1/search` | Phase 6 | | |
| `POST /api/v1/approvals` | Phase 16 | | |
| `GET /api/v1/metrics` | Phase 23 | | |

## 5. Files
```
libs/web/ (new)               create_app, CorrelationIdMiddleware (+500 error boundary), problem+json, auth deps, health
libs/security/                auth.py (verify/issue JWT, Principal), rbac.py (matrix as code), testing.py (keys, tokens)
libs/common/errors.py         401/400/412/428/429/503 error types
libs/models/api/              Page[T], incident DTOs, event payloads; EventType + IncidentUpdated/InvestigationRequested
libs/observability/logs.py    FIX: tracebacks no longer include frame locals (secrets leak)
libs/db/                      IdempotencyKey model, migration 0013, service_database_url(), users.py (make db-users)
services/incident-service/    pyproject, src/aeoi_incident/{config,db,domain,idempotency,service,events,api,main}.py, tests/
services/api/                 pyproject, src/aeoi_api/{config,ratelimit,proxy,routes,main,devtoken}.py, tests/
tests/integration/services/   conftest, test_incident_api, test_outbox, test_end_to_end, test_kafka_publisher
tests/integration/security/   test_rbac_matches_db
scripts/smoke-phase4.sh       Makefile: db-users, token, run-incident, run-api, dev, smoke; setup now creates JWT keys
```

## 6. Commands (macOS / zsh)
```zsh
cd ~/projects/ai-engineering-platform
git switch main && git pull                      # after merging the Phase 3 PR
git switch -c phase-4-backend
uv sync --all-packages
make setup                                       # creates secrets/jwt_private.pem + jwt_public.pem (only if missing)
make up && make db-upgrade                       # applies 0013
make db-users                                    # creates LOGIN user incident_svc (password in secrets/)
make check && make test-integration              # expect: 110 unit passed; 71 integration passed, 1 skipped (Kafka)
make dev                                         # terminal 1: api :8000 + incident-service :8001
make smoke                                       # terminal 2: expect 12 passed, 0 failed
open http://localhost:8000/docs                  # OpenAPI UI (authorize with: make -s token ROLE=SRE)
```
**Try it by hand:**
```zsh
export TOKEN=$(make -s token ROLE=SRE)
curl -s -X POST localhost:8000/api/v1/incidents -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -H "Idempotency-Key: demo-$(date +%s)" \
  -d '{"title":"HTTP 500 errors increased 42% after deployment","severity":"SEV2","affected_services":["checkout-api"]}'
```
**With Kafka:** `make up PROFILE=kafka`, then run the services with `AEOI_INCIDENT_EVENT_TRANSPORT=kafka make dev`, and run `AEOI_TEST_KAFKA=1 make test-integration`.

## 8–10. Tests and what was verified
| Layer | Count | Highlights |
|---|---|---|
| Unit | 110 (+50) | JWT: expired, wrong aud/iss, foreign key, `alg=none`, HS256 key confusion, tampered payload. Web: correlation id, problem+json, no input echo, no 500 leak. Domain: transitions, cursor tampering, If-Match. Gateway: 401/403/422 never reach upstream, header allow-list, Location rewrite, GET retry only, 503 on timeout, rate limit |
| Integration | 71 (+34) | idempotency replay / mismatch / per-user scope / **6 concurrent duplicates → 1 incident** (5 lost the race and replayed the winner, confirmed in logs); keyset pagination with no gaps or duplicates; If-Match 428/412/200; transitions 409; outbox written in the same tx; relay ordering, failure + retry, **two relays never publish the same event**; e2e gateway → service → DB; the service login cannot read `identity`; RBAC code == DB |
| Kafka publisher | 1 | **skipped here** (no broker in my sandbox). It runs in CI (`AEOI_TEST_KAFKA=1`), so CI is the first real proof |
| Real processes | smoke 12/12 | both services under uvicorn, real RS256 tokens for seeded users, least-privilege DB login; 3 events relayed; logs checked for tokens/passwords (none) |

**Phase 4 is done when:** `make smoke` prints 12/12 on your Mac, and the PR's CI job (including the Kafka publisher test) is green.

## 11. Failure scenarios
| Symptom | Cause | Fix |
|---|---|---|
| `secrets/jwt_public.pem not found` at startup | `make setup` not re-run after Phase 4 | `make setup` (it only creates missing files) |
| `incident_svc_password.txt not found` / auth failed | login user not created | `make db-users` |
| smoke: "token minting" fails | no seeded users | `make db-seed` |
| `permission denied for schema incident` | migrations ran after the login was created without grants | `make db-users` again (idempotent); check `make db-current` = 0013 |
| 503 from the gateway | incident-service down or slow (>5 s) | `make run-incident`; `curl localhost:8001/health/ready` |
| 429 | more than 30 requests in a burst / 120 per minute per user | tune `AEOI_API_RATE_LIMIT_*`; Redis in Phase 19 |
| events stay unpublished (`published_at IS NULL`) | Kafka transport selected but broker down | the relay backs off up to 30 s and retries; nothing is lost |

## 12. Production considerations
- **Security fix found by the tests:** structlog's default traceback renderer logged **every frame's local variables**, which can include tokens and request bodies. It is now disabled (`show_locals=False`), with a regression test. Mention this in interviews: it's a real class of incident.
- **Token keys:** RS256 with a pinned algorithm. In production, verify with the IdP's JWKS (key rotation via `kid`). Never mint tokens in the app.
- **Authorization twice:** the gateway rejects early (cheap, protects services); the service re-checks (a direct or misrouted call can't bypass it).
- **Idempotency in the DB, not only Redis:** the stored response commits atomically with the incident. Redis can cache replays later.
- **Outbox ordering:** one relay keeps per-incident order. With N relays, use leader election (advisory lock) or CDC.
- **Rate limits** are per replica until Redis (Phase 19). Say so if asked; it's the honest answer.

## Known limitations
1. The Kafka publisher has not run against a broker yet. CI is its first real test.
2. `GET /incidents` has no full-text search; filters only.
3. No audit events yet (audit service: Phase 7 minimal, Phase 26 full). The timeline is the record for now.

## 13–15. Interview prep
See [`../interview/phase-04-backend.md`](../interview/phase-04-backend.md).
