"""Phase 28a (ADR-040): failure experiments - what is injected, and the rules decided BEFORE a
run. Pure functions: the runner records, these judge. A FAIL is a finding about the system,
written down as one - never a reason to loosen a rule after the run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

REQUEST_TIMEOUT_S = 45.0  # > the api's longest backend timeout (35 s rag): a timeout = a hang
RECOVER_WITHIN_S = 60.0
CLEAN_TAIL_S = 30.0
NAMED_END = ("COMPLETE", "PARTIAL", "INCONCLUSIVE", "FAILED")
NS = "ai-engineering-platform"
KCTL = ["kubectl", "--context", "kind-aeoi", "-n", NS]


@dataclass(frozen=True)
class Exp:
    name: str
    fault: tuple[str, ...]
    undo: tuple[str, ...] | None  # None: Kubernetes brings it back (a deleted pod)
    hold_s: int
    expect: str  # what the code/ADRs say should happen - written before the run
    writes: bool = False  # create incidents during the fault (the outbox path)


def _delete(app: str) -> tuple[str, ...]:
    return (*KCTL, "delete", "pod", "-l", f"app.kubernetes.io/name={app}", "--wait=false")


EXPERIMENTS = {
    "pg": Exp("pg", ("docker", "compose", "stop", "postgres"),
              ("docker", "compose", "start", "postgres"), 60,
              "liveness has no dependency -> no restarts; readiness includes the DB, and the "
              "api's readiness includes incident-service -> the api may leave its Service "
              "(connection errors, not 503); back without restarts once the DB returns"),
    "kafka": Exp("kafka", ("docker", "compose", "stop", "kafka"),
                 ("docker", "compose", "start", "kafka"), 60,
                 "producers write the outbox, consumers WAIT (ADR-029): creating incidents "
                 "keeps working; the outboxes drain after Kafka returns; nothing to the DLQ",
                 writes=True),
    "orchestrator": Exp("orchestrator", _delete("orchestrator"), None, 0,
                        "checkpoints + resume RUNNING on start (ADR-019): the investigation "
                        "ends with a named status, the interrupted node re-runs"),
    "agents": Exp("agents", _delete("agents"), None, 0,
                  "an agent task in flight fails or is retried; the investigation ends "
                  "named (PARTIAL at worst), never stuck RUNNING"),
}  # fmt: skip


def judge(
    rows: list[tuple[float, str, float]],
    fault_at: float,
    back_at: float,
    end_at: float,
    investigation: str | None,
    restarts: dict[str, int] | None,
    writes: list[int] | None = None,
    backlog_after: int | None = None,
) -> dict[str, Any]:
    """rows: (t, outcome, latency_s); outcome "200".."599" or an exception class name.
    back_at = when the fault was undone (or the pod deleted). Times in seconds of the run."""

    def ok(o: str) -> bool:
        return o.isdigit() and 200 <= int(o) < 400

    during = [r for r in rows if fault_at <= r[0] < back_at + RECOVER_WITHIN_S]
    after = [r for r in rows if r[0] >= back_at]
    first_ok = next((r[0] for r in after if ok(r[1])), None)
    tail = [r for r in rows if r[0] >= end_at - CLEAN_TAIL_S]
    rules: dict[str, dict[str, Any]] = {
        "no_hang": {"pass": all(r[2] < REQUEST_TIMEOUT_S for r in rows),
                    "max_latency_s": round(max((r[2] for r in rows), default=0), 1)},
        "recovers": {"pass": first_ok is not None and first_ok - back_at <= RECOVER_WITHIN_S
                     and bool(tail) and all(ok(r[1]) for r in tail),
                     "seconds_to_first_ok": None if first_ok is None else round(first_ok - back_at, 1),
                     "clean_tail": bool(tail) and all(ok(r[1]) for r in tail)},
        "no_restarts": {"pass": None if restarts is None else not any(restarts.values()),
                        "restarts": restarts},
        # None = the run had already ended when the fault landed: not exercised, never a PASS
        "investigation_named": {"pass": None if investigation is None else investigation in NAMED_END,
                                "status": investigation},
    }  # fmt: skip
    if writes is not None:
        rules["writes_survive"] = {"pass": bool(writes) and all(200 <= w < 300 for w in writes),
                                   "statuses": sorted(set(writes))}  # fmt: skip
    if backlog_after is not None:
        rules["outbox_drained"] = {"pass": backlog_after == 0, "undelivered": backlog_after}
    outcomes: dict[str, int] = {}
    for r in during:
        outcomes[r[1]] = outcomes.get(r[1], 0) + 1
    verdict = all(v["pass"] for v in rules.values() if v["pass"] is not None)
    return {"pass": verdict, "rules": rules, "during_fault": outcomes}


def restart_delta(
    before: dict[str, int], after: dict[str, int], killed: str | None
) -> dict[str, int]:
    """Container restarts per app between two snapshots. The app whose pod we deleted gets a
    NEW pod (count starts at 0): it is judged by the investigation, not by restarts."""
    return {a: max(0, after.get(a, 0) - before.get(a, 0)) for a in before if a != killed}
