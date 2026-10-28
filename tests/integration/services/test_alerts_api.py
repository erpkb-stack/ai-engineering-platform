"""Phase 29b (ADR-044): the signed alert webhook against a real database - incident-service
directly, and through the api edge (which checks the signature BEFORE forwarding)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from aeoi_security.testing import KeyPair
from aeoi_security.webhook import MAX_BODY, sign
from tests.integration.services.conftest import bearer

pytestmark = pytest.mark.integration


def payload(fp: str = "c0ffee01", *, status: str = "firing", **labels: str) -> dict[str, Any]:
    return {
        "version": "4",
        "status": status,
        "receiver": "aeoi",
        "alerts": [{
            "status": status,
            "labels": {"alertname": "CheckoutHigh5xx", "service": "checkout-api",
                       "severity": "error"} | labels,
            "annotations": {"summary": "Checkout 5xx above 5% for 5 minutes"},
            "startsAt": "2026-10-02T09:50:00Z",
            "fingerprint": fp,
        }],
    }  # fmt: skip


def signed(secret_file: Path, body: bytes, ts: int | None = None) -> dict[str, str]:
    ts = int(time.time()) if ts is None else ts
    secret = secret_file.read_bytes().strip()
    return {"content-type": "application/json", "x-aeoi-timestamp": str(ts),
            "x-aeoi-signature": sign(secret, ts, body)}  # fmt: skip


def unique_fp() -> str:
    return f"{time.time_ns():x}"[-16:]


async def post(c: httpx.AsyncClient, path: str, secret: Path, doc: dict[str, Any],
               **kw: Any) -> httpx.Response:  # fmt: skip
    raw = json.dumps(doc).encode()
    return await c.post(path, content=raw, headers=signed(secret, raw, **kw))


def incidents_with(conn: psycopg.Connection, fp: str) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT i.number, i.severity, i.created_by, i.affected_services, e.source, e.payload"
        " FROM incident.incidents i JOIN incident.incident_events e ON e.incident_id = i.id"
        " WHERE e.event_type = 'IncidentCreated' AND e.payload->>'fingerprint' = %s",
        (fp,),
    ).fetchall()


async def test_a_signed_alert_opens_one_incident_with_its_triage_rule(
    client: httpx.AsyncClient, alert_secret: Path, owner_conn: psycopg.Connection
) -> None:
    fp = unique_fp()
    r = await post(client, "/v1/alerts", alert_secret, payload(fp))
    assert r.status_code == 200, r.text
    res = r.json()["results"][0]
    assert res["outcome"] == "created" and res["incident_key"].startswith("INC-")
    # no catalog in this fixture: the label decides and the rule says the tier is unknown
    assert res["severity"] == "SEV2"
    assert res["reason"] == "label severity=error -> SEV2; tier unknown (catalog not configured)"
    rows = incidents_with(owner_conn, fp)
    assert len(rows) == 1
    _, severity, created_by, services, source, detail = rows[0]
    assert (severity, created_by, services, source) == (
        "SEV2", "service:alerts", ["checkout-api"], "ALERT",
    )  # fmt: skip
    assert detail["rule"] == res["reason"] and detail["alertname"] == "CheckoutHigh5xx"


async def test_the_same_alert_twice_or_a_replayed_request_opens_one_incident(
    client: httpx.AsyncClient, alert_secret: Path, owner_conn: psycopg.Connection
) -> None:
    fp = unique_fp()
    doc = payload(fp)
    raw = json.dumps(doc).encode()
    headers = signed(alert_secret, raw)
    first = await client.post("/v1/alerts", content=raw, headers=headers)
    replay = await client.post("/v1/alerts", content=raw, headers=headers)  # the SAME bytes
    # Alertmanager re-sends with changed labels (repeat_interval): still the same alert
    resent = await post(client, "/v1/alerts", alert_secret, payload(fp, severity="critical"))
    outcomes = [r.json()["results"][0] for r in (first, replay, resent)]
    assert [o["outcome"] for o in outcomes] == ["created", "duplicate", "duplicate"]
    assert len({o["incident_key"] for o in outcomes}) == 1
    assert len(incidents_with(owner_conn, fp)) == 1


@pytest.mark.parametrize("case", ["unsigned", "bad signature", "stale", "other secret"])
async def test_unsigned_forged_or_stale_alerts_open_nothing(
    client: httpx.AsyncClient, alert_secret: Path, owner_conn: psycopg.Connection,
    tmp_path: Path, case: str,
) -> None:  # fmt: skip
    fp = unique_fp()
    raw = json.dumps(payload(fp)).encode()
    h = signed(alert_secret, raw)
    if case == "unsigned":
        h = {"content-type": "application/json"}
    elif case == "bad signature":
        h["x-aeoi-signature"] = "sha256=" + "0" * 64
    elif case == "stale":
        h = signed(alert_secret, raw, ts=int(time.time()) - 301)
    else:
        other = tmp_path / "other.txt"
        other.write_text("b" * 64)
        h = signed(other, raw)
    r = await client.post("/v1/alerts", content=raw, headers=h)
    assert r.status_code == 401, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    assert incidents_with(owner_conn, fp) == []


async def test_a_user_token_does_not_replace_the_signature(
    client: httpx.AsyncClient, keys: KeyPair, owner_conn: psycopg.Connection
) -> None:
    """Even an ADMIN's valid JWT is not a way in: this route accepts ONLY the signature."""
    fp = unique_fp()
    r = await client.post("/v1/alerts", json=payload(fp), headers=bearer(keys, "ADMIN"))
    assert r.status_code == 401
    assert incidents_with(owner_conn, fp) == []


async def test_an_oversized_body_is_413_and_a_wrong_payload_is_422(
    client: httpx.AsyncClient, alert_secret: Path
) -> None:
    big = b'{"x":"' + b"a" * MAX_BODY + b'"}'
    r = await client.post("/v1/alerts", content=big, headers=signed(alert_secret, big))
    assert r.status_code == 413
    r = await post(client, "/v1/alerts", alert_secret, {"status": "firing", "alerts": "nope"})
    assert r.status_code == 422


async def test_resolved_is_counted_and_a_bad_service_is_rejected_per_alert(
    client: httpx.AsyncClient, alert_secret: Path, owner_conn: psycopg.Connection
) -> None:
    """Per-alert outcomes and 200: a 4xx would make Alertmanager re-send the whole group."""
    fp1, fp2, fp3 = unique_fp() + "a", unique_fp() + "b", unique_fp() + "c"
    doc = payload(fp1)
    doc["alerts"] += [
        dict(payload(fp2, status="resolved")["alerts"][0]),
        dict(payload(fp3, service="Not A Service")["alerts"][0]),
    ]
    r = await post(client, "/v1/alerts", alert_secret, doc)
    assert r.status_code == 200, r.text
    assert [x["outcome"] for x in r.json()["results"]] == ["created", "ignored_resolved",
                                                           "rejected"]  # fmt: skip
    assert incidents_with(owner_conn, fp2) == [] and incidents_with(owner_conn, fp3) == []


async def test_no_secret_file_means_not_configured_never_accept_all(
    incident_app: Any, alert_secret: Path, tmp_path: Path
) -> None:
    settings = incident_app.state.settings
    incident_app.state.settings = settings.model_copy(
        update={"alert_secret_file": tmp_path / "missing.txt"}
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=incident_app), base_url="http://incident"
        ) as c:
            r = await post(c, "/v1/alerts", alert_secret, payload(unique_fp()))
        assert r.status_code == 503
    finally:
        incident_app.state.settings = settings


# ------------------------------------------------------------------ through the api edge


async def test_the_api_forwards_the_signed_raw_bytes(
    api_client: httpx.AsyncClient, alert_secret: Path, owner_conn: psycopg.Connection,
    incident_calls: list[str],
) -> None:  # fmt: skip
    fp = unique_fp()
    r = await post(api_client, "/api/v1/alerts", alert_secret, payload(fp))
    assert r.status_code == 200, r.text  # incident-service re-verified the SAME bytes
    assert r.json()["results"][0]["outcome"] == "created"
    assert incident_calls == ["/v1/alerts"]
    assert len(incidents_with(owner_conn, fp)) == 1


async def test_the_api_refuses_unsigned_alerts_before_forwarding(
    api_client: httpx.AsyncClient, alert_secret: Path, keys: KeyPair, incident_calls: list[str]
) -> None:
    raw = json.dumps(payload(unique_fp())).encode()
    for h in ({"content-type": "application/json"}, bearer(keys, "SRE"),
              {"x-aeoi-timestamp": str(int(time.time())), "x-aeoi-signature": "sha256=00"}):  # fmt: skip
        r = await api_client.post("/api/v1/alerts", content=raw, headers=h)
        assert r.status_code == 401
    big = b"a" * (MAX_BODY + 1)
    r = await api_client.post("/api/v1/alerts", content=big, headers=signed(alert_secret, big))
    assert r.status_code == 413
    assert incident_calls == []  # nothing unauthorised reached incident-service
