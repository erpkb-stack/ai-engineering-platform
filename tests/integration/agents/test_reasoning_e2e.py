"""Phase 11 gate, end to end: evidence agents -> hypothesize (code candidates, LLM rank) ->
critique (LLM, independent) -> validate (code) -> incident.hypotheses + evidence edges.
Real Postgres, real checkpointer, least-privilege logins; the model is the gateway's FAKE
provider with scripted structured answers (order: log labels, ranking, critique)."""

from __future__ import annotations

import asyncio
import secrets
from typing import Any
from uuid import UUID

import httpx
import psycopg
import pytest
from fastapi import FastAPI

from .conftest import OrchFactory, People, Planted, Tokens
from .test_multi_agent_e2e import ALL, Faults, incident10  # noqa: F401  (fixture)
from .test_orchestrator_e2e import get, q, start

pytestmark = pytest.mark.integration
RANKER_MARKER = "ranker-prose-marker"  # must never reach the critic


@pytest.fixture
def apps(incident_app: FastAPI, agents_app: FastAPI, api_app: FastAPI) -> dict[str, FastAPI]:
    return {"incident": incident_app, "agents": agents_app, "api": api_app}


def script(llm_app: FastAPI, *answers: dict[str, Any], critic: dict[str, Any] | None = None) -> Any:
    """`fast` (log labels, ranking) -> local_fake; `reasoning` (critic) -> hosted_fake: the
    test routing mirrors the owner's decision - the critic runs on a DIFFERENT model."""
    local = llm_app.state.gateway.runtimes["local_fake"].provider
    local.structured.extend(answers)
    hosted = llm_app.state.gateway.runtimes["hosted_fake"].provider
    if critic is not None:
        hosted.structured.append(critic)
    return hosted


LABELS = {"clusters": []}  # the log agent's call: rule labels are fine here
RANKING = {"ranking": [
    {"id": "h1", "explanation": f"{RANKER_MARKER}: the deploy preceded the pool errors.",
     "refs": ["o1", "o2"]},
    {"id": "h2", "explanation": "Pool saturation came late.", "refs": []},
]}  # fmt: skip


def critique(**kw: Any) -> dict[str, Any]:
    return {
        "reviews": [
            {"id": "h1", "verdict": "supported", "contradicting": [], "missing": "the PR diff"},
            {"id": "h2", "verdict": "refuted", "contradicting": ["o1"], "missing": "pool size"},
        ],
        "alternatives": [
            # declared refinement citing h1's deploy (o1) -> stored as h1's refinement
            {"statement": "Error logs began with the deploy, not with pool load.",
             "supporting": ["o1", "o2"], "refines": "h1"},
            # a rival cause -> its own hypothesis h3
            {"statement": "orders-db slowed down and held connections longer.",
             "supporting": ["o2", "o3"], "refines": ""},
        ],
        "no_alternative_reason": "",
        **kw,
    }  # fmt: skip


async def hypotheses(incident_app: FastAPI, token: str, incident_id: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=incident_app), base_url="http://incident"
    ) as c:
        r = await c.get(f"/v1/incidents/{incident_id}/hypotheses",
                        headers={"Authorization": f"Bearer {token}"})  # fmt: skip
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


async def test_hypotheses_ranked_critiqued_validated_and_stored(
    make_orch: OrchFactory, people: People, incident10: dict[str, Any], planted: Planted,  # noqa: F811
    owner: psycopg.Connection, incident_app: FastAPI, llm_app: FastAPI, tok: Tokens,
) -> None:  # fmt: skip
    fake = script(llm_app, LABELS, RANKING, critic=critique())
    token = people.token(people.add("SRE"))
    async with make_orch(agents=ALL, reasoning=True) as app:
        inv = (await start(app, token, incident10, key="key-p11-ok-0001")).json()[
            "investigation_id"
        ]
        await app.state.engine.wait(UUID(inv))
        out = await get(app, token, inv)
        trace = await get(app, token, inv, "/trace")
        mgr_trace = await get(app, tok.user("MANAGER"), inv, "/trace")
    assert out["status"] == "COMPLETE", (out["error"], out["plan"].get("hypotheses"))
    agents = {t["agent"]: t["status"] for t in out["tasks"]}
    assert agents["hypothesis"] == "SUCCEEDED" and agents["critic"] == "SUCCEEDED"
    v = out["plan"]["hypotheses"]
    assert v["passed"] and v["kept"] == 3 and v["validated"] == 1 and v["dropped"] == []

    hs = await hypotheses(incident_app, tok.user("SRE"), incident10["id"])
    by_key = {h["key"]: h for h in hs}
    h1, h2, h3 = by_key["h1"], by_key["h2"], by_key["h3"]
    # code candidate, rubric band, LLM rank + explanation (cited), critic verdict
    assert planted.deploy_key in h1["statement"] and h1["confidence"] == "HIGH"
    assert h1["rank"] == 1 and h1["status"] == "VALIDATED" and h1["detail"]["ranked_by"] == "llm"
    assert RANKER_MARKER in h1["detail"]["explanation"]
    # pool saturation: the critic "refuted" it citing the DEPLOY, which cannot contradict a
    # saturation cause -> disputed (shown by name), band and edges unchanged, not cleared
    assert h2["status"] == "PROPOSED" and h2["detail"]["critic_verdict"] == "refuted"
    assert h2["detail"]["critic_disputed"] == [planted.deploy_key]
    assert all(e["stance"] == "SUPPORTS" or planted.deploy_key not in e["evidence_key"]
               for e in h2["edges"])  # fmt: skip
    assert "Error logs began" in h1["detail"]["refinement"]
    assert h2["detail"]["explanation"] is None  # uncited prose is not stored
    assert h3["produced_by"] == "critic" and h3["detail"]["origin"] == "critic"
    assert h3["confidence"] in ("LOW", "MEDIUM")  # capped: never HIGH for a critic alternative
    # every edge points at evidence stored on THIS incident
    kinds = {e["kind"] for x in hs for e in x["edges"]}
    assert {"DEPLOY", "LOG", "METRIC"} <= kinds
    edges = q(owner, "SELECT count(*) FROM incident.hypothesis_evidence he JOIN incident.evidence "
              "e ON e.id = he.evidence_id WHERE e.incident_id=%s", incident10["id"])[0][0]  # fmt: skip
    assert edges == sum(len(x["edges"]) for x in hs)
    # the critic never saw the ranker's explanation (independence) - check what reached the model
    critic_req = fake.calls[-1]
    assert RANKER_MARKER not in critic_req.messages[0].content
    # the critic ran on a DIFFERENT model than the ranker (owner decision), fallback off
    models = {t["agent"]: t["model"] for t in out["tasks"]}
    assert models["critic"] == "fake-large" and models["hypothesis"] == "fake-small"
    assert h3["status"] == "PROPOSED"  # a critic alternative was never reviewed: not VALIDATED
    # MANAGER: code conclusions yes; raw evidence edges and MODEL-written text no
    mgr = {
        x["key"]: x for x in await hypotheses(incident_app, tok.user("MANAGER"), incident10["id"])
    }
    assert mgr["h1"]["statement"] == h1["statement"]  # code-written conclusion
    assert all(x["edges"] == [] and x["hidden_edges"] > 0 for x in mgr.values())
    assert "redacted" in mgr["h1"]["detail"]["explanation"] and RANKER_MARKER not in str(mgr)
    assert "redacted" in mgr["h3"]["statement"]  # the critic's alternative is model text
    assert "redacted" in mgr["h1"]["detail"]["refinement"]  # so is a refinement
    redacted = {e["agent"]: e["redacted"] for e in mgr_trace["executions"]}
    assert redacted["hypothesis"] and redacted["critic"]
    assert not any(e["redacted"] for e in trace["executions"])
    # timeline + outbox event
    assert q(owner, "SELECT count(*) FROM incident.incident_events WHERE incident_id=%s AND "
             "event_type='HypothesisCreated'", incident10["id"])[0][0] == 1  # fmt: skip


async def test_critic_down_means_partial_and_hypotheses_still_stored(
    make_orch: OrchFactory, people: People, incident10: dict[str, Any],  # noqa: F811
    apps: dict[str, FastAPI], incident_app: FastAPI, llm_app: FastAPI, tok: Tokens,
) -> None:  # fmt: skip
    script(llm_app, LABELS, RANKING)
    token = people.token(people.add("SRE"))
    async with make_orch(agents=ALL, reasoning=True,
                         transport=Faults(apps, fail=("critic",))) as app:  # fmt: skip
        inv = (await start(app, token, incident10, key="key-p11-crit-0001")).json()[
            "investigation_id"
        ]
        await app.state.engine.wait(UUID(inv))
        out = await get(app, token, inv)
    assert out["status"] == "PARTIAL", out
    assert "critic:" in out["error"]
    hs = await hypotheses(incident_app, tok.user("SRE"), incident10["id"])
    assert hs and all(h["detail"]["critic_verdict"] == "not_reviewed" for h in hs)


async def test_no_symptom_is_inconclusive(
    make_orch: OrchFactory, people: People, incident_app: FastAPI, tok: Tokens,
    llm_app: FastAPI,
) -> None:  # fmt: skip
    """A service with no telemetry: nothing to explain -> INCONCLUSIVE, not a made-up cause."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=incident_app), base_url="http://incident"
    ) as c:
        r = await c.post("/v1/incidents", json={
            "title": "Quiet service", "severity": "SEV3",
            "affected_services": [f"quiet-{secrets.token_hex(3)}"],
            "detected_at": "2026-10-02T09:50:00Z",
        }, headers={**tok.h("SRE"), "Idempotency-Key": secrets.token_hex(8)})  # fmt: skip
    quiet = r.json()
    token = people.token(people.add("SRE"))
    async with make_orch(agents=("metrics", "deployment"), reasoning=True) as app:
        inv = (await start(app, token, quiet, key="key-p11-quiet-0001")).json()["investigation_id"]
        await app.state.engine.wait(UUID(inv))
        out = await get(app, token, inv)
    assert out["status"] == "INCONCLUSIVE", out
    assert "no hypothesis is supported by the evidence" in out["error"]
    assert await hypotheses(incident_app, tok.user("SRE"), quiet["id"]) == []


async def test_crash_during_critique_reuses_the_ranking_and_stores_once(
    make_orch: OrchFactory, people: People, incident10: dict[str, Any],  # noqa: F811
    apps: dict[str, FastAPI], owner: psycopg.Connection, llm_app: FastAPI,
) -> None:  # fmt: skip
    script(llm_app, LABELS, RANKING, critic=critique())
    token = people.token(people.add("SRE"))
    first = Faults(apps, hang=("critic",))
    async with make_orch(agents=ALL, reasoning=True, transport=first) as app:
        inv = (await start(app, token, incident10, key="key-p11-crash-0001")).json()[
            "investigation_id"
        ]
        await asyncio.wait_for(first.hung.wait(), 30)
    healthy = Faults(apps)
    async with make_orch(agents=ALL, reasoning=True, transport=healthy,
                         resume_on_startup=True) as app2:  # fmt: skip
        await app2.state.engine.wait(UUID(inv))
        out = await get(app2, token, inv)
    assert out["status"] == "COMPLETE", out
    assert healthy.calls == ["critic"]  # evidence agents and the ranker were not called again
    attempts = {t["agent"]: t["attempt"] for t in out["tasks"]}
    assert attempts["hypothesis"] == 1 and attempts["critic"] == 2
    n = q(owner, "SELECT count(*) FROM incident.hypotheses WHERE investigation_id=%s", inv)[0][0]
    assert n == 3


async def test_hypotheses_must_cite_stored_evidence(
    incident_app: FastAPI, incident10: dict[str, Any], tok: Tokens,  # noqa: F811
) -> None:  # fmt: skip
    """incident-service refuses edges to evidence that is not on the incident (422, nothing
    written) - the evidence graph cannot point at nothing, even if the orchestrator is wrong."""
    from aeoi_security.testing import service_token_for

    svc = service_token_for(tok.keys, "orchestrator", "hypotheses:write")
    body = {"investigation_id": "01a11d00-0000-7000-8000-000000000001", "items": [{
        "key": "h1", "statement": "made up", "confidence": "HIGH", "status": "VALIDATED",
        "rank": 1, "produced_by": "hypothesis", "supports": ["LOG-" + "0" * 32 + "-1"],
    }]}  # fmt: skip
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=incident_app), base_url="http://incident"
    ) as c:
        r = await c.post(f"/v1/incidents/{incident10['id']}/hypotheses", json=body,
                         headers={"Authorization": f"Bearer {svc}"})  # fmt: skip
        assert r.status_code == 422 and "not stored" in r.text
        r = await c.post(f"/v1/incidents/{incident10['id']}/hypotheses", json=body,
                         headers=tok.h("INCIDENT_COMMANDER"))  # fmt: skip
        assert r.status_code == 403  # users never write hypotheses


async def test_crash_after_the_ranking_is_recorded_does_not_rank_twice(
    make_orch: OrchFactory, people: People, incident10: dict[str, Any],  # noqa: F811
    apps: dict[str, FastAPI], llm_app: FastAPI,
) -> None:  # fmt: skip
    """The narrow window: the hypothesis execution row is written, the process dies BEFORE the
    node's checkpoint. Resume re-runs hypothesize; the reuse guard must return the recorded
    ranking instead of calling the model again (a second ranking could differ)."""
    script(llm_app, LABELS, RANKING, critic=critique())
    token = people.token(people.add("SRE"))
    recorded = asyncio.Event()
    async with make_orch(agents=ALL, reasoning=True, transport=Faults(apps)) as app:
        store = app.state.store
        original = store.record

        async def record_then_die(task_id: Any, agent: str, *a: Any, **k: Any) -> Any:
            out = await original(task_id, agent, *a, **k)
            if agent == "hypothesis":
                recorded.set()
                await asyncio.Event().wait()  # killed here: no checkpoint for hypothesize
            return out

        store.record = record_then_die
        inv = (await start(app, token, incident10, key="key-p11-window-0001")).json()[
            "investigation_id"
        ]
        await asyncio.wait_for(recorded.wait(), 60)
        assert await app.state.engine.next_nodes(UUID(inv)) == ["hypothesize"]
    again = Faults(apps)
    async with make_orch(agents=ALL, reasoning=True, transport=again,
                         resume_on_startup=True) as app2:  # fmt: skip
        await app2.state.engine.wait(UUID(inv))
        out = await get(app2, token, inv)
    assert out["status"] == "COMPLETE", out
    assert again.calls == ["critic"], "the ranker was called again for a recorded task"
