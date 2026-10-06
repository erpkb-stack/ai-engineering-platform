"""Gateway behaviour with a fake upstream (no network, no DB)."""

import json
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from aeoi_api.config import Settings
from aeoi_api.main import build_app
from aeoi_api.ratelimit import InMemoryTokenBucket
from aeoi_security.testing import KeyPair, generate_keypair, token_for


@pytest.fixture(scope="module")
def keys() -> KeyPair:
    return generate_keypair()


@pytest.fixture
def pubfile(tmp_path: Path, keys: KeyPair) -> Path:
    p = tmp_path / "pub.pem"
    p.write_text(keys.public_pem)
    return p


class FakeUpstream:
    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.mode = "ok"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.mode == "down":
            raise httpx.ConnectError("refused", request=request)
        if self.mode == "slow":
            raise httpx.ReadTimeout("slow", request=request)
        if request.url.path == "/v1/incidents" and request.method == "POST":
            return httpx.Response(
                201,
                json={"id": "abc", "version": 1},
                headers={
                    "Location": "/v1/incidents/abc",
                    "ETag": '"1"',
                    "X-Internal-Secret": "leak",
                },
            )
        if request.url.path.startswith("/v1/incidents/missing"):
            return httpx.Response(
                404,
                json={"title": "Resource not found", "status": 404},
                headers={"content-type": "application/problem+json"},
            )
        return httpx.Response(200, json={"items": [], "next_cursor": None})


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()


def make_app(pubfile: Path, upstream: FakeUpstream, burst: int = 30) -> FastAPI:
    settings = Settings(jwt_public_key_file=pubfile, environment="test")  # type: ignore[arg-type]
    return build_app(
        settings,
        transports={"incident-service": httpx.MockTransport(upstream)},
        limiter=InMemoryTokenBucket(rate_per_s=0.001, burst=burst),
    )


@pytest.fixture
def client(pubfile: Path, upstream: FakeUpstream) -> httpx.AsyncClient:
    app = make_app(pubfile, upstream)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api")


def auth(keys: KeyPair, *roles: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_for(keys, *roles)}"}


BODY = {
    "title": "HTTP 500 errors after deploy",
    "severity": "SEV2",
    "affected_services": ["checkout-api"],
}


async def test_unauthenticated_never_reaches_upstream(
    client: httpx.AsyncClient, upstream: FakeUpstream
) -> None:
    r = await client.get("/api/v1/incidents")
    assert r.status_code == 401
    assert upstream.calls == []


async def test_forbidden_never_reaches_upstream(
    client: httpx.AsyncClient, upstream: FakeUpstream, keys: KeyPair
) -> None:
    r = await client.post("/api/v1/incidents", json=BODY, headers=auth(keys, "MANAGER"))
    assert r.status_code == 403
    assert upstream.calls == []


async def test_invalid_body_rejected_at_the_edge(
    client: httpx.AsyncClient, upstream: FakeUpstream, keys: KeyPair
) -> None:
    r = await client.post(
        "/api/v1/incidents", json={"title": "x", "severity": "SEV9"}, headers=auth(keys, "SRE")
    )
    assert r.status_code == 422
    assert upstream.calls == []


async def test_forwards_only_allowed_headers_and_rewrites_location(
    client: httpx.AsyncClient, upstream: FakeUpstream, keys: KeyPair
) -> None:
    idem_key = f"idem-{uuid.uuid4()}"  # built at runtime: literals look like secrets to gitleaks
    headers = auth(keys, "SRE") | {
        "Idempotency-Key": idem_key,
        "X-Admin": "true",
        "X-Correlation-ID": "corr-123456",
    }
    r = await client.post("/api/v1/incidents", json=BODY, headers=headers)
    assert r.status_code == 201
    sent = upstream.calls[0]
    assert sent.headers["idempotency-key"] == idem_key
    assert sent.headers["authorization"].startswith("Bearer ")
    assert sent.headers["x-correlation-id"] == "corr-123456"
    assert "x-admin" not in sent.headers  # client headers are not passed through
    assert json.loads(sent.content)["severity"] == "SEV2"
    assert r.headers["location"] == "/api/v1/incidents/abc"
    assert r.headers["etag"] == '"1"'
    assert "x-internal-secret" not in r.headers


async def test_upstream_problem_passes_through(client: httpx.AsyncClient, keys: KeyPair) -> None:
    r = await client.get("/api/v1/incidents/missing", headers=auth(keys, "ENGINEER"))
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")


async def test_upstream_down_is_503_and_get_retried_once(
    client: httpx.AsyncClient, upstream: FakeUpstream, keys: KeyPair
) -> None:
    upstream.mode = "down"
    r = await client.get("/api/v1/incidents", headers=auth(keys, "ENGINEER"))
    assert r.status_code == 503
    assert r.headers["retry-after"] == "1"
    assert len(upstream.calls) == 2  # GET retried once on connect error


async def test_post_is_never_retried(
    client: httpx.AsyncClient, upstream: FakeUpstream, keys: KeyPair
) -> None:
    upstream.mode = "down"
    r = await client.post(
        "/api/v1/incidents",
        json=BODY,
        headers=auth(keys, "SRE") | {"Idempotency-Key": f"k-{uuid.uuid4()}"},
    )
    assert r.status_code == 503
    assert len(upstream.calls) == 1


async def test_timeout_is_503(
    client: httpx.AsyncClient, upstream: FakeUpstream, keys: KeyPair
) -> None:
    upstream.mode = "slow"
    r = await client.get("/api/v1/incidents", headers=auth(keys, "ENGINEER"))
    assert r.status_code == 503


async def test_rate_limit(pubfile: Path, upstream: FakeUpstream, keys: KeyPair) -> None:
    app = make_app(pubfile, upstream, burst=2)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api")
    h = auth(keys, "ENGINEER")
    codes = [(await client.get("/api/v1/incidents", headers=h)).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


async def test_me(client: httpx.AsyncClient, keys: KeyPair) -> None:
    r = await client.get("/api/v1/me", headers=auth(keys, "INCIDENT_COMMANDER"))
    assert r.status_code == 200
    assert "actions:approve" in r.json()["permissions"]


async def test_openapi_lists_only_real_routes(client: httpx.AsyncClient) -> None:
    paths = (await client.get("/openapi.json")).json()["paths"]
    assert "/api/v1/incidents" in paths
    assert "/api/v1/search" in paths  # Phase 6
    assert "/api/v1/documents/{document_id}" in paths
    assert "/api/v1/approvals" not in paths  # Phase 16 adds it; no stubs that lie
