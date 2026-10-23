"""Phase 25a (ADR-037): run the investigation eval - the PRODUCT path, scored against the truth.

    make eval-seed                        (once, after make db-seed: the scenario pack)
    make eval-investigations [CASES=S01,S11] [COST=1] [BASELINE=data/eval/baselines/x.json]

For each case of dataset `investigations` v1 (eval.eval_cases): create the incident via the
api, POST /investigate, wait, read the hypotheses, score them against `expected` (planted).
Results: one eval.eval_runs row + eval.evaluations (one row per case and metric) + a JSON file
in data/eval/results/. The numbers we quote are the ones in those files.

Paid models: the runner reads the LLM gateway's routes first. If any route can reach a paid
provider it refuses unless COST=1, and prints the ACTUAL cost (from the investigations) after.
Scores (per case):
  top1          detectable: the top non-rejected hypothesis is the planted cause
  in_top3       detectable: the cause is among the top 3 non-rejected hypotheses
  confident_wrong  a HIGH hypothesis that is not the cause (undetectable: any HIGH) - the
                worst error: a confident answer an engineer would act on
  decoy_above   a decoy ranks above the cause (or is the top when the cause is missing)
  rule_out_ok   every subject that must be ruled out is REJECTED
  passed        detectable: top1; undetectable: not confident_wrong (the regression unit)
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from aeoi_common.ids import uuid7
from aeoi_db.config import libpq_dsn

ROOT = Path(__file__).resolve().parents[1]
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
FREE_PROVIDERS = ("local_fake", "hosted_fake", "ollama")


# ---------------------------------------------------------------------------------- scoring
def _matches(h: dict[str, Any], culprit: dict[str, Any] | None) -> bool:
    if culprit is None:
        return False
    kind = (h.get("detail") or {}).get("cause_kind")
    return kind == culprit["kind"] and culprit["subject"] in h["statement"]


def _mentions(h: dict[str, Any], subject: str) -> bool:
    return subject in h["statement"]


def score(
    expected: dict[str, Any],
    hyps: list[dict[str, Any]],
    proposal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pure: the planted truth vs the stored hypotheses (as GET /hypotheses returns them) and
    the investigation's rollback proposal (plan.proposal).
    Phase 25b: the band is what the system SHOWS; VALIDATED (critic supported + hard checks)
    and the proposal are what it STANDS BEHIND - scored apart (`high_wrong` vs
    `validated_wrong`). `passed` is unchanged, so the 25a baseline still applies."""
    ordered = sorted(hyps, key=lambda h: h["rank"])
    live = [h for h in ordered if h["status"] != "REJECTED"]
    rejected = [h for h in ordered if h["status"] == "REJECTED"]
    culprit = expected.get("culprit")
    top = live[0] if live else None
    pos = next((i for i, h in enumerate(live) if _matches(h, culprit)), None)
    highs = [h for h in live if h["confidence"] == "HIGH"]
    if expected["detectable"]:  # the answer an engineer would act on is HIGH and wrong
        confident_wrong = (
            top is not None and top["confidence"] == "HIGH" and not _matches(top, culprit)
        )
    else:  # nothing visible caused it: any HIGH claim is a confident wrong answer
        confident_wrong = bool(highs)
    decoy_rank = next(
        (i for i, h in enumerate(live) if any(_mentions(h, d) for d in expected["decoys"])), None
    )
    decoy_above = decoy_rank is not None and (pos is None or decoy_rank < pos)
    rule_out_ok = all(any(_mentions(h, s) for h in rejected) for s in expected["rule_out"])
    top1 = pos == 0
    code_claims = [h for h in live if h.get("produced_by") != "critic"]
    claims = [
        {
            "rank": h["rank"],
            "band": h["confidence"],
            "status": h["status"],
            "kind": (h.get("detail") or {}).get("cause_kind"),
            "critic": (h.get("detail") or {}).get("critic_verdict"),
            "correct": _matches(h, culprit),
        }
        for h in code_claims
    ]  # critic alternatives cannot be matched exactly (no cause kind of ours): counted only
    validated_wrong = any(c["status"] == "VALIDATED" and not c["correct"] for c in claims)
    p = proposal or {}
    deploy = p.get("deploy_key")  # set when the rollback rule held (proposed or refused)
    is_change = bool(culprit) and culprit["kind"] == "change"  # type: ignore[index]
    return {
        "top_status": top["status"] if top else None,
        "claims": claims,
        "critic_alternatives": sum(1 for h in live if h.get("produced_by") == "critic"),
        "validated_wrong": validated_wrong,
        "proposal": deploy,
        "proposal_correct": bool(deploy) and is_change and deploy == culprit["subject"],  # type: ignore[index]
        "proposal_wrong": bool(deploy) and not (is_change and deploy == culprit["subject"]),  # type: ignore[index]
        "proposal_missed": is_change and not deploy,
        "top": top["statement"] if top else None,
        "top_band": top["confidence"] if top else None,
        "ranked_by": (top.get("detail") or {}).get("ranked_by") if top else None,
        "culprit_rank": None if pos is None else pos + 1,
        "top1": top1 if expected["detectable"] else None,
        "in_top3": (pos is not None and pos < 3) if expected["detectable"] else None,
        "confident_wrong": confident_wrong,
        "decoy_above": decoy_above,
        "rule_out_ok": rule_out_ok,
        "hypotheses": len(live),
        "passed": top1 if expected["detectable"] else not confident_wrong,
    }


def summarize(cases: list[dict[str, Any]]) -> dict[str, Any]:
    det = [c for c in cases if c["expected"]["detectable"]]
    und = [c for c in cases if not c["expected"]["detectable"]]
    lat = [c["latency_s"] for c in cases if c.get("latency_s") is not None]

    def rate(xs: list[dict[str, Any]], key: str) -> str:
        return f"{sum(1 for x in xs if x['score'][key])}/{len(xs)}"

    outcomes: dict[str, int] = {}
    for c in cases:
        outcomes[c["outcome"]] = outcomes.get(c["outcome"], 0) + 1
    return {
        "cases": len(cases),
        "detectable": len(det),
        "top1": rate(det, "top1"),
        "in_top3": rate(det, "in_top3"),
        "confident_wrong": rate(cases, "confident_wrong"),
        "undetectable_abstained": f"{sum(1 for c in und if not c['score']['confident_wrong'])}"
        f"/{len(und)}",
        "decoy_above": rate([c for c in cases if c["expected"]["decoys"]], "decoy_above"),
        "rule_out_ok": rate([c for c in cases if c["expected"]["rule_out"]], "rule_out_ok"),
        "passed": rate(cases, "passed"),
        "outcomes": outcomes,
        "latency_s": {"p50": round(statistics.median(lat), 1), "max": round(max(lat), 1)}
        if lat
        else None,
        "cost_usd": round(sum(c.get("cost_usd") or 0 for c in cases), 6),
        "invalid": [c["case"][-3:] for c in cases if c.get("invalid")],
        # Phase 25b: what the system STANDS BEHIND, next to what it shows (confident_wrong)
        "validated_wrong": f"{sum(1 for c in cases if c['score'].get('validated_wrong'))}"
        f"/{len(cases)}",
        "proposals": {
            k: sum(1 for c in cases if c["score"].get(f"proposal_{k}"))
            for k in ("correct", "wrong", "missed")
        },
    }


def regressions(cases: list[dict[str, Any]], baseline: dict[str, Any]) -> list[str]:
    """A case that PASSED in the baseline and fails now. New cases and fixes are not errors."""
    # baseline rows are {"case", "passed", "top"}: no "*_key" field names in a committed file
    # (gitleaks' generic-api-key rule flags a "*_key" name next to a long value)
    was = {c["case"]: c["passed"] for c in baseline["cases"]}
    return [c["case"] for c in cases if was.get(c["case"]) and not c["score"]["passed"]]


# ---------------------------------------------------------------------------------- http
RETRYABLE_STATUS = (502, 503, 504)
RETRY_WAITS_S = (1.0, 3.0)  # two retries, then fail with what the server said


class CallError(RuntimeError):
    """A call that failed after its retries: method, path and the server's answer."""


def send(req: urllib.request.Request, timeout: float, *, retry: bool) -> Any:
    """One HTTP call. `retry` only for a call that is SAFE to repeat - a GET, or a POST with
    an Idempotency-Key (the api replays the first result). First real-model run (Mac,
    2026-10-23): the first call after hours of idle hit a connection the restarted pods had
    dropped - a 503 "retry with the same Idempotency-Key" (Phase 23a fix7), then a
    RemoteDisconnected from the LLM gateway. Both are retryable; the runner gave up."""
    waits = RETRY_WAITS_S if retry else ()
    for attempt in range(len(waits) + 1):
        try:
            with _OPENER.open(req, timeout=timeout) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            if e.code not in RETRYABLE_STATUS or attempt == len(waits):
                body = e.read()[:300].decode("utf-8", "replace")
                raise CallError(f"{req.get_method()} {req.full_url} -> {e.code}: {body}") from e
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            if attempt == len(waits):
                raise CallError(
                    f"{req.get_method()} {req.full_url} -> {type(e).__name__}: {e}"
                ) from e
        time.sleep(waits[attempt])
    raise AssertionError("unreachable")  # pragma: no cover


class Api:
    def __init__(self, base: str, token: str) -> None:
        self.base, self.token = base.rstrip("/"), token

    def call(self, method: str, path: str, body: Any = None, key: str | None = None) -> Any:
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        if key:
            headers["Idempotency-Key"] = key
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)  # noqa: S310
        return send(req, 30, retry=method == "GET" or key is not None)


def paid_models(llm_url: str, service_token: str) -> list[str]:
    req = urllib.request.Request(  # noqa: S310
        llm_url.rstrip("/") + "/v1/routes", headers={"Authorization": f"Bearer {service_token}"}
    )
    d = send(req, 10, retry=True)
    used = {m for route in d["routes"].values() for m in route["chain"]}
    return sorted(m for m in used if d["models"].get(m) not in FREE_PROVIDERS)


def run_case(api: Api, case: dict[str, Any], run_tag: str, timeout_s: float) -> dict[str, Any]:
    t0 = time.monotonic()
    inc = api.call("POST", "/api/v1/incidents", case["input"], f"eval-{run_tag}-{case['key']}")
    start = api.call("POST", f"/api/v1/incidents/{inc['key']}/investigate", None,
                     f"eval-inv-{run_tag}-{case['key']}")  # fmt: skip
    inv: dict[str, Any] = {"status": "RUNNING"}
    while inv["status"] == "RUNNING" and time.monotonic() - t0 < timeout_s:
        time.sleep(2)
        inv = api.call("GET", f"/api/v1/investigations/{start['investigation_id']}")
    latency = round(time.monotonic() - t0, 1)
    hyps = api.call("GET", f"/api/v1/incidents/{inc['key']}/hypotheses")
    runs = api.call("GET", f"/api/v1/incidents/{inc['key']}/investigations")
    cost = sum(float(t.get("cost_usd") or 0) for r in runs for t in r.get("tasks", []))
    error = str(inv.get("error") or "")
    return {
        "case": case["key"],
        "incident": inc["key"],
        "outcome": "TIMEOUT" if inv["status"] == "RUNNING" else inv["status"],
        # evidence the system never read (e.g. a rate-limited query): the case says nothing
        # about the system's reasoning, so the RUN is not trustworthy (exit 1), never "wrong"
        "invalid": "evidence incomplete" in error or inv["status"] == "RUNNING",
        "error": error[:500] or None,
        "validation_passed": ((inv.get("plan") or {}).get("hypotheses") or {}).get("passed"),
        "latency_s": latency,
        "cost_usd": round(cost, 6),
        "expected": case["expected"],
        "score": score(case["expected"], hyps, (inv.get("plan") or {}).get("proposal")),
    }


def rescore(api: Api, path: Path) -> dict[str, Any]:
    """Score a past run again from what the services STORED (hypotheses + the investigation's
    plan) - no investigation runs, no model is called, nothing is written to `eval`.
    Phase 25b: the paid real-model run (2026-10-23) predates the status/proposal scores."""
    old = json.loads(path.read_text())
    cases = []
    for c in old["cases"]:
        hyps = api.call("GET", f"/api/v1/incidents/{c['incident']}/hypotheses")
        runs = api.call("GET", f"/api/v1/incidents/{c['incident']}/investigations")
        plan = next((r.get("plan") or {} for r in runs if r.get("status") != "RUNNING"), {})
        cases.append({**c, "score": score(c["expected"], hyps, plan.get("proposal"))})
    return {**old, "rescored_from": path.name, "summary": summarize(cases), "cases": cases}


# ---------------------------------------------------------------------------------- storage
def load_cases(
    conn: psycopg.Connection[Any], name: str, version: int
) -> tuple[UUID, list[dict[str, Any]]]:
    row = conn.execute(
        "SELECT id FROM eval.datasets WHERE name = %s AND version = %s", (name, version)
    ).fetchone()
    if row is None:
        raise SystemExit(f"dataset {name} v{version} not found: make eval-seed")
    cases = conn.execute(
        "SELECT id, case_key, input, expected FROM eval.eval_cases WHERE dataset_id = %s"
        " ORDER BY case_key",
        (row[0],),
    ).fetchall()
    return row[0], [{"id": c[0], "key": c[1], "input": c[2], "expected": c[3]} for c in cases]


def git_sha() -> str | None:
    """CI sets GITHUB_SHA; a local run records none rather than shelling out to git."""
    return (os.environ.get("GITHUB_SHA") or "")[:40] or None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="investigations")
    ap.add_argument("--version", type=int, default=1)
    ap.add_argument("--cases", default="", help="comma list of suffixes, e.g. S01,S11")
    ap.add_argument("--allow-cost", action="store_true")
    ap.add_argument("--baseline", type=Path, help="fail on a case that passed there and fails now")
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--llm", default="http://localhost:8005")
    ap.add_argument("--timeout-s", type=float, default=240.0)
    # the tool gateway limits each (user, tool) to 60 calls/min, burst 20; one case makes up to
    # 9 query_metrics calls per service. Back to back, 16 cases ran out (first run): pace them.
    ap.add_argument("--pace-s", type=float, default=15.0)
    ap.add_argument("--out-dir", type=Path, default=ROOT / "data/eval/results")
    ap.add_argument("--seed", type=int, default=42, help="the seed `make eval-seed` used")
    ap.add_argument("--rescore", type=Path, help="score a past results file again (free)")
    args = ap.parse_args(argv)

    user = subprocess.run([sys.executable, "-m", "aeoi_api.devtoken", "--role", "SRE"],
                          cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()  # fmt: skip
    api = Api(args.api, user)
    if args.rescore:
        out = rescore(api, args.rescore)
        dest = args.rescore.with_name(args.rescore.stem + "-rescored.json")
        dest.write_text(json.dumps(out, indent=1, default=str) + "\n")
        print(json.dumps(out["summary"]))
        print(f"written: {dest.relative_to(ROOT) if dest.is_relative_to(ROOT) else dest}")
        return 0
    svc_token = (ROOT / "secrets/agents_service_token.txt").read_text().strip()
    try:
        paid = paid_models(args.llm, svc_token)
    except CallError as e:
        print(f"cannot read the LLM gateway's routes (is it running?): {e}")
        return 2
    if paid and not args.allow_cost:
        print(f"the LLM gateway can reach paid models ({', '.join(paid)}): re-run with COST=1 "
              "to spend money, or start it with routing.test.yaml (FAKE models, free).")  # fmt: skip
        return 2
    want = {
        f"{args.dataset}-v{args.version}-{c.strip()}" for c in args.cases.split(",") if c.strip()
    }
    run_id = uuid7()
    started = datetime.now(UTC)
    config = {"dataset": args.dataset, "version": args.version, "paid_models": paid,
              "cases": sorted(want) or "all", "api": args.api}  # fmt: skip
    with psycopg.connect(libpq_dsn(), autocommit=True) as conn:
        dataset_id, cases = load_cases(conn, args.dataset, args.version)
        cases = [c for c in cases if not want or c["key"] in want]
        conn.execute(
            "INSERT INTO eval.eval_runs (id, dataset_id, config, config_sha256, git_sha, seed)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (run_id, dataset_id, Jsonb(config),
             hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
             git_sha(), args.seed),
        )  # fmt: skip
        results = []
        status = "SUCCEEDED"
        try:
            last = 0.0
            for case in cases:
                time.sleep(max(0.0, last + args.pace_s - time.monotonic()))
                last = time.monotonic()
                r = run_case(api, case, str(run_id)[-12:], args.timeout_s)
                results.append(r)
                s = r["score"]
                mark = "INVALID" if r["invalid"] else ("PASS" if s["passed"] else "FAIL")
                print(f"{case['key'][-3:]} {mark:<7} {r['outcome']:<12} "
                      f"{r['latency_s']:>5}s top={s['top_band'] or '-'} rank={s['culprit_rank']} "
                      f"wrongHIGH={s['confident_wrong']} | {(s['top'] or 'no hypothesis')[:70]}")  # fmt: skip
                for metric in ("top1", "in_top3", "confident_wrong", "decoy_above",
                               "rule_out_ok", "passed"):  # fmt: skip
                    if s[metric] is not None:
                        conn.execute(
                            "INSERT INTO eval.evaluations (id, run_id, case_id, metric, value,"
                            " details) VALUES (%s, %s, %s, %s, %s, %s)",
                            (uuid7(), run_id, case["id"], metric, float(s[metric]),
                             Jsonb({"incident": r["incident"], "outcome": r["outcome"]})),
                        )  # fmt: skip
                conn.execute(
                    "INSERT INTO eval.evaluations (id, run_id, case_id, metric, value, details)"
                    " VALUES (%s, %s, %s, 'latency_s', %s, %s)",
                    (uuid7(), run_id, case["id"], r["latency_s"], Jsonb({"top": s["top"]})),
                )
        except CallError as e:
            status = "FAILED"
            print(f"run stopped: {type(e).__name__}: {e}")
        summary = summarize(results)
        conn.execute(
            "UPDATE eval.eval_runs SET status = %s, summary = %s, finished_at = now()"
            " WHERE id = %s",
            (status, Jsonb(summary), run_id),
        )
    out = {"run_id": str(run_id), "started_at": started.isoformat(timespec="seconds"),
           "config": config, "summary": summary, "cases": results}  # fmt: skip
    args.out_dir.mkdir(parents=True, exist_ok=True)
    path = args.out_dir / f"investigations-{started:%Y-%m-%dT%H%M%S}.json"
    path.write_text(json.dumps(out, indent=1, default=str) + "\n")
    print(json.dumps(summary))
    print(f"written: {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    if status != "SUCCEEDED":
        return 1
    if summary["invalid"]:
        print(f"INVALID run: evidence incomplete for {', '.join(summary['invalid'])} - the "
              "scores of those cases measure missing data, not the system")  # fmt: skip
        return 1
    if args.baseline:
        bad = regressions(results, json.loads(args.baseline.read_text()))
        if bad:
            print(f"REGRESSION vs {args.baseline.name}: {', '.join(bad)}")
            return 1
        print(f"no regression vs {args.baseline.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
