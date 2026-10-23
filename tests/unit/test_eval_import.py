"""Phase 25c (ADR-037): result files -> the eval schema. What is imported, what is refused."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "data/eval/results"


def _load(name: str, file: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / file)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


imp = _load("eval_imp", "eval_import.py")


def _write(tmp: Path, name: str, data: dict[str, Any]) -> Path:
    p = tmp / name
    p.write_text(json.dumps(data))
    return p


def test_every_committed_result_file_is_imported_or_refused_with_a_reason() -> None:
    seen: dict[str, str] = {}
    for f in sorted(RESULTS.glob("*.json")):
        try:
            p = imp.plan(f)
            seen[f.name] = "ok"
            imp.numbers_only(p["summary"])
        except imp.RefusedError as e:
            seen[f.name] = f"refused: {e}"
    assert seen, "no result files found"
    for name, what in seen.items():  # never a crash, never an unknown-shape skip
        assert what == "ok" or what.startswith("refused: "), name


def test_the_known_invalid_rag_run_is_refused() -> None:
    """rag-2026-10-06T043554 (RERANK=1): every rerank failed; 'hybrid' was keyword-only."""
    f = RESULTS / "rag-2026-10-06T043554.json"
    with pytest.raises(imp.RefusedError, match="validity check"):
        imp.plan(f)


def test_never_imported_kinds(tmp_path: Path) -> None:
    for name in ("rag-sweep-x.json", "calibration-x.json", "agent-log-compare-x.json"):
        with pytest.raises(imp.RefusedError, match="never imported"):
            imp.plan(_write(tmp_path, name, {}))
    with pytest.raises(imp.RefusedError, match="unknown kind"):
        imp.plan(_write(tmp_path, "catalog-latency-agent-on-x.json", {}))


def _rag(valid: Any = True, baseline: bool = True) -> dict[str, Any]:
    mode = {"overall": {"n": 3, "recall@5": 0.5, "recall@5_ci95": [0.1, 0.9], "mrr@10": 0.4},
            "leakage": {"violations": []}, "quarantine": {"violations": []},
            "degraded_queries": 0, "valid": valid}  # fmt: skip
    return {"environment": {"timestamp": "2026-10-06T21:34:25+00:00", "git_commit": "abc",
                            "dataset": "rag_queries_v1.jsonl", "is_baseline": baseline},
            "settings": {}, "results": {"hybrid": mode}}  # fmt: skip


def test_rag_fake_embeddings_and_invalid_modes_are_refused(tmp_path: Path) -> None:
    with pytest.raises(imp.RefusedError, match="not a baseline"):
        imp.plan(_write(tmp_path, "rag-a.json", _rag(baseline=False)))
    with pytest.raises(imp.RefusedError, match="valid: false"):
        imp.plan(_write(tmp_path, "rag-b.json", _rag(valid=False)))
    p = imp.plan(_write(tmp_path, "rag-c.json", _rag()))
    assert p["dataset"] == ("rag-retrieval", 1)
    assert p["summary"]["hybrid"]["recall@5"] == 0.5


def test_the_id_is_stable_and_changes_with_the_file(tmp_path: Path) -> None:
    a = imp.plan(_write(tmp_path, "rag-a.json", _rag()))
    assert imp.plan(tmp_path / "rag-a.json")["id"] == a["id"]  # re-import = same row
    d = _rag()
    d["results"]["hybrid"]["overall"]["recall@5"] = 0.6
    assert imp.plan(_write(tmp_path, "rag-a.json", d))["id"] != a["id"]


def test_summary_is_numbers_only() -> None:
    imp.numbers_only({"a": 1, "b": [0.1, 2], "c": {"k": 1, "n": 2}})
    for bad in ({"a": "11/16"}, {"a": None}, {"a": ["S01"]}, {"a": {"why": "text"}}):
        with pytest.raises(imp.RefusedError, match="not a number"):
            imp.numbers_only(bad)


def test_investigation_runs_keep_their_id_and_a_rescore_is_marked() -> None:
    orig = RESULTS / "investigations-2026-10-23T033835.json"
    resc = RESULTS / "investigations-2026-10-23T033835-rescored.json"
    if not orig.is_file():
        pytest.skip("the real-model run files are committed on main (25b)")
    po, pr = imp.plan(orig), imp.plan(resc)
    assert str(po["id"]) == json.loads(orig.read_text())["run_id"]  # = the runner's own row
    assert pr["id"] != po["id"]
    assert (po["summary"]["rescored"], pr["summary"]["rescored"]) == (0, 1)
    assert pr["summary"]["validated_wrong"] == {"k": 2, "n": 16}


def test_an_invalid_investigation_run_is_refused(tmp_path: Path) -> None:
    d = {"run_id": "01a166d6-cc1e-7191-bc90-b881a696c294", "started_at": "2026-10-23T03:38:35+00:00",
         "config": {"dataset": "investigations", "version": 1},
         "summary": {"cases": 1, "invalid": ["S03"]}, "cases": []}  # fmt: skip
    with pytest.raises(imp.RefusedError, match="invalid cases"):
        imp.plan(_write(tmp_path, "investigations-x.json", d))


def test_importer_and_runner_store_the_same_per_case_metrics() -> None:
    run = _load("eval_inv_for_import", "eval_investigations.py")
    assert imp.METRICS == run.METRICS


def test_a_run_with_an_invalid_case_or_a_stop_is_stored_failed() -> None:
    """25c-fix1: an invalid run was stored SUCCEEDED, so a dashboard showed its pass rate."""
    run = _load("eval_inv_status", "eval_investigations.py")
    assert run.run_status(False, {"invalid": []}) == "SUCCEEDED"
    assert run.run_status(False, {"invalid": ["S03"]}) == "FAILED"
    assert run.run_status(True, {"invalid": []}) == "FAILED"


def test_summarize_works_on_a_run_that_stopped_before_any_case() -> None:
    run = _load("eval_inv_empty", "eval_investigations.py")
    assert run.summarize([])["cases"] == 0  # the crash path stores this summary


def test_the_investigation_panel_shows_only_succeeded_runs() -> None:
    """25c-fix1: a run that died mid-way stayed RUNNING and showed "passed 10/10"."""
    d = json.loads((ROOT / "infrastructure/observability/grafana/dashboards/evaluation.json")
                   .read_text())  # fmt: skip
    p = next(p for p in d["panels"] if p["title"].startswith("Investigation eval"))
    q = p["targets"][0]["rawSql"]
    assert "r.status = 'SUCCEEDED'" in q
    assert "summary->'invalid'" in q  # rows written before the fix: invalid but SUCCEEDED
