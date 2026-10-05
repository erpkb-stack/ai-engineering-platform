"""Client -> api gateway -> incident-service -> Postgres, all in-process."""

from __future__ import annotations

import uuid

import httpx
import pytest

from aeoi_security.testing import KeyPair
from tests.integration.services.conftest import bearer

pytestmark = pytest.mark.integration


async def test_declare_investigate_and_read_timeline(
    api_client: httpx.AsyncClient, keys: KeyPair
) -> None:
    sre = bearer(keys, "SRE")
    r = await api_client.post(
        "/api/v1/incidents",
        json={
            "title": "HTTP 500 errors increased 42% after deployment",
            "severity": "SEV2",
            "affected_services": ["checkout-api"],
        },
        headers=sre
        | {"Idempotency-Key": f"e2e-{uuid.uuid4()}", "X-Correlation-ID": "e2e-corr-0001"},
    )
    assert r.status_code == 201, r.text
    inc = r.json()
    assert r.headers["location"] == f"/api/v1/incidents/{inc['id']}"
    assert r.headers["x-correlation-id"] == "e2e-corr-0001"

    got = await api_client.get(f"/api/v1/incidents/{inc['key']}", headers=bearer(keys, "ENGINEER"))
    assert got.json()["id"] == inc["id"]

    inv = await api_client.post(
        f"/api/v1/incidents/{inc['id']}/investigate",
        headers=sre | {"Idempotency-Key": f"e2e-{uuid.uuid4()}"},
    )
    assert inv.status_code == 202

    tl = await api_client.get(f"/api/v1/incidents/{inc['id']}/timeline", headers=sre)
    assert [e["event_type"] for e in tl.json()] == ["IncidentCreated", "InvestigationRequested"]


async def test_gateway_blocks_before_service(api_client: httpx.AsyncClient, keys: KeyPair) -> None:
    r = await api_client.post(
        "/api/v1/incidents",
        json={"title": "Manager tries", "severity": "SEV4"},
        headers=bearer(keys, "MANAGER") | {"Idempotency-Key": f"mgr-{uuid.uuid4()}"},
    )
    assert r.status_code == 403


async def test_service_problem_details_reach_the_client(
    api_client: httpx.AsyncClient, keys: KeyPair
) -> None:
    r = await api_client.get(f"/api/v1/incidents/{uuid.uuid4()}", headers=bearer(keys, "ENGINEER"))
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")
