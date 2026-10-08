"""Fixtures to run incident-service (and the api) in-process against the test database.

The service connects as a LOGIN user that is only a member of svc_incident - the same
least-privilege setup as `make db-users` - so these tests also prove the grants suffice.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from aeoi_api.config import Settings as ApiSettings
from aeoi_api.main import build_app as build_api
from aeoi_api.ratelimit import InMemoryTokenBucket
from aeoi_db.config import database_url, libpq_dsn
from aeoi_db.users import ensure_login
from aeoi_incident.config import Settings
from aeoi_incident.events import InMemoryPublisher
from aeoi_incident.main import build_app
from aeoi_security.testing import KeyPair, generate_keypair, token_for

TEST_LOGIN = "incident_svc_test"


@pytest.fixture(scope="session")
def keys() -> KeyPair:
    return generate_keypair()


@pytest.fixture(scope="session")
def pubfile(tmp_path_factory: pytest.TempPathFactory, keys: KeyPair) -> Path:
    path = tmp_path_factory.mktemp("keys") / "jwt_public.pem"
    path.write_text(keys.public_pem)
    return path


@pytest.fixture(scope="session")
def service_db_url(migrated_db: str) -> str:
    password = secrets.token_hex(16)
    with psycopg.connect(libpq_dsn(database=migrated_db), autocommit=True) as conn:
        ensure_login(conn, TEST_LOGIN, "svc_incident", password)
    url = database_url(database=migrated_db).set(username=TEST_LOGIN, password=password)
    return url.render_as_string(hide_password=False)


@pytest.fixture
def publisher() -> InMemoryPublisher:
    return InMemoryPublisher()


@pytest.fixture
async def incident_app(
    service_db_url: str, pubfile: Path, publisher: InMemoryPublisher
) -> AsyncIterator[FastAPI]:
    settings = Settings(
        db_url_override=SecretStr(service_db_url),
        jwt_public_key_file=pubfile,
        relay_enabled=False,
        environment="test",
        db_pool_size=5,
    )  # type: ignore[call-arg]
    app = build_app(settings, publisher=publisher)
    yield app
    await app.state.engine.dispose()


@pytest.fixture
async def client(incident_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=incident_app), base_url="http://incident"
    ) as c:
        yield c


class FakeOrchestrator(httpx.AsyncBaseTransport):
    """Records what the api forwards to the orchestrator (Phase 9); answers like it does.
    The real orchestrator path is tested end to end in tests/integration/agents."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.requests.append(request)
        inv = "01a11400-0000-7000-8000-000000000001"
        return httpx.Response(
            202,
            json={
                "request_id": "01a11400-0000-7000-8000-000000000002",
                "incident_id": "01a11400-0000-7000-8000-000000000003",
                "status": "RUNNING",
                "investigation_id": inv,
            },
            headers={"Location": f"/v1/investigations/{inv}"},
        )


@pytest.fixture
def fake_orchestrator() -> FakeOrchestrator:
    return FakeOrchestrator()


@pytest.fixture
async def api_client(
    incident_app: FastAPI, pubfile: Path, fake_orchestrator: FakeOrchestrator
) -> AsyncIterator[httpx.AsyncClient]:
    api = build_api(
        ApiSettings(jwt_public_key_file=pubfile, environment="test"),  # type: ignore[arg-type]
        transports={
            "incident-service": httpx.ASGITransport(app=incident_app),
            "orchestrator": fake_orchestrator,
        },
        limiter=InMemoryTokenBucket(rate_per_s=100, burst=100),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://api"
    ) as c:
        yield c


@pytest.fixture
def owner_conn(migrated_db: str) -> psycopg.Connection:
    """Owner connection for assertions on what the service wrote."""
    return psycopg.connect(libpq_dsn(database=migrated_db), autocommit=True)


def bearer(keys: KeyPair, *roles: str, **kw: object) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_for(keys, *roles, **kw)}"}  # type: ignore[arg-type]
