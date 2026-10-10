"""Observations: the evidence agents' TYPED results flattened into a timeline. Code only.

Each observation has a role, a time and the evidence ids behind it. Roles:
  effect    the symptom: error/latency metrics up, cache hit ratio down, error-code log clusters
  resource  saturation signals: pool/thread/connection/cpu utilisation up
  traffic   request rate moved
  change    a deploy (with its config diff)
  steady    a metric that stayed within its baseline (a NEGATIVE fact: rules causes out)
Refs o1..oN are assigned in TIME order, so the same results always give the same refs (the
hypothesis and critic agents rebuild them independently and must agree).
Knowledge pointers are not observations: they have no time and say nothing about the system.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from aeoi_agents.common import hhmm
from aeoi_models.api.agents import AgentRunResult
from aeoi_models.api.hypotheses import Observation
from aeoi_models.findings import EvidenceKind, EvidenceRef

EFFECT_UP = {"http_5xx_per_min", "p95_latency_ms", "db_query_latency_ms"}
EFFECT_DOWN = {"cache_hit_ratio"}
RESOURCE_UP = {
    "db_pool_utilization",
    "thread_pool_utilization",
    "db_connections_active",
    "cpu_util",
}
TRAFFIC = {"requests_per_sec"}
MIN_LOG_LINES = 5  # an error code seen fewer times is noise, not a symptom
MAX_OBS = 60
# truncation keeps causes and symptoms before negative evidence (review finding: cutting the
# LATEST observations silently dropped the steady metrics that rule causes out)
KEEP_FIRST = {"change": 0, "effect": 0, "resource": 0, "traffic": 0, "steady": 1}
ERROR_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def clean(text: str, limit: int = 500) -> str:
    """One line, no control characters. Log-derived text (error codes) reaches the prompt
    inside these statements: a newline could forge an observation line (review finding)."""
    return " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())[:limit]


def safe_code(code: str) -> str:
    return code if ERROR_CODE.match(code) else "invalid_error_code"


SOURCES = {
    "LOG": "devdata.log_events",
    "METRIC": "devdata.metric_points",
    "DEPLOY": "devdata.deployments",
    "CONFIG": "devdata.deployments",
}


def ref(evidence_id: str) -> EvidenceRef:
    kind = evidence_id.split("-", 1)[0]
    return EvidenceRef(
        evidence_id=evidence_id, kind=EvidenceKind(kind), source_system=SOURCES.get(kind, kind)
    )


def build(results: dict[str, AgentRunResult]) -> list[Observation]:
    raw: list[dict[str, Any]] = []
    m = results.get("metrics")
    if m is not None and m.status == "SUCCEEDED":
        for a in m.anomalies:
            role = (
                "effect"
                if (a.metric in EFFECT_UP and a.direction == "up")
                or (a.metric in EFFECT_DOWN and a.direction == "down")
                else "resource"
                if a.metric in RESOURCE_UP and a.direction == "up"
                else "traffic"
                if a.metric in TRAFFIC
                else None
            )
            if role is None:
                continue
            shape = "sustained shift" if a.kind == "shift" else "transient"
            raw.append(
                {
                    "sustained": role == "effect" and a.kind == "shift",
                    "agent": "metrics", "role": role, "service_key": a.service_key,
                    "subject": a.metric, "at": a.onset, "evidence": a.evidence_ids[:1],
                    "statement": f"{a.service_key} {a.metric}: {shape} {a.direction} from "
                    f"{hhmm(a.onset)} (peak {a.peak_value:.4g}, robust z {a.peak_z:+.1f})",
                }
            )  # fmt: skip
        seen_at = {e.evidence_key: e.observed_at for e in m.evidence}
        for f in m.facts:
            if ": stayed within its baseline" not in f.statement:
                continue
            head = f.statement.split(":", 1)[0]  # "<service> <metric>"
            svc, _, metric = head.rpartition(" ")
            ev = f.evidence[0].evidence_id
            at = seen_at.get(ev)
            if at is None:
                continue
            raw.append(
                {"agent": "metrics", "role": "steady", "service_key": svc, "subject": metric,
                 "at": at, "evidence": [ev], "statement": f.statement[:500]}
            )  # fmt: skip
    d = results.get("deployment")
    if d is not None and d.status == "SUCCEEDED":
        stmts = {
            c.deploy_key: f.statement
            for c in d.changes
            for f in d.facts
            if f" {c.deploy_key} " in f" {f.statement} "
        }
        for c in d.changes:
            raw.append(
                {"agent": "deployment", "role": "change", "service_key": c.service_key,
                 "subject": c.deploy_key, "at": c.started_at, "evidence": c.evidence_ids[:2],
                 "statement": stmts.get(c.deploy_key, f"deploy {c.deploy_key}")[:500]}
            )  # fmt: skip
    lg = results.get("log_analysis")
    if lg is not None and lg.status == "SUCCEEDED":
        # one observation per (service, error code): clusters split one code into several
        # templates, and counting each would double the support (review finding)
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for cl in lg.clusters:
            n = cl.count_window or cl.count_sampled
            if not cl.error_code or not cl.evidence_ids:
                continue
            k = (cl.service_key, cl.error_code)
            m0 = merged.setdefault(k, {"n": 0, "at": cl.first_seen, "ev": []})
            m0["n"] = max(m0["n"], n)  # count_window is already per error code
            m0["at"] = min(m0["at"], cl.first_seen)
            m0["ev"] = list(dict.fromkeys([*m0["ev"], *cl.evidence_ids]))[:2]
        for (svc, code), m0 in merged.items():
            if m0["n"] < MIN_LOG_LINES:
                continue
            code = safe_code(code)
            raw.append(
                {"agent": "log_analysis", "role": "effect", "service_key": svc,
                 "subject": code, "at": m0["at"], "evidence": m0["ev"],
                 "statement": f"{svc}: {m0['n']} log lines with error_code {code}, "
                 f"first seen {hhmm(m0['at'])}"}
            )  # fmt: skip
    raw.sort(key=lambda r: (KEEP_FIRST[r["role"]], r["at"], r["agent"], r["subject"]))
    kept = sorted(raw[:MAX_OBS], key=lambda r: (r["at"], r["agent"], r["subject"]))
    return [
        Observation(
            ref=f"o{i + 1}",
            **{
                **r,
                "subject": clean(str(r["subject"]), 120),
                "statement": clean(r["statement"]),
                "evidence": [ref(e) for e in r["evidence"]],
            },
        )
        for i, r in enumerate(kept)
    ]


def first_effect(obs: list[Observation]) -> Observation | None:
    """The anchor for timing: the earliest SUSTAINED metric shift; only if there is none, the
    earliest effect of any kind (a log cluster's first_seen can be background noise)."""
    sustained = [o for o in obs if o.role == "effect" and o.sustained]
    effects = sustained or [o for o in obs if o.role == "effect"]
    return min(effects, key=lambda o: o.at) if effects else None


def minutes(a: datetime, b: datetime) -> float:
    return round((b - a).total_seconds() / 60, 1)
