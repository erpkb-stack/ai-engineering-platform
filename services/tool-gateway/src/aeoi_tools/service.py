"""The invoke pipeline (architecture.md §14):

  1 registry lookup -> 2 authz (user perms ∩ agent allow-list, side-effect rule)
  -> 3 rate limit (user, tool) -> 4 input validation (bounded schema)
  -> 5 execute: deadline -> retry (READ only) -> bulkhead -> breaker -> handler
  -> 6 output contract check -> sanitise (caps, redaction, injection flags, evidence ids)
  -> 7 record: tools.tool_calls + tools.audit_outbox in ONE transaction -> respond

Every path - allowed, denied, invalid, failed - ends in step 7. If step 7 fails, the caller
gets 503 and NO data: an unrecorded tool call is worse than a failed one (fail closed).

[P] READ tools execute before they are recorded (a read leaves nothing to undo).
[Phase 16] CONSEQUENTIAL tools record the intent FIRST (status PENDING), then execute.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aeoi_common.ids import uuid7
from aeoi_common.ratelimit import RateLimiter
from aeoi_common.resilience import (
    Bulkhead,
    CircuitBreaker,
    CircuitOpenError,
    RetryPolicy,
    SaturatedError,
    with_retries,
)
from aeoi_db.models.tools import AuditOutbox, ToolCall
from aeoi_tools.contracts import (
    EgressBlockedError,
    SideEffect,
    ToolContext,
    ToolExecutionError,
    ToolSpec,
    ToolUnavailableError,
)
from aeoi_tools.policy import Decision, DenyReason, PolicyConfig, decide
from aeoi_tools.registry import Registry
from aeoi_tools.sanitize import sanitize, summarize_input

log = structlog.get_logger(__name__)
_NAME_SAFE = str.maketrans({c: "_" for c in " \t\n\r\x00\"'<>;\\/"})


class _AttemptTimeoutError(ToolUnavailableError):
    pass


class AuditWriteError(Exception):
    """The call could not be recorded. Maps to 503; the result is withheld."""


@dataclass
class Outcome:
    call_id: UUID
    tool: str
    status: str  # OK | DENIED | ERROR | TIMEOUT
    http_status: int
    data: dict[str, Any] | None = None
    evidence_ids: list[str] = field(default_factory=list)
    security: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0
    attempts: int = 0
    reason: str | None = None  # stable code (DenyReason or error code)
    detail: str = ""  # safe to show to the caller
    errors: list[dict[str, Any]] | None = None
    retry_after_s: float | None = None


@dataclass
class _Deps:
    breaker: CircuitBreaker
    bulkhead: Bulkhead


class ToolService:
    def __init__(
        self,
        registry: Registry,
        policy: PolicyConfig,
        limiter: RateLimiter,
        sessions: async_sessionmaker[AsyncSession],
        *,
        bulkhead_limits: dict[str, int],
        breaker_threshold: int = 5,
        breaker_cooldown_s: float = 30.0,
        retry: RetryPolicy | None = None,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self._limiter = limiter
        self._sessions = sessions
        self._retry = retry or RetryPolicy(max_attempts=2, base_delay_s=0.2, max_delay_s=1.0)
        self._deps: dict[str, _Deps] = {}
        for dep in sorted({spec.dependency for spec in registry.values()}):
            self._deps[dep] = _Deps(
                CircuitBreaker(dep, threshold=breaker_threshold, cooldown_s=breaker_cooldown_s),
                Bulkhead(dep, bulkhead_limits.get(dep, 4), queue_timeout_s=0.5),
            )

    def breaker_states(self) -> dict[str, str]:
        return {name: d.breaker.state.value for name, d in self._deps.items()}

    # ---------------------------------------------------------------- public
    async def invoke(self, name: str, raw_args: Any, ctx: ToolContext) -> Outcome:
        t0 = time.perf_counter()
        call_id = uuid7()
        spec = self.registry.get(name)
        # Rate limit FIRST (before policy): denials write rows too, so they must not be free.
        # Unknown names share one bucket per user (no new bucket per invented name).
        wait = await self._limiter.acquire(
            f"{ctx.user.user_id}:{spec.name if spec else '_unknown'}"
        )
        if wait > 0:
            tool = spec.name if spec else _safe_name(name)
            out = Outcome(call_id, tool, "DENIED", 429, reason=DenyReason.RATE_LIMITED)
            out.detail, out.retry_after_s = f"Rate limit for this tool. Retry in {wait:.1f}s.", wait
            return await self._finish(out, spec, raw_args, ctx, t0)

        if spec is None:
            out = Outcome(call_id, _safe_name(name), "DENIED", 404, reason=DenyReason.UNKNOWN_TOOL)
            out.detail = "No such tool."
            return await self._finish(out, None, raw_args, ctx, t0)

        decision = decide(spec, ctx, self.policy)
        if not decision.allowed:
            return await self._finish(
                self._denied(call_id, spec, decision), spec, raw_args, ctx, t0
            )

        try:
            args = spec.input_model.model_validate(raw_args)
        except ValidationError as exc:
            out = Outcome(call_id, spec.name, "ERROR", 422, reason="invalid_input")
            out.detail = "Arguments do not match the tool's input schema."
            # loc/msg/type only - never echo "input" (it may contain secrets)
            out.errors = [
                {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()[:20]
            ]
            return await self._finish(out, spec, raw_args, ctx, t0)

        out = await self._execute(call_id, spec, args, ctx)
        return await self._finish(out, spec, raw_args, ctx, t0)

    async def record_refusal(
        self,
        name: str,
        raw_args: Any,
        ctx: ToolContext,
        *,
        reason: str,
        detail: str,
        http_status: int = 403,
        status: str = "DENIED",
        errors: list[dict[str, Any]] | None = None,
    ) -> Outcome:
        """A refusal decided outside invoke() for a KNOWN user (user token asserting an agent,
        sending X-On-Behalf-Of, oversized or malformed envelope). Recorded like any call."""
        t0 = time.perf_counter()
        spec = self.registry.get(name)
        out = Outcome(uuid7(), spec.name if spec else _safe_name(name), status, http_status)
        out.reason, out.detail, out.errors = reason, detail, errors
        return await self._finish(out, spec, raw_args, ctx, t0)

    async def record_service_denial(
        self,
        *,
        service: str,
        tool: str,
        reason: str,
        detail: str,
        correlation_id: str | None,
    ) -> UUID | None:
        """A SERVICE was refused before any user identity was established (no OBO token, bad
        OBO token, untrusted agent assertion). There is no user to put in tool_calls, so this
        writes an audit event only - still recorded, still visible on the security dashboard."""
        if await self._limiter.acquire(f"svc-denied:{service}") > 0:
            # A misbehaving service hammering us: stop writing a row per refusal (bounded
            # write amplification). The log line still counts it; alert on it.
            log.warning("tool_service_denied_unrecorded", service=service, reason=reason)
            return None
        event_id = uuid7()
        event = _event(
            event_id=event_id,
            actor=f"service:{service}",
            actor_role=None,
            tool=_safe_name(tool),
            incident_id=None,
            correlation_id=correlation_id,
            outcome="DENIED",
            details={"reason": str(reason), "detail": detail[:300], "service": service},
        )
        try:
            async with self._sessions() as session, session.begin():
                session.add(AuditOutbox(id=event_id, event=event))
        except Exception as exc:
            raise AuditWriteError(type(exc).__name__) from exc
        log.warning("tool_service_denied", service=service, tool=_safe_name(tool), reason=reason)
        return event_id

    # ---------------------------------------------------------------- steps
    @staticmethod
    def _denied(call_id: UUID, spec: ToolSpec, decision: Decision) -> Outcome:
        reason = decision.reason or DenyReason.MISSING_PERMISSION
        detail = {
            DenyReason.MISSING_PERMISSION: f"Missing permission(s): {decision.detail}.",
            DenyReason.UNKNOWN_AGENT: "Unknown agent.",
            DenyReason.NOT_IN_AGENT_ALLOWLIST: "This agent may not use this tool.",
            DenyReason.APPROVAL_REQUIRED: "This action needs a recorded human approval.",
            DenyReason.APPROVAL_UNVERIFIABLE: (
                "Approvals cannot be verified yet (Phase 16); consequential tools are disabled."
            ),
        }.get(reason, "Denied by policy.")
        return Outcome(call_id, spec.name, "DENIED", 403, reason=reason, detail=detail)

    async def _execute(self, call_id: UUID, spec: ToolSpec, args: Any, ctx: ToolContext) -> Outcome:
        deps = self._deps[spec.dependency]
        attempts = 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + spec.timeout_s  # ONE budget for all attempts

        async def guarded() -> Any:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise _AttemptTimeoutError("no time left for another attempt")
            try:
                async with asyncio.timeout(remaining):
                    return await spec.handler(args, ctx)
            except TimeoutError as exc:
                # Transient on purpose: a backend that keeps timing out must open the breaker.
                raise _AttemptTimeoutError(f"attempt timed out after {remaining:.1f}s") from exc

        async def once() -> Any:
            nonlocal attempts
            attempts += 1
            await deps.bulkhead.acquire()  # outside the breaker: "busy" is not "broken"
            try:
                return await deps.breaker.call(guarded)
            finally:
                deps.bulkhead.release()

        policy = RetryPolicy(
            max_attempts=spec.max_attempts if spec.side_effect is SideEffect.READ else 1,
            base_delay_s=self._retry.base_delay_s,
            max_delay_s=self._retry.max_delay_s,
        )
        out = Outcome(call_id, spec.name, "ERROR", 500)
        try:
            # backstop: retry back-off sleeps also count against the deadline
            async with asyncio.timeout(spec.timeout_s + 0.25):
                result = await with_retries(once, policy)
        except (TimeoutError, _AttemptTimeoutError):
            out.status, out.http_status, out.reason = "TIMEOUT", 504, "timeout"
            out.detail = f"Tool did not finish within {spec.timeout_s}s."
        except (CircuitOpenError, SaturatedError, ToolUnavailableError) as exc:
            out.http_status, out.retry_after_s = 503, 1.0
            out.reason = {CircuitOpenError: "circuit_open", SaturatedError: "saturated"}.get(
                type(exc), "unavailable"
            )
            out.detail = f"Backend '{spec.dependency}' is unavailable ({out.reason})."
            if isinstance(exc, ToolUnavailableError):
                out.detail += f" {exc}"
        except EgressBlockedError as exc:
            out.status, out.http_status, out.reason = "DENIED", 403, "egress_blocked"
            out.detail = "Outbound call blocked by the egress allow-list."
            log.error("tool_egress_blocked", tool=spec.name, error=str(exc))
        except ToolExecutionError as exc:
            out.http_status, out.reason, out.detail = exc.status, "execution_failed", str(exc)
        except Exception:
            log.exception("tool_handler_crashed", tool=spec.name)
            out.reason, out.detail = "internal_error", "Tool failed unexpectedly."
        else:
            if not isinstance(result, spec.output_model):
                log.error("tool_output_contract", tool=spec.name, got=type(result).__name__)
                out.http_status, out.reason = 502, "output_contract_violation"
                out.detail = "Tool returned data outside its output contract."
            else:
                data, evidence, report = sanitize(
                    result.model_dump(mode="json"),
                    call_id=call_id,
                    max_items=spec.max_items,
                    max_bytes=spec.max_output_bytes,
                )
                out.status, out.http_status = "OK", 200
                out.data, out.evidence_ids, out.security = data, evidence, report.as_dict()
        out.attempts = attempts
        return out

    async def _finish(
        self, out: Outcome, spec: ToolSpec | None, raw_args: Any, ctx: ToolContext, t0: float
    ) -> Outcome:
        out.latency_ms = int((time.perf_counter() - t0) * 1000)
        side_effect = spec.side_effect.value if spec else SideEffect.READ.value
        stored_input = summarize_input(raw_args)
        summary: dict[str, Any] = {"http_status": out.http_status}
        if out.status == "OK" and out.data is not None:
            summary |= {
                "items": len(out.evidence_ids),
                "truncated": bool(out.data.get("truncated")),
                "evidence_ids": out.evidence_ids[:50],
                "security": out.security,
            }
        elif out.reason:
            summary |= {"error": out.reason}
        denial = f"{out.reason}: {out.detail}"[:500] if out.status == "DENIED" else None
        audit_input = {k: stored_input.get(k) for k in (spec.audit_fields if spec else ())}
        event_id = uuid7()
        event = _event(
            event_id=event_id,
            actor=ctx.actor,
            actor_role=",".join(sorted(ctx.user.roles))[:40] or None,
            tool=out.tool,
            incident_id=ctx.incident_id,
            correlation_id=ctx.correlation_id,
            outcome={"OK": "SUCCESS", "DENIED": "DENIED"}.get(out.status, "FAILURE"),
            details={
                "tool_call_id": str(out.call_id),
                "on_behalf_of": str(ctx.user.user_id),
                "agent": ctx.agent,
                "service": ctx.service,
                "status": out.status,
                "reason": out.reason,
                "side_effect": side_effect,
                "latency_ms": out.latency_ms,
                "investigation_id": str(ctx.investigation_id) if ctx.investigation_id else None,
                "approval_id": str(ctx.approval_id) if ctx.approval_id else None,
                "args": audit_input,
            },
        )
        try:
            async with self._sessions() as session, session.begin():
                session.add(
                    ToolCall(
                        id=out.call_id,
                        incident_id=ctx.incident_id,
                        investigation_id=ctx.investigation_id,
                        task_id=ctx.task_id,
                        agent_name=ctx.agent,
                        on_behalf_of=ctx.user.user_id,
                        tool_name=out.tool,
                        side_effect=side_effect,
                        input=stored_input,
                        output_summary=summary,
                        status=out.status,
                        denial_reason=denial,
                        approval_id=ctx.approval_id,
                        latency_ms=out.latency_ms,
                        attempt=max(1, out.attempts),
                    )
                )
                session.add(AuditOutbox(id=event_id, event=event))
        except Exception as exc:
            log.error("tool_call_not_recorded", tool=out.tool, error=type(exc).__name__)
            raise AuditWriteError(type(exc).__name__) from exc
        log.info(
            "tool_call",
            tool=out.tool,
            status=out.status,
            reason=out.reason,
            actor=ctx.actor,
            latency_ms=out.latency_ms,
            attempts=out.attempts,
            tool_call_id=str(out.call_id),
        )
        return out


def _safe_name(name: str) -> str:
    return name.translate(_NAME_SAFE)[:80] or "_"


def _event(
    *,
    event_id: UUID,
    actor: str,
    actor_role: str | None,
    tool: str,
    incident_id: UUID | None,
    correlation_id: str | None,
    outcome: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    """Shape = audit service POST /v1/events item (aeoi_models.api.audit.AuditEventIn)."""
    return {
        "id": str(event_id),
        "occurred_at": datetime.now(UTC).isoformat(),
        "actor": actor,
        "actor_role": actor_role,
        "action": "tool.call",
        "resource_type": "tool",
        "resource_id": tool,
        "incident_id": str(incident_id) if incident_id else None,
        "correlation_id": (correlation_id or "")[:80] or None,
        "outcome": outcome,
        "details": details,
    }
