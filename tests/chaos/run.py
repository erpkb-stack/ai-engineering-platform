"""Phase 28a (ADR-040): run ONE failure experiment against the deployed system (kind + compose
infra). `make chaos EXP=pg|kafka|orchestrator|agents` - the OWNER runs it: it stops containers
and deletes pods on this machine.

Steady state first (30 s of reads, an investigation started on a scenario-pack incident), then
the fault, the undo, and observation until the investigation ends (max 5 min) and reads have
been clean for 30 s. Writes data/chaos/results/<exp>-<UTC stamp>.json and prints the verdicts.
Refuses: a non-fake model behind the llm gateway (it would pay for every retry), a backlog of
RUNNING investigations. Fault/undo commands can be overridden (AEOI_CHAOS_FAULT / _UNDO, a
shell line) - used for a sandbox dry run only.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(HERE), str(ROOT / "tests/load")]
from chaoslib import (  # noqa: E402
    EXPERIMENTS,
    KCTL,
    NAMED_END,
    REQUEST_TIMEOUT_S,
    judge,
    restart_delta,
)
from loadlib import not_fake  # noqa: E402
from tokens import running_investigations, tokens  # noqa: E402

API = os.environ.get("AEOI_CHAOS_API", "http://localhost:8000")
LLM = os.environ.get("AEOI_CHAOS_LLM", "http://localhost:8005")
BASELINE_S, INTO_RUN_S, MAX_WAIT_S = 30.0, 10.0, 300.0


def call(method: str, path: str, token: str, body: Any = None, key: str | None = None,
         base: str = API) -> tuple[str, Any, float]:  # fmt: skip
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if key:
        h["Idempotency-Key"] = key
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers=h)  # noqa: S310
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as r:  # noqa: S310
            raw = r.read()
            return str(r.status), json.loads(raw) if raw else None, time.monotonic() - t0
    except urllib.error.HTTPError as e:
        return str(e.code), None, time.monotonic() - t0
    except Exception as e:  # the CLASS is the observation (refused, reset, timeout)
        return type(e).__name__, None, time.monotonic() - t0


def sh(cmd: tuple[str, ...] | str) -> str:
    argv = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)
    r = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False, cwd=ROOT)  # noqa: S603
    return (r.stdout + r.stderr).strip()[:300]


def restarts() -> dict[str, int] | None:
    try:
        out = subprocess.run([*KCTL, "get", "pods", "-o", "json"], capture_output=True,  # noqa: S603
                             text=True, timeout=30, check=True).stdout  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None  # no kubectl here (sandbox): the rule is reported as not measured
    n: dict[str, int] = {}
    for p in json.loads(out)["items"]:
        app = p["metadata"]["labels"].get("app.kubernetes.io/name", "?")
        n[app] = n.get(app, 0) + sum(
            c["restartCount"] for c in p["status"].get("containerStatuses", [])
        )
    return n


def undelivered() -> int:
    import psycopg

    from aeoi_db.config import libpq_dsn

    with psycopg.connect(libpq_dsn()) as conn:
        a = conn.execute(
            "SELECT count(*) FROM incident.outbox WHERE published_at IS NULL"
        ).fetchone()
        b = conn.execute(
            "SELECT count(*) FROM tools.audit_outbox WHERE delivered_at IS NULL"
        ).fetchone()
    return int(a[0] if a else 0) + int(b[0] if b else 0)


def main(name: str) -> int:
    exp = EXPERIMENTS[name]
    fault = os.environ.get("AEOI_CHAOS_FAULT") or exp.fault
    undo = os.environ.get("AEOI_CHAOS_UNDO") or exp.undo
    svc = (ROOT / "secrets/agents_service_token.txt").read_text().strip()
    st, routes, _ = call("GET", "/v1/routes", svc, base=LLM)
    # fakes answer in 0 ms: without the measured delay the run ends before the fault lands
    if st != "200" or not_fake(routes) or not routes.get("simulated_latency_ms"):
        print(f"REFUSED: llm gateway routes {st}, real models {not_fake(routes) if routes else '?'} "
              "or no simulated delay - deploy with LLM_ROUTING=routing.load.yaml.")  # fmt: skip
        return 2
    if (n := running_investigations()) > 0:
        print(f"REFUSED: {n} investigations already RUNNING - wait until they finish.")
        return 2
    sre = tokens("SRE")[0]
    from aeoi_synth import scenarios  # INPUTS only (title, services, time) - never the truth

    case = scenarios.build(42, 1)[0].case_input()
    tag = f"chaos-{name}-{int(time.time())}"
    rows: list[tuple[float, str, float]] = []
    writes: list[int] = []
    stop = threading.Event()
    t0 = time.monotonic()

    def reader() -> None:
        while not stop.is_set():
            o, _, lat = call("GET", "/api/v1/incidents?limit=5", sre)
            rows.append((round(time.monotonic() - t0, 2), o, round(lat, 2)))
            time.sleep(0.5)

    def writer() -> None:
        i = 0
        while not stop.is_set():
            i += 1
            o, _, _ = call("POST", "/api/v1/incidents", sre,
                           {**case, "title": f"{case['title']} [{tag}-w{i}]"}, f"{tag}-w{i}")  # fmt: skip
            if fault_at[0] <= time.monotonic() - t0 < back_at[0]:
                writes.append(int(o) if o.isdigit() else 0)
            time.sleep(5)

    fault_at, back_at = [1e9], [1e9]
    before = restarts()
    threading.Thread(target=reader, daemon=True).start()
    if exp.writes:
        threading.Thread(target=writer, daemon=True).start()
    time.sleep(BASELINE_S - INTO_RUN_S)
    o, inc, _ = call(
        "POST", "/api/v1/incidents", sre, {**case, "title": f"{case['title']} [{tag}]"}, tag
    )
    o2, inv, _ = call(
        "POST", f"/api/v1/incidents/{(inc or {}).get('key')}/investigate", sre, None, f"{tag}-inv"
    )
    if o != "201" or o2 != "202":
        stop.set()
        print(f"steady state not reached: create {o}, investigate {o2} - nothing injected.")
        return 1
    inv_id = inv["investigation_id"]
    time.sleep(INTO_RUN_S)  # the fault lands while the run is inside a node (~43 s per run)
    s, body, _ = call("GET", f"/api/v1/investigations/{inv_id}", sre)
    # only a run KNOWN to have ended is "not exercised"; an error answer is not a known end
    # (dry run: the poll got 500 here and the run was in fact stuck RUNNING)
    hit = not (s == "200" and bool(body) and body.get("status") in NAMED_END)
    if not hit:  # finished before the fault: the investigation rule would pass for nothing
        print(f"note: investigation already {(body or {}).get('status', s)} at the fault - "
              "its rule is reported as not exercised")  # fmt: skip
    fault_at[0] = time.monotonic() - t0
    print(f"t={fault_at[0]:.0f}s FAULT {name}: {sh(fault)}")
    if undo:
        time.sleep(exp.hold_s)
        back_at[0] = time.monotonic() - t0
        print(f"t={back_at[0]:.0f}s UNDO: {sh(undo)}")
    else:
        back_at[0] = fault_at[0]
    status, end = "RUNNING", time.monotonic() + MAX_WAIT_S
    while time.monotonic() < end:
        s, body, _ = call("GET", f"/api/v1/investigations/{inv_id}", sre)
        if s == "200" and body and body.get("status") != "RUNNING":
            status = body["status"]
            break
        time.sleep(5)
    time.sleep(35)  # the clean tail
    stop.set()
    end_at = time.monotonic() - t0
    after = restarts()
    killed = name if not exp.undo else None
    delta = (
        restart_delta(before, after, killed) if before is not None and after is not None else None
    )
    backlog = None
    if exp.writes:
        deadline = time.monotonic() + 180
        while (backlog := undelivered()) and time.monotonic() < deadline:
            time.sleep(5)
    verdict = judge(rows, fault_at[0], back_at[0], end_at, status if hit else None, delta,
                    writes if exp.writes else None, backlog)  # fmt: skip
    out = {"experiment": name, "expect": exp.expect,
           "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
           "git_commit": sh(("git", "rev-parse", "--short", "HEAD")),
           "fault": " ".join(fault) if not isinstance(fault, str) else fault,
           "times_s": {"fault": round(fault_at[0], 1), "back": round(back_at[0], 1), "end": round(end_at, 1)},
           "investigation": {"id": inv_id, "status": status}, **verdict}  # fmt: skip
    d = ROOT / "data/chaos/results"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}-{datetime.now(UTC):%Y-%m-%dT%H%M%S}.json"
    path.write_text(json.dumps(out, indent=1) + "\n")
    print(f"written: {path.relative_to(ROOT)}")
    for k, v in verdict["rules"].items():
        mark = "n/a " if v["pass"] is None else ("PASS" if v["pass"] else "FAIL")
        print(f"  {mark} {k}: {json.dumps({x: y for x, y in v.items() if x != 'pass'})}")
    print(f"  during the fault: {verdict['during_fault']}")
    print(
        f"VERDICT {name}: {'PASS' if verdict['pass'] else 'FAIL - a finding, see the rules above'}"
    )
    return 0 if verdict["pass"] else 1


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in EXPERIMENTS:
        print(f"usage: run.py {'|'.join(EXPERIMENTS)}")
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
