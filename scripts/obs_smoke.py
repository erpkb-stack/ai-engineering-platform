"""Phase 23a Verify (ADR-035): ONE investigation is ONE trace, its logs are in Loki, metrics flow.

    TOKEN=... uv run python scripts/obs_smoke.py [--resume kind|compose]

Needs: the apps running with telemetry ON (AEOI_OTEL_ENDPOINT in .env, then make k8s-apply or
make apps-up) and the stack (make obs-up). --resume restarts the orchestrator while the
investigation runs: the resumed run must land in the SAME trace (before 23a it had no
correlation id at all). Reads Tempo :3200, Loki :3100, Prometheus :9090 on 127.0.0.1.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from http.client import HTTPException
from typing import Any

API = os.environ.get("AEOI_API_URL", "http://127.0.0.1:8000")
TEMPO = "http://127.0.0.1:3200"
LOKI = "http://127.0.0.1:3100"
PROM = "http://127.0.0.1:9090"
# every Python service an investigation passes through (audit: only with the Kafka consumer;
# the catalog: only with the Java switch - both reported, never required)
REQUIRED = {"api", "orchestrator", "agents", "tool-gateway", "rag", "llm-gateway",
            "incident-service"}  # fmt: skip
OPTIONAL = {"audit", "service-catalog"}
TERMINAL = {"COMPLETE", "PARTIAL", "FAILED", "CANCELLED", "TIMED_OUT"}
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http(method: str, url: str, token: str | None = None, body: dict[str, Any] | None = None,
         headers: dict[str, str] | None = None) -> tuple[int, Any]:  # fmt: skip
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", **(headers or {})}
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=h)  # noqa: S310 - local
    try:
        with _OPENER.open(req, timeout=20) as r:
            return r.status, _body(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _body(e.read())


def _body(raw: bytes) -> Any:
    """JSON when it is JSON, else the text (Tempo's and Loki's /ready answer plain "ready":
    Mac, 2026-10-17, fix3). Never raises on a body."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace")[:300]


def status(url: str) -> int:
    """A readiness probe: 0 = no answer yet. A container that is still starting may refuse the
    connection or (Docker Desktop's port proxy) accept it and close it without a response
    (Mac, 2026-10-17: RemoteDisconnected from Tempo). Both mean "not ready", never a crash."""
    try:
        return http("GET", url)[0]
    except (OSError, HTTPException):
        return 0


SWITCH = "AEOI_OTEL_ENDPOINT"
SWITCH_OFF = (f"telemetry is OFF in the apps: .env has no {SWITCH}=http://localhost:4318 "
              "(add it, then make k8s-apply - or make apps-up for compose apps)")  # fmt: skip


def switch_on() -> bool:
    """The Makefile exports .env, so the apps' switch is visible here (Mac fix4: it was unset,
    and the smoke only noticed after a 73 s investigation as "0 traces")."""
    return bool(os.environ.get(SWITCH, "").strip())


# ---------------------------------------------------------------- pure checks (unit-tested)


def full_trace_id(tid: str) -> str:
    """Tempo's SEARCH drops leading zeros (Mac fix5: "636db42c..." = 31 chars; checked against
    the real Tempo 2.10). Our logs carry the W3C 32-hex form, so Loki needs it padded."""
    return tid.strip().lower().rjust(32, "0")


def trace_ids(search: Any) -> list[str]:
    if not isinstance(search, dict):
        return []
    return sorted({full_trace_id(t["traceID"]) for t in search.get("traces") or []})


SERVICES_URL = f"{TEMPO}/api/v2/search/tag/resource.service.name/values"


def known_services(tags: Any) -> list[str]:
    """/api/v2/search/tag/resource.service.name/values -> names. The v1 endpoint answers []
    for a scoped tag (Mac fix5; checked against the real Tempo 2.10)."""
    vals = tags.get("tagValues") if isinstance(tags, dict) else None
    return sorted(v["value"] if isinstance(v, dict) else str(v) for v in vals or [])


def foreign_correlation(streams: list[dict[str, Any]], ours: str) -> dict[str, int]:
    """Log lines of this trace that carry ANOTHER correlation id, per service. One
    investigation = one correlation id (Mac fix6: llm-gateway minted one per model call)."""
    out: dict[str, int] = {}
    for st in streams:
        labels = st.get("stream", {})
        cid = labels.get("correlation_id")
        if cid is not None and cid != ours:
            name = labels.get("service_name", "?")
            out[name] = out.get(name, 0) + len(st.get("values", []))
    return out


def services_in(trace: dict[str, Any]) -> set[str]:
    out = set()
    for batch in trace.get("batches") or trace.get("resourceSpans") or []:
        for a in batch.get("resource", {}).get("attributes", []):
            if a.get("key") == "service.name":
                out.add(str(a.get("value", {}).get("stringValue", "")))
    return out


def spans_named(trace: dict[str, Any], name: str) -> int:
    n = 0
    for batch in trace.get("batches") or trace.get("resourceSpans") or []:
        for scope in batch.get("scopeSpans") or batch.get("instrumentationLibrarySpans") or []:
            n += sum(1 for s in scope.get("spans", []) if s.get("name") == name)
    return n


def leaks(texts: Iterable[str], secrets: Iterable[str]) -> list[str]:
    """The secrets (by position, never the value) found in any exported text."""
    blob = "\n".join(texts)
    return [f"secret #{i}" for i, s in enumerate(secrets) if s and s in blob]


def count_spans(trace: dict[str, Any]) -> int:
    n = 0
    for batch in trace.get("batches") or trace.get("resourceSpans") or []:
        for scope in batch.get("scopeSpans") or batch.get("instrumentationLibrarySpans") or []:
            n += len(scope.get("spans", []))
    return n


def service_tokens(folder: str = "secrets") -> list[str]:
    """The service tokens on this machine, to SEARCH FOR in telemetry (never printed)."""
    from pathlib import Path

    out = []
    for p in sorted(Path(folder).glob("*_token.txt")):
        try:
            value = p.read_text().strip()
        except OSError:
            continue
        if len(value) > 20:
            out.append(value)
    return out


# ---------------------------------------------------------------- the smoke


class Smoke:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def ok(self, msg: str) -> None:
        print(f"✔ {msg}")
        self.passed += 1

    def bad(self, msg: str) -> None:
        print(f"✘ {msg}")
        self.failed += 1


def ready(s: Smoke) -> bool:
    good = True
    for name, url in (("tempo", f"{TEMPO}/ready"), ("loki", f"{LOKI}/ready"),
                      ("prometheus", f"{PROM}/-/ready"), ("api", f"{API}/api/v1/health")):  # fmt: skip
        code = status(url)
        if code == 200:
            s.ok(f"{name} ready")
        else:
            s.bad(f"{name} not ready ({code}): make obs-up / make k8s-apply")
            good = False
    return good


STACK = {"tempo": f"{TEMPO}/ready", "loki": f"{LOKI}/ready", "prometheus": f"{PROM}/-/ready",
         "grafana": "http://127.0.0.1:3000/api/health"}  # fmt: skip


def wait_stack(timeout_s: float = 180) -> int:
    """make obs-up: every backend answers ready (Loki and Tempo need ~15-30 s for their ring)."""
    pending, deadline = dict(STACK), time.monotonic() + timeout_s
    while pending and time.monotonic() < deadline:
        pending = {n: u for n, u in pending.items() if status(u) != 200}
        if pending:
            time.sleep(3)
    for name in STACK:
        print(f"  {name:<11} {'NOT ready' if name in pending else 'ready'}")
    return 1 if pending else 0


def restart_orchestrator(runtime: str) -> None:
    if runtime == "kind":
        cmd = ["kubectl", "--context", "kind-aeoi", "-n", "ai-engineering-platform", "delete",
               "pod", "-l", "app.kubernetes.io/name=orchestrator", "--wait=false"]  # fmt: skip
    else:
        cmd = ["docker", "compose", "restart", "orchestrator"]
    subprocess.run(cmd, check=True, capture_output=True)  # noqa: S603 - fixed argument list


def find_trace(investigation_id: str, wait_s: float = 90) -> list[str]:
    q = urllib.parse.urlencode({"q": f'{{ span.aeoi.investigation_id = "{investigation_id}" }}',
                                "limit": 20, "start": int(time.time()) - 3600,
                                "end": int(time.time()) + 60})  # fmt: skip
    deadline = time.monotonic() + wait_s
    while True:
        _, res = http("GET", f"{TEMPO}/api/search?{q}")
        ids = trace_ids(res)
        if ids or time.monotonic() > deadline:
            return ids
        time.sleep(5)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--resume", choices=["kind", "compose"], default=None)
    ap.add_argument("--wait-stack", action="store_true", help="only wait until the stack is ready")
    args = ap.parse_args(argv)
    if args.wait_stack:
        rc = wait_stack()
        if not switch_on():
            print(f"! {SWITCH_OFF}")
        return rc
    if not switch_on():
        print(f"✘ {SWITCH_OFF}")
        return 2
    token = os.environ.get("TOKEN", "")
    if not token:
        print("TOKEN is not set (the make target sets it)")
        return 2
    s = Smoke()
    if not ready(s):
        return 1
    stamp = str(int(time.time()))
    code, inc = http("POST", f"{API}/api/v1/incidents", token, headers={"Idempotency-Key": f"obs23-{stamp}"},
                     body={"title": "Checkout 5xx and DB pool timeouts after deploy (phase 23a obs smoke)",
                           "severity": "SEV2", "affected_services": ["checkout-api"],
                           "detected_at": "2026-10-02T09:50:00Z"})  # fmt: skip
    if code != 201:
        s.bad(f"create incident: {code} {str(inc)[:200]}")
        return 1
    code, start = http("POST", f"{API}/api/v1/incidents/{inc['key']}/investigate", token,
                       headers={"Idempotency-Key": f"obs23-inv-{stamp}", "X-Correlation-ID": f"obs23-{stamp}-corr"})  # fmt: skip
    if code != 202:
        s.bad(f"investigate: {code} {str(start)[:200]}")
        return 1
    inv = start["investigation_id"]
    s.ok(f"investigation {inv} started (correlation obs23-{stamp}-corr)")
    restarted = False
    state, t0 = "RUNNING", time.monotonic()
    while state == "RUNNING" and time.monotonic() - t0 < 600:
        time.sleep(4)
        if args.resume and not restarted and time.monotonic() - t0 > 12:
            restart_orchestrator(args.resume)
            restarted = True
            s.ok("orchestrator restarted mid-run (it must resume the run)")
        code, body = http("GET", f"{API}/api/v1/investigations/{inv}", token)
        state = body.get("status", "?") if code == 200 and isinstance(body, dict) else "RUNNING"
    if state in ("COMPLETE", "PARTIAL"):
        s.ok(f"investigation {state} in {time.monotonic() - t0:.0f} s")
    else:
        s.bad(f"investigation ended {state}")

    ids = find_trace(inv)
    if len(ids) == 1:
        s.ok(f"ONE trace for the investigation: {ids[0]}")
    else:
        s.bad(f"{len(ids)} traces carry aeoi.investigation_id={inv} (want exactly 1): {ids}")
        if not ids:
            _, tags = http("GET", SERVICES_URL)
            known = known_services(tags)
            print(
                f"  Tempo knows these services: {known}" if known else
                "  Tempo has NO traces at all: the pods export nothing (pods deployed before the"
                f" .env switch? check: kubectl ... get deploy orchestrator -o yaml | grep {SWITCH})"
                " or cannot reach otel-collector:4318 (make k8s-netpol-test)"
            )  # fmt: skip
            return 1
    time.sleep(10)  # late spans (the report, the audit consumer) are still being flushed
    _, trace = http("GET", f"{TEMPO}/api/traces/{ids[0]}")
    trace = trace if isinstance(trace, dict) else {}
    seen = services_in(trace)
    missing = REQUIRED - seen
    if missing:
        s.bad(f"services missing from the trace: {sorted(missing)} (seen {sorted(seen)})")
    else:
        s.ok(f"the trace crosses {len(seen)} services: {', '.join(sorted(seen))}")
    for extra in sorted(OPTIONAL - seen):
        print(f"! {extra} not in the trace (its switch off, or no call this run)")
    runs = spans_named(trace, "investigation.run")
    if args.resume:
        (s.ok if runs >= 2 else s.bad)(
            f"{runs} investigation.run spans in ONE trace (first + resume)"
        )
    else:
        (s.ok if runs >= 1 else s.bad)(f"{runs} investigation.run span(s)")

    q = urllib.parse.urlencode({"query": f'{{service_namespace="aeoi"}} | trace_id="{ids[0]}"',
                                "limit": 2000, "start": int(time.time() - 3600) * 10**9})  # fmt: skip
    _, logs = http("GET", f"{LOKI}/loki/api/v1/query_range?{q}")
    streams = (logs or {}).get("data", {}).get("result", []) if isinstance(logs, dict) else []
    log_services = {st["stream"].get("service_name", "?") for st in streams}
    lines = [v[1] for st in streams for v in st["values"]]
    (s.ok if len(log_services) >= 3 else s.bad)(
        f"{len(lines)} log lines with this trace_id from {len(log_services)} services in Loki"
    )
    ours = f"obs23-{stamp}-corr"
    cids = {st["stream"].get("correlation_id") for st in streams} - {None}
    foreign = foreign_correlation(streams, ours)
    (s.ok if ours in cids and not foreign else s.bad)(
        "the trace's log lines carry ONE correlation id, the request's"
        if ours in cids and not foreign
        else f"correlation ids: request's present={ours in cids}, other ids by service {foreign}"
    )

    _, prom = http("GET", f"{PROM}/api/v1/query?" + urllib.parse.urlencode(
        {"query": "sum by (service_name) (http_server_request_duration_seconds_count)"}))  # fmt: skip
    measured = {
        r["metric"].get("service_name") for r in (prom or {}).get("data", {}).get("result", [])
    }
    (s.ok if measured >= REQUIRED else s.bad)(
        f"request metrics in Prometheus for {len(measured & (REQUIRED | OPTIONAL))} services"
        + (f" (missing {sorted(REQUIRED - measured)})" if REQUIRED - measured else "")
    )

    # the WHOLE exported data (stream labels = structured metadata included, not only the
    # bodies) against the user token AND every service token this machine holds (review)
    secrets = [token, *service_tokens()]
    found = leaks([json.dumps(trace), json.dumps(streams)], secrets)
    (s.bad if found else s.ok)(
        f"none of {len(secrets)} tokens in the trace or its log lines" if not found
        else f"TOKEN FOUND in telemetry: {found}")  # fmt: skip
    print(f"  trace size: {count_spans(trace)} spans (Tempo drops spans above 5 MB per trace)")
    print(f"passed={s.passed} failed={s.failed}   investigation={inv} trace={ids[0]}")
    print(f"open it: Grafana http://127.0.0.1:3000 -> Explore -> Tempo -> {ids[0]}")
    return 1 if s.failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
