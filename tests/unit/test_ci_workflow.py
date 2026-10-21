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


def test_no_action_is_pinned_to_a_branch() -> None:
    for wf in WORKFLOWS:
        for ref in re.findall(r"uses:\s*\S+@(\S+)", wf.read_text()):
            assert ref not in {"main", "master", "latest"}, f"{wf.name}: @{ref}"
