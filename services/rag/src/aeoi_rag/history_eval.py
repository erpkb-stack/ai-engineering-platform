"""Phase 13 (ADR-023): honest eval of similar-incident search.

Question: given how an engineer (or the agent) DESCRIBES a new incident, does the search bring
back past incidents with the same root-cause category?

Dataset `data/eval/history_queries_v1.jsonl`: hand-written paraphrases of each category's
symptoms, NOT copied from the corpus templates (category names and template words avoided).
Two variants per query:
  symptom       - words only
  symptom+code  - words + the error code. In the synthetic corpus each category has its own
                  code (1:1), so this variant is EASY for keyword search by construction. It is
                  reported so nobody mistakes it for a semantic result.
Modes: keyword (Phase 7), vector, hybrid, hybrid+boost (same-service vote).
Leakage: the caller has only `eng-all`; any returned incident without `eng-all` is a violation.
In practice `history.search` asserts the ACL first and ABORTS the eval on a violation, so the
counter is a second line, and 0 is expected by construction (review).

Known bias (stated in the report): the queries were written by the person who wrote the corpus
templates; the corpus has ~12 distinct texts (one per category) x services, so this measures
"find the right template", not ranking among many different real write-ups.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aeoi_rag import history
from aeoi_rag.embedder import Embedder
from aeoi_rag.evaluation import _bootstrap

CALLER_GROUPS = ("eng-all",)
MODES = ("keyword", "vector", "hybrid", "hybrid+boost")
VARIANTS = ("symptom", "symptom+code")
K = 10


@dataclass(frozen=True)
class HistoryQuery:
    id: str
    query: str
    error_code: str
    service_key: str
    category: str


def load(path: Path) -> list[HistoryQuery]:
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            d = json.loads(line)
            out.append(HistoryQuery(**{f: d[f] for f in HistoryQuery.__dataclass_fields__}))
    return out


def text_for(q: HistoryQuery, variant: str) -> str:
    return q.query if variant == "symptom" else f"{q.query}. error code {q.error_code}"


def score(categories: list[str], expected: str) -> dict[str, float]:
    """Pure: metrics for one ranked list of categories."""
    first = next((i for i, c in enumerate(categories, 1) if c == expected), None)
    top5 = categories[:5]
    return {
        "hit@1": 1.0 if first == 1 else 0.0,
        "hit@5": 1.0 if first is not None and first <= 5 else 0.0,
        "mrr@10": 1.0 / first if first is not None and first <= K else 0.0,
        "precision@5": (sum(c == expected for c in top5) / 5) if top5 else 0.0,
    }


def summarise(per_query: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"n": len(per_query)}
    for m in ("hit@1", "hit@5", "mrr@10", "precision@5"):
        vals = [p[m] for p in per_query]
        lo, hi = _bootstrap(vals)
        out[m] = {"mean": round(sum(vals) / max(len(vals), 1), 3), "ci95": [lo, hi]}
    out["leakage_violations"] = sum(p["leaks"] for p in per_query)
    out["empty"] = sum(1 for p in per_query if not p["returned"])
    return out


async def run(
    sessions: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    queries: list[HistoryQuery],
    iterative_scan: bool = False,
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for variant in VARIANTS:
        for mode in MODES:
            per = []
            for q in queries:
                text = text_for(q, variant)
                qvec = await embedder.embed_query(text) if mode != "keyword" else None
                rows = await history.search(
                    sessions,
                    text,
                    CALLER_GROUPS,
                    k=K,
                    qvec=qvec,
                    model=embedder.model if qvec is not None else None,
                    service_key=q.service_key if mode == "hybrid+boost" else None,
                    use_keyword=mode != "vector",
                    iterative_scan=iterative_scan,
                )
                cats = [r["root_cause_category"] for r in rows]
                leaks = sum(1 for r in rows if history.OPEN_GROUP not in r["allowed_groups"])
                per.append(
                    {
                        "id": q.id,
                        **score(cats, q.category),
                        "leaks": leaks,
                        "returned": len(rows),
                        "top1": cats[0] if cats else None,
                    }
                )
            results[f"{variant}/{mode}"] = {"summary": summarise(per), "per_query": per}
    return results


def format_table(results: dict[str, Any]) -> str:
    lines = [
        f"{'variant/mode':<28}{'n':>4}{'hit@1':>8}{'hit@5':>8}{'mrr@10':>8}{'p@5':>8}"
        f"{'leaks':>7}{'empty':>7}"
    ]
    for name, r in results.items():
        s = r["summary"]
        lines.append(
            f"{name:<28}{s['n']:>4}{s['hit@1']['mean']:>8.3f}{s['hit@5']['mean']:>8.3f}"
            f"{s['mrr@10']['mean']:>8.3f}{s['precision@5']['mean']:>8.3f}"
            f"{s['leakage_violations']:>7}{s['empty']:>7}"
        )
    return "\n".join(lines)
