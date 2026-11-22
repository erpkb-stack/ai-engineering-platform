"""Phase 32a: ONE real fault on AEOI's own deployed system, investigated from its REAL telemetry
(Prometheus + Loki), scored by the rules in realdatalib.py (fixed before the first run).

  make realdata-run FAULT=f1   traffic surge on the api's read route (no LLM call behind it)
  make realdata-run FAULT=f2   Postgres stopped by the OWNER (this script only watches for it)

Steps: preflight -> quiet baseline (1 req/s; the anomaly maths needs a pre-window baseline) ->
fault -> signed alert (startsAt = the fault start) -> investigation -> evidence + hypotheses +
cost -> data/realdata/results/realdata-<fault>-<UTC>.json. Every run is written, FAILs too.
Needs: AEOI_TOOLS_METRICS_URL + AEOI_TOOLS_LOGS_URL on, `make obs-up`, new images deployed,
`make catalog-import`. DEV ONLY.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import realdatalib as r  # noqa: E402


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


demo = _load(ROOT / "tests/demo/run.py", "demo_run")  # alert signing, call, machine, tokens

API = demo.API
TARGET = os.environ.get("AEOI_REALDATA_TARGET", "api")
ROUTE = f"{API}/api/v1/incidents?limit=20"  # a plain read: no LLM, no write
BASELINE_MIN = float(os.environ.get("AEOI_REALDATA_BASELINE_MIN", "30"))
SURGE_S = int(os.environ.get("AEOI_REALDATA_SURGE_S", "180"))
WORKERS = int(os.environ.get("AEOI_REALDATA_WORKERS", "40"))
F2_WAIT_S = 600
RESULTS = ROOT / "data/realdata/results"


def _get(url: str, token: str, timeout: float = 10.0) -> int:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            resp.read()
            return int(resp.status)
    except urllib.error.HTTPError as e:
        return int(e.code)
    except OSError:
        return 0


def steady(token: str, seconds: float, stop: threading.Event | None = None) -> list[int]:
    """1 request per second for `seconds` (or until `stop`)."""
    out: list[int] = []
    end = time.monotonic() + seconds
    while time.monotonic() < end and not (stop and stop.is_set()):
        t = time.monotonic()
        out.append(_get(ROUTE, token))
        time.sleep(max(0.0, 1.0 - (time.monotonic() - t)))
    return out


def surge(token: str) -> list[int]:
    """WORKERS concurrent loops on the read route for SURGE_S seconds."""
    out: list[int] = []
    lock = threading.Lock()
    end = time.monotonic() + SURGE_S

    def worker() -> None:
        while time.monotonic() < end:
            s = _get(ROUTE, token, timeout=15.0)
            with lock:
                out.append(s)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(WORKERS)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    return out


def api_state() -> str:
    """ok | degraded | down - the api's readiness BODY (after the start it stays 200, ADR-041)."""
    try:
        with urllib.request.urlopen(f"{API}/health/ready", timeout=5) as resp:  # noqa: S310
            return str(json.loads(resp.read().decode()).get("status", "?"))
    except urllib.error.HTTPError as e:
        try:
            return str(json.loads(e.read().decode()).get("status", "down"))
        except (ValueError, OSError):
            return "down"
    except (OSError, ValueError):
        return "down"


def wait_for_outage(token: str) -> tuple[list[int], str | None, str | None]:
    """F2: the OWNER stops Postgres; keep 1 req/s going, note when the api reports degraded and
    when it is ok again. (A chaos-tool run refuses real models, ADR-040: so the owner does it.)"""
    print("  F2: now stop Postgres for ~60 s in another terminal:")
    print("      docker stop aeoi-postgres-1; sleep 60; docker start aeoi-postgres-1")
    stop = threading.Event()
    statuses: list[int] = []
    th = threading.Thread(target=lambda: statuses.extend(steady(token, F2_WAIT_S, stop)),
                          daemon=True)  # fmt: skip
    th.start()
    began = ended = None
    deadline = time.monotonic() + F2_WAIT_S
    while time.monotonic() < deadline:
        state = api_state()
        if began is None and state in ("degraded", "down"):
            began = datetime.now(UTC).isoformat(timespec="seconds")
            print(f"  F2: outage seen at {began}")
        elif began and state == "ok":
            ended = datetime.now(UTC).isoformat(timespec="seconds")
            print(f"  F2: recovered at {ended}")
            break
        time.sleep(2)
    stop.set()
    th.join()
    return statuses, began, ended


def cost_of(investigation_id: str) -> float | None:
    import psycopg

    from aeoi_db.config import libpq_dsn

    try:
        with psycopg.connect(libpq_dsn()) as c:
            row = c.execute(
                "SELECT coalesce(sum(cost_usd), 0) FROM llm.model_usage WHERE investigation_id = %s",
                (investigation_id,),
            ).fetchone()
            return round(float(row[0]), 4) if row else None
    except psycopg.Error:
        return None


def run(fault: str) -> int:
    if fault not in ("f1", "f2"):
        print("usage: make realdata-run FAULT=f1|f2")
        return 2
    env0 = demo.machine()
    t0 = time.monotonic()
    out: dict[str, Any] = {"rules_version": r.RULES_VERSION, "fault": fault, "target": TARGET,
                           "started_at": env0["at"]}  # fmt: skip
    down = [n for n, p in demo.PORTS.items()
            if demo.call("GET", f"http://localhost:{p}"
                         f"{'/actuator/health/readiness' if n == 'service-catalog' else '/health/ready'}")[0] != 200]  # fmt: skip
    if down:
        print(f"not ready: {down}")
        return 2
    svc = (ROOT / "secrets/agents_service_token.txt").read_text().strip()
    st, routes = demo.call("GET", f"{demo.LLM}/v1/routes", svc)
    if st != 200:
        print(f"llm gateway /v1/routes: {st}")
        return 2
    out["models"] = "real" if demo.not_fake(routes) else "fake"
    if not demo.SECRET.is_file():
        print("no secrets/alert_webhook_secret.txt: run make alert-secret")
        return 2
    sre = demo.tokens("SRE")[0]

    print(f"  baseline: {BASELINE_MIN:g} min at 1 req/s on {ROUTE}")
    out["baseline"] = {"minutes": BASELINE_MIN, "requests": r.tally(steady(sre, BASELINE_MIN * 60))}
    fault_at = datetime.now(UTC).replace(microsecond=0)
    if fault == "f1":
        print(f"  F1: surge, {WORKERS} workers for {SURGE_S} s")
        out["fault_window"] = {"start": fault_at.isoformat(), "requests": r.tally(surge(sre))}
    else:
        statuses, began, ended = wait_for_outage(sre)
        out["fault_window"] = {"start": began, "end": ended, "requests": r.tally(statuses)}
        if began is None:
            print("  F2: no outage seen in 10 min - nothing to investigate")
            return finish(out, None, [], [], env0, t0)
        fault_at = datetime.fromisoformat(began)
    out["fault_window"]["end"] = out["fault_window"].get("end") or datetime.now(UTC).isoformat(
        timespec="seconds")  # fmt: skip

    doc = demo.alert(f"realdata{int(time.time())}")
    a = doc["alerts"][0]
    a["labels"] = {"alertname": "AeoiApiHigh5xx" if fault == "f1" else "AeoiDatabaseDown",
                   "service": TARGET, "severity": "error"}  # fmt: skip
    a["annotations"] = {"summary": f"{TARGET} errors (real data run {fault})",
                        "description": "5xx above normal on the api"}  # fmt: skip
    a["startsAt"] = fault_at.isoformat().replace("+00:00", "Z")
    code, body = demo.send_alert(doc, demo.SECRET.read_bytes().strip())
    first = ((body.get("results") or [{}])[0]) if isinstance(body, dict) else {}
    key = first.get("incident_key") if first.get("outcome") == "created" else None
    # severity + the triage rule: "tier unknown" here = AEOI's services not imported (catalog)
    out["alert"] = {"status": code, "incident": key, "severity": first.get("severity"),
                    "rule": first.get("reason"), "detail": None if key else str(body)[:200]}  # fmt: skip
    if not key:
        return finish(out, None, [], [], env0, t0)
    st, acc = demo.call("POST", f"{API}/api/v1/incidents/{key}/investigate", sre, None,
                        f"realdata-{int(time.time())}")  # fmt: skip
    if st != 202:
        out["investigation"] = {"error": f"start {st}"}
        return finish(out, None, [], [], env0, t0)
    inv_id, inv, end = acc["investigation_id"], {}, time.monotonic() + demo.MAX_WAIT_S
    while time.monotonic() < end:
        time.sleep(3)
        s2, inv = demo.call("GET", f"{API}/api/v1/investigations/{inv_id}", sre)
        if s2 == 200 and inv.get("status") != "RUNNING":
            break
    _, ev = demo.call("GET", f"{API}/api/v1/incidents/{key}/evidence", sre)
    _, hyps = demo.call("GET", f"{API}/api/v1/incidents/{key}/hypotheses", sre)
    out["investigation"] = {"id": inv_id, "status": inv.get("status"),
                            "seconds": demo._since(inv), "cost_usd": cost_of(inv_id)}  # fmt: skip
    return finish(out, fault, ev or [], hyps or [], env0, t0)


def finish(out: dict[str, Any], fault: str | None, ev: list[dict[str, Any]],
           hyps: list[dict[str, Any]], env0: dict[str, Any], t0: float) -> int:  # fmt: skip
    env1 = demo.machine()
    data = r.data_check(ev)
    rule = (r.rule_f1(hyps, out["target"]) if fault == "f1" else
            r.rule_f2(hyps) if fault == "f2" else (r.FAIL, "no investigation"))  # fmt: skip
    out |= {"finished_at": env1["at"], "seconds": round(time.monotonic() - t0),
            "evidence": r.evidence_summary(ev),
            "hypotheses": [r.describe(h) for h in r.live(hyps)][:5],
            "checks": {"real_data": {"status": data[0], "detail": data[1]},
                       "rule": {"status": rule[0], "detail": rule[1]}},
            "verdict": r.verdict(data, rule),
            "machine": {"before": env0, "after": env1,
                        "warnings": demo.d.environment_warnings(env0, env1)}}  # fmt: skip
    RESULTS.mkdir(parents=True, exist_ok=True)
    p = RESULTS / f"realdata-{out['fault']}-{datetime.now(UTC):%Y-%m-%dT%H%M%S}.json"
    p.write_text(json.dumps(out, indent=1) + "\n")
    print(f"  {data[0]:<7} real_data: {data[1]}")
    print(f"  {rule[0]:<7} rule: {rule[1]}")
    print(f"  evidence: {out['evidence']['by_source']}  empty: {out['evidence']['empty_kinds']}")
    inv = out.get("investigation") or {}
    print(f"  investigation: {inv.get('status')} in {inv.get('seconds')} s, ${inv.get('cost_usd')}")
    for w in out["machine"]["warnings"]:
        print(f"  MACHINE: {w}")
    print(f"written: {p.relative_to(ROOT)}")
    print(f"VERDICT ({out.get('models')} models): {out['verdict']}")
    return 0 if out["verdict"] == r.PASS else 1


if __name__ == "__main__":
    if os.environ.get("AEOI_ENV") == "production":
        raise SystemExit("dev only")
    raise SystemExit(run(sys.argv[1] if len(sys.argv) > 1 else ""))
