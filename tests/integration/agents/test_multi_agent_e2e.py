"""Phase 10 gate, end to end: four agents in parallel on one investigation (log_analysis,
metrics, deployment, knowledge), each through the tool-gateway as the user with the
investigation's delegated token. Outcome COMPLETE / PARTIAL, per-kind evidence visibility,
per-agent trace redaction, knowledge stored as pointers only, crash + resume with siblings.
Real Postgres, least-privilege logins, real checkpointer, fake model, STUB rag."""

from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import psycopg
import pytest
from fastapi import FastAPI

from .conftest import T0, HostRouter, OrchFactory, People, Planted, Tokens
from .test_orchestrator_e2e import get, q, start

pytestmark = pytest.mark.integration
ALL = ("log_analysis", "metrics", "deployment", "knowledge")
STUB_RAG_TEXT = "Step 1: check pool"  # what the stub rag returns as chunk content


@pytest.fixture
async def incident10(incident_app: FastAPI, tok: Tokens, planted: Planted) -> dict[str, Any]:
    """Detected 80 min into the planted data: window T0+20..T0+110, so the metrics baseline
    (before T0+20) exists and the planted pool-utilisation jump at T0+60 is in the window."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=incident_app), base_url="http://incident"
    ) as c:
        r = await c.post(
            "/v1/incidents",
            json={
                "title": "Checkout pool timeouts after deploy",
                "severity": "SEV2",
                "affected_services": [planted.svc],
                "detected_at": (T0 + timedelta(minutes=80)).isoformat(),
            },
            headers={**tok.h("SRE"), "Idempotency-Key": secrets.token_hex(8)},
        )
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


@pytest.fixture
def apps(incident_app: FastAPI, agents_app: FastAPI, api_app: FastAPI) -> dict[str, FastAPI]:
    return {"incident": incident_app, "agents": agents_app, "api": api_app}


class Faults(httpx.AsyncBaseTransport):
    """Host router + per-agent faults: `fail` agents get HTTP 500, `hang` agents never answer."""

    def __init__(self, apps: dict[str, FastAPI], *, fail: tuple[str, ...] = (),
                 hang: tuple[str, ...] = ()) -> None:  # fmt: skip
        self.inner = HostRouter(apps)
        self.fail, self.hang = fail, hang
        self.calls: list[str] = []
        self.hung = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "agents" and request.url.path.endswith("/run"):
            agent = request.url.path.split("/")[-2]
            self.calls.append(agent)
            if agent in self.fail:
                return httpx.Response(500, json={"detail": f"{agent} exploded"})
            if agent in self.hang:
                self.hung.set()
                await asyncio.Event().wait()
        return await self.inner.handle_async_request(request)


async def evidence(app: FastAPI, token: str, incident_id: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://incident"
    ) as c:
        r = await c.get(
            f"/v1/incidents/{incident_id}/evidence", headers={"Authorization": f"Bearer {token}"}
        )
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


async def test_four_agents_in_parallel_complete_with_cited_evidence(
    make_orch: OrchFactory, people: People, incident10: dict[str, Any], planted: Planted,
    owner: psycopg.Connection, incident_app: FastAPI,
) -> None:  # fmt: skip
    uid = people.add("SRE")
    token = people.token(uid)
    async with make_orch(agents=ALL) as app:
        r = await start(app, token, incident10, key="key-p10-happy-0001")
        assert r.status_code == 202, r.text
        inv = r.json()["investigation_id"]
        await app.state.engine.wait(UUID(inv))
        out = await get(app, token, inv)
        trace = await get(app, token, inv, "/trace")
    assert out["status"] == "COMPLETE", out
    assert sorted(t["agent"] for t in out["tasks"]) == sorted(ALL)
    assert all(t["status"] == "SUCCEEDED" and t["attempt"] == 1 for t in out["tasks"])
    by_agent = {e["agent"]: e["output"] for e in trace["executions"]}

    m = by_agent["metrics"]
    [anomaly] = [a for a in m["anomalies"] if a["metric"] == "db_pool_utilization"]
    assert anomaly["kind"] == "shift" and anomaly["direction"] == "up"
    # T0 + 60 min, the planted jump (compare instants: the DB session may answer in -05:00)
    assert datetime.fromisoformat(anomaly["onset"]) == T0 + timedelta(minutes=60)
    d = by_agent["deployment"]
    [change] = d["changes"]
    assert change["deploy_key"] == planted.deploy_key
    assert change["minutes_before_detection"] == 70.0  # started T0+10, detected T0+80
    cfg = {c["key"]: c for c in change["config_changes"]}
    assert cfg["feature_flags.order_batching"]["after"] is True
    assert cfg["db.password"]["after"] != "new-pass"  # a secret changed; its value never stored
    assert "db.pool.max_size" not in cfg  # [20, 20] is touched, not changed
    k = by_agent["knowledge"]
    assert k["references"] and k["facts"] == []

    # every fact of every agent cites evidence that incident-service stored
    stored = {
        r[0]: r[1]
        for r in q(
            owner,
            "SELECT evidence_key, kind FROM incident.evidence WHERE incident_id=%s",
            incident10["id"],
        )
    }
    for agent, output in by_agent.items():
        for f in output["facts"]:
            assert {e["evidence_id"] for e in f["evidence"]} <= set(stored), agent
    assert {"LOG", "METRIC", "DEPLOY", "CONFIG", "RUNBOOK"} <= set(stored.values())
    # knowledge evidence is a POINTER: the doc text never reaches the incident or the trace
    texts = q(owner, "SELECT excerpt, title FROM incident.evidence WHERE incident_id=%s",
              incident10["id"])  # fmt: skip
    assert not any(STUB_RAG_TEXT in (e or "") or "DB pool exhaustion" in (t or "")
                   for e, t in texts)  # fmt: skip
    assert STUB_RAG_TEXT not in str(trace)
    # each agent used only its allow-listed tools, as the user, bound to this investigation
    calls = q(owner, "SELECT agent_name, tool_name, on_behalf_of, investigation_id FROM "
              "tools.tool_calls WHERE investigation_id=%s", inv)  # fmt: skip
    allowed = {"log_analysis": {"search_logs"}, "metrics": {"query_metrics"},
               "deployment": {"get_deployment", "get_config_diff"},
               "knowledge": {"search_runbooks", "search_docs"}}  # fmt: skip
    assert {c[0] for c in calls} == set(ALL)
    assert all(c[1] in allowed[c[0]] and c[2] == uid and str(c[3]) == inv for c in calls)
    # one grant for all four agents, revoked at the end
    assert q(owner, "SELECT revoked_reason FROM identity.delegation_grants WHERE "
             "investigation_id=%s", inv) == [("investigation COMPLETE",)]  # fmt: skip


async def test_one_failed_agent_makes_the_run_partial_not_failed(
    make_orch: OrchFactory, people: People, incident10: dict[str, Any], apps: dict[str, FastAPI],
    owner: psycopg.Connection,
) -> None:  # fmt: skip
    uid = people.add("SRE")
    token = people.token(uid)
    faults = Faults(apps, fail=("knowledge",))
    async with make_orch(agents=ALL, transport=faults) as app:
        inv = (await start(app, token, incident10, key="key-p10-part-0001")).json()[
            "investigation_id"
        ]
        await app.state.engine.wait(UUID(inv))
        out = await get(app, token, inv)
    assert out["status"] == "PARTIAL", out
    assert "knowledge" in out["error"] and "missing sources" in out["error"]
    status = {t["agent"]: t["status"] for t in out["tasks"]}
    assert status == {"log_analysis": "SUCCEEDED", "metrics": "SUCCEEDED",
                      "deployment": "SUCCEEDED", "knowledge": "FAILED"}  # fmt: skip
    assert faults.calls.count("knowledge") == 1  # a 500 is not retried (not transient)
    # the siblings' evidence is kept
    kinds = {r[0] for r in q(owner, "SELECT DISTINCT kind FROM incident.evidence WHERE "
                             "incident_id=%s", incident10["id"])}  # fmt: skip
    assert {"LOG", "METRIC", "DEPLOY"} <= kinds and "RUNBOOK" not in kinds
    assert q(owner, "SELECT revoked_reason FROM identity.delegation_grants WHERE "
             "investigation_id=%s", inv) == [("investigation PARTIAL",)]  # fmt: skip


async def test_all_agents_failed_is_failed(
    make_orch: OrchFactory, people: People, incident10: dict[str, Any], apps: dict[str, FastAPI],
) -> None:  # fmt: skip
    token = people.token(people.add("SRE"))
    async with make_orch(agents=("metrics", "deployment"),
                         transport=Faults(apps, fail=("metrics", "deployment"))) as app:  # fmt: skip
        inv = (await start(app, token, incident10, key="key-p10-fail-0001")).json()[
            "investigation_id"
        ]
        await app.state.engine.wait(UUID(inv))
        out = await get(app, token, inv)
    assert out["status"] == "FAILED"


async def test_evidence_kinds_need_the_tools_permission(
    make_orch: OrchFactory, people: People, incident10: dict[str, Any], tok: Tokens,
    incident_app: FastAPI,
) -> None:  # fmt: skip
    """Review finding (Phase 10): GET evidence returned raw log lines to anyone who may read
    the incident. A MANAGER may read incidents and docs - not logs, metrics, deploys."""
    token = people.token(people.add("SRE"))
    async with make_orch(agents=ALL) as app:
        inv = (await start(app, token, incident10, key="key-p10-ev-0001")).json()[
            "investigation_id"
        ]
        await app.state.engine.wait(UUID(inv))
        mgr = await get(app, tok.user("MANAGER"), inv, "/trace")
        sre = await get(app, token, inv, "/trace")
    sre_kinds = {e["kind"] for e in await evidence(incident_app, tok.user("SRE"), incident10["id"])}
    mgr_kinds = {
        e["kind"] for e in await evidence(incident_app, tok.user("MANAGER"), incident10["id"])
    }
    assert {"LOG", "METRIC", "DEPLOY", "CONFIG", "RUNBOOK"} <= sre_kinds
    assert not mgr_kinds & {"LOG", "METRIC", "DEPLOY", "CONFIG", "RUNBOOK"}
    # trace: per agent, the agent's tool permissions (MANAGER has docs:read, not runbooks:read)
    redacted = {e["agent"]: e["redacted"] for e in mgr["executions"]}
    assert redacted == dict.fromkeys(ALL, True)
    assert not any(e["redacted"] for e in sre["executions"])


async def test_crash_while_one_agent_hangs_resumes_only_that_agent(
    make_orch: OrchFactory, people: People, incident10: dict[str, Any], apps: dict[str, FastAPI],
    owner: psycopg.Connection,
) -> None:  # fmt: skip
    """Kill the orchestrator while knowledge hangs and the other three have finished. The new
    process must not call the three finished agents again. What makes that true HERE is
    LangGraph's pending writes (a finished Send branch keeps its result): with run_agent's
    reuse guard removed this test still passes - mutation-checked. The guard itself is
    pinned by test_crash_between_record_and_checkpoint_* (Phase 9)."""
    token = people.token(people.add("SRE"))
    first = Faults(apps, hang=("knowledge",))
    async with make_orch(agents=ALL, transport=first) as app:
        inv = (await start(app, token, incident10, key="key-p10-crash-0001")).json()[
            "investigation_id"
        ]
        await asyncio.wait_for(first.hung.wait(), 30)
        for _ in range(100):  # wait until the three siblings are recorded
            done = q(owner, "SELECT count(*) FROM orchestrator.tasks WHERE investigation_id=%s "
                     "AND status='SUCCEEDED'", inv)[0][0]  # fmt: skip
            if done == 3:
                break
            await asyncio.sleep(0.1)
        assert done == 3
    healthy = Faults(apps)
    async with make_orch(agents=ALL, transport=healthy, resume_on_startup=True) as app2:
        await app2.state.engine.wait(UUID(inv))
        out = await get(app2, token, inv)
    assert out["status"] == "COMPLETE", out
    assert healthy.calls == ["knowledge"]
    attempts = {t["agent"]: t["attempt"] for t in out["tasks"]}
    assert attempts == {"log_analysis": 1, "metrics": 1, "deployment": 1, "knowledge": 2}
    assert q(owner, "SELECT count(*) FROM orchestrator.agent_executions e JOIN orchestrator.tasks t "
             "ON t.id = e.task_id WHERE t.investigation_id=%s", inv)[0][0] == 4  # fmt: skip


async def test_agents_subset_and_unknown_agent(
    make_orch: OrchFactory, people: People, incident10: dict[str, Any],
) -> None:  # fmt: skip
    token = people.token(people.add("SRE"))
    async with make_orch(agents=("log_analysis", "metrics")) as app:
        r = await start(app, token, incident10, key="key-p10-sub-0001", agents=["deployment"])
        assert r.status_code == 400 and "not enabled" in r.text
        r = await start(app, token, incident10, key="key-p10-sub-0002", agents=["nope"])
        assert r.status_code == 422
        r = await start(app, token, incident10, key="key-p10-sub-0003", agents=["metrics"])
        inv = r.json()["investigation_id"]
        await app.state.engine.wait(UUID(inv))
        out = await get(app, token, inv)
    assert [t["agent"] for t in out["tasks"]] == ["metrics"] and out["status"] == "COMPLETE"


async def test_deadline_keeps_what_finished_partial_not_failed(
    make_orch: OrchFactory, people: People, incident10: dict[str, Any], apps: dict[str, FastAPI],
    owner: psycopg.Connection,
) -> None:  # fmt: skip
    """Review finding: the deadline hit while one agent hung, and the three finished agents'
    evidence was thrown away (FAILED, collect_evidence never ran). Now it is salvaged."""
    token = people.token(people.add("SRE"))
    async with make_orch(agents=ALL, transport=Faults(apps, hang=("knowledge",)),
                         investigation_deadline_s=4.0) as app:  # fmt: skip
        inv = (await start(app, token, incident10, key="key-p10-dl-0001")).json()[
            "investigation_id"
        ]
        await app.state.engine.wait(UUID(inv))
        out = await get(app, token, inv)
    assert out["status"] == "PARTIAL", out
    assert "deadline exceeded" in out["error"] and "knowledge: TIMED_OUT" in out["error"]
    status = {t["agent"]: t["status"] for t in out["tasks"]}
    assert status["knowledge"] == "TIMED_OUT"
    assert [status[a] for a in ("log_analysis", "metrics", "deployment")] == ["SUCCEEDED"] * 3
    kinds = {r[0] for r in q(owner, "SELECT DISTINCT kind FROM incident.evidence WHERE "
                             "incident_id=%s", incident10["id"])}  # fmt: skip
    assert {"LOG", "METRIC", "DEPLOY"} <= kinds
    assert q(owner, "SELECT revoked_reason FROM identity.delegation_grants WHERE "
             "investigation_id=%s", inv) == [("investigation PARTIAL: deadline",)]  # fmt: skip
