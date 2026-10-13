# Phase 17 — React frontend (17a: identity provider · 17b: the UI)

## 0. Gate and scope challenge
- **Gate: passed.** Phase 16 verified on the Mac; PR #17 merged (55f3d38).
- **Challenge 1 — scope.** The spec lists 12 screens and a copilot. Owner decision: **core +
  release risk + search** (dashboards wait for their data: Phases 23/25); the **copilot gets
  its own phase** (a new agent + streaming API).
- **Challenge 2 — sign-in.** Recommended: a dev user picker. Owner decision: **real OIDC
  (Keycloak)**, and - after the cost was laid out (auth in all 8 services, roles in the IdP) -
  **every service trusts Keycloak**. Answer to "roles in two places": the realm is GENERATED
  from our directory; the directory stays the source of truth (ADR-027).
- **Challenge 3 — pending approvals.** Owner decision: **polling every 15 s** (push = [Prod]).
- **Split.** An auth bug and a UI bug in one package are hard to tell apart: **17a** (IdP,
  verified with curl + smokes) ships and is verified on the Mac before **17b** (the UI).

## 17a. Identity provider
### 1–2. Objective and reason
People sign in through Keycloak; every service accepts those USER tokens; nothing that
existed (service tokens, delegation, separation of duties, tests, smokes) changes behaviour.

### 3. Architecture
```
Keycloak (realm aeoi, generated from identity.users) ─ access token ─► any service
  Authenticator.verify: iss = Keycloak -> OidcVerifier · iss = aeoi-dev-issuer -> static key
```
| Guarantee | Enforced by | Test |
|---|---|---|
| RS256 + kid from JWKS; exact iss; aud aeoi-api | `OidcVerifier` | unit (forged HS256, alg none, other key, wrong iss/aud) |
| ID tokens never accepted as access tokens | `typ` = Bearer (+ aud) | unit + REAL Keycloak id_token |
| Only listed clients | `azp` ∈ AEOI_OIDC_CLIENTS | unit + real Keycloak (cli refused when not listed) |
| Never a service, never delegated, only our 5 roles | claim mapping | unit (mutation-checked) |
| Identity = OUR directory ids (uid, aeoi_sub) | generated realm, admin-only attributes | real account API: self-edit 400 |
| Token exchange only for users in OUR directory | STS unchanged | real Keycloak integration test |
| A demoted / deactivated / removed user loses access at the edge within 60 s, even with a valid token | `DirectoryCheck` in api `guard()` + `authenticated()` (review H1) | unit (+ route wiring) + real Keycloak: absent 401, active 200, deactivated 401, demoted 403 |
| Keys a rotation removed stop working | keys REPLACED on fetch; max age 600 s | unit |
| Keys fetched only for a new kid, ≤ 1/min, 2 s | key cache | unit (spray + rotation) |
| Production: IdP users only; no dev client; https issuer + JWKS | settings validator (every service) + `static_users` (no default) | unit (6 cases, each service's Settings) + real Keycloak |
| No offline (30-day) tokens; passwords stored as hashes | realm generator (`defaultRole` grants nothing; PBKDF2) | real Keycloak + smoke |
| Every service on the path verifies | all 8 `main.py` | real Keycloak: incident, api STS, tool-gateway manual + on-behalf-of; Mac smoke: release risk through 5 services |

### 4. Files
```
libs/security/oidc.py (new) · libs/web/auth.py (routing, oidc_verifier) · libs/common/settings.py
services/api/directory.py (new) + routes.py (directory check in guard/authenticated)
services/*/main.py (8) · scripts/keycloak_realm.py, kc_token.py, smoke-phase17a.sh (new)
docker-compose.yml (keycloak, profile auth) · Makefile · .env.example
docs/adr/ADR-027 (+ README) · CLAUDE.md · roadmap · this file
tests: libs/security/tests/test_oidc.py (23), services/api/tests/test_directory.py (4),
  tests/unit/test_auth_wiring.py (17), libs/common (settings),
  integration/security/test_keycloak.py (6, needs a running Keycloak)
```

### 5–6. Commands (macOS, zsh)
```zsh
cd ~/projects/ai-engineering-platform
git switch main && git pull
shasum -a 256 -c ../_to_delete/phase17a-base.sha256 | grep -v ': OK$'   # no output = safe
git switch -c phase-17-frontend
tar xzf ../_to_delete/phase17a.tgz
uv sync --all-packages && make check
cat >> .env <<'ENV'
AEOI_KC_PORT=8090
AEOI_OIDC_ISSUER=http://localhost:8090/realms/aeoi
AEOI_OIDC_CLIENTS=aeoi-console,aeoi-cli
ENV
make keycloak-up         # generates the realm from identity.users, starts Keycloak (~4 min on the Intel Mac)
# restart: make dev, run-tools, run-agents, run-orch, run-rag, run-llm, run-audit
make kc-smoke
AEOI_TEST_KEYCLOAK_URL=http://localhost:8090 AEOI_TEST_KEYCLOAK_USERS=secrets/keycloak/users.txt \
  make test-integration    # whole suite WITH the 6 Keycloak tests (1 skipped: Kafka)
make approval-smoke && make risk-smoke   # regressions (dev tokens still work)
```

### 7. Config
`AEOI_OIDC_ISSUER`, `AEOI_OIDC_AUDIENCE` (aeoi-api), `AEOI_OIDC_JWKS_URL` (default: Keycloak
certs), `AEOI_OIDC_CLIENTS`, `AEOI_STATIC_USER_TOKENS` (true; production: false), `AEOI_KC_PORT`.
Access tokens live 300 s.

### 8. Tests (sandbox, against a real Keycloak 26.0.7 run from the release tarball)
- `make check` 812 passed, 1 skipped; integration WITH Keycloak 249 passed, 1 skipped (Kafka).
- Mutations: verifier 7/8 caught (the explicit `alg` check is shadowed by
  `jwt.decode(algorithms=[RS256])`, documented); directory check, route wiring, production
  validator and key max age 12/12 caught. The route-wiring mutation was MISSED at first: a unit
  test was added.
- Found by running the whole suite: the Keycloak STS test assumed "user absent" while the seed
  had created that user. Each test now sets up its own directory state.
- The container's start command and health check were run as-is against the local Keycloak.
- **Not run here:** the Docker image (registries blocked in the sandbox) and the full smoke.

### 10. Verify (gate for 17b)
- [ ] `make kc-smoke` all ✔ on the Mac
- [ ] `test_keycloak.py` 6 passed on the Mac (with Keycloak up)
- [ ] `make test-integration` green; `make approval-smoke`, `make risk-smoke` unchanged

### 11. Failure scenarios
| Symptom | Cause | Fix |
|---|---|---|
| 401 with a Keycloak token everywhere | service started before `.env` had AEOI_OIDC_ISSUER | restart the services |
| 401 only for `make kc-token` tokens | AEOI_OIDC_CLIENTS lacks aeoi-cli | add it (dev only) |
| 403 "Subject token is invalid" at investigation start | user in Keycloak but not in identity.users | `make keycloak-up` after `make db-seed` |
| `make keycloak-up` times out | slow start (measured ~4 min on the Intel Mac; first run also pulls the image) | `docker inspect aeoi-keycloak-1 --format '{{.State.Health.Status}}'`; if `healthy`, carry on; else `docker compose --profile auth logs keycloak` |
| A test expecting `aeoi-cli` to be refused gets 200 | `make` exports `.env` (which lists aeoi-cli) into the tests | tests set `oidc_clients` explicitly (fixed) |
| `make kc-smoke`: a token error right after `make keycloak-up` | first login on a cold Keycloak is slow (timeout was 10 s, now 30 s) | the smoke now prints the script's own error; rerun |
| issuer mismatch | AEOI_KC_PORT changed but AEOI_OIDC_ISSUER not | keep them in sync |
| 403 "Your sign-in grants roles the directory does not" | role removed in identity.users, realm not regenerated | `make keycloak-up` (re-imports a fresh realm) |
| 503 on every Keycloak request at the api | api has no directory (DB not configured) | fail closed by design: configure the DB |

## 17b. The UI
Next: React + TypeScript + Vite + TanStack Query; typed client from the OpenAPI; sign-in with
`aeoi-console` (PKCE); incidents, incident page (timeline, hypotheses with evidence chips and
contradicting evidence, evidence, postmortem, approvals with decide), investigation trace,
approvals inbox (15 s polling), release risk, search.
