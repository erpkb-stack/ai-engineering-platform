"""Tool Gateway end to end against real Postgres (least-privilege logins) + the audit service.

Covers the Phase 7 gate: 11 tools over simulated data, authz = user perms ∩ agent allow-list,
every call audited (allowed AND denied), security cases from .claude/rules/testing.md."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi import FastAPI

from aeoi_security.testing import KeyPair, generate_keypair, service_token_for, token_for
from aeoi_tools.main import build_app as build_tools

from .conftest import T0, Planted, StubRag, Tokens, make_stub_rag, tools_settings

pytestmark = pytest.mark.integration
W = {"start": T0.isoformat(), "end": (T0 + timedelta(hours=2)).isoformat()}


async def call(
    c: httpx.AsyncClient, tool: str, headers: dict[str, str], **body: Any
) -> httpx.Response:
    return await c.post(f"/v1/tools/{tool}/invoke", json=body, headers=headers)


def row(owner: psycopg.Connection, call_id: str) -> dict[str, Any]:
    cur = owner.execute(
        "SELECT status, denial_reason, agent_name, input, output_summary, attempt, on_behalf_of"
        " FROM tools.tool_calls WHERE id = %s",
        (call_id,),
    )
    names = [d.name for d in cur.description or []]
    found = cur.fetchone()
    assert found, f"tool call {call_id} NOT recorded"
    return dict(zip(names, found, strict=True))


def outbox_for(owner: psycopg.Connection, call_id: str) -> dict[str, Any]:
    r = owner.execute(
        "SELECT event FROM tools.audit_outbox WHERE event->'details'->>'tool_call_id' = %s",
        (call_id,),
    ).fetchone()
    assert r, "no audit event in the outbox for this call"
    return r[0]  # type: ignore[no-any-return]


# ------------------------------------------------------------------ happy paths (11 tools)
async def test_search_logs_counts_redacts_flags_and_records(
    tools: httpx.AsyncClient, tok: Tokens, planted: Planted, owner: psycopg.Connection
) -> None:
    r = await call(tools, "search_logs", tok.h("ENGINEER"), args={"service_key": planted.svc, **W})
    assert r.status_code == 200, r.text
    body = r.json()
    data = body["data"]
    assert data["total_matching"] == 30 and data["counts_by_error_code"]["E_POOL"] == 20
    assert all(i["level"] in ("WARN", "ERROR") for i in data["items"])  # INFO filtered
    blob = json.dumps(data)
    assert "hunter2" not in blob and "AKIA" not in blob and "ops@northwind" not in blob
    assert body["untrusted"] is True and body["security"]["injection_flags"]
    assert body["evidence_ids"][0] == f"LOG-{UUID(body['tool_call_id']).hex}-0"
    rec = row(owner, body["tool_call_id"])
    assert rec["status"] == "OK" and rec["agent_name"] is None
    assert rec["output_summary"]["items"] == len(data["items"])
    assert outbox_for(owner, body["tool_call_id"])["outcome"] == "SUCCESS"


async def test_metrics_are_bucketed_with_stats(
    tools: httpx.AsyncClient, tok: Tokens, planted: Planted
) -> None:
    r = await call(tools, "query_metrics", tok.h("SRE"), args={
        "service_key": planted.svc, "metric": "db_pool_utilization", "max_points": 12, **W,
    })  # fmt: skip
    series = r.json()["data"]["items"][0]
    assert series["bucket_seconds"] == 600 and len(series["points"]) == 12
    assert series["stats"]["max"] == pytest.approx(0.99) and series["stats"]["min"] == 0.5


async def test_deployment_and_config_diff_hide_secret_values(
    tools: httpx.AsyncClient, tok: Tokens, planted: Planted
) -> None:
    r = await call(tools, "get_deployment", tok.h("ENGINEER"),
                   args={"service_key": planted.svc, **W})  # fmt: skip
    dep = r.json()["data"]["items"][0]
    assert dep["deploy_key"] == planted.deploy_key and dep["commit_sha"] == planted.sha
    assert dep["config_changed_keys"] == ["db.password", "feature_flags.order_batching"]
    r = await call(tools, "get_config_diff", tok.h("ENGINEER"),
                   args={"deploy_key": planted.deploy_key})  # fmt: skip
    changes = {c["key"]: c for c in r.json()["data"]["items"][0]["changes"]}
    assert changes["feature_flags.order_batching"] == {
        "key": "feature_flags.order_batching", "before": False, "after": True
    }  # fmt: skip
    assert changes["db.password"]["after"] == "[REDACTED]" and "db.pool.max_size" not in changes
    assert "new-pass" not in r.text


async def test_code_tools(tools: httpx.AsyncClient, tok: Tokens, planted: Planted) -> None:
    h = tok.h("ENGINEER")
    r = await call(tools, "get_commit", h, args={"sha": planted.sha[:10]})
    assert r.json()["data"]["items"][0]["sha"] == planted.sha
    r = await call(tools, "get_commit", h, args={"sha": planted.twin_prefix})
    assert r.status_code == 422 and r.json()["reason"] == "execution_failed"
    r = await call(tools, "get_commit", h, args={"sha": "0" * 40})
    assert r.status_code == 200 and r.json()["data"]["items"] == []  # absence is a fact
    r = await call(tools, "get_pull_request", h,
                   args={"repository": planted.repo, "number": planted.pr_number})  # fmt: skip
    body = r.json()
    assert body["data"]["items"][0]["merge_commit_sha"] == planted.sha
    assert body["security"]["injection_flags"]  # malicious PR body: flagged, wrapped later
    r = await call(
        tools, "search_repository", h, args={"query": "batch", "repository": planted.repo}
    )
    kinds = {i["kind"] for i in r.json()["data"]["items"]}
    assert kinds == {"commit", "pull_request"}
    r = await call(
        tools, "search_repository", h, args={"query": "100%_" + "x", "service_key": planted.svc}
    )
    assert r.json()["data"]["items"] == []  # wildcards are literals


async def test_catalog(tools: httpx.AsyncClient, tok: Tokens, planted: Planted) -> None:
    r = await call(tools, "query_service_catalog", tok.h("MANAGER"), args={"service_key": "tg-db"})
    assert r.json()["data"]["items"][0]["dependents"] == [planted.svc]
    r = await call(tools, "query_service_catalog", tok.h("MANAGER"), args={})
    assert r.status_code == 422  # no full dumps


async def test_knowledge_tools_forward_the_user_token(
    tools: httpx.AsyncClient, tok: Tokens, stub_rag: StubRag
) -> None:
    headers = tok.agent("ENGINEER")
    user_bearer = headers["X-On-Behalf-Of"]
    r = await call(tools, "search_runbooks", headers, agent_name="knowledge",
                   args={"query": "db pool exhausted"})  # fmt: skip
    assert r.status_code == 200, r.text
    assert stub_rag.seen[-1]["auth"] == user_bearer  # NOT the service token
    assert stub_rag.seen[-1]["body"]["filters"] == {"sources": ["runbook"]}
    assert "oncall@northwind" not in r.text and r.json()["security"]["pii_redacted"]["EMAIL"] == 1
    r = await call(
        tools, "search_docs", tok.h("ENGINEER"), args={"query": "pool", "sources": ["pdf"]}
    )
    assert r.status_code == 200 and stub_rag.seen[-1]["body"]["filters"] == {"sources": ["pdf"]}
    r = await call(tools, "search_incidents", headers, agent_name="historical_incident",
                   args={"query": "pool exhaustion", "service_key": "checkout-api"})  # fmt: skip
    assert r.json()["data"]["items"][0]["incident_key"] == "INC-1001"


# ------------------------------------------------------------------ authorization
async def test_role_without_permission_is_denied_and_recorded(
    tools: httpx.AsyncClient, tok: Tokens, planted: Planted, owner: psycopg.Connection
) -> None:
    r = await call(tools, "search_logs", tok.h("MANAGER"), args={"service_key": planted.svc, **W})
    assert r.status_code == 403 and r.json()["reason"] == "missing_permission"
    rec = row(owner, r.json()["tool_call_id"])
    assert rec["status"] == "DENIED" and "logs:read" in rec["denial_reason"]
    assert outbox_for(owner, r.json()["tool_call_id"])["outcome"] == "DENIED"


async def test_agent_mode_is_intersection(
    tools: httpx.AsyncClient, tok: Tokens, planted: Planted, owner: psycopg.Connection
) -> None:
    ok = await call(tools, "search_logs", tok.agent("ENGINEER"), agent_name="log_analysis",
                    args={"service_key": planted.svc, **W})  # fmt: skip
    assert ok.status_code == 200
    rec = row(owner, ok.json()["tool_call_id"])
    assert rec["agent_name"] == "log_analysis"
    assert outbox_for(owner, ok.json()["tool_call_id"])["actor"] == "agent:log_analysis"
    # tool not in the agent's allow-list, although the user may use it
    r = await call(tools, "get_commit", tok.agent("ENGINEER"), agent_name="log_analysis",
                   args={"sha": planted.sha})  # fmt: skip
    assert r.status_code == 403 and r.json()["reason"] == "not_in_agent_allowlist"
    # tool in the allow-list, but the USER lacks the permission
    r = await call(tools, "search_logs", tok.agent("MANAGER"), agent_name="copilot",
                   args={"service_key": planted.svc, **W}, )  # fmt: skip
    assert r.status_code == 403  # 'agents' service may not assert copilot either way


@pytest.mark.parametrize(
    ("make_headers", "agent", "status", "reason"),
    [
        (
            lambda t: {"Authorization": t.agent()["Authorization"]},
            "log_analysis",
            403,
            "service_without_user",
        ),
        (
            lambda t: {
                **t.agent(),
                "X-On-Behalf-Of": "Bearer " + token_for(generate_keypair(), "SRE"),
            },
            "log_analysis",
            403,
            "invalid_on_behalf_of",
        ),
        (
            lambda t: {**t.agent(), "X-On-Behalf-Of": t.agent()["Authorization"]},
            "log_analysis",
            403,
            "invalid_on_behalf_of",
        ),
        (lambda t: t.agent(), "action_executor", 403, "untrusted_agent_assertion"),
        (lambda t: t.agent(), None, 403, "untrusted_agent_assertion"),
        (lambda t: t.agent(service="rag"), "knowledge", 403, "untrusted_agent_assertion"),
    ],
    ids=["no-obo", "forged-obo", "obo-is-service", "untrusted-agent", "no-agent", "unknown-svc"],
)
async def test_service_callers_are_refused_and_audited(
    tools: httpx.AsyncClient, tok: Tokens, owner: psycopg.Connection,
    make_headers: Any, agent: str | None, status: int, reason: str,
) -> None:  # fmt: skip
    body: dict[str, Any] = {"args": {"service_key": "x-svc", **W}}
    if agent:
        body["agent_name"] = agent
    r = await tools.post("/v1/tools/search_logs/invoke", json=body, headers=make_headers(tok))
    assert (r.status_code, r.json()["reason"]) == (status, reason), r.text
    ev = owner.execute(
        "SELECT event FROM tools.audit_outbox WHERE id = %s", (r.json()["audit_event_id"],)
    ).fetchone()
    assert ev and ev[0]["actor"].startswith("service:") and ev[0]["outcome"] == "DENIED"


async def test_service_token_without_scope_is_refused(
    tools: httpx.AsyncClient, tkeys: KeyPair
) -> None:
    h = {"Authorization": f"Bearer {service_token_for(tkeys, 'agents', 'llm:invoke')}"}
    r = await tools.post("/v1/tools/search_logs/invoke", json={"args": {}}, headers=h)
    assert r.status_code == 403


async def test_user_cannot_pose_as_agent_or_send_obo(
    tools: httpx.AsyncClient, tok: Tokens, planted: Planted, owner: psycopg.Connection
) -> None:
    r = await call(tools, "search_logs", tok.h("SRE"), agent_name="log_analysis",
                   args={"service_key": planted.svc, **W})  # fmt: skip
    assert r.status_code == 403 and r.json()["reason"] == "untrusted_agent_assertion"
    assert row(owner, r.json()["tool_call_id"])["status"] == "DENIED"
    r = await call(tools, "search_logs", {**tok.h("SRE"), "X-On-Behalf-Of": "Bearer x"},
                   args={"service_key": planted.svc, **W})  # fmt: skip
    assert r.status_code == 400


async def test_consequential_tool_is_denied_without_and_with_approval(
    tools: httpx.AsyncClient, tok: Tokens, planted: Planted, owner: psycopg.Connection
) -> None:
    args = {"deploy_key": planted.deploy_key, "reason": "p95 regression after deploy"}
    r = await call(tools, "rollback_deployment", tok.h("INCIDENT_COMMANDER"), args=args)
    assert r.status_code == 403 and r.json()["reason"] == "approval_required"
    r = await call(
        tools,
        "rollback_deployment",
        tok.h("INCIDENT_COMMANDER"),
        args=args,
        approval_id=str(uuid4()),
    )  # a made-up approval id must NOT work
    assert r.status_code == 403 and r.json()["reason"] == "approval_unverifiable"
    assert row(owner, r.json()["tool_call_id"])["status"] == "DENIED"
    r = await call(tools, "rollback_deployment", tok.h("ENGINEER"), args=args)
    assert r.json()["reason"] == "missing_permission"  # perms are checked first


async def test_list_tools_is_filtered(tools: httpx.AsyncClient, tok: Tokens) -> None:
    names = {t["name"] for t in (await tools.get("/v1/tools", headers=tok.h("MANAGER"))).json()}
    assert names == {"query_service_catalog", "search_docs", "search_incidents"}
    r = await tools.get("/v1/tools", params={"agent_name": "metrics"}, headers=tok.agent("SRE"))
    assert [t["name"] for t in r.json()] == ["query_metrics"]
    r = await tools.get("/v1/tools", headers={"Authorization": tok.agent()["Authorization"]})
    assert r.status_code == 403


# ------------------------------------------------------------------ input bounds
async def test_bad_inputs_are_rejected_and_recorded(
    tools: httpx.AsyncClient, tok: Tokens, planted: Planted, owner: psycopg.Connection
) -> None:
    wide = {"start": T0.isoformat(), "end": (T0 + timedelta(days=2)).isoformat()}
    r = await call(tools, "search_logs", tok.h("SRE"), args={"service_key": planted.svc, **wide})
    assert r.status_code == 422 and r.json()["reason"] == "invalid_input"
    assert "window larger" in json.dumps(r.json()["errors"])
    assert row(owner, r.json()["tool_call_id"])["status"] == "ERROR"
    r = await call(tools, "search_logs", tok.h("SRE"),
                   args={"service_key": planted.svc, "contains": "x" * 5000, **W})  # fmt: skip
    assert r.status_code == 422
    r = await call(tools, "drop_all_tables", tok.h("SRE"), args={})
    assert r.status_code == 404 and row(owner, r.json()["tool_call_id"])["status"] == "DENIED"


# ------------------------------------------------------------------ egress + availability
async def test_egress_outside_allowlist_never_leaves(
    tools_db_url: str, tpub: Any, catalog_file: Any, audit_token_file: Any, tok: Tokens,
    owner: psycopg.Connection,
) -> None:  # fmt: skip
    stub = StubRag()
    settings = tools_settings(tools_db_url, tpub, catalog_file, audit_token_file)
    settings = settings.model_copy(update={"rag_url": "http://attacker.example:8004"})
    app = build_tools(settings, rag_transport=httpx.ASGITransport(app=make_stub_rag(stub)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await call(c, "search_docs", tok.h("SRE"), args={"query": "pool"})
    await app.state.engine.dispose()
    assert r.status_code == 403 and r.json()["reason"] == "egress_blocked"
    assert stub.seen == []
    assert row(owner, r.json()["tool_call_id"])["status"] == "DENIED"


async def test_rag_down_is_503_not_500(
    tools_db_url: str, tpub: Any, catalog_file: Any, audit_token_file: Any, tok: Tokens
) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    app = build_tools(
        tools_settings(tools_db_url, tpub, catalog_file, audit_token_file),
        rag_transport=httpx.MockTransport(boom),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await call(c, "search_docs", tok.h("SRE"), args={"query": "pool"})
        logs = await call(c, "get_commit", tok.h("SRE"), args={"sha": "0" * 40})
    await app.state.engine.dispose()
    assert r.status_code == 503 and r.headers["retry-after"]
    assert logs.status_code == 200  # rag being down does not take devdata tools with it


# ------------------------------------------------------------------ DB-level defences
@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE tools.tool_calls SET status = 'OK'",
        "DELETE FROM tools.tool_calls",
        "INSERT INTO devdata.log_events (ts, service_key, level, message) VALUES (now(),'x','INFO','x')",
        "UPDATE devdata.deployments SET status = 'ROLLED_BACK'",
    ],
)
def test_tools_login_is_append_only_and_read_only_on_devdata(tools_db_url: str, sql: str) -> None:
    dsn = tools_db_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(sql)


def test_db_refuses_executed_consequential_call_without_approval(tools_db_url: str) -> None:
    dsn = tools_db_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO tools.tool_calls (id, on_behalf_of, tool_name, side_effect, input, status)"
            " VALUES (gen_random_uuid(), gen_random_uuid(), 'rollback_deployment',"
            " 'CONSEQUENTIAL', '{}', 'OK')"
        )


# ------------------------------------------------------------------ audit delivery + reads
async def test_relay_delivers_once_and_audit_scopes_hold(
    tools: httpx.AsyncClient, tools_app: FastAPI, audit: httpx.AsyncClient, tok: Tokens,
    planted: Planted, owner: psycopg.Connection, tkeys: KeyPair,
) -> None:  # fmt: skip
    uid = uuid4()
    me = {"Authorization": f"Bearer {token_for(tkeys, 'ENGINEER', user_id=uid)}"}
    incident = uuid4()
    r = await call(tools, "search_logs", me, incident_id=str(incident),
                   args={"service_key": planted.svc, **W})  # fmt: skip
    call_id = r.json()["tool_call_id"]
    relay = tools_app.state.relay
    while await relay.run_once():
        pass
    n_before = owner.execute("SELECT count(*) FROM audit.audit_events").fetchone()[0]  # type: ignore[index]
    owner.execute("UPDATE tools.audit_outbox SET delivered_at = NULL")  # simulate a crash+resend
    while await relay.run_once():
        pass
    assert owner.execute("SELECT count(*) FROM audit.audit_events").fetchone()[0] == n_before  # type: ignore[index]
    ev = owner.execute(
        "SELECT actor, outcome, details FROM audit.audit_events"
        " WHERE details->>'tool_call_id' = %s", (call_id,),
    ).fetchall()  # fmt: skip
    assert len(ev) == 1 and ev[0][0] == f"user:{uid}"
    assert ev[0][2]["_ingested_by"] == "service:tool-gateway"
    assert ev[0][2]["args"]["service_key"] == planted.svc

    own = (await audit.get("/v1/events", headers=me)).json()  # default window: last 7 days
    assert own["scope"] == "own" and own["items"]
    assert all(
        i["actor"] == f"user:{uid}" or i["details"].get("on_behalf_of") == str(uid)
        for i in own["items"]
    )
    # IC: an incident_id parameter must NOT widen the scope (no assignment data yet) - it only
    # narrows their own events. ADMIN (read_all) sees the incident's events.
    ic = await audit.get(
        "/v1/events", params={"incident_id": str(incident)}, headers=tok.h("INCIDENT_COMMANDER")
    )
    assert ic.json()["scope"] == "own" and ic.json()["items"] == []
    adm = await audit.get(
        "/v1/events", params={"incident_id": str(incident)}, headers=tok.h("ADMIN")
    )
    assert adm.json()["scope"] == "all" and len(adm.json()["items"]) == 1
    assert (await audit.get("/v1/events", headers=tok.h("MANAGER"))).status_code == 403
    page = await audit.get("/v1/events", params={"limit": 2}, headers=tok.h("ADMIN"))
    assert page.json()["scope"] == "all" and page.json()["next_cursor"]
    nxt = await audit.get("/v1/events", params={"limit": 2, "cursor": page.json()["next_cursor"]},
                          headers=tok.h("ADMIN"))  # fmt: skip
    assert not {i["id"] for i in nxt.json()["items"]} & {i["id"] for i in page.json()["items"]}
    wide = {"since": (T0 - timedelta(days=90)).isoformat(), "until": T0.isoformat()}
    assert (await audit.get("/v1/events", params=wide, headers=tok.h("ADMIN"))).status_code == 422
    svc_h = {"Authorization": f"Bearer {service_token_for(tkeys, 'x', 'audit:write')}"}
    assert (await audit.get("/v1/events", headers=svc_h)).status_code == 403


async def test_audit_write_requires_service_scope(audit: httpx.AsyncClient, tok: Tokens) -> None:
    ev = {"id": str(uuid4()), "occurred_at": T0.isoformat(), "actor": "user:x", "action": "x.y",
          "resource_type": "t", "outcome": "SUCCESS"}  # fmt: skip
    r = await audit.post("/v1/events", json={"events": [ev]}, headers=tok.h("ADMIN"))
    assert r.status_code == 403  # a USER can never write audit history


async def test_relay_failure_keeps_events_and_counts_attempts(
    tools_db_url: str, tpub: Any, catalog_file: Any, audit_token_file: Any, tok: Tokens,
    planted: Planted, owner: psycopg.Connection,
) -> None:  # fmt: skip
    """Audit service down: tool calls still succeed (outbox = buffer); the relay records each
    failed attempt and delivers nothing. Regression: the attempt update used to be rolled back
    together with the failed batch."""

    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    app = build_tools(
        tools_settings(tools_db_url, tpub, catalog_file, audit_token_file),
        audit_transport=httpx.MockTransport(down),
    )
    owner.execute("UPDATE tools.audit_outbox SET delivered_at = now() WHERE delivered_at IS NULL")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await call(c, "get_commit", tok.h("SRE"), args={"sha": planted.sha})
    assert r.status_code == 200  # audit outage does not block tools
    with pytest.raises(httpx.HTTPStatusError):
        await app.state.relay.run_once()
    n, age = await app.state.relay.backlog()
    await app.state.engine.dispose()
    assert n == 1 and age is not None
    attempts, err, delivered = owner.execute(
        "SELECT attempts, last_error, delivered_at FROM tools.audit_outbox"
        " WHERE event->'details'->>'tool_call_id' = %s", (r.json()["tool_call_id"],),
    ).fetchone()  # type: ignore[misc]  # fmt: skip
    assert (attempts, err, delivered) == (1, "HTTP 500", None)


async def test_api_gateway_routes_tools_and_audit_and_strips_obo(
    tools_app: FastAPI, audit_app: FastAPI, tpub: Any, tok: Tokens, planted: Planted,
    owner: psycopg.Connection,
) -> None:  # fmt: skip
    from aeoi_api.config import Settings as ApiSettings
    from aeoi_api.main import build_app as build_api
    from aeoi_api.ratelimit import InMemoryTokenBucket

    api = build_api(
        ApiSettings(jwt_public_key_file=tpub, environment="test"),  # type: ignore[arg-type]
        transports={
            "tool-gateway": httpx.ASGITransport(app=tools_app),
            "audit": httpx.ASGITransport(app=audit_app),
        },
        limiter=InMemoryTokenBucket(rate_per_s=100, burst=100),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://api"
    ) as c:
        listed = await c.get("/api/v1/tools", headers=tok.h("ENGINEER"))
        assert listed.status_code == 200 and "search_logs" in {t["name"] for t in listed.json()}
        # an end user tries to smuggle an on-behalf-of header through the edge: it is dropped,
        # so the call runs as THIS user (not as whoever the header names)
        victim = tok.user("INCIDENT_COMMANDER")
        r = await c.post(
            "/api/v1/tools/search_logs/invoke",
            json={"args": {"service_key": planted.svc, **W}},
            headers={**tok.h("ENGINEER"), "X-On-Behalf-Of": f"Bearer {victim}"},
        )
        assert r.status_code == 200, r.text
        assert row(owner, r.json()["tool_call_id"])["agent_name"] is None
        assert (await c.get("/api/v1/audit/events", headers=tok.h("ENGINEER"))).status_code == 200
        assert (await c.get("/api/v1/tools")).status_code == 401


async def test_early_refusals_are_recorded_for_known_users(
    tools: httpx.AsyncClient, tok: Tokens, planted: Planted, owner: psycopg.Connection
) -> None:
    """Review finding: 400 (user + OBO header), 413 and envelope-422 used to leave no row."""
    r = await call(
        tools, "search_logs", {**tok.h("SRE"), "X-On-Behalf-Of": "Bearer x"},
        args={"service_key": planted.svc, **W},
    )  # fmt: skip
    assert r.status_code == 400 and row(owner, r.json()["tool_call_id"])["status"] == "DENIED"
    r = await tools.post(
        "/v1/tools/search_docs/invoke", json={"args": {"query": "q" * 70_000}}, headers=tok.h("SRE")
    )
    assert r.status_code == 413
    rec = row(owner, r.json()["tool_call_id"])
    assert rec["status"] == "ERROR" and rec["input"]["_truncated"] is True
    r = await tools.post("/v1/tools/search_docs/invoke", json={"argz": {}}, headers=tok.h("SRE"))
    assert r.status_code == 422 and row(owner, r.json()["tool_call_id"])["status"] == "ERROR"


async def test_poison_event_does_not_block_audit_delivery(
    tools_db_url: str, tpub: Any, catalog_file: Any, audit_token_file: Any, tok: Tokens,
    planted: Planted, owner: psycopg.Connection, audit_app: FastAPI,
) -> None:  # fmt: skip
    """Review finding: one event the receiver rejects used to block every later batch."""
    real = httpx.ASGITransport(app=audit_app)
    poison: set[str] = set()

    async def picky(request: httpx.Request) -> httpx.Response:
        events = json.loads(request.content)["events"]
        if any(e["details"].get("tool_call_id") in poison for e in events):
            return httpx.Response(422, json={"detail": "bad event"})
        return await real.handle_async_request(request)

    app = build_tools(
        tools_settings(tools_db_url, tpub, catalog_file, audit_token_file),
        audit_transport=httpx.MockTransport(picky),
    )
    owner.execute("UPDATE tools.audit_outbox SET delivered_at = now() WHERE delivered_at IS NULL")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        ids = [
            (await call(c, "get_commit", tok.h("SRE"), args={"sha": planted.sha})).json()[
                "tool_call_id"
            ]
            for _ in range(3)
        ]
    poison.add(ids[1])
    delivered = await app.state.relay.run_once()
    assert delivered == 2
    assert await app.state.relay.rejected() == 1
    assert await app.state.relay.run_once() == 0  # the poison row is skipped, not retried forever
    await app.state.engine.dispose()
    status = dict(
        owner.execute(
            "SELECT event->'details'->>'tool_call_id', coalesce(last_error, 'delivered')"
            " FROM tools.audit_outbox WHERE event->'details'->>'tool_call_id' = ANY(%s)", (ids,),
        ).fetchall()
    )  # fmt: skip
    assert status[ids[1]].startswith("rejected: HTTP 422")
    assert status[ids[0]] == status[ids[2]] == "delivered"
