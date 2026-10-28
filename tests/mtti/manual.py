"""Phase 29e (ADR-047): the MANUAL side of MTTI. `make manual` - you investigate a planted
incident with the SAME read tools the agents use, as yourself (an SRE token, manual mode at the
tool gateway), and the clock runs from when the case opens until you `answer`.

You see only the alert (service, title, detection time). The truth stays in eval.eval_cases:
this program holds it in memory to SCORE after the clock stops and never prints it - the
answers are revealed only by `make mtti` after all 6 cases. Rules: tests/mtti/mttilib.py.

State is saved after every command (data/mtti/manual-session.json): closing the program does
NOT stop the clock (no pause). Interrupted for real? `abandon <reason>`: the case is listed as
abandoned, never scored, and the next reserve case is offered. DEV ONLY.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(HERE), str(ROOT / "tests/load")]
import mttilib as m  # noqa: E402

API = os.environ.get("AEOI_MTTI_API", "http://localhost:8000")
SESSION = ROOT / "data/mtti/manual-session.json"
HELP = """commands (times are minutes relative to detection; defaults from=-60 to=+10):
  alert                                   the alert again
  logs <service> [code=X] [contains=Y] [from=-60] [to=+10] [order=asc|desc] [limit=50]
  metrics <service> <metric> [from=-120] [to=+10]     metrics: """ + ", ".join(
    ("http_5xx_per_min", "p95_latency_ms", "requests_per_sec", "cpu_util", "db_pool_utilization",
     "db_connections_active", "db_query_latency_ms", "cache_hit_ratio", "thread_pool_utilization"))  # fmt: skip
HELP += """
  deploys <service> [from=-1430] [to=+10]  deploys of a service (the tools allow 24 h max)
  diff <DEPLOY-n>                          config diff of a deploy
  runbooks <words...>   incidents <words...>   catalog <service>
  tool <name> <json-args>                  any other read tool, e.g. tool compare_commits {...}
  answer <kind> <subject>                  STOPS the clock; kind: change|saturation|traffic|alternative
                                           subject: the DEPLOY key, or the metric (db_pool_utilization)
  abandon <reason>                         a real interruption: not scored, next reserve case
  help | quit (the clock keeps running)"""


# ------------------------------------------------------------------ io
def load_cases() -> list[dict[str, Any]]:
    import psycopg

    from aeoi_db.config import libpq_dsn

    with psycopg.connect(libpq_dsn()) as conn:
        rows = conn.execute(
            "SELECT c.case_key, c.input, c.expected FROM eval.eval_cases c JOIN eval.datasets d"
            " ON d.id = c.dataset_id WHERE d.name = 'investigations' AND d.version IN (1, 2)"
        ).fetchall()
    if not rows:
        raise SystemExit("no planted cases: make eval-seed")
    return m.detectable([{"key": r[0], "input": r[1], "expected": r[2]} for r in rows])


def save(state: dict[str, Any]) -> None:
    SESSION.parent.mkdir(parents=True, exist_ok=True)
    tmp = SESSION.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1) + "\n")
    tmp.replace(SESSION)


def invoke(token: str, tool: str, args: dict[str, Any]) -> tuple[int, Any]:
    body = json.dumps({"args": args}).encode()
    req = urllib.request.Request(f"{API}/api/v1/tools/{tool}/invoke", data=body, method="POST",  # noqa: S310
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})  # fmt: skip
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail")
        except (ValueError, AttributeError):
            detail = None
        return e.code, detail
    except OSError as e:
        return 0, type(e).__name__


def show(data: Any) -> None:
    """Tool output is untrusted: every value goes through m.clean (no control characters)."""
    if not isinstance(data, dict):
        print(" ", m.clean(data))
        return
    for k, v in data.items():
        if k == "items" or (isinstance(v, (list, dict)) and k != "counts_by_error_code"):
            continue
        print(f"  {k}: {m.clean(v)}")
    for it in data.get("items") or []:
        pts = it.get("points") if isinstance(it, dict) else None
        if pts:
            print("  stats:", m.clean(it.get("stats")))
            step = max(1, len(pts) // 30)
            shown = " ".join(f"{m.utc(p['ts'])[11:16]}={p['value']:g}" for p in pts[::step])
            print("  points (UTC):", shown)
            continue
        fields = {k: v for k, v in it.items() if k != "evidence_id" and v not in (None, [], "")} \
            if isinstance(it, dict) else {"value": it}  # fmt: skip
        print("  -", m.clean("  ".join(f"{k}={v}" for k, v in fields.items()), 400))


# ------------------------------------------------------------------ commands
def build(cmd: str, words: list[str], detected: datetime) -> tuple[str, dict[str, Any]]:
    pos, kv = m.opts(words)
    if cmd == "logs" and pos:
        a, b = m.rel_window(detected, int(kv.get("from", -60)), int(kv.get("to", 10)))
        args: dict[str, Any] = {"service_key": pos[0], "start": a, "end": b,
                                "order": kv.get("order", "asc"), "limit": int(kv.get("limit", 50))}  # fmt: skip
        if "code" in kv:
            args["error_code"] = kv["code"]
        if "contains" in kv:
            args["contains"] = kv["contains"]
        return "search_logs", args
    if cmd == "metrics" and len(pos) == 2:
        a, b = m.rel_window(detected, int(kv.get("from", -120)), int(kv.get("to", 10)))
        return "query_metrics", {"service_key": pos[0], "metric": pos[1], "start": a, "end": b}
    if cmd == "deploys" and pos:
        a, b = m.rel_window(detected, int(kv.get("from", -1430)), int(kv.get("to", 10)))
        return "get_deployment", {"service_key": pos[0], "start": a, "end": b}
    if cmd == "diff" and pos:
        return "get_config_diff", {"deploy_key": pos[0]}
    if cmd == "runbooks" and words:
        return "search_runbooks", {"query": " ".join(words)}
    if cmd == "incidents" and words:
        return "search_incidents", {"query": " ".join(words)}
    if cmd == "catalog" and pos:
        return "query_service_catalog", {"service_key": pos[0]}
    if cmd == "tool" and len(words) >= 2:
        return words[0], json.loads(" ".join(words[1:]))
    raise ValueError("wrong arguments: type help")


def next_case(state: dict[str, Any]) -> str | None:
    used = {c["case"] for c in state["done"]}
    counted = [c for c in state["done"] if not c.get("abandoned")]
    if len(counted) >= m.SAMPLE:
        return None
    return next((k for k in state["order"] if k not in used), None)


def main() -> int:
    if os.environ.get("AEOI_ENV") == "production":
        raise SystemExit("dev only")
    from tokens import tokens

    cases = {c["key"]: c for c in load_cases()}
    state = json.loads(SESSION.read_text()) if SESSION.exists() else {
        "seed": m.SEED, "order": m.sample_order(list(cases)), "done": [], "current": None}  # fmt: skip
    token = tokens("SRE")[0]
    while True:
        cur = state["current"]
        if cur is None:
            key = next_case(state)
            if key is None:
                print(
                    f"All {m.SAMPLE} cases done. Next: make mtti (reveals the answers and the report)."
                )
                return 0
            n = len([c for c in state["done"] if not c.get("abandoned")]) + 1
            try:
                input(f"\nCase {n} of {m.SAMPLE}. The clock starts when you press Enter. ")
            except EOFError:  # no terminal (piped input ran out): open nothing
                return 0
            cur = state["current"] = {"case": key, "opened_at": datetime.now(UTC).isoformat(),
                                      "t0": time.time(), "calls": []}  # fmt: skip
            save(state)
            print(HELP)
        inp = cases[cur["case"]]["input"]
        detected = datetime.fromisoformat(inp["detected_at"])
        print(f"\nALERT  {inp['title']}\n  services: {', '.join(inp['affected_services'])}"
              f"  severity: {inp['severity']}  detected: {m.utc(detected.isoformat())}"
              "  (every time shown is UTC)")  # fmt: skip
        while state["current"] is cur:
            elapsed = time.time() - cur["t0"]
            try:
                line = input(f"[{int(elapsed // 60):02d}:{int(elapsed % 60):02d}] > ").strip()
            except EOFError:
                line = "quit"
            if not line:
                continue
            words = shlex.split(line)
            cmd, rest = words[0], words[1:]
            if cmd == "quit":
                print("Saved. The clock keeps running: run make manual to continue.")
                return 0
            if cmd == "help":
                print(HELP)
                continue
            if cmd == "alert":
                break
            if cmd in ("answer", "abandon"):
                seconds = round(time.time() - cur["t0"], 1)
                rec = {"case": cur["case"], "opened_at": cur["opened_at"], "seconds": seconds,
                       "tool_calls": len(cur["calls"]), "calls": cur["calls"]}  # fmt: skip
                if cmd == "abandon":
                    rec["abandoned"] = " ".join(rest)[:200] or "interrupted"
                else:
                    try:
                        rec["answer"] = m.parse_answer(rest)
                    except ValueError as exc:
                        print(" ", exc)
                        continue
                state["done"].append(rec)
                state["current"] = None
                save(state)
                print(f"Recorded after {seconds:.0f} s. The answer is revealed only by make mtti.")
                break
            try:
                tool, args = build(cmd, rest, detected)
            except (ValueError, json.JSONDecodeError) as exc:
                print(" ", exc)
                continue
            status, out = invoke(token, tool, args)
            cur["calls"].append({"at_s": round(time.time() - cur["t0"], 1), "tool": tool,
                                 "args": args, "status": status})  # fmt: skip
            save(state)
            if status == 200:
                show(out.get("data"))
            else:
                print(f"  {tool}: {status} {m.clean(out)}")
    return 0  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
