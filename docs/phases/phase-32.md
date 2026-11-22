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

## 7. Not in 32a
Real deploys and diffs (32b: GitHub), slides + backup video (32c), any buyer's data (pilot,
after 32a), replicas, Kafka ACLs.
