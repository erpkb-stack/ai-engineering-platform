# frontend (React + TypeScript + Vite) — port 5173 — Phase 17b, ADR-028

Operations console: incident-first, evidence panels, trace waterfall, approval panel. Not a chat
clone. Rules: `.claude/rules/frontend.md`. Node >= 20. `make ui-install` · `make ui-dev` ·
`make ui-check` · `make ui-browser-test` · `make ui-demo`.

## Code map
- `openapi.json` GENERATED (`make openapi`) · `src/api/schema.d.ts` GENERATED (never edit) · `src/api/client.ts` typed client, ApiError (problem+json), auth middleware · `useAttemptKey.ts` one Idempotency-Key per attempt
- `src/auth/` oidc (PKCE, aeoi-console, tokens in MEMORY), `RequireAuth` (sign-in gate; a 401 with a VALID token = refused account, never a re-sign-in loop), `oidcAuth.safeReturnTo` (no open redirect), `me.ts` (`can(me, perm)`: UI gating only)
- `src/pages/` Incidents, IncidentPage + `incident/` (Hypotheses, Approvals, Panels), TracePage, ApprovalsInbox + `approvals.ts` (ONE polled query, 15 s), ReleaseRiskPage, SearchPage
- `src/components/ui.tsx` Pill, Time, Problem, EvidenceChip (link only when the reader may see it), Excerpt · `ErrorBoundary.tsx`

## Must
- Every AI finding shows type, confidence band, supporting AND contradicting evidence, approval state.
- Evidence, documents, trace messages are UNTRUSTED: render as text; links only via `safeHref`.
- Gate buttons with the reason shown; never rely on the gate (the server re-checks; tests prove both).
- Deciding happens next to the evidence (incident page), never from a list.
