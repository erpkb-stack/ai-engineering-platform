"""Phase 24b (ADR-036): the image-scan gate fails on what it claims, and only on that."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
TODAY = date(2026, 10, 21)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("scan_gate", ROOT / "scripts/scan_gate.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scan_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


def _vuln(vid: str, pkg: str, sev: str, fixed: str = "") -> dict[str, Any]:
    return {"VulnerabilityID": vid, "PkgName": pkg, "InstalledVersion": "1", "FixedVersion": fixed,
            "Severity": sev}  # fmt: skip


def _report(*vulns: dict[str, Any]) -> dict[str, Any]:
    return {"Results": [{"Target": "t", "Vulnerabilities": list(vulns)}, {"Target": "empty"}]}


def _run(tmp: Path, reports: dict[str, dict[str, Any]], exceptions: str) -> tuple[int, str]:
    sg = _load()
    args = []
    for image, rep in reports.items():
        (tmp / f"{image}.json").write_text(json.dumps(rep))
        args.append(f"{image}={tmp / f'{image}.json'}")
    (tmp / "exc.toml").write_text(exceptions)
    summary = tmp / "summary.md"
    rc = sg.main([*args, "--exceptions", str(tmp / "exc.toml"), "--summary", str(summary),
                  "--today", TODAY.isoformat()])  # fmt: skip
    return rc, summary.read_text()


EXC = """
[[exception]]
id = "CVE-3"
package = "webmvc"
image = "cat"
expires = {expires}
reason = "fix only in the next major"
"""


def test_unfixed_critical_and_high_do_not_gate(tmp_path: Path) -> None:
    rep = _report(_vuln("CVE-1", "zlib", "CRITICAL"), _vuln("CVE-2", "libx", "HIGH", "2"))
    rc, summary = _run(tmp_path, {"py": rep}, "")
    assert rc == 0
    assert "| py | 1 | 0 | 0 | 1 |" in summary


def test_a_fixable_critical_fails_and_names_the_fix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, _ = _run(tmp_path, {"py": _report(_vuln("CVE-9", "certifi", "CRITICAL", "2026.10.19"))}, "")
    assert rc == 1
    assert "py: CVE-9 certifi 1 -> fixed in 2026.10.19" in capsys.readouterr().out


def test_a_live_exception_covers_exactly_its_finding(tmp_path: Path) -> None:
    rep = _report(_vuln("CVE-3", "webmvc", "CRITICAL", "7.0.9"))
    rc, summary = _run(tmp_path, {"cat": rep}, EXC.format(expires="2026-12-31"))
    assert rc == 0 and "| cat | 1 | 1 | 1 | 0 |" in summary
    # the same CVE in ANOTHER image or package is not covered
    rc, _ = _run(tmp_path, {"cat": rep, "py": rep}, EXC.format(expires="2026-12-31"))
    assert rc == 1


@pytest.mark.parametrize(
    ("expires", "message"),
    [("2026-10-20", "EXPIRED"), ("2027-06-01", "max 120 days")],
)
def test_an_expired_or_too_long_exception_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], expires: str, message: str
) -> None:
    rep = _report(_vuln("CVE-3", "webmvc", "CRITICAL", "7.0.9"))
    rc, _ = _run(tmp_path, {"cat": rep}, EXC.format(expires=expires))
    assert rc == 1 and message in capsys.readouterr().out


def test_a_stale_exception_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, _ = _run(tmp_path, {"cat": _report()}, EXC.format(expires="2026-12-31"))
    assert rc == 1 and "matches nothing" in capsys.readouterr().out


def test_an_exception_without_a_reason_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="lacks"):
        _run(tmp_path, {"cat": _report()}, 'exception = [{id = "x", package = "p", image = "i"}]')


def test_the_committed_exceptions_file_is_valid_and_within_policy() -> None:
    sg = _load()
    items = sg.load_exceptions(ROOT / "infrastructure/security/scan-exceptions.toml")
    assert items, "the file exists for a reason; delete it when empty"
    for e in items:
        assert len(e["reason"].split()) >= 8, f"{e['id']}: say WHY, not just 'accepted'"
