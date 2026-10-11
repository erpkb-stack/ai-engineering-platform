"""Phase 13 gate, end to end (ADR-023): evidence -> history (search_incidents AS THE USER via
the tool gateway -> rag stub) -> only past incidents shared with every engineer are kept,
compared (fake Haiku, claims checked by code) and stored as INCIDENT pointers -> priors on the
hypotheses (shown, tie-break only) -> postmortem section. Real Postgres and logins."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import psycopg
import pytest
from fastapi import FastAPI

from ..tools.conftest import StubRag
from .conftest import OrchFactory, People, Planted, Tokens
from .test_multi_agent_e2e import ALL, Faults, incident10  # noqa: F401  (fixture)
from .test_orchestrator_e2e import get, q, start
from .test_postmortem_e2e import call, narrative
from .test_reasoning_e2e import LABELS, RANKING, critique, script

pytestmark = pytest.mark.integration


def past(
    key: str, svc: str, groups_all: bool, cat: str = "connection_pool_exhaustion"
) -> dict[str, Any]:
    return {
        "incident_key": key, "title": f"{svc} returning HTTP 500 after deployment",
        "summary": f"HTTP 5xx rose and latency increased. Error code ERR_POOL_TIMEOUT observed on {svc}.",
        "root_cause": "A code change opened one database connection per item.",
        "root_cause_category": cat, "remediation": "Rolled back the deployment.",
        "service_keys": [svc], "severity": "SEV2", "occurred_at": "2025-08-18T10:00:00Z",
        "resolved_at": "2025-08-18T11:00:00Z", "score": 0.03, "rank_k": 1, "rank_v": 1,
        "retrieval": "hybrid", "open_to_engineering": groups_all,
    }  # fmt: skip


def hits(svc: str) -> list[dict[str, Any]]:
    return [
        past("INC-2911", svc, True),  # the planted service
        past("INC-9001", svc, False, "certificate_expiry"),  # security-team only
        past("INC-4822", "orders-api", True),
    ]


COMPARE = {"comparisons": [
    {"incident": "INC-2911", "claims": [
        {"field": "service", "symptom": "", "relation": "same"},
        {"field": "deploy_before", "symptom": "", "relation": "same"}]},
    {"incident": "INC-4822", "claims": [
        {"field": "service", "symptom": "", "relation": "same"}]},  # wrong: dropped by code
]}  # fmt: skip


async def test_history_end_to_end_shared_only_pointers_priors_and_postmortem(
    make_orch: OrchFactory, people: People, incident10: dict[str, Any], planted: Planted,  # noqa: F811
    owner: psycopg.Connection, incident_app: FastAPI, llm_app: FastAPI, tok: Tokens,
    stub_rag: StubRag,
) -> None:  # fmt: skip
    stub_rag.incidents = hits(planted.svc)
    # fast route, in call order: log labels, history comparison, ranking
    hosted = script(llm_app, LABELS, COMPARE, RANKING, critic=critique())
    hosted.structured.append(narrative(planted.deploy_key))
    token = people.token(people.add("SRE"))
    async with make_orch(agents=ALL, reasoning=True, postmortem=True, history=True) as app:
        inv = (await start(app, token, incident10, "p13-ok-run")).json()["investigation_id"]
        await app.state.engine.wait(UUID(inv))
        out = await get(app, token, inv)
        trace = await get(app, token, inv, "/trace")
        mgr_trace = await get(app, tok.user("MANAGER"), inv, "/trace")
    assert out["status"] == "COMPLETE", (out["error"], out["plan"].get("history"))
    h = out["plan"]["history"]
    assert h["similar"] == ["INC-2911", "INC-4822"]
    # a count of restricted matches would tell every incident reader about them (review)
    assert "not_shared" not in h
    assert (h["claims_kept"], h["claims_rejected"]) == (2, 1)
    assert h["priors"]["change"] == "2/2" and h["post_error"] is None
    # the agent searched AS THE USER (delegated token forwarded), hybrid, service boosted
    seen = [s for s in stub_rag.seen if s["path"] == "/v1/incidents/search"]
    # two lists: this service's own history (filter), then the same failure anywhere (boost)
    assert len(seen) == 2 and all(x["obo"] and x["body"]["mode"] == "hybrid" for x in seen)
    assert not seen[0]["body"].get("boost_service") and seen[1]["body"]["boost_service"] is True
    assert seen[0]["body"]["service_key"] == planted.svc
    assert all(
        x["body"]["shared_only"] is True for x in seen
    )  # the stub ignores it: the agent filters again
    # trace: the prompt holds observations + past incidents' text -> MANAGER sees nothing
    assert {e["agent"]: e["redacted"] for e in trace["executions"]}["historical_incident"] is False
    assert {e["agent"]: e["redacted"] for e in mgr_trace["executions"]}[
        "historical_incident"
    ] is True

    iid = incident10["id"]
    ev = q(owner, "SELECT evidence_key, excerpt FROM incident.evidence WHERE incident_id=%s "
           "AND kind='INCIDENT'", iid)  # fmt: skip
    assert len(ev) == 2
    assert all("INC-9001" not in x and "Rolled back" not in x for _, x in ev)  # pointers only
    hyps = (await call(incident_app, "GET", f"/v1/incidents/{iid}/hypotheses",
                       tok.user("SRE"))).json()  # fmt: skip
    deploy = next(x for x in hyps if x["detail"].get("cause_kind") == "change")
    assert "2 of 2" in deploy["detail"]["history_prior"]
    assert deploy["detail"]["history_refs"] == ["INC-2911", "INC-4822"]
    # history never changes a band: the deploy keeps the band code gave it
    assert deploy["confidence"] == "HIGH"
    assert "INC-9001" not in str(hyps)

    md = await call(incident_app, "GET", f"/v1/incidents/{iid}/postmortem?format=markdown",
                    tok.user("SRE"))  # fmt: skip
    assert "## Similar past incidents" in md.text and "INC-2911" in md.text
    assert "INC-9001" not in md.text and "base rate, not evidence" in md.text
    line = next(x for x in md.text.splitlines() if x.startswith("- INC-4822"))
    assert "same -" in line  # the wrong 'same service' claim was dropped by code
    # MANAGER: a claim's value is THIS incident's log/metric/deploy data -> only 'service' shown
    m = (await call(incident_app, "GET", f"/v1/incidents/{iid}/postmortem",
                    tok.user("MANAGER"))).json()  # fmt: skip
    sre = (await call(incident_app, "GET", f"/v1/incidents/{iid}/postmortem",
                      tok.user("SRE"))).json()  # fmt: skip
    assert any(c["field"] == "deploy_before" for x in sre["similar"] for c in x["claims"])
    assert [x["incident_key"] for x in m["similar"]] == ["INC-2911", "INC-4822"]
    assert all(c["field"] == "service" for x in m["similar"] for c in x["claims"])


async def test_history_search_down_is_partial_named_and_reasoning_still_runs(
    make_orch: OrchFactory, people: People, incident10: dict[str, Any],  # noqa: F811
    llm_app: FastAPI, stub_rag: StubRag, incident_app: FastAPI, tok: Tokens,
) -> None:  # fmt: skip
    stub_rag.incidents_status = 503
    script(llm_app, LABELS, RANKING, critic=critique())  # no history call: the search failed
    token = people.token(people.add("SRE"))
    async with make_orch(agents=ALL, reasoning=True, history=True) as app:
        inv = (await start(app, token, incident10, "p13-down-run")).json()["investigation_id"]
        await app.state.engine.wait(UUID(inv))
        out = await get(app, token, inv)
    assert out["status"] == "PARTIAL", out["error"]
    assert "historical_incident" in (out["error"] or "")
    assert out["plan"]["hypotheses"]["kept"] >= 1  # hypotheses did not depend on history
    hyps = (await call(incident_app, "GET", f"/v1/incidents/{incident10['id']}/hypotheses",
                       tok.user("SRE"))).json()  # fmt: skip
    assert all(not x["detail"].get("history_prior") for x in hyps)


async def test_crash_after_history_reuses_it_and_stores_pointers_once(
    make_orch: OrchFactory, people: People, incident10: dict[str, Any], planted: Planted,  # noqa: F811
    incident_app: FastAPI, agents_app: FastAPI, api_app: FastAPI, owner: psycopg.Connection,
    llm_app: FastAPI, stub_rag: StubRag,
) -> None:  # fmt: skip
    """A kill while the ranker runs (history already done): resume never calls history or
    rag again, the INCIDENT pointers are stored once, the priors are the same."""
    stub_rag.incidents = hits(planted.svc)
    script(llm_app, LABELS, COMPARE, RANKING, critic=critique())
    apps = {"incident": incident_app, "agents": agents_app, "api": api_app}
    token = people.token(people.add("SRE"))
    first = Faults(apps, hang=("hypothesis",))
    async with make_orch(agents=ALL, reasoning=True, history=True, transport=first) as app:
        inv = (await start(app, token, incident10, "p13-crash-run")).json()["investigation_id"]
        await asyncio.wait_for(first.hung.wait(), 30)
    searches = len([s for s in stub_rag.seen if s["path"] == "/v1/incidents/search"])
    healthy = Faults(apps)
    async with make_orch(agents=ALL, reasoning=True, history=True, transport=healthy,
                         resume_on_startup=True) as app2:  # fmt: skip
        await app2.state.engine.wait(UUID(inv))
        out = await get(app2, token, inv)
    assert out["status"] == "COMPLETE", out
    assert healthy.calls == ["hypothesis", "critic"]  # history was NOT run again
    assert len([s for s in stub_rag.seen if s["path"] == "/v1/incidents/search"]) == searches == 2
    attempts = {t["agent"]: t["attempt"] for t in out["tasks"]}
    assert attempts["historical_incident"] == 1 and attempts["hypothesis"] == 2
    n = q(owner, "SELECT count(*) FROM incident.evidence WHERE incident_id=%s AND kind='INCIDENT'",
          incident10["id"])[0][0]  # fmt: skip
    assert n == 2
    hyps = q(owner, "SELECT detail->>'history_prior' FROM incident.hypotheses WHERE "
             "incident_id=%s AND detail->>'cause_kind'='change'", incident10["id"])  # fmt: skip
    assert hyps and "2 of 2" in hyps[0][0]
