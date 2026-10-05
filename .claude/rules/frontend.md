---
paths:
  - "frontend/**"
---
# Frontend rules

- React + TypeScript strict, Vite, TanStack Query for server state, React Router.
- Typed API client generated from the FastAPI OpenAPI spec — no hand-typed DTOs.
- This is an operations console, not a chat clone: dense tables, timelines, evidence panels, status colours.
- Every AI finding UI shows: type (Fact/Hypothesis/Recommendation), confidence band, evidence chips, contradicting evidence, and approval state.
- Approve/Reject buttons are disabled unless the user's role permits; the server re-checks anyway.
- Playwright for e2e; Vitest + Testing Library for components.
