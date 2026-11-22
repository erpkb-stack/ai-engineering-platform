"""search_logs from a real Loki (Phase 32a): the SAME tool contract as devdata.

What Loki holds here (ADR-035): every service's structlog line over OTLP. The line (body) is
the structlog EVENT NAME only - logs carry no message text, an exception keeps its TYPE only.
`service_name` is a stream label; `severity_text` and `trace_id` are structured metadata.
So, in this adapter:
- `message` = the event name; `error_code` = the event name too (the one stable, code-written,
  low-cardinality identifier a real line has - the role ERR_POOL_TIMEOUT plays in the seed);
- `counts_by_error_code` is EXACT over the whole window (one instant metric query), like devdata;
- `contains` is a case-insensitive literal (like devdata's ILIKE), escaped - never a pattern.
Loki keeps 24 h (infrastructure/observability/loki.yaml): an older window answers "no lines".
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx
from pydantic import BaseModel, ValidationError

from aeoi_tools import schemas as s
from aeoi_tools.contracts import ToolExecutionError, ToolUnavailableError

# contract level -> the severity_text values structlog writes (to_otel: level.upper())
_SEVERITY = {
    "DEBUG": ("DEBUG",),
    "INFO": ("INFO",),
    "WARN": ("WARNING", "WARN"),
    "ERROR": ("ERROR", "EXCEPTION"),
    "FATAL": ("CRITICAL", "FATAL"),
}
_LEVEL_OF = {sev: level for level, sevs in _SEVERITY.items() for sev in sevs}
TOP_CODES = 50


def _literal_regex(text: str) -> str:
    """A literal inside a LogQL backtick string: RE2-escaped; a backtick becomes \\x60 (it would
    end the string)."""
    return re.escape(text).replace("`", r"\x60")


def log_query(q: s.LogsIn, service: str) -> str:
    safe = service.replace("\\", "").replace('"', "")
    sevs = [sev for level in s.LEVELS[s.LEVELS.index(q.min_level) :] for sev in _SEVERITY[level]]
    parts = [f'{{service_name="{safe}"}}', f"| severity_text=~`{'|'.join(sevs)}`"]
    if q.error_code:
        parts.append(f"|~ `^{_literal_regex(q.error_code)}$`")
    if q.contains:
        parts.append(f"|~ `(?i){_literal_regex(q.contains)}`")
    return " ".join(parts)


def count_query(q: s.LogsIn, service: str) -> str:
    seconds = max(1, int((q.end - q.start).total_seconds()))
    inner = f'{log_query(q, service)} | label_format code="{{{{ __line__ }}}}"'
    return f"sum by (code) (count_over_time({inner} [{seconds}s]))"


def _ns(t: datetime) -> str:
    return str(int(t.timestamp() * 1_000_000_000))


class _Stream(BaseModel):
    stream: dict[str, str] = {}
    values: list[list[str]] = []


class _Sample(BaseModel):
    metric: dict[str, str] = {}
    value: tuple[float, str]


class _StreamsData(BaseModel):
    resultType: str  # noqa: N815 - Loki's field name
    result: list[_Stream] = []


class _VectorData(BaseModel):
    resultType: str  # noqa: N815
    result: list[_Sample] = []


class _Streams(BaseModel):
    status: str
    data: _StreamsData


class _Vector(BaseModel):
    status: str
    data: _VectorData


class LokiLogsAdapter:
    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        service_names: dict[str, str] | None = None,
    ) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._names = dict(service_names or {})  # service_key -> OTel service.name

    async def _get(self, path: str, params: dict[str, str]) -> bytes:
        try:
            resp = await self._client.get(f"{self._base}{path}", params=params)
        except httpx.TimeoutException as exc:
            raise ToolUnavailableError("loki timed out") from exc
        except httpx.TransportError as exc:
            raise ToolUnavailableError(f"loki unreachable: {type(exc).__name__}") from exc
        if resp.status_code >= 500 or resp.status_code == 429:
            raise ToolUnavailableError(f"loki returned {resp.status_code}")
        if resp.status_code >= 400:
            # the input was validated and the query is ours: a 400 here is OUR bug
            raise ToolExecutionError(f"loki refused the query ({resp.status_code})", status=502)
        return resp.content

    async def search_logs(self, q: s.LogsIn) -> s.LogsOut:
        service = self._names.get(q.service_key, q.service_key)
        raw_lines = await self._get(
            "/loki/api/v1/query_range",
            {
                "query": log_query(q, service),
                "start": _ns(q.start),
                "end": _ns(q.end),
                "limit": str(q.limit),
                "direction": "backward" if q.order == "desc" else "forward",
            },
        )
        raw_counts = await self._get(
            "/loki/api/v1/query", {"query": count_query(q, service), "time": _ns(q.end)}
        )
        try:
            lines = _Streams.model_validate_json(raw_lines)
            counts = _Vector.model_validate_json(raw_counts)
        except ValidationError as exc:
            raise ToolExecutionError(
                f"loki broke its contract: {exc.error_count()} error(s)", status=502
            ) from exc
        if lines.status != "success" or lines.data.resultType != "streams":
            raise ToolExecutionError("loki answered without streams", status=502)
        if counts.status != "success" or counts.data.resultType != "vector":
            raise ToolExecutionError("loki answered without a vector", status=502)

        items: list[tuple[int, s.LogItem]] = []
        for st in lines.data.result:
            sev = st.stream.get("severity_text", "")
            for value in st.values:
                if len(value) < 2:
                    raise ToolExecutionError("loki returned a line without a body", status=502)
                ts_ns, event = int(value[0]), value[1]
                items.append((ts_ns, s.LogItem(
                    ts=datetime.fromtimestamp(ts_ns / 1e9, tz=UTC),
                    level=_LEVEL_OF.get(sev.upper(), sev.upper() or "INFO"),
                    message=event,
                    error_code=event or None,
                    trace_id=st.stream.get("trace_id") or None,
                )))  # fmt: skip
        # streams are split by metadata: merge them back into ONE order, then the limit
        items.sort(key=lambda p: p[0], reverse=q.order == "desc")
        rows = [item for _, item in items[: q.limit]]

        by_code: dict[str, int] = {}
        for sample in counts.data.result:
            code = sample.metric.get("code") or "(none)"
            by_code[code] = by_code.get(code, 0) + int(float(sample.value[1]))
        total = sum(by_code.values())
        top = dict(sorted(by_code.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_CODES])
        return s.LogsOut(
            items=rows,
            total_matching=total,
            distinct_codes=len(by_code),
            counts_by_error_code=top,
            truncated=total > len(rows),
        )
