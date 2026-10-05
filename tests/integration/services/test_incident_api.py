"""incident-service HTTP API against a real database (Phase 4)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import psycopg
import pytest

from aeoi_security.testing import KeyPair
from tests.integration.services.conftest import bearer

pytestmark = pytest.mark.integration


def body(**over: Any) -> dict[str, Any]:
    return {
        "title": "HTTP 500 errors increased 42% after deployment",
        "severity": "SEV2",
        "affected_services": ["checkout-api"],
    } | over


def idem() -> dict[str, str]:
    return {"Idempotency-Key": f"test-{uuid.uuid4()}"}


async def create(
    client: httpx.AsyncClient, keys: KeyPair, role: str = "SRE", **over: Any
) -> dict[str, Any]:
    r = await client.post("/v1/incidents", json=body(**over), headers=bearer(keys, role) | idem())
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


class TestCreate:
    async def test_created_with_key_location_etag(
        self, client: httpx.AsyncClient, keys: KeyPair, owner_conn: psycopg.Connection
    ) -> None:
        r = await client.post(
            "/v1/incidents", json=body(), headers=bearer(keys, "ENGINEER") | idem()
        )
        assert r.status_code == 201
        inc = r.json()
        assert inc["key"].startswith("INC-")
        assert inc["status"] == "OPEN"
        assert inc["created_by"].startswith("user:")
        assert r.headers["location"] == f"/v1/incidents/{inc['id']}"
        assert r.headers["etag"] == '"1"'
        # same transaction wrote the timeline event and the outbox event
        events = owner_conn.execute(
            "SELECT event_type FROM incident.incident_events WHERE incident_id = %s", (inc["id"],)
        ).fetchall()
        outbox = owner_conn.execute(
            "SELECT event_type, payload->>'event_type' FROM incident.outbox WHERE aggregate_id = %s",
            (inc["id"],),
        ).fetchall()
        assert events == [("IncidentCreated",)]
        assert outbox == [("IncidentCreated", "IncidentCreated")]

    async def test_idempotency_key_required(self, client: httpx.AsyncClient, keys: KeyPair) -> None:
        r = await client.post("/v1/incidents", json=body(), headers=bearer(keys, "SRE"))
        assert r.status_code == 428

    async def test_replay_returns_same_incident(
        self, client: httpx.AsyncClient, keys: KeyPair, owner_conn: psycopg.Connection
    ) -> None:
        h = bearer(keys, "SRE", subject="oidc|replay-user") | idem()
        title = f"Replay test {uuid.uuid4()}"
        r1 = await client.post("/v1/incidents", json=body(title=title), headers=h)
        r2 = await client.post("/v1/incidents", json=body(title=title), headers=h)
        assert r1.status_code == r2.status_code == 201
        assert r1.json()["id"] == r2.json()["id"]
        assert r2.headers["idempotent-replayed"] == "true"
        n = owner_conn.execute(
            "SELECT count(*) FROM incident.incidents WHERE title = %s", (title,)
        ).fetchone()
        assert n == (1,)

    async def test_same_key_different_body_is_422(
        self, client: httpx.AsyncClient, keys: KeyPair
    ) -> None:
        h = bearer(keys, "SRE", subject="oidc|mismatch-user") | idem()
        assert (await client.post("/v1/incidents", json=body(), headers=h)).status_code == 201
        r = await client.post("/v1/incidents", json=body(severity="SEV1"), headers=h)
        assert r.status_code == 422
        assert "different request body" in r.json()["detail"]

    async def test_keys_are_scoped_per_user(self, client: httpx.AsyncClient, keys: KeyPair) -> None:
        key = idem()
        a = await client.post(
            "/v1/incidents", json=body(), headers=bearer(keys, "SRE", subject="oidc|a") | key
        )
        b = await client.post(
            "/v1/incidents", json=body(), headers=bearer(keys, "SRE", subject="oidc|b") | key
        )
        assert a.json()["id"] != b.json()["id"]

    async def test_concurrent_duplicates_create_one_incident(
        self, client: httpx.AsyncClient, keys: KeyPair, owner_conn: psycopg.Connection
    ) -> None:
        title = f"Race {uuid.uuid4()}"
        h = bearer(keys, "SRE", subject="oidc|racer") | idem()
        results = await asyncio.gather(
            *(client.post("/v1/incidents", json=body(title=title), headers=h) for _ in range(6))
        )
        assert {r.status_code for r in results} == {201}
        assert len({r.json()["id"] for r in results}) == 1
        n = owner_conn.execute(
            "SELECT count(*) FROM incident.incidents WHERE title = %s", (title,)
        ).fetchone()
        assert n == (1,)

    @pytest.mark.parametrize("role", ["MANAGER", "ADMIN"])
    async def test_roles_without_create_permission(
        self, client: httpx.AsyncClient, keys: KeyPair, role: str
    ) -> None:
        r = await client.post("/v1/incidents", json=body(), headers=bearer(keys, role) | idem())
        assert r.status_code == 403

    async def test_validation(self, client: httpx.AsyncClient, keys: KeyPair) -> None:
        bad = body(affected_services=["Checkout API!"], severity="SEV9")
        r = await client.post("/v1/incidents", json=bad, headers=bearer(keys, "SRE") | idem())
        assert r.status_code == 422


class TestRead:
    async def test_get_by_key_and_uuid(self, client: httpx.AsyncClient, keys: KeyPair) -> None:
        inc = await create(client, keys)
        h = bearer(keys, "ENGINEER")
        by_key = await client.get(f"/v1/incidents/{inc['key']}", headers=h)
        by_id = await client.get(f"/v1/incidents/{inc['id']}", headers=h)
        assert by_key.json() == by_id.json()
        assert by_key.headers["etag"] == '"1"'

    async def test_not_found_and_bad_ref(self, client: httpx.AsyncClient, keys: KeyPair) -> None:
        h = bearer(keys, "ENGINEER")
        nf = await client.get(f"/v1/incidents/{uuid.uuid4()}", headers=h)
        assert nf.status_code == 404
        assert nf.json()["correlation_id"]
        assert (await client.get("/v1/incidents/drop-table", headers=h)).status_code == 400

    async def test_keyset_pagination_has_no_gaps_or_duplicates(
        self, client: httpx.AsyncClient, keys: KeyPair
    ) -> None:
        svc = f"pager-{uuid.uuid4().hex[:8]}"
        created = [await create(client, keys, affected_services=[svc]) for _ in range(5)]
        seen: list[str] = []
        cursor: str | None = None
        pages = 0
        while True:
            params = {"service": svc, "limit": 2} | ({"cursor": cursor} if cursor else {})
            page = (
                await client.get("/v1/incidents", params=params, headers=bearer(keys, "ENGINEER"))
            ).json()
            seen += [i["id"] for i in page["items"]]
            pages += 1
            cursor = page["next_cursor"]
            if not cursor:
                break
        assert pages == 3
        assert seen == [c["id"] for c in reversed(created)]  # newest first, each exactly once

    async def test_tampered_cursor_is_400(self, client: httpx.AsyncClient, keys: KeyPair) -> None:
        r = await client.get(
            "/v1/incidents", params={"cursor": "garbage"}, headers=bearer(keys, "ENGINEER")
        )
        assert r.status_code == 400


class TestPatch:
    async def test_requires_if_match(self, client: httpx.AsyncClient, keys: KeyPair) -> None:
        inc = await create(client, keys)
        r = await client.patch(
            f"/v1/incidents/{inc['id']}",
            json={"status": "MITIGATED", "reason": "pool resized"},
            headers=bearer(keys, "INCIDENT_COMMANDER"),
        )
        assert r.status_code == 428

    async def test_optimistic_locking(self, client: httpx.AsyncClient, keys: KeyPair) -> None:
        inc = await create(client, keys)
        h = bearer(keys, "INCIDENT_COMMANDER")
        ok = await client.patch(
            f"/v1/incidents/{inc['id']}",
            json={"severity": "SEV1", "reason": "customer impact"},
            headers=h | {"If-Match": '"1"'},
        )
        assert ok.status_code == 200
        assert ok.headers["etag"] == '"2"'
        stale = await client.patch(
            f"/v1/incidents/{inc['id']}",
            json={"severity": "SEV3", "reason": "late writer"},
            headers=h | {"If-Match": '"1"'},
        )
        assert stale.status_code == 412

    async def test_invalid_transition_is_409(
        self, client: httpx.AsyncClient, keys: KeyPair
    ) -> None:
        inc = await create(client, keys)
        r = await client.patch(
            f"/v1/incidents/{inc['id']}",
            json={"status": "CLOSED", "reason": "skip it"},
            headers=bearer(keys, "INCIDENT_COMMANDER") | {"If-Match": '"1"'},
        )
        assert r.status_code == 409

    async def test_resolve_sets_resolved_at(self, client: httpx.AsyncClient, keys: KeyPair) -> None:
        inc = await create(client, keys)
        r = await client.patch(
            f"/v1/incidents/{inc['id']}",
            json={"status": "RESOLVED", "reason": "rolled back"},
            headers=bearer(keys, "INCIDENT_COMMANDER") | {"If-Match": '"1"'},
        )
        assert r.status_code == 200
        assert r.json()["resolved_at"] is not None

    async def test_engineer_cannot_change_status(
        self, client: httpx.AsyncClient, keys: KeyPair
    ) -> None:
        inc = await create(client, keys)
        r = await client.patch(
            f"/v1/incidents/{inc['id']}",
            json={"status": "MITIGATED", "reason": "x x x"},
            headers=bearer(keys, "ENGINEER") | {"If-Match": '"1"'},
        )
        assert r.status_code == 403


class TestInvestigate:
    async def test_accepted_async_and_idempotent(
        self, client: httpx.AsyncClient, keys: KeyPair, owner_conn: psycopg.Connection
    ) -> None:
        inc = await create(client, keys)
        h = bearer(keys, "ENGINEER", subject="oidc|investigator") | idem()
        r1 = await client.post(f"/v1/incidents/{inc['key']}/investigate", headers=h)
        r2 = await client.post(f"/v1/incidents/{inc['key']}/investigate", headers=h)
        assert r1.status_code == r2.status_code == 202
        assert r1.json()["request_id"] == r2.json()["request_id"]
        status = owner_conn.execute(
            "SELECT status FROM incident.incidents WHERE id = %s", (inc["id"],)
        ).fetchone()
        assert status == ("INVESTIGATING",)
        n = owner_conn.execute(
            "SELECT count(*) FROM incident.outbox WHERE aggregate_id = %s AND event_type = 'InvestigationRequested'",
            (inc["id"],),
        ).fetchone()
        assert n == (1,)

    async def test_closed_incident_cannot_be_investigated(
        self, client: httpx.AsyncClient, keys: KeyPair
    ) -> None:
        inc = await create(client, keys)
        ic = bearer(keys, "INCIDENT_COMMANDER")
        await client.patch(
            f"/v1/incidents/{inc['id']}",
            json={"status": "RESOLVED", "reason": "fixed"},
            headers=ic | {"If-Match": '"1"'},
        )
        await client.patch(
            f"/v1/incidents/{inc['id']}",
            json={"status": "CLOSED", "reason": "postmortem done"},
            headers=ic | {"If-Match": '"2"'},
        )
        r = await client.post(
            f"/v1/incidents/{inc['id']}/investigate", headers=bearer(keys, "SRE") | idem()
        )
        assert r.status_code == 409

    async def test_manager_cannot_start_investigation(
        self, client: httpx.AsyncClient, keys: KeyPair
    ) -> None:
        inc = await create(client, keys)
        r = await client.post(
            f"/v1/incidents/{inc['id']}/investigate", headers=bearer(keys, "MANAGER") | idem()
        )
        assert r.status_code == 403


class TestTimelineEvidenceFeedback:
    async def test_timeline_in_order(self, client: httpx.AsyncClient, keys: KeyPair) -> None:
        inc = await create(client, keys)
        await client.post(
            f"/v1/incidents/{inc['id']}/investigate", headers=bearer(keys, "SRE") | idem()
        )
        tl = (
            await client.get(
                f"/v1/incidents/{inc['id']}/timeline", headers=bearer(keys, "ENGINEER")
            )
        ).json()
        assert [e["event_type"] for e in tl] == ["IncidentCreated", "InvestigationRequested"]

    async def test_evidence_empty_until_agents_exist(
        self, client: httpx.AsyncClient, keys: KeyPair
    ) -> None:
        inc = await create(client, keys)
        r = await client.get(
            f"/v1/incidents/{inc['id']}/evidence", headers=bearer(keys, "ENGINEER")
        )
        assert r.status_code == 200
        assert r.json() == []

    async def test_feedback(self, client: httpx.AsyncClient, keys: KeyPair) -> None:
        inc = await create(client, keys)
        fb = {
            "incident_id": inc["id"],
            "target_type": "REPORT",
            "target_id": "report-1",
            "rating": 1,
        }
        assert (
            await client.post("/v1/feedback", json=fb, headers=bearer(keys, "ENGINEER"))
        ).status_code == 201
        bad = fb | {"rating": 5}
        assert (
            await client.post("/v1/feedback", json=bad, headers=bearer(keys, "ENGINEER"))
        ).status_code == 422
        missing = fb | {"incident_id": str(uuid.uuid4())}
        assert (
            await client.post("/v1/feedback", json=missing, headers=bearer(keys, "ENGINEER"))
        ).status_code == 404


async def test_readiness_checks_database(client: httpx.AsyncClient) -> None:
    r = await client.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["checks"]["database"] == "ok"


def test_service_login_cannot_read_other_schemas(service_db_url: str) -> None:
    dsn = service_db_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("SELECT count(*) FROM identity.users")
