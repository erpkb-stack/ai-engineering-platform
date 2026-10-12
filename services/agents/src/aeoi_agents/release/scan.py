"""`make risk-scan` (ADR-025): the release-risk rule over EVERY synthetic deploy, offline.

It answers ONE question: is the rule selective - or would it flag most releases (alert
fatigue, so people stop reading it)? It is NOT an accuracy measurement: the synthetic history
has one planted bad deploy, so "it found the planted one" proves the plumbing, not skill.

Same code as the agent: `scan_commit` (diff rules) and `rubric.assess` (factors, band). The
inputs are rebuilt from the generated dataset the way the tools return them: compare_commits'
range (previous SUCCEEDED deploy -> this one, head first, max 10), the gateway's 4000-char
cut, the catalog's dependents, and references at HEAD (simple names + "uses the type", as rag).
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aeoi_codeparse as cp

from aeoi_agents.code.agent import scan_commit
from aeoi_agents.release.rubric import Hit, assess, findings_band, flag_keys, rollback_plan
from aeoi_models.api.release import RiskFactor, RollbackStep

MAX_COMMITS = 10
MAX_CHARS = 4000


def _consumers(repos: dict[str, dict[str, str]]) -> dict[str, dict[str, set[str]]]:
    """service -> name -> files: every reference at HEAD (what `make code-index` stores)."""
    out: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for files in repos.values():
        for path, text in files.items():
            lang = cp.language_of(path)
            parts = path.split("/")
            if lang is None or len(parts) < 3 or parts[0] != "services":
                continue
            for r in cp.parse(lang, text).refs:
                out[parts[1]][r.name].add(path)
    return out


def _readers(symbol: str, owner: str, refs: dict[str, dict[str, set[str]]]) -> list[str]:
    typ, _, member = symbol.rpartition(".")
    names = cp.accessor_names(member) if typ else {member}
    return sorted(svc for svc, used in refs.items()
                  if svc != owner and (not typ or typ in used) and names & used.keys())  # fmt: skip


@dataclass
class World:
    """The generated dataset, indexed the way the tools see it."""

    commits: dict[str, dict[str, Any]]
    diffs: dict[str, list[dict[str, Any]]]
    by_service: dict[str, list[dict[str, Any]]]
    catalog: dict[str, dict[str, Any]]
    dependents: dict[str, list[str]]
    refs: dict[str, dict[str, set[str]]]
    deploys: list[dict[str, Any]]


def world(seed: int = 42) -> World:
    from aeoi_synth.generate import generate  # dev-only dependency: the workspace venv has it

    ds = generate(seed)
    t = ds.tables
    diffs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for d in t["devdata.commit_diffs"]:
        cut = len(d["patch"]) > MAX_CHARS or len(d["content_after"]) > MAX_CHARS
        diffs[d["sha"]].append({"path": d["path"], "patch": "" if cut else d["patch"],
                                "content_after": "" if cut else d["content_after"],
                                "truncated": cut})  # fmt: skip
    by_service: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in t["devdata.commits"]:
        by_service[c["service_key"]].append(c)
    dependents: dict[str, list[str]] = defaultdict(list)
    for s in ds.catalog:
        for dep in s.get("depends_on", []):
            dependents[dep].append(s["key"])
    return World({c["sha"]: c for c in t["devdata.commits"]}, diffs, by_service,
                 {s["key"]: s for s in ds.catalog}, dependents, _consumers(ds.code.repos),
                 sorted(t["devdata.deployments"], key=lambda d: d["started_at"]))  # fmt: skip


@dataclass
class Assessed:
    deploy: dict[str, Any]
    factors: list[RiskFactor]
    band: str
    findings_band: str
    rollback: list[RollbackStep]


def assess_deploy(w: World, dep: dict[str, Any]) -> Assessed:
    """One deploy, exactly as the agent sees it through the tools."""
    svc = dep["service_key"]
    prev = [p for p in w.deploys if p["service_key"] == svc and p["status"] == "SUCCEEDED"
            and p["environment"] == dep["environment"] and p["started_at"] < dep["started_at"]]  # fmt: skip
    base = w.commits.get(prev[-1]["commit_sha"]) if prev else None
    head = w.commits[dep["commit_sha"]]
    config = sorted(k for k, v in (dep.get("config_diff") or {}).items() if v[0] != v[1])
    not_checked: list[str] = []
    if base is not None and (
        head["sha"] == base["sha"] or head["committed_at"] < base["committed_at"]
    ):
        f = [RiskFactor(kind="nothing_shipped", severity="context", statement="no new code ships")]
        if config:
            f.append(RiskFactor(kind="config_change", severity="context", statement="config"))
        return Assessed(dep, f, "LOW", "LOW", rollback_plan([], flag_keys(config), [], undo="-",
                                                             code_shipped=False))  # fmt: skip
    if base is None:
        rng = [head]
        not_checked.append("no previous successful deploy")
    else:
        rng = sorted((c for c in w.by_service[svc] if c["sha"] == head["sha"] or (
            base["committed_at"] < c["committed_at"] <= head["committed_at"]
            and c["sha"] != base["sha"])),
            key=lambda c: (c["sha"] != head["sha"], -c["committed_at"].timestamp()))  # fmt: skip
        if len(rng) > MAX_COMMITS:
            not_checked.append(f"more than {MAX_COMMITS} commits")
            rng = rng[:MAX_COMMITS]
    hits: list[Hit] = []
    contents: list[tuple[str, str]] = []
    for c in rng:
        item = {"sha": c["sha"], "files": w.diffs[c["sha"]]}
        found, skipped, _ = scan_commit(item)
        not_checked.extend(skipped)
        contents.extend((f["path"], f["content_after"]) for f in item["files"])
        for fh in found:
            h = Hit(rule=fh.hit.rule, detail=fh.hit.detail, symbol=fh.symbol, where=fh.symbol,
                    guard=fh.hit.guard, evidence_id="COMMIT-scan", file=fh.path)  # fmt: skip
            if h.rule == "field_removed":
                h.consumers = _readers(fh.symbol, svc, w.refs)
            hits.append(h)
    factors, band, rollback = assess(
        hits, service_key=svc, tier=w.catalog[svc]["tier"], dependents=w.dependents[svc],
        catalog_evidence=None, config_keys=config, deploy_evidence=None,
        not_checked=not_checked, contents=contents, undo="a rollback",
    )  # fmt: skip
    return Assessed(dep, factors, band, findings_band(factors), rollback)


def check_cases(w: World, path: Path) -> list[str]:
    """The hand-labelled cases (data/eval/release_risk_cases_v1.jsonl): a list of failures."""
    by_key = {d["deploy_key"]: d for d in w.deploys}
    bad: list[str] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        a = assess_deploy(w, by_key[case["deploy_key"]])
        got = {"band": a.band, "findings_band": a.findings_band,
               "kinds": sorted({f.kind for f in a.factors if f.severity != "context"}),
               "rollback": [r.kind for r in a.rollback]}  # fmt: skip
        for k, want in case["expect"].items():
            if got[k] != want:
                bad.append(f"{case['deploy_key']} ({case['why']}): {k} = {got[k]}, expected {want}")
    return bad


def main() -> int:
    from aeoi_db.config import find_repo_root

    root = find_repo_root()
    w = world()
    bands: Counter[str] = Counter()
    found: Counter[str] = Counter()
    flagged: list[dict[str, Any]] = []
    for dep in w.deploys:
        a = assess_deploy(w, dep)
        bands[a.band] += 1
        found[a.findings_band] += 1
        if a.band != "LOW":
            flagged.append({"deploy": dep["deploy_key"], "service": dep["service_key"],
                            "band": a.band, "findings_band": a.findings_band,
                            "status": dep["status"],
                            "factors": [f"{f.severity} {f.kind}: {f.statement[:110]}"
                                        for f in a.factors if f.severity != "context"]})  # fmt: skip
    total = sum(bands.values())
    cases = root / "data" / "eval" / "release_risk_cases_v1.jsonl"
    failures = check_cases(w, cases)
    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "question": "selectivity (how often the rule flags a release), NOT accuracy",
        "deploys": total, "bands": dict(sorted(bands.items())),
        # what the rule FOUND in the code that was checked, apart from "could not check"
        "findings_bands": dict(sorted(found.items())),
        "flagged_share": round((total - bands["LOW"]) / total, 4) if total else 0.0,
        "flagged_by_code": sum(1 for x in flagged if x["findings_band"] != "LOW"),
        "flagged_only_unchecked": sum(1 for x in flagged if x["findings_band"] == "LOW"),
        "labelled_cases": str(cases.relative_to(root)), "labelled_failures": failures,
        "flagged": flagged,
    }  # fmt: skip
    print(f"{total} synthetic deploys - advice: " + ", ".join(
        f"{b} {n} ({n / total:.1%})" for b, n in sorted(bands.items())))  # fmt: skip
    print("  risk FOUND in checked code: " + ", ".join(
        f"{b} {n}" for b, n in sorted(found.items())))  # fmt: skip
    print(f"  flagged by a code finding: {report['flagged_by_code']} · flagged ONLY because part "
          f"could not be checked: {report['flagged_only_unchecked']}")  # fmt: skip
    for x in sorted(flagged, key=lambda x: x["findings_band"] == "LOW")[:12]:
        print(f"  {x['band']:<6} {x['deploy']:<12} {x['service']:<24} {x['status']:<11} "
              f"{' | '.join(x['factors'])[:140]}")  # fmt: skip
    if len(flagged) > 12:
        print(f"  ... {len(flagged) - 12} more in the results file")
    print(f"labelled cases ({report['labelled_cases']}): "
          + ("all as labelled" if not failures else f"{len(failures)} FAILED"))  # fmt: skip
    for f in failures:
        print(f"  ✘ {f}")
    out = root / "data" / "eval" / "results"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"risk-scan-{datetime.now(UTC):%Y-%m-%dT%H%M%S}.json"
    path.write_text(json.dumps(report, indent=1, default=str) + "\n")
    print(f"written: {path.relative_to(root)}")
    print("Read it as: how often the rule would flag a release on SYNTHETIC history, whose only "
          "risky code is planted. Not an accuracy measurement.")  # fmt: skip
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
