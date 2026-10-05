# evaluation (Python 3.12 / FastAPI) — local port 8009

**Purpose:** Eval datasets, experiment runs (prompt/model/retrieval versions), metrics, comparisons; online feedback aggregation.

**Owns schema:** `eval` (evaluations, datasets, runs)
**Events:** consumes FeedbackSubmitted, InvestigationCompleted
**Must never:** Run in the request path of investigations.
**Why this is a separate service:** Offline/batch workload with different resources and access to labelled data.

Status: Phase 1 — design only. Scaffold with the `new-service` skill in its phase (see docs/roadmap.md).
