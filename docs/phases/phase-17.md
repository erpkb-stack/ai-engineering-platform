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

### 10. Verify (gate for 17b) - passed on the Mac 2026-10-13, commit f358606
- [x] `make kc-smoke` 21/21 on the Mac
- [x] `test_keycloak.py` 6 passed on the Mac (with Keycloak up), inside the full run
- [x] `make test-integration` 249 passed, 1 skipped (Kafka); `make approval-smoke` 23/23, `make risk-smoke` 19/19
- Open: the first full run on the Mac had 10 agent-test failures that never came back in two
  later full runs (likely load during service restarts; not proven). If seen again: `-x`.

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

### 0. Gate and scope (17b)
- **Gate: passed.** 17a verified on the Mac (above), commit f358606 on `phase-17-frontend`.
- **Challenge - the inbox had no api.** Phase 16 listed approvals per incident only: a 15 s
  poll would be one request per incident. Added `GET /api/v1/approvals?status=PENDING` (ADR-026
  addendum) with its own partial index (migration 0025).
- **Challenge - the contract hid parameters.** The incidents and release-risk lists accepted
  filters and a cursor that the OpenAPI did not show, and four POSTs used `Idempotency-Key`
  without declaring it. Generated types would have forbidden what the api accepts. Declared
  at the edge (validated there, forwarded unchanged).
- **Not in 17b:** requesting an action from the UI (requests come from investigations and
  SREs' tools), dashboards (Phases 23/25), the copilot (its own phase).

### 1-2. Objective and reason
An incident commander can see what the AI found, with its evidence and what contradicts it,
and approve a consequential action next to that evidence - signed in through Keycloak.

### 3. Architecture (ADR-028)
```
browser SPA (:5173) --PKCE (aeoi-console)--> Keycloak (:8090); tokens in MEMORY
   /api/* via Vite proxy (one origin) --> api (:8000) --> incident / orchestrator / rag
types: api -> frontend/openapi.json (export script) -> src/api/schema.d.ts (openapi-typescript)
```
| Screen | Shows | Gate (UI; the server re-checks) |
|---|---|---|
| Incidents | filters, cursor paging | incidents:read |
| Incident | hypotheses (type, confidence band, status, rubric, critic, supporting + contradicting chips, hidden count), approvals (recommendation, way back, evidence, decide), evidence, timeline, investigations, postmortem | Investigate: investigations:run; decide: actions:approve AND not the requester AND PENDING |
| Trace | waterfall: agent, model, prompt version, tokens, latency, cost, messages | incidents:read |
| Approvals inbox | PENDING across incidents, soonest expiry first, 15 s poll, nav badge | incidents:read (decide on the incident page) |
| Release risk | deploy / PR / range -> band, coverage, factors with evidence, the way back | code:read |
| Search | hybrid/keyword/vector, degraded state shown, document view | docs:read |

| Guarantee | Enforced by | Test |
|---|---|---|
| Sign-in only through Keycloak, PKCE S256, exact callback | oidc-client-ts, realm generator | e2e (real Keycloak: S256 in the URL, wrong redirect_uri refused); unit (realm) |
| No token in browser storage | in-memory user store | e2e (mutation-checked: sessionStorage fails it) |
| A refused account does not loop sign-ins | `unauthorizedHandler` (review H1) | Vitest (valid token -> refusal; expired -> ONE redirect for 4 concurrent 401s) |
| No open redirect after sign-in | `safeReturnTo` | Vitest (absolute, //, /\\, javascript:) |
| Untrusted evidence is text; links only http(s) | no innerHTML, `safeHref` | Vitest (markup + javascript: URI) |
| Requester cannot approve; only an IC; only PENDING | `whyCannotDecide` + server | Vitest + e2e with the real stack (decide.spec) |
| Approve runs the action as the approver | Phase 16 path | e2e decide.spec + DB check (decided_by = IC #2, EXECUTED, MITIGATED) |
| Inbox: PENDING only, soonest first, max 100, same redaction | incident-service SQL + api validation | integration (4 mutations caught) + api unit |
| One retry = one action | `useAttemptKey` (review M5) | Vitest |
| Types never drift from the api | export + `check:api` | unit test + npm check (hand edit caught) |

### 4. Files
```
frontend/ (new): package.json + lock, vite/ts/playwright config, openapi.json (generated),
  src/api (client, schema.d.ts generated, useAttemptKey), src/auth (oidc, RequireAuth, me),
  src/pages (Incidents, Incident + incident/*, Trace, ApprovalsInbox, ReleaseRisk, Search),
  src/components (ui, ErrorBoundary), src/**/*.test.ts(x), e2e/ (console, decide)
services/incident-service: approvals.list_pending + GET /v1/approvals
services/api/routes.py: GET /approvals; declared list params + Idempotency-Key
libs/db: index ix_approvals_undecided_expires_at + migration 0025
scripts: export_openapi.py, ui-demo-request.sh (new); keycloak_realm.py (exact URIs, rotation)
tests: unit test_openapi_contract.py, test_keycloak_realm.py; integration inbox test; api unit
docs: ADR-028 (new), ADR-026 addendum, this file, roadmap, CLAUDE.md files
```

### 5-6. Commands (macOS, zsh) - see the Mac steps in the hand-over; summary:
```zsh
make db-upgrade && make keycloak-up            # index 0025; realm with exact callback + rotation
make ui-install && make ui-check               # Node >= 20
make ui-dev                                    # http://localhost:5173 (own terminal)
make ui-browser-test                           # real browser + real Keycloak (downloads Chromium once)
make ui-demo   # then approve in the browser as IC #2 (manual)
AEOI_UI_DECIDE=1 make ui-browser-test   # 8 tests: decide.spec makes + cleans its OWN request
```

### 7. Config
`VITE_OIDC_ISSUER` (default http://localhost:8090/realms/aeoi), `AEOI_API_URL` (Vite proxy
target, default http://localhost:8000). Port 5173 is fixed (`strictPort`): it IS the redirect URI.

### 8. Tests (sandbox: real Keycloak 26.0.7 from the tarball, all 7 services)
- `make check` 819 passed; integration WITH Keycloak 250 passed, 1 skipped (Kafka).
- Vitest 43 passed; Playwright 8 passed with AEOI_UI_DECIDE=1 (decide.spec creates its own request,
  run twice in a row: repeatable), 6 passed + 2 skipped without it.
- Mac run 1: decide.spec failed - it was pointed at INC-10061, already decided by hand. The
  spec depended on a hand-picked incident; it now creates and cleans its own (fix2).
- Mutations: inbox SQL/redaction 4/4 (+1 redundant condition removed), frontend guards 12/12,
  token storage (e2e) 1/1, types drift 1/1.
- Screens checked visually with screenshots against real data (INC-10019, a real trace, search).
- **Not run here:** Node/npm on the Mac, Docker image of Keycloak with the new realm (same
  version; realm import tested), Playwright on macOS.

### 9. Review (independent, before shipping)
1 high, 5 medium, 7 low - fixed: H1 401 loop for a refused account (and a race the new test
found: concurrent 401s), M1 stale findings after a run, M2 hash scroll, M3 stuck "Signing in",
M4 inbox without an index, M5 idempotency key per click; low: return path, frozen countdown,
100+ truncation, list refresh after a decision, no error boundary, realm wildcard + no
refresh rotation, undeclared Idempotency-Key, a tautological e2e. Documented, not fixed: CSP
(dev server; [Prod] at the ingress), error responses not declared in the OpenAPI (ADR-028).

### 10. Verify (gate for Phase 18)
- [ ] `make db-upgrade`, `make keycloak-up`, services restarted
- [ ] `make ui-install && make ui-check` green (43 tests, types match)
- [ ] `make ui-browser-test` 6 passed (2 skipped without a demo)
- [ ] `make ui-demo` -> approve in the browser as IC #2 -> Executed, MITIGATED; then
      `make ui-demo-clean`
- [ ] `AEOI_UI_DECIDE=1 make ui-browser-test` 8 passed (approve-in-browser, automated)
- [ ] `make check`, `make test-integration` (with Keycloak), `make kc-smoke`, `make approval-smoke` green

### 11. Failure scenarios
| Symptom | Cause | Fix |
|---|---|---|
| Keycloak page "Invalid parameter: redirect_uri" | old realm (wildcard) or console not on 5173 | `make keycloak-up`; port 5173 must be free |
| "the platform does not accept your account" | directory refuses the user (inactive / changed) | fix identity.users, `make keycloak-up` |
| Every screen: 502/503 problem | api not running or not restarted after `.env` | restart `make dev` |
| Inbox empty although a request exists | it expired (30 min) | `make ui-demo` again |
| `npm ci` fails | Node < 20 | `brew install node@22` |
| ui-check: "schema.d.ts is out of date" | api changed, types not regenerated | `make openapi` |
| e2e: browser executable missing | first run | `make ui-browser-test` installs Chromium |
