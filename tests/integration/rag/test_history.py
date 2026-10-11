"""Phase 13 (ADR-023): hybrid similar-incident search over rag.historical_incidents.

Embeddings are the fake hashed bag-of-words from the test routing: these tests prove the
plumbing (ACL in both branches, fusion, boost vs filter, degradation), never retrieval quality.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import httpx
import psycopg
import pytest
from fastapi import FastAPI

from aeoi_rag import history
from aeoi_security.testing import KeyPair

from .conftest import user

pytestmark = pytest.mark.integration


@pytest.fixture
def mine(owner_conn: psycopg.Connection) -> Iterator[dict[str, str]]:
    """Three past incidents with words no seed row has: a open/checkout, b restricted/checkout,
    c open/tax. Removed afterwards so other tests' embed runs are not affected."""
    tag = uuid.uuid4().hex[:6]
    n = int(tag, 16) % 10**6
    keys = {name: f"INC-7{n:06d}{i}" for i, name in enumerate("abc")}
    rows = (
        (keys["a"], ["eng-all"], "checkout-api"),
        (keys["b"], ["security-team"], "checkout-api"),
        (keys["c"], ["eng-all"], "tax-api"),
    )
    for key, groups, svc in rows:
        owner_conn.execute(
            "INSERT INTO rag.historical_incidents (id, incident_key, title, summary, root_cause,"
            " root_cause_category, remediation, service_keys, severity, occurred_at, resolved_at,"
            " allowed_groups) VALUES (gen_random_uuid(), %s, %s, %s, 'xq pool leak', 'code',"
            " 'rollback', %s, 'SEV2', now() - interval '30 days', now() - interval '29 days', %s)",
            (key, f"zq{tag} vortex outage", f"zq{tag} vortex saturation spike", [svc], groups),
        )
    keys["tag"] = tag
    yield keys
    owner_conn.execute(
        "DELETE FROM rag.historical_incidents WHERE incident_key = ANY(%s)",
        ([keys["a"], keys["b"], keys["c"]],),
    )


async def _embed(app: FastAPI) -> None:
    _, model = await history.embed_pending(app.state.sessionmaker, app.state.embedder)
    assert model, "fake embedder must report a model name"


def _ours(resp: httpx.Response, mine: dict[str, str]) -> list[dict[str, object]]:
    wanted = {mine["a"], mine["b"], mine["c"]}
    return [h for h in resp.json() if h["incident_key"] in wanted]


async def test_hybrid_uses_both_branches_and_keeps_the_acl_in_each(
    rag: httpx.AsyncClient, rag_app: FastAPI, keys: KeyPair, mine: dict[str, str]
) -> None:
    await _embed(rag_app)
    body = {"query": f"zq{mine['tag']} vortex saturation", "k": 20, "mode": "hybrid"}
    r = await rag.post("/v1/incidents/search", json=body, headers=user(keys, "eng-all"))
    assert r.status_code == 200, r.text
    hits = _ours(r, mine)
    assert {h["incident_key"] for h in hits} == {mine["a"], mine["c"]}  # b: security-team only
    assert all(h["retrieval"] == "hybrid" for h in hits)
    assert all(h["rank_k"] is not None and h["rank_v"] is not None for h in hits)
    assert all(h["open_to_engineering"] for h in hits)

    r = await rag.post(
        "/v1/incidents/search", json=body, headers=user(keys, "eng-all", "security-team")
    )
    by_key = {h["incident_key"]: h for h in _ours(r, mine)}
    assert set(by_key) == {mine["a"], mine["b"], mine["c"]}
    # a reader may SEE b, but it must never be copied onto another incident's record
    assert by_key[mine["b"]]["open_to_engineering"] is False


async def test_shared_only_drops_restricted_incidents_in_the_sql(
    rag: httpx.AsyncClient, rag_app: FastAPI, keys: KeyPair, mine: dict[str, str]
) -> None:
    """What an agent stores on another incident: even a security-team caller gets only the
    incidents shared with every engineer, in both branches."""
    await _embed(rag_app)
    body = {"query": f"zq{mine['tag']} vortex saturation", "k": 20, "mode": "hybrid",
            "shared_only": True}  # fmt: skip
    for mode in ("hybrid", "vector"):
        r = await rag.post(
            "/v1/incidents/search", json={**body, "mode": mode},
            headers=user(keys, "eng-all", "security-team"),
        )  # fmt: skip
        keys_ = {h["incident_key"] for h in _ours(r, mine)}
        assert mine["b"] not in keys_ and mine["a"] in keys_, mode
        assert all(h["open_to_engineering"] for h in r.json())


async def test_vector_only_respects_the_acl(
    rag: httpx.AsyncClient, rag_app: FastAPI, keys: KeyPair, mine: dict[str, str]
) -> None:
    await _embed(rag_app)
    body = {"query": f"zq{mine['tag']} vortex saturation", "k": 20, "mode": "vector"}
    r = await rag.post("/v1/incidents/search", json=body, headers=user(keys, "eng-all"))
    assert r.status_code == 200, r.text
    assert r.json(), "vector branch returned nothing"
    assert all(h["rank_k"] is None for h in r.json())  # no keyword branch
    assert mine["b"] not in {h["incident_key"] for h in r.json()}


async def test_service_is_a_filter_by_default_and_a_boost_on_request(
    rag: httpx.AsyncClient, rag_app: FastAPI, keys: KeyPair, mine: dict[str, str]
) -> None:
    await _embed(rag_app)
    body = {
        "query": f"zq{mine['tag']} vortex saturation",
        "k": 20,
        "mode": "hybrid",
        "service_key": "tax-api",
    }
    r = await rag.post("/v1/incidents/search", json=body, headers=user(keys, "eng-all"))
    assert [h["incident_key"] for h in _ours(r, mine)] == [mine["c"]]  # Phase 7 filter kept

    r = await rag.post(
        "/v1/incidents/search",
        json={**body, "boost_service": True},
        headers=user(keys, "eng-all"),
    )
    order = [h["incident_key"] for h in _ours(r, mine)]
    assert set(order) == {mine["a"], mine["c"]}  # other service still shown
    assert order[0] == mine["c"]  # the same-service vote breaks the tie between twins


async def test_exclude_key_holds_an_incident_out(
    rag: httpx.AsyncClient, rag_app: FastAPI, keys: KeyPair, mine: dict[str, str]
) -> None:
    await _embed(rag_app)
    body = {
        "query": f"zq{mine['tag']} vortex",
        "k": 20,
        "mode": "hybrid",
        "exclude_key": mine["a"],
    }
    r = await rag.post("/v1/incidents/search", json=body, headers=user(keys, "eng-all"))
    assert mine["a"] not in {h["incident_key"] for h in r.json()}
    assert mine["c"] in {h["incident_key"] for h in r.json()}


class _DownEmbedder:
    model = None

    async def embed_query(self, text: str) -> list[float]:
        raise httpx.ConnectError("llm gateway down")

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise httpx.ConnectError("llm gateway down")


async def test_embedder_down_hybrid_says_keyword_and_vector_fails_loudly(
    rag: httpx.AsyncClient, rag_app: FastAPI, keys: KeyPair, mine: dict[str, str]
) -> None:
    rag_app.state.embedder = _DownEmbedder()
    body = {"query": f"zq{mine['tag']} vortex", "k": 20, "mode": "hybrid"}
    r = await rag.post("/v1/incidents/search", json=body, headers=user(keys, "eng-all"))
    assert r.status_code == 200
    hits = _ours(r, mine)
    assert hits and all(h["retrieval"] == "keyword" and h["rank_v"] is None for h in hits)

    r = await rag.post(
        "/v1/incidents/search", json={**body, "mode": "vector"}, headers=user(keys, "eng-all")
    )
    assert r.status_code == 503


async def test_vectors_from_another_model_are_not_compared(
    rag_app: FastAPI, mine: dict[str, str], owner_conn: psycopg.Connection
) -> None:
    """Cosine between two models' vectors is meaningless: the vector branch must skip them."""
    await _embed(rag_app)
    emb = rag_app.state.embedder
    qvec = await emb.embed_query(f"zq{mine['tag']} vortex saturation")
    rows = await history.search(
        rag_app.state.sessionmaker,
        f"zq{mine['tag']} vortex saturation",
        ["eng-all"],
        k=20,
        qvec=qvec,
        model="some-other-model",
        use_keyword=False,
    )
    assert rows == []


async def test_reembed_replaces_every_vector(
    rag_app: FastAPI, mine: dict[str, str], owner_conn: psycopg.Connection
) -> None:
    await _embed(rag_app)
    n, _ = await history.embed_pending(
        rag_app.state.sessionmaker, rag_app.state.embedder, reembed=True
    )
    total = owner_conn.execute("SELECT count(*) FROM rag.historical_incidents").fetchone()
    assert total is not None and n == total[0]
    left = owner_conn.execute(
        "SELECT count(*) FROM rag.historical_incidents WHERE embedding IS NULL"
    ).fetchone()
    assert left == (0,)


async def test_hybrid_without_vectors_says_keyword(
    rag: httpx.AsyncClient, rag_app: FastAPI, keys: KeyPair, mine: dict[str, str],
    owner_conn: psycopg.Connection,
) -> None:  # fmt: skip
    """Before `make embed-history` the vector branch is empty: hits must not claim 'hybrid'."""
    owner_conn.execute(
        "UPDATE rag.historical_incidents SET embedding = NULL, embedding_model = NULL"
    )
    body = {"query": f"zq{mine['tag']} vortex", "k": 20, "mode": "hybrid"}
    r = await rag.post("/v1/incidents/search", json=body, headers=user(keys, "eng-all"))
    assert r.status_code == 200 and r.json()
    assert all(h["retrieval"] == "keyword" for h in r.json())


async def test_history_eval_runs_every_variant_and_mode(
    rag_app: FastAPI, mine: dict[str, str], owner_conn: psycopg.Connection
) -> None:
    """Plumbing of `make history-eval` (fake embeddings: numbers mean nothing here)."""
    from aeoi_rag import history_eval

    await _embed(rag_app)
    qs = [
        history_eval.HistoryQuery(
            id="t1", query=f"zq{mine['tag']} vortex saturation", error_code="ERR_X",
            service_key="tax-api", category="code",
        )
    ]  # fmt: skip
    res = await history_eval.run(rag_app.state.sessionmaker, rag_app.state.embedder, qs)
    assert set(res) == {f"{v}/{m}" for v in history_eval.VARIANTS for m in history_eval.MODES}
    assert all(r["summary"]["leakage_violations"] == 0 for r in res.values())
    assert res["symptom/hybrid"]["summary"]["hit@1"]["mean"] == 1.0  # our rows are 'code'
    assert "symptom+code/hybrid+boost" in history_eval.format_table(res)


async def test_vector_branch_returns_its_full_depth_among_near_duplicates(
    rag_app: FastAPI, owner_conn: psycopg.Connection
) -> None:
    """Mac smoke regression: HNSW returns at most ef_search rows (default 40 < DEPTH) in an
    arbitrary order among near-identical write-ups, so RRF rewarded whichever twins came back.
    With a wide ef_search the vector branch returns its full DEPTH."""
    tag = uuid.uuid4().hex[:6]
    n = int(tag, 16) % 10**5
    keys = [f"INC-6{n:05d}{i:02d}" for i in range(60)]
    for i, key in enumerate(keys):
        owner_conn.execute(
            "INSERT INTO rag.historical_incidents (id, incident_key, title, summary, root_cause,"
            " root_cause_category, remediation, service_keys, severity, occurred_at, resolved_at,"
            " allowed_groups) VALUES (gen_random_uuid(), %s, %s, %s, 'pool', 'code', 'rollback',"
            " %s, 'SEV2', now() - interval '30 days', now() - interval '29 days', %s)",
            (key, f"yq{tag} pool twin", f"yq{tag} connection pool exhausted", [f"svc{i}-api"],
             ["eng-all"]),
        )  # fmt: skip
    try:
        await _embed(rag_app)
        emb = rag_app.state.embedder
        q = f"yq{tag} connection pool exhausted"
        rows = await history.search(
            rag_app.state.sessionmaker, q, ["eng-all"], k=60, qvec=await emb.embed_query(q),
            model=emb.model, use_keyword=False,
        )  # fmt: skip
        assert len([r for r in rows if r["rank_v"] is not None]) == history.DEPTH
    finally:
        owner_conn.execute(
            "DELETE FROM rag.historical_incidents WHERE incident_key = ANY(%s)", (keys,)
        )
