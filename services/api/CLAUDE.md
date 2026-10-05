# api (Python 3.12 / FastAPI) — local port 8000

**Purpose:** API Gateway / BFF: edge authN (JWT), coarse RBAC, rate limiting, request routing, OpenAPI aggregation for the frontend.

**Owns schema:** none (stateless; Redis for rate limits + idempotency keys)
**Events:** consumes nothing; publishes nothing
**Must never:** Contain business logic. Re-implement what Traefik/Envoy does (TLS, LB) — those stay in infra.
**Why this is a separate service:** Single edge for auth and rate limiting; in production this would be a managed gateway + thin BFF.

Status: Phase 1 — design only. Scaffold with the `new-service` skill in its phase (see docs/roadmap.md).
