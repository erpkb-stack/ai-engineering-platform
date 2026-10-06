from __future__ import annotations

import uuid
from typing import Any

from aeoi_rag.evaluation import EvalQuery, run_eval, split_dev_test
from aeoi_rag.rerank import RerankOutcome
from aeoi_rag.search import Candidate, Hit, SearchResult, fuse


def cand(uri: str, rv: int | None, rk: int | None, n: int = 0) -> Candidate:
    return Candidate(uuid.UUID(int=n), uuid.uuid5(uuid.NAMESPACE_URL, uri), uri, rv, rk)


def test_rrf_rewards_overlap_the_mechanism_behind_the_mac_baseline() -> None:
    """A doc that is #1 in ONE branch loses to a doc that is mediocre in BOTH when k=60:
    1/61 = 0.0164 < 1/90 + 1/90 = 0.0222. With k=5 the #1 wins: 1/6 > 2/35."""
    cands = [cand("right", 1, None, 1), cand("mediocre", 30, 30, 2)]
    assert fuse(cands, rrf_k=60, wv=1, wk=1, depth=40, max_per_doc=2, k=10)[0] == "mediocre"
    assert fuse(cands, rrf_k=5, wv=1, wk=1, depth=40, max_per_doc=2, k=10)[0] == "right"


def test_depth_and_weights() -> None:
    cands = [cand("a", 15, None, 1), cand("b", None, 1, 2)]
    assert fuse(cands, rrf_k=60, wv=1, wk=1, depth=10, max_per_doc=2, k=10) == [
        "b"
    ]  # a beyond depth
    assert fuse(cands, rrf_k=5, wv=4, wk=1, depth=20, max_per_doc=2, k=10)[0] == "a"  # 4/20 > 1/6
    assert fuse(cands, rrf_k=60, wv=0, wk=1, depth=40, max_per_doc=2, k=10) == ["b"]  # keyword only


def test_max_chunks_per_document() -> None:
    doc = [Candidate(uuid.UUID(int=i), uuid.UUID(int=99), "same", i, None) for i in range(1, 5)]
    other = cand("other", 5, None, 50)
    assert fuse([*doc, other], rrf_k=60, wv=1, wk=1, depth=40, max_per_doc=2, k=3) == [
        "same",
        "other",
    ]


def test_split_is_stratified_disjoint_and_deterministic() -> None:
    qs = [
        EvalQuery(f"q{i}", "x", ("u",), "keyword" if i % 2 else "paraphrase", ("g",), ())
        for i in range(20)
    ]
    dev, test = split_dev_test(qs)
    assert {q.id for q in dev}.isdisjoint({q.id for q in test})
    assert len(dev) == len(test) == 10
    assert sum(q.kind == "keyword" for q in dev) == 5
    assert split_dev_test(qs) == (dev, test)


class _Retriever:
    async def search(self, query: str, groups: list[str], **kw: Any) -> SearchResult:
        hit = Hit(
            uuid.uuid4(),
            uuid.uuid4(),
            "t",
            "markdown",
            "docpack://x",
            "INTERNAL",
            0,
            "c",
            0.1,
            1,
            None,
            0.9,
            ["g"],
        )
        return SearchResult([hit, hit], "m", {})


class _SlowReranker:
    calls = 0

    async def rerank(self, query: str, hits: list[Hit]) -> RerankOutcome:
        _SlowReranker.calls += 1
        return RerankOutcome(hits, applied=False, error="timeout")


async def test_rerank_mode_aborts_after_three_failures() -> None:
    qs = [EvalQuery(f"q{i}", "x", ("docpack://x",), "keyword", ("g",), ()) for i in range(50)]
    res = await run_eval(_Retriever(), qs, modes=["hybrid+rerank"], reranker=_SlowReranker())  # type: ignore[arg-type]
    assert _SlowReranker.calls == 3
    assert "not viable" in res["hybrid+rerank"]["aborted"]
