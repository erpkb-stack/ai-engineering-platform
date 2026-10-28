"""Phase 29e (ADR-047): the MTTI rules, fixed before any timing. Pure: no services."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests/mtti"))
import mttilib as m
from manual import build, next_case

pytestmark = pytest.mark.unit
DET = datetime(2026, 10, 2, 9, 50, tzinfo=UTC)
CULPRIT = {"kind": "change", "subject": "DEPLOY-9100"}


def test_the_sample_is_fixed_by_the_seed_not_chosen_and_mixes_the_packs() -> None:
    keys = [f"investigations-v1-S{i:02d}" for i in range(1, 12)]
    keys += [f"investigations-v2-S{i}" for i in range(17, 23)]
    a, b = m.sample_order(keys), m.sample_order(list(reversed(keys)))
    assert a == b  # same order whatever the input order
    assert sorted(a) == sorted(set(keys) - {"investigations-v1-S01"})  # the demo's twin: out
    packs = [k.split("-")[1] for k in a[: m.SAMPLE]]
    assert packs.count("v1") == 4 and packs.count("v2") == 2
    # reserves alternate packs, so a replacement keeps the mix
    assert [k.split("-")[1] for k in a[m.SAMPLE : m.SAMPLE + 4]] == ["v1", "v2", "v1", "v2"]
    assert m.detectable([{"expected": {"culprit": None}}, {"expected": {"culprit": CULPRIT}}]) == [
        {"expected": {"culprit": CULPRIT}}
    ]


def test_manual_identified_needs_the_exact_cause_within_the_cap() -> None:
    ok = {"kind": "change", "subject": "DEPLOY-9100"}
    assert m.identified_manual(ok, CULPRIT, 600)
    assert not m.identified_manual(ok, CULPRIT, m.CAP_S + 1)  # over the cap: not identified
    assert not m.identified_manual({"kind": "saturation", "subject": "DEPLOY-9100"}, CULPRIT, 60)
    assert not m.identified_manual({"kind": "change", "subject": "DEPLOY-9101"}, CULPRIT, 60)
    assert not m.identified_manual(None, CULPRIT, 60)


def test_ai_identified_needs_the_top_cause_and_validated() -> None:
    case = {"invalid": False, "score": {"top1": True, "top_status": "VALIDATED"}}
    assert m.identified_ai(case) == (True, False)
    assert m.identified_ai({**case, "score": {"top1": True, "top_status": "PROPOSED"}}) == (
        False,
        True,
    )
    assert m.identified_ai({**case, "score": {"top1": False, "top_status": "VALIDATED"}}) == (
        False,
        False,
    )
    assert m.identified_ai({**case, "invalid": True}) == (False, False)


def test_answers_parse_strictly() -> None:
    assert m.parse_answer(["change", "DEPLOY-9100"]) == CULPRIT
    for bad in (["DEPLOY-9100"], ["cause", "DEPLOY-9100"], ["change", "a", "b"]):
        with pytest.raises(ValueError, match="answer <kind> <subject>"):
            m.parse_answer(bad)
    # fix2: the subject must be a deploy key or a metric (`answer change code` was accepted)
    with pytest.raises(ValueError, match="deploy key"):
        m.parse_answer(["change", "code"])
    with pytest.raises(ValueError, match="metric name"):
        m.parse_answer(["saturation", "pool"])
    assert m.parse_answer(["traffic", "requests_per_sec"])["subject"] == "requests_per_sec"
    assert m.parse_answer(["alternative", "dns"]) == {"kind": "alternative", "subject": "dns"}


def test_tool_output_cannot_drive_the_terminal() -> None:
    evil = "ok\x1b[2J\x1b]0;pwned\x07\nnext line\x9b"
    out = m.clean(evil)
    assert "\x1b" not in out and "\x07" not in out and "\x9b" not in out and "\n" not in out
    assert m.clean("x" * 500, 10) == "x" * 9 + "…"
    # the services answer in the DB session's offset: shown in UTC like the alert (found live)
    assert (
        m.clean("ts=2026-10-06T08:50:14-05:00 level=ERROR") == "ts=2026-10-06 13:50:14Z level=ERROR"
    )


def test_commands_map_to_the_agents_tools_relative_to_detection() -> None:
    tool, args = build("logs", ["orders-api", "code=ERR_POOL_TIMEOUT", "from=-30"], DET)
    assert tool == "search_logs" and args["error_code"] == "ERR_POOL_TIMEOUT"
    assert (
        args["start"] == "2026-10-02T09:20:00+00:00" and args["end"] == "2026-10-02T10:00:00+00:00"
    )
    assert build("metrics", ["orders-api", "db_pool_utilization"], DET)[0] == "query_metrics"
    assert build("deploys", ["orders-api"], DET)[1]["start"] == "2026-10-01T10:00:00+00:00"
    assert build("diff", ["DEPLOY-9100"], DET) == ("get_config_diff", {"deploy_key": "DEPLOY-9100"})
    assert build("tool", ["get_commit", '{"sha":', '"abc1234"}'], DET) == (
        "get_commit",
        {"sha": "abc1234"},
    )
    with pytest.raises(ValueError, match="window"):
        build("logs", ["orders-api", "from=10", "to=-10"], DET)
    with pytest.raises(ValueError, match="at most 24 h"):  # the tools refuse it (found live)
        build("deploys", ["orders-api", "from=-1440"], DET)
    with pytest.raises(ValueError, match="type help"):
        build("logs", [], DET)


def test_an_abandoned_case_is_replaced_from_its_own_pack_and_never_counted() -> None:
    """fix1: four abandons in a row must not change the 4 v1 + 2 v2 mix."""
    v1 = [f"investigations-v1-S{i:02d}" for i in range(2, 9)]
    v2 = [f"investigations-v2-S{i}" for i in range(17, 21)]
    order = [v1[0], v2[0], v1[1], v1[2], v2[1], v1[3], v1[4], v2[2], v1[5], v2[3], v1[6]]
    state = {"order": order, "done": [{"case": v2[0], "abandoned": "phone"}]}
    assert next_case(state) == v1[0]
    state["done"].append({"case": v1[0], "answer": {}})
    assert next_case(state) == v1[1]  # the fixed order goes on
    state["done"] += [{"case": k, "answer": {}} for k in (v1[1], v1[2], v2[1], v1[3])]
    # 4 v1 + 1 v2 counted: the abandoned v2 is replaced by a v2, skipping the v1 reserves
    assert next_case(state) == v2[2]
    state["done"].append({"case": v2[2], "answer": {}})
    assert next_case(state) is None


def test_only_the_typed_word_start_opens_a_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """fix1: a Return left over from a paste opened two timed cases on the Mac."""
    import manual

    answers = iter(["", "", "   ", "yes", "start"])
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers))
    assert manual.wait_for_start("Case 1 of 6") is True
    answers2 = iter(["", "quit"])
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers2))
    assert manual.wait_for_start("Case 1 of 6") is False

    def eof(_p: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert manual.wait_for_start("Case 1 of 6") is False


def _ai(case: str, s: float, ok: bool = True, status: str = "VALIDATED") -> dict:
    return {"case": case, "latency_s": s, "invalid": False,
            "expected": {"culprit": CULPRIT}, "score": {"top1": ok, "top_status": status}}  # fmt: skip


def test_report_pairs_the_same_cases_and_never_uses_the_cap_as_a_time() -> None:
    manual = [
        {"case": "a", "seconds": 600, "answer": CULPRIT, "culprit": CULPRIT, "tool_calls": 9},
        {"case": "b", "seconds": 300, "answer": {"kind": "change", "subject": "DEPLOY-1"},
         "culprit": CULPRIT},  # wrong answer
        {"case": "c", "seconds": 2000, "answer": CULPRIT, "culprit": CULPRIT},  # over the cap
        {"case": "d", "seconds": 20, "answer": CULPRIT, "culprit": CULPRIT},  # faster than AI
        {"case": "e", "seconds": 5, "abandoned": "fire alarm", "culprit": CULPRIT},
    ]  # fmt: skip
    ai = {"a": _ai("a", 50), "b": _ai("b", 40), "c": _ai("c", 45, status="PROPOSED"),
          "d": _ai("d", 60), "x": _ai("x", 30, ok=False)}  # fmt: skip
    r = m.report(manual, ai)
    by = {c["case"]: c for c in r["cases"]}
    assert by["e"] == {"case": "e", "abandoned": "fire alarm"}
    assert r["manual"]["cases"] == 4 and r["manual"]["identified"] == 2  # a, d
    assert r["manual"]["seconds_when_identified"] == {
        "n": 2,
        "median": 310.0,
        "min": 20,
        "max": 600,
    }
    assert r["ai_all"]["identified"] == 3 and r["ai_all"]["cases"] == 5
    assert (
        r["ai_all"]["correct_not_validated"] == 1
    )  # c: right, but the system did not stand behind it
    # a: AI first; b: AI (manual wrong); c: neither identified -> no winner; d: manual first
    assert (r["paired"]["ai_first"], r["paired"]["manual_first"]) == (2, 1)
    assert r["paired"]["ratio_manual_over_ai_when_both"]["n"] == 2  # a and d


def test_wilson_and_the_sign_test() -> None:
    assert m.wilson(8, 70) == (0.059, 0.21)  # the 29c postmortem measurement
    assert m.sign_test(6, 0) == 0.0312 and m.sign_test(3, 3) == 1.0 and m.sign_test(0, 0) == 1.0


def test_the_answer_metrics_are_exactly_the_tools_metrics() -> None:
    from aeoi_tools.schemas import METRICS

    assert m.METRICS == METRICS
