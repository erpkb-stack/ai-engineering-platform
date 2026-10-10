"""Routing, fallback, redaction, structured output, cache, budget, stream, embed - fakes only."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest

from aeoi_common.errors import BadRequestError
from aeoi_llm.config import ModelConfig, RoutingConfig
from aeoi_llm.errors import (
    AllProvidersFailedError,
    BudgetExceededError,
    GatewayTimeoutError,
    StructuredOutputError,
    UpstreamRejectedError,
)
from aeoi_llm.gateway import CallMeta, Gateway
from aeoi_llm.main import build_gateway
from aeoi_llm.providers import ChatRequest, FakeProvider, Message, PermanentError, RetryableError
from aeoi_llm.providers.base import RateLimitedError, Usage
from aeoi_llm.usage import MemoryUsageSink, UsageRow, cost_usd

from .conftest import no_sleep

META = CallMeta(agent_name="test-agent", prompt_id="p", prompt_version=1)
SCHEMA = {
    "type": "object",
    "properties": {"severity": {"enum": ["SEV1", "SEV2"]}, "reason": {"type": "string"}},
    "required": ["severity", "reason"],
    "additionalProperties": False,
}


def req(text: str = "What broke?", **kw: object) -> ChatRequest:
    return ChatRequest(model="", messages=[Message("user", text)], **kw)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- routing / fallback


async def test_primary_answers_and_usage_is_recorded(
    gateway: Gateway, hosted: FakeProvider, sink: MemoryUsageSink
) -> None:
    hosted.replies.append("the pool is exhausted")
    r = await gateway.generate("reasoning", req(), META)
    assert (r.model, r.provider, r.fallback_used) == ("fake-large", "hosted_fake", False)
    assert r.text == "the pool is exhausted"
    assert [row.status for row in sink.rows] == ["OK"]
    row = sink.rows[0]
    assert (row.agent_name, row.prompt_id, row.prompt_version) == ("test-agent", "p", 1)
    assert row.cost_usd == r.cost_usd > 0


async def test_fallback_is_used_and_reported_never_silent(
    gateway: Gateway, hosted: FakeProvider, sink: MemoryUsageSink
) -> None:
    hosted.fail_next(RetryableError("503"), RetryableError("503"))  # max_attempts=2
    r = await gateway.generate("reasoning", req(), META)
    assert r.fallback_used is True
    assert r.model == "fake-small"
    assert [a.outcome for a in r.attempts] == ["error", "ok"]
    assert [row.status for row in sink.rows] == ["ERROR", "FALLBACK"]


async def test_allow_fallback_false_fails_instead_of_degrading(
    gateway: Gateway, hosted: FakeProvider, local: FakeProvider
) -> None:
    hosted.fail_next(RetryableError("x"), RetryableError("x"))
    with pytest.raises(AllProvidersFailedError):
        await gateway.generate("reasoning", req(), META, allow_fallback=False)
    assert local.calls == []


async def test_rate_limit_status_is_recorded(
    gateway: Gateway, hosted: FakeProvider, sink: MemoryUsageSink
) -> None:
    hosted.fail_next(RateLimitedError("429", retry_after_s=120))  # too long: fall back at once
    r = await gateway.generate("reasoning", req(), META)
    assert r.fallback_used
    assert sink.rows[0].status == "RATE_LIMITED"


async def test_bad_request_is_not_hidden_by_fallback(
    gateway: Gateway, hosted: FakeProvider, local: FakeProvider
) -> None:
    hosted.fail_next(PermanentError("prompt too long", status=400))
    with pytest.raises(UpstreamRejectedError):
        await gateway.generate("reasoning", req(), META)
    assert local.calls == []


async def test_auth_error_on_primary_falls_back(gateway: Gateway, hosted: FakeProvider) -> None:
    hosted.fail_next(PermanentError("bad key", status=401))  # provider problem, not request
    r = await gateway.generate("reasoning", req(), META)
    assert r.fallback_used


async def test_unconfigured_provider_is_skipped_with_reason(gateway: Gateway) -> None:
    gateway.runtimes["hosted_fake"].provider = None
    gateway.runtimes["hosted_fake"].unavailable_reason = "no API key"
    r = await gateway.generate("reasoning", req(), META)
    assert r.fallback_used
    assert r.attempts[0].outcome == "skipped"
    assert r.attempts[0].detail == "no API key"


async def test_unknown_route_and_wrong_operation(gateway: Gateway) -> None:
    with pytest.raises(BadRequestError):
        await gateway.generate("nope", req(), META)
    with pytest.raises(BadRequestError):
        await gateway.generate("embed", req(), META)


async def test_max_tokens_is_capped_by_route(gateway: Gateway, hosted: FakeProvider) -> None:
    await gateway.generate("reasoning", req(max_tokens=999_999), META)
    assert hosted.calls[0].max_tokens == gateway.routing.routes["reasoning"].max_tokens_cap


async def test_deadline_covers_retries_and_fallback(gateway: Gateway, hosted: FakeProvider) -> None:
    async def slow(_: ChatRequest) -> None:
        await asyncio.sleep(10)

    hosted.generate = slow  # type: ignore[method-assign,assignment]
    gateway.request_timeout_s = 0.05
    with pytest.raises(GatewayTimeoutError):
        await gateway.generate("reasoning", req(), META)


# ----------------------------------------------------------------------------- redaction


async def test_secrets_are_redacted_before_a_hosted_provider(
    gateway: Gateway, hosted: FakeProvider
) -> None:
    secret = "sk-ant-" + "a1b2c3d4e5f6g7h8i9"
    r = await gateway.generate(
        "reasoning", req(f"log line: key={secret}", system=f"x {secret}"), META
    )
    sent = hosted.calls[0]
    assert secret not in sent.messages[0].content
    assert secret not in (sent.system or "")
    assert r.redactions == 2


async def test_local_provider_gets_original_text(gateway: Gateway, local: FakeProvider) -> None:
    secret = "sk-ant-" + "a1b2c3d4e5f6g7h8i9"
    r = await gateway.generate("local", req(f"key={secret}"), META)
    assert secret in local.calls[0].messages[0].content
    assert r.redactions == 0


# ----------------------------------------------------------------------------- structured output


async def test_structured_valid_first_time(gateway: Gateway, hosted: FakeProvider) -> None:
    hosted.structured.append({"severity": "SEV2", "reason": "pool"})
    r = await gateway.generate_structured("reasoning", req(json_schema=SCHEMA), META)
    assert r.data == {"severity": "SEV2", "reason": "pool"}
    assert r.repaired is False


async def test_structured_invalid_is_repaired_once(
    gateway: Gateway, hosted: FakeProvider, sink: MemoryUsageSink
) -> None:
    hosted.structured.extend([{"severity": "BAD"}, {"severity": "SEV1", "reason": "deploy"}])
    r = await gateway.generate_structured("reasoning", req(json_schema=SCHEMA), META)
    assert r.repaired is True
    assert r.data == {"severity": "SEV1", "reason": "deploy"}
    repair_prompt = hosted.calls[1].messages[-1].content
    assert "failed JSON Schema validation" in repair_prompt
    assert r.usage.input_tokens == 100  # both calls are paid for and recorded


async def test_structured_still_invalid_raises_after_whole_chain(
    gateway: Gateway, hosted: FakeProvider, local: FakeProvider, sink: MemoryUsageSink
) -> None:
    bad = {"severity": "nope"}
    hosted.structured.extend([bad, bad])
    local.structured.extend([bad, bad])
    with pytest.raises(StructuredOutputError):
        await gateway.generate_structured("reasoning", req(json_schema=SCHEMA), META)
    assert [r.error_type for r in sink.rows] == ["StructuredOutputError"] * 2
    assert sink.rows[0].usage.input_tokens > 0  # failed attempts still cost tokens


async def test_invalid_schema_is_a_400(gateway: Gateway) -> None:
    with pytest.raises(BadRequestError):
        await gateway.generate_structured("reasoning", req(json_schema={"type": "wat"}), META)
    with pytest.raises(BadRequestError):
        await gateway.generate_structured("reasoning", req(json_schema={"type": "array"}), META)


# ----------------------------------------------------------------------------- cache


async def test_identical_deterministic_call_is_cached(
    gateway: Gateway, hosted: FakeProvider, sink: MemoryUsageSink
) -> None:
    a = await gateway.generate("reasoning", req(), META)
    b = await gateway.generate("reasoning", req(), META)
    assert len(hosted.calls) == 1
    assert b.cached and b.text == a.text and b.cost_usd == 0
    assert len(sink.rows) == 1  # no provider call, no usage row


async def test_temperature_above_zero_is_not_cached(gateway: Gateway, hosted: FakeProvider) -> None:
    await gateway.generate("reasoning", req(temperature=0.7), META)
    await gateway.generate("reasoning", req(temperature=0.7), META)
    assert len(hosted.calls) == 2


async def test_fallback_answers_are_not_cached(
    gateway: Gateway, hosted: FakeProvider, local: FakeProvider
) -> None:
    hosted.fail_next(RetryableError("x"), RetryableError("x"))
    await gateway.generate("reasoning", req(), META)
    r = await gateway.generate("reasoning", req(), META)  # primary healthy again
    assert not r.cached
    assert r.model == "fake-large"


# ----------------------------------------------------------------------------- budget


async def test_budget_blocks_calls_once_spent(gateway: Gateway, sink: MemoryUsageSink) -> None:
    inv = uuid.uuid4()
    meta = CallMeta(investigation_id=inv)
    sink.rows.append(UsageRow(
        request_id="x:1", provider="hosted_fake", model="fake-large", operation="generate",
        status="OK", latency_ms=1, usage=Usage(), cost_usd=Decimal("0.02"), investigation_id=inv,
    ))  # fmt: skip
    with pytest.raises(BudgetExceededError):
        await gateway.generate("reasoning", req(), meta)
    # another investigation is unaffected
    await gateway.generate("reasoning", req(), CallMeta(investigation_id=uuid.uuid4()))


def test_cost_formula_prices_cached_tokens_lower() -> None:
    m = ModelConfig(provider="x", input_per_mtok=Decimal(2), output_per_mtok=Decimal(10),
                    cached_input_per_mtok=Decimal("0.2"))  # fmt: skip
    # 1M input of which 900k cached, 100k output
    c = cost_usd(m, Usage(input_tokens=1_000_000, output_tokens=100_000, cached_tokens=900_000))
    assert c == Decimal("0.2") + Decimal("0.18") + Decimal("1.0")


# ----------------------------------------------------------------------------- stream


async def _collect(gateway: Gateway, **kw: object) -> list[dict]:
    return [ev async for ev in gateway.stream("reasoning", req(), META, **kw)]  # type: ignore[arg-type]


async def test_stream_events(gateway: Gateway, hosted: FakeProvider, sink: MemoryUsageSink) -> None:
    hosted.replies.append("one two three")
    events = await _collect(gateway)
    assert events[0]["event"] == "meta"
    assert events[0]["fallback_used"] is False
    assert "".join(e["text"] for e in events if e["event"] == "delta").strip() == "one two three"
    assert events[-1]["event"] == "done"
    assert sink.rows[-1].operation == "stream"
    assert (
        gateway.runtimes["hosted_fake"].bulkhead_for("fake-large").in_flight == 0
    )  # slot released


async def test_stream_falls_back_before_first_token(gateway: Gateway, hosted: FakeProvider) -> None:
    hosted.fail_next(RetryableError("x"), RetryableError("x"))
    events = await _collect(gateway)
    assert events[0]["event"] == "meta"
    assert events[0]["fallback_used"] is True
    assert events[0]["model"] == "fake-small"


async def test_stream_all_down_yields_error_event(
    gateway: Gateway, hosted: FakeProvider, local: FakeProvider
) -> None:
    hosted.fail_next(RetryableError("x"), RetryableError("x"))
    local.fail_next(RetryableError("x"), RetryableError("x"))
    events = await _collect(gateway)
    assert [e["event"] for e in events] == ["error"]


# ----------------------------------------------------------------------------- embed


async def test_embed_returns_configured_dimensions(gateway: Gateway, sink: MemoryUsageSink) -> None:
    r = await gateway.embed("embed", ["a", "b"], META)
    assert r.dimensions == 768
    assert len(r.vectors) == 2
    assert sink.rows[-1].operation == "embed"


async def test_embed_dimension_mismatch_is_refused(gateway: Gateway, local: FakeProvider) -> None:
    local.dimensions = 384  # e.g. someone pulled a different model under the same name
    with pytest.raises(AllProvidersFailedError, match="384-d"):
        await gateway.embed("embed", ["a"], META)


async def test_embed_route_cannot_be_used_for_chat_and_vice_versa(gateway: Gateway) -> None:
    with pytest.raises(BadRequestError):
        await gateway.embed("reasoning", ["a"], META)


# ----------------------------------------------------------------------------- circuit breaker


async def test_breaker_opens_and_skips_dead_provider(
    gateway: Gateway, hosted: FakeProvider
) -> None:
    # threshold=3 in routing.test.yaml, max_attempts=2 -> 2 requests open the breaker
    hosted.fail_next(*[RetryableError("down")] * 4)
    await gateway.generate("reasoning", req("a"), META, use_cache=False)
    await gateway.generate("reasoning", req("b"), META, use_cache=False)
    calls_before = len(hosted.calls)
    r = await gateway.generate("reasoning", req("c"), META, use_cache=False)
    assert len(hosted.calls) == calls_before  # fast-failed, provider not called
    assert r.attempts[0].detail.startswith("CircuitOpenError")
    assert gateway.describe()["providers"]["hosted_fake"]["breakers"]["fake-large"] == "open"


async def test_cache_hit_is_served_even_over_budget(
    gateway: Gateway, sink: MemoryUsageSink
) -> None:
    inv = uuid.uuid4()
    meta = CallMeta(investigation_id=inv)
    await gateway.generate("reasoning", req("same"), meta)
    sink.rows.append(UsageRow(
        request_id="y:1", provider="hosted_fake", model="fake-large", operation="generate",
        status="OK", latency_ms=1, usage=Usage(), cost_usd=Decimal("1"), investigation_id=inv,
    ))  # fmt: skip
    r = await gateway.generate("reasoning", req("same"), meta)
    assert r.cached  # free answer; refusing it would only waste the budget already spent


async def test_breaker_is_per_model_not_per_provider(gateway: Gateway, local: FakeProvider) -> None:
    """Regression (first Mac run): llama3.2 failing opened the provider breaker and then
    blocked nomic-embed-text on the same Ollama, hiding which model was really broken."""
    local.fail_next(*[RetryableError("llama runner crashed")] * 5)  # threshold 5
    for text in ("a", "b", "c"):
        with pytest.raises(AllProvidersFailedError):
            await gateway.generate("local", req(text), META, use_cache=False)
    assert local.errors == []  # the 6th attempt was fast-failed by the open breaker
    assert gateway.runtimes["local_fake"].breakers["fake-small"].state.value == "open"
    r = await gateway.embed("embed", ["still works"], META)  # same provider, other model
    assert r.dimensions == 768


async def test_attempt_detail_says_why(gateway: Gateway, hosted: FakeProvider) -> None:
    hosted.fail_next(RetryableError("cannot reach http://x (ConnectError)"), RetryableError("x"))
    r = await gateway.generate("reasoning", req(), META)
    assert r.attempts[0].detail == "RetryableError: x"  # last retry's cause, not just a class name


async def test_slow_generation_does_not_starve_embeddings_on_the_same_provider(
    gateway: Gateway, local: FakeProvider
) -> None:
    """Regression (Phase 6, owner's Mac): a long llama3.2 rerank held Ollama's single slot and
    embeddings failed with SaturatedError. Bulkheads are per model now."""
    rt = gateway.runtimes["local_fake"]
    rt.config = rt.config.model_copy(update={"max_concurrency": 1, "queue_timeout_s": 0.05})
    started = asyncio.Event()

    async def slow(_: ChatRequest) -> None:
        started.set()
        await asyncio.sleep(5)

    local.generate = slow  # type: ignore[method-assign,assignment]
    gen = asyncio.create_task(gateway.generate("local", req(), META))
    await started.wait()
    r = await gateway.embed("embed", ["still served"], META)  # other model, own slot
    assert r.dimensions == 768
    gen.cancel()


async def test_caller_deadline_stops_work_early(gateway: Gateway, hosted: FakeProvider) -> None:
    cancelled = asyncio.Event()

    async def slow(_: ChatRequest) -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    hosted.generate = slow  # type: ignore[method-assign,assignment]
    gateway.request_timeout_s = 60
    with pytest.raises(GatewayTimeoutError, match=r"0\.1s"):
        await gateway.generate("reasoning", req(), META, allow_fallback=False, deadline_s=0.1)
    assert cancelled.is_set()  # the upstream call was cancelled, not left running


async def test_temperature_is_dropped_only_for_models_that_reject_it(
    routing: RoutingConfig, hosted: FakeProvider, local: FakeProvider, sink: MemoryUsageSink
) -> None:
    """supports_temperature: false (routing.yaml) -> that model never receives temperature;
    its fallback still does (it is a different model with its own setting)."""
    models = dict(routing.models)
    models["fake-large"] = models["fake-large"].model_copy(update={"supports_temperature": False})
    gw = build_gateway(
        routing.model_copy(update={"models": models}),
        sink,
        cache=None,
        request_timeout_s=5,
        providers={"hosted_fake": hosted, "local_fake": local},
    )
    gw._sleep = no_sleep
    hosted.fail_next(RetryableError("503"), RetryableError("503"))
    r = await gw.generate("reasoning", req(), META)
    assert r.fallback_used is True
    assert [c.temperature for c in hosted.calls] == [None, None]
    assert local.calls[-1].temperature == 0.0


def test_shipped_routing_marks_sonnet_5_5_without_temperature() -> None:
    from pathlib import Path

    from aeoi_llm.config import load_routing

    cfg = load_routing(Path(__file__).resolve().parents[1] / "config" / "routing.yaml")
    assert cfg.models["claude-sonnet-5-5"].supports_temperature is False
    assert cfg.models["claude-haiku-4-5-20251001"].supports_temperature is True
