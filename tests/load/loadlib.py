"""Phase 27 (ADR-039): the pure parts of the load tests - plans, the fake-only guard, per-stage
summaries and the knee. No Locust import here, so unit tests run without the `load` group."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

FAKE_PROVIDERS = ("local_fake", "hosted_fake")
WARMUP_S = 15


@dataclass(frozen=True)
class Plan:
    name: str
    read_rps: tuple[int, ...]  # target TOTAL read rps per stage
    stage_s: int
    investigations_per_min: tuple[float, ...]  # per stage; () = none
    note: str


PLANS = {
    "smoke": Plan("smoke", (2, 4), 10, (), "30 s self-check of the set-up, not a measurement"),
    "policy": Plan("policy", (10, 30, 60, 100), 60, (),
                   "rate limits as shipped: expect 429s once the team's budget is used"),
    "capacity": Plan("capacity", (10, 25, 50, 75, 100, 150), 60, (),
                     "api limit raised at deploy time: where does the SYSTEM bend?"),
    "investigations": Plan("investigations", (10, 10), 300, (2, 4),
                           "2 then 4 investigations/min on a 10 rps read load, simulated LLM "
                           "delay: below and above the orchestrator's 2 concurrent slots"),
}  # fmt: skip

# read mix: (name, weight). Paths use [key] so one incident does not make its own row.
READ_MIX = (
    ("GET /incidents", 3),
    ("GET /incidents/[key]", 3),
    ("GET /incidents/[key]/hypotheses", 2),
    ("GET /incidents/[key]/timeline", 1),
    ("POST /search", 1),
)


def not_fake(routes: dict[str, Any]) -> list[str]:
    """Models behind ANY route that are not fake: a load run must never call a real model -
    it would cost money (hosted) or measure Ollama on the laptop's CPU (local)."""
    used = {m for r in routes["routes"].values() for m in r["chain"]}
    return sorted(m for m in used if routes["models"].get(m) not in FAKE_PROVIDERS)


def stage_at(plan: Plan, t: float) -> int | None:
    """-1 = warm-up (not measured), 0..n-1 = a stage, None = done."""
    if t < WARMUP_S:
        return -1
    i = int((t - WARMUP_S) // plan.stage_s)
    return i if i < len(plan.read_rps) else None


def wait_s(total_rps: float, readers: int, u: float) -> float:
    """Poisson arrivals per reader: exponential gap with mean readers / total_rps (u in (0,1))."""
    rate = max(total_rps, 0.01) / max(readers, 1)
    return -math.log(1 - u) / rate


def pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return round(s[min(len(s) - 1, math.ceil(p / 100 * len(s)) - 1)], 1)


def summarize_stage(plan: Plan, i: int, rows: list[tuple[str, float, int]]) -> dict[str, Any]:
    """rows: (name, response_ms, status) of one stage; status 0 = no response (exception)."""
    ok = [r for r in rows if 200 <= r[2] < 400]
    limited = [r for r in rows if r[2] == 429]
    errors = [r for r in rows if r[2] == 0 or r[2] >= 500]
    other = [r for r in rows if 400 <= r[2] < 500 and r[2] != 429]
    n = len(rows)
    per = {}
    for name, _ in READ_MIX:
        xs = [r[1] for r in ok if r[0] == name]
        per[name] = {"n": len(xs), "p50_ms": pct(xs, 50), "p95_ms": pct(xs, 95)}
    lat = [r[1] for r in ok]
    return {
        "stage": i,
        "target_rps": plan.read_rps[i],
        "achieved_rps": round(n / plan.stage_s, 1),
        "ok_rps": round(len(ok) / plan.stage_s, 1),
        "requests": n,
        "p50_ms": pct(lat, 50),
        "p95_ms": pct(lat, 95),
        "p99_ms": pct(lat, 99),
        "rate_limited_share": round(len(limited) / n, 4) if n else 0.0,
        "error_share": round(len(errors) / n, 4) if n else 0.0,
        "other_4xx": len(other),
        "per_endpoint": per,
    }


def knee(stages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The first stage where the system bends: it delivers < 90 % of the target, or p95 is more
    than 2x the first stage's, or > 1 % errors. 429s are POLICY, reported apart, not a knee."""
    if not stages:
        return None
    base = stages[0]["p95_ms"] or 0
    for s in stages:
        why = []
        if s["achieved_rps"] < 0.9 * s["target_rps"]:
            why.append(f"delivered {s['achieved_rps']} of {s['target_rps']} rps")
        if base and s["p95_ms"] and s["p95_ms"] > 2 * base:
            why.append(f"p95 {s['p95_ms']} ms > 2x {base} ms")
        if s["error_share"] > 0.01:
            why.append(f"errors {s['error_share']:.1%}")
        if why:
            return {"stage": s["stage"], "target_rps": s["target_rps"], "why": why}
    return None


def investigation_stage(plan: Plan, i: int, recs: list[dict[str, Any]]) -> dict[str, Any]:
    """Per stage: how many started, finished, still running; QUEUE (wait for a slot) and RUN."""
    mine = [r for r in recs if r.get("stage") == i]
    done = [r for r in mine if r["status"] in ("COMPLETE", "PARTIAL", "INCONCLUSIVE", "FAILED")]
    q = [r["queue_s"] for r in mine if r.get("queue_s") is not None]
    run = [r["run_s"] for r in done if r.get("run_s") is not None]
    return {
        "per_min": plan.investigations_per_min[i], "started": len(mine), "finished": len(done),
        "still_running": sum(1 for r in mine if r["status"] == "RUNNING"),
        "refused": sum(1 for r in mine if r["status"].startswith(("create", "start"))),
        "queue_s_p50": pct(q, 50), "queue_s_max": max(q, default=None),
        "run_s_p50": pct(run, 50), "run_s_max": max(run, default=None),
        "end_to_end_s_p50": pct([r["seconds"] for r in done], 50),
    }  # fmt: skip
