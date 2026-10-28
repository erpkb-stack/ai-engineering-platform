"""Phase 29e (ADR-047): MTTI - mean time to IDENTIFY the root cause, AI vs a person with the
same tools. Pure functions only (the CLI and the report do the IO); unit-tested.

The rules are FIXED HERE, before any timing (as the chaos rules, ADR-040):
- Cases: the DETECTABLE planted cases of the investigations packs v1 + v2 (a culprit exists).
  The manual sample = 4 from v1 + 2 from v2, each drawn with a fixed seed (no choosing), in a
  seeded order. v1-S01 is excluded from the MANUAL sample: it is the demo's own pattern (pool
  exhaustion after a deploy), which the owner has watched dozens of times. (A plain shuffle of
  all 17 gave six v1 cases starting with S01 - found before any timing.)
- Manual clock: starts when the case is opened (only the alert is shown), stops at `answer`.
  No pause. Identified = the answer's (cause kind, subject) equals the planted culprit, AND the
  answer came within the 30-minute cap. Over the cap or wrong = "not identified" (no time).
- AI clock: the eval runner's latency (incident created -> investigation finished). Identified
  = the top hypothesis is the planted cause AND the system stood behind it (VALIDATED).
  A correct but unvalidated top is reported apart, never counted as identified.
- Reported per case and as medians with min-max; identified rates with Wilson 95% intervals;
  the paired comparison (same cases) with an exact sign test. Never one headline percentage.
"""

from __future__ import annotations

import math
import random
import re
import statistics
from datetime import UTC, datetime, timedelta
from typing import Any

SEED = 2029
SAMPLE = 6
PER_PACK = {"v1": 4, "v2": 2}
NOT_MANUAL = ("investigations-v1-S01",)
CAP_S = 30 * 60
CAUSE_KINDS = ("change", "saturation", "traffic", "alternative")
WINDOW_MAX_MIN = 24 * 60  # the tools refuse a window longer than 24 h
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)")
# terminal control characters (ESC sequences can rewrite the screen): tool output is untrusted
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def detectable(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in cases if (c["expected"] or {}).get("culprit")]


def sample_order(keys: list[str], seed: int = SEED) -> list[str]:
    """The fixed order cases are offered in: the first SAMPLE are the sample (PER_PACK from
    each pack, shuffled together); the rest are reserves, used only after an `abandon`, and
    alternate between the packs so a replacement keeps the mix."""
    rnd = random.Random(seed)
    packs: dict[str, list[str]] = {}
    for k in sorted(set(keys) - set(NOT_MANUAL)):
        packs.setdefault(k.split("-")[1], []).append(k)
    for p in sorted(packs):
        rnd.shuffle(packs[p])
    sample = [k for p in sorted(PER_PACK) for k in packs.get(p, [])[: PER_PACK[p]]]
    rnd.shuffle(sample)
    rest = {p: packs[p][PER_PACK.get(p, 0) :] for p in sorted(packs)}
    reserves = []
    while any(rest.values()):
        for p in sorted(rest):
            if rest[p]:
                reserves.append(rest[p].pop(0))
    return sample + reserves


def utc(text: str) -> str:
    """Every ISO time in UTC with a Z (the services answer in the DB session's offset, -05:00
    in the sandbox; the alert is UTC - mixing them misleads the timer's reader)."""

    def one(mt: re.Match[str]) -> str:
        d = datetime.fromisoformat(mt.group(0).replace("Z", "+00:00")).astimezone(UTC)
        return d.strftime("%Y-%m-%d %H:%M:%SZ")

    return _ISO.sub(one, text)


def clean(text: Any, limit: int = 300) -> str:
    """Tool output on a terminal: no control characters, one line, times in UTC, capped."""
    s = utc(_CONTROL.sub(" ", str(text)).replace("\n", " "))
    return s if len(s) <= limit else s[: limit - 1] + "…"


def rel_window(detected: datetime, start_min: int, end_min: int) -> tuple[str, str]:
    """Minutes relative to detection -> ISO times (the agents use the same anchor)."""
    if not (start_min < end_min and end_min - start_min <= WINDOW_MAX_MIN):
        raise ValueError("window: from < to, at most 24 h long (1440 minutes)")
    a = detected + timedelta(minutes=start_min)
    b = detected + timedelta(minutes=end_min)
    return a.isoformat(), b.isoformat()


def opts(words: list[str]) -> tuple[list[str], dict[str, str]]:
    """`logs orders-api code=ERR_X from=-60` -> (["orders-api"], {"code": "ERR_X", "from": "-60"})."""
    pos, kv = [], {}
    for w in words:
        if "=" in w:
            k, v = w.split("=", 1)
            kv[k] = v
        else:
            pos.append(w)
    return pos, kv


METRICS = ("http_5xx_per_min", "p95_latency_ms", "requests_per_sec", "cpu_util",
           "db_pool_utilization", "db_connections_active", "db_query_latency_ms",
           "cache_hit_ratio", "thread_pool_utilization")  # fmt: skip  # = the tool's list
_DEPLOY = re.compile(r"^DEPLOY-\d{1,24}$")


def parse_answer(words: list[str]) -> dict[str, str]:
    """`answer change DEPLOY-9100` or `answer saturation db_pool_utilization`. fix2: the
    SUBJECT is checked too - `answer change code` (0 tool calls, Mac 2026-10-28) was accepted
    and would have counted as a failed case. A refused answer does NOT stop the clock."""
    if len(words) != 2 or words[0] not in CAUSE_KINDS:
        raise ValueError(f"answer <kind> <subject>; kind one of {', '.join(CAUSE_KINDS)}")
    kind, subject = words
    if kind == "change" and not _DEPLOY.match(subject):
        raise ValueError("a change is answered with its deploy key, e.g. answer change DEPLOY-9100")
    if kind in ("saturation", "traffic") and subject not in METRICS:
        raise ValueError(f"a {kind} is answered with a metric name: {', '.join(METRICS)}")
    return {"kind": kind, "subject": subject}


def identified_manual(
    answer: dict[str, str] | None, culprit: dict[str, str], seconds: float
) -> bool:
    return bool(
        answer
        and seconds <= CAP_S
        and answer["kind"] == culprit["kind"]
        and answer["subject"] == culprit["subject"]
    )


def identified_ai(case: dict[str, Any]) -> tuple[bool, bool]:
    """(identified, correct_but_not_validated) from one eval result case."""
    if case.get("invalid"):
        return False, False
    s = case.get("score") or {}
    top_ok = bool(s.get("top1"))
    validated = (s.get("top_status") or "") == "VALIDATED"
    return top_ok and validated, top_ok and not validated


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round(max(0.0, c - h), 3), round(min(1.0, c + h), 3)


def spread(xs: list[float]) -> dict[str, float | int | None]:
    if not xs:
        return {"n": 0, "median": None, "min": None, "max": None}
    return {"n": len(xs), "median": round(statistics.median(xs), 1), "min": round(min(xs), 1),
            "max": round(max(xs), 1)}  # fmt: skip


def sign_test(wins: int, losses: int) -> float:
    """Exact two-sided sign test (ties dropped): how surprising is this split if neither side
    were faster? With 6 pairs the smallest possible p is 0.031."""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return round(min(1.0, 2 * tail), 4)


def report(manual: list[dict[str, Any]], ai: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """manual: finished manual cases {case, seconds, answer, culprit, abandoned?};
    ai: eval result cases by case key (current code, real models)."""
    rows, pairs = [], []
    for m in manual:
        if m.get("abandoned"):
            rows.append({"case": m["case"], "abandoned": m["abandoned"]})
            continue
        man_ok = identified_manual(m.get("answer"), m["culprit"], m["seconds"])
        a = ai.get(m["case"])
        ai_ok, ai_unvalidated = identified_ai(a) if a else (False, False)
        row = {"case": m["case"], "manual_s": round(m["seconds"], 1), "manual_identified": man_ok,
               "tool_calls": m.get("tool_calls", 0), "ai_s": a["latency_s"] if a else None,
               "ai_identified": ai_ok, "ai_correct_not_validated": ai_unvalidated}  # fmt: skip
        rows.append(row)
        if a is not None:
            pairs.append(row)
    ai_cases = [c for c in ai.values() if (c.get("expected") or {}).get("culprit")]
    ai_ok_times = [c["latency_s"] for c in ai_cases if identified_ai(c)[0]]
    done = [r for r in rows if "abandoned" not in r]
    man_ok = [r for r in done if r["manual_identified"]]
    # who identified FIRST per pair: a side that did not identify loses to one that did
    wins = losses = 0
    for r in pairs:
        if r["ai_identified"] and (not r["manual_identified"] or r["ai_s"] < r["manual_s"]):
            wins += 1
        elif r["manual_identified"] and (not r["ai_identified"] or r["manual_s"] < r["ai_s"]):
            losses += 1
    both = [r for r in pairs if r["ai_identified"] and r["manual_identified"]]
    return {
        "rules": {"cap_s": CAP_S, "seed": SEED, "sample": SAMPLE,
                  "ai_identified": "top = planted cause AND VALIDATED",
                  "manual_identified": "answer = planted (kind, subject) within the cap"},
        "cases": rows,
        "ai_all": {"cases": len(ai_cases), "identified": len(ai_ok_times),
                   "identified_wilson95": wilson(len(ai_ok_times), len(ai_cases)),
                   "seconds_when_identified": spread(ai_ok_times),
                   "correct_not_validated": sum(1 for c in ai_cases if identified_ai(c)[1])},
        "manual": {"cases": len(done), "identified": len(man_ok),
                   "identified_wilson95": wilson(len(man_ok), len(done)),
                   "seconds_when_identified": spread([r["manual_s"] for r in man_ok])},
        "paired": {"pairs": len(pairs), "ai_first": wins, "manual_first": losses,
                   "sign_test_p": sign_test(wins, losses),
                   "ratio_manual_over_ai_when_both": spread(
                       [r["manual_s"] / r["ai_s"] for r in both if r["ai_s"]])},
    }  # fmt: skip
