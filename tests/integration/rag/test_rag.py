"""Phase 6 verify gate: ingestion, permission filter (leakage = 0), quarantine, API, gateway."""

from __future__ import annotations

import itertools
import uuid
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from fastapi import FastAPI

from aeoi_api.config import Settings as ApiSettings
from aeoi_api.main import build_app as build_api
from aeoi_api.ratelimit import InMemoryTokenBucket
from aeoi_llm_client import FakeLLMClient, LLMError
from aeoi_models.api.search import SearchMode
from aeoi_rag.config import Settings
from aeoi_rag.main import build_app as build_rag
from aeoi_security.testing import KeyPair, service_token_for

from .conftest import Pack, user

pytestmark = pytest.mark.integration
PERSONAS = [("eng-all",), ("sre",), ("eng-all", "sre"), ("management",), ("security-team",),
            ("architecture-team",), ("incident-commanders",)]  # fmt: skip
MODES = [m.value for m in SearchMode]


def restricted(pack: Pack) -> list[Any]:
    return [d for d in pack.docs.values() if d.allowed_groups != ["eng-all"] and not d.adversarial]


def first_sentence(d: Any) -> str:
    text = d.body.decode("latin-1") if isinstance(d.body, bytes) else d.body
    return text.replace("#", " ").strip().split(".")[0][:200]


# ------------------------------------------------------------------ ingestion


def test_ingestion_counts(pack: Pack) -> None:
    counts = pack.report.counts()
    assert counts == {"indexed": 56, "quarantined": 4}
    assert pack.report.embedded_chunks > 56
    assert sum(o.pii for o in pack.report.outcomes) >= 4  # exec contacts: 2 emails + 2 phones


def test_chunk_acl_always_equals_document_acl(pack: Pack, owner_conn: psycopg.Connection) -> None:
    bad = owner_conn.execute(
        "SELECT count(*) FROM rag.document_chunks c JOIN rag.documents d ON d.id = c.document_id "
        "WHERE c.allowed_groups IS DISTINCT FROM d.allowed_groups"
    ).fetchone()
    assert bad == (0,)


def test_pii_never_stored(pack: Pack, owner_conn: psycopg.Connection) -> None:
    row = owner_conn.execute(
        "SELECT content FROM rag.documents WHERE source_uri = 'docpack://restricted/exec-escalation-contacts.txt'"
    ).fetchone()
    assert row is not None
    assert "555-0199" not in row[0] and "@northwind.example" not in row[0]
    assert "[PHONE]" in row[0] and "[EMAIL]" in row[0]


def test_quarantined_documents_have_reason_and_no_chunks(
    pack: Pack, owner_conn: psycopg.Connection
) -> None:
    rows = owner_conn.execute(
        "SELECT d.source_uri, d.quarantine_reason, count(c.id) FROM rag.documents d "
        "LEFT JOIN rag.document_chunks c ON c.document_id = d.id WHERE d.quarantined "
        "AND d.source_uri LIKE 'docpack://%' GROUP BY 1, 2"
    ).fetchall()
    assert {r[0] for r in rows} == {u for u, d in pack.docs.items() if d.adversarial}
    assert all(r[1].startswith("injection:") and r[2] == 0 for r in rows)


async def test_reingest_is_idempotent(pack: Pack, rag_app: FastAPI) -> None:
    from aeoi_rag.ingest import Ingestor

    ing = Ingestor(
        rag_app.state.sessionmaker, rag_app.state.retriever._embedder, rag_app.state.settings
    )
    again = await ing.ingest_pack(pack.root)
    assert again.counts() == {"unchanged": 60}


# ------------------------------------------------------------------ leakage = 0 (verify gate)


async def test_leakage_matrix_is_zero(pack: Pack, rag_app: FastAPI) -> None:
    """Every restricted doc x every persona without access x 3 modes x 3 query styles."""
    retriever = rag_app.state.retriever
    checked = leaks = 0
    for doc, persona, mode in itertools.product(restricted(pack), PERSONAS, MODES):
        if set(persona) & set(doc.allowed_groups):
            continue
        codeword = (
            next(
                w
                for w in first_sentence(doc).split() + doc.title.split()
                if w.isupper() and len(w) > 4
            )
            if any(
                w.isupper() and len(w) > 4 for w in (first_sentence(doc) + " " + doc.title).split()
            )
            else doc.title
        )
        for q in (f"What is codename {codeword}?", doc.title, first_sentence(doc)):
            res = await retriever.search(q, list(persona), k=20, mode=SearchMode(mode))
            checked += 1
            leaks += sum(1 for h in res.hits if h.source_uri == doc.source_uri)
    assert checked >= 100
    assert leaks == 0


async def test_members_do_find_restricted_docs(pack: Pack, rag_app: FastAPI) -> None:
    """Proves the filter is not simply 'hide everything sensitive'."""
    for doc in restricted(pack):
        res = await rag_app.state.retriever.search(doc.title, list(doc.allowed_groups), k=5)
        assert doc.source_uri in [h.source_uri for h in res.hits], doc.source_uri


async def test_no_groups_means_no_results(rag_app: FastAPI) -> None:
    res = await rag_app.state.retriever.search("runbook", [], k=5)
    assert res.hits == []


async def test_acl_change_takes_effect_immediately(
    pack: Pack, rag_app: FastAPI, owner_conn: psycopg.Connection
) -> None:
    uri = "docpack://runbooks/checkout-api-pool.md"
    q = "checkout-api database connection pool exhaustion"
    retriever = rag_app.state.retriever
    assert uri in [h.source_uri for h in (await retriever.search(q, ["eng-all"], k=5)).hits]
    owner_conn.execute(
        "UPDATE rag.documents SET allowed_groups = '{security-team}' WHERE source_uri = %s", (uri,)
    )
    try:
        assert uri not in [
            h.source_uri for h in (await retriever.search(q, ["eng-all"], k=20)).hits
        ]
    finally:
        owner_conn.execute(
            "UPDATE rag.documents SET allowed_groups = '{eng-all}' WHERE source_uri = %s", (uri,)
        )


async def test_quarantined_docs_never_retrieved(pack: Pack, rag_app: FastAPI) -> None:
    for doc, mode in itertools.product([d for d in pack.docs.values() if d.adversarial], MODES):
        res = await rag_app.state.retriever.search(
            first_sentence(doc) or doc.title, ["eng-all"], k=20, mode=SearchMode(mode)
        )
        assert doc.source_uri not in [h.source_uri for h in res.hits]


# ------------------------------------------------------------------ HTTP API


async def test_search_api_happy_path(rag: httpx.AsyncClient, keys: KeyPair) -> None:
    r = await rag.post(
        "/v1/search",
        json={"query": "ERR-POOL connection pool checkout-api", "k": 5},
        headers=user(keys, "eng-all"),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert 1 <= len(body["results"]) <= 5
    assert body["embedding_model"] == "fake-embed"
    assert body["rerank"] == {
        "requested": False,
        "applied": False,
        "route": None,
        "model": None,
        "fallback_used": False,
        "latency_ms": None,
        "error": None,
    }
    assert [h["rank"] for h in body["results"]] == list(range(1, len(body["results"]) + 1))


@pytest.mark.parametrize(
    ("headers_kind", "status"), [("none", 401), ("admin", 403), ("service", 403)]
)
async def test_search_api_authz(
    rag: httpx.AsyncClient, keys: KeyPair, headers_kind: str, status: int
) -> None:
    headers = {
        "none": {},
        "admin": user(
            keys, "eng-all", role="ADMIN"
        ),  # ADMIN has no docs:read (separation of duties)
        "service": {
            "Authorization": f"Bearer {service_token_for(keys, 'orchestrator', 'llm:invoke')}"
        },
    }[headers_kind]
    r = await rag.post("/v1/search", json={"query": "pool"}, headers=headers)
    assert r.status_code == status


async def test_groups_in_body_are_rejected(rag: httpx.AsyncClient, keys: KeyPair) -> None:
    r = await rag.post(
        "/v1/search",
        json={"query": "BLUEHERON", "groups": ["security-team"]},
        headers=user(keys, "eng-all"),
    )
    assert r.status_code == 422  # extra="forbid": you cannot ask for someone else's groups


async def test_get_document_404_for_outsider_200_for_member(
    rag: httpx.AsyncClient, keys: KeyPair, owner_conn: psycopg.Connection
) -> None:
    (doc_id,) = owner_conn.execute(
        "SELECT id FROM rag.documents WHERE source_uri = 'docpack://restricted/secrets-rotation-procedure.md'"
    ).fetchone()
    assert (
        await rag.get(f"/v1/documents/{doc_id}", headers=user(keys, "eng-all"))
    ).status_code == 404
    ok = await rag.get(f"/v1/documents/{doc_id}", headers=user(keys, "security-team"))
    assert ok.status_code == 200 and ok.json()["chunk_count"] >= 1


async def test_rerank_requested_via_api(rag: httpx.AsyncClient, keys: KeyPair) -> None:
    r = await rag.post(
        "/v1/search", json={"query": "retry storm", "rerank": True}, headers=user(keys, "eng-all")
    )
    assert r.status_code == 200
    assert r.json()["rerank"]["requested"] is True
    assert (
        r.json()["rerank"]["applied"] is True
    )  # fake gateway returns empty grades -> RRF order kept


async def test_gateway_down_hybrid_degrades_vector_fails(
    rag_settings: Settings, keys: KeyPair, pack: Pack
) -> None:
    class Down(FakeLLMClient):
        async def embed(
            self, inputs: list[str], *, route: str = "embed", timeout_s: float | None = None
        ) -> Any:
            raise LLMError(503, "gateway-unreachable", "down")

    app = build_rag(rag_settings, llm_client=Down())
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://rag"
        ) as c:
            h = await c.post(
                "/v1/search", json={"query": "ERR-POOL pool"}, headers=user(keys, "eng-all")
            )
            assert h.status_code == 200
            assert h.json()["degraded"].startswith("vector search unavailable")
            assert h.json()["results"]
            v = await c.post(
                "/v1/search",
                json={"query": "pool", "mode": "vector"},
                headers=user(keys, "eng-all"),
            )
            assert v.status_code == 503
    finally:
        await app.state.engine.dispose()


async def test_api_gateway_routes_search(rag_app: FastAPI, pubfile: Path, keys: KeyPair) -> None:
    api = build_api(
        ApiSettings(jwt_public_key_file=pubfile, environment="test"),  # type: ignore[arg-type]
        transports={"rag": httpx.ASGITransport(app=rag_app)},
        limiter=InMemoryTokenBucket(rate_per_s=100, burst=100),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://api"
    ) as c:
        ok = await c.post("/api/v1/search", json={"query": "pool"}, headers=user(keys, "eng-all"))
        assert ok.status_code == 200 and ok.json()["results"]
        denied = await c.post(
            "/api/v1/search", json={"query": "pool"}, headers=user(keys, "eng-all", role="ADMIN")
        )
        assert denied.status_code == 403  # stopped at the gateway, never reaches rag


async def test_quarantine_after_indexing_hides_existing_chunks(
    pack: Pack, rag_app: FastAPI, owner_conn: psycopg.Connection
) -> None:
    """A reviewer can quarantine an already-indexed document; its chunks must vanish from
    results at once (the chunks still exist - the query filters on the document flag)."""
    uri = "docpack://runbooks/payments-api-lag.md"
    q = "payments-api Kafka consumer lag"
    retriever = rag_app.state.retriever
    for mode in MODES:
        assert uri in [
            h.source_uri
            for h in (await retriever.search(q, ["eng-all"], k=10, mode=SearchMode(mode))).hits
        ]
    owner_conn.execute(
        "UPDATE rag.documents SET quarantined = true, quarantine_reason = 'manual:review' "
        "WHERE source_uri = %s",
        (uri,),
    )
    try:
        for mode in MODES:
            res = await retriever.search(q, ["eng-all"], k=20, mode=SearchMode(mode))
            assert uri not in [h.source_uri for h in res.hits], mode
    finally:
        owner_conn.execute(
            "UPDATE rag.documents SET quarantined = false, quarantine_reason = NULL "
            "WHERE source_uri = %s",
            (uri,),
        )


async def test_python_fusion_matches_sql(pack: Pack, rag_app: FastAPI) -> None:
    """The sweep evaluates fusion settings in Python; that is only valid if Python and SQL agree."""
    from aeoi_rag.search import fuse

    retriever = rag_app.state.retriever
    s = rag_app.state.settings
    queries = [
        "What does ERR-POOL mean",
        "checkout keeps failing after a release",
        "LedgerReconciler",
        "how many database links can payments-api open",
        "retry storm stampede",
    ]
    for q in queries:
        sql = await retriever.search(q, ["eng-all"], k=10)
        sql_uris: list[str] = []
        for h in sql.hits:
            if h.source_uri not in sql_uris:
                sql_uris.append(h.source_uri)
        _, wv, wk = retriever.fusion_params(q)
        cands = await retriever.candidates(q, ["eng-all"], s.vector_candidates)
        py = fuse(
            cands,
            rrf_k=s.rrf_k,
            wv=wv,
            wk=wk,
            depth=s.vector_candidates,
            max_per_doc=s.max_chunks_per_document,
            k=10,
        )
        assert py == sql_uris, q


async def test_incident_search_applies_group_acl_and_service_filter(
    rag: httpx.AsyncClient, keys: KeyPair, owner_conn: psycopg.Connection
) -> None:
    """POST /v1/incidents/search (Phase 7, backs the search_incidents tool): same ACL rule as
    documents - the filter is in the SQL, the groups come from the token."""
    tag = uuid.uuid4().hex[:6]
    n = int(tag, 16) % 10**6
    a, b, c = (f"INC-8{n:06d}{i}" for i in range(3))  # unique per run: no seed collision
    for key, groups, svc in (
        (a, ["eng-all"], "checkout-api"),
        (b, ["security-team"], "checkout-api"),
        (c, ["eng-all"], "tax-api"),
    ):
        owner_conn.execute(
            "INSERT INTO rag.historical_incidents (id, incident_key, title, summary, root_cause,"
            " root_cause_category, remediation, service_keys, severity, occurred_at, resolved_at,"
            " allowed_groups) VALUES (gen_random_uuid(), %s, %s, 'pool exhausted', %s, 'code',"
            " 'rollback', %s, 'SEV2', now() - interval '30 days', now() - interval '29 days', %s)",
            (key, f"zq{tag} connection pool exhaustion", f"zq{tag} N+1 query", [svc], groups),
        )
    mine = {a, b, c}

    def found(resp: httpx.Response) -> set[str]:
        # the OR-query also matches seeded incidents about pools: look only at ours
        return {h["incident_key"] for h in resp.json()} & mine

    body = {"query": f"zq{tag} pool exhaustion", "k": 20}
    r = await rag.post("/v1/incidents/search", json=body, headers=user(keys, "eng-all"))
    assert r.status_code == 200
    assert found(r) == {a, c}
    r = await rag.post(
        "/v1/incidents/search",
        json={**body, "service_key": "checkout-api"},
        headers=user(keys, "eng-all", "security-team"),
    )
    assert found(r) == {a, b}
    assert all("checkout-api" in h["service_keys"] for h in r.json())
    r = await rag.post("/v1/incidents/search", json=body, headers=user(keys, "nobody"))
    assert r.json() == []
