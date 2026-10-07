"""Per-task trace recorder: every tool call, every model call, the prompts actually sent."""

from __future__ import annotations

import time
from decimal import Decimal

from aeoi_llm_client import LLMError, LLMResponse
from aeoi_models.api.agents import AgentTrace, LLMCallTrace, ToolCallTrace, TraceMessage
from aeoi_security.pii import scrub_pii
from aeoi_security.redaction import redact_text

MAX_MESSAGE_CHARS = 12_000


class Tracer:
    def __init__(self, agent: str, agent_version: str) -> None:
        self.trace = AgentTrace(agent=agent, agent_version=agent_version)
        self._t0 = time.perf_counter()

    def message(self, role: str, content: str) -> None:
        # defence in depth: tool output is already sanitised by the gateway, but prompts are
        # stored long-term, so scrub again before they land in orchestrator.messages
        text = scrub_pii(redact_text(content)).text
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[:MAX_MESSAGE_CHARS] + "\n[...truncated in trace]"
        if len(self.trace.messages) < 20:
            self.trace.messages.append(TraceMessage(role=role, content=text))  # type: ignore[arg-type]

    def tool(self, call: ToolCallTrace) -> None:
        self.trace.tool_calls.append(call)

    def llm_ok(self, resp: LLMResponse) -> None:
        self.trace.llm_calls.append(
            LLMCallTrace(
                request_id=resp.request_id,
                route=resp.route,
                model=resp.model,
                provider=resp.provider,
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
                cost_usd=resp.cost_usd,
                latency_ms=resp.latency_ms,
                fallback_used=resp.fallback_used,
                repaired=resp.repaired,
                cached=resp.cached,
            )
        )
        self.trace.model = resp.model
        self.trace.cached = resp.cached
        self.trace.input_tokens += resp.usage.input_tokens
        self.trace.output_tokens += resp.usage.output_tokens
        self.trace.cost_usd += Decimal(resp.cost_usd)
        if resp.repaired:
            self.trace.retries += 1

    def llm_failed(self, route: str, latency_ms: int, exc: Exception) -> None:
        reason = (
            f"{exc.status} {exc.type.rsplit('/', 1)[-1]}: {exc.detail}"
            if isinstance(exc, LLMError)
            else (type(exc).__name__)
        )
        self.trace.llm_calls.append(
            LLMCallTrace(
                request_id=None,
                route=route,
                model=None,
                provider=None,
                latency_ms=latency_ms,
                error=reason[:400],
            )
        )

    def finish(self) -> AgentTrace:
        self.trace.latency_ms = int((time.perf_counter() - self._t0) * 1000)
        return self.trace
