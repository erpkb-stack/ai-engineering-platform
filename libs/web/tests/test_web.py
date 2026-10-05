from typing import Annotated

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from pydantic import BaseModel

from aeoi_common.errors import NotFoundError
from aeoi_security import Perm, Principal
from aeoi_security.testing import KeyPair, generate_keypair, token_for
from aeoi_web import Authenticator, create_app, require


class Body(BaseModel):
    n: int


@pytest.fixture(scope="module")
def keys() -> KeyPair:
    return generate_keypair()


@pytest.fixture
def app(keys: KeyPair) -> FastAPI:
    r = APIRouter()

    @r.get("/missing")
    async def missing() -> None:
        raise NotFoundError("nope")

    @r.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret db password in message")

    @r.post("/body")
    async def body(b: Body) -> Body:
        return b

    @r.get("/secure")
    async def secure(
        p: Annotated[Principal, Depends(require(Perm.ACTIONS_APPROVE))],
    ) -> dict[str, str]:
        return {"user": str(p.user_id)}

    async def failing() -> None:
        raise ConnectionError

    return create_app(
        service_name="test",
        version="0",
        routers=[r],
        authenticator=Authenticator(keys.public_pem, "aeoi-dev-issuer", "aeoi-api"),
        readiness={"db": failing},
        environment="test",
    )


@pytest.fixture
async def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://t"
    )


async def test_correlation_id_echoed_when_valid(client: httpx.AsyncClient) -> None:
    r = await client.get("/health/live", headers={"X-Correlation-ID": "req-123456"})
    assert r.headers["x-correlation-id"] == "req-123456"


async def test_malformed_correlation_id_replaced(client: httpx.AsyncClient) -> None:
    r = await client.get("/health/live", headers={"X-Correlation-ID": "bad id\n<script>"})
    assert r.headers["x-correlation-id"] != "bad id\n<script>"
    assert len(r.headers["x-correlation-id"]) == 36


async def test_domain_error_is_problem_json(client: httpx.AsyncClient) -> None:
    r = await client.get("/missing")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["status"] == 404
    assert body["correlation_id"] == r.headers["x-correlation-id"]


async def test_unhandled_error_does_not_leak(client: httpx.AsyncClient) -> None:
    r = await client.get("/boom")
    assert r.status_code == 500
    assert "secret" not in r.text
    assert r.json()["correlation_id"]


async def test_validation_error_does_not_echo_input(client: httpx.AsyncClient) -> None:
    r = await client.post("/body", json={"n": "sk-ant-SECRET-VALUE"})  # gitleaks:allow - fake
    assert r.status_code == 422
    assert "SECRET" not in r.text


async def test_401_without_token(client: httpx.AsyncClient) -> None:
    r = await client.get("/secure")
    assert r.status_code == 401


async def test_401_with_garbage_token(client: httpx.AsyncClient) -> None:
    r = await client.get("/secure", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401
    assert "Invalid or expired" in r.json()["detail"]


async def test_403_without_permission(client: httpx.AsyncClient, keys: KeyPair) -> None:
    r = await client.get("/secure", headers={"Authorization": f"Bearer {token_for(keys, 'ADMIN')}"})
    assert r.status_code == 403


async def test_200_with_permission(client: httpx.AsyncClient, keys: KeyPair) -> None:
    token = token_for(keys, "INCIDENT_COMMANDER")
    r = await client.get("/secure", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


async def test_live_ok_but_ready_degraded(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health/live")).status_code == 200
    r = await client.get("/health/ready")
    assert r.status_code == 503
    assert r.json()["checks"]["db"].startswith("fail")
