"""Phase 9 gate, end to end: user -> orchestrator service -> (delegation) -> LangGraph graph ->
agent -> tool-gateway -> llm-gateway -> trace rows + evidence rows, and the recovery paths:
crash + resume, revoked delegation, deadline, cancel, evidence POST failure.
Real Postgres, least-privilege logins, real checkpointer, fake model."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import httpx
import psycopg
import pytest
from fastapi import FastAPI

from .conftest import HostRouter, OrchFactory, People, Planted

pytestmark = pytest.mark.integration


def q(owner: psycopg.Connection, sql: str, *args: Any) -> list[tuple[Any, ...]]:
    return owner.execute(sql, args).fetchall()


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://orch")


async def start(
    app: FastAPI, token: str, incident: dict[str, Any], key: str | None = None, **body: Any
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"}
    if key:
        headers["Idempotency-Key"] = key
    async with client(app) as c:
        return await c.post(
            "/v1/investigations", json={"incident": incident["key"], **body}, headers=headers
        )


async def get(app: FastAPI, token: str, inv: str, suffix: str = "") -> dict[str, Any]:
    async with client(app) as c:
        r = await c.get(
            f"/v1/investigations/{inv}{suffix}", headers={"Authorization": f"Bearer {token}"}
        )
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


class Gate(httpx.AsyncBaseTransport):
    """The host router, plus: count agent calls; optionally HANG the next agent call (so a
    test can 'kill' the orchestrator mid-call) or fail evidence POSTs."""

    def __init__(self, apps: dict[str, FastAPI], *, hang_agent: bool = False,
                 evidence_down: bool = False) -> None:  # fmt: skip
        self.inner = HostRouter(apps)
        self.hang_agent = hang_agent
        self.evidence_down = evidence_down
        self.agent_calls = 0
        self.agent_entered = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "agents" and request.method == "POST":
            self.agent_calls += 1
            self.agent_entered.set()
            if self.hang_agent:
                await asyncio.Event().wait()  # until cancelled (shutdown / cancel / deadline)
        if self.evidence_down and request.url.path.endswith("/evidence"):
            return httpx.Response(503, text="incident-service down")
        return await self.inner.handle_async_request(request)


@pytest.fixture
def apps(incident_app: FastAPI, agents_app: FastAPI, api_app: FastAPI) -> dict[str, FastAPI]:
    return {"incident": incident_app, "agents": agents_app, "api": api_app}


async def test_investigation_end_to_end_with_delegation(
    make_orch: OrchFactory,
    people: People,
    incident: dict[str, Any],
    owner: psycopg.Connection,
    planted: Planted,
) -> None:
    uid = people.add("SRE")
    token = people.token(uid)
    async with make_orch() as app:
        r = await start(app, token, incident, key="key-happy-test-0001")
        assert r.status_code == 202, r.text
        body = r.json()
        inv = body["investigation_id"]
        assert r.headers["Location"] == f"/v1/investigations/{inv}"
        await app.state.engine.wait(UUID(inv))
        out = await get(app, token, inv)
        trace = await get(app, token, inv, "/trace")
    assert out["status"] == "COMPLETE", out
    assert [t["status"] for t in out["tasks"]] == ["SUCCEEDED"]
    assert out["tasks"][0]["attempt"] == 1 and out["graph_next"] == []
    assert out["evidence_inserted"] and out["evidence_inserted"] > 0
    ex = trace["executions"][0]
    assert ex["model"] == "fake-small" and ex["prompt_version"] == 2
    assert [m["role"] for m in ex["messages"]] == ["system", "user", "assistant"]
    assert "evidence" not in ex["output"] and ex["output"]["facts"]
    assert any("E_POOL" in f["statement"] for f in ex["output"]["facts"])

    # the incident was marked by the USER (incident-service), with a timeline entry
    assert q(owner, "SELECT status FROM incident.incidents WHERE id=%s", incident["id"])[0][0] == (
        "INVESTIGATING"
    )
    requested = q(owner, "SELECT actor FROM incident.incident_events WHERE incident_id=%s AND "
                  "event_type='InvestigationRequested'", incident["id"])  # fmt: skip
    assert requested == [(f"user:{uid}",)]
    # evidence rows exist and every fact's citation resolves to one
    keys = {r[0] for r in q(owner, "SELECT evidence_key FROM incident.evidence WHERE incident_id=%s",
                            incident["id"])}  # fmt: skip
    assert keys and all(
        {e["evidence_id"] for e in f["evidence"]} <= keys for f in ex["output"]["facts"]
    )
    # delegation: one grant, for this user and investigation, revoked at the end
    grant = q(
        owner,
        "SELECT id, user_id, actor, revoked_reason, tokens_issued FROM identity.delegation_grants "
        "WHERE investigation_id=%s",
        inv,
    )
    assert len(grant) == 1
    gid, guser, actor, revoked, issued = grant[0]
    assert guser == uid and actor == "service:orchestrator"
    assert revoked == "investigation COMPLETE" and issued >= 1
    events = [r[0] for r in q(owner, "SELECT event FROM identity.delegation_events WHERE grant_id=%s "
                              "ORDER BY created_at", gid)]  # fmt: skip
    assert events[0] == "CREATED" and events[-1] == "REVOKED" and "ISSUED" in events
    # tool calls: for the user, by the agent, bound to the investigation, grant in the audit
    calls = q(
        owner,
        "SELECT on_behalf_of, agent_name, investigation_id FROM tools.tool_calls WHERE incident_id=%s",
        incident["id"],
    )
    assert len(calls) == 2 and all(
        c[0] == uid and c[1] == "log_analysis" and str(c[2]) == inv for c in calls
    )
    audit = q(
        owner,
        "SELECT event->'details'->>'delegation_grant', event->'details'->>'delegated_to' "
        "FROM tools.audit_outbox WHERE event->'details'->>'investigation_id' = %s",
        inv,
    )
    assert audit and all(a == (str(gid), "service:orchestrator") for a in audit)
    # the checkpoint exists in OUR schema, under the investigation's thread
    assert (
        q(owner, "SELECT count(*) FROM orchestrator.checkpoints WHERE thread_id=%s", inv)[0][0] > 0
    )


async def test_start_is_idempotent_and_one_running_per_incident(
    make_orch: OrchFactory, people: People, incident: dict[str, Any], apps: dict[str, FastAPI]
) -> None:
    uid = people.add("SRE")
    token = people.token(uid)
    gate = Gate(apps, hang_agent=True)
    async with make_orch(transport=gate) as app:
        first = await start(app, token, incident, key="key1-test-0001")
        assert first.status_code == 202
        await asyncio.wait_for(gate.agent_entered.wait(), 30)
        replay = await start(app, token, incident, key="key1-test-0001")
        assert replay.status_code == 202 and replay.headers.get("Idempotent-Replayed") == "true"
        assert replay.json()["investigation_id"] == first.json()["investigation_id"]
        other = await start(app, token, incident, key="key2-test-0001")
        assert other.status_code == 409 and "already RUNNING" in other.text
        await app.state.engine.cancel(UUID(first.json()["investigation_id"]), "test over")


async def test_directory_not_the_token_decides(
    make_orch: OrchFactory, people: People, incident: dict[str, Any], owner: psycopg.Connection
) -> None:
    """A token CLAIMING SRE for a user whose directory role is MANAGER (stale/forged role
    claims): the exchange refuses, the investigation FAILS before any agent or tool runs."""
    uid = people.add("MANAGER")
    async with make_orch() as app:
        r = await start(app, people.token(uid, "SRE"), incident, key="key-dir-test-0001")
    assert r.status_code == 403 and "investigations:run" in r.text
    rows = q(
        owner,
        "SELECT status, error FROM orchestrator.investigations WHERE incident_id=%s",
        incident["id"],
    )
    assert rows and rows[0][0] == "FAILED" and "delegation refused" in rows[0][1]
    assert (
        q(owner, "SELECT count(*) FROM tools.tool_calls WHERE incident_id=%s", incident["id"])[0][0]
        == 0
    )
    assert (
        q(
            owner,
            "SELECT count(*) FROM identity.delegation_events WHERE user_id=%s AND event='DENIED'",
            uid,
        )[0][0]
        == 1
    )
    # review finding: the refusal comes BEFORE the incident is touched
    assert (
        q(owner, "SELECT status FROM incident.incidents WHERE id=%s", incident["id"])[0][0]
        == (incident["status"])
    )
    assert q(owner, "SELECT count(*) FROM incident.incident_events WHERE incident_id=%s AND "
             "event_type='InvestigationRequested'", incident["id"])[0][0] == 0  # fmt: skip


async def test_start_needs_an_idempotency_key(
    make_orch: OrchFactory, people: People, incident: dict[str, Any]
) -> None:
    uid = people.add("SRE")
    async with make_orch() as app:
        r = await start(app, people.token(uid), incident)
    assert r.status_code == 400 and "Idempotency-Key" in r.text


async def test_a_transient_exchange_failure_does_not_poison_the_key(
    make_orch: OrchFactory, people: People, incident: dict[str, Any], apps: dict[str, FastAPI]
) -> None:
    """Review finding: STS down once -> FAILED row kept the key -> every retry replayed FAILED."""

    class StsDownOnce(Gate):
        down = True

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.host == "api" and self.down:
                self.down = False
                return httpx.Response(503, json={"detail": "sts restarting"})
            return await super().handle_async_request(request)

    uid = people.add("SRE")
    async with make_orch(transport=StsDownOnce(apps)) as app:
        first = await start(app, people.token(uid), incident, key="key-poison-test-0001")
        assert first.status_code == 503, first.text
        retry = await start(app, people.token(uid), incident, key="key-poison-test-0001")
        assert retry.status_code == 202, retry.text
        assert "Idempotent-Replayed" not in retry.headers
        await app.state.engine.wait(UUID(retry.json()["investigation_id"]))
        out = await get(app, people.token(uid), retry.json()["investigation_id"])
    assert out["status"] == "COMPLETE"


async def test_transient_agent_failure_is_retried_not_recorded_as_failed(
    make_orch: OrchFactory, people: People, incident: dict[str, Any], apps: dict[str, FastAPI]
) -> None:
    """Review finding: one 503 from the agents worker became a permanent FAILED that resume
    would reuse forever. Now: retried in place (2x, backoff), recorded only when it lasts."""
    import aeoi_orchestrator.graph as graph

    class AgentsDownOnce(Gate):
        down = True

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.host == "agents" and self.down:
                self.down = False
                self.agent_calls += 1
                return httpx.Response(503, json={"detail": "worker restarting"})
            return await super().handle_async_request(request)

    graph.RETRY_BACKOFF_S = 0.05
    gate = AgentsDownOnce(apps)
    uid = people.add("SRE")
    async with make_orch(transport=gate) as app:
        inv = (await start(app, people.token(uid), incident, key="key-retry-test-0001")).json()[
            "investigation_id"
        ]
        await app.state.engine.wait(UUID(inv))
        out = await get(app, people.token(uid), inv)
    graph.RETRY_BACKOFF_S = 2.0
    assert out["status"] == "COMPLETE" and gate.agent_calls == 2
    assert out["tasks"][0]["attempt"] == 1  # same attempt: a retry, not a resume


async def test_trace_content_needs_what_the_agent_needed(
    make_orch: OrchFactory, people: People, incident: dict[str, Any]
) -> None:
    """Review finding: MANAGER/ADMIN have incidents:read but not logs:read - the trace must not
    hand them the log lines the agent read (prompts, clusters, facts text)."""
    sre, manager = people.add("SRE"), people.add("MANAGER")
    async with make_orch() as app:
        inv = (await start(app, people.token(sre), incident, key="key-trace-test-0001")).json()[
            "investigation_id"
        ]
        await app.state.engine.wait(UUID(inv))
        full = await get(app, people.token(sre), inv, "/trace")
        redacted = await get(app, people.token(manager, "MANAGER"), inv, "/trace")
    assert full["executions"][0]["messages"] and not full["executions"][0]["redacted"]
    r = redacted["executions"][0]
    assert r["redacted"] and r["messages"] == [] and r["output"] == {}
    assert r["model"] == full["executions"][0]["model"]  # metadata still visible


async def test_crash_mid_agent_call_resumes_from_checkpoint_without_the_user(
    make_orch: OrchFactory, people: People, incident: dict[str, Any], apps: dict[str, FastAPI],
    owner: psycopg.Connection,
) -> None:  # fmt: skip
    """Kill the orchestrator while the agent call is in flight. A NEW process resumes from the
    checkpoint (plan is not re-run), mints a fresh delegated token from the grant - no user
    token anywhere - and finishes. The task records attempt 2: the re-run is visible."""
    uid = people.add("SRE")
    token = people.token(uid)
    gate = Gate(apps, hang_agent=True)
    async with make_orch(transport=gate) as app:
        r = await start(app, token, incident, key="key-crash-test-0001")
        inv = r.json()["investigation_id"]
        await asyncio.wait_for(gate.agent_entered.wait(), 30)
        assert await app.state.engine.next_nodes(UUID(inv)) == ["run_agent"]
    # process gone: the rows stay RUNNING on purpose
    assert (
        q(owner, "SELECT status FROM orchestrator.investigations WHERE id=%s", inv)[0][0]
        == "RUNNING"
    )
    assert (
        q(owner, "SELECT status FROM orchestrator.tasks WHERE investigation_id=%s", inv)[0][0]
        == "RUNNING"
    )

    healthy = Gate(apps)
    async with make_orch(transport=healthy, resume_on_startup=True) as app2:
        await app2.state.engine.wait(UUID(inv))
        out = await get(app2, token, inv)
    assert out["status"] == "COMPLETE", out
    assert out["tasks"][0]["attempt"] == 2 and healthy.agent_calls == 1
    assert q(owner, "SELECT count(*) FROM orchestrator.agent_executions e JOIN orchestrator.tasks t "
             "ON t.id = e.task_id WHERE t.investigation_id=%s", inv)[0][0] == 1  # fmt: skip
    # the new process got its token by REFRESH (no user token): an ISSUED event says so
    assert q(owner, "SELECT count(*) FROM identity.delegation_events e JOIN identity.delegation_grants g "
             "ON g.id = e.grant_id WHERE g.investigation_id=%s AND e.detail='refresh'", inv)[0][0] >= 1  # fmt: skip


async def test_crash_after_run_agent_checkpoint_skips_the_agent(
    make_orch: OrchFactory, people: People, incident: dict[str, Any], apps: dict[str, FastAPI],
    owner: psycopg.Connection,
) -> None:  # fmt: skip
    """Kill while posting evidence (after run_agent's checkpoint). Resume runs only
    collect_evidence + finalize: the agent (and its tool calls) are not repeated."""
    uid = people.add("SRE")
    token = people.token(uid)

    class HangEvidence(Gate):
        entered = asyncio.Event()

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/evidence"):
                self.entered.set()
                await asyncio.Event().wait()
            return await super().handle_async_request(request)

    first = HangEvidence(apps)
    async with make_orch(transport=first) as app:
        inv = (await start(app, token, incident, key="key-ev-test-0001")).json()["investigation_id"]
        await asyncio.wait_for(first.entered.wait(), 60)
    tool_calls = q(owner, "SELECT count(*) FROM tools.tool_calls WHERE investigation_id=%s", inv)[
        0
    ][0]
    assert first.agent_calls == 1 and tool_calls == 2

    again = Gate(apps)
    async with make_orch(transport=again, resume_on_startup=True) as app2:
        await app2.state.engine.wait(UUID(inv))
        out = await get(app2, token, inv)
    assert out["status"] == "COMPLETE" and out["evidence_inserted"] > 0
    assert again.agent_calls == 0  # the recorded result was reused
    assert (
        q(owner, "SELECT count(*) FROM tools.tool_calls WHERE investigation_id=%s", inv)[0][0] == 2
    )


async def test_crash_between_record_and_checkpoint_reuses_the_recorded_execution(
    make_orch: OrchFactory, people: People, incident: dict[str, Any], apps: dict[str, FastAPI],
    owner: psycopg.Connection,
) -> None:  # fmt: skip
    """The narrowest window: the execution row is written, then the process dies BEFORE the
    node's checkpoint. Resume re-runs run_agent; the idempotency guard must return the
    recorded execution instead of calling the agent (and the tools) a second time."""
    uid = people.add("SRE")
    token = people.token(uid)
    recorded = asyncio.Event()
    async with make_orch(transport=Gate(apps)) as app:
        store = app.state.store
        original = store.record

        async def record_then_die(*a: Any, **k: Any) -> Any:
            out = await original(*a, **k)
            recorded.set()
            await asyncio.Event().wait()  # killed here: the checkpoint is never written
            return out

        store.record = record_then_die
        inv = (await start(app, token, incident, key="key-window-test-0001")).json()[
            "investigation_id"
        ]
        await asyncio.wait_for(recorded.wait(), 60)
        assert await app.state.engine.next_nodes(UUID(inv)) == ["run_agent"]
    calls_before = q(owner, "SELECT count(*) FROM tools.tool_calls WHERE investigation_id=%s", inv)
    again = Gate(apps)
    async with make_orch(transport=again, resume_on_startup=True) as app2:
        await app2.state.engine.wait(UUID(inv))
        out = await get(app2, token, inv)
    assert out["status"] == "COMPLETE", out
    assert again.agent_calls == 0, "the agent was called again for a recorded task"
    assert q(owner, "SELECT count(*) FROM tools.tool_calls WHERE investigation_id=%s", inv) == (
        calls_before
    )


async def test_deactivated_user_stops_a_resumed_run(
    make_orch: OrchFactory, people: People, incident: dict[str, Any], apps: dict[str, FastAPI],
    owner: psycopg.Connection,
) -> None:  # fmt: skip
    """The user is deactivated while the orchestrator is down. On resume the refresh is
    refused (directory re-check), the grant is revoked, the run FAILS with that reason."""
    uid = people.add("SRE")
    token = people.token(uid)
    gate = Gate(apps, hang_agent=True)
    async with make_orch(transport=gate) as app:
        inv = (await start(app, token, incident, key="key-deact-test-0001")).json()[
            "investigation_id"
        ]
        await asyncio.wait_for(gate.agent_entered.wait(), 30)
    owner.execute("UPDATE identity.users SET is_active = false WHERE id=%s", (uid,))
    healthy = Gate(apps)
    async with make_orch(transport=healthy, resume_on_startup=True) as app2:
        await app2.state.engine.wait(UUID(inv))
    status, error = q(
        owner, "SELECT status, error FROM orchestrator.investigations WHERE id=%s", inv
    )[0]
    assert status == "FAILED" and "delegation refused" in error and "deactivated" in error
    assert healthy.agent_calls == 0
    assert q(owner, "SELECT revoked_reason FROM identity.delegation_grants WHERE investigation_id=%s",
             inv)[0][0] == "user deactivated"  # fmt: skip


async def test_deadline_fails_the_investigation_and_revokes(
    make_orch: OrchFactory, people: People, incident: dict[str, Any], apps: dict[str, FastAPI],
    owner: psycopg.Connection,
) -> None:  # fmt: skip
    uid = people.add("SRE")
    gate = Gate(apps, hang_agent=True)
    async with make_orch(transport=gate, investigation_deadline_s=2.0) as app:
        inv = (await start(app, people.token(uid), incident, key="key-dl-test-0001")).json()[
            "investigation_id"
        ]
        await app.state.engine.wait(UUID(inv))
    status, error = q(
        owner, "SELECT status, error FROM orchestrator.investigations WHERE id=%s", inv
    )[0]
    assert status == "FAILED" and "deadline" in error
    assert (
        q(owner, "SELECT status FROM orchestrator.tasks WHERE investigation_id=%s", inv)[0][0]
        == "TIMED_OUT"
    )
    assert q(owner, "SELECT revoked_at IS NOT NULL FROM identity.delegation_grants WHERE "
             "investigation_id=%s", inv)[0][0]  # fmt: skip


async def test_cancel_by_requester_revokes_and_others_cannot(
    make_orch: OrchFactory, people: People, incident: dict[str, Any], apps: dict[str, FastAPI],
    owner: psycopg.Connection,
) -> None:  # fmt: skip
    uid, other = people.add("SRE"), people.add("SRE")
    gate = Gate(apps, hang_agent=True)
    async with make_orch(transport=gate) as app:
        inv = (await start(app, people.token(uid), incident, key="key-c-test-0001")).json()[
            "investigation_id"
        ]
        await asyncio.wait_for(gate.agent_entered.wait(), 30)
        async with client(app) as c:
            denied = await c.post(f"/v1/investigations/{inv}/cancel", json={"reason": "not mine"},
                                  headers={"Authorization": f"Bearer {people.token(other)}"})  # fmt: skip
            ok = await c.post(f"/v1/investigations/{inv}/cancel", json={"reason": "wrong incident"},
                              headers={"Authorization": f"Bearer {people.token(uid)}"})  # fmt: skip
    assert denied.status_code == 403
    assert ok.status_code == 200 and ok.json()["status"] == "CANCELLED", ok.text
    assert (
        q(owner, "SELECT status FROM orchestrator.tasks WHERE investigation_id=%s", inv)[0][0]
        == "CANCELLED"
    )
    reason = q(
        owner,
        "SELECT revoked_reason FROM identity.delegation_grants WHERE investigation_id=%s",
        inv,
    )
    assert reason[0][0].startswith("cancelled: wrong incident")


async def test_evidence_failure_is_failed_and_repost_completes(
    make_orch: OrchFactory, people: People, incident: dict[str, Any], apps: dict[str, FastAPI],
    owner: psycopg.Connection, orch_db_url: str, orch_token: str,
) -> None:  # fmt: skip
    from pydantic import SecretStr
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from aeoi_models.api.agents import EvidenceItem
    from aeoi_orchestrator.clients import IncidentClient
    from aeoi_orchestrator.config import Settings
    from aeoi_orchestrator.store import Store

    uid = people.add("SRE")
    async with make_orch(transport=Gate(apps, evidence_down=True)) as app:
        inv = (await start(app, people.token(uid), incident, key="key-e-test-0001")).json()[
            "investigation_id"
        ]
        await app.state.engine.wait(UUID(inv))
    status, error = q(
        owner, "SELECT status, error FROM orchestrator.investigations WHERE id=%s", inv
    )[0]
    assert status == "FAILED" and "evidence was not stored" in error
    # the CLI's repost path: recorded batch -> idempotent endpoint -> COMPLETE
    s = Settings(db_url_override=SecretStr(orch_db_url), environment="test")  # type: ignore[call-arg]
    engine = create_async_engine(s.sqlalchemy_url(), pool_size=1)
    store = Store(async_sessionmaker(engine, expire_on_commit=False))
    async with httpx.AsyncClient(transport=HostRouter(apps)) as http:
        incidents = IncidentClient(http, "http://incident", orch_token)
        task = (await store.tasks(UUID(inv)))[0]
        items = [EvidenceItem.model_validate(e) for e in await store.evidence_of(task["task_id"])]
        inserted = await incidents.post_evidence(
            incident["id"], UUID(inv), "log_analysis", items, "r"
        )
        assert inserted == len(items) > 0
        assert await store.complete_after_repost(UUID(inv))
        assert (
            await incidents.post_evidence(incident["id"], UUID(inv), "log_analysis", items, "r")
            == 0
        )
    await engine.dispose()
    assert (
        q(owner, "SELECT status FROM orchestrator.investigations WHERE id=%s", inv)[0][0]
        == "COMPLETE"
    )


async def test_startup_with_no_grant_or_past_deadline_fails_cleanly(
    make_orch: OrchFactory, incident: dict[str, Any], owner: psycopg.Connection
) -> None:
    """Rows left by a crash during start (no grant) or long ago (deadline passed) are closed
    with the reason on the next start, not resumed and not left RUNNING."""
    from uuid import uuid4

    a = uuid4()
    owner.execute(
        "INSERT INTO orchestrator.investigations (id, incident_id, status, requested_by, budget_usd, "
        "deadline_at) VALUES (%s, %s, 'RUNNING', 'user:x', 1, now() + interval '1 hour')",
        (a, incident["id"]),
    )
    async with make_orch(resume_on_startup=True):
        pass
    status, error = q(
        owner, "SELECT status, error FROM orchestrator.investigations WHERE id=%s", a
    )[0]
    assert status == "FAILED" and "before the delegation" in error
