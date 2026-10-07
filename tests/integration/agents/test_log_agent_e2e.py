"""Phase 8 gate, end to end: incident -> runner -> agent -> tool-gateway -> llm-gateway ->
trace rows + evidence rows. Real Postgres, least-privilege logins, fake model."""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi import FastAPI

from aeoi_orchestrator.runner import Runner, RunnerError
from aeoi_security.testing import KeyPair, service_token_for

from .conftest import Planted, Tokens

pytestmark = pytest.mark.integration


def q(owner: psycopg.Connection, sql: str, *args: Any) -> list[tuple[Any, ...]]:
    return owner.execute(sql, args).fetchall()


async def test_log_agent_end_to_end_records_trace_and_evidence(
    runner: Runner,
    incident: dict[str, Any],
    tok: Tokens,
    owner: psycopg.Connection,
    planted: Planted,
) -> None:
    s = await runner.run_log_analysis(incident["key"], tok.user("SRE"))
    assert s.status == "COMPLETE", s.error
    r = s.result
    assert r is not None and r.facts and r.clusters
    assert any("E_POOL" in f.statement and "20" in f.statement for f in r.facts)

    inv = q(
        owner,
        "SELECT status, spent_usd, plan FROM orchestrator.investigations WHERE id=%s",
        s.investigation_id,
    )
    assert inv[0][0] == "COMPLETE" and inv[0][2]["agents"] == ["log_analysis"]
    ex = q(
        owner,
        "SELECT status, agent_version, prompt_id, prompt_version, model, output, "
        "input_tokens FROM orchestrator.agent_executions WHERE id=%s",
        s.execution_id,
    )
    status, version, pid, pver, model, output, _ = ex[0]
    assert (status, pid, pver) == ("SUCCEEDED", "log-analysis", 2) and version == "1.1.0"
    assert model == "fake-small" and output["facts"] and output["evidence_keys"]
    msgs = q(
        owner,
        "SELECT role, content FROM orchestrator.messages WHERE execution_id=%s ORDER BY seq",
        s.execution_id,
    )
    assert [m[0] for m in msgs] == ["system", "user", "assistant"]
    blob = " ".join(m[1] for m in msgs)
    assert "hunter2" not in blob and "ops@northwind" not in blob and "<untrusted_data" in blob

    # evidence: stored by incident-service, every fact's citation resolves to a stored row
    keys = {
        row[0]
        for row in q(
            owner, "SELECT evidence_key FROM incident.evidence WHERE incident_id=%s", incident["id"]
        )
    }
    assert keys == {e.evidence_key for e in r.evidence} and s.evidence_inserted == len(keys)
    for fact in r.facts:
        assert {e.evidence_id for e in fact.evidence} <= keys
    assert all(k.startswith("LOG-") for k in keys)
    tl = q(
        owner,
        "SELECT actor, event_type FROM incident.incident_events WHERE incident_id=%s "
        "AND event_type='EvidenceRetrieved'",
        incident["id"],
    )
    assert tl == [("agent:log_analysis", "EvidenceRetrieved")]
    # the tool calls were made AS the agent, FOR the user, and tagged with the investigation
    calls = q(
        owner,
        "SELECT agent_name, status, investigation_id FROM tools.tool_calls WHERE incident_id=%s",
        incident["id"],
    )
    assert len(calls) == 2 and all(c[0] == "log_analysis" and c[1] == "OK" for c in calls)
    assert {c[2] for c in calls} == {s.investigation_id}
    # evidence id -> tool call -> audit: the chain an auditor follows
    tc_ids = {str(e.tool_call_id) for e in r.evidence}
    assert all(k.split("-")[1] in {t.replace("-", "") for t in tc_ids} for k in keys)

    # idempotent: the recorded batch re-posted inserts nothing and adds no timeline entry
    assert await runner.repost_evidence(s.investigation_id) == 0
    assert (
        len(
            q(
                owner,
                "SELECT 1 FROM incident.incident_events WHERE incident_id=%s AND "
                "event_type='EvidenceRetrieved'",
                incident["id"],
            )
        )
        == 1
    )


async def test_user_without_logs_permission_fails_honestly(
    runner: Runner, incident: dict[str, Any], tok: Tokens, owner: psycopg.Connection
) -> None:
    """MANAGER can read incidents but not logs: the agent must FAIL with the reason, not
    produce facts from nothing - and the denial is in the tool-gateway record."""
    s = await runner.run_log_analysis(incident["key"], tok.user("MANAGER"))
    assert s.status == "FAILED" and s.result and s.result.status == "FAILED"
    assert "missing_permission" in (s.result.error or "")
    assert (
        q(owner, "SELECT status FROM orchestrator.investigations WHERE id=%s", s.investigation_id)[
            0
        ][0]
        == "FAILED"
    )
    assert (
        q(owner, "SELECT count(*) FROM incident.evidence WHERE incident_id=%s", incident["id"])[0][
            0
        ]
        == 0
    )
    denied = q(
        owner,
        "SELECT count(*) FROM tools.tool_calls WHERE incident_id=%s AND "
        "status='DENIED' AND agent_name='log_analysis'",
        incident["id"],
    )
    assert denied[0][0] == 1


async def test_evidence_post_failure_is_recorded_and_repost_recovers(
    runner: Runner,
    incident: dict[str, Any],
    tok: Tokens,
    owner: psycopg.Connection,
    incident_app: FastAPI,
    agents_app: FastAPI,
) -> None:
    """Review finding: a failed evidence POST left facts citing evidence nobody stored, and the
    docstring claimed a re-run would fix it (it can't: new tool calls = new ids)."""

    class DownForEvidence(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.apps = {
                "incident": httpx.ASGITransport(app=incident_app),
                "agents": httpx.ASGITransport(app=agents_app),
            }

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path.endswith("/evidence"):
                return httpx.Response(503, text="incident-service down")
            return await self.apps[request.url.host].handle_async_request(request)

    good_http = runner.http
    runner.http = httpx.AsyncClient(transport=DownForEvidence(), timeout=60)
    try:
        s = await runner.run_log_analysis(incident["key"], tok.user("SRE"))
    finally:
        await runner.http.aclose()
        runner.http = good_http
    assert s.status == "FAILED" and "evidence was not stored" in (s.error or "")
    assert (
        q(owner, "SELECT count(*) FROM incident.evidence WHERE incident_id=%s", incident["id"])[0][
            0
        ]
        == 0
    )
    inserted = await runner.repost_evidence(s.investigation_id)
    assert inserted == len(s.result.evidence) > 0  # type: ignore[union-attr]
    assert (
        q(owner, "SELECT status FROM orchestrator.investigations WHERE id=%s", s.investigation_id)[
            0
        ][0]
        == "COMPLETE"
    )


async def test_runner_crash_leaves_no_running_rows(
    runner: Runner,
    incident: dict[str, Any],
    tok: Tokens,
    owner: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*a: Any, **k: Any) -> Any:
        raise ConnectionError("db went away while recording")

    monkeypatch.setattr(runner, "_record", boom)
    with pytest.raises(ConnectionError):
        await runner.run_log_analysis(incident["key"], tok.user("SRE"))
    rows = q(
        owner,
        "SELECT i.status, t.status FROM orchestrator.investigations i JOIN "
        "orchestrator.tasks t ON t.investigation_id = i.id WHERE i.incident_id=%s",
        incident["id"],
    )
    assert rows == [("FAILED", "FAILED")]


async def test_one_running_investigation_per_incident_and_stale_ones_heal(
    runner: Runner, incident: dict[str, Any], tok: Tokens, owner: psycopg.Connection
) -> None:
    owner.execute(
        "INSERT INTO orchestrator.investigations (id, incident_id, status, requested_by, "
        "budget_usd) VALUES (%s, %s, 'RUNNING', 'user:x', 1)",
        (uuid4(), incident["id"]),
    )
    with pytest.raises(RunnerError, match=r"another investigation .* is RUNNING .* \(by user:x"):
        await runner.run_log_analysis(incident["key"], tok.user("SRE"))
    owner.execute(
        "UPDATE orchestrator.investigations SET started_at = now() - interval '1 hour' "
        "WHERE incident_id=%s AND status='RUNNING'",
        (incident["id"],),
    )
    s = await runner.run_log_analysis(incident["key"], tok.user("SRE"))
    assert s.status == "COMPLETE"
    assert (
        q(
            owner,
            "SELECT count(*) FROM orchestrator.investigations WHERE incident_id=%s "
            "AND status='CANCELLED'",
            incident["id"],
        )[0][0]
        == 1
    )


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
