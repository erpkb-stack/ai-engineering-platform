"""Phase 24 (ADR-036): the CI workflow keeps the guarantees we rely on.

CI was red from 2026-10-08 to 2026-10-21 and nobody noticed. These checks are cheap rules that
a later edit could silently break: every job we promised exists, nothing in CI pulls Trivy
through an action or a mutable image (the March 2026 compromise), and the Trivy binary is
pinned by a full SHA-256.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((ROOT / ".github/workflows").glob("*.y*ml"))


def _ci() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    return data


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = job["steps"]
    return steps


def test_every_promised_job_exists_with_a_timeout() -> None:
    jobs = _ci()["jobs"]
    assert set(jobs) >= {"quality", "secrets-scan", "infra-smoke", "catalog", "frontend", "images"}
    for name, job in jobs.items():
        assert job.get("timeout-minutes"), f"{name}: no timeout"


def test_the_new_jobs_run_the_same_make_targets_as_the_mac() -> None:
    jobs = _ci()["jobs"]

    def runs(job: str) -> str:
        return "\n".join(str(s.get("run", "")) for s in _steps(jobs[job]))

    assert "make catalog-build" in runs("catalog")
    assert "surefire-reports" in runs("catalog")  # mvn -q hides the count: 0 tests = red
    assert "make ui-install" in runs("frontend") and "make ui-check" in runs("frontend")
    assert "make images" in runs("images")


def test_workflow_permissions_stay_read_only() -> None:
    assert _ci()["permissions"] == {"contents": "read"}


def test_no_workflow_uses_a_trivy_action_or_image() -> None:
    for wf in WORKFLOWS:
        text = wf.read_text()
        assert "aquasecurity/" not in re.sub(r"https://github\.com/aquasecurity/trivy/releases/"
                                             r"download/\S+", "", text), wf.name  # fmt: skip
        assert "aquasec/trivy" not in text, wf.name


def test_trivy_is_a_pinned_binary_checked_by_sha256() -> None:
    env = _ci()["jobs"]["images"]["env"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", env["TRIVY_VERSION"])
    assert re.fullmatch(r"[0-9a-f]{64}", env["TRIVY_SHA256"])
    install = "\n".join(str(s.get("run", "")) for s in _steps(_ci()["jobs"]["images"]))
    assert "sha256sum -c -" in install and "${TRIVY_SHA256}" in install
    # the hash is checked BEFORE the archive is unpacked
    assert install.index("sha256sum -c -") < install.index("tar -xzf trivy.tgz")


def test_every_action_is_pinned_by_full_commit_sha() -> None:
    # 24b: a tag can be moved (the Trivy attack moved 75 of 77); a commit SHA cannot.
    for wf in WORKFLOWS:
        for ref in re.findall(r"uses:\s*(\S+)", wf.read_text()):
            if ref.startswith("./"):
                continue
            assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref), f"{wf.name}: {ref}"


def test_runners_are_pinned_not_latest() -> None:
    for wf in WORKFLOWS:
        for runner in re.findall(r"runs-on:\s*(\S+)", wf.read_text()):
            assert not runner.endswith("-latest"), f"{wf.name}: {runner} moves without a commit"


def test_the_images_job_gates_on_the_scan() -> None:
    install = "\n".join(str(s.get("run", "")) for s in _steps(_ci()["jobs"]["images"]))
    assert "scripts/scan_gate.py" in install
    assert install.index('trivy" image') < install.index("scripts/scan_gate.py")


def test_the_investigation_eval_runs_with_its_regression_baseline() -> None:
    # Phase 25a (ADR-037): the eval in CI is a gate, not a report - and its baseline exists
    steps = _steps(_ci()["jobs"]["infra-smoke"])
    run = "\n".join(str(s.get("run", "")) for s in steps)
    assert "make eval-seed" in run and "make eval-investigations BASELINE=" in run
    bases = re.findall(r"BASELINE=(\S+)", run)
    assert bases and all((ROOT / b).is_file() for b in bases)
    from aeoi_synth import scenarios  # every pack is gated: v1 and v2 (flaw (c))

    for v in scenarios.VERSIONS:
        assert f"investigations-v{v}-fake.json" in run
    assert run.index("make db-seed") < run.index("make eval-seed")  # db-seed truncates devdata
