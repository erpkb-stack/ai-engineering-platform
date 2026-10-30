"""Phase 30 (security review #3): MODEL text needs every evidence kind of the incident.

The ranker and the critic are shown ALL observations, so their prose can quote any kind. The
old rule hid it only when THIS hypothesis' own edges included a kind the reader cannot read:
a MANAGER (incidents + docs only) saw the explanation of a hypothesis linked only to an
INCIDENT pointer - text that may quote a log line - and a hypothesis with no edges was shown
to everyone. Now: the ADR-045 rule (may_read_removed) for all model-written fields."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi import FastAPI

from aeoi_db.config import libpq_dsn
from aeoi_security.testing import KeyPair, token_for

from .test_audit_consumer import create_incident

pytestmark = pytest.mark.integration
QUOTE = "the log says PaymentClient NullPointer at line 88"


def _seed(migrated_db: str, incident_id: str) -> None:
    """One LOG and one INCIDENT evidence row; h1 linked to the INCIDENT pointer only, h2 to
    nothing; both with model text, h2 a critic alternative (model-written statement)."""
    inv = uuid4()
    ev = {"LOG": uuid4(), "INCIDENT": uuid4()}
    with psycopg.connect(libpq_dsn(database=migrated_db), autocommit=True) as c:
        for kind, eid in ev.items():
            c.execute(
                "INSERT INTO incident.evidence (id, incident_id, evidence_key, kind, source_system,"
                " title, excerpt, content_sha256, retrieved_by) VALUES (%s,%s,%s,%s,'t','t','x',%s,'t')",
                (
                    eid,
                    incident_id,
                    f"{kind}-{eid.hex[:12]}",
                    kind,
                    hashlib.sha256(b"x").hexdigest(),
                ),
            )
        rows = [
            ("h1", 1, "Deploy X may have caused it", {"explanation": QUOTE}),
            ("h2", 2, f"Critic: {QUOTE}", {"origin": "critic", "critic_missing": QUOTE}),
        ]
        ids: dict[str, UUID] = {}
        for key, rank, statement, detail in rows:
            ids[key] = uuid4()
            c.execute(
                "INSERT INTO incident.hypotheses (id, incident_id, statement, confidence, status,"
                " rank, produced_by, investigation_id, hypothesis_key, detail)"
                " VALUES (%s,%s,%s,'MEDIUM','PROPOSED',%s,'hypothesis',%s,%s,%s)",
                (ids[key], incident_id, statement, rank, inv, key, json.dumps(detail)),
            )
        c.execute(
            "INSERT INTO incident.hypothesis_evidence (hypothesis_id, evidence_id, stance)"
            " VALUES (%s, %s, 'SUPPORTS')",
            (ids["h1"], ev["INCIDENT"]),
        )


async def _read(app: FastAPI, keys: KeyPair, ref: str, role: str) -> dict[str, Any]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://i") as c:
        r = await c.get(f"/v1/incidents/{ref}/hypotheses",
                        headers={"Authorization": f"Bearer {token_for(keys, role)}"})  # fmt: skip
    assert r.status_code == 200, r.text
    return {h["key"]: h for h in r.json()}


async def test_model_text_needs_every_evidence_kind_of_the_incident(
    incident_app: FastAPI, keys: KeyPair, migrated_db: str
) -> None:
    inc = await create_incident(incident_app, keys)
    _seed(migrated_db, inc["id"])
    eng = await _read(incident_app, keys, inc["id"], "ENGINEER")  # reads every kind
    assert eng["h1"]["detail"]["explanation"] == QUOTE
    assert eng["h2"]["statement"].endswith(QUOTE)
    mgr = await _read(incident_app, keys, inc["id"], "MANAGER")  # incidents + docs only
    # h1's only edge is an INCIDENT pointer the manager may read: shown before Phase 30
    assert mgr["h1"]["hidden_edges"] == 0 and len(mgr["h1"]["edges"]) == 1
    assert QUOTE not in json.dumps(mgr)
    assert "redacted" in mgr["h1"]["detail"]["explanation"]
    # h2 has NO edges: it was shown to everyone
    assert (
        "redacted" in mgr["h2"]["statement"] and "redacted" in mgr["h2"]["detail"]["critic_missing"]
    )
    assert mgr["h1"]["statement"] == "Deploy X may have caused it"  # CODE text stays readable
