"""Phase 32a: the pure parts of a real-data run - the pass rules, fixed in this file BEFORE the
first run (like tests/chaos/chaoslib.py, ADR-040: a FAIL is a finding, never a reason to change a
rule; a rule change is a new RULES_VERSION and is written down in docs/phases/phase-32.md).

F1 traffic surge on TARGET: the top live hypothesis is a code-written `traffic` cause on TARGET,
     VALIDATED. Found but not VALIDATED = "found, not validated" (a FAIL, said apart).
F2 Postgres stopped: no cause kind fits a dependency outage (owner decision 32a-2a), so the rule
     is the negative one: NO live hypothesis at HIGH confidence.
Any run: the METRIC evidence must come from Prometheus and the LOG evidence from Loki - else the
     run was not on real data and is INVALID, whatever the hypotheses say.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

RULES_VERSION = 1
PASS, FAIL, INVALID = "PASS", "FAIL", "INVALID"
REAL_SOURCES = {"METRIC": "prometheus", "LOG": "loki"}


def live(hyps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((h for h in hyps if h.get("status") != "REJECTED"), key=lambda h: h["rank"])


def cause_kind(h: dict[str, Any]) -> str:
    return str((h.get("detail") or {}).get("cause_kind") or "?")


def describe(h: dict[str, Any] | None) -> str:
    if h is None:
        return "no live hypothesis"
    return (f"#{h['rank']} {cause_kind(h)} [{h.get('confidence')}] {h.get('status')}: "
            f"{str(h.get('statement', ''))[:140]}")  # fmt: skip


def data_check(evidence: list[dict[str, Any]]) -> tuple[str, str]:
    """INVALID if any METRIC/LOG evidence names a non-real source. No METRIC/LOG evidence at all
    is NOT invalid (the agents may have found nothing): it is reported as an empty kind."""
    wrong = sorted({f"{e['kind']}<-{e.get('source_system')}" for e in evidence
                    if e.get("kind") in REAL_SOURCES
                    and e.get("source_system") != REAL_SOURCES[e["kind"]]})  # fmt: skip
    if wrong:
        return INVALID, f"not real data: {', '.join(wrong)} (switches on? make images?)"
    return PASS, "metrics from prometheus, logs from loki"


def evidence_summary(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    kinds = Counter(str(e.get("kind")) for e in evidence)
    sources = Counter(f"{e.get('kind')}:{e.get('source_system')}" for e in evidence)
    return {"by_kind": dict(sorted(kinds.items())), "by_source": dict(sorted(sources.items())),
            "empty_kinds": [k for k in ("METRIC", "LOG", "DEPLOY") if not kinds.get(k)]}  # fmt: skip


def rule_f1(hyps: list[dict[str, Any]], target: str) -> tuple[str, str]:
    top = (live(hyps) or [None])[0]
    if top is None:
        return FAIL, "no live hypothesis"
    on_target = f" on {target} " in f" {top.get('statement', '')} "
    if cause_kind(top) == "traffic" and on_target:
        if top.get("status") == "VALIDATED":
            return PASS, describe(top)
        return FAIL, "found, not validated: " + describe(top)
    return FAIL, "top is not traffic on " + target + ": " + describe(top)


def rule_f2(hyps: list[dict[str, Any]]) -> tuple[str, str]:
    high = [h for h in live(hyps) if h.get("confidence") == "HIGH"]
    if high:
        return FAIL, "a HIGH claim on an outage no cause kind fits: " + describe(high[0])
    top = (live(hyps) or [None])[0]
    return PASS, "no HIGH claim; top: " + describe(top)


def verdict(data: tuple[str, str], rule: tuple[str, str]) -> str:
    return INVALID if data[0] == INVALID else rule[0]


def tally(statuses: list[int]) -> dict[str, int]:
    """Request outcomes by class (0 = no answer: refused, timeout, dropped)."""
    c = Counter("none" if s == 0 else f"{s // 100}xx" for s in statuses)
    return dict(sorted(c.items()))
