"""Phase 32a: query_metrics from a real Prometheus - the same contract as devdata, loud failures,
"no series" (never a zero) for a metric with no real source. A fake Prometheus speaks its HTTP
API; the real one is tested on the Mac (docs/phases/phase-32.md)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from aeoi_tools import schemas as s
from aeoi_tools.adapters.http import make_client
from aeoi_tools.adapters.metric_stats import bucket_seconds
from aeoi_tools.adapters.prometheus import PrometheusMetricsAdapter, promql
from aeoi_tools.contracts import EgressBlockedError, ToolExecutionError, ToolUnavailableError

BASE = "http://localhost:9090"
T0 = datetime(2026, 11, 21, 10, 0, tzinfo=UTC)


def adapter(handler: Any, allowed: tuple[str, ...] = ("localhost:9090",),
            names: dict[str, str] | None = None) -> PrometheusMetricsAdapter:  # fmt: skip
    client = make_client(frozenset(allowed), timeout_s=2.0, transport=httpx.MockTransport(handler))
    return PrometheusMetricsAdapter(client, BASE, names)


def q(metric: str = "requests_per_sec", minutes: int = 10, svc: str = "api") -> s.MetricsIn:
    return s.MetricsIn(service_key=svc, metric=metric, start=T0,
                       end=T0 + timedelta(minutes=minutes))  # fmt: skip


def matrix(values: list[tuple[float, str]]) -> dict[str, Any]:
    result = [{"metric": {}, "values": [list(v) for v in values]}] if values else []
    return {"status": "success", "data": {"resultType": "matrix", "result": result}}


async def test_points_are_labelled_with_the_bucket_start_and_nan_is_no_point() -> None:
    seen: list[httpx.Request] = []
    end1, end2, end3 = (T0 + timedelta(minutes=m) for m in (1, 2, 3))

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=matrix([(end1.timestamp(), "1.5"),
                                                (end2.timestamp(), "NaN"),
                                                (end3.timestamp(), "3.25")]))  # fmt: skip

    out = await adapter(handler).query_metrics(q())
    (item,) = out.items
    assert item.bucket_seconds == 60
    assert [p.ts for p in item.points] == [T0, T0 + timedelta(minutes=2)]
    assert [p.value for p in item.points] == [1.5, 3.25]
    assert item.stats == {"min": 1.5, "max": 3.25, "mean": 2.375, "p95": 3.25}
    params = seen[0].url.params
    assert seen[0].url.path == "/api/v1/query_range"
    assert params["step"] == "60s"
    assert float(params["start"]) == (T0 + timedelta(minutes=1)).timestamp()
    assert 'service_name="api"' in params["query"]


async def test_a_metric_with_no_real_source_is_no_series_and_no_call() -> None:
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json=matrix([]))

    for metric in ("cpu_util", "db_pool_utilization", "db_connections_active"):
        assert (await adapter(handler).query_metrics(q(metric))).items == []
    assert calls == []


async def test_an_empty_answer_is_no_series_not_zero() -> None:
    out = await adapter(lambda _: httpx.Response(200, json=matrix([]))).query_metrics(q())
    assert out.items == []


def test_5xx_is_zero_only_where_the_service_reported_requests() -> None:
    query = promql("http_5xx_per_min", "api", 120)
    assert query is not None
    assert 'http_response_status_code=~"5.."' in query
    assert " or (sum(increase(" in query and "* 0)" in query
    assert query.endswith("* 0.5")  # per minute from a 120 s bucket


def test_p95_is_milliseconds_from_the_histogram() -> None:
    query = promql("p95_latency_ms", "api", 60)
    assert query is not None
    assert query.startswith("histogram_quantile(0.95, sum by (le)")
    assert query.endswith("* 1000")


def test_a_quote_in_a_name_cannot_reach_promql() -> None:
    query = promql("requests_per_sec", 'x"} or vector(1) or {a="', 60)
    assert query is not None
    assert query.count('"') == 2


async def test_the_service_key_maps_to_the_otel_service_name() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=matrix([]))

    await adapter(handler, names={"aeoi-api": "api"}).query_metrics(q(svc="aeoi-api"))
    assert 'service_name="api"' in seen[0].url.params["query"]


def test_the_bucket_rule_is_the_devdata_rule() -> None:
    assert bucket_seconds(T0, T0 + timedelta(hours=24), 200) == 480
    assert bucket_seconds(T0, T0 + timedelta(minutes=5), 200) == 60


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (httpx.Response(503), ToolUnavailableError),
        (httpx.Response(429), ToolUnavailableError),
        (httpx.Response(400, json={"status": "error", "error": "parse"}), ToolExecutionError),
        (httpx.Response(200, content=b"not json"), ToolExecutionError),
        (httpx.Response(200, json={"status": "error", "error": "x"}), ToolExecutionError),
        (httpx.Response(200, json={"status": "success",
                                   "data": {"resultType": "vector", "result": []}}),
         ToolExecutionError),
        (httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix",
                                   "result": [{"values": []}, {"values": []}]}}),
         ToolExecutionError),
    ],
)  # fmt: skip
async def test_failures_are_loud(answer: httpx.Response, error: type[Exception]) -> None:
    with pytest.raises(error):
        await adapter(lambda _: answer).query_metrics(q())


async def test_a_timeout_and_a_dead_host_are_unavailable() -> None:
    def timeout(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    def refused(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    for handler in (timeout, refused):
        with pytest.raises(ToolUnavailableError):
            await adapter(handler).query_metrics(q())


async def test_prometheus_must_be_in_the_egress_allowlist() -> None:
    with pytest.raises(EgressBlockedError):
        await adapter(lambda _: httpx.Response(200, json=matrix([])),
                      allowed=("localhost:8010",)).query_metrics(q())  # fmt: skip


def test_unset_keeps_devdata_and_set_routes_only_query_metrics() -> None:
    """32a: the planted demo and every old test stay on devdata unless the URL is set; a
    Prometheus outage opens its OWN breaker, never devdata's (the logs tool keeps working)."""
    from pathlib import Path

    from aeoi_tools.adapters.catalog import CatalogAdapter
    from aeoi_tools.registry import build_registry

    catalog = CatalogAdapter(Path("/nonexistent"))
    old = build_registry(catalog, None, None)  # type: ignore[arg-type]
    assert old["query_metrics"].dependency == "devdata"
    prom = adapter(lambda _: httpx.Response(200, json=matrix([])))
    new = build_registry(catalog, None, None, metrics=prom,  # type: ignore[arg-type]
                         metrics_dependency="prometheus")  # fmt: skip
    assert new["query_metrics"].dependency == "prometheus"
    assert {n for n, t in new.items() if t.dependency == "prometheus"} == {"query_metrics"}
    assert new["search_logs"].dependency == "devdata"
