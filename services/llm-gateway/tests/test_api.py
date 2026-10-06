"""HTTP contract: auth (service scope only), problem+json, SSE. In-process, fakes, no DB."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from aeoi_llm.config import Settings
from aeoi_llm.main import build_app
from aeoi_llm.providers import FakeProvider
from aeoi_security.testing import KeyPair, generate_keypair, service_token_for, token_for

CONFIG = Path(__file__).resolve().parents[1] / "config"
BODY = {"route": "reasoning", "messages": [{"role": "user", "content": "hello"}]}


@pytest.fixture(scope="module")
def keys() -> KeyPair:
    return generate_keypair()


@pytest.fixture
def app(tmp_path: Path, keys: KeyPair) -> FastAPI:
    pub = tmp_path / "jwt_public.pem"
    pub.write_text(keys.public_pem)
    settings = Settings(
        routing_file=CONFIG / "routing.test.yaml",
        jwt_public_key_file=pub,
        record_usage=False,
        environment="test",
        log_json=False,
    )  # type: ignore[call-arg]
    return build_app(
        settings,
        providers={
            "hosted_fake": FakeProvider("hosted_fake", hosted=True),
            "local_fake": FakeProvider("local_fake"),
        },
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://llm"
    ) as c:
        yield c


def svc(keys: KeyPair, *scopes: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {service_token_for(keys, 'orchestrator', *scopes)}"}


async def test_no_token_is_401(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/generate", json=BODY)
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")


async def test_user_token_is_403_even_for_admin(client: httpx.AsyncClient, keys: KeyPair) -> None:
    r = await client.post(
        "/v1/generate", json=BODY, headers={"Authorization": f"Bearer {token_for(keys, 'ADMIN')}"}
    )
    assert r.status_code == 403


async def test_service_without_scope_is_403(client: httpx.AsyncClient, keys: KeyPair) -> None:
    r = await client.post("/v1/generate", json=BODY, headers=svc(keys, "tools:invoke"))
    assert r.status_code == 403


async def test_generate_ok(client: httpx.AsyncClient, keys: KeyPair) -> None:
    r = await client.post("/v1/generate", json=BODY, headers=svc(keys, "llm:invoke"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"] == "fake-large"
    assert body["fallback_used"] is False
    assert set(body["usage"]) == {"input_tokens", "output_tokens", "cached_tokens", "estimated"}
    assert "x-correlation-id" in {k.lower() for k in r.headers}


async def test_unknown_route_is_problem_json_400(client: httpx.AsyncClient, keys: KeyPair) -> None:
    r = await client.post(
        "/v1/generate", json={**BODY, "route": "nope"}, headers=svc(keys, "llm:invoke")
    )
    assert r.status_code == 400
    assert r.json()["type"].endswith("/bad-request")


async def test_extra_fields_are_rejected(client: httpx.AsyncClient, keys: KeyPair) -> None:
    r = await client.post(
        "/v1/generate", json={**BODY, "model": "claude-opus-5-5"}, headers=svc(keys, "llm:invoke")
    )
    assert r.status_code == 422  # callers choose a ROUTE, never a raw model


async def test_structured(client: httpx.AsyncClient, keys: KeyPair) -> None:
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    r = await client.post(
        "/v1/generate_structured",
        json={**BODY, "json_schema": schema},
        headers=svc(keys, "llm:invoke"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"n": 0}


async def test_stream_is_sse(client: httpx.AsyncClient, keys: KeyPair) -> None:
    async with client.stream("POST", "/v1/stream", json=BODY, headers=svc(keys, "llm:invoke")) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        text = (await r.aread()).decode()
    events = [line.split(": ", 1)[1] for line in text.splitlines() if line.startswith("event: ")]
    assert events[0] == "meta"
    assert events[-1] == "done"
    assert "delta" in events


async def test_stream_unknown_route_is_400_not_broken_stream(
    client: httpx.AsyncClient, keys: KeyPair
) -> None:
    r = await client.post(
        "/v1/stream", json={**BODY, "route": "nope"}, headers=svc(keys, "llm:invoke")
    )
    assert r.status_code == 400


async def test_embed(client: httpx.AsyncClient, keys: KeyPair) -> None:
    r = await client.post("/v1/embed", json={"inputs": ["a", "b"]}, headers=svc(keys, "llm:invoke"))
    assert r.status_code == 200
    assert r.json()["dimensions"] == 768


async def test_routes_never_expose_keys(client: httpx.AsyncClient, keys: KeyPair) -> None:
    r = await client.get("/v1/routes", headers=svc(keys, "llm:invoke"))
    assert r.status_code == 200
    body = r.json()
    assert body["routes"]["reasoning"]["chain"] == ["fake-large", "fake-small"]
    assert "api_key" not in r.text.lower()


async def test_health_needs_no_auth(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health/live")).status_code == 200
