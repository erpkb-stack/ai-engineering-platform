"""OpenTelemetry for every AEOI service (Phase 23a, ADR-035): traces, metrics, logs over OTLP.

    tel = setup_telemetry("api", environment="development")   # None when switched off

OFF unless AEOI_OTEL_ENDPOINT is set (e.g. http://otel-collector:4318): tests, host runs and
the smokes run exactly as before. Rules that make it safe:
- NO request/response header or body is ever captured (the instrumentation defaults, kept);
  the collector also deletes header attributes and blocks token-shaped values (defence twice).
- A ROOT client span is dropped: readiness DB pings, the outbox poll, a health call between
  services start a trace of their own and would be noise. Every real trace starts at a server
  span (a request), a consumer span (Kafka) or an investigation run - those are kept.
- /health/* is never traced or measured (kubelet probes every 5-10 s on every pod).
- Export is batched and non-blocking: a dead collector drops telemetry, never a request.
- A span keeps an exception's TYPE only: messages, stack traces and status descriptions can hold
  DB rows, user input or tokens (ScrubbingExporter; the collector repeats it).
- The api's public /api/ routes ignore an incoming traceparent (EdgePropagator).
- Logs: structlog lines go to stdout as before AND to the OTel log pipeline, after redaction,
  with the trace and span id of the current span.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator, Mapping, MutableMapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from opentelemetry import context, propagate, trace
from opentelemetry._logs import SeverityNumber, set_logger_provider
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.propagators.textmap import TextMapPropagator
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, LogRecordExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import (
    Decision,
    ParentBased,
    Sampler,
    SamplingResult,
)
from opentelemetry.trace import Link, SpanKind, Status
from opentelemetry.util.types import Attributes

from aeoi_common.correlation import get_correlation_id, set_correlation_id

ENDPOINT_ENV = "AEOI_OTEL_ENDPOINT"
# regexes searched in the full URL by the FastAPI instrumentation (spans AND metrics)
EXCLUDED_URLS = "/health/live,/health/ready,/api/v1/health"
TRACEPARENT = "traceparent"
_MAX_ATTR = 1000


class DropRootClients(Sampler):
    """Root sampler: keep server / consumer / internal roots, drop a client span with no
    parent (a background DB ping or health call is not the start of anything)."""

    def should_sample(
        self,
        parent_context: context.Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        trace_state: Any = None,
    ) -> SamplingResult:
        if kind in (SpanKind.CLIENT, SpanKind.PRODUCER):
            return SamplingResult(Decision.DROP)
        return SamplingResult(Decision.RECORD_AND_SAMPLE, attributes)

    def get_description(self) -> str:
        return "DropRootClients"


def sampler() -> Sampler:
    """Children follow their parent; roots by kind (DropRootClients)."""
    return ParentBased(root=DropRootClients())


@dataclass
class Telemetry:
    service_name: str
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    logger_provider: LoggerProvider

    def shutdown(self) -> None:
        for p in (self.tracer_provider, self.meter_provider, self.logger_provider):
            with contextlib.suppress(Exception):  # shutting down: nothing useful to do with it
                p.shutdown()


# exception text can hold whole DB rows (psycopg: "DETAIL: Failing row contains (...)"), user
# input or a token: a span keeps the exception TYPE only (independent review, Phase 23a)
_DROP_EVENT_KEYS = ("exception.message", "exception.stacktrace", "exception.escaped")


def scrub(span: ReadableSpan) -> ReadableSpan:
    """A copy without exception messages, stack traces and status descriptions."""
    events = [
        Event(
            e.name,
            {k: v for k, v in (e.attributes or {}).items() if k not in _DROP_EVENT_KEYS},
            e.timestamp,
        )
        for e in span.events
    ]
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=span.attributes,
        events=events,
        links=span.links,
        kind=span.kind,
        status=Status(span.status.status_code),  # the description is the exception text
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class ScrubbingExporter(SpanExporter):
    def __init__(self, inner: SpanExporter) -> None:
        self.inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self.inner.export([scrub(s) for s in spans])

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self.inner.force_flush(timeout_millis)


class EdgePropagator(TextMapPropagator):
    """At the PUBLIC edge (the api's /api/ routes) an incoming traceparent is ignored: any
    client could otherwise join another investigation's trace or send flag 00 and switch the
    whole investigation's tracing off (review). Internal calls keep continuing the trace."""

    def __init__(self, inner: TextMapPropagator, untrusted_prefixes: Sequence[str]) -> None:
        self.inner = inner
        self.untrusted = tuple(untrusted_prefixes)

    def extract(self, carrier: Any, context: Context | None = None, getter: Any = None) -> Context:
        if isinstance(carrier, dict) and str(carrier.get("path", "")).startswith(self.untrusted):
            return context if context is not None else Context()
        if getter is None:
            return self.inner.extract(carrier, context)
        return self.inner.extract(carrier, context, getter)

    def inject(self, carrier: Any, context: Context | None = None, setter: Any = None) -> None:
        if setter is None:
            self.inner.inject(carrier, context)
        else:
            self.inner.inject(carrier, context, setter)

    @property
    def fields(self) -> set[str]:
        return set(self.inner.fields)


_ACTIVE: Telemetry | None = None
_otel_logger: Any = None


def active() -> Telemetry | None:
    return _ACTIVE


def setup_telemetry(
    service_name: str,
    *,
    environment: str = "development",
    endpoint: str | None = None,
    span_exporter: SpanExporter | None = None,
    log_exporter: LogRecordExporter | None = None,
    metric_reader: MetricReader | None = None,
    untrusted_trace_paths: Sequence[str] = (),
) -> Telemetry | None:
    """Once per process (a second call returns the first setup). Exporters can be passed in
    (tests); otherwise OTLP/HTTP to `endpoint` or $AEOI_OTEL_ENDPOINT, or nothing at all."""
    global _ACTIVE, _otel_logger
    if _ACTIVE is not None:
        _edge(untrusted_trace_paths)  # the app factory may come after an earlier setup
        return _ACTIVE
    url = (endpoint if endpoint is not None else os.environ.get(ENDPOINT_ENV, "")).strip()
    if not url and span_exporter is None:
        return None
    url = url.rstrip("/")
    # the STABLE http semantic conventions: http.server.request.duration (seconds) - the
    # dashboards depend on these names (tests/unit checks them)
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")
    resource = Resource.create({
        "service.name": service_name,
        "service.namespace": "aeoi",
        "deployment.environment.name": environment,
    })  # fmt: skip
    tp = TracerProvider(resource=resource, sampler=sampler())
    tp.add_span_processor(
        BatchSpanProcessor(
            ScrubbingExporter(
                span_exporter or OTLPSpanExporter(endpoint=f"{url}/v1/traces", timeout=5)
            )
        )
    )
    _edge(untrusted_trace_paths)
    reader = metric_reader or PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{url}/v1/metrics", timeout=5), export_interval_millis=15_000
    )
    mp = MeterProvider(resource=resource, metric_readers=[reader])
    lp = LoggerProvider(resource=resource)
    lp.add_log_record_processor(
        BatchLogRecordProcessor(
            log_exporter or OTLPLogExporter(endpoint=f"{url}/v1/logs", timeout=5)
        )
    )
    trace.set_tracer_provider(tp)
    from opentelemetry import metrics

    metrics.set_meter_provider(mp)
    set_logger_provider(lp)
    _otel_logger = lp.get_logger("aeoi")
    _instrument_clients(tp, mp)
    _ACTIVE = Telemetry(service_name, tp, mp, lp)
    return _ACTIVE


REDACTED_QUERY = "[query removed]"


def server_no_query(span: Any, scope: dict[str, Any]) -> None:
    """FastAPI server_request_hook: a query string may hold anything a client put there (a
    cursor today, a token by mistake tomorrow) - it never reaches a span."""
    if span is not None and span.is_recording() and scope.get("query_string"):
        span.set_attribute("url.query", REDACTED_QUERY)


def _client_no_query(span: Any, request: Any) -> None:
    url = getattr(request, "url", None)
    if span is not None and span.is_recording() and url is not None and getattr(url, "query", b""):
        span.set_attribute("url.full", str(url.copy_with(query=None)))


async def _async_client_no_query(span: Any, request: Any) -> None:
    _client_no_query(span, request)


def _edge(untrusted: Sequence[str]) -> None:
    current = propagate.get_global_textmap()
    if untrusted and not isinstance(current, EdgePropagator):
        propagate.set_global_textmap(EdgePropagator(current, untrusted))


def _instrument_clients(tp: TracerProvider, mp: MeterProvider) -> None:
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor

    HTTPXClientInstrumentor().instrument(
        tracer_provider=tp,
        meter_provider=mp,
        request_hook=_client_no_query,
        async_request_hook=_async_client_no_query,
    )
    # psycopg 3 (sync + async): db.statement is the SQL with placeholders, never the values
    PsycopgInstrumentor().instrument(tracer_provider=tp, enable_commenter=False)


# ---------------------------------------------------------------- logs


_LEVELS = {
    "debug": SeverityNumber.DEBUG, "info": SeverityNumber.INFO, "warning": SeverityNumber.WARN,
    "warn": SeverityNumber.WARN, "error": SeverityNumber.ERROR, "critical": SeverityNumber.FATAL,
    "exception": SeverityNumber.ERROR,
}  # fmt: skip
_NOT_ATTRIBUTES = {"event", "level", "timestamp", "exception"}


def _attr(value: Any) -> Any:
    if isinstance(value, bool | int | float):
        return value
    return str(value)[:_MAX_ATTR]


def add_trace_ids(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: the current span's ids on every line (stdout JSON too)."""
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        event_dict.setdefault("trace_id", format(ctx.trace_id, "032x"))
        event_dict.setdefault("span_id", format(ctx.span_id, "016x"))
    return event_dict


def to_otel(
    _logger: Any, method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor AFTER redaction: the same (redacted) line to the OTel log pipeline.
    Never raises - a telemetry problem must not break logging."""
    if _otel_logger is None:
        return event_dict
    try:
        level = str(event_dict.get("level", method)).lower()
        _otel_logger.emit(
            severity_number=_LEVELS.get(level, SeverityNumber.INFO),
            severity_text=level.upper(),
            body=str(event_dict.get("event", ""))[:_MAX_ATTR],
            attributes={k: _attr(v) for k, v in event_dict.items() if k not in _NOT_ATTRIBUTES},
        )
    except Exception:  # noqa: S110 - see docstring
        pass
    return event_dict


# ---------------------------------------------------------------- context carried by data


def current_traceparent() -> str | None:
    """The W3C traceparent of the current span, to STORE with work that runs later
    (an investigation, an outbox event). None outside a recording span."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier.get(TRACEPARENT)


def context_from(traceparent: str | None) -> context.Context:
    return propagate.extract({TRACEPARENT: traceparent} if traceparent else {})


@contextmanager
def continue_from(traceparent: str | None, correlation_id: str | None) -> Iterator[None]:
    """Run the block as part of the STORED trace and correlation id (a resumed investigation
    has no request of its own: before Phase 23 it ran with no correlation id at all)."""
    import structlog

    token = context.attach(context_from(traceparent))
    previous = get_correlation_id()
    if correlation_id:
        set_correlation_id(correlation_id)
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
    try:
        yield
    finally:
        context.detach(token)
        set_correlation_id(previous)
        if previous:
            structlog.contextvars.bind_contextvars(correlation_id=previous)
        else:
            structlog.contextvars.unbind_contextvars("correlation_id")


def headers_with_context(headers: Mapping[str, str]) -> dict[str, str]:
    """Outbox headers + the traceparent of NOW (inside the request that made the change)."""
    out = dict(headers)
    tp = current_traceparent()
    if tp:
        out[TRACEPARENT] = tp
    return out


def tracer(name: str = "aeoi") -> trace.Tracer:
    return trace.get_tracer(name)
