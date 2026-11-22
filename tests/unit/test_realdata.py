"""Phase 32a: the real-data run's rules (tests/realdata/realdatalib.py), fixed before any run."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "realdata"))
import realdatalib as r


def hyp(
    rank: int, kind: str, svc: str, status: str = "VALIDATED", conf: str = "HIGH"
) -> dict[str, Any]:
    return {"rank": rank, "status": status, "confidence": conf,
            "statement": f"A traffic change (requests_per_sec) on {svc} may have driven the symptoms.",
            "detail": {"cause_kind": kind}}  # fmt: skip


def test_f1_passes_only_on_a_validated_traffic_cause_on_the_target() -> None:
    assert r.rule_f1([hyp(1, "traffic", "api")], "api")[0] == r.PASS
    status, why = r.rule_f1([hyp(1, "traffic", "api", status="PROPOSED")], "api")
    assert status == r.FAIL and why.startswith("found, not validated")
    assert r.rule_f1([hyp(1, "traffic", "rag")], "api")[0] == r.FAIL  # another service
    assert r.rule_f1([hyp(1, "saturation", "api")], "api")[0] == r.FAIL
    assert r.rule_f1([], "api") == (r.FAIL, "no live hypothesis")


def test_f1_reads_the_top_live_hypothesis() -> None:
    hyps = [hyp(1, "change", "api", status="REJECTED"), hyp(2, "traffic", "api")]
    assert r.rule_f1(hyps, "api")[0] == r.PASS


def test_f1_service_match_is_a_whole_name() -> None:
    assert r.rule_f1([hyp(1, "traffic", "api-gateway")], "api")[0] == r.FAIL


def test_f2_fails_on_any_live_high_claim() -> None:
    assert r.rule_f2([hyp(1, "saturation", "api", conf="MEDIUM")])[0] == r.PASS
    assert r.rule_f2([hyp(1, "change", "api", conf="HIGH")])[0] == r.FAIL
    assert r.rule_f2([hyp(1, "change", "api", status="REJECTED", conf="HIGH")])[0] == r.PASS
    assert r.rule_f2([])[0] == r.PASS


def test_a_run_on_seeded_data_is_invalid_whatever_the_hypotheses() -> None:
    ev = [{"kind": "METRIC", "source_system": "devdata.metric_points"},
          {"kind": "LOG", "source_system": "loki"}]  # fmt: skip
    data = r.data_check(ev)
    assert data[0] == r.INVALID and "METRIC<-devdata.metric_points" in data[1]
    assert r.verdict(data, (r.PASS, "x")) == r.INVALID
    ok = r.data_check([{"kind": "METRIC", "source_system": "prometheus"},
                       {"kind": "DEPLOY", "source_system": "devdata.deployments"}])  # fmt: skip
    assert ok[0] == r.PASS  # deploys stay seeded until 32b: not a metric/log source
    assert r.data_check([])[0] == r.PASS  # nothing found is reported as empty kinds, not invalid


def test_the_evidence_summary_names_empty_kinds() -> None:
    s = r.evidence_summary([{"kind": "METRIC", "source_system": "prometheus"}])
    assert s["empty_kinds"] == ["LOG", "DEPLOY"]
    assert s["by_source"] == {"METRIC:prometheus": 1}


def test_tally_counts_no_answer_apart() -> None:
    assert r.tally([200, 200, 503, 0, 401]) == {"2xx": 2, "4xx": 1, "5xx": 1, "none": 1}
