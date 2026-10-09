"""CLI over the orchestrator SERVICE (Phase 9). Needs `make run-orch` on :8002.
The user token comes from $AEOI_USER_TOKEN (the make targets mint a dev one).

python -m aeoi_orchestrator investigate INC-10001 [--agents metrics,deployment] [--no-cache]
python -m aeoi_orchestrator run-log-agent INC-10001 [--route local|fast] [--no-cache]
python -m aeoi_orchestrator compare INC-10001 [--routes local,fast]   # same task, two models
python -m aeoi_orchestrator status INC-10001
python -m aeoi_orchestrator cancel <investigation_id> --reason "..."
python -m aeoi_orchestrator resume <investigation_id>
python -m aeoi_orchestrator repost-evidence <investigation_id>   # direct DB + service token
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import secrets
import subprocess
import sys
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx

from aeoi_db.config import find_repo_root
from aeoi_models.api.agents import AgentRunResult
from aeoi_observability import configure_logging
from aeoi_orchestrator.config import Settings

POLL_S = 2.0


class CliError(Exception):
    pass


def _dt(value: str | None) -> str | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise SystemExit("--start/--end need a timezone, e.g. 2026-10-02T09:00:00Z")
    return dt.isoformat()


def _base() -> str:
    return os.environ.get("AEOI_ORCH_URL", "http://localhost:8002").rstrip("/")


def _client(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=_base(),
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
        trust_env=False,
    )


def _check(r: httpx.Response, *ok: int) -> Any:
    if r.status_code not in ok:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        raise CliError(f"HTTP {r.status_code}: {str(detail)[:400]}")
    return r.json() if r.content else {}


def _start_and_wait(
    c: httpx.Client, incident: str, body_extra: dict[str, Any], timeout_s: float
) -> tuple[dict[str, Any], AgentRunResult | None]:
    try:
        r = c.post(
            "/v1/investigations",
            json={"incident": incident, **body_extra},
            headers={"Idempotency-Key": f"cli-{secrets.token_hex(8)}"},
        )
    except httpx.TransportError as exc:
        raise CliError(
            f"orchestrator unreachable at {_base()} ({type(exc).__name__}): is `make run-orch` running?"
        ) from exc
    accepted = _check(r, 202)
    inv_id = accepted["investigation_id"]
    print(f"  started investigation {inv_id} (polling every {POLL_S:.0f}s) ", end="", flush=True)
    deadline = time.monotonic() + timeout_s
    inv: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            inv = _check(c.get(f"/v1/investigations/{inv_id}"), 200)
        except httpx.TransportError:
            # the orchestrator restarting is the Phase 9 crash/resume demo: keep waiting
            print("(orchestrator down, waiting)", end="", flush=True)
            time.sleep(POLL_S)
            continue
        if inv["status"] != "RUNNING":
            break
        print(".", end="", flush=True)
        time.sleep(POLL_S)
    print()
    if inv.get("status") == "RUNNING":
        raise CliError(f"still RUNNING after {timeout_s:.0f}s: `status {incident}` to follow it")
    trace = _check(c.get(f"/v1/investigations/{inv_id}/trace"), 200)
    result = None
    others: dict[str, AgentRunResult] = {}
    for e in trace["executions"]:
        if not e["output"]:
            continue
        out = AgentRunResult.model_validate(e["output"])
        if e["agent"] == "log_analysis":
            result = out
        else:
            others[e["agent"]] = out
    inv["_others"] = others  # printed by _print (Phase 10 agents); not part of the API
    return inv, result


def _print_other(agent: str, r: AgentRunResult) -> None:
    """Phase 10 code-only agents: no model, so no labels/tokens - just facts and what backs them."""
    t = r.trace
    print(
        f"\n  agent {agent} v{t.agent_version}  (code only, no model)  "
        f"tool calls={len(t.tool_calls)}  latency={t.latency_ms} ms"
    )
    if r.degraded:
        print(f"  DEGRADED: {r.degraded}")
    for f in r.facts:
        print(f"   - {f.statement}  [{', '.join(e.evidence_id[:14] + '…' for e in f.evidence)}]")
    for ref in r.references:
        flag = "  ⚠ untrusted text flagged" if ref.untrusted_content_flagged else ""
        print(f"   ref {ref.kind} rank {ref.rank} document {ref.document_id}{flag}")
    for n in r.notes:
        print(f"   · note: {n}")


def _print(inv: dict[str, Any], r: AgentRunResult | None) -> None:
    print(f"\ninvestigation={inv['investigation_id']}  status={inv['status']}")
    if inv.get("error"):
        print(f"  error: {inv['error']}")
    for t in inv.get("tasks", []):
        if t["attempt"] > 1:
            print(f"  ⚠ task {t['agent']} ran {t['attempt']} times (resumed after a crash)")
    for agent, other in sorted((inv.get("_others") or {}).items()):
        _print_other(agent, other)
    if r is None:
        return
    print()
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
    print(f"  evidence stored: {inv.get('evidence_inserted')} new")


def _timeout() -> float:
    return Settings().investigation_deadline_s + 30


def run_one(args: argparse.Namespace, token: str) -> int:
    extra: dict[str, Any] = {"llm_cache": not args.no_cache}
    if args.agents:
        extra["agents"] = args.agents.split(",")
    if args.route:
        extra["llm_route"] = args.route
    if args.start:
        extra |= {"start": _dt(args.start), "end": _dt(args.end)}
    with _client(token) as c:
        inv, result = _start_and_wait(c, args.incident, extra, _timeout())
    if args.json:
        print(result.model_dump_json(indent=2) if result else "{}")
    else:
        _print(inv, result)
    return 0 if inv["status"] == "COMPLETE" else 1


def _row(route: str, inv: dict[str, Any], r: AgentRunResult | None) -> dict[str, Any]:
    if r is None:
        return {"route": route, "status": inv["status"], "error": inv.get("error")}
    t = r.trace
    llm = t.llm_calls[-1] if t.llm_calls else None
    return {
        "route": route,
        "status": inv["status"],
        "investigation_id": inv["investigation_id"],
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
    token_file = find_repo_root() / "secrets" / "agents_service_token.txt"
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


def compare(args: argparse.Namespace, token: str) -> int:
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
    with _client(token) as c:
        for route in routes:
            # never compare a cache hit; only the agent that HAS a model (Phase 10)
            extra: dict[str, Any] = {
                "llm_route": route,
                "llm_cache": False,
                "agents": ["log_analysis"],
            }
            if args.start:
                extra |= {"start": _dt(args.start), "end": _dt(args.end)}
            inv, result = _start_and_wait(c, args.incident, extra, _timeout())
            _print(inv, result)
            if result is None:  # infrastructure failure, not a model result: nothing to compare
                print(
                    f"\n✘ {route}: the run produced no result ({inv.get('error')}). Stopped before\n"
                    "  the next route; no comparison file written. Check that run-orch, run-agents,\n"
                    "  run-tools, run-llm and dev are all running, then retry.",
                    file=sys.stderr,
                )
                return 1
            rows.append(_row(route, inv, result))
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
                "caveat": "ONE incident, one run per model: an anecdote, not an eval. Do not quote "
                "as a quality number; quote latency/cost/repair counts with n=1.",
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


def _utc(iso: str) -> str:
    """Display in UTC. Mac finding (Phase 8 facts, again here): slicing an ISO string with a
    -05:00 offset and appending 'Z' prints local time labelled as UTC - 5 hours wrong."""
    return datetime.fromisoformat(iso).astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def status(args: argparse.Namespace, token: str) -> int:
    with _client(token) as c:
        rows = _check(c.get(f"/v1/incidents/{args.incident}/investigations"), 200)
    if not rows:
        print("no investigations")
    for inv in rows:
        secs = (
            int(
                (
                    datetime.fromisoformat(inv["finished_at"])
                    - datetime.fromisoformat(inv["started_at"])
                ).total_seconds()
            )
            if inv["finished_at"]
            else None
        )
        tasks = ", ".join(
            f"{t['agent']}={t['status']}" + (f"(x{t['attempt']})" if t["attempt"] > 1 else "")
            for t in inv["tasks"]
        )
        route = (inv.get("plan") or {}).get("route") or "-"
        print(
            f"{_utc(inv['started_at'])}  {inv['status']:<10} {secs if secs is not None else '…'}s"
            f"  route={route}  {tasks or 'no tasks'}  {inv['investigation_id']}"
        )
        if inv["status"] == "RUNNING" and inv.get("graph_next"):
            print(f"    next step: {', '.join(inv['graph_next'])}")
        if inv.get("error"):
            print(f"    error: {inv['error']}")
        for t in inv["tasks"]:
            if t.get("degraded"):
                print(f"    degraded ({t['agent']}): {t['degraded']}")
    return 0


def control(args: argparse.Namespace, token: str) -> int:
    with _client(token) as c:
        if args.cmd == "cancel":
            inv = _check(
                c.post(
                    f"/v1/investigations/{args.investigation_id}/cancel",
                    json={"reason": args.reason},
                ),
                200,
            )
        else:
            inv = _check(c.post(f"/v1/investigations/{args.investigation_id}/resume"), 200)
    print(f"{inv['investigation_id']}  status={inv['status']}  next={inv.get('graph_next')}")
    return 0


async def repost(args: argparse.Namespace) -> int:
    """Retry the evidence POST of a FAILED investigation from its recorded batches."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from aeoi_orchestrator.clients import CallError, IncidentClient
    from aeoi_orchestrator.salvage import post_recorded_evidence
    from aeoi_orchestrator.store import Store

    settings = Settings()
    engine = create_async_engine(settings.sqlalchemy_url(), pool_size=1)
    store = Store(async_sessionmaker(engine, expire_on_commit=False))
    inv_id = UUID(args.investigation_id)
    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as http:
        incidents = IncidentClient(
            http, settings.incident_service_url, settings.service_token_file.read_text().strip()
        )
        try:
            inv = await store.get(inv_id)
            if inv is None:
                print("✘ unknown investigation", file=sys.stderr)
                return 1
            _, inserted, error = await post_recorded_evidence(
                store, incidents, inv_id, inv.incident_id, "repost"
            )
            if error:
                raise CallError(error)
            reopened = await store.complete_after_repost(inv_id)
        except CallError as exc:
            print(f"✘ {exc}", file=sys.stderr)
            return 1
        finally:
            await engine.dispose()
    print(
        f"✔ evidence re-posted: {inserted} new row(s)"
        + (f"; investigation now {reopened}" if reopened else "")
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="aeoi_orchestrator",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status", help="investigations of an incident")
    st.add_argument("incident")
    for name in ("cancel", "resume"):
        cp = sub.add_parser(name)
        cp.add_argument("investigation_id")
        if name == "cancel":
            cp.add_argument("--reason", default="cancelled from the CLI")
    rp = sub.add_parser("repost-evidence", help="retry the evidence POST of an investigation")
    rp.add_argument("investigation_id")
    for name in ("investigate", "run-log-agent", "compare"):
        sp = sub.add_parser(name)
        sp.add_argument("incident", help="INC-10001 or the incident uuid")
        sp.add_argument("--start")
        sp.add_argument("--end")
        if name == "investigate":
            sp.add_argument("--agents", help="comma list; default = every configured agent")
        if name in ("investigate", "run-log-agent"):
            sp.add_argument("--route", choices=["fast", "local", "reasoning"])
            sp.add_argument("--json", action="store_true")
            sp.add_argument(
                "--no-cache", action="store_true", help="force a real model call (no cache hit)"
            )
        else:
            sp.add_argument("--routes", default="local,fast")
            sp.add_argument("--allow-same-model", action="store_true")
    args = p.parse_args(argv)
    configure_logging("orchestrator", level="WARNING", json_output=False)
    if args.cmd == "repost-evidence":
        return asyncio.run(repost(args))
    token = os.environ.get("AEOI_USER_TOKEN", "").strip()
    if not token:
        print("set AEOI_USER_TOKEN (the make targets do it for you)", file=sys.stderr)
        return 2
    try:
        if args.cmd == "run-log-agent":  # Phase 8/9 behaviour: the one agent with a model
            args.agents = "log_analysis"
            return run_one(args, token)
        if args.cmd == "investigate":
            return run_one(args, token)
        if args.cmd == "compare":
            return compare(args, token)
        if args.cmd == "status":
            return status(args, token)
        return control(args, token)
    except CliError as exc:
        print(f"✘ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
