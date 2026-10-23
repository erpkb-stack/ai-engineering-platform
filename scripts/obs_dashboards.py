# ruff: noqa: S608 - the "SQL" below is dashboard text over constant table names; no input here
"""The Grafana dashboards, as code (Phase 23a/23b, ADR-035).

The JSON files in infrastructure/observability/grafana/dashboards/ are what Grafana provisions;
THIS script writes them, so a dashboard is reviewed as a diff, never clicked together.
  uv run python scripts/obs_dashboards.py          # write the files
  uv run python scripts/obs_dashboards.py --check  # exit 1 if a file differs (a unit test runs it)
Postgres panels read through `grafana_ro` (role obs_reader): only the columns migrations 0026 and
0027 grant - tests/unit/test_telemetry.py checks every query against those lists.
"""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

OUT = (
    pathlib.Path(__file__).resolve().parents[1] / "infrastructure/observability/grafana/dashboards"
)
PROM = {"type": "prometheus", "uid": "prometheus"}
PG = {"type": "grafana-postgresql-datasource", "uid": "postgres"}
LOKI = {"type": "loki", "uid": "loki"}


def panel(
    pid: int,
    title: str,
    kind: str,
    ds: dict[str, str],
    targets: list[dict[str, Any]],
    x: int,
    y: int,
    w: int = 12,
    h: int = 8,
    unit: str | None = None,
    desc: str = "",
) -> dict[str, Any]:
    defaults: dict[str, Any] = {"unit": unit} if unit else {}
    return {
        "id": pid,
        "title": title,
        "type": kind,
        "datasource": ds,
        "description": desc,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": targets,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {},
    }


def prom(expr: str, legend: str) -> dict[str, Any]:
    return {"refId": "A", "datasource": PROM, "expr": expr, "legendFormat": legend}


def sql(q: str, fmt: str = "time_series", ref: str = "A") -> dict[str, Any]:
    return {
        "refId": ref,
        "datasource": PG,
        "format": fmt,
        "rawQuery": True,
        "editorMode": "code",
        "rawSql": q,
    }


def dash(
    uid: str,
    title: str,
    question: str,
    panels: list[dict[str, Any]],
    refresh: str = "30s",
    tag: str = "phase-23a",
    time_from: str = "now-6h",
) -> dict[str, Any]:
    return {
        "uid": uid,
        "title": title,
        "description": question,
        "tags": ["aeoi", tag],
        "editable": False,
        "schemaVersion": 39,
        "version": 1,
        "refresh": refresh,
        "time": {"from": time_from, "to": "now"},
        "timepicker": {},
        "templating": {"list": []},
        "annotations": {"list": []},
        "panels": panels,
    }


H = "http_server_request_duration_seconds"
platform = dash(
    "aeoi-platform-health",
    "AEOI - Platform health",
    "Which service is failing or slow right now?",
    [
        panel(
            1,
            "Requests per second, by service",
            "timeseries",
            PROM,
            [prom(f"sum by (service_name) (rate({H}_count[5m]))", "{{service_name}}")],
            0,
            0,
            unit="reqps",
            desc="Every HTTP request a service answered (health probes excluded at the source).",
        ),
        panel(
            2,
            "Share of 5xx answers, by service",
            "timeseries",
            PROM,
            [
                prom(
                    f'sum by (service_name) (rate({H}_count{{http_response_status_code=~"5.."}}[5m])) / sum by (service_name) (rate({H}_count[5m]))',
                    "{{service_name}}",
                )
            ],
            12,
            0,
            unit="percentunit",
            desc="A 503 from a service that is down counts at its CALLER.",
        ),
        panel(
            3,
            "p95 latency, by service",
            "timeseries",
            PROM,
            [
                prom(
                    f"histogram_quantile(0.95, sum by (service_name, le) (rate({H}_bucket[5m])))",
                    "{{service_name}}",
                )
            ],
            0,
            8,
            unit="s",
        ),
        panel(
            4,
            "p95 latency, by route (last 15 min)",
            "table",
            PROM,
            [
                {
                    **prom(
                        f"histogram_quantile(0.95, sum by (service_name, http_route, le) (rate({H}_bucket[15m]))) > 0",
                        "",
                    ),
                    "instant": True,
                    "format": "table",
                }
            ],
            12,
            8,
            unit="s",
        ),
        panel(
            5,
            "Error log lines (click a line: its trace)",
            "logs",
            LOKI,
            [
                {
                    "refId": "A",
                    "datasource": LOKI,
                    "expr": '{service_namespace="aeoi"} | severity_text=~"ERROR|FATAL"',
                }
            ],
            0,
            16,
            w=24,
            h=9,
        ),
    ],
)

inv = "orchestrator.investigations"
funnel = dash(
    "aeoi-investigation-funnel",
    "AEOI - Investigation funnel",
    "How many investigations start, how do they end (COMPLETE / PARTIAL / FAILED), and how long do they take?",
    [
        panel(
            1,
            "Started per hour",
            "timeseries",
            PG,
            [
                sql(
                    f"SELECT $__timeGroupAlias(started_at, 1h), count(*) AS started FROM {inv} WHERE $__timeFilter(started_at) GROUP BY 1 ORDER BY 1"
                )
            ],
            0,
            0,
        ),
        panel(
            2,
            "How they ended",
            "barchart",
            PG,
            [
                sql(
                    f"SELECT status, count(*) AS investigations FROM {inv} WHERE $__timeFilter(started_at) GROUP BY status ORDER BY 2 DESC",
                    "table",
                )
            ],
            12,
            0,
        ),
        panel(
            3,
            "Duration of finished runs (p50 / p95, seconds)",
            "stat",
            PG,
            [
                sql(
                    "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM finished_at - started_at)) AS p50, "
                    "percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM finished_at - started_at)) AS p95 "
                    f"FROM {inv} WHERE finished_at IS NOT NULL AND $__timeFilter(started_at)",
                    "table",
                )
            ],
            0,
            8,
            unit="s",
        ),
        panel(
            4,
            "Spend vs budget (USD)",
            "stat",
            PG,
            [
                sql(
                    f"SELECT avg(spent_usd) AS avg_spent, max(spent_usd) AS max_spent, avg(budget_usd) AS budget FROM {inv} WHERE $__timeFilter(started_at)",
                    "table",
                )
            ],
            12,
            8,
            unit="currencyUSD",
        ),
        panel(
            5,
            'Latest investigations (Tempo: { span.aeoi.investigation_id = "<id>" })',
            "table",
            PG,
            [
                sql(
                    "SELECT id, status, started_at, round(extract(epoch FROM coalesce(finished_at, now()) - started_at)) AS seconds, spent_usd "
                    f"FROM {inv} WHERE $__timeFilter(started_at) ORDER BY started_at DESC LIMIT 50",
                    "table",
                )
            ],
            0,
            16,
            w=24,
            h=10,
            desc="Copy an id into Tempo search: the whole run, every service, also after a resume, is ONE trace.",
        ),
    ],
)

mu = "llm.model_usage"
cost = dash(
    "aeoi-llm-cost",
    "AEOI - LLM cost and tokens",
    "What did each model and agent cost, and which investigations cost the most? (exact rows from model_usage)",
    [
        panel(
            1,
            "Cost per hour, by model (USD)",
            "timeseries",
            PG,
            [
                sql(
                    f"SELECT $__timeGroupAlias(created_at, 1h), model AS metric, sum(cost_usd) AS value FROM {mu} WHERE $__timeFilter(created_at) GROUP BY 1, 2 ORDER BY 1"
                )
            ],
            0,
            0,
            unit="currencyUSD",
        ),
        panel(
            2,
            "By model: calls, tokens, cost, p95 latency",
            "table",
            PG,
            [
                sql(
                    "SELECT provider, model, count(*) AS calls, sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens, "
                    "sum(cached_tokens) AS cached_tokens, sum(cost_usd) AS cost_usd, "
                    "percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_latency_ms, "
                    "round(100.0 * count(*) FILTER (WHERE status <> 'OK') / count(*), 1) AS not_ok_or_fallback_percent "
                    f"FROM {mu} WHERE $__timeFilter(created_at) GROUP BY provider, model ORDER BY cost_usd DESC",
                    "table",
                )
            ],
            12,
            0,
        ),
        panel(
            3,
            "By agent: cost and tokens",
            "table",
            PG,
            [
                sql(
                    "SELECT coalesce(agent_name, '(none)') AS agent, operation, count(*) AS calls, sum(cost_usd) AS cost_usd, "
                    f"sum(input_tokens + output_tokens) AS tokens FROM {mu} WHERE $__timeFilter(created_at) GROUP BY 1, 2 ORDER BY cost_usd DESC",
                    "table",
                )
            ],
            0,
            8,
        ),
        panel(
            4,
            "Most expensive investigations",
            "table",
            PG,
            [
                sql(
                    "SELECT investigation_id, count(*) AS calls, sum(cost_usd) AS cost_usd, sum(input_tokens + output_tokens) AS tokens "
                    f"FROM {mu} WHERE investigation_id IS NOT NULL AND $__timeFilter(created_at) GROUP BY investigation_id ORDER BY cost_usd DESC LIMIT 10",
                    "table",
                )
            ],
            12,
            8,
        ),
    ],
)


# ---------------------------------------------------------------- Phase 23b
incs, hyp, appr, tc = (
    "incident.incidents",
    "incident.hypotheses",
    "incident.approvals",
    "tools.tool_calls",
)
P50 = "percentile_cont(0.5) WITHIN GROUP (ORDER BY {x})"
P95 = "percentile_cont(0.95) WITHIN GROUP (ORDER BY {x})"
kpis = dash(
    "aeoi-kpis",
    "AEOI - Incident KPIs",
    "How fast does a declared incident get a first hypothesis and a mitigation, and what is waiting on a human?",
    [
        panel(
            1,
            "Open incidents now, by status",
            "barchart",
            PG,
            [
                sql(
                    f"SELECT status, count(*) AS incidents FROM {incs} WHERE status NOT IN ('RESOLVED', 'CLOSED') "
                    "GROUP BY status ORDER BY 2 DESC",
                    "table",
                )
            ],
            0,
            0,
            desc="Not time-filtered: everything still open, whenever it was declared.",
        ),
        panel(
            2,
            "Time to first hypothesis (investigation start -> first hypothesis)",
            "stat",
            PG,
            [
                sql(
                    f"SELECT {P50.format(x='s')} AS p50, {P95.format(x='s')} AS p95, count(*) AS investigations FROM ("
                    f"SELECT extract(epoch FROM min(h.created_at) - i.started_at) AS s FROM orchestrator.investigations i "
                    f"JOIN {hyp} h ON h.investigation_id = i.id WHERE $__timeFilter(i.started_at) GROUP BY i.id, i.started_at) t",
                    "table",
                )
            ],
            12,
            0,
            unit="s",
            desc="Only investigations that produced a hypothesis are counted (count shown).",
        ),
        panel(
            3,
            "Time to mitigate by an approved action (declared -> executed), by severity",
            "table",
            PG,
            [
                sql(
                    f"SELECT inc.severity, count(*) AS incidents, {P50.format(x='m.s')} AS p50_s, {P95.format(x='m.s')} AS p95_s "
                    f"FROM {incs} inc JOIN (SELECT incident_id, extract(epoch FROM min(executed_at)) AS e FROM {appr} "
                    "WHERE outcome = 'EXECUTED' GROUP BY incident_id) x ON x.incident_id = inc.id "
                    "CROSS JOIN LATERAL (SELECT x.e - extract(epoch FROM inc.created_at) AS s) m "
                    "WHERE $__timeFilter(inc.created_at) GROUP BY inc.severity ORDER BY inc.severity",
                    "table",
                )
            ],
            0,
            8,
            desc="From DECLARED (created_at), not detected_at: the smokes send a fixed historical detected_at. "
            "A mitigation set by hand (status PATCH) is not counted here.",
        ),
        panel(
            4,
            "Time to resolve (declared -> resolved), by severity",
            "table",
            PG,
            [
                sql(
                    f"SELECT severity, count(*) AS incidents, {P50.format(x='s')} AS p50_s, {P95.format(x='s')} AS p95_s FROM ("
                    f"SELECT severity, extract(epoch FROM resolved_at - created_at) AS s FROM {incs} "
                    "WHERE resolved_at IS NOT NULL AND $__timeFilter(created_at)) t GROUP BY severity ORDER BY severity",
                    "table",
                )
            ],
            12,
            8,
            desc="Empty until incidents are resolved (the smokes rarely resolve one) - no data is the honest answer.",
        ),
        panel(
            5,
            "Approvals waiting on a human now",
            "table",
            PG,
            [
                sql(
                    "SELECT id, incident_id, action, source, requested_at, expires_at, "
                    "round(extract(epoch FROM now() - requested_at) / 60) AS waiting_min "
                    f"FROM {appr} WHERE decision IS NULL AND expires_at > now() ORDER BY requested_at",
                    "table",
                )
            ],
            0,
            16,
            w=24,
            h=7,
            desc="Not time-filtered. An expired request becomes EXPIRED and leaves this list.",
        ),
        panel(
            6,
            "Approval decisions: how many, how fast (requested -> decided)",
            "table",
            PG,
            [
                sql(
                    "SELECT CASE WHEN decision IS NOT NULL THEN decision WHEN expires_at > now() THEN 'PENDING' "
                    "ELSE 'EXPIRED (not marked yet)' END AS decision, coalesce(decider_role, '-') AS decider_role, count(*) AS approvals, "
                    f"{P50.format(x='extract(epoch FROM decided_at - requested_at)')} AS p50_s, "
                    f"count(*) FILTER (WHERE outcome = 'EXECUTED') AS executed, count(*) FILTER (WHERE outcome = 'FAILED') AS failed "
                    f"FROM {appr} WHERE $__timeFilter(requested_at) GROUP BY 1, 2 ORDER BY 3 DESC",
                    "table",
                )
            ],
            0,
            23,
            w=24,
            h=7,
        ),
    ],
    tag="phase-23b",
)

tools = dash(
    "aeoi-tool-gateway",
    "AEOI - Tool gateway",
    "Which tool is denied, failing or slow, and for which agent?",
    [
        panel(
            1,
            "Tool calls per hour, by status",
            "timeseries",
            PG,
            [
                sql(
                    f"SELECT $__timeGroupAlias(created_at, 1h), status AS metric, count(*) AS value FROM {tc} "
                    "WHERE $__timeFilter(created_at) GROUP BY 1, 2 ORDER BY 1"
                )
            ],
            0,
            0,
            desc="OK, DENIED (policy said no), ERROR, TIMEOUT, PENDING (the recorded intent of a consequential call).",
        ),
        panel(
            2,
            "Denials by code, agent and tool",
            "table",
            PG,
            [
                sql(
                    "SELECT denial_code, coalesce(agent_name, '(user, no agent)') AS agent, tool_name, count(*) AS denials, "
                    f"max(created_at) AS last_seen FROM {tc} WHERE status = 'DENIED' AND $__timeFilter(created_at) "
                    "GROUP BY 1, 2, 3 ORDER BY denials DESC LIMIT 50",
                    "table",
                )
            ],
            12,
            0,
            desc="denial_code is derived by the DB from the stored reason; the reason text is not readable here.",
        ),
        panel(
            3,
            "Per tool: calls, outcomes, retries, latency",
            "table",
            PG,
            [
                sql(
                    "SELECT tool_name, count(*) AS calls, count(*) FILTER (WHERE status = 'OK') AS ok, "
                    "count(*) FILTER (WHERE status = 'DENIED') AS denied, count(*) FILTER (WHERE status = 'ERROR') AS error, "
                    "count(*) FILTER (WHERE status = 'TIMEOUT') AS timeout, count(*) FILTER (WHERE attempt > 1) AS retried, "
                    "percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) FILTER (WHERE status = 'OK') AS p95_ok_ms "
                    f"FROM {tc} WHERE $__timeFilter(created_at) GROUP BY tool_name ORDER BY calls DESC",
                    "table",
                )
            ],
            0,
            8,
            w=24,
            h=10,
        ),
        panel(
            4,
            "Consequential calls (need a recorded approval)",
            "table",
            PG,
            [
                sql(
                    "SELECT tool_name, status, count(*) AS calls, max(created_at) AS last_seen "
                    f"FROM {tc} WHERE side_effect = 'CONSEQUENTIAL' AND $__timeFilter(created_at) GROUP BY 1, 2 ORDER BY 1, 2",
                    "table",
                )
            ],
            0,
            18,
            w=24,
            h=7,
            desc="The DB refuses an executed consequential call without an approval (constraint, ADR-007).",
        ),
    ],
    tag="phase-23b",
)

# Phase 25c (ADR-037): eval runs, read as obs_reader (0028: run + numeric columns, never config
# or details). Every number is an UPPER bound on data written for the eval.
UB = "Upper bound: measured on data written for the eval, not on real incidents. "
runs_, evs, dss = "eval.eval_runs", "eval.evaluations", "eval.datasets"
RATE = "avg(e.value) FILTER (WHERE e.metric = '{m}')"
COUNT = "sum(e.value) FILTER (WHERE e.metric = '{m}')::int"
evaluation = dash(
    "aeoi-evaluation",
    "AEOI - Evaluation",
    "What did each eval run measure, on how many cases, and did the investigation eval get worse?",
    [
        panel(
            1,
            "Eval runs (rag, history, risk-scan imported from files; investigations from the runner)",
            "table",
            PG,
            [
                sql(
                    f"SELECT r.started_at, d.name AS dataset, d.version, r.status, r.git_sha, "
                    f"count(DISTINCT e.case_id) AS cases_scored, "
                    f"coalesce((r.summary->>'rescored')::int, 0) AS rescored "
                    f"FROM {runs_} r JOIN {dss} d ON d.id = r.dataset_id "
                    f"LEFT JOIN {evs} e ON e.run_id = r.id WHERE $__timeFilter(r.started_at) "
                    "GROUP BY r.id, r.started_at, d.name, d.version, r.status, r.git_sha, r.summary "
                    "ORDER BY r.started_at DESC",
                    "table",
                )
            ],
            0,
            0,
            w=24,
            desc="cases_scored = per-case rows; 0 for a file run (summary only). rescored = 1: the "
            "same paid run scored again (two rows, ONE run). Files are the record (data/eval/results).",
        ),
        panel(
            2,
            "Investigation eval per run (pack v1: 16 planted cases, v2: 6)",
            "table",
            PG,
            [
                sql(
                    f"SELECT r.started_at, d.version AS pack, coalesce((r.summary->>'rescored')::int, 0) AS rescored, "
                    f"count(DISTINCT e.case_id) AS n_cases, "
                    f"{RATE.format(m='passed')} AS passed, {COUNT.format(m='confident_wrong')} AS confident_wrong, "
                    f"{COUNT.format(m='validated_wrong')} AS validated_wrong, "
                    f"{COUNT.format(m='wrong_high')} AS wrong_high, "
                    f"{COUNT.format(m='proposal_correct')} AS rollback_correct, "
                    f"{COUNT.format(m='proposal_wrong')} AS rollback_wrong, "
                    f"{COUNT.format(m='proposal_missed')} AS rollback_missed "
                    f"FROM {runs_} r JOIN {dss} d ON d.id = r.dataset_id JOIN {evs} e ON e.run_id = r.id "
                    "WHERE d.name = 'investigations' AND r.status = 'SUCCEEDED' "
                    "AND jsonb_array_length(coalesce(r.summary->'invalid', '[]')) = 0 "
                    "AND $__timeFilter(r.started_at) "
                    "GROUP BY r.id, r.started_at, d.version, r.summary ORDER BY r.started_at DESC",
                    "table",
                )
            ],
            0,
            8,
            w=24,
            desc=UB + "SUCCEEDED runs without INVALID cases only (older runs stored an invalid run "
            "as SUCCEEDED): a crashed or invalid run is a failed measurement "
            "(the runs table above still lists it). FAKE models (CI): nothing is ever VALIDATED and no rollback is proposed, so "
            "validated_wrong 0 and rollbacks 0 say nothing there; only a paid run measures them. "
            "Empty = not scored by that runner version. n_cases is small: quote a rate only with its interval.",
        ),
        panel(
            3,
            "RAG retrieval per run and mode (imported files)",
            "table",
            PG,
            [
                sql(
                    f"SELECT r.started_at, m.key AS mode, (m.value->>'n')::int AS n, "
                    "(m.value->>'recall@5')::float AS recall_at_5, "
                    "(m.value->'recall@5_ci95'->>0)::float AS ci95_low, "
                    "(m.value->'recall@5_ci95'->>1)::float AS ci95_high, "
                    "(m.value->>'mrr@10')::float AS mrr_at_10, "
                    "(m.value->>'leakage_violations')::int AS leakage "
                    f"FROM {runs_} r JOIN {dss} d ON d.id = r.dataset_id "
                    "CROSS JOIN LATERAL jsonb_each(r.summary) m "
                    "WHERE d.name = 'rag-retrieval' AND $__timeFilter(r.started_at) "
                    "ORDER BY r.started_at DESC, m.key",
                    "table",
                )
            ],
            0,
            16,
            desc=UB + "Hand-written queries over synthetic docs. Only runs with a real embedding "
            "model and a passed validity check are imported.",
        ),
        panel(
            4,
            "Similar-incident search per run and mode (imported files)",
            "table",
            PG,
            [
                sql(
                    f"SELECT r.started_at, m.key AS mode, (m.value->>'n')::int AS n, "
                    "(m.value->>'hit@5')::float AS hit_at_5, "
                    "(m.value->'hit@5_ci95'->>0)::float AS ci95_low, "
                    "(m.value->'hit@5_ci95'->>1)::float AS ci95_high, "
                    "(m.value->>'mrr@10')::float AS mrr_at_10, "
                    "(m.value->>'leakage_violations')::int AS leakage "
                    f"FROM {runs_} r JOIN {dss} d ON d.id = r.dataset_id "
                    "CROSS JOIN LATERAL jsonb_each(r.summary) m "
                    "WHERE d.name = 'history-similarity' AND $__timeFilter(r.started_at) "
                    "ORDER BY r.started_at DESC, m.key",
                    "table",
                )
            ],
            12,
            16,
            desc=UB + "Queries written from the synthetic history itself.",
        ),
    ],
    refresh="",
    tag="phase-25c",
    time_from="now-90d",
)

DASHBOARDS = {"platform-health": platform, "investigation-funnel": funnel, "llm-cost": cost,
              "incident-kpis": kpis, "tool-gateway": tools, "evaluation": evaluation}  # fmt: skip


def render(d: dict[str, Any]) -> str:
    return json.dumps(d, indent=2) + "\n"


def main(argv: list[str]) -> int:
    if "--check" in argv:
        stale = [n for n, d in DASHBOARDS.items()
                 if not (OUT / f"{n}.json").is_file() or (OUT / f"{n}.json").read_text() != render(d)]  # fmt: skip
        for n in stale:
            print(f"{n}.json differs from scripts/obs_dashboards.py: run it without --check")
        return 1 if stale else 0
    for n, d in DASHBOARDS.items():
        (OUT / f"{n}.json").write_text(render(d))
    print(f"wrote {len(DASHBOARDS)} dashboards to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
