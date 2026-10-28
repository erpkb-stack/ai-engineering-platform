# Phase 29 - End-to-end demonstration (ADR-043)

Status: **29a RUN on the Mac 2026-10-27**: rehearsal OK, one real-model run PASS 13/13 (not counted - 29b-29e missing). **29b VERIFIED on the Mac 2026-10-28 (ADR-044, PR #47, main 2569212).** **29c VERIFIED on the Mac 2026-10-28 (ADR-045, PR #48, main 33a3ee5).** **29d BUILT 2026-10-28 (sandbox only; ADR-046)** - Mac run pending. Owner (2026-10-27): "Phase 29";
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
| 7:30 | removed sentence shown as a diff | **29c built** | `removed_shown`: listed == counted, shown apart, none in the report; "0 removed" is a PASS (owner) |
| 8:00 | evidence graph | **29d built** | `evidence_graph`: every node is readable evidence, top cause supported by >= 3 kinds incl. DEPLOY; screen = Vitest + browser test |
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

**On the Mac (kind, fake models, after `make demo-fresh`):** `alert_intake` PASS - "INC-10273
SEV1 by rule 'label severity=error -> SEV2; tier 1 -> raised to SEV1'; re-sent = duplicate"
(so triage reached the catalog from inside the pod with its own token); verdict REHEARSAL-OK
(9 PASS, 4 n/a; investigation PARTIAL in 45 s, the 4 agents overlapped 2.8 s).
`k8s-netpol-test` 122 passed, 16 skipped, 0 failed: incident-service -> service-catalog `open`,
only it and tool-gateway reach the catalog, every other pod times out; the 16 skips = 8 calls
to a pod's own Service + 8 to Keycloak (stopped). 0 restarts on all 9 pods after the fresh
start (2 clean of 3 fresh starts so far). One port-5433 refusal on the first `demo-fresh`
(the port not yet released after `down`): a retry passed. Idle node CPU 70% one minute after
the netpol test - no earlier idle reading to compare; read again before each counted pass.

## 5. 29c - removed model text, kept and shown (ADR-045)
Owner (2026-10-28): **postmortem + ranker**; a run with **0 removed is a PASS** that says so.

**Built:** the postmortem check keeps every sentence and action item it removes, with the first
rule it broke and a code-written detail (`removed`); the counts are derived from that list. The
ranker's uncited explanation is kept as `explanation_removed` + why. Removed text is shown only
to a reader of every evidence kind of the incident; apart from the report: a "Removed by code"
markdown section (escaped, bare URLs broken) and a folded, struck-through list in the console
(postmortem panel and hypothesis card). The critic never sees a removed explanation.

**Found while building:**
1. A 3-4 character action item passed the sentence check and was then dropped by a second rule
   WITHOUT being counted. Now `too_short`, counted. (An action item with a bad category, which I
   first called a gap, cannot arrive: the gateway's schema check refuses it. The agent's own
   filter is now defensive and counted.)
2. My first edit of the hypotheses redaction moved the critic-alternative redaction under the
   new branch: a MANAGER would have seen the critic's alternative statement. The existing
   reasoning e2e test failed on it before anything shipped; fixed, and that test now also
   checks the removed explanation.
3. The demo cannot promise a removal: fake models write no narrative (always 0), and a real
   Sonnet run may remove nothing. The check proves the mechanism instead.

**On the Mac (kind, fake models):** `removed_shown` PASS "0 removed" (fakes write no narrative),
REHEARSAL-OK, 0 restarts (3 clean fresh starts of 4). Idle node CPU 79.55% (70% before).

**Measured (owner's query over every stored real postmortem):** 70 written by
claude-sonnet-5-5; **8 had a removal (11%; Wilson 95%: 6-21%)**, exactly one sentence each:
**6 for a number not in the facts, 2 uncited**. So the number check does most of the work.
For the 3 counted runs, P(at least one shows a removal) = 31% (17-51%): most likely all three
say "0 removed", which the owner's rule counts as a PASS. These 70 span many eval incidents
(planted scenarios: an upper bound), not only the demo.

## 6. 29d - the evidence graph screen (ADR-046)
**Built:** `frontend/src/pages/incident/EvidenceGraph.tsx`, a panel on the incident page: plain
SVG, hypotheses by rank vs evidence by kind, one node per evidence key, stance by line style
AND colour, focus dims the rest, the same links as a table. No graph library, no new endpoint:
it draws GET /hypotheses as the server filtered it. Demo step `evidence_graph`. Vitest (9) +
a browser test (needs an investigated incident; skips and says why otherwise).

**Why a graph at all (challenged before building):** the hypothesis cards already list their
own evidence. The graph earns its place only by showing SHARED evidence: the demo deploy
supports one cause and contradicts another. If it did not show that, it would be decoration.

**Sandbox render** (Chromium, demo-shaped data): two columns, the shared DEPLOY node with a solid
and a dashed line; focus on the pool cause leaves its METRIC support and the DEPLOY/CONFIG
contradictions bright. At phone width it scales down too small to read: desktop-first.

## 7. Next
29e MTTI. Then the gate: `make
demo-fresh && make demo && make demo-clean`, 3 real-model PASS in a row.

## 8. Verify
- [x] 29a rehearsal on the Mac (fake): REHEARSAL-OK, `make demo-clean` removed 0 rows.
- [x] One real-model run: PASS 13/13 (not counted: the script is not complete yet).
- [x] 29b on the Mac: CI green (PR #47), `alert_intake` PASS with SEV1 + "tier 1", netpol 0 failed with incident-service -> service-catalog open, 0 restarts.
- [x] 29c on the Mac: CI green (PR #48), `removed_shown` PASS ("0 removed" on fakes); real-postmortem rate measured (8/70).
- [ ] 29d on the Mac: CI green; `make demo` (fake) shows `evidence_graph` PASS; `make ui-browser-test` passes the graph test (not skipped) after `make demo`.
- [ ] 29e built and in the demo.
- [ ] 3 real-model PASS in a row, each after `make demo-fresh`, files in `data/demo/results/`.
