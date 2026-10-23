"""Phase 25b (ADR-037): calibration counts claims honestly and says how unsure it is."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("eval_cal", ROOT / "scripts/eval_calibration.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["eval_cal"] = mod
    spec.loader.exec_module(mod)
    return mod


def _case(key: str, claims: list[tuple[str, str, bool]], passed: bool = True,
          top: str = "t") -> dict[str, Any]:  # fmt: skip
    return {"case": f"investigations-v1-{key}", "score": {
        "passed": passed, "top": top, "top_band": claims[0][0] if claims else None,
        "top_status": claims[0][1] if claims else None, "proposal": None,
        "critic_alternatives": 1,
        "claims": [{"rank": i + 1, "band": b, "status": s, "kind": "change", "correct": ok}
                   for i, (b, s, ok) in enumerate(claims)]}}  # fmt: skip


def test_wilson_interval_is_wide_for_small_n() -> None:
    cal = _load()
    lo, hi = cal.wilson(9, 12)
    assert 0.46 < lo < 0.48 and 0.90 < hi < 0.92  # "75 %" is really "somewhere in 47-91 %"
    assert cal.wilson(0, 0) is None
    assert cal.wilson(3, 3)[1] == 1.0 and cal.wilson(3, 3)[0] < 0.5  # 3/3 is not "100 %"


def test_calibration_by_band_and_status() -> None:
    cal = _load()
    run = {"summary": {"proposals": {"correct": 1, "wrong": 0, "missed": 1}}, "cases": [
        _case("S01", [("HIGH", "VALIDATED", True), ("HIGH", "VALIDATED", False)]),
        _case("S14", [("HIGH", "PROPOSED", False)]),
        _case("S15", [("MEDIUM", "PROPOSED", True)]),
        _case("S11", []),
    ]}  # fmt: skip
    c = cal.calibration(run)
    assert c["by_band"]["HIGH"] == {"claims": 3, "correct": 1, "wilson95": cal.wilson(1, 3)}
    assert c["by_status"]["VALIDATED"]["claims"] == 2 and c["by_status"]["PROPOSED"]["correct"] == 1
    assert c["band_x_status"]["HIGH/PROPOSED"]["correct"] == 0
    assert c["wrong"] == ["S01 #2 HIGH/VALIDATED (change)", "S14 #1 HIGH/PROPOSED (change)"]
    assert c["critic_alternatives"] == 4 and c["proposals"]["missed"] == 1


def test_a_run_without_claims_asks_for_a_rescore() -> None:
    cal = _load()
    old = {"summary": {}, "cases": [{"case": "x-S01", "score": {"passed": True}}]}
    with pytest.raises(SystemExit, match="eval-rescore"):
        cal.calibration(old)


def test_compare_lists_every_changed_case_and_unmatched_ones(tmp_path: Path) -> None:
    cal = _load()
    a = {
        "summary": {"passed": "2/2"},
        "cases": [
            _case("S01", [("HIGH", "VALIDATED", True)]),
            _case("S04", [("HIGH", "PROPOSED", True)]),
        ],
    }
    b = {"summary": {"passed": "1/2"}, "cases": [
        _case("S01", [("HIGH", "VALIDATED", True)]),
        _case("S04", [("HIGH", "VALIDATED", False)], passed=False, top="other"),
        _case("S09", [])]}  # fmt: skip
    cmp = cal.compare(a, b)
    assert cmp["cases_compared"] == 2 and cmp["only_in_vs"] == ["S09"]
    (ch,) = cmp["changed"]
    assert ch["case"] == "S04" and ch["passed"] == [True, False] and ch["top_status"] == [
        "PROPOSED", "VALIDATED"]  # fmt: skip
    # the CLI writes its result file and prints the comparison
    fa, fb = tmp_path / "a.json", tmp_path / "b.json"
    fa.write_text(json.dumps(a))
    fb.write_text(json.dumps(b))
    assert cal.main(["--run", str(fa), "--vs", str(fb), "--out-dir", str(tmp_path)]) == 0
    (out,) = tmp_path.glob("calibration-*.json")
    assert json.loads(out.read_text())["comparison"]["changed"][0]["case"] == "S04"
