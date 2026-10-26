"""Phase 27 (ADR-039): the load tests' pure logic - the fake-only guard, stages, the knee."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/load"))
import loadlib as ll  # noqa: E402


def _routes(models: dict[str, str]) -> dict[str, object]:
    return {"routes": {"r": {"chain": list(models)}}, "models": models}


def test_a_load_run_refuses_any_real_model() -> None:
    assert ll.not_fake(_routes({"fake-large": "hosted_fake", "fake-small": "local_fake"})) == []
    assert ll.not_fake(_routes({"claude": "anthropic", "f": "local_fake"})) == ["claude"]
    assert ll.not_fake(_routes({"llama": "ollama"})) == ["llama"]  # free, but measures the CPU


def test_stages_follow_the_warmup_and_end() -> None:
    p = ll.PLANS["policy"]
    assert ll.stage_at(p, 0) == -1 and ll.stage_at(p, ll.WARMUP_S) == 0
    assert ll.stage_at(p, ll.WARMUP_S + 61) == 1
    assert ll.stage_at(p, ll.WARMUP_S + 60 * len(p.read_rps)) is None


def test_poisson_gaps_average_to_the_target_rate() -> None:
    import random

    rnd = random.Random(1)
    gaps = [ll.wait_s(100, 31, rnd.random()) for _ in range(20000)]
    assert abs(sum(gaps) / len(gaps) - 31 / 100) < 0.01  # each of 31 readers: 100/31 rps


def test_stage_summary_separates_policy_from_errors() -> None:
    p = ll.PLANS["policy"]
    rows = [("GET /incidents", 10.0, 200)] * 90 + [("GET /incidents", 1.0, 429)] * 9
    rows += [("POST /search", 5.0, 503)]
    s = ll.summarize_stage(p, 0, rows)
    assert s["requests"] == 100 and s["rate_limited_share"] == 0.09 and s["error_share"] == 0.01
    assert s["p95_ms"] == 10.0 and s["per_endpoint"]["GET /incidents"]["n"] == 90


def test_the_knee_is_delivery_latency_or_errors_never_429s() -> None:
    def st(i: int, target: int, got: float, p95: float, err: float = 0.0, lim: float = 0.0) -> dict:  # type: ignore[type-arg]
        return {"stage": i, "target_rps": target, "achieved_rps": got, "p95_ms": p95,
                "error_share": err, "rate_limited_share": lim}  # fmt: skip

    assert ll.knee([st(0, 10, 10, 50), st(1, 30, 30, 60, lim=0.4)]) is None  # 429s: policy
    k = ll.knee([st(0, 10, 10, 50), st(1, 30, 29, 70), st(2, 60, 40, 90)])
    assert k and k["target_rps"] == 60 and "delivered" in k["why"][0]
    k = ll.knee([st(0, 10, 10, 50), st(1, 30, 30, 120)])
    assert k and "p95" in k["why"][0]


def test_investigation_stages_split_queue_from_run() -> None:
    p = ll.PLANS["investigations"]
    recs = [
        {"stage": 0, "status": "PARTIAL", "seconds": 40.0, "queue_s": 0.1, "run_s": 38.0},
        {"stage": 1, "status": "PARTIAL", "seconds": 90.0, "queue_s": 50.0, "run_s": 39.0},
        {"stage": 1, "status": "RUNNING", "seconds": None, "queue_s": None, "run_s": None},
        {"stage": 1, "status": "start 429", "seconds": 0.0},
    ]
    s0, s1 = (ll.investigation_stage(p, i, recs) for i in (0, 1))
    assert (s0["per_min"], s0["finished"], s0["queue_s_p50"]) == (2, 1, 0.1)
    assert (s1["started"], s1["finished"], s1["still_running"], s1["refused"]) == (3, 1, 1, 1)
    assert s1["queue_s_max"] == 50.0 and s1["run_s_p50"] == 39.0
