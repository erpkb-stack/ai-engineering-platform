"""Run by tests/unit/test_telemetry.py in a SUBPROCESS (OTel providers are process-global).

    python tests/unit/telemetry_probe.py normal|failing   -> JSON on stdout

Drives real code paths with in-memory exporters and PLANTED secrets, then prints everything
that was exported, so the test can assert on it. Not collected by pytest (no test_ prefix).
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from typing import Any

import httpx
from fastapi import APIRouter, Request
from opentelemetry import trace
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# A real-SHAPED JWT (the collector's pattern must match it), built at runtime: a token-shaped
# literal in the source is what gitleaks blocks - rightly; it cannot tell a planted fake (fix8).
SECRET_BEARER = ".".join(
    _b64url(part) for part in (b'{"alg":"RS256"}', b'{"sub":"planted"}', b"signature-planted")
)
SECRET_PW = "planted-password-7f3a9c"
SECRET_TEXT = "planted user text: the patient record of Jane Roe"
SECRET_QUERY = "planted-query-token-55aa"
FOREIGN_TRACE = "11111111111111111111111111111111"  # a trace id a CLIENT picked
ENGINE_STORED = "00-22222222222222222222222222222222-3333333333333333-01"


class Failing(SpanExporter):
    def export(self, spans: Any) -> SpanExportResult:
        raise RuntimeError("collector down")

    def shutdown(self) -> None:
        return None


def main(mode: str) -> dict[str, Any]:
    from aeoi_observability import get_logger
    from aeoi_observability.telemetry import (
        continue_from,
        current_traceparent,
        headers_with_context,
        setup_telemetry,
    )

    spans = InMemorySpanExporter()
    logs = InMemoryLogRecordExporter()
    metrics = InMemoryMetricReader()
    tel = setup_telemetry(
        "probe-svc",
        span_exporter=Failing() if mode == "failing" else spans,
        log_exporter=logs,
        metric_reader=metrics,
    )
    assert tel is not None
    from aeoi_web.app import create_app

    log = get_logger("probe")
    router = APIRouter()
    out: dict[str, Any] = {}

    @router.post("/echo")
    async def echo(request: Request) -> dict[str, str]:
        body = await request.json()
        log.info("echo_called", password=body["password"], note="ok")
        # a REAL transport (the instrumented one) to a closed port: refused at once
        async with httpx.AsyncClient(timeout=2) as c:
            try:
                await c.get(f"http://127.0.0.1:9/thing?token={SECRET_QUERY}")
            except httpx.HTTPError:
                out["downstream"] = "refused"
        out["traceparent_in_request"] = current_traceparent()
        out["outbox_headers"] = headers_with_context({"correlation_id": "x"})
        return {"ok": "yes"}

    @router.post("/boom")
    async def boom() -> None:
        # psycopg puts whole rows into its messages: an exception span must keep the TYPE only
        with trace.get_tracer("probe").start_as_current_span("db op"):
            raise RuntimeError(f"DETAIL: Failing row contains ({SECRET_TEXT})")

    @router.get("/api/v1/edge")
    async def edge() -> dict[str, str]:
        return {"ok": "edge"}

    @router.get("/internal/v1/trusted")
    async def trusted() -> dict[str, str]:
        return {"ok": "trusted"}

    app = create_app(service_name="probe-svc", version="0", routers=[router], authenticator=None,
                     untrusted_trace_paths=("/api/",))  # fmt: skip

    async def run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://probe") as c:
            r = await c.post(
                f"/echo?token={SECRET_QUERY}",
                json={"password": SECRET_PW, "text": SECRET_TEXT},
                headers={
                    "Authorization": f"Bearer {SECRET_BEARER}",
                    "X-Correlation-ID": "corr-probe-0001",
                },
            )
            out["echo_status"] = r.status_code
            out["health_status"] = (await c.get("/health/live")).status_code
            out["boom_status"] = (await c.post("/boom")).status_code
            named = {"traceparent": f"00-{FOREIGN_TRACE}-00f067aa0ba902b7-01"}
            out["edge_status"] = (await c.get("/api/v1/edge", headers=named)).status_code
            out["trusted_status"] = (await c.get("/internal/v1/trusted", headers=named)).status_code
        # a client call with NO parent (a background ping): must be dropped
        async with httpx.AsyncClient(timeout=2) as c:
            try:
                await c.get("http://127.0.0.1:9/background-ping")
            except httpx.HTTPError:
                out["background"] = "refused"
        # a resumed run: continue a STORED context and correlation id
        stored = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
        with (
            continue_from(stored, "corr-stored-0002"),
            trace.get_tracer("probe").start_as_current_span("investigation.run"),
        ):
            log.info("resumed_step")
        # Kafka: a consumer continues the traceparent header
        from aeoi_messaging import Consumer, MemoryBroker, MemoryTransport

        broker = MemoryBroker()
        handled: list[str] = []

        async def handler(msg: Any) -> None:
            handled.append(format(trace.get_current_span().get_span_context().trace_id, "032x"))
            log.info("consumed")

        broker.produce("incident.lifecycle", b"k", b'{"event_id": "e1"}', (
            ("traceparent", b"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"),
            ("correlation_id", b"corr-kafka-0003"),
        ))  # fmt: skip
        cons = Consumer("audit", MemoryTransport(broker, "audit", ["incident.lifecycle"]), handler)
        await cons.transport.start()
        await cons.run_once()
        out["consumer_trace"] = handled
        # the REAL Engine._run (not a copy of its `with` block) resumes a stored investigation
        from types import SimpleNamespace
        from uuid import uuid4

        from aeoi_orchestrator.engine import Engine

        class Store:
            async def get(self, _i: Any) -> Any:
                return SimpleNamespace(status="RUNNING", delegation_grant_id=uuid4(),
                                       incident_id=uuid4(), trace_context=ENGINE_STORED,
                                       correlation_id="corr-engine-0005")  # fmt: skip

            async def begin_run(self, _i: Any, _d: float) -> Any:
                import datetime as dt

                return dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)

        class Graph:
            async def aget_state(self, _c: Any) -> Any:
                return SimpleNamespace(values={"x": 1}, next=("agents",))

            async def ainvoke(self, *_a: Any, **_k: Any) -> None:
                log.info("engine_node_ran")

        engine = Engine(Graph(), Store(), delegation=None, max_concurrent=1, deadline_s=60)  # type: ignore[arg-type]
        out["engine_investigation"] = str(uuid4())
        from uuid import UUID

        await engine._run(UUID(out["engine_investigation"]), {})

    asyncio.run(run())
    tel.tracer_provider.force_flush()
    tel.logger_provider.force_flush()

    def span_doc(s: ReadableSpan) -> dict[str, Any]:
        ctx = s.get_span_context()
        return {
            "name": s.name,
            "kind": s.kind.name,
            "trace_id": format(ctx.trace_id, "032x") if ctx else "",
            "parent": format(s.parent.span_id, "016x") if s.parent else None,
            "attributes": {k: str(v) for k, v in (s.attributes or {}).items()},
            "events": [
                {"name": e.name, "attributes": {k: str(v) for k, v in (e.attributes or {}).items()}}
                for e in s.events
            ],
            "status": {"code": s.status.status_code.name, "description": s.status.description},
        }

    out["spans"] = [span_doc(s) for s in spans.get_finished_spans()]
    out["logs"] = []
    for rec in logs.get_finished_logs():
        lr = rec.log_record
        out["logs"].append({
            "body": str(lr.body),
            "attributes": {k: str(v) for k, v in (lr.attributes or {}).items()},
            "trace_id": format(lr.trace_id or 0, "032x"),
        })  # fmt: skip
    data = metrics.get_metrics_data()
    names = set()
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                names.add(m.name)
    out["metrics"] = sorted(names)
    return out


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1] if len(sys.argv) > 1 else "normal")))
