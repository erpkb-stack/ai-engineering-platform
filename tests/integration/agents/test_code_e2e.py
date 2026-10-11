"""Phase 14 gate, end to end (ADR-024): evidence -> code (compare_commits AS THE USER via the
tool gateway -> the planted deploy's diff in devdata) -> code rules find the query added in a
loop -> COMMIT evidence stored -> one more independent source for the deploy hypothesis (the
rubric is unchanged). Real Postgres and logins; no model reads code."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import psycopg
import pytest
from fastapi import FastAPI

from aeoi_synth import codegen

from .conftest import OrchFactory, People, Planted, Tokens
from .test_multi_agent_e2e import ALL, Faults, incident10  # noqa: F401  (fixture)
from .test_orchestrator_e2e import get, q, start
from .test_postmortem_e2e import call
from .test_reasoning_e2e import LABELS, RANKING, critique, script

pytestmark = pytest.mark.integration
PATH = "src/main/java/com/northwind/checkout/CartService.java"


def plant_n_plus_one(owner: psycopg.Connection, sha: str) -> None:
    before = codegen._java_cart_service("com.northwind.checkout")
    start_ = before.index("    public CartTotal priceCart(Cart cart) {")
    end = before.index("    }\n", before.index("return new CartTotal", start_)) + len("    }\n")
    after = before[:start_] + codegen.CART_AFTER_812 + before[end:]
    patch, adds, dels = codegen._diff(PATH, before, after)
    owner.execute(
        "INSERT INTO devdata.commit_diffs VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (sha, PATH, patch, after, adds, dels),
    )


async def test_code_end_to_end_finds_the_loop_query_and_supports_the_deploy(
    make_orch: OrchFactory, people: People, incident10: dict[str, Any], planted: Planted,  # noqa: F811
    owner: psycopg.Connection, incident_app: FastAPI, llm_app: FastAPI, tok: Tokens,
) -> None:  # fmt: skip
    plant_n_plus_one(owner, planted.sha)
    script(llm_app, LABELS, RANKING, critic=critique())  # no model call for code
    token = people.token(people.add("SRE"))
    async with make_orch(agents=ALL, reasoning=True, code=True) as app:
        inv = (await start(app, token, incident10, "p14-ok-run")).json()["investigation_id"]
        await app.state.engine.wait(UUID(inv))
        out = await get(app, token, inv)
        trace = await get(app, token, inv, "/trace")
        mgr_trace = await get(app, tok.user("MANAGER"), inv, "/trace")
        mgr_run = await get(app, tok.user("MANAGER"), inv)
    assert out["status"] == "COMPLETE", (out["error"], out["plan"].get("code"))
    c = out["plan"]["code"]
    assert c["findings_by_rule"] == {"query_in_loop": 1} and c["degraded"] is False
    assert (c["deploys_checked"], c["commits_checked"]) == (1, 1) and c["post_error"] is None
    # the plan and the run's error are readable by every incident reader: counts only (review)
    assert "findOpenOrders" not in str(mgr_run) and "CartService" not in str(mgr_run)
    # the agent read code AS THE USER: its trace holds diffs -> MANAGER (no code:read) sees none
    assert {e["agent"]: e["redacted"] for e in trace["executions"]}["code_intelligence"] is False
    assert {e["agent"]: e["redacted"] for e in mgr_trace["executions"]}["code_intelligence"] is True

    iid = incident10["id"]
    ev = q(owner, "SELECT evidence_key, excerpt FROM incident.evidence WHERE incident_id=%s "
           "AND kind='COMMIT'", iid)  # fmt: skip
    assert len(ev) == 1 and "findOpenOrders(item.getSku())" in ev[0][1]
    hyps = (await call(incident_app, "GET", f"/v1/incidents/{iid}/hypotheses",
                       tok.user("SRE"))).json()  # fmt: skip
    deploy = next(x for x in hyps if x["detail"].get("cause_kind") == "change")
    assert ev[0][0] in str(deploy["edges"])
    assert "code_intelligence" in str(deploy["detail"])
    assert deploy["confidence"] == "HIGH"  # code is one more source, never a band override
    others = [x for x in hyps if x["detail"].get("cause_kind") != "change"]
    assert all(ev[0][0] not in str(x["edges"]) for x in others)
    # MANAGER may read the incident, not the commit
    m = (await call(incident_app, "GET", f"/v1/incidents/{iid}/evidence",
                    tok.user("MANAGER"))).json()  # fmt: skip
    assert "findOpenOrders" not in str(m)


async def test_code_agent_down_is_partial_named_and_reasoning_still_runs(
    make_orch: OrchFactory, people: People, incident10: dict[str, Any],  # noqa: F811
    incident_app: FastAPI, agents_app: FastAPI, api_app: FastAPI, llm_app: FastAPI,
) -> None:  # fmt: skip
    script(llm_app, LABELS, RANKING, critic=critique())
    apps = {"incident": incident_app, "agents": agents_app, "api": api_app}
    token = people.token(people.add("SRE"))
    faults = Faults(apps, fail=("code_intelligence",))
    async with make_orch(agents=ALL, reasoning=True, code=True, transport=faults) as app:
        inv = (await start(app, token, incident10, "p14-down-run")).json()["investigation_id"]
        await app.state.engine.wait(UUID(inv))
        out = await get(app, token, inv)
    assert "code_intelligence" in faults.calls
    assert out["status"] == "PARTIAL" and "code_intelligence" in (out["error"] or "")
    assert out["plan"]["hypotheses"]["kept"] >= 1  # hypotheses did not depend on code


async def test_crash_after_code_resumes_without_rereading_code_or_double_storing(
    make_orch: OrchFactory, people: People, incident10: dict[str, Any], planted: Planted,  # noqa: F811
    incident_app: FastAPI, agents_app: FastAPI, api_app: FastAPI, owner: psycopg.Connection,
    llm_app: FastAPI,
) -> None:  # fmt: skip
    """A kill while the ranker runs (code already done): resume never calls the code agent or
    compare_commits again, the COMMIT evidence is stored once, the deploy keeps its support."""
    plant_n_plus_one(owner, planted.sha)
    script(llm_app, LABELS, RANKING, critic=critique())
    apps = {"incident": incident_app, "agents": agents_app, "api": api_app}
    token = people.token(people.add("SRE"))
    first = Faults(apps, hang=("hypothesis",))
    async with make_orch(agents=ALL, reasoning=True, code=True, transport=first) as app:
        inv = (await start(app, token, incident10, "p14-crash-run")).json()["investigation_id"]
        await asyncio.wait_for(first.hung.wait(), 30)
    assert "code_intelligence" in first.calls
    healthy = Faults(apps)
    async with make_orch(agents=ALL, reasoning=True, code=True, transport=healthy,
                         resume_on_startup=True) as app2:  # fmt: skip
        await app2.state.engine.wait(UUID(inv))
        out = await get(app2, token, inv)
    assert out["status"] == "COMPLETE", out
    assert "code_intelligence" not in healthy.calls  # code was NOT run again
    compares = q(owner, "SELECT count(*) FROM tools.tool_calls WHERE investigation_id=%s "
                 "AND tool_name='compare_commits'", inv)[0][0]  # fmt: skip
    assert compares == 1
    ev = q(owner, "SELECT evidence_key FROM incident.evidence WHERE incident_id=%s "
           "AND kind='COMMIT'", incident10["id"])  # fmt: skip
    assert len(ev) == 1
    hyps = (await call(incident_app, "GET", f"/v1/incidents/{incident10['id']}/hypotheses",
                       people.token(people.add("SRE")))).json()  # fmt: skip
    deploy = next(x for x in hyps if x["detail"].get("cause_kind") == "change")
    assert ev[0][0] in str(deploy["edges"])
