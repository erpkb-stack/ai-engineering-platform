"""rag CLI (operator tooling; connects as the least-privilege `rag_svc` login).

python -m aeoi_rag ingest [--root data/sample-documents]   parse + chunk + embed a doc pack
python -m aeoi_rag chunk-db                                 chunk the seeded rag.documents
python -m aeoi_rag embed [--reembed]                        embed pending chunks (resumable)
python -m aeoi_rag stats
python -m aeoi_rag eval [--modes hybrid,vector,keyword] [--rerank]
python -m aeoi_rag embed-history [--reembed]                 vectors for rag.historical_incidents
python -m aeoi_rag history-eval                              similar-incident search eval (ADR-023)
python -m aeoi_rag history-explain --query "..." [--service s]  why each past incident ranked
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from typing import Any

from sqlalchemy import text

from aeoi_db.config import find_repo_root
from aeoi_rag import history, history_eval
from aeoi_rag.config import Settings
from aeoi_rag.db import make_engine, make_sessionmaker
from aeoi_rag.evaluation import (
    environment,
    format_sweep,
    format_table,
    load_queries,
    run_eval,
    run_sweep,
)
from aeoi_rag.ingest import Ingestor
from aeoi_rag.main import make_embedder, make_llm_client, make_reranker
from aeoi_rag.search import Retriever, detect_iterative_scan


def _progress(done: int, total: int, started: float) -> None:
    rate = done / max(time.perf_counter() - started, 1e-6)
    eta = (total - done) / rate if rate else 0
    sys.stderr.write(f"\r  embedded {done}/{total} chunks  {rate:5.1f}/s  eta {eta:5.0f}s ")
    sys.stderr.flush()


async def _main(args: argparse.Namespace) -> int:
    settings = Settings()
    engine = make_engine(settings.sqlalchemy_url(), 2)
    sessions = make_sessionmaker(engine)
    client = make_llm_client(settings)
    embedder = make_embedder(settings, client)
    ingestor = Ingestor(sessions, embedder, settings)
    root = find_repo_root()
    try:
        if args.command in ("ingest", "chunk-db"):
            if args.command == "ingest":
                report = await ingestor.ingest_pack((root / args.root).resolve())
            else:
                report = await ingestor.chunk_db_documents()
            print(
                json.dumps(
                    {
                        "documents": report.counts(),
                        "chunks": sum(o.chunks for o in report.outcomes),
                        "pii_scrubbed": sum(o.pii for o in report.outcomes),
                        "secrets_redacted": sum(o.secrets for o in report.outcomes),
                        "seconds": round(report.seconds, 1),
                    }
                )
            )
            for o in report.outcomes:
                if o.status in ("quarantined", "rejected"):
                    print(f"  {o.status:<11} {o.source_uri}  ({o.detail})")
            if args.no_embed:
                return 0
        if args.command in ("ingest", "chunk-db", "embed"):
            if getattr(args, "reembed", False):
                await embedder.embed_query("warm-up")  # learn the current model name
                n = await ingestor.reset_embeddings_for_other_models(embedder.model or "")
                print(f"cleared {n} vectors from other embedding models")
            started = time.perf_counter()
            n = await ingestor.embed_pending(progress=lambda d, t: _progress(d, t, started))
            secs = time.perf_counter() - started
            sys.stderr.write("\n")
            print(
                json.dumps(
                    {
                        "embedded": n,
                        "model": embedder.model,
                        "seconds": round(secs, 1),
                        "chunks_per_s": round(n / secs, 2) if n and secs else None,
                    }
                )
            )
            return 0
        if args.command == "stats":
            print(json.dumps(await ingestor.stats(), indent=1, default=str))
            return 0
        if args.command == "bench":
            async with sessions() as s:
                texts = list(
                    (
                        await s.execute(
                            text("SELECT content FROM rag.document_chunks ORDER BY id LIMIT :n"),
                            {"n": args.n},
                        )
                    ).scalars()
                )
            await embedder.embed_query("warm-up")  # first Ollama call loads the model
            t0 = time.perf_counter()
            await embedder.embed_documents(texts)
            secs = time.perf_counter() - t0
            lat = []
            for i in range(10):
                t1 = time.perf_counter()
                await embedder.embed_query(f"query number {i} about connection pools")
                lat.append((time.perf_counter() - t1) * 1000)
            lat.sort()
            print(
                json.dumps(
                    {
                        "model": embedder.model,
                        "chunks": len(texts),
                        "chunks_per_s": round(len(texts) / secs, 2),
                        "query_embed_ms_p50": round(lat[len(lat) // 2]),
                        "query_embed_ms_max": round(lat[-1]),
                    }
                )
            )
            return 0
        if args.command == "sweep":
            dataset = root / args.dataset
            retriever = Retriever(
                sessions, embedder, settings, iterative_scan=await detect_iterative_scan(sessions)
            )
            await embedder.embed_query("warm-up")
            async with sessions() as s:
                pgv = await s.scalar(
                    text("SELECT extversion FROM pg_extension WHERE extname='vector'")
                )
            sweep = await run_sweep(retriever, load_queries(dataset), settings)
            env = environment(embedder.model, pgv, dataset)
            out_dir = root / "data" / "eval" / "results"
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / f"rag-sweep-{env['timestamp'][:19].replace(':', '')}.json"
            out.write_text(json.dumps({"environment": env, "sweep": sweep}, indent=1) + "\n")
            if env["warning"]:
                print(f"!! {env['warning']}")
            print(format_sweep(sweep))
            best = sweep["best_on_dev"]["config"]
            print("\nbest on dev:", json.dumps(best))
            print("to try it (it is NOT applied automatically):")
            print(
                f"  AEOI_RAG_RRF_K={best['rrf_k']} AEOI_RAG_VECTOR_CANDIDATES={best['depth']} "
                f"AEOI_RAG_KEYWORD_CANDIDATES={best['depth']} "
                f"AEOI_RAG_KEYWORD_WEIGHT_IDENTIFIERS={best['wk_identifiers']} "
                f"AEOI_RAG_VECTOR_WEIGHT_NO_IDENTIFIERS={best['wv_descriptive']} make rag-eval"
            )
            print(f"-> {out.relative_to(root)}")
            return 0
        if args.command == "embed-history":
            n, model = await history.embed_pending(sessions, embedder, reembed=args.reembed)
            async with sessions() as s:
                left = await s.scalar(
                    text("SELECT count(*) FROM rag.historical_incidents WHERE embedding IS NULL")
                )
            print(json.dumps({"embedded": n, "model": model, "without_vector": left}))
            return 0 if not left else 1
        if args.command == "history-explain":
            # operator debugging: groups come from the ARGUMENT (this CLI is the rag_svc login,
            # like eval), never use it as a user-facing path
            qvec = await embedder.embed_query(args.query)
            iterative = await detect_iterative_scan(sessions)
            rows = await history.search(
                sessions, args.query, args.groups.split(","), k=args.k, qvec=qvec,
                model=embedder.model, service_key=args.service, shared_only=args.shared_only,
                iterative_scan=iterative,
            )  # fmt: skip
            print(
                f"model={embedder.model} iterative_scan={iterative} ef_search={history.EF_SEARCH}"
                f" vector_branch_rows={rows[0]['vec_hits'] if rows else 0} (depth {history.DEPTH})"
            )
            print(
                f"{'#':>3} {'incident':<10} {'service':<24} {'category':<28} {'kw':>4} {'vec':>4} {'score':>7}"
            )
            for i, r in enumerate(rows, 1):
                print(
                    f"{i:>3} {r['incident_key']:<10} {(r['service_keys'] or ['-'])[0]:<24} "
                    f"{r['root_cause_category']:<28} {r['rank_k'] or '-':>4} {r['rank_v'] or '-':>4} "
                    f"{r['score']:>7.4f}"
                )
            async with sessions() as s, s.begin():
                await s.execute(
                    text("SELECT set_config('hnsw.ef_search', :v, true)"),
                    {"v": str(history.EF_SEARCH)},
                )
                plan = (await s.execute(text(
                    "EXPLAIN SELECT h.id FROM rag.historical_incidents h WHERE h.embedding IS NOT "
                    "NULL AND h.embedding_model = :m ORDER BY h.embedding <=> CAST(:v AS vector) "
                    "LIMIT :n"), {"m": embedder.model or "", "v": history._vec(qvec),
                                  "n": history.DEPTH})).scalars().all()  # fmt: skip
            steps = [re.sub(r"'\[[^']*\]'::vector", "<query vector>", x.split("(cost")[0].strip())
                     for x in plan]  # fmt: skip
            print("vector branch plan:", " | ".join(steps))
            return 0
        if args.command == "history-eval":
            dataset = root / args.dataset
            hqueries = history_eval.load(dataset)
            await embedder.embed_query("warm-up")
            async with sessions() as s:
                pgv = await s.scalar(
                    text("SELECT extversion FROM pg_extension WHERE extname='vector'")
                )
                missing = await s.scalar(
                    text(
                        "SELECT count(*) FROM rag.historical_incidents "
                        "WHERE embedding IS NULL OR embedding_model <> :m"
                    ),
                    {"m": embedder.model or ""},
                )
            if missing:
                print(
                    f"!! {missing} past incidents have no vector for {embedder.model}: "
                    "run `make embed-history` first (vector numbers would be wrong)"
                )
                return 1
            results = await history_eval.run(
                sessions, embedder, hqueries, iterative_scan=await detect_iterative_scan(sessions)
            )
            env = environment(embedder.model, pgv, dataset)
            out_dir = root / "data" / "eval" / "results"
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / f"history-{env['timestamp'][:19].replace(':', '')}.json"
            out.write_text(json.dumps({"environment": env, "results": results}, indent=1) + "\n")
            if env["warning"]:
                print(f"!! {env['warning']}")
            print(history_eval.format_table(results))
            print("note: symptom+code is easy for keyword BY CONSTRUCTION (1 code per category)")
            print(f"\nembedding_model={env['embedding_model']} -> {out.relative_to(root)}")
            leaks = sum(r["summary"]["leakage_violations"] for r in results.values())
            return 1 if leaks else 0
        if args.command == "eval":
            dataset = root / args.dataset
            queries = load_queries(dataset)
            retriever = Retriever(
                sessions, embedder, settings, iterative_scan=await detect_iterative_scan(sessions)
            )
            modes = [m.strip() for m in args.modes.split(",") if m.strip()]
            if args.rerank:
                modes.append("hybrid+rerank")
            reranker = make_reranker(settings, client) if args.rerank else None
            await embedder.embed_query(
                "warm-up"
            )  # model name for the report; first call warms Ollama
            async with sessions() as s:
                pgv = await s.scalar(
                    text("SELECT extversion FROM pg_extension WHERE extname='vector'")
                )
            results = await run_eval(retriever, queries, modes=modes, reranker=reranker)
            env = environment(embedder.model, pgv, dataset)
            out_report: dict[str, Any] = {
                "environment": env,
                "settings": {
                    "rrf_k": settings.rrf_k,
                    "vector_candidates": settings.vector_candidates,
                    "keyword_candidates": settings.keyword_candidates,
                    "keyword_weight_identifiers": settings.keyword_weight_identifiers,
                    "chunk_target_tokens": settings.chunk_target_tokens,
                    "rerank_route": settings.rerank_route if args.rerank else None,
                },
                "results": results,
            }
            out_dir = root / "data" / "eval" / "results"
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / f"rag-{env['timestamp'][:19].replace(':', '')}.json"
            out.write_text(json.dumps(out_report, indent=1) + "\n")
            if env["warning"]:
                print(f"!! {env['warning']}")
            print(format_table(results))
            print(
                f"\nembedding_model={env['embedding_model']} pgvector={pgv} -> {out.relative_to(root)}"
            )
            violations = sum(
                len(r["leakage"]["violations"]) + len(r["quarantine"]["violations"])
                for r in results.values()
            )
            return 1 if violations else 0
        return 2
    finally:
        await client.aclose()
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="aeoi_rag", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="command", required=True)
    ing = sub.add_parser("ingest")
    ing.add_argument("--root", default="data/sample-documents")
    ing.add_argument("--no-embed", action="store_true")
    cdb = sub.add_parser("chunk-db")
    cdb.add_argument("--no-embed", action="store_true")
    emb = sub.add_parser("embed")
    emb.add_argument("--reembed", action="store_true", help="re-embed vectors from other models")
    sub.add_parser("stats")
    ev = sub.add_parser("eval")
    ev.add_argument("--dataset", default="data/eval/rag_queries_v1.jsonl")
    ev.add_argument("--modes", default="keyword,vector,hybrid")
    ev.add_argument("--rerank", action="store_true", help="also run hybrid+rerank (LLM calls!)")
    eh = sub.add_parser("embed-history")
    eh.add_argument("--reembed", action="store_true", help="clear all vectors first")
    he = sub.add_parser("history-eval")
    he.add_argument("--dataset", default="data/eval/history_queries_v1.jsonl")
    hx = sub.add_parser("history-explain")
    hx.add_argument("--query", required=True)
    hx.add_argument("--service", default=None, help="boost this service (as the agent does)")
    hx.add_argument("--groups", default="eng-all")
    hx.add_argument("--k", type=int, default=20)
    hx.add_argument("--shared-only", action="store_true", default=True)
    sw = sub.add_parser("sweep")
    sw.add_argument("--dataset", default="data/eval/rag_queries_v1.jsonl")
    bn = sub.add_parser("bench")
    bn.add_argument("--n", type=int, default=64)
    return asyncio.run(_main(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
