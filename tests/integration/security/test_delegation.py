# ruff: noqa: F811  - fixtures imported from the agents conftest are re-used as parameters
"""Delegation security (ADR-019): the token exchange, where delegated tokens are accepted,
and what they are bound to. Real Postgres (identity schema, api_svc login)."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx
import jwt
import psycopg
import pytest
from fastapi import FastAPI

from aeoi_security.testing import KeyPair, delegated_token_for, service_token_for
from tests.integration.agents.conftest import (  # noqa: F401
    People,
    agents_app,
    agents_token_file,
    api_app,
    api_db_url,
    audit_app,
    audit_db_url,
    audit_token_file,
    catalog_file,
    dpriv,
    dpub,
    incident,
    incident_app,
    incident_db_url,
    llm_app,
    owner,
    people,
    planted,
    skeys,
    stub_rag,
    tkeys,
    tok,
    tools_app,
    tools_db_url,
    tpub,
)
from tests.integration.tools.conftest import Planted, Tokens

pytestmark = pytest.mark.integration
BASE = "/internal/v1/delegations"
INCIDENT = UUID("01a11400-0000-7000-8000-00000000cafe")  # soft ref: any id will do here


def orch(tkeys: KeyPair, *scopes: str) -> dict[str, str]:
    t = service_token_for(tkeys, "orchestrator", *(scopes or ("delegation:create",)))
    return {"Authorization": f"Bearer {t}"}


def asgi(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x")


async def exchange(
    api: FastAPI, headers: dict[str, str], subject: str, inv: UUID, incident: UUID = INCIDENT
) -> httpx.Response:
    body = {"subject_token": subject, "investigation_id": str(inv), "incident_id": str(incident)}
    async with asgi(api) as c:
        return await c.post(BASE, json=body, headers=headers)


async def call(
    api: FastAPI, path: str, headers: dict[str, str], body: Any = None
) -> httpx.Response:
    async with asgi(api) as c:
        return await c.post(path, json=body if body is not None else {}, headers=headers)


def sts_claims(token: str, skeys: KeyPair) -> dict[str, Any]:
    return jwt.decode(  # type: ignore[no-any-return]
        token, skeys.public_pem, algorithms=["RS256"], audience="aeoi-internal", issuer="aeoi-sts"
    )


async def test_exchange_mints_an_incident_and_investigation_bound_token_from_directory_roles(
    api_app: FastAPI, people: People, tkeys: KeyPair, skeys: KeyPair, owner: psycopg.Connection
) -> None:
    uid = people.add("SRE")
    inv = uuid4()
    # the token CLAIMS INCIDENT_COMMANDER; the directory says SRE -> the delegated token says SRE
    r = await exchange(api_app, orch(tkeys), people.token(uid, "INCIDENT_COMMANDER"), inv)
    assert r.status_code == 201, r.text
    body = r.json()
    claims = sts_claims(body["access_token"], skeys)
    assert claims["roles"] == ["SRE"] and claims["uid"] == str(uid)
    assert claims["inv"] == str(inv) and claims["inc"] == str(INCIDENT)
    assert claims["act"] == {"sub": "service:orchestrator"} and claims["token_use"] == "delegated"
    assert claims["exp"] - claims["iat"] <= 300
    events = owner.execute(
        "SELECT event FROM identity.delegation_events WHERE grant_id=%s ORDER BY created_at",
        (body["grant_id"],),
    ).fetchall()
    assert [e[0] for e in events] == ["CREATED", "ISSUED"]


@pytest.mark.parametrize("caller", ["user", "service-without-scope", "other-service-scope"])
async def test_only_a_service_with_the_scope_may_exchange(
    api_app: FastAPI, people: People, tkeys: KeyPair, tok: Tokens, caller: str
) -> None:
    uid = people.add("SRE")
    headers = {
        "user": tok.h("ADMIN"),
        "service-without-scope": orch(tkeys, "agents:run"),
        "other-service-scope": {
            "Authorization": "Bearer " + service_token_for(tkeys, "x", "llm:invoke")
        },
    }[caller]
    r = await exchange(api_app, headers, people.token(uid), uuid4())
    assert r.status_code == 403, r.text


async def test_subject_must_be_a_real_active_user_with_investigations_run(
    api_app: FastAPI, people: People, tkeys: KeyPair, skeys: KeyPair, tok: Tokens,
    owner: psycopg.Connection,
) -> None:  # fmt: skip
    inactive = people.add("SRE", active=False)
    manager = people.add("MANAGER")
    ok_user = people.add("SRE")
    cases = {
        "service subject": service_token_for(tkeys, "agents", "tools:invoke"),
        "delegated subject": delegated_token_for(skeys, "SRE", investigation_id=uuid4()),
        "unknown user": tok.user("SRE"),  # valid signature, not in the directory
        "inactive user": people.token(inactive),
        "no investigations:run": people.token(manager, "SRE"),
        "garbage": "not-a-jwt",
    }
    for name, subject in cases.items():
        r = await exchange(api_app, orch(tkeys), subject, uuid4())
        assert r.status_code == 403, (name, r.text)
    denied = owner.execute(
        "SELECT count(*) FROM identity.delegation_events WHERE event='DENIED' AND user_id = ANY(%s)",
        ([inactive, manager],),
    ).fetchone()
    assert denied is not None and denied[0] == 2
    assert (await exchange(api_app, orch(tkeys), people.token(ok_user), uuid4())).status_code == 201


async def test_one_grant_per_investigation(
    api_app: FastAPI, people: People, tkeys: KeyPair
) -> None:
    a, b = people.add("SRE"), people.add("SRE")
    inv = uuid4()
    first = await exchange(api_app, orch(tkeys), people.token(a), inv)
    retry = await exchange(api_app, orch(tkeys), people.token(a), inv)
    assert first.status_code == retry.status_code == 201
    assert first.json()["grant_id"] == retry.json()["grant_id"]  # idempotent for the same party
    assert (await exchange(api_app, orch(tkeys), people.token(b), inv)).status_code == 409
    other_incident = await exchange(api_app, orch(tkeys), people.token(a), inv, uuid4())
    assert other_incident.status_code == 409


async def test_every_refusal_leaves_an_event(
    api_app: FastAPI, people: People, tkeys: KeyPair, owner: psycopg.Connection
) -> None:
    """Review finding: 409 / 404 / non-actor revoke refusals left no trace in the history."""
    a, b = people.add("SRE"), people.add("SRE")
    inv = uuid4()
    grant = (await exchange(api_app, orch(tkeys), people.token(a), inv)).json()["grant_id"]
    assert (await exchange(api_app, orch(tkeys), people.token(b), inv)).status_code == 409
    thief = {"Authorization": "Bearer " + service_token_for(tkeys, "agents", "delegation:create")}
    revoke = await call(api_app, f"{BASE}/{grant}/revoke", thief, {"reason": "steal it"})
    assert revoke.status_code == 403
    missing = uuid4()
    assert (await call(api_app, f"{BASE}/{missing}/token", orch(tkeys))).status_code == 404
    rows = owner.execute(
        "SELECT detail FROM identity.delegation_events WHERE event='DENIED' AND "
        "(investigation_id=%s OR grant_id = ANY(%s))",
        (inv, [UUID(grant), missing]),
    ).fetchall()
    details = " | ".join(r[0] for r in rows)
    assert "another party" in details and "(revoke)" in details and "unknown grant" in details


async def test_refresh_rechecks_the_directory_and_only_the_actor_may_refresh(
    api_app: FastAPI, people: People, tkeys: KeyPair, skeys: KeyPair, owner: psycopg.Connection
) -> None:
    uid = people.add("SRE")
    grant = (await exchange(api_app, orch(tkeys), people.token(uid), uuid4())).json()["grant_id"]
    thief = {"Authorization": "Bearer " + service_token_for(tkeys, "agents", "delegation:create")}
    assert (await call(api_app, f"{BASE}/{grant}/token", thief)).status_code == 403
    owner.execute(
        "INSERT INTO identity.user_roles (user_id, role_name) VALUES (%s, 'INCIDENT_COMMANDER')",
        (uid,),
    )
    r = await call(api_app, f"{BASE}/{grant}/token", orch(tkeys))
    assert r.status_code == 200
    assert set(sts_claims(r.json()["access_token"], skeys)["roles"]) == {
        "SRE",
        "INCIDENT_COMMANDER",
    }
    owner.execute("DELETE FROM identity.user_roles WHERE user_id=%s", (uid,))
    r = await call(api_app, f"{BASE}/{grant}/token", orch(tkeys))
    assert r.status_code == 403 and "lost investigations:run" in r.text
    row = owner.execute(
        "SELECT revoked_reason FROM identity.delegation_grants WHERE id=%s", (grant,)
    ).fetchone()
    assert row is not None and row[0] == "user lost investigations:run"


async def test_revoked_and_expired_grants_mint_nothing(
    api_app: FastAPI, people: People, tkeys: KeyPair, owner: psycopg.Connection
) -> None:
    uid = people.add("SRE")
    g1 = (await exchange(api_app, orch(tkeys), people.token(uid), uuid4())).json()["grant_id"]
    r = await call(api_app, f"{BASE}/{g1}/revoke", orch(tkeys), {"reason": "test revoke"})
    assert r.status_code == 200 and r.json()["revoked"]
    again = await call(api_app, f"{BASE}/{g1}/revoke", orch(tkeys), {"reason": "second"})
    assert again.json()["revoked_reason"] == "test revoke"  # idempotent, first reason kept
    assert (await call(api_app, f"{BASE}/{g1}/token", orch(tkeys))).status_code == 403
    g2 = (await exchange(api_app, orch(tkeys), people.token(uid), uuid4())).json()["grant_id"]
    owner.execute(
        "UPDATE identity.delegation_grants SET created_at = now() - interval '3 hours', "
        "expires_at = now() - interval '1 hour' WHERE id=%s",
        (g2,),
    )
    r = await call(api_app, f"{BASE}/{g2}/token", orch(tkeys))
    assert r.status_code == 403 and "expired" in r.text


async def test_delegation_history_is_append_only_for_the_api(
    api_db_url: str, people: People, api_app: FastAPI, tkeys: KeyPair
) -> None:
    uid = people.add("SRE")
    grant = (await exchange(api_app, orch(tkeys), people.token(uid), uuid4())).json()["grant_id"]
    dsn = api_db_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn, autocommit=True) as conn:
        for stmt, arg in (
            ("UPDATE identity.delegation_events SET detail='x' WHERE grant_id=%s", grant),
            ("DELETE FROM identity.delegation_events WHERE grant_id=%s", grant),
            ("DELETE FROM identity.delegation_grants WHERE id=%s", grant),
            # review finding: an FK cascade (run as the OWNER) bypassed the revokes above
            ("DELETE FROM identity.users WHERE id=%s", uid),
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(stmt, (arg,))


async def test_even_the_owner_cannot_cascade_away_delegation_history(
    people: People, api_app: FastAPI, tkeys: KeyPair, owner: psycopg.Connection
) -> None:
    uid = people.add("SRE")
    grant = (await exchange(api_app, orch(tkeys), people.token(uid), uuid4())).json()["grant_id"]
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        owner.execute("DELETE FROM identity.users WHERE id=%s", (uid,))
    row = owner.execute(
        "SELECT count(*) FROM identity.delegation_events WHERE grant_id=%s", (grant,)
    ).fetchone()
    assert row is not None and row[0] >= 2


async def test_a_delegated_token_is_never_a_primary_bearer(
    api_app: FastAPI, tools_app: FastAPI, incident_app: FastAPI, skeys: KeyPair
) -> None:
    h = {"Authorization": "Bearer " + delegated_token_for(skeys, "SRE", investigation_id=uuid4())}
    for app, path in (
        (api_app, "/api/v1/me"),
        (tools_app, "/v1/tools"),
        (incident_app, "/v1/incidents"),
    ):
        async with asgi(app) as c:
            r = await c.get(path, headers=h)
        assert r.status_code == 401, (path, r.text)


async def test_tool_gateway_binds_a_delegated_token_to_its_investigation_and_incident(
    tools_app: FastAPI, skeys: KeyPair, tkeys: KeyPair, planted: Planted, owner: psycopg.Connection
) -> None:
    inv, inc, grant = uuid4(), uuid4(), uuid4()
    delegated = delegated_token_for(
        skeys, "SRE", investigation_id=inv, incident_id=inc, grant_id=grant
    )
    headers = {
        "Authorization": "Bearer " + service_token_for(tkeys, "agents", "tools:invoke"),
        "X-On-Behalf-Of": f"Bearer {delegated}",
    }
    args = {"service_key": planted.svc, "start": "2026-10-02T09:00:00Z",
            "end": "2026-10-02T10:00:00Z", "min_level": "WARN", "limit": 5}  # fmt: skip

    async def invoke(investigation: UUID | None, incident: UUID | None) -> httpx.Response:
        body: dict[str, Any] = {"agent_name": "log_analysis", "args": args}
        if investigation:
            body["investigation_id"] = str(investigation)
        if incident:
            body["incident_id"] = str(incident)
        async with asgi(tools_app) as c:
            return await c.post("/v1/tools/search_logs/invoke", json=body, headers=headers)

    ok = await invoke(inv, inc)
    assert ok.status_code == 200, ok.text
    # wrong / missing investigation, and (review finding) right investigation + other incident
    for bad_inv, bad_inc in ((uuid4(), inc), (None, inc), (inv, uuid4()), (inv, None)):
        r = await invoke(bad_inv, bad_inc)
        assert r.status_code == 403 and "delegation_scope_mismatch" in r.text, (bad_inv, bad_inc)
    row = owner.execute(
        "SELECT event->'details'->>'delegation_grant' FROM tools.audit_outbox "
        "WHERE event->'details'->>'investigation_id' = %s AND event->>'outcome' = 'SUCCESS'",
        (str(inv),),
    ).fetchone()
    assert row is not None and row[0] == str(grant)


async def test_agents_refuse_a_delegated_token_for_another_investigation_or_incident(
    agents_app: FastAPI, skeys: KeyPair, tkeys: KeyPair, incident: dict[str, Any], planted: Planted
) -> None:
    inv = uuid4()
    body = {"incident_id": incident["id"], "investigation_id": str(inv),
            "service_keys": [planted.svc], "start": "2026-10-02T09:00:00Z",
            "end": "2026-10-02T10:00:00Z"}  # fmt: skip
    service = "Bearer " + service_token_for(tkeys, "orchestrator", "agents:run")
    for token in (
        delegated_token_for(
            skeys, "SRE", investigation_id=uuid4(), incident_id=UUID(incident["id"])
        ),
        delegated_token_for(skeys, "SRE", investigation_id=inv, incident_id=uuid4()),
    ):
        async with asgi(agents_app) as c:
            r = await c.post(
                "/v1/agents/log_analysis/run",
                json=body,
                headers={"Authorization": service, "X-On-Behalf-Of": f"Bearer {token}"},
            )
        assert r.status_code == 403 and "investigation/incident" in r.text, r.text
