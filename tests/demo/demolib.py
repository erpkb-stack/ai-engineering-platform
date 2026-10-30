"""Phase 29 (ADR-043): the demo script as checks. Pure functions over what the API returned -
the runner fetches, these judge. Every step of docs/demo/demo-scenario.md that EXISTS is a
check here; a step that needs a real model is "n/a" on fake models, never a PASS."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

PASS, FAIL, NA = "PASS", "FAIL", "n/a"
DEMO_DEPLOY = "DEPLOY-4821"  # scripts/synth: checkout-api 2026.10.02.4, pool exhaustion
DEMO_DECOY = "cart-cache"
EVIDENCE_AGENTS = ("log_analysis", "metrics", "deployment", "knowledge")
REQUIRED_KINDS = ("LOG", "METRIC", "DEPLOY", "CONFIG", "INCIDENT")
REAL_ONLY = ("critic_challenge", "proposal", "approval", "decision_record")
# the machine the demo ran on (fix4): recorded, never part of the verdict; restarts decide if a PASS counts (`counts`)
K8S_CONTEXT = "kind-aeoi"
K8S_NODE = "aeoi-control-plane"
APP_NAMESPACE = "ai-engineering-platform"  # = scripts/k8s_manifests.NAMESPACE (pinned by a test)
CPU_PRESSURE_WARN = 20.0  # % of the last 10 s that runnable tasks waited for a CPU
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

Step = tuple[str, str]  # (status, detail)


def _t(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def alert_intake(first: tuple[int, Any], again: tuple[int, Any]) -> tuple[Step, str | None]:
    """Phase 29b (ADR-044): the demo starts from a SIGNED ALERT, not a typed form. Sent twice
    (Alertmanager re-sends): one incident, the second answer is `duplicate` with the same
    key, and the triage rule is recorded. Returns (step, incident key)."""
    (s1, b1), (s2, b2) = first, again
    if s1 != 200 or not isinstance(b1, dict):
        why = f": {b1}" if isinstance(b1, str) and b1 else ""  # fix4: the reason, not only 503
        return (FAIL, f"webhook answered {s1}{why}"), None
    r1 = (b1.get("results") or [{}])[0]
    if r1.get("outcome") != "created" or not r1.get("incident_key"):
        return (FAIL, f"first alert: {str(r1)[:160]}"), None
    r2 = ((b2 or {}).get("results") or [{}])[0] if isinstance(b2, dict) else {}
    key = str(r1["incident_key"])
    if s2 != 200 or r2.get("outcome") != "duplicate" or r2.get("incident_key") != key:
        return (FAIL, f"re-sent alert: {s2} {str(r2)[:120]} (must be duplicate of {key})"), key
    # fix5 (owner, 2026-10-29): the script says SEV1 by the tier-1 rule (minute 1:00). A SEV2
    # "tier unknown" (the catalog's degraded path) kept the alert but is not the demo - it
    # passed before, and hid a cold catalog behind a green step. The run goes on (key returned).
    sev, why = r1.get("severity"), str(r1.get("reason") or "")
    if sev != "SEV1" or "tier 1" not in why:
        return (FAIL, f"{key} {sev} by rule '{why}': the script needs SEV1 by the tier-1 rule"), key
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


def evidence_graph(hyps: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> Step:
    """Phase 29d (ADR-046): the graph screen draws GET /hypotheses edges. Checked on that
    data: the top cause has supporting links to >= 3 evidence kinds incl. DEPLOY, and every
    node of the graph is evidence this reader can open (no dangling node)."""
    live = _live(hyps)
    if not live:
        return FAIL, "no live hypothesis"
    edges = [(h, e) for h in hyps for e in h.get("edges") or []]
    stored = {e["evidence_key"] for e in evidence}
    dangling = sorted({e["evidence_key"] for _, e in edges} - stored)
    if dangling:
        return FAIL, f"{len(dangling)} graph node(s) not in the evidence list: {dangling[0]}"
    kinds = sorted({e["kind"] for e in live[0].get("edges") or [] if e["stance"] == "SUPPORTS"})
    if "DEPLOY" not in kinds or len(kinds) < 3:
        return FAIL, f"top cause supported by kinds {kinds}"
    contra = sum(1 for _, e in edges if e["stance"] == "CONTRADICTS")
    nodes = len({e["evidence_key"] for _, e in edges})
    return PASS, (
        f"{len(hyps)} hypotheses, {nodes} evidence nodes, {len(edges)} links "
        f"({contra} contradicting); top supported by {' '.join(kinds)}"
    )


def mtti(report: dict[str, Any] | None, real: bool) -> Step:
    """Phase 29e (ADR-047): minute 12:00 quotes MEASURED numbers or nothing. Needs the latest
    `make mtti` report: 6 manual cases scored and the AI side from a real-model run."""
    if report is None:
        return (FAIL if real else NA), "not measured yet: make manual (6 cases), then make mtti"
    man, ai, pr = report["manual"], report["ai_all"], report["paired"]
    real_ai = bool(report.get("ai_runs")) and all(r.get("paid_models") for r in report["ai_runs"])
    if report.get("manual_valid") is False:
        # owner decision 2026-10-29 (ADR-047 amendment): the manual side was declared invalid
        # with a reason; minute 12:00 quotes the AI side ONLY and says no human baseline exists
        if not real_ai:
            return FAIL, "the AI side is not a real-model run"
        return PASS, (
            f"AI ONLY - identified {ai['identified']}/{ai['cases']} (median "
            f"{ai['seconds_when_identified']['median']} s); NO valid human baseline: "
            f"{str(report.get('manual_invalid_reason'))[:80]}"
        )
    if man["cases"] < 6:
        return FAIL, f"only {man['cases']} manual cases scored (need 6)"
    if not all(r.get("paid_models") for r in report.get("ai_runs", [])) or not report.get(
        "ai_runs"
    ):
        return FAIL, "the AI side is not a real-model run"
    return PASS, (
        f"AI identified {ai['identified']}/{ai['cases']} (median "
        f"{ai['seconds_when_identified']['median']} s); manual {man['identified']}/"
        f"{man['cases']} (median {man['seconds_when_identified']['median']} s); paired: AI "
        f"first {pr['ai_first']}, manual first {pr['manual_first']}"
    )


def removed_shown(pm: dict[str, Any] | None, markdown: str, hyps: list[dict[str, Any]]) -> Step:
    """Phase 29c (ADR-045): what code removed from the model's text is SHOWN, with a reason,
    and kept OUT of the report. Owner (2026-10-28): a run that removed nothing is a PASS that
    says "0 removed" - a bad sentence is never planted into real output to make one."""
    if not pm:
        return FAIL, "no postmortem"
    if pm.get("removed_kept") is not True:
        return FAIL, "removed text not kept (a postmortem from before 29c?)"
    removed = pm.get("removed") or []
    counted = int(pm.get("sentences_dropped") or 0) + int(pm.get("numbers_rejected") or 0)
    if len(removed) != counted:
        return FAIL, f"{len(removed)} removed listed, {counted} counted"
    if pm.get("removed_redacted"):
        return FAIL, f"{pm['removed_redacted']} removed text(s) hidden from the SRE"
    expl = sum(1 for h in hyps if (h.get("detail") or {}).get("explanation_removed"))
    if not removed:
        return PASS, f"0 removed from the postmortem this run; {expl} ranker explanation(s) removed"
    if "## Removed by code" not in markdown:
        return FAIL, "removed sentences are not in the markdown"
    report = markdown.split("## Removed by code")[0]
    inside = [r["text"][:40] for r in removed if r["text"][:40] in report]
    if inside:
        return FAIL, f"removed text inside the report: {inside[0]!r}"
    reasons = sorted({r["reason"] for r in removed})
    return PASS, (
        f"{len(removed)} removed ({', '.join(reasons)}), shown apart with reasons; "
        f"{expl} ranker explanation(s) removed"
    )


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
        # the reason, not 160 characters of the approval record (Phase 29 close)
        why = (decision or {}).get("execution_error") or str(decision)[:160]
        return FAIL, f"IC decision did not execute: {str(why)[:300]}"
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


def problem_text(raw: bytes, limit: int = 200) -> str:
    """fix4: an error answer as one short line (problem+json `detail`/`title`, else the text).
    Two runs failed with a bare `webhook answered 503` - the reason was thrown away."""
    text = raw.decode("utf-8", "replace")
    try:
        doc = json.loads(text)
    except ValueError:
        doc = None
    if isinstance(doc, dict):
        text = str(doc.get("detail") or doc.get("title") or text)
    text = _CONTROL.sub(" ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---- the machine (fix4). Phase 29 gate, 2026-10-29: two 503s came from a restart storm on a
# power-limited Mac (pmset CPU_Speed_Limit 39, node CPU pressure 39%, etcd timeouts), not from
# the code. The result file now says what the machine was doing, so a FAIL caused by the
# machine reads as one - and a PASS on a sick machine is not mistaken for a healthy one.


def speed_limit(pmset_therm: str) -> int | None:
    """`pmset -g therm` -> CPU_Speed_Limit (100 = not limited). None = not a Mac / unknown."""
    m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", pmset_therm)
    return int(m.group(1)) if m else None


def cpu_pressure(psi: str) -> float | None:
    """/proc/pressure/cpu -> the `some avg10` share (% of time a runnable task waited)."""
    m = re.search(r"^some\s.*?avg10=([\d.]+)", psi, re.M)
    return float(m.group(1)) if m else None


def pod_restarts(pods_json: str) -> dict[str, list[Any]] | None:
    """`kubectl get pods -o json` -> {app: [pod name, restarts]}. None = not readable."""
    try:
        items = json.loads(pods_json)["items"]
    except (ValueError, KeyError, TypeError):
        return None
    out: dict[str, list[Any]] = {}
    for p in items:
        meta = p.get("metadata") or {}
        app = (meta.get("labels") or {}).get("app.kubernetes.io/name") or meta.get("name", "?")
        n = sum(
            c.get("restartCount", 0) for c in (p.get("status") or {}).get("containerStatuses") or []
        )
        out[str(app)] = [meta.get("name", "?"), n]
    return out


def environment_warnings(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """What a reader must know before trusting this run. Facts only; the verdict is unchanged."""
    w = []
    limits = [
        x for x in (before.get("cpu_speed_limit"), after.get("cpu_speed_limit")) if x is not None
    ]
    if limits and min(limits) < 100:
        w.append(f"Mac CPU power-limited: CPU_Speed_Limit {min(limits)} (100 = full speed)")
    psi = [
        x
        for x in (before.get("node_cpu_pressure"), after.get("node_cpu_pressure"))
        if x is not None
    ]
    if psi and max(psi) >= CPU_PRESSURE_WARN:
        w.append(f"node CPU pressure {max(psi)}% (tasks waiting for a CPU)")
    for ns, moved in restarted(before, after).items():
        if moved:
            w.append(f"{ns} pods restarted or replaced DURING the run: {', '.join(moved)}")
    return w


def restarted(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[str] | None]:
    """Per namespace: the pods that restarted or were replaced during the run; None = unreadable."""
    out: dict[str, list[str] | None] = {}
    for ns in ("kube-system", "app"):
        b, a = (before.get("restarts") or {}).get(ns), (after.get("restarts") or {}).get(ns)
        if b is None or a is None:
            out[ns] = None
            continue
        out[ns] = sorted(k for k in a if k not in b or a[k][0] != b[k][0] or a[k][1] > b[k][1])
    return out


def counts(verdict_: str, before: dict[str, Any], after: dict[str, Any]) -> tuple[bool, str]:
    """The GATE rule (owner, 2026-10-29, fixed before the gate restarted): a real-model PASS
    counts only if no pod restarted or was replaced DURING the run - a restart changes the
    system under test mid-run. CPU speed and pressure are warnings, not disqualifying: a slow
    machine that still passes is a real pass. Restarts that cannot be read = not counted
    (the rule cannot be checked)."""
    if verdict_ != "PASS":
        return False, f"verdict {verdict_}"
    moved = restarted(before, after)
    blind = [ns for ns, m in moved.items() if m is None]
    if blind:
        return False, f"pod restarts unreadable ({', '.join(blind)}): the rule cannot be checked"
    hit = [f"{ns}: {', '.join(m)}" for ns, m in moved.items() if m]
    if hit:
        return False, "pods restarted or replaced during the run - " + "; ".join(hit)
    return True, "real-model PASS, no pod restarted or replaced during the run"
