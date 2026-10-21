# ADR-036: CI checks everything on every PR; images built and scanned, not pushed or deployed

Status: Proposed
Date: 2026-10-21
Refines: architecture.md §23 (CI/CD), C9; ADR-033 (images)

## Context
- CI was **red from 2026-10-08 (PR #8, Phase 9) to 2026-10-21** and 22 PRs were merged on red.
  Two causes, both found only in Phase 24: the 0026 downgrade dropped a server-wide role (29
  integration failures), and the agent smoke never started the orchestrator Phase 9 added.
  Nothing enforced "green before merge".
- CI did not run the Java tests, the console checks, or build the images. Those ran only on
  the Mac, by hand.
- Trivy (the planned scanner) was compromised in March 2026: release v0.69.4, 75-76 of the 77
  `trivy-action` tags, `setup-trivy` tags, Docker Hub images 0.69.5 and 0.69.6. Aqua's advice:
  pin by full commit SHA.
- Owner decision (2026-10-21): "CI catch-up + images". No GHCR push, no deploy, no environments.

## Decision
- **Every PR runs 6 jobs:** quality, gitleaks, infra-smoke (as before), `catalog`
  (`make up PROFILE=infra` + `make catalog-build` = `mvn verify` against the compose Postgres),
  `frontend` (`make ui-install` + `make ui-check`), `images` (`make images` + a Trivy scan).
  CI runs the SAME make targets as the Mac, so "works on the Mac" and "green in CI" test the
  same thing.
- **Trivy = the v0.69.3 release binary, pinned by SHA-256 in the workflow**, checked before it
  is unpacked. No Trivy action, no Trivy image. The hash
  (`1816b632…88b75`, Linux-64bit tar.gz) was checked against the release's Sigstore bundle:
  signed 2026-03-03 by `aquasecurity/trivy`'s own release workflow at tag v0.69.3 (before the
  compromise), Rekor log index 1017902085. A version change = a new hash checked the same way.
- **The scan reports, it does not gate yet.** HIGH + CRITICAL, table in the log, counts in the
  job summary. A gate set before anyone has seen the baseline would either block every PR or
  be loosened until it means nothing. The gate is decided after the first baseline is reviewed.
- **Rule: merge only when every check is green** (`gh pr checks`). A unit test
  (`tests/unit/test_ci_workflow.py`) guards the jobs, the read-only permissions, the Trivy pin
  and "no Trivy action/image".

## Not done (and why)
- **No push to GHCR, no kind-in-runner deploy, no dev/staging/prod environments**: owner
  decision. Nothing consumes a pushed image yet (the Mac builds its own; kind loads local
  images). [Prod] push by digest, sign (cosign), deploy by digest with environment approvals.
- **Third-party and first-party actions are pinned by major tag, not SHA** (`actions/*`,
  `astral-sh/setup-uv`, `gitleaks/gitleaks-action`). The sandbox cannot reach the GitHub API to
  read and verify SHAs, and an unverified SHA is worse than a tag. [Prod] SHA-pin all actions
  with Dependabot updates. Recorded as a known gap.
- **Base images are pinned by tag, not digest** (`.claude/rules/infrastructure.md` asks for
  digests in CI). Same reason; same gap.
- **No arm64 build** (the rules ask for an amd64 + arm64 matrix): it doubles the image job's
  time for a platform nobody runs here.
- **No path filters**: every job runs on every PR. Simpler; the cost is CI minutes, not time
  on the Mac.

## Tradeoffs
- Two more jobs start Docker (catalog, images): more CI minutes per PR, in parallel.
- A report-only scan can be ignored. It is a step towards a gate, not a control yet.
- A pinned scanner gets no fixes by itself; updating it is a manual, reviewed change.

## Prototype / Production / Enterprise-scale
- Prototype (this): checks on every PR, images built and scanned, nothing published.
- Production: push signed images by digest, SBOM, gated scan, deploy on main with approvals.
- Enterprise: org-wide required checks, action allow-list, provenance (SLSA), private mirrors.
