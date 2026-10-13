# ruff: noqa: F811  - fixtures imported from conftest modules are re-used as parameters below
"""Phase 16 gate (ADR-026), end to end: an investigation PROPOSES a rollback (code rule) ->
ANOTHER incident commander approves -> the action_executor runs it AS THAT IC through the
tool gateway (approval verified + consumed at incident-service, intent recorded first) -> a
new CD record through the insert-only login -> outcome recorded -> incident MITIGATED.
And every way it must NOT run: requester approves, other IC executes, tampered args, replay,
stale target, expiry, reject.

Real Postgres, real services in-process, least-privilege logins (cd_svc = INSERT only on
devdata.deployments). Only the model is fake."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Iterator
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
from aeoi_security.testing import KeyPair, service_token_for
from aeoi_tools.main import build_app as build_tools

from ..tools.conftest import T0, make_stub_rag, tools_settings
from .conftest import OrchFactory, People, Planted, Tokens, _login
from .test_multi_agent_e2e import ALL, incident10  # noqa: F401  (fixture)
from .test_orchestrator_e2e import get, q, start
from .test_reasoning_e2e import LABELS, RANKING, critique, script

pytestmark = pytest.mark.integration
IC = "INCIDENT_COMMANDER"


# ------------------------------------------------------------------ the Phase 16 stack
@pytest.fixture(scope="session")
def cd_db_url(migrated_db: str) -> str:
    return _login(migrated_db, "cd_svc_test", "svc_cd")


@pytest.fixture
def tools_app_tokens(tmp_path: Path, tkeys: KeyPair) -> Path:
    p = tmp_path / "tools_incident_token.txt"
    p.write_text(service_token_for(tkeys, "tool-gateway", "approvals:consume"))
    return p


@pytest.fixture
async def tools_app(  # overrides the Phase 7 fixture: approvals + the CD login wired in
    tools_db_url: str, tpub: Path, catalog_file: Path, audit_token_file: Path, stub_rag: Any,
    audit_app: FastAPI, dpub: Path, incident_app: FastAPI, cd_db_url: str,
    tools_app_tokens: Path,
) -> AsyncIterator[FastAPI]:  # fmt: skip
    app = build_tools(
        tools_settings(
            tools_db_url,
            tpub,
            catalog_file,
            audit_token_file,
            delegation_public_key_file=dpub,
            incident_service_url="http://incident",
            approvals_token_file=tools_app_tokens,
            cd_db_url_override=SecretStr(cd_db_url),
        ),
        rag_transport=httpx.ASGITransport(app=make_stub_rag(stub_rag)),
        audit_transport=httpx.ASGITransport(app=audit_app),
        incident_transport=httpx.ASGITransport(app=incident_app),
    )
    yield app
    await app.state.engine.dispose()


@pytest.fixture
def executor_token_file(tmp_path: Path, tkeys: KeyPair) -> Path:
    p = tmp_path / "action_executor_token.txt"
    p.write_text(service_token_for(tkeys, "action_executor", "tools:invoke"))
    return p


@pytest.fixture
async def agents_app(  # overrides: the executor gets its OWN token file
    tpub: Path, dpub: Path, agents_token_file: Path, executor_token_file: Path,
    tools_app: FastAPI, llm_app: FastAPI,
) -> AsyncIterator[FastAPI]:  # fmt: skip
    app = build_agents(
        AgentSettings(
            jwt_public_key_file=tpub,
            delegation_public_key_file=dpub,
            service_token_file=agents_token_file,
            action_executor_token_file=executor_token_file,
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
def orch_token(tkeys: KeyPair) -> str:  # overrides: + actions:propose, actions:record
    return service_token_for(
        tkeys, "orchestrator", "agents:run", "evidence:write", "hypotheses:write",
        "timeline:write", "postmortems:write", "delegation:create", "actions:propose",
        "actions:record",
    )  # fmt: skip


# ------------------------------------------------------------------ helpers
def _key() -> str:
    return f"DEPLOY-{secrets.randbelow(10**8):08d}"


def plant_pair(owner: psycopg.Connection, svc: str | None = None) -> dict[str, str]:
    """An older good release and the current one (with a flag change) of a fresh service."""
    svc = svc or f"ap-{secrets.token_hex(3)}"
    old, new = _key(), _key()
    old_sha, new_sha = "c" * 34 + secrets.token_hex(3), "d" * 34 + secrets.token_hex(3)
    for key, sha, at, diff in (
        (old, old_sha, T0 - timedelta(days=1), "{}"),
        (
            new,
            new_sha,
            T0 + timedelta(minutes=10),
            '{"feature_flags.fast_path": [false, true], "pool.size": [20, 20]}',
        ),
    ):
        owner.execute(
            "INSERT INTO devdata.deployments (id, deploy_key, service_key, version, environment,"
            " status, commit_sha, deployed_by, config_diff, started_at) VALUES (gen_random_uuid(),"
            " %s, %s, 'v1', 'production', 'SUCCEEDED', %s, 'cd-bot', %s::jsonb, %s)",
            (key, svc, sha, diff, at),
        )
    return {"svc": svc, "old": old, "new": new, "old_sha": old_sha}


def http(app: FastAPI, host: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{host}")


async def new_incident(incident_app: FastAPI, tok: Tokens, svc: str) -> dict[str, Any]:
    async with http(incident_app, "incident") as c:
        r = await c.post("/v1/incidents", json={
            "title": "Errors after deploy", "severity": "SEV2", "affected_services": [svc],
            "detected_at": (T0 + timedelta(minutes=20)).isoformat(),
        }, headers={**tok.h("SRE"), "Idempotency-Key": secrets.token_hex(8)})  # fmt: skip
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


async def request_rollback(incident_app: FastAPI, token: str, incident: dict[str, Any],
                           deploy_key: str) -> dict[str, Any]:  # fmt: skip
    async with http(incident_app, "incident") as c:
        r = await c.post(f"/v1/incidents/{incident['id']}/actions", json={
            "action": "rollback_deployment",
            "args": {"deploy_key": deploy_key, "reason": "errors started with this deploy"},
        }, headers={"Authorization": f"Bearer {token}",
                    "Idempotency-Key": secrets.token_hex(8)})  # fmt: skip
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


async def decide_at_orch(app: FastAPI, token: str, approval_id: str, decision: str = "APPROVED",
                         suffix: str = "decision") -> httpx.Response:  # fmt: skip
    async with http(app, "orch") as c:
        body = {"decision": decision, "reason": "rollback is the fastest safe mitigation"}
        return await c.post(f"/v1/approvals/{approval_id}/{suffix}",
                            json=body if suffix == "decision" else None,
                            headers={"Authorization": f"Bearer {token}"})  # fmt: skip


async def incident_status(incident_app: FastAPI, tok: Tokens, ref: str) -> str:
    async with http(incident_app, "incident") as c:
        r = await c.get(f"/v1/incidents/{ref}", headers=tok.h("SRE"))
    return str(r.json()["status"])


async def gateway_call(tools_app: FastAPI, tkeys: KeyPair, user_token: str, approval: dict[str, Any],
                       args: dict[str, Any] | None = None,
                       service: str = "action_executor") -> httpx.Response:  # fmt: skip
    svc = service_token_for(tkeys, service, "tools:invoke")
    async with http(tools_app, "tools") as c:
        return await c.post("/v1/tools/rollback_deployment/invoke", json={
            "args": args or approval["action_args"], "agent_name": "action_executor",
            "incident_id": approval["incident_id"], "approval_id": approval["id"],
        }, headers={"Authorization": f"Bearer {svc}",
                    "X-On-Behalf-Of": f"Bearer {user_token}"})  # fmt: skip


@pytest.fixture
def cleanup(owner: psycopg.Connection) -> Iterator[list[str]]:
    """Service keys whose devdata rows this test added (the planted service is shared)."""
    services: list[str] = []
    yield services
    for svc in services:
        owner.execute("DELETE FROM devdata.deployments WHERE service_key = %s AND "
                      "(deployed_by LIKE 'approval:%%' OR service_key LIKE 'ap-%%')", (svc,))  # fmt: skip


# ------------------------------------------------------------------ the gate
async def test_investigation_proposes_another_ic_approves_and_the_rollback_runs(
    make_orch: OrchFactory, people: People, incident10: dict[str, Any], planted: Planted,
    owner: psycopg.Connection, incident_app: FastAPI, llm_app: FastAPI, tok: Tokens,
    tools_app: FastAPI, tkeys: KeyPair, cleanup: list[str],
) -> None:  # fmt: skip
    # an earlier good release of the planted service (so there is something to go back to)
    old_key, old_sha = _key(), "e" * 34 + secrets.token_hex(3)
    owner.execute(
        "INSERT INTO devdata.deployments (id, deploy_key, service_key, version, environment,"
        " status, commit_sha, deployed_by, config_diff, started_at) VALUES (gen_random_uuid(),"
        " %s, %s, 'v0', 'production', 'SUCCEEDED', %s, 'cd-bot', '{}', %s)",
        (old_key, planted.svc, old_sha, T0 - timedelta(days=2)),
    )
    cleanup.append(planted.svc)
    try:
        script(llm_app, LABELS, RANKING, critic=critique())
        starter = people.add(IC)  # an IC starts it: the request is theirs, so not their call
        s_token = people.token(starter, IC)
        async with make_orch(agents=ALL, reasoning=True) as app:
            inv = (await start(app, s_token, incident10, key="key-p16-e2e-001")).json()[
                "investigation_id"]  # fmt: skip
            await app.state.engine.wait(UUID(inv))
            out = await get(app, s_token, inv)
            prop = out["plan"]["proposal"]
            assert prop["proposed"], prop
            assert prop["deploy_key"] == planted.deploy_key
            aid = prop["approval_id"]
            assert await incident_status(incident_app, tok, incident10["key"]) == "AWAITING_REVIEW"
            # the requester can never approve their own request (separation of duties)
            r = await decide_at_orch(app, s_token, aid)
            assert r.status_code == 403, r.text
            # an ENGINEER/SRE cannot decide at all
            r = await decide_at_orch(app, tok.user("SRE"), aid)
            assert r.status_code == 403
            approver = uuid4()
            a_token = tok.user(IC, user_id=approver)
            r = await decide_at_orch(app, a_token, aid)
            assert r.status_code == 200, r.text
            res = r.json()
            assert res["executed"], res
            item = res["result"]["items"][0]
            assert item["rolled_back_from"] == planted.deploy_key
            assert item["rolled_back_to"] == old_sha
            assert "feature_flags.order_batching" in item["config_restored"]
            assert res["approval"]["status"] == "EXECUTED"
            # retry after success: refused before anything is called again
            r = await decide_at_orch(app, a_token, aid, suffix="execute")
            assert r.status_code == 409
            # a second decision: 409 (decided once)
            r = await decide_at_orch(app, tok.user(IC), aid, decision="REJECTED")
            assert r.status_code == 409
        # the effect: ONE new CD record, by the approval, of the previous commit
        rows = q(owner, "SELECT deploy_key, commit_sha, deployed_by, config_diff FROM "
                 "devdata.deployments WHERE deployed_by = %s", f"approval:{aid}")  # fmt: skip
        assert len(rows) == 1 and rows[0][1] == old_sha
        assert rows[0][3]["feature_flags.order_batching"] == [True, False]
        # the record: intent first, then exactly one OK, both AS THE APPROVER
        calls = q(owner, "SELECT status, on_behalf_of::text, agent_name FROM tools.tool_calls WHERE "
                  "approval_id = %s ORDER BY created_at", aid)  # fmt: skip
        assert [c[0] for c in calls] == ["PENDING", "OK"], calls
        assert {c[1] for c in calls} == {str(approver)}
        assert {c[2] for c in calls} == {"action_executor"}
        a = q(owner, "SELECT requested_by, decided_by::text, outcome, source, consumed_at "
              "IS NOT NULL FROM incident.approvals WHERE id = %s", aid)[0]  # fmt: skip
        assert a == (f"user:{starter}", str(approver), "EXECUTED", "investigation", True)
        assert await incident_status(incident_app, tok, incident10["key"]) == "MITIGATED"
        events = {e[0] for e in q(owner, "SELECT event_type FROM incident.incident_events "
                                         "WHERE incident_id = %s", incident10["id"])}  # fmt: skip
        assert {"HumanReviewRequired", "HumanApproved", "ActionExecuted"} <= events
        # replay straight at the gateway, as the approver: the approval is used
        appr = {"id": aid, "incident_id": incident10["id"],
                "action_args": q(owner, "SELECT action_args FROM incident.approvals WHERE id=%s",
                                 aid)[0][0]}  # fmt: skip
        r = await gateway_call(tools_app, tkeys, a_token, appr)
        assert r.status_code == 409 and r.json()["reason"] == "approval_already_executed", r.text
        assert len(q(owner, "SELECT 1 FROM devdata.deployments WHERE deployed_by = %s",
                     f"approval:{aid}")) == 1  # fmt: skip
    finally:
        owner.execute("DELETE FROM devdata.deployments WHERE deploy_key = %s", (old_key,))


async def test_reject_records_the_reason_and_nothing_runs(
    make_orch: OrchFactory, owner: psycopg.Connection, incident_app: FastAPI, tok: Tokens,
    cleanup: list[str],
) -> None:  # fmt: skip
    pair = plant_pair(owner)
    cleanup.append(pair["svc"])
    inc = await new_incident(incident_app, tok, pair["svc"])
    a = await request_rollback(incident_app, tok.user("SRE"), inc, pair["new"])
    assert a["status"] == "PENDING" and a["source"] == "manual"
    assert await incident_status(incident_app, tok, inc["key"]) == "AWAITING_REVIEW"
    async with make_orch() as app:
        r = await decide_at_orch(app, tok.user(IC), a["id"], decision="REJECTED")
        assert r.status_code == 200, r.text
        assert r.json()["approval"]["status"] == "REJECTED" and not r.json()["executed"]
        r = await decide_at_orch(app, tok.user(IC), a["id"], suffix="execute")
        assert r.status_code == 409
    assert q(owner, "SELECT count(*) FROM tools.tool_calls WHERE approval_id = %s",
             a["id"])[0][0] == 0  # fmt: skip
    assert q(owner, "SELECT count(*) FROM devdata.deployments WHERE service_key = %s",
             pair["svc"])[0][0] == 2  # fmt: skip
    assert await incident_status(incident_app, tok, inc["key"]) == "INVESTIGATING"


async def test_the_gateway_runs_only_this_approval_by_this_approver_with_these_args(
    owner: psycopg.Connection, incident_app: FastAPI, tools_app: FastAPI, tok: Tokens,
    tkeys: KeyPair, cleanup: list[str],
) -> None:  # fmt: skip
    pair = plant_pair(owner)
    cleanup.append(pair["svc"])
    inc = await new_incident(incident_app, tok, pair["svc"])
    a = await request_rollback(incident_app, tok.user("SRE"), inc, pair["new"])
    # not yet approved: refused, nothing recorded as used
    approver = uuid4()
    a_token = tok.user(IC, user_id=approver)
    r = await gateway_call(tools_app, tkeys, a_token, a)
    assert r.status_code == 409 and r.json()["reason"] == "approval_unusable", r.text
    async with http(incident_app, "incident") as c:
        r = await c.post(f"/v1/approvals/{a['id']}/decision", json={
            "decision": "APPROVED", "reason": "deploy correlates with the errors"},
            headers={"Authorization": f"Bearer {a_token}"})  # fmt: skip
    assert r.status_code == 200, r.text
    # another IC: refused (approval is personal)
    r = await gateway_call(tools_app, tkeys, tok.user(IC), a)
    assert r.status_code == 403 and r.json()["reason"] == "approval_invalid", r.text
    # tampered arguments (one character): a different action
    bad = {**a["action_args"], "reason": a["action_args"]["reason"] + "!"}
    r = await gateway_call(tools_app, tkeys, a_token, a, args=bad)
    assert r.status_code == 403 and r.json()["reason"] == "approval_invalid", r.text
    # the investigation workers' identity can never claim the executor (agents.yaml)
    r = await gateway_call(tools_app, tkeys, a_token, a, service="agents")
    assert r.status_code == 403, r.text
    assert (
        q(owner, "SELECT consumed_at FROM incident.approvals WHERE id = %s", a["id"])[0][0] is None
    )  # fmt: skip   - none of the refusals used it up
    # the approver, exact args: runs once
    r = await gateway_call(tools_app, tkeys, a_token, a)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["items"][0]["rolled_back_to"] == pair["old_sha"]
    # replay (outcome not recorded yet): never twice - a distinct answer, not a failure
    r = await gateway_call(tools_app, tkeys, a_token, a)
    assert r.status_code == 409 and r.json()["reason"] == "approval_already_executed", r.text
    assert len(q(owner, "SELECT 1 FROM devdata.deployments WHERE deployed_by = %s",
                 f"approval:{a['id']}")) == 1  # fmt: skip


async def test_a_newer_deploy_makes_the_approved_rollback_fail_safely(
    make_orch: OrchFactory, owner: psycopg.Connection, incident_app: FastAPI, tok: Tokens,
    cleanup: list[str],
) -> None:  # fmt: skip
    pair = plant_pair(owner)
    cleanup.append(pair["svc"])
    inc = await new_incident(incident_app, tok, pair["svc"])
    a = await request_rollback(incident_app, tok.user("SRE"), inc, pair["new"])
    # someone ships a fix while the IC reads the request
    owner.execute(
        "INSERT INTO devdata.deployments (id, deploy_key, service_key, version, environment,"
        " status, commit_sha, deployed_by, config_diff, started_at) VALUES (gen_random_uuid(),"
        " %s, %s, 'v2', 'production', 'SUCCEEDED', %s, 'cd-bot', '{}', %s)",
        (_key(), pair["svc"], "f" * 40, T0 + timedelta(minutes=40)),
    )
    async with make_orch() as app:
        r = await decide_at_orch(app, tok.user(IC), a["id"])
    assert r.status_code == 200, r.text
    res = r.json()
    assert not res["executed"] and "is newer" in res["execution_error"], res
    assert res["approval"]["status"] == "FAILED"
    assert q(owner, "SELECT count(*) FROM devdata.deployments WHERE deployed_by = %s",
             f"approval:{a['id']}")[0][0] == 0  # fmt: skip
    # a failed action mitigates nothing, and leaves nothing to review: back to INVESTIGATING
    assert await incident_status(incident_app, tok, inc["key"]) == "INVESTIGATING"


async def test_an_expired_request_is_recorded_expired_and_cannot_be_approved(
    make_orch: OrchFactory, owner: psycopg.Connection, incident_app: FastAPI, tok: Tokens,
    cleanup: list[str],
) -> None:  # fmt: skip
    pair = plant_pair(owner)
    cleanup.append(pair["svc"])
    inc = await new_incident(incident_app, tok, pair["svc"])
    a = await request_rollback(incident_app, tok.user("SRE"), inc, pair["new"])
    owner.execute("UPDATE incident.approvals SET requested_at = now() - interval '31 minutes', "
                  "expires_at = now() - interval '1 minute' WHERE id = %s", (a["id"],))  # fmt: skip
    async with make_orch() as app:
        r = await decide_at_orch(app, tok.user(IC), a["id"])
    assert r.status_code == 409, r.text
    row = q(owner, "SELECT decision, decided_by FROM incident.approvals WHERE id = %s", a["id"])
    assert row == [("EXPIRED", None)]  # the expiry is RECORDED, not only answered
    ev = q(owner, "SELECT event_type, actor FROM incident.incident_events WHERE incident_id = %s "
           "ORDER BY occurred_at DESC LIMIT 1", inc["id"])  # fmt: skip
    assert ev == [("ActionExpired", "service:incident-service")]  # not "HumanRejected"
    assert await incident_status(incident_app, tok, inc["key"]) == "INVESTIGATING"


async def test_cd_login_can_only_insert(cd_db_url: str, owner: psycopg.Connection) -> None:
    """The simulated CD system's login: INSERT on devdata.deployments, nothing else."""
    dsn = cd_db_url.replace("postgresql+psycopg://", "postgresql://").replace(
        "postgresql+asyncpg://", "postgresql://")  # fmt: skip
    with psycopg.connect(dsn, autocommit=True) as c:
        for sql in ("SELECT 1 FROM devdata.deployments LIMIT 1",
                    "UPDATE devdata.deployments SET version = 'x' WHERE false",
                    "DELETE FROM devdata.deployments WHERE false",
                    "SELECT 1 FROM incident.approvals LIMIT 1"):  # fmt: skip
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(sql)


async def test_a_proposal_is_bound_to_the_delegated_token_and_the_orchestrator(
    owner: psycopg.Connection, incident_app: FastAPI, tok: Tokens, skeys: KeyPair,
    tkeys: KeyPair, cleanup: list[str],
) -> None:  # fmt: skip
    """The proposals route: only the orchestrator (`actions:propose`), only with a delegated
    token bound to THIS incident and THIS investigation, only for a user who may request."""
    from aeoi_security.testing import delegated_token_for

    pair = plant_pair(owner)
    cleanup.append(pair["svc"])
    inc = await new_incident(incident_app, tok, pair["svc"])
    other = await new_incident(incident_app, tok, pair["svc"])
    inv = uuid4()
    body = {"action": "rollback_deployment", "investigation_id": str(inv),
            "args": {"deploy_key": pair["new"], "reason": "proposed by an investigation"}}  # fmt: skip
    orch = service_token_for(tkeys, "orchestrator", "actions:propose")

    async def post(service: str, obo: str, ref: str = inc["id"]) -> httpx.Response:
        async with http(incident_app, "incident") as c:
            return await c.post(f"/v1/incidents/{ref}/actions/proposals", json=body,
                                headers={"Authorization": f"Bearer {service}",
                                         "X-On-Behalf-Of": f"Bearer {obo}"})  # fmt: skip

    good = delegated_token_for(skeys, "SRE", investigation_id=inv, incident_id=UUID(inc["id"]))
    wrong_inc = delegated_token_for(skeys, "SRE", investigation_id=inv,
                                    incident_id=UUID(other["id"]))  # fmt: skip
    wrong_inv = delegated_token_for(skeys, "SRE", investigation_id=uuid4(),
                                    incident_id=UUID(inc["id"]))  # fmt: skip
    engineer = delegated_token_for(skeys, "ENGINEER", investigation_id=inv,
                                   incident_id=UUID(inc["id"]))  # fmt: skip
    no_scope = service_token_for(tkeys, "orchestrator", "evidence:write")
    # M3: a USER bearer (an SRE or IC by hand) can never store an "investigation" proposal
    assert (await post(tok.user("SRE"), good)).status_code == 403
    assert (await post(tok.user(IC), tok.user("SRE"))).status_code == 403
    # ... nor can the orchestrator without the investigation's delegated token
    assert (await post(orch, tok.user("SRE"))).status_code == 403
    assert (await post(orch, wrong_inc)).status_code == 403  # token of another incident
    assert (await post(orch, good, other["id"])).status_code == 403  # route of another one
    assert (await post(orch, wrong_inv)).status_code == 403
    assert (await post(orch, engineer)).status_code == 403  # may not request actions
    assert (await post(no_scope, good)).status_code == 403
    assert (
        q(
            owner,
            "SELECT count(*) FROM incident.approvals WHERE incident_id IN (%s, %s)",
            inc["id"],
            other["id"],
        )[0][0]
        == 0
    )  # fmt: skip   - nothing recorded by a refusal
    r = await post(orch, good)
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "investigation" and r.json()["requested_by"].startswith("user:")
    # idempotent per (investigation, args): same answer, one row - also after a decision
    async with http(incident_app, "incident") as c:
        d = await c.post(f"/v1/approvals/{r.json()['id']}/decision", headers=tok.h(IC),
                         json={"decision": "REJECTED", "reason": "not the cause, we checked"})  # fmt: skip
    assert d.status_code == 200, d.text
    again = await post(orch, good)
    assert again.status_code == 200 and again.json()["id"] == r.json()["id"]
    assert again.json()["status"] == "REJECTED"  # a resumed graph never reopens it


# ------------------------------------------------------------------ review findings (H/M/L)
async def approved(incident_app: FastAPI, tok: Tokens, owner: psycopg.Connection,
                   cleanup: list[str]) -> tuple[dict[str, Any], dict[str, Any], str, dict[str, Any]]:  # fmt: skip
    """A fresh service + incident + SRE request, approved at incident-service by a new IC
    (nothing executed). -> (pair, approval, approver token, incident)."""
    pair = plant_pair(owner)
    cleanup.append(pair["svc"])
    inc = await new_incident(incident_app, tok, pair["svc"])
    a = await request_rollback(incident_app, tok.user("SRE"), inc, pair["new"])
    a_token = tok.user(IC, user_id=uuid4())
    async with http(incident_app, "incident") as c:
        r = await c.post(f"/v1/approvals/{a['id']}/decision", json={
            "decision": "APPROVED", "reason": "deploy correlates with the errors"},
            headers={"Authorization": f"Bearer {a_token}"})  # fmt: skip
    assert r.status_code == 200, r.text
    return pair, a, a_token, inc


async def test_the_approver_cannot_run_or_consume_it_by_hand(
    owner: psycopg.Connection, incident_app: FastAPI, tools_app: FastAPI, tok: Tokens,
    cleanup: list[str],
) -> None:  # fmt: skip
    """H1: manual mode (user token, no agent) consumed the approval and ran the rollback with
    nothing recording the outcome. H3: an IC could POST /consume with their own bearer."""
    _, a, a_token, _ = await approved(incident_app, tok, owner, cleanup)
    async with http(tools_app, "tools") as c:
        r = await c.post("/v1/tools/rollback_deployment/invoke", json={
            "args": a["action_args"], "incident_id": a["incident_id"], "approval_id": a["id"]},
            headers={"Authorization": f"Bearer {a_token}"})  # fmt: skip
    assert r.status_code == 403 and r.json()["reason"] == "executor_only", r.text
    async with http(incident_app, "incident") as c:
        r = await c.post(f"/v1/approvals/{a['id']}/consume", json={
            "action": "rollback_deployment", "args_sha256": a["args_sha256"],
            "incident_id": a["incident_id"]}, headers={"Authorization": f"Bearer {a_token}"})  # fmt: skip
    assert r.status_code == 403, r.text
    assert q(owner, "SELECT consumed_at FROM incident.approvals WHERE id = %s",
             a["id"])[0][0] is None  # fmt: skip


async def test_two_executions_at_once_run_it_once_and_the_retry_records_executed(
    make_orch: OrchFactory, owner: psycopg.Connection, incident_app: FastAPI,
    tools_app: FastAPI, tok: Tokens, tkeys: KeyPair, cleanup: list[str],
) -> None:  # fmt: skip
    """H2/M4: a double click. One runs; the other is `in progress` or `already executed` -
    never a failure. The orchestrator's retry then records EXECUTED (not FAILED)."""
    import asyncio

    _, a, a_token, inc = await approved(incident_app, tok, owner, cleanup)
    r1, r2 = await asyncio.gather(gateway_call(tools_app, tkeys, a_token, a),
                                  gateway_call(tools_app, tkeys, a_token, a))  # fmt: skip
    codes = sorted([r1.status_code, r2.status_code])
    assert codes == [200, 409], (r1.text, r2.text)
    loser = r1 if r1.status_code == 409 else r2
    assert loser.json()["reason"] in ("approval_in_progress", "approval_already_executed")
    assert q(owner, "SELECT count(*) FROM devdata.deployments WHERE deployed_by = %s",
             f"approval:{a['id']}")[0][0] == 1  # fmt: skip
    assert q(owner, "SELECT count(*) FROM tools.tool_calls WHERE approval_id = %s AND "
             "status = 'OK'", a["id"])[0][0] == 1  # fmt: skip
    async with make_orch() as app:  # the outcome was never recorded: retry
        r = await decide_at_orch(app, a_token, a["id"], suffix="execute")
    assert r.status_code == 200, r.text
    assert r.json()["approval"]["status"] == "EXECUTED", r.json()
    assert await incident_status(incident_app, tok, inc["key"]) == "MITIGATED"


async def test_an_expired_approval_never_acts_but_a_finished_one_is_still_reported(
    owner: psycopg.Connection, incident_app: FastAPI, tools_app: FastAPI, tok: Tokens,
    tkeys: KeyPair, cleanup: list[str],
) -> None:  # fmt: skip
    """H3: the 'resumed' path ignored expiry - a consumed, unfinished approval could act days
    later. After expiry: nothing runs; if it DID run, the gateway still says so."""
    expire = ("UPDATE incident.approvals SET requested_at = now() - interval '2 hours', "
              "expires_at = now() - interval '1 minute' WHERE id = %s")  # fmt: skip
    # consumed, never ran (the process died before the intent), then expired
    _, a, a_token, _ = await approved(incident_app, tok, owner, cleanup)
    owner.execute("UPDATE incident.approvals SET consumed_at = now() - interval '1 hour' "
                  "WHERE id = %s", (a["id"],))  # fmt: skip
    owner.execute(expire, (a["id"],))
    r = await gateway_call(tools_app, tkeys, a_token, a)
    assert r.status_code == 409 and r.json()["reason"] == "approval_unusable", r.text
    assert q(owner, "SELECT count(*) FROM devdata.deployments WHERE deployed_by = %s",
             f"approval:{a['id']}")[0][0] == 0  # fmt: skip
    # ran, outcome not recorded, then expired: "already executed", by the same approver only
    _, b, b_token, _ = await approved(incident_app, tok, owner, cleanup)
    assert (await gateway_call(tools_app, tkeys, b_token, b)).status_code == 200
    owner.execute(expire, (b["id"],))
    r = await gateway_call(tools_app, tkeys, b_token, b)
    assert r.status_code == 409 and r.json()["reason"] == "approval_already_executed", r.text
    r = await gateway_call(tools_app, tkeys, tok.user(IC), b)  # another IC learns nothing
    assert r.json()["reason"] != "approval_already_executed", r.text


async def test_a_taken_deploy_key_is_refused_never_reported_as_ours(
    owner: psycopg.Connection, incident_app: FastAPI, tools_app: FastAPI, tok: Tokens,
    tkeys: KeyPair, cleanup: list[str],
) -> None:  # fmt: skip
    """M1: a key collision returned another record as this approval's rollback (EXECUTED,
    MITIGATED, nothing deployed)."""
    from aeoi_tools.adapters.deployer import rollback_key

    _, a, a_token, _ = await approved(incident_app, tok, owner, cleanup)
    other = f"ap-{secrets.token_hex(3)}"
    cleanup.append(other)
    owner.execute(
        "INSERT INTO devdata.deployments (id, deploy_key, service_key, version, environment,"
        " status, commit_sha, deployed_by, config_diff, started_at) VALUES (gen_random_uuid(),"
        " %s, %s, 'x', 'production', 'SUCCEEDED', %s, 'cd-bot', '{}', now())",
        (rollback_key(UUID(a["id"])), other, "a" * 40),
    )
    r = await gateway_call(tools_app, tkeys, a_token, a)
    assert r.status_code == 409 and "taken by another record" in r.json()["detail"], r.text


async def test_requests_are_refused_for_a_target_already_asked_and_on_a_closed_incident(
    owner: psycopg.Connection, incident_app: FastAPI, tok: Tokens, cleanup: list[str],
) -> None:  # fmt: skip
    """M2: a new reason = a new hash = a second live request for the same rollback. M5: a
    rollback on a RESOLVED incident would change production behind a closed record."""
    pair = plant_pair(owner)
    cleanup.append(pair["svc"])
    inc = await new_incident(incident_app, tok, pair["svc"])
    first = await request_rollback(incident_app, tok.user("SRE"), inc, pair["new"])
    async with http(incident_app, "incident") as c:
        r = await c.post(f"/v1/incidents/{inc['id']}/actions", json={
            "action": "rollback_deployment",
            "args": {"deploy_key": pair["new"], "reason": "a different reason, same rollback"},
        }, headers={**tok.h("SRE"), "Idempotency-Key": secrets.token_hex(8)})  # fmt: skip
    assert r.status_code == 409 and first["id"] in r.text, r.text
    owner.execute("UPDATE incident.incidents SET status = 'RESOLVED', resolved_at = now() "
                  "WHERE id = %s", (inc["id"],))  # fmt: skip
    async with http(incident_app, "incident") as c:
        r = await c.post(f"/v1/approvals/{first['id']}/decision", headers=tok.h(IC),
                         json={"decision": "APPROVED", "reason": "late approval of a resolved one"})  # fmt: skip
    assert r.status_code == 409, r.text
    inc2 = await new_incident(incident_app, tok, pair["svc"])
    owner.execute("UPDATE incident.incidents SET status = 'CLOSED', resolved_at = now() "
                  "WHERE id = %s", (inc2["id"],))  # fmt: skip
    async with http(incident_app, "incident") as c:
        r = await c.post(f"/v1/incidents/{inc2['id']}/actions", json={
            "action": "rollback_deployment",
            "args": {"deploy_key": pair["new"], "reason": "rollback on a closed incident"},
        }, headers={**tok.h("SRE"), "Idempotency-Key": secrets.token_hex(8)})  # fmt: skip
    assert r.status_code == 409, r.text


async def test_a_reader_without_code_read_sees_no_commit_of_the_rollback(
    make_orch: OrchFactory, owner: psycopg.Connection, incident_app: FastAPI, tok: Tokens,
    cleanup: list[str],
) -> None:  # fmt: skip
    """L1: outcome_detail carried the commit sha to every incident reader (MANAGER)."""
    pair, a, a_token, inc = await approved(incident_app, tok, owner, cleanup)
    async with make_orch() as app:
        r = await decide_at_orch(app, a_token, a["id"], suffix="execute")
    assert r.status_code == 200 and r.json()["executed"], r.text
    async with http(incident_app, "incident") as c:
        mgr = (await c.get(f"/v1/incidents/{inc['id']}/approvals", headers=tok.h("MANAGER"))).json()
        sre = (await c.get(f"/v1/incidents/{inc['id']}/approvals", headers=tok.h("SRE"))).json()
    assert pair["old_sha"] not in str(mgr) and "redacted" in str(mgr[0]["outcome_detail"])
    assert pair["old_sha"] in str(sre)


async def test_a_running_attempt_is_not_a_failure_and_is_not_run_again(
    make_orch: OrchFactory, owner: psycopg.Connection, incident_app: FastAPI, tok: Tokens,
    cleanup: list[str],
) -> None:  # fmt: skip
    """H2: while one attempt runs (fresh intent, nothing finished), a retry must neither run
    a second time nor be recorded as FAILED - the result is unknown, not failed."""
    _, a, a_token, inc = await approved(incident_app, tok, owner, cleanup)
    owner.execute("UPDATE incident.approvals SET consumed_at = now() WHERE id = %s", (a["id"],))
    owner.execute(
        "INSERT INTO tools.tool_calls (id, on_behalf_of, tool_name, side_effect, input, status,"
        " approval_id, incident_id, agent_name) VALUES (gen_random_uuid(), %s, "
        "'rollback_deployment', 'CONSEQUENTIAL', '{}', 'PENDING', %s, %s, 'action_executor')",
        (
            q(owner, "SELECT decided_by FROM incident.approvals WHERE id=%s", a["id"])[0][0],
            a["id"],
            inc["id"],
        ),
    )
    async with make_orch() as app:
        r = await decide_at_orch(app, a_token, a["id"], suffix="execute")
    assert r.status_code == 200, r.text
    res = r.json()
    assert not res["executed"] and "approval_in_progress" in res["execution_error"], res
    assert res["approval"]["status"] == "APPROVED"  # nothing recorded: unknown, not FAILED
    assert q(owner, "SELECT outcome FROM incident.approvals WHERE id=%s", a["id"])[0][0] is None
    assert q(owner, "SELECT count(*) FROM devdata.deployments WHERE deployed_by = %s",
             f"approval:{a['id']}")[0][0] == 0  # fmt: skip


# ------------------------------------------------------------------ the inbox (Phase 17b)
async def test_the_inbox_lists_pending_across_incidents_soonest_expiry_first(
    make_orch: OrchFactory, owner: psycopg.Connection, incident_app: FastAPI, tok: Tokens,
    cleanup: list[str],
) -> None:  # fmt: skip
    made = []
    for _ in range(4):  # four incidents, one request each
        pair = plant_pair(owner)
        cleanup.append(pair["svc"])
        inc = await new_incident(incident_app, tok, pair["svc"])
        made.append((inc, await request_rollback(incident_app, tok.user("SRE"), inc, pair["new"])))
    (i_late, late), (i_soon, soon), (_, expired), (_, rejected) = made
    owner.execute("UPDATE incident.approvals SET expires_at = now() + interval '5 minutes', "
                  "recommendation = '{\"way_back\": \"redeploy abc123\"}'::jsonb WHERE id = %s",
                  (soon["id"],))  # fmt: skip
    owner.execute("UPDATE incident.approvals SET requested_at = now() - interval '31 minutes', "
                  "expires_at = now() - interval '1 minute' WHERE id = %s", (expired["id"],))  # fmt: skip
    async with make_orch() as app:
        assert (
            await decide_at_orch(app, tok.user(IC), rejected["id"], "REJECTED")
        ).status_code == 200

    async with http(incident_app, "incident") as c:
        r = await c.get("/v1/approvals?status=PENDING", headers=tok.h("SRE"))
        mgr = await c.get("/v1/approvals", headers=tok.h("MANAGER"))
        bad = [await c.get(f"/v1/approvals?{qs}", headers=tok.h("SRE"))
               for qs in ("status=APPROVED", "limit=0", "limit=101")]  # fmt: skip
        anon = await c.get("/v1/approvals")
    assert r.status_code == 200, r.text
    mine = [
        a for a in r.json() if a["id"] in {soon["id"], late["id"], expired["id"], rejected["id"]}
    ]
    # only the two still pending (expired and rejected are not), the one about to lapse first
    assert [(a["id"], a["status"], a["incident_key"]) for a in mine] == [
        (soon["id"], "PENDING", i_soon["key"]),
        (late["id"], "PENDING", i_late["key"]),
    ]
    assert (
        next(a for a in mine if a["id"] == soon["id"])["recommendation"]["way_back"]
        == "redeploy abc123"
    )
    # the same redaction as the per-incident list: no code:read, no commit-quoting way back
    m = next(a for a in mgr.json() if a["id"] == soon["id"])
    assert m["recommendation"]["way_back"] == "[redacted: needs code:read]"
    assert [b.status_code for b in bad] == [422, 422, 422]
    assert anon.status_code == 401
