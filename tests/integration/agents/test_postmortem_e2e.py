"""Phase 12 gate, end to end (ADR-022): evidence -> hypotheses -> critic -> validate ->
report (code timeline + model narrative, checked by code) -> incident.incident_events +
incident.postmortems -> GET /postmortem assembled at read time, redacted per reader.
Real Postgres and logins; the model is the gateway's FAKE provider (writer = hosted_fake,
the `reasoning` route, like the critic)."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import httpx
import psycopg
import pytest
from fastapi import FastAPI

from aeoi_llm.providers.base import ProviderTimeoutError

from .conftest import OrchFactory, People, Planted, Tokens
from .test_multi_agent_e2e import ALL, Faults, incident10  # noqa: F401  (fixture)
from .test_orchestrator_e2e import get, q, start
from .test_reasoning_e2e import LABELS, RANKING, critique, script

pytestmark = pytest.mark.integration
# start(app, token, incident, "<idempotency key>"): positional on purpose - gitleaks flags
# key="<random-looking literal>" as a generic API key (Phase 11 and 12 commits were blocked)


def narrative(deploy_key: str) -> dict[str, Any]:
    return {
        "summary": [
            {"text": f"{deploy_key} most likely triggered the regression.", "refs": ["o1", "o2"]},
            {"text": "Everyone stayed calm and fixed it fast.", "refs": []},  # uncited: dropped
            {"text": "About 40% of checkouts failed.", "refs": ["o2"]},  # invented number
        ],
        "what_happened": [{"text": "Latency rose after the deploy.", "refs": ["o1", "o2"]}],
        "why": [], "detection": [], "went_well": [], "went_poorly": [],
        "action_items": [
            {"text": "Alert on pool saturation before errors reach users.", "category": "detect",
             "refs": ["o2"]},
        ],
    }  # fmt: skip


async def call(app: FastAPI, method: str, path: str, token: str, **kw: Any) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://incident"
    ) as c:
        return await c.request(method, path, headers={"Authorization": f"Bearer {token}"}, **kw)


async def test_postmortem_end_to_end_timeline_doc_and_redaction(
    make_orch: OrchFactory, people: People, incident10: dict[str, Any], planted: Planted,  # noqa: F811
    owner: psycopg.Connection, incident_app: FastAPI, llm_app: FastAPI, tok: Tokens,
    orch_token: str,
) -> None:  # fmt: skip
    hosted = script(llm_app, LABELS, RANKING, critic=critique())
    hosted.structured.append(narrative(planted.deploy_key))
    token = people.token(people.add("SRE"))
    async with make_orch(agents=ALL, reasoning=True, postmortem=True) as app:
        inv = (await start(app, token, incident10, "p12-ok-run")).json()["investigation_id"]
        await app.state.engine.wait(UUID(inv))
        out = await get(app, token, inv)
        trace = await get(app, token, inv, "/trace")
        mgr_trace = await get(app, tok.user("MANAGER"), inv, "/trace")
    assert out["status"] == "COMPLETE", (out["error"], out["plan"].get("postmortem"))
    # the writer's prompt holds every observation: SRE may debug it, MANAGER may not
    assert {e["agent"]: e["redacted"] for e in trace["executions"]}["postmortem"] is False
    assert {e["agent"]: e["redacted"] for e in mgr_trace["executions"]}["postmortem"] is True
    tasks = {t["agent"]: t for t in out["tasks"]}
    assert tasks["postmortem"]["status"] == "SUCCEEDED"
    assert tasks["postmortem"]["model"] == "fake-large"  # the writer = reasoning route
    pm = out["plan"]["postmortem"]
    assert pm["written_by"] == "llm" and pm["sentences"] == 2 and pm["action_items"] == 1
    assert pm["sentences_dropped"] == 1 and pm["numbers_rejected"] == 1
    assert pm["removed_kept"] == 2  # Phase 29c (ADR-045): the two texts, not just the counts
    assert pm["timeline_inserted"] > 0 and pm["post_error"] is None

    iid = incident10["id"]
    r = await call(incident_app, "GET", f"/v1/incidents/{iid}/postmortem", tok.user("SRE"))
    assert r.status_code == 200, r.text
    doc = r.json()
    rc = doc["root_cause"]
    # root cause = the stored top hypothesis, verbatim, by code; never "confirmed" by a model
    assert planted.deploy_key in rc["statement"] and rc["status"] == "VALIDATED"
    assert rc["confidence"] == "HIGH" and rc["confirmed_by_human"] is False
    # numbers by code from the stored timeline + incident record
    assert doc["first_symptom_at"] and doc["trigger_at"] and doc["minutes_to_detect"] is not None
    assert doc["minutes_trigger_to_symptom"] > 0 and doc["minutes_to_resolve"] is None
    sources = {row["source"] for row in doc["timeline"]}
    assert {"DEPLOY", "METRIC", "HUMAN", "AGENT"} <= sources
    summary = next(s for s in doc["sections"] if s["name"] == "summary")
    assert [s["text"][: len(planted.deploy_key)] for s in summary["sentences"]] == [
        planted.deploy_key
    ]
    assert doc["redacted_sentences"] == 0
    assert len(doc["ruled_out"]) == out["plan"]["hypotheses"]["ruled_out"]
    md = await call(incident_app, "GET", f"/v1/incidents/{iid}/postmortem?format=markdown",
                    tok.user("SRE"))  # fmt: skip
    assert md.headers["content-type"].startswith("text/markdown")
    assert "not yet confirmed by a human" in md.text and "## Timeline" in md.text
    report, removed_md = md.text.split("## Removed by code")
    assert "40%" not in report and "fixed it fast" not in report
    # Phase 29c (ADR-045): what code took out is listed APART, with the reason
    assert (
        '· cites no observation (cites nothing): "Everyone stayed calm and fixed it fast."'
        in removed_md
    )
    assert ('· number not in the facts (not in the facts it cites: 40): '
            '"About 40% of checkouts failed."') in removed_md  # fmt: skip
    assert [(x["section"], x["reason"]) for x in doc["removed"]] == [
        ("summary", "no_valid_ref"),
        ("summary", "unknown_number"),
    ]
    assert doc["removed_redacted"] == 0 and doc["removed_kept"] is True

    # MANAGER: code conclusions yes; evidence rows and model sentences that cite evidence no
    m = (await call(incident_app, "GET", f"/v1/incidents/{iid}/postmortem",
                    tok.user("MANAGER"))).json()  # fmt: skip
    assert m["root_cause"]["statement"] == rc["statement"]
    # 2 sentences + 1 action item + every "did not change" line cite METRIC/DEPLOY evidence
    assert m["redacted_sentences"] == 3 + len(doc["no_change"])
    shown = [s for x in m["sections"] for s in x["sentences"]] + m["action_items"] + m["no_change"]
    assert all("redacted" in s["text"] and s.get("evidence_keys") == [] for s in shown)
    assert "LOG-" not in str(m) and "METRIC-" not in str(m)  # no evidence ids either
    # removed text cites nothing valid: it could quote ANY evidence -> hidden, reasons stay
    assert m["removed_redacted"] == 2 and "40" not in str(m["removed"])
    assert [x["reason"] for x in m["removed"]] == ["no_valid_ref", "unknown_number"]
    assert all("removed text needs read access" in x["text"] and x["detail"] == ""
               for x in m["removed"])  # fmt: skip
    assert not {row["source"] for row in m["timeline"]} & {"DEPLOY", "METRIC", "LOG", "CONFIG"}
    assert m["hidden_timeline_rows"] > 0
    tr = await call(incident_app, "GET", f"/v1/incidents/{iid}/timeline", tok.user("MANAGER"))
    assert not {row["source"] for row in tr.json()} & {"DEPLOY", "METRIC", "LOG", "CONFIG"}
    assert int(tr.headers["X-Hidden-Rows"]) == m["hidden_timeline_rows"] > 0

    # idempotent: a resent timeline/postmortem stores nothing new; one outbox event
    n = q(owner, "SELECT count(*) FROM incident.incident_events WHERE incident_id=%s AND "
          "dedupe_key IS NOT NULL", iid)[0][0]  # fmt: skip
    assert n == pm["timeline_inserted"]
    src, et, summary, at, payload = q(
        owner, "SELECT source, event_type, summary, occurred_at, payload FROM "
        "incident.incident_events WHERE incident_id=%s AND dedupe_key IS NOT NULL LIMIT 1", iid,
    )[0]  # fmt: skip
    point = {"at": at.isoformat(), "source": src, "event_type": et, "statement": summary,
             "subject": payload["subject"], "evidence_keys": payload["evidence_keys"],
             "service_key": payload["service_key"]}  # fmt: skip
    re_t = await call(incident_app, "POST", f"/v1/incidents/{iid}/timeline", orch_token,
                      json={"investigation_id": inv, "agent": "postmortem", "points": [point]})  # fmt: skip
    assert re_t.status_code == 200 and re_t.json() == {"received": 1, "inserted": 0}
    # `source` decides visibility, so the server derives it from the STORED evidence kind:
    # claiming AGENT (always visible) for a METRIC-backed point changes nothing (review)
    lie = {**point, "source": "AGENT", "subject": f"{point['subject']}-x"}
    r_lie = await call(incident_app, "POST", f"/v1/incidents/{iid}/timeline", orch_token,
                       json={"investigation_id": inv, "agent": "postmortem", "points": [lie]})  # fmt: skip
    assert r_lie.status_code == 200, r_lie.text
    stored_src = q(owner, "SELECT source FROM incident.incident_events WHERE incident_id=%s AND "
                   "payload->>'subject'=%s", iid, lie["subject"])[0][0]  # fmt: skip
    assert stored_src == src
    other_kind = "LOG" if src != "LOG" else "METRIC"  # a kind needing ANOTHER permission
    other = q(owner, "SELECT evidence_key FROM incident.evidence WHERE incident_id=%s AND "
              "kind=%s LIMIT 1", iid, other_kind)[0][0]  # fmt: skip
    mixed = {**point, "subject": "mixed", "evidence_keys": [*point["evidence_keys"][:1], other]}
    r_mixed = await call(incident_app, "POST", f"/v1/incidents/{iid}/timeline", orch_token,
                         json={"investigation_id": inv, "agent": "postmortem", "points": [mixed]})  # fmt: skip
    assert r_mixed.status_code == 422, r_mixed.text
    again = await call(incident_app, "POST", f"/v1/incidents/{iid}/postmortems", orch_token,
                       json={"investigation_id": inv, "generated_at": doc["generated_at"],
                             "written_by": "code"})  # fmt: skip
    assert again.status_code == 200 and again.json()["inserted"] is False
    assert q(owner, "SELECT count(*) FROM incident.outbox WHERE aggregate_id=%s AND "
             "event_type='PostmortemGenerated'", iid)[0][0] == 1  # fmt: skip


async def test_negative_evidence_is_redacted_like_the_evidence_it_comes_from(
    incident_app: FastAPI, incident10: dict[str, Any], orch_token: str, tok: Tokens,  # noqa: F811
    owner: psycopg.Connection, make_orch: OrchFactory, people: People, llm_app: FastAPI,
) -> None:  # fmt: skip
    """Review: "cpu_util stayed within its baseline (median 0.45 ...)" is METRIC data; it was
    shown to MANAGER while the METRIC rows were hidden."""
    script(llm_app, LABELS, RANKING, critic=critique())
    token = people.token(people.add("SRE"))
    async with make_orch(agents=ALL, reasoning=True) as app:  # stores evidence on the incident
        inv0 = (await start(app, token, incident10, "p12-nc-run")).json()["investigation_id"]
        await app.state.engine.wait(UUID(inv0))
    iid = incident10["id"]
    metric = q(owner, "SELECT evidence_key FROM incident.evidence WHERE incident_id=%s AND "
               "kind='METRIC' LIMIT 1", iid)[0][0]  # fmt: skip
    inv = "01a12000-0000-7000-8000-0000000000aa"
    body = {"investigation_id": inv, "generated_at": "2026-10-10T10:00:00Z", "written_by": "code",
            "root_cause_state": "reasoning_off",
            "no_change": [{"text": "cpu_util stayed within its baseline (median 0.45)",
                           "evidence_keys": [metric]}]}  # fmt: skip
    r = await call(incident_app, "POST", f"/v1/incidents/{iid}/postmortems", orch_token, json=body)
    assert r.status_code == 200 and r.json()["inserted"] is True, r.text
    path = f"/v1/incidents/{iid}/postmortem?investigation_id={inv}"
    sre = (await call(incident_app, "GET", path, tok.user("SRE"))).json()
    mgr = (await call(incident_app, "GET", path, tok.user("MANAGER"))).json()
    assert sre["no_change"][0]["text"].startswith("cpu_util")
    assert "redacted" in mgr["no_change"][0]["text"] and "0.45" not in str(mgr)
    assert mgr["no_change"][0]["evidence_keys"] == [] and mgr["redacted_sentences"] == 1
    assert sre["root_cause"] is None and sre["root_cause_state"] == "reasoning_off"
    md = await call(incident_app, "GET", path + "&format=markdown", tok.user("SRE"))
    assert "not generated for this investigation" in md.text and "survived" not in md.text


async def test_writer_down_is_partial_but_the_code_timeline_and_doc_are_stored(
    make_orch: OrchFactory, people: People, incident10: dict[str, Any],  # noqa: F811
    incident_app: FastAPI, llm_app: FastAPI, tok: Tokens,
) -> None:  # fmt: skip
    """No fallback for the writer: a 3B llama narrative would be stored as the postmortem."""
    hosted = script(llm_app, LABELS, RANKING, critic=critique())
    token = people.token(people.add("SRE"))
    async with make_orch(agents=ALL, reasoning=True, postmortem=True) as app:
        # the critic answers; every call of the WRITER (its system prompt) times out
        orig = hosted.generate_structured

        async def flaky(req: Any) -> Any:
            if "postmortem" in (req.system or "").lower()[:400]:
                raise ProviderTimeoutError("writer down")
            return await orig(req)

        hosted.generate_structured = flaky  # type: ignore[method-assign]
        inv = (await start(app, token, incident10, "p12-down-run")).json()["investigation_id"]
        await app.state.engine.wait(UUID(inv))
        out = await get(app, token, inv)
    assert out["status"] == "PARTIAL" and "postmortem: narrative not written" in out["error"]
    assert {t["agent"]: t["status"] for t in out["tasks"]}["postmortem"] == "SUCCEEDED"
    r = await call(incident_app, "GET", f"/v1/incidents/{incident10['id']}/postmortem",
                   tok.user("SRE"))  # fmt: skip
    doc = r.json()
    assert doc["written_by"] == "code" and doc["sections"] == [] and doc["timeline"]
    assert doc["root_cause"] is not None  # code parts do not need the model


async def test_a_postmortem_citing_foreign_evidence_is_refused(
    incident_app: FastAPI, incident10: dict[str, Any], orch_token: str, tok: Tokens,  # noqa: F811
) -> None:  # fmt: skip
    """Nothing stored may point at evidence this incident does not have (as hypotheses)."""
    bad = {
        "investigation_id": "01a12000-0000-7000-8000-000000000001",
        "generated_at": "2026-10-10T10:00:00Z", "written_by": "llm",
        "sections": [{"name": "summary", "sentences": [
            {"text": "x happened", "refs": ["o1"], "evidence_keys": ["LOG-not-on-this-incident"]}
        ]}],
    }  # fmt: skip
    r = await call(incident_app, "POST", f"/v1/incidents/{incident10['id']}/postmortems",
                   orch_token, json=bad)  # fmt: skip
    assert r.status_code == 422, r.text
    user = await call(incident_app, "POST", f"/v1/incidents/{incident10['id']}/postmortems",
                      tok.user("ADMIN"), json=bad)  # fmt: skip
    assert user.status_code == 403  # users never write postmortems: service scope only


async def test_crash_during_the_writer_reuses_everything_before_and_stores_once(
    make_orch: OrchFactory, people: People, incident10: dict[str, Any], planted: Planted,  # noqa: F811
    incident_app: FastAPI, agents_app: FastAPI, api_app: FastAPI, owner: psycopg.Connection,
    llm_app: FastAPI,
) -> None:  # fmt: skip
    """A kill while the writer runs: resume calls ONLY the writer again (attempt 2); the
    timeline and the postmortem are stored once (idempotent POSTs)."""
    hosted = script(llm_app, LABELS, RANKING, critic=critique())
    hosted.structured.append(narrative(planted.deploy_key))
    apps = {"incident": incident_app, "agents": agents_app, "api": api_app}
    token = people.token(people.add("SRE"))
    first = Faults(apps, hang=("postmortem",))
    async with make_orch(agents=ALL, reasoning=True, postmortem=True, transport=first) as app:
        inv = (await start(app, token, incident10, "p12-crash-run")).json()["investigation_id"]
        await asyncio.wait_for(first.hung.wait(), 30)
    healthy = Faults(apps)
    async with make_orch(agents=ALL, reasoning=True, postmortem=True, transport=healthy,
                         resume_on_startup=True) as app2:  # fmt: skip
        await app2.state.engine.wait(UUID(inv))
        out = await get(app2, token, inv)
    assert out["status"] == "COMPLETE", out
    assert healthy.calls == ["postmortem"]  # nothing before the writer ran again
    attempts = {t["agent"]: t["attempt"] for t in out["tasks"]}
    assert attempts["critic"] == 1 and attempts["postmortem"] == 2
    iid = incident10["id"]
    assert (
        q(owner, "SELECT count(*) FROM incident.postmortems WHERE incident_id=%s", iid)[0][0] == 1
    )
    n = q(owner, "SELECT count(*) FROM incident.incident_events WHERE incident_id=%s AND "
          "dedupe_key IS NOT NULL", iid)[0][0]  # fmt: skip
    assert n == out["plan"]["postmortem"]["timeline_inserted"]
