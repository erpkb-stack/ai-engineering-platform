# api (Python 3.12 / FastAPI) — port 8000 — package `aeoi_api`

**Purpose:** the only public entry point (BFF): JWT authentication, rate limiting, coarse RBAC,
request validation, routing to internal services. Stateless for requests — identity comes
from the token. **Phase 9:** also the token exchange (STS, `sts.py`, ADR-019) under
`/internal/v1/delegations` (service tokens with `delegation:create` only; not for the edge LB).
**Owns schema:** `identity` (login `api_svc`: grants/events + reading users/roles; `devtoken.py` reads it in dev).
**Must never:** contain business logic; forward arbitrary client headers; retry non-GET requests;
expose a route that isn't implemented (OpenAPI must not lie).
**Why a separate service:** one place for edge concerns. TLS/LB stay in infrastructure (ingress).

## Code map
- `routes.py` typed `/api/v1` routes (OpenAPI contract for the frontend) → `guard(Perm)` → `Upstream.forward`
- `proxy.py` header allow-list, Location rewrite, GET-only retry, timeouts → 503 problem
- `ratelimit.py` token bucket per user (in-memory now; Redis in Phase 19)
- `devtoken.py` DEV ONLY token minting (`make token ROLE=SRE`); refuses in production
- `sts.py` token exchange: create (user token → grant, roles from the DIRECTORY), refresh (actor only, re-checks the user), revoke; every decision is a `delegation_events` row

Order on every route: 401 → 429 → 403 → 422 → forward. Unauthorised requests never reach a service.
