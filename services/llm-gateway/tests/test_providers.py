"""Adapters against recorded-shape fake HTTP (httpx.MockTransport). No network, no key."""

from __future__ import annotations

import json

import httpx
import pytest

from aeoi_llm.providers import (
    AnthropicProvider,
    ChatRequest,
    Message,
    OpenAICompatProvider,
    PermanentError,
    ProviderTimeoutError,
    RateLimitedError,
    RetryableError,
    UnsupportedOperationError,
)

REQ = ChatRequest(model="m", messages=[Message("user", "hi")], system="sys", max_tokens=50)
SCHEMA = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}


def anthropic(handler) -> AnthropicProvider:
    return AnthropicProvider(
        "anthropic",
        base_url="https://api.test",
        api_key="k",
        timeout_s=5,
        transport=httpx.MockTransport(handler),
    )


def oai(handler) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        "ollama",
        base_url="http://ollama.test/v1",
        api_key=None,
        timeout_s=5,
        hosted=False,
        transport=httpx.MockTransport(handler),
    )


# ----------------------------------------------------------------------------- Anthropic


async def test_anthropic_generate_sends_headers_and_normalises_usage() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "m",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "hel"}, {"type": "text", "text": "lo"}],
                "usage": {"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 90},
            },
        )

    r = await anthropic(handler).generate(REQ)
    assert r.text == "hello"
    assert seen["headers"]["x-api-key"] == "k"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    assert seen["body"]["system"] == "sys"
    # cache reads are reported separately by Anthropic; we fold them into input_tokens
    assert (r.usage.input_tokens, r.usage.cached_tokens, r.usage.output_tokens) == (100, 90, 4)


async def test_anthropic_structured_forces_the_tool() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "m",
                "content": [{"type": "tool_use", "name": "result", "input": {"x": 1}}],
                "usage": {"input_tokens": 5, "output_tokens": 5},
            },
        )

    r = await anthropic(handler).generate_structured(
        ChatRequest(**{**REQ.__dict__, "json_schema": SCHEMA})
    )
    assert r.data == {"x": 1}
    assert seen["tool_choice"] == {"type": "tool", "name": "result"}
    assert seen["tools"][0]["input_schema"] == SCHEMA


@pytest.mark.parametrize(
    ("status", "headers", "exc"),
    [
        (429, {"retry-after": "7"}, RateLimitedError),
        (529, {}, RetryableError),
        (503, {}, RetryableError),
        (400, {}, PermanentError),
        (401, {}, PermanentError),
    ],
)
async def test_anthropic_error_classification(status: int, headers: dict, exc: type) -> None:
    p = anthropic(lambda _: httpx.Response(status, headers=headers, json={"error": {}}))
    with pytest.raises(exc) as info:
        await p.generate(REQ)
    if status == 429:
        assert info.value.retry_after_s == 7
    assert type(info.value) is exc or issubclass(type(info.value), exc)


async def test_anthropic_timeout_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ProviderTimeoutError):
        await anthropic(handler).generate(REQ)


async def test_anthropic_stream_parses_sse() -> None:
    events = [
        (
            "message_start",
            {"type": "message_start", "message": {"model": "m", "usage": {"input_tokens": 12}}},
        ),
        (
            "content_block_delta",
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}},
        ),
        ("ping", {"type": "ping"}),
        (
            "content_block_delta",
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": " there"}},
        ),
        ("message_delta", {"type": "message_delta", "usage": {"output_tokens": 3}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    body = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events)
    p = anthropic(
        lambda _: httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
    )
    chunks = [c async for c in p.stream(REQ)]
    assert "".join(c.text for c in chunks) == "Hi there"
    assert chunks[-1].usage is not None
    assert (chunks[-1].usage.input_tokens, chunks[-1].usage.output_tokens) == (12, 3)


async def test_anthropic_has_no_embeddings() -> None:
    with pytest.raises(UnsupportedOperationError):
        await anthropic(lambda _: httpx.Response(200)).embed("m", ["x"])


# ----------------------------------------------------------------------------- OpenAI-compatible


async def test_openai_compat_generate_puts_system_first() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(
            200,
            json={
                "model": "m",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 1},
            },
        )

    r = await oai(handler).generate(REQ)
    assert r.text == "ok"
    assert seen["messages"][0] == {"role": "system", "content": "sys"}
    assert r.usage.input_tokens == 8


async def test_openai_compat_structured_rejects_non_json() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "Sure! {x: 1}"}}]})

    with pytest.raises(RetryableError, match="not valid JSON"):
        await oai(handler).generate_structured(
            ChatRequest(**{**REQ.__dict__, "json_schema": SCHEMA})
        )


async def test_openai_compat_embeddings_are_reordered_by_index() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "e",
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ],
            },
        )

    r = await oai(handler).embed("e", ["a", "b"])
    assert r.vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert r.dimensions == 2
    assert r.usage.estimated  # Ollama may not report usage: we say so instead of inventing 0


async def test_openai_compat_stream_with_usage_and_done() -> None:
    lines = [
        {"model": "m", "choices": [{"delta": {"content": "a"}}]},
        {"model": "m", "choices": [{"delta": {"content": "b"}}]},
        {"model": "m", "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
    ]
    body = "".join(f"data: {json.dumps(x)}\n\n" for x in lines) + "data: [DONE]\n\n"
    chunks = [c async for c in oai(lambda _: httpx.Response(200, text=body)).stream(REQ)]
    assert "".join(c.text for c in chunks) == "ab"
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.output_tokens == 2


def test_local_provider_ignores_proxy_env_hosted_uses_it() -> None:
    """Regression candidate (first Mac run): a corporate HTTP(S)_PROXY must not capture
    calls to Ollama on localhost; hosted APIs may legitimately need the proxy."""
    local = OpenAICompatProvider(
        "ollama", base_url="http://localhost:11434/v1", api_key=None, timeout_s=5, hosted=False
    )
    remote = OpenAICompatProvider(
        "openai", base_url="https://api.test/v1", api_key="k", timeout_s=5, hosted=True
    )
    assert local._client.trust_env is False
    assert remote._client.trust_env is True


async def test_connection_error_names_the_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(
        RetryableError, match=r"cannot reach http://ollama.test/v1 \(ConnectError\)"
    ):
        await oai(handler).generate(REQ)


async def test_anthropic_omits_temperature_when_the_model_rejects_it() -> None:
    """Mac finding (Phase 11 preflight): claude-sonnet-5-5 answers HTTP 400 "`temperature` is
    deprecated for this model". None = the field is not sent at all."""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"model": "m", "stop_reason": "end_turn",
                  "content": [{"type": "text", "text": "ok"}],
                  "usage": {"input_tokens": 1, "output_tokens": 1}},
        )  # fmt: skip

    from dataclasses import replace

    await anthropic(handler).generate(replace(REQ, temperature=None))
    await anthropic(handler).generate(REQ)
    assert "temperature" not in bodies[0]
    assert bodies[1]["temperature"] == 0.0
