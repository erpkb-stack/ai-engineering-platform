"""CLI (Phase 8). The user token comes from $AEOI_USER_TOKEN (make agent-run mints a dev one).

python -m aeoi_orchestrator run-log-agent INC-10001 [--start ISO --end ISO] [--route local|fast]
python -m aeoi_orchestrator compare INC-10001 [--routes local,fast]   # same task, two models
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aeoi_db.config import find_repo_root
from aeoi_observability import configure_logging
from aeoi_orchestrator.config import Settings
from aeoi_orchestrator.runner import Runner, RunnerError, RunSummary


def _dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise SystemExit("--start/--end need a timezone, e.g. 2026-10-02T09:00:00Z")
    return dt


def _print(summary: RunSummary) -> None:
    r = summary.result
    print(
        f"\n{summary.incident_key}  investigation={summary.investigation_id}  status={summary.status}"
    )
    if summary.error:
        print(f"  error: {summary.error}")
    if r is None:
        return
    t = r.trace
    llm = t.llm_calls[-1] if t.llm_calls else None
    print(
        f"  agent {t.agent} v{t.agent_version}  prompt {t.prompt_id} v{t.prompt_version} "
        f"({(t.prompt_sha256 or '')[:12]})"
    )
    if t.cached:
        print(
            "  ⚠ CACHED: labels came from the llm-gateway cache - NO model ran now. Latency and\n"
            "    tokens are not the model's. Use --no-cache (make agent-run ... NOCACHE=1)."
        )
    print(
        f"  model={t.model}  fallback={llm.fallback_used if llm else '-'}  "
        f"tokens in/out={t.input_tokens}/{t.output_tokens}  cost=${t.cost_usd}  "
        f"latency={t.latency_ms} ms  tool calls={len(t.tool_calls)}"
    )
    if r.degraded:
        print(f"  DEGRADED: {r.degraded}")
    q = r.label_quality
    print(
        f"  labels passed validation {q.labels_accepted}/{q.clusters_sent} (format/ids only, "
        f"NOT correctness), citations dropped "
        f"{q.citations_dropped}, unknown cluster ids {q.unknown_cluster_ids}"
    )
    print("  FACTS (code):")
    for f in r.facts:
        print(f"   - {f.statement}  [{', '.join(e.evidence_id[:14] + '…' for e in f.evidence)}]")
    for n in r.notes:
        print(f"   · note: {n}")
    print("  CLUSTERS:")
    for c in r.clusters:
        flag = "  ⚠ untrusted text flagged" if c.untrusted_content_flagged else ""
        print(
            f"   {c.id} [{c.labelled_by}] {c.label}  ({c.category}, {c.count_window or '?'} in "
            f"window, {c.count_sampled} sampled){flag}"
        )
    print(f"  evidence stored: {summary.evidence_inserted} new")


async def _runner(settings: Settings) -> tuple[Runner, Any]:
    engine = create_async_engine(settings.sqlalchemy_url(), pool_size=settings.db_pool_size)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    http = httpx.AsyncClient(timeout=30.0, trust_env=False)
    token = settings.service_token_file.read_text().strip()
    return Runner(settings, sessions, http, token), (engine, http)


async def _close(handles: Any) -> None:
    engine, http = handles
    await http.aclose()
    await engine.dispose()


def _requested_by(token: str) -> str:
    """Who asked, for the investigation row. Read WITHOUT verifying: this string is a label;
    the token itself is verified by every service it is sent to (incident, agents, tools)."""
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
        return f"user:{claims['uid']}"
    except (jwt.PyJWTError, KeyError):
        return "user:unknown"


def _git() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607 - dev CLI, git on PATH
            capture_output=True,
            text=True,
            check=False,
            cwd=find_repo_root(),
        )
        return out.stdout.strip() or "unknown"
    except OSError:
        return "unknown"


async def run_one(args: argparse.Namespace, user_token: str) -> int:
    runner, handles = await _runner(Settings())
    try:
        summary = await runner.run_log_analysis(
            args.incident,
            user_token,
            start=_dt(args.start),
            end=_dt(args.end),
            llm_route=args.route,
            llm_cache=not args.no_cache,
            requested_by=_requested_by(user_token),
        )
    except RunnerError as exc:
        print(f"✘ {exc}", file=sys.stderr)
        return 1
    finally:
        await _close(handles)
    if args.json:
        print(summary.result.model_dump_json(indent=2) if summary.result else "{}")
    else:
        _print(summary)
    return 0 if summary.status == "COMPLETE" else 1


def _row(route: str, s: RunSummary) -> dict[str, Any]:
    r = s.result
    if r is None:
        return {"route": route, "status": s.status, "error": s.error}
    t = r.trace
    llm = t.llm_calls[-1] if t.llm_calls else None
    return {
        "route": route,
        "status": s.status,
        "investigation_id": str(s.investigation_id),
        "model": t.model,
        "fallback_used": llm.fallback_used if llm else None,
        "cached": t.cached,
        "llm_error": llm.error if llm else None,
        "llm_latency_ms": llm.latency_ms if llm else None,
        "agent_latency_ms": t.latency_ms,
        "input_tokens": t.input_tokens,
        "output_tokens": t.output_tokens,
        "cost_usd": str(t.cost_usd),
        "schema_repairs": t.retries,
        "degraded": r.degraded,
        "label_quality": r.label_quality.model_dump(),
        "labels": {
            c.id: {"label": c.label, "category": c.category, "by": c.labelled_by}
            for c in r.clusters
        },
        "facts": [f.statement for f in r.facts],
    }


def _facts_equal(rows: list[dict[str, Any]]) -> bool:
    """True only when every run HAS facts and they match (no facts == nothing proven)."""
    facts = [r.get("facts") for r in rows]
    return all(facts) and len({json.dumps(f) for f in facts}) == 1


def _usable_chains(gateway: dict[str, Any], routes: list[str]) -> dict[str, list[str]]:
    """Per route, only the models whose provider is configured (has a key / is reachable).
    Mac finding: with no Anthropic key the `fast` chain still lists Claude first, so a check on
    chain[0] passed and the run silently fell back to llama."""
    providers = gateway.get("providers", {})
    model_provider = gateway.get("models", {})
    out: dict[str, list[str]] = {}
    for name in routes:
        chain = list(gateway["routes"][name]["chain"])
        out[name] = [
            m for m in chain if providers.get(model_provider.get(m, ""), {}).get("configured", True)
        ]
    return out


def _route_chains(routes: list[str]) -> dict[str, Any] | None:
    """Ask the llm-gateway which models each route resolves to (dev preflight; uses the agents
    token, which has llm:invoke). None = could not check."""
    root = find_repo_root()
    token_file = root / "secrets" / "agents_service_token.txt"
    url = os.environ.get("AEOI_ORCH_LLM_GATEWAY_URL", "http://localhost:8005")
    try:
        r = httpx.get(
            f"{url}/v1/routes",
            timeout=5,
            trust_env=False,
            headers={"Authorization": f"Bearer {token_file.read_text().strip()}"},
        )
        r.raise_for_status()
        body = r.json()
        return {
            "chains": {name: list(body["routes"][name]["chain"]) for name in routes},
            "usable": _usable_chains(body, routes),
        }
    except (OSError, httpx.HTTPError, KeyError, ValueError):
        return None


async def compare(args: argparse.Namespace, user_token: str) -> int:
    routes = [r.strip() for r in args.routes.split(",")]
    info = _route_chains(routes)
    chains = None if info is None else info["chains"]
    if info is None:
        print("! could not read llm-gateway routes; continuing without the same-model check")
    else:
        for name, chain in info["chains"].items():
            usable = info["usable"][name]
            skipped = [m for m in chain if m not in usable]
            note = f"   (not configured, skipped: {', '.join(skipped)})" if skipped else ""
            print(f"  route {name}: {' -> '.join(chain)}{note}")
        # The model that will REALLY answer first is the first one whose provider is configured.
        primaries = {u[0] for u in info["usable"].values() if u}
        if len(primaries) < len(routes) and not args.allow_same_model:
            print(
                "✘ these routes would be answered by the SAME model - that is not a model "
                "comparison.\n"
                "  Usually: no Anthropic key, so `fast` falls back to llama. Save the key to\n"
                "  secrets/anthropic_api_key.txt (or export ANTHROPIC_API_KEY in the gateway's\n"
                "  shell), then `make stop-llm && make run-llm`. The startup log must NOT show\n"
                "  `llm_provider_disabled`.",
                file=sys.stderr,
            )
            return 2
    rows = []
    for route in routes:
        runner, handles = await _runner(Settings())
        try:
            s = await runner.run_log_analysis(
                args.incident,
                user_token,
                start=_dt(args.start),
                end=_dt(args.end),
                llm_route=route.strip(),
                llm_cache=False,  # a cache hit is not a model run: never compare cached answers
                requested_by=_requested_by(user_token),
            )
        except RunnerError as exc:
            print(f"✘ {route}: {exc}", file=sys.stderr)
            return 1
        finally:
            await _close(handles)
        _print(s)
        if s.result is None:  # infrastructure failure, not a model result: nothing to compare
            print(
                f"\n✘ {route}: the run produced no result ({s.error}). Stopped before the next\n"
                "  route; no comparison file written. Check that make run-agents, run-tools,\n"
                "  run-llm and dev are all running, then retry.",
                file=sys.stderr,
            )
            return 1
        rows.append(_row(route.strip(), s))
    facts_equal = _facts_equal(rows)
    models = [r.get("model") for r in rows]
    valid = (
        all(models)
        and len(set(models)) == len(models)
        and not any(r.get("cached") or r.get("fallback_used") for r in rows)
    )
    print(
        "\n| route | model | fallback | LLM ms | tokens in/out | cost $ | repairs | labels ok | "
        "cites dropped | degraded |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        q = r.get("label_quality", {})
        print(
            f"| {r['route']} | {r.get('model')} | {r.get('fallback_used')} | "
            f"{r.get('llm_latency_ms')} | {r.get('input_tokens')}/{r.get('output_tokens')} | "
            f"{r.get('cost_usd')} | {r.get('schema_repairs')} | "
            f"{q.get('labels_accepted')}/{q.get('clusters_sent')} | "
            f"{q.get('citations_dropped')} | {r.get('degraded') or '-'} |"
        )
    print(f"\nfacts identical across models: {facts_equal}  (they must be: facts are code)")
    if not valid:
        print(
            f"! NOT a valid model comparison: models answered = {models} "
            "(a model that never answered, the same model twice, a fallback, or a cache hit)"
        )
    out_dir = find_repo_root() / "data" / "eval" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"agent-log-compare-{datetime.now(UTC).strftime('%Y-%m-%dT%H%M%S')}.json"
    path.write_text(
        json.dumps(
            {
                "kind": "agent-model-comparison",
                "caveat": "ONE incident, one run per model: an anecdote, not an eval. Do not quote as a "
                "quality number; quote latency/cost/repair counts with n=1.",
                "environment": {
                    "git_commit": _git(),
                    "host": platform.node(),
                    "machine": platform.machine(),
                    "incident": args.incident,
                },
                "facts_identical": facts_equal,
                "valid_comparison": valid,
                "route_chains": chains,
                "runs": rows,
            },
            indent=2,
        )
        + "\n"  # end-of-file-fixer hook: a file without a final newline blocks the commit
    )
    print(f"wrote {path.relative_to(find_repo_root())}")
    return 0 if facts_equal and all(r["status"] == "COMPLETE" for r in rows) else 1


async def status(args: argparse.Namespace, user_token: str) -> int:
    runner, handles = await _runner(Settings())
    try:
        rows = await runner.status(await runner.incident_id(args.incident, user_token))
    except RunnerError as exc:
        print(f"✘ {exc}", file=sys.stderr)
        return 1
    finally:
        await _close(handles)
    if not rows:
        print("no investigations")
    for r in rows:
        print(
            f"{r['started_at']}  {r['status']:<10} task={r['task'] or '-':<9} "
            f"{r['seconds'] if r['seconds'] is not None else '…'}s  route={r['route'] or '-'} "
            f"model={r['model'] or '-'}  {r['investigation']}"
        )
        for key in ("degraded", "error"):
            if r[key]:
                print(f"    {key}: {r[key]}")
    return 0


async def repost(args: argparse.Namespace) -> int:
    from uuid import UUID

    runner, handles = await _runner(Settings())
    try:
        n = await runner.repost_evidence(UUID(args.investigation_id))
    except RunnerError as exc:
        print(f"✘ {exc}", file=sys.stderr)
        return 1
    finally:
        await _close(handles)
    print(f"✔ evidence re-posted: {n} new row(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="aeoi_orchestrator",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status", help="investigations of an incident (needs AEOI_USER_TOKEN)")
    st.add_argument("incident")
    rp = sub.add_parser("repost-evidence", help="retry the evidence POST of an investigation")
    rp.add_argument("investigation_id")
    for name in ("run-log-agent", "compare"):
        sp = sub.add_parser(name)
        sp.add_argument("incident", help="INC-10001 or the incident uuid")
        sp.add_argument("--start")
        sp.add_argument("--end")
        if name == "run-log-agent":
            sp.add_argument("--route", choices=["fast", "local", "reasoning"])
            sp.add_argument("--json", action="store_true")
            sp.add_argument(
                "--no-cache", action="store_true", help="force a real model call (no cache hit)"
            )
        else:
            sp.add_argument("--routes", default="local,fast")
            sp.add_argument("--allow-same-model", action="store_true")
    args = p.parse_args(argv)
    if args.cmd == "status":
        token = os.environ.get("AEOI_USER_TOKEN", "").strip()
        configure_logging("orchestrator", level="WARNING", json_output=False)
        return asyncio.run(status(args, token))
    if args.cmd == "repost-evidence":
        configure_logging("orchestrator", level="WARNING", json_output=False)
        return asyncio.run(repost(args))
    token = os.environ.get("AEOI_USER_TOKEN", "").strip()
    if not token:
        print("set AEOI_USER_TOKEN (make agent-run does it for you)", file=sys.stderr)
        return 2
    configure_logging("orchestrator", level="WARNING", json_output=False)
    if args.cmd == "run-log-agent":
        return asyncio.run(run_one(args, token))
    return asyncio.run(compare(args, token))


if __name__ == "__main__":
    raise SystemExit(main())
