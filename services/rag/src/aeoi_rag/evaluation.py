"""Retrieval evaluation: recall@k, MRR@10, leakage and quarantine checks, per mode.

Rules (AGENTS.md rule 8): numbers come only from a real run, and every result file records
WHAT produced it (embedding model, pgvector version, dataset hash, git commit, host).
A run with the fake (hashing) embedder is labelled NOT A BASELINE: it measures plumbing.

Metrics are document-level (a query is answered if the right DOCUMENT is in the top k,
whichever chunk matched). Confidence intervals: percentile bootstrap, 2,000 resamples,
fixed seed. With ~50 queries per type the intervals are wide - that is the honest answer.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import platform
import random
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aeoi_models.api.search import SearchMode
from aeoi_rag.rerank import LLMReranker
from aeoi_rag.search import Retriever

KS = (1, 3, 5, 10)
ABORT_AFTER = 3


@dataclass(frozen=True)
class EvalQuery:
    id: str
    query: str
    relevant: tuple[str, ...]
    kind: str
    as_groups: tuple[str, ...]
    forbidden: tuple[str, ...]


def load_queries(path: Path) -> list[EvalQuery]:
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            d = json.loads(line)
            out.append(
                EvalQuery(
                    d["id"],
                    d["query"],
                    tuple(d["relevant"]),
                    d["kind"],
                    tuple(d["as_groups"]),
                    tuple(d.get("forbidden", ())),
                )
            )
    return out


def _bootstrap(values: list[float], seed: int = 7, n: int = 2000) -> tuple[float, float]:
    if not values:
        return (0.0, 0.0)
    rnd = random.Random(seed)  # noqa: S311 - statistics, not security
    means = sorted(sum(rnd.choice(values) for _ in values) / len(values) for _ in range(n))
    return (means[int(0.025 * n)], means[int(0.975 * n) - 1])


def _summary(per_query: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"n": len(per_query)}
    for k in KS:
        vals = [1.0 if q["rank"] is not None and q["rank"] <= k else 0.0 for q in per_query]
        out[f"recall@{k}"] = round(sum(vals) / len(vals), 4) if vals else None
        if k == 5:
            lo, hi = _bootstrap(vals)
            out["recall@5_ci95"] = [round(lo, 4), round(hi, 4)]
    rr = [1.0 / q["rank"] if q["rank"] is not None else 0.0 for q in per_query]
    out["mrr@10"] = round(sum(rr) / len(rr), 4) if rr else None
    lo, hi = _bootstrap(rr)
    out["mrr@10_ci95"] = [round(lo, 4), round(hi, 4)]
    lat = sorted(q["latency_ms"] for q in per_query)
    out["latency_ms_p50"] = lat[len(lat) // 2] if lat else None
    out["latency_ms_p95"] = lat[min(len(lat) - 1, int(0.95 * len(lat)))] if lat else None
    return out


async def run_eval(
    retriever: Retriever,
    queries: list[EvalQuery],
    *,
    modes: list[str],
    reranker: LLMReranker | None = None,
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for mode_name in modes:
        base, _, extra = mode_name.partition("+")
        mode = SearchMode(base)
        use_rerank = extra == "rerank"
        if use_rerank and reranker is None:
            raise ValueError("mode with +rerank needs a reranker")
        per_query: list[dict[str, Any]] = []
        leaks: list[dict[str, Any]] = []
        rerank_failures = 0
        rerank_attempts = 0
        aborted: str | None = None
        for q in queries:
            t0 = time.perf_counter()
            res = await retriever.search(q.query, list(q.as_groups), k=10, mode=mode)
            hits = res.hits
            if use_rerank and reranker is not None:
                out = await reranker.rerank(q.query, hits)
                hits = out.hits
                rerank_attempts += 1
                rerank_failures += 0 if out.applied else 1
                # Fail fast: if the first calls all fail, the rest will too (CPU-bound model,
                # missing route...). Don't burn an hour proving it - record why and stop.
                if rerank_attempts == ABORT_AFTER and rerank_failures == ABORT_AFTER:
                    aborted = (
                        f"first {ABORT_AFTER} rerank calls failed ({out.error}) - "
                        "rerank is not viable in this setup; mode aborted"
                    )
                    break
            latency = int((time.perf_counter() - t0) * 1000)
            uris: list[str] = []
            for h in hits:
                if h.source_uri not in uris:
                    uris.append(h.source_uri)
            bad = sorted(set(uris) & set(q.forbidden))
            if bad:
                leaks.append({"id": q.id, "kind": q.kind, "returned_forbidden": bad})
            if q.kind in ("keyword", "paraphrase"):
                rank = next((i + 1 for i, u in enumerate(uris) if u in q.relevant), None)
                per_query.append(
                    {
                        "id": q.id,
                        "kind": q.kind,
                        "rank": rank,
                        "latency_ms": latency,
                        "top3": uris[:3],
                    }
                )
        by_kind = {
            kind: _summary([p for p in per_query if p["kind"] == kind])
            for kind in sorted({p["kind"] for p in per_query})
        }
        results[mode_name] = {
            "overall": _summary(per_query),
            "by_kind": by_kind,
            "leakage": {
                "queries": sum(1 for q in queries if q.kind == "leakage"),
                "violations": [v for v in leaks if v["kind"] == "leakage"],
            },
            "quarantine": {
                "queries": sum(1 for q in queries if q.kind == "adversarial"),
                "violations": [v for v in leaks if v["kind"] == "adversarial"],
            },
            "rerank_failures": rerank_failures if use_rerank else None,
            "aborted": aborted,
            "misses": [p for p in per_query if p["rank"] is None or p["rank"] > 5],
        }
    return results


def environment(embedding_model: str | None, pgvector: str | None, dataset: Path) -> dict[str, Any]:
    try:
        commit = (
            subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607 - dev tool on PATH
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
            or None
        )
    except (OSError, subprocess.SubprocessError):
        commit = None
    fake = embedding_model is None or embedding_model.startswith("fake")
    return {
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_commit": commit,
        "host": f"{platform.system()} {platform.machine()}",
        "embedding_model": embedding_model,
        "pgvector": pgvector,
        "dataset": dataset.name,
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest()[:16],
        "is_baseline": not fake,
        "warning": "FAKE EMBEDDINGS - measures plumbing only, NOT a retrieval baseline"
        if fake
        else None,
    }


def format_table(results: dict[str, Any]) -> str:
    lines = [
        "| mode | kind | n | R@1 | R@5 (95% CI) | R@10 | MRR@10 | p50 ms | leaks | quarantine |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for mode, r in results.items():
        if r.get("aborted"):
            lines.append(f"| {mode} | ABORTED: {r['aborted']} |||||||||")
            continue
        rows = [("all", r["overall"]), *r["by_kind"].items()]
        for kind, s in rows:
            ci = s.get("recall@5_ci95") or [0, 0]
            lines.append(
                f"| {mode} | {kind} | {s['n']} | {s['recall@1']} | {s['recall@5']} ({ci[0]}-{ci[1]}) "
                f"| {s['recall@10']} | {s['mrr@10']} | {s['latency_ms_p50']} "
                f"| {len(r['leakage']['violations'])} | {len(r['quarantine']['violations'])} |"
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------- fusion sweep
GRID = {
    "rrf_k": (1, 5, 10, 20, 60),
    "depth": (10, 20, 40),
    "wk_identifiers": (1.0, 2.0, 4.0),
    "wv_descriptive": (1.0, 2.0, 4.0),
}


def split_dev_test(
    queries: list[EvalQuery], seed: int = 13
) -> tuple[list[EvalQuery], list[EvalQuery]]:
    """Stratified 50/50 split per query kind. Tune on dev, REPORT on test - otherwise the
    'best' setting is just fitted to the questions it is scored on."""
    rnd = random.Random(seed)  # noqa: S311 - experiment design, not security
    dev: list[EvalQuery] = []
    test: list[EvalQuery] = []
    for kind in sorted({q.kind for q in queries}):
        group = sorted((q for q in queries if q.kind == kind), key=lambda q: q.id)
        rnd.shuffle(group)
        half = len(group) // 2
        dev += group[:half]
        test += group[half:]
    return dev, test


def _metrics(ranks: list[int | None]) -> dict[str, float]:
    n = len(ranks) or 1
    return {
        "recall@1": sum(1 for r in ranks if r is not None and r <= 1) / n,
        "recall@5": sum(1 for r in ranks if r is not None and r <= 5) / n,
        "mrr@10": sum(1.0 / r for r in ranks if r is not None and r <= 10) / n,
    }


async def run_sweep(
    retriever: Retriever, queries: list[EvalQuery], settings: Any
) -> dict[str, Any]:
    from aeoi_rag.search import fuse, identifier_terms

    rel = [q for q in queries if q.kind in ("keyword", "paraphrase")]
    dev, test = split_dev_test(rel)
    max_depth = int(max(GRID["depth"]))
    cands = {q.id: await retriever.candidates(q.query, list(q.as_groups), max_depth) for q in rel}
    has_ident = {q.id: bool(identifier_terms(q.query)) for q in rel}

    def ranks(qs: list[EvalQuery], cfg: dict[str, Any]) -> list[int | None]:
        out: list[int | None] = []
        for q in qs:
            wv, wk = (
                (1.0, cfg["wk_identifiers"]) if has_ident[q.id] else (cfg["wv_descriptive"], 1.0)
            )
            if cfg.get("only") == "vector":
                wv, wk = 1.0, 0.0
            elif cfg.get("only") == "keyword":
                wv, wk = 0.0, 1.0
            uris = fuse(
                cands[q.id],
                rrf_k=cfg["rrf_k"],
                wv=wv,
                wk=wk,
                depth=cfg["depth"],
                max_per_doc=settings.max_chunks_per_document,
                k=10,
            )
            out.append(next((i + 1 for i, u in enumerate(uris) if u in q.relevant), None))
        return out

    default = {
        "rrf_k": settings.rrf_k,
        "depth": settings.vector_candidates,
        "wk_identifiers": settings.keyword_weight_identifiers,
        "wv_descriptive": settings.vector_weight_no_identifiers,
    }
    configs = [dict(zip(GRID, values, strict=True)) for values in itertools.product(*GRID.values())]
    scored = []
    for cfg in configs:
        m = _metrics(ranks(dev, cfg))
        # prefer higher MRR; on ties prefer the config closest to plain RRF (fewer knobs moved)
        moved = sum(cfg[k] != v for k, v in default.items())
        scored.append((m["mrr@10"], m["recall@5"], -moved, cfg))
    scored.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    best = scored[0][3]

    def report(cfg: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, qs in (("dev", dev), ("test", test)):
            r = ranks(qs, cfg)
            out[name] = {k: round(v, 4) for k, v in _metrics(r).items()}
            rr = [1.0 / x if x is not None and x <= 10 else 0.0 for x in r]
            lo, hi = _bootstrap(rr)
            out[name]["mrr@10_ci95"] = [round(lo, 4), round(hi, 4)]
            for kind in ("keyword", "paraphrase"):
                sub = [x for x, q in zip(r, qs, strict=True) if q.kind == kind]
                out[name][kind] = {k: round(v, 4) for k, v in _metrics(sub).items()}
        return out

    return {
        "split": {"dev": len(dev), "test": len(test), "seed": 13},
        "grid": {k: list(v) for k, v in GRID.items()},
        "default": {"config": default, **report(default)},
        "best_on_dev": {"config": best, **report(best)},
        "vector_only": report({**default, "depth": max_depth, "only": "vector"}),
        "keyword_only": report({**default, "depth": max_depth, "only": "keyword"}),
        "top5_dev": [
            {"mrr@10": round(s[0], 4), "recall@5": round(s[1], 4), "config": s[3]}
            for s in scored[:5]
        ],
    }


def format_sweep(r: dict[str, Any]) -> str:
    lines = [
        "| config | split | MRR@10 (95% CI) | R@1 | R@5 | keyword R@1 | paraphrase R@5 |",
        "|---|---|---|---|---|---|---|",
    ]
    for name in ("default", "best_on_dev", "vector_only", "keyword_only"):
        for split in ("dev", "test"):
            m = r[name][split]
            ci = m["mrr@10_ci95"]
            lines.append(
                f"| {name} | {split} | {m['mrr@10']} ({ci[0]}-{ci[1]}) | {m['recall@1']} | "
                f"{m['recall@5']} | {m['keyword']['recall@1']} | {m['paraphrase']['recall@5']} |"
            )
    return "\n".join(lines)
