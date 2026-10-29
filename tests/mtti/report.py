"""Phase 29e (ADR-047): `make mtti FILES="<v1 result> <v2 result>"` - the MTTI report.

Joins your manual session (data/mtti/manual-session.json) with the AI's eval results on REAL
models (the two `make eval-investigations COST=1` files of the CURRENT code), scores both by
the rules in mttilib (fixed before timing), reveals the planted answers, and writes
data/mtti/results/mtti-<UTC>.json. A file from fake models is refused: fakes never validate.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import mttilib as m  # noqa: E402
from manual import SESSION, load_cases  # noqa: E402


def ai_cases(files: list[Path]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    out: dict[str, dict[str, Any]] = {}
    runs = []
    for f in files:
        d = json.loads(f.read_text())
        paid = (d.get("config") or {}).get("paid_models") or []
        if not paid:
            raise SystemExit(f"{f.name}: fake models (no paid model) - MTTI needs a real-model run")
        runs.append({"file": f.name, "run_id": d.get("run_id"), "started_at": d.get("started_at"),
                     "version": (d.get("config") or {}).get("version"), "paid_models": paid})  # fmt: skip
        out.update({c["case"]: c for c in d["cases"]})
    return out, runs


def main(argv: list[str]) -> int:
    invalid = None
    if "--manual-invalid" in argv:  # the owner's declaration, with its reason (ADR-047)
        i = argv.index("--manual-invalid")
        invalid = (argv[i + 1] if i + 1 < len(argv) else "").strip()
        if len(invalid) < 20:
            raise SystemExit("--manual-invalid needs the reason (20+ characters)")
        argv = argv[:i] + argv[i + 2 :]
    files = [Path(a) for a in argv]
    if not files or not SESSION.exists():
        raise SystemExit('usage: make mtti FILES="<v1 result> <v2 result>" (after make manual)')
    cases = {c["key"]: c for c in load_cases()}
    truth = {k: c["expected"]["culprit"] for k, c in cases.items()}
    session = json.loads(SESSION.read_text())
    if session.get("current"):
        raise SystemExit("a manual case is still open: finish it (make manual) first")
    manual = [{**c, "culprit": truth[c["case"]],
               "services": cases[c["case"]]["input"]["affected_services"]}
              for c in session["done"]]  # fmt: skip
    ai, runs = ai_cases(files)
    rep = m.report(manual, ai, invalid)
    out = {"written_at": datetime.now(UTC).isoformat(timespec="seconds"), "ai_runs": runs,
           "manual_session": {"seed": session["seed"], "order": session["order"]},
           "answers": [{"case": c["case"], "answer": c.get("answer"), "planted": c["culprit"],
                        "abandoned": c.get("abandoned")} for c in manual], **rep}  # fmt: skip
    p = ROOT / "data/mtti/results" / f"mtti-{datetime.now(UTC):%Y-%m-%dT%H%M%S}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=1) + "\n")
    print(f"{'case':<26} {'manual':>9} {'ok':>3} {'calls':>5} {'metr':>4} {'off':>3} {'AI':>7} "
          f"{'ok':>3}  your answer -> planted   (metr = metrics calls, off = other services)")  # fmt: skip
    for r, a in zip(rep["cases"], out["answers"], strict=True):
        if "abandoned" in r:
            print(f"{r['case']:<26} abandoned: {r['abandoned']}")
            continue
        ans = f"{a['answer']['kind']} {a['answer']['subject']}" if a["answer"] else "-"
        print(f"{r['case']:<26} {r['manual_s']:>8.0f}s {'Y' if r['manual_identified'] else 'n':>3}"
              f" {r['tool_calls']:>5} {r['metrics_calls']:>4} {r['off_alert_calls']:>3}"
              f" {r['ai_s'] or 0:>6.0f}s {'Y' if r['ai_identified'] else 'n':>3}"
              f"  {ans} -> {a['planted']['kind']} {a['planted']['subject']}")  # fmt: skip
    a_all, man, pr = rep["ai_all"], rep["manual"], rep["paired"]
    print(f"\nAI, all {a_all['cases']} planted cases: identified {a_all['identified']} "
          f"(Wilson 95% {a_all['identified_wilson95']}), seconds when identified "
          f"{a_all['seconds_when_identified']}; correct but not validated: "
          f"{a_all['correct_not_validated']}")  # fmt: skip
    print(f"Manual, {man['cases']} cases: identified {man['identified']} (Wilson 95% "
          f"{man['identified_wilson95']}), seconds {man['seconds_when_identified']}")  # fmt: skip
    print(f"Paired ({pr['pairs']}): AI first {pr['ai_first']}, manual first {pr['manual_first']}, "
          f"sign test p={pr['sign_test_p']}; manual/AI time when both identified "
          f"{pr['ratio_manual_over_ai_when_both']}")  # fmt: skip
    if invalid:
        print(f"\nMANUAL BASELINE MARKED INVALID by the owner: {invalid}\n"
              "Only the AI line above may be quoted; the manual and paired lines are kept as a "
              "record, never as a comparison.")  # fmt: skip
    print(f"written: {p.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
