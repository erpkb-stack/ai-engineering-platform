"""The client library and the service agree on the wire format (consumer-side contract test)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from aeoi_llm.config import Settings
from aeoi_llm.main import build_app
from aeoi_llm.providers import FakeProvider
from aeoi_llm_client import CallMetadata, FakeLLMClient, HttpLLMClient, LLMError, Msg
from aeoi_security.testing import generate_keypair, service_token_for

CONFIG = Path(__file__).resolve().parents[1] / "config"


@pytest.fixture
def client(tmp_path: Path) -> HttpLLMClient:
    keys = generate_keypair()
    pub = tmp_path / "pub.pem"
    pub.write_text(keys.public_pem)
    app = build_app(
        Settings(
            routing_file=CONFIG / "routing.test.yaml",
            jwt_public_key_file=pub,
            record_usage=False,
            environment="test",
            log_json=False,
        ),  # type: ignore[call-arg]
        providers={
            "hosted_fake": FakeProvider("hosted_fake", hosted=True),
            "local_fake": FakeProvider("local_fake"),
        },
    )

    async def token() -> str:
        return service_token_for(keys, "orchestrator", "llm:invoke")

    return HttpLLMClient("http://llm", token, transport=httpx.ASGITransport(app=app))


async def test_generate_roundtrip(client: HttpLLMClient) -> None:
    r = await client.generate(
        [Msg(role="user", content="hi")],
        metadata=CallMetadata(agent_name="triage", prompt_version=2),
    )
    assert r.model == "fake-large"
    assert r.usage.input_tokens > 0


async def test_structured_roundtrip(client: HttpLLMClient) -> None:
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    r = await client.generate_structured([Msg(role="user", content="hi")], schema)
    assert r.data == {"ok": False}


async def test_embed_roundtrip(client: HttpLLMClient) -> None:
    r = await client.embed(["a"])
    assert r.dimensions == 768


async def test_problem_json_becomes_typed_error(client: HttpLLMClient) -> None:
    with pytest.raises(LLMError) as info:
        await client.generate([Msg(role="user", content="hi")], route="nope")
    assert info.value.status == 400
    assert info.value.type.endswith("/bad-request")


async def test_fake_client_records_calls() -> None:
    fake = FakeLLMClient(texts=["a"], data=[{"x": 1}])
    assert (await fake.generate([Msg(role="user", content="q")])).text == "a"
    assert (await fake.generate_structured([Msg(role="user", content="q")], {})).data == {"x": 1}
    assert [c["op"] for c in fake.calls] == ["generate", "generate_structured"]
