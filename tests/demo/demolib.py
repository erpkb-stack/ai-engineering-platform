"""Phase 29 (ADR-043): the demo script as checks. Pure functions over what the API returned -
the runner fetches, these judge. Every step of docs/demo/demo-scenario.md that EXISTS is a
check here; a step that needs a real model is "n/a" on fake models, never a PASS."""

from __future__ import annotations

from datetime import datetime
from typing import Any

PASS, FAIL, NA = "PASS", "FAIL", "n/a"
DEMO_DEPLOY = "DEPLOY-4821"  # scripts/synth: checkout-api 2026.10.02.4, pool exhaustion
DEMO_DECOY = "cart-cache"
EVIDENCE_AGENTS = ("log_analysis", "metrics", "deployment", "knowledge")
REQUIRED_KINDS = ("LOG", "METRIC", "DEPLOY", "CONFIG", "INCIDENT")
REAL_ONLY = ("critic_challenge", "proposal", "approval", "decision_record")

Step = tuple[str, str]  # (status, detail)


def _t(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def alert_intake(first: tuple[int, Any], again: tuple[int, Any]) -> tuple[Step, str | None]:
    """Phase 29b (ADR-044): the demo starts from a SIGNED ALERT, not a typed form. Sent twice
    (Alertmanager re-sends): one incident, the second answer is `duplicate` with the same
    key, and the triage rule is recorded. Returns (step, incident key)."""
    (s1, b1), (s2, b2) = first, again
    if s1 != 200 or not isinstance(b1, dict):
        return (FAIL, f"webhook answered {s1}"), None
    r1 = (b1.get("results") or [{}])[0]
    if r1.get("outcome") != "created" or not r1.get("incident_key"):
        return (FAIL, f"first alert: {str(r1)[:160]}"), None
    r2 = ((b2 or {}).get("results") or [{}])[0] if isinstance(b2, dict) else {}
    key = str(r1["incident_key"])
    if s2 != 200 or r2.get("outcome") != "duplicate" or r2.get("incident_key") != key:
        return (FAIL, f"re-sent alert: {s2} {str(r2)[:120]} (must be duplicate of {key})"), key
    return (
        PASS,
        f"{key} {r1.get('severity')} by rule '{r1.get('reason')}'; re-sent = duplicate",
    ), key


def parallel_agents(inv: dict[str, Any]) -> Step:
    """The 4 evidence agents ran AT THE SAME TIME: every interval overlaps every other."""
    spans = []
    for t in inv.get("tasks", []):
        if t["agent"] in EVIDENCE_AGENTS:
            a, b = _t(t.get("started_at")), _t(t.get("finished_at"))
            if a is None or b is None:
                return FAIL, f"{t['agent']} has no start/finish"
            spans.append((t["agent"], a, b))
    if {s[0] for s in spans} != set(EVIDENCE_AGENTS):
        return FAIL, f"evidence agents seen: {sorted({s[0] for s in spans})}"
    latest_start = max(s[1] for s in spans)
    earliest_end = min(s[2] for s in spans)
    if latest_start < earliest_end:
        return PASS, f"all 4 overlap for {(earliest_end - latest_start).total_seconds():.1f} s"
    return FAIL, "not all evidence agents overlapped (sequential?)"


def evidence_kinds(evidence: list[dict[str, Any]]) -> Step:
    kinds = sorted({e["kind"] for e in evidence})
    missing = [k for k in REQUIRED_KINDS if k not in kinds]
    return (FAIL, f"missing {missing}; have {kinds}") if missing else (PASS, " ".join(kinds))


def _live(hyps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((h for h in hyps if h["status"] != "REJECTED"), key=lambda h: h["rank"])


def top_hypothesis(hyps: list[dict[str, Any]]) -> Step:
    live = _live(hyps)
    if not live:
        return FAIL, "no live hypothesis"
    top = live[0]
    ok = DEMO_DEPLOY in top["statement"]
    return (PASS if ok else FAIL), f"#1 [{top['confidence']}] {top['statement'][:120]}"


def decoy(hyps: list[dict[str, Any]]) -> Step:
    """The cart-cache restart must not be the answer: never #1, never HIGH while live."""
    mine = [h for h in hyps if DEMO_DECOY in h["statement"]]
    if not mine:
        return PASS, "the decoy never became a candidate cause (code saw no support for it)"
    bad = [
        h
        for h in mine
        if h["status"] != "REJECTED" and (h["rank"] == 1 or h["confidence"] == "HIGH")
    ]
    if bad:
        return FAIL, f"decoy ranked {bad[0]['rank']} [{bad[0]['confidence']}]"
    return PASS, "; ".join(f"#{h['rank']} {h['status']} {h['confidence']}" for h in mine)


def critic_challenge(hyps: list[dict[str, Any]], real: bool) -> Step:
    if not real:
        return NA, "fake models never validate (ADR-037)"
    live = _live(hyps)
    if not live:
        return FAIL, "no live hypothesis"
    top = live[0]
    verdict = critic_of(top)
    ok = top["status"] == "VALIDATED" and verdict == "supported"
    return (PASS if ok else FAIL), f"#1 {top['status']}, critic {verdict}"


def critic_of(h: dict[str, Any]) -> str:
    """The API's HypothesisRecord keeps the critic's verdict in `detail` (found on the Mac's
    first real-model run: the check read a top-level key and crashed the runner)."""
    return str((h.get("detail") or {}).get("critic_verdict") or "not_reviewed")


def safe(check: Any, *args: Any) -> Step:
    """A check that crashes is a FAIL with its reason - the runner must finish every step."""
    try:
        result: Step = check(*args)
        return result
    except Exception as exc:
        return FAIL, f"check crashed: {type(exc).__name__}: {exc}"[:200]


def report(pm: dict[str, Any] | None, markdown: str) -> Step:
    if not pm:
        return FAIL, "no postmortem"
    rc = (pm.get("root_cause") or {}).get("statement", "")
    sections = all(s in markdown for s in ("## Similar past incidents",))
    if DEMO_DEPLOY not in rc:
        return FAIL, f"root cause: {rc[:120] or 'none'}"
    return (PASS if sections else FAIL), f"root cause cites {DEMO_DEPLOY}; sections ok={sections}"


def proposal(approvals: list[dict[str, Any]], real: bool) -> tuple[Step, str | None]:
    if not real:
        return (NA, "the propose rule needs a VALIDATED cause (real models)"), None
    for a in approvals:
        if (a["action"] == "rollback_deployment" and a["source"] == "investigation"
                and a["action_args"].get("deploy_key") == DEMO_DEPLOY):  # fmt: skip
            return (PASS, f"{a['status']} rollback of {DEMO_DEPLOY} for {a['requested_by']}"), a[
                "id"
            ]
    return (FAIL, f"no rollback proposal ({len(approvals)} approvals)"), None


def approval(engineer_code: int, decision: dict[str, Any] | None, approval_after: dict[str, Any] | None,
             incident_status: str | None) -> Step:  # fmt: skip
    if engineer_code != 403:
        return FAIL, f"ENGINEER decision answered {engineer_code} (must be 403)"
    if not decision or decision.get("executed") is not True:
        return FAIL, f"IC decision did not execute: {str(decision)[:160]}"
    status = (approval_after or {}).get("status")
    if status != "EXECUTED" or incident_status != "MITIGATED":
        return FAIL, f"approval {status}, incident {incident_status}"
    return PASS, "ENGINEER 403; IC approved -> EXECUTED; incident MITIGATED"


def decision_record(rec: dict[str, Any] | None) -> Step:
    """Audit: who decided, when, why, over WHICH arguments (hash), what ran."""
    if not rec:
        return FAIL, "no record"
    need = ("decided_by", "decided_at", "reason", "args_sha256", "tool_call_id")
    missing = [k for k in need if not rec.get(k)]
    return (
        (FAIL, f"missing {missing}")
        if missing
        else (PASS, f"by {rec['decider_role']}, args {rec['args_sha256'][:12]}")
    )


def injection(results: list[dict[str, Any]]) -> Step:
    n = sum(1 for r in results if "adversarial/" in r["source_uri"])
    return (
        (PASS, "0 quarantined documents returned")
        if n == 0
        else (FAIL, f"{n} quarantined returned")
    )


def verdict(steps: dict[str, Step], real: bool) -> str:
    """A COUNTED pass (Phase 29 gate) = real models and every step PASS. Fake = rehearsal."""
    if any(s == FAIL for s, _ in steps.values()):
        return "FAIL"
    if not real:
        return "REHEARSAL-OK"
    return "PASS" if all(s == PASS for s, _ in steps.values()) else "FAIL"
