# Phase 29 - End-to-end demonstration (ADR-043)

Status: **29a RUN on the Mac 2026-10-27**: rehearsal OK, one real-model run PASS 13/13 (not counted - 29b-29e missing). **29b BUILT 2026-10-27 (sandbox only; ADR-044)** - Mac run pending. Owner (2026-10-27): "Phase 29";
clean = **fresh containers, keep data**; models = **fake to rehearse, real for the 3 passes**;
the 4 missing steps = **build all four**.

## 0. Gate
- Phase 28 closed: PRs #43-#45 merged, main 43c4d0b green, `git ls-files` of 0029 + ADR-042 +
  the restored 28c-1 result = 3.

## 1. Need check - the Phase 1 script against what exists
| min | step | exists? | how 29a checks it |
|---|---|---|---|
| 1:00 | alert webhook -> incident | **29b built** | `alert_intake`: a signed alert -> `created`; the same alert re-sent -> `duplicate`, same key |
| 2:00 | orchestrator plan | yes | the investigation ends named (COMPLETE on real models) |
| 3:00 | parallel agents | yes | the 4 evidence agents' intervals all overlap |
| 5:00 | evidence | yes | kinds LOG METRIC DEPLOY CONFIG INCIDENT |
| 6:00-6:30 | hypothesis + critic challenge | yes (real models) | #1 = DEPLOY-4821, VALIDATED, critic "supported" |
| 6:30 | decoy rejected | **differs** | the system never proposes the cart-cache restart (no support in code) - checked as "never #1 / HIGH"; the script line is corrected |
| 7:30 | removed sentence shown as a diff | **no** (counts only) | 29c |
| 8:00 | evidence graph | **no** (edges in the API only) | 29d |
| 9:00 | report | yes | postmortem root cause = DEPLOY-4821, similar-incident section |
| 10:00 | approval -> controlled tool | yes (real models) | the investigation's proposal; ENGINEER 403; IC approves -> EXECUTED, MITIGATED |
| 11:00 | audit trail | yes | decision record: who, when, why, args sha256, tool call |
| 11:30 | injection ignored | yes | the adversarial document is never returned |
| 12:00 | MTTI manual vs AI | **no** | 29e - needs the owner's timed manual runs |

Found: **the rollback changes the demo data** (a new deploy row on checkout-api) -> `make
demo-clean` after every pass. **`make db-seed` truncates rag + history** -> never before a demo.

## 2. What 29a adds
`tests/demo/demolib.py` (checks, `tests/unit/test_demo.py`), `tests/demo/run.py`, `make demo`,
`make demo-clean`, `make demo-fresh`; results in `data/demo/results/`.

Sandbox rehearsal (processes, fake models, no Java catalog): 9 PASS, 4 n/a, 1 FAIL
(`services_ready`: no catalog in the sandbox); investigation PARTIAL in 38 s, the 4 agents
overlapped 0.7 s, evidence COMMIT CONFIG DEPLOY DOC INCIDENT LOG METRIC RUNBOOK.

## 3. Runs on the Mac (kind; branch phase-29a-demo)
| run | models | verdict | investigation | evidence agents overlap | notes |
|---|---|---|---|---|---|
| 19:22 UTC | fake (rehearsal) | REHEARSAL-OK: 9 PASS, 4 n/a | PARTIAL, 47 s | 3.4 s | demo-clean removed 0 |
| (first real try) | real | crashed | - | - | my bug: critic verdict read at the top level; it is in `detail` (fix1, tested on the API shape; a crashing check is now a FAIL) |
| 21:24 UTC | **real** | **PASS: 13 of 13** | COMPLETE, 89 s | 8.1 s | critic "supported", VALIDATED; rollback proposed for the SRE; ENGINEER 403; IC approved -> EXECUTED, MITIGATED; demo-clean removed 1 |

Not counted toward the gate: 29b-29e are not in the script yet.

Pods during `make demo-fresh`: the FIRST fresh start (right after a deploy with the load
routing, so two rollouts back to back) restarted every pod 2-3 times ("Completed" = a liveness
kill shut them down cleanly); the SECOND (one rollout) restarted none. One of two - the liveness
probes are NOT changed on it; `make k8s-status` after each `demo-fresh` keeps the count.

## 4. 29b - the signed alert webhook (ADR-044)
**Built:** `POST /api/v1/alerts` (Alertmanager v4 payload). HMAC-SHA256 over `ts + "." + raw
body`, 300 s window (`aeoi_security.webhook`), checked at the api (rate limit per sender ->
64 KB -> signature, then the RAW bytes are forwarded) and again in incident-service. One
incident per (fingerprint, startsAt). Triage rule table in `aeoi_incident.alerts`; tier and
dependents from the catalog with incident-service's own token; the event says the rule.

**Found while building (design changed):**
1. The design said "affected_services = the service + its catalog dependents". The
   orchestrator investigates the FIRST 3 affected services: checkout-api has 6 dependents,
   so the agents would have looked at victims. Now affected = the alert's service; the
   dependents are the blast radius in the description.
2. 20 alerts x a 2 s catalog timeout could reach 40 s; the api's upstream timeout is 5 s.
   Now one call per service per batch and none after the first failure in a batch.
3. FastAPI 0.142 wraps included routers: a route walk over `app.routes` sees none of them.
   The "only public route" test reads the router itself (it first failed for that reason).

**Sandbox (processes, real HTTP, Postgres; catalog = a stub on :8010):** signed alert ->
INC created, SEV2 without the catalog, SEV1 with tier 1 + blast radius "chat-worker,
returns-api"; re-sent -> duplicate, same key; unsigned -> 401 problem+json; the timeline
shows source ALERT, actor service:alerts. Mutation check: with the incident-service
signature check removed, 5 of 12 webhook integration tests fail.

**Demo change:** `incident_created` is now `alert_intake` (the runner signs with
`secrets/alert_webhook_secret.txt` - dev only). With the catalog on, the demo incident is
SEV1 (was SEV2 typed by hand); nothing in the investigation reads severity.

**New setup on the Mac (once):** `make alert-secret` (also in `make setup`) and `make
tools-tokens` (mints `secrets/incident_catalog_token.txt`); both files must exist before
`make k8s-apply` - compose and the k8s Secrets are built from them.

## 5. Next
29c removed-sentence diff, 29d evidence-graph screen, 29e MTTI. Then the gate: `make
demo-fresh && make demo && make demo-clean`, 3 real-model PASS in a row.

## 6. Verify
- [x] 29a rehearsal on the Mac (fake): REHEARSAL-OK, `make demo-clean` removed 0 rows.
- [x] One real-model run: PASS 13/13 (not counted: the script is not complete yet).
- [ ] 29b on the Mac: CI green; `make demo` (fake) shows `alert_intake` PASS with SEV1 and "tier 1" (catalog on); `make k8s-netpol-test` passes (new edge incident-service -> service-catalog).
- [ ] 29c-29e built and in the demo.
- [ ] 3 real-model PASS in a row, each after `make demo-fresh`, files in `data/demo/results/`.
