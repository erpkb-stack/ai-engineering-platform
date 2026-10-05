# knowledge-service (Python 3.12 / FastAPI) — local port 8007

**Purpose:** Engineering knowledge graph queries (service→repo/API/DB/pipeline/runbook) and change-impact traversal; reads catalog data, adds derived edges (code deps, incident links).

**Owns schema:** `knowledge` (edges, derived graph)
**Events:** consumes CatalogChanged, DocumentIndexed
**Must never:** Duplicate the catalog's source-of-truth data.
**Why this is a separate service:** Spec requirement. NOTE (review): a strong candidate for merging into service-catalog or rag — revisit at Phase 30.

Status: Phase 1 — design only. Scaffold with the `new-service` skill in its phase (see docs/roadmap.md).
