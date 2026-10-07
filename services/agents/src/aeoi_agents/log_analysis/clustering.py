"""Deterministic log clustering. No LLM: counting, grouping and ordering are code's job.

Template = the message with variable parts replaced (ids, numbers, IPs, quoted values), so
"timeout after 5003ms for order 8812" and "timeout after 4870ms for order 1194" are one cluster.
Cluster key = (service, error_code, template). Counts per error_code come from the gateway's
window-wide `counts_by_error_code` (exact); per-template counts are over the SAMPLE only and
are labelled as such.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

_SUBS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
        "<uuid>",
    ),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{8,}\b", re.I), "<hex>"),
    (re.compile(r"\"[^\"]{1,200}\"|'[^']{1,200}'"), "<str>"),
    (re.compile(r"\[(?:EMAIL|PHONE|CARD|SSN|REDACTED)\]"), "<redacted>"),
    (re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?"), "<n>"),
)
LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3, "FATAL": 4}


def template_of(message: str) -> str:
    out = message.strip()
    for pattern, repl in _SUBS:
        out = pattern.sub(repl, out)
    return re.sub(r"\s+", " ", out)[:400]


@dataclass
class Cluster:
    service_key: str
    error_code: str | None
    template: str
    lines: list[dict[str, Any]] = field(default_factory=list)
    count_window: int | None = None

    @property
    def first(self) -> dict[str, Any]:
        return min(self.lines, key=lambda line: line["ts"])

    @property
    def last(self) -> dict[str, Any]:
        return max(self.lines, key=lambda line: line["ts"])

    @property
    def levels(self) -> list[str]:
        return sorted(
            {line["level"] for line in self.lines}, key=lambda lv: -LEVEL_ORDER.get(lv, 0)
        )

    def sample_ids(self, n: int = 3) -> list[str]:
        """First, last and the middle line: spread over time, not the first n duplicates."""
        ordered = sorted(self.lines, key=lambda line: line["ts"])
        picks = [ordered[0], ordered[len(ordered) // 2], ordered[-1]][:n]
        out: list[str] = []
        for line in picks:
            if line["evidence_id"] not in out:
                out.append(line["evidence_id"])
        return out

    def sort_key(self) -> tuple[int, int, int, datetime]:
        top_level = LEVEL_ORDER.get(self.levels[0], 0) if self.lines else 0
        return (-(self.count_window or 0), -len(self.lines), -top_level, self.first["ts"])


def cluster_lines(
    service_key: str, lines: list[dict[str, Any]], counts_by_code: dict[str, int]
) -> list[Cluster]:
    """`lines` are log items from search_logs (each has evidence_id, ts as datetime, level,
    message, error_code). Duplicate evidence ids (asc + desc samples overlap) are merged."""
    seen: set[str] = set()
    by_key: dict[tuple[str | None, str], Cluster] = {}
    for line in lines:
        if line["evidence_id"] in seen:
            continue
        seen.add(line["evidence_id"])
        code = line.get("error_code") or None
        key = (code, template_of(line["message"]))
        cluster = by_key.get(key)
        if cluster is None:
            cluster = by_key[key] = Cluster(service_key, code, key[1])
        cluster.lines.append(line)
    # The window-wide exact count belongs to the error CODE. A cluster gets it only when its
    # code has a single template in the sample; otherwise null (never split or double count).
    by_code: dict[str | None, list[Cluster]] = {}
    for cluster in by_key.values():
        by_code.setdefault(cluster.error_code, []).append(cluster)
    for code, group in by_code.items():
        if len(group) == 1:
            group[0].count_window = counts_by_code.get(code or "(none)")
    return sorted(by_key.values(), key=lambda c: c.sort_key())
