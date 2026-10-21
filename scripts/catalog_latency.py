"""Phase 23c: how slow is the Java catalog itself - with and without the OTel Java agent?

The tool gateway saw a slow tail (2026-10-20: 4 timeouts, 12 errors in 111 calls; every other
tool 0 timeouts; the catalog tool's deadline is 2 s). This measures the catalog DIRECTLY, without
the gateway, so the two runs differ only in the agent:
  make catalog-latency LABEL=agent-on   (telemetry on in .env, after make k8s-apply)
  make catalog-latency LABEL=agent-off  (AEOI_OTEL_ENDPOINT removed, after make k8s-apply)
Each run starts right after k8s-apply restarted the catalog, so the FIRST requests (cold JVM
paths) are reported apart from the rest. The result is written to data/eval/results/ - the only
numbers we quote are the ones in those files.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
FIRST = 10  # the cold requests, reported apart


def queries(catalog: dict[str, Any], n: int) -> list[str]:
    """service_key for every service, then team, then name_contains - round robin to n."""
    services = catalog["services"]
    pool = [f"service_key={urllib.parse.quote(s['key'])}" for s in services]
    pool += [f"team={urllib.parse.quote(t)}" for t in sorted({s["team"] for s in services})]
    pool += [f"name_contains={w}" for w in ("api", "worker", "service")]
    return [pool[i % len(pool)] for i in range(n)]


def one(url: str, token: str, timeout: float) -> tuple[str, float]:
    """(outcome, milliseconds). outcome: 'http:<status>' or the error type."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})  # noqa: S310
    t0 = time.perf_counter()
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            resp.read()
            out = f"http:{resp.status}"
    except urllib.error.HTTPError as e:
        out = f"http:{e.code}"
    except urllib.error.URLError as e:
        out = type(e.reason).__name__
    except (TimeoutError, OSError) as e:
        out = type(e).__name__
    return out, (time.perf_counter() - t0) * 1000


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, round(p / 100 * (len(s) - 1)))], 1)


def summarize(label: str, samples: list[tuple[str, float]], deadline_ms: float) -> dict[str, Any]:
    ok_ms = [ms for out, ms in samples if out == "http:200"]
    outcomes: dict[str, int] = {}
    for out, _ in samples:
        outcomes[out] = outcomes.get(out, 0) + 1
    rest = [ms for out, ms in samples[FIRST:] if out == "http:200"]
    return {
        "label": label,
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "requests": len(samples),
        "outcomes": outcomes,
        "deadline_ms": deadline_ms,
        "over_deadline": sum(1 for _, ms in samples if ms > deadline_ms),
        "ok_ms": {
            "p50": pct(ok_ms, 50),
            "p95": pct(ok_ms, 95),
            "p99": pct(ok_ms, 99),
            "max": round(max(ok_ms), 1) if ok_ms else None,
            "mean": round(statistics.fmean(ok_ms), 1) if ok_ms else None,
        },
        "first_requests_ms": [round(ms, 1) for _, ms in samples[:FIRST]],
        "after_first_ok_ms": {
            "p50": pct(rest, 50),
            "p95": pct(rest, 95),
            "max": round(max(rest), 1) if rest else None,
        },
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True, help="agent-on | agent-off (free text)")
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--url", default="http://127.0.0.1:8010")
    ap.add_argument("--deadline-ms", type=float, default=2000.0, help="the catalog tool's deadline")
    ap.add_argument("--token-file", type=Path, default=ROOT / "secrets/tools_catalog_token.txt")
    ap.add_argument("--catalog", type=Path, default=ROOT / "data/generated/catalog.json")
    ap.add_argument("--out-dir", type=Path, default=ROOT / "data/eval/results")
    args = ap.parse_args(argv)
    if not args.token_file.is_file():
        print(f"{args.token_file.name} not found: make tools-tokens")
        return 2
    if not args.catalog.is_file():
        print(f"{args.catalog} not found: make db-seed")
        return 2
    token = args.token_file.read_text().strip()
    qs = queries(json.loads(args.catalog.read_text()), args.n)
    samples = [one(f"{args.url}/v1/services?{q}", token, timeout=10.0) for q in qs]
    result = summarize(args.label, samples, args.deadline_ms)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%S")
    path = args.out_dir / f"catalog-latency-{args.label}-{stamp}.json"
    path.write_text(json.dumps(result, indent=2) + "\n")
    ok = result["ok_ms"]
    print(f"{args.label}: {result['requests']} requests {result['outcomes']}; ok p50 {ok['p50']} "
          f"p95 {ok['p95']} p99 {ok['p99']} max {ok['max']} ms; over {args.deadline_ms:.0f} ms: "
          f"{result['over_deadline']}")  # fmt: skip
    print(f"  first {FIRST} (cold) ms: {result['first_requests_ms']}")
    print(f"  written: {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    return 0 if result["outcomes"].get("http:200") == result["requests"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
