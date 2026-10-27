# Final 10–15 minute demo — "HTTP 500s after deploy"

Fictional company: Northwind Cloud Systems. Service: `checkout-api` → `orders-db` (Postgres) via `order-repository`.
Planted ground truth (synthetic): deploy `2026.10.02.4` changed `OrderRepository.findOpenOrders()` to open a new connection per item, which exhausts the connection pool. A **decoy** signal: a `cart-cache` node restart at 09:52 UTC. All times are planted in seed v1 (`scripts/synth`): deploy 09:42, flag on 09:47, app p95 up 09:49, pool 100% 09:50, first `ERR_POOL_TIMEOUT` 09:50:10, HTTP 5xx +42% 09:51, thread pool 85% 09:52 (alternative hypothesis), DB latency +18% 09:53.

Status per step: `docs/phases/phase-29.md` section 1 (Phase 29 need check). Every step that exists is CHECKED by `make demo` (ADR-043); the decoy line is corrected: the system never proposes the cache restart as a cause.

| Min | Step | What the audience sees | Spec item |
|---|---|---|---|
| 0:00 | Problem framing | Before/after workflow slide; KPI definitions | business |
| 1:00 | Incident creation | Signed Alertmanager webhook (`POST /api/v1/alerts`, ADR-044) → incident INC-xxxx; SEV by the code rule (label `error` → SEV2, tier-1 checkout-api → SEV1); the catalog's dependents shown as the blast radius (affected = checkout-api only); the alert re-sent → `duplicate`, still one incident | 1 |
| 2:00 | Orchestrator planning | plan with chosen agents + budget in the trace view | 2, 3 |
| 3:00 | Parallel agents | Agent Trace waterfall: logs, metrics, deploy, code, RAG, historical running at the same time | 4–10 |
| 5:00 | Evidence | Log cluster `ERR_POOL_TIMEOUT`, pool utilisation metric, deploy diff, RUNBOOK-DB-012, similar INC-2911 | 5–10 |
| 6:00 | Initial hypothesis | "DB latency caused 500s" | 11 |
| 6:30 | **Critic challenge** | shows app latency rose *before* DB latency; proposes pool exhaustion; rejects the cache-restart decoy with evidence | 12 |
| 7:30 | Validation | citation checks pass; one unsupported sentence removed (shown in diff) | 13 |
| 8:00 | Evidence graph | hypothesis ↔ LOG/METRIC/DEPLOY/INCIDENT nodes | 14 |
| 9:00 | Report | Facts vs Hypotheses vs Recommendations; confidence band + rubric | 15 |
| 10:00 | Human approval | IC approves "roll back 2026.10.02.4" with a reason → controlled tool (simulated) | 16 |
| 11:00 | Audit trail | who/when/what/evidence/args hash; ENGINEER tries to approve → 403 | 17 |
| 11:30 | Security moment | poisoned runbook chunk tries injection → ignored, flagged | (bonus) |
| 12:00 | Business value | measured MTTI (manual vs AI on the 20-incident set), cost/investigation, eval dashboard | 18 |
| 13:00 | Architecture + what's prototype vs production | one slide, honest limits | — |

Rules: `make demo-fresh && make demo && make demo-clean` 3 times in a row on real models before any interview (ADR-043). Have a recorded video as a fallback (live LLM calls can fail; say so if they do and show the recording).
