"""Phase 25b (ADR-037): how often is a band (and a status) right - and did a config change it?

    make eval-calibration RUN=data/eval/results/x.json [VS=data/eval/results/y.json]

Reads investigation-eval result files (`make eval-investigations`, or `--rescore` for a run
made before Phase 25b). Two answers, both from what the system stored:
1. Calibration: for every CODE claim that was not rejected - per band (HIGH / MEDIUM / LOW)
   and per status (VALIDATED / PROPOSED / CHALLENGED) - how many named the planted cause, with
   a Wilson 95 % interval. Critic alternatives are COUNTED, not scored: they name no cause kind
   of ours, so matching them would need a judge (ADR-037: exact scoring only).
2. Comparison (VS): per case, did `passed`, the top claim, its band or status change?
Why Wilson and not "9/12 = 75 %": with a dozen claims the honest interval is wide, and a
point estimate alone would be quoted as if it were precise. No bootstrap either: one
proportion has a closed-form interval; resampling 12 claims adds nothing but noise.
Upper bound: every number is on scenarios written for the eval.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BANDS = ("HIGH", "MEDIUM", "LOW")
STATUSES = ("VALIDATED", "PROPOSED", "CHALLENGED")


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95 % Wilson score interval for k successes in n; None when n = 0."""
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round(max(0.0, c - h), 3), round(min(1.0, c + h), 3)


def claims_of(run: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for c in run["cases"]:
        if "claims" not in c["score"]:
            raise SystemExit(
                f"{c['case']}: no per-claim scores in this file (made before Phase 25b) - run "
                "`make eval-rescore FILE=...` first (free: it re-reads what was stored)"
            )
        out += [{**x, "case": c["case"][-3:]} for x in c["score"]["claims"]]
    return out


def _row(xs: list[dict[str, Any]]) -> dict[str, Any]:
    k = sum(1 for x in xs if x["correct"])
    return {"claims": len(xs), "correct": k, "wilson95": wilson(k, len(xs))}


def calibration(run: dict[str, Any]) -> dict[str, Any]:
    cl = claims_of(run)
    return {
        "by_band": {b: _row([x for x in cl if x["band"] == b]) for b in BANDS},
        "by_status": {s: _row([x for x in cl if x["status"] == s]) for s in STATUSES},
        "band_x_status": {
            f"{b}/{s}": _row([x for x in cl if x["band"] == b and x["status"] == s])
            for b in BANDS
            for s in STATUSES
            if any(x["band"] == b and x["status"] == s for x in cl)
        },
        "wrong": sorted(
            f"{x['case']} #{x['rank']} {x['band']}/{x['status']} ({x['kind']})"
            for x in cl
            if not x["correct"]
        ),
        "critic_alternatives": sum(c["score"].get("critic_alternatives", 0) for c in run["cases"]),
        "proposals": run["summary"].get("proposals"),
    }


def compare(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Per case A vs B. A case only in one file is listed, never silently dropped."""
    ia = {c["case"]: c for c in a["cases"]}
    ib = {c["case"]: c for c in b["cases"]}
    changed = []
    for key in sorted(ia.keys() & ib.keys()):
        sa, sb = ia[key]["score"], ib[key]["score"]
        diff = {
            f: [sa.get(f), sb.get(f)]
            for f in ("passed", "top", "top_band", "top_status", "proposal")
            if sa.get(f) != sb.get(f)
        }
        if diff:
            changed.append({"case": key[-3:], **diff})
    return {
        "cases_compared": len(ia.keys() & ib.keys()),
        "only_in_run": sorted(k[-3:] for k in ia.keys() - ib.keys()),
        "only_in_vs": sorted(k[-3:] for k in ib.keys() - ia.keys()),
        "passed": [a["summary"]["passed"], b["summary"]["passed"]],
        "changed": changed,
    }


def _fmt(name: str, r: dict[str, Any]) -> str:
    ci = r["wilson95"]
    rng = f"[{ci[0]:.2f}, {ci[1]:.2f}]" if ci else "-"
    return f"  {name:<20} {r['correct']:>3}/{r['claims']:<3} correct   95% {rng}"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--vs", type=Path, help="a second run of the same pack to compare with")
    ap.add_argument("--out-dir", type=Path, default=ROOT / "data/eval/results")
    args = ap.parse_args(argv)
    run = json.loads(args.run.read_text())
    out: dict[str, Any] = {"run": args.run.name, "config": run.get("config"),
                           "calibration": calibration(run)}  # fmt: skip
    if args.vs:
        vs = json.loads(args.vs.read_text())
        out["vs"] = {"file": args.vs.name, "config": vs.get("config")}
        out["comparison"] = compare(run, vs)
    cal = out["calibration"]
    print(f"calibration of {args.run.name} (code claims, not rejected; planted scenarios):")
    for b, r in cal["by_band"].items():
        print(_fmt(b, r))
    for s, r in cal["by_status"].items():
        print(_fmt(s, r))
    print(f"  wrong claims: {', '.join(cal['wrong']) or 'none'}")
    print(f"  critic alternatives (counted, not scored): {cal['critic_alternatives']}")
    print(f"  rollback proposals: {cal['proposals']}")
    if args.vs:
        cmp = out["comparison"]
        print(f"vs {args.vs.name}: passed {cmp['passed'][0]} vs {cmp['passed'][1]}, "
              f"{len(cmp['changed'])} of {cmp['cases_compared']} cases changed")  # fmt: skip
        for ch in cmp["changed"]:
            print(f"  {ch}")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    dest = args.out_dir / f"calibration-{datetime.now(UTC):%Y-%m-%dT%H%M%S}.json"
    dest.write_text(json.dumps(out, indent=1) + "\n")
    print(f"written: {dest.relative_to(ROOT) if dest.is_relative_to(ROOT) else dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
