# ruff: noqa: F811  - fixtures imported from tools/conftest are re-used as parameters below
"""End-to-end fixtures (Phase 8 + 9): incident-service, tool-gateway, audit, llm-gateway (fake
providers), agents, the api (as token exchange) and the orchestrator SERVICE - all in-process,
real Postgres, each with its own least-privilege login. Only the model is fake."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from aeoi_agents.config import Settings as AgentSettings
from aeoi_agents.main import build_app as build_agents
from aeoi_api.config import Settings as ApiSettings
from aeoi_api.main import build_app as build_api
from aeoi_db.config import database_url, libpq_dsn
from aeoi_db.users import ensure_login
from aeoi_incident.config import Settings as IncidentSettings
from aeoi_incident.events import InMemoryPublisher
from aeoi_incident.main import build_app as build_incident
from aeoi_llm.config import Settings as LlmSettings
from aeoi_llm.main import build_app as build_llm
from aeoi_orchestrator.config import Settings as OrchSettings
from aeoi_orchestrator.main import build_app as build_orch
from aeoi_security.testing import KeyPair, service_token_for

# re-use the Phase 7 fixtures (importing a fixture into a conftest registers it here)
from tests.integration.tools.conftest import (  # noqa: F401
    T0,
    Planted,
    Tokens,
    audit_app,
    audit_db_url,
    audit_token_file,
    catalog_file,
    dpriv,
    dpub,
    owner,
    planted,
    skeys,
    stub_rag,
    tkeys,
    tok,
    tools_app,
    tools_db_url,
    tpub,
)

LLM_CONFIG = Path(__file__).resolve().parents[3] / "services" / "llm-gateway" / "config"


def _login(db: str, user: str, role: str) -> str:
    password = secrets.token_hex(16)
    with psycopg.connect(libpq_dsn(database=db), autocommit=True) as conn:
        ensure_login(conn, user, role, password)
    return (
        database_url(database=db)
        .set(username=user, password=password)
        .render_as_string(hide_password=False)
    )


class HostRouter(httpx.AsyncBaseTransport):
    """One client, several in-process apps, chosen by host (like DNS for services)."""

    def __init__(self, apps: dict[str, FastAPI]) -> None:
        self._t = {host: httpx.ASGITransport(app=app) for host, app in apps.items()}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._t[request.url.host].handle_async_request(request)


@pytest.fixture(scope="session")
def orch_db_url(migrated_db: str) -> str:
    return _login(migrated_db, "orch_svc_test", "svc_orchestrator")


@pytest.fixture(scope="session")
def api_db_url(migrated_db: str) -> str:
    return _login(migrated_db, "api_svc_test", "svc_api")


@pytest.fixture(scope="session")
def incident_db_url(migrated_db: str) -> str:
    return _login(migrated_db, "incident_svc_p8", "svc_incident")


@pytest.fixture
async def incident_app(incident_db_url: str, tpub: Path) -> AsyncIterator[FastAPI]:
    app = build_incident(
        IncidentSettings(
            db_url_override=SecretStr(incident_db_url),
            jwt_public_key_file=tpub,
            relay_enabled=False,
            environment="test",
            db_pool_size=3,
        ),  # type: ignore[call-arg]
        publisher=InMemoryPublisher(),
    )
    yield app
    await app.state.engine.dispose()


@pytest.fixture
def llm_app(tpub: Path) -> FastAPI:
    return build_llm(
        LlmSettings(
            routing_file=LLM_CONFIG / "routing.test.yaml",
            jwt_public_key_file=tpub,
            record_usage=False,
            cache_enabled=False,
            environment="test",
            log_json=False,
        )  # type: ignore[call-arg]
    )


@pytest.fixture
def agents_token_file(tmp_path: Path, tkeys: KeyPair) -> Path:
    p = tmp_path / "agents_service_token.txt"
    p.write_text(service_token_for(tkeys, "agents", "tools:invoke", "llm:invoke"))
    return p


@pytest.fixture
async def agents_app(
    tpub: Path, dpub: Path, agents_token_file: Path, tools_app: FastAPI, llm_app: FastAPI
) -> AsyncIterator[FastAPI]:
    app = build_agents(
        AgentSettings(
            jwt_public_key_file=tpub,
            delegation_public_key_file=dpub,
            service_token_file=agents_token_file,
            tool_gateway_url="http://tools",
            llm_gateway_url="http://llm",
            environment="test",
            log_json=False,
        ),  # type: ignore[call-arg]
        tool_transport=httpx.ASGITransport(app=tools_app),
        llm_transport=httpx.ASGITransport(app=llm_app),
    )
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
def orch_token(tkeys: KeyPair) -> str:
    return service_token_for(
        tkeys,
        "orchestrator",
        "agents:run",
        "evidence:write",
        "hypotheses:write",
        "delegation:create",
    )


@pytest.fixture
async def api_app(api_db_url: str, tpub: Path, dpriv: Path) -> AsyncIterator[FastAPI]:
    """The api, here only as the token exchange (STS, ADR-019)."""
    app = build_api(
        ApiSettings(
            jwt_public_key_file=tpub,
            db_url_override=SecretStr(api_db_url),
            delegation_private_key_file=dpriv,
            environment="test",
            log_json=False,
        )  # type: ignore[call-arg]
    )
    async with app.router.lifespan_context(app):
        yield app


class People:
    """Users in identity.users: the STS trusts the DIRECTORY, not the token's role claims."""

    def __init__(self, owner: psycopg.Connection, tok: Tokens) -> None:
        self.owner = owner
        self.tok = tok

    def add(self, *roles: str, active: bool = True) -> UUID:
        uid = uuid4()
        self.owner.execute(
            "INSERT INTO identity.users (id, external_subject, email, display_name, is_active) "
            "VALUES (%s, %s, %s, 'Test User', %s)",
            (uid, f"oidc|test-{uid}", f"{uid}@northwind.example", active),
        )
        for role in roles:
            self.owner.execute(
                "INSERT INTO identity.user_roles (user_id, role_name) VALUES (%s, %s)", (uid, role)
            )
        return uid

    def token(self, uid: UUID, *claimed_roles: str) -> str:
        """A valid user token for `uid` (the roles it CLAIMS may differ from the directory)."""
        return self.tok.user(claimed_roles[0] if claimed_roles else "SRE", user_id=uid)


@pytest.fixture
def people(owner: psycopg.Connection, tok: Tokens) -> People:
    return People(owner, tok)


OrchFactory = Callable[..., Any]


@pytest.fixture
def make_orch(
    orch_db_url: str,
    orch_token: str,
    tpub: Path,
    agents_app: FastAPI,
    incident_app: FastAPI,
    api_app: FastAPI,
) -> OrchFactory:
    """`async with make_orch(**settings) as app:` = one orchestrator PROCESS (lifespan: pool
    open, optional resume on start; exit = shutdown, rows stay RUNNING). Call it twice in one
    test to simulate a restart. `transport` wraps the default host router."""

    @asynccontextmanager
    async def factory(
        transport: httpx.AsyncBaseTransport | None = None, **overrides: Any
    ) -> AsyncIterator[FastAPI]:
        settings = OrchSettings(
            db_url_override=SecretStr(orch_db_url),
            jwt_public_key_file=tpub,
            incident_service_url="http://incident",
            agents_url="http://agents",
            api_url="http://api",
            environment="test",
            log_json=False,
            # Phase 9 tests pin ONE agent (their counts assume it); Phase 10 tests pass agents=
            # Phase 11 reasoning is OFF unless a test turns it on (reasoning=True)
            **{
                "resume_on_startup": False,
                "agents": ("log_analysis",),
                "reasoning": False,
                **overrides,
            },
        )  # type: ignore[call-arg]
        app = build_orch(
            settings,
            transport=transport
            or HostRouter({"incident": incident_app, "agents": agents_app, "api": api_app}),
            service_token=orch_token,
        )
        async with app.router.lifespan_context(app):
            yield app

    return factory


def hosts(incident_app: FastAPI, agents_app: FastAPI, api_app: FastAPI) -> dict[str, FastAPI]:
    return {"incident": incident_app, "agents": agents_app, "api": api_app}


@pytest.fixture
async def incident(incident_app: FastAPI, tok: Tokens, planted: Planted) -> dict[str, Any]:
    """A fresh incident on the planted service, detected 15 min into the planted logs."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=incident_app), base_url="http://incident"
    ) as c:
        r = await c.post(
            "/v1/incidents",
            json={
                "title": "Checkout pool timeouts after deploy",
                "severity": "SEV2",
                "affected_services": [planted.svc],
                "detected_at": (T0 + timedelta(minutes=15)).isoformat(),
            },
            headers={**tok.h("SRE"), "Idempotency-Key": secrets.token_hex(8)},
        )
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]
