"""Phase 26 (ADR-038): the threat model is an index of REAL tests, checked - it cannot rot.

Every `path::name` in docs/security/threat-model.md must point at a test that exists: a
Python `def test_name` / `async def test_name`, or (frontend) an `it("name"` in that file.
A threat row without a test must say it is a gap."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "docs/security/threat-model.md"
REF = re.compile(r"`([\w./-]+\.(?:py|tsx|ts))::([^`]+)`")


def _rows() -> list[str]:
    return [
        line for line in MODEL.read_text().splitlines() if re.match(r"\| T-[A-Z]+-\d+ \|", line)
    ]


def test_every_test_the_threat_model_names_exists() -> None:
    refs = REF.findall(MODEL.read_text())
    assert len(refs) >= 30, "the model should cite the suites it relies on"
    missing = []
    for path, name in refs:
        f = ROOT / path
        if not f.is_file():
            missing.append(f"{path} (no such file)")
            continue
        text = f.read_text()
        if path.endswith(".py"):
            ok = re.search(rf"^\s*(async\s+)?def {re.escape(name)}\(", text, re.M)
        else:
            ok = re.search(rf"\bit\(\s*[\"']{re.escape(name)}[\"']", text)
        if not ok:
            missing.append(f"{path}::{name}")
    assert not missing, missing


def test_every_threat_has_a_test_or_says_it_is_a_gap() -> None:
    rows = _rows()
    assert len(rows) >= 15
    ids = [r.split("|")[1].strip() for r in rows]
    assert len(ids) == len(set(ids)), "threat ids must be unique"
    for r in rows:
        assert REF.search(r) or "gap" in r.split("|")[-2], r[:60]
