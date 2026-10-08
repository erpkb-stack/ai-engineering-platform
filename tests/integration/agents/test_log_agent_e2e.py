"""Agent worker + evidence endpoint boundaries (Phase 8). The full investigation path
(orchestrator service, graph, delegation) is in test_orchestrator_e2e.py."""

from __future__ import annotations

import hashlib
from typing import Any

import httpx
import psycopg
import pytest
from fastapi import FastAPI

from aeoi_security.testing import KeyPair, service_token_for

from .conftest import Planted, Tokens

pytestmark = pytest.mark.integration


def q(owner: psycopg.Connection, sql: str, *args: Any) -> list[tuple[Any, ...]]:
    return owner.execute(sql, args).fetchall()


async def test_user_without_logs_permission_fails_honestly(
    agents_app: FastAPI,
    tkeys: KeyPair,
    tok: Tokens,
    incident: dict[str, Any],
    planted: Planted,
    owner: psycopg.Connection,
) -> None:
    """MANAGER can read incidents but not logs: called directly (the orchestrator would refuse
    a MANAGER earlier - no investigations:run), the agent must FAIL with the reason, not
    produce facts from nothing - and the denial is in the tool-gateway record."""
    body = {
        "incident_id": incident["id"],
        "service_keys": [planted.svc],
        "start": "2026-10-02T09:00:00Z",
        "end": "2026-10-02T10:00:00Z",
    }
    headers = {
        "Authorization": "Bearer " + service_token_for(tkeys, "orchestrator", "agents:run"),
        "X-On-Behalf-Of": "Bearer " + tok.user("MANAGER"),
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=agents_app), base_url="http://agents"
    ) as c:
        r = await c.post("/v1/agents/log_analysis/run", json=body, headers=headers)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "FAILED" and "missing_permission" in (out["error"] or "")
    assert not out["facts"]
    denied = q(
        owner,
        "SELECT count(*) FROM tools.tool_calls WHERE incident_id=%s AND "
        "status='DENIED' AND agent_name='log_analysis'",
        incident["id"],
    )
    assert denied[0][0] == 1


@pytest.mark.parametrize(
    ("make_headers", "status"),
    [
        (lambda t, k: t.h("SRE"), 403),  # a user can't call a worker directly
        (
            lambda t, k: {
                "Authorization": "Bearer " + service_token_for(k, "orchestrator", "agents:run")
            },
            403,
        ),
        (
            lambda t, k: {
                "Authorization": "Bearer " + service_token_for(k, "x", "llm:invoke"),
                "X-On-Behalf-Of": "Bearer " + t.user("SRE"),
            },
            403,
        ),
        (
            lambda t, k: {
                "Authorization": "Bearer " + service_token_for(k, "orchestrator", "agents:run"),
                "X-On-Behalf-Of": "Bearer " + service_token_for(k, "o", "agents:run"),
            },
            403,
        ),
    ],
    ids=["user-token", "no-obo", "wrong-scope", "obo-is-service"],
)
async def test_agent_endpoint_refuses_bad_callers(
    agents_app: FastAPI,
    tok: Tokens,
    tkeys: KeyPair,
    make_headers: Any,
    status: int,
    incident: dict[str, Any],
    planted: Planted,
) -> None:
    body = {
        "incident_id": incident["id"],
        "service_keys": [planted.svc],
        "start": "2026-10-02T09:00:00Z",
        "end": "2026-10-02T10:00:00Z",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=agents_app), base_url="http://agents"
    ) as c:
        r = await c.post("/v1/agents/log_analysis/run", json=body, headers=make_headers(tok, tkeys))
    assert r.status_code == status, r.text


async def test_evidence_write_is_service_only(
    incident_app: FastAPI, tok: Tokens, tkeys: KeyPair, incident: dict[str, Any]
) -> None:
    item = {
        "evidence_key": "LOG-abc-0",
        "kind": "LOG",
        "source_system": "x",
        "title": "t",
        "excerpt": "e",
        "content_sha256": hashlib.sha256(b"e").hexdigest(),
    }
    body = {"agent": "log_analysis", "items": [item], "summary": "s"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=incident_app), base_url="http://incident"
    ) as c:
        user = await c.post(
            f"/v1/incidents/{incident['id']}/evidence", json=body, headers=tok.h("ADMIN")
        )
        wrong = await c.post(
            f"/v1/incidents/{incident['id']}/evidence",
            json=body,
            headers={"Authorization": "Bearer " + service_token_for(tkeys, "x", "agents:run")},
        )
        bad_kind = await c.post(
            f"/v1/incidents/{incident['id']}/evidence",
            json={**body, "items": [{**item, "evidence_key": "METRIC-abc-0"}]},
            headers={"Authorization": "Bearer " + service_token_for(tkeys, "o", "evidence:write")},
        )
        forged = await c.post(
            f"/v1/incidents/{incident['id']}/evidence",
            json={**body, "items": [{**item, "content_sha256": "0" * 64}]},
            headers={"Authorization": "Bearer " + service_token_for(tkeys, "o", "evidence:write")},
        )
    assert user.status_code == 403 and wrong.status_code == 403
    assert bad_kind.status_code == 422 and forged.status_code == 422
