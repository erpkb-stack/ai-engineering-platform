"""query_metrics from a real Prometheus (Phase 32a): the SAME tool contract as devdata.

What Prometheus holds here (ADR-035): only the OTel HTTP server histogram
`http_server_request_duration_seconds` (OTLP push, label `service_name`). So 3 of the contract's
metrics have a real source; every other metric answers "no series" - the metrics agent turns
that into a NOTE, never a fact (absence of a series is not a measurement). Owner decision 32a-1.

Buckets: the devdata rule (whole minutes, >= 60 s, <= max_points). Each point is the value
OVER its bucket ([bucket] range, evaluated at the bucket's end) and is labelled with the
bucket's START, like devdata's `date_bin`. 5xx: zero when the service reported requests in the
bucket and none were 5xx; no point when it reported nothing ("no data, not zero").
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import BaseModel, ValidationError

from aeoi_tools import schemas as s
from aeoi_tools.adapters.metric_stats import bucket_seconds, series_stats
from aeoi_tools.contracts import ToolExecutionError, ToolUnavailableError

HISTOGRAM = "http_server_request_duration_seconds"


def _sel(service: str, extra: str = "") -> str:
    # service names come from config or the validated SERVICE_KEY pattern ([a-z0-9-]); quoted
    # defensively anyway (a quote or backslash can never reach PromQL)
    safe = service.replace("\\", "").replace('"', "")
    return f'{{service_name="{safe}"{extra}}}'


def promql(metric: str, service: str, bucket_s: int) -> str | None:
    """The PromQL for one contract metric, or None = no real source for it."""
    w = f"[{bucket_s}s]"
    count = f"{HISTOGRAM}_count"
    if metric == "requests_per_sec":
        return f"sum(rate({count}{_sel(service)}{w}))"
    if metric == "http_5xx_per_min":
        per_min = 60 / bucket_s
        five_xx = _sel(service, ',http_response_status_code=~"5.."')
        errors = f"sum(increase({count}{five_xx}{w}))"
        any_req = f"(sum(increase({count}{_sel(service)}{w})) * 0)"
        return f"({errors} or {any_req}) * {per_min:g}"
    if metric == "p95_latency_ms":
        return (f"histogram_quantile(0.95, sum by (le) "
                f"(rate({HISTOGRAM}_bucket{_sel(service)}{w}))) * 1000")  # fmt: skip
    return None


class _Series(BaseModel):
    metric: dict[str, str] = {}
    values: list[tuple[float, str]] = []


class _Data(BaseModel):
    resultType: str  # noqa: N815 - Prometheus' field name
    result: list[_Series] = []


class _Answer(BaseModel):
    status: str
    data: _Data | None = None
    error: str | None = None


class PrometheusMetricsAdapter:
    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        service_names: dict[str, str] | None = None,
    ) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        # catalog service_key -> the OTel service.name; unmapped = the same name
        self._names = dict(service_names or {})

    async def query_metrics(self, q: s.MetricsIn) -> s.MetricsOut:
        bucket = bucket_seconds(q.start, q.end, q.max_points)
        query = promql(q.metric, self._names.get(q.service_key, q.service_key), bucket)
        if query is None:
            return s.MetricsOut(items=[])
        step = timedelta(seconds=bucket)
        params = {
            "query": query,
            # evaluate at each bucket's END; the first bucket ends one step after the start
            "start": f"{(q.start + step).timestamp():.3f}",
            "end": f"{q.end.timestamp():.3f}",
            "step": f"{bucket}s",
        }
        try:
            resp = await self._client.get(f"{self._base}/api/v1/query_range", params=params)
        except httpx.TimeoutException as exc:
            raise ToolUnavailableError("prometheus timed out") from exc
        except httpx.TransportError as exc:
            raise ToolUnavailableError(f"prometheus unreachable: {type(exc).__name__}") from exc
        if resp.status_code >= 500 or resp.status_code == 429:
            raise ToolUnavailableError(f"prometheus returned {resp.status_code}")
        if resp.status_code >= 400:
            # the input was validated and the query is ours: a 400 here is OUR bug
            raise ToolExecutionError(
                f"prometheus refused the query ({resp.status_code})", status=502
            )
        try:
            body = _Answer.model_validate_json(resp.content)
        except ValidationError as exc:
            raise ToolExecutionError(
                f"prometheus broke its contract: {exc.error_count()} error(s)", status=502
            ) from exc
        if body.status != "success" or body.data is None or body.data.resultType != "matrix":
            raise ToolExecutionError("prometheus answered without a matrix", status=502)
        if len(body.data.result) > 1:
            raise ToolExecutionError("prometheus returned more than one series", status=502)
        points: list[s.MetricPointOut] = []
        for series in body.data.result:
            for t, raw in series.values:
                value = float(raw)
                if math.isnan(value) or math.isinf(value):
                    continue  # no requests in the bucket: no point, not a zero
                start = datetime.fromtimestamp(t, tz=UTC) - step
                points.append(s.MetricPointOut(ts=start, value=round(value, 4)))
        if not points:
            return s.MetricsOut(items=[])
        return s.MetricsOut(
            items=[
                s.MetricSeriesItem(
                    service_key=q.service_key,
                    metric=q.metric,
                    bucket_seconds=bucket,
                    points=points,
                    stats=series_stats([p.value for p in points]),
                )
            ]
        )
