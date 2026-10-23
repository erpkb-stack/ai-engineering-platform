"""Phase 25a (ADR-037): the scenario pack is deterministic, contained, and keeps its truth out
of everything the system can read."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

from aeoi_synth import scenarios
from aeoi_synth.generate import generate

ROOT = Path(__file__).resolve().parents[3]


def test_the_pack_is_a_pure_function_of_the_seed() -> None:
    a, b = scenarios.build(42), scenarios.build(42)
    assert [(s.key, s.expected, len(s.metrics), s.logs[:3]) for s in a] == [
        (s.key, s.expected, len(s.metrics), s.logs[:3]) for s in b
    ]
    assert scenarios.build(7)[0].metrics != a[0].metrics  # the noise depends on the seed


def test_every_case_has_its_own_service_day_and_deploy_keys() -> None:
    pack = scenarios.build(42)
    assert len(pack) == 16
    assert len({s.service for s in pack}) == len(pack)
    assert len({s.detected_at.date() for s in pack}) == len(pack)
    keys = [d["deploy_key"] for s in pack for d in s.deployments]
    assert len(keys) == len(set(keys)) and all(re.fullmatch(r"DEPLOY-[0-9]+", k) for k in keys)


def test_every_row_is_inside_the_pack_range_and_the_main_dataset_is_not() -> None:
    lo, hi = scenarios.pack_range()
    pack = scenarios.build(42)
    times = [r["ts"] for s in pack for r in s.metrics + s.logs]
    times += [d["started_at"] for s in pack for d in s.deployments]
    assert lo <= min(times) and max(times) <= hi
    ds = generate(42)  # the main dataset: nothing of it may be deleted by `make eval-seed`
    main = [r["ts"] for t in ("devdata.metric_points", "devdata.log_events") for r in ds.rows(t)]
    main += [r["started_at"] for r in ds.rows("devdata.deployments")]
    assert max(main) < lo
    # and the deployment agent's 6 h look-back from the first case cannot reach a main deploy
    first_window = scenarios.FIRST_DAY - timedelta(minutes=60)
    assert max(r["started_at"] for r in ds.rows("devdata.deployments")) < first_window - timedelta(
        hours=6
    )


def test_the_truth_is_planted_not_observed() -> None:
    """Every culprit names a deploy of THIS case or a metric it shifted; every undetectable
    case has no culprit; decoys and rule-outs refer to something that exists in the case."""
    for s in scenarios.build(42):
        e = s.expected
        own = {d["deploy_key"] for d in s.deployments} | set(scenarios.BASE)
        if e["detectable"]:
            assert e["culprit"] and e["culprit"]["subject"] in own, s.key
        else:
            assert e["culprit"] is None, s.key
        assert set(e["decoys"]) <= own and set(e["rule_out"]) <= own, s.key


def test_no_row_the_system_can_read_carries_the_label() -> None:
    """The category is the answer key: it must never appear in a log, metric or deploy."""
    for s in scenarios.build(42):
        rows = json.dumps([s.logs, s.metrics, s.deployments, s.case_input()], default=str)
        assert s.expected["category"] not in rows, s.key


def test_nothing_the_system_runs_reads_the_eval_schema_or_the_pack() -> None:
    for d in ("services", "libs"):
        for f in (ROOT / d).rglob("*.py"):
            if "/tests/" in str(f) or "alembic" in str(f) or f.name == "eval.py":
                continue
            text = f.read_text()
            assert "aeoi_synth.scenarios" not in text, f
            assert not re.search(r"\beval\.(eval_cases|datasets)\b", text), f
