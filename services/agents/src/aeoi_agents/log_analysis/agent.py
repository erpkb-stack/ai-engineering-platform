"""Log Analysis agent (architecture.md §10 #3). ADR-018.

  1 tools (code):   search_logs per service, oldest-first AND newest-first (onset + recency)
  2 cluster (code): template per message, exact counts per error_code from the gateway
  3 facts (code):   one Fact per error code / top cluster, citing real evidence ids
  4 label (LLM):    name + category per cluster, ONE structured call, validated:
                    unknown cluster ids dropped, citations outside the cluster dropped
  5 degrade:        LLM down / timeout / budget -> rule labels, result marked `degraded`

Why the LLM never writes facts: counts, times and codes are exactly what small models get
wrong, and they are trivial for code. The LLM adds the one thing code can't: a readable name.
A prompt-injected log line can at worst produce a silly LABEL; it cannot change a count, add a
tool call (this agent has one tool) or cite evidence that doesn't exist.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from aeoi_agents.config import LogAgentBudget
from aeoi_agents.log_analysis.clustering import Cluster, cluster_lines
from aeoi_agents.prompts import Prompt, load_prompt
from aeoi_agents.trace import Tracer
from aeoi_llm_client import CallMetadata, LLMClient, LLMError, Msg
from aeoi_models.api.agents import (
    AgentRunResult,
    EvidenceItem,
    LabelQuality,
    LogAnalysisTask,
    LogCluster,
    ToolCallTrace,
)
from aeoi_models.findings import EvidenceKind, EvidenceRef, Fact
from aeoi_security.untrusted import wrap_untrusted
from aeoi_tool_client import CallContext, ToolCallError, ToolClient

log = structlog.get_logger(__name__)
AGENT_NAME = "log_analysis"
AGENT_VERSION = "1.1.0"
PROMPT_VERSION = 2
SOURCE = "devdata.log_events"
CATEGORIES = (
    "dependency_timeout",
    "resource_exhaustion",
    "bad_request",
    "auth",
    "data_error",
    "crash",
    "unknown",
)

LABEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "clusters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "pattern": "^c[0-9]{1,3}$"},
                    "label": {"type": "string", "maxLength": 60},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    # short line refs ("c1.2"), mapped back to evidence ids in code: a 3B model
                    # copying 40-char ids costs ~20 output tokens each and mangles them
                    "lines": {
                        "type": "array",
                        "items": {"type": "string", "pattern": "^c[0-9]{1,3}[.][0-9]{1,2}$"},
                        "maxItems": 2,
                    },
                },
                "required": ["id", "label", "category", "lines"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clusters"],
    "additionalProperties": False,
}


@dataclass
class Deps:
    tools: ToolClient
    llm: LLMClient
    budget: LogAgentBudget


def _ts(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def _hhmm(dt: datetime) -> str:
    """Always UTC with a Z. (Regression: a -05:00 timestamp was printed with a literal 'Z',
    i.e. 5 hours wrong - found in the Phase 8 smoke run.)"""
    if dt.tzinfo is None:
        raise ValueError("naive datetime in a fact")
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ")


class LogAnalysisAgent:
    def __init__(self, deps: Deps, prompt: Prompt | None = None) -> None:
        self.deps = deps
        self.prompt = prompt or load_prompt(AGENT_NAME, PROMPT_VERSION)

    async def run(
        self, task: LogAnalysisTask, user_token: str, correlation_id: str | None
    ) -> AgentRunResult:
        tracer = Tracer(AGENT_NAME, AGENT_VERSION)
        tracer.trace.prompt_id = self.prompt.prompt_id
        tracer.trace.prompt_version = self.prompt.version
        tracer.trace.prompt_sha256 = self.prompt.sha256
        ctx = CallContext(
            user_token=user_token,
            agent_name=AGENT_NAME,
            incident_id=task.incident_id,
            investigation_id=task.investigation_id,
            task_id=task.task_id,
            correlation_id=correlation_id,
        )
        # ONE deadline for the whole task, below the orchestrator's HTTP timeout, so the
        # caller never gives up while we keep spending (review finding).
        deadline = time.monotonic() + self.deps.budget.task_deadline_s
        # ---- 1 tools
        lines_by_svc: dict[str, list[dict[str, Any]]] = {}
        counts_by_svc: dict[str, dict[str, int]] = {}
        totals: dict[str, int] = {}
        distinct: dict[str, int] = {}
        flagged: set[str] = set()
        denied: list[str] = []
        failed: dict[str, str] = {}
        for svc in task.service_keys:
            for order in ("asc", "desc"):
                if len(tracer.trace.tool_calls) >= self.deps.budget.max_tool_calls:
                    break
                if deadline - time.monotonic() < self.deps.budget.min_time_for_llm_s:
                    failed.setdefault(svc, "task deadline reached")
                    break
                args = {
                    "service_key": svc,
                    "start": task.start.isoformat(),
                    "end": task.end.isoformat(),
                    "min_level": "WARN",
                    "order": order,
                    "limit": self.deps.budget.lines_per_call,
                }
                t0 = time.perf_counter()
                try:
                    res = await self.deps.tools.call("search_logs", args, ctx)
                except ToolCallError as exc:
                    tracer.tool(
                        ToolCallTrace(
                            tool="search_logs",
                            tool_call_id=_uuid(exc.tool_call_id),
                            status="ERROR",
                            reason=exc.reason,
                            latency_ms=_ms(t0),
                        )
                    )
                    if exc.denied:
                        denied.append(f"{svc}: {exc.reason}")
                        break  # same answer for the other order; don't ask twice
                    failed.setdefault(svc, exc.reason)
                    continue
                failed.pop(svc, None)
                tracer.tool(
                    ToolCallTrace(
                        tool="search_logs",
                        tool_call_id=res.tool_call_id,
                        status="OK",
                        latency_ms=_ms(t0),
                        items=len(res.evidence_ids),
                        truncated=res.truncated,
                    )
                )
                for item in res.data.get("items", []):
                    item["ts"] = _ts(item["ts"])
                    item["_tool_call_id"] = res.tool_call_id
                    item["_order"] = order
                    lines_by_svc.setdefault(svc, []).append(item)
                counts_by_svc[svc] = res.data.get("counts_by_error_code", {})
                totals[svc] = int(res.data.get("total_matching", 0))
                distinct[svc] = int(res.data.get("distinct_codes", 0)) or len(counts_by_svc[svc])
                flagged |= {
                    f["evidence_id"] for f in res.security.injection_flags if f.get("evidence_id")
                }

        if not tracer.trace.tool_calls or all(c.status == "ERROR" for c in tracer.trace.tool_calls):
            why = "; ".join(denied) or "every search_logs call failed"
            return AgentRunResult(
                status="FAILED", error=f"no log data: {why}", trace=tracer.finish()
            )

        # oldest-first and newest-first samples overlap when the window is small: the SAME line
        # then has two evidence ids (one per call). Merge by content, keep the first id, and
        # remember which sample(s) saw it - "first seen" is only exact for the oldest-first one.
        for svc, lines in lines_by_svc.items():
            lines_by_svc[svc] = _dedupe(lines)
        # ---- 2 cluster + 3 facts (code only)
        clusters: list[Cluster] = []
        for svc, lines in lines_by_svc.items():
            clusters += cluster_lines(svc, lines, counts_by_svc.get(svc, {}))
        clusters.sort(key=lambda c: c.sort_key())
        by_id = {
            e: line
            for lines in lines_by_svc.values()
            for line in lines
            for e in [line["evidence_id"]]
        }
        facts, cited, notes = self._facts(task, clusters, counts_by_svc, totals, distinct)
        for svc, why in failed.items():  # a silently missing service is a false "all clear"
            if svc not in counts_by_svc:
                notes.append(f"{svc}: NO DATA - search_logs failed ({why}); absence is unknown.")

        # ---- 4 labels (LLM, validated) or 5 rule labels
        top = _fair_top(clusters, self.deps.budget.max_clusters_for_llm)
        ids = {c_id: c for c_id, c in ((f"c{i + 1}", c) for i, c in enumerate(top))}
        if len(top) < len(clusters):
            notes.append(f"{len(clusters) - len(top)} smaller cluster(s) not shown (budget).")
        remaining = deadline - time.monotonic()
        labels, quality, degraded = await self._label(task, ids, tracer, flagged, remaining)
        out_clusters = []
        for c_id, c in ids.items():
            lab = labels.get(c_id)
            sample = c.sample_ids()
            out_clusters.append(
                LogCluster(
                    id=c_id,
                    service_key=c.service_key,
                    error_code=c.error_code,
                    template=c.template,
                    levels=c.levels,
                    count_window=c.count_window,
                    count_sampled=len(c.lines),
                    first_seen=c.first["ts"],
                    last_seen=c.last["ts"],
                    evidence_ids=(lab["evidence_ids"] if lab and lab["evidence_ids"] else sample),
                    label=lab["label"] if lab else _rule_label(c),
                    summary=lab.get("summary", "") if lab else "",
                    category=lab["category"] if lab else "unknown",
                    labelled_by="llm" if lab else "rule",
                    untrusted_content_flagged=any(
                        line["evidence_id"] in flagged for line in c.lines
                    ),
                )
            )
            cited |= set(out_clusters[-1].evidence_ids)
        if denied:
            degraded = "; ".join(filter(None, [degraded, "denied: " + ", ".join(denied)]))
        if failed:
            degraded = "; ".join(filter(None, [degraded, "no data: " + ", ".join(failed)]))
        notes = _cap_notes(notes)
        evidence = [self._evidence(by_id[e]) for e in sorted(cited) if e in by_id]
        return AgentRunResult(
            status="SUCCEEDED",
            degraded=degraded,
            facts=facts,
            notes=notes,
            clusters=out_clusters,
            evidence=evidence,
            label_quality=quality,
            trace=tracer.finish(),
        )

    # ------------------------------------------------------------------ facts
    def _facts(
        self,
        task: LogAnalysisTask,
        clusters: list[Cluster],
        counts_by_svc: dict[str, dict[str, int]],
        totals: dict[str, int],
        distinct: dict[str, int],
    ) -> tuple[list[Fact], set[str], list[str]]:
        """Facts are CODE output. Two subtleties keep them true:
        - "first seen" is exact only if the code appears in the OLDEST-first sample (that
          sample holds every line up to its cut-off); likewise "last seen" for newest-first.
          Otherwise we say "earliest/latest sampled".
        - a code that has a count but no sampled line gets a note, not a fact: a Fact must
          cite a real evidence id, and we refuse to invent one."""
        facts: list[Fact] = []
        notes: list[str] = []
        cited: set[str] = set()
        window = f"{_hhmm(task.start)} to {_hhmm(task.end)}"
        for svc, counts in counts_by_svc.items():
            svc_clusters = [c for c in clusters if c.service_key == svc]
            # both samples together hold EVERY matching line -> first/last are exact for all
            complete = sum(len(c.lines) for c in svc_clusters) >= totals.get(svc, 0)
            if not svc_clusters:
                notes.append(f"{svc}: no WARN/ERROR/FATAL log lines from {window}.")
                continue
            if len(facts) >= self.deps.budget.max_facts:
                notes.append(f"{svc}: not summarised - fact budget reached.")
                continue
            top_ids = svc_clusters[0].sample_ids(1)
            n_codes = distinct.get(svc, len(counts)) - (1 if "(none)" in counts else 0)
            facts.append(
                self._fact(
                    f"{svc}: {totals.get(svc, 0)} WARN-or-worse log lines from {window}, "
                    f"{n_codes} distinct error code(s)"
                    + (" plus lines without a code." if "(none)" in counts else "."),
                    top_ids,
                )
            )
            cited |= set(top_ids)
            for code, n in sorted(counts.items(), key=lambda kv: -kv[1]):
                if len(facts) >= self.deps.budget.max_facts:
                    notes.append(f"{svc}: more error codes than the fact budget; see clusters.")
                    break
                lines = [
                    line
                    for c in svc_clusters
                    if (c.error_code or "(none)") == code
                    for line in c.lines
                ]
                name = f"error_code {code}" if code != "(none)" else "no error code"
                if not lines:
                    notes.append(f"{svc}: {n} lines with {name}, none in the sampled lines.")
                    continue
                first = min(lines, key=lambda line: line["ts"])
                last = max(lines, key=lambda line: line["ts"])
                first_txt = (
                    "first seen"
                    if complete or any("asc" in x["_orders"] for x in lines)
                    else "earliest sampled"
                )
                last_txt = (
                    "last seen"
                    if complete or any("desc" in x["_orders"] for x in lines)
                    else "latest sampled"
                )
                ev = list(dict.fromkeys([first["evidence_id"], last["evidence_id"]]))
                facts.append(
                    self._fact(
                        f"{svc}: {n} WARN-or-worse lines with {name}; {first_txt} "
                        f"{_hhmm(first['ts'])}, {last_txt} {_hhmm(last['ts'])}.",
                        ev,
                    )
                )
                cited |= set(ev)
        return facts, cited, notes

    @staticmethod
    def _fact(statement: str, evidence_ids: list[str]) -> Fact:
        refs = [
            EvidenceRef(evidence_id=e, kind=EvidenceKind.LOG, source_system=SOURCE)
            for e in evidence_ids
        ]
        return Fact(statement=statement[:2000], produced_by=AGENT_NAME, evidence=refs)

    @staticmethod
    def _evidence(line: dict[str, Any]) -> EvidenceItem:
        excerpt = str(line["message"])[:4000]
        code = line.get("error_code")
        title = f"{line['level']} {code or ''} at {_hhmm(line['ts'])}".replace("  ", " ")
        return EvidenceItem(
            evidence_key=line["evidence_id"],
            kind="LOG",
            source_system=SOURCE,
            title=title[:300],
            excerpt=excerpt,
            content_sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
            observed_at=line["ts"],
            tool_call_id=line.get("_tool_call_id"),
        )

    # ------------------------------------------------------------------ labels
    async def _label(
        self,
        task: LogAnalysisTask,
        clusters: dict[str, Cluster],
        tracer: Tracer,
        flagged: set[str],
        remaining_s: float,
    ) -> tuple[dict[str, dict[str, Any]], LabelQuality, str | None]:
        quality = LabelQuality(clusters_sent=len(clusters))
        if not clusters:
            return {}, quality, None
        b = self.deps.budget
        timeout_s = min(b.llm_timeout_s, remaining_s - 5)
        if timeout_s < b.min_time_for_llm_s:
            quality.clusters_sent = 0
            return {}, quality, "labels by rule: not enough time left before the task deadline"
        blocks = []
        refs: dict[str, dict[str, str]] = {}  # c_id -> {"c1.1": evidence_id}
        for c_id, c in clusters.items():
            lines = sorted(c.lines, key=lambda line: line["ts"])[: b.lines_per_cluster_in_prompt]
            refs[c_id] = {f"{c_id}.{i + 1}": line["evidence_id"] for i, line in enumerate(lines)}
            # error_code is log DATA too: it goes INSIDE the untrusted block (review finding)
            body = f"error_code={c.error_code or '-'}\n" + "\n".join(
                f"{c_id}.{i + 1} {line['level']} {str(line['message'])[: b.line_chars_in_prompt]}"
                for i, line in enumerate(lines)
            )
            blocks.append(
                f"CLUSTER {c_id} service={c.service_key}\n"
                + wrap_untrusted(
                    body,
                    source=f"search_logs:{c.service_key}",
                    item_id=c_id,
                    max_chars=(b.line_chars_in_prompt + 40) * len(lines) + 80,
                )
            )
        user = "Label these clusters.\n\n" + "\n\n".join(blocks)
        tracer.message("system", self.prompt.text)
        tracer.message("user", user)
        route = task.llm_route or b.route
        t0 = time.perf_counter()
        try:
            resp = await asyncio.wait_for(
                self.deps.llm.generate_structured(
                    [Msg(role="user", content=user)],
                    LABEL_SCHEMA,
                    route=route,
                    system=self.prompt.text,
                    max_tokens=b.llm_max_tokens,
                    schema_name="cluster_labels",
                    timeout_s=timeout_s,
                    use_cache=task.llm_cache,
                    metadata=CallMetadata(
                        agent_name=AGENT_NAME,
                        prompt_id=self.prompt.prompt_id,
                        prompt_version=self.prompt.version,
                        investigation_id=task.investigation_id,
                    ),
                ),
                timeout=timeout_s + 5,
            )
        except (TimeoutError, LLMError) as exc:
            tracer.llm_failed(route, _ms(t0), exc)
            reason = (
                f"timeout after {timeout_s:.0f}s"
                if isinstance(exc, TimeoutError)
                else f"{exc.status} {exc.type.rsplit('/', 1)[-1]}: {exc.detail[:160]}"
            )
            log.warning("log_agent_labels_skipped", route=route, reason=reason)
            return {}, quality, f"labels by rule: LLM unavailable ({reason})"
        tracer.llm_ok(resp)
        tracer.message("assistant", resp.text)
        accepted: dict[str, dict[str, Any]] = {}
        for entry in (resp.data or {}).get("clusters", []):
            c_id = str(entry.get("id", ""))
            cluster = clusters.get(c_id)
            if cluster is None or c_id in accepted:
                quality.unknown_cluster_ids += 1
                continue
            own = refs.get(c_id, {})  # only THIS cluster's refs count
            cites = [e for e in entry.get("lines", []) if isinstance(e, str)]
            valid = [own[e] for e in cites if e in own][:2]
            quality.citations_dropped += len(cites) - len(valid)
            label = str(entry.get("label", "")).strip()[:60]
            if not label:
                continue
            category = entry.get("category") if entry.get("category") in CATEGORIES else "unknown"
            accepted[c_id] = {
                "label": label,
                "summary": "",
                "category": category,
                "evidence_ids": valid,
            }
        quality.labels_accepted = len(accepted)
        missing = len(clusters) - len(accepted)
        parts = [f"labels by rule for {missing} cluster(s): model skipped them" if missing else ""]
        hot = [
            c_id
            for c_id, c in clusters.items()
            if any(line["evidence_id"] in flagged for line in c.lines)
        ]
        if hot and accepted:
            # one call labels all clusters, so injected text in one cluster can sway the
            # labels of the others: say so instead of pretending the flag is local
            parts.append(f"model labels were produced next to flagged log text ({', '.join(hot)})")
        return accepted, quality, "; ".join(p for p in parts if p) or None


def _fair_top(clusters: list[Cluster], n: int) -> list[Cluster]:
    """Top n by size, but every service keeps its 2 biggest clusters first: one noisy service
    must not hide a quieter one completely (review finding)."""
    picked: list[Cluster] = []
    for svc in dict.fromkeys(c.service_key for c in clusters):
        picked += [c for c in clusters if c.service_key == svc][:2]
    picked = picked[:n]
    for c in clusters:
        if len(picked) >= n:
            break
        if c not in picked:
            picked.append(c)
    return sorted(picked, key=lambda c: c.sort_key())


def _cap_notes(notes: list[str], limit: int = 25) -> list[str]:
    if len(notes) <= limit:
        return notes
    return [*notes[:limit], f"... and {len(notes) - limit} more note(s) (truncated)."]


def _dedupe(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    for line in lines:
        key = (
            line["ts"],
            line["level"],
            line.get("error_code"),
            line["message"],
            line.get("trace_id"),
        )
        seen = merged.get(key)
        if seen is None:
            merged[key] = {**line, "_orders": {line["_order"]}}
        else:
            seen["_orders"].add(line["_order"])
    return list(merged.values())


def _rule_label(c: Cluster) -> str:
    # untrusted text: the template is log content, shown as-is and flagged, never obeyed
    head = f"{c.error_code}: " if c.error_code else "no error code: "
    return (head + c.template)[:120]


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _uuid(value: str | None) -> Any:
    from uuid import UUID

    try:
        return UUID(value) if value else None
    except ValueError:
        return None
