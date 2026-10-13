# ruff: noqa: F811  - fixtures imported from the agents conftest are re-used as parameters
"""Phase 17a (ADR-027): every service trusts the OIDC IdP - against a REAL Keycloak.

Skipped unless a Keycloak with the generated realm runs (like the Kafka test):
  AEOI_TEST_KEYCLOAK_URL=http://localhost:8090   (make keycloak-up)
  AEOI_TEST_KEYCLOAK_USERS=secrets/keycloak/users.txt   (make keycloak-realm)
  the realm's `aeoi-cli` client (dev only) gives tokens without a browser.

What it proves, in-process, with the real IdP's keys:
- an incident-service and an api built from settings verify a Keycloak access token, map roles
  from the realm, and authorise by OUR permissions;
- the token exchange accepts a Keycloak subject token ONLY for a user in OUR directory (uid +
  aeoi_sub), and refuses one that is not there - the directory stays the source of truth;
- production mode (static_user_tokens off): dev user tokens 401, Keycloak 200;
- a token from a client the services do not list, and a tampered token: 401.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import jwt
import psycopg
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from aeoi_api.config import Settings as ApiSettings
from aeoi_api.main import build_app as build_api
from aeoi_incident.config import Settings as IncidentSettings
from aeoi_incident.events import InMemoryPublisher
from aeoi_incident.main import build_app as build_incident
from aeoi_security.testing import KeyPair, service_token_for
from aeoi_tools.main import build_app as build_tools
from tests.integration.agents.conftest import (  # noqa: F401
    T0,
    Planted,
    api_db_url,
    audit_app,
    audit_db_url,
    audit_token_file,
    catalog_file,
    dpriv,
    dpub,
    incident_db_url,
    owner,
    planted,
    skeys,
    stub_rag,
    tkeys,
    tok,
    tools_db_url,
    tpub,
)
from tests.integration.tools.conftest import Tokens, make_stub_rag, tools_settings

pytestmark = pytest.mark.integration
KC = os.environ.get("AEOI_TEST_KEYCLOAK_URL", "")
USERS = os.environ.get("AEOI_TEST_KEYCLOAK_USERS", "")
ISSUER = f"{KC.rstrip('/')}/realms/aeoi"
if not KC or not USERS or not Path(USERS).is_file():
    pytest.skip("needs a running Keycloak (AEOI_TEST_KEYCLOAK_URL, AEOI_TEST_KEYCLOAK_USERS)",
                allow_module_level=True)  # fmt: skip
OIDC = {"oidc_issuer": ISSUER, "oidc_clients": "aeoi-console,aeoi-cli"}


def kc_token(role_hint: str) -> str:
    """Password grant via the dev-only `aeoi-cli` client, for the first user whose email
    matches (users.txt holds email + password per active user)."""
    for line in Path(USERS).read_text().splitlines():
        email, _, password = line.partition(" ")
        if role_hint in email:
            r = httpx.post(f"{ISSUER}/protocol/openid-connect/token", data={
                "grant_type": "password", "client_id": "aeoi-cli",
                "username": email, "password": password,
            }, timeout=10, trust_env=False)  # fmt: skip
            r.raise_for_status()
            return str(r.json()["access_token"])
    raise LookupError(role_hint)


def claims(token: str) -> dict[str, Any]:
    return jwt.decode(token, options={"verify_signature": False})  # type: ignore[no-any-return]


def remove_user(owner: psycopg.Connection, uid: UUID) -> None:
    """Start from 'not in the directory', whatever earlier tests (the seed) put there."""
    for sql in ("DELETE FROM identity.delegation_events WHERE user_id = %s",
                "DELETE FROM identity.delegation_grants WHERE user_id = %s",
                "DELETE FROM identity.user_groups WHERE user_id = %s",
                "DELETE FROM identity.user_roles WHERE user_id = %s",
                "DELETE FROM identity.users WHERE id = %s"):  # fmt: skip
        owner.execute(sql, (uid,))


def asgi(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x")


@pytest.fixture
async def incident_kc(incident_db_url: str, tpub: Path) -> AsyncIterator[FastAPI]:
    app = build_incident(IncidentSettings(  # type: ignore[call-arg]
        db_url_override=SecretStr(incident_db_url), jwt_public_key_file=tpub,
        relay_enabled=False, environment="test", **OIDC,
    ), publisher=InMemoryPublisher())  # fmt: skip
    yield app
    await app.state.engine.dispose()


@pytest.fixture
async def api_kc(api_db_url: str, tpub: Path, dpriv: Path) -> AsyncIterator[FastAPI]:
    app = build_api(ApiSettings(  # type: ignore[call-arg]
        jwt_public_key_file=tpub, db_url_override=SecretStr(api_db_url),
        delegation_private_key_file=dpriv, environment="test", log_json=False, **OIDC,
    ))  # fmt: skip
    async with app.router.lifespan_context(app):
        yield app


async def test_a_keycloak_token_is_verified_and_authorised_by_our_permissions(
    incident_kc: FastAPI,
) -> None:
    ic = kc_token("lena.ivanova")
    assert (
        claims(ic)["iss"] == ISSUER and "INCIDENT_COMMANDER" in claims(ic)["realm_access"]["roles"]
    )
    async with asgi(incident_kc) as c:
        r = await c.get("/v1/incidents?limit=1", headers={"Authorization": f"Bearer {ic}"})
        assert r.status_code == 200, r.text
        bad = ic[:-6] + ("A" if ic[-6] != "A" else "B") + ic[-5:]  # tampered signature
        r = await c.get("/v1/incidents?limit=1", headers={"Authorization": f"Bearer {bad}"})
        assert r.status_code == 401


async def test_a_client_the_services_do_not_list_is_refused(
    incident_db_url: str, tpub: Path
) -> None:
    app = build_incident(IncidentSettings(  # type: ignore[call-arg]
        db_url_override=SecretStr(incident_db_url), jwt_public_key_file=tpub,
        relay_enabled=False, environment="test", oidc_issuer=ISSUER,
            oidc_clients="aeoi-console",  # EXPLICIT: make exports .env (which lists aeoi-cli)
    ), publisher=InMemoryPublisher())  # fmt: skip
    try:
        async with asgi(app) as c:
            r = await c.get("/v1/incidents?limit=1",
                            headers={"Authorization": f"Bearer {kc_token('lena.ivanova')}"})  # fmt: skip
        assert r.status_code == 401
    finally:
        await app.state.engine.dispose()


async def test_production_mode_users_only_from_the_idp(
    incident_db_url: str, tpub: Path, tok: Tokens
) -> None:
    app = build_incident(IncidentSettings(  # type: ignore[call-arg]
        db_url_override=SecretStr(incident_db_url), jwt_public_key_file=tpub,
        relay_enabled=False, environment="test", static_user_tokens=False, **OIDC,
    ), publisher=InMemoryPublisher())  # fmt: skip
    try:
        async with asgi(app) as c:
            dev = await c.get("/v1/incidents?limit=1", headers=tok.h("INCIDENT_COMMANDER"))
            kc = await c.get("/v1/incidents?limit=1",
                             headers={"Authorization": f"Bearer {kc_token('lena.ivanova')}"})  # fmt: skip
        assert dev.status_code == 401 and kc.status_code == 200, (dev.text, kc.text)
    finally:
        await app.state.engine.dispose()


async def test_the_token_exchange_accepts_keycloak_only_for_a_user_in_our_directory(
    api_kc: FastAPI, owner: psycopg.Connection, tkeys: KeyPair
) -> None:
    token = kc_token("lena.ivanova")
    c = claims(token)
    uid, sub = UUID(c["uid"]), c["aeoi_sub"]
    svc = {
        "Authorization": f"Bearer {service_token_for(tkeys, 'orchestrator', 'delegation:create')}"
    }
    body = {"subject_token": token, "investigation_id": str(uuid4()), "incident_id": str(uuid4())}
    remove_user(owner, uid)
    owner.execute("DELETE FROM identity.users WHERE external_subject = %s", (sub,))
    async with asgi(api_kc) as cl:
        absent = await cl.post("/internal/v1/delegations", json=body, headers=svc)
    assert absent.status_code == 403, absent.text  # not in THIS directory: refused
    owner.execute(
        "INSERT INTO identity.users (id, external_subject, email, display_name, is_active) "
        "VALUES (%s, %s, %s, %s, true) ON CONFLICT (id) DO NOTHING",
        (uid, sub, c["email"], c["name"]),
    )
    owner.execute(
        "INSERT INTO identity.user_roles (user_id, role_name) VALUES (%s, 'INCIDENT_COMMANDER') "
        "ON CONFLICT DO NOTHING", (uid,),
    )  # fmt: skip
    body["investigation_id"] = str(uuid4())
    async with asgi(api_kc) as cl:
        ok = await cl.post("/internal/v1/delegations", json=body, headers=svc)
    assert ok.status_code == 201, ok.text
    delegated = jwt.decode(ok.json()["access_token"], options={"verify_signature": False})
    assert delegated["uid"] == str(uid) and delegated["sub"] == sub  # OUR ids, not Keycloak's


async def test_the_tool_gateway_trusts_keycloak_in_both_slots(
    tools_db_url: str, tpub: Path, catalog_file: Path, audit_token_file: Path, stub_rag: Any,
    audit_app: FastAPI, dpub: Path, planted: Planted, tkeys: KeyPair,
) -> None:  # fmt: skip
    """The request path ends here: a USER token as the bearer (manual mode) and the same
    token forwarded on-behalf-of by a service (agent mode) - both from Keycloak."""
    from datetime import timedelta

    app = build_tools(
        tools_settings(
            tools_db_url,
            tpub,
            catalog_file,
            audit_token_file,
            delegation_public_key_file=dpub,
            **OIDC,
        ),
        rag_transport=httpx.ASGITransport(app=make_stub_rag(stub_rag)),
        audit_transport=httpx.ASGITransport(app=audit_app),
    )
    sre = kc_token("hana.okafor")
    args = {"service_key": planted.svc, "start": T0.isoformat(),
            "end": (T0 + timedelta(minutes=30)).isoformat()}  # fmt: skip
    agents = service_token_for(tkeys, "agents", "tools:invoke")
    try:
        async with asgi(app) as c:
            manual = await c.post("/v1/tools/search_logs/invoke", json={"args": args},
                                  headers={"Authorization": f"Bearer {sre}"})  # fmt: skip
            obo = await c.post("/v1/tools/search_logs/invoke",
                               json={"args": args, "agent_name": "log_analysis"},
                               headers={"Authorization": f"Bearer {agents}",
                                        "X-On-Behalf-Of": f"Bearer {sre}"})  # fmt: skip
            as_bearer_for_agent = await c.post(
                "/v1/tools/search_logs/invoke", json={"args": args, "agent_name": "log_analysis"},
                headers={"Authorization": f"Bearer {sre}"})  # fmt: skip
    finally:
        await app.state.engine.dispose()
    assert manual.status_code == 200, manual.text
    assert obo.status_code == 200, obo.text
    assert as_bearer_for_agent.status_code in (400, 403)  # a user can never assert an agent


async def test_the_api_refuses_a_keycloak_user_the_directory_no_longer_backs(
    api_kc: FastAPI, owner: psycopg.Connection
) -> None:
    """Review H1, end to end with a real Keycloak token: absent -> 401, active -> 200,
    deactivated -> 401, demoted in the directory (realm still says IC) -> 403."""
    from aeoi_api.directory import DirectoryCheck

    api_kc.state.directory = DirectoryCheck(api_kc.state.sts_sessions, ttl_s=0)
    token = kc_token("lena.ivanova")
    c = claims(token)
    uid, sub = UUID(c["uid"]), c["aeoi_sub"]
    h = {"Authorization": f"Bearer {token}"}

    async def me() -> int:
        async with asgi(api_kc) as cl:
            return (await cl.get("/api/v1/me", headers=h)).status_code

    remove_user(owner, uid)
    assert await me() == 401
    owner.execute("INSERT INTO identity.users (id, external_subject, email, display_name, "
                  "is_active) VALUES (%s, %s, %s, %s, true)", (uid, sub, c["email"], c["name"]))  # fmt: skip
    owner.execute("INSERT INTO identity.user_roles (user_id, role_name) VALUES (%s, "
                  "'INCIDENT_COMMANDER')", (uid,))  # fmt: skip
    assert await me() == 200
    owner.execute("UPDATE identity.users SET is_active = false WHERE id = %s", (uid,))
    assert await me() == 401
    owner.execute("UPDATE identity.users SET is_active = true WHERE id = %s", (uid,))
    owner.execute("UPDATE identity.user_roles SET role_name = 'SRE' WHERE user_id = %s", (uid,))
    assert await me() == 403
