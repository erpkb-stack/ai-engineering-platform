# Phase 29 - End-to-end demonstration (ADR-043)

Status: **29a RUN on the Mac 2026-10-27**: rehearsal OK, one real-model run PASS 13/13 (not counted - 29b-29e missing). **29b VERIFIED on the Mac 2026-10-28 (ADR-044, PR #47, main 2569212).** **29c VERIFIED on the Mac 2026-10-28 (ADR-045, PR #48, main 33a3ee5).** **29d VERIFIED on the Mac 2026-10-28 (ADR-046, PRs #49 + #50, main 86b26ba).** **29e VERIFIED on the Mac 2026-10-29 (ADR-047 + amendment, PR #55, main f471ac2): AI-only, manual baseline declared invalid.** **Gate: 0 of 3** (7 runs: 4 PASS, 3 FAIL; fix5 = new code, count restarts; see section 8). Owner (2026-10-27): "Phase 29";
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
| 12:00 | MTTI manual vs AI | **29e built** | `mtti`: a report with 6 scored manual cases + a real-model AI run; n/a on a rehearsal |

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

**On the Mac (fake models, 2026-10-28):** `evidence_graph` PASS - "5 hypotheses, 11 evidence
nodes, 22 links (9 contradicting); top supported by COMMIT CONFIG DEPLOY LOG METRIC".
`make ui-browser-test`: 5 passed, 1 failed, 3 skipped - two findings in MY browser tests:
1. The graph test SKIPPED: it read the panel with an instant `isVisible()` while the
   hypotheses were still loading. A skip proves nothing. fix1 WAITS (15 s) for the picture or
   the "no links" sentence, and skips only on the sentence. Checked here against a page that
   renders the graph 2 s late: the old logic skips, the new one waits and passes.
2. "an SRE signs in" failed: the redirect to Keycloak was in flight to the RIGHT URL when the
   5 s default ran out (first test of the run: cold Vite, Keycloak just recreated - it took
   436 s to become healthy). fix1 waits 20 s for that redirect; the URL rule is unchanged.
   The 2 decide tests skip by design (opt-in: `AEOI_UI_DECIDE=1`).

**29d on the Mac:** the evidence-graph browser test PASSED after fix1 (7 passed, 2 opt-in skips;
first test 17.2 s, so the old 5 s wait would have failed). Before that, a run failed 7 of 7 with
`ERR_EMPTY_RESPONSE` from Keycloak itself (an old container had stopped answering; a fresh
`make keycloak-up` fixed it; the cause was not captured). Process finding: PR #49 was merged at
ae76b5b BEFORE fix1 was pushed; the head check refused the second merge and fix1 went in as
PR #50 (cherry-pick; main's code proved equal to the tested commit first).

## 7. 29e - MTTI, measured (ADR-047)
Owner (2026-10-28): manual = **a CLI over the same read tools**; **one fresh paid run** of all
planted cases (~$1.30); **6 manual cases, 30-minute cap**. Protocol: `docs/demo/mtti-protocol.md`.

**Built:** `tests/mtti/` - `mttilib.py` (the rules, fixed before timing: sample, identified, report
statistics), `manual.py` (`make manual`), `report.py` (`make mtti`); demo step `mtti`.

**Found before any timing (sandbox, a throwaway session against the ci-sim stack):**
1. A plain seeded shuffle of the 17 detectable cases gave SIX v1 cases led by S01 - the demo's
   own pattern. Now 4 from v1 + 2 from v2, S01 out of the manual sample, reserves alternate packs.
2. The services answer times in the DB session's offset (-05:00 in the sandbox) while the alert
   is UTC: the CLI shows every time in UTC.
3. `deploys` with a 24 h look-back was refused (the tool allows 24 h at most, end included):
   default -1430 min, and the CLI refuses a longer window itself.
4. Tool output reaches a terminal: control characters are removed (an ESC sequence in a log line
   could rewrite the screen).

**29e fix1 (the owner's first manual session, 2026-10-28):** 4 timed cases were abandoned before
one was worked, and every cause was the TOOL or the protocol, not the person: (1) an open case
looked like an idle prompt, and its clock ran 10, then 87 minutes with 0 tool calls; (2) a Return
left over from pasting an `abandon` line opened the next case at once (twice); (3) there was no
practice round, so the first case would include learning the tool; (4) the help's `<service>`
placeholders were pasted literally. Fixed: a case opens only on the typed word `start`; a resumed
case says CLOCK RUNNING and shows the commands; `make manual PRACTICE=1` = one untimed case
(v1-S01, never sampled) with the answer shown; placeholders are refused with an example; an
abandoned case is replaced from ITS pack (the 4 + 2 mix holds). The 4 abandons stay in the record
with their reasons.

**29e fix2:** an answer's SUBJECT is checked (a change needs a `DEPLOY-n` key; a saturation or
traffic answer needs a metric the tools know - a unit test pins that list to the tool's): `answer
change code`, typed with 0 tool calls on an accidental case, had been accepted and would have
counted as a failed case. The owner corrected that record openly (marked abandoned, the original
answer kept in `corrected_from`). Practice judges the answer only (the right answer after an
80-minute practice was shown as "not right" because the 30-minute cap was applied).

**29e result (Mac, 2026-10-28/29): the AI side is measured; the manual side is NOT a valid
baseline (owner decision 2026-10-29, ADR-047 amendment).**
- AI, real models (v1 `investigations-2026-10-28T145524.json`, v2 `...150514.json`, 0 invalid):
  **14 of 17 detectable planted cases identified (top cause = planted AND VALIDATED; Wilson 95%
  59-94%), median 35.7 s (23.1-45.8 s)**; 2 more had the right top cause without validation.
  Planted data: an upper bound.
- Manual: 12 cases opened, 6 abandoned (each with its reason: idle clocks, pasted Returns, no
  practice round), 6 answered, 0 identified. The answers were not investigations: the last 5
  took 56-174 s with 4-6 tool calls and NO metrics call; two queried the previous case's services;
  the answers repeated (traffic requests_per_sec x2, saturation cpu_util x4); one ran `diff` on the
  culprit deploy and answered cpu_util. The first report (`mtti-2026-10-29T152516.json`) printed
  "AI first 6, manual first 0, sign test p=0.031" - a true computation of a meaningless
  comparison. It is kept as a record, never quoted.
- My fault too: the demo check counted cases and would have PASSED on this; fixed (the report now
  shows the work per case; an owner-declared invalid baseline makes the step AI-only).
- What I learned about the protocol: a timed terminal investigation is a skill test, not a time
  measurement, unless the person practises first and works in one sitting; and only a person who
  never saw the data gives a fair baseline.

## 8. The gate - runs so far (main f471ac2, real models)
Rule: 3 real-model PASS **in a row**, each after `make demo-fresh`, `make demo-clean` after each.
Every run is committed, the FAILs too.

| file (`data/demo/results/`) | verdict | s | note |
|---|---|---|---|
| demo-2026-10-29T161620.json | PASS | 116 | SEV1 by the tier-1 rule; COMPLETE in 88 s |
| demo-2026-10-29T163823.json | FAIL | 18 | `alert_intake`: webhook answered 503 (reason not kept) |
| demo-2026-10-29T164933.json | PASS | 73 | SEV2: `tier unknown (catalog unreachable (ReadTimeout))` |
| demo-2026-10-29T170406.json | FAIL | 30 | `alert_intake`: webhook answered 503 (reason not kept) |

Count: **0 of 3**. (I first reported 164933 as "pass 2 of 3"; I had not seen 163823. Read the
files, not the messages.)

**Finding: the machine, not the code.** After 170406, read-only checks on the Mac showed:
- a restart storm: 6 services failed their liveness probe in the same second, then startup
  probes failed and the kubelet killed them again; kube-controller-manager 95 and kube-scheduler
  96 restarts in 9 days, `etcdserver: request timed out` -> `stopped leading`;
- node CPU pressure (PSI `some avg10`) 39%, load 47 on 12 vCPUs, memory/io pressure low;
- `pmset -g therm`: `CPU_Speed_Limit = 39` with `CPU Power notify`, no thermal warning - the CPU
  was power-limited. Power: AC, a **60 W** charger, battery "Service Recommended". After a
  restart of the node (Docker/Mac): limit 70, 0 pod restarts, control plane quiet for 10 min. Mac swap 11.4 of 12 GB.
- The VM's "idle %" hid it (a throttled host gives the VM less CPU than it shows); PSI did not.
- Not done, on purpose: no probe or timeout was loosened. That would turn a starved machine into
  green checks. A probe change may still be right later, as its own decision.
- The two 503s are probably the api's 5 s upstream timeout or a restarting incident-service; the
  run threw the reason away, so this is a hypothesis, not a fact.

**fix4 (tests/demo only):** the webhook's refusal reason is printed and stored
(`webhook answered 503: <detail>`); every result file records `machine.before/after` (pmset
CPU_Speed_Limit, node CPU pressure + load, kube-system and app pod restarts, read-only:
`pmset`, `docker exec ... cat /proc/...`, `kubectl get pods`) and `machine.warnings`; `MACHINE:`
lines print before the VERDICT. The verdict is the steps' alone; whether it COUNTS is the gate rule below. `started_at` was the END time;
it is the start now (+ `finished_at`).

**The gate rule from now on (owner, 2026-10-29, fixed BEFORE the gate restarts):** a
real-model PASS counts only if **no pod restarted or was replaced during the run** (kube-system
or app; a restart changes the system under test mid-run). CPU speed limit and CPU pressure are
recorded warnings, not disqualifying: a slow machine that still passes is a real pass. Pod
restarts that cannot be read = not counted. Code: `demolib.counts`; every result file has
`gate.counts` + `gate.why`, and the run prints `GATE: COUNTS` or `GATE: does NOT count - why`.
3 in a row = 3 consecutive real-model runs that each print `GATE: COUNTS`.

### After fix4 (main f6c9b04)
| file | verdict | gate | machine |
|---|---|---|---|
| demo-2026-10-29T184743.json | PASS | COUNTS | speed limit 58 |
| demo-2026-10-29T190036.json | FAIL | - | `webhook answered 503: incident-service timed out.`; speed limit 31, CPU pressure 59% |
| demo-2026-10-29T191257.json | PASS | COUNTS | speed limit 48 |

Count: 1 (not in a row). fix4 gave the reason; it was ours, not only the machine's:
- INC-10332, 10334, 10336 exist (OPEN, SEV2, `service:alerts`): **every FAIL created its
  incident** - the api answered 503 for requests that were applied.
- Tempo traces (4 slowest alert requests): the "2 s" catalog timeout waited 2.2-4.0 s (httpx
  limits each step, not the call); the catalog answered its first request in 2.1-21.1 s while
  ready; 1.3-1.5 s passed before incident-service started (starved node); DB work 0.3-0.7 s.
  The 16:38 FAIL missed the api's 5 s by 0.02 s; the 16:48 PASS had 0.2 s left.
- No lock: the "17 s after the incident" was the catalog finishing work no one waited for.

**fix5 (ADR-044 amendment; owner chose A+B+C+D):** A. a total 1.5 s catalog deadline + a budget
test; B. `CatalogWarmup` before ready + DispatcherServlet at start (Java: verified on the Mac and
in CI only); C. a timed-out POST says it may have been applied; D. `alert_intake` requires SEV1
by the tier-1 rule. New code: **the gate restarts at 0 of 3.**
Open: the 3 OPEN SEV2 incidents on checkout-api from the FAILs stay as the record; the history
agent may see them (the later runs passed with the same evidence picture - watched).

## 8b. Next
Apply fix5, `make catalog-build` on the Mac, then 3 in a row by the rule above.

## 9. Verify
- [x] 29a rehearsal on the Mac (fake): REHEARSAL-OK, `make demo-clean` removed 0 rows.
- [x] One real-model run: PASS 13/13 (not counted: the script is not complete yet).
- [x] 29b on the Mac: CI green (PR #47), `alert_intake` PASS with SEV1 + "tier 1", netpol 0 failed with incident-service -> service-catalog open, 0 restarts.
- [x] 29c on the Mac: CI green (PR #48), `removed_shown` PASS ("0 removed" on fakes); real-postmortem rate measured (8/70).
- [x] 29d on the Mac: CI green (PRs #49, #50), `evidence_graph` PASS, browser graph test passed (not skipped).
- [x] 29e on the Mac (PR #55, main f471ac2; `mtti` PASS AI-only in demo 161620 + 164933): the paid AI run (v1 + v2, 0 invalid); manual session recorded and declared invalid by the owner (ADR-047 amendment); `make mtti INVALID=...` report committed; `make demo` shows `mtti` PASS as AI-only on real models (checked in the gate runs).
- [ ] 3 real-model PASS in a row, each after `make demo-fresh`, files in `data/demo/results/`.
