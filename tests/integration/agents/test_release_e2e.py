"""Phase 15 gate, end to end (ADR-025): POST /v1/release-risk on the orchestrator -> the
release_risk agent (code only) AS THE CALLER -> tool gateway -> devdata + catalog -> stored,
advisory report. Real Postgres and logins; no model."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi import FastAPI

from aeoi_security.testing import KeyPair, delegated_token_for, service_token_for

from ..tools.conftest import T0
from .conftest import OrchFactory, People, Planted, Tokens
from .test_code_e2e import plant_n_plus_one
from .test_multi_agent_e2e import Faults
from .test_orchestrator_e2e import client, q

pytestmark = pytest.mark.integration


async def post(app: FastAPI, token: str, key: str | None = None, **body: Any) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"}
    if key:
        headers["Idempotency-Key"] = key
    async with client(app) as c:
        return await c.post("/v1/release-risk", json=body, headers=headers)


async def read(app: FastAPI, token: str, path: str) -> httpx.Response:
    async with client(app) as c:
        return await c.get(f"/v1/release-risk{path}", headers={"Authorization": f"Bearer {token}"})


async def test_the_planted_deploy_is_high_advisory_stored_and_retry_safe(
    make_orch: OrchFactory, people: People, planted: Planted, owner: psycopg.Connection,
    tok: Tokens,
) -> None:  # fmt: skip
    plant_n_plus_one(owner, planted.sha)
    token = people.token(people.add("SRE"))
    key = f"rr-{uuid4().hex[:12]}"
    async with make_orch() as app:
        r = await post(app, token, key, deploy_key=planted.deploy_key)
        again = await post(app, token, key, deploy_key=planted.deploy_key)
        other = await post(app, token, key, repository=planted.repo, pull_request=planted.pr_number)
        got = await read(app, token, f"/{r.json()['id']}")
        listed = await read(app, token, f"?deploy_key={planted.deploy_key}")
        mgr_post = await post(app, tok.user("MANAGER"), deploy_key=planted.deploy_key)
        mgr_get = await read(app, tok.user("MANAGER"), f"/{r.json()['id']}")
    assert r.status_code == 201, r.text
    out = r.json()
    rep = out["report"]
    assert out["band"] == "HIGH" and rep["advisory"] is True and rep["service_key"] == planted.svc
    kinds = {f["kind"]: f for f in rep["factors"]}
    assert kinds["code_mechanism"]["severity"] == "HIGH"
    assert "findOpenOrders" in kinds["code_mechanism"]["statement"]
    # the planted deploy also flipped feature_flags.order_batching - which the code does not read
    assert [s["kind"] for s in rep["rollback"]] == ["redeploy", "flag_not_read"]
    # no earlier deploy of this service: only its own commit was checked, said, not hidden
    assert (out["status"], rep["coverage"], rep["findings_band"]) == ("PARTIAL", "partial", "HIGH")
    cited = {e for f in rep["factors"] for e in f["evidence_ids"]}
    assert cited and cited <= {e["evidence_key"] for e in out["evidence"]}
    assert "new-pass" not in str(out) and "old-pass" not in str(out)  # secret config values
    # retry: same report, nothing re-run; same key for another target: 409
    assert again.status_code == 200 and again.json()["id"] == out["id"]
    assert other.status_code == 409
    assert got.status_code == 200 and got.json()["band"] == "HIGH"
    assert [x["id"] for x in listed.json()] == [out["id"]]
    # code:read is needed to ask and to read
    assert mgr_post.status_code == 403 and mgr_get.status_code == 403
    rows = q(owner, "SELECT band, status, target_kind FROM orchestrator.release_assessments "
             "WHERE id = %s", out["id"])  # fmt: skip
    assert rows == [("HIGH", "PARTIAL", "deploy")]
    calls = q(owner, "SELECT DISTINCT agent_name FROM tools.tool_calls WHERE task_id = %s",
              out["id"])  # fmt: skip
    assert calls == [("release_risk",)]


async def test_a_pull_request_and_a_release_candidate_range(
    make_orch: OrchFactory, people: People, planted: Planted, owner: psycopg.Connection,
) -> None:  # fmt: skip
    plant_n_plus_one(owner, planted.sha)
    base = ("bd" + planted.sha[2:])[:40]  # what runs now: an older commit of the service
    owner.execute(
        "INSERT INTO devdata.commits VALUES (%s, %s, %s, 'dev@northwind.example', 'older',"
        " %s, 1, 1, %s) ON CONFLICT DO NOTHING",
        (base, planted.repo, planted.svc, ["src/A.java"], T0 - timedelta(days=2)),
    )
    token = people.token(people.add("ENGINEER"))
    async with make_orch() as app:
        pr = await post(app, token, repository=planted.repo, pull_request=planted.pr_number)
        rng = await post(app, token, service_key=planted.svc, base_sha=base,
                         head_sha=planted.sha)  # fmt: skip
        swapped = await post(app, token, service_key=planted.svc, base_sha=planted.sha,
                             head_sha=base)  # fmt: skip
        missing = await post(app, token, repository=planted.repo, pull_request=999_999)
    assert pr.status_code == 201 and pr.json()["band"] == "HIGH", pr.text
    assert pr.json()["report"]["head_sha"] == planted.sha
    assert "reverting PR" in pr.json()["report"]["rollback"][0]["statement"]
    r = rng.json()
    assert rng.status_code == 201 and r["band"] == "HIGH" and r["report"]["target_kind"] == "range"
    assert "not shipping" in r["report"]["rollback"][0]["statement"]
    # a REAL base: the range from it to head was checked, nothing left out
    assert (r["report"]["coverage"], r["report"]["base_sha"]) == ("full", base)
    shas = {e["title"].split()[1] for e in r["evidence"] if e["kind"] == "COMMIT"}
    assert planted.sha[:12] in shas
    # swapped base/head: recorded as FAILED with the reason, never a quiet LOW
    assert swapped.status_code == 201 and swapped.json()["status"] == "FAILED"
    assert "swapped" in swapped.json()["error"]
    # an unknown PR is recorded as FAILED with the reason, never as a quiet LOW
    assert missing.status_code == 201 and missing.json()["status"] == "FAILED"
    assert missing.json()["band"] is None and "not found" in missing.json()["error"]


async def test_agents_down_is_503_and_nothing_is_recorded(
    make_orch: OrchFactory, people: People, planted: Planted, owner: psycopg.Connection,
    incident_app: FastAPI, agents_app: FastAPI, api_app: FastAPI,
) -> None:  # fmt: skip
    token = people.token(people.add("SRE"))
    apps = {"incident": incident_app, "agents": agents_app, "api": api_app}
    before = q(owner, "SELECT count(*) FROM orchestrator.release_assessments")[0][0]
    async with make_orch(transport=Faults(apps, fail=("release_risk",))) as app:
        r = await post(app, token, deploy_key=planted.deploy_key)
    assert r.status_code == 503
    assert q(owner, "SELECT count(*) FROM orchestrator.release_assessments")[0][0] == before


async def test_an_idempotency_key_never_returns_another_users_report(
    make_orch: OrchFactory, people: People, planted: Planted,
) -> None:  # fmt: skip
    a, b = people.token(people.add("SRE")), people.token(people.add("SRE"))
    key = f"rr-{uuid4().hex[:12]}"
    async with make_orch() as app:
        first = await post(app, a, key, deploy_key=planted.deploy_key)
        second = await post(app, b, key, deploy_key=planted.deploy_key)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]  # the key is scoped to its requester
    assert first.json()["requested_by"] != second.json()["requested_by"]


async def test_a_token_delegated_to_an_investigation_cannot_run_release_risk(
    agents_app: FastAPI, tkeys: KeyPair, skeys: KeyPair, planted: Planted,
) -> None:  # fmt: skip
    """ADR-019: a delegated token is bound to ONE investigation + incident; this task has
    neither, so the token is refused - a background run cannot assess releases as the user."""
    task = {"assessment_id": str(uuid4()), "request": {"deploy_key": planted.deploy_key}}
    delegated = delegated_token_for(skeys, "SRE", investigation_id=uuid4(), incident_id=uuid4())
    headers = {"Authorization": "Bearer " + service_token_for(tkeys, "orchestrator", "agents:run"),
               "X-On-Behalf-Of": "Bearer " + delegated}  # fmt: skip
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=agents_app),
                                 base_url="http://agents") as c:  # fmt: skip
        r = await c.post("/v1/agents/release_risk/run", json=task, headers=headers)
    assert r.status_code == 403 and "bound to another investigation" in r.text
