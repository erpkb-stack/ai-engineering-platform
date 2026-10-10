"""One structured LLM call with a deadline, traced; failures return a reason (never raise)."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import UUID

import structlog

from aeoi_agents.common import ms
from aeoi_agents.prompts import Prompt
from aeoi_agents.trace import Tracer
from aeoi_llm_client import CallMetadata, LLMClient, LLMError, Msg

log = structlog.get_logger(__name__)


async def structured(
    llm: LLMClient,
    tracer: Tracer,
    prompt: Prompt,
    user: str,
    schema: dict[str, Any],
    *,
    agent: str,
    route: str,
    max_tokens: int,
    timeout_s: float,
    use_cache: bool,
    investigation_id: UUID | None,
    schema_name: str,
    allow_fallback: bool = True,
) -> tuple[dict[str, Any] | None, str | None]:
    tracer.message("system", prompt.text)
    tracer.message("user", user)
    t0 = time.perf_counter()
    try:
        resp = await asyncio.wait_for(
            llm.generate_structured(
                [Msg(role="user", content=user)],
                schema,
                route=route,
                system=prompt.text,
                max_tokens=max_tokens,
                schema_name=schema_name,
                allow_fallback=allow_fallback,
                timeout_s=timeout_s,
                use_cache=use_cache,
                metadata=CallMetadata(
                    agent_name=agent,
                    prompt_id=prompt.prompt_id,
                    prompt_version=prompt.version,
                    investigation_id=investigation_id,
                ),
            ),
            timeout=timeout_s + 5,
        )
    except (TimeoutError, LLMError) as exc:
        tracer.llm_failed(route, ms(t0), exc)
        reason = (
            f"timeout after {timeout_s:.0f}s"
            if isinstance(exc, TimeoutError)
            else f"{exc.status} {exc.type.rsplit('/', 1)[-1]}: {exc.detail[:160]}"
        )
        log.warning("reasoning_llm_failed", agent=agent, route=route, reason=reason)
        return None, f"LLM unavailable ({reason})"
    tracer.llm_ok(resp)
    tracer.message("assistant", resp.text)
    return dict(resp.data or {}), None
