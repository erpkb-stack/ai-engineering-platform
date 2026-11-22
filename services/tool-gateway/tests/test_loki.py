"""Phase 32a: search_logs from a real Loki - the same contract as devdata (exact counts over the
whole window, one order, the limit), literal filters only, loud failures. A fake Loki speaks its
HTTP API; the real one is tested on the Mac (docs/phases/phase-32.md)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from aeoi_tools import schemas as s
from aeoi_tools.adapters.http import make_client
from aeoi_tools.adapters.loki import LokiLogsAdapter, count_query, log_query
from aeoi_tools.contracts import EgressBlockedError, ToolExecutionError, ToolUnavailableError

BASE = "http://localhost:3100"
T0 = datetime(2026, 11, 21, 10, 0, tzinfo=UTC)


def ns(minute: int) -> str:
    return str(int((T0 + timedelta(minutes=minute)).timestamp() * 1e9))


def streams(*result: dict[str, Any]) -> dict[str, Any]:
    return {"status": "success", "data": {"resultType": "streams", "result": list(result)}}


def vector(counts: dict[str, int]) -> dict[str, Any]:
    result = [{"metric": {"code": c}, "value": [T0.timestamp(), str(n)]} for c, n in counts.items()]
    return {"status": "success", "data": {"resultType": "vector", "result": result}}


def fake(lines: dict[str, Any], counts: dict[str, Any], seen: list[httpx.Request] | None = None):  # type: ignore[no-untyped-def]
    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(req)
        if req.url.path == "/loki/api/v1/query_range":
            return httpx.Response(200, json=lines)
        if req.url.path == "/loki/api/v1/query":
            return httpx.Response(200, json=counts)
        return httpx.Response(404)

    return handler


def adapter(handler: Any, allowed: tuple[str, ...] = ("localhost:3100",),
            names: dict[str, str] | None = None) -> LokiLogsAdapter:  # fmt: skip
    client = make_client(frozenset(allowed), timeout_s=2.0, transport=httpx.MockTransport(handler))
    return LokiLogsAdapter(client, BASE, names)


def q(**kw: Any) -> s.LogsIn:
    base: dict[str, Any] = {"service_key": "incident-service", "start": T0,
                            "end": T0 + timedelta(minutes=30)}  # fmt: skip
    return s.LogsIn(**{**base, **kw})


async def test_streams_are_merged_into_one_order_and_counts_are_exact() -> None:
    seen: list[httpx.Request] = []
    lines = streams(
        {"stream": {"service_name": "incident-service", "severity_text": "WARNING",
                    "trace_id": "abc"}, "values": [[ns(5), "catalog_timeout"]]},
        {"stream": {"service_name": "incident-service", "severity_text": "ERROR"},
         "values": [[ns(9), "upstream_timeout"], [ns(2), "upstream_timeout"]]},
    )  # fmt: skip
    out = await adapter(fake(lines, vector({"upstream_timeout": 40, "catalog_timeout": 7}),
                             seen)).search_logs(q(limit=2, order="desc"))  # fmt: skip
    assert [(i.ts, i.level, i.error_code) for i in out.items] == [
        (T0 + timedelta(minutes=9), "ERROR", "upstream_timeout"),
        (T0 + timedelta(minutes=5), "WARN", "catalog_timeout"),
    ]  # newest first (desc), across streams, then the limit
    assert out.items[1].trace_id == "abc"
    assert out.items[1].message == "catalog_timeout"
    assert out.total_matching == 47 and out.distinct_codes == 2 and out.truncated
    assert out.counts_by_error_code == {"upstream_timeout": 40, "catalog_timeout": 7}
    rng, inst = seen
    assert rng.url.params["direction"] == "backward"
    assert rng.url.params["start"] == ns(0) and rng.url.params["end"] == ns(30)
    assert inst.url.params["time"] == ns(30)
    assert "[1800s]" in inst.url.params["query"]  # the WHOLE window, one instant query


async def test_the_default_order_is_onset_first_like_devdata() -> None:
    seen: list[httpx.Request] = []
    lines = streams({"stream": {"severity_text": "ERROR"},
                     "values": [[ns(9), "b"], [ns(2), "a"]]})  # fmt: skip
    out = await adapter(fake(lines, vector({"a": 1, "b": 1}), seen)).search_logs(q())
    assert seen[0].url.params["direction"] == "forward"
    assert [i.message for i in out.items] == ["a", "b"]


async def test_no_lines_is_an_empty_answer_not_an_error() -> None:
    out = await adapter(fake(streams(), vector({}))).search_logs(q())
    assert out.items == [] and out.total_matching == 0 and not out.truncated


def test_min_level_maps_to_the_severities_structlog_writes() -> None:
    assert "severity_text=~`WARNING|WARN|ERROR|EXCEPTION|CRITICAL|FATAL`" in log_query(q(), "x")
    assert "`CRITICAL|FATAL`" in log_query(q(min_level="FATAL"), "x")
    assert "`INFO|WARNING" in log_query(q(min_level="INFO"), "x")


def test_filters_are_literals_never_patterns() -> None:
    query = log_query(q(error_code="db.pool-timeout", contains="a.*b`x"), "svc")
    assert r"|~ `^db\.pool\-timeout$`" in query
    assert r"|~ `(?i)a\.\*b\x60x`" in query  # a backtick cannot end the LogQL string
    assert query.count("`") == 6


def test_a_quote_in_a_name_cannot_reach_logql() -> None:
    query = log_query(q(), 'x"} | foo="')
    assert query.startswith('{service_name="x} | foo="}')
    assert query.count('"') == 2  # severity and filters use backticks: only the name's quotes


def test_the_count_query_groups_by_the_line() -> None:
    query = count_query(q(), "svc")
    assert query.startswith("sum by (code) (count_over_time(")
    assert 'label_format code="{{ __line__ }}"' in query


async def test_the_service_key_maps_to_the_otel_service_name() -> None:
    seen: list[httpx.Request] = []
    await adapter(fake(streams(), vector({}), seen),
                  names={"aeoi-incident": "incident-service"}).search_logs(
        q(service_key="aeoi-incident"))  # fmt: skip
    assert seen[0].url.params["query"].startswith('{service_name="incident-service"}')


@pytest.mark.parametrize(
    ("lines", "counts", "error"),
    [
        (streams(), {"status": "success", "data": {"resultType": "matrix", "result": []}},
         ToolExecutionError),
        ({"status": "success", "data": {"resultType": "matrix", "result": []}}, vector({}),
         ToolExecutionError),
        (streams({"stream": {}, "values": [[ns(1)]]}), vector({}), ToolExecutionError),
        ({"nope": 1}, vector({}), ToolExecutionError),
    ],
)  # fmt: skip
async def test_a_broken_answer_is_loud(lines: Any, counts: Any, error: type[Exception]) -> None:
    with pytest.raises(error):
        await adapter(fake(lines, counts)).search_logs(q())


@pytest.mark.parametrize(
    ("status", "error"),
    [(503, ToolUnavailableError), (429, ToolUnavailableError), (400, ToolExecutionError)],
)
async def test_http_errors(status: int, error: type[Exception]) -> None:
    with pytest.raises(error):
        await adapter(lambda _: httpx.Response(status)).search_logs(q())


async def test_a_timeout_is_unavailable_and_egress_is_enforced() -> None:
    def timeout(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(ToolUnavailableError):
        await adapter(timeout).search_logs(q())
    with pytest.raises(EgressBlockedError):
        await adapter(fake(streams(), vector({})), allowed=("localhost:9090",)).search_logs(q())


def test_set_routes_only_search_logs_to_loki() -> None:
    from pathlib import Path

    from aeoi_tools.adapters.catalog import CatalogAdapter
    from aeoi_tools.registry import build_registry

    catalog = CatalogAdapter(Path("/nonexistent"))
    assert build_registry(catalog, None, None)["search_logs"].dependency == "devdata"  # type: ignore[arg-type]
    loki = adapter(fake(streams(), vector({})))
    reg = build_registry(catalog, None, None, logs=loki, logs_dependency="loki")  # type: ignore[arg-type]
    assert {n for n, t in reg.items() if t.dependency == "loki"} == {"search_logs"}
    assert reg["query_metrics"].dependency == "devdata"
