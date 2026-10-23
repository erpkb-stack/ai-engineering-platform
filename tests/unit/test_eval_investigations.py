"""Phase 25a (ADR-037): the investigation eval scores what it claims (pure functions)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "eval_inv", ROOT / "scripts/eval_investigations.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["eval_inv"] = mod
    spec.loader.exec_module(mod)
    return mod


def _h(rank: int, statement: str, band: str = "HIGH", status: str = "PROPOSED",
       kind: str = "change") -> dict[str, Any]:  # fmt: skip
    return {"rank": rank, "statement": statement, "confidence": band, "status": status,
            "detail": {"cause_kind": kind, "ranked_by": "rubric"}}  # fmt: skip


def _e(culprit: dict[str, str] | None, **kw: Any) -> dict[str, Any]:
    return {"detectable": culprit is not None, "category": "x", "culprit": culprit,
            "decoys": kw.get("decoys", []), "rule_out": kw.get("rule_out", [])}  # fmt: skip


DEPLOY = {"kind": "change", "subject": "DEPLOY-9100"}


def test_top1_needs_the_right_kind_and_subject() -> None:
    ev = _load()
    s = ev.score(_e(DEPLOY), [_h(1, "Deploy DEPLOY-9100 on a may have caused it")])
    assert s["top1"] and s["in_top3"] and s["passed"] and not s["confident_wrong"]
    # same subject, wrong kind (a saturation statement that mentions the deploy) is no match
    s = ev.score(_e(DEPLOY), [_h(1, "pool saturation after DEPLOY-9100", kind="saturation")])
    assert s["top1"] is False and s["culprit_rank"] is None


def test_rejected_hypotheses_never_count_as_the_answer() -> None:
    ev = _load()
    hyps = [_h(1, "Deploy DEPLOY-9100 did not start it", status="REJECTED"),
            _h(2, "A traffic change (requests_per_sec)", band="MEDIUM", kind="traffic")]  # fmt: skip
    s = ev.score(_e(DEPLOY), hyps)
    assert s["top1"] is False and s["top"].startswith("A traffic") and not s["confident_wrong"]


def test_a_wrong_high_top_is_a_confident_wrong_answer() -> None:
    ev = _load()
    hyps = [_h(1, "Deploy DEPLOY-9101 on b"), _h(2, "Deploy DEPLOY-9100 on a", band="MEDIUM")]
    s = ev.score(_e(DEPLOY, decoys=["DEPLOY-9101"]), hyps)
    assert s["confident_wrong"] and s["decoy_above"] and s["culprit_rank"] == 2 and s["in_top3"]
    assert not s["passed"]


def test_undetectable_passes_only_without_any_high_claim() -> None:
    ev = _load()
    assert ev.score(_e(None), [])["passed"]  # no hypothesis at all: the honest answer
    assert ev.score(_e(None), [_h(1, "Deploy DEPLOY-9230", band="MEDIUM")])["passed"]
    s = ev.score(_e(None, decoys=["DEPLOY-9230"]), [_h(1, "Deploy DEPLOY-9230 on c")])
    assert s["confident_wrong"] and s["decoy_above"] and not s["passed"]
    assert s["top1"] is None and s["in_top3"] is None  # not scored for an undetectable case


def test_rule_out_needs_a_rejected_statement_naming_the_subject() -> None:
    ev = _load()
    e = _e(DEPLOY, rule_out=["requests_per_sec"])
    ok = [_h(1, "Deploy DEPLOY-9100"), _h(2, "A traffic surge ... requests_per_sec stayed",
                                         band="LOW", status="REJECTED", kind="traffic")]  # fmt: skip
    assert ev.score(e, ok)["rule_out_ok"]
    assert not ev.score(e, ok[:1])["rule_out_ok"]


def test_summary_and_regressions() -> None:
    ev = _load()
    cases = [
        {"case": "k-S01", "expected": _e(DEPLOY), "outcome": "PARTIAL", "latency_s": 2.0,
         "cost_usd": 0.001, "invalid": False, "score": ev.score(_e(DEPLOY), [_h(1, "DEPLOY-9100")])},
        {"case": "k-S11", "expected": _e(None), "outcome": "INCONCLUSIVE", "latency_s": 4.0,
         "cost_usd": 0, "invalid": True, "score": ev.score(_e(None), [_h(1, "Deploy X")])},
    ]  # fmt: skip
    s = ev.summarize(cases)
    assert s["top1"] == "1/1" and s["confident_wrong"] == "1/2" and s["passed"] == "1/2"
    assert s["undetectable_abstained"] == "0/1" and s["invalid"] == ["S11"]
    assert s["latency_s"] == {"p50": 3.0, "max": 4.0}
    base = {"cases": [{"case": "k-S01", "passed": True}, {"case": "k-S11", "passed": True}]}  # fmt: skip
    assert ev.regressions(cases, base) == ["k-S11"]  # passed in the baseline, fails now
    assert ev.regressions(cases, {"cases": []}) == []  # new cases are not regressions


def test_the_committed_baseline_covers_the_pack() -> None:
    import json

    from aeoi_synth import scenarios

    base = json.loads((ROOT / "data/eval/baselines/investigations-v1-fake.json").read_text())
    assert {c["case"] for c in base["cases"]} == {s.key for s in scenarios.build(42)}


def _server(answers: list[Any]) -> tuple[Any, list[dict[str, str]]]:
    """A local server that plays `answers` in order: "drop" closes the connection without a
    response, an int is that status with a small problem body, a dict is a 200 JSON body."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen: list[dict[str, str]] = []

    class H(BaseHTTPRequestHandler):
        def _reply(self) -> None:
            seen.append({"method": self.command, "key": self.headers.get("Idempotency-Key", "")})
            a = answers.pop(0)
            if a == "drop":
                self.close_connection = True
                self.connection.close()
                return
            body = json.dumps(a if isinstance(a, dict) else {"detail": f"status {a}"}).encode()
            self.send_response(200 if isinstance(a, dict) else a)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _reply  # noqa: N815

        def log_message(self, *_: object) -> None:
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def test_a_safe_call_survives_a_dropped_connection_and_a_503(monkeypatch: Any) -> None:
    ev = _load()
    monkeypatch.setattr(ev, "RETRY_WAITS_S", (0.0, 0.0))
    srv, seen = _server(["drop", 503, {"key": "INC-1"}])
    try:
        api = ev.Api(f"http://127.0.0.1:{srv.server_address[1]}", "t")
        assert api.call("POST", "/x", {"a": 1}, key="k-1") == {"key": "INC-1"}
    finally:
        srv.shutdown()
    assert [s["key"] for s in seen] == ["k-1", "k-1", "k-1"]  # the SAME key each time


def test_an_unsafe_or_final_failure_names_the_call_and_the_answer(monkeypatch: Any) -> None:
    import pytest

    ev = _load()
    monkeypatch.setattr(ev, "RETRY_WAITS_S", (0.0, 0.0))
    srv, seen = _server([503, 404])
    try:
        api = ev.Api(f"http://127.0.0.1:{srv.server_address[1]}", "t")
        with pytest.raises(ev.CallError, match=r"POST .*/x -> 503: .*status 503"):
            api.call("POST", "/x", {"a": 1})  # no Idempotency-Key: never repeated
        with pytest.raises(ev.CallError, match=r"GET .*/y -> 404"):
            api.call("GET", "/y")  # 404 is an answer, not a retryable failure
    finally:
        srv.shutdown()
    assert len(seen) == 2


# ------------------------------------------------------------------ Phase 25b: status + proposal
def _hv(rank: int, statement: str, band: str, status: str, by: str = "hypothesis",
        kind: str = "change") -> dict[str, Any]:  # fmt: skip
    h = _h(rank, statement, band=band, status=status, kind=kind)
    return {**h, "produced_by": by}


def test_shown_wrong_and_stood_behind_wrong_are_scored_apart() -> None:
    """S14 on the real-model run: the wrong deploy was HIGH (shown) but PROPOSED (the critic
    weakened it), so the system did not stand behind it and proposed no rollback."""
    ev = _load()
    hyps = [_hv(1, "Deploy DEPLOY-9230 on ledger-api", "HIGH", "PROPOSED"),
            _hv(2, "An expired TLS certificate", "MEDIUM", "PROPOSED", by="critic", kind="change")]  # fmt: skip
    s = ev.score(_e(None, decoys=["DEPLOY-9230"]), hyps, {"proposed": False, "why": "..."})
    assert s["confident_wrong"] and not s["validated_wrong"]
    assert s["critic_alternatives"] == 1 and len(s["claims"]) == 1  # critic alt: counted only
    assert s["proposal"] is None and not s["proposal_wrong"] and not s["proposal_missed"]


def test_a_validated_mechanism_next_to_its_cause_is_a_validated_wrong_claim() -> None:
    """S01/S04 on the real-model run: pool saturation HIGH + VALIDATED beside the deploy."""
    ev = _load()
    hyps = [_hv(1, "Deploy DEPLOY-9100 on a", "HIGH", "VALIDATED"),
            _hv(2, "DB connection pool saturation on a", "HIGH", "VALIDATED", kind="saturation")]  # fmt: skip
    s = ev.score(_e(DEPLOY), hyps, {"proposed": True, "deploy_key": "DEPLOY-9100"})
    assert s["top1"] and s["validated_wrong"] and s["proposal_correct"]
    assert [c["correct"] for c in s["claims"]] == [True, False]


def test_proposals_wrong_and_missed() -> None:
    ev = _load()
    wrong = ev.score(_e(DEPLOY), [_hv(1, "Deploy DEPLOY-9101", "HIGH", "VALIDATED")],
                     {"proposed": True, "deploy_key": "DEPLOY-9101"})  # fmt: skip
    assert wrong["proposal_wrong"] and not wrong["proposal_correct"]
    missed = ev.score(_e(DEPLOY), [_hv(1, "Deploy DEPLOY-9100", "HIGH", "PROPOSED")],
                      {"proposed": False, "why": "top deploy hypothesis is PROPOSED"})  # fmt: skip
    assert missed["proposal_missed"] and not missed["proposal_wrong"]
    # a saturation culprit can never be "missed" by a ROLLBACK rule
    sat = {"kind": "saturation", "subject": "db_pool_utilization"}
    assert not ev.score(_e(sat), [], None)["proposal_missed"]


def test_rescore_reads_what_was_stored_and_keeps_the_rest(tmp_path: Path) -> None:
    import json

    ev = _load()
    old = {"config": {"x": 1}, "summary": {}, "cases": [
        {"case": "k-S01", "incident": "INC-1", "outcome": "COMPLETE", "latency_s": 1.0,
         "cost_usd": 0.05, "invalid": False, "expected": _e(DEPLOY), "score": {"passed": True}}]}  # fmt: skip
    f = tmp_path / "run.json"
    f.write_text(json.dumps(old))

    class StubApi:
        calls: list[str] = []

        def call(self, method: str, path: str, body: Any = None, key: str | None = None) -> Any:
            self.calls.append(f"{method} {path}")
            if path.endswith("/hypotheses"):
                return [_hv(1, "Deploy DEPLOY-9100 on a", "HIGH", "VALIDATED")]
            return [{"status": "COMPLETE", "plan": {"proposal": {"deploy_key": "DEPLOY-9100"}}}]

    api = StubApi()
    out = ev.rescore(api, f)
    assert api.calls == ["GET /api/v1/incidents/INC-1/hypotheses",
                         "GET /api/v1/incidents/INC-1/investigations"]  # fmt: skip
    case = out["cases"][0]
    assert case["cost_usd"] == 0.05 and case["score"]["proposal_correct"]
    assert out["rescored_from"] == "run.json" and out["summary"]["proposals"]["correct"] == 1
