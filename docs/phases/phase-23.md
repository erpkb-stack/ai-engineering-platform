# Phase 23 - Observability (full architecture.md §19, apps on kind; ADR-035)

Status: **23a BUILT in the sandbox (2026-10-17), Verify on the Mac open.** 23b (the other 5
dashboards, eval_runs, the Kafka lag gauge, measured limits) after 23a is verified. The design
below was approved with "go" (dashboards for 23a: Platform health, Investigation funnel, LLM cost).

## 0. Gate and scope
- **Gate: passed.** Phase 22a verified on the Mac and merged (PR #25, main 0cc19ce). 22b deferred.
- **Owner decisions (2026-10-17):** the FULL §19 scope (traces, metrics, logs in Loki, the
  dashboards) and the apps on **kind**. I recommended the smaller scope on compose; the owner
  chose this. The costs are measured below, not guessed.
- **The honest reason:** one investigation crosses 8 services, a background run, Kafka and the
  Java catalog. Today: JSON logs with a correlation_id, and LLM latency/tokens/cost per call in
  Postgres (`model_usage`). No traces, no metrics. Debugging = grep the id in 8 pods' logs.

## 0b. Found in the code during the need check (real gaps, not wishes)
1. **A resumed investigation has no correlation id.** The first run inherits the POST request's
   context (`asyncio.create_task` copies contextvars). After an orchestrator restart,
   `resume_pending()` runs with no request: its logs have no `correlation_id`, and every call it
   makes sends an empty `X-Correlation-ID`. The run we most need to debug is the one we cannot
   follow. Nothing stores the id with the investigation.
2. **Kafka carries the correlation id, but not a trace context.** The outbox writes
   `correlation_id` into the event headers; the audit consumer cannot join the producer's trace.
3. **`eval_runs` exists but nothing writes it.** `make rag-eval` writes JSON files only. A "RAG
   quality" dashboard has no data source today.
4. **kubelet probes run every 5-10 s on 9 pods.** Traced as requests, they would be ~1.5 spans/s
   of noise, more than all real traffic.

## 0c. Measured on the Mac (2026-10-17, probe: default configs, NO traffic)
| Container | Memory (10 samples after 2 min) | CPU |
|---|---|---|
| grafana 12.3.9 | 104-122 MiB | ~0.9 % |
| loki 3.6.12 | 50-58 MiB (still rising slowly) | ~1.5 % |
| otel-collector-contrib 0.154.0 | 43 MiB | ~0.9 % |
| tempo 2.10.0 | 29 MiB | ~0.6 % |
| prometheus 3.5.0 | 28-29 MiB | ~0.1 % |
| **total** | **~263 MiB** (architecture.md guessed ~1 GB) | **~4 %** |
- Idle and EMPTY. Prometheus grows with series, Loki and Tempo with ingest. The limits come
  from a measurement run with all smokes (the Phase 21 method), never from this table.
- Budget: compose infra ~1.4 GiB + cluster with apps 1.84 GiB + this stack ~0.3-0.6 GiB under
  load (to be measured) = ~3.5-4 GiB of 7.75.

## 1-2. Objective and reason
Every investigation is ONE trace from the api to the last LLM call, including the background
run, a resume after an orchestrator restart, the Kafka hop to audit and the Java catalog; every
log line carries its trace id and is searchable in Loki; RED metrics per service in Prometheus;
dashboards, each answering one named question. No prompt, token, password or user text in any
span, metric or log attribute (a test). KPI: none directly - time-to-find for the Phase 24+
demos; the interview story "show me the trace".

## 3. Architecture
```
kind (apps, 1 replica each)                                      compose profile `observability`
  api ─ orchestrator ─ agents ─ tool-gateway ─ rag ─ llm-gateway  ──OTLP/HTTP──> otel-collector:4318
  incident-service ─(Kafka, traceparent in headers)─> audit                         │ batch, memory_limiter,
  service-catalog (Java agent)                                                      │ redaction (attributes)
        stdout JSON logs stay (kubectl logs)                                        ├─> tempo   (traces)
                                                                                    ├─> prometheus (OTLP in)
                                                                                    └─> loki    (OTLP logs)
  grafana (127.0.0.1:3000) ── datasources: Tempo, Prometheus, Loki, Postgres (read-only login)
```
- **The backends AND the collector run in compose** (profile `observability`), like Postgres and
  Kafka: the stand-in for managed backends. One address `otel-collector:4318` works from kind
  pods (via aeoi_default, like postgres) AND from the Phase 21 compose apps. The 22a generator
  only adds one egress rule per app (compose subnet, port 4318). [Prod] a collector gateway in
  the cluster; recorded, not built.
- **Python:** `libs/observability` gets `setup_telemetry(service)`: OTel SDK 1.45, OTLP/HTTP
  (:4318), instrumentation for FastAPI, httpx and psycopg 3 (our driver); Kafka is propagated by
  hand (we own the outbox and the consumer). structlog lines go to the OTel log pipeline AFTER
  redaction, and stdout JSON gains `trace_id`/`span_id`. Off unless `AEOI_OTEL_ENDPOINT` is set:
  tests and host runs stay unchanged.
- **Investigation continuity (gap 1):** at creation the orchestrator stores the W3C
  `traceparent` and the correlation id with the investigation (migration: 2 columns). A run - first
  or resumed - starts its span from the STORED context and restores the correlation id. A resume
  is a new span in the SAME trace, with an event "resumed after restart".
- **Kafka (gap 2):** the outbox row stores `traceparent` at WRITE time (inside the request), the
  relay publishes it as a header, `aeoi_messaging.Consumer` extracts it: the audit span is a child
  of the request that changed the incident, not of the relay loop.
- **Java:** the OTel Java agent, added in the catalog image's build stage from Maven Central
  (pinned version + checksum), switched on by env only.
- **Not traced:** `/health/*` (gap 4) - the instrumentation excludes those routes.
- **Sampling:** 100 % (dev traffic is tiny). [Prod] tail sampling in the collector.

| Guarantee | Enforced by | Test |
|---|---|---|
| One investigation = one trace | stored traceparent; HTTP + Kafka propagation | Mac: multi-smoke -> one trace id across the 8 services + catalog + audit |
| A resume stays in the trace and has its correlation id | run starts from the stored context | unit (fake exporter); Mac: kill the orchestrator mid-run, resume, same trace |
| No secret or user text in telemetry | no header/body capture; collector deletes `http.request.header.*`, `db.statement` values are parameters only; a deny-list processor | unit: drive requests with planted secrets through an in-memory exporter, assert none appears in any span/log/metric |
| Telemetry never breaks a request | BatchSpanProcessor, non-blocking export, collector down = dropped data | unit: exporter that raises; Mac: stop the collector, smokes still pass |
| Health probes are not traced | excluded URLs | unit |
| Network rules stay exact | one generated egress rule (subnet:4318) per app | the 22a netpol matrix gains `otel-collector:4318` (allowed for all apps) |

## 4. Dashboards - each answers ONE question (owner: keep or cut each)
| Dashboard | The question | Source |
|---|---|---|
| Platform health | which service is failing or slow right now? | Prometheus (RED from http server metrics) |
| Investigation funnel | how many investigations start, finish COMPLETE/PARTIAL/FAILED, and how long? | Postgres (investigations) |
| Agents | which agent fails or is slow, per investigation? | Tempo (span metrics) + Postgres (agent_executions) |
| LLM cost/tokens | what did each route/model cost today, and per investigation? | Postgres (`model_usage` - exact, not copied into Prometheus) |
| Tool gateway | which tool is denied, failing or slow, for whom? | Prometheus + Loki |
| RAG quality | did retrieval quality move between eval runs? | **needs gap 3 fixed** (rag-eval writes `eval_runs`), else cut |
| Kafka lag | is a consumer falling behind, is the DLQ growing? | Prometheus (gauge from `aeoi_messaging` lag) |
| KPIs | MTTR, time to first hypothesis, approvals waiting | Postgres |
Dashboards are files in the repo (provisioned), never clicked together. Each panel names its
query; a unit test loads every dashboard and checks every datasource uid and metric name exists.

## 5. Delivery: two builds, one Verify each (my default - say if you want one)
- **23a** - pipeline: collector + 4 backends, Python + Java instrumentation, gaps 1, 2, 4, the
  secret test, Platform health + Investigation funnel + LLM cost dashboards.
- **23b** - the other 5 dashboards, gap 3 (eval_runs), Kafka lag gauge, measured limits.
Reason: a broken trace in 23a would make every 23b dashboard wrong; one Verify for everything
mixes pipeline bugs with dashboard bugs (the same reason 22 was split).

## Defaults I chose (say if you disagree)
1. Backends + collector in compose (one address for kind and compose apps).
2. OTLP logs from the SDK, not a node log agent: a DaemonSet with hostPath would break the
   namespace's Pod Security "restricted".
3. Retention: traces 24 h, logs 24 h, metrics 7 d, local volumes (`make obs-down` keeps them,
   `make obs-clean` deletes them).
4. Grafana on 127.0.0.1:3000, admin password from a secrets file (generated by `make setup`),
   Postgres datasource with a NEW read-only login `grafana_ro` (SELECT on the dashboard tables only).
5. Versions: chosen from the current releases at build time and pinned (Tempo 3.0 is a major
   release; I will read its notes before choosing it over 2.10).

## 6. What was built (23a)
```
libs/observability/.../telemetry.py   setup, DropRootClients sampler, ScrubbingExporter, EdgePropagator,
                                      continue_from(), headers_with_context(), structlog -> OTel logs
libs/observability/.../logs.py        trace ids on every line; httpx/httpcore request lines off (they
                                      printed full URLs unredacted - found by the secret test)
libs/web/.../app.py, middleware.py    FastAPI instrumentation (no health, no ASGI sub-spans, no query);
                                      aeoi.correlation_id on the request span
libs/messaging/.../consumer.py        a CONSUMER span continuing the producer's traceparent + its correlation id
services/orchestrator (engine, api, store)  stored trace_context + correlation_id; every run continues them;
                                      resume_pending in a span of its own
services/incident-service events.py   the outbox row stores the traceparent of the REQUEST
services/api main.py                  the public /api/ edge ignores a client-named traceparent
libs/db migration 0026 + users.py     2 columns; role obs_reader (SELECT on 19 named columns) + login grafana_ro
docker-compose.yml                    AEOI_OTEL_ENDPOINT switch; profile observability: otel-collector,
                                      tempo(+init), prometheus, loki, grafana; 127.0.0.1 ports only
infrastructure/observability/         collector (filter health, drop exception text, strip headers/query,
                                      redact token shapes), tempo, loki, prometheus, grafana provisioning,
                                      3 dashboards (files, read-only in the UI)
infrastructure/docker/catalog.Dockerfile  OTel Java agent 2.26.1 (only when the switch is on; its log export off)
scripts/k8s_manifests.py              otel-collector is compose infra: one egress rule per app (subnet:4318)
scripts/obs_smoke.py, Makefile        obs-up, obs-down, obs-clean, obs-smoke [RESUME=kind|compose]
```
**Verified in the sandbox with the REAL binaries** (collector 0.154.0, Tempo 2.10.0, Loki 3.6.12,
Prometheus 3.5.0 from GitHub releases; Grafana could not be fetched): every config validates; a
request + a resumed run landed as ONE trace in Tempo; the log lines with trace_id and
correlation_id in Loki; `http_server_request_duration_seconds` with `service_name` in Prometheus;
the platform dashboard's PromQL and LogQL run; the collector removed an exception message and a
health span sent around the SDK. Mutation check: 19/19 breaks of the telemetry code caught.

**Independent review (before the Mac), fixed:** exception messages and status text reached Tempo
(psycopg puts whole rows there) -> scrubbed in the SDK AND the collector; span->logs link would
find nothing (trace_id is structured metadata) -> custom query; Grafana's default log file on a
read-only root -> console only; a client could pick the trace id or switch tracing off at the
edge -> EdgePropagator; the Java agent's health spans and unredacted log export -> filtered / off;
two Maven stages on one cache -> own cache id; the smoke's leak check scanned only log bodies ->
whole export + every service token; the probe tested a COPY of the engine -> the real Engine._run.

## 7. Fixes found on the Mac
- fix1 (`make obs-up`): `tempo-init` ran `chown` inside `grafana/tempo:2.10.0`, which has no
  `chown` ("exec: chown: executable file not found"). The sandbox e2e ran the binaries, not the
  images, so it could not see this. Now `redis:7.4-alpine` (already pulled; busybox `chown`).
  Guard: `test_one_shot_helpers_use_an_image_that_has_their_tool` (a list of tools seen working
  per image - a regression guard, not proof).

- fix2 (`make obs-up`, after fix1): `--wait-stack` crashed with `RemoteDisconnected` while Tempo
  was starting (Docker Desktop's port proxy accepts the connection, then closes it). The probe
  caught HTTP errors only. Now every readiness probe (`status()`) counts a refused or closed
  connection as "not ready" and keeps waiting. Test: a socket that accepts and closes, then a
  closed port.

- fix3 (`make obs-up`, after fix2): `--wait-stack` crashed with `JSONDecodeError`: Tempo and Loki
  answer `/ready` with plain text ("ready"), and `http()` parsed every 200 body as JSON. Now a
  body is JSON when it is JSON, else text. Root cause of fix2 + fix3: the sandbox e2e checked
  the DATA path (traces, logs, metrics) but never ran `--wait-stack` itself. Now: a test runs
  the real `wait_stack()` and `ready()` against a local server (text 200, Loki's 503 "not ready"
  text, a dropped connection, JSON), and `--wait-stack` was run against the real Tempo 2.10,
  Loki 3.6.12 and Prometheus 3.5 binaries (all "ready" after ~30 s; Grafana not in the sandbox).

- fix4 (`make obs-smoke`): "0 traces" after a 73 s investigation. Cause: `.env` had no
  `AEOI_OTEL_ENDPOINT` (a Verify step was skipped), so every pod ran with telemetry OFF - not a
  code bug, but the smoke should have said so in one second. Now `obs-smoke` refuses at once
  when the switch is off, `obs-up` warns, and "0 traces" also says whether Tempo has ANY
  service (none = the pods export nothing; some = the query or the run is wrong).

- fix5 (`make obs-smoke`, 2026-10-19): one run found the trace but "0 log lines in Loki". Tempo's
  SEARCH drops a trace id's leading zeros ("636db42c..." = 31 hex; ~1 run in 16); our logs carry
  the 32-hex W3C id, so the Loki lookup missed. Now ids are padded to 32. Also: the "which
  services does Tempo know" call used the v1 tag API, which answers [] for
  `resource.service.name` - only `/api/v2/` lists them (fix4's diagnostic line would have
  said "NO traces" even with traces). Both shapes reproduced against the real Tempo 2.10 binary.

- fix6 (`make obs-smoke`, 2026-10-19): one investigation trace carried 11 correlation ids. Loki
  showed it exactly: the request's id on api, orchestrator, agents, tool-gateway, rag and
  incident-service, plus 10 ids on llm-gateway ONLY (one per model call). Cause, older than 23a:
  `libs/llm-client` sent no `X-Correlation-ID`, so the gateway minted its own. Now the client
  forwards the caller's id (new dependency: `aeoi-common`, a workspace lib). The smoke now
  FAILS on any other id and names the service (it only checked that ours was present).
  Also seen: the Java agent exports traces and metrics (`service-catalog` in Tempo and in
  Prometheus after `make catalog-smoke`); audit is in the trace but logs no line with it.

- fix7 (`make multi-smoke`, 2026-10-20): one 500 on "create incident". The api's own log line
  (found by its correlation id) said `ReadError`: incident-service dropped the connection and
  never logged the request - most likely a pooled keep-alive to a pod `k8s-apply` had just
  replaced (the re-run passed 26/0). The proxy handled connect errors and timeouts, not a
  dropped connection, so it fell through to a 500. Older than 23a (Phase 4). Now: a 503 problem
  that says "the request may have been applied: retry with the same Idempotency-Key"; a GET is
  retried once (safe), a POST never. Also: the "p95 by route" table showed `NaN` for a route with
  no requests in 15 min (`> 0` drops it; checked with promtool 3.5).
- Measured on the Mac (2026-10-19/20), not fixed: the Java catalog has a slow tail - Tempo found
  catalog server spans of 1.1-1.6 s in 6 h; the catalog tool's deadline is 2 s, so one
  risk-smoke call timed out (PARTIAL; the re-run passed 19/19). New since 22a (no agent then):
  the OTel Java agent is the first suspect, not proven. Open item: compare with the agent off.
- Dashboards seen on the Mac: real data in all 3. The api 5xx spikes (18 %, 42 %) line up with
  the two `RESUME=kind` runs (the smoke polls while the only orchestrator pod is deleted -
  replicas: 1 by decision). The 2 INCONCLUSIVE runs (~$0.007) are `make smoke`'s evidence-less
  incident.

- fix8 (the commit, not the Mac Verify): gitleaks blocked the commit - `telemetry_probe.py` held
  a JWT-shaped literal (the planted fake the secret test needs). Now the same value is built at
  runtime from its decoded parts. Checked with gitleaks 8.30.1 (the hook's version) and the repo
  config over all 52 files of the change: no leaks; the old literal is still caught.

## 8. Verify (on the Mac) - result 2026-10-17..20
Result: **PASSED with the gaps named below** (owner's Mac, kind cluster, 9 apps, 7 fixes).
- [x] Unpack, `make check`: 1170 -> 1178 passed after fix1-fix7 (each fix added its test)
- [x] `make db-upgrade` (0026), `make db-users` (`grafana_ro` + its password file)
- [x] `.env` switch - first SKIPPED, which produced "0 traces" (fix4 makes that a 1-second refusal)
- [x] `make images`, `make k8s-apply`: 9/9 available
- [x] `make obs-up`: 4 backends ready (after fix1 tempo-init image, fix2/fix3 readiness probe)
- [x] `make obs-smoke` 13/13: ONE trace across 8 services (catalog when called), 82-90 log lines
      from 7 services, ONE correlation id (after fix6), metrics, no token in 9; 615-631 spans
- [x] `make obs-smoke RESUME=kind` 14/14 (3 runs): orchestrator pod deleted mid-run, 2
      `investigation.run` spans in ONE trace; 660-662 spans
- [x] Smokes with telemetry on: smoke 12/12, multi-smoke 26/26 (one transient 500 -> fix7),
      tools-smoke 16/16, approval-smoke 23/23, risk-smoke 19/19 (one run PARTIAL: catalog slow
      tail, see 8b), catalog-smoke 6/6. NOT run in 23a: kc-smoke (Keycloak) - unchanged code path
- [x] `make k8s-netpol-test`: 113-121 passed, 0 FAILED; `otel-collector:4318` open for every app
- [x] Java agent: `service-catalog` in Tempo and in Prometheus after `make catalog-smoke`
- [x] Grafana: the 3 dashboards show real data (screenshots 2026-10-19); the error-log panel shows
      the fix7 500. NOT checked by hand: "span -> its log lines" (the smoke proves the same
      trace_id is in Loki; the Grafana link itself was not clicked)
- [x] Collector down = no harm: `make obs-down`, `make smoke` 12/12, `make obs-up`
- [ ] Memory under load: NOT measured. One `apps-peaks` run AFTER the smokes (134 samples):
      Tempo 189, Loki 148, Grafana 140, Prometheus 106, collector 64 = 647 MiB for the stack,
      kind node 2,510 MiB, total 4,034 MiB of 7.75 GiB. Already above ADR-035's 0.3-0.6 GiB
      guess; the loaded peak is a 23b task (limits stay provisional until then)

## 8b. Open items (each its own later change)
- Java catalog slow tail: server spans of 1.1-1.6 s (Tempo, 6 h); the tool deadline is 2 s, one
  risk-smoke call timed out. New since 22a; the OTel Java agent is the first suspect, NOT proven.
  Next: the same smoke with the agent off, and the slow trace's child spans.
- Mac -> api after a Docker restart (22a): twice `make smoke` gave 000 (timeout) until the node
  was recreated. Hypothesis, NOT proven (the evidence was lost both times): published ports then
  arrive from the `aeoi_default` gateway, `dev-host-ingress` allows only the `kind` gateway.
  BEFORE recreating the node, run: `curl -sS -m 5 http://127.0.0.1:8000/api/v1/health` (timeout?),
  `docker inspect aeoi-control-plane` (its two networks + gateways), and inside the node
  `conntrack -L | grep 30800` while a curl runs (the real `src=`).
- `make k8s-netpol-test` has no Mac -> api check: it said 0 FAILED while the only path the owner
  uses was broken. Add one (pod-to-pod is not enough).
- audit is in the trace but logs no line with its trace_id.
- `make catalog-smoke` says "make run-catalog" when 8010 does not answer - wrong under kind.
- the Grafana "span -> logs" link is untested by hand; the dashboards' generator is not in the
  repo (the JSON files are the source; a test checks them).
- stdout logs still carry a traceback's exception VALUE (Phase 4 behaviour, for whoever reads the
  pod log); it is never exported. Decide: keep, or type-only on stdout too.
- Trace size: every checkpoint write is a psycopg span; the smoke prints the span count (Tempo
  drops spans above 5 MB per trace).
- A consumer span stays open while the consumer waits out a dependency outage (one long span).
- The resource/limits of the 5 containers are PROVISIONAL until the LOADED peaks are measured (23b).
- `DropRootClients` drops a root client call: work started outside a request must open a span of
  its own (done for resume_pending; a new background job must do the same).

## 8c. Test plan
- Sandbox: the secret test, propagation (HTTP, Kafka, stored context on resume), probe
  exclusion, exporter failure, collector config validated (`otelcol validate` is not available
  here - config schema checked by a test), dashboard files, generator rule + netpol graph.
- Mac (Verify): stack up; `make k8s-apply`; the smokes unchanged; one investigation = one
  trace; kill + resume = same trace; collector stopped = smokes still pass; memory peaks.

## 11. Known risks
| Risk | Why | Plan |
|---|---|---|
| Memory growth under load | measured idle only | Phase 21 method: high limits, smokes, peaks, then limits |
| psycopg / httpx instrumentation gaps | library versions | the sandbox e2e covered httpx + FastAPI; psycopg spans are seen first on the Mac |
| Auto-instrumentation records too much | defaults change between versions | the secret test runs on every `make check` |
| Kind + 5 more containers on a laptop | ~1 core idle already | measure; Grafana is the only one with a UI cost |
