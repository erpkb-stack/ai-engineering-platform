"""Phase 24b (ADR-036): the image-scan gate. Standard library only (CI runs it with the runner's
python3, without uv).

    python3 scripts/scan_gate.py IMAGE=REPORT.json [IMAGE=REPORT.json ...]
        [--exceptions infrastructure/security/scan-exceptions.toml] [--summary FILE]

A report is Trivy's JSON (`trivy image --format json`). The gate FAILS on:
  1. a CRITICAL finding with a fixed version (somewhere) that no exception covers;
  2. an exception that has expired, or that runs longer than MAX_DAYS from today;
  3. an exception that matches nothing (stale: remove it, the list must stay true).
Unfixed CRITICALs and all HIGHs are reported, never gating: nobody can fix them by upgrading.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MAX_DAYS = 120  # an exception is a dated promise, not a permanent waiver


@dataclass(frozen=True)
class Finding:
    image: str
    id: str
    package: str
    installed: str
    fixed: str
    severity: str
    target: str


def findings(image: str, report: dict[str, Any]) -> list[Finding]:
    out = []
    for result in report.get("Results") or []:
        for v in result.get("Vulnerabilities") or []:
            out.append(Finding(
                image=image, id=v["VulnerabilityID"], package=v.get("PkgName", ""),
                installed=v.get("InstalledVersion", ""), fixed=v.get("FixedVersion", "") or "",
                severity=v.get("Severity", "UNKNOWN"), target=result.get("Target", ""),
            ))  # fmt: skip
    return out


def load_exceptions(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    items: list[dict[str, Any]] = tomllib.loads(path.read_text()).get("exception", [])
    for e in items:
        missing = {"id", "package", "image", "expires", "reason"} - set(e)
        if missing:
            raise SystemExit(f"{path.name}: exception {e.get('id', '?')} lacks {sorted(missing)}")
        if not isinstance(e["expires"], date):
            raise SystemExit(f"{path.name}: {e['id']} expires must be a date (YYYY-MM-DD)")
    return items


def _covers(e: dict[str, Any], f: Finding) -> bool:
    return e["id"] == f.id and e["package"] == f.package and e["image"] == f.image


def gate(all_findings: list[Finding], exceptions: list[dict[str, Any]], today: date) -> list[str]:
    """The problems that fail the gate (empty = pass)."""
    problems = []
    for e in exceptions:
        if e["expires"] < today:
            problems.append(f"exception {e['id']} ({e['package']}) EXPIRED on {e['expires']}")
        elif (e["expires"] - today).days > MAX_DAYS:
            problems.append(f"exception {e['id']} runs to {e['expires']}: max {MAX_DAYS} days")
        if not any(_covers(e, f) for f in all_findings):
            problems.append(f"exception {e['id']} ({e['package']}) matches nothing: remove it")
    live = [e for e in exceptions if e["expires"] >= today]
    for f in all_findings:
        if f.severity == "CRITICAL" and f.fixed and not any(_covers(e, f) for e in live):
            problems.append(f"{f.image}: {f.id} {f.package} {f.installed} -> fixed in {f.fixed}")
    return sorted(set(problems))


def summary(all_findings: list[Finding], exceptions: list[dict[str, Any]]) -> str:
    rows = ["| image | CRITICAL | fixable CRITICAL | excepted | HIGH |", "|---|---|---|---|---|"]
    for image in sorted({f.image for f in all_findings} | {e["image"] for e in exceptions}):
        mine = [f for f in all_findings if f.image == image]
        crit = [f for f in mine if f.severity == "CRITICAL"]
        fixable = [f for f in crit if f.fixed]
        excepted = [f for f in fixable if any(_covers(e, f) for e in exceptions)]
        high = [f for f in mine if f.severity == "HIGH"]
        rows.append(f"| {image} | {len(crit)} | {len(fixable)} | {len(excepted)} | {len(high)} |")
    return "\n".join(rows) + "\n"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("reports", nargs="+", help="IMAGE=REPORT.json (IMAGE without the tag)")
    ap.add_argument("--exceptions", type=Path,
                    default=ROOT / "infrastructure/security/scan-exceptions.toml")  # fmt: skip
    ap.add_argument("--summary", type=Path, help="append the markdown table here")
    ap.add_argument("--today", type=date.fromisoformat, default=date.today())
    args = ap.parse_args(argv)
    all_findings: list[Finding] = []
    for spec in args.reports:
        image, _, path = spec.partition("=")
        all_findings += findings(image, json.loads(Path(path).read_text()))
    exceptions = load_exceptions(args.exceptions)
    table = summary(all_findings, exceptions)
    print(table)
    if args.summary:
        with args.summary.open("a") as fh:
            fh.write(table)
    problems = gate(all_findings, exceptions, args.today)
    for p in problems:
        print(f"GATE: {p}")
    print("scan gate: FAILED" if problems else "scan gate: passed")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
