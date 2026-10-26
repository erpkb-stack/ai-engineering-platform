"""Phase 27 (ADR-039): load against the deployed api, as the seeded team. Run ONLY via
`make load PLAN=policy|capacity|investigations` (it sets the env and the output path).

- Every engineer and SRE of the seed is a reader with their OWN token (31 users): the api's
  per-user limit then behaves as it would for a real team. One SRE-token pool starts
  investigations (plan `investigations`), each on a scenario-pack incident (make eval-seed).
- Refuses to run unless EVERY llm-gateway route is fake (routing.test/load.yaml): a load run
  must never call a paid model, nor measure Ollama on the laptop's CPU.
- Writes data/load/results/<plan>-<UTC stamp>.json: measured numbers only, per stage.
"""

from __future__ import annotations

import json
import os
import platform
import random
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import gevent
import psutil
from locust import LoadTestShape, between, events, task
from locust.contrib.fasthttp import FastHttpUser

sys.path.insert(0, str(Path(__file__).parent))
from loadlib import PLANS, investigation_stage, knee, not_fake, stage_at, summarize_stage, wait_s

ROOT = Path(__file__).resolve().parents[2]
PLAN = PLANS[os.environ.get("AEOI_LOAD_PLAN", "policy")]
LLM_URL = os.environ.get("AEOI_LOAD_LLM_URL", "http://localhost:8005")
PROM_URL = os.environ.get("AEOI_LOAD_PROM_URL", "http://localhost:9090")
CONTAINERS = os.environ.get("AEOI_LOAD_CONTAINERS", "aeoi-control-plane,aeoi-postgres-1")
OUT = Path(os.environ.get("AEOI_LOAD_OUT", str(ROOT / "data/load/results")))


class State:
    stage: int | None = -1
    rows: dict[int, list[tuple[str, float, int]]] = {}
    keys: list[str] = []
    investigations: list[dict[str, Any]] = []
    machine: dict[int, dict[str, Any]] = {}
    routes: dict[str, Any] = {}
    backlog_start = 0
    proc = psutil.Process()


def _tokens(role: str) -> list[str]:
    """Same users and claims as `make token ROLE=... NTH=n` (aeoi_api.devtoken), minted here."""
    import psycopg

    from aeoi_api.devtoken import QUERY
    from aeoi_db.config import libpq_dsn
    from aeoi_security.auth import issue_token

    key = (ROOT / "secrets/jwt_private.pem").read_text()
    out = []
    with psycopg.connect(libpq_dsn()) as conn:
        for skip in range(200):
            row = conn.execute(QUERY, {"email": None, "role": role, "group": None,
                                       "without": None, "skip": skip}).fetchone()  # fmt: skip
            if row is None:
                break
            uid, sub, email, name, roles, groups = row
            out.append(issue_token(private_key=key, subject=sub, user_id=uid, email=email,
                                   name=name, roles=list(roles), groups=list(groups),
                                   ttl=timedelta(minutes=120)))  # fmt: skip
    return out


def _running() -> int:
    """Investigations already RUNNING: a backlog (an earlier run, a crash) sits in the same
    max_concurrent_investigations slots and would quietly ruin the measurement."""
    import psycopg

    from aeoi_db.config import libpq_dsn

    with psycopg.connect(libpq_dsn()) as conn:
        row = conn.execute(
            "SELECT count(*) FROM orchestrator.investigations WHERE status = 'RUNNING'"
        ).fetchone()
    return int(row[0]) if row else 0


READERS = _tokens("ENGINEER") + _tokens("SRE")
STARTERS = _tokens("SRE")


def _llm_routes() -> dict[str, Any]:
    svc = (ROOT / "secrets/agents_service_token.txt").read_text().strip()
    req = urllib.request.Request(LLM_URL + "/v1/routes", headers={"Authorization": f"Bearer {svc}"})  # noqa: S310
    return json.loads(urllib.request.urlopen(req, timeout=10).read())  # type: ignore[no-any-return]  # noqa: S310


@events.test_start.add_listener
def _guard(environment: Any, **_: Any) -> None:
    State.routes = _llm_routes()
    State.backlog_start = _running()
    if PLAN.investigations_per_min and State.backlog_start:
        print(f"REFUSED: {State.backlog_start} investigations already RUNNING - wait until they "
              "finish (a restart resumes them, it does not clear them).", file=sys.stderr)  # fmt: skip
        environment.process_exit_code = 2
        environment.runner.quit()
        return
    bad = not_fake(State.routes)
    if bad or (PLAN.investigations_per_min and not State.routes.get("simulated_latency_ms")):
        print(f"REFUSED: real models behind the llm gateway {bad} (or no simulated delay for "
              "investigations). Deploy with LLM_ROUTING=routing.load.yaml.", file=sys.stderr)  # fmt: skip
        environment.process_exit_code = 2
        environment.runner.quit()


@events.request.add_listener
def _record(name: str, response_time: float, response: Any, exception: Any, **_: Any) -> None:
    st = State.stage
    if st is None or st < 0 or name.startswith("investigation"):
        return
    status = 0 if exception and not getattr(response, "status_code", 0) else response.status_code
    State.rows.setdefault(st, []).append((name, response_time, int(status or 0)))


def _snapshot(stage: int) -> None:
    """End of a stage: this process's CPU (the client competes for the same laptop), the
    containers' CPU/memory, and each service's server-side p95 from Prometheus."""
    snap: dict[str, Any] = {"load_client_cpu_percent": State.proc.cpu_percent(None)}
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv
            ["docker", "stats", "--no-stream", "--format", "{{json .}}", *CONTAINERS.split(",")],  # noqa: S607
            capture_output=True, text=True, timeout=20, check=False,
        )  # fmt: skip
        found = {
            d["Name"]: {"cpu": d["CPUPerc"], "mem": d["MemUsage"]}
            for d in (json.loads(x) for x in out.stdout.splitlines() if x.strip())
        }
        err = (out.stderr.strip().splitlines() or ["no output"])[0][:120]
        snap["containers"] = found or f"unavailable: {err}"
    except (OSError, subprocess.TimeoutExpired) as e:
        snap["containers"] = f"unavailable: {type(e).__name__}"
    q = ("histogram_quantile(0.95, sum by (service_name, le) "
         f"(rate(http_server_request_duration_seconds_bucket[{PLAN.stage_s}s])))")  # fmt: skip
    try:
        url = PROM_URL + "/api/v1/query?" + urllib.parse.urlencode({"query": q})
        res = json.loads(urllib.request.urlopen(url, timeout=5).read())["data"]["result"]  # noqa: S310
        snap["server_p95_ms"] = {r["metric"].get("service_name", "?"): round(float(r["value"][1]) * 1000, 1)
                                 for r in res if r["value"][1] not in ("NaN", "+Inf")}  # fmt: skip
    except (OSError, KeyError, ValueError) as e:
        snap["server_p95_ms"] = f"unavailable: {type(e).__name__}"
    State.machine[stage] = snap


class Steps(LoadTestShape):
    def tick(self) -> tuple[int, float, list[type]] | None:
        st = stage_at(PLAN, self.get_run_time())
        if st != State.stage and State.stage is not None and State.stage >= 0:
            _snapshot(State.stage)
        if st == 0 and State.stage == -1:
            State.proc.cpu_percent(None)  # start the CPU counter at the first measured stage
        State.stage = st
        if st is None:
            return None
        classes: list[type] = [Reader] + ([Starter] if PLAN.investigations_per_min else [])
        return len(READERS) + (1 if PLAN.investigations_per_min else 0), 50, classes


class Reader(FastHttpUser):
    n = 0

    def on_start(self) -> None:
        Reader.n += 1
        self.h = {"Authorization": f"Bearer {READERS[(Reader.n - 1) % len(READERS)]}"}
        if not State.keys:
            r = self.client.get("/api/v1/incidents?limit=50", headers=self.h, name="GET /incidents")
            State.keys = [i["key"] for i in r.json().get("items", [])] or ["INC-1"]

    def wait_time(self) -> float:
        st = State.stage if State.stage is not None and State.stage >= 0 else 0
        return wait_s(PLAN.read_rps[st], len(READERS), random.random())

    @task(3)
    def list_incidents(self) -> None:
        self.client.get("/api/v1/incidents?limit=20", headers=self.h, name="GET /incidents")

    @task(3)
    def incident(self) -> None:
        k = random.choice(State.keys)
        self.client.get(f"/api/v1/incidents/{k}", headers=self.h, name="GET /incidents/[key]")

    @task(2)
    def hypotheses(self) -> None:
        k = random.choice(State.keys)
        self.client.get(f"/api/v1/incidents/{k}/hypotheses", headers=self.h,
                        name="GET /incidents/[key]/hypotheses")  # fmt: skip

    @task(1)
    def timeline(self) -> None:
        k = random.choice(State.keys)
        self.client.get(f"/api/v1/incidents/{k}/timeline", headers=self.h,
                        name="GET /incidents/[key]/timeline")  # fmt: skip

    @task(1)
    def search(self) -> None:
        q = random.choice(["db pool timeout", "rollback a deploy", "5xx after release",
                           "certificate expired", "thread pool saturation"])  # fmt: skip
        self.client.post("/api/v1/search", json={"query": q, "k": 8}, headers=self.h,
                         name="POST /search")  # fmt: skip


class Starter(FastHttpUser):
    """Starts investigations on scenario-pack incidents, round robin over the SREs' tokens
    (spreads the tool gateway's per-user budget), and times each to its end."""

    wait_time = between(0, 0)
    fixed_count = 1  # exactly one starter: Locust otherwise splits users across classes by weight
    i = 0

    def on_start(self) -> None:
        from aeoi_synth import scenarios  # the INPUTS only (title, services, time) - never truth

        self.cases = [c.case_input() for v in scenarios.VERSIONS for c in scenarios.build(42, v)]

    @task
    def tick(self) -> None:
        """Start one investigation every 60/rate seconds, each in its own greenlet: a start
        never waits for the previous investigation (that would measure them one at a time)."""
        st = State.stage
        if st is None or st < 0:
            gevent.sleep(1)
            return
        gevent.spawn(self._one)
        gevent.sleep(60 / PLAN.investigations_per_min[st])

    def _one(self) -> None:
        Starter.i += 1
        n = Starter.i
        h = {"Authorization": f"Bearer {STARTERS[n % len(STARTERS)]}"}
        case = self.cases[n % len(self.cases)]
        tag = f"load-{int(time.time())}-{n}"
        t0 = time.monotonic()
        rec: dict[str, Any] = {"status": "RUNNING", "seconds": None, "stage": State.stage}
        State.investigations.append(rec)  # at START: one still running at the end is counted
        inc = self.client.post("/api/v1/incidents", json={**case, "title": f"{case['title']} [{tag}]"},
                               headers={**h, "Idempotency-Key": tag}, name="investigation: create")  # fmt: skip
        if inc.status_code != 201:
            rec.update(status=f"create {inc.status_code}", seconds=0.0)
            return
        r = self.client.post(f"/api/v1/incidents/{inc.json()['key']}/investigate",
                             headers={**h, "Idempotency-Key": f"{tag}-inv"},
                             name="investigation: start")  # fmt: skip
        if r.status_code != 202:
            rec.update(status=f"start {r.status_code}", seconds=0.0)
            return
        inv_id, status, body = r.json()["investigation_id"], "RUNNING", {}
        while status == "RUNNING" and time.monotonic() - t0 < 900:
            gevent.sleep(3)
            g = self.client.get(f"/api/v1/investigations/{inv_id}", headers=h,
                                name="investigation: poll")  # fmt: skip
            if g.status_code == 200:
                body = g.json()
                status = body.get("status", "RUNNING")
        rec.update(status=status, seconds=round(time.monotonic() - t0, 1), **_split(body))


def _split(inv: dict[str, Any]) -> dict[str, float | None]:
    """Server times: QUEUE = first task start - created (the orchestrator runs at most
    max_concurrent_investigations at once); RUN = finished - first task start."""
    t = [
        datetime.fromisoformat(x["started_at"]) for x in inv.get("tasks", []) if x.get("started_at")
    ]
    if not t or not inv.get("started_at"):
        return {"queue_s": None, "run_s": None}
    first, created = min(t), datetime.fromisoformat(inv["started_at"])
    end = inv.get("finished_at")
    return {"queue_s": round((first - created).total_seconds(), 1),
            "run_s": round((datetime.fromisoformat(end) - first).total_seconds(), 1) if end else None}  # fmt: skip


@events.test_stop.add_listener
def _write(environment: Any, **_: Any) -> None:
    if environment.process_exit_code == 2:
        return
    stages = [summarize_stage(PLAN, i, State.rows.get(i, [])) for i in range(len(PLAN.read_rps))]
    for s in stages:
        s["machine"] = State.machine.get(s["stage"], {})
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,  # noqa: S607
                         text=True, cwd=ROOT, check=False).stdout.strip()  # fmt: skip
    out = {
        "plan": PLAN.name, "note": PLAN.note,
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "environment": {
            "git_commit": sha, "api": environment.host, "readers": len(READERS),
            "api_rate_limit_per_minute": os.environ.get("API_RATE_LIMIT_PER_MINUTE", "120 (default)"),
            "machine": {"system": platform.system(), "machine": platform.machine(),
                        "cpus": os.cpu_count(), "memory_gb": round(psutil.virtual_memory().total / 2**30, 1)},
            "llm": {"models": State.routes.get("models"),
                    "simulated_latency_ms": State.routes.get("simulated_latency_ms"),
                    "in_flight_at_end": {n: p.get("in_flight") for n, p in _llm_routes().get("providers", {}).items()}},
            "investigations_running_at_start": State.backlog_start,
            "investigations_running_at_end": _running(),
            "warning": "client and system share ONE laptop; LLM delays are simulated p50s (no tail)",
        },
        "stages": stages,
        "knee": knee(stages),
        "investigations": {
            "stages": [investigation_stage(PLAN, i, State.investigations)
                       for i in range(len(PLAN.investigations_per_min))],
            "by_status": {k: sum(1 for x in State.investigations if x["status"] == k)
                          for k in sorted({x["status"] for x in State.investigations})},
            "note": "queue_s = wait for one of max_concurrent_investigations (default 2) slots",
        } if PLAN.investigations_per_min else None,
    }  # fmt: skip
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{PLAN.name}-{datetime.now(UTC):%Y-%m-%dT%H%M%S}.json"
    path.write_text(json.dumps(out, indent=1) + "\n")
    print(f"written: {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    for s in stages:
        print(f"stage {s['stage']}: target {s['target_rps']} rps -> {s['achieved_rps']} "
              f"(ok {s['ok_rps']}), p95 {s['p95_ms']} ms, 429 {s['rate_limited_share']:.1%}, "
              f"errors {s['error_share']:.1%}")  # fmt: skip
    print(f"knee: {out['knee']}")
