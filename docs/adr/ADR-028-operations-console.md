# ADR-028: The operations console - a browser app with PKCE, tokens in memory, types from the contract

Status: Proposed
Date: 2026-10-13
Builds on: ADR-027 (Keycloak, the `aeoi-console` public client), ADR-026 (approvals; inbox addendum)

## Context
Phase 17b puts a UI in front of the platform. Owner decisions (2026-10-12): scope = incidents,
incident page (timeline, hypotheses with evidence chips and contradicting evidence, evidence,
postmortem, approvals with decide), investigation trace, approvals inbox, release risk,
knowledge search; real OIDC sign-in (17a); pending approvals by **polling every 15 s**; the
copilot is its own later phase.

## Decision
```
browser (React SPA, :5173)  ──PKCE──►  Keycloak (:8090)   code -> tokens, kept IN MEMORY
   │  /api/*  (Vite proxy, same origin: no CORS on the api)
   ▼
 api (:8000)  ── every check stays in the services ──►  incident / orchestrator / rag
```
1. **Single-page app, public client, Authorization Code + PKCE (S256)** with `oidc-client-ts`.
   Exact redirect URI (`/callback`), refresh tokens single-use (rotation), access token 5 min.
2. **Tokens in memory only.** A reload signs in again through the Keycloak session (a redirect,
   no password). The PKCE verifier lives in sessionStorage for one redirect.
3. **Types are generated, never typed by hand:** `scripts/export_openapi.py` writes
   `frontend/openapi.json` (the `/api/v1` routes only); `openapi-typescript` turns it into
   `src/api/schema.d.ts`; `openapi-fetch` calls are typed by path. Two guards: a Python unit
   test fails when the api changed and the file was not regenerated; `npm run check:api` fails
   when the types were edited or not regenerated. To make the contract honest, the api now
   DECLARES what it already accepted: list filters/cursor (incidents, release risk) and
   `Idempotency-Key` on the four creating POSTs.
4. **The UI gates, the server decides.** Buttons are disabled with the reason (role, separation
   of duties, state); every check is repeated by the services (tested).
5. **Untrusted content is text.** Evidence, documents, trace messages: no
   `dangerouslySetInnerHTML`; links only when http(s).
6. **Deciding happens next to the evidence.** The inbox lists and links; Approve/Reject live on
   the incident page with the recommendation's evidence chips and the way back.
7. **A 401 with a valid token is a refusal, not an expired session** (review H1): the console
   says so instead of signing in again in a loop (the directory check of ADR-027).

## Alternatives
| Option | Why not (here) |
|---|---|
| BFF session (the api holds tokens, the browser gets an HttpOnly cookie) | Stronger against XSS token theft, and the [Prod] choice for a high-value console; needs server-side sessions, CSRF protection and a token store in the api. Here: tokens in memory + strict "text only" rendering + short tokens + rotation. |
| Tokens in localStorage | Survives reloads, but any XSS reads them forever. |
| Hand-written DTO types | Drift silently from the api; the rules forbid it. |
| Push for the inbox (SSE/WebSocket) | Owner chose polling; push is [Prod] (ADR-026 "revisit"). |
| A component library (MUI etc.) | ~1 MB of UI for ~10 screens; plain CSS keeps the console dense and small (312 kB JS). |

## Tradeoffs
- In-memory tokens: every reload is a Keycloak round trip (fast while the SSO session lives).
- Polling: one request per 15 s per open console (paused while the tab is hidden). Served by the
  partial index `ix_approvals_undecided_expires_at` (migration 0025): expiry is recorded lazily,
  so "undecided" rows include long-expired ones.
- No CSP yet: Vite's dev server injects inline scripts. [Prod] the built app is served with
  `Content-Security-Policy: default-src 'self'; connect-src 'self' <issuer>` by the ingress.
- Error responses (401/403/404/409) are not declared in the OpenAPI (only 2xx/422): the client
  reads problem+json at run time. Declaring them on every route is a separate api change.
- `oidc-client-ts`, `openapi-fetch`, TanStack Query, React Router: four runtime dependencies,
  each replacing code we would otherwise write and test ourselves.

## Prototype vs Production vs Enterprise-scale
[P] Vite dev server, one origin via proxy, dev users from the generated realm.
[Prod] built static files behind the ingress with CSP; or a BFF with HttpOnly session cookies;
push for approvals; the corporate IdP.
[Ent] step-up authentication (re-sign-in) before approving a consequential action.

## When we would revisit
When the console holds rights worth stealing for longer than a token's 5 minutes (then a BFF),
or when the copilot phase needs streaming.
