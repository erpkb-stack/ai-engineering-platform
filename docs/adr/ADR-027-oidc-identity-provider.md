# ADR-027: Every service trusts the OIDC identity provider (Keycloak); our directory stays the source of truth

Status: Proposed
Date: 2026-10-12
Refines: ADR-019 (delegated tokens: unchanged), the auth notes in architecture.md §15 ("[Prod] tokens from the corporate OIDC IdP, verified with its JWKS")

## Context
Until Phase 17, every token came from our own static issuer (`make token`, `aeoi-dev-issuer`,
`secrets/jwt_public.pem`). A browser UI needs people to sign in. Owner decisions (2026-10-12):
real OIDC with **Keycloak**, and **every service trusts Keycloak** (not only the edge) - knowing
the cost: auth changes in all 8 services and roles held by the IdP. Docker Desktop has 8 GB+.

Constraints that shaped the design:
1. The token exchange (ADR-019) loads the user from identity.users by `uid` AND
   `external_subject = token sub`, and takes roles from the DIRECTORY. A Keycloak `sub` is
   Keycloak's own id - it matches nothing in our directory.
2. ~1,000 tests, the smokes and the CLI use static-issuer tokens; service-to-service calls use
   static-issuer SERVICE tokens. Keycloak must not break them.
3. A token checker in 8 services must not make each of them call the IdP on every request.

## Decision
```
browser ──PKCE──► Keycloak (realm aeoi, GENERATED from identity.users)  ── access token ──►
  api, incident-service, orchestrator, agents, tool-gateway, rag, llm-gateway, audit
     Authenticator.verify(token): route by `iss`
        iss = AEOI_OIDC_ISSUER   -> OidcVerifier (JWKS, rules below)      USER tokens only
        iss = aeoi-dev-issuer    -> static key (unchanged)                 SERVICE tokens always;
                                                                           USER tokens unless
                                                                           AEOI_STATIC_USER_TOKENS=false
```
### A. The directory stays the source of truth
`make keycloak-realm` generates the realm from identity.users (+ roles, groups): one Keycloak
user per directory user, with two ADMIN-ONLY attributes mapped into the access token:
`uid` (identity.users.id) and `aeoi_sub` (external_subject). The Principal built from a Keycloak
token uses THOSE - so the token exchange, audit actors (`user:<uid>`), approvals' separation of
duties and every per-user record are unchanged. A Keycloak user without them is refused.
Roles live in two places, but one is generated from the other; [Prod] SCIM sync from the IdP.

**Review H1 - a generated copy goes stale.** Without a check, a user demoted or deactivated in
the directory kept their old Keycloak roles until someone regenerated the realm. So the api
(the only public entry point) runs a **directory check** on every IdP token: the user must
exist with this `uid` AND `aeoi_sub` and be active (else 401), and the token's roles must be a
SUBSET of the directory's (else 403; a promotion not yet in the realm only means fewer rights).
Cached 60 s per user; no directory = 503 (fail closed). Services BEHIND the api trust the token
itself, bounded by its 5-minute lifetime and by the network ([Prod] only the api is reachable
from outside). The token exchange (ADR-019) already loads the user from the directory.

### B. What a Keycloak token must be (each rule closes an attack; unit + real-IdP tests)
RS256 with a `kid` from the issuer's JWKS · exact `iss` · `aud` contains `aeoi-api` (an
audience mapper) · `typ = Bearer` (an ID token is never an access token) · `azp` in
`AEOI_OIDC_CLIENTS` (a token minted for another client is refused) · no `act`/`token_use`
(never delegated) · a user, never a service · roles filtered to OUR five (Keycloak's default
roles grant nothing). Users cannot edit `uid`/`aeoi_sub` (user profile: admin-only) - tested
against the real account API (400 read-only).

### C. Keys
Cached per `kid`; fetched only for an unknown `kid`, at most once a minute (5 s while no keys
are cached), 2 s timeout, 256 KB response cap. A fetch REPLACES the key set (a key the IdP
rotated out stops working) and keys older than 10 minutes are refetched. A sprayed unknown
`kid` cannot hammer the IdP; a down IdP keeps the last keys, and with none it is a 401, not a crash. [P] the fetch
is a blocking call (rare, bounded); [Prod] prefetch on startup.

### D. Two clients
`aeoi-console`: public, Authorization Code + PKCE (S256), the UI (17b). `aeoi-cli`: DEV ONLY,
password grant, so smokes and curl get tokens without a browser; services accept it only where
`AEOI_OIDC_CLIENTS` lists it (`.env.example` does; production must not).

### E. Production stance
`AEOI_ENV=production` refuses to start (every service, in `BaseServiceSettings`) unless
`AEOI_OIDC_ISSUER` is set, `AEOI_STATIC_USER_TOKENS=false`, `aeoi-cli` is not an allowed
client, and the issuer and JWKS URLs are https: people sign in through the IdP only.
`Authenticator(static_users=...)` has no default, so a service cannot forget the setting.

### F. Realm hygiene
Passwords are stored as PBKDF2-SHA512 hashes (the realm file holds no plaintext; dev passwords
are in `secrets/keycloak/users.txt`, mode 600). The realm's default role grants nothing (no
`offline_access`: no 30-day tokens). `uid`, `aeoi_sub` and names are admin-edit only. Service tokens stay on
the static issuer [P]; [Prod] client-credentials from the IdP.

## Alternatives
| Option | Why not (here) |
|---|---|
| Keycloak at the edge only (api swaps it for an internal token) | the recommended option - one service changes; the owner chose service-wide trust (closer to many real setups) |
| Roles from Keycloak claims as the authority | two sources of truth; the token exchange would need rewriting; a realm edit would silently change who may approve |
| Map Keycloak `sub` to the directory by a lookup on every request | a DB call per request in 8 services; the generated attributes carry the same fact |
| Dev user picker / pasted token | no real sign-in flow to show |

## Tradeoffs
- Keycloak 0.55-0.65 GB of Docker memory (measured on the Mac: 542 MiB after start, 652 MiB an
  hour later; limit 1 GiB, heap 512 MB; `make keycloak-down` frees it). Start: ~4 min on the Intel Mac (measured), because `start-dev` rebuilds on every
  fresh container. [Prod] a pre-built (`kc.sh build`) image starts with `--optimized`.
- Realm and directory can drift if a user is added in one place only: re-run
  `make keycloak-up` after `make db-seed` (it re-imports a fresh realm). [Prod] SCIM.
- The explicit `alg` header check is shadowed by `jwt.decode(algorithms=["RS256"])`: removing
  either alone is caught by nothing (mutation-checked, by design).
- `start-dev` (H2, HTTP) - [Prod] a production Keycloak on Postgres behind TLS.
- A DB lookup per IdP request at the api (cached 60 s): a demotion takes up to 60 s at the
  edge and up to 5 minutes (token lifetime) behind it.
- `populate_by_name=True` means each setting can now also be set by its field name (e.g.
  `ENVIRONMENT` as well as `AEOI_ENV`): more names to know.
- One audience (`aeoi-api`) for all services: a token for one service is valid at every service.
  Per-service audiences need token exchange per hop - [Ent].
- Port 8090 on the host (8080 is often taken on a dev Mac); bound to 127.0.0.1.

## Review (architecture-critic, before shipping 17a)
H1 stale realm roles -> directory check at the edge (above). M1/M2 production could start with
static user tokens or the dev client -> validator in every service. Key rotation kept old keys
-> replace, max age. Offline tokens and plaintext realm passwords -> removed. Self-editable
names -> admin-only. Each fix has a test; the guards were mutation-checked.

## Found while building
`Settings(environment="test")` was silently IGNORED in every test (aliased field + extra="ignore");
`populate_by_name=True` fixes it, with a regression test. Found because an OIDC test setting did
not apply.

## Prototype vs Production vs Enterprise-scale
[P] Keycloak start-dev, generated realm, dev CLI client, static service tokens.
[Prod] Keycloak (or the corporate IdP) on Postgres + TLS, SCIM provisioning, client-credentials
for services, no dev client, `AEOI_STATIC_USER_TOKENS=false`.
[Ent] federation to the corporate IdP (SAML/OIDC brokering), step-up auth for approvals.

## When we would revisit
When a second app needs the same users (then the IdP becomes the directory and we import from
it), or when per-request authorisation needs data the token cannot carry.
