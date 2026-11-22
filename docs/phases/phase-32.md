# Phase 32 - Real data (buyer demo)

Status: **PLAN 2026-11-21, not started.** Owner (2026-11-21): the demo is for a **real buyer**,
start with 32a. Section 6 decided by the owner 2026-11-21: "Accept all four" = 1a, 2a, 3a, 4.

## 0. Gate
Phase 31a docs on PR #61 (not merged; mocks postponed by the owner to after the project).
Before code: #61 merged, `main` pulled, this plan's decisions answered.

## 1. Objective and business reason
A buyer asks one question first: "does it work on MY data?" Today the answer is no. Every
log, metric, deploy and diff the agents see comes from `DevDataAdapter` (seeded `devdata`
tables). 32a: **one real fault on AEOI's own running system, investigated from its real
telemetry (Prometheus + Loki), the result measured and recorded.** No buyer data needed; it
proves the adapter swap and gives a story a buyer can believe. Expect accuracy to DROP versus
the planted 14/17: the code rules were built on synthetic data. A drop we measure is the point.

## 2. Need check (read in the code, 2026-11-21)
| fact | where | what it means for 32a |
|---|---|---|
| Agents see only the tool contract; one adapter serves logs + metrics | `aeoi_tools/schemas.py`, `adapters/devdata.py:52,94` | swap per tool, agents unchanged |
| Egress allow-list for every HTTP tool | `adapters/http.py` | Prometheus + Loki origins go in the list |
| `query_metrics` = service + one of 6 metric names + window -> one series + stats | `schemas.py:18,134` | a metric -> PromQL template map fits |
| Prometheus holds ONLY `http_server_request_duration_seconds` (OTLP push, `scrape_configs: []`) | `prometheus.yml`, `scripts/obs_dashboards.py` | real: `http_5xx_per_min`, `p95_latency_ms`, `requests_per_sec`. **No source:** `cpu_util`, `db_pool_utilization`, `db_connections_active` |
| A missing series is a NOTE, never a fact | `metrics/agent.py:9-11` | the 3 missing metrics degrade honestly |
| Logs carry no content: an exception keeps its TYPE only | ADR-035, `otel-collector.yaml` | log evidence = level + event name + exception type; thinner than the seed |
| `search_logs` returns exact counts per `error_code` | `schemas.py:126`, `log_analysis/agent.py` | needs a LogQL `count_over_time ... by (error_code)`; `error_code` := exception type or event name |
| Agents hard-code `SOURCE = "devdata.*"` | `metrics/agent.py`, `log_analysis/agent.py:48` | provenance must come from the gateway's answer |
| Cause kinds: `change`, `saturation`, `traffic`, `alternative` only | `reasoning/candidates.py` | a dependency outage (pg down) has NO kind -> pick the fault to fit, or add a kind (section 6) |
| AEOI's own 9 services are not in the catalog seed | grep `data/generated`, `scripts/synth` | triage would give SEV2 "tier unknown" -> add catalog entries |
| No Alertmanager | `docker-compose.yml` | the alert is still a signed webhook sent by the harness (say so) |
| `make load` refuses real models | `tests/load/loadlib.py:44` | correct; the fault needs its own no-LLM traffic script |
| Prometheus 9090, Loki 3100 in compose | `docker-compose.yml:464,480` | kind tool-gateway needs one generated egress rule each |

## 3. The faults (rules fixed in a file BEFORE the first run, like ADR-040)
- **F1 traffic surge (cause kind `traffic`):** a read-only surge script on one AEOI service's
  read routes (no LLM calls). Phase 27 measured overload = slow 503s, so `requests_per_sec`,
  `p95_latency_ms` and `http_5xx_per_min` should all move. Expected: top cause = traffic on the
  surged service, VALIDATED. Quiet baseline of >= 30 min before the surge (the anomaly maths
  compares against the pre-window baseline).
- **F2 Postgres stopped 60 s (`make chaos EXP=pg`, owner runs it):** no cause kind fits.
  Expected: **no wrong HIGH claim** (INCONCLUSIVE or a weak `alternative`). A negative case: it
  tests that the system says "I don't know" on real data.
- Per run, recorded: identified yes/no, time to finish, cost, which evidence kinds were empty,
  the machine block (as in `data/demo/results/`). Results: `data/realdata/results/`.

## 4. What 32a builds
1. `PrometheusMetricsAdapter`: PromQL template per supported metric; `service_key` ->
   `service_name` label map; the 3 unsupported metrics return no series (NOTE).
2. `LokiLogsAdapter`: level, `error_code`, `contains` (line filter), exact counts by code.
3. Per-tool backend by config (`AEOI_TOOLS_METRICS_URL`, `AEOI_TOOLS_LOGS_URL`; unset = devdata,
   so every existing test and the planted demo stay as they are); egress list; generated netpol
   rule; evidence provenance from the gateway.
4. Catalog entries for AEOI's 9 services (tier, dependencies).
5. `make realdata-run FAULT=f1|f2`: baseline wait -> fault -> signed alert -> investigation ->
   result file. F2's fault step is the owner's (it stops a container).
6. Tests: each adapter against a recorded Prometheus/Loki answer (unit); contract tests shared
   with devdata (same In/Out); a test that `unset` keeps devdata.

## 5. Verify
- [ ] both adapters pass the shared contract tests; devdata path unchanged (CI green)
- [ ] F1 x3 and F2 x2 on real models, each result file committed (FAILs too)
- [ ] F2: 0 wrong HIGH claims
- [ ] a table in this file: planted 14/17 vs real F1 result, and what evidence was missing
- [ ] CLAUDE.md + roadmap row 32

## 6. Owner decisions (DECIDED 2026-11-21: 1a, 2a, 3a, 4 - the recommended options)
1. **Missing metrics:** (a) accept 3 of 6 [recommended for 32a: less to build, honest NOTEs];
   (b) add OTel process metrics for `cpu_util`; (c) add a postgres exporter for
   `db_connections_active`. Each of (b)/(c) is a container or SDK change on a power-limited laptop.
2. **F2 with no fitting cause kind:** (a) keep it as the negative case [recommended];
   (b) add a `dependency` kind (a new reasoning rule: needs its own eval, like R1 in 25e).
3. **Alert source:** (a) harness sends the signed webhook [recommended for 32a]; (b) add
   Alertmanager + one Prometheus rule (the full real loop; more memory) - candidate for 32c.
4. **Paid runs:** 5 real-model runs, estimate from the 25b rate $0.04-0.10 each (~$0.50 total);
   cost per run recorded, not extrapolated.

## 6b. Real check of both adapters (Mac, 2026-11-22, read-only; kind tool-gateway as the traffic source)
`make obs-up`, 90 + 50 real requests to the kind tool-gateway (401s), then the COMMITTED
adapters called against the real Prometheus :9090 and Loki :3100 (script in the session
scratchpad, not committed).
- Names match what the adapters assume: Prometheus `service_name`,
  `http_response_status_code` (also `error_type`, `http_route`); Loki stream label
  `service_name`, metadata `severity_text`, `trace_id`, `status`, `path`. OTel exports every 15 s.
- Values: requests_per_sec peak 2.40 (raw instant rate 2.00 over a different window), p95 18.5 ms,
  http_5xx_per_min 0 WITH traffic (the zero-not-empty rule works), cpu_util NO SERIES; logs INFO+
  168 lines, exact counts.
- **Finding 1 - a counter is born non-zero:** a series' first sample already holds every request
  before it (the 401 series appeared at 40, so its rate was 0). Requests before a series' first
  sample are invisible: the FIRST 5xx burst on a route after a pod start is undercounted at its
  onset. Not fixed; F1 must be read with it in mind (the onset may look later than it was).
- **Finding 2 - real 5xx leave no WARN log line:** the access line `http_request` is logged at
  INFO for every status, 500 included (`libs/web/src/aeoi_web/middleware.py:85`); the log agent
  asks WARN+ (`log_analysis/agent.py:149`); no warning was found on the api's overload path.
  So in F1 the real log evidence is probably EMPTY. Owner decision (below, 6c).
- **6c (owner, 2026-11-22): fix the code, not the adapter.** `http_request` is logged at
  WARNING for a 5xx, INFO otherwise (4xx = a client's mistake, noise at WARN).
  `libs/web/tests/test_web.py::test_a_5xx_access_line_is_a_warning_and_a_4xx_stays_info`
  (failed on the old code). Needs `make images` before a kind run uses it.
- Not proven yet: a real non-zero 5xx count (no 5xx traffic was made).

## 6d. Built: catalog entries + evidence provenance (2026-11-22)
- `services/tool-gateway/config/catalog-aeoi.json`: AEOI's 9 apps + aeoi-postgres/kafka/keycloak
  (12), `depends_on` = the compose call graph (telemetry backends left out), tier 1 = api,
  incident-service, aeoi-postgres. Read ON TOP of the generated Northwind file (no re-seed, the
  planted demo data unchanged); a key in two files is refused. `make catalog-import` merges both
  (`scripts/catalog_merge.py`: the Java import deletes and replaces); the parity smoke reads both.
  `tests/unit/test_catalog_aeoi.py` keeps the file equal to the graph.
- `ToolResult.source` (api contract + console types regenerated): the dependency that answered.
  The metrics and log agents label evidence with it; devdata keeps its table name
  (`devdata.metric_points`). The new real-backend tests failed on the old agent code.
- For kind: `make images`, `make catalog-import`, then deploy (owner).

## 6e. Built: the run (2026-11-22) - rules fixed BEFORE the first run
- Rules: `tests/realdata/realdatalib.py`, RULES_VERSION 1 (tests: `tests/unit/test_realdata.py`).
  F1 PASS = top live hypothesis is a code-written `traffic` cause on the target, VALIDATED
  (found-but-not-validated is a FAIL, said apart). F2 PASS = no live HIGH claim. Any run whose
  METRIC/LOG evidence is not from prometheus/loki is INVALID.
- Runner: `make realdata-run FAULT=f1|f2 [BASELINE_MIN=30]` (`tests/realdata/run.py`, reuses
  the demo's signed alert, polling, machine snapshot). Baseline 1 req/s on the api's
  `GET /api/v1/incidents` (no LLM behind it); F1 = 40 workers for 180 s on the same route.
  Results: `data/realdata/results/realdata-<fault>-<UTC>.json` (cost from `llm.model_usage`).
- **Change from section 3:** F2 is NOT `make chaos EXP=pg` - the chaos runner refuses real
  models (ADR-040, correctly). The OWNER runs `docker stop aeoi-postgres-1; sleep 60;
  docker start aeoi-postgres-1`; the script only watches the api's readiness body.

### How to run on the Mac (owner)
```
# .env: AEOI_TOOLS_METRICS_URL=http://localhost:9090  AEOI_TOOLS_LOGS_URL=http://localhost:3100
make obs-up && make images && make catalog-import && make k8s-apply
make realdata-run FAULT=f1          # ~35 min: 30 baseline + 3 surge + the investigation
make realdata-run FAULT=f2          # stop Postgres yourself when it says so
```
Verify (section 5): F1 x3, F2 x2, each file committed.

## 7. Not in 32a
Real deploys and diffs (32b: GitHub), slides + backup video (32c), any buyer's data (pilot,
after 32a), replicas, Kafka ACLs.

**32c adds (owner, 2026-11-22): a buyer-facing Overview page in the console** - the 4-6 numbers
a buyer asks about, so the demo stays in the product, not in Grafana: open incidents by
severity, median time to identify, cost per investigation, approvals waiting / approved vs
rejected, share of investigations with a VALIDATED cause. Data already in Postgres (incidents,
approvals, `llm.model_usage`): a few read-only api endpoints + one page. NOT a Grafana rebuild
(no charting/alerting engine). Built AFTER 32a, so its numbers come from real runs.
